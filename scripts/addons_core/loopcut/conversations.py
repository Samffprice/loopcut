"""Conversations on disk, so they outlive a Blender session.

Stored in Loopcut's data folder, never inside the .blend: a shared .blend must not carry the
chats that built it, and the user's file is not ours to write. A conversation remembers every
file it has worked in (Save As and checkpoint restores add to the list); opening any of them
resumes the most recent conversation for it.

Layout, per conversation id:  conversations/<id>/conversation.json, meta.json, images/<sha>.png
(every stored image is named .png, but most hold JPEG, image_budget.compact: media_type reads the bytes)
Viewport captures are stored once as files and referenced from messages as `loopcut-image:<name>`;
inline base64 would make every save rewrite megabytes. wire_messages() expands them per request.
"""

import base64
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

from . import checkpoints, context, image_budget, state

IMAGE_SCHEME = "loopcut-image:"
_ID = re.compile(r"^[0-9a-f]{32}$")
_IMAGE_NAME = re.compile(r"^[0-9a-f]{64}\.png$")
_AUDIO_SUFFIXES = (".wav", ".mp3", ".ogg", ".flac", ".m4a", ".aac", ".opus", ".bin")  # aud reads by content.
_AUDIO_NAME = re.compile(r"^[0-9a-f]{64}(?:" + "|".join(map(re.escape, _AUDIO_SUFFIXES)) + ")$")
_ITEM_KINDS = {"user", "assistant", "tool", "error", "notice", "changes", "limit", "wave", "merge"}
# Which images are still sent is context.kept_images; an image costs about ten tool results.
KEEP_IMAGES, KEEP_ATTACHED = context.KEEP_IMAGES, context.KEEP_ATTACHED
PRIVATE_PREFIX, ATTACHED = context.PRIVATE_PREFIX, context.ATTACHED
_ROLES = {"user", "assistant", "tool"}
# Every key of state.new_session()'s usage: a resumed conversation's turns add to each of them, so one
# missing here (as "cached" was) fails the first turn after a reopen. Older files without one load it as 0.
_USAGE = ("input", "cached", "output", "context")
_PERSISTED = ("id", "items", "messages", "input", "attachments", "references", "projects", "title", "created", "usage",
              "mode", "codex_thread", "spend")
_lock = threading.Lock()  # Saves come from the agent thread and the main thread.
# Conversations kept somewhere other than root(): a worker's transcript and captures live in its
# job folder (waves.py). Keyed by conversation id; set before the first save.
FOLDERS: dict[str, Path] = {}


class ConversationError(Exception):
    pass


def root() -> Path:
    return checkpoints.data_root() / "conversations"


def _folder(conversation_id: str) -> Path:
    if not _ID.match(conversation_id):
        raise ConversationError(f"Bad conversation id {conversation_id!r}")
    return FOLDERS.get(conversation_id) or root() / conversation_id


def project_key(filepath: str) -> str:
    """The same file must give the same key however its path was spelled."""
    return os.path.normcase(str(Path(filepath).resolve())) if filepath else ""


