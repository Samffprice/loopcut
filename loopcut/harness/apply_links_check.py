"""apply_build keeps every driver link that runs through shape keys, headless, against a real Blender
and the real background Blenders apply_build starts (workspace.py), the way the workspace agent uses it:
snapshot_scene, run_blender on source.blend, apply_build with owns. No model, no network.

    Blender -b --factory-startup --python-exit-code 1 --python loopcut/harness/apply_links_check.py [-- <dir>]

Why: a Mesh's shape keys are a datablock of their own (a Key) that dies with the mesh. apply_build
replaces meshes three ways (merge.py: the weak-reference copy of everything appended, an owned mesh
replacing the live one, a copy of an unowned one going back to the live one), and remapped only the
Mesh, so every driver aimed at the dropped mesh's Key was left aimed at nothing. In the G1 Rain rigging
study both the Loopcut loop and Codex's loop lost the gums' and tongue's mouth_open links this way.

The project, a small Rain-like face: an armature Rig whose Jaw bone drives Face's mouth_open shape key
through key_blocks["mouth_open"].value; Gums and Tongue whose mouth_open follow Face's (mesh A's keys
driven by mesh B's Key), Face's smile following Gums' mouth_open (the reverse); an empty Indicator with
one driver on Face's Key (id_type KEY) and one through Face's object (data.shape_keys.key_blocks[...]),
and a Panel armature whose Meter bone follows Face's Key (a bone driven through a Key).

Apply 1, the rigging pass: the build parents Face and Gums to the Rig with armature modifiers (so their
objects and their meshes with shape keys are replaced) and adds a pucker key on Gums driven by the Jaw
bone; owns Face and Gums. Tongue, Indicator and Panel are live only, never in the build.
Apply 2, rebuilt from the same snapshot: the build rigs Tongue, owns Tongue and Gums (Gums left as it
was: the live one stays) and carries Face only as a copy, so the build's Tongue driver comes in aimed
at the copy of Face's Key and must end on the live one.

Checked after each apply and again after a save and reopen: every driver target in the file is set,
points at a datablock in use (a Key whose mesh an object in the scene wears), and the named ones at
exactly the live Face/Gums Keys and objects; every driver is valid; moving the Jaw bone moves every
driven value down the chain (two different values); no orphan Key is left behind; the replaced meshes
keep their shape keys and names.

Then, from a new snapshot of the reopened file (the jaw moved live first, so the driven key values
differ between the live scene and the build):
Apply 3, shape keys only: the build changes only the type of Face's mouth_open driver and adds a curl
key with a jaw driver on Tongue; owns Face, Tongue and Gums (Gums untouched by both sides). Face and
Tongue are replaced (a change), Gums is not (no change), nothing is a conflict or a dropped edit.
Apply 4, a conflict: the user changes Gums' pucker slider range live, the build moves a vertex of Gums'
mouth_open key; owns Gums and Tongue (Tongue as the snapshot had it). Gums is a conflict and stays as
the user has it; Tongue stays live with its curl key.
Apply 5, a correct re-target: the build gives Face a new mesh (FaceNew) with the same key blocks and
deletes the old one; owns Face. The drivers aimed at the old Face Key now aim at FaceNew's, and the
result says so.
Apply 6, losses: the build deletes Tongue and its mesh and clears Gums' shape keys; owns Tongue,
MESH:Tongue and Gums. The driver on the Indicator aimed at Tongue's Key and Face's smile driver aimed
at Gums' Key are unset, and the result names each: owner, path, old target, unset.
Apply 7, a key block gone: the build removes Face's smile key; the Indicator's scale driver reading
key_blocks["smile"] is reported, as it no longer resolves.
"""

import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
import checkout  # noqa: E402

ARGS = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
ROOT = Path(ARGS[0] if ARGS else tempfile.mkdtemp(prefix="loopcut-apply-links-")).resolve()
RESULTS = []
KEY_PATH = 'key_blocks["mouth_open"].value'

