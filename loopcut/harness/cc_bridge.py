"""Loopcut's model, played by a Claude Code session. Plain Python, runs outside Blender.

The add-on streams from whatever LOOPCUT_BASE_URL names; this serves that /chat/completions on
127.0.0.1 and parks each request on disk until Claude Code answers it, so everything except the
model (context.prepare, auto-look, approvals, checkpoints, tools, UI) runs as it does for users.

    python3 cc_bridge.py serve [--port 8765]   # one run folder per serve; leave it running
    python3 cc_bridge.py wait                   # blocks until a request is unanswered, claims it
    python3 cc_bridge.py submit <request dir> --by <model>   # validates reply.draft.json, releases it
    python3 cc_bridge.py note "text"            # a tester's observation, stamped into the timeline
    python3 cc_bridge.py report [run dir]       # transcript.md + summary.json for the run

Blender side: scripts/cc_session.sh. The model side's instructions: harness/cc_bridge_model.md.

A run folder (out/cc_bridge/<time>/) holds events.jsonl (every event, timestamped) and one folder
per request: request.json (the body, images swapped for files), images/, prompt.md (what the model
reads), delta.md (only the messages after the previous request's), meta.json, reply.json (with the
model's notes for developers, which are never streamed).

Eval runs (harness/evals/run.py --cc-orchestrator, or --cc-single for Claude as the single loop)
start one bridge per task in a folder of their own, and one Claude Code subagent per task drives it
(harness/cc_orchestrator_model.md or harness/cc_single_model.md, as <out>/cc_tasks.json says):

    python3 cc_bridge.py serve --port P --run-dir D   # no current.txt: several bridges at once
    python3 cc_bridge.py wait --run D --timeout 540   # REQUEST <folder>, IDLE, or DONE once D/done exists
"""

import argparse
import base64
import hashlib
import json
import re
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import prompt_assembly
from prompt_proxy import request_meta, send_error

WORKSPACE = Path(__file__).resolve().parents[3]
ROOT = WORKSPACE / "out" / "cc_bridge"
CURRENT = ROOT / "current.txt"
DONE = "done"            # Touched in a run folder by run.py when the task's Blender has exited.
KEEPALIVE_SECONDS = 15   # The add-on's read timeout is 120 s (llm.stream_chat); comment lines keep it fed.
POLL_SECONDS = 0.5
STREAM_PIECE = 48        # Characters per streamed text chunk, so the panel streams as it does live.
# The add-on's own estimate (context.py), so the usage it is told matches what it calibrates against.
CHARS_PER_TOKEN, IMAGE_TOKENS = 3.4, 800
DATA_URI = re.compile(r"^data:image/(\w+);base64,(.*)$", re.S)

_lock = threading.Lock()


def now() -> float:
    return round(time.time(), 3)


def log(run: Path, event: str, **fields) -> None:
    line = json.dumps({"t": now(), "event": event, **fields}, ensure_ascii=False)
    with _lock, (run / "events.jsonl").open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def current_run() -> Path:
    if not CURRENT.exists():
        sys.exit("No bridge run yet: start `cc_bridge.py serve` first.")
    return Path(CURRENT.read_text().strip())


def run_of(args) -> Path:
    """The run folder a command names with --run, else the newest `serve` without --run-dir."""
    return Path(args.run).resolve() if getattr(args, "run", None) else current_run()


def write_json(path: Path, value) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)  # Readers polling for the file never see half of it.


# ------------------------------------------------------------------ request on disk

def extract_images(body: dict, folder: Path) -> int:
    """Swap every data URI for a file next to the request; returns how many."""
    count = 0
    for i, message in enumerate(body.get("messages") or []):
        if not isinstance(message.get("content"), list):
            continue
        for j, part in enumerate(message["content"]):
            match = DATA_URI.match(part.get("image_url", {}).get("url", "")) if part.get("type") == "image_url" else None
            if not match:
                continue
            name = f"images/m{i:03d}_{j}.{match.group(1)}"
            (folder / "images").mkdir(exist_ok=True)
            (folder / name).write_bytes(base64.b64decode(match.group(2)))
            part["image_url"]["url"] = "file:" + name
            count += 1
    return count


