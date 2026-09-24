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
reads), meta.json, reply.json (with the model's private notes, which are never streamed).
"""

import argparse
import base64
import json
import re
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[3]
ROOT = WORKSPACE / "out" / "cc_bridge"
CURRENT = ROOT / "current.txt"
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
    for i, message in enumerate(messages):
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
    return "\n".join(out)


# ------------------------------------------------------------------ server

class Bridge:
    def __init__(self, run: Path):
        self.run = run
        self.count = 0
        self.last_streamed: float | None = None
        self.count_lock = threading.Lock()

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
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            number, folder = bridge.next_folder()
            images = extract_images(body, folder)
            tokens = estimate_tokens(body)
            kind = "step" if body.get("tools") else "text_only"  # Summaries and reference cards send no tools.
            meta = {"number": number, "kind": kind, "received": received, "messages": len(body.get("messages") or []),
                    "images": images, "estimated_tokens": tokens, "request_bytes": int(self.headers["Content-Length"]),
                    "since_last_reply": round(received - bridge.last_streamed, 3) if bridge.last_streamed else None}
            write_json(folder / "request.json", body)
            (folder / "prompt.md").write_text(render_prompt(number, body), encoding="utf-8")
            write_json(folder / "meta.json", meta)
            log(bridge.run, "request", number=number, kind=kind, messages=meta["messages"], images=images,
                estimated_tokens=tokens, since_last_reply=meta["since_last_reply"])

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
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
    run = ROOT / datetime.now().strftime("%Y%m%d-%H%M%S")
    run.mkdir(parents=True)
    CURRENT.write_text(str(run))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(Bridge(run)))
    log(run, "serve", port=args.port)
    print(f"Bridge on http://127.0.0.1:{args.port}/v1, run folder {run}", flush=True)
    server.serve_forever()


# ------------------------------------------------------------------ Claude Code side

def pending(run: Path) -> list[Path]:
    return sorted(p for p in run.iterdir() if p.is_dir() and (p / "meta.json").exists()
                  and not (p / "claimed").exists() and not (p / "cancelled").exists())


def wait(args) -> None:
    run, deadline = current_run(), time.time() + args.timeout
    while time.time() < deadline:
        found = pending(run)
        if found:
            folder = found[0]
            (folder / "claimed").write_text(str(now()))
            meta = json.loads((folder / "meta.json").read_text())
            log(run, "claimed", number=meta["number"])
            body = json.loads((folder / "request.json").read_text())
            last_user = next((m for m in reversed(body["messages"]) if m["role"] == "user"), {})
            content = last_user.get("content")
            text = content if isinstance(content, str) else " ".join(p.get("text", "") for p in content or [] if p.get("type") == "text")
            print(f"REQUEST {folder}\nkind={meta['kind']} messages={meta['messages']} images={meta['images']} "
                  f"~{meta['estimated_tokens']} tokens, last role={body['messages'][-1]['role']}\n"
                  f"newest user text: {text[:300]!r}")
            return
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
    run = current_run()
    log(run, "note", text=args.text)
    print(f"Noted in {run.name}.")


# ------------------------------------------------------------------ report

def report(args) -> None:
    run = Path(args.run) if args.run else current_run()
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
    p.set_defaults(func=serve)
    p = commands.add_parser("wait")
    p.add_argument("--timeout", type=float, default=3000)
    p.set_defaults(func=wait)
    p = commands.add_parser("submit")
    p.add_argument("folder")
    p.add_argument("--by", required=True, help="which model answered, e.g. opus-5.5")
    p.set_defaults(func=submit)
    p = commands.add_parser("note")
    p.add_argument("text")
    p.set_defaults(func=note)
    p = commands.add_parser("report")
    p.add_argument("run", nargs="?")
    p.set_defaults(func=report)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