BUILD_RIG = '''import sys
import bpy
out = sys.argv[sys.argv.index("--") + 1]
rig = bpy.data.objects["Rig"]
for name in ("Face", "Gums"):
    ob = bpy.data.objects[name]
    ob.parent = rig
    ob.modifiers.new("Rig", "ARMATURE").object = rig
gums = bpy.data.objects["Gums"]
pucker = gums.shape_key_add(name="pucker", from_mix=False)
pucker.data[0].co.z += 0.1
driver = pucker.driver_add("value").driver
driver.type = "AVERAGE"
var = driver.variables.new()
var.type = "TRANSFORMS"
var.targets[0].id = rig
var.targets[0].bone_target = "Jaw"
var.targets[0].transform_type = "LOC_Y"
var.targets[0].transform_space = "LOCAL_SPACE"
bpy.ops.wm.save_as_mainfile(filepath=out, copy=True)
'''

BUILD_TONGUE = '''import sys
import bpy
out = sys.argv[sys.argv.index("--") + 1]
tongue = bpy.data.objects["Tongue"]
tongue.parent = bpy.data.objects["Rig"]
tongue.modifiers.new("Rig", "ARMATURE").object = bpy.data.objects["Rig"]
bpy.ops.wm.save_as_mainfile(filepath=out, copy=True)
'''

BUILD_KEYS = '''import sys
import bpy
out = sys.argv[sys.argv.index("--") + 1]
for fcurve in bpy.data.objects["Face"].data.shape_keys.animation_data.drivers:
    if fcurve.data_path == 'key_blocks["mouth_open"].value':
        fcurve.driver.type = "SUM"  # A driver-only change: one variable, the same value.
curl = bpy.data.objects["Tongue"].shape_key_add(name="curl", from_mix=False)  # A shape-key-only change.
curl.data[2].co.z += 0.1
driver = curl.driver_add("value").driver
driver.type = "AVERAGE"
var = driver.variables.new()
var.type = "TRANSFORMS"
var.targets[0].id = bpy.data.objects["Rig"]
var.targets[0].bone_target = "Jaw"
var.targets[0].transform_type = "LOC_Y"
var.targets[0].transform_space = "LOCAL_SPACE"
bpy.ops.wm.save_as_mainfile(filepath=out, copy=True)
'''

BUILD_CONFLICT = '''import sys
import bpy
out = sys.argv[sys.argv.index("--") + 1]
bpy.data.objects["Gums"].data.shape_keys.key_blocks["mouth_open"].data[1].co.z += 0.3
bpy.ops.wm.save_as_mainfile(filepath=out, copy=True)
'''

BUILD_NEW_FACE = '''import sys
import bpy
out = sys.argv[sys.argv.index("--") + 1]
face = bpy.data.objects["Face"]
old = face.data
new = bpy.data.meshes.new("FaceNew")
new.from_pydata([v.co[:] for v in old.vertices], [], [tuple(p.vertices) for p in old.polygons])
face.data = new
for name in ("Basis", "mouth_open", "smile"):
    face.shape_key_add(name=name, from_mix=False)
keys = new.shape_keys
keys.key_blocks["mouth_open"].data[0].co.z += 0.2
keys.key_blocks["smile"].data[1].co.z += 0.2

def drive(block, id_type, target, path):
    driver = keys.key_blocks[block].driver_add("value").driver
    driver.type = "AVERAGE"
    var = driver.variables.new()
    var.targets[0].id_type = id_type
    var.targets[0].id = target
    var.targets[0].data_path = path

drive("mouth_open", "OBJECT", bpy.data.objects["Rig"], 'pose.bones["Jaw"].location[1]')
drive("smile", "KEY", bpy.data.objects["Gums"].data.shape_keys, 'key_blocks["mouth_open"].value')
bpy.data.meshes.remove(old)
bpy.ops.wm.save_as_mainfile(filepath=out, copy=True)
'''

BUILD_LOSSES = '''import sys
import bpy
out = sys.argv[sys.argv.index("--") + 1]
tongue = bpy.data.objects["Tongue"]
mesh = tongue.data
bpy.data.objects.remove(tongue)
bpy.data.meshes.remove(mesh)
bpy.data.objects["Gums"].shape_key_clear()
bpy.ops.wm.save_as_mainfile(filepath=out, copy=True)
'''

BUILD_NO_SMILE = '''import sys
import bpy
out = sys.argv[sys.argv.index("--") + 1]
face = bpy.data.objects["Face"]
smile = face.data.shape_keys.key_blocks["smile"]
smile.driver_remove("value")  # Its driver first: removing a driven key block crashed Blender 5.2's next update.
face.shape_key_remove(smile)
bpy.ops.wm.save_as_mainfile(filepath=out, copy=True)
'''


