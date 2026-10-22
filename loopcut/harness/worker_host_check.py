"""The worker host with real child Blenders, the parent headless too (07_m4_design §4 W1).

    Blender -b --factory-startup --python-exit-code 1 --python loopcut/harness/worker_host_check.py \\
        [-- --measure] [--keep]

A small living room is saved as a wave's spawn copy; children are started on it through
worker_host.Child and driven through WorkerRuntime, as the worker loop will. Cases:

- round trip: run_python makes a cube (code and output with newlines and non-ASCII), get_scene_info
  in the same child sees it, init reports undo and stamps, finish writes result.blend and a manifest;
- a look per role: a 1024-px PNG, names drawn on it only with labels: true (modeling: close views of its own
  stool; lighting and camera: through the camera, thirds for the camera role, exposure numbers for
  lighting; a sheet of frames for animation), the final look through the camera, the time each
  takes, and no look leaves a trace in the scene;
- rollback: editing the non-owned Rug, a palette material and the camera (frame_camera) is undone
  and reported, the owned lamp's edit is kept, report_only frame_camera is never rolled back;
- a child timeout kills the child and gives a crash status; restart() is back at the spawn state;
  a child that exits mid-call is a crash too;
- broken mesh: the asset_pack chest's craft pass (polygons.add without loops, which raised halfway
  and left a face with no corners) is repaired and said so, and the looks after it render instead
  of segfaulting the child (Mesh.corner_tris);
- crash kit: a child that segfaults leaves <worker>/crash-1 with Blender's crash report, the spawn
  copy, the log's tail and crash.json; after restart() the deliverable is gone and finish says so;
- Stop: cancelling kills every child's call and process in under 3 s;
- a force-killed parent (a helper process that starts the child, then is killed) leaves no child
  after 5 s, idle (EOF) or busy in model code (the watchdog);
- reap_orphans kills a stuck child whose owner is dead, and nothing else;
- the child cannot see keys: the parent's LOOPCUT_API_KEY, other *_KEY/*_TOKEN variables, or a
  .env the parent points LOOPCUT_ENV_FILE at;
- import_asset split: a kit made on the spot is registered as an asset library in this Blender
  only; the parent fetches, the child loads an object and a material, the material onto an
  object that exists only in the child;
- unlit cell: in a scene with no light of its own, the contact sheet cell is lit by the studio world;
- texture fidelity: a cream image painted with numpy and packed shows cream in the materials look
  (it showed slate under the old studio.exr world: Arcane run W0025-W0044) and exactly in albedo; every
  caption says its engine, shading and view; a multiview of angles x shadings, the camera-scale
  crops, an object in front named and isolate hiding it; no look leaves a trace;
- bloom: the scene shading and the crops run the scene's own compositor (a Glare's halo shows), the
  studio shading does not;
- reference: a brief's reference (init "reference", waves.reference_tile) is a 320-px tile at the top
  right of every look but the crops, labelled reference and said in the caption; a still that cannot
  be read costs the look nothing; the wave's contact sheet cell of it (reference_cell) is cell-sized;
- tracking (eval 20260930-154037): a text block written or edited, an image added, painted and packed
  past the pixel hash budget, an unsaved repaint, and changes made before an exception are each in
  Scene changes and count as a change; a further unsaved paint the snapshot cannot compare is named;
  an edit to a text block the worker does not own is rolled back; variables do not outlive a call;
  finish lists the text and the packed image (support), hashed as inspect_texture hashes.

Without --measure no more than three Blenders run at once (this one and two children).
--measure adds a Stop of three children and, on the shared_support and perfume_ad eval start
scenes, three children at once (modeling, materials, lighting) with their peak RSS: four Blenders.
Prints WORKER OK or WORKER FAIL, plus MEASURE lines.
"""

import json
import os
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

import bmesh
import bpy

sys.path.insert(0, str(Path(__file__).parent))
import checkout
checkout.use()
from loopcut import llm, worker_host, worker_ipc, worker_process  # noqa: E402

os.environ.setdefault("LOOPCUT_PROMPT_INDEX", str(Path(__file__).parent / "fixtures" / "prompt-index.json"))
if os.name == "nt":
    # Let the CI's software OpenGL selection reach children, within this harness only.
    worker_ipc.KEEP_ENV += ("GALLIUM_DRIVER",)

MEASURE = "--measure" in sys.argv
KEEP = "--keep" in sys.argv
BINARY = bpy.app.binary_path
PYTHON = sys.executable if Path(sys.executable).name.startswith("python") else ("python" if os.name == "nt" else "python3")
ROLES = ("modeling", "materials", "lighting", "camera", "animation")
LABEL_BOX = (15, 18, 25)
FAKE = "-".join(("check", "fake", "value"))  # Planted in the parent; the child must never see it.
work = Path(tempfile.mkdtemp(prefix="loopcut-worker-check-"))
failures, measured = [], []


def note(line: str) -> None:
    print(line, flush=True)


def measure(line: str) -> None:
    measured.append(line)
    note(f"MEASURE {line}")


# ------------------------------------------------------------------ scenes

def _box(name, size, location, material=None):
    mesh = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    for v in bm.verts:
        v.co.x, v.co.y, v.co.z = v.co.x * size[0], v.co.y * size[1], v.co.z * size[2]
    bm.to_mesh(mesh)
    bm.free()
    obj = bpy.data.objects.new(name, mesh)
    obj.location = location
    bpy.context.scene.collection.objects.link(obj)
    if material:
        mesh.materials.append(material)
    return obj


def _material(name, rgb, roughness=0.5):
    material = bpy.data.materials.new(name)
    material.diffuse_color = (*rgb, 1.0)
    if material.node_tree is None:
        material.use_nodes = True
    bsdf = next(n for n in material.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    bsdf.inputs["Base Color"].default_value = (*rgb, 1.0)
    bsdf.inputs["Roughness"].default_value = roughness
    return material


def living_room(job: Path) -> Path:
    """Floor, Rug, Table (keyframed for the animation look), a lamp and a camera; the spawn copy."""
    bpy.ops.wm.read_factory_settings(use_empty=False)  # Keeps the window undo needs in the child.
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj)
    wood, wool = _material("Palette_Wood", (0.45, 0.28, 0.15), 0.6), _material("Rug_Wool", (0.6, 0.1, 0.1), 0.9)
    _box("Floor", (6, 6, 0.02), (0, 0, -0.01), _material("Floor_Mat", (0.5, 0.5, 0.5)))
    _box("Rug", (2, 1.4, 0.015), (0, 0, 0.0075), wool)
    table = _box("Table", (0.9, 0.6, 0.72), (0.4, 0.1, 0.36), wood)
    table.keyframe_insert("location", frame=1)
    table.location.x = -0.4
    table.keyframe_insert("location", frame=24)
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end = 1, 24
    scene.frame_set(1)
    lamp = bpy.data.objects.new("Lamp", bpy.data.lights.new("Lamp", "POINT"))
    lamp.location, lamp.data.energy = (1.5, -1.5, 2.5), 400.0
    scene.collection.objects.link(lamp)
    camera = bpy.data.objects.new("Camera", bpy.data.cameras.new("Camera"))
    camera.location, camera.rotation_euler = (4.2, -4.2, 2.6), (1.2, 0.0, 0.785)
    scene.collection.objects.link(camera)
    scene.camera = camera
    scene.render.resolution_x, scene.render.resolution_y = 1920, 1080
    return save_spawn(job)


def save_spawn(job: Path) -> Path:
    wave = job / "w1"
    wave.mkdir(parents=True, exist_ok=True)
    path = wave / "spawn.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(path), copy=True, compress=True, relative_remap=True)
    return path


def eval_start_scene(task_id: str, job: Path) -> Path:
    sys.path.insert(0, str(Path(__file__).parent / "evals"))
    import tasks
    bpy.ops.wm.read_factory_settings(use_empty=False)
    tasks.BY_ID[task_id].setup()
    return save_spawn(job)


