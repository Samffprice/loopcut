"""Exercise the bundled System extensions in isolated Blender processes (run with plain python3).

    python3 loopcut/harness/bundled_addons_check.py

LOOPCUT_BLENDER selects Blender; default tools/Blender.app next to the checkout. --seconds
limits each process (default 240). No HOME override, user preferences, or source packages are
used for writes. Logs are kept in a printed temporary directory; only CHECK lines are echoed
on success and the log tail on failure. A desktop session is required for UniV and Node Arrange.
Node Arrange's batch operator also requires a node editor, so its lifecycle is tested headlessly
and its measured operator check runs separately with an interactive context override.
"""

import os
import math
import shutil
import signal
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
OPERATORS = {"looptools": "mesh.looptools_circle", "node_arrange": "node.na_arrange_selected",
             "bool_tool": "object.boolean_brush_difference", "mpfb": "mpfb.create_human",
             "univ": "uv.univ_home"}


def report(package, step, ok, detail=""):
    print(f"CHECK {package} {step}: {'ok' if ok else 'FAIL'} {detail}", flush=True)


def readonly(directory, enabled):
    for current, dirs, files in os.walk(directory):
        for name in files:
            (Path(current) / name).chmod(0o444 if enabled else 0o644)
        for name in dirs:
            (Path(current) / name).chmod(0o555 if enabled else 0o755)
    directory.chmod(0o555 if enabled else 0o755)


def watchdog(argv):
    import argparse
    import checkout
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seconds", type=float, default=240, help="time limit per Blender process")
    args = parser.parse_args(argv)
    if not math.isfinite(args.seconds) or args.seconds <= 0:
        parser.error("--seconds must be finite and positive")
    blender = os.environ.get("LOOPCUT_BLENDER") or str(checkout.WORKSPACE / "tools/Blender.app/Contents/MacOS/Blender")
    if not Path(blender).is_file():
        parser.error(f"No Blender at {blender}; set LOOPCUT_BLENDER")
    work = Path(tempfile.mkdtemp(prefix="loopcut-bundled-check-"))
    print(f"Blender logs: {work}", flush=True)
    extensions = work / "extensions"
    shutil.copytree(checkout.REPO / "release/extensions", extensions, ignore=shutil.ignore_patterns("__pycache__"))
    readonly(extensions, True)
    jobs = [(p, False) for p in ("looptools", "node_arrange", "bool_tool", "mpfb")]
    jobs += [("node_arrange", True), ("univ", True)]
    failed = False
    try:
        for package, interactive in jobs:
            label = package + ("-interactive" if interactive else "-headless")
            user = work / label
            user.mkdir()
            env = {k: v for k, v in os.environ.items() if not k.startswith("BLENDER_USER_")}
            env.update(BLENDER_SYSTEM_EXTENSIONS=str(extensions), BLENDER_USER_RESOURCES=str(user),
                       LOOPCUT_CONFIG_DIR=str(user / "config/loopcut"), LOOPCUT_DATA_DIR=str(user / "data"),
                       PYTHONUNBUFFERED="1", LOOPCUT_DEV="0")
            command = [blender] + ([] if interactive else ["-b"])
            command += ["--factory-startup", "--python-exit-code", "1", "--python", str(Path(__file__).resolve()),
                        "--", "--child", package]
            log_path = work / (label + ".log")
            timed_out = False
            with log_path.open("w") as log:
                child = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL, stdout=log,
                                         stderr=subprocess.STDOUT, start_new_session=os.name != "nt")
                try:
                    child.wait(timeout=args.seconds)
                except (subprocess.TimeoutExpired, KeyboardInterrupt) as ex:
                    if os.name == "nt":
                        child.kill()
                    else:
                        os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
                    if isinstance(ex, KeyboardInterrupt):
                        raise
                    timed_out = True
            lines = log_path.read_text(errors="replace").splitlines()
            for line in lines:
                if line.startswith("CHECK "):
                    print(line)
            complete = f"CHECK {package} complete: ok" in lines
            bad = (timed_out or child.returncode != 0 or not complete
                   or any(": FAIL" in line or "Traceback (most recent call last)" in line for line in lines))
            if bad:
                failed = True
                report(package, "process", False,
                       f"{label}: exit {child.returncode}, timeout={timed_out}; log {log_path}")
                print("\n".join(lines[-60:]))
        # Prove that execution did not mutate the copied System repository.
        sys.path.insert(0, str(checkout.REPO / "loopcut/scripts"))
        import bundled_extensions
        for pin in bundled_extensions.manifest(extensions):
            bundled_extensions.check_tree(extensions / "system" / pin["id"], pin)
        assert not list(extensions.rglob("__pycache__")), "bytecode was written into the read-only copy"
        report("all", "read-only", True, "copied sources unchanged; no bytecode caches")
    except (OSError, ValueError, AssertionError) as ex:
        failed = True
        report("all", "read-only", False, str(ex))
    finally:
        readonly(extensions, False)
        shutil.rmtree(extensions)  # Keep each log and isolated user folder, not the large source copy.
    return int(failed)


