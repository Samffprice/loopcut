"""The agent's tools. Every function here runs on Blender's main thread."""

import contextlib
import io
import json
import math
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

import bpy

MAX_OUTPUT_CHARS = 8000
CAPTURE_WIDTH = 640  # Image tokens scale with pixels; 640 still shows shape, placement and contact.
# Above this many objects get_scene_info lists names only; details come from get_object_info.
FULL_DETAIL_OBJECTS = 40
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


SCHEMAS = [  # Sent with every request: every word here is paid for on every step.
    {"type": "function", "function": {
        "name": "run_python",
        "description": (
            "Run Python in the live Blender session; `bpy` is imported and bpy.ops have a 3D viewport "
            "context. Prefer the data API over bpy.ops. Returns print output, any traceback, and "
            "`Scene changes`: what really changed, measured from the scene. One undo step per call."),
        "parameters": {"type": "object", "properties": {
            "code": {"type": "string", "description": "Python source to execute"},
            "summary": {"type": "string", "description": "A few words on what this does, shown to the user"},
            "capture": {"type": "string", "enum": ["three_quarter", "front", "side", "top", "camera"],
                        "description": "Also return a viewport capture afterwards, framed like "
                                       "capture_viewport. Cheaper than a separate call."},
        }, "required": ["code", "summary"]},
    }},
    {"type": "function", "function": {
        "name": "get_scene_info",
        "description": (
            "The scene as JSON: objects with type, transforms, materials, modifiers, selection, mode and "
            "frame range. `bounds` is the world-space box the geometry occupies: judge placement and "
            "contact from it. `location` is only the origin, relative to a parent, and can sit far from "
            "the geometry. Large scenes list names by collection; narrow with name_contains or type."),
        "parameters": {"type": "object", "properties": {
            "name_contains": {"type": "string", "description": "Only objects whose name contains this"},
            "type": {"type": "string", "description": "Only this object type, e.g. MESH, LIGHT, CAMERA"},
        }},
    }},
    {"type": "function", "function": {
        "name": "get_object_info",
        "description": (
            "Full setup of up to 5 objects: modifier settings, material node trees (nodes, values, "
            "links), geometry-nodes inputs, constraints, animation, children, custom properties. Read "
            "before changing an existing setup."),
        "parameters": {"type": "object", "properties": {
            "names": {"type": "array", "items": {"type": "string"}, "description": "Object names"},
        }, "required": ["names"]},
    }},
    {"type": "function", "function": {
        "name": "inspect_api",
        "description": (
            "The Python API of THIS Blender: property names, enum values, defaults, operator arguments, "
            "node sockets. Use before an API you are not sure of, and after any AttributeError, "
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
            "An image of the scene, framed by the tool from a 3/4 angle with materials, leaving the "
            "user's viewport untouched. Also lists the framed objects nearest first, which settles what "
            "is in front when colors look alike."),
        "parameters": {"type": "object", "properties": {
            "focus": {"type": "array", "items": {"type": "string"},
                      "description": "Objects to frame; default everything visible"},
            "angle": {"type": "string", "enum": ["three_quarter", "front", "side", "top", "camera", "user"],
                      "description": "Default three_quarter. camera: through the scene camera. user: the "
                                     "user's current view. Both ignore focus."},
            "style": {"type": "string", "enum": ["material", "distinct"],
                      "description": "distinct gives each object a flat color, to judge shape and contact"},
        }},
    }},
]

# Tools that can change the scene. A checkpoint is taken before the first one in a turn, and they
# need the user's go-ahead unless auto-run is on.
CHANGES_SCENE = {"run_python"}
NEEDS_APPROVAL = CHANGES_SCENE


def _view3d_override() -> dict:
    wm = bpy.context.window_manager
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                region = next((r for r in area.regions if r.type == "WINDOW"), None)
                if region:
                    return {"window": window, "screen": window.screen, "area": area, "region": region}
    raise ToolError("No 3D viewport is open; open one and try again.")


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


