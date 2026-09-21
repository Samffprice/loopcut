"""The agent's tools. Every function here runs on Blender's main thread."""

import contextlib
import io
import json
import math
import re
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

import bpy

from . import scratch

MAX_OUTPUT_CHARS = 8000
CAPTURE_WIDTH = 640  # Image tokens scale with pixels; 640 still shows shape, placement and contact.
STRIP_HEIGHT = 160   # A frame of the progress strip: enough to see what moved, a fraction of a capture's pixels.
STRIP_GAP = 6
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
    evidence_token: dict | None = None  # Rechecked if later tools in the same batch change the scene.


SCHEMAS = [  # Sent with every request: every word here is paid for on every step.
    {"type": "function", "function": {
        "name": "run_python",
        "description": (
            "Run Python in the live Blender session; `bpy` is imported and bpy.ops have a 3D viewport "
            "context (or another editor's, see `editor`). Prefer the data API over bpy.ops. Returns print "
            "output, any traceback, and `Scene changes`: what really changed, measured from the scene. "
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
        "description": (
            "The scene as JSON, in as much detail as fits. Always: mode, frame, camera, counts by type and "
            "by collection. Then one row per matching object (type, location, bounds, materials, modifiers, "
            "mesh size; zero rotation, unit scale, visible and unselected are left out) when they fit, else "
            "names by collection: then narrow with name_contains, type or collection, or ask detail=rows. "
            "`bounds` is the world-space box the geometry occupies: judge placement and contact from it. "
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
        "description": (
            "Full setup of up to 5 objects: modifier settings, material node trees (nodes, values, "
            "links), geometry-nodes inputs, constraints, animation, children, custom properties. Read "
            "before changing an existing setup."),
        "parameters": {"type": "object", "properties": {
            "names": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 5,
                      "description": "Object names"},
        }, "required": ["names"]},
    }},
    {"type": "function", "function": {
        "name": "inspect_api",
        "description": (
            "The Python API of THIS Blender, installed add-ons included: property names, enum values, "
            "defaults, operator arguments, node sockets. Use before an API you are not sure of, and after any AttributeError, "
            "TypeError or 'enum not found'. `path` like bpy.types.BevelModifier, Object.modifiers, "
            "bpy.ops.mesh.bevel, ShaderNodeTexNoise, bmesh.ops.bevel; or `search` words: 'action fcurves'."),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Dotted path to describe"},
            "search": {"type": "string", "description": "Words to find in names and descriptions"},
        }},
    }},
    {"type": "function", "function": {
        "name": "compare_with_reference",
        "description": (
            "One image: the reference the user attached on the left, a viewport capture on the right at "
            "the same height. Use it after each change when copying a reference, and fix what differs."),
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "File name of the attached image"},
            "focus": {"type": "array", "items": {"type": "string"}, "description": "Objects to frame; default all"},
            "angle": {"type": "string", "enum": ["three_quarter", "front", "side", "top", "camera", "user"],
                      "description": "Default three_quarter; camera looks through the scene camera"},
            "style": {"type": "string", "enum": ["material", "distinct"]},
        }, "required": ["name"]},
    }},
    {"type": "function", "function": {
        "name": "look_at_reference",
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
        "description": (
            "An image of the scene, framed by the tool from a 3/4 angle with materials and object names "
            "drawn on, leaving the user's viewport untouched. Also lists the visible objects nearest "
            "first, which settles what is in front when colors look alike."),
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
        }},
    }},
]

# Tools that can change the scene. A checkpoint is taken before the first one in a turn, and they
# need the user's go-ahead unless auto-run is on.
CHANGES_SCENE = {"run_python", "import_polyhaven", "import_asset", "import_polypizza", "import_blenderkit"}
NEEDS_APPROVAL = CHANGES_SCENE
# Code that renders, bakes or simulates can hold Blender for minutes. Such a step waits for the
# user every time, whatever they have allowed: "Always allow" and auto-run do not cover it.
HEAVY_CODE = re.compile(r"ops\.(?:render\.(?:render|opengl)|object\.bake|cycles\.bake|ptcache\.bake|"
                        r"fluid\.bake|rigidbody\.bake|object\.(?:voxel|quadriflow)_remesh)")


