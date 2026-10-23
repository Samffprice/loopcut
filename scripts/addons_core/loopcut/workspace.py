"""The single agent's workspace (LOOPCUT_WORKSPACE=on): a folder of its own per conversation, where it
works the way a coding agent works in a terminal that has Blender.

Why: on 2026-09-30 GPT-6.1 Sol and GPT-6 Astra, alone in Codex with only a shell, file edits and an
image viewer, did the Arcane chest lookdev in 18 and 15 minutes at a fraction of the cost and better
than Loopcut's orchestrator and workers (83-134 minutes; evals 20260930-224428_sol_raw,
20260930-222301_astra_raw, against 20260930-154037 and -200330). Both built the same workflow on their
own: inspect the scene with a script, export what they needed to JSON, paint small per-part textures
with numpy, and rebuild the whole result from the untouched .blend every pass (blender -b scene.blend
--python build.py), look at the render, fix the scripts, rebuild, verify that nothing else moved. This
module gives Loopcut's single agent that setup; bringing the result into the live scene, which Codex
never had to do, is apply_build.

<data>/workspaces/<conversation id>/, the shell's working directory:
  source.blend   the live scene as the conversation's first workspace turn began; snapshot_scene saves it
                 again (the one before kept as source-<n>.blend). Every datablock carries its own name as
                 its stamp (ownership.STAMP) and each scene its snapshot's number (SNAPSHOT_PROP), so
                 apply_build knows what a build made, changed or left alone, however the build was run.
  <images>       what the user attached, under their own names
  logs/          run_blender's logs, and shell output too long to show whole
  .loopcut/      Loopcut's own (hidden from the file tools): state.json, bin/ (blender and python3 on
                 the shell's PATH), tmp/ (TMPDIR), apply-<n>/ (each apply's prepared result and report)

Tools (SCHEMAS): shell, run_blender, edit_file, view_image, apply_build, snapshot_scene; list_files,
read_file and write_file work here too (files.relative_base: a relative path means the workspace).
shell, run_blender and edit_file ask the user as run_python does (needs_approval), apply_build as any
scene change (tools.CHANGES_SCENE: a checkpoint first). Processes run on the agent thread (ToolResult.then),
so Blender's window stays responsive, with a time limit, and are killed on Stop.

apply_build is merge.merge_wave with the build as its one worker: a background Blender opens the
result and writes what the build created and what the agent names in owns (ownership.write_result,
role "" so nothing is held back; images loaded from the workspace packed first), and the merge brings
that into the live scene: created datablocks come in, owned ones replace their live versions unless
those changed live meanwhile (a conflict), anything else the build changed is a dropped edit, reported.
Rebuilding from the same source and applying again replaces what the last apply brought: what an
earlier apply created is recognised by name (stamped in the prepared copy) and the live side of every
check is measured against what the last apply left (merge.Spawn.live_tokens), the build side against
the snapshot it was built from. Scene settings (render, colour management, EEVEE, Cycles) and
scene.camera / scene.world follow the build where it changed them and they are still as they were live.
A name in owns the result no longer has means the build removed it (workspace_child._removed). A driver
aimed at what the apply removed is re-aimed at the replacement's shape keys when they have the same key
blocks, else the result names it as left without its target (merge._settle).
"""

import json
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from . import op_notes, shape_key_guard
from .tools import MAX_OUTPUT_CHARS, ToolError, ToolResult, _clip

FOLDER = "workspaces"
OWN = ".loopcut"
SOURCE = "source.blend"
LOGS = "logs"
STATE = "state.json"
CHILD_SCRIPT = Path(__file__).with_name("workspace_child.py")
SHAPE_KEY_GUARD = Path(shape_key_guard.__file__).resolve()
OP_NOTES = Path(op_notes.__file__).resolve()
RENDER_PATH_GUARD = Path(__file__).with_name("render_path_guard.py").resolve()
SNAPSHOT_PROP = "lc_snapshot"   # On every scene of a snapshot: its number, so a build says what it came from.
SHELL_TIMEOUT, BLENDER_TIMEOUT, MAX_TIMEOUT = 600, 900, 3600
CHILD_TIMEOUT = 600             # Stamping a snapshot, preparing a result.
KILL_GRACE = 2.0
POLL = 0.1
LOG_TAIL = 40                   # Lines of a run_blender log shown.
WRITTEN_LIMIT = 12              # Files a run lists as written; the rest counted.
LISTED = 30                     # Entries the turn's note names.
DRIVERS_LISTED = 20             # Drivers an apply's result names, of each kind (re-aimed, left without a target).
VIEW_MAX_SIDE = 2048            # Keeps 1920x1080 renders intact for critique; larger textures stay bounded.
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".tga", ".exr", ".hdr")
NOTED = "loopcut_workspace"     # On the message a turn started from: the workspace note is on it.
TOOLS = ("shell", "run_blender", "edit_file", "view_image", "apply_build", "snapshot_scene")
ASKS = {"shell", "run_blender", "edit_file"}   # Wait for the user unless auto-run / "Always allow".
# Log lines run_blender leaves out of the tail it shows: render progress, start-up and quit.
_NOISE = re.compile(r"^Fra:\d+ |^Blender quit$|^Blender \d+\.\d+|\bRead blend: |\bRead prefs: |\| Using OCIO=")
# Scene settings a build may change and apply_build carries over: these structs' plain properties.
SETTINGS = ("render", "render.image_settings", "view_settings", "display_settings", "eevee", "cycles")
SETTINGS_SKIP = {"filepath", "rna_type", "name", "use_file_extension", "preview_pause"}  # preview_pause: the viewport's.
SCENE_SETTINGS = ("frame_start", "frame_end", "frame_step")

# The workspace agent's whole system prompt, in place of agent.SYSTEM_PROMPT: what its environment is,
# as the raw runs' prompt said it, and no workflow. The rules agent.SYSTEM_PROMPT gives a cheaper model and
# a workflow this prompt once prescribed ("small previews", "a studio for previews") cost the look: on the
# same chest, Sol given them judged its paint from 640x360 Cycles previews of a side scene, flat AgX, and
# came out grey (evals 20261001-000435, -092556); Sol given only this kind of note rendered the real scene
# at 1056x594 in EEVEE with a contrast look and came out right (20260930-224428_sol_raw).
PROMPT = """You are Loopcut, an AI agent working on the Blender scene the user has open. You work the way a \
coding agent works in a terminal: a folder of your own, a shell, Blender, an image viewer, and the user's live scene.

Environment:
- Your workspace is a folder for this conversation: the working directory of shell and run_blender, and what \
relative paths in the file tools mean. It holds source.blend (the live scene as this conversation's first request \
found it; keep it as it is), the images the user attached, at full size, and every file you make. The <workspace> \
note on a request lists it. snapshot_scene saves the live scene as source.blend again.
- In shell, blender is this Blender and python3 its Python, with numpy. run_blender runs headless \
(blender -b source.blend --python script.py) and lists files written. No window: save with bpy. There // is the \
workspace; in the live scene it is the .blend's folder.
- Windows shell: cmd.exe. For Python, write_file a .py and run python3 file.py.
- view_image shows you any image: your renders, crops, the references.
- The user's live Blender: run_python runs bpy code in it, get_scene_info reads it, capture_viewport shows it, and \
after a step that changed it you get a capture. inspect_api describes its live API and turns on a bundled add-on \
it names in the live Blender.
- search_assets and import_asset bring models, materials and HDRIs into the live scene from BlenderKit, Poly Haven, \
Poly Pizza, the user's own libraries and Loopcut's index. Results come as a thumbnail sheet with each asset's \
description, size and polycount; an import reports the size it measures and any credit owed. For props, set \
dressing, materials and skies that are not their own work, the user prefers a fitting library asset to one built \
from primitives, and a build when nothing fits or when they ask. What they made or are modelling stays theirs. \
They want assets at real-world size that match the scene's look (a mix that reads well is fine), and to be told \
which library each came from, with any credit passed on.
- apply_build brings a result .blend into the live scene in one undo step: what the build created comes in, what \
you name in owns replaces its live version, and anything else the build changed is reported, not applied. A scene \
the build made comes in as a scene of its own. Applying after a rebuild replaces what the last apply brought. What \
you apply is what the user gets.
- Each user message starts with <scene_context>: the open file, mode, selection and objects; @Name means that \
object, material or collection. A message in <steer> arrived while you worked: fold it in and go on. Earlier turns \
are shown as short records of each step; your files stay in the workspace.
- Installed add-ons, if any, are listed at the end of this prompt.

Leave what the user did not ask you to change as it was; never delete their objects, materials or files. When you \
finish, say briefly what you did and anything the user must decide."""