def estimate_tokens(body: dict) -> int:
    chars = images = 0
    for message in body.get("messages") or []:
        content = message.get("content")
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if part.get("type") == "image_url":
                    images += 1
                else:
                    chars += len(part.get("text", ""))
        for call in message.get("tool_calls") or []:
            chars += len(call["function"].get("name", "")) + len(call["function"].get("arguments", ""))
    chars += len(json.dumps(body.get("tools") or []))
    return round(chars / CHARS_PER_TOKEN) + images * IMAGE_TOKENS


def _call_block(call: dict) -> str:
    function = call.get("function") or {}
    try:
        arguments = json.loads(function.get("arguments") or "{}")
    except ValueError:
        return f"**call** `{function.get('name')}` (id {call.get('id')}), unparseable arguments:\n```\n{function.get('arguments')}\n```"
    code = arguments.pop("code", None) if isinstance(arguments, dict) else None
    text = f"**call** `{function.get('name')}` (id {call.get('id')}) arguments: `{json.dumps(arguments, ensure_ascii=False)}`"
    return text + (f"\n```python\n{code}\n```" if code else "")


def render_prompt(number: int, body: dict) -> str:
    """The request as a model subagent reads it: nothing left out, nothing added but layout."""
    messages = body.get("messages") or []
    out = [f"# Loopcut model request {number:04d}", "",
           f"reasoning_effort: {body.get('reasoning_effort')} · messages: {len(messages)} · "
           f"tools offered: {len(body.get('tools') or [])}", ""]
    system = [m for m in messages if m.get("role") == "system"]
    if system:
        out += ["## System prompt", "", system[0]["content"] if isinstance(system[0]["content"], str)
                else json.dumps(system[0]["content"]), ""]
    if body.get("tools"):
        out += ["## Tools", ""]
        for tool in body["tools"]:
            fn = tool.get("function") or {}
            out += [f"### {fn.get('name')}", "", fn.get("description") or "", "",
                    "```json", json.dumps(fn.get("parameters") or {}, ensure_ascii=False), "```", ""]
    else:
        out += ["## Tools", "", "None offered: answer in text only.", ""]
    out += ["## Conversation", ""]
    return "\n".join(out + render_messages(messages, 0))


def render_messages(messages: list, start: int) -> list[str]:
    """Messages [start:] as prompt.md lays them out, the system prompt left out."""
    out = []
    for i, message in enumerate(messages[start:], start):
        if message.get("role") == "system":
            continue
        head = f"### [{i}] {message.get('role')}"
        if message.get("tool_call_id"):
            head += f" (result of {message['tool_call_id']})"
        out += [head, ""]
        content = message.get("content")
        if isinstance(content, str):
            out += [content, ""]
        elif isinstance(content, list):
            for part in content:
                if part.get("type") == "image_url":
                    url = part["image_url"]["url"]
                    out += [f"[image: {url[5:]} — Read this file to see it]" if url.startswith("file:") else f"[image: {url[:80]}]", ""]
                else:
                    out += [part.get("text", ""), ""]
        for call in message.get("tool_calls") or []:
            out += [_call_block(call), ""]
    return out


# ------------------------------------------------------------------ delta.md

def fingerprints(body: dict) -> dict:
    """Per-message hashes and the tool list's, taken before images are swapped for files, so an
    image that changed changes its message's hash."""
    def digest(value) -> str:
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
    return {"messages": [digest(m) for m in body.get("messages") or []], "tools": digest(body.get("tools") or [])}


def delta_kind(previous: dict | None, current: dict, has_tools: bool) -> tuple[str, str]:
    """How this request relates to the previous step request: "first", "side" (a summary or a
    reference card: its own short conversation, no tools), "append" (the previous request's messages
    are an unchanged prefix) or "changed" (context.prepare folded or summarised earlier messages, or
    the tools changed). Returns the kind and the reason in words."""
    if not has_tools:
        return "side", "a side request with its own short conversation and no tools (a summary or a reference description)"
    if previous is None:
        return "first", "the first request of this conversation"
    number, old, new = previous["number"], previous["messages"], current["messages"]
    if previous["tools"] != current["tools"]:
        return "changed", f"the tool list differs from request {number:04d}"
    if len(new) < len(old):
        return "changed", f"the conversation shrank from {len(old)} to {len(new)} messages since request {number:04d} (folded or summarised)"
    differs = next((i for i, (a, b) in enumerate(zip(old, new)) if a != b), None)
    if differs is not None:
        return "changed", f"message [{differs}] differs from request {number:04d} (earlier messages were folded or summarised)"
    return "append", f"messages [0]-[{len(old) - 1}] are unchanged from request {number:04d}"


