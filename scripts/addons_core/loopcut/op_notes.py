"""Facts at operator calls, without changing their arguments or results.

No package imports: workspace Blenders also run this file with --python. Live
Blender installs only during agent code; startup files install for the process.
"""

import contextlib
import sys

NOTICE_PREFIX = "Loopcut note:"


def _upstream(socket):
    """(node, output name) feeding a socket, through reroutes; (None, "") when nothing does."""
    seen = set()
    while socket.is_linked:
        link = socket.links[0]
        node = link.from_node
        if node.type != "REROUTE" or node.as_pointer() in seen:
            return node, link.from_socket.name
        seen.add(node.as_pointer())
        socket = node.inputs[0]
    return None, ""


def _painted_map(kwargs) -> bool:
    """A mesh in Edit Mode whose materials read image textures through the UV map the operator writes."""
    import bmesh
    import bpy
    for obj in bpy.context.objects_in_mode_unique_data or ():
        if obj.type != "MESH":
            continue
        layers = bmesh.from_edit_mesh(obj.data).loops.layers.uv
        layer = layers.active
        if layer is None:
            continue
        # An image node with no UV Map node reads the rendering map, which Edit Mode does not show here
        # (the mesh's own layers are empty meanwhile): it counts only when there is one map.
        default = len(layers) == 1
        for slot in obj.material_slots:
            tree = getattr(slot.material, "node_tree", None)
            for node in tree.nodes if tree else ():
                if node.type != "TEX_IMAGE" or node.image is None:
                    continue
                source, output = _upstream(node.inputs["Vector"])
                if source is not None and source.type == "MAPPING":
                    source, output = _upstream(source.inputs["Vector"])
                reads_default = source is None or (source.type == "TEX_COORD" and output == "UV") or (
                    source.type == "UVMAP" and not source.uv_map)
                if (reads_default and default) or (source is not None and source.type == "UVMAP"
                                                   and source.uv_map == layer.name):
                    return True
    return False


PAINTED = (
    _painted_map,
    "The mesh's materials read image textures through the UV map this writes: a new layout moves that "
    "artwork on the surface. To keep the look, unwrap into a new UV map, keep the old one, and bake each "
    "image from the old map onto the new one (transfer_textures does it in one call where it is offered). "
    "A texture that tiles needs nothing.",
)
NOTES = {
    "uv.pack_islands": ((
        lambda kwargs: kwargs.get("margin_method") == "FRACTION",
        "pack_islands with margin_method='FRACTION' adds margin around each island. "
        "Neighbouring islands are about 2 x margin x texture size apart; the minimum "
        "tile-border distance is about margin x texture size. At 2048 px, margin=8/2048 "
        "leaves about 16 px between islands and 8 px at the border. A gap of g px "
        "at size R corresponds to margin=g/(2R).",
    ), PAINTED),
    **{f"uv.{name}": (PAINTED,) for name in ("unwrap", "smart_project", "lightmap_pack", "cube_project",
                                             "cylinder_project", "sphere_project", "project_from_view", "reset",
                                             "follow_active_quads")},
}
# Python values only. A module reload and take() both preserve once-per-process delivery.
_seen = globals().get("_seen", set())
_notices = globals().get("_notices", [])


def _record(notes, kwargs, echo, active):
    try:
        if not active[0]:
            return
        for predicate, text in notes:
            if text not in _seen and predicate(kwargs):
                notice = f"{NOTICE_PREFIX} {text}"
                _seen.add(text)
                _notices.append(notice)
                if echo:
                    print(notice, flush=True)
    except Exception:
        # Intentional diagnostic boundary, like api_docs.error_note: a broken
        # predicate or output stream must never replace the operator's outcome.
        return


class _NotedOp:
    __slots__ = ("_op", "_notes", "_echo", "_active")
    __doc__ = property(lambda self: self._op.__doc__)

    def __init__(self, op, notes, echo, active):
        self._op, self._notes, self._echo, self._active = op, notes, echo, active

    def __call__(self, *args, **kwargs):
        _record(self._notes, kwargs, self._echo, self._active)
        return self._op(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._op, name)

    def __repr__(self):
        return repr(self._op)

    def __str__(self):
        return str(self._op)

    def __dir__(self):
        return dir(self._op)


def install(echo=False):
    """Install for the process; return a cleanup for scoped, stack-ordered use."""
    module = sys.modules.get("bpy.ops")
    real = getattr(module, "_op_create_function", None)
    if real is None:
        return lambda: None
    active = [True]

    def create(submodule, name):
        op = real(submodule, name)
        try:
            notes = NOTES.get(f"{submodule}.{name}", ())
            return _NotedOp(op, notes, echo, active) if notes else op
        except Exception:
            # Intentional diagnostic boundary: keep the original lookup intact.
            return op

    def restore():
        active[0] = False  # A proxy saved by agent code also becomes inert.
        module._op_create_function = real

    module._op_create_function = create
    return restore


@contextlib.contextmanager
def watching_ops():
    """Observe calls only while agent code runs; compose with cost_guard's hook."""
    restore = install()
    try:
        yield
    finally:
        restore()


def take():
    """Return and clear pending notices, without repeating previously seen facts."""
    notices = list(_notices)
    _notices.clear()
    return notices


if __name__ == "__main__":
    install(echo=True)
