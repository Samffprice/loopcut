"""The agent's tools. Every function here runs on Blender's main thread."""

import contextlib
import inspect
import io
import json
import math
import re
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import bpy

from . import scratch

MAX_OUTPUT_CHARS = 8000
CAPTURE_WIDTH = 640  # Image tokens scale with pixels; 640 still shows shape, placement and contact.
NAMES_LIMIT = 400    # Names get_scene_info lists when rows do not fit; past this, narrow the question.
MAX_SELECTED_ROWS = 40  # Rows for the selection when only names fit.
DEFAULT_RUN_TIMEOUT = 60.0


class ToolError(Exception):
    """A failure the model should see and can react to."""


@dataclass
class ToolResult:
    text: str
    image_path: Path | None = None
    ok: bool = True
    # scene_diff snapshots around a scene-changing tool, for the turn's "what changed" card.
    scene_before: dict | None = None
    scene_after: dict | None = None
    pending_job: Path | None = None  # Inspections wait on the agent thread, never in the UI pump.
    # The rest of a call that waits (render_job status with wait): agent._run_tool_on_main calls it
    # on the agent thread as then(is_cancelled, on_main) and returns what it returns.
    then: Callable | None = None
    evidence_token: dict | None = None  # Rechecked if later tools in the same batch change the scene.
    guard_notes: list | None = None  # cost_guard.take: modifiers it switched off, shown in the panel too.
    file_image: bool = False  # The image is a file preview, not a look at the open scene.
    receipt: str = ""  # A paid cloud call that finished: what it used ("Used 6 credits ($0.06)"), for its card.
    plan_refused: bool = False  # Helper arguments/plan validation; raw text still goes to the model and Details.


SCHEMAS = [  # Sent with every request: every word here is paid for on every step.
    {"type": "function", "function": {
        "name": "run_python",
        "title": "Updating the scene",
        "description": (
            "Run Python in the live Blender session, a new namespace each call (`bpy`, `math`, `bmesh`, "
            "`mathutils`, `Vector`, `Matrix` preloaded; reuse code via a Text block); "
            "bpy.ops have a 3D viewport context (or another editor's, see `editor`). "
            "Prefer the data API over bpy.ops. Returns prints, any traceback and the measured `Scene changes`. "
            "One undo step per call."),
        "parameters": {"type": "object", "properties": {
            "code": {"type": "string", "description": "Python source to execute"},
            "summary": {"type": "string", "description": "A few words on what this does, shown to the user"},
            "editor": {"type": "string", "description": "Editor whose context bpy.ops run in, for operators that "
                       "need one: ShaderNodeTree, GeometryNodeTree, IMAGE_EDITOR, SEQUENCE_EDITOR... Default VIEW_3D."},
            "capture": {"type": "string", "enum": ["three_quarter", "front", "side", "top", "camera", "sheet"],
                        "description": "Also return a viewport capture afterwards, framed like "
                                       "capture_viewport. Cheaper than a separate call."},
        }, "required": ["code", "summary"]},
    }},
    {"type": "function", "function": {
        "name": "get_scene_info",
        "title": "Checking the scene",
        "description": (
            "The scene as JSON, in as much detail as fits. Always: mode, frame, camera, counts by type and "
            "by collection. Then one row per matching object (type, location, bounds, materials, modifiers, "
            "mesh size; zero rotation, unit scale, visible and unselected are left out) when they fit, else "
            "names by collection: then narrow with name_contains, type or collection, or ask detail=rows. "
            "`bounds` is the world-space box the geometry occupies: use it for layout; check_contact "
            "measures contact. "
            "`location` is only the origin, relative to a parent, and can sit far from the geometry. "
            "get_object_info has the full setup of an object."),
        "parameters": {"type": "object", "properties": {
            "name_contains": {"type": "string", "description": "Only objects whose name contains this"},
            "type": {"type": "string", "description": "Only this object type, e.g. MESH, LIGHT, CAMERA"},
            "collection": {"type": "string", "description": "Only objects in a collection whose name contains this"},
            "changed_only": {"type": "boolean", "description": "Only objects added or changed since this turn "
                                                               "began: what you have touched so far"},
            "detail": {"type": "string", "enum": ["rows", "names"],
                       "description": "rows: as many full rows as fit, selected first. names: names only. "
                                      "Default: rows if they all fit, else names."},
        }},
    }},
    {"type": "function", "function": {
        "name": "get_object_info",
        "title": "Checking object details",
        "description": (
            "Full setup of up to 5 objects: modifier settings, material node trees, geometry-nodes "
            "inputs, constraints, animation, custom properties. Read before changing an existing setup."),
        "parameters": {"type": "object", "properties": {
            "names": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 5,
                      "description": "Object names; \"world\" for the scene's world"},
            "detail": {"type": "string", "enum": ["full", "transform"],
                       "description": "transform: only transform, bounds, mesh counts, modifiers, parent/children, "
                                      "slot names"},
        }, "required": ["names"]},
    }},
    {"type": "function", "function": {
        "name": "inspect_api",
        "title": "Checking Blender capabilities",
        "description": (
            "The Python API of THIS Blender, installed add-ons included: property names, enum values, "
            "defaults, operator arguments, node sockets. Use before an API you are not sure of, and after any AttributeError, "
            "TypeError or 'enum not found'. `path` like bpy.types.BevelModifier, Object.modifiers, "
            "bpy.ops.mesh.bevel, ShaderNodeTexNoise, bmesh.ops.bevel; or `search` words: 'action fcurves'. "
            "Naming a bundled add-on turns it on."),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Dotted path to describe"},
            "search": {"type": "string", "description": "Words to find in names and descriptions"},
        }},
    }},
    {"type": "function", "function": {
        "name": "compare_with_reference",
        "title": "Comparing with the reference",
        "description": (
            "One image: the reference the user attached on the left, a viewport capture on the right at "
            "the same height. Use it after each change when copying a reference, and fix what differs."),
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "File name of the attached image"},
            "focus": {"type": "array", "items": {"type": "string"}, "description": "Objects to frame; default all"},
            "angle": {"type": "string", "enum": ["three_quarter", "front", "side", "top", "camera", "user"],
                      "description": "Default three_quarter; camera looks through the scene camera"},
            "style": {"type": "string", "enum": ["material", "distinct"]},
            "labels": {"type": "boolean", "description": "Draw object names, to tell objects apart; default off"},
        }, "required": ["name"]},
    }},
    {"type": "function", "function": {
        "name": "look_at_reference",
        "title": "Looking at the reference",
        "description": (
            "The attached image at full resolution, or a part of it, to check a detail. `region` is "
            "[x0, y0, x1, y1] as fractions of width and height from the top-left; omit for the whole."),
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "File name of the attached image"},
            "region": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
        }, "required": ["name"]},
    }},
    {"type": "function", "function": {
        "name": "capture_viewport",
        "title": "Checking the model’s appearance",
        "description": (
            "An image of the scene, framed by the tool from a 3/4 angle with materials, leaving the "
            "user's viewport untouched. Also lists the visible objects nearest first, which settles "
            "what is in front when colors look alike."),
        "parameters": {"type": "object", "properties": {
            "focus": {"type": "array", "items": {"type": "string"},
                      "description": "Objects to frame; default everything visible"},
            "angle": {"type": "string", "enum": ["three_quarter", "front", "side", "top", "camera", "user", "sheet"],
                      "description": "Default three_quarter. camera: through the scene camera. user: the "
                                     "user's current view. Both ignore focus. sheet: three_quarter, front, "
                                     "side and top as one 2x2 image, for placement."},
            "style": {"type": "string", "enum": ["material", "distinct", "rendered"],
                      "description": "distinct: a flat color per object, to judge shape and contact. "
                                     "rendered: EEVEE preview with scene lights and world; an approximation "
                                     "when the final engine is Cycles. material uses studio lighting, not scene lights"},
            "labels": {"type": "boolean", "description": "Draw object names, to tell objects apart; default off"},
        }},
    }},
]