def render_delta(number: int, body: dict, kind: str, reason: str, previous: dict | None) -> str:
    """delta.md: only what is new since the previous step request, or a heading that says to read
    prompt.md in full because the delta would not show everything the model now sees."""
    messages = body.get("messages") or []
    if kind == "first":
        return f"# FIRST REQUEST — read prompt.md\n\nRequest {number:04d} is {reason}.\n"
    if kind in ("side", "changed"):
        tail = (" Answer in content only and follow its last message; the main conversation continues at "
                "the next request." if kind == "side" else " Answer from what it shows now: anything it no "
                "longer shows is gone for the model too.")
        return f"# HISTORY CHANGED — read prompt.md\n\nRequest {number:04d}: {reason}.{tail}\n"
    start = len(previous["messages"])
    new = messages[start:]
    images = sum(1 for m in new if isinstance(m.get("content"), list)
                 for p in m["content"] if p.get("type") == "image_url")
    out = [f"# Loopcut model request {number:04d} · delta since request {previous['number']:04d}", "",
           f"reasoning_effort: {body.get('reasoning_effort')} · messages: {len(messages)} · "
           f"{reason.replace('messages', 'Messages', 1)}; system prompt and tools unchanged. "
           + (f"New: [{start}]-[{len(messages) - 1}], {images} image(s)." if new else
              "Nothing is new: the add-on sent the same conversation again (a retry after an error or a stop)."), ""]
    return "\n".join(out + render_messages(messages, start))


# ------------------------------------------------------------------ server

class Bridge:
    def __init__(self, run: Path, source=None, prompts=None, skills=None):
        self.run = run
        self.source = source
        self.prompts, self.skills = prompts, skills
        self.source_lock = threading.Lock()
        self.count = 0
        self.last_streamed: float | None = None
        self.count_lock = threading.Lock()
        self.chain: dict | None = None   # The previous step request's number and fingerprints, for delta.md.

    def prompt_source(self):
        # Old, plain requests must work even on machines without hosted source files.
        with self.source_lock:
            if self.source is None:
                self.source = prompt_assembly.load(self.prompts, self.skills)
            return self.source

    def delta(self, number: int, body: dict) -> tuple[str, str, dict | None]:
        """This request's delta kind and reason, and the step request it is measured against.
        Side requests do not move the chain: the next step is measured against the step before."""
        current = {"number": number, **fingerprints(body)}
        with self.count_lock:
            previous = self.chain
            kind, reason = delta_kind(previous, current, bool(body.get("tools")))
            if kind != "side":
                self.chain = current
        return kind, reason, previous

    def next_folder(self) -> tuple[int, Path]:
        with self.count_lock:
            self.count += 1
            folder = self.run / f"{self.count:04d}"
        folder.mkdir()
        return self.count, folder


def _chunk(delta: dict, finish: str | None = None) -> bytes:
    choice = {"index": 0, "delta": delta, **({"finish_reason": finish} if finish else {})}
    return b"data: " + json.dumps({"choices": [choice]}).encode() + b"\n\n"


def stream_reply(write, reply: dict, prompt_tokens: int) -> None:
    if reply.get("error"):
        write(b"data: " + json.dumps({"error": reply["error"]}).encode() + b"\n\n")
        return
    text = reply.get("content") or ""
    for start in range(0, len(text), STREAM_PIECE):
        write(_chunk({"content": text[start:start + STREAM_PIECE]}))
        time.sleep(0.01)
    calls = reply.get("tool_calls") or []
    for index, call in enumerate(calls):
        raw = json.dumps(call["arguments"], ensure_ascii=False)
        half = len(raw) // 2  # Split like real providers do, so the add-on's reassembly is exercised.
        write(_chunk({"tool_calls": [{"index": index, "id": call["id"], "type": "function",
                                      "function": {"name": call["name"], "arguments": raw[:half]}}]}))
        write(_chunk({"tool_calls": [{"index": index, "function": {"arguments": raw[half:]}}]}))
    write(_chunk({}, "tool_calls" if calls else "stop"))
    output = round((len(text) + sum(len(json.dumps(c["arguments"])) for c in calls)) / CHARS_PER_TOKEN)
    usage = {"prompt_tokens": prompt_tokens, "completion_tokens": output,
             "total_tokens": prompt_tokens + output, "prompt_tokens_details": {"cached_tokens": 0}}
    write(b"data: " + json.dumps({"choices": [], "usage": usage}).encode() + b"\n\n")
    write(b"data: [DONE]\n\n")


