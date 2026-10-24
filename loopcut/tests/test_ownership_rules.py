"""What a worker may create and change, and how the merge reports it, where no Blender is needed: the
materials aspect and a worker's own material copies; animation for the roles that animate; what a
brief's own words ask for, whatever the role, and the reason given for what is still held back. The
real append, rollback and checks are loopcut/harness/merge_check.py's materials and shot waves and
worker_host_check.py's rollback case."""

import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

fake_bpy = types.ModuleType("bpy")
fake_bpy.app = types.SimpleNamespace(driver_namespace={})
sys.modules.setdefault("bpy", fake_bpy)

from loopcut import merge, ownership  # noqa: E402


def made(id_type: str, kind: str = "", name: str = ""):
    return types.SimpleNamespace(id_type=id_type, type=kind, name=name)


class MaterialsTest(unittest.TestCase):
    def test_the_materials_aspect_lets_any_role_make_materials(self):
        self.assertIn("materials", ownership.ASPECTS)
        material = made("MATERIAL")
        self.assertFalse(ownership.allowed("camera", material))
        self.assertTrue(ownership.allowed("camera", material, ["materials"]))
        self.assertTrue(ownership.allowed("modeling", material), "a modeler's own copies need no aspect")
        self.assertFalse(ownership.allowed("modeling", made("OBJECT", "LIGHT"), ["materials"]), "materials only")


    def test_merge_lines_say_what_happened_to_materials(self):
        report = merge.MergeReport(
            created=[{"worker": "a", "name": "Chair_Wood", "id_type": "MATERIAL", "type": "", "parent": None,
                      "variant_of": "Pal_Wood"}],
            reused=[{"worker": "a", "name": "Pal_Wool", "made": "Chair_Wool", "id_type": "MATERIAL", "copy": True},
                    {"worker": "b", "name": "Pal_Glass", "made": "Pal_Glass.001", "id_type": "MATERIAL"}],
            dropped_edits=[{"worker": "a", "key": "MATERIAL:Pal_Wood", "name": "Pal_Wood"},
                           {"worker": "a", "key": "OBJECT:Rug", "name": "Rug"}])
        lines = report.lines()
        self.assertIn("a: material Chair_Wool is an unchanged copy of the live Pal_Wool: the live one is used, "
                      "no copy merged", lines)
        self.assertIn("b: material Pal_Glass.001 is the live Pal_Glass made again (same name and settings): the live "
                      "one is used, no copy merged", lines)
        self.assertIn("a: material Chair_Wood is its own variant of Pal_Wood (on its own parts)", lines)
        self.assertIn("a: edit to Pal_Wood dropped (not owned; a palette material changes only with aspect materials, "
                      "a worker's own copy merges)", lines)
        self.assertIn("a: edit to Rug dropped (not owned)", lines)


class AnimationTest(unittest.TestCase):
    def test_roles_that_animate_may_create_actions(self):
        action = made("ACTION")
        for role in ("lighting", "camera", "animation"):
            self.assertTrue(ownership.allowed(role, action), role)
        self.assertFalse(ownership.allowed("modeling", action), "a modeler's keys need the brief to ask for motion")
        self.assertTrue(ownership.allowed("modeling", action, ["lighting"]), "a shot's aspects bring actions")
        self.assertTrue(ownership.allowed("materials", action, ["camera"]))
        self.assertTrue(ownership.allowed("camera", made("OBJECT", "CURVE")), "a path to follow")
        self.assertTrue(ownership.allowed("animation", made("CURVE")))


