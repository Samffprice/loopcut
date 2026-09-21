"""Real Blender regressions from the perfume runs: GN inspection, Cycles preview, state restoration.

    Blender --factory-startup --python loopcut/harness/perception_check.py
Needs a window/GPU, no model or network. Synthetic emission gives a known visible signal;
file size alone would accept a black capture with text labels on it.
"""

import json
import os
import sys
import traceback
from pathlib import Path

import bpy
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import checkout
checkout.use()
from loopcut import object_info, scene_context, tools


def check():
    status = 1
    try:
        fixture = Path(__file__).parent / "fixtures" / "perfume_bottle.blend"
        with bpy.data.libraries.load(str(fixture)) as (source, target):
            target.objects = source.objects
        for obj in target.objects:
            if obj:
                bpy.context.scene.collection.objects.link(obj)
        for name in ("verre", "ouverture", "liquide", "bouchon", "boite"):
            details = object_info.describe(bpy.data.objects[name])
            json.dumps(details)  # Native IDs/arrays must be converted, not stringified on failure.
            assert details["data"], name
        cap = object_info.describe(bpy.data.objects["bouchon"])
        modifiers = [m for m in cap["modifiers"] if m["type"] == "NODES"]
        assert modifiers and modifiers[0]["inputs"], cap
        if hasattr(bpy.data.objects["bouchon"].modifiers[modifiers[0]["name"]], "properties"):
            assert all("access" in value for value in modifiers[0]["inputs"].values())
        for obj in list(bpy.data.objects):
            if obj.name not in ("Cube", "Camera", "Light"):
                bpy.data.objects.remove(obj, do_unlink=True)

        material = bpy.data.materials.new("Emission probe")
        material.use_nodes = True
        material.node_tree.nodes.clear()
        emission = material.node_tree.nodes.new("ShaderNodeEmission")
        emission.inputs["Color"].default_value = (1, .1, .05, 1)
        output = material.node_tree.nodes.new("ShaderNodeOutputMaterial")
        material.node_tree.links.new(emission.outputs[0], output.inputs["Surface"])
        cube = bpy.data.objects["Cube"]
        cube.data.materials.clear()
        cube.data.materials.append(material)
        scene = bpy.context.scene
        space = tools._view3d_override()["area"].spaces.active
        shading = space.shading
        shading.use_scene_world = shading.use_scene_lights = True
        shading.use_scene_world_render = shading.use_scene_lights_render = False

        def settings():
            return (scene.render.engine, shading.type, shading.use_scene_world, shading.use_scene_lights,
                    shading.use_scene_world_render, shading.use_scene_lights_render, space.overlay.show_overlays,
                    tuple(space.region_3d.view_matrix), scene.render.resolution_percentage, scene.render.filepath)

        for engine in ("BLENDER_EEVEE", "CYCLES"):
            # A build missing Cycles fails this regression deliberately: test against a supported build.
            scene.render.engine = engine
            before = settings()
            shot = tools.capture_viewport(angle="camera", style="rendered")
            pixels = tools._pixels(shot.image_path)[..., :3]
            red = np.mean((pixels[..., 0] > .2) & (pixels[..., 0] > pixels[..., 1] * 1.5))
            assert red > .05, f"{engine} preview lost the bright red object: {red}"
            assert "EEVEE lighting preview" in shot.text and engine in shot.text
            assert settings() == before, "preview changed render or viewport settings"
            print(f"PERCEPTION {engine}: red area {red:.3f}", flush=True)
        assert "CYCLES" in scene_context.for_message("set up lighting")
        assert "BLENDER_WORKBENCH" in scene_context.for_message("set up lighting")
        assert "render engines:" in scene_context.for_message("set up lighting")
        assert "studio lighting" in tools.capture_viewport(angle="camera", style="material").text
        assert settings() == before

        draw = tools._draw_offscreen
        def fail(*args, **kwargs):
            raise RuntimeError("injected GPU failure")
        tools._draw_offscreen = fail
        try:
            try:
                tools.capture_viewport(angle="camera", style="rendered")
                raise AssertionError("expected the injected failure")
            except RuntimeError as error:
                assert str(error) == "injected GPU failure"
            assert settings() == before, "failed preview leaked temporary settings"
        finally:
            tools._draw_offscreen = draw

        # The real conversation failed whenever the user left the 3D workspace.
        areas = [(a, a.type, a.ui_type) for w in bpy.context.window_manager.windows for a in w.screen.areas]
        try:
            for area, kind, _ in areas:
                if kind == "VIEW_3D":
                    area.type = "SEQUENCE_EDITOR"
            layouts = [(a.type, a.ui_type) for a, _, _ in areas]
            result = tools.run_python("bpy.data.objects['Cube'].location.x = 0.25", "Move in sequencer")
            assert result.ok and cube.location.x == .25, result.text
            assert [(a.type, a.ui_type) for a, _, _ in areas] == layouts
            result = tools.capture_viewport(angle="camera", style="rendered")
            assert result.ok and result.image_path.is_file()
            assert [(a.type, a.ui_type) for a, _, _ in areas] == layouts
            result = tools.run_python("raise ValueError('probe')", "Fail in sequencer", editor="VIEW_3D")
            assert not result.ok
            assert [(a.type, a.ui_type) for a, _, _ in areas] == layouts
            result = tools.run_python("assert bpy.context.area.type == 'SEQUENCE_EDITOR'", "Use sequencer",
                                      editor="SEQUENCE_EDITOR")
            assert result.ok, result.text
        finally:
            for area, kind, ui_type in areas:
                area.type, area.ui_type = kind, ui_type
        print("PERCEPTION OK", flush=True)
        status = 0
    except Exception:
        traceback.print_exc()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(status)


bpy.app.timers.register(check, first_interval=1)
