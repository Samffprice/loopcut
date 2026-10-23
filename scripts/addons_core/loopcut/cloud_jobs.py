"""Managed jobs share BYOK's result envelope and Blender result handlers.

Scene access stays in resolve_files() and apply_result(). A worker snapshots HTTP settings on
main, then uploads, follows events and verifies downloads without scene access. The small
registry is process-local; cloud_account persists submission keys for recovery after a restart.
"""

import copy
import hashlib
import json
import math
import mimetypes
import re
import shutil
import tempfile
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

from . import blobs, cloud_http

TERMINAL = {"succeeded", "failed", "canceled"}
STATES = {"queued", "running"} | TERMINAL
OUTPUT_TYPES = {"image", "model_3d", "material", "audio", "video", "render_frames", "report", "motion"}
POLL_SECONDS = .25
STREAM_TIMEOUT = 2
STREAM_ATTEMPTS = 2
MAX_EVENT = 4 * 1024 * 1024
MAX_REPORT = 64 * 1024
MAX_OUTPUTS = 4096
MAX_DOWNLOAD = 2 * 1024 * 1024 * 1024
MAX_REPORT_FILE = 4 * 1024 * 1024
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_JOB_ID = re.compile(r"job_[A-Za-z0-9_-]{1,128}\Z")
FBX_MAGIC = b"Kaydara FBX Binary"
_SUFFIXES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
             "model/gltf-binary": ".glb", "model/gltf+json": ".gltf", "model/vnd.fbx": ".fbx",
             "audio/wav": ".wav", "audio/mpeg": ".mp3", "video/mp4": ".mp4", "video/webm": ".webm",
             "application/json": ".json", "text/plain": ".txt", "text/markdown": ".md", "application/x-bvh": ".bvh"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".webm"}  # video/mp4, video/quicktime, video/webm (motion_from_video).
MAX_CAPTURE_SECONDS = 60  # Of video one motion capture takes (contract 0.7, SPEC §3.2 Motion).
MAX_MOTION_BYTES = 64 * 1024 * 1024
_registry = {}
_lock = threading.Lock()


def _invalid(message="Invalid job response from Loopcut"):
    return cloud_http.CloudError(message, "invalid_response")


def _input_error(message, field):
    return cloud_http.CloudError(message, "invalid_input", 422, {"field": field})


def _main_only():
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("This operation must run on Blender's main thread")


def _client():
    def snapshot():
        client = cloud_http.Client(client_headers=cloud_http.contract_headers())
        client.api_key = cloud_http.credentials.api_key(f"{client.base_url}/v1")
        return client
    if threading.current_thread() is threading.main_thread():
        return snapshot()
    from . import mainthread
    return mainthread.run_on_main(snapshot).result()


def _blob_multipart():
    """The server's multipart capability, so large tool files (e.g. a 150 MB GLB) go in parts."""
    from . import cloud
    return cloud.blob_multipart()


def _redraw():
    from . import mainthread
    mainthread.request_redraw()


def progress_rows(registry):
    """Pure panel data, with no Events, locks, Blender or mutable registry values exposed. A running job can be
    canceled and the service returns its whole credit hold (C47); once it has finished and only its outputs are
    downloading, it was charged and can no longer be canceled, and the row says so."""
    rows = []
    for key, row in registry.items():
        job = row["job"]
        downloading = job["status"] == "succeeded" and not row.get("ready", True)
        if row.get("error") or (job["status"] in TERMINAL and not downloading):
            continue
        message = "Downloading outputs" if downloading else job["progress"]["message"]
        held = (job.get("credits") or {}).get("reserved", 0) if not downloading else 0
        note = ("Finished and charged; it can't be canceled now." if downloading
                else f"{held:,} credits held. Cancel returns them." if held else "")
        rows.append({"id": key, "title": row["title"], "pct": job["progress"]["pct"],
                     "message": "Canceling…" if row.get("canceling") else message,
                     "cancelable": not row.get("canceling", False) and not downloading,
                     "note": "" if row.get("canceling") else note})
    from .cloud import safe_value
    return safe_value(rows)


def ui_rows():
    with _lock:
        return progress_rows(_registry)


def cancel_job(job_id):
    """False when the job is unknown or already over. A finished job still downloading only stops its download:
    it was charged, so the panel offers no Cancel then (progress_rows)."""
    with _lock:
        row = _registry.get(job_id)
        if row is None or row.get("error") or (row["job"]["status"] in TERMINAL and row.get("ready", True)):
            return False
        row["cancel"].set()
        row["canceling"] = True
    _redraw()
    return True


def watch(job_id, fn):
    """fn(job, error) once the job is over here (outputs ready, or an error such as a cancel); at once when it
    already is. cloud.py settles the chat's spend with it (C47). False for an unknown job."""
    with _lock:
        row = _registry.get(job_id)
        if row is None:
            return False
        if row.get("error") is None and not (row["job"]["status"] in TERMINAL and row.get("ready", True)):
            row.setdefault("watchers", []).append(fn)
            return True
        job, error = copy.deepcopy(row["job"]), row.get("error")
    _notify([fn], job, error)
    return True


def _notify(watchers, job, error):
    for fn in watchers:
        try:
            fn(job, error)
        except Exception as ex:  # A card's bookkeeping must never break a job.
            print(f"Loopcut: could not settle a cloud job's charge: {ex!r}")


def _final(job_id, job, error=None):
    with _lock:
        row = _registry.get(job_id)
        watchers = row.pop("watchers", []) if row is not None else []
    _notify(watchers, copy.deepcopy(job), error)


def cancel_all():
    """Stop both blocking and detached jobs when the add-on is disabled."""
    with _lock:
        ids = list(_registry)
    for job_id in ids:
        cancel_job(job_id)


def job_status(job_id):
    """Long tools return immediately; their eventual local paths are available here."""
    with _lock:
        row = _registry.get(job_id)
        if row is None:
            raise cloud_http.CloudError("Cloud job is not available in this session", "not_found", 404)
        if row.get("error"):
            raise row["error"]
        job = copy.deepcopy(row["job"])
        incomplete = job['status'] in TERMINAL and not row.get('ready', True)
        if incomplete and (job['status'] == 'succeeded' or not row.get('final', True)):
            job.update(status="running", progress={"pct": 100, "message": "Downloading outputs"})
        _raise_failed(job)  # A done event can precede _finish's stored exception.
        from .cloud import safe_value
        return safe_value({**job, 'outputs': [{k: v for k, v in output.items() if k not in {'url', 'expires_at'}}
                                             for output in job['outputs']]})


def _remember(job, title, cancel, inputs=None, apply_args=None, references=None):
    """inputs: what the call asked for, kept for its result's names (_add_audio): a job does not carry it.
    apply_args: the call's client-only arguments for the apply handler (contract 0.7), never sent.
    references: the scene references its files came from (a clip's name), for names only."""
    with _lock:
        if job["id"] in _registry:
            row = _registry[job['id']]
            error = row.get('error')
            if error is None or error.code not in {'connection_failed', 'output_save_failed', 'hash_mismatch'}:
                return False  # Preserve live workers and definitive outcomes on replay.
            del _registry[job['id']]
        # Keep a bounded history for job_status, retaining every running job.
        finished = [key for key, row in _registry.items()
                    if (row["job"]["status"] in TERMINAL and row.get("ready", True)) or row.get("error")]
        for key in finished[:-31]:
            del _registry[key]
        _registry[job["id"]] = {"title": title, "job": copy.deepcopy(job), "cancel": cancel, "ready": False,
                                "input": copy.deepcopy(inputs or {}), "apply_args": copy.deepcopy(apply_args or {}),
                                "references": dict(references or {}),
                                'final': job['status'] in TERMINAL and (job['status'] != 'failed' or bool(job.get('error')))}
    _redraw()
    return True