# ------------------------------------------------------------------ helpers

def start(job: Path, spawn: Path, worker: str, role: str = "modeling", warm: bool = False, run_timeout: float = 60,
          **init):
    child = worker_host.Child(job, worker, spawn, role, binary=BINARY, run_timeout=run_timeout).start()
    ready = child.call("init", {"warm": warm, **init})
    assert ready["ok"], ready
    return child, worker_host.WorkerRuntime(child, {})


def run(runtime, code: str, **arguments):
    return runtime.run_tool("run_python", json.dumps({"code": code, "summary": "check", **arguments}))


def png_size(path: Path) -> tuple:
    head = path.read_bytes()[:24]
    assert head[:8] == b"\x89PNG\r\n\x1a\n", f"not a PNG: {path}"
    return struct.unpack(">II", head[16:24])


def pixels_like(path: Path, rgb: tuple) -> int:
    """Pixels of this 8-bit colour, give or take one step."""
    import numpy as np
    image = bpy.data.images.load(str(path), check_existing=False)
    try:
        width, height = image.size
        values = np.empty(width * height * 4, dtype=np.float32)
        image.pixels.foreach_get(values)
        rgb8 = np.round(values.reshape(-1, 4)[:, :3] * 255)
        return int(np.sum(np.all(np.abs(rgb8 - np.array(rgb)) <= 1, axis=1)))
    finally:
        bpy.data.images.remove(image)


def gone(pid: int, seconds: float) -> float | None:
    """Seconds until pid is gone, or None if it is still there after `seconds`."""
    started = time.monotonic()
    while time.monotonic() - started < seconds:
        if not worker_host._alive(pid):
            return time.monotonic() - started
        time.sleep(0.05)
    return None


def trace_code() -> str:
    """What a look must leave as it found it: datablocks, engine, world, camera, frame, view."""
    return ("import bpy\ns = bpy.context.scene\n"
            "print('TRACE', sorted(f'{i.id_type}:{i.name}' for c in (bpy.data.objects, bpy.data.cameras, "
            "bpy.data.worlds, bpy.data.images, bpy.data.lights, bpy.data.materials) for i in c), s.render.engine, "
            "s.world.name if s.world else None, s.camera.name, s.frame_current, s.view_settings.view_transform, "
            "s.render.resolution_x, s.render.filepath, s.display.shading.color_type, s.render.film_transparent, "
            "[(o.hide_render, tuple(o.color), [m.name for m in o.material_slots if m.material]) for o in s.objects], "
            "[tuple(m.diffuse_color) for m in bpy.data.materials])")


def dark_room(job: Path) -> Path:
    """The factory scene with no light, camera or cube, and a floor: nothing lights it."""
    bpy.ops.wm.read_factory_settings(use_empty=False)
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj)
    _box("Floor", (6, 6, 0.02), (0, 0, -0.01), _material("Floor_Mat", (0.5, 0.5, 0.5)))
    return save_spawn(job)


def mean_value(path: Path) -> float:
    """The mean of an image's RGB, 0..1."""
    import numpy as np
    image = bpy.data.images.load(str(path), check_existing=False)
    try:
        values = np.empty(image.size[0] * image.size[1] * 4, dtype=np.float32)
        image.pixels.foreach_get(values)
        return float(values.reshape(-1, 4)[:, :3].mean())
    finally:
        bpy.data.images.remove(image)


def trace_of(result) -> str:
    return next(line for line in result.text.splitlines() if line.startswith("TRACE"))


def region_rgb(path: Path, box: tuple) -> tuple:
    """Mean 8-bit RGB of a box (x0, y0, x1, y1 as fractions from the top-left) of an image."""
    import numpy as np
    image = bpy.data.images.load(str(path), check_existing=False)
    try:
        width, height = image.size
        values = np.empty(width * height * 4, dtype=np.float32)
        image.pixels.foreach_get(values)
        rows = values.reshape(height, width, 4)[::-1]
        x0, y0, x1, y1 = (int(v * s) for v, s in zip(box, (width, height, width, height)))
        return tuple(int(round(v * 255)) for v in rows[y0:y1, x0:x1, :3].reshape(-1, 3).mean(axis=0))
    finally:
        bpy.data.images.remove(image)


# ------------------------------------------------------------------ cases

def round_trip(job, spawn):
    started = time.monotonic()
    child, runtime = start(job, spawn, "chair", prefix="Chair")
    measure(f"spawn_to_ready {child.t['ready'] - child.t['spawn']:.2f} s (init {child.ready['init_seconds']:.2f} s, "
            f"no warm-up), whole start {time.monotonic() - started:.2f} s")
    assert child.ready["undo"] == "undo", child.ready
    assert child.ready["stamped"] >= 8, child.ready
    made = run(runtime, "import bpy\nbpy.ops.mesh.primitive_cube_add(size=0.5, location=(0, 0, 0.25))\n"
                        "bpy.context.object.name = 'Chair_Seat'\nleg = bpy.data.objects.new('Leg', None)\n"
                        "bpy.context.scene.collection.objects.link(leg)\nprint('made\\n', bpy.context.object.name, '✓ ü')")
    assert made.ok and "made\n Chair_Seat ✓ ü" in made.text, made.text
    assert made.changed and made.rolled_back is None, (made.changed, made.rolled_back)
    assert made.advisory == {"outside_role": []}, made.advisory  # A mesh and an empty: a modeler's own.
    seen = runtime.run_tool("get_scene_info", "{}")
    assert seen.ok and "Chair_Seat" in seen.text and not seen.changed, seen.text[:400]
    unknown = runtime.run_tool("render_job", "{}")
    assert not unknown.ok and "Unknown tool" in unknown.text, unknown.text
    bad = runtime.run_tool("get_object_info", "{not json")
    assert not bad.ok and "Invalid tool arguments" in bad.text
    # UV tools run in the child: its own seat unwraps, the user's Rug does not, the checker renders as a look.
    unwrapped = runtime.run_tool("uv_unwrap", json.dumps({"objects": ["Chair_Seat"], "method": "smart"}))
    assert unwrapped.ok and "Unwrapped Chair_Seat with smart" in unwrapped.text, unwrapped.text
    assert unwrapped.rolled_back is None, unwrapped.rolled_back
    refused = runtime.run_tool("uv_unwrap", json.dumps({"objects": ["Rug"], "method": "cube"}))
    assert not refused.ok and "the mesh of Rug is not yours" in refused.text, refused.text
    checker = runtime.run_tool("inspect_uv", json.dumps({"objects": ["Chair_Seat"], "checker": True}))
    assert checker.ok and "6 islands" in checker.text and checker.image_path.is_file(), checker.text
    texture = runtime.run_tool("inspect_texture", json.dumps({"target": "Chair_Seat"}))
    assert texture.ok, texture.text
    finished = child.call("finish")
    assert finished["ok"] and Path(finished["result"]).stat().st_size > 0, finished
    assert "OBJECT:Chair_Seat" in finished["manifest"]["created"], finished["manifest"]
    cell = Path(finished["manifest"]["look"] or "")  # Its cell on the wave's contact sheet.
    assert cell.is_file() and max(png_size(cell)) == 384, finished["manifest"].get("look")
    assert finished["manifest"]["facts"]["Chair_Seat"]["tris"] == 12, finished["manifest"]["facts"]
    assert not any(k.startswith("OBJECT:Rug") for k in finished["manifest"]["created"]), finished["manifest"]
    child.exit()
    assert child.process.returncode == 0, child.process.returncode
    note(f"WORKER round trip: ok (peak RSS {child.peak_rss_mb:.0f} MB)")