def fresh():
    import bpy
    if bpy.context.object and bpy.context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)


def finished(operator, **kwargs):
    assert operator.poll(), f"{operator.idname()} poll failed"
    result = operator(**kwargs)
    assert result == {'FINISHED'}, f"{operator.idname()}: {result}"


def operator_names(package):
    import bpy
    group = OPERATORS[package].split(".")[0]
    prefix = {"looptools": "looptools_", "node_arrange": "na_", "bool_tool": "boolean_",
              "mpfb": "", "univ": "univ_"}[package]
    return {name for name in dir(getattr(bpy.ops, group)) if name.startswith(prefix)}


def exercise(package):
    """Yield only outside context overrides so the UI can draw the node tree before measuring it."""
    import bpy
    fresh()
    if package == "looptools":
        import bmesh
        mesh = bpy.data.meshes.new("UnevenRing")
        mesh.from_pydata([(0, 0, 0), (3, 0, 0), (2, 1, 0), (0, 2, 0)],
                         [(0, 1), (1, 2), (2, 3), (3, 0)], [])
        obj = bpy.data.objects.new("Ring", mesh)
        bpy.context.collection.objects.link(obj)
        bpy.context.view_layer.objects.active = obj
        obj.select_set(True)
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')
        before = [v.co.copy() for v in bmesh.from_edit_mesh(mesh).verts]
        finished(bpy.ops.mesh.looptools_circle)
        after = [v.co.copy() for v in bmesh.from_edit_mesh(mesh).verts]
        assert any((a - b).length > 1e-5 for a, b in zip(after, before)), "vertex positions unchanged"
        bpy.ops.object.mode_set(mode='OBJECT')
        report(package, "operator", True, "looptools_circle moved selected vertices")
    elif package == "bool_tool":
        import bmesh

        def volume(obj):
            bpy.context.view_layer.update()
            evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
            bm = bmesh.new()
            try:
                bm.from_mesh(evaluated.data)
                return abs(bm.calc_volume())
            finally:
                bm.free()

        bpy.ops.mesh.primitive_cube_add()
        target = bpy.context.object
        before = volume(target)
        bpy.ops.mesh.primitive_cube_add(size=1.5, location=(0.7, 0.4, 0.3))
        target.select_set(True)
        bpy.context.view_layer.objects.active = target
        finished(bpy.ops.object.boolean_brush_difference)
        after = volume(target)
        assert 0 < after < before - 1e-4, f"evaluated volume did not decrease: {before} -> {after}"
        report(package, "operator", True, f"boolean_brush_difference: volume {before:.4f} -> {after:.4f}")
    elif package == "mpfb":
        from bl_ext.system.mpfb.services.humanservice import HumanService
        finished(bpy.ops.mpfb.create_human)
        body = bpy.context.object
        assert body.type == 'MESH' and len(body.data.vertices) > 1000, "human mesh missing"
        rig = HumanService.add_builtin_rig(body, "default")
        assert rig and rig.type == 'ARMATURE' and len(rig.data.bones) > 0, "built-in rig missing"
        assert any(m.type == 'ARMATURE' and m.object == rig for m in body.modifiers), "Armature modifier missing"
        report(package, "operator", True, f"mpfb.create_human: {len(body.data.vertices)} vertices, {len(rig.data.bones)} bones")
    elif package == "node_arrange":
        if bpy.app.background:
            assert not bpy.ops.node.na_batch_arrange.poll(), "unexpected background node editor"
            report(package, "context", True, "batch also requires a node editor; operator measured in interactive run")
            return
        bpy.ops.mesh.primitive_cube_add()
        material = bpy.data.materials.new("ArrangementCheck")
        material.use_nodes = True
        bpy.context.object.data.materials.append(material)
        tree = material.node_tree
        tree.nodes.clear()
        color = tree.nodes.new("ShaderNodeRGB")
        shader = tree.nodes.new("ShaderNodeBsdfPrincipled")
        output = tree.nodes.new("ShaderNodeOutputMaterial")
        tree.links.new(color.outputs[0], shader.inputs['Base Color'])
        tree.links.new(shader.outputs[0], output.inputs['Surface'])
        for node in tree.nodes:
            node.location = (0, 0)
            node.select = True
        window = bpy.context.window_manager.windows[0]
        area = next(a for a in window.screen.areas if a.type == 'VIEW_3D')
        previous = area.type
        try:
            area.type = 'NODE_EDITOR'
            area.ui_type = 'ShaderNodeTree'
            area.spaces.active.shader_type = 'OBJECT'
            area.spaces.active.pin = True
            area.spaces.active.path.start(tree)
            area.tag_redraw()
            yield 0.5  # Node dimensions are populated by drawing, not by creating a node.
            region = next(r for r in area.regions if r.type == 'WINDOW')
            with bpy.context.temp_override(window=window, area=area, region=region):
                assert all(n.dimensions.x > 0 for n in tree.nodes), "node editor has not drawn dimensions"
                before = [n.location.copy() for n in tree.nodes]
                finished(bpy.ops.node.na_arrange_selected)
                assert any((n.location - old).length > 1e-4 for n, old in zip(tree.nodes, before)), "nodes did not move"
            report(package, "operator", True, "na_arrange_selected moved nodes in an interactive node editor override")
        finally:
            area.type = previous
    elif package == "univ":
        bpy.ops.mesh.primitive_plane_add()
        mesh = bpy.context.object.data
        uv = mesh.uv_layers.active or mesh.uv_layers.new()
        for item, coordinate in zip(uv.data, ((2.1, 3.1), (2.9, 3.1), (2.9, 3.9), (2.1, 3.9))):
            item.uv = coordinate
        before = [item.uv.copy() for item in uv.data]
        window = bpy.context.window_manager.windows[0]
        area = next(a for a in window.screen.areas if a.type == 'VIEW_3D')
        previous = area.type
        try:
            area.type = 'IMAGE_EDITOR'
            area.ui_type = 'UV'
            region = next(r for r in area.regions if r.type == 'WINDOW')
            with bpy.context.temp_override(window=window, area=area, region=region):
                # Upstream UNIV_OT_Home handles selected object-mode meshes, with no modal event.
                finished(bpy.ops.uv.univ_home, to_cursor=False)
            after = [item.uv.copy() for item in mesh.uv_layers.active.data]
            assert any((a - b).length > 1 for a, b in zip(after, before)), "UVs did not move"
            assert all(0 <= component <= 1 for point in after for component in point), "UVs not moved to base tile"
            report(package, "operator", True, "uv.univ_home moved UVs into the base tile with a UV editor override")
        finally:
            area.type = previous


