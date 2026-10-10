"""The cost guard against real Blender: heavy modifiers never get evaluated, and heavy renders are refused.

    python3 loopcut/harness/guard_check.py            # light: tiny meshes, limits lowered to fire (~15 s)
    python3 loopcut/harness/guard_check.py --heavy    # real sizes and limits: 33M-face subdivisions

Run with plain python3: it is a watchdog that starts headless Blender on itself and kills it if it
runs past --seconds or grows past --rss-mb, since a guard that misses freezes Blender in one native
call no timeout inside it can stop (macOS has no `timeout` binary). LOOPCUT_BLENDER picks the
Blender, default tools/Blender.app next to the repository. The child's output goes to --log, by
default ../out/guard_check/<mode>.log; GUARD lines are echoed.

Cases, each through the real run_python (the 3D-view context override aside: headless has no windows):
- ops: subdivision level 6 on a dense mesh, then bpy.ops.object.shade_smooth(), which evaluates first;
- snapshot: subdivision level 6 left for run_python's snapshot to evaluate;
- array: a 1,000-count array;
- render: render levels 6 (viewport 0), then see_render, a render job, inspect_scene and
  bpy.ops.object.bake in the model's code, all refused with render settings untouched;
- render in code: bpy.ops.render.render and render.play_rendered_anim in the model's code, refused
  however light the scene, before they run, naming see_render and render_job.
- live file (C28, live_file.py): the model's save_as_mainfile and save_mainfile into its workspace write
  copies and leave bpy.data.filepath on the user's file; open_mainfile is refused; the user's own save
  afterwards writes the user's file.
Asserted each time: the right modifiers have viewport display off, the result names them, the
modifier settings themselves are unchanged. --heavy is risky on an 8 GB machine if the guard is broken:
the watchdog is what stands between a miss and swapping, so close other heavy work first.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


# ------------------------------------------------------------------ the watchdog (plain python3)

def _rss_mb(pid: int) -> float:
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return int(out) / 1024 if out.isdigit() else 0.0


def watchdog(argv: list[str]) -> int:
    import argparse
    sys.path.insert(0, str(HERE))
    import checkout
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--heavy", action="store_true", help="real mesh sizes and default limits")
    parser.add_argument("--seconds", type=float, default=None, help="kill Blender after this long")
    parser.add_argument("--rss-mb", type=float, default=None, help="kill Blender above this resident memory")
    parser.add_argument("--log", default=None, help="the child's output (default ../out/guard_check/<mode>.log)")
    args = parser.parse_args(argv)
    seconds = args.seconds or (240 if args.heavy else 120)
    rss_limit = args.rss_mb or (3000 if args.heavy else 1500)
    blender = os.environ.get("LOOPCUT_BLENDER") or str(checkout.WORKSPACE / "tools/Blender.app/Contents/MacOS/Blender")
    if not Path(blender).is_file():
        sys.exit(f"No Blender at {blender}; set LOOPCUT_BLENDER.")
    log_path = Path(args.log) if args.log else checkout.OUT / "guard_check" / f"{'heavy' if args.heavy else 'light'}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = [blender, "-b", "--factory-startup", "--python-exit-code", "1", "--python", str(Path(__file__).resolve()),
               "--", "--child"] + (["--heavy"] if args.heavy else [])
    started, peak, verdict = time.monotonic(), 0.0, ""
    with open(log_path, "w") as log:
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        while child.poll() is None:
            peak = max(peak, _rss_mb(child.pid))
            elapsed = time.monotonic() - started
            if elapsed > seconds or peak > rss_limit:
                child.kill()
                child.wait()
                verdict = (f"GUARD FAIL: killed Blender at {elapsed:.0f} s, {peak:.0f} MB resident "
                           f"(limits {seconds:.0f} s, {rss_limit:.0f} MB): something evaluated what the guard should have stopped")
                break
            time.sleep(0.2)
    for text in log_path.read_text(errors="replace").splitlines():
        if text.startswith("GUARD") or "Error" in text or "Traceback" in text:
            print(text)
    print(verdict or f"GUARD exit {child.returncode} after {time.monotonic() - started:.0f} s, peak {peak:.0f} MB; log {log_path}")
    return 1 if verdict or child.returncode else 0


# ------------------------------------------------------------------ inside Blender

def child(heavy: bool) -> None:
    import contextlib
    import tempfile

    import bpy
    sys.path.insert(0, str(HERE))
    import checkout
    checkout.use()
    from loopcut import cost_guard, files, inspection, job_tools, scene_diff, tools

    work = Path(tempfile.mkdtemp(prefix="loopcut-guard-check-"))
    os.environ["LOOPCUT_DATA_DIR"] = str(work / "data")
    tools._editor_context = lambda editor: contextlib.nullcontext()  # Headless: no windows to borrow.
    if not heavy:  # Same code paths, limits low enough for tiny meshes to cross them.
        cost_guard.MAX_OBJECT_FACES, cost_guard.MAX_SCENE_FACES = 1_000, 3_000
        cost_guard.MAX_RENDER_OBJECT_FACES, cost_guard.MAX_RENDER_SCENE_FACES = 2_000, 6_000
    # Dense: 8192 faces (heavy) or 128. Subdivision 6 of the dense one is 33.5M faces if it ever evaluates.
    segments, rings = (128, 64) if heavy else (16, 8)
    level, render_level = (6, 6) if heavy else (2, 3)
    print(f"GUARD mode {'heavy' if heavy else 'light'}: dense mesh {segments * rings} faces, level {level}, "
          f"limits {cost_guard.MAX_OBJECT_FACES}/{cost_guard.MAX_SCENE_FACES} viewport, "
          f"{cost_guard.MAX_RENDER_OBJECT_FACES}/{cost_guard.MAX_RENDER_SCENE_FACES} render")

    def fresh() -> None:
        for obj in list(bpy.data.objects):
            bpy.data.objects.remove(obj)
        for mesh in list(bpy.data.meshes):
            bpy.data.meshes.remove(mesh)
        bpy.ops.mesh.primitive_uv_sphere_add(segments=segments, ring_count=rings)
        bpy.context.active_object.name = "Dense"
        camera = bpy.data.objects.new("Camera", bpy.data.cameras.new("Camera"))
        bpy.context.scene.collection.objects.link(camera)
        camera.location = (0, -8, 0)
        camera.rotation_euler = (1.5708, 0, 0)
        bpy.context.scene.camera = camera

    def run(code: str, label: str):
        started = time.monotonic()
        result = tools.run_python(code, summary=label)
        print(f"GUARD {label}: {time.monotonic() - started:.2f} s, ok={result.ok}")
        return result

    def base_faces() -> int:
        dense = bpy.data.objects["Dense"]
        return len(dense.evaluated_get(bpy.context.evaluated_depsgraph_get()).data.polygons)

    def expect_switched(result, modifier: str, settings: dict) -> None:
        mod = bpy.data.objects["Dense"].modifiers[modifier]
        assert not mod.show_viewport, f"{modifier} still shown: {result.text}"
        assert mod.show_render, "render settings are the user's"
        for key, value in settings.items():
            assert getattr(mod, key) == value, f"{modifier}.{key} changed to {getattr(mod, key)}"
        assert f"{modifier} on Dense, est. " in result.text and "viewport display off" in result.text, result.text
        assert ["Dense", modifier] in [m for n in result.guard_notes for m in n["modifiers"]], result.guard_notes
        assert base_faces() <= cost_guard.MAX_OBJECT_FACES, "evaluated without it"  # Safe: it is off now.
        print(f"GUARD   {next(n['text'] for n in result.guard_notes if n['modifiers'])}")

    # ops: the operator's own view-layer update would evaluate level 6 before shade_smooth runs.
    fresh()
    result = run(f"""