def make_handler(bridge: Bridge):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            if not self.path.rstrip("/").endswith("/chat/completions"):
                self.send_error(404)
                return
            received = now()
            prompt_id = self.headers.get('x-loopcut-prompt') or None  # Absent or empty: pass-through.
            version = None
            if prompt_id is not None:
                try:
                    if prompt_id not in prompt_assembly.PROMPT_IDS:
                        raise prompt_assembly.AssemblyError('unknown_prompt', 'Unknown prompt id.')
                    source = bridge.prompt_source()
                    version = prompt_assembly.version(source)
                except prompt_assembly.AssemblyError as ex:
                    send_error(self, 400, ex, version)
                    return
                except (OSError, ValueError, KeyError) as ex:
                    print(f'cc_bridge: cannot load hosted prompts: {ex}', file=sys.stderr, flush=True)
                    send_error(self, 500, prompt_assembly.AssemblyError('server_error', 'Hosted prompts unavailable.'))
                    return
            try:
                length = int(self.headers.get('Content-Length', ''))
                if length < 0 or self.headers.get('Transfer-Encoding'):
                    raise ValueError('Unsupported request framing')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError('Expected an object')
                if prompt_id is not None:
                    body['messages'] = prompt_assembly.assemble(body.get('messages'), prompt_id, source)
            except prompt_assembly.AssemblyError as ex:
                send_error(self, 400, ex, version)
                return
            except (ValueError, UnicodeError):
                send_error(self, 400, prompt_assembly.AssemblyError('invalid_request_error', 'Invalid request body.'), version)
                return
            number, folder = bridge.next_folder()
            delta, reason, previous = bridge.delta(number, body)  # Before images become files.
            images = extract_images(body, folder)
            tokens = estimate_tokens(body)
            kind = "step" if body.get("tools") else "text_only"  # Summaries and reference cards send no tools.
            meta = {"number": number, "kind": kind, "received": received, "messages": len(body.get("messages") or []),
                    "images": images, "estimated_tokens": tokens, "request_bytes": int(self.headers["Content-Length"]),
                    "since_last_reply": round(received - bridge.last_streamed, 3) if bridge.last_streamed else None,
                    "delta": delta}
            meta.update({key: value for key, value in request_meta(self.headers).items() if value is not None})
            write_json(folder / "request.json", body)
            (folder / "prompt.md").write_text(render_prompt(number, body), encoding="utf-8")
            (folder / "delta.md").write_text(render_delta(number, body, delta, reason, previous), encoding="utf-8")
            write_json(folder / "meta.json", meta)  # Last: wait treats a folder with meta.json as ready.
            log(bridge.run, "request", number=number, kind=kind, messages=meta["messages"], images=images,
                estimated_tokens=tokens, since_last_reply=meta["since_last_reply"])

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            if version:
                self.send_header('x-loopcut-prompt-version', version)
            self.end_headers()
            self.close_connection = True

            def write(data: bytes) -> None:
                self.wfile.write(data)
                self.wfile.flush()

            reply_path, last_ping = folder / "reply.json", time.time()
            try:
                while not reply_path.exists():
                    time.sleep(POLL_SECONDS)
                    if time.time() - last_ping >= KEEPALIVE_SECONDS:
                        write(b": waiting for Claude Code\n\n")  # llm.stream_chat skips non-data lines.
                        last_ping = time.time()
                reply = json.loads(reply_path.read_text(encoding="utf-8"))
                stream_reply(write, reply, tokens)
            except (BrokenPipeError, ConnectionResetError):
                (folder / "cancelled").touch()
                log(bridge.run, "cancelled", number=number, after=round(time.time() - received, 3))
                return
            bridge.last_streamed = now()
            meta.update(streamed=bridge.last_streamed, model_seconds=round(bridge.last_streamed - received, 3))
            write_json(folder / "meta.json", meta)
            log(bridge.run, "streamed", number=number, model_seconds=meta["model_seconds"],
                tool_calls=[c["name"] for c in reply.get("tool_calls") or []], error=bool(reply.get("error")))

        def log_message(self, *args):
            pass

    return Handler