# Tools that can change the scene. A checkpoint is taken before the first one in a turn, and they
# need the user's go-ahead unless auto-run is on.
# apply_build brings a workspace build into it (workspace.py, the single agent in workspace mode).
# delegate starts workers whose results merge into the scene (orchestrator.py); accept_quarantine
# moves what a merge kept aside into it; uv_unwrap writes UV maps and transfer_textures bakes images and
# repoints materials (uv_tools.py); bake_paint_masks makes images and stylize_material rebuilds a material
# (paint_tools.py, offered to workers only). clip_background puts an attached clip behind a camera.
CHANGES_SCENE = {"run_python", "import_asset", "frame_camera", "delegate", "accept_quarantine", "uv_unwrap",
                 "transfer_textures", "bake_paint_masks", "stylize_material", "apply_build",
                 "clip_background"}  # A camera background and the scene's frame rate (video_attach.py).
NEEDS_APPROVAL = CHANGES_SCENE
# Tools since merged into one with a `source` or an `action`: old name -> (current name, the
# arguments the old name implied, the old name of its `id` argument). Saved conversations still
# hold calls to the old names: conversations.load rewrites them and agent._describe labels them.
# They are never dispatched.
RENAMED = {
    "search_polyhaven": ("search_assets", {"source": "polyhaven"}, ""),
    "import_polyhaven": ("import_asset", {"source": "polyhaven"}, "asset_id"),
    "search_blenderkit": ("search_assets", {"source": "blenderkit"}, ""),
    "import_blenderkit": ("import_asset", {"source": "blenderkit"}, "asset_id"),
    "search_polypizza": ("search_assets", {"source": "polypizza"}, ""),
    "import_polypizza": ("import_asset", {"source": "polypizza"}, "model_id"),
    "search_assets": ("search_assets", {"source": "library"}, ""),  # The Blender libraries' own, before `source`.
    "import_asset": ("import_asset", {"source": "library"}, "ref"),
    "start_render_job": ("render_job", {"action": "start"}, ""),
    "render_job_status": ("render_job", {"action": "status"}, ""),
    "cancel_render_job": ("render_job", {"action": "cancel"}, ""),
    "resume_render_job": ("render_job", {"action": "resume"}, ""),
}
# Tools whose result depends only on their arguments and the scene, so the same call with nothing
# changed in between says the same again; see repeats.py. Not the viewport looks, which agent.py
# caps on its own (_capture_refusal), nor files and jobs, which change without the scene changing.
# search_assets is every library's search: the source is an argument, so part of the call's key.
# UV and pixel edits are not in scene_diff, but they come from run_python or uv_unwrap, and any call
# to a scene-changing tool starts the reads over (agent.run_loop).
SAME_WHILE_UNCHANGED = {"get_scene_info", "get_object_info", "inspect_api", "look_at_reference", "search_assets",
                        "check_contact", "inspect_uv", "inspect_texture"}
# Code that renders, bakes or simulates can hold Blender for minutes. Such a step waits for the
# user unless they said "Always allow renders": "Always allow" and auto-run do not cover it.
# Not render.render: the cost guard refuses it in the code whatever the user allows
# (cost_guard.IN_PROCESS_RENDERS), so asking to allow it would only lead to that refusal.
HEAVY_CODE = re.compile(r"ops\.(?:render\.opengl|object\.bake|cycles\.bake|ptcache\.bake|"
                        r"fluid\.bake|rigidbody\.bake|object\.(?:voxel|quadriflow)_remesh)")


# Code that installs packages or downloads code and runs it asks every time, whatever the user allowed:
# neither auto-run nor "Always allow" covers it, since it puts third-party code on their machine. In the
# v0.2.0 RC smoke test the workspace agent, auto-run on, ran `python3 -m pip install --target ./verify_deps
# imageio-ffmpeg` to read back the frames of the MP4 it had rendered (C29). Read from run_python's code,
# the shell's command and the scripts run_blender or the command runs (workspace.installs); a pattern
# list for what agents write, not a sandbox. agent._approval calls such a step "install".
INSTALLS = tuple(re.compile(pattern, re.MULTILINE) for pattern in (
    r"\bpip[\d.]*\b.*\binstall\b",                     # pip install, python -m pip --quiet install, uv pip install
    r"\bpip[\d.]*\W{0,12}install\b",                   # [sys.executable, "-m", "pip",\n "install", ...]
    r"\bpip\.main\b|\bpip\._internal\b|^\s*(?:from|import)\s+pip\b",
    r"\bensurepip\b|\bget-pip\b|\beasy_install\b",
    r"\b(?:pipx|uv|conda|mamba|micromamba|poetry|npm|pnpm|yarn|bun|gem|cargo|brew|apt-get|apt)"
    r"\s+(?:-\S+\s+)*(?:install|add)\b",
    r"\bnpm\s+i\b|\b(?:pipx|uv)\s+(?:-\S+\s+)*run\b|\buv\s+tool\s+(?:install|run)\b|\b(?:npx|uvx|bunx)\s+\S",
    r"\bextensions\.package_install\b|\bpreferences\.addon_install\b",   # Blender extensions and add-ons.
    # curl … | sh (or python3, python3 -): what is fetched runs. Not `| python3 -m json.tool`, which only reads.
    r"\b(?:curl|wget)\b[^\n]*\|\s*(?:sudo\s+)?(?:(?:ba|z|da|k)?sh(?:\s+-s\b[^\n|;&]*)?|python[\d.]*(?:\s+-)?"
    r"|perl|ruby|node)\s*(?:$|[;&|)])",
    r"\b(?:ba|z|da|k)?sh\s+<\(\s*(?:curl|wget)\b|\b(?:eval|(?:ba|z|da|k)?sh\s+-c)\s+[\"']?\$\(\s*(?:curl|wget)\b",
    r"\b(?:curl|wget)\b[^\n]*\.(?:sh|py|pkg|whl|run|command|dmg|exe|msi)\b",  # fetching a script or installer
))
# Python that fetches and then runs what it fetched: both halves anywhere in the code.
_FETCHES = re.compile(r"\burlopen\b|\burlretrieve\b|\brequests\.get\b|\bhttpx\.get\b|\bhttp\.client\b")
_RUNS_FETCHED = re.compile(r"\bexec\s*\(|\beval\s*\(|\brunpy\.|\bimportlib\.")


def installs_code(text: str) -> bool:
    """Whether code or a command installs packages or downloads code and runs it."""
    return (any(pattern.search(text) for pattern in INSTALLS)
            or bool(_FETCHES.search(text) and _RUNS_FETCHED.search(text)))


def installs(name: str, arguments: dict) -> bool:
    """A step whose own text installs or fetches-and-runs code: it asks even when other steps run unasked."""
    field = {"run_python": "code", "shell": "command"}.get(name)
    return bool(field) and installs_code(str(arguments.get(field, "")))


def only_reads(name: str, arguments: dict) -> bool:
    """A call to a scene-changing tool that only measures: frame_camera with report_only. It takes
    no checkpoint, needs no go-ahead, and counts as a read (SAME_WHILE_UNCHANGED)."""
    return name == "frame_camera" and str(arguments.get("report_only")).lower() == "true"


def is_heavy(name: str, arguments: dict) -> bool:
    """A step that renders, bakes or simulates: it asks even when other steps run unasked."""
    if name == "run_python":
        return bool(HEAVY_CODE.search(str(arguments.get("code", ""))))
    if name == "render_job":
        return arguments.get("action") in ("start", "resume")  # Not a set: the model may send a list.
    if name in ("inspect_scene", "bake_paint_masks", "transfer_textures"):
        return True
    return name == "see_render" and bool(arguments.get("render"))