def _write_atomic(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def save(session: dict) -> None:
    """Safe from any thread. A conversation nobody has written in is not worth a folder. Offer cards
    (state.item_offer) are left out: they belong to the moment, and older add-ons refuse unknown items."""
    items = [i for i in session["items"] if i.get("kind") != "offer"]
    if not items:
        return
    with _lock:
        folder = _folder(session["id"])
        folder.mkdir(parents=True, exist_ok=True)
        if not session["title"]:
            first = next((i["text"] for i in session["items"] if i["kind"] == "user"), "")
            session["title"] = " ".join(first.split())[:80]
        body = {key: session.get(key, "") if key in ("mode", "codex_thread") else session[key] for key in _PERSISTED
                if key != "spend"}
        from . import spending
        with spending.lock:
            body["spend"] = dict(spending.ledger(session))  # What this chat spent survives a reload (C47).
        body["items"] = items
        meta = {"id": session["id"], "projects": session["projects"], "title": session["title"],
                "created": session["created"], "updated": time.time(),
                "turns": sum(1 for i in session["items"] if i["kind"] == "user")}
        _write_atomic(folder / "conversation.json", json.dumps(body))
        _write_atomic(folder / "meta.json", json.dumps(meta))


def _own_image_path(value) -> bool:
    """"" or a path store_image makes: <a conversation's folder>/images/<sha256>.png (a worker's conversation
    folder is named after the worker, so the folder's own name is not checked). Resolve symlinks
    before checking ownership; conversation and worker captures must stay under Loopcut's data root."""
    if value == "":
        return True
    if not isinstance(value, str):
        return False
    path = Path(value)
    if not (path.is_absolute() and _IMAGE_NAME.match(path.name) is not None and path.parent.name == "images"
            and ".." not in path.parts):
        return False
    try:
        resolved, data_root = path.resolve(), checkpoints.data_root().resolve()
        if resolved.is_relative_to(data_root):
            return True
        # POSIX path comparisons remain case-sensitive on macOS's usual case-insensitive volumes.
        # Compare ancestors by filesystem identity after resolving symlinks, including file symlinks.
        for parent in resolved.parents:
            try:
                if parent.samefile(data_root):
                    return True
            except OSError:
                continue  # A missing capture directory can still have an existing, owned ancestor.
        return False
    except (OSError, RuntimeError, ValueError):
        return False


def _validate_preview(item: dict) -> None:
    """A preview is optional: an unsafe or unreadable path must never hide the saved conversation."""
    if not _own_image_path(item.get("image_path", "")):
        print(f"Loopcut: dropping invalid preview path: {item.pop('image_path')!r}")


def _audio_card(item: dict) -> bool:
    """A notice's sound (state.item_audio). Its length and peaks are drawn, so a NaN or a string would break the
    panel; Play reads only a copy store_audio made in a conversation's own audio folder."""
    audio = item["audio"]
    path = Path(audio["path"]) if isinstance(audio, dict) and isinstance(audio.get("path"), str) else None
    return (item["kind"] == "notice" and path is not None and isinstance(audio.get("name"), str)
            and path.is_absolute() and _AUDIO_NAME.match(path.name) is not None and path.parent.name == "audio"
            and ".." not in path.parts
            and type(audio.get("seconds")) in (int, float) and 0 <= audio["seconds"] < float("inf")
            and isinstance(audio.get("peaks"), list) and len(audio["peaks"]) <= 256
            and all(type(p) in (int, float) and 0 <= p <= 1 for p in audio["peaks"]))


def _validate(body) -> dict:
    """The file is outside our control once written; do not trust its shape."""
    if not isinstance(body, dict) or not _ID.match(str(body.get("id", ""))):
        raise ConversationError("not a conversation file")
    items, messages = body.get("items"), body.get("messages")
    if not isinstance(items, list) or not isinstance(messages, list):
        raise ConversationError("items and messages must be lists")
    projects = body.get("projects", [])
    if not isinstance(projects, list) or any(not isinstance(p, str) for p in projects):
        raise ConversationError("projects must be a list of paths")
    for key in ("attachments", "references"):
        entries = body.get(key, [])
        if not isinstance(entries, list) or any(
                not isinstance(a, dict) or not isinstance(a.get("name"), str)
                or not isinstance(a.get("pinned", True), bool)
                or any(not _IMAGE_NAME.match(str(a.get(field, a.get("ref", "")))[len(IMAGE_SCHEME):])
                       for field in ("ref", "full")) for a in entries):
            raise ConversationError(f"malformed {key}")
    for item in items:
        if not isinstance(item, dict) or item.get("kind") not in _ITEM_KINDS:
            raise ConversationError(f"unknown item: {str(item)[:80]}")
        required = ("name", "summary", "code", "status", "output") if item["kind"] == "tool" else ("text",)
        if any(not isinstance(item.get(key), str) for key in required):
            raise ConversationError(f"malformed {item['kind']} item")
        refs = item.get("file_references", [])
        if not isinstance(refs, list) or any(
                not isinstance(ref, dict) or ref.get("kind") not in ("file", "url")
                or any(not isinstance(ref.get(key), str) for key in ("name", "target")) for ref in refs):
            raise ConversationError("malformed file references")
        _validate_preview(item)
        if "audio" in item and not _audio_card(item):
            raise ConversationError("malformed audio item")
        lines = item.get("lines", []) if item["kind"] in ("changes", "merge") else item.get("images", [])
        if not isinstance(lines, list) or any(not isinstance(line, str) for line in lines):
            raise ConversationError(f"malformed {item['kind']} item")
        rows = item.get("rows", [])
        if item["kind"] == "wave":
            if not isinstance(rows, list) or any(
                    not isinstance(r, dict) or any(not isinstance(r.get(k), str) for k in ("id", "status"))
                    for r in rows):
                raise ConversationError("malformed wave item")
            for row in rows:
                _validate_preview(row)
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in _ROLES:
            raise ConversationError(f"unknown message: {str(message)[:80]}")
        if any(k.startswith(PRIVATE_PREFIX) and not isinstance(v, (str, int, bool)) for k, v in message.items()):
            raise ConversationError(f"malformed message marker: {str(message)[:80]}")
    if not isinstance(body.get("codex_thread", ""), str):
        raise ConversationError("malformed codex_thread")
    usage = body.get("usage", {})
    if not isinstance(usage, dict) or any(not isinstance(usage.get(k, 0), int) for k in _USAGE):
        raise ConversationError("malformed usage")
    from . import spending
    if "spend" in body and not spending.valid_ledger(body["spend"]):
        body.pop("spend")  # A damaged ledger starts over rather than losing the conversation.
    return body


def load(conversation_id: str) -> dict:
    path = _folder(conversation_id) / "conversation.json"
    try:
        body = _validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as ex:
        raise ConversationError(f"Could not read {path}: {ex}") from ex
    if body["id"] != conversation_id:
        raise ConversationError(f"{path} claims to be conversation {body['id']}")
    session = state.new_session()
    session.update({key: body[key] for key in _PERSISTED if key in body})
    session["usage"] = {key: int(body.get("usage", {}).get(key, 0)) for key in _USAGE}
    from . import spending
    session["spend"] = {key: body.get("spend", {}).get(key, value) for key, value in spending.new_ledger().items()}
    # Whatever was in flight when Blender closed is over.
    for item in session["items"]:
        if item.get("streaming"):
            item["streaming"] = False
        if item.get("status") in ("awaiting", "running"):
            item["status"] = "rejected"
        for row in item.get("rows", []) if item["kind"] == "wave" else ():
            if row["status"] in state.WAVE_RUNNING:
                row["status"] = "stopped"
    session["turn_number"] = sum(1 for i in session["items"] if i["kind"] == "user")
    close_open_tool_calls(session["messages"], "Blender was closed before this ran.")
    rename_merged_tools(session["messages"])
    session["cursor"] = len(session["input"])
    return session


def rename_merged_tools(messages: list) -> None:
    """Calls to tools since merged into one (tools.RENAMED), respelled as the current tool, so the
    model is shown only tools it is offered. The chat's cards keep the name and label they had."""
    from . import tools
    for message in messages:
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            if function.get("name") not in tools.RENAMED:
                continue
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(arguments, dict):
                continue
            name, current = tools.current_call(function["name"], arguments)
            if (name, current) != (function["name"], arguments):
                function["name"], function["arguments"] = name, json.dumps(current)


def close_open_tool_calls(messages: list, reason: str) -> None:
    """The API rejects a history where a tool call has no result. Wherever a turn was cut short,
    answer the unanswered calls so the conversation can continue."""
    position = 0
    while position < len(messages):
        message = messages[position]
        position += 1
        calls = message.get("tool_calls") if message["role"] == "assistant" else None
        if not calls:
            continue
        answered = set()
        while position < len(messages) and messages[position]["role"] == "tool":
            answered.add(messages[position].get("tool_call_id"))
            position += 1
        for call in calls:
            if call["id"] not in answered:
                messages.insert(position, {"role": "tool", "tool_call_id": call["id"], "content": reason})
                position += 1


def list_for(project: str) -> list[dict]:
    """Newest first. Unreadable entries are skipped, never deleted."""
    found = []
    if not root().is_dir():
        return found
    for folder in root().iterdir():
        if not (folder.is_dir() and _ID.match(folder.name)):
            continue
        try:
            meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(meta, dict) and meta.get("id") == folder.name and project in (meta.get("projects") or []):
            found.append(meta)
    return sorted(found, key=lambda m: m.get("updated", 0), reverse=True)


def resume_or_new(filepath: str) -> dict:
    """The session to show for this file: its latest conversation, or a fresh one. An unsaved
    scene always starts fresh, since 'untitled' says nothing about which scene this is."""
    project = project_key(filepath)
    if project:
        for meta in list_for(project):
            try:
                return load(meta["id"])
            except ConversationError as ex:
                print(f"Loopcut: skipping unreadable conversation: {ex}")
    session = state.new_session()
    add_project(session, filepath)
    return session


def add_project(session: dict, filepath: str) -> None:
    project = project_key(filepath)
    if project and project not in session["projects"]:
        session["projects"].append(project)


def store_image(session: dict, source: Path, card: dict | None = None, *, side: int = image_budget.STORE_SIDE) -> str:
    """Copy an image into the conversation, compact (image_budget.compact: at most `side` px, JPEG unless it has
    transparency or PNG is smaller), and return the reference to put in a message. Every request re-sends it."""
    data = image_budget.compact(source.read_bytes(), side)
    name = hashlib.sha256(data).hexdigest() + ".png"
    folder = _folder(session["id"]) / "images"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    if not target.is_file():
        target.write_bytes(data)
    if card is not None:
        card["image_path"] = str(target)
    return IMAGE_SCHEME + name


def store_audio(session: dict, source: Path) -> str:
    """Copy a generated sound into the conversation, for its chat card: the download is in a temp folder the
    system may clear, and the .blend's packed copy is not a file Play can read. Returns the copy's path."""
    data = source.read_bytes()
    suffix = source.suffix.lower()
    name = hashlib.sha256(data).hexdigest() + (suffix if suffix in _AUDIO_SUFFIXES else ".bin")
    folder = _folder(session["id"]) / "audio"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    if not target.is_file():
        target.write_bytes(data)
    return str(target)


def media_type(data: bytes, default: str = "image/png") -> str:
    """The image type its first bytes say; a stored image's .png name does not."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return default


def image_path(conversation_id: str, reference: str) -> Path:
    name = reference[len(IMAGE_SCHEME):]
    if not reference.startswith(IMAGE_SCHEME) or not _IMAGE_NAME.match(name):
        raise ConversationError(f"Bad image reference {reference[:80]!r}")
    return _folder(conversation_id) / "images" / name


def wire_messages(conversation_id: str, messages: list, unpinned: frozenset = frozenset(), keep: bool = False,
                  *, full_refs: dict | None = None) -> list:
    """Messages as the API wants them: stored image references become data URIs, and only the
    images context.kept_images names are sent; the conversation on disk keeps them all. Workspace
    full_refs substitutes the larger attached copies on the wire, retaining the stored refs for pinning."""
    kept = context.kept_messages(messages, unpinned, keep)
    wired = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            parts = []
            for part in content:
                url = part.get("image_url", {}).get("url", "") if part.get("type") == "image_url" else ""
                if url and (id(message) not in kept or url in unpinned):
                    part = {"type": "text", "text": "[an earlier image, no longer attached]"}
                elif url.startswith(IMAGE_SCHEME):
                    path = image_path(conversation_id, url)
                    if full_refs and message.get(ATTACHED) and url in full_refs:
                        full = image_path(conversation_id, full_refs[url])
                        if full.is_file():
                            path = full
                    if path.is_file():
                        data = path.read_bytes()
                        url = f"data:{media_type(data)};base64,{base64.b64encode(data).decode('ascii')}"
                        part = {**part, "image_url": {**part["image_url"], "url": url}}
                    else:
                        part = {"type": "text", "text": "[image no longer available]"}
                parts.append(part)
            message = message | {"content": parts}
        wired.append({key: value for key, value in message.items() if not key.startswith(PRIVATE_PREFIX)})
    return wired