def serve(args) -> None:
    if args.run_dir:  # One bridge per eval task; wait/submit/report name the folder, so no current.txt.
        run = Path(args.run_dir).resolve()
        run.mkdir(parents=True, exist_ok=True)
    else:
        run = ROOT / datetime.now().strftime("%Y%m%d-%H%M%S")
        run.mkdir(parents=True)
        CURRENT.write_text(str(run))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(Bridge(
        run, prompts=getattr(args, 'prompts', None), skills=getattr(args, 'skills', None))))
    log(run, "serve", port=args.port)
    print(f"Bridge on http://127.0.0.1:{args.port}/v1, run folder {run}", flush=True)
    server.serve_forever()


# ------------------------------------------------------------------ Claude Code side

def pending(run: Path) -> list[Path]:
    return sorted(p for p in run.iterdir() if p.is_dir() and (p / "meta.json").exists()
                  and not (p / "claimed").exists() and not (p / "cancelled").exists())


def wait(args) -> None:
    """Prints REQUEST <folder> (claimed for the caller), DONE once the run's Blender has exited
    (run.py touches <run>/done), or IDLE when nothing came within the timeout: in an orchestrated
    eval, a worker wave is running inside Blender (delegate blocks) or the task is still queued."""
    run, deadline = run_of(args), time.time() + args.timeout
    while True:
        if (run / DONE).exists():  # Before any pending request: nothing will read its reply now.
            print("DONE")
            return
        found = pending(run) if run.is_dir() else []
        if found:
            folder = found[0]
            (folder / "claimed").write_text(str(now()))
            meta = json.loads((folder / "meta.json").read_text())
            log(run, "claimed", number=meta["number"])
            body = json.loads((folder / "request.json").read_text())
            last_user = next((m for m in reversed(body["messages"]) if m["role"] == "user"), {})
            content = last_user.get("content")
            text = content if isinstance(content, str) else " ".join(p.get("text", "") for p in content or [] if p.get("type") == "text")
            print(f"REQUEST {folder}\nkind={meta['kind']} delta={meta.get('delta')} messages={meta['messages']} "
                  f"images={meta['images']} ~{meta['estimated_tokens']} tokens, last role={body['messages'][-1]['role']}\n"
                  f"newest user text: {text[:300]!r}")
            return
        if time.time() >= deadline:
            break
        time.sleep(POLL_SECONDS)
    print("IDLE")


def validate(reply: dict, request: dict) -> list[str]:
    problems = []
    unknown = set(reply) - {"content", "tool_calls", "notes", "error"}
    if unknown:
        problems.append(f"unknown keys {sorted(unknown)}")
    if reply.get("error"):
        return problems
    offered = {t["function"]["name"]: t["function"].get("parameters") or {} for t in request.get("tools") or []}
    calls = reply.get("tool_calls") or []
    if not isinstance(reply.get("content") or "", str):
        problems.append("content must be a string or null")
    if not reply.get("content") and not calls:
        problems.append("reply has neither content nor tool_calls")
    if calls and not offered:
        problems.append("this request offered no tools; answer in content only")
    for i, call in enumerate(calls):
        name = call.get("name")
        if name not in offered:
            problems.append(f"tool_calls[{i}]: {name!r} is not an offered tool")
            continue
        if not isinstance(call.get("arguments"), dict):
            problems.append(f"tool_calls[{i}]: arguments must be a JSON object")
            continue
        missing = [k for k in offered[name].get("required") or [] if k not in call["arguments"]]
        if missing:
            problems.append(f"tool_calls[{i}] {name}: missing required {missing}")
    return problems