def _wait_known(job_id, cancel, on_progress):
    """A blocking replay joins the original job without replacing its panel entry."""
    while True:
        if cancel.is_set():
            cancel_job(job_id)
        job = job_status(job_id)
        if on_progress:
            on_progress(dict(job["progress"]))
        if job["status"] in TERMINAL:
            return job
        time.sleep(POLL_SECONDS)


def _update(job, on_progress, *, final=False):
    with _lock:
        if job["id"] in _registry:
            _registry[job["id"]]["job"] = copy.deepcopy(job)
            _registry[job['id']]['final'] = final
    _redraw()
    if on_progress:
        on_progress(dict(job["progress"]))


def _progress(value):
    if (not isinstance(value, dict) or type(value.get("pct")) is not int or not 0 <= value["pct"] <= 100
            or not isinstance(value.get("message"), str) or not value["message"]):
        raise _invalid("Invalid job progress from Loopcut")
    from .cloud import safe_value
    return {'pct': value['pct'], 'message': safe_value(value['message'])}


def _output(value, *, local=False):
    if (not isinstance(value, dict) or not isinstance(value.get("name"), str) or not value["name"]
            or not isinstance(value.get("type"), str) or value["type"] not in OUTPUT_TYPES or not isinstance(value.get("mime"), str)
            or not value["mime"] or not isinstance(value.get("sha256"), str)
            or not _HASH.fullmatch(value["sha256"]) or type(value.get("size")) is not int
            or value["size"] < 0 or not isinstance(value.get("meta", {}), dict)):
        raise _invalid("Invalid job output from Loopcut")
    if local:
        if not isinstance(value.get("path"), str) or not Path(value["path"]).is_file():
            raise _invalid("Job output is missing its local file")
    else:
        try:
            cloud_http.validate_url(value.get("url"))
        except ValueError as ex:
            raise _invalid("Invalid output download URL") from ex
        if not isinstance(value.get("expires_at"), str):
            raise _invalid()
    return value


def _job(value, *, expected=None, local=False):
    if (not isinstance(value, dict) or not isinstance(value.get("id"), str)
            or (not local and not _JOB_ID.fullmatch(value["id"]))
            or not isinstance(value.get("status"), str) or value["status"] not in STATES or not isinstance(value.get("tool"), str)
            or not isinstance(value.get("outputs"), list) or "error" not in value):
        raise _invalid()
    if expected and (value["id"] != expected["id"] or value["tool"] != expected["tool"]):
        raise _invalid("Job identity changed in a response")
    _progress(value.get("progress"))
    credits = value.get("credits")
    if not isinstance(credits, dict) or any(type(credits.get(k)) is not int or credits[k] < 0
                                           for k in ("reserved", "charged", "refunded")):
        raise _invalid()
    provenance = value.get("provenance")
    if (not isinstance(provenance, dict) or provenance.get("job_id") != value["id"]
            or provenance.get("tool") != value["tool"] or not isinstance(provenance.get("kind"), str)
            or provenance["kind"] not in {"gen", "assist"}
            or any(not isinstance(provenance.get(k), str) or not provenance[k]
                   for k in ("vendor", "model", "created_at"))):
        raise _invalid("Invalid job provenance")
    error = value.get("error")
    if error is not None and (not isinstance(error, dict) or any(not isinstance(error.get(k), str)
                                                               for k in ("code", "message", "type"))):
        raise _invalid()
    for output in value["outputs"]:
        _output(output, local=local)
    _output_limits(value)
    from .cloud import safe_value
    clean = safe_value(value)
    if not local:
        for original, output in zip(value['outputs'], clean['outputs']):
            output['url'] = original['url']  # The download capability stays private until verified.
    return clean


def _output_limits(job):
    if (len(job['outputs']) > MAX_OUTPUTS or sum(o['size'] for o in job['outputs']) > MAX_DOWNLOAD
            or any(o['type'] == 'report' and o['size'] > MAX_REPORT_FILE for o in job['outputs'])):
        raise _invalid('Cloud outputs exceed the local download limit')
    try:
        size = len(json.dumps(job['outputs'], allow_nan=False).encode('utf-8'))
    except (ValueError, TypeError, RecursionError) as ex:
        raise _invalid('Invalid cloud output metadata') from ex
    if size > MAX_EVENT:
        raise _invalid('Cloud output metadata is too large')


def parse_sse(response, cancel=None):
    """Yield complete SSE records only; pings, unknown fields and partial EOFs are ignored."""
    event, event_id, data, size = None, None, [], 0
    while cancel is None or not cancel.is_set():
        raw = response.readline(MAX_EVENT + 1)
        if not raw:
            return
        size += len(raw)
        if size > MAX_EVENT:
            raise _invalid("Cloud event is too large")
        try:
            line = raw.decode("utf-8").rstrip("\r\n")
        except UnicodeError as ex:
            raise _invalid("Cloud event is not UTF-8") from ex
        if not line:
            if data:
                try:
                    payload = json.loads("\n".join(data))
                except ValueError as ex:
                    raise _invalid("Invalid JSON in cloud event") from ex
                if not isinstance(payload, dict):
                    raise _invalid("Expected an object in cloud event")
                yield event, payload, event_id
            event, event_id, data, size = None, None, [], 0
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if field == "event":
            event = value
        elif field == "data":
            data.append(value)
        elif field == "id":
            if not value.isascii() or not value.isdigit() or len(value) > 20:
                raise _invalid("Invalid cloud event id")
            event_id = value


def _follow(client, job, cancel, on_progress):
    path = "/v1/jobs/" + urllib.parse.quote(job["id"], safe="")
    last_id = None
    for _ in range(STREAM_ATTEMPTS):
        if cancel.is_set():
            job = _job(client.request("POST", path + "/cancel"), expected=job)
            if job['status'] in TERMINAL:
                return job
            break  # Cancellation may be acknowledged before the worker stops; keep polling.
        try:
            headers = {"Last-Event-ID": last_id} if last_id else {}
            with client.stream(path + "/events", headers=headers, timeout=STREAM_TIMEOUT) as response:
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                if content_type != "text/event-stream":
                    break
                for event, data, event_id in parse_sse(response, cancel):
                    if event_id is not None and last_id is not None and int(event_id) <= int(last_id):
                        continue
                    if event == "done":
                        final = _job(data, expected=job)
                        if final["status"] not in TERMINAL:
                            raise _invalid("Cloud done event is not terminal")
                        _update(final, on_progress, final=True)
                        return final
                    if event == "status":
                        if not isinstance(data.get("status"), str) or data["status"] not in STATES:
                            raise _invalid()
                        job["status"] = data["status"]
                    elif event == "progress":
                        job["progress"] = _progress(data)
                    elif event == "output":
                        output = _output(data)
                        if output not in job["outputs"]:
                            job["outputs"].append(output)
                            _output_limits(job)
                    # Additive contract events are ignored, but can advance the replay cursor.
                    last_id = event_id or last_id
                    _update(job, on_progress)
        except cloud_http.CloudError as ex:
            if ex.code != "connection_failed" and ex.status not in {404, 405, 406, 501}:
                raise
            if ex.code != "connection_failed":
                break
        # EOF without done reconnects with the last fully processed event id.
    while True:
        method, suffix = ("POST", "/cancel") if cancel.is_set() else ("GET", "")
        job = _job(client.request(method, path + suffix), expected=job)
        _update(job, on_progress, final=job['status'] in TERMINAL)
        if job["status"] in TERMINAL:
            return job
        cancel.wait(POLL_SECONDS)


