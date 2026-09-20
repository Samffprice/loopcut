"""Exercise the real tools inside Blender:  Blender --factory-startup --python harness/tools_check.py"""
import json
import os
import shutil
import sys
import traceback
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
import checkout  # noqa: E402
checkout.use()
# Harness runs must not write conversations or checkpoints into the user's real Loopcut data.
import os as _os
import tempfile as _tempfile
_os.environ.setdefault("LOOPCUT_DATA_DIR", _tempfile.mkdtemp(prefix="loopcut-harness-"))

from loopcut import tools  # noqa: E402


def check():
    code = 1
    try:
        info = json.loads(tools.execute("get_scene_info", "").text)
        assert {o["name"] for o in info["objects"]} == {"Cube", "Camera", "Light"}, info

        ok = tools.execute("run_python", json.dumps({
            "summary": "Add sphere",
            "code": "bpy.ops.mesh.primitive_uv_sphere_add(location=(3, 0, 0))\nprint(bpy.context.object.name)"}))
        assert ok.ok and ok.text.startswith("Sphere\n\nScene changes"), ok
        # The model is told what its code really did, in world space.
        assert "+ Sphere (mesh) at [2.0, -1.0, -1.0]..[4.0, 1.0, 1.0]" in ok.text, ok.text
        assert ok.scene_before["object_count"] == 3 and ok.scene_after["object_count"] == 4
        quiet = tools.execute("run_python", json.dumps({"summary": "Nothing", "code": "x = 1"}))
        assert "Scene changes: none" in quiet.text, quiet.text
        # A step can look at its own result, so the model does not need a second request for it.
        looked = tools.execute("run_python", json.dumps({"summary": "Look", "code": "x = 1", "capture": "front"}))
        assert looked.ok and looked.image_path and looked.image_path.stat().st_size > 5_000, looked
        assert "Scene changes: none" in looked.text and "Image attached: front view" in looked.text, looked.text
        from loopcut import scene_context
        block = scene_context.for_message("hi")
        assert "other objects: " in block and "Camera (CAMERA)" in block and "Light (LIGHT)" in block, block
        assert "Sphere" in bpy.data.objects

        # After a default transform_apply the origin reads 0,0,0 but the geometry has not moved.
        tools.execute("run_python", json.dumps({"summary": "Apply", "code": "bpy.ops.object.transform_apply()"}))
        sphere = next(o for o in json.loads(tools.execute("get_scene_info", "").text)["objects"]
                      if o["name"] == "Sphere")
        assert sphere["location"] == [0.0, 0.0, 0.0], sphere
        assert sphere["bounds"] == {"min": [2.0, -1.0, -1.0], "max": [4.0, 1.0, 1.0]}, sphere

        bad = tools.execute("run_python", json.dumps({"summary": "Oops", "code": "1/0"}))
        assert not bad.ok and "ZeroDivisionError" in bad.text, bad

        # An endless loop must come back as an error, not freeze Blender. The except is the
        # model's own: it must not be able to swallow the stop.
        os.environ["LOOPCUT_RUN_TIMEOUT"] = "1"
        stuck = tools.execute("run_python", json.dumps({"summary": "Loop", "code": (
            "bpy.data.objects['Cube'].location.x = 7\nwhile True:\n    try:\n        pass\n"
            "    except Exception:\n        pass")}))
        del os.environ["LOOPCUT_RUN_TIMEOUT"]
        assert not stuck.ok and "more than 1 s" in stuck.text, stuck.text
        assert "~ Cube: location" in stuck.text, "changes made before the stop are still reported"
        assert sys.gettrace() is None, "the deadline trace must not outlive the call"

        details = json.loads(tools.execute("get_object_info", json.dumps({"names": ["Cube"]})).text)[0]
        assert details["materials"][0]["nodes"] and details["materials"][0]["links"], details
        assert details["mesh"]["faces"] == 6, details
        assert "Similar names: Cube" in tools.execute("get_object_info", json.dumps({"names": ["cub"]})).text

        api = tools.execute("inspect_api", json.dumps({"path": "BevelModifier"}))
        assert api.ok and "segments: int" in api.text, api.text
        assert not tools.execute("inspect_api", json.dumps({"path": "bpy.types.Nope"})).ok

        # A big scene lists names and asks for a narrower question instead of truncating JSON.
        tools.execute("run_python", json.dumps({"summary": "Many", "code": (
            "for i in range(60):\n    o = bpy.data.objects.new(f'Marker{i:02}', None)\n"
            "    bpy.context.scene.collection.objects.link(o)")}))
        big = json.loads(tools.execute("get_scene_info", "").text)
        assert "objects by type: EMPTY 60" in scene_context.for_message("hi"), scene_context.for_message("hi")
        assert "objects" not in big and "Marker59 (EMPTY)" in big["objects_by_collection"]["Scene Collection"], big
        assert [o["name"] for o in big["selected_objects"]] == ["Sphere"], big["selected_objects"]
        lights = json.loads(tools.execute("get_scene_info", json.dumps({"type": "light"})).text)
        assert [o["name"] for o in lights["objects"]] == ["Light"], lights

        assert not tools.execute("run_python", "{not json").ok
        assert not tools.execute("nope", "{}").ok

        space = next(a for a in bpy.context.window_manager.windows[0].screen.areas
                     if a.type == "VIEW_3D").spaces.active
        before = (space.region_3d.view_distance, tuple(space.region_3d.view_rotation), space.shading.type)
        shot = tools.execute("capture_viewport", json.dumps({"focus": ["Cube", "Sphere"]}))
        assert shot.ok and shot.image_path.stat().st_size > 10_000, shot
        after = (space.region_3d.view_distance, tuple(space.region_3d.view_rotation), space.shading.type)
        assert before == after, f"user's view must be restored: {before} -> {after}"
        assert not tools.execute("capture_viewport", json.dumps({"focus": ["Nope"]})).ok
        assert "Nearest to the viewpoint first: " in shot.text, shot.text
        out = checkout.OUT
        tools.execute("run_python", json.dumps({"summary": "Red cube in frame", "code": (
            "cube = bpy.data.objects['Cube']\ncube.location.x = 0\n"
            "cube.data.materials[0].node_tree.nodes['Principled BSDF'].inputs['Base Color'].default_value = (1, 0, 0, 1)")}))
        for name, arguments in (("capture_camera", {"angle": "camera"}),
                                ("capture_distinct", {"style": "distinct", "focus": ["Cube", "Sphere"]})):
            extra = tools.execute("capture_viewport", json.dumps(arguments))
            assert extra.ok and extra.image_path.stat().st_size > 5_000, extra
            shutil.copy(extra.image_path, out / f"{name}.png")
        after = (space.region_3d.view_distance, tuple(space.region_3d.view_rotation), space.shading.type)
        assert before == after, f"user's view must be restored after every style: {before} -> {after}"
        shutil.copy(shot.image_path, checkout.OUT / "capture_check.png")
        assert bpy.context.scene.render.resolution_x == 1920, "render settings must be restored"
        print(f"TOOLS OK: capture at {shot.image_path} ({shot.image_path.stat().st_size} bytes)")
        code = 0
    except Exception:
        traceback.print_exc()
        print("TOOLS FAILED", file=sys.stderr)
    sys.stdout.flush()
    os._exit(code)


bpy.context.preferences.view.show_splash = False
bpy.app.timers.register(check, first_interval=1.0)
