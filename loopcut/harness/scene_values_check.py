"""Values that are not plain floats, through the real snapshot, run_python and get_object_info; and
relative output paths, which must land next to the .blend and never in Blender's working directory.

    Blender -b --factory-startup --python-exit-code 1 --python loopcut/harness/scene_values_check.py

Eval 20260927-043942 shared_support: once a BlenderKit floor lamp came in, every run_python failed
with "type Vector doesn't define __round__ method", even print("x"). Its wire has a hook modifier,
whose matrix_inverse is a 4x4 FLOAT property, and the snapshot run_python takes before the model's
code rounded it row by row. Built here: that hook, a Child Of constraint (inverse_matrix, also 4x4),
custom properties holding a Vector, a Matrix, an IDProperty array and a group, a driver on one, and a
Mapping node's Vector and Euler inputs. Then the old rounding is put back once, to show it fails in
real Blender and is reported as the tool's failure, not as bad arguments.

Relative paths: render_job output_dir and the file tools resolved "product_reveal_final/" against
Blender's working directory, which during evals is the harness folder in the git checkout.
"""
import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path

import bpy
from mathutils import Euler, Matrix, Vector

sys.path.insert(0, str(Path(__file__).parent))
import checkout
checkout.use()
from loopcut import files, job_tools, scene_diff, tools

temporary = tempfile.TemporaryDirectory(prefix="loopcut-values-check-")
work = Path(temporary.name).resolve()
os.environ["LOOPCUT_DATA_DIR"] = str(work / "data")
tools._editor_context = lambda editor: contextlib.nullcontext()  # Headless: no windows to borrow.


def run(name, **arguments):
    result = tools.execute(name, json.dumps(arguments))
    assert result.ok, f"{name} failed: {result.text}"
    return result


# ---------------------------------------------------------------- the lamp and its neighbours

scene = bpy.context.scene
curve = bpy.data.curves.new("FloorLamp_Wire", "CURVE")
curve.splines.new("BEZIER").bezier_points.add(1)
wire = bpy.data.objects.new("FloorLamp_Wire", curve)
scene.collection.objects.link(wire)
stem = bpy.data.objects.new("FloorLamp_Stem", None)
scene.collection.objects.link(stem)
hook = wire.modifiers.new("Hook-Cylinder", "HOOK")
hook.object = stem
hook.matrix_inverse = Matrix.Translation((0.006, 0.006, -1.309))

cube = bpy.data.objects["Cube"]
cube.constraints.new("CHILD_OF").inverse_matrix = Matrix.Translation((0, 0, 0.25))
cube["offset"] = Vector((1, 2, 3))
cube["rest"] = Matrix.Identity(3)  # Stored flat: an IDProperty array of 9.
cube["weights"] = [0.123456, 2.0]
cube["blenderkit"] = {"asset_base_id": "c674300a", "size": Vector((0.3, 0.3, 1.4))}
driver = cube.driver_add('["weights"]', 0).driver
driver.expression = "frame / 100"
material = bpy.data.materials.new("Lamp Bulb")
mapping = material.node_tree.nodes.new("ShaderNodeMapping")
mapping.inputs["Location"].default_value = Vector((0.1, 0.2, 0.3))
mapping.inputs["Rotation"].default_value = Euler((0.5, 0, 0))
cube.data.materials.append(material)

before = scene_diff.snapshot()
assert before["objects"]["FloorLamp_Wire"]["modifiers"]["Hook-Cylinder"]["matrix_inverse"][2] == (0.0, 0.0, 1.0, -1.309)
result = run("run_python", code='print("x")')
assert result.text.startswith("x"), result.text
result = run("run_python", code="bpy.data.objects['FloorLamp_Wire'].modifiers[0].matrix_inverse = "
                                "__import__('mathutils').Matrix.Translation((0, 0, -1.2))")
