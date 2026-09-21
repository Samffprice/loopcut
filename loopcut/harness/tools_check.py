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

        # An add-on operator that polls for another editor gets it: an open one, or the viewport
        # switched over for the step and back afterwards, with its view untouched.
        view3d = next(a for w in bpy.context.window_manager.windows for a in w.screen.areas if a.type == "VIEW_3D")
        region_3d = view3d.spaces.active.region_3d
        view_before = (region_3d.view_rotation.copy(), region_3d.view_distance, view3d.spaces.active.shading.type)
        assert not any(a.type == "NODE_EDITOR" for w in bpy.context.window_manager.windows for a in w.screen.areas)
        bpy.context.view_layer.objects.active = bpy.data.objects["Cube"]  # The editor follows the active object.
        bpy.data.objects["Cube"].active_material = bpy.data.materials.new("Probe")
        bpy.data.materials["Probe"].use_nodes = True
        node_step = tools.execute("run_python", json.dumps({
            "summary": "Node context", "editor": "node_editor", "code":
            "s = bpy.context.space_data\n"
            "print(bpy.context.area.type, s.type, s.tree_type, s.id.name)\n"
            "bpy.ops.node.select_all(action='SELECT')\n"
            "print(sum(n.select for n in bpy.data.materials['Probe'].node_tree.nodes))"}))
        assert node_step.ok and node_step.text.startswith("NODE_EDITOR NODE_EDITOR ShaderNodeTree Probe\n2\n"), node_step.text
        assert view3d.type == "VIEW_3D", "the viewport was not switched back"
        assert (region_3d.view_rotation, region_3d.view_distance, view3d.spaces.active.shading.type) == view_before
        bad = tools.execute("run_python", json.dumps({"summary": "Bad", "editor": "SPACESHIP", "code": "x = 1"}))
        assert not bad.ok and "No editor 'SPACESHIP'" in bad.text and "ShaderNodeTree" in bad.text, bad.text

        # Many small objects still fit as rows, without their defaults, the selection first.
        tools.execute("run_python", json.dumps({"summary": "Many", "code": (
            "for i in range(60):\n    o = bpy.data.objects.new(f'Marker{i:02}', None)\n"
            "    bpy.context.scene.collection.objects.link(o)")}))
        big = json.loads(tools.execute("get_scene_info", "").text)
        assert "objects by type: EMPTY 60" in scene_context.for_message("hi"), scene_context.for_message("hi")
        assert len(big["objects"]) == 64 and big["objects"][0]["name"] == "Sphere", big["objects"][:2]
        assert big["by_type"]["EMPTY"] == 60 and big["collections"]["Collection"] == 4 and big["matching"] == 64, big
        marker = next(o for o in big["objects"] if o["name"] == "Marker59")
        assert "rotation_deg" not in marker and "scale" not in marker and "visible" not in marker, marker
        lights = json.loads(tools.execute("get_scene_info", json.dumps({"type": "light"})).text)
        assert [o["name"] for o in lights["objects"]] == ["Light"], lights
        # The cascade: only the turn's own changes with changed_only; when rows do not fit, names
        # by collection and the selection as rows; detail=rows gets as many as fit; never a
        # truncated JSON.
        from loopcut import scene_context, scene_diff, state
        session = state.session()
        session["scene_turn_start"] = scene_diff.snapshot()
        tools.execute("run_python", json.dumps({"summary": "Nudge", "code": "bpy.data.objects['Cube'].location.y = 2"}))
        touched = json.loads(tools.execute("get_scene_info", json.dumps({"changed_only": True})).text)
        assert [o["name"] for o in touched["objects"]] == ["Cube"], touched
        assert "changed since your last step" in scene_context.for_message("hi", session["scene_turn_start"]), "user edits reach the model"
        assert "changed since" not in scene_context.for_message("hi", scene_diff.snapshot())
        tools.execute("run_python", json.dumps({"summary": "Many meshes", "code": (
            "for i in range(40):\n    bpy.ops.mesh.primitive_cube_add(location=(i, 0, 0))\n"
            "    bpy.context.object.name = f'Block{i:02}'\n    bpy.context.object.rotation_euler.z = 0.3")}))
        blocks = tools.execute("get_scene_info", json.dumps({"name_contains": "Block"})).text
        assert "chars omitted" not in blocks and len(blocks) <= tools.MAX_OUTPUT_CHARS, len(blocks)
        parsed = json.loads(blocks)  # Valid whatever the size: names when rows do not fit.
        assert "objects" not in parsed and "Block39 (MESH)" in parsed["objects_by_collection"]["Collection"], parsed.get("note")
        assert [o["name"] for o in parsed["selected_objects"]] == ["Block39"], parsed["selected_objects"]
        assert "only names are listed" in parsed["note"], parsed["note"]
        in_collection = json.loads(tools.execute("get_scene_info", json.dumps({"collection": "Collection", "type": "EMPTY"})).text)
        assert in_collection["matching"] == 0, in_collection  # The markers are in the scene collection only.
        some = json.loads(tools.execute("get_scene_info", json.dumps({"name_contains": "Block", "detail": "rows"})).text)
        assert 5 < len(some["objects"]) < 40 and "the first" in some["note"], (len(some["objects"]), some["note"])
        tools.execute("run_python", json.dumps({"summary": "Clean up", "code": (
            "for o in [o for o in bpy.data.objects if o.name.startswith('Block')]:\n    bpy.data.objects.remove(o)")}))

        assert not tools.execute("run_python", "{not json").ok
        assert not tools.execute("nope", "{}").ok

        space = next(a for a in bpy.context.window_manager.windows[0].screen.areas
                     if a.type == "VIEW_3D").spaces.active
        before = (space.region_3d.view_distance, tuple(space.region_3d.view_rotation), space.shading.type,
                  space.overlay.show_overlays, "Render Result" in bpy.data.images)
        shot = tools.execute("capture_viewport", json.dumps({"focus": ["Cube", "Sphere"]}))
        assert shot.ok and shot.image_path.stat().st_size > 10_000, shot
        assert "labelled with their names" in shot.text, shot.text
        out = checkout.OUT
        shutil.copy(shot.image_path, out / "capture_check.png")  # viewport.png is rewritten by every capture.
        assert ("Render Result" in bpy.data.images) == before[-1], "an offscreen capture must not write Render Result"
        # The same picture twice is alike; after a change it is not.
        same = tools.execute("capture_viewport", json.dumps({"focus": ["Cube", "Sphere"]}))
        shutil.copy(same.image_path, out_dir_same := same.image_path.with_name("same.png"))
        assert tools.images_alike(shot.image_path, out_dir_same), "two captures of an unchanged scene must be alike"
        after = (space.region_3d.view_distance, tuple(space.region_3d.view_rotation), space.shading.type,
                 space.overlay.show_overlays, "Render Result" in bpy.data.images)
        assert before == after, f"user's view must be restored: {before} -> {after}"
        assert not tools.execute("capture_viewport", json.dumps({"focus": ["Nope"]})).ok
        assert not tools.execute("capture_viewport", json.dumps({"angle": "nope"})).ok
        assert "Nearest to the viewpoint first: " in shot.text, shot.text
        tools.execute("run_python", json.dumps({"summary": "Red cube in frame", "code": (
            "cube = bpy.data.objects['Cube']\ncube.location.x = 0\n"
            "cube.data.materials[0].node_tree.nodes['Principled BSDF'].inputs['Base Color'].default_value = (1, 0, 0, 1)")}))
        moved = tools.execute("capture_viewport", json.dumps({"focus": ["Cube", "Sphere"]}))  # viewport.png is reused per capture.
        assert not tools.images_alike(moved.image_path, out_dir_same), "moving and recoloring the cube must show"
        for name, arguments in (("capture_camera", {"angle": "camera"}),
                                ("capture_distinct", {"style": "distinct", "focus": ["Cube", "Sphere"]}),
                                ("capture_rendered", {"style": "rendered"}),
                                ("capture_user", {"angle": "user"}),
                                ("capture_sheet", {"angle": "sheet"})):
            extra = tools.execute("capture_viewport", json.dumps(arguments))
            assert extra.ok and extra.image_path.stat().st_size > 5_000, (name, extra.text)
            shutil.copy(extra.image_path, out / f"{name}.png")
            if name == "capture_sheet":
                sheet = bpy.data.images.load(str(extra.image_path))
                assert tuple(sheet.size) == (tools.SHEET_TILE[0] * 2, tools.SHEET_TILE[1] * 2), tuple(sheet.size)
                bpy.data.images.remove(sheet)
                assert "four views of" in extra.text, extra.text
            if name == "capture_user":
                assert "labelled" not in extra.text, "the user's view is shown as is"
        after = (space.region_3d.view_distance, tuple(space.region_3d.view_rotation), space.shading.type,
                 space.overlay.show_overlays, "Render Result" in bpy.data.images)
        assert before == after, f"user's view must be restored after every style: {before} -> {after}"
        # The progress strip: earlier looks side by side, each STRIP_HEIGHT tall, one small image.
        frames = [out / "capture_camera.png", out / "capture_distinct.png", out / "capture_check.png"]
        strip = tools.progress_strip(frames)
        strip_image = bpy.data.images.load(str(strip))
        strip_size = tuple(strip_image.size)
        bpy.data.images.remove(strip_image)
        assert strip_size[1] == tools.STRIP_HEIGHT and strip_size[0] > 3 * tools.STRIP_HEIGHT, strip_size
        assert tools.progress_strip([out / "nope.png"]) is None
        shutil.copy(strip, out / "strip_check.png")

        # Reference images: a side-by-side comparison and a full-resolution crop, nothing left in bpy.data.
        from loopcut import conversations, state
        session = state.session()
        session["references"] = [{"ref": conversations.store_image(session, out / "capture_check.png"),
                                  "full": conversations.store_image(session, out / "capture_check.png"),
                                  "name": "Reference Photo.png", "pinned": True}]
        images_before = set(bpy.data.images.keys())
        both = tools.execute("compare_with_reference", json.dumps({"name": "reference photo.png", "angle": "front"}))
        assert both.ok and "on the left, front view" in both.text, both.text
        side_by_side = bpy.data.images.load(str(both.image_path))
        assert side_by_side.size[0] > 2 * tools.CAPTURE_WIDTH and side_by_side.size[1] > 100, tuple(side_by_side.size)
        bpy.data.images.remove(side_by_side)
        shutil.copy(both.image_path, out / "compare_check.png")
        crop = tools.execute("look_at_reference", json.dumps({"name": "Reference Photo.png", "region": [0.25, 0.25, 0.75, 0.75]}))
        assert crop.ok and "region x 0.25-0.75" in crop.text, crop.text
        cropped = bpy.data.images.load(str(crop.image_path))
        full = bpy.data.images.load(str(conversations.image_path(session["id"], session["references"][0]["full"])))
        full_size = tuple(full.size)
        bpy.data.images.remove(full)
        assert all(abs(c - f / 2) <= 1 for c, f in zip(cropped.size, full_size)), (tuple(cropped.size), full_size)
        bpy.data.images.remove(cropped)
        assert not tools.execute("look_at_reference", json.dumps({"name": "nope.png"})).ok
        assert not tools.execute("look_at_reference", json.dumps({"name": "Reference Photo.png", "region": [0.5, 0, 0.2, 1]})).ok
        assert set(bpy.data.images.keys()) == images_before, "reference tools left an image datablock behind"
        session["references"] = []
        assert bpy.context.scene.render.resolution_x == 1920, "render settings must be restored"

        # File tools: read an image the tool then shows, list, write, move, and the render.
        from loopcut import files
        work = Path(_tempfile.mkdtemp(prefix="loopcut-harness-work-"))  # Not under the data dir: that is off limits.
        shutil.copy(shot.image_path, work / "photo.png")
        seen = tools.execute("read_file", json.dumps({"path": str(work / "photo.png")}))
        assert seen.ok and seen.text.startswith("Image attached: the file photo.png, ") and seen.image_path.stat().st_size > 5_000, seen
        listing = tools.execute("list_files", json.dumps({"path": str(work)})).text
        assert "photo.png  " in listing and "(1 entries)" in listing, listing
        assert tools.execute("write_file", json.dumps({"path": str(work / "notes.txt"), "content": "hello"})).ok
        assert not tools.execute("write_file", json.dumps({"path": str(work / "notes.txt"), "content": "x"})).ok
        moved = tools.execute("move_file", json.dumps({"source": str(work / "notes.txt"), "destination": str(work / "done.txt")}))
        assert moved.ok and (work / "done.txt").read_text() == "hello" and not (work / "notes.txt").exists(), moved.text
        assert not tools.execute("read_file", json.dumps({"path": str(work / ".hidden")})).ok
        roots = files.project_roots()
        assert Path(bpy.app.tempdir).resolve() in roots, roots
        files.LAST_RENDER.unlink(missing_ok=True)
        if files.on_render_complete not in bpy.app.handlers.render_complete:  # lifecycle does this in the add-on.
            bpy.app.handlers.render_complete.append(files.on_render_complete)
        nothing = tools.execute("see_render", "{}")
        assert not nothing.ok and "render=true" in nothing.text, nothing.text
        scene = bpy.context.scene
        engine, scene.render.engine = scene.render.engine, "BLENDER_WORKBENCH"  # Fast; put back before capturing.
        before = (scene.render.filepath, scene.render.resolution_percentage, scene.render.image_settings.file_format)
        rendered = tools.execute("see_render", json.dumps({"render": True}))
        assert rendered.ok and rendered.text.startswith("Image attached: a preview render through Camera with BLENDER_WORKBENCH"), rendered.text
        assert rendered.image_path.stat().st_size > 5_000, rendered.image_path
        assert (scene.render.filepath, scene.render.resolution_percentage, scene.render.image_settings.file_format) == before
        shutil.copy(rendered.image_path, out / "render_check.png")
        scene.render.engine = engine  # Workbench has no material shading, which the capture needs.
        again = tools.execute("capture_viewport", "{}")  # Goes through Render Result too; must not pass for a render.
        assert again.ok and again.image_path.is_file(), again.text
        last = tools.execute("see_render", "{}")
        assert last.ok and last.text.startswith("Image attached: the last render, finished at "), last.text
        assert set(bpy.data.images.keys()) - images_before <= {"Render Result"}, "file tools left an image datablock behind"
        # A still preview while video output is configured must work and preserve the video setup.
        scene.render.engine = "BLENDER_WORKBENCH"
        scene.render.image_settings.media_type = "VIDEO"
        scene.render.image_settings.file_format = "FFMPEG"
        scene.render.ffmpeg.format, scene.render.ffmpeg.codec = "MPEG4", "H264"
        movie = tools.execute("see_render", json.dumps({"render": True}))
        assert movie.ok, movie.text
        assert scene.render.image_settings.media_type == "VIDEO"
        assert scene.render.image_settings.file_format == "FFMPEG"
        assert scene.render.ffmpeg.format == "MPEG4" and scene.render.ffmpeg.codec == "H264"
        print(f"TOOLS OK: capture at {shot.image_path} ({shot.image_path.stat().st_size} bytes)")
        code = 0
    except Exception:
        traceback.print_exc()
        print("TOOLS FAILED", file=sys.stderr)
    sys.stdout.flush()
    os._exit(code)


bpy.context.preferences.view.show_splash = False
bpy.app.timers.register(check, first_interval=1.0)