def is_heavy(name: str, arguments: dict) -> bool:
    """A step that renders, bakes or simulates: the user approves it every time."""
    if name == "run_python":
        return bool(HEAVY_CODE.search(str(arguments.get("code", ""))))
    if name in {"start_render_job", "resume_render_job", "inspect_scene"}:
        return True
    return name == "see_render" and bool(arguments.get("render"))


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


@contextlib.contextmanager
def _editor_context(editor: str):
    """bpy.context for one step. The 3D viewport by default; another open editor of the asked
    kind; else the viewport is switched to that editor for the step and switched back, which
    keeps its view and shading. Nothing redraws in between, so the user sees no change."""
    editor = _editor_name(editor) if editor and editor.strip() else "VIEW_3D"
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            region = next((r for r in area.regions if r.type == "WINDOW"), None)
            if region and _editor_matches(area, editor):
                with bpy.context.temp_override(window=window, screen=window.screen, area=area, region=region):
                    yield
                return
    # The user may be in the Sequencer, render viewer, or a workspace without a 3D view.
    # Borrow a regular editor only for this synchronous step and restore its exact UI type.
    candidates = [(w, a) for w in bpy.context.window_manager.windows for a in w.screen.areas
                  if a.type not in ("LOOPCUT", "TOPBAR", "STATUSBAR")
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
    frames = [f for f in traceback.extract_tb(ex.__traceback__) if f.filename == "<loopcut>"]
    return "".join(["Traceback (most recent call last):\n", *traceback.format_list(frames),
                    *traceback.format_exception_only(type(ex), ex)])


def run_python(code: str, summary: str = "", capture: str = "", editor: str = "") -> ToolResult:
    # Running model-written code is the product; the approval gate lives in agent.py.
    from . import config, scene_diff
    stdout = io.StringIO()
    ok = True
    deadline = _Deadline(config.run_timeout())
    before = scene_diff.snapshot()
    with _editor_context(editor):
        bpy.ops.ed.undo_push(message=f"Before Loopcut: {summary or 'run_python'}"[:60])
        previous_trace = sys.gettrace()
        try:
            compiled = compile(code, "<loopcut>", "exec")
            sys.settrace(deadline)
            with contextlib.redirect_stdout(stdout):
                exec(compiled, {"bpy": bpy, "__name__": "__loopcut__"})
        except _RunTimeout:
            ok = False
            stdout.write(f"\nStopped: the code ran for more than {deadline.seconds:g} s, which usually means a "
                         f"loop that never ends. Whatever it changed before that is listed below.")
        except Exception as ex:
            ok = False
            stdout.write("\n" + _model_traceback(ex))
        finally:
            sys.settrace(previous_trace)
    after = scene_diff.snapshot()
    output = _clip(stdout.getvalue().strip()) or "OK (no output)"
    changes = scene_diff.for_model(scene_diff.diff(before, after))
    result = ToolResult(f"{output}\n\n{changes}", ok=ok, scene_before=before, scene_after=after)
    if capture:
        # One request instead of two: the step and the look at what it did.
        try:
            shot = capture_viewport(angle=capture)
            result.text += f"\n\n{shot.text}"
            result.image_path = shot.image_path
        except ToolError as ex:
            result.text += f"\n\nCapture failed: {ex}"
    return result


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
    entry = {
        "name": obj.name,
        "type": obj.type,
        "location": _round(obj.location),
        "rotation_deg": _round([math.degrees(a) for a in obj.rotation_euler], 2),
        "scale": _round(obj.scale),
        "visible": obj.visible_get(),
        "selected": obj.select_get(),
    }
    bounds = _world_bounds(obj)
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
    from . import capabilities
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


def get_object_info(names: list[str]) -> ToolResult:
    from . import object_info
    if not names or len(names) > 5:
        raise ToolError("Pass 1 to 5 object names.")
    missing = [name for name in names if name not in bpy.data.objects]
    if missing:
        close = [o.name for o in bpy.data.objects if any(m.lower() in o.name.lower() for m in missing)][:10]
        raise ToolError(f"No such object(s): {', '.join(missing)}."
                        + (f" Similar names: {', '.join(close)}." if close else ""))
    described = []
    for name in names:
        obj = bpy.data.objects[name]
        described.append({**object_summary(obj), **object_info.describe(obj)})
    return ToolResult(_clip(json.dumps(described, separators=(",", ":"), default=str)))


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


def _bounding_sphere(objects) -> tuple["Vector", float]:
    from mathutils import Vector
    corners = [obj.matrix_world @ Vector(corner) for obj in objects for corner in obj.bound_box]
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
                     style: str = "material") -> ToolResult:
    if angle == "user":
        return _capture_viewport(focus, angle, style)
    with _editor_context("VIEW_3D"):
        return _capture_viewport(focus, angle, style)


