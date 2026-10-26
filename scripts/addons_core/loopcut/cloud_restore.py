"""Managed long cloud jobs that outlive Blender (0.2.19).

A job is recorded with the conversation that started it (session["cloud_jobs"], saved in conversation.json) as soon
as it is submitted: plain fields only, never the live objects cloud._jobs holds. When that conversation opens again,
in this Blender or a later one (lifecycle.switch_to), each job goes back behind loopcut_cloud.job_status, the service
is asked where each unfinished one stands, and one still running is followed again.

Settlement is the service's (contract 0.10). What a restart leaves owed here: the chat's spend (its hold becomes the
charge), the submission's retry key (cloud_account.complete), and the outputs in the file. Whether they are in the
file is read from the file itself (cloud_jobs.applied_here): Undo, a checkpoint restore, or quitting without saving
take them away, and the card and job_status then offer them again. A lock file per job only keeps two Blenders from
applying at the same moment.

A record changes in the conversation as it is now: the copy on screen, or else its file. History reopens a chat as a
new copy, so an older copy never decides a charge and is never written over newer turns.
"""

import copy
import json
import math
import re
import threading
import time
from pathlib import Path

KEEP = 32  # Jobs a conversation remembers, the newest kept.
CHECKS = 8  # Unfinished jobs asked about when a conversation opens, the longest unasked first (the jobs budget).
MAX_INPUT = 16 * 1024  # Bytes of a call's inputs kept for its result's names; a larger one keeps none.
LOCK_SECONDS = 600  # A lock older than this was left by a Blender that stopped mid-apply.
THUMBNAILS = 4  # Frames a card shows (a render's previews: first, middle and last).
# Apply handlers whose outputs the file shows (stamped datablocks or strips). "none" changes nothing, and
# "import_material" is not applied in this build, so those count as applied once job_status returned them.
EVIDENCE = {"add_image_reference", "import_model", "add_audio_strip", "load_frames", "attach_report", "apply_motion"}
_JOB_ID = re.compile(r"job_[A-Za-z0-9_-]{1,128}\Z")
_HEX = re.compile(r"(?:[0-9a-f]{64})?\Z")
_TERMINAL = {"succeeded", "failed", "canceled"}
_STATUSES = {"running"} | _TERMINAL
_lock = threading.RLock()  # Records change on the agent thread, the job threads and main; saves read them.

_FIELDS = {"id": str, "tool": str, "version": int, "apply": str, "kind": str, "title": str, "apply_args": dict,
           "input": dict, "references": dict, "blend": str, "scene": str, "conversation": str, "auto": bool,
           "fingerprint": str, "key": str, "engine": str, "status": str, "created_at": str, "finished_at": str,
           "settled": bool, "applied": bool, "seen": bool, "previews": list}


