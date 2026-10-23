"""frame_camera: aim a camera and fit objects in its render frame in one call, and say where each
object lands.

In the eval transcripts every model rewrote its camera after the first try (26 re-frame writes in
13 tasks), and what failed was framing math: camera object vs camera data, projection matrices,
rotation modes, hand-written coverage scripts. Framing has a closed-form answer, in framing.py;
this module reads Blender for it and writes the result back.

Points are evaluated vertices in world space, instances included: a collection instance or a
geometry-nodes scatter counts for the object that makes it. The fit gets, from all vertices, the
ones that decide it (the extremes toward each frame edge) plus a deterministic sample for the
centring; the report gets the sample, for the share inside, and the exact extremes in the final
view, for the bounds and margins.

region fits the subject into a part of the frame instead of all of it (a product in the right 60%),
by moving the camera, not its rotation; a lens shift is kept and allowed for, never reset. The
result names the margin that binds: both of the fitted side's, or at fill 1 around aim the one
edge. A close shot is fill > 1: the camera keeps the rotation and comes toward aim (an object's
centre or a point; default the subject's centre), which stays in the middle of the frame (the
region), until the subject is fill times as large as the whole fit makes it. The report's "% inside" and clipped edges then
show the crop. coverage() is the report's measurement on its own, for acceptance checks and the
orchestrator's layout dry-run. report_only with frames measures at each frame, constraints and
animation evaluated there, a line per frame, and puts the scene back on its frame.
"""

import json
import math

import bpy

from . import framing, tools, view_layers
from .tools import ToolError, ToolResult

SCHEMAS = [{"type": "function", "function": {  # Sent with every request: every word here is paid for on every step.
    "name": "frame_camera",
    "title": "Framing the camera",
    "description": (
        "Aim a camera and fit objects in its render frame, solved exactly. Returns where each object lands "
        "(frame 0..1), % inside, clipped edges, margins, the camera transform. Use it, not camera math in "
        "run_python."),
    "parameters": {"type": "object", "properties": {
        "objects": {"type": "array", "items": {"type": "string"},
                    "description": "A parent includes its children. Default all visible"},
        "camera": {"type": "string", "description": "Default the scene camera"},
        "direction": {"type": "string", "description": "keep (default), front, side, top, three_quarter, "
                                                       "or \"x,y,z\" toward the camera"},
        "margin": {"type": "number", "description": "Per side, frame fraction. Default 0.08"},
        "region": {"type": "string", "description": "\"x0,y0,x1,y1\" (frame 0..1) to fit in; right 60%: \"0.4,0,1,1\""},
        "fill": {"type": "number", "description": "1 frames all of it; 1.5-3 for a close shot, cropped around aim"},
        "aim": {"type": "string", "description": "Object or \"x,y,z\" to centre on"},
        "lens": {"type": "number", "description": "mm, set first. Orthographic: fits ortho_scale"},
        "create": {"type": "boolean", "description": "Make one if none"},
        "report_only": {"type": "boolean", "description": "Change nothing; report coverage"},
        "frames": {"type": "array", "items": {"type": "integer"}, "description": "report_only at each"},
    }},
}}]

MAX_POINTS = 2_000_000  # Vertices read in all; meshes past this are measured by their bounding boxes.
SAMPLE = 6000           # Sampled points over all objects, for the centring and the share inside...
MIN_SAMPLE = 64         # ...and at least this many per object.
_POINTLESS = {"EMPTY", "LIGHT", "CAMERA", "SPEAKER", "LIGHT_PROBE", "ARMATURE", "LATTICE"}


def _true(value) -> bool:
    return str(value).lower() == "true"  # A model may send "true"; tools.only_reads reads it the same way.


def _number(value, default: float):
    """A number the model may send as text; anything else as it is, for the check to name."""
    if value in (None, ""):
        return default
    try:
        return float(value) if not isinstance(value, bool) else value
    except (TypeError, ValueError):
        return value


def _world(local, matrix):
    import numpy as np
    m = np.array([list(r) for r in matrix], dtype=np.float64)
    return local @ m[:3, :3].T + m[:3, 3]


