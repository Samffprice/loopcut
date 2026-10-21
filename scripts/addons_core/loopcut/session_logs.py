"""Conversation logs and problem reports: POST /v1/logs and POST /v1/reports (contract SPEC §3.12, proposal 015).

When a hosted chat failed (a four-token reply; a turn that stopped after its workers) nobody could see what was
asked or what broke: the gateway keeps token counts only, and on a ChatGPT plan or the user's own key the server
sees nothing at all. The owner decided on 2026-10-20: "on, 90 days, no training". So, for every model route and
with or without a Loopcut account:
- After each turn, the conversation as text (messages, replies, tool calls and results, the chat's errors and
  notices) and the add-on's console lines since the conversation started here go to /v1/logs, one upload per
  conversation that replaces the last. A turn that delegated also sends each of that turn's worker conversations,
  referenced from the parent by a "worker session" event. Preference: "Share conversations to improve Loopcut"
  (on by default; the first-run privacy screen shows it).
- "Report a Problem…" in Preferences › Add-ons › Loopcut (never the chat panel) sends a note, the recent console
  log and, if ticked, the three latest conversations to /v1/reports, and shows the code the server returns. It
  is an explicit request, so it works with sharing off.

What never goes: images (each becomes an "image omitted" event with its size), .blend files, and credentials. The
Loopcut bearer travels only as the Authorization header. Before anything is sent, every known secret (the keys in
Loopcut's credentials file, ChatGPT's tokens, key-like environment and .env values) is replaced by "[redacted]",
key-shaped strings are caught by pattern (the server redacts again), URL query strings are cut, and the home
folder becomes "~". Long fields keep their head and tail so a body fits 6 MiB decoded and 1 MiB gzipped.

Gating for the logs, as for usage events (telemetry.py): nothing from background processes (workers, -b runs), with
LOOPCUT_TELEMETRY off (the eval harness), with the preference off, or without an install id. Delivery never blocks
the UI or the agent: one daemon thread, a bounded queue that keeps only the newest snapshot of each conversation, a
30 s timeout and one retry a minute later after a network error or a 5xx. 404 (the server's conversation_logs flag
is off, or a server without this route) and 426 stop uploads until the next launch; 429 pauses them for ten
minutes; 409 (the conversation belongs to another install or account) drops that conversation; any other 4xx
drops the upload.

The add-on has no log file, only print() and tracebacks, so register() puts a tee on sys.stdout and sys.stderr:
every write goes through unchanged, and the last LOG_LINES complete lines (at most LOG_BYTES) are kept here.
"""

import copy
import json
import os
import re
import struct
import sys
import threading
import time
import uuid
from collections import OrderedDict, deque
from pathlib import Path

MAX_ENTRIES = 2000              # transcript entries per conversation (the schema's cap); the newest are kept.
FIELD_SIDE = 16 * 1024          # characters kept at each end of a long text field.
MAX_DECODED = 6 * 1024 * 1024   # the body as JSON; the server allows 8 MiB.
MAX_GZIP = 1024 * 1024 - 16 * 1024  # the body on the wire; the server allows 1 MiB.
SESSION_LOG = 256 * 1024        # a conversation's log: its console lines, the tail kept (bytes).
REPORT_LOG = 512 * 1024         # a report's log: the console's recent lines (bytes).
WORKER_LOG = 64 * 1024          # the tail of a worker's own Blender log (worker.log).
NOTE_CHARS = 4000
EMAIL_CHARS = 320
REPORT_SESSIONS = 3
DATA_LINES = 40                 # lines of a changes or merge card kept in its event.
NAME_CHARS = 200                # a tool's name or call id (the schema's cap).

LOG_LINES = 4000                # the console buffer: lines...
LOG_BYTES = 512 * 1024          # ...and characters.
LINE_CHARS = 2000               # one console line, cut beyond this.

TIMEOUT = 30.0
RETRY_AFTER = 60.0
PAUSE_429 = 600.0
QUEUE_SIZE = 16                 # conversations waiting to be sent; the oldest goes first when full.
IDLE_SECONDS = 30.0             # the sender thread ends after this long with nothing to send.
SUPPORT_EMAIL = "hello@loopcut.org"

_ID = re.compile(r"[0-9a-f]{32}\Z")
_INSTALL_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
_IMAGE_NAME = re.compile(r"[0-9a-f]{64}\.png\Z")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\Z")
_CODE = re.compile(r"LC-[A-Z0-9]{4,16}\Z")
_DATA_URI = re.compile(r"data:[\w/+.-]+;base64,[A-Za-z0-9+/=\s]{64,}")
_JOB = re.compile(r"([0-9]+)-[0-9]+\Z")
EVENT_KINDS = ("error", "notice", "limit", "changes", "wave", "merge", "offer")


# --- The console tee ---------------------------------------------------------------------------------------

_log_lock = threading.Lock()
_log: deque = deque()           # (wall time, "out" | "err", line), oldest first.
_log_chars = 0
_partial = {"out": "", "err": ""}
_tees: dict = {}                # "stdout" / "stderr" -> the _Tee installed there.


class _Tee:
    """Stands in for sys.stdout or sys.stderr: every write goes to the stream it replaced, unchanged, and its
    complete lines are kept for the logs. Everything else (fileno, encoding, buffer...) is the stream's own."""
    _loopcut_tee = True

    def __init__(self, stream, name: str):
        self._stream = stream
        self._name = name
        self.active = True

    def write(self, text):
        result = self._stream.write(text)
        if self.active:
            try:
                _keep(self._name, text)
            except Exception:  # The log is a convenience; printing must never fail because of it.
                pass
        return result

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def flush(self):
        return self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _keep(name: str, text) -> None:
    global _log_chars
    if not isinstance(text, str) or not text:
        return
    now = time.time()
    with _log_lock:
        pending = _partial[name] + text
        lines = pending.split("\n")
        _partial[name] = lines.pop()
        if len(_partial[name]) > LINE_CHARS:  # A line that never ends (a progress bar): kept in pieces.
            lines.append(_partial[name])
            _partial[name] = ""
        for line in lines:
            line = line.rstrip("\r")[:LINE_CHARS]
            if not line.strip():
                continue
            _log.append((now, name, line))
            _log_chars += len(line) + 1
        excess = max(len(_log) - LOG_LINES, 0)
        while _log and (excess > 0 or _log_chars > LOG_BYTES):
            _log_chars -= len(_log.popleft()[2]) + 1
            excess -= 1