# The workspace's tools (workspace.py): what they act on are files and processes, which change without
# the scene changing, so the same call later in a reply (run_blender after an edit_file) runs again.
WORKSPACE_TOOLS = {"shell", "run_blender", "edit_file", "view_image", "apply_build", "snapshot_scene"}


def changes_over_time(name: str, arguments: dict) -> bool:
    """A call whose result moves on by itself while nothing else changes: a render job's status as
    the job renders, and the workspace's tools. The same call again is never refused or noted as a
    repeat, even in one reply; polling a running job without `wait` gets its own note (repeats.POLLS)."""
    return name == "render_job" and arguments.get("action") == "status" or name in WORKSPACE_TOOLS


def current_call(name: str, arguments: dict) -> tuple[str, dict]:
    """A call as the current tools spell it: an old name becomes its merged tool (see RENAMED)."""
    if name not in RENAMED or {"source", "action"} & set(arguments):
        return name, arguments
    current, implied, id_name = RENAMED[name]
    arguments = dict(arguments)
    if id_name in arguments:
        arguments["id"] = arguments.pop(id_name)
    return current, {**implied, **arguments}


def _view3d_override() -> dict:
    wm = bpy.context.window_manager
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                region = next((r for r in area.regions if r.type == "WINDOW"), None)
                if region:
                    return {"window": window, "screen": window.screen, "area": area, "region": region}
    raise ToolError("No 3D viewport is open; open one and try again.")


_TREE_EDITORS = ("ShaderNodeTree", "GeometryNodeTree", "CompositorNodeTree", "TextureNodeTree")
_EDITOR_ALIASES = {"NODE_EDITOR": "ShaderNodeTree"}  # A bare node editor shows no tree; the shader one follows the active material.


def _editor_name(editor: str) -> str:
    """An Area.type or a node tree kind, whatever case the model typed it in."""
    wanted = editor.strip()
    types = [e.identifier for e in bpy.types.Area.bl_rna.properties["type"].enum_items
             if e.identifier not in ("EMPTY", "LOOPCUT")]
    for name in (*types, *_TREE_EDITORS):
        if name.lower() == wanted.lower():
            return _EDITOR_ALIASES.get(name, name)
    raise ToolError(f"No editor {editor!r}. Editors: {', '.join([*_TREE_EDITORS, *types])}.")


def _editor_matches(area, editor: str) -> bool:
    return area.type == editor or (editor in _TREE_EDITORS and area.ui_type == editor)


def _panel_host():
    """UI registration is unavailable in background Blender and lightweight tests."""
    if getattr(bpy.app, "background", False):
        return None
    try:
        from .ui import host
        return host
    except (ImportError, AttributeError):
        return None


@contextlib.contextmanager
def _preserve_host_areas():
    """Keep the panel reachable even when executed code changes its editor type."""
    host = _panel_host()
    saved = [(w, a.as_pointer(), a.type, a.ui_type)
             for w in bpy.context.window_manager.windows for a in w.screen.areas
             if host.is_host(a)] if host else []
    notices = []
    try:
        yield notices
    finally:
        restored = False
        for window, pointer, saved_type, saved_ui in saved:
            try:
                area = next((a for a in window.screen.areas if a.as_pointer() == pointer), None)
                if area is None or (area.type, area.ui_type) == (saved_type, saved_ui):
                    continue
                with bpy.context.temp_override(window=window, screen=window.screen, area=area):
                    area.type = saved_type
                    area.ui_type = saved_ui
                    if not host.NATIVE:
                        host.hosts().add(pointer)
                    area.tag_redraw()
                restored = True
            except (ReferenceError, RuntimeError):
                continue  # The code may have closed the area or its window.
        if restored:
            notices.append("Loopcut panel's area was restored; show previews in another editor.")


@contextlib.contextmanager
def _editor_context(editor: str):
    """bpy.context for one step. The 3D viewport by default; another open editor of the asked
    kind; else the viewport is switched to that editor for the step and switched back, which
    keeps its view and shading. Nothing redraws in between, so the user sees no change."""
    editor = _editor_name(editor) if editor and editor.strip() else "VIEW_3D"
    if getattr(bpy.app, "background", False):
        with _background_context(editor):
            yield
        return
    host = _panel_host()
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if host and host.is_host(area):
                continue
            region = next((r for r in area.regions if r.type == "WINDOW"), None)
            if region and _editor_matches(area, editor):
                with bpy.context.temp_override(window=window, screen=window.screen, area=area, region=region):
                    yield
                return
    # The user may be in the Sequencer, render viewer, or a workspace without a 3D view.
    # Borrow a regular editor only for this synchronous step and restore its exact UI type.
    candidates = [(w, a) for w in bpy.context.window_manager.windows for a in w.screen.areas
                  if a.type not in ("LOOPCUT", "TOPBAR", "STATUSBAR")
                  and not (host and host.is_host(a))
                  and any(r.type == "WINDOW" for r in a.regions)]
    if not candidates:
        raise ToolError("No editor area is available for this operation.")
    window, area = max(candidates, key=lambda pair: pair[1].width * pair[1].height)
    saved_type, saved_ui = area.type, area.ui_type
    with bpy.context.temp_override(window=window, screen=window.screen, area=area):
        if editor in _TREE_EDITORS:
            area.type = "NODE_EDITOR"
            area.ui_type = editor  # Points the editor at the active object's material or modifier.
        else:
            area.type = editor
        try:
            region = next((r for r in area.regions if r.type == "WINDOW"), None)
            with bpy.context.temp_override(window=window, screen=window.screen, area=area, region=region):
                yield
        finally:
            area.type, area.ui_type = saved_type, saved_ui


@contextlib.contextmanager
def _background_context(editor: str):
    """bpy.context in background Blender (a worker's child Blender, the headless checks). The file's
    first window and its screen, which undo_push polls for; also that screen's 3D viewport for the
    default editor, so view3d operators work as they do for the user. No editor is ever switched:
    nothing draws, and there is no UI to restore. With no window at all, no override."""
    windows = bpy.context.window_manager.windows
    if not windows:
        yield
        return
    window = windows[0]
    override = {"window": window, "screen": window.screen}
    area = next((a for a in window.screen.areas if a.type == "VIEW_3D"), None) if editor == "VIEW_3D" else None
    region = next((r for r in area.regions if r.type == "WINDOW"), None) if area else None
    if region:
        override.update(area=area, region=region)
    with bpy.context.temp_override(**override):
        yield


