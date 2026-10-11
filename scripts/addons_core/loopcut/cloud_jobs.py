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
OUTPUT_TYPES = {"image", "model_3d", "material", "audio", "video", "render_frames", "report"}
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
_SUFFIXES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
             "model/gltf-binary": ".glb", "model/gltf+json": ".gltf", "audio/wav": ".wav",
             "audio/mpeg": ".mp3", "video/mp4": ".mp4", "video/webm": ".webm",
             "application/json": ".json", "text/plain": ".txt", "text/markdown": ".md"}
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


def _redraw():
    from . import mainthread
    mainthread.request_redraw()


def progress_rows(registry):
    """Pure panel data, with no Events, locks, Blender or mutable registry values exposed."""
    rows = []
    for key, row in registry.items():
        job = row["job"]
        downloading = job["status"] == "succeeded" and not row.get("ready", True)
        if row.get("error") or (job["status"] in TERMINAL and not downloading):
            continue
        message = "Downloading outputs" if downloading else job["progress"]["message"]
        rows.append({"id": key, "title": row["title"], "pct": job["progress"]["pct"],
                     "message": "Canceling…" if row.get("canceling") else message,
                     "cancelable": not row.get("canceling", False)})
    from .cloud import safe_value
    return safe_value(rows)


def ui_rows():
    with _lock:
        return progress_rows(_registry)


def cancel_job(job_id):
    with _lock:
        row = _registry.get(job_id)
        if row is None or row.get("error") or (row["job"]["status"] in TERMINAL and row.get("ready", True)):
            return False
        row["cancel"].set()
        row["canceling"] = True
    _redraw()
    return True


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


def _remember(job, title, cancel):
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
        return job
    except cloud_http.CloudError as ex:
        ex.extra.setdefault('job', job)
        with _lock:
            _registry[job["id"]]["error"] = ex
        _redraw()
        raise
    except OSError as ex:
        error = cloud_http.CloudError("Could not save cloud outputs", "output_save_failed")
        with _lock:
            _registry[job["id"]]["error"] = error
        _redraw()
        raise error from ex


def _background(client, job, cancel, on_progress):
    try:
        _finish(client, job, cancel, on_progress)
    except cloud_http.CloudError:
        return  # Retained in the registry and raised by job_status().