def unlit_cell(job):
    """A scene with no light of its own: the contact sheet cell is lit by the studio world, not black."""
    spawn = dark_room(job)
    child, runtime = start(job, spawn, "dark", prefix="Dark")
    made = run(runtime, "import bpy\nbpy.ops.mesh.primitive_cube_add(size=0.5, location=(0, 0, 0.25))\n"
                        "bpy.context.object.name = 'Dark_Box'\n")
    assert made.ok, made.text
    finished = child.call("finish")
    cell = Path(finished["manifest"]["look"] or "")
    assert finished["ok"] and cell.is_file(), finished["manifest"].get("look")
    mean = mean_value(cell)
    assert mean > 0.4, f"the unlit scene's cell is dark: mean {mean:.3f} (0.17 in its own light)"
    child.exit()
    note(f"WORKER unlit cell: ok (mean {mean:.2f})")


def glow_room(job: Path) -> Path:
    """An emissive ball before a camera on a dark world, and the scene's own compositor: a Glare bloom."""
    bpy.ops.wm.read_factory_settings(use_empty=False)
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj)
    scene = bpy.context.scene
    scene.render.engine = "BLENDER_EEVEE"
    scene.world.color = (0.02, 0.02, 0.02)
    bpy.ops.mesh.primitive_uv_sphere_add(radius=0.3, location=(0, 0, 0))
    ball = bpy.context.active_object
    ball.name = "Glow_Ball"
    glow = bpy.data.materials.new("Glow_Mat")
    emission = glow.node_tree.nodes.new("ShaderNodeEmission")
    emission.inputs["Strength"].default_value = 30
    glow.node_tree.links.new(emission.outputs[0], glow.node_tree.nodes["Material Output"].inputs["Surface"])
    ball.data.materials.append(glow)
    camera = bpy.data.objects.new("Camera", bpy.data.cameras.new("Camera"))
    scene.collection.objects.link(camera)
    camera.location, camera.rotation_euler, scene.camera = (0, -4, 0), (1.5708, 0, 0), camera
    light = bpy.data.objects.new("Key", bpy.data.lights.new("Key", "POINT"))
    scene.collection.objects.link(light)
    light.location, light.data.energy = (1, -2, 2), 100
    tree = bpy.data.node_groups.new("Bloom", "CompositorNodeTree")
    tree.interface.new_socket(socket_type="NodeSocketColor", name="Image", in_out="OUTPUT")
    layers, glare, out = (tree.nodes.new(t) for t in ("CompositorNodeRLayers", "CompositorNodeGlare", "NodeGroupOutput"))
    for name, value in (("Type", "Bloom"), ("Threshold", 1.0), ("Size", 0.7), ("Strength", 1.0)):
        glare.inputs[name].default_value = value
    tree.links.new(layers.outputs["Image"], glare.inputs["Image"])
    tree.links.new(glare.outputs["Image"], out.inputs[0])
    scene.compositing_node_group = tree
    return save_spawn(job)


def bloom_look(job):
    """The scene shading and the camera crops run the scene's compositor (a glow halo around the ball,
    "its compositor" in the caption); the studio shading does not."""
    spawn = glow_room(job)
    child, runtime = start(job, spawn, "glow", "lighting", prefix="Glow")
    scene_look = runtime.run_tool("capture_viewport", json.dumps({"angle": "camera", "shading": "scene"}))
    assert scene_look.ok and "Render: EEVEE" in scene_look.text and "its compositor" in scene_look.text, scene_look.text
    halo, far = region_rgb(scene_look.image_path, (0.63, 0.45, 0.68, 0.55)), region_rgb(scene_look.image_path, (0, 0, 0.08, 0.1))
    assert sum(halo) - sum(far) > 60, f"no glow beside the ball: {halo} vs the corner {far}"
    studio = runtime.run_tool("capture_viewport", json.dumps({"angle": "camera", "shading": "studio"}))
    assert studio.ok and "its compositor" not in studio.text, studio.text
    crops = runtime.run_tool("capture_viewport", json.dumps({"angle": "crops", "focus": ["Glow_Ball"]}))
    assert crops.ok and "its compositor" in crops.text, crops.text
    child.exit()
    note(f"WORKER bloom: ok (beside the ball {halo}, the corner {far})")


STOOL = ("import bpy\n"
         "def box(name, size, loc, parent=None):\n"
         "    bpy.ops.mesh.primitive_cube_add(size=1, location=loc)\n"
         "    o = bpy.context.object; o.name = name; o.scale = size\n"
         "    bpy.ops.object.transform_apply(scale=True)\n"
         "    if parent: o.parent = parent\n"
         "    return o\n"
         "seat = box('Look_Stool', (0.36, 0.36, 0.04), (-1.2, -0.8, 0.45))\n"
         "for i, (x, y) in enumerate(((-0.15, -0.15), (0.15, -0.15), (-0.15, 0.15), (0.15, 0.15))):\n"
         "    box(f'Leg{i}', (0.03, 0.03, 0.43), (-1.2 + x, -0.8 + y, 0.215), seat)\n")


def looks(job, spawn):
    """Each role's default look (what capture_viewport with no angle gives), its size, what it frames
    and says, a named angle through run_python's capture, and the final look; timings measured."""
    for role in ROLES:
        child, runtime = start(job, spawn, f"look-{role}", role, warm=True, prefix="Look", deliverable="Look_Stool",
                               checks=[{"kind": "in_frame", "camera": "Camera", "names": ["Table"],
                                        "region": [0.2, 0.2, 0.8, 0.8], "quote": "the table in frame"}])
        if role == "modeling":
            built = run(runtime, STOOL)
            assert built.ok and not built.rolled_back, built.text
        before = trace_of(run(runtime, trace_code()))
        started = time.monotonic()
        shot = runtime.run_tool("capture_viewport", "{}")
        first = time.monotonic() - started
        assert shot.ok and shot.image_path and shot.image_path.is_file(), shot.text
        assert shot.image_path.parent == child.folder / "captures", shot.image_path
        width, height = png_size(shot.image_path)
        assert 1020 <= max(width, height) <= 1024, (role, width, height)  # A grid of 3 is 3 x 341.
        named = runtime.run_tool("capture_viewport", json.dumps({"labels": True}))
        labels = pixels_like(named.image_path, LABEL_BOX) if named.ok else 0
        assert labels > 150, f"{role}: no labels drawn ({labels} label px)"
        if role in ("camera", "lighting"):  # One wide tile through the camera: only the names add boxes.
            assert labels > pixels_like(shot.image_path, LABEL_BOX) + 150, \
                f"{role}: labels: true adds no names to the default look"
        if role == "modeling":
            assert (width, height) == (1024, 672) and "your Look_Stool (5 objects)" in shot.text \
                and "front and back views" in shot.text, (width, height, shot.text)
        if role in ("camera", "lighting"):
            assert "through Camera" in shot.text and (width, height) == (1024, 576), (role, width, height, shot.text)
        if role == "camera":
            assert pixels_like(shot.image_path, (255, 255, 255)) > 600, "camera: no thirds lines"
            assert pixels_like(shot.image_path, (255, 200, 40)) > 300, "camera: the brief's region is not drawn"
        if role == "lighting":
            assert "Exposure" in shot.text and "subject " in shot.text and "EEVEE" in shot.text, shot.text
        if role == "animation":
            assert "frames 1, " in shot.text and height >= 500, (shot.text, height)
        if role == "materials":
            assert "Palette_Wood" in shot.text and "roughness 0.60" in shot.text, shot.text
        after = trace_of(run(runtime, trace_code()))
        assert after == before, f"{role}: the look left a trace:\n{before}\n{after}"
        started = time.monotonic()
        again = runtime.run_tool("run_python", json.dumps({"code": "print('x')", "capture": "front"}))
        second = time.monotonic() - started
        assert again.ok and again.image_path and "Image attached" in again.text, again.text
        started = time.monotonic()
        final = runtime.run_tool("capture_viewport", json.dumps({"angle": "final"}))
        last = time.monotonic() - started
        # Modeling's final look frames its own work (worker_capture.own_focus); the others', with nothing
        # of their own to frame, go through the camera.
        framed = "three quarter view of your Look_Stool" if role == "modeling" else "through Camera"
        assert final.ok and "final look" in final.text and framed in final.text \
            and "Exposure" in final.text, final.text
        assert trace_of(run(runtime, trace_code())) == before, f"{role}: the final look left a trace"
        numbers = next(line for line in final.text.splitlines() if line.startswith("Exposure"))
        measure(f"look {role}: init with warm-up {child.ready['init_seconds']:.2f} s (warm {child.ready['warm_seconds']:.2f} s), "
                f"default look {first:.2f} s {width}x{height}, run_python+front {second:.2f} s, final {last:.2f} s; "
                f"{labels} label px; {numbers}")
        child.exit()
    cycles = job.parent / "job-cycles"
    bpy.ops.wm.open_mainfile(filepath=str(spawn))
    bpy.context.scene.render.engine = "CYCLES"
    bpy.context.scene.cycles.samples = 256
    cycles_spawn = save_spawn(cycles)
    child, runtime = start(cycles, cycles_spawn, "look-cycles", "lighting", warm=True, prefix="Look",
                           checks=[{"kind": "in_frame", "camera": "Camera", "names": ["Table"], "quote": "the table"}])
    started = time.monotonic()
    shot = runtime.run_tool("capture_viewport", "{}")
    first = time.monotonic() - started
    assert shot.ok and "Cycles" in shot.text and "16 samples" in shot.text and "Exposure" in shot.text, shot.text
    samples = run(runtime, "import bpy\nprint('SAMPLES', bpy.context.scene.cycles.samples)")
    assert "SAMPLES 256" in samples.text, samples.text
    measure(f"look lighting on Cycles (CPU, 16 of 256 samples, denoised): init with warm-up "
            f"{child.ready['init_seconds']:.2f} s, look {first:.2f} s")
    child.exit()
    note("WORKER looks per role: ok")