def finish(code: int, message: str):
    print(message, file=sys.stderr if code else sys.stdout)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


def verdict(name: str, ok: bool, detail="") -> None:
    detail = detail if isinstance(detail, str) else repr(detail)
    RESULTS.append((name, bool(ok), detail))
    print(f"{'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail else ''}", flush=True)


def verify(name: str, fn) -> None:
    try:
        ok, detail = fn()
    except Exception as ex:  # noqa: BLE001
        traceback.print_exc()
        ok, detail = False, f"{type(ex).__name__}: {ex}"
    verdict(name, ok, detail)


def environment() -> None:
    data, tmp = ROOT / "data", ROOT / "tmp"
    for folder in (data, tmp):
        folder.mkdir(parents=True, exist_ok=True)
    env_file = ROOT / "apply_links_check.env"
    env_file.write_text("", encoding="utf-8")
    for key in [k for k in os.environ if k.startswith("LOOPCUT_")]:
        del os.environ[key]
    os.environ.update({"LOOPCUT_ENV_FILE": str(env_file), "LOOPCUT_DATA_DIR": str(data), "TMPDIR": str(tmp),
                       "LOOPCUT_WORKSPACE": "true", "LOOPCUT_ORCHESTRATE": "false"})
    tempfile.tempdir = str(tmp)


# ------------------------------------------------------------------ the project

def _mesh(name: str, keys=("mouth_open",)):
    me = bpy.data.meshes.new(name)
    me.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0), (1, 1, 0)], [], [(0, 1, 3, 2)])
    ob = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(ob)
    ob.shape_key_add(name="Basis", from_mix=False)
    for index, key in enumerate(keys):
        block = ob.shape_key_add(name=key, from_mix=False)
        block.data[index % 4].co.z += 0.2
    me.shape_keys.name = f"{name}Keys"
    return ob


def _armature(name: str, bones):
    rig = bpy.data.objects.new(name, bpy.data.armatures.new(name))
    bpy.context.scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode="EDIT")
    for bone, x in bones:
        edit = rig.data.edit_bones.new(bone)
        edit.head, edit.tail = (x, 0, 0), (x, 0, 1)
    bpy.ops.object.mode_set(mode="OBJECT")
    return rig


def _driver(fcurve, id_type: str, target, path: str):
    driver = fcurve.driver
    driver.type = "AVERAGE"
    var = driver.variables.new()
    var.type = "SINGLE_PROP"
    var.targets[0].id_type = id_type
    var.targets[0].id = target
    var.targets[0].data_path = path
    return driver


def make_project() -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    rig = _armature("Rig", (("Jaw", 0.0),))
    panel = _armature("Panel", (("Meter", 2.0),))
    face = _mesh("Face", ("mouth_open", "smile"))
    gums, tongue = _mesh("Gums"), _mesh("Tongue")
    indicator = bpy.data.objects.new("Indicator", None)
    bpy.context.scene.collection.objects.link(indicator)
    face_keys, gums_keys = face.data.shape_keys, gums.data.shape_keys
    # The jaw bone drives the face's mouth through key_blocks["mouth_open"].value (Rain's mouth control).
    _driver(face_keys.key_blocks["mouth_open"].driver_add("value"), "OBJECT", rig, 'pose.bones["Jaw"].location[1]')
    _driver(gums_keys.key_blocks["mouth_open"].driver_add("value"), "KEY", face_keys, KEY_PATH)
    _driver(tongue.data.shape_keys.key_blocks["mouth_open"].driver_add("value"), "KEY", face_keys, KEY_PATH)
    _driver(face_keys.key_blocks["smile"].driver_add("value"), "KEY", gums_keys, KEY_PATH)
    _driver(indicator.driver_add("location", 2), "KEY", face_keys, KEY_PATH)
    _driver(indicator.driver_add("location", 0), "OBJECT", face, "data.shape_keys." + KEY_PATH)
    _driver(indicator.driver_add("location", 1), "KEY", tongue.data.shape_keys, KEY_PATH)
    _driver(indicator.driver_add("scale", 0), "KEY", face_keys, 'key_blocks["smile"].value')
    _driver(panel.pose.bones["Meter"].driver_add("location", 0), "KEY", face_keys, KEY_PATH)
    bpy.context.view_layer.update()