def _box(obj):
    """The bounding-box corners, or None when they are all one point (nothing evaluated)."""
    import numpy as np
    corners = np.array([tuple(c) for c in obj.bound_box], dtype=np.float64)
    return corners if np.ptp(corners, axis=0).any() else None


def _local_points(obj, budget: list):
    """An evaluated object's vertices in its own space; its box once the read budget is spent."""
    import numpy as np
    if obj.type != "MESH":
        return _box(obj)  # Curves, text, volumes, point clouds: the evaluated box.
    count = len(obj.data.vertices)
    if not count:
        return None
    if count > budget[0]:
        budget[1] = True
        return _box(obj)
    budget[0] -= count
    co = np.empty(count * 3, dtype=np.float32)
    obj.data.vertices.foreach_get("co", co)
    return co.reshape(count, 3).astype(np.float64)


def _named(names: list) -> list:
    found = [bpy.data.objects.get(name) for name in names]
    missing = [name for name, obj in zip(names, found) if obj is None]
    if missing:
        import difflib
        hints = difflib.get_close_matches(missing[0], [o.name for o in bpy.context.scene.objects], n=3)
        raise ToolError(f"No object named {', '.join(map(repr, missing))}"
                        + (f"; close: {', '.join(hints)}" if hints else ""))
    return found


def _geometry(targets, depsgraph) -> tuple[dict, list]:
    """World points per row, {name: Nx3 array or None}, and notes. A row is a named object with
    its children and what it instances; with no targets, every visible object with geometry."""
    import numpy as np
    rows_of = {}
    for obj in targets or ():
        for member in (obj, *obj.children_recursive):
            rows_of.setdefault(member, []).append(obj.name)
    parts = {obj.name: [] for obj in targets or ()}
    budget = [MAX_POINTS, False]  # Vertices left to read; whether any mesh was measured by its box.
    for instance in depsgraph.object_instances:
        obj = instance.object
        if obj.type in _POINTLESS:
            continue
        owner = (instance.parent if instance.is_instance else obj).original
        names = rows_of.get(owner) if targets else [owner.name]
        if not owner.visible_get(view_layer=depsgraph.view_layer):
            # Explicit hidden geometry keeps the existing bounding-box fallback below. A parent
            # includes only visible descendants, including when hidden objects are still evaluated.
            names = None
        local = _local_points(obj, budget) if names else None
        if local is not None:
            for name in names:
                parts.setdefault(name, []).append(_world(local, instance.matrix_world))
    notes = ["Some large meshes were measured by their bounding boxes."] if budget[1] else []
    hidden, lost, layer = [], [], depsgraph.view_layer
    for obj in targets or ():
        if parts[obj.name] or obj.type in _POINTLESS:
            continue
        if not view_layers.includes(layer, obj):   # Not evaluated here: its matrix_world is stale, maybe identity.
            other = view_layers.home(obj)
            if other is None:
                lost.append(obj.name)
                continue
            with view_layers.evaluating(other):
                evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
                box = _box(evaluated)
                if box is not None:
                    parts[obj.name].append(_world(box, evaluated.matrix_world))
                    hidden.append(f"{obj.name} (in view layer {other.name})")
        elif _box(obj) is not None:
            parts[obj.name].append(_world(_box(obj), obj.matrix_world))  # Hidden: nothing evaluated to read.
            hidden.append(obj.name)
    if hidden:
        notes.append(f"Not visible, measured by bounding box: {', '.join(hidden)}.")
    if lost:
        notes.append(view_layers.unavailable(lost) + ".")
    return {name: np.vstack(arrays) if arrays else None for name, arrays in parts.items()}, notes


def _pick(points, count: int, directions=()):
    """A deterministic sample of `count` points, plus the extremes along each direction."""
    import numpy as np
    index = np.unique(np.linspace(0, len(points) - 1, min(len(points), count)).round().astype(int))
    if len(directions):
        along = points @ np.asarray(directions, dtype=np.float64).T
        index = np.union1d(index, np.concatenate([along.argmin(axis=0), along.argmax(axis=0)]))
    return points[index]