def _raise_failed(job):
    if job["status"] == "failed":
        error = job.get("error")
        if not error:
            raise _invalid("Failed cloud job has no error")
        # Async failures arrive inside a successful HTTP response, not an HTTP 502.
        raise cloud_http.CloudError(error["message"], error["code"], None,
                                   {**{k: v for k, v in error.items() if k not in {"message", "code", "type"}},
                                    "job": job}, error["type"])
    if job["status"] == "canceled":
        raise cloud_http.CloudError("Cloud job canceled; unspent credits were refunded", "canceled",
                                   extra={"job": job, "refunded": job["credits"]["refunded"]})


def _download(client, job, cancel):
    _output_limits(job)
    if not job['outputs']:
        return copy.deepcopy(job)
    directory = Path(tempfile.mkdtemp(prefix=job["id"] + "-"))
    result = copy.deepcopy(job)
    try:
        for n, output in enumerate(result["outputs"]):
            if cancel.is_set():
                raise cloud_http.CloudError("Cloud output download canceled", "canceled", extra={"job": job})
            # Server names never become filesystem paths. Index keeps identical frame hashes distinct.
            suffix = _SUFFIXES.get(output["mime"], ".bin")
            target = directory / f"{n:04d}-{output['sha256'][:12]}{suffix}"
            digest, size = hashlib.sha256(), 0
            with target.open("xb") as handle, client.download(output["url"]) as response:
                while True:
                    if cancel.is_set():
                        raise cloud_http.CloudError("Cloud output download canceled", "canceled", extra={"job": job})
                    chunk = response.read(blobs.CHUNK_SIZE)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > output["size"]:
                        raise cloud_http.CloudError("Cloud output size does not match", "hash_mismatch")
                    digest.update(chunk)
                    try:
                        handle.write(chunk)
                    except OSError as ex:
                        raise cloud_http.CloudError("Could not save cloud outputs", "output_save_failed") from ex
            if size != output["size"] or digest.hexdigest() != output["sha256"]:
                raise cloud_http.CloudError("Cloud output checksum does not match", "hash_mismatch")
            output["path"] = str(target)
            output.pop('url', None)
            output.pop('expires_at', None)
        from .cloud import safe_value
        return safe_value(result)
    except (cloud_http.CloudError, OSError):
        shutil.rmtree(directory)
        raise


def _finish(client, job, cancel, on_progress):
    try:
        if job["status"] not in TERMINAL:
            job = _follow(client, job, cancel, on_progress)
        _update(job, on_progress, final=True)
        _raise_failed(job)
        job = _download(client, job, cancel)
        _update(job, None, final=True)
        with _lock:
            _registry[job["id"]]["ready"] = True
        _redraw()
        _final(job["id"], job)
        return job
    except cloud_http.CloudError as ex:
        ex.extra.setdefault('job', job)
        with _lock:
            _registry[job["id"]]["error"] = ex
        _redraw()
        _final(job["id"], ex.extra["job"], ex)
        raise
    except OSError as ex:
        error = cloud_http.CloudError("Could not save cloud outputs", "output_save_failed")
        with _lock:
            _registry[job["id"]]["error"] = error
        _redraw()
        _final(job["id"], job, error)
        raise error from ex


def _background(client, job, cancel, on_progress):
    try:
        _finish(client, job, cancel, on_progress)
    except cloud_http.CloudError:
        return  # Retained in the registry and raised by job_status().


def run(descriptor: dict, arguments: dict, files: dict[str, Path], *, project: dict,
        on_progress=None, cancel=None, client=None, confirm_token=None, quote=False) -> dict:
    """Upload, submit once (retry a lost reply with the same key), follow and verify outputs.

    quote_id, bundle (an already uploaded manifest hash), and optional idempotency_key are
    call metadata, never vendor input. A caller retrying across run() calls can reuse the key
    returned in CloudError.extra; a failed or canceled job returns none, as its key only replays that
    job (SPEC §3.4). Ordinary separate calls always get fresh UUIDs. confirm_token: the user
    approved past a server limit (cloud_account._run_confirmed); it goes once, as the x-loopcut-confirm header.
    quote: price this call first and raise quote_required with the quote (a quote_required tool always does).
    """
    if (not isinstance(project, dict) or not isinstance(project.get("id"), str) or not project["id"]
            or type(project.get("no_generation")) is not bool):
        raise _input_error("Project id and no_generation are required", "project")
    if not isinstance(arguments, dict):
        raise _input_error("Tool arguments must be an object", "input")
    cancel = cancel if cancel is not None else threading.Event()
    if cancel.is_set():
        raise cloud_http.CloudError("Cloud call canceled before submission", "canceled")
    client = client if client is not None else _client()
    file_fields = {row["field"] for row in descriptor.get("file_inputs", [])}
    if set(files) - file_fields:
        raise _input_error("Unknown tool file field", "files")
    selected = []
    hashes = {}
    for row in descriptor.get("file_inputs", []):
        field = row["field"]
        if field not in files:
            if row.get("required"):
                raise _input_error("Required tool file is missing", field)
            continue
        try:
            path = Path(files[field])
            size = path.stat().st_size
            hashes[field] = blobs.sha256(path)
        except (OSError, TypeError, ValueError) as ex:
            raise _input_error("Tool file could not be read", field) from ex
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix.lower() == ".glb":
            mime = "model/gltf-binary"
        if size <= 0 or size > row["max_mb"] * 1024 * 1024 or mime not in row["accept"]:
            raise _input_error("Tool file type or size is not accepted", field)
        selected.append({"path": path, "mime": mime, "sha256": hashes[field], "size": size})
    from .cloud import apply_args as client_args
    local = set(client_args(descriptor))  # Contract 0.7: kept for the apply handler, never sent to the server.
    inputs = {k: v for k, v in arguments.items()
              if k not in file_fields | local | {"bundle", "quote_id", "idempotency_key"}}
    payload = {"tool": descriptor["name"], "input": inputs, "files": hashes}
    if arguments.get("bundle") is not None:
        digest = arguments["bundle"]
        if not isinstance(digest, str) or not _HASH.fullmatch(digest):
            raise _input_error("Bundle must be an uploaded manifest SHA-256", "bundle")
        payload["bundle"] = digest
    elif (descriptor.get("bundle_input") or {}).get("required"):
        raise _input_error("An uploaded bundle manifest is required", "bundle")
    try:
        blobs.upload_missing(selected, client=client, cancel=cancel, multipart=_blob_multipart())
    except (OSError, ValueError) as ex:
        raise _input_error("Tool file changed or could not be read during upload", "files") from ex
    if cancel.is_set():
        raise cloud_http.CloudError("Cloud call canceled before submission", "canceled")
    # Quotes and jobs pass the same server limits; the user's approval past one goes as this header (C47).
    confirm = {"headers": {"x-loopcut-confirm": confirm_token}} if confirm_token else {}
    # A quote_required tool always, and any tool when asked (cloud_account.authorize prices every paid call).
    if ((descriptor.get("cost") or {}).get("quote_required") or quote) and not arguments.get("quote_id"):
        quote = client.request("POST", "/v1/quotes", payload, **confirm)
        if (not isinstance(quote.get("quote_id"), str) or type(quote.get("credits")) is not int
                or quote["credits"] < 0 or not isinstance(quote.get("expires_at"), str)):
            raise _invalid("Invalid quote from Loopcut")
        raise cloud_http.CloudError("Approve the credit quote before running this tool", "quote_required", extra=quote)
    key = arguments.get("idempotency_key") or uuid.uuid4().hex
    if not isinstance(key, str) or not 1 <= len(key) <= 200:
        raise _input_error("Invalid idempotency key", "idempotency_key")
    payload.update(tool_version=descriptor["version"], project={"id": project["id"],
                   "no_generation": project["no_generation"]}, idempotency_key=key)
    if arguments.get("quote_id"):
        payload["quote_id"] = arguments["quote_id"]
    try:
        for attempt in range(2):
            try:
                job = _job(client.request("POST", "/v1/jobs", payload, **confirm))
                if job["tool"] != descriptor["name"]:
                    raise _invalid("Submitted tool changed in job response")
                break
            except cloud_http.CloudError as ex:
                if ex.code != "connection_failed" or attempt:
                    raise
        if not _remember(job, descriptor.get("title", descriptor["name"]), cancel, inputs,
                         {k: v for k, v in arguments.items() if k in local},
                         {k: arguments[k] for k in file_fields if isinstance(arguments.get(k), str)}):
            if descriptor.get("latency") == "long":
                if cancel.is_set():
                    cancel_job(job["id"])
                return job_status(job["id"])
            return _wait_known(job["id"], cancel, on_progress)
        _update(job, on_progress)
        if descriptor.get("latency") == "long" and job["status"] not in TERMINAL:
            threading.Thread(target=_background, args=(client, copy.deepcopy(job), cancel, on_progress),
                             name="Loopcut cloud job", daemon=True).start()
            return job
        return _finish(client, job, cancel, on_progress)
    except cloud_http.CloudError as ex:
        if ex.extra.get("job", {}).get("status") not in {"failed", "canceled"}:
            ex.extra.setdefault("idempotency_key", key)
        raise