# ------------------------------------------------------------------ the checks

def _owners():
    for attr in ("objects", "shape_keys", "meshes", "armatures", "materials", "node_groups", "worlds",
                 "scenes", "cameras", "lights", "curves", "lattices"):
        for idb in getattr(bpy.data, attr):
            anim = getattr(idb, "animation_data", None)
            if anim is not None:
                yield idb, anim


def _targets():
    """(owner, fcurve, variable, target) for every target a driver variable uses."""
    for owner, anim in _owners():
        for fcurve in anim.drivers:
            for var in fcurve.driver.variables:
                used = 2 if var.type in ("LOC_DIFF", "ROTATION_DIFF") else 1
                for target in list(var.targets)[:used]:
                    yield owner, fcurve, var, target


def _in_scene(idb) -> bool:
    scene_objects = set(bpy.context.scene.objects)
    if idb.id_type == "OBJECT":
        return idb in scene_objects
    if idb.id_type == "KEY":
        user = idb.user
        return user is not None and getattr(user, "shape_keys", None) == idb and any(
            ob.data == user for ob in scene_objects)
    return any(ob.data == idb for ob in scene_objects)


def _where(owner, fcurve, var) -> str:
    return f"{owner.id_type}:{owner.name} {fcurve.data_path}[{fcurve.array_index}] {var.name}"


def check_targets(tag: str, expected: int = 10, key_names=None) -> None:
    def targets():
        bad, count = [], 0
        for owner, fcurve, var, target in _targets():
            count += 1
            idb = target.id
            if idb is None:
                bad.append(f"{_where(owner, fcurve, var)} -> None")
            elif not _in_scene(idb):
                bad.append(f"{_where(owner, fcurve, var)} -> {idb.id_type}:{idb.name} (not in use)")
        return not bad and count == expected, f"{count} targets (want {expected})" + (f"; {bad}" if bad else "")
    verify(f"{tag}: every driver target is set and in use", targets)

    def named():
        face, gums, rig = (bpy.data.objects[n] for n in ("Face", "Gums", "Rig"))
        face_keys, gums_keys = face.data.shape_keys, gums.data.shape_keys
        tongue_keys = bpy.data.objects["Tongue"].data.shape_keys
        expected = {
            ("KEY", face_keys.name, 'key_blocks["mouth_open"].value', 0): rig,
            ("KEY", face_keys.name, 'key_blocks["smile"].value', 0): gums_keys,
            ("KEY", gums_keys.name, 'key_blocks["mouth_open"].value', 0): face_keys,
            ("KEY", gums_keys.name, 'key_blocks["pucker"].value', 0): rig,
            ("KEY", tongue_keys.name, 'key_blocks["mouth_open"].value', 0): face_keys,
            ("KEY", tongue_keys.name, 'key_blocks["curl"].value', 0): rig,
            ("OBJECT", "Indicator", "location", 0): face,
            ("OBJECT", "Indicator", "location", 1): tongue_keys,
            ("OBJECT", "Indicator", "location", 2): face_keys,
            ("OBJECT", "Indicator", "scale", 0): face_keys,
            ("OBJECT", "Panel", 'pose.bones["Meter"].location', 0): face_keys,
        }
        wrong = []
        for owner, fcurve, var, target in _targets():
            key = (owner.id_type, owner.name, fcurve.data_path, fcurve.array_index)
            if key not in expected:
                wrong.append(f"{_where(owner, fcurve, var)}: not expected")
                continue
            want = expected[key]
            if target.id != want:
                wrong.append(f"{_where(owner, fcurve, var)} -> {getattr(target.id, 'name', None)}, want {want.name}")
        return not wrong, "; ".join(wrong) or "Face/Gums Keys and the Rig as expected"
    verify(f"{tag}: the links point at the live Face and Gums Keys", named)

    def keys():
        orphans = [k.name for k in bpy.data.shape_keys if k.user is None or getattr(k.user, "shape_keys", None) != k]
        missing = [n for n in ("Face", "Gums", "Tongue") if bpy.data.objects[n].data.shape_keys is None]
        names = {n: bpy.data.objects[n].data.shape_keys.name for n in ("Face", "Gums", "Tongue") if n not in missing}
        ok = not orphans and not missing and len(bpy.data.shape_keys) == 3 and names == (key_names or {
            "Face": "FaceKeys", "Gums": "GumsKeys", "Tongue": "TongueKeys"})
        return ok, f"keys {sorted(k.name for k in bpy.data.shape_keys)}; orphans {orphans}; missing {missing}; {names}"
    verify(f"{tag}: one Key per mesh, owned, names kept", keys)

    def behaviour():
        rig = bpy.data.objects["Rig"]
        readings = []
        for value in (0.6, 0.25):
            rig.pose.bones["Jaw"].location[1] = value
            bpy.context.view_layer.update()
            depsgraph = bpy.context.evaluated_depsgraph_get()

            def block(name, key):
                return bpy.data.objects[name].data.shape_keys.evaluated_get(depsgraph).key_blocks[key].value
            indicator = bpy.data.objects["Indicator"].evaluated_get(depsgraph)
            meter = bpy.data.objects["Panel"].evaluated_get(depsgraph).pose.bones["Meter"].location[0]
            got = {"face": block("Face", "mouth_open"), "gums": block("Gums", "mouth_open"),
                   "tongue": block("Tongue", "mouth_open"), "smile": block("Face", "smile"),
                   "indicator_z": indicator.location.z, "indicator_x": indicator.location.x, "meter": meter,
                   "indicator_y": indicator.location.y, "indicator_scale": indicator.scale.x}
            for mesh, key in (("Gums", "pucker"), ("Tongue", "curl")):
                if key in bpy.data.objects[mesh].data.shape_keys.key_blocks:
                    got[key] = block(mesh, key)
            off = {k: round(v, 4) for k, v in got.items() if abs(v - value) > 1e-4}
            readings.append((value, off))
        invalid = [_where(o, f, f.driver.variables[0]) for o, a in _owners() for f in a.drivers if not f.driver.is_valid]
        rig.pose.bones["Jaw"].location[1] = 0.0
        bpy.context.view_layer.update()
        ok = not invalid and all(not off for _value, off in readings)
        return ok, f"invalid {invalid}; off {[r for r in readings if r[1]]}" if not ok else "every driven value follows the Jaw"
    verify(f"{tag}: every driver is valid and follows the Jaw bone", behaviour)


