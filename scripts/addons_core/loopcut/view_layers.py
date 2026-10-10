"""Which view layer a read evaluates in, and which one a production shot renders.

An object in a collection excluded from the active view layer is not in that layer's depsgraph:
evaluated_get returns the original, whose matrix_world and bounds are whatever was last flushed
back to it, the identity for one made while excluded. The Arcane chest run (20260930-154037)
read its staged cameras at the origin with the chest behind them (O0007-O0008, W0043-W0044) and
failed six preserved props (W0047), until a script refreshed the staging layer. Reads here
evaluate each object in a view layer that includes it, the active one first (evaluating), with
bpy.context.temp_override(view_layer=...): no window, viewport or file setting changes, and the
context is back when the block ends. Evaluating a layer flushes its results back to the
originals, so matrix_world and bound_box read right afterwards too. An object no view layer
includes has no evaluated placement: callers say it is unavailable (unavailable()) instead of
giving a number.

A production shot is one view layer through one camera: its collection exclusions, holdouts and
indirect-only collections, its world and material overrides, the scene's lights in it and its
colour management (production, summary, lights, world).
"""

from contextlib import nullcontext

import bpy

MAX_NAMES = 4   # Names listed in a summary before "+N more".


def active():
    return bpy.context.view_layer


def includes(layer, obj) -> bool:
    """Whether obj is in this view layer: linked to the scene through a collection the layer does
    not exclude (hidden or not). Visible there settles it at once (a base lookup); otherwise the
    layer's objects are searched by name."""
    if obj.visible_get(view_layer=layer):
        return True
    found = layer.objects.get(obj.name)
    if found is not None and found == obj:
        return True
    return obj.library is not None and any(o == obj for o in layer.objects)   # A linked name can repeat.


def rendering(scene) -> list:
    """The view layers a render renders (use), in the scene's order."""
    return [layer for layer in scene.view_layers if layer.use]


def _ordered(scene) -> list:
    """The active layer, then the rendering ones, then the rest: who evaluates an object first."""
    current = active()
    layers = [current] if current is not None and current.id_data == scene else []
    return layers + [l for l in rendering(scene) if l not in layers] + [l for l in scene.view_layers if l not in layers
                                                                         and not l.use]


def home(obj, scene=None):
    """The view layer obj is evaluated in (_ordered: the active one when it includes obj), or None
    when every view layer excludes it."""
    scene = scene or bpy.context.scene
    return next((layer for layer in _ordered(scene) if includes(layer, obj)), None)


def common(objects, scene=None, required=()) -> tuple:
    """(layer, missing): the first view layer (_ordered) that includes every object, else the one
    that includes the most; missing are the objects it excludes. required: only layers that include
    these count (a camera, whose placement every number depends on), unless none does. (active,
    []) for no objects."""
    scene = scene or bpy.context.scene
    objects = [o for o in objects if o is not None]
    required = [o for o in required if o is not None]
    candidates = [l for l in _ordered(scene) if all(includes(l, o) for o in required)] or _ordered(scene)
    best, best_missing = None, None
    for layer in candidates:
        missing = [o for o in objects if not includes(layer, o)]
        if not missing:
            return layer, []
        if best_missing is None or len(missing) < len(best_missing):
            best, best_missing = layer, missing
    return (best, best_missing) if best is not None else (active(), list(objects))


def evaluating(layer):
    """A block whose context view layer is this one: bpy.context.view_layer, evaluated_depsgraph_get,
    visible_get and view_layer.update read it. Nothing when it is already the active one."""
    if layer is None or layer == active():
        return nullcontext()
    return bpy.context.temp_override(view_layer=layer)


def unavailable(names) -> str:
    """What a read says for objects every view layer excludes, instead of a number."""
    names = list(dict.fromkeys(names))
    one = len(names) == 1
    return (f"{', '.join(names)} {'is' if one else 'are'} excluded from every view layer: {'its' if one else 'their'} "
            f"placement and geometry are unavailable in this context")


def elsewhere(layer, objects) -> str:
    """The note for a read evaluated in another view layer than the active one ("" when it is the
    active one): which, and which of these objects the active one excludes."""
    current = active()
    if layer is None or layer == current:
        return ""
    names = list(dict.fromkeys(o.name for o in objects if o is not None and not includes(current, o)))
    return f"Evaluated in view layer {layer.name}" + (f": the active one ({current.name}) excludes {listed(names)}"
                                                      if names else "")