obj = bpy.data.objects["Dense"]
mod = obj.modifiers.new("Subsurf", "SUBSURF")
mod.levels = mod.render_levels = {level}
bpy.context.view_layer.objects.active = obj
obj.select_set(True)
bpy.ops.object.shade_smooth()
print("shown after the operator:", mod.show_viewport)
""", "ops")
    assert result.ok, result.text
    assert "shown after the operator: False" in result.text, "the operator, not the snapshot, must catch it"
    expect_switched(result, "Subsurf", {"levels": level, "render_levels": level})

    # snapshot: no operator; run_python's snapshot is the first thing that would evaluate it.
    fresh()
    result = run(f"""
mod = bpy.data.objects["Dense"].modifiers.new("Subsurf", "SUBSURF")
mod.levels = mod.render_levels = {level}
print("shown at the end of the code:", mod.show_viewport)
""", "snapshot")
    assert result.ok and "shown at the end of the code: True" in result.text, result.text
    expect_switched(result, "Subsurf", {"levels": level, "render_levels": level})
    assert "Subsurf" in result.text.split("Scene changes", 1)[-1], "the diff shows the switch too"

    # array: 1,000 copies. A Remesh after it cannot be estimated: reported, left on.
    fresh()
    result = run("""
obj = bpy.data.objects["Dense"]
array = obj.modifiers.new("Array", "ARRAY")
array.count = 1000
remesh = obj.modifiers.new("Remesh", "REMESH")
remesh.voxel_size = 0.5  # Coarse: about 50 faces once the array is off.
bpy.ops.object.select_all(action="DESELECT")
""", "array")
    assert result.ok, result.text
    expect_switched(result, "Array", {"count": 1000})
    assert "est. at least" in result.text and "could not estimate, left on: Remesh on Dense" in result.text, result.text
    assert bpy.data.objects["Dense"].modifiers["Remesh"].show_viewport

    # render: viewport level 0 draws the base mesh; render level 6 must never reach a render.
    fresh()
    result = run(f"""
