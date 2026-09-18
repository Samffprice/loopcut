"""The agent's tools. Every function here runs on Blender's main thread."""

import contextlib
import io
import json
import math
import tempfile
import traceback
from dataclasses import dataclass
from pathlib import Path

import bpy

MAX_OUTPUT_CHARS = 8000
CAPTURE_WIDTH = 960


class ToolError(Exception):
    """A failure the model should see and can react to."""


@dataclass
class ToolResult:
    text: str
    image_path: Path | None = None
    ok: bool = True


SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": (
                "Execute Python in the running Blender session. `bpy` is imported. Runs with a 3D "
                "viewport context, so bpy.ops work. Prefer the data API (bpy.data, obj.location) over "
                "bpy.ops when both work. print() output and any traceback are returned. Each call is "
                "one undo step."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python source to execute"},
                    "summary": {"type": "string",
                                "description": "A few words on what this does, shown to the user"},
                },
                "required": ["code", "summary"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_scene_info",
            "description": (
                "Objects, transforms, materials, selection, mode and frame range of the current scene, "
                "as JSON. Call this before editing a scene you have not seen. `bounds` is the world-space "
                "box the geometry actually occupies; judge placement and contact from it. `location` is "
                "only the object's origin: it is relative to the parent when there is one, and can sit "
                "far from the geometry (for example at 0,0,0 after transform_apply)."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "capture_viewport",
            "description": (
                "Return an image of the scene so you can check your work. The tool frames the objects "
                "itself from a 3/4 angle with materials shown, and leaves the user's viewport as it was, "
                "so never move the viewport or change shading yourself just to take a capture."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "focus": {"type": "array", "items": {"type": "string"},
                              "description": "Names of objects to frame. Omit to frame everything visible."},
                    "angle": {"type": "string", "enum": ["three_quarter", "front", "side", "top", "user"],
                              "description": "Default three_quarter. 'user' keeps the user's current view "
                                             "unchanged and ignores focus."},
                },
            },
        },
    },
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


def run_python(code: str, summary: str = "") -> ToolResult:
    # Running model-written code is the product; the approval gate lives in agent.py.
    stdout = io.StringIO()
    ok = True
    with bpy.context.temp_override(**_view3d_override()):
        bpy.ops.ed.undo_push(message=f"Before Loopcut: {summary or 'run_python'}"[:60])
        try:
            with contextlib.redirect_stdout(stdout):
                exec(compile(code, "<loopcut>", "exec"), {"bpy": bpy, "__name__": "__loopcut__"})
        except Exception:
            ok = False
            stdout.write("\n" + traceback.format_exc())
    output = stdout.getvalue().strip()
    return ToolResult(_clip(output) or "OK (no output)", ok=ok)


def _round(values, digits=3) -> list:
    return [round(v, digits) for v in values]


def _world_bounds(obj) -> dict | None:
    from mathutils import Vector
    if obj.type in {"CAMERA", "LIGHT", "EMPTY", "SPEAKER", "LIGHT_PROBE", "ARMATURE"}:
        return None
    corners = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
    return {"min": _round([min(c[i] for c in corners) for i in range(3)]),
            "max": _round([max(c[i] for c in corners) for i in range(3)])}


def get_scene_info() -> ToolResult:
    scene = bpy.context.scene
    view_layer = bpy.context.view_layer
    active = view_layer.objects.active
    objects = []
    for obj in scene.objects:
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
        objects.append(entry)
    info = {
        "blender_version": bpy.app.version_string,
        "scene": scene.name,
        "mode": bpy.context.mode,
        "active_object": active.name if active else None,
        "frame": {"current": scene.frame_current, "start": scene.frame_start, "end": scene.frame_end},
        "render_engine": scene.render.engine,
        "unit_system": scene.unit_settings.system,
        "object_count": len(objects),
    }
    # One object per line: compact for the token budget, still valid JSON.
    rows = ",\n".join(json.dumps(entry, separators=(",", ":")) for entry in objects)
    text = json.dumps(info, separators=(",", ":"))[:-1] + ',"objects":[\n' + rows + "\n]}"
    return ToolResult(_clip(text))


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


def capture_viewport(focus: list[str] | None = None, angle: str = "three_quarter") -> ToolResult:
    if angle != "user" and angle not in _VIEW_EULERS:
        raise ToolError(f"angle must be one of {', '.join([*_VIEW_EULERS, 'user'])}")
    override = _view3d_override()
    region, space = override["region"], override["area"].spaces.active
    rv3d = space.region_3d

    if focus:
        missing = [name for name in focus if name not in bpy.data.objects]
        if missing:
            raise ToolError(f"No such object(s): {', '.join(missing)}")
        targets = [bpy.data.objects[name] for name in focus]
    else:
        targets = [o for o in bpy.context.scene.objects
                   if o.visible_get() and o.type not in {"CAMERA", "LIGHT", "EMPTY", "SPEAKER", "LIGHT_PROBE"}]
    if angle != "user" and not targets:
        raise ToolError("Nothing visible to frame.")

    render = bpy.context.scene.render
    image_settings = render.image_settings
    saved_render = (render.filepath, render.resolution_x, render.resolution_y,
                    render.resolution_percentage, image_settings.file_format)
    saved_view = (rv3d.view_perspective, rv3d.view_rotation.copy(), rv3d.view_location.copy(),
                  rv3d.view_distance, space.shading.type, space.overlay.show_overlays)
    out_dir = Path(tempfile.gettempdir()) / "loopcut"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / "viewport.png"
    path.unlink(missing_ok=True)
    try:
        render.filepath = str(path)
        render.resolution_x = CAPTURE_WIDTH
        render.resolution_y = max(1, round(CAPTURE_WIDTH * region.height / max(1, region.width)))
        render.resolution_percentage = 100
        image_settings.file_format = "PNG"
        if angle != "user":
            _frame_view(space, region, targets, angle)
            space.shading.type = "MATERIAL"
            # Overlays are composited without depth, so the grid shows through solid objects and
            # makes it impossible to judge contact and occlusion.
            space.overlay.show_overlays = False
            rv3d.update()
        with bpy.context.temp_override(**override):
            bpy.ops.render.opengl(write_still=True, view_context=True)
    finally:
        (render.filepath, render.resolution_x, render.resolution_y,
         render.resolution_percentage, image_settings.file_format) = saved_render
        (rv3d.view_perspective, rv3d.view_rotation, rv3d.view_location,
         rv3d.view_distance, space.shading.type, space.overlay.show_overlays) = saved_view
        rv3d.update()
    if not path.is_file():
        raise ToolError("Viewport capture produced no image.")
    framed = "the user's current view" if angle == "user" else f"{angle} view of {', '.join(o.name for o in targets)}"
    return ToolResult(f"Image attached: {framed}.", image_path=path)


_DISPATCH = {
    "run_python": run_python,
    "get_scene_info": get_scene_info,
    "capture_viewport": capture_viewport,
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