def submit(args) -> None:
    folder = Path(args.folder).resolve()
    run = folder.parent
    if (folder / "reply.json").exists():
        sys.exit("Already answered.")
    if (run / DONE).exists():
        sys.exit("The task's Blender has exited; nothing will read a reply. Do not retry: run wait, which prints DONE.")
    if (folder / "cancelled").exists():
        sys.exit("The user stopped this request in Blender; nothing will read a reply. Do not retry.")
    try:
        reply = json.loads((folder / "reply.draft.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as ex:
        sys.exit(f"REJECTED: reply.draft.json unreadable: {ex}")
    request = json.loads((folder / "request.json").read_text(encoding="utf-8"))
    problems = validate(reply, request)
    if problems:
        log(run, "rejected", number=int(folder.name), problems=problems)
        sys.exit("REJECTED:\n- " + "\n- ".join(problems))
    for i, call in enumerate(reply.get("tool_calls") or []):
        call["id"] = f"call_{folder.name}_{i}"
    reply["by"], reply["answered"] = args.by, now()
    write_json(folder / "reply.json", reply)
    log(run, "answered", number=int(folder.name), by=args.by, content_chars=len(reply.get("content") or ""),
        tool_calls=[c["name"] for c in reply.get("tool_calls") or []], has_notes=bool(reply.get("notes")))
    print("SUBMITTED")


def note(args) -> None:
    run = run_of(args)
    log(run, "note", text=args.text)
    print(f"Noted in {run.name}.")


# ------------------------------------------------------------------ report

def report(args) -> None:
    run = run_of(args)
    events = [json.loads(line) for line in (run / "events.jsonl").read_text().splitlines() if line]
    folders = sorted(p for p in run.iterdir() if p.is_dir() and (p / "meta.json").exists())
    lines = [f"# Claude Code bridge run {run.name}", ""]
    rows, tool_counts, notes_count = [], {}, 0
    seen_messages = 0
    for folder in folders:
        meta = json.loads((folder / "meta.json").read_text())
        body = json.loads((folder / "request.json").read_text())
        reply = json.loads((folder / "reply.json").read_text()) if (folder / "reply.json").exists() else None
        status = "cancelled" if (folder / "cancelled").exists() else "streamed" if meta.get("streamed") else "open"
        rows.append({**meta, "status": status, "by": (reply or {}).get("by"),
                     "tool_calls": [c["name"] for c in (reply or {}).get("tool_calls") or []]})
        lines += [f"## Request {meta['number']:04d} · {meta['kind']} · ~{meta['estimated_tokens']} tokens · "
                  f"{meta['messages']} messages · {status}", ""]
        if meta.get("since_last_reply") is not None:
            lines += [f"Blender side since the previous reply (tools, approvals, user): {meta['since_last_reply']} s · "
                      f"model: {meta.get('model_seconds')} s", ""]
        # Only what is new since the previous request; context.prepare may rewrite older messages,
        # so a shrinking count means earlier history was folded or summarized.
        messages = body["messages"]
        side = meta["kind"] == "text_only"  # A summary or reference card: its own short conversation.
        start = len(messages) - 1 if side else seen_messages if 0 < seen_messages <= len(messages) else 1
        if not side and seen_messages > len(messages):
            lines += [f"_History shrank from {seen_messages} to {len(messages)} messages (folded or summarized)._", ""]
        for i in range(start, len(messages)):
            m = messages[i]
            if m["role"] == "assistant":
                continue  # Shown under the reply that produced it.
            content = m.get("content")
            if isinstance(content, list):
                content = "\n".join(f"![image]({meta['number']:04d}/{p['image_url']['url'][5:]})"
                                    if p.get("type") == "image_url" and p["image_url"]["url"].startswith("file:")
                                    else p.get("text", "") for p in content)
            label = f"tool result {m.get('tool_call_id')}" if m["role"] == "tool" else m["role"]
            lines += [f"**{label}:**", "", (content or "").strip()[:4000], ""]
        if not side:
            seen_messages = len(messages) + 1  # The reply joins the history as the next message.
        if reply:
            lines += [f"**reply** (by {reply.get('by')}):", ""]
            if reply.get("error"):
                lines += [f"injected error: `{json.dumps(reply['error'])}`", ""]
            if reply.get("content"):
                lines += [reply["content"], ""]
            for call in reply.get("tool_calls") or []:
                tool_counts[call["name"]] = tool_counts.get(call["name"], 0) + 1
                arguments = dict(call["arguments"])
                code = arguments.pop("code", None)
                lines += [f"`{call['name']}` {json.dumps(arguments, ensure_ascii=False)}", ""]
                if code:
                    lines += ["```python", code, "```", ""]
            if reply.get("notes"):
                notes_count += 1
                lines += ["> **model notes:** " + reply["notes"].replace("\n", "\n> "), ""]
    human = [e for e in events if e["event"] == "note"]
    if human:
        lines += ["## Tester notes", ""] + [f"- {datetime.fromtimestamp(e['t']):%H:%M:%S} {e['text']}" for e in human] + [""]
    streamed = [r for r in rows if r.get("model_seconds")]
    summary = {
        "run": run.name,
        "requests": len(rows),
        "steps": sum(r["kind"] == "step" for r in rows),
        "text_only_requests": sum(r["kind"] == "text_only" for r in rows),
        "cancelled": sum(r["status"] == "cancelled" for r in rows),
        "rejected_submissions": sum(e["event"] == "rejected" for e in events),
        "estimated_input_tokens": sum(r["estimated_tokens"] for r in rows),
        "largest_request_tokens": max((r["estimated_tokens"] for r in rows), default=0),
        "images_sent": sum(r["images"] for r in rows),
        "tool_calls": tool_counts,
        "replies_with_notes": notes_count,
        "model_seconds": round(sum(r["model_seconds"] for r in streamed), 1),
        "blender_seconds": round(sum(r["since_last_reply"] or 0 for r in rows), 1),
        "per_request": [{k: r.get(k) for k in ("number", "kind", "estimated_tokens", "messages", "images",
                                               "model_seconds", "since_last_reply", "status", "by", "tool_calls")}
                        for r in rows],
    }
    table = ["| # | kind | ~tokens | msgs | imgs | model s | blender s | calls |", "|---|---|---|---|---|---|---|---|"]
    table += [f"| {r['number']} | {r['kind']} | {r['estimated_tokens']} | {r['messages']} | {r['images']} | "
              f"{r.get('model_seconds') or ''} | {r.get('since_last_reply') or ''} | {', '.join(r['tool_calls'])} |" for r in rows]
    head = [f"Requests {summary['requests']} ({summary['steps']} steps), ~{summary['estimated_input_tokens']} input tokens "
            f"(largest ~{summary['largest_request_tokens']}), {summary['images_sent']} images, "
            f"{summary['cancelled']} cancelled, {summary['rejected_submissions']} rejected submissions. "
            f"Model time {summary['model_seconds']} s, Blender time {summary['blender_seconds']} s. "
            f"Token counts are the add-on's own estimate, not a tokenizer.", ""] + table + [""]
    lines[2:2] = head
    (run / "transcript.md").write_text("\n".join(lines), encoding="utf-8")
    write_json(run / "summary.json", summary)
    print(f"{run / 'transcript.md'}\n{run / 'summary.json'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("serve")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--run-dir", help="use this run folder and leave current.txt alone (one bridge per eval task)")
    p.add_argument('--prompts', type=Path, help='hosted prompts directory (default LOOPCUT_PROMPTS_DIR)')
    p.add_argument('--skills', type=Path, help='server skills directory (default LOOPCUT_SERVER_SKILLS_DIR)')
    p.set_defaults(func=serve)
    p = commands.add_parser("wait")
    p.add_argument("--run", help="the run folder (serve --run-dir); default: the current run")
    p.add_argument("--timeout", type=float, default=3000)
    p.set_defaults(func=wait)
    p = commands.add_parser("submit")
    p.add_argument("folder")
    p.add_argument("--by", required=True, help="which model answered, e.g. opus-5.5")
    p.set_defaults(func=submit)
    p = commands.add_parser("note")
    p.add_argument("text")
    p.add_argument("--run", help="the run folder; default: the current run")
    p.set_defaults(func=note)
    p = commands.add_parser("report")
    p.add_argument("run", nargs="?")
    p.set_defaults(func=report)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