def run_python(code: str, summary: str = "", capture: str = "") -> ToolResult:
    # Running model-written code is the product; the approval gate lives in agent.py.
    from . import config, scene_diff
    stdout = io.StringIO()
    ok = True
    deadline = _Deadline(config.run_timeout())
    before = scene_diff.snapshot()
    with bpy.context.temp_override(**_view3d_override()):
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


def _rows(entries: list) -> str:
    # One entry per line: compact for the token budget, still valid JSON.
    return "[\n" + ",\n".join(json.dumps(entry, separators=(",", ":")) for entry in entries) + "\n]"


def get_scene_info(name_contains: str = "", type: str = "") -> ToolResult:
    scene = bpy.context.scene
    active = bpy.context.view_layer.objects.active
    chosen = [o for o in scene.objects
              if name_contains.lower() in o.name.lower() and (not type or o.type == type.upper())]
    info = {
        "blender_version": bpy.app.version_string,
        "scene": scene.name,
        "mode": bpy.context.mode,
        "active_object": active.name if active else None,
        "frame": {"current": scene.frame_current, "start": scene.frame_start, "end": scene.frame_end},
        "render_engine": scene.render.engine,
        "unit_system": scene.unit_settings.system,
        "camera": scene.camera.name if scene.camera else None,
        "object_count": len(scene.objects),
    }
    head = json.dumps(info, separators=(",", ":"))[:-1]
    if len(chosen) <= FULL_DETAIL_OBJECTS:
        return ToolResult(_clip(f'{head},"objects":{_rows([object_summary(o) for o in chosen])}}}'))
    # Too many to detail: every name, grouped the way the user organized them, plus full detail
    # for what the user is working on right now.
    by_collection: dict[str, list[str]] = {}
    for obj in chosen:
        for collection in obj.users_collection or [scene.collection]:
            by_collection.setdefault(collection.name, []).append(f"{obj.name} ({obj.type})")
    selected = [object_summary(o) for o in chosen if o.select_get()][:FULL_DETAIL_OBJECTS]
    note = (f"{len(chosen)} objects match, so only names are listed. Narrow with name_contains or "
            f"type, or call get_object_info for the ones that matter.")
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
_FRAME_MARGIN = 1.15


def _bounding_sphere(objects) -> tuple["Vector", float]:
    from mathutils import Vector
    corners = [obj.matrix_world @ Vector(corner) for obj in objects for corner in obj.bound_box]
    low = Vector(min(c[i] for c in corners) for i in range(3))
    high = Vector(max(c[i] for c in corners) for i in range(3))
    center = (low + high) / 2
    return center, max((c - center).length for c in corners)


def _frame_view(space, region, objects, angle: str) -> None:
    from mathutils import Euler
    rv3d = space.region_3d
    center, radius = _bounding_sphere(objects)
    # The lens applies to the longer region side, so the shorter side sees less.
    half_fov = math.atan(36.0 / space.lens)
    aspect = min(region.width, region.height) / max(region.width, region.height)
    half_fov_short = math.atan(math.tan(half_fov) * aspect)
    rv3d.view_perspective = "PERSP"
    rv3d.view_rotation = Euler([math.radians(a) for a in _VIEW_EULERS[angle]]).to_quaternion()
    rv3d.view_location = center
    rv3d.view_distance = max(radius, 0.01) / math.sin(half_fov_short) * _FRAME_MARGIN


def _nearest_first(eye, objects) -> str:
    """Two objects of a similar color overlapping in an image do not show which is in front; say it."""
    ranked = sorted(((_bounding_sphere([o])[0] - eye).length, o.name) for o in objects)
    listed = ", ".join(f"{name} {distance:.1f} m" for distance, name in ranked[:12])
    return f" Nearest to the viewpoint first: {listed}." if len(ranked) > 1 else ""