def install_tee() -> None:
    """Idempotent, and safe across an add-on reload: a tee left by an earlier copy of this module is replaced."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:  # Blender on Windows without a console.
            continue
        if getattr(stream, "_loopcut_tee", False):
            if stream is _tees.get(name):
                continue
            stream.active = False
            stream = stream._stream
        tee = _Tee(stream, "out" if name == "stdout" else "err")
        setattr(sys, name, tee)
        _tees[name] = tee


def remove_tee() -> None:
    """Puts the original streams back; a tee something else has since wrapped just stops keeping lines."""
    for name, tee in list(_tees.items()):
        tee.active = False
        if getattr(sys, name, None) is tee:
            setattr(sys, name, tee._stream)
        del _tees[name]


def _stamp(when: float) -> str:
    return time.strftime("%H:%M:%S", time.gmtime(when)) + f".{int(when % 1 * 1000):03d}Z"


def recent_log(since: float | None = None, limit: int = SESSION_LOG) -> str:
    """The kept console lines from `since` (wall time) on, stderr marked "!", the tail within `limit` bytes."""
    with _log_lock:
        rows = [row for row in _log if since is None or row[0] >= since]
    return _tail("\n".join(f"{_stamp(t)} {'! ' if name == 'err' else ''}{line}" for t, name, line in rows), limit)


def _tail(text: str, limit: int) -> str:
    """The end of text within `limit` UTF-8 bytes, starting at a line when it was cut."""
    if limit <= 0:
        return ""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    cut = data[-limit:].decode("utf-8", "ignore")
    newline = cut.find("\n")
    return cut[newline + 1:] if 0 <= newline < len(cut) - 1 else cut


# --- The transcript (pure) -----------------------------------------------------------------------------------

def session_uuid(value) -> str | None:
    """A conversation id (uuid4().hex, state.new_session) as the contract wants it: a lowercase uuid4 with
    hyphens. None for anything else."""
    if not isinstance(value, str) or not _ID.match(value):
        return None
    text = str(uuid.UUID(hex=value))
    return text if _INSTALL_ID.match(text) else None


def rfc3339(when) -> str:
    try:
        when = float(when)
    except (TypeError, ValueError):
        when = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(max(when, 0)))


def _clean(text) -> str:
    """Text as the transcript keeps it: inline image data is never sent."""
    text = text if isinstance(text, str) else "" if text is None else str(text)
    return _DATA_URI.sub("[image data omitted]", text) if "base64," in text else text


def image_size(path: Path) -> tuple[int, int] | None:
    """Width and height from a PNG or JPEG header (the first 64 KiB), or None."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(65536)
    except OSError:
        return None
    if head.startswith(b"\x89PNG\r\n\x1a\n") and head[12:16] == b"IHDR" and len(head) >= 24:
        return struct.unpack(">II", head[16:24])
    if head.startswith(b"\xff\xd8"):
        index = 2
        while index + 9 < len(head):
            if head[index] != 0xFF:
                index += 1
                continue
            marker = head[index + 1]
            if marker == 0xFF:
                index += 1
            elif marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
                index += 2
            elif marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                height, width = struct.unpack(">HH", head[index + 5:index + 9])
                return width, height
            else:
                index += 2 + struct.unpack(">H", head[index + 2:index + 4])[0]
    return None


def _image_event(url: str, kind: str, images: Path | None) -> dict:
    """What replaces an image: its kind and, when cheaply known, its size. Never its bytes."""
    data = {"kind": kind}
    from . import conversations
    name = url[len(conversations.IMAGE_SCHEME):] if url.startswith(conversations.IMAGE_SCHEME) else ""
    if images is not None and _IMAGE_NAME.match(name):
        path = images / name
        try:
            size = path.stat().st_size
        except OSError:
            size = None
        if size is not None:
            dimensions = image_size(path)
            if dimensions:
                data["width"], data["height"] = int(dimensions[0]), int(dimensions[1])
            data["bytes"] = int(size)
    elif url.startswith("data:") and "base64," in url:
        data["bytes"] = len(url.split("base64,", 1)[1]) * 3 // 4
    return {"role": "event", "text": "image omitted", "data": data}


def _parts(content) -> tuple[str, list]:
    """A message's text, and its other parts in order: ("image", url) or (type, None)."""
    if content is None:
        return "", []
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        return str(content), []
    texts, others = [], []
    for part in content:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind in ("text", "input_text", "output_text"):
            texts.append(_clean(part.get("text")))
        elif kind in ("image_url", "input_image", "image"):
            url = part.get("image_url")
            url = url.get("url", "") if isinstance(url, dict) else url if isinstance(url, str) else ""
            others.append(("image", url))
        else:
            others.append((str(kind or "part"), None))
    return "\n\n".join(text for text in texts if text), others


def _user_kind(message: dict, text: str) -> str:
    """turn (what the user typed, with the scene context), steer (sent while the agent worked), capture (a tool's
    image) or continuation (the add-on's own wrap-up prompt)."""
    from . import context
    if message.get(context.CAPTURE) or text.startswith((context.CAPTURE_TEXT, context.CAPTURE_LABEL,
                                                          context.FILE_PREVIEW_LABEL)):
        return "capture"
    if message.get(context.STEER):
        return "steer"
    if message.get(context.STEP_LIMIT):
        return "continuation"
    return "turn"


