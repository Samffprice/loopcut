"""The cost guard: keeps heavy modifiers the model adds from freezing Blender.

run_python runs the model's code on the main thread, and its timeout is a line trace, so it cannot
stop one long native call. Subdivision level 6 on a dense mesh, or a 1,000-count array, is such a
call as soon as the scene is evaluated, and that happens early: every bpy.ops call updates the view
layer before the operator runs and again after it finishes (bpy_op_view_layer_update in
bpy_operator_function.cc), and run_python's snapshot afterwards updates it too. So the guard runs
before each of those, and before every render:

- watching_ops(): while the model's code runs, each operator it looks up in bpy.ops is wrapped, and
  the wrapper checks before calling the real one. It patches bpy.ops' _op_create_function, which
  every `bpy.ops.<module>.<op>` lookup calls, so every import style is covered. Swapping the bpy.ops
  attribute for a proxy missed `from bpy.ops import mesh` (headless spike, Blender 5.2).
- check(): scene_diff.snapshot calls it between its non-evaluating pass and view_layer.update().
- render_refusal(): see_render, render_job start (a resume renders the copy that start checked),
  inspect_scene, and object.bake in the model's code. A render over the limit is refused with the
  reason; nothing is changed for it.
- IN_PROCESS_RENDERS: render.render and render.play_rendered_anim in the model's code are refused
  whatever the scene weighs (in_process_refusal): even a light Cycles render there holds the main
  thread for as long as it takes. see_render and render_job are the ways to render.

A check estimates the final face count of each object whose modifiers changed since the last
check, from modifier settings alone: nothing is evaluated. Above MAX_OBJECT_FACES, or when the
scene passes MAX_SCENE_FACES, it switches off the viewport display of the most expensive modifiers
until the estimate fits. The modifiers and their render settings stay as they are, and each switch
is named in the tool result and in the panel. Remesh, geometry nodes and whatever else cannot be
estimated are reported and never switched off.

Known and accepted as uncatchable:
- code that evaluates by itself: evaluated_depsgraph_get(), evaluated_get(), to_mesh(),
  view_layer.update(), depsgraph.update(), scene.frame_set();
- operators reached other than through a bpy.ops lookup made while the code runs: a callable
  fetched before, a UI button, a timer;
- geometry built directly with bmesh or from_pydata, which is no modifier.

estimate() and plan() are pure and tested without Blender (tests/test_cost_guard.py).
"""

import contextlib
import math
import sys
import traceback

MAX_OBJECT_FACES = 2_000_000    # One object in the viewport; still drawable on an 8 GB laptop.
MAX_SCENE_FACES = 6_000_000     # All objects with modifiers together.
# Renders use render levels, usually a level above the viewport's: four times the faces.
MAX_RENDER_OBJECT_FACES = 4_000_000
MAX_RENDER_SCENE_FACES = 12_000_000
RENDER_OPS = frozenset({"object.bake"})  # Operators that evaluate at render settings.
# Final renders in the model's code: refused always, however light the scene.
IN_PROCESS_RENDERS = frozenset({"render.render", "render.play_rendered_anim"})

# Modifiers that move, smooth, weight or mask what they get: at most as many faces come out.
SAME_FACES = frozenset({
    "ARMATURE", "BUILD", "CAST", "CLOTH", "COLLISION", "CORRECTIVE_SMOOTH", "CURVE", "DATA_TRANSFER",
    "DISPLACE", "DYNAMIC_PAINT", "EDGE_SPLIT", "HOOK", "LAPLACIANDEFORM", "LAPLACIANSMOOTH", "LATTICE",
    "MASK", "MESH_CACHE", "MESH_DEFORM", "NORMAL_EDIT", "SHRINKWRAP", "SIMPLE_DEFORM", "SMOOTH",
    "SOFT_BODY", "SURFACE", "SURFACE_DEFORM", "UV_PROJECT", "UV_WARP", "VERTEX_WEIGHT_EDIT",
    "VERTEX_WEIGHT_MIX", "VERTEX_WEIGHT_PROXIMITY", "WARP", "WAVE", "WEIGHTED_NORMAL", "WELD"})