def _clip(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    half = MAX_OUTPUT_CHARS // 2
    return f"{text[:half]}\n... [{len(text) - MAX_OUTPUT_CHARS} chars omitted] ...\n{text[-half:]}"


class _RunTimeout(BaseException):
    """BaseException so that model code catching Exception in its loop cannot swallow it."""


class _Deadline:
    """sys.settrace hook that stops model-written code which runs too long. It runs on the main
    thread, so an endless loop would otherwise freeze Blender with no way out but killing it.
    Only lines of the model's own code are checked; one long call into Blender is not interrupted."""

    def __init__(self, seconds: float):
        self.seconds = seconds
        self.at = time.monotonic() + seconds

    def __call__(self, frame, event, arg):
        return self._line if frame.f_code.co_filename == "<loopcut>" else None

    def _line(self, frame, event, arg):
        if time.monotonic() > self.at:
            raise _RunTimeout()
        return self._line


def _model_traceback(ex: BaseException) -> str:
    """Only the frames in the model's own code: our wrapper's frames and Blender's paths are
    noise it pays for on every later step."""
    from . import api_docs
    frames = [f for f in traceback.extract_tb(ex.__traceback__) if f.filename == "<loopcut>"]
    text = "".join(["Traceback (most recent call last):\n", *traceback.format_list(frames),
                    *traceback.format_exception_only(type(ex), ex)])
    note = api_docs.error_note(ex)  # An Action note for action.fcurves, how to write // for an exporter.
    return text + (f"NOTE: {note}\n" if note else "")


def _python_namespace() -> dict:
    """What every run_python call starts with: models often reuse these from an earlier step's imports."""
    namespace = {"bpy": bpy, "math": math, "__name__": "__loopcut__"}
    try:  # Always present in Blender; absent only in tests that run without it.
        import bmesh
        import mathutils
    except ImportError:
        return namespace
    namespace.update(bmesh=bmesh, mathutils=mathutils, Vector=mathutils.Vector, Matrix=mathutils.Matrix,
                     Euler=mathutils.Euler, Quaternion=mathutils.Quaternion)
    return namespace


def run_python(code: str, summary: str = "", capture: str = "", editor: str = "") -> ToolResult:
    # Running model-written code is the product; the approval gate lives in agent.py.
    from . import config, contact, cost_guard, live_file, op_notes, scene_diff, shape_key_guard
    stdout = io.StringIO()
    ok, raised = True, False
    deadline = _Deadline(config.run_timeout())
    shape_key_guard.install()
    cost_guard.begin()
    before = scene_diff.snapshot()
    with _preserve_host_areas() as host_notices, _editor_context(editor):
        bpy.ops.ed.undo_push(message=f"Before Loopcut: {summary or 'run_python'}"[:60])
        previous_trace = sys.gettrace()
        try:
            compiled = compile(code, "<loopcut>", "exec")
            sys.settrace(deadline)
            # The deadline cannot stop one long native call, such as evaluating subdivision level 6;
            # the cost guard checks before each bpy.ops call evaluates the scene.
            # live_file: a save elsewhere is a copy, so the user's open file and their next save stay theirs.
            with (contextlib.redirect_stdout(stdout), cost_guard.watching_ops(), op_notes.watching_ops(),
                  live_file.watching_ops()):
                exec(compiled, _python_namespace())
        except _RunTimeout:
            ok = False
            stdout.write(f"\nStopped: the code ran for more than {deadline.seconds:g} s, which usually means a "
                         f"loop that never ends. Whatever it changed before that is listed below.")
        except Exception as ex:
            ok, raised = False, True
            stdout.write("\n" + _model_traceback(ex))
        finally:
            sys.settrace(previous_trace)
            shape_key_guard.check()
            notices = shape_key_guard.take() + op_notes.take() + live_file.take()
    notices.extend(host_notices)
    after = scene_diff.snapshot(guard=cost_guard.check)  # The guard runs before it evaluates.
    guarded, notes = cost_guard.take()
    output = _clip(stdout.getvalue().strip()) or "OK (no output)"
    if notices:
        output += "\n\n" + "\n".join(notices)
    diff = scene_diff.diff(before, after)
    diff["other"] += contact.recheck()  # Pairs check_contact was asked to watch: a change of status.
    changes = scene_diff.for_model(diff)
    if raised and not scene_diff.is_empty(diff):
        # A paint that landed before a later line failed read as lost (eval 20260930-154037 W0022).
        changes = "The code made these changes before the error; they stay in the scene.\n" + changes
    result = ToolResult(f"{output}\n\n{guarded}{changes}", ok=ok, scene_before=before, scene_after=after,
                        guard_notes=notes)
    if capture:
        # One request instead of two: the step and the look at what it did. Shaders the code just
        # made compile first, off the main thread (shaders.py); then the look, in its own task.
        from . import llm, shaders
        try:
            shot = shaders.before_look(lambda: capture_viewport(angle=capture), "material", capture,
                                       name="the run_python tool's capture")
        except ToolError as ex:
            result.text += f"\n\nCapture failed: {ex}"
            return result
        if shot.then is None:
            _add_shot(result, shot)
        else:
            def then(is_cancelled, on_main):
                try:
                    _add_shot(result, shot.then(is_cancelled, on_main))
                except llm.Cancelled:  # The code already ran: keep its result, and honor Stop after.
                    result.text += "\n\nCapture skipped: stopped."
                return result
            result.then = then
    return result


def _add_shot(result: ToolResult, shot: ToolResult) -> None:
    if shot.ok:
        result.text += f"\n\n{shot.text}"
        result.image_path = shot.image_path
    else:
        result.text += f"\n\nCapture failed: {shot.text}"


def _round(values, digits=3) -> list:
    return [round(v, digits) for v in values]


def _world_bounds(obj) -> dict | None:
    from mathutils import Vector
    if obj.type in {"CAMERA", "LIGHT", "EMPTY", "SPEAKER", "LIGHT_PROBE", "ARMATURE"}:
        return None
    corners = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
    return {"min": _round([min(c[i] for c in corners) for i in range(3)]),
            "max": _round([max(c[i] for c in corners) for i in range(3)])}


def object_summary(obj) -> dict:
    from . import view_layers
    entry = {
        "name": obj.name,
        "type": obj.type,
        "location": _round(obj.location),
        "rotation_deg": _round([math.degrees(a) for a in obj.rotation_euler], 2),
        "scale": _round(obj.scale),
        "visible": obj.visible_get(),
        "selected": obj.select_get(),
    }
    # One the active view layer excludes is not evaluated there: its bounds are read in a layer that
    # includes it (evaluating flushes them back), or said to be unavailable, never stale numbers.
    included = entry["visible"] or view_layers.includes(view_layers.active(), obj)
    layer = None if included else view_layers.home(obj)
    if layer is not None:
        with view_layers.evaluating(layer):
            bpy.context.evaluated_depsgraph_get()
        entry["evaluated_in"] = layer.name
    bounds = _world_bounds(obj)
    if bounds and not included and layer is None:
        bounds = "unavailable: excluded from every view layer"
    if bounds:
        entry["bounds"] = bounds
    if obj.parent:
        entry["parent"] = obj.parent.name
    if obj.material_slots:
        entry["materials"] = [s.material.name for s in obj.material_slots if s.material]
    if obj.modifiers:
        entry["modifiers"] = [f"{m.name} ({m.type})" for m in obj.modifiers]
    if obj.type == "MESH":
        mesh = obj.data
        entry["mesh"] = {"verts": len(mesh.vertices), "faces": len(mesh.polygons)}
    return entry


_ROW_DEFAULTS = {"rotation_deg": [0.0, 0.0, 0.0], "scale": [1.0, 1.0, 1.0], "visible": True, "selected": False}


def object_row(obj) -> dict:
    """object_summary without its defaults: what is not there is zero rotation, unit scale,
    visible and not selected. A third shorter, and the exceptions stand out."""
    return {key: value for key, value in object_summary(obj).items() if _ROW_DEFAULTS.get(key, _ROW_DEFAULTS) != value}


def _rows(entries: list) -> str:
    # One entry per line: compact for the token budget, still valid JSON.
    return "[\n" + ",\n".join(json.dumps(entry, separators=(",", ":")) for entry in entries) + "\n]"


def _name_entry(obj) -> str:
    entry = f"{obj.name} ({obj.type})"
    return f"{entry} child of {obj.parent.name}" if obj.parent else entry


def _changed_this_turn(objects: list) -> tuple[list, str]:
    """The objects added or changed since the turn began, judged by scene_diff's own reading of
    them; or all of them with a note when there is nothing to compare with."""
    from . import scene_diff, state
    since = state.session().get("scene_turn_start")
    if not since or since.get("too_many"):
        return objects, "changed_only: no snapshot from the start of this turn to compare with, so everything is listed. "
    before, now = since["objects"], scene_diff.snapshot()["objects"]
    return [o for o in objects if before.get(o.name) != now.get(o.name)], ""


def get_scene_info(name_contains: str = "", type: str = "", collection: str = "", changed_only: bool = False,
                   detail: str = "") -> ToolResult:
    """The scene in as much detail as fits MAX_OUTPUT_CHARS: an overview always, rows when they
    fit, names by collection otherwise. Never a truncated JSON: what does not fit is said to be
    missing, and the narrower question that would fit is named."""
    if detail not in ("", "rows", "names"):
        raise ToolError('detail must be "rows" or "names"')
    from . import capabilities, view_layers
    scene = bpy.context.scene
    active = bpy.context.view_layer.objects.active
    wanted = collection.lower()
    # An exact collection name wins ("Collection" is not "Scene Collection"); else a part of one.
    exact = wanted and any(c.name.lower() == wanted for c in [*bpy.data.collections, scene.collection])
    in_collection = (lambda o: True) if not wanted else \
        (lambda o: any((c.name.lower() == wanted) if exact else (wanted in c.name.lower()) for c in o.users_collection))
    chosen = [o for o in scene.objects
              if name_contains.lower() in o.name.lower() and (not type or o.type == type.upper()) and in_collection(o)]
    note = ""
    if changed_only:
        chosen, note = _changed_this_turn(chosen)
    chosen.sort(key=lambda o: (not o.select_get(), o.name))  # What the user is working on first.
    by_type: dict[str, int] = {}
    for obj in scene.objects:
        by_type[obj.type] = by_type.get(obj.type, 0) + 1
    info = {
        "blender_version": bpy.app.version_string,
        "scene": scene.name,
        "mode": bpy.context.mode,
        "active_object": active.name if active else None,
        "frame": {"current": scene.frame_current, "start": scene.frame_start, "end": scene.frame_end},
        "render_engine": scene.render.engine,
        "render_engines": capabilities.render_engines(scene.render),
        "output": {"media_type": getattr(scene.render.image_settings, "media_type", None),
                   "format": scene.render.image_settings.file_format, "path": scene.render.filepath,
                   "video_encoding": bpy.app.ffmpeg.supported},
        "editors": sorted({a.ui_type for w in bpy.context.window_manager.windows for a in w.screen.areas}),
        "unit_system": scene.unit_settings.system,
        "camera": scene.camera.name if scene.camera else None,
        "object_count": len(scene.objects),
        "by_type": dict(sorted(by_type.items(), key=lambda item: -item[1])),
        "collections": {c.name: len(c.objects) for c in bpy.data.collections if c.objects},
        "matching": len(chosen),
    }
    layers = view_layers.overview(scene)
    if layers:
        info["view_layers"] = layers
    head = json.dumps(info, separators=(",", ":"))[:-1]
    room = MAX_OUTPUT_CHARS - len(head) - 400  # For the note and the envelope.
    rows = [json.dumps(object_row(o), separators=(",", ":")) for o in chosen]
    if detail != "names" and sum(len(r) + 2 for r in rows) <= room:
        body = f'{head},"objects":[\n' + ",\n".join(rows) + "\n]"
        return ToolResult(body + (f',"note":{json.dumps(note.strip())}' if note else "") + "}")
    if detail == "rows":
        fitting, used = [], 0
        for row in rows:
            if used + len(row) + 2 > room:
                break
            fitting.append(row)
            used += len(row) + 2
        note += (f"{len(chosen)} objects match; the first {len(fitting)} are listed (selected first, then by "
                 f"name). Narrow with name_contains, type or collection for the rest.")
        return ToolResult(f'{head},"note":{json.dumps(note)},"objects":[\n' + ",\n".join(fitting) + "\n]}")
    # Names, grouped the way the user organized them, plus rows for what they are working on now.
    by_collection: dict[str, list[str]] = {}
    listed = 0
    for obj in chosen:
        for parent in obj.users_collection or [scene.collection]:
            if listed < NAMES_LIMIT:
                by_collection.setdefault(parent.name, []).append(_name_entry(obj))
                listed += 1
    selected = [object_row(o) for o in chosen if o.select_get()][:MAX_SELECTED_ROWS]
    note += (f"{len(chosen)} objects match, so only names are listed, by collection"
             + (f" (the first {NAMES_LIMIT})" if len(chosen) > NAMES_LIMIT else "")
             + '. Narrow with name_contains, type or collection, or ask detail="rows" for as many rows as fit; '
             "get_object_info has the full setup of one.")
    body = (f'{head},"note":{json.dumps(note)},"selected_objects":{_rows(selected)},'
            f'"objects_by_collection":{json.dumps(by_collection, separators=(",", ":"))}}}')
    return ToolResult(_clip(body))


def get_object_info(names: list[str], detail: str = "full") -> ToolResult:
    """detail full: object_summary and object_info.describe per object; transform: object_summary
    and object_info.brief. Past MAX_OUTPUT_CHARS, whole sections are left out and named, never a
    value cut in half (object_info.fit)."""
    from . import object_info
    if detail not in ("full", "transform"):
        raise ToolError("detail is full or transform.")
    if not names or len(names) > 5:
        raise ToolError("Pass 1 to 5 object names.")
    scene_world = bpy.context.scene.world

    def world(name):  # "world" is the scene's; a world's own name, that one. Objects come first.
        if name in bpy.data.objects:
            return None
        return scene_world if name.strip().lower() == "world" and scene_world else bpy.data.worlds.get(name)
    missing = [name for name in names if name not in bpy.data.objects and world(name) is None]
    if missing:
        close = [o.name for o in bpy.data.objects if any(m.lower() in o.name.lower() for m in missing)][:10]
        raise ToolError(f"No such object(s): {', '.join(missing)}."
                        + (f" Similar names: {', '.join(close)}." if close else ""))
    described = []
    for name in names:
        if world(name) is not None:
            described.append(object_info.describe_world(world(name), world(name) == scene_world))
            continue
        obj = bpy.data.objects[name]
        if detail == "transform":
            summary = {k: v for k, v in object_summary(obj).items() if k not in ("materials", "mesh")}
            described.append({**summary, **object_info.brief(obj)})
        else:
            described.append({**object_summary(obj), **object_info.describe(obj)})
    text, left_out = object_info.fit(described, MAX_OUTPUT_CHARS - 300)
    if left_out:
        text += (f"\nLeft out to fit {MAX_OUTPUT_CHARS:,} characters, largest first, each named in its "
                 f"\"omitted\": {'ask for fewer objects, or ' if len(names) > 1 else ''}read the rest in run_python; "
                 f"detail=transform lists the layout only.")
    return ToolResult(text)


def inspect_api(path: str = "", search: str = "") -> ToolResult:
    from . import api_docs
    try:
        return ToolResult(api_docs.inspect_api(path, search))
    except api_docs.ApiError as ex:
        raise ToolError(str(ex)) from ex


_VIEW_EULERS = {  # Degrees, as a viewport rotation.
    "three_quarter": (62.0, 0.0, 38.0),
    "front": (90.0, 0.0, 0.0),
    "side": (90.0, 0.0, 90.0),
    "top": (0.0, 0.0, 0.0),
}
SHEET_ANGLES = ("three_quarter", "front", "side", "top")  # The tiles of a contact sheet, reading order.
SHEET_TILE = (CAPTURE_WIDTH // 2, CAPTURE_WIDTH * 3 // 8)  # Four 4:3 tiles make one capture-sized image.
_FRAME_MARGIN = 1.15
LABEL_LIMIT = 12       # Names drawn onto a capture, nearest objects first; more would cover the picture.
LABEL_SIZE = 12        # Font size in pixels at 640 wide.
_HIDDEN_TYPES = {"CAMERA", "LIGHT", "EMPTY", "SPEAKER", "LIGHT_PROBE"}


def _has_geometry_bounds(obj) -> bool:
    if obj.type in _HIDDEN_TYPES | {"ARMATURE", "LATTICE"}:
        return False
    corners = obj.bound_box
    # Blender uses eight identical (-1, -1, -1) corners when no box is available.
    return any(tuple(corner) != tuple(corners[0]) for corner in corners[1:])


def _framing_targets(objects) -> list:
    """Geometry-free parents stand for their visible descendants, never their origin/display box."""
    targets = {}
    for obj in objects:
        if _has_geometry_bounds(obj):
            targets[obj.name] = obj
        else:
            for child in obj.children_recursive:
                if child.visible_get() and _has_geometry_bounds(child):
                    targets[child.name] = child
    return list(targets.values())


def _bounding_sphere(objects) -> tuple["Vector", float]:
    from mathutils import Vector
    targets = _framing_targets(objects)
    if not targets:
        raise ToolError("Nothing visible to frame.")
    corners = [obj.matrix_world @ Vector(corner) for obj in targets for corner in obj.bound_box]
    low = Vector(min(c[i] for c in corners) for i in range(3))
    high = Vector(max(c[i] for c in corners) for i in range(3))
    center = (low + high) / 2
    return center, max((c - center).length for c in corners)


def _perspective(half_fov_x: float, width: int, height: int, near: float, far: float) -> "Matrix":
    """A projection like the viewport's: the lens applies to the image width."""
    from mathutils import Matrix
    f = 1.0 / math.tan(half_fov_x)
    return Matrix(((f, 0.0, 0.0, 0.0),
                   (0.0, f * width / height, 0.0, 0.0),
                   (0.0, 0.0, (far + near) / (near - far), 2.0 * far * near / (near - far)),
                   (0.0, 0.0, -1.0, 0.0)))


def _framed_view(space, objects, angle: str, width: int, height: int) -> tuple["Matrix", "Matrix", "Vector"]:
    """View and projection matrices that show `objects` whole from `angle`, and the eye position.
    The user's viewport is not touched: the matrices go straight to the offscreen draw."""
    from mathutils import Euler, Matrix
    center, radius = _bounding_sphere(objects)
    half_fov_x = math.atan(36.0 / space.lens)
    # The shorter image side sees less; fit the sphere into that.
    half_fov_short = math.atan(math.tan(half_fov_x) * min(width, height) / max(width, height))
    distance = max(radius, 0.01) / math.sin(half_fov_short) * _FRAME_MARGIN
    rotation = Euler([math.radians(a) for a in _VIEW_EULERS[angle]]).to_matrix().to_4x4()
    camera = Matrix.Translation(center) @ rotation @ Matrix.Translation((0.0, 0.0, distance))
    far = max(space.clip_end, distance + radius * 2)
    return camera.inverted(), _perspective(half_fov_x, width, height, space.clip_start, far), camera.translation


def _to_pixel(view, projection, point, width: int, height: int):
    """Image position of a world point, or None when it is behind the eye or outside the image."""
    from mathutils import Vector
    clip = projection @ view @ Vector((*point, 1.0))
    if clip.w <= 0:
        return None
    x, y = (clip.x / clip.w * 0.5 + 0.5) * width, (clip.y / clip.w * 0.5 + 0.5) * height
    return (x, y) if 0 <= x < width and 0 <= y < height else None


def _labels(objects, eye, view, projection, width: int, height: int) -> list[tuple[str, float, float]]:
    """(name, x, y) for the nearest LABEL_LIMIT objects whose centre is in the image, skipping a
    label that would sit on one already placed. Pixel coordinates from the bottom-left."""
    import blf
    blf.size(0, LABEL_SIZE)
    placed, boxes = [], []
    by_distance = sorted(objects, key=lambda o: (_bounding_sphere([o])[0] - eye).length)
    for obj in by_distance:
        at = _to_pixel(view, projection, _bounding_sphere([obj])[0], width, height)
        if at is None:
            continue
        w, h = blf.dimensions(0, obj.name)
        box = (at[0], at[1], at[0] + w + 8, at[1] + h + 6)
        if any(not (box[2] < b[0] or b[2] < box[0] or box[3] < b[1] or b[3] < box[1]) for b in boxes):
            continue
        boxes.append(box)
        placed.append((obj.name, at[0], at[1]))
        if len(placed) == LABEL_LIMIT:
            break
    return placed


def _draw_text(width: int, height: int, labels: list[tuple[str, float, float]]) -> None:
    """Draw name tags into the bound offscreen buffer, in pixel space."""
    import blf
    import gpu
    from gpu_extras.batch import batch_for_shader
    from mathutils import Matrix
    with gpu.matrix.push_pop():
        gpu.matrix.load_matrix(Matrix.Identity(4))
        gpu.matrix.load_projection_matrix(Matrix(((2.0 / width, 0.0, 0.0, -1.0), (0.0, 2.0 / height, 0.0, -1.0),
                                                  (0.0, 0.0, -1.0, 0.0), (0.0, 0.0, 0.0, 1.0))))
        gpu.state.blend_set("ALPHA")
        shader = gpu.shader.from_builtin("UNIFORM_COLOR")
        shader.uniform_float("color", (0.0, 0.0, 0.0, 0.65))
        blf.size(0, LABEL_SIZE)
        for text, x, y in labels:
            w, h = blf.dimensions(0, text)
            x1, y1 = x + w + 8, y + h + 6
            batch_for_shader(shader, "TRIS", {"pos": [(x, y), (x1, y), (x1, y1), (x, y), (x1, y1), (x, y1)]}).draw(shader)
        blf.color(0, 1.0, 1.0, 1.0, 1.0)
        for text, x, y in labels:
            blf.position(0, x + 4, y + 3, 0.0)
            blf.draw(0, text)
        gpu.state.blend_set("NONE")


def _draw_offscreen(scene, space, region, view, projection, width: int, height: int,
                    labels: list[tuple[str, float, float]]):
    """The scene as the viewport would draw it with `space`'s shading, into a buffer of our own:
    rows bottom-up, RGBA in 0..1, display colours. Nothing of the user's is touched, and
    Blender's Render Result is not written."""
    import gpu
    import numpy as np
    offscreen = gpu.types.GPUOffScreen(width, height, format="RGBA8")
    try:
        with offscreen.bind():
            gpu.state.active_framebuffer_get().clear(color=(0.0, 0.0, 0.0, 1.0))
            offscreen.draw_view3d(scene, bpy.context.view_layer, space, region, view, projection,
                                  do_color_management=True, draw_background=True)
            if labels:
                _draw_text(width, height, labels)
            pixels = np.asarray(offscreen.texture_color.read())
    finally:
        offscreen.free()
    return pixels.reshape(height, width, 4).astype(np.float32) / 255.0


def _nearest_first(eye, objects) -> str:
    """Two objects of a similar color overlapping in an image do not show which is in front; say it."""
    ranked = sorted(((_bounding_sphere([o])[0] - eye).length, o.name) for o in objects)
    listed = ", ".join(f"{name} {distance:.1f} m" for distance, name in ranked[:12])
    return f" Nearest to the viewpoint first: {listed}." if len(ranked) > 1 else ""


def capture_viewport(focus: list[str] | None = None, angle: str = "three_quarter",
                     style: str = "material", labels: bool = False) -> ToolResult:
    """labels: object names drawn on the image. Off by default: on a close shot they cover the subject."""
    if angle == "user":
        return _capture_viewport(focus, angle, style, labels)
    with _editor_context("VIEW_3D"):
        return _capture_viewport(focus, angle, style, labels)


def capture_viewport_tool(focus: list[str] | None = None, angle: str = "three_quarter",
                          style: str = "material", labels: bool = False) -> ToolResult:
    """The capture_viewport tool: capture_viewport, after the shaders it needs have compiled off
    the main thread (shaders.before_look). Called directly, capture_viewport compiles in the draw."""
    from . import shaders
    return shaders.before_look(lambda: capture_viewport(focus, angle, style, labels), style, angle, focus)


def _capture_viewport(focus: list[str] | None, angle: str, style: str, labels: bool = False) -> ToolResult:
    import numpy as np
    angles = [*_VIEW_EULERS, "camera", "user", "sheet"]
    if angle not in angles:
        raise ToolError(f"angle must be one of {', '.join(angles)}")
    if style not in ("material", "distinct", "rendered"):
        raise ToolError("style must be material, distinct or rendered")
    if not isinstance(labels, bool):
        raise ToolError("labels must be true or false")
    scene = bpy.context.scene
    if angle == "camera" and scene.camera is None:
        raise ToolError("The scene has no active camera (scene.camera is None).")
    override = _view3d_override()
    region, space = override["region"], override["area"].spaces.active
    rv3d = space.region_3d
    framed = angle in _VIEW_EULERS or angle == "sheet"

    visible = [o for o in scene.objects if o.visible_get() and _has_geometry_bounds(o)]
    if focus and framed:
        missing = [name for name in focus if name not in bpy.data.objects]
        if missing:
            raise ToolError(f"No such object(s): {', '.join(missing)}")
        targets = _framing_targets(bpy.data.objects[name] for name in focus)
    else:
        targets = visible
    if framed and not targets:
        raise ToolError("Nothing visible to frame.")

    shading = space.shading
    saved_view = (shading.type, shading.color_type, shading.light, space.overlay.show_overlays)
    saved_lighting = (shading.use_scene_lights, shading.use_scene_world,
                      shading.use_scene_lights_render, shading.use_scene_world_render)
    saved_engine = scene.render.engine
    out_dir = scratch.folder()
    out_dir.mkdir(exist_ok=True)
    path = out_dir / "viewport.png"
    path.unlink(missing_ok=True)
    try:
        if angle != "user":
            # The user's view is shown as is; every other look uses our shading and no overlays,
            # which the offscreen draw would otherwise include: grid, outlines, gizmos.
            if style == "distinct":
                shading.type, shading.color_type, shading.light = "SOLID", "RANDOM", "STUDIO"
            else:
                if style == "rendered":
                    # Cycles viewport draws accumulate asynchronously. A single offscreen draw
                    # returns a black buffer, not a lighting diagnosis. EEVEE draws synchronously.
                    scene.render.engine = "BLENDER_EEVEE"
                    shading.use_scene_lights_render = shading.use_scene_world_render = True
                else:
                    # rna_space.cc:rna_3DViewShading_type_itemf (1555-1565) offers MATERIAL
                    # for every engine except Workbench; no engine capability flag is checked.
                    # Use EEVEE temporarily to preserve the requested studio-lit materials,
                    # rather than Solid's viewport colours. The finally restores the engine.
                    if scene.render.engine == "BLENDER_WORKBENCH":
                        scene.render.engine = "BLENDER_EEVEE"
                    shading.use_scene_lights = shading.use_scene_world = False
                shading.type = "RENDERED" if style == "rendered" else "MATERIAL"
            space.overlay.show_overlays = False
        if angle == "sheet":
            tile_w, tile_h = SHEET_TILE
            tiles = []
            for tile_angle in SHEET_ANGLES:
                view, projection, tile_eye = _framed_view(space, targets, tile_angle, tile_w, tile_h)
                names = _labels(visible, tile_eye, view, projection, tile_w, tile_h) if labels else []
                tiles.append(_draw_offscreen(scene, space, region, view, projection, tile_w, tile_h,
                                             [(tile_angle.replace("_", " "), 4.0, tile_h - LABEL_SIZE - 10.0), *names]))
            # Rows are bottom-up: the first two tiles go on the upper row.
            pixels = np.concatenate([np.concatenate(tiles[2:], axis=1), np.concatenate(tiles[:2], axis=1)], axis=0)
            eye = _framed_view(space, targets, "three_quarter", tile_w, tile_h)[2]
        else:
            if framed:
                width, height = CAPTURE_WIDTH, max(1, round(CAPTURE_WIDTH * region.height / max(1, region.width)))
                view, projection, eye = _framed_view(space, targets, angle, width, height)
            elif angle == "camera":
                # The camera's own frame at its own aspect, so "in frame" means in the image.
                render = scene.render
                width = CAPTURE_WIDTH
                height = max(1, round(CAPTURE_WIDTH * render.resolution_y / max(1, render.resolution_x)))
                camera = scene.camera
                view = camera.matrix_world.inverted()
                projection = camera.calc_matrix_camera(bpy.context.evaluated_depsgraph_get(), x=width, y=height)
                eye = camera.matrix_world.translation
            else:
                width, height = CAPTURE_WIDTH, max(1, round(CAPTURE_WIDTH * region.height / max(1, region.width)))
                view, projection = rv3d.view_matrix.copy(), rv3d.window_matrix.copy()
                eye = rv3d.view_matrix.inverted().translation
            names = _labels(visible, eye, view, projection, width, height) if labels else []
            pixels = _draw_offscreen(scene, space, region, view, projection, width, height, names)
    finally:
        (shading.type, shading.color_type, shading.light, space.overlay.show_overlays) = saved_view
        (shading.use_scene_lights, shading.use_scene_world,
         shading.use_scene_lights_render, shading.use_scene_world_render) = saved_lighting
        scene.render.engine = saved_engine
    _save_pixels(pixels, path)
    # Everything visible, not only what was framed: an object that was not asked for is
    # exactly the one that turns up in front of the subject.
    order = _nearest_first(eye.copy(), visible)
    if angle == "camera":
        what = f"the view through {scene.camera.name}; the image edges are the camera frame"
    elif angle == "user":
        what = "the user's current view"
    elif angle == "sheet":
        what = f"four views of {', '.join(o.name for o in targets[:12])}: three quarter, front (top row), side, top (bottom row)"
    else:
        what = f"{angle} view of {', '.join(o.name for o in targets[:12])}"
    if labels:
        what += "; objects are labelled with their names"
    if angle != "user":
        if style == "rendered":
            what += f"; EEVEE lighting preview (final scene engine: {saved_engine}), not a final render"
        elif style == "material":
            what += "; studio lighting, not the scene's lights or world"
    # Provenance: how the image was drawn, so no one mistakes a viewport draw for a render.
    drawn = {"material": "Material Preview", "distinct": "Solid, a random colour per object",
             "rendered": "Rendered (EEVEE)"}[style] if angle != "user" else "as the user has it"
    what += (f". Drawn: the viewport, {drawn}, {scene.view_settings.view_transform} view, "
             f"{pixels.shape[1]}x{pixels.shape[0]} px")
    return ToolResult(f"Image attached: {what}.{order}", image_path=path)


# ------------------------------------------------------------------ reference images

CROP_MAX_SIDE = 1024   # A crop is a look at a detail; a whole stored reference is up to 2048.
COMPARE_GAP = 8        # Pixels between the two halves of a comparison.


def _reference(name: str) -> tuple[dict, Path]:
    from . import conversations, state
    session = state.session()
    references = session.get("references") or []
    wanted = name.strip().lower()
    found = next((r for r in references if r["name"].lower() == wanted), None) or \
        next((r for r in references if wanted and wanted in r["name"].lower()), None)
    if found is None:
        names = ", ".join(r["name"] for r in references) or "none"
        raise ToolError(f"No attached image called {name!r}. Attached: {names}.")
    path = conversations.image_path(session["id"], found.get("full") or found["ref"])
    if not path.is_file():
        raise ToolError(f"The image {found['name']} is no longer on disk.")
    return found, path


def _pixels(path: Path, height: int | None = None):
    """Rows bottom-up, as Blender keeps them; scaled to `height` when given."""
    import numpy as np
    image = bpy.data.images.load(str(path), check_existing=False)
    try:
        width, current = image.size
        if height and current != height:
            image.scale(max(1, round(width * height / current)), height)
            width, current = image.size
        pixels = np.empty(width * current * 4, dtype=np.float32)
        image.pixels.foreach_get(pixels)
        return pixels.reshape(current, width, 4)
    finally:
        bpy.data.images.remove(image)  # Nothing may stay in the user's file.


def _save_pixels(pixels, path: Path, max_side: int | None = None) -> tuple[int, int]:
    height, width = pixels.shape[:2]
    image = bpy.data.images.new("loopcut_tool_image", width, height, alpha=True)
    try:
        image.pixels.foreach_set(pixels.astype("float32").ravel())
        if max_side and max(width, height) > max_side:
            image.scale(max(1, round(width * max_side / max(width, height))),
                        max(1, round(height * max_side / max(width, height))))
        image.filepath_raw = str(path)
        image.file_format = "PNG"
        image.save()
        return tuple(image.size)
    finally:
        bpy.data.images.remove(image)


ALIKE_CHANNEL = 8 / 255   # A channel must move at least this much for a pixel to count as changed...
ALIKE_FRACTION = 0.002    # ...and this share of pixels must change for two looks to differ.


def images_alike(a: Path, b: Path) -> bool:
    """Whether two captures show the same picture, to the eye: same size, and nearly no pixel
    differs by more than a shade. The agent's automatic look is dropped when it would only
    repeat the previous one."""
    import numpy as np
    first, second = _pixels(a), _pixels(b)
    if first.shape != second.shape:
        return False
    changed = (np.abs(first[..., :3] - second[..., :3]) > ALIKE_CHANNEL).any(axis=-1)
    return float(changed.mean()) < ALIKE_FRACTION


def crop(pixels, region: list | None):
    """(pixels, what they show): region [x0, y0, x1, y1], fractions from the top-left, of rows kept
    bottom-up as Blender keeps them; the whole image for None."""
    if region is None:
        return pixels, "the whole image"
    height, width = pixels.shape[:2]
    try:
        x0, y0, x1, y1 = (float(v) for v in region)
    except (TypeError, ValueError) as ex:
        raise ToolError("region must be four numbers [x0, y0, x1, y1] between 0 and 1") from ex
    if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
        raise ToolError("region must be [x0, y0, x1, y1] with 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1")
    top_down = pixels[::-1]
    rows, cols = slice(round(y0 * height), max(round(y0 * height) + 8, round(y1 * height))), \
        slice(round(x0 * width), max(round(x0 * width) + 8, round(x1 * width)))
    return top_down[rows, cols][::-1], f"region x {x0:.2f}-{x1:.2f}, y {y0:.2f}-{y1:.2f} from the top-left"


def look_at_reference(name: str, region: list | None = None, frame: int | None = None) -> ToolResult:
    """frame: a clip frame of an attached clip (video_attach.py offers it while there is one)."""
    found, path = _reference(name)
    title = found["name"]
    if frame is not None:
        from . import video_attach
        path, _, title = video_attach.frame_for(found, frame)
    pixels = _pixels(path)
    height, width = pixels.shape[:2]
    pixels, what = crop(pixels, region)
    out = scratch.folder() / "reference.png"
    out.parent.mkdir(exist_ok=True)
    shown = _save_pixels(pixels, out, CROP_MAX_SIDE)
    return ToolResult(f"Image attached: {title}, {what}, {shown[0]}x{shown[1]} px "
                      f"(the full image is {width}x{height}).", image_path=out)


def compare_with_reference_tool(name: str, focus: list[str] | None = None, angle: str = "three_quarter",
                                style: str = "material", frame: int | None = None,
                                labels: bool = False) -> ToolResult:
    """The compare_with_reference tool; its capture waits for shaders like capture_viewport_tool."""
    from . import shaders
    found, _ = _reference(name)  # An unknown reference fails before any shader work.
    if frame is not None:
        from . import video_attach
        video_attach.frame_for(found, frame)  # So does a clip frame that is not there.
    return shaders.before_look(lambda: compare_with_reference(name, focus, angle, style, frame, labels),
                               style, angle, focus, name="the compare_with_reference tool")


def compare_with_reference(name: str, focus: list[str] | None = None, angle: str = "three_quarter",
                           style: str = "material", frame: int | None = None, labels: bool = False) -> ToolResult:
    """frame: a clip frame of an attached clip, beside the scene captured at the scene frame showing it
    (video_attach.scene_frame); the scene goes back to its own frame after. labels as capture_viewport's:
    off by default, since the reference beside it has none."""
    import numpy as np
    found, path = _reference(name)
    title = f"the reference {found['name']}"
    if frame is None:
        shot = capture_viewport(focus, angle, style, labels)
    else:
        from . import video_attach
        path, at, title = video_attach.frame_for(found, frame)
        scene = bpy.context.scene
        before = scene.frame_current
        scene.frame_set(at)
        try:
            shot = capture_viewport(focus, angle, style, labels)
        finally:
            scene.frame_set(before)
        title += f" (scene frame {at})"
    right = _pixels(shot.image_path)
    height = right.shape[0]
    left = _pixels(path, height)
    canvas = np.ones((height, left.shape[1] + COMPARE_GAP + right.shape[1], 4), dtype=np.float32)
    canvas[:, :left.shape[1]] = left
    canvas[:, left.shape[1] + COMPARE_GAP:] = right
    out = scratch.folder() / "compare.png"
    _save_pixels(canvas, out)
    return ToolResult(f"Image attached: {title} on the left, {shot.text[len('Image attached: '):]}"
                      f" on the right, at the same height.", image_path=out)


_DISPATCH = {
    "run_python": run_python,
    "get_scene_info": get_scene_info,
    "get_object_info": get_object_info,
    "inspect_api": inspect_api,
    "capture_viewport": capture_viewport_tool,
    "compare_with_reference": compare_with_reference_tool,
    "look_at_reference": look_at_reference,
}


def forward(fn, arguments: dict, what: str) -> ToolResult:
    """Call fn, the implementation behind a merged tool's `what` (a source, an action), with the
    arguments that tool was given, naming in any complaint the parameters the model sees."""
    parameters = inspect.signature(fn).parameters
    extra = [key for key in arguments if key not in parameters]
    if extra:
        raise ToolError(f"{what} does not take {', '.join(extra)}")
    missing = [key for key, p in parameters.items() if p.default is p.empty and key not in arguments]
    if missing:
        raise ToolError(f"{what} needs {', '.join(missing)}")
    return fn(**arguments)


def execute(name: str, arguments_json: str) -> ToolResult:
    from . import cloud
    if cloud.is_tool(name):
        try:
            arguments = json.loads(arguments_json) if arguments_json.strip() else {}
        except ValueError:
            return ToolResult("Invalid cloud tool arguments: use a JSON object.", ok=False)
        if not isinstance(arguments, dict):
            return ToolResult("Invalid cloud tool arguments: use a JSON object.", ok=False)
        return cloud.execute(name, arguments)
    from . import (assets, camera, contact, files, inspection, job_tools, orchestrator, paint_tools, texture_tools,
                   uv_tools, video_attach, workspace)
    tables = (_DISPATCH, contact.DISPATCH, files.DISPATCH, job_tools.DISPATCH, inspection.DISPATCH, assets.DISPATCH,
              camera.DISPATCH, orchestrator.DISPATCH, uv_tools.DISPATCH, texture_tools.DISPATCH, paint_tools.DISPATCH,
              workspace.DISPATCH, video_attach.DISPATCH)
    fn = next((table[name] for table in tables if name in table), None)
    if fn is None:
        return ToolResult(f"Unknown tool {name!r}. Available: {', '.join(n for table in tables for n in table)}", ok=False)
    try:
        arguments = json.loads(arguments_json) if arguments_json.strip() else {}
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be a JSON object")
    except ValueError as ex:
        return ToolResult(f"Invalid tool arguments: {ex}", ok=False, plan_refused=name == "delegate")
    try:
        inspect.signature(fn).bind(**arguments)
    except TypeError as ex:
        return ToolResult(f"Bad arguments for {name}: {ex}", ok=False, plan_refused=name == "delegate")
    try:
        return fn(**arguments)
    except TypeError as ex:
        # From inside the tool, not the model's arguments: reported as "Bad arguments", it sent the
        # model looking for other arguments (get_object_info on a Blender 5 modifier, 2026-09-19).
        return ToolResult(f"{name} failed inside the tool, not because of your arguments: TypeError: {ex}", ok=False)
    except ToolError as ex:
        return ToolResult(str(ex), ok=False)
