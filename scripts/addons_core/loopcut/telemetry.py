"""Anonymous usage stats: launch, setup and turn events on POST /v1/events (contract SPEC §3.7, proposal 014).

The platform cannot see where people stop: it sees downloads, sign-ups and hosted requests, but not a launch, the
first-run setup or a turn on a ChatGPT plan or the user's own key. Three events fill that gap:
- app_start: Blender and add-on versions, OS, processor type and whether this is the first launch; once a session.
- setup_step: the first-run step reached, the option picked, whether it worked and a short code: the error,
  or which action of the step it was (shown, claim, a first-project card), since steps and choices are a
  closed list in the contract.
- turn: which kind of model ran a turn, the model id, how it ended, how long it took and how many tool calls ran.

Every prop comes from an allow-list (enums, booleans, bounded integers, short codes), so no prompt, reply, file or
object name, path or other free text can fit; the server rejects unknown keys too. A model name the user typed
for their own key is sent only when it is a well-known public model family, otherwise as "other".

Gating, as for the anonymous Coming soon clicks, which share the random install id: nothing from background
processes (workers, -b runs: credentials.interest_install_id is None there), nothing while the preference "Share
anonymous usage stats" is off, and nothing while LOOPCUT_TELEMETRY is off (the harness sets that). Signed in, the
Loopcut bearer goes along, like interest events; signed out, the install id alone.

Delivery is fire-and-forget: one short-lived daemon thread, a short timeout, no retries, and every answer but 204
is dropped quietly (a server before 014 answers 422). A 404 (the server's app_telemetry switch is off), a 426, or a
422 on `type` stops sending until the next launch; a 429 pauses it. Client-side caps: app_start once per process,
the same setup step once per process, at most MAX_EVENTS events in any WINDOW seconds, and a bounded queue.
"""

import json
import os
import platform
import queue
import re
import sys
import threading
import time
from collections import deque

EVENTS = ("app_start", "setup_step", "turn")
STEPS = ("welcome", "chatgpt", "loopcut_account", "own_key", "done")
CHOICES = ("chatgpt", "loopcut", "own_key", "skip", None)
PROVIDERS = ("chatgpt", "loopcut", "own_key")
OUTCOMES = ("ok", "error", "cancelled", "empty")
OS_NAMES = ("mac", "windows", "linux")
ARCHES = ("arm64", "x86_64", "other")
PROPS = {  # The exact allow-list per event: (required keys, optional keys).
    "app_start": ({"blender_version", "os", "arch", "first_run"}, set()),
    "setup_step": ({"step", "choice", "ok"}, {"error_code"}),
    "turn": ({"provider", "model", "outcome", "duration_ms", "tool_calls"}, {"error_code"}),
}
MAX_DURATION_MS = 86_400_000
MAX_TOOL_CALLS = 10_000

# \Z, not $: $ also matches before a trailing line break.
_CODE = re.compile(r"[a-z0-9_]{1,64}\Z")
_MODEL = re.compile(r"[a-z0-9._-]{1,32}\Z")
_BLENDER = re.compile(r"[0-9]+(?:\.[0-9]+){0,3}(?:-[0-9A-Za-z]+)?\Z")
_INSTALL_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
_APP_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*)?\Z")
# Public model families a user's own key may name. Anything else is "other": a fine-tune or a local model's
# name is the user's own text.
_PUBLIC_MODEL = re.compile(
    r"(?:gpt|o[0-9]|chatgpt|codex|claude|gemini|gemma|glm|deepseek|qwen|qwq|llama|mistral|mixtral|codestral|"
    r"devstral|magistral|grok|kimi|mimo|muse|minimax|phi|command|sonar|nemotron|ernie|hunyuan|doubao)"
    r"[a-z0-9._-]*\Z")

TIMEOUT = 5.0          # Seconds per send; the thread gives up quietly after that.
WINDOW = 600.0         # Rolling window for the overall cap, seconds.
MAX_EVENTS = 30        # Events in any WINDOW: a runaway caller cannot flood the endpoint.
PAUSE_429 = 600.0      # After a 429, nothing is sent for this long.
QUEUE_SIZE = 20
IDLE_SECONDS = 30.0    # The sender thread ends after this long with nothing to send.
_APP_START_FLAG = "_LOOPCUT_APP_START_SENT"  # Process-wide, so an add-on reload does not send it twice.