def resolve_files(descriptor: dict, arguments: dict, out_dir: Path) -> dict[str, Path]:
    """Resolve names before paths: image buffers and selected object hierarchies are exported."""
    _main_only()
    import bpy
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    found = {}
    for n, row in enumerate(descriptor.get("file_inputs", [])):
        field, value = row["field"], arguments.get(row["field"])
        if value is None:
            if row.get("required"):
                raise _input_error("Required scene reference is missing", field)
            continue
        if not isinstance(value, str) or not value:
            raise _input_error("File reference must be an image, object name or path", field)
        if _accepts_video(row):
            found[field] = _video_plan(descriptor, arguments, row, value)["path"]
            continue
        image, ob = bpy.data.images.get(value), bpy.data.objects.get(value)
        if image is not None:
            path = directory / f"input-{n}.png"
            # Work on a copy: saving must not replace a painted buffer's filepath or format.
            temporary = image.copy()
            try:
                temporary.filepath_raw, temporary.file_format = str(path), "PNG"
                temporary.save()
            finally:
                bpy.data.images.remove(temporary)
        elif ob is not None:
            from . import glb
            path = directory / f"input-{n}.glb"
            glb._export(path, [ob, *ob.children_recursive])
        elif (attached := attached_image(value)) is not None:
            path = attached
        else:
            from . import files
            path = files.check(files.resolve(value))
            if not path.is_file():
                raise _input_error("Scene reference or file was not found", field)
        found[field] = path
    return found


def attached_image(name: str) -> Path | None:
    """The file of an image attached to this conversation, by the name its reference card gives it: the model sees
    the name, never a path, so "turn the attached photo into a model" had nothing it could pass (0.2.8's photo Try it
    card searched the disk, then generated from a text prompt). The original file the user attached while it is
    there (what try_samples recognises), else the copy stored with the conversation. None for any other name."""
    from . import conversations, state
    session = state.session()
    for reference in reversed(session.get("references") or []):
        if reference.get("name") != name or reference.get("video"):
            continue
        source = Path(reference["source"]) if reference.get("source") else None
        if source is not None and source.is_file():
            return source
        try:
            stored = conversations.image_path(session["id"], reference.get("full") or reference["ref"])
        except (conversations.ConversationError, KeyError):
            continue
        if stored.is_file():
            return stored
    return None


def _accepts_video(row):
    return any(isinstance(mime, str) and mime.startswith("video/") for mime in row.get("accept", []))


def _movie_strip(name):
    import bpy
    editor = getattr(bpy.context.scene, "sequence_editor", None)
    if editor is None:
        return None
    every = getattr(editor, "strips_all", None)
    strip = (every if every is not None else editor.sequences_all).get(name)
    return strip if strip is not None and strip.type == "MOVIE" else None


_probes = {}


def _probe_video(path):
    """(seconds, fps) as Blender reads the file; a clip is loaded and removed. Cached per file version."""
    import bpy
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    if key not in _probes:
        try:
            clip = bpy.data.movieclips.load(str(path), check_existing=False)
        except RuntimeError as ex:
            raise _input_error(f"{path.name} is not a video Blender can read", "video") from ex
        try:
            fps, frames = float(clip.fps), int(clip.frame_duration)
        finally:
            bpy.data.movieclips.remove(clip)
        if fps <= 0 or frames < 1:
            raise _input_error(f"{path.name} has no frames or frame rate Blender can read", "video")
        if len(_probes) > 32:
            _probes.clear()
        _probes[key] = (frames / fps, fps)
    return _probes[key]


def _video_plan(descriptor, arguments, row, value):
    """A video scene reference (movie clip, movie strip or file path) checked before anything is uploaded:
    mp4/mov/webm, the size the tool takes, and at most MAX_CAPTURE_SECONDS captured. A trimmed strip's
    trim becomes the capture's start_s/end_s unless the call gave them."""
    import bpy
    field = row["field"]
    clip = getattr(bpy.data, "movieclips", {}).get(value)
    strip = None if clip is not None else _movie_strip(value)
    if clip is not None:
        if getattr(clip, "source", "MOVIE") != "MOVIE":
            raise _input_error(f"Movie clip {value!r} is an image sequence; use a video file", field)
        path = Path(bpy.path.abspath(clip.filepath))
    elif strip is not None:
        path = Path(bpy.path.abspath(strip.filepath))
    else:
        from . import files
        path = files.check(files.resolve(value))
    try:
        path = path.resolve()
        size = path.stat().st_size if path.is_file() else -1
    except OSError:
        size = -1
    if size <= 0:
        raise _input_error(f"Video {value!r} was not found as a movie clip, video strip or file", field)
    if path.suffix.lower() not in VIDEO_SUFFIXES:
        raise _input_error(f"{path.name} is not an MP4, MOV or WebM video", field)
    if size > row["max_mb"] * 1024 * 1024:
        raise _input_error(f"{path.name} is {size / 1048576:.0f} MB; the limit is {row['max_mb']:g} MB", field)
    seconds, _fps = _probe_video(path)
    start, end = arguments.get("start_s"), arguments.get("end_s")
    trim = {}
    if strip is not None and start is None and end is None and strip.frame_duration > 0:
        per_frame = seconds / strip.frame_duration  # Scene frames of the whole strip, whatever its speed.
        first = max(0, strip.frame_offset_start)
        if first > 0 or strip.frame_final_duration < strip.frame_duration:
            trim = {"start_s": round(first * per_frame, 3),
                    "end_s": round(min(seconds, (first + strip.frame_final_duration) * per_frame), 3)}
            start, end = trim["start_s"], trim["end_s"]
    start = 0.0 if start is None else float(start)
    end = seconds if end is None else float(end)
    if end > seconds + 0.05 or start >= end:
        raise _input_error(f"The capture {start:.2f}–{end:.2f} s is outside the video (0–{seconds:.2f} s)", field)
    if end - start > MAX_CAPTURE_SECONDS + 1e-6:
        raise _input_error(f"{path.name} is {end - start:.1f} s long; capture at most {MAX_CAPTURE_SECONDS} s by "
                           "choosing the part with start_s and end_s", field)
    return {"path": path, "seconds": seconds, "trim": trim}


