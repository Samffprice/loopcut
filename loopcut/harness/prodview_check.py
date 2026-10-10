"""The production shot and explicit evaluation context, headless, with a real worker child.

    Blender -b --factory-startup --python-exit-code 1 --python loopcut/harness/prodview_check.py [-- --keep]

The scene is the Arcane chest run's staging (20260930-154037): the pack's props, light and camera in
collection Pack; the chest, its floor, a hero camera and a warm key and cool rim in Stage, made after
the active layer (ViewLayer, not rendered) excluded Stage, so their matrices were never evaluated and
read as the identity; a layer ChestStage that excludes Pack and renders, with a dim blue world
override; BrightStage, the same without the override; and Ghost, in a collection every layer
excludes. Cases:

- parent reads: frame_camera report_only of HeroCam reads it where it is (not the origin) and says
  the layer; get_scene_info's rows have the chest's real bounds and evaluated_in, Ghost's are
  unavailable, the header lists the view layers; checks (rests_on, preserved, dims) measure in the
  layer that includes their objects, a preserved snapshot taken in ViewLayer still passes with
  ChestStage active, and Ghost's dims is unavailable; framing HeroCam from front moves it there and
  leaves the active layer as it was;
- orchestrator: inspect_scene final_lighting through HeroCam with view_layer ChestStage reports the
  layer, its world and lights, and labels the chest where it is; without view_layer it renders the
  same (the layer that renders); a name the scene lacks is refused;
- worker child (materials): capture_viewport camera HeroCam with view_layer ChestStage renders the
  production shot (1024 px) and says the camera, the layer with its exclusion and override, the world
  and the lights by name (Key, Rim; not PackLight); without view_layer the rendered layer is the
  same; ChestStage is darker than BrightStage (the override is honoured); studio shading clears the
  override; frame_camera and a contact check in the child read in the right layer; a bad layer name
  is refused with the names there are; no look leaves a trace (layers' use, overrides, camera, window
  layer). Prints the look's seconds as MEASURE lines and PRODVIEW OK or PRODVIEW FAIL.
"""

import json
import os
import re
import sys
import tempfile
import time
import traceback
from pathlib import Path

import bmesh
import bpy

sys.path.insert(0, str(Path(__file__).parent))
import checkout
checkout.use()
from loopcut import acceptance, tools, worker_host  # noqa: E402

KEEP = "--keep" in sys.argv
work = Path(tempfile.mkdtemp(prefix="loopcut-prodview-check-"))
os.environ["LOOPCUT_DATA_DIR"] = str(work / "data")   # inspect_scene's jobs.
failures, measured = [], []


def note(line: str) -> None:
    print(line, flush=True)