_lock = threading.Lock()
_queue: "queue.Queue[tuple]" = queue.Queue(maxsize=QUEUE_SIZE)
_sender: threading.Thread | None = None
_sent_times: deque = deque()
_seen_steps: set = set()
_stopped = False       # 404, 426 or an old server: nothing more until the next launch.
_paused_until = 0.0    # A 429's pause.
_first_run: bool | None = None  # Whether the install record was missing when the add-on registered.


# --- Payloads (pure: unit-tested against the contract schema) ----------------------------------------------

def os_name(value: str | None = None) -> str:
    value = sys.platform if value is None else value
    return "mac" if value == "darwin" else "windows" if value.startswith(("win", "cygwin")) else "linux"


def arch_name(value: str | None = None) -> str:
    value = (platform.machine() if value is None else value).lower()
    return "arm64" if value in ("arm64", "aarch64", "armv8", "arm64e") else \
        "x86_64" if value in ("x86_64", "amd64", "x64") else "other"


def blender_version(version=None, cycle: str = "") -> str:
    """bpy.app.version joined with dots, with -alpha, -beta or -rc for a pre-release build."""
    if version is None:
        import bpy
        version, cycle = bpy.app.version, getattr(bpy.app, "version_cycle", "")
    text = ".".join(str(int(part)) for part in tuple(version)[:3])
    if cycle in ("alpha", "beta", "rc"):
        text += "-" + cycle
    if len(text) > 16 or not _BLENDER.match(text):
        raise ValueError("Unexpected Blender version")
    return text


def error_code(value) -> str | None:
    """A short machine code, or None. Dashes and dots become underscores; anything with other characters
    (a message, not a code) is "other", so no free text can pass as a code."""
    if value is None or value == "":
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return f"http_{value}"
    text = str(value).strip().lower().replace("-", "_").replace(".", "_")
    return text if _CODE.match(text) else "other"


def model_id(provider: str, model: str) -> str:
    """The model id as the server takes it: lowercase [a-z0-9._-], at most 32. Loopcut's own aliases as they
    are; on a ChatGPT plan or the user's own key only a public model family, otherwise "other"."""
    text = str(model or "").strip().lower()
    text = text.rsplit("/", 1)[-1]  # anthropic/claude-opus-5.5 -> claude-opus-5.5
    text = re.sub(r"[^a-z0-9._-]", "-", text)[:32].strip("-") or "other"
    if not _MODEL.match(text):
        return "other"
    if provider != "loopcut" and not _PUBLIC_MODEL.match(text):
        return "other"
    return text


def app_start_props(first_run: bool, version=None, cycle: str = "", os_value: str | None = None,
                    machine: str | None = None) -> dict:
    return {"blender_version": blender_version(version, cycle) if version is not None else blender_version(),
            "os": os_name(os_value), "arch": arch_name(machine), "first_run": bool(first_run)}


def setup_step_props(step: str, choice: str | None, ok: bool, code=None) -> dict:
    if step not in STEPS or choice not in CHOICES:
        raise ValueError("Unknown setup step or choice")
    props = {"step": step, "choice": choice, "ok": bool(ok)}
    code = error_code(code)
    if code:  # A failure's reason, or the action taken on a step that worked.
        props["error_code"] = code
    return props


def turn_props(provider: str, model: str, outcome: str, duration_ms, tool_calls, code=None) -> dict:
    if provider not in PROVIDERS or outcome not in OUTCOMES:
        raise ValueError("Unknown provider or outcome")
    props = {"provider": provider, "model": model_id(provider, model), "outcome": outcome,
             "duration_ms": max(0, min(MAX_DURATION_MS, int(duration_ms))),
             "tool_calls": max(0, min(MAX_TOOL_CALLS, int(tool_calls)))}
    code = error_code(code)
    if code and outcome in ("error", "empty"):
        props["error_code"] = code
    return props


