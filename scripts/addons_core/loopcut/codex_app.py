"""The workspace agent run by Codex's own agent loop (LOOPCUT_CODEX_APP=on): `codex app-server` over stdio
JSON-RPC, a thread whose working directory is the conversation's workspace (workspace.py), and Loopcut's
live-scene tools as Codex dynamic tools.

Why: on the Arcane chest lookdev GPT-6.1 Sol alone in a plain `codex exec` shell did far better than the same
model inside Loopcut, even once the workspace had an environment-only prompt (evals 20260930-224428_sol_raw
against 20261001-*). What was left was the loop: Loopcut's driver had Codex role-play the model behind Loopcut
and answer each step in JSON, so it never used its own shell, apply_patch, view_image or context handling.
Here Codex drives: its prompt, shell, file edits, image viewing and compaction are its own, and Loopcut adds
only what Codex lacks: the live scene and a shell-free Blender script runner (DYNAMIC). The turn, its cards, approvals, checkpoints and the end of
turn bookkeeping stay agent.run_loop's (CODEX_SPEC hands it the turn through LoopSpec.drive).

One app-server per turn, started on the agent thread in a session of its own, so that a Stop takes the
commands Codex started with it; the thread is kept on disk under CODEX_HOME (not ephemeral) and the next turn
resumes it by id (session["codex_thread"]). Codex signs in on its own: the user's ChatGPT plan, never an API
key (OPENAI_API_KEY and CODEX_API_KEY are not passed on). Every JSON-RPC line both ways is logged as it is, one
file per turn, in the workspace's .loopcut/codex/.

The protocol (codex-cli 0.159.0, its experimental API; probed 2026-10-01, scratchpad appserver_probe/):
  initialize {clientInfo, capabilities: {experimentalApi: true}}, then the "initialized" notification.
  thread/start {cwd, model, approvalPolicy, sandbox, developerInstructions, dynamicTools: [{type: "function",
    name, description, inputSchema}], ephemeral}; thread/resume {threadId} in a later process keeps the tools.
  turn/start {threadId, input: [{type: "text"}, {type: "localImage", path}], effort, summary, approvalPolicy};
    turn/steer adds the user's mid-turn note; turn/interrupt ends the turn as "interrupted".
  The server asks item/tool/call {callId, tool, arguments} and takes {success, contentItems: [{type: "inputText",
    text} | {type: "inputImage", imageUrl: a data URL}]}; item/commandExecution/requestApproval and
    item/fileChange/requestApproval take {decision: "accept" | "decline"}.
  It tells item/started and item/completed (agentMessage, commandExecution, fileChange, imageView, reasoning,
    ...), item/agentMessage/delta, thread/tokenUsage/updated (its "last": one model request) and turn/completed
    {turn: {status, error}}.
  app-server has no --ignore-user-config: it reads $CODEX_HOME/config.toml, so OVERRIDES switch off what the
    user's own Codex adds (MCP servers, plugins, apps, memories, hooks, notify, web search, AGENTS.md files).

Sandbox: Blender crashes inside Codex's macOS seatbelt sandbox (Metal detection), so commands always run with
full access. With auto-run on Codex never asks; off, every command it does not trust asks through Loopcut's
approval card ("untrusted").
"""

import base64
import json
import os
import queue
import re
import signal
import subprocess
import threading
import time
from pathlib import Path

from . import agent, checkpoints, cloud, config, context, conversations, llm, router, state, tools, workspace

CLIENT = {"name": "loopcut", "title": "Loopcut", "version": "1"}
LOG_FOLDER = "codex"          # In the workspace's .loopcut/: one JSON-RPC transcript per turn.
REQUEST_SECONDS = 120         # The answer to one request (initialize, thread/start...); a turn itself has no limit.
INTERRUPT_SECONDS = 15        # After Stop, how long the turn has to end before its process is killed.
CLOSE_SECONDS = 5
POLL = 0.1
SHOWN = 4000                  # Characters of a command's output or a patch on its card.
SENT = "loopcut_codex"        # On a turn's message once Codex has it: a resumed turn sends RESUME instead.
RESUME = "Go on from where you stopped."
# Blender tools Codex lacks, including a shell-free script runner; its own file edits and image viewer do the rest.
DYNAMIC = ("apply_build", "snapshot_scene", "run_blender", "run_python", "get_scene_info", "inspect_api", "capture_viewport",
           "search_assets", "import_asset")