assert "modifier Hook-Cylinder: matrix_inverse" in result.text, result.text
info = json.loads(run("get_object_info", names=["Cube", "FloorLamp_Wire"]).text)
custom = info[0]["custom_properties"]
assert custom["offset"] == [1.0, 2.0, 3.0] and custom["weights"] == [0.01, 2.0], custom  # Driven: frame / 100.
assert custom["rest"] == [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0], custom
assert custom["blenderkit"]["size"] == [0.3, 0.3, 1.4], custom
assert info[0]["constraints"][0]["inverse_matrix"][2] == [0.0, 0.0, 1.0, 0.25], info[0]["constraints"]
assert info[1]["modifiers"][0]["matrix_inverse"][2] == [0.0, 0.0, 1.0, -1.2], info[1]["modifiers"]
print("VALUES OK: snapshot, run_python, get_object_info with matrices, vectors and ID property arrays")

# The old rounding, once: a snapshot rounding the real hook's rows fails as the eval did, and the
# message now names the real error instead of "Bad arguments for run_python".
fixed = scene_diff.snapshot
scene_diff.snapshot = lambda guard=None: tuple(round(v, 4) for v in wire.modifiers[0].matrix_inverse)
try:
    old = tools.execute("run_python", json.dumps({"code": 'print("x")'}))
finally:
    scene_diff.snapshot = fixed
assert not old.ok and old.text == ("run_python failed inside the tool, not because of your arguments: "
                                   "TypeError: type Vector doesn't define __round__ method"), old.text
print(f"VALUES OLD ROUNDING: {old.text}")

# ---------------------------------------------------------------- a textured mesh, read whole or by its layout

# Four materials of 24 texture and mix nodes each: far past the result's 8,000 characters, which once
# cut the result in the middle of a node's values. Whole sections go, largest first, and are named.
bpy.ops.mesh.primitive_uv_sphere_add()
bottle = bpy.context.object
bottle.name = "Bottle"
bpy.ops.mesh.primitive_cube_add(location=(0, 0, 1.2))
bpy.context.object.name = "Bottle_Cap"
bpy.context.object.parent = bottle
bottle.modifiers.new("Bevel", "BEVEL")
for n in range(4):
    textured = bpy.data.materials.new(f"Label {n}")
    tree = textured.node_tree
    last = tree.nodes["Principled BSDF"]
    for k in range(24):
        node = tree.nodes.new(("ShaderNodeTexNoise", "ShaderNodeTexImage", "ShaderNodeMix", "ShaderNodeValToRGB")[k % 4])
        tree.links.new(node.outputs[0], last.inputs[k % 6])
    bottle.data.materials.append(textured)
bottle.data.materials.append(None)  # An empty slot.
result = run("get_object_info", names=["Bottle"])
body, _, note = result.text.partition("\nLeft out")
assert len(body) <= tools.MAX_OUTPUT_CHARS and note.startswith(" to fit 8,000 characters"), (len(body), note)
entry = json.loads(body)[0]  # Whole JSON: nothing cut in the middle of a value.
assert entry["name"] == "Bottle" and entry["bounds"] and entry["modifiers"][0]["name"] == "Bevel", entry.keys()
assert any(o.startswith("material Label") for o in entry["omitted"]), entry["omitted"]
for material in entry.get("materials", []):
    assert material["links"] and material["nodes"], "a material shown is shown whole"
brief = json.loads(run("get_object_info", names=["Bottle"], detail="transform").text)[0]
assert set(brief) == {"name", "type", "location", "scale", "visible", "selected", "bounds", "modifiers",
                      "dimensions", "children", "material_slots", "mesh", "rotation_deg"}, sorted(brief)
assert brief["material_slots"] == ["Label 0", "Label 1", "Label 2", "Label 3", ""], brief["material_slots"]
assert brief["children"] == ["Bottle_Cap"] and brief["modifiers"] == ["Bevel (BEVEL)"], brief
assert brief["mesh"]["edges"] == len(bottle.data.edges) and brief["dimensions"] == [2.0, 2.0, 2.0], brief
assert len(json.dumps(brief)) < 700, len(json.dumps(brief))
print(f"VALUES TEXTURED MESH OK: full {len(body)} chars, omitted {entry['omitted']}; transform {len(json.dumps(brief))} chars")

