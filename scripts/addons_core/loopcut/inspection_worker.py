"""Real renders and labeled contact sheets. Imported only in the isolated render worker."""

import math
import time
from pathlib import Path

import blf
import bpy
import imbuf
import numpy as np
from mathutils import Vector
from bpy_extras.object_utils import world_to_camera_view

import jobs
from job_worker import configure, image_record


def bounds(objects):
    graph = bpy.context.evaluated_depsgraph_get()
    points = [evaluated.matrix_world @ Vector(corner) for obj in objects
              for evaluated in [obj.evaluated_get(graph)] for corner in evaluated.bound_box]
    if not points:
        raise jobs.JobError("No geometry to frame. Supply a camera or focus objects with geometry.")
    low = Vector(min(p[i] for p in points) for i in range(3))
    high = Vector(max(p[i] for p in points) for i in range(3))
    return (low + high) / 2, max((high - low).length / 2, .01)


def projections(scene, camera, objects):
    result = []
    graph = bpy.context.evaluated_depsgraph_get()
    for obj in objects:
        evaluated = obj.evaluated_get(graph)
        points = [world_to_camera_view(scene, camera, evaluated.matrix_world @ Vector(corner))
                  for corner in evaluated.bound_box]
        front = [p for p in points if p.z > 0]
        result.append({"name": obj.name, "bounds": [round(min(p.x for p in front), 4),
                       round(min(p.y for p in front), 4), round(max(p.x for p in front), 4),
                       round(max(p.y for p in front), 4)] if front else None,
                       "corners_in_frame": sum(0 <= p.x <= 1 and 0 <= p.y <= 1 and p.z > 0 for p in points),
                       "hidden_render": obj.hide_render})
    return result


def studio(scene):
    for obj in scene.objects:
        if obj.type == "LIGHT":
            obj.hide_render = True
    world = bpy.data.worlds.new("Inspection studio")
    world.use_nodes = True
    world.node_tree.nodes.get("Background").inputs["Color"].default_value = (.15, .15, .15, 1)
    world.node_tree.nodes.get("Background").inputs["Strength"].default_value = .35
    scene.world = world
    lights = []
    for name in ("Key", "Fill", "Rim"):
        data = bpy.data.lights.new("Inspection " + name, "AREA")
        obj = bpy.data.objects.new(data.name, data)
        scene.collection.objects.link(obj)
        lights.append(obj)
    return lights