# What the user's own ~/.codex/config.toml would add to a Loopcut thread: MCP servers (switched off by name,
# _mcp_servers), plugins, apps, memories, hooks, a notify program, web search, AGENTS.md files and personal
# skills. allow_login_shell=false keeps the shell's PATH as given: a login shell's profile puts /usr/bin first,
# ahead of the workspace's blender and python3.
OVERRIDES = ("notify=[]", 'web_search="disabled"', "project_doc_max_bytes=0", "allow_login_shell=false",
             "skills.include_instructions=false") + tuple(
    f"features.{name}=false" for name in ("plugins", "apps", "memories", "browser_use", "computer_use", "hooks",
                                          "tool_suggest"))
_MCP_SERVER = re.compile(r'(?m)^\s*\[mcp_servers\.("?)([^".\]\s]+)\1\]')
_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}

# The thread's developer instructions: what this environment is, as workspace.PROMPT says it, and no workflow.
# Codex knows its own shell, apply_patch and view_image; only Loopcut's side is described.
PROMPT = """You are Loopcut, working inside the Blender the user has open.

Your working directory is this conversation's workspace. It holds source.blend (the live scene as this \
conversation's first message found it; keep it as it is), the images the user attached, at full size, and every \
file you make. The <workspace> note on a message lists it. In your shell, blender is this Blender, to run headless \
(blender -b source.blend --python script.py), and python3 is Blender's Python, with numpy. A headless run has no \
window: save with bpy. In it // is the workspace; in the live scene it is the .blend's folder.
On Windows, write Python to a .py file and run that file instead of using python -c, py -c or Blender's \
--python-expr: shell quoting and expansion can change inline code. Run plain Python as python3 file.py; for bpy \
use run_python in the live scene or run_blender(script="script.py", blend="") in an empty background scene.

The user's live scene is reached through Loopcut's tools:
- apply_build brings a result .blend into the live scene in one undo step: what the build created comes in, what \
you name in owns replaces its live version, and anything else the build changed is reported, not applied. A scene \
the build made comes in as a scene of its own. Applying after a rebuild replaces what the last apply brought. What \
you apply is what the user gets.
- snapshot_scene saves the live scene as source.blend again (the previous one kept as source-<n>.blend), to build \
on what is live now.
- run_python runs bpy code in the live scene, get_scene_info reads it, capture_viewport shows it; inspect_api \
describes its live API and turns on a bundled add-on it names in the live Blender.
- search_assets and import_asset bring in assets from BlenderKit, Poly Haven, Poly Pizza or the user's own libraries.

Each user message starts with <scene_context>: the open file, mode, selection and objects; @Name means that \
object, material or collection.

Leave what the user did not ask you to change as it was; never delete their objects, materials or files. When you \
finish, say briefly what you did and anything the user must decide."""


def developer_instructions(addons_line: str = "") -> str:
    return f"{PROMPT}\n\n{addons_line}" if addons_line else PROMPT


def dynamic_tools() -> list[dict]:
    """DYNAMIC as Codex dynamic tools, with the schemas and descriptions the HTTP loop sends."""
    by_name = {s["function"]["name"]: s["function"] for s in agent._tool_schemas() + workspace.SCHEMAS}
    names = [*DYNAMIC, *(name for name in by_name if cloud.is_tool(name))]
    return [{"type": "function", "name": name, "description": by_name[name]["description"],
             "inputSchema": by_name[name]["parameters"]} for name in names if name in by_name]


