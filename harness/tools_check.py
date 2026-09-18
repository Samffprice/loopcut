"""Exercise the real tools inside Blender:  Blender --factory-startup --python harness/tools_check.py"""
import json
import sys
import traceback
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "extension"))
from loopcut import tools  # noqa: E402


def check():
    code = 1
    try:
        info = json.loads(tools.execute("get_scene_info", "").text)
        assert {o["name"] for o in info["objects"]} == {"Cube", "Camera", "Light"}, info

        ok = tools.execute("run_python", json.dumps({
            "summary": "Add sphere",
            "code": "bpy.ops.mesh.primitive_uv_sphere_add(location=(3, 0, 0))\nprint(bpy.context.object.name)"}))
        assert ok.ok and ok.text == "Sphere", ok
        assert "Sphere" in bpy.data.objects

        # After a default transform_apply the origin reads 0,0,0 but the geometry has not moved.
        tools.execute("run_python", json.dumps({"summary": "Apply", "code": "bpy.ops.object.transform_apply()"}))
        sphere = next(o for o in json.loads(tools.execute("get_scene_info", "").text)["objects"]
                      if o["name"] == "Sphere")
        assert sphere["location"] == [0.0, 0.0, 0.0], sphere
        assert sphere["bounds"] == {"min": [2.0, -1.0, -1.0], "max": [4.0, 1.0, 1.0]}, sphere

        bad = tools.execute("run_python", json.dumps({"summary": "Oops", "code": "1/0"}))
        assert not bad.ok and "ZeroDivisionError" in bad.text, bad

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
        import shutil
        shutil.copy(shot.image_path, Path(__file__).resolve().parent.parent / "out" / "capture_check.png")
        assert bpy.context.scene.render.resolution_x == 1920, "render settings must be restored"
        print(f"TOOLS OK: capture at {shot.image_path} ({shot.image_path.stat().st_size} bytes)")
        code = 0
    except Exception:
        traceback.print_exc()
        print("TOOLS FAILED", file=sys.stderr)
    sys.stdout.flush()
    import os
    os._exit(code)


bpy.context.preferences.view.show_splash = False
bpy.app.timers.register(check, first_interval=1.0)
