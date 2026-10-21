"""Drivers in a cloud render's bundle: packed as they are when the render service can run them, refused otherwise.

    <blender> -b --factory-startup --python loopcut/harness/driver_bundle_check.py -- <tmpdir> [<worker folder>]

The render service runs no Python, so it refuses a driver that needs it; its rule, shared with the packer's
bundles.driver_acceptable, accepts a muted F-curve, a non-scripted driver and a simple expression. Rigs carry many
scripted drivers: BlenderKit's free rigged characters have 100 each, "var" and "1-var" IK/FK switches, all simple.
This scene has the same kinds: an object constraint's influence ("var") and an array item ("1-var") driven by an
animated control property, a shape key ("var * 0.5", a Key data-block) and a material node input ("frame / 100",
an embedded node tree), plus a muted Python driver. This checkout's packer (bundles.collect) must refuse none of
them and leave them as they are; the bundle, opened without scripts as the worker opens it, must give every driven
property the same value at every job frame. A scene with an active Python driver, and one linking a library whose
object has one, must be refused with that driver named; the library's simple driver passes. A muted Python driver
whose mute an action keyframes off at frame 5 (it would run Python from then on) must be refused as that action's
keyframes. With the render worker's folder (loopcut-web workers/render, with both driver rules) its own scene checks
run on the first bundle too and refuse the animated one the same way. Only staging under tmpdir is written; nothing
is uploaded.
"""

import os
import sys
import tempfile
import traceback
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
import checkout  # noqa: E402

FRAMES = (3, 6, 9)


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print("PASS " + message, flush=True)


def driver(holder, path, expression, index=-1, control=None):
    curve = holder.driver_add(path, index) if index >= 0 else holder.driver_add(path)
    curve.driver.type = "SCRIPTED"
    curve.driver.expression = expression
    if control is not None:
        var = curve.driver.variables.new()
        var.name, var.type = "var", "SINGLE_PROP"
        var.targets[0].id = control
        var.targets[0].data_path = '["ik_fk"]'
    return curve