# Mix Shader's two Shader inputs: which one each shader reaches shows by index, in get_object_info and in
# run_python's Scene changes, so a swap is visible. The world reads the same way, by "world".
glass = bpy.data.materials.new("Glass Mix")
nodes, links = glass.node_tree.nodes, glass.node_tree.links
mix = nodes.new("ShaderNodeMixShader")
links.new(nodes.new("ShaderNodeBsdfDiffuse").outputs[0], mix.inputs[1])
links.new(nodes.new("ShaderNodeBsdfGlossy").outputs[0], mix.inputs[2])
links.new(mix.outputs[0], nodes["Material Output"].inputs["Surface"])
bpy.ops.mesh.primitive_cube_add(location=(4, 0, 0))
bpy.context.object.name = "Glass"
bpy.context.object.data.materials.append(glass)
shown = json.loads(run("get_object_info", names=["Glass"]).text)[0]["materials"][0]["links"]
assert "Diffuse BSDF.BSDF -> Mix Shader.Shader (inputs[1])" in shown and \
    "Glossy BSDF.BSDF -> Mix Shader.Shader (inputs[2])" in shown, shown
swap = run("run_python", code="t = bpy.data.materials['Glass Mix'].node_tree\n"
                              "m, d, g = t.nodes['Mix Shader'], t.nodes['Diffuse BSDF'], t.nodes['Glossy BSDF']\n"
                              "t.links.new(g.outputs[0], m.inputs[1])\nt.links.new(d.outputs[0], m.inputs[2])").text
assert "+link Glossy BSDF.BSDF -> Mix Shader.Shader (inputs[1])" in swap, swap
assert "-link Diffuse BSDF.BSDF -> Mix Shader.Shader (inputs[1])" in swap, swap
world = json.loads(run("get_object_info", names=["world"]).text)[0]
assert world["type"] == "WORLD" and world["scene_world"] and world["name"] == scene.world.name, world
assert "Background.Background -> World Output.Surface" in world["links"], world["links"]
assert {n["type"] for n in world["nodes"]} >= {"ShaderNodeBackground", "ShaderNodeOutputWorld"}, world["nodes"]
by_name = json.loads(run("get_object_info", names=[scene.world.name, "Glass"], detail="transform").text)
assert by_name[0]["type"] == "WORLD" and by_name[1]["name"] == "Glass", by_name
print(f"VALUES NODE READOUT OK: {shown}; swap: {[line for line in swap.splitlines() if 'link' in line]}; "
      f"world {world['links']}")

# ---------------------------------------------------------------- relative paths

project, elsewhere = work / "project", work / "checkout"
project.mkdir()
elsewhere.mkdir()
os.chdir(elsewhere)  # Blender's working directory: the git checkout, during evals.
bpy.ops.object.camera_add()
scene.camera = bpy.context.object
bpy.ops.wm.save_as_mainfile(filepath=str(project / "shot.blend"))
spec, _ = job_tools.create_spec("product_reveal_final/", [1], 60)
assert Path(spec["output"]).parent == project.resolve() / "product_reveal_final", spec["output"]
assert Path(job_tools.create_spec("//finals", [1], 60)[0]["output"]).parent == project.resolve() / "finals"
run("write_file", path="notes.txt", content="x")
assert (project / "notes.txt").is_file() and not (elsewhere / "notes.txt").exists()
assert files.relative_note("product_reveal_final/") == ""
assert not any(elsewhere.iterdir()), list(elsewhere.iterdir())
print(f"PATHS SAVED OK: relative output under {project}")

bpy.ops.wm.read_factory_settings()
scene = bpy.context.scene
scene.render.filepath = str(work / "renders") + "/"
bpy.ops.object.camera_add()
scene.camera = bpy.context.object
spec, _ = job_tools.create_spec("product_reveal_final/", [1], 60)
assert Path(spec["output"]).parent == (work / "renders").resolve() / "product_reveal_final", spec["output"]
note = files.relative_note("product_reveal_final/")
assert "not saved" in note and str((work / "renders").resolve()) in note, note
(work / "renders").mkdir()
assert "not saved" in run("write_file", path="notes.txt", content="x").text
assert (work / "renders" / "notes.txt").is_file() and not any(elsewhere.iterdir())
print(f"PATHS UNSAVED OK:{note}")
print("SCENE VALUES OK")