# The live-scene tools the workspace agent keeps (agent._tool_schemas' names): the rest are the cheaper
# single agent's ways to do what the shell, run_blender and view_image do here.
LIVE_TOOLS = ("run_python", "get_scene_info", "inspect_api", "capture_viewport", "search_assets", "import_asset")

SCHEMAS = [  # Sent with every workspace request: every word here is paid for on every step.
    {"type": "function", "function": {
        "name": "shell",
        "title": "Running a command",
        "description": (
            "Run a workspace command; returns exit code and output (over 8,000 chars clipped, full log in "
            "logs/). No input. Windows: cmd.exe; inline Python refused: write_file a .py, then python3 file.py."),
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"},
            "summary": {"type": "string", "description": "A few words on what this does, shown to the user"},
            "timeout": {"type": "integer", "description": f"Seconds, default {SHELL_TIMEOUT}, at most {MAX_TIMEOUT}"},
        }, "required": ["command", "summary"]},
    }},
    {"type": "function", "function": {
        "name": "run_blender",
        "title": "Updating the model",
        "description": (
            "Run a Python script in a background Blender on a .blend: blender -b <blend> --python <script> -- <args>. "
            "The live scene is untouched. Returns the exit code (1 on an exception), the log's tail (all of it in "
            "logs/) and the files the run wrote, with image sizes."),
        "parameters": {"type": "object", "properties": {
            "script": {"type": "string", "description": "The script file, in the workspace"},
            "blend": {"type": "string", "description": f"Default {SOURCE}; \"\" for Blender's empty scene"},
            "args": {"type": "array", "items": {"type": "string"}, "description": "After --, in sys.argv"},
            "timeout": {"type": "integer", "description": f"Seconds, default {BLENDER_TIMEOUT}, at most {MAX_TIMEOUT}"},
        }, "required": ["script"]},
    }},
    {"type": "function", "function": {
        "name": "edit_file",
        "title": "Editing a file",
        "description": ("Replace exact text in a text file: old_text must occur exactly once, unless replace_all. "
                        "New files: write_file."),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "old_text": {"type": "string"},
            "new_text": {"type": "string"},
            "replace_all": {"type": "boolean"},
        }, "required": ["path", "old_text", "new_text"]},
    }},
    {"type": "function", "function": {
        "name": "view_image",
        "title": "Viewing an image",
        "description": ("See an image file: a render, a texture, a reference. The whole image or "
                        f"region [x0, y0, x1, y1] (fractions from the top-left), up to {VIEW_MAX_SIDE} px."),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "region": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
        }, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "apply_build",
        "title": "Applying the changes",
        "description": (
            "Bring a .blend you built into the live scene. Everything the build created comes in (objects with "
            "their data, materials, images, worlds, text blocks; images from the workspace packed). What you "
            "name in owns replaces its live version; everything else stays as it is live. scene.camera, "
            "scene.world and the render, colour and engine settings follow the build where it changed them. "
            "Returns what was created, replaced, kept and not brought. One undo step."),
        "parameters": {"type": "object", "properties": {
            "blend": {"type": "string", "description": "The result .blend"},
            "owns": {"type": "array", "items": {"type": "string"},
                     "description": "Datablocks that existed in the source and that the build changed or "
                                    "removed (objects, materials, worlds...), by their names in the build"},
        }, "required": ["blend"]},
    }},
    {"type": "function", "function": {
        "name": "snapshot_scene",
        "title": "Copying the scene",
        "description": (f"Save the live scene as {SOURCE} again (the previous one kept as source-<n>.blend), for "
                        "builds that start from what is live now: after the user changed the scene, or to build "
                        "on work already applied."),
        "parameters": {"type": "object", "properties": {}},
    }},
]

_ROOTS: set = set()     # Every workspace this Blender has used: files.check lets the model in there.
_BLENDER: list = []     # This Blender's binary, read on the main thread (ensure) for the agent thread.
_UIDS: dict = {}        # Conversation id -> {key: live session_uid}: identity across renames this session.
_LIVE_PROCESSES: dict = {}
_PROCESS_LOCK = threading.Lock()


# ------------------------------------------------------------------ where things are

def folder(conversation_id: str) -> Path:
    from . import checkpoints
    return Path(checkpoints.data_root()) / FOLDER / conversation_id


def current() -> Path | None:
    """The current conversation's workspace while it runs in workspace mode, else None. Main thread."""
    from . import state
    session = state.session()
    if session.get("mode") != "workspace":
        return None
    try:
        return folder(session["id"]).resolve()
    except Exception:  # No data folder (tests without Blender): no workspace.
        return None


def roots() -> tuple:
    return tuple(_ROOTS)


def inside(path: Path) -> Path | None:
    """The workspace this path is in, or None."""
    return next((root for root in _ROOTS if path == root or root in path.parents), None)


def _own(ws: Path) -> Path:
    return ws / OWN


def _binary() -> str:
    if not _BLENDER:
        import bpy
        _BLENDER.append(bpy.app.binary_path)
    return _BLENDER[0]


def _python() -> str:
    """Blender's own Python: it has numpy, and every Loopcut user has it."""
    return sys.executable


def shell_env(ws: Path, environ=None) -> dict:
    """The parent's environment without keys, tokens or Loopcut's settings; the workspace's bin/ first on
    PATH, its own TMPDIR, and BLENDER naming this Blender."""
    from . import worker_ipc
    environ = os.environ if environ is None else environ
    env = {k: v for k, v in environ.items() if not worker_ipc.is_secret(k) and not k.startswith("LOOPCUT_")
           and k not in ("PYTHONHOME", "PYTHONPATH")}
    env["PATH"] = os.pathsep.join([str(_own(ws) / "bin"), env.get("PATH") or "/usr/bin:/bin"])
    env["TMPDIR"] = str(_own(ws) / "tmp")
    env["BLENDER"] = _binary()
    return env


def _shims(ws: Path) -> None:
    """blender and python3 (and python) on the shell's PATH: this Blender, and its Python."""
    bin_dir = _own(ws) / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    binary, python = _binary(), _python()
    if os.name == "nt":
        shims = {"blender.cmd": f'@"{binary}" --factory-startup --python-exit-code 1 --python "{SHAPE_KEY_GUARD}" --python "{OP_NOTES}" --python "{RENDER_PATH_GUARD}" %*\r\n',
                 "python3.cmd": f'@"{python}" %*\r\n', "python.cmd": f'@"{python}" %*\r\n'}
    else:
        shims = {"blender": f'#!/bin/sh\nexec "{binary}" --factory-startup --python-exit-code 1 --python "{SHAPE_KEY_GUARD}" --python "{OP_NOTES}" --python "{RENDER_PATH_GUARD}" "$@"\n',
                 "python3": f'#!/bin/sh\nexec "{python}" "$@"\n', "python": f'#!/bin/sh\nexec "{python}" "$@"\n'}
    for name, text in shims.items():
        path = bin_dir / name
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
            path.chmod(0o755)