class AskedForTest(unittest.TestCase):
    LAMP = {"deliverable": "ReadingLamp", "requirements": [{"quote": "a floor lamp that lights the reading corner",
                                                            "do": "floor lamp"}]}

    def test_words_that_ask_for_a_light_or_motion(self):
        self.assertEqual(ownership.asked_for("LIGHT", self.LAMP), "a floor lamp that lights the reading corner")
        for words in ("light grey sofa", "a lightweight chair", "light wood shelves", "a floor-standing vase"):
            self.assertEqual(ownership.asked_for("LIGHT", {"requirements": [{"quote": words}]}), "", words)
        for words in ("glowing crystals", "a lantern", "candles on the table", "the lamp is on"):
            self.assertTrue(ownership.asked_for("LIGHT", {"requirements": [{"quote": words}]}), words)
        self.assertTrue(ownership.asked_for("ACTION", {"defaults": ["blades spin at 1 turn a second"]}))
        self.assertTrue(ownership.asked_for("ACTION", {"requirements": [{"do": "keyframe the lid opening"}]}))
        self.assertEqual(ownership.asked_for("ACTION", {"requirements": [{"quote": "a still life"}]}), "")
        self.assertEqual(ownership.asked_for("WORLD", self.LAMP), "", "a world is lighting's, never asked around it")
        long = {"requirements": [{"quote": "x" * 100 + " and a lamp that lights it " + "y" * 100}]}
        self.assertLessEqual(len(ownership.asked_for("LIGHT", long)), 90)

    def test_names_the_brief_gives(self):
        brief = {"deliverable": "Shot", "owns": ["Rim_Rig"], "checks": [{"kind": "in_frame", "names": ["Bottle"],
                                                                          "camera": "Cam_Hero"}],
                 "requirements": [{"quote": "aim everything at Cam_Target", "do": "an aim empty Cam_Target"}]}
        for name in ("Shot", "Rim_Rig", "Bottle", "Cam_Hero", "Cam_Target", "Cam_Target.001"):
            self.assertTrue(ownership.pinned(name, brief), name)
        for name in ("Cam", "Light", "Other_Thing", "Target"):
            self.assertFalse(ownership.pinned(name, brief), name)
        self.assertFalse(ownership.pinned("Shot", None))

    def test_a_refusal_says_what_the_role_makes_and_what_would_let_it_through(self):
        text = ownership.refusal("modeling", [], "LIGHT")
        self.assertTrue(text.startswith("outside its role: a modeling worker makes curves, empties, materials, meshes, text;"),
                        text)
        self.assertIn("lights need aspect lighting", text)
        self.assertIn("with aspect camera", ownership.refusal("lighting", ["camera"], "WORLD"))

    def test_merge_lines_give_the_reason_for_what_is_held_and_what_came_through(self):
        report = merge.MergeReport(
            quarantined=[{"worker": "t", "name": "Lamp_Test", "id_type": "OBJECT", "type": "LIGHT",
                          "reason": "outside its role: ..."}, {"worker": "u", "name": "Old", "id_type": "WORLD", "type": ""}],
            admitted=[{"worker": "t", "name": "Lamp_Bulb", "id_type": "OBJECT", "type": "LIGHT",
                       "reason": "part of Lamp, and the brief asks for it: \"a lamp that lights\""}])
        lines = report.lines()
        self.assertIn("t: Lamp_Test (light) quarantined: outside its role: ...", lines)
        self.assertIn("u: Old (world) quarantined: outside its role", lines)
        self.assertIn("t: Lamp_Bulb (light) merged though outside its role: part of Lamp, and the brief asks for it: "
                      "\"a lamp that lights\"", lines)
        self.assertEqual(report.log()["admitted"], ["Lamp_Bulb"])


