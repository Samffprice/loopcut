"""A scripted stand-in for an OpenAI-compatible model: /chat/completions as SSE on 127.0.0.1.

Harness checks drive the real agent against it, so everything but the model runs for real and
nothing costs tokens. It is the server from tests/test_agent_loop.py, made to serve several
conversations at once: scripts are keyed by the request's `model`, so an orchestrator and its
workers (LOOPCUT_ORCH_MODEL, LOOPCUT_WORKER_MODEL) each follow their own, and the workers of one
wave, which share a model, are told apart by a `route` function of the request.

    server = fake_model.Server().start()
    server.script("orch", [fake_model.tool_reply("run_python", {...}), fake_model.text_reply("Done.")])
    server.script("worker", {"BoxA": [...], "BoxB": [...]},
                  route=fake_model.named_in_first_message(["BoxA", "BoxB"]))
    os.environ["LOOPCUT_BASE_URL"] = server.base_url
    os.environ["LOOPCUT_SERVICE_URLS"] = server.base_url  # Stand in for the gateway's workers/router.
    os.environ["LOOPCUT_PROMPT_INDEX"] = str(Path(fake_model.__file__).parent / "fixtures/prompt-index.json")

Every request is logged (`server.log`) with its model, route key, arrival and reply times and the
usage its reply reported, so a check can assert ordering (a worker's first request after another's
first reply) and accounting. Each reply ends with a usage chunk (prompt tokens = request bytes / 4)
unless it carries its own. A request with nothing scripted for it gets HTTP 400, which llm.py does
not retry, and the reason is added to `server.errors`.

As a process, for a windowed run (run_task) against scripted models:

    python3 loopcut/harness/fake_model.py --port 8766 --script script.json

script.json is {"<model>": [<reply>, ...]}, or {"<model>": {"route": ["BoxA", "BoxB"], "BoxA":
[<reply>, ...], ...}} for several conversations; a reply is {"text": "..."}, {"tool": name,
"arguments": {...}} or {"tools": [[name, {...}], ...]}. Point LOOPCUT_BASE_URL (and the
LOOPCUT_ORCH_* / LOOPCUT_WORKER_* settings) at http://127.0.0.1:<port>/v1. For workers or routing,
also set LOOPCUT_SERVICE_URLS to that base URL so the fake model stands in for the gateway.
"""

import argparse
import json
import re
import signal
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DONE = b"data: [DONE]\n\n"


def sse(*chunks: dict) -> bytes:
    return b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks) + DONE


def text_reply(*pieces: str) -> bytes:
    return sse(*({"choices": [{"delta": {"content": p}}]} for p in pieces),
               {"choices": [{"delta": {}, "finish_reason": "stop"}]})


