"""Measure the new inspection on an existing perfume result without re-running the model.
Blender -b --factory-startup --python-exit-code 1 --python this.py -- <perfume.blend> <output-directory>
"""
import json
import os
import sys
import time
from pathlib import Path
import bpy

sys.path.insert(0, str(Path(__file__).parent))
import checkout
checkout.use()
from loopcut import context, inspection, jobs

source, out = [Path(p).resolve() for p in sys.argv[sys.argv.index("--") + 1:]]
out.mkdir(parents=True, exist_ok=True)
os.environ["LOOPCUT_DATA_DIR"] = str(out / "data")
bpy.ops.wm.open_mainfile(filepath=str(source), load_ui=False, use_scripts=False)
scene = bpy.context.scene
cameras = sorted(o.name for o in scene.objects if o.type == "CAMERA")
assert 1 <= len(cameras) <= 4, cameras
started = time.monotonic()
request = inspection.inspect_scene("final_lighting", cameras=cameras, frames=[1], max_side=384,
                                     samples=8, budget_seconds=180)
while True:
    row = jobs.status(request.pending_job)
    if row["state"] not in jobs.ACTIVE:
        break
    time.sleep(.1)
result = inspection.finish(request.pending_job)
assert result.ok and result.image_path, result.text
metadata = jobs.read_json(Path(row["output"]) / "inspection.json")
report = {"source": str(source), "source_revision": row["scene_revision"], "cameras": cameras,
          "wall_seconds": round(time.monotonic() - started, 3), "render_seconds": metadata["elapsed_seconds"],
          "image_bytes": metadata["sheet_bytes"], "image_dimensions": metadata["size"],
          "context_token_estimate_including_fixed_overhead": context.estimate_tokens([
              {"role": "tool", "content": result.text}, {"role": "user", "content": [
                  {"type": "image_url", "image_url": {"url": "contact_sheet"}}]}]),
          "provider_billed_tokens": None, "contact_sheet": str(result.image_path),
          "signals": [{"camera": v["camera"], **v["signal"]} for v in metadata["views"]]}
(out / "metrics.json").write_text(json.dumps(report, indent=1))
jobs.reap()
print("PERFUME INSPECTION OK " + json.dumps(report), flush=True)