mod = bpy.data.objects["Dense"].modifiers.new("Subsurf", "SUBSURF")
mod.levels, mod.render_levels = 0, {render_level}
""", "render setup")
    assert result.ok and not result.guard_notes, result.text
    scene = bpy.context.scene
    settings = (scene_diff.rna_values(scene.render), scene.render.engine, scene.frame_current)
    refusals = []
    for label, call in (("see_render", lambda: files.see_render(render=True)),
                        ("render_job start", lambda: job_tools.render_job(  # Through the tool's dispatcher.
                            action="start", output_dir=str(work / "outputs"), frames=[1], budget_seconds=30)),
                        ("inspect_scene", lambda: inspection.inspect_scene("geometry"))):
        try:
            call()
        except tools.ToolError as ex:
            refusals.append(label)
            assert "Render refused by the cost guard" in str(ex) and "Subsurf on Dense renders at est." in str(ex), str(ex)
            assert "lower its render_levels" in str(ex), str(ex)
        else:
            raise AssertionError(f"{label} rendered")
    result = run("bpy.ops.object.bake(type='COMBINED')", "bake in code")
    assert not result.ok and "Subsurf on Dense renders at est." in result.text, result.text
    assert any(not n["modifiers"] and "Render refused" in n["text"] for n in result.guard_notes), result.guard_notes
    mod = bpy.data.objects["Dense"].modifiers["Subsurf"]
    assert (mod.levels, mod.show_viewport, mod.show_render) == (0, True, True), "nothing changed for a refusal"
    assert (scene_diff.rna_values(scene.render), scene.render.engine, scene.frame_current) == settings
    assert not list((work / "data").rglob("scene.blend")), "no job was saved"
    print(f"GUARD   refused: {', '.join(refusals)}, bpy.ops.object.bake")
    mod.render_levels = 1
    assert cost_guard.render_refusal() == "", "a light render is not refused"

    # render in code: a final render in the model's code is refused however light, before it runs.
    for op in ("render", "play_rendered_anim"):
        result = run(f"bpy.ops.render.{op}()\nprint('ran past it')", f"render.{op} in code")
        assert not result.ok and f"Render refused by the cost guard: render.{op} in run_python" in result.text, result.text
        assert "see_render" in result.text and "render_job" in result.text, result.text
        assert "ran past it" not in result.text, result.text
        assert any(n["text"] == cost_guard.in_process_refusal(f"render.{op}") for n in result.guard_notes)
    print("GUARD   refused however light: render.render, render.play_rendered_anim")

    # live file (C28): the model's saves into its workspace are copies; the user's file stays open and theirs.
    user_file, ws = work / "Projects" / "mug.blend", work / "data" / "workspaces" / "c28"
    user_file.parent.mkdir(parents=True)
    ws.mkdir(parents=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(user_file))  # The user's own Save As, outside run_python.
    assert bpy.data.filepath == str(user_file), bpy.data.filepath
    for name, code in (("coffee-table.blend", "bpy.ops.wm.save_as_mainfile(filepath={!r})"),
                       ("preview.blend", "bpy.ops.wm.save_mainfile(filepath={!r}, compress=True)")):
        result = run(code.format(str(ws / name)), f"save {name} into the workspace")
        assert result.ok and (ws / name).is_file(), result.text
        assert bpy.data.filepath == str(user_file), f"the open file moved to {bpy.data.filepath}"
        assert f"saved {name} as a copy; the user's open file is still {user_file}" in result.text, result.text
    result = run(f"bpy.ops.wm.open_mainfile(filepath={str(ws / 'coffee-table.blend')!r})", "open in the user's place")
    assert not result.ok and "Refused: wm.open_mainfile in run_python" in result.text, result.text
    assert bpy.data.filepath == str(user_file) and "Dense" in bpy.data.objects, bpy.data.filepath
    before = user_file.stat().st_mtime_ns
    time.sleep(0.05)
    bpy.ops.wm.save_mainfile()  # The user's Cmd+S afterwards.
    assert user_file.stat().st_mtime_ns > before and bpy.data.filepath == str(user_file), "Cmd+S missed the user's file"
    print(f"GUARD   live file: still {user_file.name} after two saves into the workspace; open refused; Cmd+S wrote it")

    # Overhead: every operator reads every object with modifiers first.
    fresh()
    for i in range(200):
        copy = bpy.data.objects.new(f"Copy{i}", bpy.data.objects["Dense"].data)
        bpy.context.scene.collection.objects.link(copy)
        copy.modifiers.new("Subsurf", "SUBSURF").levels = 0
    code = "for _ in range(200):\n    bpy.ops.object.select_all(action='DESELECT')"
    guarded = run(code, "200 operators, 200 modified objects")
    watching, cost_guard.watching_ops = cost_guard.watching_ops, contextlib.nullcontext
    plain = run(code, "the same without the guard")
    cost_guard.watching_ops = watching
    assert guarded.ok and plain.ok and not guarded.guard_notes, guarded.text
    print("GUARD OK")


if __name__ == "__main__":
    if "--child" in sys.argv:
        child("--heavy" in sys.argv)
    else:
        sys.exit(watchdog(sys.argv[1:]))