def envelope(event: str, props: dict, install_id: str, app_version: str) -> dict:
    """The whole request body. Raises ValueError for anything the contract would reject, so a bad event is
    dropped here rather than sent."""
    if event not in EVENTS:
        raise ValueError("Unknown event")
    if not isinstance(install_id, str) or not _INSTALL_ID.match(install_id):
        raise ValueError("Invalid install id")
    if not isinstance(app_version, str) or len(app_version) > 32 or not _APP_VERSION.match(app_version):
        raise ValueError("Invalid app version")
    required, optional = PROPS[event]
    if not required <= set(props) <= required | optional:
        raise ValueError("Props outside the allow-list")
    return {"type": "usage", "event": event, "install_id": install_id, "app_version": app_version,
            "props": dict(props)}


# --- Gating ---------------------------------------------------------------------------------------------------

def env_off() -> bool:
    return os.environ.get("LOOPCUT_TELEMETRY", "").strip().lower() in ("0", "off", "false", "no")


def _preferences():
    from . import settings
    return settings.preferences()


def enabled() -> bool:
    """Main thread: the preference, the environment switch, and an interactive Blender."""
    if env_off() or _stopped:
        return False
    try:
        import bpy
        if getattr(bpy.app, "background", True):
            return False
        prefs = _preferences()
    except Exception:  # No preferences (unregistered, a test stand-in): no events.
        return False
    return prefs is not None and bool(getattr(prefs, "share_usage_stats", False))


def _allowed(event: str, props: dict, now: float) -> bool:
    """Client-side caps; call under _lock."""
    if now < _paused_until:
        return False
    if event == "app_start":
        if os.environ.get(_APP_START_FLAG):
            return False
    elif event == "setup_step":
        key = tuple(sorted(props.items(), key=lambda item: item[0]))
        if key in _seen_steps:
            return False
    while _sent_times and now - _sent_times[0] > WINDOW:
        _sent_times.popleft()
    if len(_sent_times) >= MAX_EVENTS:
        return False
    if event == "app_start":
        os.environ[_APP_START_FLAG] = "1"
    elif event == "setup_step":
        _seen_steps.add(tuple(sorted(props.items(), key=lambda item: item[0])))
    _sent_times.append(now)
    return True


# --- Sending ----------------------------------------------------------------------------------------------------

def record(event: str, props_fn) -> None:
    """Queue one event; never blocks or raises. props_fn builds the props on the main thread (it may read
    preferences), so callers on worker threads pass a function. Off the main thread, the work hops to it."""
    try:
        if threading.current_thread() is not threading.main_thread():
            from . import mainthread
            mainthread.run_on_main(lambda: record(event, props_fn))
            return
        if not enabled():
            return
        props = props_fn() if callable(props_fn) else props_fn
        if props is None:
            return
        from . import cloud_http, config, credentials
        install_id = credentials.interest_install_id()  # None in background processes.
        if install_id is None:
            return
        body = envelope(event, props, install_id, config.app_version())
        with _lock:
            if not _allowed(event, props, time.monotonic()):
                return
        key = credentials.api_key(config.SERVICE_BASE_URL)
        signed_in = isinstance(key, str) and key.startswith("lc_")
        headers = cloud_http.contract_headers()
        _queue.put_nowait((body, key if signed_in else None, headers))
        _ensure_sender()
    except queue.Full:
        pass
    except Exception as ex:  # Telemetry never gets in the way of anything.
        print(f"Loopcut: usage event {event} not sent: {type(ex).__name__}")


def _ensure_sender() -> None:
    global _sender
    with _lock:
        if _sender is not None and _sender.is_alive():
            return
        _sender = threading.Thread(target=_send_loop, name="loopcut-telemetry", daemon=True)
        _sender.start()


def _send_loop() -> None:
    global _sender
    while True:
        try:
            item = _queue.get(timeout=IDLE_SECONDS)
        except queue.Empty:
            with _lock:
                if _queue.empty():
                    _sender = None
                    return
            continue
        _send(*item)