def tool_reply(name: str, arguments: dict, call_id: str = "call_1") -> bytes:
    raw = json.dumps(arguments)
    half = len(raw) // 2  # Arguments arrive split across chunks, as real APIs send them.
    return sse(
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": call_id, "type": "function",
                                                "function": {"name": name, "arguments": raw[:half]}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": raw[half:]}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    )


def multi_tool_reply(*calls: tuple) -> bytes:
    """Several tool calls in one reply, as parallel tool calls arrive."""
    return sse(*({"choices": [{"delta": {"tool_calls": [{"index": i, "id": f"call_{i + 1}", "type": "function",
                                                        "function": {"name": name, "arguments": json.dumps(args)}}]}}]}
                 for i, (name, args) in enumerate(calls)),
               {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})


def from_json(spec: dict) -> bytes:
    """One reply of a script.json."""
    if "text" in spec:
        return text_reply(spec["text"])
    if "tool" in spec:
        return tool_reply(spec["tool"], spec.get("arguments", {}))
    if "tools" in spec:
        return multi_tool_reply(*(tuple(call) for call in spec["tools"]))
    raise ValueError(f"a reply is {{text}}, {{tool, arguments}} or {{tools}}, got {sorted(spec)}")


def message_text(message: dict) -> str:
    content = message.get("content") or ""
    if isinstance(content, list):
        return "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
    return str(content)


def first_user_text(body: dict) -> str:
    return next((message_text(m) for m in body.get("messages", []) if m.get("role") == "user"), "")


def named_in_first_message(names: list[str]):
    """A route: the one of `names` that the conversation's first user message mentions, as a
    whole name (BoxA matches "BoxA" and "BoxA_Leg", not "BoxAB"). A worker's first message is its
    brief, so a prefix or deliverable picks the worker; only its <brief> part counts when it has
    one, since the shared part names the wave's other workers too."""
    patterns = {name: re.compile(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])") for name in names}

    def route(body: dict) -> str:
        text = first_user_text(body)
        brief = re.search(r"<brief>(.*?)</brief>", text, re.S)
        text = brief.group(1) if brief else text
        found = [name for name, pattern in patterns.items() if pattern.search(text)]
        if len(found) != 1:
            raise LookupError(f"the first user message names {found or 'none'} of {names}; expected exactly one")
        return found[0]
    return route


def _usage_in(reply: bytes) -> dict | None:
    for line in reply.split(b"\n"):
        if line.startswith(b"data: {") and b'"usage"' in line:
            usage = json.loads(line[6:]).get("usage")
            if isinstance(usage, dict):
                return {"input": int(usage.get("prompt_tokens") or 0), "output": int(usage.get("completion_tokens") or 0)}
    return None


@dataclass
class Request:
    model: str
    key: str | None        # The route's answer; None for a model with one conversation.
    n: int                 # Position in its conversation (model, key), from 0.
    body: dict
    auth: str
    received: float        # time.monotonic() when the request arrived.
    replied: float = 0.0   # When the reply was written in full; 0 if it was not.
    status: int = 200
    usage: dict = field(default_factory=dict)  # {"input", "output"} as the reply reported them.
    raw_body: bytes = b""  # Exact HTTP entity bytes, before JSON decoding; parity checks must use these.


class Server:
    def __init__(self, usage: bool = True):
        self.usage = usage
        self.headers: dict = {}  # Sent with every reply, like the gateway's x-loopcut-* headers.
        self.log: list[Request] = []
        self.errors: list[str] = []
        self._scripts: dict[str, dict] = {}   # model -> {key: [reply, ...]}
        self._routes: dict = {}               # model -> route(body) -> key
        self._lock = threading.Lock()
        self._httpd = None

    def start(self, port: int = 0) -> "Server":
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                server._handle(self)

            def log_message(self, *args):
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self._httpd.daemon_threads = True
        threading.Thread(target=self._httpd.serve_forever, name="fake-model", daemon=True).start()
        return self

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def script(self, model: str, replies, route=None) -> None:
        """replies: a list for one conversation, or {key: list} with `route(body) -> key` choosing.
        A reply is SSE bytes, (status, payload) for an HTTP error (a str payload becomes the
        error message), or a callable(body) returning either."""
        with self._lock:
            if isinstance(replies, dict):
                if route is None:
                    raise ValueError("several conversations on one model need a route")
                self._scripts[model] = {key: list(queue) for key, queue in replies.items()}
                self._routes[model] = route
            else:
                self._scripts[model] = {None: list(replies)}
                self._routes.pop(model, None)

    def reset(self) -> None:
        with self._lock:
            self._scripts.clear()
            self._routes.clear()
            self.log.clear()
            self.errors.clear()

    def requests(self, model: str | None = None, key=...) -> list[Request]:
        with self._lock:
            return [r for r in self.log if (model is None or r.model == model) and (key is ... or r.key == key)]

    def remaining(self) -> dict:
        """(model, key) -> replies not asked for yet."""
        with self._lock:
            return {(model, key): len(queue) for model, queues in self._scripts.items()
                    for key, queue in queues.items() if queue}

    def _next(self, body: dict) -> tuple[Request, object]:
        model = str(body.get("model", ""))
        with self._lock:
            reply, key = None, None
            try:
                key = self._routes[model](body) if model in self._routes else None
            except Exception as ex:  # The route's own message says what it could not tell apart.
                reply = (400, f"fake_model: {model}: {ex}")
            n = sum(1 for r in self.log if r.model == model and r.key == key)
            if reply is None:
                queue = self._scripts.get(model, {}).get(key)
                if queue is None:
                    reply = (400, f"fake_model: nothing scripted for model {model!r}"
                                  + (f" conversation {key!r}" if key is not None else ""))
                elif not queue:
                    reply = (400, f"fake_model: {model}{'/' + key if key else ''}: no reply left for request {n + 1}")
                else:
                    reply = queue.pop(0)
            record = Request(model, key, n, body, "", time.monotonic())
            self.log.append(record)
            return record, reply

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        raw = handler.rfile.read(int(handler.headers.get("Content-Length") or 0))
        if not handler.path.rstrip("/").endswith("/chat/completions"):
            self._send_json(handler, 404, {"error": {"message": f"fake_model: no route {handler.path}"}})
            return
        record, reply = self._next(json.loads(raw))
        record.raw_body = raw
        record.auth = handler.headers.get("Authorization", "")
        if callable(reply):
            reply = reply(record.body)
        if isinstance(reply, tuple):
            status, payload = reply
            if isinstance(payload, str):
                with self._lock:
                    self.errors.append(payload)
                payload = {"error": {"message": payload}}
            record.status = status
            self._send_json(handler, status, payload)
            record.replied = time.monotonic()
            return
        usage = _usage_in(reply)
        if usage is None and self.usage:
            usage = {"input": max(1, len(raw) // 4), "output": max(1, len(reply) // 16)}
            chunk = {"choices": [], "usage": {"prompt_tokens": usage["input"], "completion_tokens": usage["output"]}}
            reply = reply[:-len(DONE)] + b"data: " + json.dumps(chunk).encode() + b"\n\n" + DONE
        record.usage = usage or {}
        try:
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            for name, value in self.headers.items():
                handler.send_header(name, value)
            handler.end_headers()
            handler.wfile.write(reply)
            handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):  # The client gave up (a Stop).
            record.status = 499
            return
        record.replied = time.monotonic()

    def _send_json(self, handler, status: int, payload: dict) -> None:
        try:
            handler.send_response(status)
            handler.send_header("Content-Type", "application/json")
            for name, value in self.headers.items():
                handler.send_header(name, value)
            handler.end_headers()
            handler.wfile.write(json.dumps(payload).encode())
        except (BrokenPipeError, ConnectionResetError):
            pass


def load_script(server: Server, spec: dict) -> None:
    for model, replies in spec.items():
        if isinstance(replies, dict):
            replies = dict(replies)
            names = replies.pop("route")
            server.script(model, {key: [from_json(r) for r in queue] for key, queue in replies.items()},
                          named_in_first_message(names))
        else:
            server.script(model, [from_json(r) for r in replies])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--script", required=True, help="JSON: model -> replies (see the module docstring)")
    args = parser.parse_args()
    spec = json.loads(Path(args.script).read_text(encoding="utf-8"))
    server = Server()
    load_script(server, spec)
    server.start(args.port)
    print(f"fake_model: {server.base_url} serving {', '.join(spec)}", flush=True)
    signal.signal(signal.SIGTERM, signal.default_int_handler)  # kill ends it like Ctrl+C, with the summary.
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
        for record in server.log:
            print(f"{record.model}{'/' + record.key if record.key else ''} #{record.n + 1}: {record.status}")
        for line in server.errors:
            print(line)
        left = server.remaining()
        if left:
            print("not asked for:", ", ".join(f"{m}{'/' + k if k else ''} ×{n}" for (m, k), n in left.items()))


if __name__ == "__main__":
    main()
