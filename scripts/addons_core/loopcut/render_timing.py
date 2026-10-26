"""Time each frame a workspace Blender renders, for a long render's choice (render_offer.py, 0.2.19).

No package imports: run_blender runs this prelude with --python, as it does render_path_guard.py. Each frame a render
finishes prints one PREFIX line: the scene, what it rendered at (engine, pixels, samples, device) and its seconds.
workspace.run_blender reads the lines back out of the log (render_offer.log_frames) and leaves them out of what the
model reads. A background Blender with --factory-startup has no GPU compute device, so Cycles renders on the CPU here,
as a render job does.
"""

import json
import time

import bpy

PREFIX = "Loopcut frame: "
_started = []


def _settings(scene):
    r = scene.render
    engine = "BLENDER_EEVEE" if r.engine in ("BLENDER_EEVEE", "BLENDER_EEVEE_NEXT") else r.engine
    samples, device = None, "GPU"
    if engine == "CYCLES":
        samples = scene.cycles.samples
        addon = bpy.context.preferences.addons.get("cycles")
        kind = getattr(getattr(addon, "preferences", None), "compute_device_type", "NONE")
        device = "GPU" if scene.cycles.device == "GPU" and kind not in ("NONE", "", None) else "CPU"
    elif engine == "BLENDER_EEVEE":
        samples = scene.eevee.taa_render_samples
    return {"engine": engine, "width": max(4, r.resolution_x * r.resolution_percentage // 100),
            "height": max(4, r.resolution_y * r.resolution_percentage // 100), "samples": samples, "device": device}


@bpy.app.handlers.persistent
def _pre(scene, *_):
    _started[:] = [time.monotonic()]


@bpy.app.handlers.persistent
def _post(scene, *_):
    if not _started:
        return
    seconds = time.monotonic() - _started.pop()
    try:
        print(PREFIX + json.dumps({"scene": scene.name, **_settings(scene), "seconds": round(seconds, 3)}), flush=True)
    except Exception:  # A timing line must never fail the render.
        pass


bpy.app.handlers.render_pre.append(_pre)
bpy.app.handlers.render_post.append(_post)