def prepare_inputs(descriptor: dict, arguments: dict, files: dict) -> dict:
    """Main thread, after resolve_files: the arguments the call actually sends and applies with. Adds a video's
    measured length (video_seconds) and a trimmed strip's start_s/end_s when the tool takes them, and resolves
    apply_motion's target_armature (contract 0.7 apply_args) now, so selection changes during the job don't matter."""
    out = dict(arguments)
    properties = (descriptor.get("input_schema") or {}).get("properties", {})
    for row in descriptor.get("file_inputs", []):
        if _accepts_video(row) and row["field"] in files and isinstance(arguments.get(row["field"]), str):
            plan = _video_plan(descriptor, arguments, row, arguments[row["field"]])
            if "video_seconds" in properties:
                out["video_seconds"] = round(plan["seconds"], 3)
            out.update({k: v for k, v in plan["trim"].items() if k in properties})
    from .cloud import apply_args as client_args
    if descriptor.get("apply") == "apply_motion" and "target_armature" in client_args(descriptor):
        target = motion_target(arguments.get("target_armature"))
        if target is not None:
            out["target_armature"] = target.name
    return out


def motion_target(name):
    """The armature apply_motion animates: the named object (or a mesh's armature), else the active or the one
    selected humanoid armature, else None (a new skeleton). A named one that is not a humanoid is refused."""
    _main_only()
    import bpy
    from . import motion_retarget
    if name is not None:
        ob = bpy.data.objects.get(name)
        armature = motion_retarget.armature_of(ob)
        if armature is None:
            raise _input_error(f"{name!r} is not an armature or a mesh deformed by one in this file; name an "
                               "armature, or leave target_armature out for a new skeleton", "target_armature")
        if motion_retarget.rig_of(armature) is None:
            raise _input_error(f"{armature.name!r} does not look like a humanoid rig (hips, legs, arms and head); "
                               "leave target_armature out for a new skeleton", "target_armature")
        return armature
    view_layer = bpy.context.view_layer
    candidates = [motion_retarget.armature_of(view_layer.objects.active)]
    selected = {motion_retarget.armature_of(o) for o in getattr(bpy.context, "selected_objects", ())} - {None}
    if len(selected) == 1:
        candidates.append(selected.pop())
    return next((a for a in candidates if a is not None and motion_retarget.rig_of(a) is not None), None)


# Include embedded ID datablocks, which do not occur in bpy.data.node_groups / shape_keys.
_DATA_COLLECTIONS = ("objects", "meshes", "materials", "images", "collections", "texts", "sounds", "actions",
                     "armatures", "cameras", "lights", "curves", "hair_curves", "node_groups", "shape_keys",
                     "worlds", "scenes", "movieclips", "textures", "volumes", "pointclouds", "grease_pencils")


def datablock_snapshot(data):
    """Pure identity map; linked/reused IDs are excluded by the before/after difference."""
    found = {}
    for name in _DATA_COLLECTIONS:
        for block in getattr(data, name, ()):
            found[_identity(block)] = block
            for attribute in ("node_tree", "shape_keys"):
                child = getattr(block, attribute, None)
                if child is not None:
                    found[_identity(child)] = child
    return found


def _identity(block):
    return block.as_pointer() if hasattr(block, "as_pointer") else id(block)


def created_datablocks(before, after):
    return [block for key, block in after.items() if key not in before]


def stamp_provenance(blocks, provenance):
    from .cloud import safe_value
    fields = {'job_id', 'tool', 'vendor', 'model', 'kind', 'created_at', 'backend'}
    encoded = json.dumps(safe_value({k: v for k, v in provenance.items() if k in fields}),
                         sort_keys=True, ensure_ascii=False, allow_nan=False)
    for block in blocks:
        block["loopcut_provenance"] = encoded


def result_summary(handler, job, names=(), reports=()):
    """Pure model-facing result; reports are also saved in Text datablocks and the conversation."""
    actions = {"add_image_reference": "Added image references", "import_model": "Imported and selected models",
               "add_audio_strip": "Added sound strips at the current frame", "load_frames": "Loaded frames in the sequencer",
               "attach_report": "Attached reports", "none": "Saved outputs", "apply_motion": "Applied motion"}
    line = f"{actions[handler]}: {', '.join(names) or 'no outputs'}. Job {job['id']}; provenance recorded."
    from .cloud import safe_value
    return safe_value(line + ("\n" + "\n".join(reports) if reports else ""))


def _report_text(output):
    path = Path(output["path"])
    with path.open("r", encoding="utf-8") as handle:
        text = handle.read(MAX_REPORT + 1)
    if len(text) > MAX_REPORT:
        text = text[:MAX_REPORT] + "\n[Report truncated; the complete file is at " + str(path) + "]"
    from .cloud import safe_value
    return safe_value(f"{output['name']}:\n{text}")


def _reports(job):
    import bpy
    from . import conversations, state
    reports = []
    for output in job["outputs"]:
        if output["type"] != "report":
            continue
        path = Path(output["path"])
        from .cloud import safe_value
        with path.open('r', encoding='utf-8') as handle:
            text = handle.read(MAX_REPORT_FILE + 1)
        if len(text.encode('utf-8')) > MAX_REPORT_FILE:
            raise _invalid('Cloud report exceeds the local report limit')
        text = safe_value(text)
        block = bpy.data.texts.new(f"Loopcut {job['id']} {output['name']}")
        block.write(text)
        report = _report_text(output)
        reports.append(report)
        session = state.session()
        session["items"].append(state.item_notice(report))
        conversations.save(session)
    return reports


def _remember_assets(job):
    """The raw asset_search tool's report: register its rows' import ids the way search_assets does, or
    import_asset refuses an ambientCG or Physically Based id from it as unknown (Poly Haven's importer
    looks its ids up itself, so only those worked; platform audit of 0.2.1, 2026-10-10)."""
    from . import asset_report, indexed_assets
    for output in job["outputs"]:
        if output["type"] != "report" or output.get("mime") != "application/json":
            continue
        try:
            with Path(output["path"]).open("rb") as handle:
                raw = handle.read(asset_report.MAX_BYTES + 1)
            report = json.loads(raw) if len(raw) <= asset_report.MAX_BYTES else None
        except (OSError, ValueError, RecursionError):
            continue
        for row in asset_report.importable_rows(report):
            indexed_assets.remember(row)


def _add_images(job):
    import bpy
    from . import attachments, conversations, state
    names = []
    for output in job["outputs"]:
        if output["type"] != "image":
            continue
        image = bpy.data.images.load(output["path"], check_existing=False)
        image.name = f"Loopcut {job['id']} {output['name']}"
        image.pack()
        ob = bpy.data.objects.new(image.name, None)
        ob.empty_display_type, ob.data = "IMAGE", image
        ob.empty_display_size = 2
        ob.location = bpy.context.scene.cursor.location
        bpy.context.collection.objects.link(ob)
        session = state.session()
        # add() makes bounded copies for the model; promote this attachment to a pinned reference.
        pending = list(session["attachments"])
        session["attachments"] = []
        try:
            problems = attachments.add(session, [output["path"]])
            if problems:
                raise cloud_http.CloudError("; ".join(problems), "apply_failed")
            for reference in session["attachments"]:
                reference.update(name=image.name, pinned=True)
                session["references"].append(reference)
        finally:
            session["attachments"] = pending
        conversations.save(session)
        names.append(image.name)
    return names


def _model_bounds(objects):
    """Conservative evaluated world bounds for the existing size fit, excluding helper origins."""
    import bpy
    from mathutils import Vector
    graph = bpy.context.evaluated_depsgraph_get()
    points = []
    for ob in objects:
        if ob.type != "MESH":
            continue
        evaluated = ob.evaluated_get(graph)
        if all(tuple(corner) == (-1, -1, -1) for corner in evaluated.bound_box):
            continue  # Blender's sentinel for unavailable bounds.
        points.extend(evaluated.matrix_world @ Vector(corner) for corner in evaluated.bound_box)
    if not points:
        return None
    return (Vector(tuple(min(p[i] for p in points) for i in range(3))),
            Vector(tuple(max(p[i] for p in points) for i in range(3))))