def craft_look(job, spawn):
    """check_brief's final look of the deliverable, attached the first time the checks pass, once; the
    brief's advisory checks shown beside them, a miss never holding it back."""
    child, runtime = start(job, spawn, "craft", prefix="Look", deliverable="Look_Stool",
                           checks=[{"kind": "exists", "name": "Look_Stool", "quote": "a stool"}],
                           advisory=[{"kind": "flat_shaded", "name": "Look_Stool"},
                                     {"kind": "triangles", "name": "Look_Stool", "max": 10}])
    failing = runtime.run_tool("check_brief", "{}")
    assert failing.ok and "0 of 1 checks pass" in failing.text and failing.image_path is None, failing.text
    built = run(runtime, STOOL)
    assert built.ok, built.text
    passing = runtime.run_tool("check_brief", "{}")
    assert passing.ok and passing.image_path and passing.image_path.is_file(), passing.text
    assert "craft-pass view" in passing.text and "final look" in passing.text \
        and "your Look_Stool (5 objects)" in passing.text, passing.text
    assert "a1 met: flat_shaded Look_Stool" in passing.text and "a2 missed: triangles Look_Stool (60 triangles" \
        in passing.text, passing.text
    measured = child.call("check")["results"]
    assert [(r["id"], r["status"], r.get("advisory", False)) for r in measured] == [
        ("c1", "pass", False), ("a1", "pass", True), ("a2", "fail", True)], measured
    again = runtime.run_tool("check_brief", "{}")
    assert again.image_path is None and "1 of 1 checks pass" in again.text, again.text
    child.exit()
    note("WORKER craft look: ok")


PAINTED = ("import bpy\nimport numpy as np\n"
           "bpy.ops.mesh.primitive_cube_add(size=0.4, location=(0.4, 0.1, 0.92))\n"
           "box = bpy.context.object\nbox.name = 'Paint_Box'\n"
           "image = bpy.data.images.new('Paint_Cream', 64, 64, alpha=True)\n"
           "pixels = np.empty((64, 64, 4), dtype=np.float32)\npixels[...] = (0.9, 0.8, 0.58, 1.0)\n"
           "image.pixels.foreach_set(pixels.ravel())\nimage.update()\nimage.pack()\n"
           "material = bpy.data.materials.new('Paint_Cream')\nnodes = material.node_tree.nodes\n"
           "texture = nodes.new('ShaderNodeTexImage')\ntexture.image = image\n"
           "bsdf = next(n for n in nodes if n.type == 'BSDF_PRINCIPLED')\n"
           "material.node_tree.links.new(texture.outputs['Color'], bsdf.inputs['Base Color'])\n"
           "material.diffuse_color = (0.2, 0.3, 0.4, 1.0)\n"
           "box.data.materials.append(material)\n")
CREAM = (230, 204, 148)          # The painted 8-bit value.
FACE = (0.42, 0.24, 0.58, 0.36)  # Of a front view of the box: its front face, above the name drawn at its centre.


def texture_fidelity(job, spawn):
    """A painted texture shows as painted: the materials look shows the cream face within a light's
    falloff of cream (warm, not the slate studio.exr gave), albedo shows it exactly; captions say how
    each image was made; angles x shadings in one sheet; crops; an object in front is named and
    isolate removes it; nothing is left behind."""
    child, runtime = start(job, spawn, "paint", "materials", warm=True, prefix="Paint", deliverable="Paint_Box")
    made = run(runtime, PAINTED)
    assert made.ok, made.text
    before = trace_of(run(runtime, trace_code()))
    studio = runtime.run_tool("capture_viewport", json.dumps({"angle": "front", "focus": ["Paint_Box"]}))
    assert studio.ok and "Render: EEVEE 16 samples, studio (" in studio.text and "Standard view" in studio.text, studio.text
    lit = region_rgb(studio.image_path, FACE)
    assert all(abs(a - b) <= 30 for a, b in zip(lit, CREAM)) and lit[0] - lit[2] > 50, f"studio shows {lit}, painted {CREAM}"
    albedo = runtime.run_tool("capture_viewport", json.dumps({"angle": "front", "focus": ["Paint_Box"],
                                                              "shading": ["albedo"]}))
    flat = region_rgb(albedo.image_path, FACE)
    assert all(abs(a - b) <= 4 for a, b in zip(flat, CREAM)) and "albedo (" in albedo.text, (flat, albedo.text)
    sheet = runtime.run_tool("capture_viewport", json.dumps({"angles": ["front", "back"], "focus": ["Paint_Box"],
                                                             "shading": ["studio", "albedo"]}))
    assert sheet.ok and "Panels: 1 front - studio: camera at (" in sheet.text and "4 back - albedo" in sheet.text \
        and png_size(sheet.image_path) == (1024, 768) and "Warning" not in sheet.text, sheet.text
    crops = runtime.run_tool("capture_viewport", json.dumps({"angle": "crops", "focus": ["Paint_Box", "Table"]}))
    assert crops.ok and "camera-scale review through Camera at its output size 1920x1080" in crops.text \
        and "\n1 Paint_Box: " in crops.text and "px per cm" in crops.text and "\n2 Table: " in crops.text, crops.text
    after = trace_of(run(runtime, trace_code()))
    assert after == before, f"a look left a trace:\n{before}\n{after}"
    blocked = run(runtime, "import bpy\nbpy.ops.mesh.primitive_cube_add(size=0.5, location=(0.4, -0.7, 0.92))\n"
                           "bpy.context.object.name = 'Paint_Blocker'\n")
    assert blocked.ok, blocked.text
    hidden = runtime.run_tool("capture_viewport", json.dumps({"angle": "front", "focus": ["Paint_Box"]}))
    assert "Paint_Blocker hides" in hidden.text and "isolate shows it alone" in hidden.text, hidden.text
    alone = runtime.run_tool("capture_viewport", json.dumps({"angle": "front", "focus": ["Paint_Box"], "isolate": True}))
    assert "Warning" not in alone.text and "the rest hidden (isolate)" in alone.text, alone.text
    assert region_rgb(alone.image_path, FACE)[0] - region_rgb(alone.image_path, FACE)[2] > 50, "isolate: the box"
    child.exit()
    note(f"WORKER texture fidelity: ok (painted {CREAM}, studio {lit}, albedo {flat})")