class DriverReportTest(unittest.TestCase):
    """The words for drivers a merge re-aims or leaves without a target (merge._settle); the real cases
    are loopcut/harness/apply_links_check.py's applies 5 to 7."""

    def test_lost_and_re_aimed_drivers_are_counted_and_named(self):
        lost = {"worker": "build", "owner": "object Indicator", "path": "location[1]", "variable": "var",
                "target": "the shape keys of mesh Tongue",
                "why": "was aimed at the shape keys of mesh Tongue, which this apply removed: now unset"}
        moved = {"worker": "build", "owner": "object Panel", "path": 'pose.bones["Meter"].location[0]',
                 "variable": "var", "old": "the shape keys of mesh Face", "new": "the shape keys of mesh FaceNew"}
        report = merge.MergeReport(unlinked=[lost, dict(lost, path="location[2]")], retargeted=[moved])
        self.assertIn("2 drivers left without their target", report.headline())
        self.assertIn("1 driver left without its target", merge.MergeReport(unlinked=[lost]).headline())
        self.assertIn("build: driver object Indicator location[1] (variable var) was aimed at the shape keys of mesh "
                      "Tongue, which this apply removed: now unset", report.lines())
        self.assertIn('build: driver object Panel pose.bones["Meter"].location[0] (variable var) re-aimed from the '
                      "shape keys of mesh Face, removed, to the shape keys of mesh FaceNew, which have the same key "
                      "blocks", report.lines())
        self.assertEqual(report.log()["unlinked"][0]["target"], "the shape keys of mesh Tongue")

    def test_the_key_block_a_driver_reads_is_found_through_any_path(self):
        key = types.SimpleNamespace(id_type="KEY")
        face = types.SimpleNamespace(id_type="OBJECT", data=types.SimpleNamespace(shape_keys=key))
        mesh = types.SimpleNamespace(id_type="MESH", shape_keys=key)

        def read(idb, path):
            return merge._key_read(types.SimpleNamespace(id=idb, data_path=path))
        self.assertEqual(read(key, 'key_blocks["smile"].value'), (key, "smile"))
        self.assertEqual(read(face, 'data.shape_keys.key_blocks["mouth \\"open\\""].slider_max'), (key, 'mouth "open"'))
        self.assertEqual(read(mesh, 'shape_keys.key_blocks["smile"].value'), (key, "smile"))
        self.assertEqual(read(face, "location[0]"), (None, None))
        self.assertEqual(read(key, "eval_time"), (None, None))