def _model_geometry_bounds(objects):
    """Actual evaluated world vertices for placement; rotated local box corners can lie in empty space."""
    import bpy
    import numpy as np
    from mathutils import Vector
    graph = bpy.context.evaluated_depsgraph_get()
    low, high = None, None
    for ob in objects:
        if ob.type != "MESH":
            continue
        evaluated = ob.evaluated_get(graph)
        vertices = evaluated.data.vertices
        if not len(vertices):
            continue
        # One bulk read per mesh, with the transform and reductions in NumPy for large imports.
        coordinates = np.empty(len(vertices) * 3, dtype=np.float32)
        vertices.foreach_get("co", coordinates)
        matrix = np.array(evaluated.matrix_world, dtype=np.float64)
        world = coordinates.reshape(-1, 3) @ matrix[:3, :3].T + matrix[:3, 3]
        mesh_low, mesh_high = world.min(axis=0), world.max(axis=0)
        low = mesh_low if low is None else np.minimum(low, mesh_low)
        high = mesh_high if high is None else np.maximum(high, mesh_high)
    return (Vector(low), Vector(high)) if low is not None else None


def _frame_model(objects):
    """Best-effort Frame Selected in an existing viewport, without moving a locked camera."""
    import bpy
    try:
        if bpy.app.background or not any(ob.type == "MESH" for ob in objects):
            return False
        for window in bpy.context.window_manager.windows:
            if window.scene != bpy.context.scene or window.view_layer != bpy.context.view_layer:
                continue
            for area in window.screen.areas:
                try:
                    if area.type != "VIEW_3D":
                        continue
                    region = next((r for r in area.regions if r.type == "WINDOW"), None)
                    if region is None:
                        continue
                    space = area.spaces.active
                    with bpy.context.temp_override(window=window, screen=window.screen, area=area, region=region):
                        # Hidden geometry can make view_selected return FINISHED without moving the view.
                        if not any(ob.type == "MESH" and ob.visible_get(view_layer=window.view_layer, viewport=space)
                                   for ob in objects):
                            continue
                        if not bpy.ops.view3d.view_selected.poll():
                            continue
                        locked = space.lock_camera
                        # An offset export can leave its root/armature origin far from the geometry.
                        selection = [(ob, ob.select_get()) for ob in objects if ob.type != "MESH"]
                        try:
                            space.lock_camera = False
                            for ob, _ in selection:
                                ob.select_set(False)
                            # EXEC_DEFAULT frames immediately, before camera lock is restored.
                            result = bpy.ops.view3d.view_selected("EXEC_DEFAULT", use_all_regions=False)
                        finally:
                            for ob, selected in selection:
                                ob.select_set(selected)
                            space.lock_camera = locked
                    if "FINISHED" in result:
                        return True
                except Exception as ex:  # A failed candidate must not prevent trying the next view.
                    print(f"Loopcut: could not frame the imported model in this viewport: {ex!r}")
    except Exception as ex:  # UI context can disappear while a cloud job is finishing.
        print(f"Loopcut: could not frame the imported model: {ex!r}")
    return False


def _model_import(job):
    import bpy
    from mathutils import Vector
    names = []
    for output in job["outputs"]:
        if output["type"] != "model_3d":
            continue
        path = Path(output["path"])
        fbx = path.suffix.lower() == ".fbx"
        if not fbx and path.suffix.lower() not in {".glb", ".gltf"}:
            raise cloud_http.CloudError("Only GLB, glTF and FBX model imports are available in this build",
                                        "apply_not_available")
        if fbx:
            _check_fbx(path)
        else:
            _check_gltf(path)
        previous = datablock_snapshot(bpy.data)
        note = ""
        if fbx:
            # Tripo returns quads only as FBX; GLB cannot hold them.
            try:
                result = _import_fbx(path)
            finally:
                dropped = _drop_outside_textures(path, previous)
            note = f" (FBX; left off {dropped} texture(s) not embedded in the file)" if dropped else " (FBX)"
        else:
            result = bpy.ops.import_scene.gltf(filepath=str(path), import_scene_extras=False,
                                               import_scene_as_collection=False)
        if "FINISHED" not in result:
            raise cloud_http.CloudError("Blender could not import the model", "apply_failed")
        objects = [ob for ob in bpy.data.objects if _identity(ob) not in previous]
        if not objects:
            raise cloud_http.CloudError("Model contained no objects", "apply_failed")
        _fresh_materials(objects, previous)
        label = f"Loopcut {job['id']} {output['name']}"
        if job["provenance"]["kind"] == "gen":
            _neutral_names(previous, label)
        collection = bpy.data.collections.new(label)
        bpy.context.scene.collection.children.link(collection)
        root = bpy.data.objects.new(collection.name, None)
        collection.objects.link(root)
        imported_ids = {_identity(ob) for ob in objects}
        bpy.context.view_layer.update()
        for ob in objects:
            for parent_collection in list(ob.users_collection):
                parent_collection.objects.unlink(ob)
            collection.objects.link(ob)
            if ob.parent is None or _identity(ob.parent) not in imported_ids:
                matrix = ob.matrix_world.copy()
                ob.parent = root
                ob.matrix_world = matrix
        # Generated props fit a 2m box and rest on the cursor's Z, centred on its X/Y.
        # Assist tools keep source dimensions and placement.
        bpy.context.view_layer.update()
        if job["provenance"]["kind"] == "gen":
            bounds = _model_bounds(objects)
            if bounds:
                low, high = bounds
                extent = max(high - low)
                units = bpy.context.scene.unit_settings.scale_length or 1
                factor = 2 / units / extent if math.isfinite(extent) and extent > 0 else 1
                root.scale = (factor,) * 3
                bpy.context.view_layer.update()
                bounds = _model_geometry_bounds(objects)
                if bounds:
                    low, high = bounds
                    bottom = Vector(((low[0] + high[0]) / 2, (low[1] + high[1]) / 2, low[2]))
                    root.location = bpy.context.scene.cursor.location - bottom
                    note += f"; placed on the ground at the 3D cursor, {(high[2] - low[2]) * units:.3g} m tall"
        bpy.context.view_layer.update()
        for ob in bpy.context.view_layer.objects:
            ob.select_set(False)
        for ob in objects:
            ob.select_set(True)
        root.select_set(True)
        bpy.context.view_layer.objects.active = root
        _frame_model([root, *objects])
        names.append(collection.name + note)
    return names


def _check_fbx(path):
    """Binary FBX only, the one format the adapters label model/vnd.fbx (fal.model_mime)."""
    with path.open("rb") as handle:
        if handle.read(len(FBX_MAGIC)) != FBX_MAGIC:
            raise cloud_http.CloudError("Cloud model is not a binary FBX", "apply_failed")


def _import_fbx(path):
    """Blender's native FBX importer (wm.fbx_import, C++/ufbx), else the bundled Python add-on's.

    The native operator polls for a window, which a timer-driven apply has not got: borrow the
    first one; background Blender has none and uses the add-on. Both set the scene's frame rate to
    the file's, so it is put back. Custom properties stay off, as glTF extras do."""
    import bpy
    rates = [(scene, scene.render.fps, scene.render.fps_base) for scene in bpy.data.scenes]
    wm = bpy.context.window_manager
    window = bpy.context.window or (wm.windows[0] if wm.windows else None)
    try:
        if "fbx_import" in dir(bpy.ops.wm) and window is not None:
            before = datablock_snapshot(bpy.data)
            with bpy.context.temp_override(window=window, screen=window.screen):
                result = bpy.ops.wm.fbx_import(filepath=str(path), use_custom_props=False)
            if "FINISHED" not in result or not _reused_images(before) or "fbx" not in dir(bpy.ops.import_scene):
                return result
            # The native importer takes an existing image of the same name in place of the file's
            # embedded one, so a second result showed the first one's textures. Undo it and use the
            # Python importer, which always makes new images from the embedded pixels.
            _remove_created(before)
        if "fbx" in dir(bpy.ops.import_scene):
            return bpy.ops.import_scene.fbx(filepath=str(path), use_custom_props=False, use_image_search=False)
        raise cloud_http.CloudError("This Blender build has no FBX importer", "apply_not_available")
    finally:
        for scene, fps, base in rates:
            if (scene.render.fps, scene.render.fps_base) != (fps, base):
                scene.render.fps, scene.render.fps_base = fps, base