def _reference_name(reference: dict, taken: set, stored: Path) -> str:
    """The attached image's own file name; a stored copy is named after it, with the suffix of what it
    holds (PNG, or JPEG for a clip's sheet, whose name is "take.mp4 2.00-4.27s")."""
    from . import conversations
    name = Path(str(reference.get("name") or "attached.png")).name or "attached.png"
    source = Path(str(reference.get("source") or ""))
    if not source.is_file():
        stem = Path(name).stem if Path(name).suffix.lower() in IMAGE_EXTENSIONS else name
        with stored.open("rb") as handle:
            name = stem + (".jpg" if conversations.media_type(handle.read(12)) == "image/jpeg" else ".png")
    stem, suffix, n = Path(name).stem, Path(name).suffix, 2
    while name in taken:
        name, n = f"{stem}-{n}{suffix}", n + 1
    return name


def _copy_references(session: dict, ws: Path) -> list[str]:
    """The user's attached images into the workspace, once each: the original file when it is still
    there, else the full-size copy the conversation keeps. Returns the names copied now."""
    from . import conversations
    copied, done = [], session.setdefault("workspace_refs", {})
    taken = set(done.values())
    for reference in session.get("references") or ():
        key = reference.get("full") or reference.get("ref")
        if not key or key in done:
            continue
        name = reference.get("name")
        source = Path(str(reference.get("source") or ""))
        try:
            origin = source if source.is_file() else conversations.image_path(session["id"], key)
            name = _reference_name(reference, taken, origin)
            shutil.copy2(origin, ws / name)
        except Exception as ex:  # A missing image is said in the note, never a failed turn.
            print(f"Loopcut: could not copy {name} into the workspace: {ex!r}")
            continue
        done[key] = name
        taken.add(name)
        copied.append(name)
    return copied


def ensure(session: dict) -> Path:
    """Main thread: the conversation's workspace, made if missing, with its shims and the attached images."""
    ws = folder(session["id"]).resolve()
    for path in (ws / LOGS, _own(ws) / "tmp"):
        path.mkdir(parents=True, exist_ok=True)
    _ROOTS.add(ws)
    _shims(ws)
    _copy_references(session, ws)
    return ws


# ------------------------------------------------------------------ state

def _state_path(ws: Path) -> Path:
    return _own(ws) / STATE