def codex_home(environ=None) -> Path:
    """The Codex account: CODEX_HOME when the user set one, else Codex's own default."""
    environ = os.environ if environ is None else environ
    return Path(environ.get("CODEX_HOME") or Path.home() / ".codex")


def _mcp_servers(home: Path) -> list[str]:
    """Names of the MCP servers the user's config.toml declares (its table headers only)."""
    try:
        text = (home / "config.toml").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    return list(dict.fromkeys(name for _, name in _MCP_SERVER.findall(text)))


def command(app: config.CodexApp, home: Path) -> list[str]:
    overrides = OVERRIDES + tuple(f"mcp_servers.{name}.enabled=false" for name in _mcp_servers(home))
    return [app.binary, "app-server", *(arg for o in overrides for arg in ("-c", o))]


def environment(ws: Path, home: Path, environ=None) -> dict:
    """The workspace shell's environment (workspace.shell_env: its blender and python3 first on PATH, no keys or
    Loopcut settings) for Codex and every command it runs, with the Codex account and no API key."""
    env = workspace.shell_env(ws, environ)
    for name in ("OPENAI_API_KEY", "CODEX_API_KEY"):  # Would bill an API account instead of the plan.
        env.pop(name, None)
    env["CODEX_HOME"] = str(home)
    return env


def _text_input(text: str) -> dict:
    return {"type": "text", "text": text, "text_elements": []}


def _image_file(session: dict, ws: Path, ref: str) -> Path | None:
    """An attached image's full-size copy in the workspace, else the conversation's own copy."""
    reference = next((r for r in session.get("references") or () if r.get("ref") == ref), {})
    key = reference.get("full") or ref
    name = (session.get("workspace_refs") or {}).get(key)
    if name and (ws / name).is_file():
        return ws / name
    try:
        path = conversations.image_path(session["id"], key)
    except Exception:  # noqa: BLE001 - a reference that is not an image of this conversation
        return None
    return path if path.is_file() else None


def turn_input(session: dict, ws: Path) -> list[dict]:
    """The message that started the turn as Codex input: its text (<scene_context>, what the user wrote, the
    <workspace> note), images as local files, and tools already run for this request (Try it). A turn
    resumed after it was sent goes on instead."""
    messages = session["messages"]
    start = next((i for i in range(len(messages) - 1, -1, -1) if context.is_turn_start(messages[i])), None)
    message = messages[start] if start is not None else None
    if message is None or message.get(SENT):
        return [_text_input(RESUME)]
    content = message.get("content")
    refs = [p["image_url"]["url"] for p in content if isinstance(p, dict) and p.get("type") == "image_url"] \
        if isinstance(content, list) else []
    images = [path for path in (_image_file(session, ws, ref) for ref in refs) if path is not None]
    sent = [_text_input(router._text(message)), *({"type": "localImage", "path": str(p)} for p in images)]
    recorded = [m for m in messages[start + 1:] if m.get("tool_calls") or m.get("role") == "tool"]
    if recorded:
        sent.append(_text_input("Loopcut recorded these tool calls and results for this request before starting "
                                "Codex. Continue from their results without repeating completed work:\n"
                                + json.dumps(recorded)))
    return sent


def data_url(path: Path) -> str:
    """The bytes say the type (a stored <sha>.png can hold a clip's JPEG sheet); else the suffix."""
    data = path.read_bytes()
    media = conversations.media_type(data, _MIME.get(path.suffix.lower(), "image/png"))
    return f"data:{media};base64,{base64.b64encode(data).decode()}"