def make_sheet(spec, output: Path, records):
    width, height, footer = spec["width"], spec["height"], 52
    columns, rows = len(spec["frames"]), max(1, len(spec["cameras"]))
    sheet = imbuf.new((width * columns, (height + footer) * rows))
    with sheet.with_buffer(write=True) as buffer:
        pixels = np.asarray(buffer)
        pixels[:] = (20, 23, 29, 255)
    try:
        for index, record in enumerate(records):
            tile = imbuf.load(str(output / record["file"]))
            x = (index % columns) * width
            y = (rows - 1 - index // columns) * (height + footer)
            try:
                with tile.with_buffer() as source, sheet.with_buffer(write=True) as dest:
                    pixels = np.asarray(source)
                    np.asarray(dest)[y + footer:y + footer + height, x:x + width] = pixels
                    rgb = pixels[..., :3].astype(np.float32) / 255
                    record["signal"] = {"mean_rgb": [round(float(v), 4) for v in rgb.mean(axis=(0, 1))],
                                        "fraction_above_002": round(float(np.mean(rgb.max(axis=2) > .02)), 4),
                                        "near_black": bool(np.quantile(rgb.max(axis=2), .95) < .02)}
            finally:
                tile.free()
            with blf.bind_imbuf(0, sheet):
                blf.size(0, 13)
                blf.color(0, 1, 1, 1, 1)
                for line, text in enumerate((f"{record['camera']} | frame {record['frame']}",
                                             f"{spec['purpose']} | {spec['engine']}",
                                             f"{width}x{height} | {spec['samples']} samples | {spec['scene_revision'][:8]}")):
                    # Clip long names, never let one tile's label bleed into the next.
                    while text and blf.dimensions(0, text)[0] > width - 12:
                        text = text[:-1]
                    blf.position(0, x + 6, y + 36 - line * 15, 0)
                    blf.draw_buffer(0, text)
                for focus in record["focus"]:
                    box = focus["bounds"]
                    if box is None or focus["hidden_render"]:
                        continue
                    center_x, center_y = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
                    if not 0 <= center_x <= 1 or not 0 <= center_y <= 1:
                        continue
                    label = focus["name"][:28]
                    label_width = min(width - 4, math.ceil(blf.dimensions(0, label)[0]) + 8)
                    lx = x + max(2, min(width - label_width - 2, int(box[0] * width)))
                    label_y = int(box[3] * height) + 2
                    if label_y > height - 20:
                        label_y = int(box[1] * height) - 20
                    ly = y + footer + max(2, min(height - 20, label_y))
                    with sheet.with_buffer(write=True) as buffer:
                        np.asarray(buffer)[ly:ly + 18, lx:lx + label_width] = (15, 18, 25, 255)
                    blf.position(0, lx + 4, ly + 4, 0)
                    blf.draw_buffer(0, label)
        target = output / "contact_sheet.png"
        sheet.file_type = "PNG"
        imbuf.write(sheet, filepath=str(target))
        return target, list(sheet.size)
    finally:
        sheet.free()


def render_inspection(path: Path, spec: dict, scene) -> None:
    started = time.monotonic()
    if jobs.digest(Path(__file__)) != spec.get("inspection_revision"):
        raise jobs.JobError("Inspection renderer changed; request a fresh inspection.")
    output = Path(spec["output"])
    configure(scene, spec)
    # Geometry/studio materials should not pass through the scene's compositing or sequencer.
    # Final lighting preserves both, so a scene's composited camera result is what gets judged.
    if spec["purpose"] != "final_lighting":
        scene.render.use_compositing = scene.render.use_sequencer = False
    if spec["purpose"] == "geometry":
        shading = scene.display.shading
        shading.light, shading.color_type = "STUDIO", "MATERIAL"
        shading.show_shadows, shading.show_cavity = True, True
        shading.background_type, shading.background_color = "WORLD", (.06, .06, .06)
    targets = [scene.objects[name] for name in spec["focus"]] if spec["focus"] else [
        obj for obj in scene.objects if obj.type in {"MESH", "CURVE", "FONT", "SURFACE", "META", "VOLUME"} and not obj.hide_render]
    generated = None
    if not spec["cameras"]:
        generated = bpy.data.objects.new("Inspection view", bpy.data.cameras.new("Inspection view"))
        scene.collection.objects.link(generated)
        generated.data.type, generated.data.ortho_scale = "ORTHO", 5
    cameras = [scene.objects[name] for name in spec["cameras"]] if spec["cameras"] else [generated]
    rig = studio(scene) if spec["purpose"] == "materials" else []
    records = []
    progress = {"phase": "inspecting", "completed": 0, "total": len(cameras) * len(spec["frames"]),
                "verified_complete": False, "files": records}
    jobs.atomic_json(path / "progress.json", progress)
    for camera_index, camera in enumerate(cameras):
        for frame in spec["frames"]:
            scene.frame_set(frame)
            scene.camera = camera
            if generated or rig:
                center, radius = bounds(targets)
                if generated:
                    direction = Vector((1, -1.5, .8)).normalized()
                    camera.location = center + direction * radius * 4
                    camera.rotation_euler = (-direction).to_track_quat("-Z", "Y").to_euler()
                    camera.data.ortho_scale = radius * 2.4 * max(1, spec["width"] / spec["height"])
                    camera.data.clip_end = max(1000, radius * 10)
                for light, direction, energy in zip(rig, ((-2, -3, 3), (3, -1, 1), (0, 3, 3)), (450, 200, 600)):
                    light.location = center + Vector(direction) * radius
                    light.rotation_euler = (center - light.location).to_track_quat("-Z", "Y").to_euler()
                    light.data.energy, light.data.size = energy * radius ** 2, radius * 3
            bpy.context.view_layer.update()
            target = output / f"view_{camera_index:02d}_{frame:07d}.png"
            scene.render.filepath = str(target)
            bpy.ops.render.render(write_still=True, scene=scene.name)
            record = image_record(target, spec["width"], spec["height"])
            record.update(camera=camera.name, frame=frame, scene_revision=spec["scene_revision"],
                          focus=projections(scene, camera, targets[:12]) if spec["focus"] else [],
                          camera_matrix=[list(row) for row in camera.matrix_world])
            records.append(record)
            progress["completed"] = len(records)
            jobs.atomic_json(path / "progress.json", progress)
    sheet, size = make_sheet(spec, output, records)
    metadata = {"scene_revision": spec["scene_revision"], "purpose": spec["purpose"],
                "engine": spec["engine"], "source_engine": spec["source_engine"], "samples": spec["samples"],
                "quality": "bounded inspection, not production render", "size": size,
                "view_transform": scene.view_settings.view_transform, "look": scene.view_settings.look,
                "exposure": scene.view_settings.exposure, "gamma": scene.view_settings.gamma,
                "elapsed_seconds": round(time.monotonic() - started, 3), "sheet_bytes": sheet.stat().st_size,
                "sheet_sha256": jobs.digest(sheet), "views": records}
    jobs.atomic_json(output / "inspection.json", metadata)
    progress.update(phase="verified", verified_complete=True, elapsed_seconds=metadata["elapsed_seconds"])
    jobs.atomic_json(path / "progress.json", progress)
    jobs.atomic_json(output / "manifest.json", {"job": spec, "result": metadata})