def checks(package):
    import addon_utils
    import bpy
    import checkout
    checkout.use()
    from loopcut import api_docs, bundled_addons, credentials
    module = f"bl_ext.system.{package}"
    repo = next(r for r in bpy.context.preferences.extensions.repos if r.source == 'SYSTEM')
    assert Path(repo.directory).resolve() == (Path(os.environ['BLENDER_SYSTEM_EXTENSIONS']) / 'system').resolve()
    assert not (Path(repo.directory).stat().st_mode & 0o222), "System directory is writable"
    assert module in {m.__name__ for m in addon_utils.modules()}, "not discovered in System repository"
    assert not addon_utils.check(module)[1] and module not in bpy.context.preferences.addons, "enabled by default"
    report(package, "present", True, "discovered, disabled, read-only System repository")
    record = credentials.folder() / bundled_addons.RECORD_FILE
    assert not record.exists(), "unexpected existing record in fresh config"
    dirty = bpy.context.preferences.is_dirty = False
    text = api_docs.inspect_api(path="bpy.ops." + OPERATORS[package])
    assert text.startswith("Turned on the bundled "), text
    assert addon_utils.check(module)[1] and module in bpy.context.preferences.addons, text
    assert bpy.context.preferences.is_dirty == dirty, "enable dirtied preferences"
    registered = operator_names(package)
    assert OPERATORS[package].split('.')[1] in registered, "representative operator not registered"
    if bpy.app.background:
        assert not record.exists(), "background wrote the record"
    else:
        assert module in bundled_addons._read_record(record), "interactive enable was not recorded"
    report(package, "enable", True, "inspect_api enabled it, dirty unchanged, journal policy respected")
    bpy.context.preferences.is_dirty = True
    again = api_docs.inspect_api(path=package)
    assert not again.startswith("Turned on"), "already-on add-on re-enabled"
    assert bpy.context.preferences.is_dirty, "already-on lookup cleared dirty preferences"
    if not bpy.app.background:
        yield 0.25  # UniV schedules its initialization after register() leaves restricted context.
    yield from exercise(package)
    addon_utils.disable(module, default_set=True, handle_error=bundled_addons._addon_error)
    assert not addon_utils.check(module)[1] and module not in bpy.context.preferences.addons
    assert not (registered & operator_names(package)), "operators still registered"
    report(package, "disable", True, "operators and prefs entry gone")
    if not bpy.app.background:
        api_docs.inspect_api(path=module)
        yield 0.25
        bundled_addons._session_enabled.clear()  # Simulate a new process seeing an incidentally saved entry.
        bpy.context.preferences.is_dirty = False
        bundled_addons.startup_cleanup()
        assert not addon_utils.check(module)[1] and module not in bpy.context.preferences.addons
        assert bpy.context.preferences.is_dirty, "repair not marked for auto-save"
        assert module in bundled_addons._read_record(record), "repair forgotten before a later startup"
        bundled_addons.startup_cleanup()  # Next startup sees it off.
        assert module not in bundled_addons._read_record(record)
        report(package, "startup-cleanup", True, "disabled, dirty, retained until a later startup found it off")
    else:
        bundled_addons.startup_cleanup()
        assert not record.exists(), "background cleanup wrote a record"
        report(package, "startup-cleanup", True, "background left journal untouched")
    print(f"CHECK {package} complete: ok", flush=True)


def child(package):
    import bpy
    iterator = checks(package)
    if bpy.app.background:
        try:
            for _ in iterator:
                raise AssertionError("headless exercise unexpectedly needed a UI tick")
        except Exception as ex:
            report(package, "run", False, str(ex))
            raise
        return

    def tick():
        if not bpy.context.window_manager.windows:
            return 0.1
        try:
            return next(iterator)
        except StopIteration:
            # --factory-startup is already isolated; do not stop at a save-confirmation dialog.
            bpy.ops.wm.quit_blender()
            return None
        except Exception as ex:
            report(package, "run", False, str(ex))
            traceback.print_exc()
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(1)  # Timer exceptions otherwise leave Blender open with a success exit code.

    bpy.app.timers.register(tick, first_interval=0.5)


if __name__ == "__main__":
    sys.path.insert(0, str(HERE))  # checkout.py, in Blender's Python as in plain python3.
    if "--child" in sys.argv:
        child(sys.argv[sys.argv.index("--child") + 1])
    else:
        sys.exit(watchdog(sys.argv[1:]))