def _message_entries(message: dict, kind: str, names: dict, images: Path | None) -> list:
    """One message as transcript entries. The add-on's private markers (context.PRIVATE_PREFIX) are never copied;
    what they say about the message is in data."""
    from . import context
    role = message.get("role")
    text, others = _parts(message.get("content"))
    text = _clean(text)
    entry = {"role": role}
    data = {}
    if role == "assistant":
        entry["text"] = text
        calls = []
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            arguments = function.get("arguments", "")
            arguments = arguments if isinstance(arguments, str) else json.dumps(arguments, default=str)
            call_id = str(call.get("id") or "")[:NAME_CHARS]
            names[call_id] = str(function.get("name") or "")[:NAME_CHARS]
            calls.append({"id": call_id, "name": names[call_id], "arguments": _clean(arguments)})
        if calls:
            entry["tool_calls"] = calls
        if message.get(context.FOLDED):
            data["folded"] = True
    elif role == "tool":
        call_id = str(message.get("tool_call_id") or "")[:NAME_CHARS]
        entry["call_id"] = call_id
        if names.get(call_id):
            entry["name"] = names[call_id]
        entry["text"] = text
    else:
        entry["text"] = text
        if kind != "turn":
            data["kind"] = kind
    if message.get(context.SUMMARY):
        data["summary"] = True
    if data:
        entry["data"] = data
    entries = [entry]
    from . import conversations
    image_kind = ("file_preview" if text.startswith(context.FILE_PREVIEW_LABEL) else "viewport") if kind == "capture" \
        else "attachment" if role == "user" and (kind == "turn" or message.get(conversations.ATTACHED)) else "image"
    for what, url in others:
        entries.append(_image_event(url, image_kind, images) if what == "image" else
                       {"role": "event", "text": f"{what} omitted"})
    return entries


def _event(item: dict) -> dict | None:
    """A chat card the model never saw (an error, a notice, a limit, a wave...) as an event, or None."""
    kind = item.get("kind")
    if kind not in EVENT_KINDS:
        return None
    text = _clean(item.get("text"))
    if kind == "error":
        entry = {"role": "event", "error": text, "data": {"kind": "error"}}
        if item.get("retry"):
            entry["data"]["retry"] = True
        return entry
    data = {"kind": kind}
    if kind == "notice":
        if item.get("url"):
            data["url"] = str(item["url"])
        if isinstance(item.get("audio"), dict):
            data["audio"] = str(item["audio"].get("name") or "")
    elif kind == "limit":
        data.update({key: str(item.get(key) or "") for key in ("reason", "plan", "resets_at")})
    elif kind in ("changes", "merge"):
        lines = [str(line)[:300] for line in list(item.get("lines") or [])]
        more = [f"… {len(lines) - DATA_LINES} more"] if len(lines) > DATA_LINES else []
        data["lines"] = lines[:DATA_LINES] + more
        if kind == "merge":
            data["wave"] = str(item.get("wave") or "")
            data["quarantined"] = [str(name) for name in list(item.get("quarantined") or [])][:DATA_LINES]
    elif kind == "wave":
        data["wave"] = str(item.get("wave") or "")
        fields = ("id", "deliverable", "role", "status", "checks", "note")
        data["rows"] = [{key: str(row.get(key) or "") for key in fields}
                        for row in list(item.get("rows") or []) if isinstance(row, dict)]
    elif kind == "offer":
        data.update(nudge=str(item.get("nudge") or ""), title=str(item.get("title") or ""))
    return {"role": "event", "text": text, "data": data}


def build_transcript(messages: list, items: list, *, images: Path | None = None, workers: list = ()) -> list:
    """The conversation as transcript entries: every message in order, with the chat's cards (errors, notices,
    limits, scene changes, waves, merges, offers) as events where they happened, as far as that is known.

    Messages and chat items are separate lists with no shared clock, so they are lined up by what both have: each
    message the user sent (a turn or a note sent mid-turn) opens a segment in both, and inside a segment the n-th
    tool card matches the n-th tool result. A card with tool cards after it goes after the result of the tool card
    before it; any other card goes at the end of its segment. `workers` ({session_id, worker, job, wave, status,
    turn}) are referenced at the end of the turn that ran them."""
    entries: list = []
    after_tool: dict = {}    # (segment, n) -> position just past the n-th tool result (n=0: past the opening).
    segment_end: dict = {0: 0}
    segment_turn: dict = {0: 0}
    names: dict = {}
    segment = turn = tools = 0
    after_tool[(0, 0)] = 0
    for message in list(messages):
        if not isinstance(message, dict) or message.get("role") not in ("user", "assistant", "tool"):
            continue
        kind = ""
        if message["role"] == "user":
            kind = _user_kind(message, _parts(message.get("content"))[0])
            if kind in ("turn", "steer"):
                segment += 1
                turn += kind == "turn"
                tools = 0
        entries.extend(_message_entries(message, kind, names, images))
        if kind in ("turn", "steer"):
            after_tool[(segment, 0)] = len(entries)
            segment_turn[segment] = turn
        elif message["role"] == "tool":
            tools += 1
            after_tool[(segment, tools)] = len(entries)
        segment_end[segment] = len(entries)

    inserts: dict = {}
    pending: list = []       # (event, tool cards before it) in the current item segment.
    item_segment = seen_tools = 0

    def place(segment_index: int, total_tools: int) -> None:
        for event, before in pending:
            if before < total_tools and (segment_index, before) in after_tool:
                position = after_tool[(segment_index, before)]
            else:
                position = segment_end.get(segment_index, len(entries))
            inserts.setdefault(position, []).append(event)
        pending.clear()

    for item in list(items):
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        if kind == "user":
            place(item_segment, seen_tools)
            item_segment += 1
            seen_tools = 0
        elif kind == "tool":
            seen_tools += 1
        else:
            event = _event(item)
            if event is not None:
                pending.append((event, seen_tools))
    place(item_segment, seen_tools)

    for worker in workers:
        matching = [index for index, number in segment_turn.items() if number == worker.get("turn") and index]
        position = segment_end.get(max(matching), len(entries)) if matching else len(entries)
        data = {key: worker[key] for key in ("session_id", "worker", "job", "wave", "status") if worker.get(key)}
        inserts.setdefault(position, []).append({"role": "event", "text": "worker session", "data": data})

    result = []
    for position in range(len(entries) + 1):
        result.extend(inserts.get(position, ()))
        if position < len(entries):
            result.append(entries[position])
    return result