class Server:
    """One codex app-server process: JSON-RPC lines both ways, every one logged as it is (log: JSONL of
    {t, dir: out|in|start, raw}); its stderr beside the log."""

    def __init__(self, cmd: list, cwd: Path, env: dict, log: Path):
        log.parent.mkdir(parents=True, exist_ok=True)
        self.log = log.open("a", encoding="utf-8")
        self.lock = threading.Lock()
        self.inbox: queue.Queue = queue.Queue()
        self.last_id = 0
        self._record("start", json.dumps({"command": cmd, "cwd": str(cwd)}))
        with log.with_suffix(".stderr.txt").open("ab") as stderr:
            self.process = subprocess.Popen(cmd, cwd=str(cwd), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=stderr, start_new_session=os.name != "nt")
        threading.Thread(target=self._read, name="loopcut-codex-read", daemon=True).start()

    def _record(self, direction: str, line: str) -> None:
        with self.lock:
            if not self.log.closed:
                self.log.write(json.dumps({"t": round(time.time(), 3), "dir": direction, "raw": line}) + "\n")
                self.log.flush()

    def _read(self) -> None:
        try:
            for raw in self.process.stdout:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                self._record("in", line)
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if isinstance(message, dict):
                    self.inbox.put(message)
        finally:
            self.process.stdout.close()
            self.inbox.put(None)  # The server has exited.

    def send(self, message: dict) -> None:
        line = json.dumps(message)
        self._record("out", line)
        try:
            self.process.stdin.write(line.encode("utf-8") + b"\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as ex:
            raise llm.LLMError(f"Codex stopped unexpectedly ({ex}); see {Path(self.log.name).name}.") from ex

    def request(self, method: str, params: dict) -> int:
        self.last_id += 1
        self.send({"jsonrpc": "2.0", "id": self.last_id, "method": method, "params": params})
        return self.last_id

    def notify(self, method: str) -> None:
        self.send({"jsonrpc": "2.0", "method": method})

    def respond(self, request_id, result: dict) -> None:
        self.send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def refuse(self, request_id, message: str) -> None:
        self.send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": message}})

    def close(self) -> None:
        """Ends the server and everything it started: stdin closed, then its process group stopped."""
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=CLOSE_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        for sig in (signal.SIGTERM, signal.SIGKILL) if os.name != "nt" else ():
            try:
                os.killpg(self.process.pid, sig)  # Its commands too: they share its session.
            except (ProcessLookupError, PermissionError):
                break
            time.sleep(0.5)
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait()
        with self.lock:
            self.log.close()


class CodexTurn:
    """One user turn in a Codex thread: started, its events turned into chat items and messages, its dynamic
    tool calls run through run_tool, its approvals asked through Loopcut's cards."""

    def __init__(self, session: dict, turn, cfg: config.Config, app: config.CodexApp, ws: Path, server: Server,
                 run_tool, ensure_checkpoint):
        self.session, self.turn, self.cfg, self.app, self.ws, self.server = session, turn, cfg, app, ws, server
        self.run_tool, self.ensure_checkpoint = run_tool, ensure_checkpoint
        self.items, self.messages = session["items"], session["messages"]
        self.thread = self.turn_id = ""
        self.cards: dict = {}     # Codex item id -> its tool card.
        self.replies: dict = {}   # Codex item id -> its assistant item, streaming.
        self.status = ""          # The turn's, once completed: completed | interrupted | failed.
        self.error = ""
        self.interrupted_at = 0.0
        self.steering = True

    # -------------------------------------------------------------- the turn

    def run(self) -> str:
        self.call("initialize", {"clientInfo": CLIENT, "capabilities": {"experimentalApi": True,
                                                                         "requestAttestation": False}})
        self.server.notify("initialized")
        policy = "never" if self.cfg.auto_run or self.session["auto_run"] else "untrusted"
        model = {"model": self.app.model} if self.app.model else {}
        settings = {"cwd": str(self.ws), "approvalPolicy": policy, "sandbox": "danger-full-access", **model}
        thread = self.session.get("codex_thread") or ""
        if thread:
            try:
                self.call("thread/resume", {"threadId": thread, **settings})
            except llm.LLMError as ex:
                print(f"Loopcut: Codex could not resume thread {thread}: {ex}")
                self.items.append(state.item_notice("Codex could not continue its earlier thread; it starts a new "
                                                    "one, with the workspace as it is."))
                thread = ""
        if not thread:
            result = self.call("thread/start", {
                **settings, "developerInstructions": developer_instructions(self.session.get("addons_line") or ""),
                "dynamicTools": dynamic_tools(), "ephemeral": False})
            thread = result["thread"]["id"]
            self.session["codex_thread"] = thread
            conversations.save(self.session)
        self.thread = thread
        result = self.call("turn/start", {
            "threadId": thread, "input": turn_input(self.session, self.ws), "approvalPolicy": policy,
            "sandboxPolicy": {"type": "dangerFullAccess"}, "summary": self.app.summary, **model,
            **({"effort": self.app.effort} if self.app.effort else {})})
        self.turn_id = result["turn"]["id"]
        started = next((m for m in reversed(self.messages) if context.is_turn_start(m)), None)
        if started is not None:
            started[SENT] = True
        while not self.status:
            if self.turn.cancel.is_set():
                self.interrupt()
            elif self.turn.notes and self.steering:
                self.steer()
            try:
                message = self.server.inbox.get(timeout=POLL)
            except queue.Empty:
                continue
            if message is None:
                raise llm.LLMError(f"Codex exited during the turn; see {Path(self.server.log.name).name}.")
            self.handle(message)
        if self.status == "interrupted" or self.turn.cancel.is_set():
            raise llm.Cancelled()
        if self.status == "failed":
            raise llm.LLMError(f"Codex: {self.error or 'the turn failed'}")
        return "completed"

    def interrupt(self) -> None:
        """Stop: the turn is interrupted, and given INTERRUPT_SECONDS to end before its process is killed."""
        if not self.interrupted_at:
            self.interrupted_at = time.monotonic()
            if self.turn_id:
                self.server.request("turn/interrupt", {"threadId": self.thread, "turnId": self.turn_id})
        elif time.monotonic() - self.interrupted_at > INTERRUPT_SECONDS:
            raise llm.Cancelled()

    def steer(self) -> None:
        """What the user sent while Codex works goes into the running turn."""
        text, item = self.turn.notes[0]
        try:
            self.call("turn/steer", {"threadId": self.thread, "expectedTurnId": self.turn_id,
                                     "input": [_text_input(text)]})
        except llm.LLMError as ex:  # Left for run_loop's end, which sends it as the next message.
            print(f"Loopcut: could not pass the note to Codex: {ex}")
            self.steering = False
            return
        self.turn.notes.pop(0)
        item.pop("queued", None)
        self.messages.append({"role": "user", context.STEER: True, "content": f"<steer>{text}</steer>"})
        agent._redraw()

    def call(self, method: str, params: dict) -> dict:
        """A request, waited for; whatever else arrives meanwhile is handled."""
        request_id = self.server.request(method, params)
        deadline = time.monotonic() + REQUEST_SECONDS
        while True:
            if self.turn.cancel.is_set() and not self.turn_id:
                raise llm.Cancelled()
            if time.monotonic() > deadline:
                raise llm.LLMError(f"Codex did not answer {method} in {REQUEST_SECONDS} s.")
            try:
                message = self.server.inbox.get(timeout=POLL)
            except queue.Empty:
                continue
            if message is None:
                raise llm.LLMError(f"Codex exited before answering {method}; see {Path(self.server.log.name).name}.")
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    raise llm.LLMError(f"Codex refused {method}: {(message['error'] or {}).get('message')}")
                return message.get("result") or {}
            self.handle(message)

    # -------------------------------------------------------------- what Codex sends

    def handle(self, message: dict) -> None:
        method, params = message.get("method"), message.get("params") or {}
        if not method:
            return  # The answer to a request not waited for (turn/interrupt).
        if "id" in message:
            self.answer(message["id"], method, params)
        elif method == "item/agentMessage/delta":
            reply = self.replies.get(params.get("itemId"))
            if reply is not None:
                reply["text"] += params.get("delta") or ""
                agent._redraw()
        elif method == "item/started":
            self.started(params.get("item") or {})
        elif method == "item/completed":
            self.completed(params.get("item") or {})
        elif method == "thread/tokenUsage/updated":
            self.usage((params.get("tokenUsage") or {}).get("last") or {})
        elif method == "turn/completed":
            done = params.get("turn") or {}
            if done.get("id") in (self.turn_id, None):
                self.status = done.get("status") or "completed"
                self.error = (done.get("error") or {}).get("message") or ""
        elif method == "error":
            error = params.get("error") or {}
            print(f"Loopcut: Codex error{' (retrying)' if params.get('willRetry') else ''}: {error.get('message')}")

    def answer(self, request_id, method: str, params: dict) -> None:
        if method == "item/tool/call":
            self.server.respond(request_id, self.tool_call(params))
        elif method in ("item/commandExecution/requestApproval", "execCommandApproval"):
            raw = params.get("command")
            shown = " ".join(map(str, raw)) if isinstance(raw, list) else str(raw or "")
            ok = self.approve(params.get("itemId") or params.get("callId"), "shell", f"$ {_first_line(shown)}", shown)
            self.server.respond(request_id, {"decision": _decision(method, ok)})
        elif method in ("item/fileChange/requestApproval", "applyPatchApproval"):
            ok = self.approve(params.get("itemId") or params.get("callId"), "edit_file", "Edit files", "")
            self.server.respond(request_id, {"decision": _decision(method, ok)})
        elif method == "mcpServer/elicitation/request":
            self.server.respond(request_id, {"action": "decline", "content": None, "_meta": None})
        elif method == "item/tool/requestUserInput":
            self.server.respond(request_id, {"answers": {}})
        else:
            self.server.refuse(request_id, f"Loopcut does not handle {method}")

    def card(self, key, name: str, summary: str, code: str, status: str = "running") -> dict:
        card = self.cards.get(key)
        if card is None:
            card = state.item_tool(name, summary, code)
            self.items.append(card)
            if key:
                self.cards[key] = card
        card["status"] = status
        agent._redraw()
        return card

    def started(self, item: dict) -> None:
        kind, key = item.get("type"), item.get("id")
        if kind == "agentMessage":
            self.replies[key] = reply = state.item_assistant()
            self.items.append(reply)
        elif kind == "commandExecution":
            shown = _command_text(item)
            if self.cards.get(key, {}).get("status") != "rejected":
                self.card(key, "shell", f"$ {_first_line(shown)}", shown)
        elif kind == "fileChange":
            paths = [Path(c.get("path") or "").name for c in item.get("changes") or ()]
            self.card(key, "edit_file", f"Edit {', '.join(paths) or 'files'}", "")
        elif kind == "imageView":
            path = str(item.get("path") or "")
            card = self.card(key, "view_image", f"View {Path(path).name}", path, "done")
            if path:
                from .files import resolve
                try:
                    conversations.store_image(self.session, resolve(path, base=self.ws), card)
                except (OSError, ValueError):
                    pass  # An unavailable preview must not interrupt the turn.
        elif kind in ("webSearch", "mcpToolCall", "collabAgentToolCall", "imageGeneration"):
            self.card(key, kind, kind, "")

    def completed(self, item: dict) -> None:
        kind, key = item.get("type"), item.get("id")
        if kind == "agentMessage":
            reply = self.replies.pop(key, None) or state.item_assistant()
            reply["text"], reply["streaming"] = item.get("text") or reply["text"], False
            agent.finish_reply(reply, self.ws)
            if not reply["text"].strip():
                if reply in self.items:
                    self.items.remove(reply)
                return
            if reply not in self.items:
                self.items.append(reply)
            self.messages.append({"role": "assistant", "content": reply["text"]})
            conversations.save(self.session)
        elif kind == "contextCompaction":
            self.items.append(state.item_notice("Codex summarized the earlier conversation to make room."))
        elif key in self.cards:
            card = self.cards.pop(key)
            if card["status"] in ("rejected", "done"):  # Declined by the user, or an image Codex looked at.
                return
            ok = item.get("status") in ("completed", None) and item.get("exitCode") in (0, None)
            card["status"] = "done" if ok else "failed"
            if kind == "commandExecution":
                seconds = (item.get("durationMs") or 0) / 1000
                output = (item.get("aggregatedOutput") or "").rstrip()
                card["output"] = f"exit {item.get('exitCode')} in {seconds:.1f} s\n{_clip(output) or '(no output)'}"
                agent._note_level(self.turn, "shell", "{}", len(self.turn.delegations))
            elif kind == "fileChange":
                changes = item.get("changes") or ()
                card["code"] = _clip("\n".join(c.get("diff") or "" for c in changes))
                card["output"] = "\n".join(f"{(c.get('kind') or {}).get('type', 'update')} {c.get('path')}" for c in changes)
                agent._note_level(self.turn, "edit_file", "{}", len(self.turn.delegations))
            agent._redraw()

    def usage(self, last: dict) -> None:
        """One model request's tokens, where agent._request records the HTTP loop's."""
        if not last:
            return
        used = {"input": int(last.get("inputTokens") or 0), "cached": int(last.get("cachedInputTokens") or 0),
                "output": int(last.get("outputTokens") or 0)}
        bucket = self.session.setdefault("usage_by_role", {}).setdefault(
            "single", {"input": 0, "cached": 0, "output": 0, "requests": 0})
        for key, value in used.items():
            self.session["usage"][key] += value
            bucket[key] += value
        bucket["requests"] += 1
        self.session["usage"]["context"] = used["input"]

    # -------------------------------------------------------------- approvals and Loopcut's tools

    def approve(self, key, name: str, summary: str, code: str) -> bool:
        """Codex asks to run a command or change files: Loopcut's approval card, unless auto-run or "Always
        allow" says yes for the user."""
        card = self.card(key, name, summary, code)
        # An install asks every time, as in agent._approval; Codex's command runs in the workspace.
        install = tools.installs_code(code) or workspace.installs("shell", {"command": code}, self.turn.roots)
        if (self.cfg.auto_run or self.session["auto_run"]) and not install:
            return True
        self.turn.decided.clear()
        card["status"] = "awaiting"
        card["install"] = install
        agent._redraw()
        if agent._wait_for_approval(self.turn):
            card["status"] = "running"
            return True
        card["status"] = "rejected"
        return False

    def tool_call(self, params: dict) -> dict:
        """A dynamic tool call: run as agent.run_loop runs a tool (approval, checkpoint, card, the scene's
        before and after), recorded in the conversation, answered with its text and image."""
        name, raw = str(params.get("tool") or ""), params.get("arguments")
        arguments = json.dumps(raw if isinstance(raw, dict) else {})
        call_id = str(params.get("callId") or "")
        if name not in DYNAMIC and not cloud.is_tool(name):
            return _reply(f"Unknown tool {name!r}: Loopcut's tools here are {', '.join(DYNAMIC)}.", False)
        summary, code = agent._describe(llm.ToolCall(id=call_id, name=name, arguments=arguments))
        card = state.item_tool(name, summary, code)
        self.items.append(card)
        self.messages.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}]})
        text, ok, image, file_image = self.run_dynamic(card, name, arguments)
        if card["status"] != "rejected":
            card["status"] = "done" if ok else "failed"
        card["output"] = text
        self.messages.append({"role": "tool", "tool_call_id": call_id, "content": text})
        if image is not None:
            self.messages.append(agent._image_message(conversations.store_image(self.session, image, card),
                                                      agent._image_what(text), file_preview=file_image))
        conversations.save(self.session)
        agent._redraw()
        return _reply(text, ok, image)

    def run_dynamic(self, card: dict, name: str, arguments: str) -> tuple[str, bool, Path | None, bool]:
        approval = agent._approval(name, arguments, self.turn.roots)
        if agent._asks(approval, name, self.session, self.cfg):
            self.turn.decided.clear()
            card["status"] = "awaiting"
            card["heavy"] = approval == "heavy" and not self.session["auto_heavy"]
            card["install"] = approval == "install"
            agent._redraw()
            if not agent._wait_for_approval(self.turn):
                card["status"] = "rejected"
                return "The user rejected this action. Ask what they want instead.", False, None, False
        card["status"] = "running"
        agent._redraw()
        if agent._changes_scene(name, arguments):
            try:
                self.ensure_checkpoint(self.session)
            except checkpoints.CheckpointError as ex:  # No way back means no going forward.
                return (f"This step was NOT run, because a checkpoint could not be saved first: {ex} Tell the user; "
                        f"do not retry."), False, None, False
        from . import cloud_account
        with cloud_account.approval_context(agent._Consent(self.session, self.turn, card, True,
                files_allowed=self.cfg.auto_run or self.session['auto_run'])):
            result = self.run_tool(name, arguments)
        agent._note_level(self.turn, name, arguments, len(self.turn.delegations))
        if getattr(result, "scene_before", None) is not None:
            self.turn.scene_before = self.turn.scene_before or result.scene_before
            self.turn.scene_after = result.scene_after
        for guard in getattr(result, "guard_notes", None) or ():
            self.items.append(state.item_guard(guard["text"], guard["modifiers"]))
        if getattr(result, "receipt", ""):
            card["cost"] = result.receipt  # What the paid call used, in place of its estimate (C47).
        image = getattr(result, "image_path", None)
        return (result.text, result.ok, Path(image) if image and Path(image).is_file() else None,
                getattr(result, "file_image", False))