REPO = Path(__file__).resolve().parents[2]
BLENDER = Path(os.environ.get("LOOPCUT_BLENDER") or REPO.parent / "tools/Blender.app/Contents/MacOS/Blender")
_WEIGHTS = """\
import sys
from pathlib import Path
sys.path.insert(0, ADDONS)
import bmesh
import bpy
from loopcut import merge, ownership
failures = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f": {detail}"), flush=True)
    if not ok:
        failures.append(name)


def layers(mesh):
    bm = bmesh.new()
    bm.from_mesh(mesh)
    count = len(bm.verts.layers.deform)
    bm.free()
    return count


def weighted(name, x):
    bpy.ops.mesh.primitive_cube_add(location=(x, 0, 0))
    ob = bpy.context.object
    ob.name = ob.data.name = name
    ob.vertex_groups.new(name="bone_1").add(list(range(8)), 1.0, "REPLACE")
    ob.vertex_groups.new(name="bone_2").add([0, 1], 0.5, "REPLACE")
    return ob


check("the add-on under test", ownership.__file__.startswith(ADDONS), ownership.__file__)
bpy.ops.wm.read_factory_settings(use_empty=True)
dog, cat = weighted("Dog", 0), weighted("Cat", 3)
dog.modifiers.new("Wrap", "SHRINKWRAP").target = cat  # Cat goes into the result as an unowned copy.
weighted("Fox", 9)  # Unowned too, and nothing the worker makes or owns refers to it: never in the result.
bpy.ops.mesh.primitive_cube_add(location=(6, 0, 0))
rock = bpy.context.object
rock.name = rock.data.name = "Rock"
rock_tokens = (ownership.token(rock), ownership.token(rock.data))
check("an object without vertex groups: no weights read, so no deform layer made", layers(rock.data) == 0)
base = (ownership.token(dog), ownership.token(dog.data))
dog.vertex_groups["bone_1"].add([0], 0.25, "REPLACE")
check("a weight-only edit changes the object's token", ownership.token(dog) != base[0])
check("...and not the mesh's (weights count on the object)", ownership.token(dog.data) == base[1])
dog.vertex_groups["bone_1"].add([0], 1.0, "REPLACE")
check("the same weights again: the same token", ownership.token(dog) == base[0])
dog.vertex_groups["bone_2"].name = "bone_2b"
check("a vertex group renamed changes the object's token", ownership.token(dog) != base[0])
dog.vertex_groups["bone_2b"].name = "bone_2"
bpy.context.view_layer.objects.active = dog
bpy.ops.object.mode_set(mode="EDIT")
in_edit = ownership.token(dog)
bpy.ops.object.mode_set(mode="OBJECT")
check("Edit Mode and back read the same", in_edit == base[0] == ownership.token(dog), [in_edit, base[0]])
rock.data.uv_layers.active.uv[0].vector = (0.5, 0.5)
check("a UV edit still changes the mesh token", ownership.token(rock.data) != rock_tokens[1])
uv_token = ownership.token(rock.data)
rock.shape_key_add(name="Basis")
check("a shape key still changes the mesh token", ownership.token(rock.data) != uv_token)
check("the object without groups keeps its token", ownership.token(rock) == rock_tokens[0])
pup = weighted("Pup", 12)  # Owned, its mesh shared with the unowned PupTwin: the mesh is not owned.
twin = bpy.data.objects.new("PupTwin", pup.data)
bpy.context.scene.collection.objects.link(twin)
bpy.ops.mesh.primitive_cube_add(location=(15, 0, 0))
bare = bpy.context.object
bare.name = bare.data.name = "Bare"
bare.vertex_groups.new(name="bone_1")  # A group, no weight assigned.
bare_before = (layers(bare.data), sorted(a.name for a in bare.data.attributes), ownership.token(bare.data))
bare_token = ownership.token(bare)
check("groups but no weights: the first token adds an empty deform layer, which no token or attribute sees",
      bare_before[0] == 0 and layers(bare.data) == 1 and ownership.token(bare) == bare_token
      and sorted(a.name for a in bare.data.attributes) == bare_before[1] and ownership.token(bare.data) == bare_before[2],
      [bare_before, layers(bare.data)])
check("describe shows the weights of an object with groups", "weights" in ownership.describe(dog)
      and "weights" not in ownership.describe(rock))
hashed, full_hash = [], ownership.weights_hash
ownership.weights_hash = lambda mesh: hashed.append(mesh.name) or full_hash(mesh)
ownership.spawn_state()
ownership.weights_hash = full_hash
check("one pass hashes a shared mesh's weights once", hashed.count("Pup") == 1, hashed)

# A wave: a rig worker owns Dog (and its single-user mesh) and fixes weights; it also edits Cat's,
# which it does not own and Dog's modifier refers to. Per call the worker compares weight-free tokens
# (worker_child._changed), so that edit is not rolled back: the merge drops and reports it. The child runs in this same process on the spawn copy; the live file is
# then reopened, its identity map rebuilt the way save_spawn makes it.
root = Path(ROOT)
live_path = root / "live.blend"
bpy.ops.wm.save_as_mainfile(filepath=str(live_path))
_, in_memory = ownership.spawn_state()
spawn = merge.save_spawn(root / "job" / "w1")
owned = ownership.expand_owns(["Dog", "Pup"], [])
check("Dog is owned with its mesh, Pup without its shared one", owned == {"OBJECT:Dog", "MESH:Dog", "OBJECT:Pup"},
      sorted(owned))
bpy.ops.wm.open_mainfile(filepath=str(spawn.path))
child = ownership.stamp_all()
check("tokens survive the spawn copy's save and load", child == in_memory == spawn.tokens,
      sorted(k for k in in_memory if child.get(k) != in_memory[k]))
check("the per-call map: weight-free only for objects with vertex groups",
      ownership.CALL_TOKENS["OBJECT:Rock"] == child["OBJECT:Rock"] and ownership.CALL_TOKENS["MESH:Cat"] == child[
          "MESH:Cat"] and ownership.CALL_TOKENS["OBJECT:Cat"] != child["OBJECT:Cat"])
bpy.data.objects["Dog"].vertex_groups["bone_1"].add([0], 0.25, "REPLACE")
cat = bpy.data.objects["Cat"]
cat.vertex_groups["bone_1"].add([1], 0.1, "REPLACE")
bpy.data.objects["Fox"].vertex_groups["bone_1"].add([2], 0.3, "REPLACE")
bpy.data.objects["Pup"].vertex_groups["bone_1"].add([4], 0.4, "REPLACE")  # The shared mesh: the known gap.
bpy.context.scene.collection.objects.link(bpy.data.objects.new("DogRig_Pivot", None))  # Created: never "dropped".
hashed, full_hash = [], ownership.weights_hash
ownership.weights_hash = lambda mesh: hashed.append(mesh.name) or full_hash(mesh)
import types
from loopcut import worker_child
worker = types.SimpleNamespace(owned=owned, prefix="", not_undone=set(), spawn_tokens=child)
per_call = worker_child.Worker._changed(worker)  # What _after_call rolls back for.
check("per call: the unowned Cat's weight edit is not caught, and no weights are hashed",
      per_call == [] and hashed == [], [per_call, hashed])
cat.vertex_groups["bone_2"].name = "bone_2b"
check("per call: a vertex group renamed on the unowned Cat still is",
      worker_child.Worker._changed(worker) == ["OBJECT:Cat"] and hashed == [], hashed)
cat.vertex_groups["bone_2b"].name = "bone_2"
check("with full tokens the child finds the weight edits", ownership.changed_keys(ownership.SPAWN_TOKENS, owned)
      == ["OBJECT:Cat", "OBJECT:Fox", "OBJECT:PupTwin"] and hashed, hashed)
ownership.weights_hash = full_hash
edited = worker_child.Worker._unowned_weight_edits(worker)
check("check_brief's full-weight look finds the unowned weight edits", edited == ["OBJECT:Cat", "OBJECT:Fox",
      "OBJECT:PupTwin"], edited)
checking = types.SimpleNamespace(checks=[], advisory=[], craft_look=True, _scope=lambda: {"owned": sorted(owned)},
                                 _unowned_weight_edits=lambda: edited)
said = worker_child.Worker._check_brief(checking, 1).text
check("...and check_brief tells the worker", "Weights changed on" in said and "PupTwin" in said
      and "the merge will drop those edits" in said.lower(), said)
# What worker_child.finish does before write_result: facts and the final look, which must not show up
# as edits (changed_not_owned is reported at the merge).
from loopcut import facts, worker_capture
worker_capture.configure("animation", root / "captures", 256, None, brief={
    "deliverable": "DogRig", "prefix": "DogRig", "owned": sorted(owned), "checks": [], "reference": None})
facts.deliverables(worker_capture.own_objects(bpy.context.scene), [])
try:
    looked = str(worker_capture.cell("dogrig", root / "cell"))
except Exception as ex:  # noqa: BLE001 - said, not failed: the look is the noise probe, not the subject.
    looked = f"{type(ex).__name__}: {ex}"
print(f"NOTE final look: {looked}", flush=True)
# A weight edit left in Edit Mode at finish (the review: "0 replaced"): flushed before the result.
import bmesh
dog = bpy.data.objects["Dog"]
for ob in bpy.context.scene.objects:
    ob.select_set(ob == dog)  # Edit Mode on Dog alone.
bpy.context.view_layer.objects.active = dog
bpy.ops.object.mode_set(mode="EDIT")
edit = bmesh.from_edit_mesh(dog.data)
deform = edit.verts.layers.deform.verify()
edit.verts.ensure_lookup_table()
edit.verts[3][deform][dog.vertex_groups["bone_1"].index] = 0.6
bmesh.update_edit_mesh(dog.data)
del edit, deform
check("finish flushes Edit Mode", worker_child.flush_edit_mode() == ["Dog"] and dog.mode == "EDIT")
manifest = ownership.write_result(str(root / "job" / "w1" / "dogrig" / "result.blend"), owned, "DogRig", "animation")
bpy.ops.wm.open_mainfile(filepath=str(live_path))
ids, tokens = ownership.spawn_state()
check("tokens survive the live file's save and reload", tokens == in_memory)
spawn = merge.Spawn(spawn.wave, spawn.path, ids, tokens, {"camera": None, "world": None})
brief = {"id": "dogrig", "prefix": "DogRig", "role": "animation", "aspects": [], "deliverable": "DogRig",
         "requirements": [], "checks": []}
report = merge.merge_wave(spawn, [{"brief": brief, "manifest": manifest}], lambda: "")
replaced = sorted(e["key"] for e in report.replaced)
dog, cat = bpy.data.objects["Dog"], bpy.data.objects["Cat"]
check("the owned Dog, changed only in weights, is replaced with its mesh", replaced == ["MESH:Dog", "OBJECT:Dog"],
      replaced)
check("...and the live Dog has the worker's weights, the one left in Edit Mode too",
      round(dog.vertex_groups["bone_1"].weight(0), 4) == 0.25 and round(dog.vertex_groups["bone_1"].weight(3), 4) == 0.6,
      [dog.vertex_groups["bone_1"].weight(0), dog.vertex_groups["bone_1"].weight(3)])
check("at the end (write_result, full tokens) Cat, Fox and PupTwin are listed as changed, nothing else (no look "
      "or facts noise)", manifest["advisory"]["changed_not_owned"] == ["OBJECT:Cat", "OBJECT:Fox", "OBJECT:PupTwin"],
      manifest["advisory"])
dropped = {e["key"]: e for e in report.dropped_edits}
check("the unowned Cat's weight edit, not rolled back per call, is dropped at the merge and reported",
      "OBJECT:Cat" in dropped and not dropped["OBJECT:Cat"].get("in_copy_only")
      and cat.vertex_groups["bone_1"].weight(1) == 1.0, report.dropped_edits)
fox = bpy.data.objects["Fox"]
check("the unreferenced Fox's edit, which never left the worker's copy, is reported as dropped too",
      dropped.get("OBJECT:Fox", {}).get("in_copy_only") and fox.vertex_groups["bone_1"].weight(2) == 1.0
      and "dogrig: edit to Fox dropped (not owned)" in report.lines(), [report.dropped_edits, report.lines()])
check("each once, and never what the worker created; the owned Pup only for its shared mesh",
      len(report.dropped_edits) == 4 and sorted(dropped) == ["OBJECT:Cat", "OBJECT:Fox", "OBJECT:Pup", "OBJECT:PupTwin"]
      and dropped["OBJECT:Pup"].get("mesh_not_owned"), report.dropped_edits)
check("the shared-mesh weight edit is said naming the owned Pup", manifest["advisory"]["weights_not_merged"]
      == ["OBJECT:Pup"] and any(line.startswith("dogrig: weight edit to Pup dropped (its mesh is shared")
                                for line in report.lines()), [manifest["advisory"], report.lines()])
check("a dropped weights-only edit shows different weights", dropped["OBJECT:Cat"]["worker_value"].get("weights")
      != dropped["OBJECT:Cat"]["live_value"].get("weights"), dropped["OBJECT:Cat"])
check("the known gap: Pup's edit to its shared mesh is not merged, and is reported on PupTwin too",
      bpy.data.objects["Pup"].vertex_groups["bone_1"].weight(4) == 1.0 and dropped["OBJECT:PupTwin"].get("in_copy_only"))
from loopcut import waves
check("where dropped edits go: the merge card and the log", "- Fox (edit not kept)" in waves.card_lines(report)
      and waves._log_merge(report, "")["dropped_edits"] == ["Cat", "Fox", "PupTwin", "Pup"],
      [waves.card_lines(report), waves._log_merge(report, "")])
check("no conflicts, only the pivot created, no errors but the background undo note",
      not report.conflicts and [c["name"] for c in report.created] == ["DogRig_Pivot"]
      and not [e for e in report.errors if "undo" not in e], [report.conflicts, report.created, report.errors])
if failures:
    raise SystemExit(f"{len(failures)} failed")
print("WEIGHTS OK", flush=True)
"""