def cap_entries(entries: list, limit: int = MAX_ENTRIES) -> list:
    """At most `limit` entries: the newest, after one event saying how many earlier ones were left out."""
    if len(entries) <= limit:
        return entries
    dropped = len(entries) - (limit - 1)
    first = entries[0]
    if first.get("role") == "event" and (first.get("data") or {}).get("kind") == "omitted":
        dropped += first["data"].get("entries", 0) - 1
    marker = {"role": "event", "text": f"{dropped} earlier entries omitted",
              "data": {"kind": "omitted", "entries": dropped}}
    return [marker, *entries[-(limit - 1):]]


# --- Redaction and size (pure) ------------------------------------------------------------------------------

# Key shapes, as cloud.safe_value's lc_/sk/AIza plus JWTs (ChatGPT's tokens), bearer headers and other vendors'
# keys. Prefixes that start ordinary identifiers (sk_basis, re_pattern) need a digit too, as real keys have.
PATTERNS = (
    re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{10,}"),
    re.compile(r"\b[sr]k[-_](?=[A-Za-z0-9_-]*[0-9])[A-Za-z0-9_-]{16,}"),
    re.compile(r"\blc_[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}"),
    re.compile(r"\bre_(?=[A-Za-z0-9_]*[0-9])[A-Za-z0-9_]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:[0-9a-f]{32}\b"),  # fal
    re.compile(r"\bhf_[A-Za-z0-9]{30,}"),
    re.compile(r"\bxai-[A-Za-z0-9]{20,}"),
)
_URL = re.compile(r"https?://[^\s<>\"'`]+")
REDACTED = "[redacted]"


def _home_forms(home: str | None) -> list[str]:
    if not home or len(home) < 4 or home.strip("/\\") == "":
        return []
    return sorted({home, home.replace("\\", "/"), home.replace("\\", "\\\\")}, key=len, reverse=True)


def redact_text(text: str, secrets=(), home: str | None = None) -> str:
    for secret in secrets:
        if secret and secret in text:
            text = text.replace(secret, REDACTED)
    for pattern in PATTERNS:
        text = pattern.sub(REDACTED, text)
    if "://" in text:  # Signed URLs carry their capability in the query.
        text = _URL.sub(lambda m: m[0].split("?", 1)[0] + "?[redacted]" if "?" in m[0] else m[0], text)
    for form in _home_forms(home):
        text = text.replace(form, "~")
    return text


def redact(value, secrets=(), home: str | None = None):
    """value with every known secret, key-shaped string, URL query and the home folder replaced, recursively.
    Longer secrets first, so one that contains another goes whole."""
    secrets = sorted({s for s in secrets if isinstance(s, str) and len(s) >= 8}, key=len, reverse=True)

    def walk(node):
        if isinstance(node, str):
            return redact_text(node, secrets, home)
        if isinstance(node, dict):
            return {key: walk(item) for key, item in node.items()}
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node
    return walk(value)


def trim_text(text: str, side: int = FIELD_SIDE) -> str:
    """text, or its first and last `side` characters around a note of what was left out."""
    if not isinstance(text, str) or len(text) <= 2 * side + 64:
        return text
    return f"{text[:side]}\n[… {len(text) - 2 * side:,} characters omitted …]\n{text[-side:]}"


def _trim_entry(entry: dict, side: int, tool_side: int | None = None) -> dict:
    tool_side = side if tool_side is None else tool_side
    own = tool_side if entry.get("role") == "tool" else side
    for key in ("text", "error"):
        if isinstance(entry.get(key), str):
            entry[key] = trim_text(entry[key], own)
    for call in entry.get("tool_calls") or []:
        if isinstance(call, dict) and isinstance(call.get("arguments"), str):
            call["arguments"] = trim_text(call["arguments"], tool_side)
    return entry


class TooLarge(Exception):
    """Even the smallest form of the body does not fit the contract's limits."""


# Each level is applied to a fresh copy: (older tool results' side, every tool's side, other fields' side,
# entries kept, log bytes, report sessions kept).
_LEVELS = (
    (FIELD_SIDE, FIELD_SIDE, FIELD_SIDE, MAX_ENTRIES, None, REPORT_SESSIONS),
    (2048, FIELD_SIDE, FIELD_SIDE, MAX_ENTRIES, 128 * 1024, REPORT_SESSIONS),
    (1024, 1024, 8192, MAX_ENTRIES, 64 * 1024, REPORT_SESSIONS),
    (1024, 1024, 2048, 500, 16 * 1024, REPORT_SESSIONS),
    (512, 512, 512, 100, 0, 1),
)
RECENT = 20  # Entries at a transcript's end whose tool results keep their full length at level 1.


def _shrink(body: dict, level: int) -> dict:
    older, tool_side, side, keep, log_limit, sessions_kept = _LEVELS[level]
    body = copy.deepcopy(body)
    if "sessions" in body:
        body["sessions"] = body["sessions"][:sessions_kept]
    for session in [body] + list(body.get("sessions") or []):
        if "transcript" not in session:
            continue
        entries = cap_entries(session["transcript"], keep)
        recent_from = len(entries) - RECENT
        session["transcript"] = [_trim_entry(entry, side, older if index < recent_from else tool_side)
                                 for index, entry in enumerate(entries)]
        if log_limit is not None and "log" in session:
            session["log"] = _tail(session["log"], log_limit)
            if not session["log"]:
                del session["log"]
    return body


def fit(body: dict) -> dict:
    """body trimmed as little as needed to fit MAX_DECODED and MAX_GZIP (older tool results first, then every
    field, then the oldest entries and the logs). Raises TooLarge when nothing fits."""
    from . import cloud_http
    for level in range(len(_LEVELS)):
        candidate = _shrink(body, level)
        raw = cloud_http.json_body(candidate)
        if len(raw) <= MAX_DECODED and len(cloud_http.gzip_body(raw)) <= MAX_GZIP:
            return candidate
    raise TooLarge("conversation log too large")


# --- Secrets (main thread) ---------------------------------------------------------------------------------

