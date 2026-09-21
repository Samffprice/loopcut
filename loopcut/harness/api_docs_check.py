"""inspect_api against the real API, no model or window needed:
    Blender -b --factory-startup --python harness/api_docs_check.py
Asserts the answers a model most often gets wrong from memory are in the output."""

import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import checkout  # noqa: E402
checkout.use()
import bpy  # noqa: E402,F401
from loopcut import api_docs  # noqa: E402

CASES = [
    # (kwargs, substrings that must all appear)
    ({"path": "bpy.types.BevelModifier"}, ["segments: int", "width: float", "inherited from Modifier"]),
    ({"path": "BevelModifier.limit_method"}, ["enum [", "'ANGLE'"]),
    ({"path": "bpy.types.Object.modifiers"}, ["ObjectModifiers", "new(name, type)", "items are bpy.types.Modifier"]),
    ({"path": "bpy.ops.mesh.primitive_cube_add"}, ["size: float", "location", "poll"]),
    ({"path": "bpy.ops.mesh"}, ["primitive_cube_add", "subdivide"]),
    ({"path": "ShaderNodeTexChecker"}, ["inputs:", "'Scale'", "'Color1'", "outputs:", "'Color'"]),
    ({"path": "GeometryNodeDistributePointsOnFaces"}, ["'Density'", "'Points'", "distribute_method"]),
    ({"path": "bpy.data.node_groups"}, ["BlendDataNodeTrees", "new(name, type)"]),
    ({"path": "bpy.types.NodeTreeInterface.new_socket"}, ["in_out", "socket_type"]),
    ({"path": "bmesh.ops.bevel"}, ["offset"]),
    ({"path": "mathutils.Vector"}, ["members:", "to_track_quat"]),
    ({"path": "bpy.types.NodeTreeInterface.new_socket"}, ["'NodeSocketGeometry'", "'NodeSocketFloat'"]),
    ({"path": "bpy.types.GeometryNodeTree"}, ["NOTE:", "tree.interface.new_socket"]),
    ({"path": "bpy.types.Action"}, ["NOTE:", "channelbag(slot)"]),
    ({"search": "bevel modifier"}, ["bpy.types.BevelModifier"]),
    ({"search": "shade smooth"}, ["bpy.ops.object.shade_smooth"]),
]
ERRORS = [
    ({"path": "bpy.types.NoSuchThing"}, "No type"),
    ({"path": "GeometryNodeMeshIcosahedron"}, "GeometryNodeMeshIcoSphere"),
    ({"path": "bpy.types.ShaderNodeBsdfPrincipledd"}, "ShaderNodeBsdfPrincipled"),
    ({"path": "bpy.types.Object.nope"}, "has no property or function"),
    ({"path": "bpy.ops.mesh.nope"}, "No operator"),
    ({}, "exactly one"),
    ({"path": "a", "search": "b"}, "exactly one"),
]


def notes_are_true() -> int:
    """Run what the NOTES tell the model to write. A note that no longer works is worse than none."""
    try:
        tree = bpy.data.node_groups.new("NoteCheck", "GeometryNodeTree")
        assert not hasattr(tree, "inputs"), "NOTES['NodeTree'] says tree.inputs is gone"
        tree.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
        tree.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
        group_in, group_out = tree.nodes.new("NodeGroupInput"), tree.nodes.new("NodeGroupOutput")
        tree.links.new(group_in.outputs[0], group_out.inputs[0])
        bpy.data.node_groups.remove(tree)

        cube = bpy.data.objects["Cube"]
        cube.keyframe_insert("location", frame=1)
        action = cube.animation_data.action
        assert not hasattr(action, "fcurves"), "NOTES['Action'] says action.fcurves is gone"
        bag = action.layers[0].strips[0].channelbag(cube.animation_data.action_slot)
        assert len(list(bag.fcurves)) == 3
        print("ok   the code in NOTES runs on this Blender")
        return 0
    except Exception:
        print("FAIL a NOTE in api_docs.py is wrong for this Blender:\n" + traceback.format_exc())
        return 1


def addons_are_seen() -> int:
    """An add-on enabled after the first search is still found, described, and named to the model.
    The fixture add-on stands in for one the user installed; Blender's own stay unlisted."""
    import addon_utils
    from loopcut import addons
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
        api_docs.inspect_api(search="probe say")  # Builds the index before the add-on exists.
        # default_set: enabled the way the preferences do it, which is what addons.enabled() reads.
        addon_utils.enable("node_wrangler", default_set=True)  # Bundled with Blender: known, unlisted.
        addon_utils.enable("loopcut_probe_addon", default_set=True)
        assert bpy.context.preferences.addons.get("loopcut_probe_addon"), "fixture add-on did not enable"
        found = api_docs.inspect_api(search="probe say")
        assert "bpy.ops.probe.say_hi" in found and "bpy.ops.probe.say_bye" in found, found
        described = api_docs.inspect_api(path="bpy.ops.probe.say_hi")
        assert "Print a greeting" in described and "times: int" in described and "How many greetings" in described, described
        module = api_docs.inspect_api(path="loopcut_probe_addon")
        assert "members:" in module and "PROBE_OT_say_hi" in module, module
        line = addons.line(addons.enabled())
        assert line.startswith("Installed add-ons: Loopcut Probe [loopcut_probe_addon] "
                               "bpy.ops.probe.say_* (2), bpy.ops.wm.probe_wave: A tiny add-on"), line
        assert "Node Wrangler" not in line and "loopcut" not in line.split(":", 1)[1].lower().replace("loopcut probe", "").replace("loopcut_probe", ""), line
        addon_utils.disable("loopcut_probe_addon", default_set=True)
        assert "Loopcut Probe" not in addons.line(addons.enabled()), "a disabled add-on stays listed"
        assert "Nothing in bpy.types" in api_docs.inspect_api(search="probe say"), "the index kept a disabled add-on"
        print("ok   an add-on enabled mid-session is searchable, described, and named to the model")
        return 0
    except Exception:
        print("FAIL add-ons:\n" + traceback.format_exc())
        return 1


def main() -> int:
    failed = 0
    for kwargs, wanted in CASES:
        try:
            text = api_docs.inspect_api(**kwargs)
            missing = [w for w in wanted if w not in text]
        except Exception:
            text, missing = traceback.format_exc(), ["(raised)"]
        if missing or len(text) > api_docs.MAX_CHARS + 100:
            failed += 1
            print(f"FAIL {kwargs}: missing {missing}\n{text[:1500]}\n")
        else:
            print(f"ok   {kwargs} ({len(text)} chars)")
    for kwargs, wanted in ERRORS:
        try:
            api_docs.inspect_api(**kwargs)
            failed += 1
            print(f"FAIL {kwargs}: did not raise")
        except api_docs.ApiError as ex:
            ok = wanted in str(ex)
            failed += not ok
            print(f"{'ok  ' if ok else 'FAIL'} {kwargs} -> {str(ex)[:100]}")
    failed += notes_are_true()
    failed += addons_are_seen()
    if any(g.name.startswith(".loopcut_inspect") for g in bpy.data.node_groups):
        failed += 1
        print("FAIL scratch node trees were left behind")
    print("API DOCS " + ("FAILED" if failed else "OK"))
    return 1 if failed else 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.exit(code)