def _reply(text: str, ok: bool, image: Path | None = None) -> dict:
    items = [{"type": "inputText", "text": text}]
    if image is not None:
        items.append({"type": "inputImage", "imageUrl": data_url(image)})
    return {"success": ok, "contentItems": items}


def _decision(method: str, ok: bool):
    if method in ("execCommandApproval", "applyPatchApproval"):  # The older protocol's words.
        return "approved" if ok else {"denied": {"rejection": "The user rejected this action."}}
    return "accept" if ok else "decline"


def _command_text(item: dict) -> str:
    """What Codex ran, without its shell wrapper (/bin/zsh -c '...')."""
    actions = [a.get("command") for a in item.get("commandActions") or () if a.get("command")]
    return " && ".join(actions) if actions else str(item.get("command") or "")


def _first_line(text: str) -> str:
    lines = str(text or "").strip().splitlines()
    return lines[0][:80] if lines else ""


def _clip(text: str) -> str:
    return text if len(text) <= SHOWN else f"{text[:SHOWN // 2]}\n... [{len(text) - SHOWN} chars] ...\n{text[-SHOWN // 2:]}"


def run_turn(session: dict, turn, cfg: config.Config, run_tool, ensure_checkpoint) -> str:
    """LoopSpec.drive for CODEX_SPEC (agent thread): the turn in the conversation's Codex thread. Returns the
    outcome; raises llm.Cancelled after a Stop and llm.LLMError when Codex fails, as run_loop expects."""
    app = getattr(turn, "codex", None) or config.codex_app()
    if app is None:
        raise config.ConfigError("LOOPCUT_CODEX_APP is off.")
    ws = agent._on_main(lambda: workspace.ensure(session), turn.cancel.is_set)
    home = codex_home()
    log = ws / workspace.OWN / LOG_FOLDER / f"turn-{session.get('turn_number') or 0:03d}-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    try:
        server = Server(command(app, home), ws, environment(ws, home), log)
    except OSError as ex:
        raise llm.LLMError(f"Could not start Codex ({app.binary}): {ex}") from ex
    try:
        return CodexTurn(session, turn, cfg, app, ws, server, run_tool, ensure_checkpoint).run()
    finally:
        server.close()