REFERENCE_RGB = (200, 40, 160)   # A magenta nothing in the living room has.


def make_still(path: Path, size=(768, 432), rgb=REFERENCE_RGB) -> Path:
    """A still of one colour, the shape of an attachment's 768 px copy of a 16:9 frame."""
    import numpy as np
    image = bpy.data.images.new("check_still", *size)
    try:
        image.pixels.foreach_set(np.tile(np.array([*(c / 255 for c in rgb), 1.0], dtype=np.float32), size[0] * size[1]))
        image.filepath_raw, image.file_format = str(path), "PNG"
        image.save()
    finally:
        bpy.data.images.remove(image)
    return path


def reference_tile(job, spawn):
    """The user's still beside a worker's look (a brief's references): top right, 320 px on its long side,
    no taller than the look, labelled, in the caption; not on the crops; a bad file costs nothing."""
    still = make_still(job / "still.png")
    child, runtime = start(job, spawn, "refs", "materials", prefix="Refs", deliverable="Refs_Box",
                           reference={"path": str(still), "name": "ref1.jpg"})
    made = run(runtime, "import bpy\nbpy.ops.mesh.primitive_cube_add(size=0.4, location=(0, 0, 0.9))\n"
                        "bpy.context.object.name = 'Refs_Box'\n")
    assert made.ok, made.text
    look = runtime.run_tool("capture_viewport", json.dumps({"angle": "front", "focus": ["Refs_Box"]}))
    width, height = png_size(look.image_path)
    assert look.ok and (width, height) == (1024 + 4 + 320, 768), (width, height, look.text)
    assert "\nRight, labelled reference: the user's ref1.jpg (320x180 px), to compare your render with." in look.text, \
        look.text
    tile = region_rgb(look.image_path, ((width - 300) / width, 30 / height, (width - 20) / width, 170 / height))
    assert all(abs(a - b) <= 3 for a, b in zip(tile, REFERENCE_RGB)), f"the tile shows {tile}"
    label = pixels_like(look.image_path, LABEL_BOX)
    render = region_rgb(look.image_path, (0.3, 0.3, 0.5, 0.5))
    assert label > 100 and render != tile, (label, render)
    final = runtime.run_tool("capture_viewport", json.dumps({"angle": "final"}))
    assert final.ok and "labelled reference" in final.text and png_size(final.image_path)[0] > 1024, final.text
    crops = runtime.run_tool("capture_viewport", json.dumps({"angle": "crops", "focus": ["Refs_Box"]}))
    assert crops.ok and "reference" not in crops.text, crops.text
    finished = child.call("finish")
    cell_look = Path(finished["manifest"]["look"] or "")
    assert finished["ok"] and png_size(cell_look)[0] <= 384, "the contact sheet cell is the worker's own"
    child.exit()
    shown = [look.image_path.name, final.image_path.name]
    child, runtime = start(job, spawn, "refs_bad", "materials", prefix="RefsBad",
                           reference={"path": str(job / "no_such.png"), "name": "gone.jpg"})
    plain = runtime.run_tool("capture_viewport", json.dumps({"angle": "front"}))
    assert plain.ok and "reference" not in plain.text and png_size(plain.image_path)[0] == 1024, plain.text
    child.exit()
    from loopcut import worker_capture
    cell = worker_capture.reference_cell(still, (384, 288), job / "ref-sheet.png")
    assert png_size(cell) == (384, 288), png_size(cell)
    inside = region_rgb(cell, (0.3, 0.3, 0.7, 0.7))
    edge = region_rgb(cell, (0.4, 0.0, 0.6, 0.05))
    assert all(abs(a - b) <= 3 for a, b in zip(inside, REFERENCE_RGB)) and edge == (38, 38, 38), (inside, edge)
    note(f"WORKER reference: ok (looks {', '.join(shown)} {width}x{height}, tile {tile}; cell 384x288)")


def rollback(job, spawn):
    child, runtime = start(job, spawn, "table", prefix="Table2", owned=["OBJECT:Lamp", "LIGHT:Lamp"])
    bad = run(runtime, "import bpy\nbpy.data.objects['Rug'].scale = (2, 2, 1)\n"
                       "top = bpy.data.objects.new('Table2_Top', None)\nbpy.context.scene.collection.objects.link(top)")
    assert bad.rolled_back and bad.rolled_back["keys"] == ["OBJECT:Rug"], (bad.rolled_back, bad.text)
    assert "Rolled back: this call changed Rug" in bad.text and not bad.changed, bad.text
    state = run(runtime, "import bpy\nprint('STATE', tuple(round(v, 3) for v in bpy.data.objects['Rug'].scale), "
                         "'Table2_Top' in bpy.data.objects)")
    assert "STATE (1.0, 1.0, 1.0) False" in state.text, state.text
    owned = run(runtime, "import bpy\nbpy.data.lights['Lamp'].energy = 250\nbpy.data.objects['Lamp'].location.z = 3")
    assert owned.rolled_back is None and owned.changed, (owned.rolled_back, owned.text)
    palette = run(runtime, "import bpy\nbpy.data.materials['Palette_Wood'].diffuse_color = (0, 1, 0, 1)\n"
                           "bpy.data.materials['Palette_Wood'].node_tree.nodes['Principled BSDF'].inputs['Roughness']"
                           ".default_value = 0.1")
    assert palette.rolled_back and palette.rolled_back["keys"] == ["MATERIAL:Palette_Wood"], palette.rolled_back
    assert "copy it: m = bpy.data.materials['Palette_Wood'].copy(); m.name = 'Table2_Wood'" in palette.text, palette.text
    copied = run(runtime, "import bpy\nm = bpy.data.materials['Palette_Wood'].copy()\nm.name = 'Table2_Wood'\n"
                          "m.node_tree.nodes['Principled BSDF'].inputs['Roughness'].default_value = 0.2\n"
                          "bpy.ops.mesh.primitive_cube_add(size=0.1)\nbpy.context.object.name = 'Table2_Leg'\n"
                          "bpy.context.object.data.materials.append(m)")
    assert copied.rolled_back is None and copied.changed, (copied.rolled_back, copied.text)
    measured_only = runtime.run_tool("frame_camera", json.dumps({"camera": "Camera", "objects": ["Table"],
                                                                 "report_only": True}))
    assert measured_only.ok and measured_only.rolled_back is None, measured_only.text
    moved = runtime.run_tool("frame_camera", json.dumps({"camera": "Camera", "objects": ["Table"]}))
    assert moved.rolled_back and moved.rolled_back["keys"] == ["OBJECT:Camera"], (moved.rolled_back, moved.text)
    check = run(runtime, "import bpy\nprint('KEPT', bpy.data.lights['Lamp'].energy, "
                         "round(bpy.data.objects['Camera'].location.x, 3), "
                         "round(bpy.data.materials['Palette_Wood'].diffuse_color[1], 3))")
    assert "KEPT 250.0 4.2" in check.text and "0.28" in check.text, check.text
    assert runtime.rollbacks == 3, runtime.rollbacks
    finished = child.call("finish")
    manifest = finished["manifest"]
    assert "OBJECT:Lamp" in manifest["owned_written"] and "OBJECT:Table2_Top" not in manifest["created"], manifest
    assert "MATERIAL:Table2_Wood" in manifest["created"] and not manifest["advisory"]["changed_not_owned"], manifest
    assert len(manifest["rollbacks"]) == 3, manifest["rollbacks"]
    child.exit()
    note("WORKER rollback: ok (Rug, Palette_Wood and Camera undone, with how to copy a palette material; "
         "the owned Lamp and a worker's own copy of Palette_Wood kept)")