_SECRET_NAME = re.compile(r"(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|BEARER|CREDENTIALS?)\Z", re.IGNORECASE)
_NOT_SECRET = {"chatgpt-plan", "true", "false", "none", "null"}


def known_secrets() -> list[str]:
    """Every credential this computer gives the add-on, so it can be cut out before anything is sent: the keys in
    Loopcut's credentials file (the Loopcut bearer, own-model keys, BlenderKit, Poly Pizza and vendor keys),
    ChatGPT's tokens (read only to redact them), and key-like values in the environment and the .env file."""
    found: set = set()

    def add(value) -> None:
        if isinstance(value, str):
            value = value.strip()
            if len(value) >= 8 and value.lower() not in _NOT_SECRET:
                found.add(value)

    def tokens(node, depth=0) -> None:
        if depth > 8:
            return
        if isinstance(node, dict):
            for key, item in node.items():
                if isinstance(item, str) and re.search(r"token|secret|key|password", str(key), re.IGNORECASE):
                    add(item)
                else:
                    tokens(item, depth + 1)
        elif isinstance(node, list):
            for item in node:
                tokens(item, depth + 1)

    try:
        from . import credentials
        data = credentials._read()
        for section in ("api_keys", "vendor_keys"):
            for value in (data.get(section) or {}).values() if isinstance(data.get(section), dict) else ():
                add(value)
    except Exception:
        pass
    try:
        from . import chatgpt_auth
        tokens(chatgpt_auth._read_json(chatgpt_auth.folder() / chatgpt_auth.ACCOUNTS_FILE))
    except Exception:
        pass
    for name, value in os.environ.items():
        if _SECRET_NAME.search(name):
            add(value)
    try:
        from . import config
        env_file = config._find_env_file()
        for name, value in (config._parse_env_file(env_file) if env_file else {}).items():
            if _SECRET_NAME.search(name):
                add(value)
    except Exception:
        pass
    return sorted(found, key=len, reverse=True)


def home_folder() -> str:
    try:
        return str(Path.home())
    except (OSError, RuntimeError, KeyError):
        return ""


# --- Gating ---------------------------------------------------------------------------------------------------

_lock = threading.Lock()
_stopped = False                # 404 or 426: no uploads until the next launch.
_paused_until = 0.0             # A 429's pause (monotonic).
_conflicts: set = set()         # Session ids the server said belong to someone else (409).
_first_seen: dict = {}          # session id -> wall time its first turn started in this process (its log's start).
_meta_seen: dict = {}           # session id -> {provider, model, outcome} as this process last ran it (reports).


def env_off() -> bool:
    from . import telemetry
    return telemetry.env_off()


def _preferences():
    from . import settings
    return settings.preferences()


def sharing_on() -> bool:
    """Main thread: uploads allowed by the environment, the preference, an interactive Blender, and a user who has
    seen what is shared (the setup's privacy step, which installs from before the setup go through too:
    onboarding.privacy_seen)."""
    if env_off() or _stopped:
        return False
    try:
        import bpy
        if getattr(bpy.app, "background", True):
            return False
        prefs = _preferences()
        seen = _privacy_seen()
    except Exception:  # No preferences (unregistered, a test stand-in): nothing is sent.
        return False
    return seen and prefs is not None and bool(getattr(prefs, "share_conversations", False))


def _privacy_seen() -> bool:
    from . import onboarding
    return onboarding.privacy_seen()


def _current_route(cfg=None) -> tuple[str, str]:
    """Main thread: (provider, model) as the 014 turn event names them."""
    from . import cloud, telemetry
    provider = telemetry.provider_of(cloud.model_source())
    model = getattr(cfg, "model", "") if cfg is not None else ""
    if not model:
        try:
            from . import config
            model = config.load().model
        except Exception:
            model = ""
    return provider, telemetry.model_id(provider, model or "")


def turn_start(session: dict, cfg) -> dict:
    """Main thread, as a turn starts (agent._launch): {} when this turn's conversation will not be uploaded,
    otherwise what ran it. Never raises."""
    try:
        _first_seen.setdefault(session["id"], time.time())
        try:
            provider, model = _current_route(cfg)
        except Exception:  # No preferences (a test stand-in, an unregistered add-on): nothing to name.
            return {}
        _meta_seen[session["id"]] = {"provider": provider, "model": model}
        return {"provider": provider, "model": model} if sharing_on() else {}
    except Exception as ex:
        print(f"Loopcut: conversation log not set up: {type(ex).__name__}")
        return {}


# --- Snapshots (the turn's thread) ----------------------------------------------------------------------------

def _jobs_root() -> Path:
    from . import waves
    return waves.jobs_root()


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def worker_folders(parent_id: str, turn: int | None = None) -> list[dict]:
    """The worker conversations a conversation's delegated turns left on disk (waves.jobs_root():
    <parent>/<turn>-<n>/w<k>/<worker>/), each {session (32 hex), session_id, worker, job, wave, turn, folder,
    model, outcome, status}; only turn `turn`'s when it is given. Job folders are reaped after KEEP_DAYS."""
    found = []
    try:
        base = _jobs_root() / parent_id
        jobs = sorted(base.iterdir()) if base.is_dir() else []
    except OSError:
        return found
    for job in jobs:
        match = _JOB.match(job.name)
        if not match or not job.is_dir() or (turn is not None and int(match[1]) != turn):
            continue
        log = _read_json(job / "job.json")
        known = {}
        for wave in (log or {}).get("waves") or [] if isinstance(log, dict) else []:
            for worker in (wave.get("workers") or []) if isinstance(wave, dict) else []:
                if isinstance(worker, dict) and worker.get("id"):
                    known[(f"w{wave.get('n')}", str(worker["id"]))] = worker
        for meta_path in sorted(job.glob("w*/*/meta.json")):
            meta = _read_json(meta_path)
            session = meta.get("id") if isinstance(meta, dict) else None
            session_id = session_uuid(session)
            if not session_id:
                continue
            folder = meta_path.parent
            info = known.get((folder.parent.name, folder.name), {})
            found.append({"session": session, "session_id": session_id, "worker": folder.name, "job": job.name,
                          "wave": folder.parent.name, "turn": int(match[1]), "folder": folder,
                          "model": str(info.get("model") or ""), "outcome": str(info.get("outcome") or ""),
                          "status": str(info.get("status") or "")})
    return found


