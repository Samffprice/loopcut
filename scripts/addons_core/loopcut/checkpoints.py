"""Scene checkpoints: restore the scene and the conversation to before any message.

The rule everything here serves: a restore never destroys work. Restoring first snapshots the
current state (including manual edits made since), so a restore is itself a checkpoint and can
be undone. The user's own .blend on disk is never written to.

A snapshot is a compressed copy-save of the whole .blend. One is taken per user message, lazily,
right before the first scene-changing step of that turn, so chat-only turns cost nothing, and one
before each merge of a wave of workers (kind "wave"), which restores to before that wave was asked for.
A cloud render's bundle is collected from its own snapshot of the live scene (kind "bundle"), which is
dropped once collected and never offered as a restore point.

In the Loopcut build of Blender a snapshot is a recovery file (wm.loopcut_snapshot_write): it
records the path of the file it was taken from, so restoring it (wm.loopcut_snapshot_restore)
puts the scene back in place. The open file keeps its path and is marked unsaved.

Stock Blender cannot do that from Python: opening a snapshot leaves Blender's file path pointing
at it, so the user's next Ctrl+S would land in our store. There restore() re-saves straight away
as `<name>.restored-<time>.blend` beside the original. Index entries say which kind they are
(`in_place`), so a store written by one Blender is still restored correctly by the other.
"""

import json
import os
import re
import time
import uuid
from pathlib import Path

INDEX_NAME = "index.json"
DEFAULT_BUDGET_MB = 2048
STALE_SESSION_DAYS = 30
KEEP_NEWEST = 5
_ID = re.compile(r"^[0-9a-f]{32}$")
_RESTORED_SUFFIX = re.compile(r"\.restored-\d{8}-\d{6}(-\d+)?$")


class CheckpointError(Exception):
    pass


# ------------------------------------------------------------------ pure logic (no bpy)

def restored_path(original: str, fallback_dir: Path, now: float, exists=os.path.exists) -> Path:
    """Where a restored scene gets saved. Never an existing file, never inside the store."""
    if original:
        source = Path(original)
        folder, stem = source.parent, _RESTORED_SUFFIX.sub("", source.stem)
    else:
        folder, stem = fallback_dir, "untitled"
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    candidate = folder / f"{stem}.restored-{stamp}.blend"
    counter = 1
    while exists(candidate):
        counter += 1
        candidate = folder / f"{stem}.restored-{stamp}-{counter}.blend"
    return candidate


def plan_pruning(entries: list[dict], budget_bytes: int) -> list[str]:
    """Ids to expire, oldest first, until the live snapshots fit the budget.

    Protected: the first checkpoint (the state before Loopcut touched anything), the newest
    KEEP_NEWEST, and the latest pre-restore snapshot (the only way back from the last restore).
    """
    live = [e for e in entries if not e["expired"]]
    total = sum(e["size"] for e in live)
    if total <= budget_bytes:
        return []
    protected = {e["id"] for e in live[:1]} | {e["id"] for e in live[-KEEP_NEWEST:]}
    pre_restores = [e for e in live if e["kind"] == "pre_restore"]
    if pre_restores:
        protected.add(pre_restores[-1]["id"])
    expire = []
    for entry in live:
        if total <= budget_bytes:
            break
        if entry["id"] not in protected:
            expire.append(entry["id"])
            total -= entry["size"]
    return expire