def timeout_and_crash(job, spawn):
    saved = worker_ipc.TOOL_GRACE
    worker_ipc.TOOL_GRACE = 2.0  # A tool's timeout is run_timeout + this: 3 s here.
    try:
        child, runtime = start(job, spawn, "slow", run_timeout=1)
        run(runtime, "import bpy\nbpy.data.objects.new('Slow_Thing', None)")
        started = time.monotonic()
        stuck = run(runtime, "import time\ntime.sleep(30)")  # One native call: the run deadline cannot stop it.
        waited = time.monotonic() - started
        assert not stuck.ok and stuck.crashed == "timeout", (stuck.crashed, stuck.text)
        assert waited < 3 + worker_host.KILL_GRACE + 1, waited
        assert child.process.poll() is not None and runtime.crashed == "timeout"
        refused = runtime.run_tool("get_scene_info", "{}")
        assert not refused.ok and refused.crashed, refused.text
        runtime.restart()
        fresh = run(runtime, "import bpy\nprint('FRESH', 'Slow_Thing' in bpy.data.objects, 'Rug' in bpy.data.objects)")
        assert fresh.ok and "FRESH False True" in fresh.text, fresh.text
        died = run(runtime, "import os\nos._exit(7)")
        assert not died.ok and died.crashed == "exited", (died.crashed, died.text)
        assert "code 7" in died.text, died.text
        note(f"WORKER timeout: ok (killed after {waited:.1f} s, crash status; restart back at spawn; exit is a crash)")
    finally:
        worker_ipc.TOOL_GRACE = saved


CHEST = ("import bpy\n"
         "me = bpy.data.meshes.new('Chest')\n"
         "me.from_pydata([(x, y, z) for x in (-0.4, 0.4) for y in (-0.2, 0.2) for z in (0, 0.5)], [],\n"
         "               [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)])\n"
         "ob = bpy.data.objects.new('Chest', me)\n"
         "bpy.context.scene.collection.objects.link(ob)\n"
         "ob.location = (-1.5, 1.5, 0)\n")
# The asset_pack TreasureChest worker's craft pass (GLM, 2026-09-29): its second polygons.add raised
# ValueError after the first had left a face with no corners; the next look segfaulted the child twice.
RIVETS = ("import bpy\n"
          "mesh = bpy.data.objects['Chest'].data\n"
          "base = len(mesh.vertices)\n"
          "for co in [(a, -0.21, c) for a in (-0.01, 0.01) for c in (0.2, 0.22)]:\n"
          "    mesh.vertices.add(1)\n"
          "    mesh.vertices[-1].co = co\n"
          "mesh.polygons.add(1)\n"
          "mesh.polygons[-1].vertices = [base, base + 1, base + 3, base + 2]\n")


def broken_mesh(job, spawn):
    child, runtime = start(job, spawn, "chest", prefix="Chest", deliverable="Chest",
                           checks=[{"kind": "exists", "name": "Chest", "quote": "a chest"}])
    built = run(runtime, CHEST)
    assert built.ok, built.text
    passing = runtime.run_tool("check_brief", "{}")
    assert passing.image_path and "final look" in passing.text, passing.text
    broken = run(runtime, RIVETS)
    assert not broken.ok and "array length cannot be changed to 4 (expected 0)" in broken.text, broken.text
    assert "Loopcut repaired the mesh of Chest" in broken.text, broken.text
    for angle in ("three_quarter", "final"):
        shot = runtime.run_tool("capture_viewport", json.dumps({"angle": angle}))
        assert shot.ok and shot.image_path and not shot.crashed, (angle, shot.crashed, shot.text)
    faces = run(runtime, "import bpy\nme = bpy.data.objects['Chest'].data\n"
                         "print('FACES', len(me.polygons), min(p.loop_total for p in me.polygons))")
    assert "FACES 6 4" in faces.text, faces.text
    finished = child.call("finish")
    manifest = finished["manifest"]
    assert manifest["facts"]["Chest"]["faces"] == 6 and manifest["missing"] == [], manifest
    assert child.alive()
    child.exit()
    note("WORKER broken mesh: ok (the corner-less face repaired and said so; clay and final looks render)")


def crash_kit(job, spawn):
    child, runtime = start(job, spawn, "segv", prefix="Chest", deliverable="Chest")
    assert run(runtime, CHEST).ok
    # Windows ctypes turns every exception in a foreign call into OSError except a breakpoint, which it leaves
    # unhandled so a debugger can attach: with none attached it reaches Blender's crash handler, as a real fault
    # does (faulthandler._sigsegv() aborts with exit code 3 and no crash report, CI 2026-10-22).
    fault = ("import ctypes\nctypes.windll.kernel32.DebugBreak()" if os.name == "nt"
             else "import ctypes\nctypes.string_at(0)")
    died = run(runtime, fault)
    assert died.crashed == "exited" and died.crash_kept, (died.crashed, died.text)
    kit = Path(died.crash_kept)
    assert kit == child.folder / "crash-1", kit
    record = json.loads((kit / "crash.json").read_text())
    assert record["tool"] == "run_python" and record["report"] and record["blend"] == "spawn.blend", record
    report = (kit / record["report"][0]).read_text()
    assert "# backtrace" in report, report[:200]
    assert (kit / "spawn.blend").stat().st_size == spawn.stat().st_size
    assert "Writing:" in (kit / "worker.log.tail.txt").read_text()
    assert not list((child.folder / "tmp").glob("*.crash.txt"))
    runtime.restart()
    manifest = runtime.child.call("finish")["manifest"]
    assert manifest["missing"] == ["Chest"] and manifest["facts"] == {}, manifest
    runtime.child.exit()
    note(f"WORKER crash kit: ok ({', '.join(sorted(p.name for p in kit.iterdir()))}; after restart the deliverable "
         f"is missing at finish)")


def busy_call(runtime, cancel, outcomes):
    try:
        runtime.run_tool("run_python", json.dumps({"code": "import time\ntime.sleep(60)"}), cancel.is_set)
        outcomes.append("returned")
    except llm.Cancelled:
        outcomes.append("cancelled")


def stop(job, spawn, count: int):
    children = [start(job, spawn, f"stop{i}") for i in range(count)]
    cancel, outcomes = threading.Event(), []
    threads = [threading.Thread(target=busy_call, args=(runtime, cancel, outcomes)) for _child, runtime in children]
    for thread in threads:
        thread.start()
    time.sleep(1.5)  # All of them inside model code.
    started = time.monotonic()
    cancel.set()
    for thread in threads:
        thread.join(timeout=10)
    remaining = [c.pid for c, _r in children if gone(c.pid, 5) is None]
    took = time.monotonic() - started
    assert not remaining, f"still running: {remaining}"
    assert outcomes == ["cancelled"] * count, outcomes
    assert took < 3.0, f"Stop took {took:.2f} s"
    measure(f"stop {count} children busy in model code: all gone in {took:.2f} s")
    note(f"WORKER stop: ok ({count} children in {took:.2f} s)")


HELPER = """
import sys, time
sys.path.insert(0, {addons!r})
from loopcut import worker_host, worker_ipc
worker_ipc.KEEP_ENV = {keep_env!r}
child = worker_host.Child({job!r}, {worker!r}, {spawn!r}, "modeling", binary={binary!r}, run_timeout=60).start()
assert child.call("init", {{"warm": False}})["ok"]
if {busy!r}:
    child.channel.send({{"id": 2, "op": "tool", "name": "run_python",
                         "arguments": {{"code": "import time\\ntime.sleep(120)"}}}})
    time.sleep(1.0)
print(child.pid, flush=True)
time.sleep(600)
"""


def killed_parent(job, spawn):
    addons = str(checkout.PACKAGE.parent)
    for busy in (False, True):
        worker = "orphan-busy" if busy else "orphan-idle"
        script = work / f"helper_{worker}.py"
        script.write_text(HELPER.format(addons=addons, job=str(job), worker=worker, spawn=str(spawn), binary=BINARY,
                                        busy=busy, keep_env=worker_ipc.KEEP_ENV), encoding="utf-8")
        helper = subprocess.Popen([PYTHON, str(script)], stdout=subprocess.PIPE, text=True)
        try:
            pid = int(helper.stdout.readline().strip())
            created = worker_process.windows_creation(pid) if os.name == "nt" else None
        finally:
            helper.kill()  # TerminateProcess on Windows, SIGKILL on POSIX.
            helper.wait()
        took = gone(pid, 5.0)
        if took is None:
            if os.name == "nt":
                worker_process.windows_kill(pid, created)
            else:
                os.kill(pid, signal.SIGKILL)
        assert took is not None, f"{worker}: child {pid} outlived its force-killed parent by 5 s"
        how = "busy in model code: the watchdog" if busy else "idle: EOF"
        measure(f"child of a force-killed parent gone in {took:.2f} s ({how})")
    note("WORKER killed parent: ok (no child after 5 s, idle or busy)")