def _new_materials(before):
    """Materials the import created or assigned to the objects it created."""
    import bpy
    found = {_identity(m): m for m in bpy.data.materials if _identity(m) not in before}
    for ob in bpy.data.objects:
        if _identity(ob) not in before:
            for slot in getattr(ob, "material_slots", ()):
                if slot.material is not None:
                    found[_identity(slot.material)] = slot.material
    return list(found.values())


def _reused_images(before):
    """True when an imported material samples an image that existed before the import."""
    for material in _new_materials(before):
        tree = getattr(material, "node_tree", None)
        for node in getattr(tree, "nodes", ()):
            image = getattr(node, "image", None)
            if getattr(node, "type", None) == "TEX_IMAGE" and image is not None and _identity(image) in before:
                return True
    return False


def _remove_created(before):
    """Remove every datablock an import added (never one that existed before it). Scenes stay."""
    import bpy
    for name in _DATA_COLLECTIONS:
        collection = getattr(bpy.data, name, None)
        if collection is None or name == "scenes":
            continue
        for block in [b for b in collection if _identity(b) not in before]:
            collection.remove(block)


# Generated models arrive with the vendor's export names (tripo_node_<uuid>, Color_<uuid>), which then show in
# the Outliner and the change card. Assist tools keep the user's own names, so only "gen" results are renamed.
_VENDOR_TAG = re.compile(r"(?i)(?:^|[_\- ])(?:tripo|rodin|hyper3d|meshy|trellis|hunyuan)(?=[_\- ]|$)")
_UUID_TAG = re.compile(r"(?i)[_\- ]?[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}")
_GENERIC_PART = {"node": "mesh", "mesh": "mesh", "geometry": "mesh", "": ""}


def _neutral_name(name: str, label: str) -> str | None:
    """'tripo_node_<uuid>' -> '<label> mesh', 'Color_<uuid>' -> '<label> Color'; None keeps the name."""
    if not (_UUID_TAG.search(name) or _VENDOR_TAG.search(name)):
        return None
    part = re.sub(r"[_\-]+", " ", _VENDOR_TAG.sub(" ", _UUID_TAG.sub("", name))).strip()
    part = _GENERIC_PART.get(part.lower(), part)
    return f"{label} {part}" if part else label


def _neutral_names(previous, label):
    import bpy
    for block in created_datablocks(previous, datablock_snapshot(bpy.data)):
        name = getattr(block, "name", None)
        renamed = _neutral_name(name, label) if isinstance(name, str) else None
        if renamed:
            block.name = renamed


def _fresh_materials(objects, previous):
    """Imported objects get their own materials: one that existed before the import is copied, so
    editing this result never changes another object's material (images were made fresh on import)."""
    for ob in objects:
        for slot in getattr(ob, "material_slots", ()):
            material = slot.material
            if material is not None and _identity(material) in previous:
                slot.material = material.copy()


def _drop_outside_textures(path, previous):
    """Untrusted FBX, like glTF, may only use embedded images or files beside the downloaded output.

    Both importers pack embedded textures into the .blend, but open any other texture at the path
    the file names, anywhere on disk, or leave a placeholder when it is missing. Pixels load lazily,
    so removing those images (and the texture nodes that use them) reads nothing. Returns the count."""
    import bpy
    folder = path.parent.resolve()
    outside = []
    for image in bpy.data.images:
        if _identity(image) in previous or image.packed_file is not None:
            continue
        try:
            target = Path(bpy.path.abspath(image.filepath)).resolve()
            beside = target.is_relative_to(folder) and target.is_file()
        except (OSError, ValueError):
            beside = False
        if not beside:
            outside.append(image)
    if not outside:
        return 0
    removed = {_identity(image) for image in outside}
    for material in bpy.data.materials:
        tree = material.node_tree
        if _identity(material) in previous or tree is None:
            continue
        for node in list(tree.nodes):
            if node.type == "TEX_IMAGE" and node.image is not None and _identity(node.image) in removed:
                tree.nodes.remove(node)
    for image in outside:
        bpy.data.images.remove(image)
    return len(outside)


def _check_gltf(path):
    """Untrusted glTF may only reference embedded data or files beside the downloaded output."""
    if path.suffix.lower() == ".glb":
        from . import glb
        document, _ = glb.parse(path.read_bytes())
    else:
        document = json.loads(path.read_text(encoding="utf-8"))
    # Blender's importer clears bpy.data.scenes[0]'s existing variant list for this extension.
    # Refuse before importing; scene extras=False does not prevent that destructive side effect.
    if 'KHR_materials_variants' in (document.get('extensions') or {}):
        raise cloud_http.CloudError('Cloud models with material variants cannot be safely imported.', 'apply_not_available')
    for row in [*document.get("buffers", []), *document.get("images", [])]:
        uri = row.get("uri")
        if uri is None or (isinstance(uri, str) and uri.startswith("data:")):
            continue
        if not isinstance(uri, str):
            raise cloud_http.CloudError("Invalid glTF file reference", "apply_failed")
        decoded = urllib.parse.unquote(uri)
        parts = urllib.parse.urlsplit(decoded)
        target = (path.parent / decoded).resolve()
        if parts.scheme or parts.netloc or "\\" in decoded or not target.is_relative_to(path.parent.resolve()):
            raise cloud_http.CloudError("glTF references a file outside the job folder", "apply_failed")
        if not target.is_file():
            raise cloud_http.CloudError("glTF companion file is missing; use a self-contained GLB", "apply_failed")


def _strips():
    import bpy
    editor = bpy.context.scene.sequence_editor_create()
    return editor.strips if hasattr(editor, "strips") else editor.sequences


_AUDIO_LABELS = {"voice_line": ("VO", "text"), "sfx": ("SFX", "prompt")}  # Strip name prefix, the input it quotes.
NAME_BYTES = 36  # Of the line or prompt in a strip's name; Blender keeps 63 bytes of a name.


def _tool_input(job):
    """What the call asked for: kept from a managed submit (run), carried by a BYOK job (byok.runner)."""
    with _lock:
        kept = (_registry.get(job["id"]) or {}).get("input")
    value = kept or job.get("input")
    return value if isinstance(value, dict) else {}


def _first_words(text, limit=NAME_BYTES):
    """The start of `text` within `limit` UTF-8 bytes, ended on a whole word when it can be, … when cut."""
    text = " ".join(text.split())
    if len(text.encode("utf-8")) <= limit:
        return text
    cut = text[:limit]
    while len((cut + "…").encode("utf-8")) > limit:
        cut = cut[:-1]
    if " " in cut and text[len(cut)] != " ":
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,.;:") + "…"


def _audio_name(job, output, taken):
    """VO • <the line's first words> for voice_line, SFX • <the prompt> for sfx, else the output's name;
    numbered the way Blender names a copy (.001) when the scene already has a strip by that name."""
    label, field = _AUDIO_LABELS.get(job["tool"], ("", ""))
    words = _first_words(str(_tool_input(job).get(field) or "")) if label else ""
    base = f"{label} • {words}" if words else output["name"]
    name, n = base, 0
    while name in taken:
        n += 1
        name = f"{base}.{n:03d}"
    return name


def _strip_names():
    import bpy
    editor = bpy.context.scene.sequence_editor_create()
    every = getattr(editor, "strips_all", None)  # With the strips inside meta strips.
    return {strip.name for strip in (every if every is not None else editor.sequences_all)}