# ------------------------------------------------------------------ the run

def run_tool(result):
    """A tool's ToolResult, its then run the way the agent thread does, on_main called in place."""
    if result.then is None:
        return result
    return result.then(lambda: False, lambda fn: fn())


def run() -> None:
    from loopcut import state, workspace
    session = state.session()
    session["mode"] = "workspace"
    # A real conversation has the user's message, so it is saved with the file and resumed when the
    # file is reopened (lifecycle.resume_for_open_file). Without one, a Loopcut build, where
    # read_factory_settings re-enables the bundled add-on and so its load handler, starts a fresh
    # conversation and workspace on the reopen below (C27; stock Blender registers no handler).
    session["items"].append(state.item_user("Rig the face (apply_links_check)"))
    check_targets("live, before any apply", 9)
    snapshot = run_tool(workspace.snapshot_scene())
    verdict("snapshot", "snapshot 1" in snapshot.text, snapshot.text[:120])
    ws = workspace.current()
    (ws / "build_rig.py").write_text(BUILD_RIG, encoding="utf-8")
    (ws / "build_tongue.py").write_text(BUILD_TONGUE, encoding="utf-8")

    built = run_tool(workspace.run_blender("build_rig.py", "source.blend", [str(ws / "rigged.blend")]))
    verdict("build 1 ran", built.ok and (ws / "rigged.blend").is_file(), built.text[-300:])
    face_mesh = bpy.data.objects["Face"].data.session_uid
    applied = run_tool(workspace.apply_build("rigged.blend", ["Face", "Gums"]))
    print(applied.text, flush=True)
    verdict("apply 1 ok", applied.ok, applied.text[:400])
    verdict("apply 1 replaced Face's mesh", bpy.data.objects["Face"].data.session_uid != face_mesh
            and "pucker" in bpy.data.objects["Gums"].data.shape_keys.key_blocks)
    check_targets("apply 1")

    built = run_tool(workspace.run_blender("build_tongue.py", "source.blend", [str(ws / "tongue.blend")]))
    verdict("build 2 ran", built.ok and (ws / "tongue.blend").is_file(), built.text[-300:])
    gums_mesh = bpy.data.objects["Gums"].data.session_uid
    tongue_mesh = bpy.data.objects["Tongue"].data.session_uid
    applied = run_tool(workspace.apply_build("tongue.blend", ["Tongue", "Gums"]))
    print(applied.text, flush=True)
    verdict("apply 2 ok", applied.ok, applied.text[:400])
    verdict("apply 2 replaced Tongue, kept the live Gums", bpy.data.objects["Tongue"].data.session_uid != tongue_mesh
            and bpy.data.objects["Gums"].data.session_uid == gums_mesh
            and bpy.data.objects["Tongue"].parent == bpy.data.objects["Rig"])
    check_targets("apply 2")

    saved = ROOT / "applied.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(saved))
    bpy.ops.wm.open_mainfile(filepath=str(saved))
    verdict("reopened: the same conversation and workspace", state.session()["id"] == session["id"]
            and workspace.current() == ws, f"{state.session()['id']} (was {session['id']})")
    check_targets("saved and reopened")
    later_applies(ws)