def reap(job):
    left = worker_host.reap_orphans(job.parent)
    assert left == [], f"children of the killed helpers were still there: {left}"
    assert not list(job.glob(f"w1/orphan-*/{worker_host.RECORD}")), "records of dead children were kept"
    root = work / "reap"
    dead = subprocess.Popen([PYTHON, "-c", "pass"])
    dead.wait()
    sleepers = {}
    for name, owner, looks_like_worker in (("stuck", dead.pid, True), ("mine", os.getpid(), True),
                                           ("reused", dead.pid, False)):
        record_dir = root / "job" / "w1" / name
        record_dir.mkdir(parents=True)
        argv = [PYTHON, "-c", "import time; time.sleep(60)"]
        if looks_like_worker:
            argv += [worker_host.CHILD_SCRIPT.name, "--job", str(root / "job")]
        sleepers[name] = subprocess.Popen(argv)
        record = {"pid": sleepers[name].pid, "owner": owner, "job": str(root / "job"), "worker": name}
        if os.name == "nt":
            created = worker_process.windows_creation(sleepers[name].pid)
            assert created is not None, f"could not query creation time for {name}"
            # Windows identifies workers by creation time, not command line. Mismatch simulates PID reuse.
            record["windows_created"] = created if looks_like_worker else created - 1
        (record_dir / worker_host.RECORD).write_text(json.dumps(record))
    time.sleep(0.3)
    try:
        killed = worker_host.reap_orphans(root)
        assert killed == [sleepers["stuck"].pid], (killed, {k: p.pid for k, p in sleepers.items()})
        assert sleepers["stuck"].wait(timeout=5) != 0
        assert sleepers["mine"].poll() is None and sleepers["reused"].poll() is None, "reaped the wrong process"
    finally:
        for process in sleepers.values():
            if process.poll() is None:
                process.kill()
                process.wait()
    note("WORKER reap orphans: ok (a stuck orphan killed; a live owner's child and a reused pid left alone)")


def keys(job, spawn):
    fake_env = work / "fake.env"
    fake_env.write_text("LOOPCUT_API_KEY" + "=" + FAKE + "\n", encoding="utf-8")
    planted = {"LOOPCUT_API_KEY": FAKE, "OPENAI_API_KEY": FAKE, "SOME_TOKEN": FAKE, "LOOPCUT_ENV_FILE": str(fake_env)}
    saved = {name: os.environ.get(name) for name in planted}
    os.environ.update(planted)
    try:
        child, runtime = start(job, spawn, "keys")
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    seen = run(runtime, "import os\nfrom loopcut import config\nenv_file, get = config._getter()\n"
                        "print('ENV', sorted(os.environ))\nprint('FILE', env_file)\n"
                        "print('KEY', bool(get('LOOPCUT_API_KEY')), bool(get('OPENAI_API_KEY')))\n"
                        f"print('LEAK', any({FAKE!r} in v for v in os.environ.values()))")
    names = json.loads(next(line[4:] for line in seen.text.splitlines() if line.startswith("ENV ")).replace("'", '"'))
    assert not [n for n in names if worker_ipc.is_secret(n)], names
    assert "LOOPCUT_API_KEY" not in names and "OPENAI_API_KEY" not in names, names
    assert f"FILE {job / 'child.env'}" in seen.text, seen.text
    assert "KEY False False" in seen.text and "LEAK False" in seen.text, seen.text
    child.exit()
    extra = sorted(set(names) - {"PATH", "HOME", "LANG", "TMPDIR", "LOOPCUT_WORKER", "LOOPCUT_ENV_FILE"})
    note(f"WORKER keys: ok (the child's environment: {len(names)} names"
         f"{', Blender adds ' + ', '.join(extra) if extra else ''})")


def make_kit(folder: Path) -> None:
    """asset_libraries_check's kit: one material and one object asset in one .blend."""
    folder.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.read_homefile(use_empty=True)
    material = bpy.data.materials.new("Harness Oak")
    material.asset_mark()
    stool = bpy.data.objects.new("Harness Stool", bpy.data.meshes.new("Harness Stool"))
    bm = bmesh.new()
    bmesh.ops.create_cone(bm, segments=16, depth=0.45, radius1=0.3, radius2=0.3, cap_ends=True)
    bm.to_mesh(stool.data)
    bm.free()
    bpy.context.scene.collection.objects.link(stool)
    stool.asset_mark()
    bpy.ops.wm.save_as_mainfile(filepath=str(folder / "kit.blend"), copy=True)
    bpy.ops.export_scene.gltf(filepath=str(folder / "stool.glb"), export_format="GLB")  # Poly Pizza's format.


def asset_split(job, spawn, kit: Path):
    prefs = bpy.context.preferences.filepaths
    for library in list(prefs.asset_libraries):
        prefs.asset_libraries.remove(library)
    prefs.asset_libraries.new(name="Harness Kit", directory=str(kit))  # In memory: factory startup, never saved.
    fetched_here = []

    def fetch(arguments):
        from loopcut import assets
        if arguments["source"] == "polypizza":  # No key here: what fetch_polypizza returns, for a local GLB.
            return {"source": "polypizza", "id": arguments["id"], "path": str(kit / "stool.glb"),
                    "model": {"ID": arguments["id"], "Title": "Stool", "Licence": "CC0", "Attribution": ""}}
        fetched = assets.fetch_asset(**arguments)
        fetched_here.append(fetched)
        return fetched

    child = worker_host.Child(job, "kit", spawn, "modeling", binary=BINARY, run_timeout=60).start()
    assert child.call("init", {"prefix": "Kit", "warm": False})["ok"]
    runtime = worker_host.WorkerRuntime(child, {"fetch_asset": fetch})
    stool = runtime.run_tool("import_asset", json.dumps({"source": "library",
                                                          "id": "Harness Kit::kit.blend::OBJECT::Harness Stool"}))
    assert stool.ok and "Harness Stool" in stool.text and '"bounds"' in stool.text, stool.text
    assert stool.changed and stool.rolled_back is None, (stool.changed, stool.rolled_back)
    assert stool.advisory == {"outside_role": []}, stool.advisory  # Names are the worker's own (no prefix rule).
    assert "Harness Stool" not in bpy.data.objects, "the parent only fetched"
    run(runtime, "import bpy\nbpy.ops.mesh.primitive_cube_add(size=0.4)\nbpy.context.object.name = 'Kit_Box'")
    oak = runtime.run_tool("import_asset", json.dumps({"source": "library", "id": "Harness Kit::kit.blend::MATERIAL::Harness Oak",
                                                        "apply_to": ["Kit_Box"]}))
    assert oak.ok and "applied to Kit_Box" in oak.text, oak.text
    missing = runtime.run_tool("import_asset", json.dumps({"source": "library", "id": "Harness Kit::kit.blend::OBJECT::Nope"}))
    assert not missing.ok and "search again" in missing.text and len(fetched_here) == 2, missing.text
    assert all(f["source"] == "library" and Path(f["blend"]).is_file() for f in fetched_here), fetched_here
    seen = run(runtime, "import bpy\nprint('SEEN', 'Harness Stool' in bpy.data.objects, "
                        "bpy.data.objects['Kit_Box'].active_material.name)")
    assert "SEEN True Harness Oak" in seen.text, seen.text
    glb = runtime.run_tool("import_asset", json.dumps({"source": "polypizza", "id": "stool1", "target_size": 2.0}))
    assert glb.ok and "so the largest side is 2 m" in glb.text, glb.text
    assert "From Poly Pizza" in glb.text, glb.text
    child.exit()
    note("WORKER import split: ok (fetched here, loaded in the child, applied to a child-only object)")