# Settings each estimable modifier is read for. Anything not here and not SAME_FACES is unknown.
PARAMS = {
    "SUBSURF": ("levels", "render_levels"),
    "MULTIRES": ("levels", "render_levels"),
    "BEVEL": ("segments", "affect", "limit_method"),
    "ARRAY": ("fit_type", "count", "fit_length", "use_relative_offset", "relative_offset_displace",
              "use_constant_offset", "constant_offset_displace"),
    "MIRROR": ("use_axis",),
    "SOLIDIFY": ("use_rim",),
    "SCREW": ("steps", "render_steps", "iterations"),
    "DECIMATE": ("decimate_type", "ratio", "iterations"),
    "TRIANGULATE": (),
    "WIREFRAME": (),
    "BOOLEAN": ("operand_type",),
}
# Share of edges a bevel's limit method selects. It depends on the mesh's angles and weights,
# which the estimate does not read.
BEVEL_SHARE = {"NONE": 1.0, "ANGLE": 0.5, "WEIGHT": 0.25, "VGROUP": 0.25}
_HINTS = {"SUBSURF": ("lower the viewport level", "lower its render_levels"),
          "MULTIRES": ("lower the viewport level", "lower its render_levels"),
          "ARRAY": ("lower the count", "lower the count"),
          "BEVEL": ("use fewer segments", "use fewer segments"),
          "SCREW": ("use fewer steps", "use fewer render_steps")}


# ------------------------------------------------------------------ the estimate (pure)

def _edges(faces: int, verts: int) -> int:
    # About two edges per face on a quad mesh; a loose-edge profile has about one per vertex.
    return max(verts, 2 * faces)


def _array_count(mod: dict, dims) -> int | None:
    if mod.get("fit_type", "FIXED_COUNT") == "FIXED_COUNT":
        return max(1, mod.get("count", 1))
    if mod["fit_type"] != "FIT_LENGTH" or dims is None:
        return None  # FIT_CURVE: the curve's length needs evaluating the curve.
    offset = [0.0, 0.0, 0.0]
    if mod.get("use_relative_offset"):
        offset = [o * d for o, d in zip(mod["relative_offset_displace"], dims)]
    if mod.get("use_constant_offset"):
        offset = [a + b for a, b in zip(offset, mod["constant_offset_displace"])]
    step = math.hypot(*offset)
    # MOD_array: count = length / distance + 1, or one copy when the offset does not move it.
    return int(mod.get("fit_length", 0.0) / step) + 1 if step > 1e-6 else 1