def _capture_viewport(focus: list[str] | None, angle: str, style: str) -> ToolResult:
    import numpy as np
    angles = [*_VIEW_EULERS, "camera", "user", "sheet"]
    if angle not in angles:
        raise ToolError(f"angle must be one of {', '.join(angles)}")
    if style not in ("material", "distinct", "rendered"):
        raise ToolError("style must be material, distinct or rendered")
    scene = bpy.context.scene
    if angle == "camera" and scene.camera is None:
        raise ToolError("The scene has no active camera (scene.camera is None).")
    override = _view3d_override()
    region, space = override["region"], override["area"].spaces.active
    rv3d = space.region_3d
    framed = angle in _VIEW_EULERS or angle == "sheet"

    visible = [o for o in scene.objects if o.visible_get() and o.type not in _HIDDEN_TYPES]
    if focus and framed:
        missing = [name for name in focus if name not in bpy.data.objects]
        if missing:
            raise ToolError(f"No such object(s): {', '.join(missing)}")
        targets = [bpy.data.objects[name] for name in focus]
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
                    shading.use_scene_lights = shading.use_scene_world = False
                shading.type = "RENDERED" if style == "rendered" else "MATERIAL"
            space.overlay.show_overlays = False
        if angle == "sheet":
            tile_w, tile_h = SHEET_TILE
            tiles = []
            for tile_angle in SHEET_ANGLES:
                view, projection, _ = _framed_view(space, targets, tile_angle, tile_w, tile_h)
                tiles.append(_draw_offscreen(scene, space, region, view, projection, tile_w, tile_h,
                                             [(tile_angle.replace("_", " "), 4.0, tile_h - LABEL_SIZE - 10.0)]))
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
            labels = _labels(visible, eye, view, projection, width, height) if angle != "user" else []
            pixels = _draw_offscreen(scene, space, region, view, projection, width, height, labels)
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
    if angle != "user":
        what += "; objects are labelled with their names"
        if style == "rendered":
            what += f"; EEVEE lighting preview (final scene engine: {saved_engine}), not a final render"
        elif style == "material":
            what += "; studio lighting, not the scene's lights or world"
    return ToolResult(f"Image attached: {what}.{order}", image_path=path)


# ------------------------------------------------------------------ reference images

CROP_MAX_SIDE = 1024   # A crop is a look at a detail; a whole reference at full size is up to 1568.
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


def progress_strip(paths: list) -> "Path | None":
    """The captures at `paths` side by side, oldest first, each STRIP_HEIGHT tall: the model's
    earlier looks as one small image. None when none of the files is there any more."""
    import numpy as np
    frames = [_pixels(path, height=STRIP_HEIGHT) for path in paths if Path(path).is_file()]
    if not frames:
        return None
    width = sum(frame.shape[1] for frame in frames) + STRIP_GAP * (len(frames) - 1)
    canvas = np.zeros((STRIP_HEIGHT, width, 4), dtype=np.float32)
    canvas[..., :3], canvas[..., 3] = 0.15, 1.0  # A dark gap: the frames read as separate pictures.
    x = 0
    for frame in frames:
        canvas[:, x:x + frame.shape[1]] = frame
        x += frame.shape[1] + STRIP_GAP
    out = scratch.folder() / "strip.png"
    out.parent.mkdir(exist_ok=True)
    _save_pixels(canvas, out)
    return out