def _number(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _count(value) -> bool:
    return type(value) is int and value >= 0


def _valid(row) -> bool:
    if not isinstance(row, dict) or any(not isinstance(row.get(k), t) or (t is int and type(row[k]) is bool)
                                        for k, t in _FIELDS.items()):
        return False
    resolution, partial = row.get("resolution"), row.get("partial")
    from . import conversations
    return (_JOB_ID.fullmatch(row["id"]) is not None and conversations._ID.match(row["conversation"]) is not None
            and row["status"] in _STATUSES and _HEX.fullmatch(row["fingerprint"]) is not None
            and len(row["key"]) <= 200 and _number(row.get("held"))
            and all(row.get(k) is None or _number(row[k]) for k in ("quoted", "charged", "refunded", "checked_at"))
            and all(row.get(k) is None or _count(row[k]) for k in ("frames", "frames_done"))
            and all(isinstance(row.get(k, ""), str) for k in ("expired", "error"))
            and isinstance(row.get("preview_frames", []), list) and all(_count(f) for f in row.get("preview_frames", []))
            and isinstance(row.get("video", False), bool)
            and (partial is None or isinstance(partial, dict) and all(_count(partial.get(k)) for k in ("requested", "delivered")))
            and (resolution is None or isinstance(resolution, list) and len(resolution) == 2
                 and all(type(v) is int and v > 0 for v in resolution))
            and all(isinstance(p, str) and conversations._own_image_path(p) for p in row["previews"]))


def restored(value) -> list:
    """A saved list as load reads it: damaged records are dropped, never the conversation."""
    if not isinstance(value, list):
        return []
    rows = [row for row in value[-KEEP:] if _valid(row)]
    if len(rows) != len(value[-KEEP:]):
        print(f"Loopcut: dropped {len(value[-KEEP:]) - len(rows)} damaged cloud job record(s)")
    return rows


def snapshot(session: dict) -> list:
    """For conversations.save, from any thread."""
    with _lock:
        return copy.deepcopy(list(session.get("cloud_jobs") or []))


def _frames(inputs):
    """How many frames a render asked for (SPEC: inclusive frame_start..frame_end by frame_step; a turntable
    without frame_end renders 120)."""
    start = inputs.get("frame_start", 1)
    end = inputs.get("frame_end", start + 119 if inputs.get("turntable") else start)
    step = inputs.get("frame_step", 1)
    if any(type(v) is not int for v in (start, end, step)) or step < 1 or end < start:
        return None
    return (end - start) // step + 1


def origin(scene) -> dict:
    """Main thread: where a call's outputs belong (the .blend and scene by name, never a pointer that a restart
    invalidates), and the scene's output size and engine for its card."""
    import bpy
    render = getattr(scene, "render", None)
    try:
        resolution = [int(render.resolution_x), int(render.resolution_y)]
    except (AttributeError, TypeError, ValueError):
        resolution = None
    return {"blend": str(getattr(getattr(bpy, "data", None), "filepath", "") or ""),
            "scene": str(getattr(scene, "name", "") or ""), "resolution": resolution,
            "engine": str(getattr(render, "engine", "") or "")}


def record(session: dict, job: dict, descriptor: dict, arguments: dict, where: dict, hold=None) -> dict | None:
    """Agent thread, as a managed long job is handed off: remember it with the conversation and save at once, so a
    quit, crash or power cut right after still finds it. hold: the chat's (credits held, ran without asking)."""
    from . import cloud
    if descriptor.get("apply") == "apply_rig" or not _JOB_ID.fullmatch(str(job.get("id", ""))):
        return None  # A rig's apply needs this session's prepared character; it is never picked up later.
    client_only = set(cloud.apply_args(descriptor))
    file_fields = {f["field"] for f in descriptor.get("file_inputs", [])}
    inputs = {k: v for k, v in arguments.items()
              if k not in client_only | file_fields | {"bundle", "quote_id", "idempotency_key"}}
    references = {k: arguments[k] for k in file_fields if isinstance(arguments.get(k), str)}
    try:
        if len(json.dumps([inputs, references], allow_nan=False)) > MAX_INPUT:
            inputs, references = {}, {}
    except (TypeError, ValueError):
        inputs, references = {}, {}
    resolution, scale = where.get("resolution"), inputs.get("resolution_pct", 100)
    if resolution and type(scale) is int and 1 <= scale <= 100:
        resolution = [max(1, resolution[0] * scale // 100), max(1, resolution[1] * scale // 100)]
    engine = inputs.get("engine") if isinstance(inputs.get("engine"), str) else where.get("engine", "")
    held, auto = hold if isinstance(hold, tuple) and len(hold) == 2 else (0, False)
    quote = getattr(arguments, "quote", None)  # cloud_account.authorize's: what the user approved.
    quoted = quote.get("credits") if isinstance(quote, dict) and _number(quote.get("credits")) else None
    key = arguments.get("idempotency_key")
    row = {"id": job["id"], "tool": descriptor["name"], "version": descriptor["version"],
           "apply": descriptor.get("apply", "none"), "kind": descriptor.get("kind", "assist"),
           "title": str(cloud._display(descriptor, "title") or descriptor.get("title") or descriptor["name"]),
           "apply_args": {k: v for k, v in arguments.items() if k in client_only}, "input": inputs,
           "references": references, "blend": where.get("blend", ""), "scene": where.get("scene", ""),
           "conversation": session["id"], "held": held if _number(held) else 0, "auto": bool(auto),
           "fingerprint": str(getattr(arguments, "fingerprint", "") or ""),
           "key": key if isinstance(key, str) and len(key) <= 200 else "",
           "frames": _frames(inputs) if descriptor["name"] == "render" else None, "resolution": resolution,
           "engine": engine or "", "status": "running", "quoted": quoted, "charged": None, "refunded": None,
           "created_at": "", "finished_at": "", "settled": False, "applied": False, "seen": False, "previews": []}
    if not _valid(row):
        print("Loopcut: this cloud job could not be recorded with its conversation")
        return None
    with _lock:
        rows = session.setdefault("cloud_jobs", [])
        known = next((r for r in rows if r["id"] == row["id"]), None)
        if known is not None:
            return known  # A replay of a job already recorded.
        rows.append(row)
        del rows[:-KEEP]
    _save(session)
    return row


def _save(session: dict) -> None:
    from . import conversations
    try:
        conversations.save(session)
    except Exception as ex:  # A job's bookkeeping must never break the job (no data folder, a full disk).
        print(f"Loopcut: could not save this chat's cloud jobs: {ex!r}")


def _change(saved: dict, change) -> None:
    """change(record, ledger) -> {field: value} it set, on the conversation as it is now: the copy on screen (then
    saved), else its file. The caller's copy of the record learns the same fields."""
    from . import conversations, spending, state
    session = state.session()
    if session.get("id") == saved["conversation"]:
        with _lock:
            row = next((r for r in session.get("cloud_jobs") or [] if r["id"] == saved["id"]), None)
            made = change(row, spending.ledger(session)) if row is not None else {}
        if row is not None:
            _save(session)
    else:
        made = {}

        def on_disk(body):
            rows = body.get("cloud_jobs") if isinstance(body.get("cloud_jobs"), list) else []
            row = next((r for r in rows if isinstance(r, dict) and r.get("id") == saved["id"]), None)
            if row is None:
                return False
            if not spending.valid_ledger(body.get("spend")):
                body["spend"] = spending.new_ledger()
            made.update(change(row, body["spend"]))
            return True
        try:
            conversations.patch(saved["conversation"], on_disk)
        except Exception as ex:
            print(f"Loopcut: could not update a closed chat's cloud job: {ex!r}")
    with _lock:
        saved.update(made)


def update(saved: dict, **changes) -> None:
    """Record what this Blender learned about a job, in its conversation as it is now."""
    def change(row, ledger):
        row.update(changes)
        return dict(changes)
    _change(saved, change)


def learn(saved: dict, job: dict) -> dict:
    """A job as the service last returned it: its status, times, credits, and what its card says once it is over,
    so a later opening needs no request for it."""
    from . import cloud_jobs
    credits = job.get("credits") if isinstance(job.get("credits"), dict) else {}
    facts = {"status": job["status"] if job.get("status") in _TERMINAL else "running", "checked_at": time.time()}
    for key in ("created_at", "finished_at"):
        if isinstance(job.get(key), str):
            facts[key] = job[key]
    for key in ("charged", "refunded"):
        if _number(credits.get(key)):
            facts[key] = credits[key]
    outputs = job.get("outputs") if isinstance(job.get("outputs"), list) else []
    if outputs and facts["status"] == "succeeded":
        frames = sum(1 for o in outputs if o.get("type") == "render_frames" and not cloud_jobs.preview(o))
        if frames:
            facts["frames_done"] = frames
        facts["expired"] = cloud_jobs.expired_message(job)
        facts["preview_frames"] = sorted(o["meta"]["frame"] for o in outputs if cloud_jobs.preview(o)
                                         and _count(o["meta"].get("frame")))[:THUMBNAILS]
        # A render's animation.mp4 beside its frames: its card opens it rather than asking for one (Make the MP4).
        facts["video"] = bool(frames) and any((o.get("type") == "video" or str(o.get("mime", "")).startswith("video/"))
                                              and not cloud_jobs.preview(o) and o.get("expired") is not True
                                              for o in outputs)
    partial = job.get("partial")
    if isinstance(partial, dict) and _count(partial.get("requested")) and _count(partial.get("delivered")):
        facts["partial"] = {"requested": partial["requested"], "delivered": partial["delivered"]}
    error = job.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        facts["error"] = " ".join(error["message"].split())[:200]
    with _lock:
        saved.update(facts)
    return facts


def settle(saved: dict, job: dict) -> None:
    """What the service said about a job, kept with its conversation. Once it is over, the chat's hold becomes what
    was charged, once (by the record's own held and auto, in the conversation as it is now), and the submission's
    retry key is released."""
    from . import spending
    facts = learn(saved, job)

    def change(row, ledger):
        made = dict(facts)
        if facts["status"] in _TERMINAL and not row.get("settled") and _number(facts.get("charged")):
            with spending.lock:
                spending.settle(ledger, row["held"], facts["charged"], row["auto"])
            made["settled"] = True
        row.update(made)
        return made
    _change(saved, change)
    if facts["status"] in _TERMINAL:
        release(saved)


def release(saved: dict) -> None:
    """cloud_account.complete for a submission this Blender no longer holds: its pending row and key go, as
    job_status releases them for a job it follows. Harmless twice, or for another account's cache."""
    from . import cloud, cloud_account, cloud_http
    runtime = cloud._runtime
    if runtime is None or not saved.get("fingerprint") or not saved.get("key"):
        return
    arguments = cloud_account._Authorized({"idempotency_key": saved["key"]})
    arguments.fingerprint = saved["fingerprint"]
    try:
        cloud_account.complete(runtime, arguments, saved["status"] != "succeeded")
    except cloud_http.CloudError as ex:
        print(f"Loopcut: could not release a cloud job's retry key: {ex}")


# -------------------------------------------------------------------------------------- applied, once, in the file

def applied_before(saved: dict, module=None) -> bool:
    """Its outputs are in the open file (stamped with its job id), or, for a handler that leaves nothing there,
    job_status returned them already. module: the one that applies them (cloud_jobs), which reads the file."""
    if module is None:
        from . import cloud_jobs as module
    look = getattr(module, "applied_here", None)
    if saved["apply"] in EVIDENCE and callable(look):
        try:
            return bool(look(saved["id"]))
        except Exception:  # Nothing readable to look in (no Blender data).
            return False
    return bool(saved.get("applied"))


def _lock_file(saved: dict) -> Path:
    from . import conversations
    return conversations._folder(saved["conversation"]) / "cloud_jobs" / (saved["id"] + ".applying")


def claim(saved: dict) -> bool:
    """Main thread, just before outputs are applied: False while another Blender applies them (its fresh lock
    file). One left by a Blender that stopped mid-apply is taken over after LOCK_SECONDS. A lock that cannot be
    written never stops an apply. unclaim gives it back, whatever the apply did."""
    for attempt in range(2):
        try:
            path = _lock_file(saved)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8") as handle:
                handle.write(saved["id"])
            return True
        except FileExistsError:
            try:
                stale = time.time() - path.stat().st_mtime > LOCK_SECONDS
            except OSError:
                stale = True
            if not stale or attempt:
                return False
            path.unlink(missing_ok=True)
        except Exception as ex:  # No data folder, or no room: the apply goes ahead.
            print(f"Loopcut: could not lock a cloud job's apply: {ex!r}")
            return True
    return False


def unclaim(saved: dict) -> None:
    try:
        _lock_file(saved).unlink(missing_ok=True)
    except Exception:
        pass


def applied(saved: dict, ok: bool) -> None:
    """After an apply: a good one is recorded, and its card in the chat on screen says so."""
    if not ok:
        return
    update(saved, applied=True)
    from . import state
    for item in state.session()["items"]:
        if item.get("kind") == "cloud_back" and item.get("job_id") == saved["id"] and not item.get("resolved"):
            item.update(busy="", error="", resolved=_loaded(item))


def _loaded(item: dict) -> str:
    frames = item.get("frames")
    mp4 = " and the MP4" if any(a["do"] == "open_mp4" for a in item.get("actions") or ()) else ""
    return (f"Loaded {frames:,} frame{'' if frames == 1 else 's'}{mp4} in the Video Sequencer." if frames
            else "Loaded into your scene.")


def recheck(session: dict) -> None:
    """Main thread, after Undo, Redo or a checkpoint restore (lifecycle): each card reads its outputs from the file
    again. Undo after Load takes them out, so the card offers Load again; Redo puts them back, and it says so."""
    from . import mainthread
    with _lock:
        records = {r["id"]: r for r in session.get("cloud_jobs") or ()}
    changed = False
    for item in session["items"]:
        saved = records.get(item.get("job_id")) if item.get("kind") == "cloud_back" else None
        if saved is None or saved["apply"] not in EVIDENCE or not item["actions"] or item.get("busy"):
            continue
        loaded = applied_before(saved)
        if item.get("resolved") and not loaded:
            item.update(resolved="", error="")
        elif loaded and not item.get("resolved"):
            item.update(resolved=_loaded(item), error="")
        else:
            continue
        changed = True
    if changed:
        mainthread.request_redraw()


# ------------------------------------------------------------------------------------------- which file and scene

def origin_state(saved: dict) -> str:
    """"here": the open file and scene are the job's. "elsewhere": its file is another one that still exists, or its
    scene is not the one shown. "ask": it started in an unsaved file, or its file has moved or been renamed, so only
    the user can say this is the scene it was for."""
    import bpy
    from . import conversations
    scene = getattr(getattr(getattr(bpy, "context", None), "scene", None), "name", None)
    current = conversations.project_key(str(getattr(bpy.data, "filepath", "") or ""))
    if not saved["blend"]:
        return "ask"
    if current == conversations.project_key(saved["blend"]):
        return "here" if scene == saved["scene"] else "elsewhere"
    return "elsewhere" if Path(saved["blend"]).is_file() else "ask"


def where(row: dict) -> str:
    """Where a cloud._jobs row's outputs may be applied from: a job started in this Blender by its scene itself; a
    picked-up one by its file and scene names."""
    if not row.get("restored"):
        from . import cloud
        return "here" if cloud._same_scene(row.get("scene")) else "elsewhere"
    return origin_state(row["restored"])


def refusal(row: dict, state: str) -> str:
    saved = row.get("restored")
    if state == "ask":
        return ("This job started in an unsaved file, or its file has moved, so Loopcut can't tell this scene is the "
                "one it was for. Its card in the chat has Load here anyway, for the user to decide.")
    if not saved:
        return "Cloud output belongs to another scene; switch back before applying."
    name = Path(saved["blend"]).name
    return f"Cloud output belongs to {name}, scene {saved['scene'] or 'Scene'}; open that file and scene before applying."


def retarget(saved: dict) -> None:
    """Load here anyway: the job's outputs belong to the file and scene open now."""
    import bpy
    scene = getattr(getattr(bpy.context, "scene", None), "name", "") or ""
    update(saved, blend=str(bpy.data.filepath or ""), scene=scene)


def file_saved(session: dict, before: str, after: str) -> None:
    """Save As, or a first save: this chat's jobs not yet in the file follow it to its new path (the conversation
    does too, lifecycle._on_save_post)."""
    from . import conversations
    if not after or conversations.project_key(before) == conversations.project_key(after):
        return
    with _lock:
        rows = [r for r in session.get("cloud_jobs") or ()
                if conversations.project_key(r["blend"]) == conversations.project_key(before)]
    for saved in rows:
        if not applied_before(saved):
            update(saved, blend=after)


def descriptor(row: dict):
    """(descriptor, why not): a picked-up job's tool as the catalog has it now. A new version still applies it while
    its apply handler and client arguments fit; a tool missing from the catalog applies with the handler saved with
    it. Only a handler that changed, or that this build lacks, leaves the job fetchable but not applied."""
    from . import cloud
    saved = row.get("restored")
    if not saved or row.get("resolved"):
        return row["descriptor"], ""
    runtime = cloud._runtime
    tools = (runtime.manifest.get("tools") if runtime is not None else None) or []
    found = next((d for d in tools if isinstance(d, dict) and d.get("name") == saved["tool"]), None)
    if found is not None and cloud._descriptor(found):
        if found["apply"] != saved["apply"] or not set(saved["apply_args"]) <= set(cloud.apply_args(found)):
            return None, f"{saved['title']} applies its results differently now, so this job's outputs can't be applied here."
        row["descriptor"], row["resolved"] = {**copy.deepcopy(found), "backend": "managed"}, True
        return row["descriptor"], ""
    if saved["apply"] not in cloud._APPLY:
        return None, f"This Loopcut can't apply {saved['title']}'s outputs."
    return row["descriptor"], ""


# ----------------------------------------------------------------------------------------------- picking jobs up

def _row(saved: dict) -> dict:
    """A cloud._jobs row for a job this Blender did not start (or no longer follows), from plain fields."""
    from . import cloud, cloud_account, cloud_jobs

    def handle_error(error):
        if cloud._runtime is not None:
            cloud._runtime.handle_error(error)

    def complete(spent=False):
        release(saved)

    def charge_reporter(charged, text=""):  # A rig's refund flow only; settle() owns the chat's spend.
        pass
    stub = {"name": saved["tool"], "title": saved["title"], "version": saved["version"], "apply": saved["apply"],
            "kind": saved["kind"], "backend": "managed", "latency": "long"}
    return {"descriptor": stub, "module": cloud_jobs, "handle_error": handle_error,
            "report_error": lambda error: cloud_account.report_error(error), "result": None, "scene": None,
            "complete": complete, "charge_reporter": charge_reporter, "saved": saved, "restored": saved}


def follow(row: dict, watcher=None, job=None) -> bool:
    """Main thread: follow a recorded job here (cloud_jobs.adopt), fetching it (unless job is a fresh copy), then its
    verified outputs."""
    from . import cloud_jobs
    saved = row["saved"]
    return cloud_jobs.adopt(saved["id"], saved["title"], saved["input"], saved["apply_args"], saved["references"],
                            watcher=watcher, job=job)


def pickup(session: dict) -> None:
    """Main thread, as a conversation opens: put its jobs back behind job_status. A job over while the chat was
    closed gets its card: from its record when that knows the outcome, from this Blender when it followed it, else
    from one request (at most CHECKS, the longest unasked first), which a running job's following reuses. Opening a
    conversation twice adds nothing twice."""
    from . import cloud, cloud_jobs
    with _lock:
        rows = list(session.get("cloud_jobs") or [])
    over, unknown = [], []
    for saved in rows:
        with cloud._jobs_lock:
            row = cloud._jobs.get(saved["id"])
            if row is None:
                row = cloud._jobs[saved["id"]] = _row(saved)
            else:  # Started or picked up before in this Blender: this opening's record is the one to keep.
                row["saved"] = saved
                if row.get("restored"):
                    row["restored"] = saved
        if applied_before(saved):
            continue
        ended = cloud_jobs.over(saved["id"])
        if ended is not None:  # Followed to its end in this Blender while this chat was closed.
            settle(saved, ended)
            over.append((saved, ended))
        elif cloud_jobs.known(saved["id"]):
            continue  # Running here: its row shows it.
        elif saved["status"] in _TERMINAL:
            if not saved.get("seen"):
                over.append((saved, None))
        else:
            unknown.append(saved)
    for saved, job in over:
        finished(session["id"], saved, job, away=True)
    unknown = sorted(unknown, key=lambda s: s.get("checked_at") or 0)[:CHECKS]
    if not unknown:
        return
    client = cloud_jobs._client()

    def check():
        from . import mainthread
        for saved in unknown:
            try:
                job = cloud_jobs.fetch(client, saved["id"])
            except Exception as ex:  # Offline, signed out or another account: asked again next time it opens.
                print(f"Loopcut: could not check cloud job {saved['id']}: {ex}")
                continue
            mainthread.run_on_main(lambda saved=saved, job=job: _checked(session["id"], saved, job))
    threading.Thread(target=check, name="Loopcut cloud pickup", daemon=True).start()


def _checked(conversation: str, saved: dict, job: dict) -> None:
    """Main thread: what the service said about a picked-up job."""
    from . import cloud, state
    settle(saved, job)
    if state.session().get("id") != conversation:
        return  # The user moved on; its card comes when this conversation opens again.
    with cloud._jobs_lock:
        row = cloud._jobs.get(saved["id"])
    if row is None:
        return
    if job["status"] not in _TERMINAL:
        if _other_file(saved):
            return  # Following it would download its outputs for another file; that file's opening follows it.
        follow(row, lambda final, error: _over(conversation, saved, final, error), job=job)
        return
    finished(conversation, saved, job, away=True)


def _other_file(saved: dict) -> bool:
    """The job's file is another one than the open file, and it still exists (origin_state's "elsewhere" by file)."""
    import bpy
    from . import conversations
    current = conversations.project_key(str(getattr(bpy.data, "filepath", "") or ""))
    return (bool(saved["blend"]) and current != conversations.project_key(saved["blend"])
            and Path(saved["blend"]).is_file())


def _over(conversation: str, saved: dict, job, error) -> None:
    """A job followed here is over (its outputs downloaded, or an error): settle and say so. One started here (cloud.py)
    or picked up after a restart; its chat says so if it is the open one, else when it opens (pickup). A cancel asked
    here (its row's Cancel, or the model's job_status) gets no card: what said so already is the one word on it."""
    from . import cloud_http, cloud_jobs, mainthread
    final = job if isinstance(job, dict) and job.get("status") in _TERMINAL else (
        error.extra.get("job") if isinstance(error, cloud_http.CloudError) else None)

    def land():
        from . import state
        if not isinstance(final, dict) or final.get("status") not in _TERMINAL:
            return
        settle(saved, final)
        if final["status"] == "canceled" and cloud_jobs.canceled_here(saved["id"]):
            update(saved, seen=True)
        elif state.session().get("id") == conversation:
            finished(conversation, saved, final, away=False)
    mainthread.run_on_main(land)


# --------------------------------------------------------------------------------------------------- its card

SIZES = {(7680, 4320): "8K", (3840, 2160): "4K", (4096, 2160): "4K", (2560, 1440): "1440p", (1920, 1080): "1080p",
         (1280, 720): "720p"}


def took(created: str, finished: str) -> str:
    """"took 1 h 52 min" from a job's RFC 3339 times (finished_at - created_at); "" without both."""
    from datetime import datetime
    try:
        seconds = (datetime.fromisoformat(finished.replace("Z", "+00:00"))
                   - datetime.fromisoformat(created.replace("Z", "+00:00"))).total_seconds()
    except (AttributeError, TypeError, ValueError):
        return ""
    if seconds < 0:
        return ""
    if seconds < 60:
        return f"took {round(seconds)} s"
    hours, minutes = divmod(max(1, round(seconds / 60)), 60)
    return f"took {hours} h {minutes} min" if hours and minutes else f"took {hours} h" if hours else f"took {minutes} min"


def size_label(resolution) -> str:
    if not resolution:
        return ""
    width, height = resolution
    return SIZES.get((width, height)) or SIZES.get((height, width)) or f"{width}×{height}"


def _credits(value) -> str:
    return f"{value:,g} credit{'' if value == 1 else 's'}"


def card(saved: dict, *, away: bool, ask: bool = False) -> dict:
    """The chat's card for a job that is over (f0's next-morning card), from its record alone: what it was, how long
    it took and what it cost, and what to do next. ask: only the user can say this file is the job's (origin_state).
    No model call is made to write it."""
    from . import state
    render = saved["tool"] == "render"
    noun = "render" if render else "job"
    when = " while you were away" if away else ""
    charged, refunded = saved.get("charged"), saved.get("refunded") or 0
    refund = f"{_credits(refunded)} were returned" if refunded else ""
    if saved["status"] == "canceled":
        charge = f"{_credits(charged)} charged" if charged else "Nothing was charged." if charged == 0 else ""
        return state.item_cloud_back(saved["id"], "canceled", f"Your cloud {noun} was canceled{when}",
                                     " · ".join(p for p in (charge, refund) if p), [])
    if saved["status"] == "failed":
        message = saved.get("error") or "It stopped with an error."
        message = message if len(message) <= 160 else message[:159].rstrip() + "…"
        return state.item_cloud_back(saved["id"], "failed", f"Your cloud {noun} failed{when}",
                                     " · ".join(p for p in (message, refund) if p), [])
    frames = (saved.get("frames_done") or saved.get("frames")) if render else None
    parts = ([f"{frames:,} frame{'' if frames == 1 else 's'}" if frames else "", size_label(saved.get("resolution"))]
             if render else [])
    parts += [took(saved.get("created_at", ""), saved.get("finished_at", "")),
              f"{_credits(charged)} charged" if charged else "No charge" if charged == 0 else ""]
    lines = [" · ".join(p for p in parts if p)]
    partial = saved.get("partial")
    if partial:
        lines.append(f"Stopped early: {partial['delivered']:,} of {partial['requested']:,} frames.")
    title = f"Your cloud render finished{when}" if render else f"{saved['title']} finished{when}"
    if saved.get("expired"):
        return state.item_cloud_back(saved["id"], "succeeded", title, "\n".join(lines + [saved["expired"]]), [], frames)
    if ask:
        lines.append("It started in an unsaved file, or its file has moved." if saved["blend"] else
                     "It started in an unsaved file.")
        actions = [{"label": "Load here anyway", "do": "load_here"}]
    else:
        actions = ([{"label": "Open the MP4", "do": "open_mp4"}] if render and saved.get("video") else
                   [{"label": "Make the MP4", "do": "mp4"}] if render and (frames or 0) > 1 else [])
        actions.append({"label": ("Load the frames" if (frames or 0) > 1 else "Load the frame") if render
                        else "Load the result", "do": "load"})
    item = state.item_cloud_back(saved["id"], "succeeded", title, "\n".join(lines), actions, frames)
    item["job_title"] = saved["title"]  # Its Background jobs row: "Ready · <title>", as its running row was named.
    item["thumbnail_labels"] = [f"Frame {f:,}" for f in saved.get("preview_frames", [])][:THUMBNAILS]
    resolution = saved.get("resolution")
    if resolution and all(_count(n) for n in resolution):  # Each preview drawn in its frames' shape (ui/layout.py).
        item["thumbnail_aspect"] = resolution[1] / resolution[0]
    return item


def finished(conversation: str, saved: dict, job, *, away: bool) -> None:
    """Main thread: a job is over, so its chat says so, once per opening. A failed, canceled or expired one is said
    once ever; a finished one each time the chat opens until its outputs are in the file. job: the service's copy
    when this opening has one, for the render's previews."""
    from . import cloud, mainthread, state
    session = state.session()
    if session.get("id") != conversation or applied_before(saved):
        return
    if any(i.get("kind") == "cloud_back" and i.get("job_id") == saved["id"] for i in session["items"]):
        return  # Opened twice: one card.
    with cloud._jobs_lock:
        row = cloud._jobs.get(saved["id"])
    item = card(saved, away=away, ask=row is not None and saved["status"] == "succeeded" and where(row) == "ask")
    session["items"].append(item)
    if not item["actions"]:
        update(saved, seen=True)
    elif saved.get("preview_frames"):
        _thumbnails(session, saved, item, job)
    mainthread.request_redraw()


def _thumbnails(session: dict, saved: dict, item: dict, job) -> None:
    """The render's preview images on its card (meta.preview, their meta.frame as labels): kept with the chat once
    fetched, verified like any output. job: the service's or this Blender's copy, or None to ask for it once."""
    from . import cloud_jobs, conversations, mainthread
    kept = [p for p in saved.get("previews") or () if Path(p).is_file()]
    if kept:
        item["thumbnails"] = kept[:THUMBNAILS]
        return

    def chosen(job):
        return sorted((o for o in job.get("outputs") or () if cloud_jobs.preview(o) and o["mime"].startswith("image/")
                       and o.get("expired") is not True),
                      key=lambda o: (o["meta"].get("frame", 0), o["name"]))[:THUMBNAILS]

    def store(paths, frames):
        stored = []
        for path in paths:
            holder = {}
            try:
                conversations.store_image(session, Path(path), holder)
            except (OSError, ValueError, conversations.ConversationError) as ex:
                print(f"Loopcut: could not keep a render preview: {ex}")
                continue
            stored.append(holder["image_path"])
        item["thumbnails"] = stored
        item["thumbnail_labels"] = [f"Frame {f:,}" if _count(f) else "" for f in frames]
        if stored:
            update(saved, previews=stored)
        mainthread.request_redraw()
    previews = chosen(job) if isinstance(job, dict) else []
    if previews and all(o.get("path") for o in previews):  # Downloaded here already (followed to its end here).
        store([o["path"] for o in previews], [o["meta"].get("frame") for o in previews])
        return
    client = cloud_jobs._client()

    def fetch():
        try:
            current = job if previews else cloud_jobs.fetch(client, saved["id"])
            wanted = chosen(current)
            if not wanted:
                return
            fetched = cloud_jobs._download(client, {**current, "outputs": wanted}, threading.Event())
        except Exception as ex:  # The card stands without its pictures.
            print(f"Loopcut: could not fetch a render's previews: {ex}")
            return
        paths = [o["path"] for o in fetched["outputs"]]

        def land():
            import shutil
            try:
                store(paths, [o["meta"].get("frame") for o in wanted])
            finally:
                shutil.rmtree(Path(paths[0]).parent, ignore_errors=True)
        mainthread.run_on_main(land)
    threading.Thread(target=fetch, name="Loopcut render previews", daemon=True).start()


def act(session: dict, index: int, choice: int) -> None:
    """Main thread: a button on a cloud job's card. Load applies the outputs as job_status does, with no model
    call; Load here anyway does so in a file Loopcut can't match to the job; Make the MP4 sends a user turn, whose
    agent fetches the frames through job_status, and Load stays until they are in. Open the MP4 (a render that
    returned one) opens the copy kept beside its frames, loading them first when they are not in yet; it stays
    once they are."""
    from . import mainthread
    items = session["items"]
    if not 0 <= index < len(items) or items[index].get("kind") != "cloud_back":
        return
    item = items[index]
    if item.get("busy") or not 0 <= choice < len(item["actions"]):
        return
    do = item["actions"][choice]["do"]
    if item.get("resolved") and do != "open_mp4":
        return
    item["error"] = ""
    if do == "mp4":
        _make_mp4(item)
    elif do == "open_mp4":
        if item.get("resolved"):
            _open_mp4(item)
        elif not _open_mp4(item, quiet=True):
            item["open"] = True  # Kept with the frames as they load (cloud_jobs._load_frames), then opened.
            load(session, item)
    elif do in ("load", "load_here"):
        load(session, item, here=do == "load_here")
    mainthread.request_redraw()


def _make_mp4(item: dict) -> None:
    from . import agent, version_guard
    try:
        sent = agent.send(f"Make the MP4 from the frames of my cloud render (job {item['job_id']}).")
    except version_guard.RestartNeeded as ex:
        item["error"] = str(ex)
        return
    if sent:  # If the agent can't, the frames still load from here.
        item["note"] = "Asked Loopcut to make the MP4."
        item["actions"] = [a for a in item["actions"] if a["do"] != "mp4"]


def _open_mp4(item: dict, *, quiet: bool = False) -> bool:
    """Open the render's MP4 kept beside its frames in the system's player, as Background jobs' Output does, and say
    where it went: the player opens outside Blender, often behind it (rel019-cloud2: "did nothing I can see").
    quiet: say nothing when it is not there yet (Open the MP4 then loads the frames first)."""
    import bpy
    from . import cloud_jobs, state
    try:
        path = cloud_jobs.kept_video(item["job_id"])
    except (AttributeError, OSError, RuntimeError, ValueError):
        path = None
    if path is None:
        if not quiet:
            item["error"] = "The MP4 isn't beside the frames any more. Load the frames again to get it back."
        return False
    try:
        opened = "FINISHED" in bpy.ops.wm.path_open(filepath=str(path))
    except (AttributeError, RuntimeError, TypeError) as ex:
        print(f"Loopcut: could not open a render's MP4: {ex!r}")
        opened = False
    if not opened:
        item["error"] = f"Couldn't open the MP4. It is at {path}."
    else:  # UI only, as a file card's Open says it (ui/host.py reference_open).
        state.ui["action_notice"] = {"text": f"Opened {path.name} in your default app (outside Blender).",
                                     "until": time.time() + 6.0}
    return True


def load(session: dict, item: dict, *, here: bool = False) -> None:
    """Apply a finished job's outputs through job_status (fetched and verified first, on a thread)."""
    from . import cloud, cloud_jobs, mainthread
    job_id, conversation = item["job_id"], session["id"]
    result = cloud._job_status({"job_id": job_id}, here=here)
    with cloud._jobs_lock:
        row = cloud._jobs.get(job_id)
    if result.ok and result.text.startswith("{"):  # Still fetching or downloading: try again once it is over here.
        item["busy"] = "Fetching the frames…" if item.get("frames") else "Fetching the result…"

        def again(job, error):
            mainthread.run_on_main(lambda: _again(conversation, job_id, here))
        attached = cloud_jobs.watch(job_id, again)
        if not attached and row is not None and row.get("saved"):
            follow(row, again)
            attached = cloud_jobs.known(job_id)
        if not attached:
            item["busy"] = ""
            item.pop("open", None)  # Nothing loads now, so a later Load the frames never opens it unasked.
        return
    item["busy"] = ""
    if row is not None and row.get("already"):  # Not loaded now: the file has them from earlier.
        item["resolved"] = "These are in this file already, from an earlier load."
    elif not result.ok:
        item["error"] = result.text if len(result.text) <= 300 else result.text[:299] + "…"
        item.pop("open", None)
    else:
        item["resolved"] = _loaded(item)
    if item.pop("open", False):
        _open_mp4(item)


def _again(conversation: str, job_id: str, here: bool) -> None:
    from . import mainthread, state
    session = state.session()
    if session.get("id") != conversation:
        return  # Load is pressed again when this chat opens.
    item = next((i for i in session["items"] if i.get("kind") == "cloud_back" and i.get("job_id") == job_id), None)
    if item is not None and item.get("busy"):
        item["busy"] = ""
        load(session, item, here=here)
        mainthread.request_redraw()
    elif item is not None and item.get("resolved") and item.pop("open", False):
        _open_mp4(item)  # The model's job_status loaded them while the card waited (applied): the click still opens.
        mainthread.request_redraw()
