"""Scene checkpoints: restore the scene and the conversation to before any message.

The rule everything here serves: a restore never destroys work. Restoring first snapshots the
current state (including manual edits made since), so a restore is itself a checkpoint and can
be undone. The user's own .blend on disk is never written to.

A snapshot is a compressed copy-save of the whole .blend. One is taken per user message, lazily,
right before the first scene-changing step of that turn, so chat-only turns cost nothing.

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


def begin_turn(session: dict, user_item: dict) -> None:
    """Call before the user's message is appended. Nothing is written yet."""
    session["pending_checkpoint"] = {
        "item": user_item,
        "message_index": len(session["messages"]),
        "item_index": len(session["items"]),
    }


def ensure_for_turn(session: dict) -> None:
    """Main thread. Snapshot the scene if this turn has not yet. Raises CheckpointError, in
    which case the caller must not go on to change the scene."""
    pending = session.get("pending_checkpoint")
    if not pending:
        return
    entry = _write_snapshot(_store(session), "turn", pending["item"]["text"],
                            message_index=pending["message_index"], item_index=pending["item_index"])
    pending["item"]["checkpoint"] = entry["id"]
    session["pending_checkpoint"] = None


def status(session: dict, checkpoint_id: str) -> str:
    """'ok' | 'expired' | 'missing', for the UI."""
    try:
        entry = _store(session).get(checkpoint_id)
    except CheckpointError:
        return "missing"
    return "expired" if entry["expired"] else "ok"


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
        text = next((i["text"] for i in removed if i["kind"] == "user"), "")
        del session["messages"][cut_messages:]
        del session["items"][cut_items:]
        session["input"], session["cursor"], session["anchor"] = text, len(text), None
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