def run(descriptor: dict, arguments: dict, files: dict[str, Path], *, project: dict,
        on_progress=None, cancel=None, client=None) -> dict:
    """Upload, submit once (retry a lost reply with the same key), follow and verify outputs.

    quote_id, bundle (an already uploaded manifest hash), and optional idempotency_key are
    call metadata, never vendor input. A caller retrying across run() calls can reuse the key
    returned in CloudError.extra. Ordinary separate calls always get fresh UUIDs.
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
    inputs = {k: v for k, v in arguments.items() if k not in file_fields | {"bundle", "quote_id", "idempotency_key"}}
    payload = {"tool": descriptor["name"], "input": inputs, "files": hashes}
    if arguments.get("bundle") is not None:
        digest = arguments["bundle"]
        if not isinstance(digest, str) or not _HASH.fullmatch(digest):
            raise _input_error("Bundle must be an uploaded manifest SHA-256", "bundle")
        payload["bundle"] = digest
    elif (descriptor.get("bundle_input") or {}).get("required"):
        raise _input_error("An uploaded bundle manifest is required", "bundle")
    try:
        blobs.upload_missing(selected, client=client)
    except (OSError, ValueError) as ex:
        raise _input_error("Tool file changed or could not be read during upload", "files") from ex
    if cancel.is_set():
        raise cloud_http.CloudError("Cloud call canceled before submission", "canceled")
    if (descriptor.get("cost") or {}).get("quote_required") and not arguments.get("quote_id"):
        quote = client.request("POST", "/v1/quotes", payload)
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
                job = _job(client.request("POST", "/v1/jobs", payload))
                if job["tool"] != descriptor["name"]:
                    raise _invalid("Submitted tool changed in job response")
                break
            except cloud_http.CloudError as ex:
                if ex.code != "connection_failed" or attempt:
                    raise
        if not _remember(job, descriptor.get("title", descriptor["name"]), cancel):
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
        else:
            from . import files
            path = files.check(files.resolve(value))
            if not path.is_file():
                raise _input_error("Scene reference or file was not found", field)
        found[field] = path
    return found


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
               "attach_report": "Attached reports", "none": "Saved outputs"}
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
        session["items"].append({"kind": "notice", "text": report})
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


def _model_import(job):
    import bpy
    from mathutils import Vector
    names = []
    for output in job["outputs"]:
        if output["type"] != "model_3d":
            continue
        path = Path(output["path"])
        if path.suffix.lower() not in {".glb", ".gltf"}:
            raise cloud_http.CloudError("Only GLB/glTF model imports are available in this build", "apply_not_available")
        _check_gltf(path)
        previous = datablock_snapshot(bpy.data)
        result = bpy.ops.import_scene.gltf(filepath=str(path), import_scene_extras=False,
                                           import_scene_as_collection=False)
        if "FINISHED" not in result:
            raise cloud_http.CloudError("Blender could not import the model", "apply_failed")
        objects = [ob for ob in bpy.data.objects if _identity(ob) not in previous]
        if not objects:
            raise cloud_http.CloudError("Model contained no objects", "apply_failed")
        collection = bpy.data.collections.new(f"Loopcut {job['id']} {output['name']}")
        bpy.context.scene.collection.children.link(collection)
        root = bpy.data.objects.new(collection.name, None)
        collection.objects.link(root)
        imported_ids = {_identity(ob) for ob in objects}
        bpy.context.view_layer.update()
        points = [ob.matrix_world @ Vector(corner) for ob in objects if ob.type == "MESH" for corner in ob.bound_box]
        for ob in objects:
            for parent_collection in list(ob.users_collection):
                parent_collection.objects.unlink(ob)
            collection.objects.link(ob)
            if ob.parent is None or _identity(ob.parent) not in imported_ids:
                matrix = ob.matrix_world.copy()
                ob.parent = root
                ob.matrix_world = matrix
        # Generated props fit a 2m bounding box at the cursor. Assist tools keep source dimensions.
        if points and job["provenance"]["kind"] == "gen":
            low = Vector(tuple(min(p[i] for p in points) for i in range(3)))
            high = Vector(tuple(max(p[i] for p in points) for i in range(3)))
            extent = max(high - low)
            units = bpy.context.scene.unit_settings.scale_length or 1
            factor = 2 / units / extent if math.isfinite(extent) and extent > 0 else 1
            root.scale = (factor,) * 3
            root.location = bpy.context.scene.cursor.location - (low + high) * (.5 * factor)
        bpy.context.view_layer.update()
        for ob in bpy.context.view_layer.objects:
            ob.select_set(False)
        for ob in objects:
            ob.select_set(True)
        root.select_set(True)
        bpy.context.view_layer.objects.active = root
        names.append(collection.name)
    return names


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


def _add_audio(job):
    import bpy
    names = []
    strips = _strips()
    channel = max((strip.channel for strip in strips), default=0) + 1
    for output in job["outputs"]:
        if output["type"] == "audio":
            strip = strips.new_sound(output["name"], output["path"], channel=channel,
                                     frame_start=bpy.context.scene.frame_current)
            stamp_provenance([strip], job["provenance"])
            names.append(strip.name)
            channel += 1
    return names


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
                "none": lambda job: [o["path"] for o in job["outputs"]]}
    if handler not in handlers:
        raise cloud_http.CloudError(f"Result handler {handler!r} is not available in this build", "apply_not_available")
    before = datablock_snapshot(bpy.data)
    try:
        names = handlers[handler](job)
        reports = _reports(job) if handler != "none" else []
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