def _fit_directions(basis, view: framing.Frame, margin: float, region, fill: float) -> list:
    """The directions whose extremes decide the fit: toward each edge of the region moved in by the
    margin (framing._axis_fit), for a close shot also fill times as far out (what spans how much),
    and along the camera axes, for the orthographic spans and the depth."""
    import numpy as np
    right, up, back = (np.array(axis) for axis in basis)
    directions = [right, up, back]
    if not view.ortho:
        for axis, (centre, half) in zip((right, up), framing._window(view, margin, region)):
            for reach in ((1.0, fill) if fill > 1.0 else (1.0,)):
                directions += [axis + (centre + reach * half) * back, axis + (centre - reach * half) * back]
    return directions


def _view_extremes(points, location, basis, view: framing.Frame, scale: float):
    """The points that land furthest left, right, down and up in the camera's frame."""
    import numpy as np
    right, up, back = (np.array(axis) for axis in basis)
    relative = points - np.array(location)
    x, y, depth = relative @ right, relative @ up, -(relative @ back)
    seen = np.ones(len(points), dtype=bool) if view.ortho else depth > 1e-12
    if not seen.any():
        return points[:0]
    divisor = scale if view.ortho else depth[seen]
    across, high = x[seen] / divisor, y[seen] / divisor
    return points[np.flatnonzero(seen)[[across.argmin(), across.argmax(), high.argmin(), high.argmax()]]]


def _frame(data, scene, lens=None) -> framing.Frame:
    render = scene.render
    return framing.frame((render.resolution_x, render.resolution_y), data.sensor_fit, data.sensor_width,
                         data.sensor_height, lens or data.lens, (data.shift_x, data.shift_y),
                         (render.pixel_aspect_x, render.pixel_aspect_y), ortho=data.type == "ORTHO")


def _placed(obj, depsgraph) -> tuple:
    """Where the camera renders from: location, (right, up, back) and the world matrix, after
    parents and constraints."""
    matrix = obj.evaluated_get(depsgraph).matrix_world.copy()
    rotation = matrix.to_3x3().normalized()
    return tuple(matrix.translation), tuple(tuple(rotation.col[i]) for i in range(3)), matrix


def _camera(name: str, create: bool, report_only: bool):
    """The camera object, or None when create is to make one."""
    obj = bpy.data.objects.get(name) if name else bpy.context.scene.camera
    if obj is not None:
        if obj.type != "CAMERA":
            raise ToolError(f"{obj.name!r} is a {obj.type.lower()}, not a camera.")
        if obj.data.type == "PANO":
            raise ToolError("A panoramic camera has no frame edges to fit; switch it to perspective first.")
        return obj
    if report_only or not create:
        raise ToolError((f"No camera named {name!r}." if name else "The scene has no camera.")
                        + ("" if report_only else " create=true makes one."))
    return None


def _new_camera(name: str):
    scene = bpy.context.scene
    obj = bpy.data.objects.new(name or "Camera", bpy.data.cameras.new(name or "Camera"))
    scene.collection.objects.link(obj)
    if scene.camera is None:
        scene.camera = obj
    return obj


def _undo_push(message: str) -> None:
    """One undo step before the change, as run_python pushes one."""
    try:
        with tools._editor_context(""):
            bpy.ops.ed.undo_push(message=message[:60])
    except (RuntimeError, ToolError):
        pass  # No editor to push from: the turn's checkpoint still covers this step.


def _direction_words(direction) -> str:
    basis_name = str(direction).strip().lower() if isinstance(direction, str) else ""
    if basis_name in ("", "keep"):
        return "its rotation"
    return basis_name if basis_name in framing.NAMED else f"direction {direction}"