def build_and_apply(ws: Path, number: int, script: str, text: str, owns: list):
    from loopcut import workspace
    (ws / script).write_text(text, encoding="utf-8")
    out = Path(script).stem + ".blend"
    built = run_tool(workspace.run_blender(script, "source.blend", [str(ws / out)]))
    verdict(f"build {number} ran", built.ok and (ws / out).is_file(), built.text[-300:])
    applied = run_tool(workspace.apply_build(out, owns))
    print(applied.text, flush=True)
    return applied


def snapshot(number: int) -> None:
    from loopcut import workspace
    result = run_tool(workspace.snapshot_scene())
    verdict(f"snapshot {number}", f"snapshot {number}" in result.text, result.text[:80])


def line_with(text: str, *needles) -> str:
    return next((line for line in text.splitlines() if all(n in line for n in needles)), "")


def later_applies(ws: Path) -> None:
    """Applies 3 to 7 (the module docstring), each from a snapshot of what is live."""
    snapshot(2)
    rig = bpy.data.objects["Rig"]
    face_keys = bpy.data.objects["Face"].data.shape_keys
    before = face_keys.key_blocks["mouth_open"].value
    rig.pose.bones["Jaw"].location[1] = 0.4  # Driven values move live; the build has the snapshot's.
    bpy.context.view_layer.update()
    print(f"live mouth_open {before} -> {face_keys.key_blocks['mouth_open'].value} with the jaw moved", flush=True)
    meshes = {n: bpy.data.objects[n].data.session_uid for n in ("Face", "Gums", "Tongue")}

    applied = build_and_apply(ws, 3, "build_keys.py", BUILD_KEYS, ["Face", "Tongue", "Gums"])
    now = {n: bpy.data.objects[n].data.session_uid for n in ("Face", "Gums", "Tongue")}
    face_driver = next(f for f in bpy.data.objects["Face"].data.shape_keys.animation_data.drivers
                       if f.data_path == KEY_PATH)
    verdict("apply 3: a shape-key-only and a driver-only change are applied",
            now["Face"] != meshes["Face"] and now["Tongue"] != meshes["Tongue"] and face_driver.driver.type == "SUM"
            and "curl" in bpy.data.objects["Tongue"].data.shape_keys.key_blocks, applied.text[:300])
    verdict("apply 3: Gums, untouched with its keys and drivers, is not a change (the jaw moved live)",
            now["Gums"] == meshes["Gums"] and "MESH:Gums" not in line_with(applied.text, "Replaced:"), applied.text[:300])
    verdict("apply 3: no conflict, no dropped edit", applied.ok and "Kept live" not in applied.text
            and "Not brought" not in applied.text, applied.text[:600])
    rig.pose.bones["Jaw"].location[1] = 0.0
    bpy.context.view_layer.update()
    check_targets("apply 3", 11)

    gums = bpy.data.objects["Gums"]
    gums.data.shape_keys.key_blocks["pucker"].slider_max = 3.0  # The user's edit, while the build runs.
    meshes = {n: bpy.data.objects[n].data.session_uid for n in ("Gums", "Tongue")}
    applied = build_and_apply(ws, 4, "build_conflict.py", BUILD_CONFLICT, ["Gums", "Tongue"])
    gums = bpy.data.objects["Gums"]
    verdict("apply 4: Gums changed live and in the build is a conflict; the user's version stays",
            "MESH:Gums" in line_with(applied.text, "Kept live") and gums.data.session_uid == meshes["Gums"]
            and gums.data.shape_keys.key_blocks["pucker"].slider_max == 3.0
            and abs(gums.data.shape_keys.key_blocks["mouth_open"].data[1].co.z) < 1e-6, applied.text[:600])
    verdict("apply 4: Tongue, owned but as the snapshot had it, stays live with its curl key",
            bpy.data.objects["Tongue"].data.session_uid == meshes["Tongue"]
            and "Tongue" not in line_with(applied.text, "Kept live")
            and "curl" in bpy.data.objects["Tongue"].data.shape_keys.key_blocks, applied.text[:600])
    check_targets("apply 4", 11)

    snapshot(3)
    old_face = bpy.data.objects["Face"].data.name
    applied = build_and_apply(ws, 5, "build_new_face.py", BUILD_NEW_FACE, ["Face"])
    face = bpy.data.objects["Face"]
    verdict("apply 5: Face wears FaceNew, the old mesh is gone", face.data.name == "FaceNew"
            and old_face not in bpy.data.meshes, f"{face.data.name}; meshes {sorted(m.name for m in bpy.data.meshes)}")
    wanted = [("shape keys of mesh Gums", 'key_blocks["mouth_open"].value'),
              ("shape keys of mesh Tongue", 'key_blocks["mouth_open"].value'),
              ("object Indicator", "location[2]"), ("object Indicator", "scale[0]"),
              ("object Panel", 'pose.bones["Meter"].location[0]')]
    missing = [w for w in wanted if not line_with(applied.text, *w, "mesh FaceNew")]
    verdict("apply 5: the drivers aimed at the old Face Key are re-aimed at FaceNew's, and said so",
            not missing and applied.ok, f"missing {missing}; {applied.text[:900]}")
    check_targets("apply 5", 11, {"Face": face.data.shape_keys.name, "Gums": "GumsKeys", "Tongue": "TongueKeys"})

    snapshot(4)
    applied = build_and_apply(ws, 6, "build_losses.py", BUILD_LOSSES, ["Tongue", "MESH:Tongue", "Gums"])
    indicator = bpy.data.objects["Indicator"]
    lost_y = next(f for f in indicator.animation_data.drivers if f.data_path == "location" and f.array_index == 1)
    smile = next(f for f in bpy.data.objects["Face"].data.shape_keys.animation_data.drivers
                 if f.data_path == 'key_blocks["smile"].value')
    verdict("apply 6: Tongue and its mesh removed, Gums has no shape keys",
            "Tongue" not in bpy.data.objects and "Tongue" not in bpy.data.meshes
            and bpy.data.objects["Gums"].data.shape_keys is None, applied.text[:400])
    verdict("apply 6: the two drivers that lost their target are unset",
            lost_y.driver.variables[0].targets[0].id is None and smile.driver.variables[0].targets[0].id is None)
    lines = [line_with(applied.text, "object Indicator", "location[1]", "shape keys of mesh Tongue", "unset"),
             line_with(applied.text, 'key_blocks["smile"].value', "shape keys of mesh Gums", "unset")]
    verdict("apply 6: the result names each lost target: owner, path, old target, unset", all(lines),
            f"{lines}; {applied.text[:900]}")

    snapshot(5)
    applied = build_and_apply(ws, 7, "build_no_smile.py", BUILD_NO_SMILE, ["Face"])
    verdict("apply 7: the driver reading the removed smile key block is reported",
            bool(line_with(applied.text, "object Indicator", "scale[0]", '"smile"', "mesh FaceNew no longer")),
            applied.text[:900])


def main() -> None:
    checkout.use()
    try:
        environment()
        make_project()
        print(f"apply links check in {ROOT}", flush=True)
        run()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        verdict("the check ran to the end", False, "see the traceback above")
    failed = [name for name, ok, _ in RESULTS if not ok]
    if failed:
        finish(1, f"APPLY LINKS CHECK FAILED: {len(failed)} of {len(RESULTS)}; files kept in {ROOT}")
    if not ARGS:
        shutil.rmtree(ROOT, ignore_errors=True)
    finish(0, f"APPLY LINKS CHECK OK: {len(RESULTS)} checks")


main()