def capture_viewport(focus: list[str] | None = None, angle: str = "three_quarter",
                     style: str = "material") -> ToolResult:
    angles = [*_VIEW_EULERS, "camera", "user"]
    if angle not in angles:
        raise ToolError(f"angle must be one of {', '.join(angles)}")
    if style not in ("material", "distinct"):
        raise ToolError("style must be material or distinct")
    scene = bpy.context.scene
    if angle == "camera" and scene.camera is None:
        raise ToolError("The scene has no active camera (scene.camera is None).")
    override = _view3d_override()
    region, space = override["region"], override["area"].spaces.active
    rv3d = space.region_3d
    framed = angle in _VIEW_EULERS

    visible = [o for o in scene.objects
               if o.visible_get() and o.type not in {"CAMERA", "LIGHT", "EMPTY", "SPEAKER", "LIGHT_PROBE"}]
    if focus and framed:
        missing = [name for name in focus if name not in bpy.data.objects]
        if missing:
            raise ToolError(f"No such object(s): {', '.join(missing)}")
        targets = [bpy.data.objects[name] for name in focus]
    else:
        targets = visible
    if framed and not targets:
        raise ToolError("Nothing visible to frame.")

    render, shading = scene.render, space.shading
    image_settings = render.image_settings
    saved_render = (render.filepath, render.resolution_x, render.resolution_y,
                    render.resolution_percentage, image_settings.file_format)
    saved_view = (rv3d.view_perspective, rv3d.view_rotation.copy(), rv3d.view_location.copy(),
                  rv3d.view_distance, shading.type, shading.color_type, shading.light,
                  space.overlay.show_overlays, space.use_local_camera)
    out_dir = Path(tempfile.gettempdir()) / "loopcut"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / "viewport.png"
    path.unlink(missing_ok=True)
    try:
        render.filepath = str(path)
        # The camera view is rendered at the camera's own aspect, so "in frame" means in the image.
        aspect = (render.resolution_y / max(1, render.resolution_x) if angle == "camera"
                  else region.height / max(1, region.width))
        render.resolution_x = CAPTURE_WIDTH
        render.resolution_y = max(1, round(CAPTURE_WIDTH * aspect))
        render.resolution_percentage = 100
        image_settings.file_format = "PNG"
        if framed:
            _frame_view(space, region, targets, angle)
        if angle != "user":
            if style == "distinct":
                shading.type, shading.color_type, shading.light = "SOLID", "RANDOM", "STUDIO"
            else:
                shading.type = "MATERIAL"
            # Overlays are composited without depth, so the grid shows through solid objects and
            # makes it impossible to judge contact and occlusion.
            space.overlay.show_overlays = False
        if angle == "camera":
            # In camera view the viewport render is exactly the camera frame, at our resolution.
            rv3d.view_perspective = "CAMERA"
            space.use_local_camera = False
        rv3d.update()
        eye = scene.camera.matrix_world.translation if angle == "camera" else rv3d.view_matrix.inverted().translation
        # Everything visible, not only what was framed: an object that was not asked for is
        # exactly the one that turns up in front of the subject.
        order = _nearest_first(eye.copy(), visible)
        with bpy.context.temp_override(**override):
            bpy.ops.render.opengl(write_still=True, view_context=True)
    finally:
        (render.filepath, render.resolution_x, render.resolution_y,
         render.resolution_percentage, image_settings.file_format) = saved_render
        (rv3d.view_perspective, rv3d.view_rotation, rv3d.view_location, rv3d.view_distance,
         shading.type, shading.color_type, shading.light, space.overlay.show_overlays,
         space.use_local_camera) = saved_view
        rv3d.update()
    if not path.is_file():
        raise ToolError("Viewport capture produced no image.")
    if angle == "camera":
        what = f"the view through {scene.camera.name}; the image edges are the camera frame"
    elif angle == "user":
        what = "the user's current view"
    else:
        what = f"{angle} view of {', '.join(o.name for o in targets[:12])}"
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
    out = Path(tempfile.gettempdir()) / "loopcut" / "reference.png"
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
    out = Path(tempfile.gettempdir()) / "loopcut" / "compare.png"
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
    fn = _DISPATCH.get(name)
    if fn is None:
        return ToolResult(f"Unknown tool {name!r}. Available: {', '.join(_DISPATCH)}", ok=False)
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