def look_at_reference(name: str, region: list | None = None) -> ToolResult:
    found, path = _reference(name)
    pixels = _pixels(path)
    height, width = pixels.shape[:2]
    if region is not None:
        try:
            x0, y0, x1, y1 = (float(v) for v in region)
        except (TypeError, ValueError) as ex:
            raise ToolError("region must be four numbers [x0, y0, x1, y1] between 0 and 1") from ex
        if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
            raise ToolError("region must be [x0, y0, x1, y1] with 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1")
        top_down = pixels[::-1]
        rows, cols = slice(round(y0 * height), max(round(y0 * height) + 8, round(y1 * height))), \
            slice(round(x0 * width), max(round(x0 * width) + 8, round(x1 * width)))
        pixels = top_down[rows, cols][::-1]
        what = f"region x {x0:.2f}-{x1:.2f}, y {y0:.2f}-{y1:.2f} from the top-left"
    else:
        what = "the whole image"
    out = scratch.folder() / "reference.png"
    out.parent.mkdir(exist_ok=True)
    shown = _save_pixels(pixels, out, CROP_MAX_SIDE)
    return ToolResult(f"Image attached: {found['name']}, {what}, {shown[0]}x{shown[1]} px "
                      f"(the full image is {width}x{height}).", image_path=out)


def compare_with_reference(name: str, focus: list[str] | None = None, angle: str = "three_quarter",
                           style: str = "material") -> ToolResult:
    import numpy as np
    found, path = _reference(name)
    shot = capture_viewport(focus, angle, style)
    right = _pixels(shot.image_path)
    height = right.shape[0]
    left = _pixels(path, height)
    canvas = np.ones((height, left.shape[1] + COMPARE_GAP + right.shape[1], 4), dtype=np.float32)
    canvas[:, :left.shape[1]] = left
    canvas[:, left.shape[1] + COMPARE_GAP:] = right
    out = scratch.folder() / "compare.png"
    _save_pixels(canvas, out)
    return ToolResult(f"Image attached: the reference {found['name']} on the left, {shot.text[len('Image attached: '):]}"
                      f" on the right, at the same height.", image_path=out)


_DISPATCH = {
    "run_python": run_python,
    "get_scene_info": get_scene_info,
    "get_object_info": get_object_info,
    "inspect_api": inspect_api,
    "capture_viewport": capture_viewport,
    "compare_with_reference": compare_with_reference,
    "look_at_reference": look_at_reference,
}


def execute(name: str, arguments_json: str) -> ToolResult:
    from . import asset_libraries, blenderkit, files, inspection, job_tools, polyhaven, polypizza
    tables = (_DISPATCH, files.DISPATCH, job_tools.DISPATCH, inspection.DISPATCH, polyhaven.DISPATCH,
              asset_libraries.DISPATCH, polypizza.DISPATCH, blenderkit.DISPATCH)
    fn = next((table[name] for table in tables if name in table), None)
    if fn is None:
        return ToolResult(f"Unknown tool {name!r}. Available: {', '.join(n for table in tables for n in table)}", ok=False)
    try:
        arguments = json.loads(arguments_json) if arguments_json.strip() else {}
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be a JSON object")
    except ValueError as ex:
        return ToolResult(f"Invalid tool arguments: {ex}", ok=False)
    try:
        return fn(**arguments)
    except TypeError as ex:
        return ToolResult(f"Bad arguments for {name}: {ex}", ok=False)
    except ToolError as ex:
        return ToolResult(str(ex), ok=False)