_BUILD = """\
import sys
from pathlib import Path
sys.path.insert(0, ADDONS)
import bpy
from loopcut import merge, ownership
failures = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f": {detail}"), flush=True)
    if not ok:
        failures.append(name)


# A workspace build, as apply_build merges it: the prepare process sets SPAWN_TOKENS from the snapshot
# and never calls stamp_all (no CALL_TOKENS). The build deletes an owned object (Blender drops its
# now unused mesh and material on save) and swaps scene.world (the old World goes the same way).
root = Path(ROOT)
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add()
crate = bpy.context.object
crate.name = crate.data.name = "Crate"
crate.data.materials.append(bpy.data.materials.new("Wood"))
bpy.context.scene.world = bpy.data.worlds.new("Old")
live_path = root / "live.blend"
bpy.ops.wm.save_as_mainfile(filepath=str(live_path))
spawn = merge.save_spawn(root / "ws" / "apply-1")
ownership.stamp_all()  # The snapshot's stamp op.
bpy.data.objects.remove(bpy.data.objects["Crate"])
bpy.context.scene.world = bpy.data.worlds.new("New")
build = root / "build.blend"
bpy.ops.wm.save_as_mainfile(filepath=str(build))
bpy.ops.wm.open_mainfile(filepath=str(build))
ownership.CALL_TOKENS.clear()
ownership.SPAWN_TOKENS.clear()
ownership.SPAWN_TOKENS.update(spawn.tokens)  # workspace_child.prepare
manifest = ownership.write_result(str(root / "ws" / "apply-1" / "result.blend"), {"OBJECT:Crate"}, "", "")
check("the build's save side effects are in changed_not_owned", {"MESH:Crate", "WORLD:Old"}
      <= set(manifest["advisory"]["changed_not_owned"]), manifest["advisory"])
check("...and none of them in in_copy_only (not a worker's child)", manifest["advisory"]["in_copy_only"] == [],
      manifest["advisory"])
bpy.ops.wm.open_mainfile(filepath=str(live_path))
ids, tokens = ownership.spawn_state()
spawn = merge.Spawn(spawn.wave, spawn.path, ids, tokens, {"camera": None, "world": ownership.key_of(bpy.context.scene.world)})
brief = {"id": "build", "role": "", "prefix": "", "aspects": ["camera", "lighting"], "keep_collections": True}
report = merge.merge_wave(spawn, [{"brief": brief, "manifest": manifest}], lambda: "", keep_spawn=True)
check("no extra dropped edits from the build", report.dropped_edits == [], report.dropped_edits)
check("the deleted owned Crate is removed live", bpy.data.objects.get("Crate") is None, report.replaced)
if failures:
    raise SystemExit(f"{len(failures)} failed")
print("BUILD OK", flush=True)
"""