# ------------------------------------------------------------------ the production shot

def production(scene, name: str = ""):
    """The view layer a production shot renders: the named one, else the active one when it
    renders, else the first that renders, else the active one. ToolError for a name the scene does
    not have, with the names it has."""
    if name:
        layer = scene.view_layers.get(name)
        if layer is None:
            from .tools import ToolError   # Here, not at the top: the render job imports this module alone.
            raise ToolError(f"No view layer {name!r} in scene {scene.name}; it has "
                            f"{', '.join(l.name for l in scene.view_layers)}.")
        return layer
    current = active()
    if current is not None and current.id_data == scene and (current.use or not rendering(scene)):
        return current
    return (rendering(scene) or list(scene.view_layers))[0]


def _collections(layer) -> dict:
    """{"excludes", "holdout", "indirect only": [collection names]}: the outermost of each (a child
    of an excluded collection is not listed again)."""
    found = {"excludes": [], "holdout": [], "indirect only": []}

    def walk(parent):
        for child in parent.children:
            if child.exclude:
                found["excludes"].append(child.name)
                continue
            if child.holdout:
                found["holdout"].append(child.name)
            if child.indirect_only:
                found["indirect only"].append(child.name)
            walk(child)

    walk(layer.layer_collection)
    return found


def world(scene, layer):
    """The world a render of this layer uses: its override, else the scene's."""
    return getattr(layer, "world_override", None) or scene.world


def lights(scene, layer) -> list:
    """The lights a render of this layer lights with: in the layer, rendered, with some power."""
    return [o for o in scene.objects if o.type == "LIGHT" and not o.hide_render and getattr(o.data, "energy", 0) > 0
            and includes(layer, o) and any(not c.hide_render for c in o.users_collection)]


def listed(names) -> str:
    names = list(names)
    return ", ".join(names[:MAX_NAMES]) + (f" +{len(names) - MAX_NAMES} more" if len(names) > MAX_NAMES else "")


def summary(scene, layer) -> str:
    """The layer as a render uses it, in words: "view layer Stage (excludes Props; holdout Glass;
    world override Dusk)". Just "view layer Stage" when it has none of these."""
    parts = [f"{kind} {listed(names)}" for kind, names in _collections(layer).items() if names]
    for attr, words in (("world_override", "world override"), ("material_override", "material override")):
        value = getattr(layer, attr, None)
        if value is not None:
            parts.append(f"{words} {value.name}")
    if getattr(layer, "samples", 0):
        parts.append(f"its own {layer.samples} samples")
    return f"view layer {layer.name}" + (f" ({'; '.join(parts)})" if parts else "")


def overview(scene) -> dict | None:
    """get_scene_info's view layers, when there is more than the one plain layer: {name: {"use",
    "excludes", "holdout", "indirect only", "world_override", "material_override"}} with the empty
    ones left out, and which is active. None for a single layer with nothing excluded or overridden."""
    rows = {}
    for layer in scene.view_layers:
        row = {"use": layer.use, **{k: v for k, v in _collections(layer).items() if v}}
        for attr in ("world_override", "material_override"):
            value = getattr(layer, attr, None)
            if value is not None:
                row[attr] = value.name
        rows[layer.name] = row
    if len(rows) == 1 and list(next(iter(rows.values()))) == ["use"]:
        return None
    current = active()
    return {"active": current.name if current is not None else None, "layers": rows}


def composited(scene, asked: bool) -> bool:
    """Whether a production render is the compositor's mix of several view layers: more than one
    renders, the compositor is on and no layer was asked for. Then the whole render is the shot;
    otherwise one layer is rendered (render.render's layer, its use turned on for the render)."""
    return not asked and bool(scene.render.use_compositing) and len(rendering(scene)) > 1


def described(scene, layer, asked: bool = True) -> dict:
    """The context a production render uses, as data: the layer in words (summary), its world and
    lights. With composited, every rendering layer."""
    if composited(scene, asked):
        return {"view_layer": "the compositor's mix of " + ", ".join(l.name for l in rendering(scene)),
                "world": scene.world.name if scene.world else None}
    used = world(scene, layer)
    return {"view_layer": summary(scene, layer), "world": used.name if used else None,
            "lights": [o.name for o in lights(scene, layer)]}