def load_state(ws: Path) -> dict:
    try:
        data = json.loads(_state_path(ws).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data.setdefault("snapshots", [])
    data.setdefault("applies", [])
    data.setdefault("runs", 0)
    # The live side after the applies: key -> [token, snapshots taken by then], and pointers and
    # settings with the same count. A build from a later snapshot already has them in its source.
    data.setdefault("live", {"tokens": {}, "names": {}, "pointers": None, "settings": None})
    return data


def save_state(ws: Path, data: dict) -> None:
    path = _state_path(ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(temporary, path)


def _next_run(ws: Path) -> int:
    data = load_state(ws)
    data["runs"] += 1
    save_state(ws, data)
    return data["runs"]


def snapshot_for(data: dict, number) -> dict | None:
    """The snapshot a build came from (its scenes' SNAPSHOT_PROP), else the newest."""
    snapshots = data["snapshots"]
    found = next((s for s in snapshots if s["number"] == number), None) if number is not None else None
    return found or (snapshots[-1] if snapshots else None)


def live_side(data: dict, snapshot: dict | None) -> tuple[dict, dict | None, dict | None]:
    """What the live scene should be now for a build from this snapshot: (key -> token for what applies
    changed since the snapshot was taken, pointers, settings), each None where nothing was applied since."""
    count = snapshot["number"] if snapshot else 0
    live = data["live"]
    tokens = {key: value[0] for key, value in live["tokens"].items() if value[1] >= count}
    pointers = live["pointers"]["value"] if live["pointers"] and live["pointers"]["snapshots"] >= count else None
    settings = live["settings"]["value"] if live["settings"] and live["settings"]["snapshots"] >= count else None
    return tokens, pointers, settings


def recreated_keys(data: dict, snapshot: dict | None) -> list[str]:
    """What earlier applies created that this build's source does not have: made again by the build, it
    replaces the live one instead of arriving beside it as Name.001."""
    known = set((snapshot or {}).get("tokens") or ())
    tokens, _, _ = live_side(data, snapshot)
    return sorted(key for key in tokens if key not in known)


# ------------------------------------------------------------------ scene settings (bpy)

def _plain(value):
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    try:
        return [_plain(v) for v in value]
    except TypeError:
        return None


def _struct(scene, path: str):
    target = scene
    for part in path.split("."):
        target = getattr(target, part, None)
        if target is None:
            return None
    return target


def scene_settings(scene) -> dict:
    """"view_settings.look" -> value for the plain properties of SETTINGS, and SCENE_SETTINGS."""
    found = {f"scene.{name}": _plain(getattr(scene, name)) for name in SCENE_SETTINGS if hasattr(scene, name)}
    for path in SETTINGS:
        struct_ = _struct(scene, path)
        if struct_ is None or not hasattr(struct_, "bl_rna"):
            continue
        for prop in struct_.bl_rna.properties:
            if (prop.identifier in SETTINGS_SKIP or prop.is_readonly or "path" in prop.identifier
                    or prop.type not in ("BOOLEAN", "INT", "FLOAT", "ENUM", "STRING")):
                continue
            try:
                found[f"{path}.{prop.identifier}"] = _plain(getattr(struct_, prop.identifier))
            except Exception:  # Some properties refuse reading outside their engine.
                continue
    return found


def settings_changes(built: dict, source: dict, live: dict, expected: dict) -> tuple[dict, dict]:
    """(path -> value to set live, path -> (live value, built value) kept live): what the build changed
    from its source, set where the live value is still what it should be (expected), else a conflict."""
    wanted, kept = {}, {}
    for path, value in built.items():
        if path not in source or value == source[path] or path not in live:
            continue
        if live[path] == value:
            continue
        if live[path] == expected.get(path, source[path]):
            wanted[path] = value
        else:
            kept[path] = (live[path], value)
    return wanted, kept


def set_settings(scene, wanted: dict) -> list[str]:
    """Set them; returns what could not be set."""
    failed = []
    for path, value in wanted.items():
        owner_path, _, name = path.rpartition(".")
        owner = scene if owner_path == "scene" else _struct(scene, owner_path)
        try:
            current = getattr(owner, name)
            setattr(owner, name, set(value) if isinstance(current, (set, frozenset)) else value)
        except Exception as ex:  # noqa: BLE001 - say which, set the rest.
            failed.append(f"{path} ({type(ex).__name__}: {ex})"[:160])
    return failed


# ------------------------------------------------------------------ processes (agent thread)

def live_rows() -> list[dict]:
    """Snapshot of running children for the panel; no mutable registry entries escape."""
    with _PROCESS_LOCK:
        now = time.monotonic()
        return [{**row, "elapsed": max(0.0, now - row["started"])} for row in _LIVE_PROCESSES.values()]


def _kill(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, AttributeError):
        process.terminate()
    try:
        process.wait(timeout=KILL_GRACE)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, AttributeError):
            process.kill()
        process.wait()


def run_process(command: list | str, cwd: Path, env: dict, timeout: float, log: Path, is_cancelled,
                label: str = "") -> tuple:
    """Run to the end, the time limit or a Stop; stdout and stderr into log. Returns (exit code or None,
    seconds, "timeout" | ""). A Stop kills it and raises llm.Cancelled."""
    from . import llm
    log.parent.mkdir(parents=True, exist_ok=True)
    id = uuid.uuid4().hex
    label = (label.strip() or ("cmd.exe" if isinstance(command, str) else Path(command[0]).name)).splitlines()[0][:80]
    started = time.monotonic()
    stopped = ""
    with log.open("wb") as out:
        process = subprocess.Popen(command, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=out,
                                   stderr=subprocess.STDOUT, start_new_session=os.name != "nt")
        try:
            with _PROCESS_LOCK:
                _LIVE_PROCESSES[id] = {"id": id, "label": label, "started": started, "log": str(log)}
            while True:
                try:
                    process.wait(timeout=POLL)
                    break
                except subprocess.TimeoutExpired:
                    if is_cancelled():
                        raise llm.Cancelled()
                    if time.monotonic() - started > timeout:
                        stopped = "timeout"
                        break
        finally:
            try:
                if process.poll() is None:
                    _kill(process)
            finally:
                with _PROCESS_LOCK:
                    _LIVE_PROCESSES.pop(id, None)
    return None if stopped else process.returncode, time.monotonic() - started, stopped


def _shell_command(command: str) -> list | str:
    if os.name == "nt":
        # Popen serializes a list with MS C-runtime rules (\" becomes \\\"), which cmd does not use.
        # /s removes only our outer quotes; the command's own quotes reach cmd unchanged. /d skips AutoRun.
        shell = subprocess.list2cmdline([os.environ.get("COMSPEC") or "cmd.exe"])
        return f'{shell} /d /s /c "{command}"'
    for shell in ("/bin/bash", "/bin/sh"):
        if os.path.exists(shell):
            return [shell, "-c", command]
    return ["sh", "-c", command]


# Detection only, not a shell parser or sandbox: never decode or rewrite generated Python as shell text.
_SHELL_WORD = re.compile(r'''"[^"\r\n]*"|'[^'\r\n]*'|[^\s&|;()]+|[&|;()\r\n]''')
_PYTHON_NAME = re.compile(r"(?:python(?:w|[23](?:\.\d+)*)?|py)(?:\.(?:exe|cmd|bat))?", re.I)


def _inline_python(command: str, depth: int = 0) -> bool:
    import ntpath
    words = _SHELL_WORD.findall(command)
    for index, word in enumerate(words):
        name = ntpath.basename(word.strip("\"'"))
        if name.lower() in ("cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe") and depth < 4:
            for offset, arg in enumerate(words[index + 1:], index + 1):
                if arg.lower() in ("/c", "-command", "-c") and offset + 1 < len(words):
                    if _inline_python(words[offset + 1].strip("\"'"), depth + 1):
                        return True
                    break
        python = bool(_PYTHON_NAME.fullmatch(name))
        blender = name.lower() in ("blender", "blender.exe", "blender.cmd")
        if not python and not blender:
            continue
        takes_value = False
        for arg in words[index + 1:]:
            arg = arg.strip("\"'")
            if arg in ("&", "|", ";", "(", ")", "\r", "\n"):
                break
            if takes_value:
                takes_value = False
                continue
            if python:
                if re.match(r"-[bBdEhiIOPqRsSuvVx]*c", arg):
                    return True
                if not arg.startswith("-") or arg in ("-m", "--"):
                    break  # A file/module's arguments are not interpreter options.
                takes_value = arg in ("-W", "-X", "--check-hash-based-pycs")
            elif arg == "--python-expr" or arg.startswith("--python-expr="):
                return True
    return False


INLINE_PYTHON_HELP = ("Write the code to a .py file with write_file, then run python3 file.py here; for bpy use "
                      'run_blender(script="script.py", blend="") or run_python for the live scene.')


def _exit_text(code, seconds: float, stopped: str, timeout: float) -> str:
    if stopped == "timeout":
        return f"stopped after the {timeout:.0f} s time limit (killed)"
    if code is not None and code < 0:
        return f"killed by signal {-code} after {seconds:.1f} s"
    return f"exit {code} in {seconds:.1f} s"


def _timeout(value, default: int) -> float:
    try:
        seconds = float(value if value is not None else default)
    except (TypeError, ValueError) as ex:
        raise ToolError("timeout must be a number of seconds") from ex
    if not 1 <= seconds <= MAX_TIMEOUT:
        raise ToolError(f"timeout must be 1-{MAX_TIMEOUT} seconds")
    return seconds


def _read(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return ""


def image_size(path: Path) -> tuple[int, int] | None:
    """Width and height of a PNG or JPEG from its header, without loading it."""
    try:
        with path.open("rb") as handle:
            head = handle.read(26)
            if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
                return struct.unpack(">II", head[16:24])
            if head[:2] != b"\xff\xd8":
                return None
            handle.seek(2)
            while True:
                marker = handle.read(2)
                if len(marker) < 2 or marker[0] != 0xFF:
                    return None
                if marker[1] in (0xD8, 0x01) or 0xD0 <= marker[1] <= 0xD7:
                    continue
                length = struct.unpack(">H", handle.read(2))[0]
                if 0xC0 <= marker[1] <= 0xCF and marker[1] not in (0xC4, 0xC8, 0xCC):
                    height, width = struct.unpack(">xHH", handle.read(5))
                    return width, height
                handle.seek(length - 2, 1)
    except (OSError, struct.error):
        return None


def files_state(ws: Path) -> dict:
    """relative path -> (mtime_ns, size) of every file in the workspace but Loopcut's own and the logs."""
    found = {}
    for root, dirs, names in os.walk(ws):
        here = Path(root)
        if here == ws:
            dirs[:] = [d for d in dirs if d not in (OWN, LOGS)]
        for name in names:
            path = here / name
            try:
                stat = path.stat()
            except OSError:
                continue
            found[str(path.relative_to(ws))] = (stat.st_mtime_ns, stat.st_size)
    return found


def _human(size: int) -> str:
    for unit in ("B", "KB", "MB"):
        if size < 1000:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1000
    return f"{size:.1f} GB"


def written(ws: Path, before: dict, after: dict) -> str:
    """"Wrote: result.blend (586 KB), preview.png (960x540), ... and 38 more in maps/" or ""."""
    changed = sorted((path for path, stat in after.items() if before.get(path) != stat),
                     key=lambda path: (len(Path(path).parts), path))  # The workspace's own files first.
    if not changed:
        return ""
    shown = []
    for path in changed[:WRITTEN_LIMIT]:
        size = image_size(ws / path) if path.lower().endswith((".png", ".jpg", ".jpeg")) else None
        shown.append(f"{path} ({size[0]}x{size[1]})" if size else f"{path} ({_human(after[path][1])})")
    rest = changed[WRITTEN_LIMIT:]
    more = ""
    if rest:
        folders = sorted({str(Path(p).parent) for p in rest if str(Path(p).parent) != "."})
        more = f", and {len(rest)} more" + (f" in {', '.join(f + '/' for f in folders[:4])}" if folders else "")
    return "Wrote: " + ", ".join(shown) + more


def log_tail(text: str, lines: int = LOG_TAIL) -> tuple[str, int]:
    """The last lines of a Blender log without its render progress and start-up lines; and how many of
    those were left out of the part shown."""
    rows = text.splitlines()
    kept = [row for row in rows if not _NOISE.search(row)]
    tail = kept[-lines:]
    return "\n".join(tail), len(rows) - len(kept)


# ------------------------------------------------------------------ snapshots

def _save_snapshot(session: dict, ws: Path) -> dict:
    """Main thread: the live scene into source.blend (a compressed copy, relative paths remapped, what
    nothing uses kept, as merge.save_spawn does) and its record: tokens, pointers and settings. The
    previous source.blend is kept as source-<n>.blend. The stamps come after, in a background Blender."""
    import bpy
    from . import merge, ownership
    data = load_state(ws)
    number = len(data["snapshots"]) + 1
    path = ws / SOURCE
    if path.exists() and data["snapshots"]:
        previous = data["snapshots"][-1]
        kept = ws / f"source-{previous['number']}.blend"
        os.replace(path, kept)
        previous["file"] = kept.name
    unused = merge._unused()
    for idb in unused:
        idb.use_fake_user = True
    try:
        bpy.ops.wm.save_as_mainfile(filepath=str(path), copy=True, compress=True, relative_remap=True)
    finally:
        for idb in unused:
            idb.use_fake_user = False
    ids, tokens = ownership.spawn_state()
    scene = bpy.context.scene
    entry = {"number": number, "file": SOURCE, "taken": round(time.time(), 3), "scene": scene.name, "tokens": tokens,
             "pointers": {attr: ownership.key_of(getattr(scene, attr)) if getattr(scene, attr) is not None else None
                          for attr in ("camera", "world")},
             "settings": scene_settings(scene), "stamped": False}
    data["snapshots"].append(entry)
    save_state(ws, data)
    _UIDS[session["id"]] = dict(ids)
    return entry


def _child(ws: Path, op: str, blend: Path, payload: dict, out_dir: Path, is_cancelled) -> dict:
    """workspace_child.py in a background Blender on blend: payload in, its reply out (out_dir/<op>.json)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    request, reply, log = out_dir / f"{op}.request.json", out_dir / f"{op}.json", out_dir / f"{op}.log"
    request.write_text(json.dumps(payload), encoding="utf-8")
    reply.unlink(missing_ok=True)
    command = [shell_env(ws)["BLENDER"], "-b", "--factory-startup", "--disable-autoexec", str(blend),
               "--python-exit-code", "1", "--python", str(CHILD_SCRIPT), "--", op, str(request), str(reply)]
    code, seconds, stopped = run_process(command, _own(ws), shell_env(ws), CHILD_TIMEOUT, log, is_cancelled)
    if code != 0 or not reply.is_file():
        tail, _ = log_tail(_read(log), 12)
        raise ToolError(f"Loopcut's background Blender could not {op} {blend.name} "
                        f"({_exit_text(code, seconds, stopped, CHILD_TIMEOUT)}):\n{tail}")
    return json.loads(reply.read_text(encoding="utf-8"))


def stamp(ws: Path, entry: dict, is_cancelled) -> None:
    """Agent thread: every datablock of the new snapshot stamped with its name, its scenes numbered."""
    path = ws / entry["file"]
    _child(ws, "stamp", path, {"number": entry["number"]}, _own(ws) / f"snapshot-{entry['number']}", is_cancelled)
    data = load_state(ws)
    for snapshot in data["snapshots"]:
        if snapshot["number"] == entry["number"]:
            snapshot["stamped"] = True
    save_state(ws, data)


def snapshot_scene() -> ToolResult:
    from . import state
    session = state.session()
    ws = ensure(session)
    try:
        entry = _save_snapshot(session, ws)
    except RuntimeError as ex:
        raise ToolError(f"Could not save the scene into the workspace: {ex}") from ex

    def then(is_cancelled, on_main):
        stamp(ws, entry, is_cancelled)
        older = f" The previous one is kept as source-{entry['number'] - 1}.blend." if entry["number"] > 1 else ""
        return ToolResult(f"Saved the live scene as {SOURCE} (snapshot {entry['number']}).{older} Builds from it "
                          f"apply as before; earlier applies stay applied.")
    return ToolResult("", then=then)


# ------------------------------------------------------------------ the turn's note (agent thread)

def _listing(ws: Path) -> str:
    entries = sorted(p for p in ws.iterdir() if not p.name.startswith("."))
    names = [p.name + ("/" if p.is_dir() else "") for p in entries]
    shown = ", ".join(names[:LISTED])
    return shown + (f", and {len(names) - LISTED} more" if len(names) > LISTED else "")


def note_text(ws: Path, data: dict, attached: list[str], problems: list[str]) -> str:
    snapshot = data["snapshots"][-1] if data["snapshots"] else None
    when = (time.strftime("%H:%M", time.localtime(snapshot["taken"])) if snapshot else "")
    source = (f"{SOURCE} is snapshot {snapshot['number']}, the live scene at {when}"
              if snapshot else f"{SOURCE} could not be saved")
    lines = [f"<workspace path=\"{ws}\">", f"{source}. Files: {_listing(ws) or 'none'}."]
    if attached:
        lines.append(f"Attached with this message: {', '.join(attached)}.")
    if data["applies"]:
        last = data["applies"][-1]
        lines.append(f"Applied {len(data['applies'])} time(s); the last: {last['result']}.")
    lines += problems
    return "\n".join(lines + ["</workspace>"])


def start_turn(session: dict, is_cancelled, on_main, craft=None) -> None:
    """LoopSpec.on_turn_start for the workspace loop: the workspace made (a snapshot on its first turn,
    stamped here on the agent thread), the images attached copied in, and a note on the message that
    started the turn: where the workspace is and what is in it, and the craft notes its words call for
    (craft: text -> notes). Once per message: a resumed turn finds it noted."""
    from . import context, router
    messages = session["messages"]
    starts = [i for i, m in enumerate(messages) if context.is_turn_start(m)]
    if not starts or messages[starts[-1]].get(NOTED):
        return
    message = messages[starts[-1]]
    problems = []

    def prepare():
        ws = ensure(session)
        before = set(session.get("workspace_refs", {}).values())
        data = load_state(ws)
        entry = None
        if not data["snapshots"]:
            try:
                entry = _save_snapshot(session, ws)
            except Exception as ex:  # noqa: BLE001 - the workspace still works; apply has no baseline.
                problems.append(f"The live scene could not be saved as {SOURCE}: {ex}")
        attached = [n for n in session.get("workspace_refs", {}).values() if n not in before]
        return ws, entry, attached
    ws, entry, attached = on_main(prepare)
    if entry is not None or any(not s.get("stamped") for s in load_state(ws)["snapshots"]):
        for snapshot in [s for s in load_state(ws)["snapshots"] if not s.get("stamped")]:
            try:
                stamp(ws, snapshot, is_cancelled)
            except ToolError as ex:
                problems.append(f"{SOURCE} could not be prepared for apply_build: {ex}")
    text = note_text(ws, load_state(ws), attached, problems)
    notes = craft(router.user_request(message)) if craft else ""
    if notes:
        text += f"\n\n<craft_notes>\n{notes}\n</craft_notes>"
    router._add_note(message, text)
    message[NOTED] = True


# ------------------------------------------------------------------ the tools

def _workspace() -> tuple[dict, Path]:
    from . import state
    session = state.session()
    return session, ensure(session)


def _path(raw: str, must_exist: bool = True) -> Path:
    from . import files
    target = files.check(files.resolve(raw))
    if must_exist and not target.exists():
        raise ToolError(f"{target} does not exist.")
    return target


def shell(command: str, timeout: int | None = None, summary: str = "") -> ToolResult:
    if not isinstance(command, str) or not command.strip():
        raise ToolError("command must not be empty")
    seconds = _timeout(timeout, SHELL_TIMEOUT)
    if os.name == "nt" and _inline_python(command):
        return ToolResult("Not run: inline Python in Windows shell commands can change the code. "
                          + INLINE_PYTHON_HELP, ok=False)
    _, ws = _workspace()
    env = shell_env(ws)

    def then(is_cancelled, on_main):
        number = _next_run(ws)
        log = _own(ws) / "tmp" / f"shell-{number:04d}.txt"
        code, took, stopped = run_process(_shell_command(command), ws, env, seconds, log, is_cancelled,
                                         label=command.strip().splitlines()[0][:80])
        output = _read(log)
        head = _exit_text(code, took, stopped, seconds)
        if len(output) > MAX_OUTPUT_CHARS:
            kept = ws / LOGS / f"shell-{number:04d}.txt"
            os.replace(log, kept)
            head += f"; the whole output ({_human(len(output.encode('utf-8')))}) is in {LOGS}/{kept.name}"
        else:
            log.unlink(missing_ok=True)
        return ToolResult(f"{head}\n{_clip(output.rstrip()) or '(no output)'}", ok=code == 0 and not stopped)
    return ToolResult("", then=then)


def run_blender(script: str, blend: str = SOURCE, args: list | None = None, timeout: int | None = None) -> ToolResult:
    seconds = _timeout(timeout, BLENDER_TIMEOUT)
    _, ws = _workspace()
    script_path = _path(script)
    if not script_path.is_file():
        raise ToolError(f"{script_path} is not a file.")
    blend_path = _path(blend) if str(blend or "").strip() else None
    if blend_path is not None and blend_path.suffix.lower() != ".blend":
        raise ToolError(f"{blend_path.name} is not a .blend file.")
    if args is not None and not isinstance(args, (list, tuple)):
        raise ToolError("args must be a list of strings")
    extra = [str(a) for a in args or ()]
    env = shell_env(ws)

    def then(is_cancelled, on_main):
        number = _next_run(ws)
        log = ws / LOGS / f"{number:04d}-{script_path.stem}.log"
        command = [env["BLENDER"], "-b", "--factory-startup", *([str(blend_path)] if blend_path else []),
                   "--python-exit-code", "1", "--python", str(SHAPE_KEY_GUARD),
                   "--python", str(OP_NOTES), "--python", str(RENDER_PATH_GUARD),
                   "--python", str(script_path), "--", *extra]
        before = files_state(ws)
        code, took, stopped = run_process(command, ws, env, seconds, log, is_cancelled, label=script_path.name)
        text = _read(log)
        tail, noise = log_tail(text)
        on = blend_path.name if blend_path else "an empty scene"
        lines = [f"{script_path.name} on {on}: {_exit_text(code, took, stopped, seconds)}. "
                 f"Log: {LOGS}/{log.name} ({len(text.splitlines())} lines"
                 + (f", {noise} start-up and render progress lines not shown" if noise else "") + ")."]
        wrote = written(ws, before, files_state(ws))
        if wrote:
            lines.append(wrote)
        shown = _clip(tail)
        shown_lines = set(shown.splitlines())
        lines.extend(line for line in text.splitlines()
                     if line.startswith((shape_key_guard.NOTICE_PREFIX, op_notes.NOTICE_PREFIX))
                     and line not in shown_lines)
        lines.append(f"--- log tail ---\n{shown or '(empty)'}")
        return ToolResult("\n".join(lines), ok=code == 0 and not stopped)
    return ToolResult("", then=then)


def edit_file(path: str, old_text: str, new_text: str, replace_all: bool = False) -> ToolResult:
    target = _path(path)
    if not target.is_file():
        raise ToolError(f"{target} is not a file.")
    if not old_text:
        raise ToolError("old_text must not be empty; to write a whole file use write_file with overwrite=true.")
    try:
        text = target.read_bytes().decode("utf-8")
    except UnicodeDecodeError as ex:
        raise ToolError(f"{target.name} is not a UTF-8 text file.") from ex
    except OSError as ex:
        raise ToolError(f"Cannot read {target}: {ex.strerror or ex}") from ex
    count = text.count(old_text)
    if count == 0:
        raise ToolError(f"old_text is not in {target.name}: copy it exactly (whitespace and indentation count); "
                        f"read_file shows the file.")
    starts, at = [], text.find(old_text)
    while at >= 0:
        starts.append(text.count("\n", 0, at) + 1)
        at = text.find(old_text, at + len(old_text))
    if count > 1 and not replace_all:
        raise ToolError(f"old_text occurs {count} times in {target.name} (lines {', '.join(map(str, starts[:10]))}); "
                        f"give more of the surrounding text, or replace_all=true.")
    updated = text.replace(old_text, new_text) if replace_all else text.replace(old_text, new_text, 1)
    temporary = target.with_name(f".{target.name}.loopcut-edit")
    try:
        temporary.write_bytes(updated.encode("utf-8"))
        os.replace(temporary, target)
    except OSError as ex:
        temporary.unlink(missing_ok=True)
        raise ToolError(f"Cannot write {target}: {ex.strerror or ex}") from ex
    span = new_text.count("\n")
    where = f"line {starts[0]}" if count == 1 else f"lines {', '.join(map(str, starts[:10]))}"
    return ToolResult(f"Edited {target.name}: {count if replace_all else 1} replacement(s) at {where}"
                      + (f" (now {span + 1} lines there)" if span else "") + f"; {len(updated.splitlines())} lines in all.")


def view_image(path: str, region: list | None = None) -> ToolResult:
    from . import scratch, tools
    target = _path(path)
    if not target.is_file() or target.suffix.lower() not in IMAGE_EXTENSIONS:
        raise ToolError(f"{target.name} is not an image file ({', '.join(e[1:] for e in IMAGE_EXTENSIONS)}).")
    try:
        pixels = tools._pixels(target)
    except RuntimeError as ex:
        raise ToolError(f"Blender cannot read {target.name}: {ex}") from ex
    height, width = pixels.shape[:2]
    pixels, what = tools.crop(pixels, region)
    out = scratch.folder() / "view.png"
    out.parent.mkdir(exist_ok=True)
    shown = tools._save_pixels(pixels, out, VIEW_MAX_SIDE)
    return ToolResult(f"Image attached: {target.name}, {what}, {shown[0]}x{shown[1]} px "
                      f"(the file is {width}x{height}).", image_path=out, file_image=True)


# ------------------------------------------------------------------ apply_build

def _build_key_names(report) -> dict:
    """Live names of created datablocks Blender renamed on arrival (build name -> live name), by type."""
    renamed = {}
    for line in report.renamed:
        found = re.match(r"^[^:]+: (\w+) (.+) -> (.+)$", line)
        if found:
            renamed[(found.group(1), found.group(2))] = found.group(3)
    return renamed


def _live_ids(session_id: str, keys, names: dict) -> dict:
    """key -> live session_uid: by this session's own record when the datablock is still there (a rename
    keeps it), else by the name it has live now (names) or its key's name."""
    from . import ownership
    by_uid = {idb.session_uid: idb for idb in ownership.all_ids()}
    remembered = _UIDS.get(session_id) or {}
    ids = {}
    for key in keys:
        uid = remembered.get(key)
        if uid in by_uid and ownership.key_of(by_uid[uid]).split(":", 1)[0] == key.split(":", 1)[0]:
            ids[key] = uid
            continue
        kind, _, name = key.partition(":")
        idb = ownership.find(f"{kind}:{names.get(key, name)}")
        if idb is not None:
            ids[key] = idb.session_uid
    return ids


def _summary_lines(report, settings_set: dict, settings_kept: dict, failed: list, packed: list,
                   built: set = frozenset()) -> list[str]:
    """What the apply did, from the merge's report: created by kind, replaced, the scene pointers and
    settings, and everything that did not come in, with why."""
    def listed(names: list) -> str:
        return ", ".join(names[:8]) + (f" (+{len(names) - 8})" if len(names) > 8 else "")
    objects = [f"{c['name']} ({c['type'].lower()})" for c in report.created if c["id_type"] == "OBJECT"]
    by_kind = {}
    for entry in [c for c in report.created if c["id_type"] != "OBJECT"]:
        by_kind.setdefault(entry["id_type"].lower().replace("nodetree", "node group"), []).append(entry["name"])
    lines = []
    if objects:
        lines.append(f"Created objects: {listed(objects)}.")
    if by_kind:
        lines.append("Created data: " + "; ".join(f"{kind} {listed(names)}" for kind, names in sorted(by_kind.items())) + ".")
    if packed:
        lines.append(f"Packed into the file from the workspace: {len(packed)} image(s).")
    replaced = [r for r in report.replaced if not r.get("pointer")]
    if replaced:
        lines.append("Replaced: " + ", ".join(f"{r['key']}{' (removed)' if r.get('removed') else ''}"
                                              for r in replaced) + ".")
    lines += [f"scene.{r['pointer']} is now {r['name']}." for r in report.replaced if r.get("pointer")]
    renamed = _build_key_names(report)  # Only what the build named so: a .001 of its own is no rename.
    lines += [f"Renamed on arrival, the name being taken live: {kind} {name} -> {live}." for (kind, name), live
              in renamed.items() if f"{kind}:{name}" in built]
    lines += [f"Material {r['made']} is the live {r['name']} made again: the live one is used." for r in report.reused]
    lines += [f"Kept live, not replaced: {c.get('pointer') or ', '.join(c.get('keys', []))}: {c['reason']}."
              for c in report.conflicts]
    lines += [f"Mesh {f['name']} had invalid geometry; fixed before it came in." for f in report.fixed]
    lines += [f"{s['kind'].lower()} {s['name']} is missing after the apply: {s.get('why', '')}."
              for s in report.support if s.get("state") == "missing"]
    from .merge import retargeted_text, unlinked_text
    lines += [f"Driver re-aimed: {retargeted_text(r)}." for r in report.retargeted[:DRIVERS_LISTED]]
    lines += [f"Driver left without its target: {unlinked_text(u)}. Re-link it, or leave it if the loss is "
              f"intended." for u in report.unlinked[:DRIVERS_LISTED]]
    more = max(0, len(report.retargeted) - DRIVERS_LISTED) + max(0, len(report.unlinked) - DRIVERS_LISTED)
    if more:
        lines.append(f"And {more} more drivers re-aimed or left without their target, not listed here.")
    lines += [line.replace("build: ", "", 1) for line in report.errors]
    if settings_set:
        shown = [f"{path} = {value}" for path, value in list(settings_set.items())[:12]]
        lines.append("Scene settings from the build: " + "; ".join(shown)
                     + (f"; and {len(settings_set) - 12} more" if len(settings_set) > 12 else "") + ".")
    if settings_kept:
        lines.append("Scene settings kept live (changed there since your source): " + "; ".join(
            f"{path} live {live}, build {built}" for path, (live, built) in list(settings_kept.items())[:8]) + ".")
    lines += [f"Could not set {item}." for item in failed]
    dropped = [d["name"] for d in report.dropped_edits]
    if dropped:
        lines.append(f"Not brought (changed in the build, not in owns): {', '.join(dropped[:12])}"
                     + (f" (+{len(dropped) - 12})" if len(dropped) > 12 else "")
                     + ". Name them in owns and apply again to bring them.")
    return lines


def _merge(session: dict, ws: Path, prepared: dict, number: int, result_name: str) -> ToolResult:
    """Main thread: the prepared result into the live scene, the scene settings after it, and the
    workspace's record of what is live now."""
    import bpy
    from . import merge, ownership, scene_diff
    data = load_state(ws)
    snapshot = snapshot_for(data, prepared.get("snapshot"))
    live_tokens, live_pointers, live_settings = live_side(data, snapshot)
    names = {key: data["live"]["names"].get(key, key.split(":", 1)[1]) for key in live_tokens}
    keys = set((snapshot or {}).get("tokens") or ()) | set(live_tokens)
    ids = _live_ids(session["id"], keys, names)
    source_pointers = (snapshot or {}).get("pointers") or {"camera": None, "world": None}
    pointers = live_pointers or source_pointers
    manifest = dict(prepared["manifest"])
    built = dict(manifest.get("pointers") or {})
    for attr in ("camera", "world"):  # Left as its source had it: no change, whatever is live now.
        if built.get(attr) == source_pointers.get(attr):
            built[attr] = pointers.get(attr)
    manifest["pointers"] = built
    spawn = merge.Spawn(f"workspace-apply-{number}", _own(ws) / f"apply-{number}" / SOURCE, ids,
                        dict((snapshot or {}).get("tokens") or {}), dict(pointers), live_tokens=live_tokens)
    brief = {"id": "build", "role": "", "prefix": "", "aspects": ["camera", "lighting"], "keep_collections": True}
    before = scene_diff.snapshot()
    report = merge.merge_wave(spawn, [{"brief": brief, "manifest": manifest}], checkpoint=lambda: "", keep_spawn=True)
    scene = bpy.context.scene
    source_settings = (snapshot or {}).get("settings") or {}
    live_now = scene_settings(scene)
    wanted, kept = settings_changes(prepared.get("settings") or {}, source_settings, live_now,
                                    live_settings or source_settings)
    failed = set_settings(scene, wanted)

    # What is live now, for the next apply: every key this one created or replaced, measured as the
    # next apply will measure it (the same key -> uid map), with the snapshot count at this time.
    renamed = _build_key_names(report)
    for entry in manifest.get("created") or ():
        kind, _, name = entry.partition(":")
        live = ownership.find(f"{kind}:{renamed.get((kind, name), name)}")
        if live is not None:
            ids[entry] = live.session_uid
            names[entry] = live.name
    for key, uid in spawn.moved.items():
        ids[key] = uid
    by_uid = {idb.session_uid: idb for idb in ownership.all_ids()}
    uid2spawn = {uid: key.split(":", 1)[1] for key, uid in ids.items() if uid in by_uid}
    touched = set(manifest.get("created") or ()) | set(spawn.moved) | {r["key"] for r in report.replaced if not r.get("pointer")}
    count = len(data["snapshots"])
    for key in touched:
        idb = by_uid.get(ids.get(key))
        if idb is None:
            data["live"]["tokens"].pop(key, None)
            continue
        data["live"]["tokens"][key] = [ownership.token(idb, uid2spawn), count]
        data["live"]["names"][key] = idb.name
    uid2key = {uid: key for key, uid in ids.items()}
    data["live"]["pointers"] = {"snapshots": count, "value": {
        attr: (uid2key.get(getattr(scene, attr).session_uid, ownership.key_of(getattr(scene, attr)))
               if getattr(scene, attr) is not None else None) for attr in ("camera", "world")}}
    data["live"]["settings"] = {"snapshots": count, "value": scene_settings(scene)}
    scene_lines = _mirror_scenes(prepared.get("scenes") or [], ids, by_uid, data, scene)
    after = scene_diff.snapshot()
    summary = {"number": number, "result": result_name, "snapshot": (snapshot or {}).get("number"),
               "time": round(time.time(), 3), "log": report.log(), "settings": wanted, "settings_kept": kept}
    data["applies"].append(summary)
    save_state(ws, data)
    _UIDS[session["id"]] = {**(_UIDS.get(session["id"]) or {}), **ids}
    (_own(ws) / f"apply-{number}" / "report.json").write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")

    built_from = f"built from snapshot {snapshot['number']}" if snapshot else "with no snapshot to compare against"
    head = f"Applied {result_name} ({built_from}). {report.headline()}."
    lines = [head, *_summary_lines(report, wanted, kept, failed, prepared.get("packed") or [],
                                   set(manifest.get("created") or ()))]
    lines += scene_lines
    if prepared.get("active_scene"):
        lines.append(f"{result_name} was saved with its own scene {prepared['active_scene']} active; the user's scene "
                     f"took its camera, world and settings from {prepared.get('scene') or 'the source scene'} in it.")
    if not report.created and not report.replaced and not wanted:
        lines.append("Nothing changed live: the build made nothing new, and owns named nothing it changed.")
    return ToolResult("\n".join(lines), scene_before=before, scene_after=after,
                      ok=not report.conflicts and not report.errors)


def _mirror_scenes(entries: list, ids: dict, by_uid: dict, data: dict, main) -> list[str]:
    """Main thread, after the merge: each scene the build made (workspace_child's "scenes") as a scene of
    its own here, made once and refilled on later applies (data["scenes"]: build name -> live name), with
    the live versions of the objects it holds, its camera, world and settings. Objects only such a scene
    has leave the user's scene, where the merge put them. Returns a line per scene."""
    import bpy
    made, lines = data.setdefault("scenes", {}), []
    tree = {main.collection, *main.collection.children_recursive}
    for entry in entries:
        scene = bpy.data.scenes.get(made.get(entry["name"], "\0"))
        if scene is None:
            scene = bpy.data.scenes.new(entry["name"])
            made[entry["name"]] = scene.name
        else:
            for ob in list(scene.collection.objects):
                scene.collection.objects.unlink(ob)
        missing = []
        for key in entry.get("objects") or ():
            ob = by_uid.get(ids.get(key))
            if ob is None or ob.id_type != "OBJECT":
                missing.append(key.split(":", 1)[1])
            elif scene.collection.objects.get(ob.name) is None:
                scene.collection.objects.link(ob)
        for key in entry.get("only_here") or ():
            ob = by_uid.get(ids.get(key))
            for collection in list(ob.users_collection) if ob is not None else ():
                if collection in tree:
                    collection.objects.unlink(ob)
        for attr in ("camera", "world"):
            target = by_uid.get(ids.get(entry.get(attr) or ""))
            if target is not None:
                setattr(scene, attr, target)
        failed = set_settings(scene, entry.get("settings") or {})
        lines.append(f"Scene {scene.name}, the build's own, is a scene here too: {len(scene.collection.objects)} objects"
                     + (f", camera {scene.camera.name}" if scene.camera else "") + (f", world {scene.world.name}" if scene.world else "")
                     + (f"; not found: {', '.join(missing[:8])}" if missing else "")
                     + (f"; {len(failed)} settings could not be set" if failed else "") + ".")
    return lines


def apply_build(blend: str, owns: list | None = None) -> ToolResult:
    from . import merge
    session, ws = _workspace()
    result = _path(blend)
    if not result.is_file() or result.suffix.lower() != ".blend":
        raise ToolError(f"{result.name} is not a .blend file.")
    if owns is not None and not (isinstance(owns, list) and all(isinstance(n, str) and n.strip() for n in owns)):
        raise ToolError("owns must be a list of datablock names")
    data = load_state(ws)
    number = len(data["applies"]) + 1
    out_dir = _own(ws) / f"apply-{number}"

    def then(is_cancelled, on_main):
        payload = {"owns": [n.strip() for n in owns or ()], "workspace": str(ws), "out": str(out_dir / "result.blend"),
                   "snapshots": {str(s["number"]): {"tokens": s["tokens"], "recreated": recreated_keys(data, s),
                                                    "scene": s.get("scene")} for s in data["snapshots"]},
                   "recreated": recreated_keys(data, data["snapshots"][-1] if data["snapshots"] else None)}
        prepared = _child(ws, "prepare", result, payload, out_dir, is_cancelled)
        if prepared.get("problems"):
            return ToolResult("Nothing applied: " + "; ".join(prepared["problems"]), ok=False)
        try:
            return on_main(lambda: _merge(session, ws, prepared, number, result.name))
        except merge.MergeError as ex:
            return ToolResult(str(ex), ok=False)
    return ToolResult("", then=then)


# ------------------------------------------------------------------ the gate, cards, dispatch

def needs_approval(name: str, arguments: dict, roots_) -> bool:
    """shell, run_blender and edit_file wait for the user like run_python (unless auto-run or "Always
    allow"); view_image reads like read_file. apply_build is a scene change (tools.CHANGES_SCENE)."""
    if name in ASKS:
        return True
    if name == "view_image":
        from . import files
        return files.needs_approval("read_file", arguments, roots_)
    return False


# Scripts a shell command names: read for what they would install (tools.installs_code).
_SCRIPT_NAME = re.compile(r"[\w./~-]+\.(?:py|sh|bash|zsh|command)\b")
SCRIPT_READ_LIMIT = 1_000_000


def installs(name: str, arguments: dict, roots_) -> bool:
    """Whether the script run_blender runs, or one a shell command names, installs packages or downloads
    code and runs it (the command's own text is tools.installs). Off the main thread: only reads files;
    a relative name means the workspace (roots_[0])."""
    from .tools import installs_code
    if name == "run_blender":
        names = [str(arguments.get("script", ""))]
    elif name == "shell":
        names = _SCRIPT_NAME.findall(str(arguments.get("command", "")))
    else:
        return False
    for script in dict.fromkeys(n for n in names if n):
        path = Path(script).expanduser()
        if not path.is_absolute():
            if not roots_:
                continue
            path = Path(roots_[0]) / path
        try:
            if path.is_file() and path.stat().st_size <= SCRIPT_READ_LIMIT and installs_code(
                    path.read_text(encoding="utf-8", errors="replace")):
                return True
        except OSError:
            continue
    return False


def describe(name: str, arguments: dict) -> tuple[str, str] | None:
    if name == "shell":
        command = str(arguments.get("command", ""))
        summary = " ".join(str(arguments.get("summary") or "").split())
        if summary:
            return summary, command
        first = command.strip().splitlines()[0] if command.strip() else ""
        return f"$ {first[:80]}", command
    if name == "run_blender":
        blend = arguments.get("blend", SOURCE)
        return f"Blender: {arguments.get('script', '')} on {blend or 'an empty scene'}", ""
    if name == "edit_file":
        return (f"Edit {arguments.get('path', '')}",
                f"- {arguments.get('old_text', '')}\n+ {arguments.get('new_text', '')}")
    if name == "view_image":
        return f"View {arguments.get('path', '')}", ""
    if name == "apply_build":
        owns = arguments.get("owns") or []
        return f"Apply {arguments.get('blend', '')} to the scene" + (f" (owns {', '.join(map(str, owns))})" if owns else ""), ""
    if name == "snapshot_scene":
        return f"Save the scene as the workspace's {SOURCE}", ""
    return None


DISPATCH = {"shell": shell, "run_blender": run_blender, "edit_file": edit_file, "view_image": view_image,
            "apply_build": apply_build, "snapshot_scene": snapshot_scene}


def system_prompt(addons_line: str = "") -> str:
    """The workspace agent's prompt, then what is installed."""
    return f"{PROMPT}\n\n{addons_line}" if addons_line else PROMPT