@unittest.skipUnless(os.environ.get("LOOPCUT_BLENDER_TESTS") == "1" and BLENDER.is_file(),
                     "set LOOPCUT_BLENDER_TESTS=1 and provide a Blender binary")
class WeightTokenBlenderTest(unittest.TestCase):
    """ownership.token and the merge against real Blender: weight painting alone is a change
    (autorig_dog 20261023-180603). One headless Blender, ~5 s:

    LOOPCUT_BLENDER_TESTS=1 python3 -m unittest discover -s loopcut/tests -p test_ownership_rules.py
    """

    def run_script(self, body: str):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "case.py"
            script.write_text(f"ADDONS = {str(REPO / 'scripts/addons_core')!r}\nROOT = {tmp!r}\n" + body, encoding="utf-8")
            result = subprocess.run([str(BLENDER), "-b", "--factory-startup", "--python-exit-code", "1",
                                     "--python", str(script)], cwd=tmp, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, timeout=120,
                                    env={**os.environ, "LOOPCUT_DATA_DIR": str(Path(tmp) / "data")})
        lines = [line for line in result.stdout.splitlines()
                 if line.startswith(("PASS", "FAIL", "NOTE", "WEIGHTS", "BUILD", "Traceback"))]
        self.assertEqual(result.returncode, 0, result.stdout[-3000:])
        return lines

    def test_weight_edits_count_and_an_owned_weight_only_edit_merges(self):
        lines = self.run_script(_WEIGHTS)
        self.assertIn("WEIGHTS OK", lines, "\n".join(lines))
        print(*(line for line in lines if line.startswith("NOTE")), sep="\n")  # Whether the final look rendered.

    def test_a_workspace_builds_side_effects_are_no_dropped_edits(self):
        lines = self.run_script(_BUILD)
        self.assertIn("BUILD OK", lines, "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