def _worker_outcome(outcome: str) -> str:
    from . import telemetry
    return telemetry.outcome_of(outcome or "error")[0] if outcome else "error"


def _worker_snapshot(worker: dict, provider: str, fallback_model: str) -> dict | None:
    """One worker conversation from its folder, as an upload of its own."""
    from . import telemetry
    body = _read_json(worker["folder"] / "conversation.json")
    if not isinstance(body, dict) or body.get("id") != worker["session"]:
        return None
    log = ""
    try:
        with open(worker["folder"] / "worker.log", "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(handle.tell() - WORKER_LOG, 0))
            log = _tail(handle.read().decode("utf-8", "replace"), WORKER_LOG)
    except OSError:
        pass
    items = body.get("items") if isinstance(body.get("items"), list) else []
    return {"session_id": worker["session_id"],
            "meta": {"provider": provider,
                     "model": telemetry.model_id(provider, worker["model"]) if worker["model"] else fallback_model,
                     "started_at": rfc3339(body.get("created")),
                     "turns": max(1, sum(1 for item in items if isinstance(item, dict) and item.get("kind") == "user")),
                     "outcome": _worker_outcome(worker["outcome"])},
            "transcript": cap_entries(build_transcript(body.get("messages") or [], items,
                                                       images=worker["folder"] / "images")),
            "log": log}


def snapshot(session: dict, turn, start: dict) -> list[dict]:
    """The turn's thread, once the turn has ended: the conversation and this turn's worker conversations as
    uploads ({session_id, meta without the system fields, transcript, log}). New dicts and lists only, so a next
    turn changing the session cannot change what is sent."""
    from . import conversations, telemetry
    session_id = session_uuid(session.get("id"))
    if not session_id:
        return []
    outcome, _code = telemetry.outcome_of(getattr(turn, "outcome", "") or "", getattr(turn, "error_code", "") or None)
    _meta_seen.setdefault(session["id"], {}).update(outcome=outcome)
    number = int(session.get("turn_number") or 0)
    workers = worker_folders(session["id"])
    try:
        images = conversations._folder(session["id"]) / "images"
    except Exception:
        images = None
    transcript = build_transcript(list(session.get("messages") or []), list(session.get("items") or []),
                                  images=images, workers=workers)
    provider, model = start["provider"], start["model"]
    turns = number or sum(1 for item in list(session.get("items") or []) if item.get("kind") == "user")
    uploads = [{"session_id": session_id,
                "meta": {"provider": provider, "model": model, "started_at": rfc3339(session.get("created")),
                         "turns": max(0, min(10_000, turns)), "outcome": outcome},
                "transcript": cap_entries(transcript),
                "log": recent_log(_first_seen.get(session["id"]), SESSION_LOG)}]
    for worker in workers:
        if worker["turn"] == number and worker["session_id"] not in _conflicts:
            upload = _worker_snapshot(worker, provider, model)
            if upload is not None:
                uploads.append(upload)
    return uploads


def on_turn_end(session: dict, turn) -> None:
    """The turn's thread (agent._run, beside the usage event): snapshot the conversation and hand it to the main
    thread, which checks the gates again and queues it. Never raises into the agent loop."""
    try:
        start = getattr(turn, "conversation_log", None)
        if not start or env_off() or _stopped:
            return
        uploads = snapshot(session, turn, start)
        if not uploads:
            return
        from . import mainthread
        mainthread.run_on_main(lambda: queue_uploads(uploads))
    except Exception as ex:
        print(f"Loopcut: conversation log not sent: {type(ex).__name__}")


def _sender_context() -> dict | None:
    """Main thread: who sends (install id, the Loopcut bearer if signed in), the request headers, the system and
    what to redact. None without an install id (background processes)."""
    from . import cloud_http, config, credentials, telemetry
    install_id = credentials.interest_install_id()
    if not isinstance(install_id, str) or not _INSTALL_ID.match(install_id):
        return None
    key = credentials.api_key(config.SERVICE_BASE_URL)
    return {"install_id": install_id, "app_version": config.app_version(),
            "key": key if isinstance(key, str) and key.startswith("lc_") else None,
            "headers": cloud_http.contract_headers(), "secrets": known_secrets(), "home": home_folder(),
            "system": {"blender_version": telemetry.blender_version(), "os": telemetry.os_name(),
                       "arch": telemetry.arch_name()}}


def queue_uploads(uploads: list) -> None:
    """Main thread: the gates once more (the preference may have changed during the turn), then the queue."""
    try:
        if not sharing_on():
            return
        sender = _sender_context()
        if sender is None:
            return
        for upload in uploads:
            enqueue({**upload, **sender, "retried": False, "not_before": 0.0})
    except Exception as ex:
        print(f"Loopcut: conversation log not sent: {type(ex).__name__}")


# --- Bodies (the sender's thread) --------------------------------------------------------------------------

def log_body(upload: dict) -> dict:
    """The /v1/logs body: redacted, trimmed to the limits. Raises TooLarge."""
    secrets, home = upload.get("secrets") or (), upload.get("home")
    body = {"session_id": upload["session_id"], "install_id": upload["install_id"],
            "app_version": upload["app_version"], "meta": {**upload["system"], **upload["meta"]},
            "transcript": cap_entries(redact(upload["transcript"], secrets, home))}
    log = _tail(redact(upload.get("log") or "", secrets, home), SESSION_LOG)
    if log:
        body["log"] = log
    return fit(body)


# --- Sending ------------------------------------------------------------------------------------------------

_cond = threading.Condition(_lock)
_pending: "OrderedDict[str, dict]" = OrderedDict()   # session id -> the newest upload for it.
_sender: threading.Thread | None = None


def enqueue(upload: dict) -> None:
    """Keep only the newest upload of each conversation; the oldest waiting conversation goes when full."""
    session_id = upload["session_id"]
    with _cond:
        if _stopped or session_id in _conflicts:
            return
        _pending.pop(session_id, None)
        _pending[session_id] = upload
        while len(_pending) > QUEUE_SIZE:
            _pending.popitem(last=False)
        _cond.notify_all()
    _ensure_sender()


def _ensure_sender() -> None:
    global _sender
    with _lock:
        if _sender is not None and _sender.is_alive():
            return
        _sender = threading.Thread(target=_send_loop, name="loopcut-conversation-logs", daemon=True)
        _sender.start()


def _take(now: float):
    """Under _cond: (the first upload due now or None, seconds until one may be; None when nothing waits)."""
    if not _pending:
        return None, None
    if now < _paused_until:
        return None, _paused_until - now
    for session_id, upload in _pending.items():
        if upload["not_before"] <= now:
            return _pending.pop(session_id), 0.0
    waits = [upload["not_before"] - now for upload in _pending.values()]
    return None, min(waits) if waits else None


def _send_loop() -> None:
    global _sender
    idle_since = time.monotonic()
    while True:
        with _cond:
            if _stopped:
                _pending.clear()
                _sender = None
                return
            now = time.monotonic()
            upload, wait = _take(now)
            if upload is None:
                if wait is None and now - idle_since >= IDLE_SECONDS:
                    _sender = None
                    return
                _cond.wait(timeout=min(wait if wait is not None else IDLE_SECONDS, IDLE_SECONDS))
                continue
        deliver(upload)
        idle_since = time.monotonic()


def _requeue(upload: dict, not_before: float, retried: bool) -> None:
    """Back in the queue for later, unless a newer snapshot of the same conversation arrived meanwhile."""
    with _cond:
        if upload["session_id"] not in _pending and not _stopped:
            _pending[upload["session_id"]] = {**upload, "retried": retried, "not_before": not_before}
            _cond.notify_all()


def deliver(upload: dict, transport=None) -> str:
    """Send one upload. Returns what happened (for tests and the console): sent, stopped, paused, conflict,
    retry, dropped or too_large. Never raises."""
    global _stopped, _paused_until
    from . import cloud_http
    if _stopped:
        return "stopped"
    try:
        body = log_body(upload)
        client = cloud_http.Client(api_key=upload["key"], client_headers=upload["headers"], timeout=TIMEOUT,
                                   transport=transport)
        client.request_response("POST", "/v1/logs", body, auth=upload["key"] is not None, compress=True)
        return "sent"
    except TooLarge:
        print("Loopcut: conversation log not sent: too large")
        return "too_large"
    except cloud_http.CloudError as ex:
        status = ex.status
        if status in (404, 426):
            with _cond:
                _stopped = True  # The server's switch is off, or it wants a newer add-on: until the next launch.
                _pending.clear()
            return "stopped"
        if status == 429:
            with _cond:
                _paused_until = time.monotonic() + PAUSE_429
            _requeue(upload, 0.0, bool(upload.get("retried")))  # Sent once the pause is over.
            return "paused"
        if status == 409:
            with _cond:
                _conflicts.add(upload["session_id"])
            print("Loopcut: conversation log not sent: it belongs to another install or account")
            return "conflict"
        if (status is None or status >= 500) and not upload.get("retried"):
            _requeue(upload, time.monotonic() + RETRY_AFTER, True)
            return "retry"
        field = ex.extra.get("field") if isinstance(ex.extra, dict) else None
        print(f"Loopcut: conversation log not sent: HTTP {status or 'error'} {ex.code}"
              + (f" ({field})" if isinstance(field, str) else ""))
        return "dropped"
    except Exception as ex:
        print(f"Loopcut: conversation log not sent: {type(ex).__name__}")
        return "dropped"


# --- Problem reports -----------------------------------------------------------------------------------------

class ReportError(Exception):
    """What to tell the person whose report did not go."""


_report = {"status": "", "code": "", "message": ""}   # status: "" | sending | sent | failed


def report_state() -> dict:
    return dict(_report)


def report_line() -> str:
    """One line for the preferences page about the last report, or ""."""
    if _report["status"] == "sending":
        return "Sending the report…"
    return _report["message"]


def account_email() -> str:
    """Main thread: the signed-in Loopcut account's email, for the report's contact field, or ""."""
    try:
        from . import account
        if not account.signed_in():
            return ""
        email = (account.status().get("account") or {}).get("email")
        return email if isinstance(email, str) and _EMAIL.match(email) and len(email) <= EMAIL_CHARS else ""
    except Exception:
        return ""


def _items_outcome(items: list) -> str:
    """How a conversation read from disk last ended, from its cards: the add-on does not save the outcome."""
    users = [index for index, item in enumerate(items) if isinstance(item, dict) and item.get("kind") == "user"]
    last_user = users[-1] if users else -1
    tail = [item for item in items[last_user + 1:] if isinstance(item, dict)]
    errors = [item for item in tail if item.get("kind") in ("error", "limit")]
    if errors:
        return "cancelled" if errors[-1].get("text") == "Stopped." else "error"
    if last_user >= 0 and not any(item.get("kind") in ("assistant", "tool") for item in tail):
        return "empty"
    return "ok"


def recent_conversations(root: Path, limit: int = REPORT_SESSIONS) -> list[tuple[Path, dict]]:
    """The newest `limit` conversations across every project: (folder, meta.json)."""
    found = []
    try:
        folders = list(root.iterdir()) if root.is_dir() else []
    except OSError:
        return found
    for folder in folders:
        if not _ID.match(folder.name):
            continue
        meta = _read_json(folder / "meta.json")
        if isinstance(meta, dict) and meta.get("id") == folder.name:
            found.append((folder, meta))
    found.sort(key=lambda pair: pair[1].get("updated", 0) if isinstance(pair[1].get("updated"), (int, float)) else 0,
               reverse=True)
    return found[:limit]


def report_session(folder: Path, meta: dict, route: tuple[str, str]) -> dict | None:
    """One saved conversation as a report's session (a /v1/logs body without install_id and app_version)."""
    body = _read_json(folder / "conversation.json")
    session_id = session_uuid(meta.get("id"))
    if not isinstance(body, dict) or not session_id or body.get("id") != meta["id"]:
        return None
    items = body.get("items") if isinstance(body.get("items"), list) else []
    seen = _meta_seen.get(meta["id"], {})
    turns = meta.get("turns") if isinstance(meta.get("turns"), int) else 0
    return {"session_id": session_id,
            "meta": {"provider": seen.get("provider") or route[0], "model": seen.get("model") or route[1],
                     "started_at": rfc3339(meta.get("created", body.get("created"))),
                     "turns": max(0, min(10_000, turns)), "outcome": seen.get("outcome") or _items_outcome(items)},
            "transcript": cap_entries(build_transcript(body.get("messages") or [], items, images=folder / "images",
                                                       workers=worker_folders(meta["id"])))}


def report_body(note: str, email: str, include: bool, context: dict) -> dict:
    """The /v1/reports body: redacted, trimmed to the limits. Raises TooLarge."""
    secrets, home = context.get("secrets") or (), context.get("home")
    sessions = []
    if include and context.get("root") is not None:
        for folder, meta in recent_conversations(context["root"]):
            try:
                session = report_session(folder, meta, context["route"])
            except Exception as ex:  # One unreadable conversation never stops the report.
                print(f"Loopcut: report left out a conversation: {type(ex).__name__}")
                session = None
            if session is not None:
                session["meta"] = {**context["system"], **session["meta"]}
                session["transcript"] = redact(session["transcript"], secrets, home)
                sessions.append(session)
    body = {"install_id": context["install_id"], "app_version": context["app_version"],
            "note": redact_text((note or "").strip(), secrets, home)[:NOTE_CHARS],
            "system": dict(context["system"]), "sessions": sessions}
    email = (email or "").strip()
    if email and len(email) <= EMAIL_CHARS and _EMAIL.match(email):
        body["contact_email"] = email
    log = _tail(redact(context.get("log") or "", secrets, home), REPORT_LOG)
    if log:
        body["log"] = log
    try:
        return fit(body)
    except TooLarge:  # The note and a short log still help: the conversations are what does not fit.
        body["sessions"] = []
        if log:
            body["log"] = _tail(log, 64 * 1024)
        return fit(body)


def send_report(body: dict, key: str | None, headers: dict, transport=None) -> str:
    """POST /v1/reports; the code the server gives back (LC-…). Raises ReportError with words for the person."""
    from . import cloud_http
    elsewhere = f"Please email {SUPPORT_EMAIL} instead."
    attempts = [(body, key)]
    if key is not None:
        attempts.append((body, None))  # A revoked bearer is refused (401), never downgraded: send it unsigned.
    while attempts:
        payload, bearer = attempts.pop(0)
        try:
            client = cloud_http.Client(api_key=bearer, client_headers=headers, timeout=TIMEOUT, transport=transport)
            data = client.request("POST", "/v1/reports", payload, auth=bearer is not None, compress=True)
        except cloud_http.CloudError as ex:
            if ex.status == 401 and attempts:
                continue
            if ex.status == 413 and payload.get("sessions"):
                attempts = [({**payload, "sessions": []}, bearer)]  # Without the conversations, once.
                continue
            if ex.status in (404, 426):
                raise ReportError(f"Reports aren't available right now. {elsewhere}") from None
            if ex.status == 429:
                raise ReportError(f"Too many reports from this computer for now. {elsewhere}") from None
            raise ReportError(f"Could not send the report ({ex.code}). {elsewhere}") from None
        code = data.get("code") if isinstance(data, dict) else None
        if isinstance(code, str) and _CODE.match(code):
            return code
        raise ReportError(f"The report went, but no code came back. {elsewhere}")
    raise ReportError(f"Could not send the report. {elsewhere}")


def start_report(note: str, email: str, include: bool, on_done=None) -> bool:
    """Main thread (the operator): read what the report needs, then build and send it on a thread of its own.
    on_done(report_state()) runs on the main thread when it has gone or failed. False when one is on its way or
    this is a background process."""
    import bpy
    if _report["status"] == "sending" or getattr(bpy.app, "background", True):
        return False
    from . import conversations
    try:
        context = _sender_context()
    except Exception as ex:
        print(f"Loopcut: problem report not sent: {type(ex).__name__}")
        return False
    if context is None:
        return False
    try:
        route = _current_route()
    except Exception:
        route = ("own_key", "other")
    try:
        root = conversations.root()
    except Exception:
        root = None
    context.update(root=root, route=route, log=recent_log(None, REPORT_LOG))
    _report.update(status="sending", code="", message="")

    def work() -> None:
        try:
            code = send_report(report_body(note, email, include, context), context["key"], context["headers"])
            result = {"status": "sent", "code": code, "message": f"Report sent: {code}, copied to the clipboard. "
                                                                 f"Quote it if you write to {SUPPORT_EMAIL}."}
        except ReportError as ex:
            result = {"status": "failed", "code": "", "message": str(ex)}
        except Exception as ex:
            print(f"Loopcut: problem report not sent: {type(ex).__name__}")
            result = {"status": "failed", "code": "",
                      "message": f"Could not send the report. Please email {SUPPORT_EMAIL} instead."}
        from . import mainthread
        mainthread.run_on_main(lambda: _report_done(result, on_done))

    threading.Thread(target=work, name="loopcut-report", daemon=True).start()
    return True


def _report_done(result: dict, on_done) -> None:
    _report.update(result)
    print(f"Loopcut: problem report {'sent: ' + result['code'] if result['code'] else 'not sent'}")
    if on_done is not None:
        try:
            on_done(report_state())
        except Exception as ex:
            print(f"Loopcut: problem report result not shown: {type(ex).__name__}")


# --- Registration -----------------------------------------------------------------------------------------

def register() -> None:
    try:
        import bpy
        if getattr(bpy.app, "background", True):
            return  # Nothing is ever sent from a background process; its output stays its own.
    except ImportError:
        return
    install_tee()


def unregister() -> None:
    remove_tee()