def _aim(aim, depsgraph) -> tuple:
    """(point, words) for aim: an object's centre (its evaluated bounds, children included) or an
    "x,y,z" point; (None, "") for the subject's centre."""
    import numpy as np
    if aim is None or (isinstance(aim, str) and not aim.strip()):
        return None, ""
    obj = bpy.data.objects.get(aim) if isinstance(aim, str) else None
    if obj is None and (isinstance(aim, (list, tuple)) or "," in str(aim)):
        parts = aim.strip().strip("[]()").split(",") if isinstance(aim, str) else aim
        try:
            point = tuple(float(p) for p in parts)
        except (TypeError, ValueError):
            point = ()
        if len(point) != 3 or not all(math.isfinite(c) for c in point):
            raise ToolError(f'aim is an object name or "x,y,z"; got {aim!r}')
        return point, "(" + ", ".join(f"{c:g}" for c in point) + ")"
    if obj is None:
        obj = _named([str(aim)])[0]
    rows, _ = _geometry([obj], depsgraph)
    points = rows[obj.name]
    if points is None:
        return tuple(obj.matrix_world.translation), obj.name
    return tuple(float(c) for c in (points.min(axis=0) + points.max(axis=0)) / 2.0), obj.name


def _measure(rows: dict, per_row: int, location, basis, view: framing.Frame, scale: float, clip) -> tuple:
    """framing.coverage per row, on a sample plus the points that land furthest toward each frame
    edge, and of all rows together: ([(name, coverage)], coverage of all, or None)."""
    import numpy as np
    measured, together = [], []
    for name, points in rows.items():
        if points is None:
            measured.append((name, {"empty": True}))
            continue
        shown = np.vstack([_pick(points, per_row), _view_extremes(points, location, basis, view, scale)])
        together.append(shown)
        measured.append((name, framing.coverage([tuple(p) for p in shown], location, basis, view, scale, clip)))
    subject = framing.coverage([tuple(p) for p in np.vstack(together)], location, basis, view, scale,
                               clip) if together else None
    return measured, subject