def driven_scene():
    """Give the factory scene's Cube the drivers; return a reader of every driven property's evaluated value."""
    scene = bpy.context.scene
    cube = bpy.data.objects["Cube"]
    control = bpy.data.objects.new("Ctrl", None)
    scene.collection.objects.link(control)
    control["ik_fk"] = 0.0
    control.keyframe_insert('["ik_fk"]', frame=1)
    control["ik_fk"] = 1.0
    control.keyframe_insert('["ik_fk"]', frame=9)
    constraint = cube.constraints.new("COPY_LOCATION")
    constraint.name, constraint.target = "FK_follow", control
    driver(cube, 'constraints["FK_follow"].influence', "var", control=control)
    driver(cube, "delta_scale", "1-var", index=1, control=control)
    cube.shape_key_add(name="Basis")
    cube.shape_key_add(name="Smile")
    driver(cube.data.shape_keys, 'key_blocks["Smile"].value', "var * 0.5", control=control)
    material = bpy.data.materials.new("Driven")
    material.use_nodes = True
    cube.data.materials.append(material)
    bsdf = next(node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED")
    roughness = next(n for n, socket in enumerate(bsdf.inputs) if socket.name == "Roughness")
    roughness_path = f'nodes["{bsdf.name}"].inputs[{roughness}].default_value'
    driver(material.node_tree, roughness_path, "frame / 100")
    muted = driver(cube, "location", "bpy.data.objects['Ctrl'].location.x + 5", index=2)
    muted.mute = True
    check(not muted.driver.is_simple_expression, "the muted driver needs Python")

    def readers(depsgraph):
        ob = bpy.data.objects["Cube"].evaluated_get(depsgraph)
        mat = bpy.data.materials["Driven"].evaluated_get(depsgraph)
        return {"influence": ob.constraints["FK_follow"].influence, "delta_scale[1]": ob.delta_scale[1],
                "smile": ob.data.shape_keys.key_blocks["Smile"].value,
                "roughness": mat.node_tree.path_resolve(roughness_path), "location[2]": ob.location[2]}
    return readers


def values_at_frames(readers):
    scene, values = bpy.context.scene, {}
    for frame in FRAMES:
        scene.frame_set(frame)
        values[frame] = readers(bpy.context.evaluated_depsgraph_get())
    scene.frame_set(1)
    return values


def scripted_drivers():
    """(data path, expression, muted) of every scripted driver, each ID once (bpy.data's own lists repeat them)."""
    found, seen = [], set()
    for block in bpy.data.all_ids:
        for holder in (block, getattr(block, "node_tree", None)):
            if holder is None or holder.as_pointer() in seen:
                continue
            seen.add(holder.as_pointer())
            animation = getattr(holder, "animation_data", None)
            found += [(curve.data_path, curve.driver.expression, curve.mute) for curve in
                      (animation.drivers if animation else ()) if curve.driver.type == "SCRIPTED"]
    return sorted(found)


def run(tmpdir, worker):
    tmpdir.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="loopcut-driver-bundle-", dir=tmpdir)).resolve()
    os.environ["LOOPCUT_DATA_DIR"] = str(root / "data")
    os.environ["LOOPCUT_CONFIG_DIR"] = str(root / "config")
    checkout.use()
    from loopcut import bundles

    readers = driven_scene()
    drivers = scripted_drivers()
    check(len(drivers) == 5, f"the scene has five scripted drivers ({len(drivers)})")
    expected = values_at_frames(readers)
    check(expected[3]["influence"] != expected[9]["influence"], "the control property moves between job frames")
    snapshot, stage = root / "driven.blend", root / "bundle"
    bpy.ops.wm.save_as_mainfile(filepath=str(snapshot), copy=True)
    info = bundles.collect(snapshot, stage)
    check(info["missing"] == [] and info["unsupported"] == [], f"nothing missing or unsupported: {info}")

    bpy.context.preferences.filepaths.use_scripts_auto_execute = False
    loaded = bpy.ops.wm.open_mainfile(filepath=str(stage / "scene.blend"), load_ui=False, use_scripts=False)
    check(loaded == {"FINISHED"}, "opened the collected scene.blend without scripts, as the worker does")
    check(scripted_drivers() == drivers, "the bundle keeps every driver as it was, the muted one included")
    packed = values_at_frames(readers)
    for frame, values in expected.items():
        for key, value in values.items():
            check(abs(packed[frame][key] - value) < 1e-5, f"frame {frame} {key} is {value:.4f} as the driver gave")
    if worker is not None:
        sys.path.insert(0, str(worker))
        import render_job
        render_job.secure_scene(bpy)
        render_job.validate_asset_paths(bpy, stage.resolve())
        check(True, "the render worker's scene checks accept the bundle")

    bpy.ops.wm.read_homefile(use_empty=False)
    driver(bpy.data.objects["Cube"], "location", "bpy.data.objects['Camera'].location.x", index=2)
    python, python_stage = root / "python.blend", root / "python-bundle"
    bpy.ops.wm.save_as_mainfile(filepath=str(python), copy=True)
    rows = bundles.collect(python, python_stage)["unsupported"]
    check(rows == [{"type": "driver", "name": 'Object "Cube"', "path": "location[2]", "reason": bundles.PYTHON_DRIVER}],
          f"an active Python driver is refused by name: {rows}")

    # Measured on the worker (Blender 5.2.2, 2026-10-21): this action unmutes the driver at frame 5, after the
    # load-time check passed it, and the driver runs Python from then on.
    bpy.ops.wm.read_homefile(use_empty=False)
    cube = bpy.data.objects["Cube"]
    driver(cube, "location", "bpy.data.objects['Camera'].location.x", index=2).mute = True
    action = bpy.data.actions.new("Sneaky")
    cube.animation_data.action = action
    unmute = action.fcurve_ensure_for_datablock(cube, "animation_data.drivers[0].mute", index=0)
    for frame, value in ((1, 1.0), (5, 0.0)):
        unmute.keyframe_points.insert(frame, value).interpolation = "CONSTANT"
    animated, animated_stage = root / "animated.blend", root / "animated-bundle"
    bpy.ops.wm.save_as_mainfile(filepath=str(animated), copy=True)
    rows = bundles.collect(animated, animated_stage)["unsupported"]
    check(rows == [{"type": "keyframes", "name": 'Action "Sneaky"', "path": "animation_data.drivers[0].mute",
                    "reason": bundles.DRIVER_ANIMATED}],
          f"keyframes that unmute a Python driver are refused by name: {rows}")
    if worker is not None:
        bpy.context.preferences.filepaths.use_scripts_auto_execute = False
        bpy.ops.wm.open_mainfile(filepath=str(animated_stage / "scene.blend"), load_ui=False, use_scripts=False)
        try:
            render_job.secure_scene(bpy)
            refused = None
        except render_job.SceneRejected as ex:
            refused = (ex.reason, ex.items)
        check(refused == ("driver_animated", ["Action 'Sneaky' animation_data.drivers[0].mute[0]"]),
              f"the render worker refuses the same scene for the same keyframes: {refused}")

    bpy.ops.wm.read_homefile(use_empty=True)
    for name, expression in (("Simple", "frame / 10"), ("Scripted", "bpy.context.scene.frame_current / 10")):
        holder = bpy.data.objects.new(name, None)
        bpy.context.scene.collection.objects.link(holder)
        driver(holder, "location", expression, index=0)
    library = root / "library.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(library), copy=True)
    bpy.ops.wm.read_homefile(use_empty=True)
    with bpy.data.libraries.load(str(library), link=True) as (source, target):
        target.objects = ["Simple", "Scripted"]
    for linked in target.objects:
        bpy.context.scene.collection.objects.link(linked)
    check(all(ob.library is not None for ob in bpy.data.objects), "both objects are linked")
    linking, linking_stage = root / "linking.blend", root / "linking-bundle"
    bpy.ops.wm.save_as_mainfile(filepath=str(linking), copy=True)
    rows = bundles.collect(linking, linking_stage)["unsupported"]
    check(rows == [{"type": "driver", "name": 'Object "Scripted"', "path": "location[0]",
                    "reason": bundles.LINKED_PYTHON_DRIVER}],
          f"linked data follows the same rule: only its Python driver is refused: {rows}")
    print("PASS driver bundle check; artifacts: " + str(root), flush=True)


if __name__ == "__main__":
    try:
        arguments = sys.argv[sys.argv.index("--") + 1:]
        run(Path(arguments[0]), Path(arguments[1]).resolve() if len(arguments) > 1 else None)
    except Exception:
        traceback.print_exc()
        print("FAIL driver bundle check", flush=True)
        sys.stderr.flush()
        os._exit(1)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)