class Store:
    """The checkpoint folder of one conversation: snapshots, conversation tails, and an index."""

    def __init__(self, root: Path, session_id: str):
        if not _ID.match(session_id):
            raise CheckpointError(f"Bad session id {session_id!r}")
        self.folder = root / "checkpoints" / session_id
        self.entries: list[dict] = []
        index = self.folder / INDEX_NAME
        if index.is_file():
            loaded = json.loads(index.read_text(encoding="utf-8"))
            # The index is a file on disk; ids become file names, so they are checked.
            self.entries = [e for e in loaded if _ID.match(str(e.get("id", "")))]

    def snapshot_path(self, checkpoint_id: str) -> Path:
        return self.folder / f"{checkpoint_id}.blend"

    def tail_path(self, checkpoint_id: str) -> Path:
        return self.folder / f"{checkpoint_id}.tail.json"

    def get(self, checkpoint_id: str) -> dict:
        for entry in self.entries:
            if entry["id"] == checkpoint_id:
                return entry
        raise CheckpointError("That checkpoint no longer exists.")

    def save_index(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        temporary = self.folder / (INDEX_NAME + ".tmp")
        temporary.write_text(json.dumps(self.entries, indent=1), encoding="utf-8")
        os.replace(temporary, self.folder / INDEX_NAME)

    def add(self, entry: dict, budget_bytes: int) -> None:
        self.entries.append(entry)
        for checkpoint_id in plan_pruning(self.entries, budget_bytes):
            self.get(checkpoint_id)["expired"] = True
            self.snapshot_path(checkpoint_id).unlink(missing_ok=True)
            self.tail_path(checkpoint_id).unlink(missing_ok=True)
        self.save_index()


# ------------------------------------------------------------------ Blender side

def data_root() -> Path:
    override = os.environ.get("LOOPCUT_DATA_DIR")
    if override:
        return Path(override)
    import bpy
    return Path(bpy.utils.user_resource("DATAFILES", path="loopcut", create=True))


def _budget_bytes() -> int:
    from . import config
    return config.checkpoint_budget_mb() * 1024 * 1024


def _store(session: dict) -> Store:
    return Store(data_root(), session["id"])


def in_place_supported() -> bool:
    """True in the Loopcut build of Blender. (getattr on bpy.ops never fails, so ask dir().)"""
    import bpy
    return "loopcut_snapshot_restore" in dir(bpy.ops.wm)


def _write_snapshot(store: Store, kind: str, label: str, **extra) -> dict:
    import bpy
    checkpoint_id = uuid.uuid4().hex
    path = store.snapshot_path(checkpoint_id)
    in_place = in_place_supported()
    try:
        store.folder.mkdir(parents=True, exist_ok=True)
        # Either way the open file, its path and its unsaved-changes state are left alone.
        if in_place:
            if bpy.ops.wm.loopcut_snapshot_write(filepath=str(path)) != {"FINISHED"}:
                raise RuntimeError("Blender could not write the snapshot file")
        else:
            bpy.ops.wm.save_as_mainfile(filepath=str(path), copy=True, compress=True)
        size = path.stat().st_size
    except (OSError, RuntimeError) as ex:
        path.unlink(missing_ok=True)
        raise CheckpointError(f"Could not save a checkpoint to {store.folder}: {ex}") from ex
    entry = {"id": checkpoint_id, "kind": kind, "label": label[:80], "created": time.time(),
             "size": size, "expired": False, "source": bpy.data.filepath, "in_place": in_place, **extra}
    store.add(entry, _budget_bytes())
    return entry


def begin_turn(session: dict, user_item: dict, *, text: str | None = None) -> None:
    """Record the request before sending; reused notes may already be in history. Nothing is written yet."""
    session["pending_checkpoint"] = {
        "item": user_item,
        "input": user_item["text"] if text is None else text,
        "message_index": len(session["messages"]),
        "item_index": next((i for i, item in enumerate(session["items"]) if item is user_item), len(session["items"])),
    }


def ensure_for_turn(session: dict) -> None:
    """Main thread. Snapshot the scene if this turn has not yet. Raises CheckpointError, in
    which case the caller must not go on to change the scene."""
    pending = session.get("pending_checkpoint")
    if not pending:
        return
    entry = _write_snapshot(_store(session), "turn", pending["item"]["text"],
                            message_index=pending["message_index"], item_index=pending["item_index"],
                            input=pending["input"])
    pending["item"]["checkpoint"] = entry["id"]
    session["pending_checkpoint"] = None


def ensure_for_wave(session: dict, label: str, message_index: int, item_index: int) -> str:
    """Main thread, as a wave's workers start (waves.prepare): snapshot the scene and return the
    checkpoint's id, which each of the wave's merges names. message_index and item_index point at the
    orchestrator's delegate message and tool card (or the wave card when started directly), so restoring
    puts the scene back to before the delegate call, every merge of the wave undone, and cuts the
    conversation to just before the wave was asked for. Raises CheckpointError; the wave must not
    start without one."""
    entry = _write_snapshot(_store(session), "wave", label, message_index=message_index, item_index=item_index)
    return entry["id"]


def for_bundle(session: dict) -> str:
    """Main thread, as a cloud tool with a bundle input is called (cloud.execute): snapshot the scene as it
    is now, this turn's edits included, and return the id bundle_from_checkpoint collects. Not a restore
    point and not the turn's checkpoint; discard() drops it once collected. Raises CheckpointError."""
    return _write_snapshot(_store(session), "bundle", "Cloud bundle")["id"]


def discard(session: dict, checkpoint_id: str) -> None:
    """Main thread: remove a snapshot nothing restores (a bundle's, once collected), so it never takes
    the storage budget from the conversation's checkpoints. A missing one is already gone."""
    store = _store(session)
    try:
        entry = store.get(checkpoint_id)
    except CheckpointError:
        return
    store.snapshot_path(checkpoint_id).unlink(missing_ok=True)
    store.entries.remove(entry)
    store.save_index()


def status(session: dict, checkpoint_id: str) -> str:
    """'ok' | 'expired' | 'missing', for the UI."""
    try:
        entry = _store(session).get(checkpoint_id)
    except CheckpointError:
        return "missing"
    return "expired" if entry["expired"] else "ok"


def bundle_from_checkpoint(session: dict, checkpoint_id: str, staging_dir) -> dict:
    """Collect one indexed checkpoint without opening it in the live Blender. No upload.

    Return {manifest, missing, unsupported, system}; callers can review dependencies.
    Recovery files preserve source-relative paths, while stock copy-saves rebase them.
    """
    from . import bundles
    import threading
    if threading.current_thread() is threading.main_thread():
        store = _store(session)
    else:
        from . import mainthread
        store = mainthread.run_on_main(lambda: _store(session)).result(timeout=30)
    entry = store.get(checkpoint_id)
    snapshot = store.snapshot_path(checkpoint_id)
    if entry["expired"] or not snapshot.is_file():
        raise CheckpointError("That checkpoint has expired to stay within the storage budget.")
    original = entry.get("source", "") if entry.get("in_place", False) else None
    info = bundles.collect(snapshot, staging_dir, original_path=original)
    info["created_at"] = bundles._timestamp(entry["created"])
    source = {"kind": "checkpoint", "session": session["id"], "checkpoint": checkpoint_id}
    return {"manifest": bundles.manifest(staging_dir, info, source),
            "missing": info["missing"], "unsupported": info["unsupported"], "system": info["system"]}


def _blocking_job() -> str | None:
    import bpy
    for job in ("RENDER", "RENDER_PREVIEW", "OBJECT_BAKE", "COMPOSITE", "SHADER_COMPILATION"):
        if bpy.app.is_job_running(job):
            return job
    return None


def restore(session: dict, checkpoint_id: str) -> Path | None:
    """Main thread, outside any operator. Returns the new file the restored scene was saved to,
    or None when it was restored in place."""
    import bpy
    from . import state

    if session["busy"]:
        raise CheckpointError("Stop the running request before restoring.")
    job = _blocking_job()
    if job:
        raise CheckpointError(f"Blender is busy ({job.lower().replace('_', ' ')}). Try again when it finishes.")
    store = _store(session)
    target = store.get(checkpoint_id)
    snapshot = store.snapshot_path(checkpoint_id)
    if target["expired"] or not snapshot.is_file():
        raise CheckpointError("That checkpoint has expired to stay within the storage budget.")

    # 1. Save where we are now, with the part of the conversation about to be cut, so this
    #    restore can be undone. If this fails, nothing has been touched yet.
    #    Both kinds of checkpoint record where the conversation was cut, and everything before
    #    that point is common to both branches, so the same cut applies either way.
    cut_messages, cut_items = target["message_index"], target["item_index"]
    undo = _write_snapshot(store, "pre_restore", "Before restore",
                           message_index=cut_messages, item_index=cut_items)
    tail = {"messages": session["messages"][cut_messages:], "items": session["items"][cut_items:],
            "input": session["input"]}
    store.tail_path(undo["id"]).write_text(json.dumps(tail), encoding="utf-8")

    # 2. Load the snapshot. Whichever way it goes, Blender must not end up thinking the snapshot
    #    is the open file: a Ctrl+S must never write into the store.
    in_place = in_place_supported() and target.get("in_place", False)
    open_path = bpy.data.filepath
    destination = None
    state.restoring = True  # Tells lifecycle this file load is ours, not the user opening a file.
    try:
        if in_place:
            if bpy.ops.wm.loopcut_snapshot_restore(filepath=str(snapshot)) != {"FINISHED"}:
                raise CheckpointError("Blender could not read that checkpoint; nothing was changed.")
        else:
            destination = restored_path(target["source"] or undo["source"], data_root() / "restored", time.time())
            destination.parent.mkdir(parents=True, exist_ok=True)
            bpy.ops.wm.open_mainfile(filepath=str(snapshot), load_ui=False)
            bpy.ops.wm.save_as_mainfile(filepath=str(destination), compress=False)
    finally:
        state.restoring = False
    # In place, the file that was open stays the open file; an unsaved scene takes the path the
    # snapshot recorded, which for a scene that was never saved is none.
    expected = str(destination) if destination else (open_path or target["source"])
    if Path(bpy.data.filepath) != Path(expected):
        raise CheckpointError(f"Restored, but Blender is pointing at {bpy.data.filepath or 'no file'}; use Save As.")

    # 3. The conversation.
    if target["kind"] == "turn":
        removed = session["items"][cut_items:]
        text = target.get("input", next((i["text"] for i in removed if i["kind"] == "user"), ""))
        del session["messages"][cut_messages:]
        del session["items"][cut_items:]
        session["input"], session["cursor"], session["anchor"] = text, len(text), None
    elif target["kind"] == "wave":  # Mid-turn: back to before the delegate call, the input left alone.
        del session["messages"][cut_messages:]
        del session["items"][cut_items:]
    else:
        tail_file = store.tail_path(checkpoint_id)
        saved = json.loads(tail_file.read_text(encoding="utf-8")) if tail_file.is_file() else None
        del session["messages"][cut_messages:]
        del session["items"][cut_items:]
        if saved:
            session["messages"].extend(saved["messages"])
            session["items"].extend(saved["items"])
            session["input"], session["cursor"], session["anchor"] = saved["input"], len(saved["input"]), None
    if destination:
        message = f"Restored. You are now working in {destination.name}; your original file was not changed."
    elif open_path:
        message = (f"Restored. {Path(open_path).name} on disk was not changed; save when you want to "
                   f"keep this state.")
    else:
        message = "Restored."
    session["items"].append(state.item_notice(message, "Undo restore", undo["id"]))
    session["scroll"], session["confirm_restore"], session["pending_checkpoint"] = 0.0, None, None
    from . import conversations
    if destination:
        conversations.add_project(session, str(destination))  # The chat belongs to the restored file too.
    conversations.save(session)
    return destination


def remove_stale_sessions(now: float | None = None) -> None:
    """Drop snapshot stores nothing has been added to for STALE_SESSION_DAYS. Snapshots are the
    bulky part; the conversation itself is kept and shows those checkpoints as expired."""
    import shutil
    root = data_root() / "checkpoints"
    if not root.is_dir():
        return
    cutoff = (now or time.time()) - STALE_SESSION_DAYS * 86400
    for folder in root.iterdir():
        if folder.is_dir() and _ID.match(folder.name) and folder.stat().st_mtime < cutoff:
            shutil.rmtree(folder)