def _faces_after(mod: dict, faces: int, verts: int, dims, render: bool) -> int | None:
    """Faces out of one modifier given the faces and vertices going in, or None when unknown."""
    kind = mod["type"]
    if kind in SAME_FACES:
        return faces
    if kind in ("SUBSURF", "MULTIRES"):
        return faces * 4 ** max(0, mod.get("render_levels" if render else "levels", 0))
    if kind == "BEVEL":
        segments, share = max(1, mod.get("segments", 1)), BEVEL_SHARE.get(mod.get("limit_method"), 1.0)
        if mod.get("affect") == "VERTICES":
            return faces + round(share * verts * segments * segments)
        # A strip of `segments` faces along each beveled edge, a patch of about segments^2 at each corner.
        return faces + round(share * (_edges(faces, verts) * segments + verts * segments * segments))
    if kind == "ARRAY":
        count = _array_count(mod, dims)
        return None if count is None else faces * count + mod.get("cap_faces", 0)
    if kind == "MIRROR":
        return faces * 2 ** sum(1 for axis in mod.get("use_axis", (True,)) if axis)
    if kind == "SOLIDIFY":
        # Both shells, plus the rim along boundary edges: about 4 * sqrt(faces) on an open grid.
        return 2 * faces + (round(4 * math.sqrt(faces)) if mod.get("use_rim", True) else 0)
    if kind == "SCREW":
        steps = mod.get("render_steps" if render else "steps", 1)
        return _edges(faces, verts) * max(1, steps) * max(1, mod.get("iterations", 1))
    if kind == "TRIANGULATE":
        return faces * 2
    if kind == "WIREFRAME":
        return _edges(faces, verts) * 4
    if kind == "DECIMATE":
        if mod.get("decimate_type") == "COLLAPSE":
            return math.ceil(faces * mod.get("ratio", 1.0))
        if mod.get("decimate_type") == "UNSUBDIV":
            return max(1, faces // 4 ** max(0, mod.get("iterations", 0)))
        return faces
    if kind == "BOOLEAN" and mod.get("operand_faces") is not None:
        return faces + mod["operand_faces"]
    return None  # Remesh, geometry nodes, ocean, particle instances, skin...


def estimate(view: dict, render: bool = False) -> tuple[int, list[dict]]:
    """(final faces, unknown modifiers) of one mesh object from its modifier settings. An unknown
    modifier counts as x1, so with any the faces are a lower bound. view: see read_object."""
    faces, verts, dims = view["faces"], view["verts"], view.get("dims")
    flag = "show_render" if render else "show_viewport"
    unknown = []
    for mod in view["modifiers"]:
        if not mod.get(flag, True):
            continue
        after = _faces_after(mod, faces, verts, dims, render)
        if after is None:
            unknown.append(mod)
            continue
        verts = verts * after // faces if faces else after
        faces = max(0, int(after))
    return faces, unknown


def _cheapest_without(view: dict, render: bool) -> tuple[dict | None, int]:
    """The enabled estimable modifier whose switch-off lowers the estimate most, and the estimate then."""
    flag = "show_render" if render else "show_viewport"
    best, best_faces = None, estimate(view, render)[0]
    for mod in view["modifiers"]:
        if mod.get(flag, True) and mod["type"] in PARAMS and mod["type"] not in SAME_FACES:
            mod[flag] = False
            faces = estimate(view, render)[0]
            mod[flag] = True
            if faces < best_faces:
                best, best_faces = mod, faces
    return best, best_faces


def plan(views: dict, changed, render: bool = False) -> tuple[list[dict], list[str]]:
    """What to switch off so the estimate fits, and what could not be estimated.

    views: {object name: read_object view}. changed: the names that may be switched; the others
    count toward the scene total only. Returns ([{object, modifier, type, faces, scene, lower_bound}],
    ["Remesh on Rock", ...]). Pure: views are copied, never changed."""
    max_object = MAX_RENDER_OBJECT_FACES if render else MAX_OBJECT_FACES
    max_scene = MAX_RENDER_SCENE_FACES if render else MAX_SCENE_FACES
    flag = "show_render" if render else "show_viewport"
    counted = {name: {**view, "modifiers": [dict(m) for m in view["modifiers"]]} for name, view in views.items()
               if view["faces"] is not None and view["renders" if render else "shown"]}
    switches, unknown = [], []

    def switch(name: str, mod: dict, scene: int | None) -> None:
        faces, unknowns = estimate(counted[name], render)
        switches.append({"object": name, "modifier": mod["name"], "type": mod["type"], "faces": faces,
                         "scene": scene, "lower_bound": bool(unknowns)})
        mod[flag] = False

    for name in changed:
        view = views.get(name)
        if view is None:
            continue
        if view["faces"] is None:  # A curve, text or surface: its base geometry is not a mesh.
            if any(m.get(flag, True) and m["type"] not in SAME_FACES for m in view["modifiers"]):
                unknown.append(f"modifiers on {name} (a {view.get('kind', 'non-mesh').lower()} object)")
            continue
        if name not in counted:
            continue
        unknown += [f"{m['name']} on {name}" for m in estimate(counted[name], render)[1]]
        while estimate(counted[name], render)[0] > max_object:
            mod, _ = _cheapest_without(counted[name], render)
            if mod is None:
                break
            switch(name, mod, None)
    total = sum(estimate(view, render)[0] for view in counted.values())
    while total > max_scene:
        options = []
        for name in changed:
            if name in counted:
                mod, faces = _cheapest_without(counted[name], render)
                if mod is not None:
                    options.append((estimate(counted[name], render)[0] - faces, name, mod))
        if not options:
            break
        _, name, mod = max(options, key=lambda option: option[0])
        switch(name, mod, total)
        total = sum(estimate(view, render)[0] for view in counted.values())
    return switches, unknown


def count_text(n: int) -> str:
    """12000000 -> 12M, 1500000 -> 1.5M, 384000 -> 384K."""
    for unit, size in (("B", 10 ** 9), ("M", 10 ** 6), ("K", 10 ** 3)):
        if n >= size:
            value = n / size
            return (f"{value:.0f}" if value >= 10 else f"{value:.1f}".rstrip("0").rstrip(".")) + unit
    return str(n)


def line(switch: dict, render: bool = False) -> str:
    """One switch as the user and the model read it."""
    faces = ("at least " if switch["lower_bound"] else "") + f"{count_text(switch['faces'])} faces"
    if switch["scene"] is not None:
        faces += f", scene est. {count_text(switch['scene'])}"
    hint = _HINTS.get(switch["type"], ("simplify what comes before it",) * 2)[render]
    if render:
        return f"{switch['modifier']} on {switch['object']} renders at est. {faces}; {hint}"
    return f"{switch['modifier']} on {switch['object']}, est. {faces}: viewport display off; {hint}"


# ------------------------------------------------------------------ reading Blender

def _faces_of(obj) -> int:
    return len(obj.data.polygons) if obj is not None and obj.type == "MESH" and obj.data else 0


def _mesh_counts(mesh) -> tuple[int, int]:
    if mesh.is_editmode:  # The edit-mode mesh, not the copy last flushed to mesh.polygons.
        import bmesh
        edit = bmesh.from_edit_mesh(mesh)
        return len(edit.faces), len(edit.verts)
    return len(mesh.polygons), len(mesh.vertices)


def _read_modifier(mod) -> dict:
    entry = {"name": mod.name, "type": mod.type, "show_viewport": mod.show_viewport, "show_render": mod.show_render}
    for key in PARAMS.get(mod.type, ()):
        value = getattr(mod, key, None)
        entry[key] = tuple(value) if hasattr(value, "__len__") and not isinstance(value, str) else value
    if mod.type == "ARRAY":
        entry["cap_faces"] = _faces_of(mod.start_cap) + _faces_of(mod.end_cap)
    elif mod.type == "BOOLEAN":
        operand = mod.object if mod.operand_type == "OBJECT" else None
        entry["operand_faces"] = _faces_of(operand) if operand is not None and operand.type == "MESH" else None
    return entry


def read_object(obj) -> dict | None:
    """What the estimate needs, read without evaluating anything: modifier settings and the counts
    of the original mesh. None for an object without modifiers, which the guard cannot lighten."""
    if not len(obj.modifiers):
        return None
    view = {"kind": obj.type, "faces": None, "verts": None, "dims": None,
            "shown": not obj.hide_viewport, "renders": not obj.hide_render,
            "modifiers": [_read_modifier(m) for m in obj.modifiers]}
    if obj.type == "MESH" and obj.data is not None:
        view["faces"], view["verts"] = _mesh_counts(obj.data)
        if any(m.get("fit_type") == "FIT_LENGTH" for m in view["modifiers"]):
            corners = [tuple(c) for c in obj.bound_box]
            view["dims"] = tuple(max(c[i] for c in corners) - min(c[i] for c in corners) for i in range(3))
    return view


def read_scene() -> dict:
    import bpy
    scene = bpy.context.scene
    if scene is None:
        return {}
    views = {}
    for obj in scene.objects:
        view = read_object(obj)
        if view is not None:
            views[obj.name] = view
    return views


# ------------------------------------------------------------------ the guard

_seen: dict = {}          # Object name -> view at the last check.
_watching = False         # True while watching_ops() is on: a wrapped operator kept past it just runs.
_notes: list = []         # {"text", "modifiers": [[object, modifier]]} since begin(), for the result and panel.
_unknown: list = []       # "Remesh on Rock": could not be estimated, left on.


def begin() -> None:
    """Before the model's code runs. What is in the scene now was already drawn and is the user's,
    so only what changes from here on is guarded."""
    _seen.clear()
    _seen.update(read_scene())
    _notes.clear()
    _unknown.clear()


def _check() -> list[str]:
    import bpy
    views = read_scene()
    changed = [name for name, view in views.items() if _seen.get(name) != view]
    switches, unknown = plan(views, changed) if changed else ([], [])
    objects = bpy.context.scene.objects
    touched = set()
    for switch in switches:
        try:
            objects[switch["object"]].modifiers[switch["modifier"]].show_viewport = False
        except AttributeError:  # Linked from a library, so read-only.
            unknown.append(f"{switch['modifier']} on {switch['object']} (linked, could not switch it off)")
            continue
        touched.add(switch["object"])
        _notes.append({"text": line(switch), "modifiers": [[switch["object"], switch["modifier"]]]})
    touched = sorted(touched)
    views.update({name: read_object(objects[name]) for name in touched})
    _unknown.extend(u for u in unknown if u not in _unknown)
    _seen.clear()
    _seen.update(views)
    return touched


def check() -> list[str]:
    """Guard the objects whose modifiers changed since the last check, before anything evaluates
    them. Returns the names of objects whose modifiers it switched off."""
    try:
        return _check()
    except Exception:  # A bug here must not take the model's step down with it.
        traceback.print_exc()
        return []


def render_refusal() -> str:
    """Why rendering now would be too heavy, or "" when it fits. Reads render levels; changes nothing."""
    try:
        views = read_scene()
        lines = [line(switch, render=True) for switch in plan(views, list(views), render=True)[0]]
    except Exception:
        traceback.print_exc()
        return ""
    if not lines:
        return ""
    return (f"Render refused by the cost guard: at render settings this is over its limit of "
            f"{count_text(MAX_RENDER_OBJECT_FACES)} faces per object and {count_text(MAX_RENDER_SCENE_FACES)} "
            f"per scene, and one long render cannot be stopped. Nothing was changed.\n"
            + "\n".join(f"- {text}" for text in lines) + "\nLower these, then render again.")


def in_process_refusal(idname: str) -> str:
    """Why the model's code may not run this final-render operator; see IN_PROCESS_RENDERS."""
    return (f"Render refused by the cost guard: {idname} in run_python holds Blender until it ends, and "
            f"nothing can stop it. Use see_render with render=true for a still to look at, or render_job "
            f"to write stills or animations to disk.")


def _before_operator(idname: str) -> None:
    if not _watching:
        return
    if idname in IN_PROCESS_RENDERS:
        refusal = in_process_refusal(idname)  # Refused before anything evaluates, so no check needed.
    else:
        check()
        refusal = render_refusal() if idname in RENDER_OPS else ""
    if refusal:
        _notes.append({"text": refusal, "modifiers": []})  # Reported even if the code catches it.
        raise RuntimeError(refusal)


class _GuardedOp:
    # An operator from bpy.ops that runs the guard before Blender evaluates for it. Everything but
    # the call is the operator's own: poll(), idname(), get_rna_type(), bl_options, __doc__.
    __slots__ = ("_op",)
    __doc__ = property(lambda self: self._op.__doc__)

    def __init__(self, op):
        self._op = op

    def __call__(self, *args, **kwargs):
        _before_operator(self._op.idname_py())
        return self._op(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._op, name)

    def __repr__(self):
        return repr(self._op)


@contextlib.contextmanager
def watching_ops():
    """While the model's code runs: every operator looked up in bpy.ops checks before it runs."""
    module = sys.modules.get("bpy.ops")
    real = getattr(module, "_op_create_function", None)
    if real is None:
        print("Loopcut: this bpy.ops has no _op_create_function; the cost guard checks only after the code.")
        yield
        return
    global _watching
    module._op_create_function = lambda submodule, name: _GuardedOp(real(submodule, name))
    _watching = True
    try:
        yield
    finally:
        module._op_create_function = real
        _watching = False


def take() -> tuple[str, list[dict]]:
    """What the guard did since begin(): text for the tool result ("" when nothing) and the notes
    the panel shows, one per switched modifier or refused render."""
    notes, unknown = list(_notes), list(_unknown)
    _notes.clear()
    _unknown.clear()
    parts = []
    switched = [n["text"] for n in notes if n["modifiers"]]
    if switched:
        parts.append("Cost guard: switched off show_viewport before Blender evaluated these (render settings "
                     "unchanged; turn it back on once lighter):\n" + "\n".join(f"- {t}" for t in switched))
    parts += [n["text"] for n in notes if not n["modifiers"]]  # Refused renders.
    if unknown:
        parts.append(f"Cost guard could not estimate, left on: {', '.join(unknown)}.")
    return ("\n".join(parts) + "\n\n") if parts else "", notes


def show_again(modifiers: list) -> int:
    """The panel's "Show anyway": viewport display back on for [[object, modifier]]. Returns how many."""
    import bpy
    shown = 0
    for object_name, modifier_name in modifiers:
        obj = bpy.data.objects.get(object_name)
        mod = obj.modifiers.get(modifier_name) if obj else None
        if mod is not None:
            mod.show_viewport = True
            shown += 1
    return shown
