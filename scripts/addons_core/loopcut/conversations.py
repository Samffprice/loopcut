"""Conversations on disk, so they outlive a Blender session.

Stored in Loopcut's data folder, never inside the .blend: a shared .blend must not carry the
chats that built it, and the user's file is not ours to write. A conversation remembers every
file it has worked in (Save As and checkpoint restores add to the list); opening any of them
resumes the most recent conversation for it.

Layout, per conversation id:  conversations/<id>/conversation.json, meta.json, images/<sha>.png
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

from . import checkpoints, state

IMAGE_SCHEME = "loopcut-image:"
_ID = re.compile(r"^[0-9a-f]{32}$")
_IMAGE_NAME = re.compile(r"^[0-9a-f]{64}\.png$")
_ITEM_KINDS = {"user", "assistant", "tool", "error", "notice", "changes"}
KEEP_IMAGES = 4  # Viewport captures sent per request; older ones cost tokens and show a stale scene.
KEEP_ATTACHED = 8  # Images the user attached, counted apart: a reference photo must outlive captures.
ATTACHED = "loopcut_attached"  # Marks a user message whose images the user attached. Never sent.
_ROLES = {"user", "assistant", "tool"}
_PERSISTED = ("id", "items", "messages", "input", "attachments", "projects", "title", "created")
_lock = threading.Lock()  # Saves come from the agent thread and the main thread.


class ConversationError(Exception):
    pass


def root() -> Path:
    return checkpoints.data_root() / "conversations"


def _folder(conversation_id: str) -> Path:
    if not _ID.match(conversation_id):
        raise ConversationError(f"Bad conversation id {conversation_id!r}")
    return root() / conversation_id


def project_key(filepath: str) -> str:
    """The same file must give the same key however its path was spelled."""
    return os.path.normcase(str(Path(filepath).resolve())) if filepath else ""


def _write_atomic(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def save(session: dict) -> None:
    """Safe from any thread. A conversation nobody has written in is not worth a folder."""
    if not session["items"]:
        return
    with _lock:
        folder = _folder(session["id"])
        folder.mkdir(parents=True, exist_ok=True)
        if not session["title"]:
            first = next((i["text"] for i in session["items"] if i["kind"] == "user"), "")
            session["title"] = " ".join(first.split())[:80]
        body = {key: session[key] for key in _PERSISTED}
        meta = {"id": session["id"], "projects": session["projects"], "title": session["title"],
                "created": session["created"], "updated": time.time(),
                "turns": sum(1 for i in session["items"] if i["kind"] == "user")}
        _write_atomic(folder / "conversation.json", json.dumps(body))
        _write_atomic(folder / "meta.json", json.dumps(meta))


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
    attachments = body.get("attachments", [])
    if not isinstance(attachments, list) or any(
            not isinstance(a, dict) or not isinstance(a.get("name"), str)
            or not _IMAGE_NAME.match(str(a.get("ref", ""))[len(IMAGE_SCHEME):]) for a in attachments):
        raise ConversationError("malformed attachments")
    for item in items:
        if not isinstance(item, dict) or item.get("kind") not in _ITEM_KINDS:
            raise ConversationError(f"unknown item: {str(item)[:80]}")
        required = ("name", "summary", "code", "status", "output") if item["kind"] == "tool" else ("text",)
        if any(not isinstance(item.get(key), str) for key in required):
            raise ConversationError(f"malformed {item['kind']} item")
        lines = item.get("lines", []) if item["kind"] == "changes" else item.get("images", [])
        if not isinstance(lines, list) or any(not isinstance(line, str) for line in lines):
            raise ConversationError(f"malformed {item['kind']} item")
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in _ROLES:
            raise ConversationError(f"unknown message: {str(message)[:80]}")
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
    # Whatever was in flight when Blender closed is over.
    for item in session["items"]:
        if item.get("streaming"):
            item["streaming"] = False
        if item.get("status") in ("awaiting", "running"):
            item["status"] = "rejected"
    close_open_tool_calls(session["messages"], "Blender was closed before this ran.")
    session["cursor"] = len(session["input"])
    return session


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


def store_image(session: dict, source: Path) -> str:
    """Copy a capture into the conversation and return the reference to put in a message."""
    data = source.read_bytes()
    name = hashlib.sha256(data).hexdigest() + ".png"
    folder = _folder(session["id"]) / "images"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    if not target.is_file():
        target.write_bytes(data)
    return IMAGE_SCHEME + name


def image_path(conversation_id: str, reference: str) -> Path:
    name = reference[len(IMAGE_SCHEME):]
    if not reference.startswith(IMAGE_SCHEME) or not _IMAGE_NAME.match(name):
        raise ConversationError(f"Bad image reference {reference[:80]!r}")
    return _folder(conversation_id) / "images" / name


def wire_messages(conversation_id: str, messages: list) -> list:
    """Messages as the API wants them: stored image references become data URIs. Only the newest
    KEEP_IMAGES captures and KEEP_ATTACHED attached images are sent; the conversation on disk
    keeps them all."""
    dropped = set()
    for attached, keep in ((False, KEEP_IMAGES), (True, KEEP_ATTACHED)):
        references = [part["image_url"]["url"] for message in messages
                      if isinstance(message.get("content"), list) and bool(message.get(ATTACHED)) == attached
                      for part in message["content"] if part.get("type") == "image_url"]
        dropped |= set(references[:-keep]) - set(references[-keep:])
    wired = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            parts = []
            for part in content:
                url = part.get("image_url", {}).get("url", "") if part.get("type") == "image_url" else ""
                if url in dropped:
                    part = {"type": "text", "text": "[an earlier image, no longer attached]"}
                elif url.startswith(IMAGE_SCHEME):
                    path = image_path(conversation_id, url)
                    if path.is_file():
                        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
                        part = {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}}
                    else:
                        part = {"type": "text", "text": "[image no longer available]"}
                parts.append(part)
            message = {key: value for key, value in message.items() if key != ATTACHED} | {"content": parts}
        wired.append(message)
    return wired