def check(condition, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _box(name, size, location, collection, material=None):
    mesh = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    for v in bm.verts:
        v.co.x, v.co.y, v.co.z = v.co.x * size[0], v.co.y * size[1], v.co.z * size[2]
    bm.to_mesh(mesh)
    bm.free()
    obj = bpy.data.objects.new(name, mesh)
    obj.location = location
    collection.objects.link(obj)
    if material is not None:
        mesh.materials.append(material)
    return obj


def _material(name, rgb):
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    bsdf = next(n for n in material.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    bsdf.inputs["Base Color"].default_value = (*rgb, 1.0)
    return material


def _world(name, rgb, strength):
    world = bpy.data.worlds.new(name)
    world.use_nodes = True
    background = next(n for n in world.node_tree.nodes if n.type == "BACKGROUND")
    background.inputs["Color"].default_value = (*rgb, 1.0)
    background.inputs["Strength"].default_value = strength
    return world


def _light(name, kind, energy, location, collection):
    light = bpy.data.objects.new(name, bpy.data.lights.new(name, kind))
    light.data.energy, light.location = energy, location
    collection.objects.link(light)
    return light


def staged_scene(job: Path) -> Path:
    bpy.ops.wm.read_factory_settings(use_empty=False)   # Keeps the window undo needs in the child.
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj)
    scene = bpy.context.scene
    scene.world = _world("World", (0.8, 0.8, 0.8), 1.0)
    pack, stage, hidden = (bpy.data.collections.new(n) for n in ("Pack", "Stage", "Hidden"))
    for collection in (pack, stage, hidden):
        scene.collection.children.link(collection)
    viewport = scene.view_layers[0]
    _box("Crate", (0.6, 0.6, 0.6), (3.0, 1.0, 0.3), pack, _material("Crate_Wood", (0.4, 0.25, 0.1)))
    _light("PackLight", "POINT", 800.0, (3.0, -2.0, 3.0), pack)
    pack_camera = bpy.data.objects.new("PackCamera", bpy.data.cameras.new("PackCamera"))
    pack_camera.location, pack_camera.rotation_euler = (8.0, -8.0, 5.0), (1.1, 0.0, 0.785)
    pack.objects.link(pack_camera)
    scene.camera = pack_camera
    bpy.context.view_layer.update()
    # Stage is excluded from the active layer before anything is made in it: never evaluated there.
    viewport.layer_collection.children["Stage"].exclude = True
    viewport.layer_collection.children["Hidden"].exclude = True
    _box("StageFloor", (4.0, 4.0, 0.02), (0.0, 0.0, -0.01), stage, _material("Stage_Charcoal", (0.05, 0.05, 0.05)))
    _box("Chest", (0.8, 0.5, 0.5), (0.0, 0.0, 0.25), stage, _material("Chest_Paint", (0.8, 0.35, 0.1)))
    hero = bpy.data.objects.new("HeroCam", bpy.data.cameras.new("HeroCam"))
    hero.location, hero.rotation_euler = (0.0, -3.0, 1.2), (1.27, 0.0, 0.0)
    stage.objects.link(hero)
    _light("Key", "AREA", 250.0, (-1.5, -1.5, 2.0), stage)
    _light("Rim", "AREA", 120.0, (1.0, 1.5, 1.8), stage)
    _box("Ghost", (0.3, 0.3, 0.3), (5.0, 5.0, 0.15), hidden)
    for name, override in (("ChestStage", _world("StageWorld", (0.2, 0.25, 0.35), 0.1)), ("BrightStage", None)):
        layer = scene.view_layers.new(name)
        layer.layer_collection.children["Pack"].exclude = True
        layer.layer_collection.children["Hidden"].exclude = True
        layer.world_override = override
        layer.use = name == "ChestStage"
    viewport.use = False
    scene.render.engine = "CYCLES"
    scene.cycles.samples = 64
    scene.render.resolution_x, scene.render.resolution_y = 1920, 1080
    check(tuple(hero.matrix_world.translation) == (0.0, 0.0, 0.0), "the staged camera must start unevaluated")
    wave = job / "w1"
    wave.mkdir(parents=True, exist_ok=True)
    path = wave / "spawn.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(path), copy=True, compress=True, relative_remap=True)
    return path


def location_in(text: str) -> tuple:
    """The camera's location in frame_camera's first line: ': at (x, y, z)'."""
    found = re.search(r": at [\[(](-?[\d.]+), (-?[\d.]+), (-?[\d.]+)[\])]", text)
    return tuple(float(v) for v in found.groups()) if found else ()


# ------------------------------------------------------------------ cases

def parent_reads(spawn: Path):
    bpy.ops.wm.open_mainfile(filepath=str(spawn))
    scene = bpy.context.scene
    check(bpy.context.view_layer.name == "ViewLayer", bpy.context.view_layer.name)
    framed = tools.execute("frame_camera", json.dumps({"camera": "HeroCam", "objects": ["Chest"], "report_only": True}))
    check(framed.ok, framed.text)
    check("Evaluated in view layer ChestStage" in framed.text and "excludes HeroCam" in framed.text, framed.text)
    where = location_in(framed.text)
    check(where and abs(where[1] + 3.0) < 1e-3 and abs(where[2] - 1.2) < 1e-3, f"camera read at {where}: {framed.text}")
    note(f"frame_camera: {framed.text.splitlines()[0][:160]}")

    info = json.loads(tools.execute("get_scene_info", json.dumps({"detail": "rows"})).text)
    rows = {r["name"]: r for r in info["objects"]}
    chest = rows["Chest"]
    check(chest.get("evaluated_in") == "ChestStage", chest)
    check(chest["bounds"]["min"][2] == 0.0 and chest["bounds"]["max"][0] == 0.4, chest)
    check(str(rows["Ghost"].get("bounds", "")).startswith("unavailable"), rows["Ghost"])
    layers = info["view_layers"]
    check(layers["active"] == "ViewLayer" and layers["layers"]["ChestStage"]["world_override"] == "StageWorld"
          and layers["layers"]["ChestStage"]["excludes"] == ["Pack", "Hidden"], layers)
    note(f"get_scene_info Chest: {json.dumps(chest)}; view_layers {json.dumps(layers)[:200]}")

    baseline = acceptance.snapshot(["Crate", "Chest"])
    check(baseline["Chest"]["dims"] == [0.8, 0.5, 0.5] and baseline["Crate"]["dims"] == [0.6, 0.6, 0.6], baseline)
    checks = [{"kind": "contact", "a": "Chest", "relation": "rests_on", "b": "StageFloor"},
              {"kind": "preserved", "names": ["Crate", "Chest"], "snapshot": "s1", "baseline": baseline},
              {"kind": "dims", "name": "Chest", "size": [0.8, 0.5, 0.5], "tol": 0.001},
              {"kind": "dims", "name": "Ghost", "size": [0.3, 0.3, 0.3]},
              {"kind": "in_frame", "camera": "HeroCam", "names": ["Chest"]}]
    checks = [{**c, "quote": "keep the chest staged"} for c in checks]
    window = bpy.context.window
    window.view_layer = scene.view_layers["ChestStage"]   # The staging layer active: Crate excluded now.
    try:
        results = {r["id"]: r for r in acceptance.run(checks)}
    finally:
        window.view_layer = scene.view_layers["ViewLayer"]
    for result in results.values():
        note(f"check {result['kind']}: {result['status']}: {result['detail'][:170]}")
    statuses = [r["status"] for r in results.values()]
    check(statuses == ["pass", "pass", "pass", "unavailable", "pass"], statuses)
    ghost = [r for r in results.values() if r["kind"] == "dims"][1]
    check("excluded from every view layer" in ghost["detail"], ghost)

    moved = tools.execute("frame_camera", json.dumps({"camera": "HeroCam", "objects": ["Chest"], "direction": "front"}))
    check(moved.ok and "Evaluated in view layer ChestStage" in moved.text, moved.text)
    again = tools.execute("frame_camera", json.dumps({"camera": "HeroCam", "objects": ["Chest"], "report_only": True}))
    check(location_in(again.text) == location_in(moved.text) and location_in(moved.text)[2] < 1.0,
          (again.text, moved.text))
    check(bpy.context.view_layer.name == "ViewLayer" and bpy.context.window.view_layer.name == "ViewLayer",
          "the active layer is put back")
    ghost_frame = tools.execute("frame_camera", json.dumps({"camera": "HeroCam", "objects": ["Chest", "Ghost"],
                                                            "report_only": True}))
    check("Ghost is excluded from every view layer" in ghost_frame.text, ghost_frame.text)


def orchestrator_inspection(spawn: Path):
    from loopcut import inspection, jobs
    bpy.ops.wm.open_mainfile(filepath=str(spawn))

    def inspected(**arguments):
        request = inspection.inspect_scene("final_lighting", cameras=["HeroCam"], frames=[1], focus=["Chest"],
                                           max_side=256, samples=4, **arguments)
        deadline = time.monotonic() + 120
        while jobs.status(request.pending_job)["state"] in jobs.ACTIVE:
            check(time.monotonic() < deadline, "inspection did not finish")
            time.sleep(0.1)
        result = inspection.finish(request.pending_job)
        check(result.ok, result.text)
        return json.loads(result.text.split("\n", 1)[1])

    asked = inspected(view_layer="ChestStage")
    note(f"inspect_scene: {json.dumps({k: asked[k] for k in ('view_layer', 'world', 'lights')})}")
    check(asked["view_layer"] == "view layer ChestStage (excludes Pack, Hidden; world override StageWorld)", asked)
    check(asked["world"] == "StageWorld" and asked["lights"] == ["Key", "Rim"], asked)
    box = asked["views"][0]["focus"][0]["bounds"]
    check(box and 0.25 < box[0] < 0.35 and 0.65 < box[2] < 0.75, f"the chest's label box {box}")
    default = inspected()
    check(default["view_layer"] == asked["view_layer"], default)
    try:
        inspection.inspect_scene("final_lighting", cameras=["HeroCam"], view_layer="Nope")
        raise AssertionError("an unknown view layer was accepted")
    except tools.ToolError as ex:
        check("ChestStage" in str(ex), str(ex))


TRACE = ("import bpy\ns = bpy.context.scene\n"
         "print('TRACE', [(l.name, l.use, l.world_override.name if l.world_override else None, l.samples) "
         "for l in s.view_layers], bpy.context.view_layer.name, bpy.context.window.view_layer.name, s.camera.name, "
         "s.world.name, s.render.engine, s.cycles.samples, s.render.resolution_x, s.render.film_transparent, "
         "sorted(w.name for w in bpy.data.worlds), sorted(o.name for o in bpy.data.objects))")


def child_looks(job: Path, spawn: Path):
    child = worker_host.Child(job, "W1", spawn, "materials", binary=bpy.app.binary_path, run_timeout=120).start()
    try:
        ready = child.call("init", {"warm": False, "deliverable": "Chest", "prefix": "Chest"})
        check(ready["ok"], ready)
        runtime = worker_host.WorkerRuntime(child, {})

        def run(code):
            return runtime.run_tool("run_python", json.dumps({"code": code, "summary": "check"}))

        before = run(TRACE).text
        check("TRACE" in before, before)
        started = time.monotonic()
        shot = runtime.run_tool("capture_viewport", json.dumps({"camera": "HeroCam", "view_layer": "ChestStage"}))
        seconds = time.monotonic() - started
        check(shot.ok and shot.image_path, shot.text)
        note(f"production shot: {shot.text[:700]}")
        if KEEP:
            import shutil
            shutil.copy(shot.image_path, work / "production_shot.png")
        for words in ("through HeroCam", "view layer ChestStage (excludes Pack, Hidden; world override StageWorld)",
                      "world StageWorld (the layer override)", "2 lights: Key, Rim", "Cycles 16 samples"):
            check(words in shot.text, f"{words!r} not in {shot.text}")
        check("PackLight" not in shot.text, shot.text)
        size = Path(shot.image_path).stat().st_size
        measured.append(f"production shot (Cycles 16 spp capped, 1024 px wide, CPU) {seconds:.1f} s round trip, "
                        f"{size // 1024} KB")
        dim = float(re.search(r"frame (\d\.\d+)", shot.text).group(1))

        started = time.monotonic()
        default = runtime.run_tool("capture_viewport", json.dumps({"camera": "HeroCam"}))
        measured.append(f"the same shot again, warm: {time.monotonic() - started:.1f} s round trip")
        check(default.ok and "view layer ChestStage" in default.text, default.text)
        bright = runtime.run_tool("capture_viewport", json.dumps({"camera": "HeroCam", "view_layer": "BrightStage"}))
        check(bright.ok and "(excludes Pack, Hidden), world World" in bright.text and "override" not in bright.text, bright.text)
        lit = float(re.search(r"frame (\d\.\d+)", bright.text).group(1))
        check(lit > dim * 1.5, f"the override's dim world must show: ChestStage mean {dim}, BrightStage {lit}")
        note(f"frame mean: ChestStage {dim}, BrightStage {lit}")

        studio = runtime.run_tool("capture_viewport", json.dumps({"camera": "HeroCam", "angle": "camera",
                                                                  "shading": "studio", "view_layer": "ChestStage"}))
        check(studio.ok and "a white world and a key light" in studio.text, studio.text)
        crops = runtime.run_tool("capture_viewport", json.dumps({"angle": "crops", "camera": "HeroCam",
                                                                 "view_layer": "ChestStage", "focus": ["Chest"]}))
        check(crops.ok and "through HeroCam" in crops.text and "view layer ChestStage" in crops.text, crops.text)
        wrong = runtime.run_tool("capture_viewport", json.dumps({"camera": "HeroCam", "view_layer": "Nope"}))
        check(not wrong.ok and "ChestStage" in wrong.text, wrong.text)
        wrong_camera = runtime.run_tool("capture_viewport", json.dumps({"camera": "Chest"}))
        check(not wrong_camera.ok and "HeroCam" in wrong_camera.text, wrong_camera.text)

        framed = runtime.run_tool("frame_camera", json.dumps({"camera": "HeroCam", "objects": ["Chest"],
                                                              "report_only": True}))
        check(framed.ok and "Evaluated in view layer ChestStage" in framed.text, framed.text)
        results = child.call("check", {"checks": [{"kind": "contact", "a": "Chest", "relation": "rests_on",
                                                   "b": "StageFloor", "quote": "on its floor"}]})["results"]
        check(results[0]["status"] == "pass" and "ChestStage" in results[0]["detail"], results)

        after = run(TRACE).text
        check(after.split("TRACE", 1)[1].split("\n")[0] == before.split("TRACE", 1)[1].split("\n")[0],
              f"a look left a trace:\n{before}\n{after}")
    finally:
        child.exit()


def main() -> int:
    job = work / "job1"
    spawn = staged_scene(job)
    for name, case in (("parent reads", lambda: parent_reads(spawn)),
                       ("orchestrator inspection", lambda: orchestrator_inspection(spawn)),
                       ("child looks", lambda: child_looks(job, spawn))):
        try:
            case()
            note(f"PRODVIEW {name}: ok")
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            failures.append(name)
            note(f"PRODVIEW {name}: FAIL")
        finally:
            worker_host.shutdown_all()
    for line in measured:
        print(f"MEASURE {line}")
    print("PRODVIEW FAIL: " + ", ".join(failures) if failures else "PRODVIEW OK", flush=True)
    return 1 if failures else 0


code = main()
if not KEEP:
    import shutil
    shutil.rmtree(work, ignore_errors=True)
else:
    print(f"kept {work}")
sys.stdout.flush()
sys.exit(code)
