"""A local render whose scene names missing files renders without them and says which; a packed image
is never missing, an attempt after a missing file appears is refused, and a Quick Smoke domain's cache
folder that doesn't exist yet is still refused up front, as in 0.2.18.
Blender -b --factory-startup --python-exit-code 1 --python loopcut/harness/missing_assets_check.py
rel018-ru-en: a BlenderKit 1K .blend named an unpacked roughness map that was never downloaded and a
lamp's IES profile at its author's C:\\ path, and every look at the room was refused.
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).parent))
import checkout
checkout.use()
from loopcut import inspection, job_tools, jobs, tools

temporary = tempfile.TemporaryDirectory(prefix="loopcut-missing-assets-check-")
root = Path(os.environ.get("LOOPCUT_MISSING_ASSETS_CHECK_DIR", temporary.name)).resolve()
root.mkdir(parents=True, exist_ok=True)
os.environ["LOOPCUT_DATA_DIR"] = str(root / "data")
IES = "C:/Users/pc/Downloads/aasadsdad.ies"


def wait(path, timeout=150):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = jobs.status(path)
        if row["state"] not in jobs.ACTIVE:
            return row
        time.sleep(.2)
    raise AssertionError(f"job did not finish: {row}")


def texture(name, rgba):
    """An 8x8 PNG written to disk and loaded from it, as an imported asset's texture is."""
    path = root / f"{name}.png"
    image = bpy.data.images.new(name, 8, 8)
    image.pixels[:] = list(rgba) * 64
    image.filepath_raw, image.file_format = str(path), "PNG"
    image.save()
    bpy.data.images.remove(image)
    return bpy.data.images.load(str(path)), path


scene = bpy.context.scene
scene.render.engine = "BLENDER_EEVEE"
packed, packed_file = texture("packed_wood", (.1, .8, .1, 1))
packed.pack()
packed_file.unlink()  # Inside the .blend now, as BlenderKit packs most of a model's textures.
roughness, roughness_file = texture("SP_2K_Roughness", (.5, .5, .5, 1))
roughness_file.unlink()  # Named by the .blend, never downloaded.
material = bpy.data.materials.new("Side table")
material.use_nodes = True
nodes, links = material.node_tree.nodes, material.node_tree.links
bsdf = nodes["Principled BSDF"]
for image, socket in ((roughness, "Base Color"), (packed, "Roughness")):
    node = nodes.new("ShaderNodeTexImage")
    node.image = image
    links.new(node.outputs["Color"], bsdf.inputs[socket])
cube = bpy.data.objects["Cube"]
cube.data.materials.clear()
cube.data.materials.append(material)
light = bpy.data.lights["Light"]
if light.node_tree is None:
    light.use_nodes = True
ies = light.node_tree.nodes.new("ShaderNodeTexIES")
ies.mode, ies.filepath = "EXTERNAL", IES

assets, missing = job_tools.external_assets()
named = {asset["path"] for asset in missing}
assert named == {str(roughness_file), IES}, missing
assert not any("packed_wood" in asset["path"] for asset in assets + missing), "a packed image is inside the file"
print("MISSING listed, packed image not:", sorted(named), flush=True)

result = job_tools.start_render_job(output_dir=str(root / "outputs"), frames=[1], width=32, height=32, samples=1,
                                    budget_seconds=150)
assert "Renders without these missing files" in result.text, result.text
assert str(roughness_file) in result.text and IES in result.text, result.text
job_id = json.loads(result.text.partition("\n")[0])["id"]
row = wait(job_tools._path(job_id))
assert row["state"] == "complete" and row["progress"]["verified_complete"], row
status = job_tools.render_job_status(job_id).text
assert f"Rendered without these missing files: {roughness_file}" in status and "shows magenta" in status, status
assert "no IES profile" in status, status
frame = bpy.data.images.load(str(Path(row["output"]) / row["progress"]["files"][0]["file"]))
r, g, b, _ = frame.pixels[(16 * 32 + 16) * 4:(16 * 32 + 16) * 4 + 4]
assert r > 2 * g and b > 2 * g, ("the missing base color is drawn magenta", r, g, b)
print("RENDERED without them; the cube's missing texture is magenta", (round(r, 3), round(g, 3), round(b, 3)), flush=True)

inspection.register()
request = inspection.inspect_scene("final_lighting", max_side=128, samples=1)
assert wait(request.pending_job)["state"] == "complete"
look = inspection.finish(request.pending_job)
assert look.ok and look.image_path.is_file(), look.text
assert "Rendered without these missing files" in look.text and IES in look.text, look.text
inspection.unregister()
print("INSPECTED final_lighting with the missing files named", flush=True)

roughness_file.write_bytes(b"downloaded since")
assert jobs.assets_changed(row)
job_tools.resume_render_job(job_id)
again = wait(job_tools._path(job_id))
assert again["state"] == "failed" and "external scene asset changed" in (again.get("error") or ""), again
print("REFUSED an attempt after the missing file appeared", flush=True)

# Quick Smoke: its domain names a Replay cache folder that playing the simulation makes. A job that
# went on without it would write that folder into the project from its own Blender, then fail itself for
# an asset that appeared, and refuse every later job. This Blender makes the folder as it evaluates the
# domain, so it is removed here to stand for a saved file opened before any play.
import shutil
project = root / "project"
project.mkdir()
bpy.ops.wm.save_as_mainfile(filepath=str(project / "room.blend"))
for obj in bpy.context.view_layer.objects:
    obj.select_set(obj == cube)
bpy.context.view_layer.objects.active = cube
assert bpy.ops.object.quick_smoke() == {"FINISHED"}
domain = next(o for o in scene.objects if any(m.type == "FLUID" and m.fluid_type == "DOMAIN" for m in o.modifiers))
for folder in project.glob("cache_fluid*"):
    shutil.rmtree(folder)
try:
    job_tools.external_assets()
    refused = ""
except jobs.JobError as ex:
    refused = str(ex)
assert "External asset is missing" in refused and "cache_fluid" in refused and f'object "{domain.name}"' in refused, refused
print("REFUSED Quick Smoke's missing cache folder:", refused[:200], flush=True)
before = {row["id"] for row in jobs.list_jobs(job_tools.checkpoints.data_root())}
try:
    job_tools.start_render_job(output_dir=str(root / "outputs"), frames=[1, 2, 3], width=32, height=32, samples=1,
                               budget_seconds=150)
    refused = ""
except tools.ToolError as ex:
    refused = str(ex)
assert "cache_fluid" in refused and f'object "{domain.name}"' in refused, refused
assert {row["id"] for row in jobs.list_jobs(job_tools.checkpoints.data_root())} == before, "no job was made"
print("REFUSED the 3-frame render job up front, no job made:", refused[:200], flush=True)
print("MISSING ASSETS CHECK OK", flush=True)