def _send(body: dict, key: str | None, headers: dict, transport=None) -> None:
    global _stopped, _paused_until
    from . import cloud_http
    if _stopped:
        return
    try:
        client = cloud_http.Client(api_key=key, client_headers=headers, timeout=TIMEOUT, transport=transport)
        client.request("POST", "/v1/events", body, auth=key is not None)
    except cloud_http.CloudError as ex:
        field = (ex.extra or {}).get("field") if isinstance(ex.extra, dict) else None
        if ex.status in (404, 426) or (ex.status == 422 and field == "type"):
            _stopped = True  # Switched off on the server, too old a client, or a server without usage events.
        elif ex.status == 429:
            _paused_until = time.monotonic() + PAUSE_429
    except Exception:  # Network down, DNS, TLS: dropped, never retried.
        pass


# --- The events -------------------------------------------------------------------------------------------------

def note_first_run() -> None:
    """At register, before anything creates the install record: whether this is the first launch."""
    global _first_run
    if _first_run is not None or env_off():
        return
    try:
        from . import credentials
        _first_run = not (credentials.folder() / "install.json").is_file()
    except Exception:
        _first_run = False


def app_start() -> None:
    record("app_start", lambda: app_start_props(bool(_first_run)))


def setup_step(step: str, choice: str | None, ok: bool = True, code=None) -> None:
    record("setup_step", lambda: setup_step_props(step, choice, ok, code))


# First-use milestones use the existing closed setup_step contract. Persist only event flags, never content.
_FIRST_ACTIONS = ("panel_opened", "prompt_typed", "prompt_sent", "result_ok", "result_error",
                  "result_cancelled", "result_empty")


def first_use(action: str) -> None:
    if action not in _FIRST_ACTIONS:
        raise ValueError("Unknown first-use action")

    def props():
        from . import credentials
        prefs = _preferences()
        path = credentials.folder() / "first-use.json"
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        saved = saved if isinstance(saved, dict) else {}
        result = action.startswith("result_")
        key = "result" if result else action
        if saved.get(key) or (not result and getattr(prefs, "first_message_sent", False)):
            return None
        if result and not saved.get("prompt_sent"):
            return None
        saved[key] = True
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(saved), encoding="utf-8")
            os.replace(temporary, path)
        except OSError as ex:
            print(f"Loopcut: first-use event flag not saved: {type(ex).__name__}")
        return setup_step_props("done", None, action not in ("result_error", "result_cancelled", "result_empty"),
                                "first_" + action)
    record("setup_step", props)


def provider_of(source: str) -> str:
    """cloud.model_source() as the event's provider: a model server on this computer is the user's own."""
    return source if source in ("chatgpt", "loopcut") else "own_key"


def turn_start(cfg) -> dict:
    """Main thread, as a turn starts: what ran it. Never raises."""
    try:
        from . import cloud
        provider = provider_of(cloud.model_source())
        return {"provider": provider, "model": getattr(cfg, "model", "") or ""}
    except Exception:
        return {}


def outcome_of(outcome: str, code: str | None = None) -> tuple[str, str | None]:
    """A turn's agent.Turn.outcome and error code as an event's outcome (OUTCOMES) and code. The conversation
    logs (session_logs.py) use the same values, so a log and its turn event agree."""
    if outcome == "completed":
        return "ok", code
    if outcome == "cancelled":
        return "cancelled", code
    if code == "empty_reply":
        return "empty", code
    return "error", code or (outcome if outcome in ("step_limit", "no_progress", "budget") else "internal")


def turn_end(turn, items: list) -> None:
    """Any thread, once a user's turn has ended (agent._run)."""
    start = getattr(turn, "telemetry", None)
    started = getattr(turn, "started", None)
    if not start or started is None:
        return
    result, code = outcome_of(getattr(turn, "outcome", ""), getattr(turn, "error_code", "") or None)
    first = getattr(turn, "first_item", 0) or 0
    tools = sum(1 for item in items[first:] if item.get("kind") == "tool")
    duration = (time.monotonic() - started) * 1000
    record("turn", turn_props(start["provider"], start["model"], result, duration, tools, code))
    first_use("result_" + result)


def register() -> None:
    note_first_run()
    import bpy
    if not bpy.app.timers.is_registered(app_start):
        # A few seconds after startup, off the startup path; persistent, so loading the startup file keeps it.
        bpy.app.timers.register(app_start, first_interval=3.0, persistent=True)


def unregister() -> None:
    import bpy
    if bpy.app.timers.is_registered(app_start):
        bpy.app.timers.unregister(app_start)