def three_at_once(task_id: str, job: Path):
    if not (Path(__file__).parent / "evals").exists():
        note(f"SKIP worker RSS {task_id} (eval harness not in this checkout)")
        return
    spawn = eval_start_scene(task_id, job)
    children, lock, errors = [], threading.Lock(), []

    def one(role):
        try:
            child, runtime = start(job, spawn, f"rss-{role}", role, warm=True, prefix="Probe")
            with lock:
                children.append(child)
            made = run(runtime, "import bpy\nfor i in range(3):\n    bpy.ops.mesh.primitive_uv_sphere_add(radius=0.1, "
                                "location=(i * 0.3, 0, 0.1))\n    bpy.context.object.name = f'Probe_{i}'")
            assert made.ok, made.text
            started = time.monotonic()
            shot = runtime.run_tool("capture_viewport", "{}")
            assert shot.ok, shot.text
            child.first_look = time.monotonic() - started
        except Exception as ex:  # noqa: BLE001
            errors.append(f"{role}: {ex}")

    threads = [threading.Thread(target=one, args=(role,)) for role in ("modeling", "materials", "lighting")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=240)
    try:
        assert not errors, errors
        peaks = {c.role: c.peak_rss_mb for c in children}
        measure(f"3 children at once on {task_id}: peak RSS " + ", ".join(f"{r} {v:.0f} MB" for r, v in peaks.items())
                + f"; sum {sum(peaks.values()):.0f} MB; spawn-to-ready "
                + ", ".join(f"{c.role} {c.t['ready'] - c.t['spawn']:.1f} s" for c in children)
                + "; first look " + ", ".join(f"{c.role} {c.first_look:.1f} s" for c in children))
    finally:
        for child in children:
            child.exit()


def tracking(job: Path):
    """Text blocks and paint in Scene changes, through the real child (eval 20260930-154037: the
    painter's 4K atlas and its ArcaneRecipe edits read "Scene changes: none", W0022 W0031 W0039)."""
    living_room(job)
    bpy.data.texts.new("UserNotes").from_string("the user's notes\n")
    spawn = save_spawn(job)
    child, runtime = start(job, spawn, "painter", prefix="Chest")
    paint = ("import bpy, numpy as np\nimage = bpy.data.images['Chest_Atlas']\n"
             "image.pixels.foreach_set(np.random.default_rng({seed}).random(len(image.pixels), dtype=np.float32))\n")
    made = run(runtime, "import bpy\nfrom loopcut import scene_diff\nscene_diff.MAX_IMAGE_PIXELS = 4096\n"
                        "bpy.data.texts.new('ArcaneRecipe').from_string('PALETTE = 1\\n')\nx = 1")
    assert made.ok and made.changed and "+ text ArcaneRecipe (2 lines)" in made.text, made.text
    fresh = run(runtime, "print(x)")
    assert not fresh.ok and "NameError" in fresh.text, fresh.text  # A new namespace each call (run_python's description).
    added = run(runtime, "import bpy\nbpy.data.images.new('Chest_Atlas', 128, 128)\n" + paint.format(seed=1)
                + "image.pack()")
    assert added.changed and "+ image Chest_Atlas (128x128)" in added.text, added.text
    packed = run(runtime, paint.format(seed=2) + "image.pack()")
    assert packed.changed and "~ image Chest_Atlas: pixels edited (packed again)" in packed.text, packed.text
    unsaved = run(runtime, paint.format(seed=3))
    assert unsaved.changed and "~ image Chest_Atlas: pixels edited (unsaved)" in unsaved.text, unsaved.text
    unseen = run(runtime, paint.format(seed=4))
    assert not unseen.changed and "Scene changes: none" in unseen.text and "text blocks" in unseen.text \
        and "Not compared: the pixels of Chest_Atlas" in unseen.text, unseen.text
    failed = run(runtime, "import bpy\nt = bpy.data.texts['ArcaneRecipe']\nt.from_string(t.as_string() + 'BRASS = 2\\n')\n"
                          "raise ValueError('later line')")
    assert not failed.ok and failed.changed and "ValueError: later line" in failed.text, failed.text
    assert "made these changes before the error; they stay" in failed.text, failed.text
    assert "~ text ArcaneRecipe: edited (2 -> 3 lines)" in failed.text, failed.text
    theirs = run(runtime, "import bpy\nbpy.data.texts['UserNotes'].from_string('mine now\\n')")
    assert theirs.rolled_back and theirs.rolled_back["keys"] == ["TEXT:UserNotes"], (theirs.rolled_back, theirs.text)
    kept = run(runtime, "import bpy\nprint('NOTES', repr(bpy.data.texts['UserNotes'].as_string()))")
    assert "NOTES \"the user's notes\\n\"" in kept.text, kept.text
    texture = runtime.run_tool("inspect_texture", json.dumps({"target": "Chest_Atlas"}))
    finished = child.call("finish")
    support = {e["key"]: e for e in finished["manifest"]["support"]}
    assert set(support) == {"TEXT:ArcaneRecipe", "IMAGE:Chest_Atlas"}, support
    assert support["IMAGE:Chest_Atlas"]["state"] == "new" and support["IMAGE:Chest_Atlas"]["about"] == "128x128 packed"
    assert support["TEXT:ArcaneRecipe"]["about"] == "3 lines", support
    child.exit()
    note(f"WORKER tracking: ok (text and paint changes listed, an unsaved repaint named as not compared; "
         f"inspect_texture said {texture.text.split('hash ')[-1][:12] if 'hash ' in texture.text else '?'}, "
         f"the manifest {support['IMAGE:Chest_Atlas']['hash']} after packing)")


def main() -> int:
    job, kit = work / "job1", work / "kit"
    make_kit(kit)
    spawn = living_room(job)
    cases = [("round trip", lambda: round_trip(job, spawn)), ("looks", lambda: looks(job, spawn)),
             ("craft look", lambda: craft_look(job, spawn)),
             ("rollback", lambda: rollback(job, spawn)), ("timeout", lambda: timeout_and_crash(job, spawn)),
             ("broken mesh", lambda: broken_mesh(job, spawn)), ("crash kit", lambda: crash_kit(job, spawn)),
             ("stop", lambda: stop(job, spawn, 3 if MEASURE else 2)),
             ("killed parent", lambda: killed_parent(job, spawn)), ("reap", lambda: reap(job)),
             ("keys", lambda: keys(job, spawn)), ("import split", lambda: asset_split(job, spawn, kit)),
             ("unlit cell", lambda: unlit_cell(work / "job-dark")), ("bloom", lambda: bloom_look(work / "job-glow")),
             ("texture fidelity", lambda: texture_fidelity(job, spawn)),
             ("reference", lambda: reference_tile(job, spawn)), ("tracking", lambda: tracking(work / "job-tracking"))]
    if MEASURE:
        cases += [(f"rss {t}", lambda t=t: three_at_once(t, work / f"job-{t}")) for t in ("shared_support", "perfume_ad")]
    only = [a for a in sys.argv[sys.argv.index("--") + 1:] if not a.startswith("--")] if "--" in sys.argv else []
    for name, case in cases:
        if only and not any(o in name for o in only):
            continue
        try:
            case()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            failures.append(name)
            note(f"WORKER {name}: FAIL")
        finally:
            worker_host.shutdown_all()
    for line in measured:
        print(f"  {line}")
    print("WORKER FAIL: " + ", ".join(failures) if failures else "WORKER OK", flush=True)
    return 1 if failures else 0


code = main()
if not KEEP:
    import shutil
    shutil.rmtree(work, ignore_errors=True)
else:
    print(f"kept {work}")
sys.stdout.flush()
sys.exit(code)
