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
    if any(g.name.startswith(".loopcut_inspect") for g in bpy.data.node_groups):
        failed += 1
        print("FAIL scratch node trees were left behind")
    print("API DOCS " + ("FAILED" if failed else "OK"))
    return 1 if failed else 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.exit(code)