def _placement(strip, fps):
    """Where a strip sits, so the model can place things against it without reading it back."""
    return (f'"{strip.name}" (channel {strip.channel}, frames {strip.frame_final_start}–{strip.frame_final_end} '
            f"at {round(fps, 3):g} fps, end exclusive, {strip.frame_final_duration / fps:.2f} s)")


def _add_audio(job):
    """Each sound becomes a strip at the current frame, packed into the .blend as _add_images packs images (the
    download sits in a temp folder), and a card in the chat under the call to hear it (state.item_audio)."""
    import bpy
    from . import conversations, playback, state
    scene = bpy.context.scene
    strips, taken = _strips(), _strip_names()
    channel = max((strip.channel for strip in strips), default=0) + 1
    fps = scene.render.fps / scene.render.fps_base
    session = state.session()
    placed, cards = [], []
    for output in job["outputs"]:
        if output["type"] != "audio":
            continue
        strip = strips.new_sound(_audio_name(job, output, taken), output["path"], channel=channel,
                                 frame_start=scene.frame_current)
        strip.sound.pack()
        stamp_provenance([strip], job["provenance"])
        taken.add(strip.name)
        placed.append(_placement(strip, fps))
        kept = conversations.store_audio(session, Path(output["path"]))  # The card's own copy, like a capture's.
        cards.append(state.item_audio(strip.name, kept, strip.frame_final_duration / fps, playback.peaks(kept)))
        channel += 1
    if cards:
        session["items"].extend(cards)
        conversations.save(session)
    return placed


def _load_frames(job):
    import bpy
    names, strips = [], _strips()
    channel = max((strip.channel for strip in strips), default=0) + 1
    start = bpy.context.scene.frame_current
    images = [o for o in job["outputs"] if o["type"] in {"render_frames", "image"} and o["mime"].startswith("image/")]
    images.sort(key=lambda o: (o.get("meta", {}).get("frame", 0), o["name"]))
    if images:
        # Blender sequences require every frame in one folder, including BYOK results.
        folder = Path(tempfile.mkdtemp(prefix="loopcut-frames-"))
        paths = []
        for n, output in enumerate(images):
            target = folder / f"frame-{n:06d}{Path(output['path']).suffix}"
            shutil.copyfile(output["path"], target)
            paths.append(target)
        strip = strips.new_image(f"Loopcut {job['id']} frames", str(paths[0]), channel=channel, frame_start=start)
        for path in paths[1:]:
            strip.elements.append(path.name)
        strip.frame_final_duration = len(paths)
        stamp_provenance([strip], job["provenance"])
        names.append(strip.name)
        channel += 1
    for output in job["outputs"]:
        if output["type"] == "video" or (output["type"] == "render_frames" and output["mime"].startswith("video/")):
            strip = strips.new_movie(output["name"], output["path"], channel=channel, frame_start=start)
            stamp_provenance([strip], job["provenance"])
            names.append(strip.name)
            channel += 1
    return names


def _kept(job, key):
    with _lock:
        value = (_registry.get(job["id"]) or {}).get(key)
    return value if isinstance(value, dict) else {}


def _read_output(output, limit):
    path = Path(output["path"])
    if path.stat().st_size > limit:
        raise cloud_http.CloudError(f"{output['name']} is larger than Loopcut applies", "apply_failed")
    return path.read_text(encoding="utf-8")


# Said after every applied motion (owner 2026-10-21): a spinning jump kick came back as a kick, and the agent faked
# the jump and spin with root keyframes over it, then reported what was asked instead of what the motion did.
MOTION_CHECK = {
    "motion_from_text": (
        "Before you report, look at its key frames and say what this motion actually does, not what was asked. If it "
        "misses the request, call motion_from_text again with a simpler prompt (one plain physical action; it is "
        "charged again) or tell the user what it made. Never keyframe the root, hips or whole body over it to fake "
        "missing motion. A small local touch-up (say, a hand pose or holding the last pose) is fine if you say so."),
    "motion_from_video": (
        "Before you report, look at its key frames and say what was captured and where tracking failed. Never "
        "keyframe the root, hips or whole body over it to fake missing motion."),
}


def _apply_motion(job):
    """Retarget motion.bvh onto the call's target_armature (resolved when it was called), or onto a new SOMA
    skeleton with a simple body; motion.json adds the frame rate check, fingers and warnings."""
    import bpy
    from . import motion_retarget
    outputs = job["outputs"]
    bvh = next((o for o in outputs if o["mime"] == "application/x-bvh" or o["name"].lower().endswith(".bvh")), None)
    sidecar = next((o for o in outputs if o is not bvh and (o["mime"] == "application/json"
                                                             or o["name"].lower().endswith(".json"))), None)
    if bvh is None:
        raise cloud_http.CloudError("The motion result has no motion.bvh", "apply_failed")
    meta = motion_retarget.read_meta(_read_output(sidecar, 1024 * 1024) if sidecar else "")
    args, inputs = _kept(job, "apply_args") or job.get("apply_args") or {}, _tool_input(job)
    notes, target = [], None
    name = args.get("target_armature")
    if isinstance(name, str) and name:
        target = motion_retarget.armature_of(bpy.data.objects.get(name))
        if target is None or motion_retarget.rig_of(target) is None:
            notes.append(f"'{name}' is no longer a humanoid armature in this scene, so the motion went on a new skeleton")
            target = None
    title = inputs.get("prompt")
    if not isinstance(title, str) or not title.strip():
        reference = Path(next(iter(_kept(job, "references").values()), ""))
        title = reference.stem if reference.suffix.lower() in VIDEO_SUFFIXES else reference.name
    line = motion_retarget.apply(_read_output(bvh, MAX_MOTION_BYTES), meta, target=target, title=title or "motion",
                                 in_place=args.get("in_place") is True,
                                 frame_start=args.get("frame_start") if type(args.get("frame_start")) is int else None)
    return [line + ("; " + "; ".join(notes) if notes else "")]


def apply_result(descriptor: dict, job: dict) -> str:
    """Apply a finished managed or BYOK job and stamp every newly created ID datablock."""
    _main_only()
    import bpy
    job = _job(job, local=True)
    _raise_failed(job)
    if job["status"] != "succeeded":
        raise cloud_http.CloudError("Cloud job is still running; check job_status before applying", "job_not_finished")
    handler = descriptor.get("apply", "none")
    if handler == "import_material":
        return "Material import is not yet available in this build. Verified outputs: " + ", ".join(o["path"] for o in job["outputs"])
    handlers = {"add_image_reference": _add_images, "import_model": _model_import,
                "add_audio_strip": _add_audio, "load_frames": _load_frames,
                "attach_report": lambda job: [o["name"] for o in job["outputs"] if o["type"] == "report"],
                "apply_motion": _apply_motion, "none": lambda job: [o["path"] for o in job["outputs"]]}
    if handler not in handlers:
        raise cloud_http.CloudError(f"Result handler {handler!r} is not available in this build", "apply_not_available")
    before = datablock_snapshot(bpy.data)
    try:
        names = handlers[handler](job)
        reports = _reports(job) if handler not in ("none", "apply_motion") else []
        if handler == "apply_motion" and job.get("tool") in MOTION_CHECK:
            reports = [MOTION_CHECK[job["tool"]]]
        if descriptor.get("name") == "asset_search" and handler == "attach_report":
            _remember_assets(job)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as ex:
        if isinstance(ex, cloud_http.CloudError):
            raise
        raise cloud_http.CloudError(f"Could not apply cloud result: {ex}", "apply_failed") from ex
    finally:
        # Even a partial Blender import carries its source; existing scene IDs are never relabeled.
        stamp_provenance(created_datablocks(before, datablock_snapshot(bpy.data)), job["provenance"])
    return result_summary(handler, job, names, reports)