def coverage(camera: str, names: list[str] | None = None, points: dict | None = None) -> dict:
    """Where objects, or given world points, land in a camera's frame, measured exactly as
    frame_camera report_only measures them: {"camera": name, "rows": {name: framing.coverage, or
    {"empty": True}}, "all": the coverage of all rows together or None, "notes": [...]}. points is
    {label: [(x, y, z), ...]}, planned boxes' corners say. No names and no points: every visible
    object with geometry. Raises tools.ToolError for a missing camera or object."""
    import numpy as np
    obj = _camera(camera, False, True)
    targets = _named(names) if names else None
    layer, note = _layer(obj, targets)
    with view_layers.evaluating(layer):
        depsgraph = bpy.context.evaluated_depsgraph_get()
        rows, notes = ({}, []) if points and not names else _geometry(targets, depsgraph)
        for label, corners in (points or {}).items():
            array = np.asarray(corners, dtype=np.float64).reshape(-1, 3)
            rows[str(label)] = array if len(array) else None
        location, basis, _matrix = _placed(obj, depsgraph)
    per_row = max(MIN_SAMPLE, SAMPLE // max(1, len(rows)))
    measured, subject = _measure(rows, per_row, location, basis, _frame(obj.data, bpy.context.scene),
                                 obj.data.ortho_scale, (obj.data.clip_start, obj.data.clip_end))
    return {"camera": obj.name, "rows": dict(measured), "all": subject, "notes": ([note + "."] if note else []) + notes,
            "location": location}


def _layer(obj, targets) -> tuple:
    """(view layer, note) a camera and its subject are read in: one that includes the camera (and
    the most of the subject), the active one first; the note says which when it is not the active
    one. A camera every view layer excludes has no placement to read: ToolError, not a number."""
    if obj is not None and view_layers.home(obj) is None:
        raise ToolError(view_layers.unavailable([obj.name]) + ". Include its collection in a view layer first.")
    layer, _missing = view_layers.common([obj, *(targets or ())], required=[obj])
    return layer, view_layers.elsewhere(layer, [obj, *(targets or ())])


def _objects(objects):
    if isinstance(objects, str):
        objects = [objects] if objects.strip() else []
    if objects is not None and not (isinstance(objects, list) and all(isinstance(n, str) for n in objects)):
        raise ToolError("objects must be a list of object names")
    return objects


def frame_camera(camera: str = "", objects: list | None = None, margin: float = 0.08, direction: str = "keep",
                 lens: float | None = None, create: bool = False, report_only: bool = False,
                 fill: float = 1.0, aim: str | None = None, region=None, frames=None,
                 frame: int | None = None) -> ToolResult:
    """The tool (see SCHEMAS). Every argument and name is checked before anything changes. frames,
    with report_only: the scene is set to each frame for the measure (constraints, parents and
    animation evaluated there), then back to the frame it was on. frame is the one-frame spelling
    it had before frames, still taken."""
    if frame not in (None, ""):
        if frames not in (None, "", []):
            raise ToolError("Give frames, not frame too.")
        frames = frame
    if frames in (None, "", []):
        return _frame_camera(camera, objects, margin, direction, lens, create, report_only, fill, aim, region)
    try:
        frames = framing.frame_numbers(frames)
    except ValueError as ex:
        raise ToolError(str(ex)) from ex
    if not _true(report_only):
        raise ToolError("frames works with report_only: it measures at those frames and changes nothing. To frame "
                        "the camera at a frame, set the frame first (run_python), then call frame_camera.")
    scene = bpy.context.scene
    back = (scene.frame_current, scene.frame_subframe)
    try:
        if len(frames) == 1:
            scene.frame_set(frames[0])
            result = _frame_camera(camera, objects, margin, direction, lens, create, report_only, fill, aim, region)
            result.text = f"At frame {frames[0]} (the scene is back on frame {back[0]}):\n{result.text}"
            return result
        names = _objects(objects)
        if region not in (None, "", []):
            raise ToolError("region is for framing; report_only measures the whole frame.")
        measured, notes = [], []
        for number in frames:
            scene.frame_set(number)
            found = coverage(camera, names or None)
            obj = bpy.data.objects[found["camera"]]
            measured.append((number, found["location"], list(found["rows"].items()), found["all"]))
            notes += [note for note in found["notes"] if note not in notes]
    finally:
        scene.frame_set(back[0], subframe=back[1])
    data, view = obj.data, _frame(obj.data, scene)
    if scene.camera != obj:
        notes.insert(0, f"Not the scene camera ({scene.camera.name if scene.camera else 'none is set'}).")
    return ToolResult(framing.frames_report({
        "name": obj.name, "lens": data.lens, "ortho_scale": data.ortho_scale if view.ortho else None,
        "resolution": (scene.render.resolution_x, scene.render.resolution_y),
        "fit": f"AUTO, {view.fit.lower()}" if data.sensor_fit == "AUTO" else data.sensor_fit}, measured, back[0], notes))


def _frame_camera(camera, objects, margin, direction, lens, create, report_only, fill, aim, region) -> ToolResult:
    """_fit, read and written in the view layer that includes the camera and the most of the subject
    (_layer): a camera in a collection the active layer excludes is otherwise read at the origin."""
    names = _validated(objects, margin, direction, lens, create, report_only, fill, region)[2]
    obj = _camera(camera, _true(create), _true(report_only))
    layer, note = _layer(obj, _named(names) if names else None)
    with view_layers.evaluating(layer):
        return _fit(camera, objects, margin, direction, lens, create, report_only, fill, aim, region, note)


def _validated(objects, margin, direction, lens, create, report_only, fill, region) -> tuple:
    """The arguments checked and normalised before Blender is touched: (report_only, create, objects,
    margin, lens, fill, basis, region); ToolError for the first that is wrong."""
    report_only, create = _true(report_only), _true(create)
    objects = _objects(objects)
    try:
        margin = float(margin)
        lens = float(lens) if lens not in (None, "", 0) else None
        fill = framing._checked_fill(_number(fill, 1.0))
        basis = None if report_only else framing.direction_basis(direction)
        region = framing.region_of(region if region != [] else None)
    except (TypeError, ValueError) as ex:
        raise ToolError(str(ex)) from ex
    if not 0.0 <= margin <= framing.MAX_MARGIN:
        raise ToolError(f"margin must be between 0 and {framing.MAX_MARGIN}")
    if report_only and region != framing.FULL:
        raise ToolError("region is for framing; report_only measures the whole frame.")
    if min(framing.usable(region, margin)) <= 1e-6:
        raise ToolError(f"region {list(region)} leaves no room inside margin {margin:g} on each side; "
                        "a smaller margin or a larger region")
    if lens is not None and not 1.0 <= lens <= 5000.0:
        raise ToolError("lens is a focal length in mm, 1 to 5000")
    return report_only, create, objects, margin, lens, fill, basis, region


def _fit(camera, objects, margin, direction, lens, create, report_only, fill, aim, region, layer_note="") -> ToolResult:
    report_only, create, objects, margin, lens, fill, basis, region = _validated(
        objects, margin, direction, lens, create, report_only, fill, region)
    import numpy as np
    from mathutils import Matrix, Vector
    from . import scene_diff
    scene = bpy.context.scene
    targets = _named(objects) if objects else None
    obj = _camera(camera, create, report_only)
    if lens is not None and obj is not None and obj.data.type == "ORTHO" and not report_only:
        raise ToolError("This camera is orthographic: lens does not apply. Leave lens out to fit ortho_scale.")

    depsgraph = bpy.context.evaluated_depsgraph_get()
    aim_point, aim_words = (None, "") if report_only else _aim(aim, depsgraph)
    rows, notes = _geometry(targets, depsgraph)
    if layer_note:
        notes.insert(0, layer_note + ".")
    if not any(points is not None for points in rows.values()):
        raise ToolError("Nothing to frame: " + (f"{', '.join(rows)} have no geometry." if targets
                                                else "no visible object has geometry."))
    per_row = max(MIN_SAMPLE, SAMPLE // len(rows))
    everything = np.vstack([points for points in rows.values() if points is not None])
    if not report_only and np.ptp(everything, axis=0).max() < 1e-9:
        raise ToolError("Nothing to frame: the objects have no size.")
    centre = (everything.min(axis=0) + everything.max(axis=0)) / 2.0
    around = not report_only and (aim_point is not None or fill > 1.0)

    before = None if report_only else scene_diff.snapshot()
    if not report_only:
        _undo_push("Before Loopcut: frame camera")
    created = obj is None
    if created:
        obj = _new_camera(camera)
        bpy.context.view_layer.update()  # Into the depsgraph, which _placed reads.
    data = obj.data
    if report_only:
        what = "as it is"
    else:
        location, current, matrix = _placed(obj, depsgraph)
        if basis is None:  # keep: the rotation it renders with; a new camera starts at three_quarter.
            basis = framing.euler_basis(framing.NAMED["three_quarter"]) if created else current
        view = _frame(data, scene, lens)
        directions = _fit_directions(basis, view, margin, region, fill if around else 1.0)
        chosen = np.vstack([_pick(points, per_row, directions) for points in rows.values() if points is not None])
        if aim_point is None:
            aim_point, aim_words = tuple(float(c) for c in centre), "the subject's centre"
        try:
            if around:
                fitted = framing.fit_around([tuple(p) for p in chosen], basis, view, margin, aim_point, fill, region)
            else:
                fitted = framing.fit([tuple(p) for p in chosen], basis, view, margin, region)
        except ValueError as ex:
            raise ToolError(str(ex)) from ex
        if lens is not None:
            data.lens = lens
        if fitted.ortho_scale is not None:
            data.ortho_scale = fitted.ortho_scale
        rotation = Matrix(basis).transposed()  # The axes as columns.
        obj.matrix_world = Matrix.LocRotScale(Vector(fitted.location), rotation, matrix.to_scale())
        bpy.context.view_layer.update()
        what = f"{'created and ' if created else ''}framed from {_direction_words(direction)} with "
        inside = "" if region == framing.FULL else (f" in region x {region[0]:g}..{region[2]:g} "
                                                    f"y {region[1]:g}..{region[3]:g}")
        sides = {"width": "left and right", "height": "bottom and top"}[fitted.limit]
        if not around:
            what += f"margin {margin:g}{inside}, fitted to the {fitted.limit} ({sides} margins bind)"
        elif fill == 1.0:
            binds = f"the {fitted.edge} margin binds" if fitted.edge else "no margin binds: nearest the camera may go"
            what += f"margin {margin:g}{inside}, centred on {aim_words}, fitted to the {fitted.limit} ({binds})"
        else:
            what += (f"fill {fill:g}{inside}, centred on {aim_words} (the subject spans {fitted.span:.2f} of the "
                     f"frame {fitted.limit}, cropped)")
            wanted = fill * framing.usable(region, margin)[0 if fitted.limit == "width" else 1]
            if fitted.span < wanted * 0.995:
                notes.append(f"fill {fill:g} needs {wanted:.2f}; {fitted.span:.2f} is the most with the camera "
                             f"in front of all of the subject. A longer lens, or fewer objects, gets closer.")

    depsgraph = bpy.context.evaluated_depsgraph_get()
    location, basis, matrix = _placed(obj, depsgraph)
    view = _frame(data, scene)
    scale = data.ortho_scale
    depths = -((everything - np.array(location)) @ np.array(basis[2]))
    if not report_only:
        # The clip range follows the subject, so framing it can never cut it off.
        near, far = float(depths.min()), float(depths.max())
        if 0.0 < near < data.clip_start:
            notes.append(f"clip_start {data.clip_start:g} -> {near * 0.5:.4g}")
            data.clip_start = near * 0.5
        if far > data.clip_end:
            notes.append(f"clip_end {data.clip_end:g} -> {far * 1.1:.4g}")
            data.clip_end = far * 1.1
    clip = (data.clip_start, data.clip_end)
    measured, subject = _measure(rows, per_row, location, basis, view, scale, clip)

    if scene.camera != obj:
        notes.insert(0, f"Not the scene camera ({scene.camera.name if scene.camera else 'none is set'}).")
    if not report_only and (data.shift_x or data.shift_y):
        notes.append(f"Lens shift x {data.shift_x:g} y {data.shift_y:g} kept; the fit allows for it.")
    if not report_only:
        constraints = [c.name for c in obj.constraints if c.enabled]
        if constraints:
            notes.append(f"Its constraints ({', '.join(constraints)}) move it after this; "
                         f"the numbers are where it renders from.")
        if obj.animation_data and obj.animation_data.action:
            notes.append("It is animated: key this pose or the next frame change puts it back.")
    fit_word = f"AUTO, {view.fit.lower()}" if data.sensor_fit == "AUTO" else data.sensor_fit
    rotation = [math.degrees(a) for a in matrix.to_3x3().normalized().to_euler("XYZ")]
    text = framing.report({
        "name": obj.name, "what": what, "location": location, "rotation": rotation,
        "lens": data.lens, "ortho_scale": scale if view.ortho else None,
        "distance": float(np.linalg.norm(centre - np.array(location))),
        "resolution": (scene.render.resolution_x, scene.render.resolution_y), "fit": fit_word,
    }, measured, subject, notes)
    if report_only:
        return ToolResult(text)
    return ToolResult(text, scene_before=before, scene_after=scene_diff.snapshot())


DISPATCH = {"frame_camera": frame_camera}


def describe(name: str, arguments: dict):
    """The step's label in the chat, like job_tools.describe."""
    if name != "frame_camera":
        return None
    objects = arguments.get("objects")
    subject = (", ".join(map(str, objects)) if isinstance(objects, list) and objects
               else str(objects or "everything visible"))
    subject = subject if len(subject) <= 60 else subject[:57] + "..."
    if _true(arguments.get("report_only")):
        return f"Check camera coverage of {subject}", ""
    fill, aim = _number(arguments.get("fill"), 1.0), arguments.get("aim")
    if isinstance(fill, float) and fill > 1.0:
        around = f" around {aim}" if isinstance(aim, str) and aim.strip() else ""
        return f"Close shot of {subject}{around}", json.dumps(arguments, indent=2)
    return f"Frame camera on {subject}", json.dumps(arguments, indent=2)
