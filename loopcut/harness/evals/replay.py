"""Re-enact a Loopcut eval run from its logs, in a windowed Blender, for demos and for rendering
any stage of the work at full quality:
    Blender --python harness/evals/replay.py -- <run_dir> <task_id> [--pause 2.0] [--stages]
Opens <task_id>.work.blend (the start scene the run saved before its first turn), then applies the
run's run_python steps in order, exactly as the model wrote them, pausing between them so a screen
recording reads as a walkthrough. The viewport is framed on the scene between steps. Every step's
summary is printed with its timing, and --stages saves <task_id>.replay.NN.blend after each step
so a frame from any point of the process can be rendered later. Nothing talks to the model: this
is the scene's history, not a new run. Captures and questions are skipped (they changed nothing).
The manual arm's <task>.steps/NN_*.py scripts can be replayed the same way with --scripts <folder>."""

import json
import sys
import time
from pathlib import Path

import bpy

ARGS = sys.argv[sys.argv.index("--") + 1:]
RUN_DIR, TASK_ID = Path(ARGS[0]).resolve(), ARGS[1]
PAUSE = float(ARGS[ARGS.index("--pause") + 1]) if "--pause" in ARGS else 2.0
STAGES = "--stages" in ARGS
SCRIPTS = Path(ARGS[ARGS.index("--scripts") + 1]) if "--scripts" in ARGS else None


def steps() -> list[dict]:
    if SCRIPTS is not None:
        return [{"summary": p.stem, "code": p.read_text(encoding="utf-8")} for p in sorted(SCRIPTS.glob("*.py"))]
    result = json.loads((RUN_DIR / f"{TASK_ID}.json").read_text(encoding="utf-8"))
    return [s for s in result.get("steps", []) if s.get("code") and s.get("status") != "failed"]


def frame_viewport() -> None:
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                region = next(r for r in area.regions if r.type == "WINDOW")
                with bpy.context.temp_override(window=window, area=area, region=region):
                    bpy.ops.view3d.view_all(center=True)
                    area.spaces.active.shading.type = "MATERIAL"
                return


STATE = {"index": 0, "steps": steps(), "t0": time.monotonic()}


def tick():
    i = STATE["index"]
    if i >= len(STATE["steps"]):
        print(f"REPLAY done: {len(STATE['steps'])} steps in {time.monotonic() - STATE['t0']:.0f}s")
        return None
    step = STATE["steps"][i]
    print(f"REPLAY step {i + 1}/{len(STATE['steps'])} at {time.monotonic() - STATE['t0']:.1f}s: {step['summary']}")
    try:
        exec(compile(step["code"], f"<step {i + 1}>", "exec"), {"__name__": "__replay__", "bpy": bpy})
    except Exception as ex:
        print(f"REPLAY step {i + 1} raised {type(ex).__name__}: {ex} (the run may have seen the same)")
    bpy.context.view_layer.update()
    frame_viewport()
    if STAGES:
        bpy.ops.wm.save_as_mainfile(filepath=str(RUN_DIR / f"{TASK_ID}.replay.{i + 1:02d}.blend"), copy=True)
    STATE["index"] += 1
    return PAUSE


bpy.ops.wm.open_mainfile(filepath=str(RUN_DIR / f"{TASK_ID}.work.blend"), load_ui=False)
bpy.app.timers.register(tick, first_interval=PAUSE)
