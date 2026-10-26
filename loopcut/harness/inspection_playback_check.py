"""inspect_scene while an animation plays, in real Blender with its window drawing: no red card.

    Blender --factory-startup --enable-event-simulate --python loopcut/harness/inspection_playback_check.py
GUI only (playback needs a window); ~20 s. Run it with BLENDER_USER_RESOURCES and TMPDIR set to fresh
folders. Nothing is typed into the system: the scrub is Blender's own simulated events
(--enable-event-simulate, which also keeps real input out of that Blender); without the flag the scrub
case is skipped.

Auto-rig round 7 (0.2.18, GPT-6 Luna): while a walk played, every inspection went stale (each frame
played is a scene change) and showed a red card naming a raw job id, and [1, 4, 7, 10, 13, 238] failed
as too many frames.

Scrub: the timeline's playhead is dragged (anim.change_frame held, a simulated move). Blender then says
is_animation_playing (a timer or a scrub, ED_screen_animation_playing); Loopcut must not count it as
playing, not "pause" it, and start nothing afterwards.

Play: a keyed cube plays, and inspect_scene runs through the agent's own path (agent._run_tool_on_main
on a thread, the main-thread pump serving it) with six frames, then the batch's freshness check
(agent._fresh_images) and the resume (agent._resume_playback) as the agent loop runs them. Checked:
one render (paused before the first try, not after a stale one), the look attached and ok with the
paused note, five frames spread from first to last with 10 named as left out, no job id in the text,
the card still done after the freshness check, and the animation playing again afterwards.
One PASS or FAIL line; exits 1 on a failure.
"""

import ctypes
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import bpy

sys.path.insert(0, str(Path(__file__).parent))
import checkout
checkout.use()
from loopcut import agent, inspection, jobs, mainthread  # noqa: E402

temporary = tempfile.TemporaryDirectory(prefix="loopcut-inspection-playback-check-")
root = Path(os.environ.get("LOOPCUT_INSPECTION_CHECK_DIR", temporary.name)).resolve()
root.mkdir(parents=True, exist_ok=True)
os.environ["LOOPCUT_DATA_DIR"] = str(root / "data")

scene = bpy.context.scene
scene.frame_start, scene.frame_end, scene.render.fps = 1, 48, 24
cube = bpy.data.objects["Cube"]
for frame, x in ((1, -1.5), (24, 1.5), (48, -1.5)):
    cube.location.x = x
    cube.keyframe_insert("location", frame=frame)
scene.frame_set(1)
inspection.register()
mainthread.register()

ARGUMENTS = json.dumps({"purpose": "geometry", "frames": [1, 4, 7, 10, 13, 40], "max_side": 128, "samples": 1,
                        "budget_seconds": 60})
LEAD = SimpleNamespace(role="single")
outcome, failures = {}, []
started = time.monotonic()


def window():
    return bpy.context.window_manager.windows[0]


def window_override():
    return bpy.context.temp_override(window=window(), screen=window().screen)


def blender_playing() -> bool:
    return window().screen.is_animation_playing


def finish() -> None:
    for line in failures:
        print("FAIL", line, flush=True)
    print("INSPECTION PLAYBACK " + ("FAIL" if failures else "PASS")
          + f" in {time.monotonic() - started:.1f}s", flush=True)
    ctypes.CDLL(None).fflush(None)  # Blender's printf: os._exit would drop what a redirected stdout buffered.
    os._exit(1 if failures else 0)


def steps(*functions):
    """Run each function on its own event-loop turn, each returning the seconds before the next."""
    queue = list(functions)

    def run():
        if not queue:
            return None
        try:
            return queue.pop(0)()
        except Exception as ex:
            failures.append(f"{getattr(run, 'name', '')}: {ex!r}")
            finish()
    return run


# ------------------------------------------------------------------ scrub

def timeline():
    for area in window().screen.areas:
        if area.type == "DOPESHEET_EDITOR":
            return area, next(r for r in area.regions if r.type == "WINDOW")
    return None, None


def scrub_press():
    """Blender's own scrub (anim.change_frame invoked in the timeline, as a press in its scrub strip
    does), held until a simulated release: Screen.is_scrubbing is set while it runs."""
    area, region = timeline()
    if not hasattr(window(), "event_simulate") or area is None or "--enable-event-simulate" not in sys.argv:
        print("SKIP scrub: start Blender with --enable-event-simulate", flush=True)
        outcome["scrub"] = "skipped"
        return 0.1
    point = outcome["scrub_point"] = (region.x + region.width // 2, region.y + region.height // 2)
    window().event_simulate("MOUSEMOVE", "NOTHING", x=point[0], y=point[1])
    with bpy.context.temp_override(window=window(), screen=window().screen, area=area, region=region):
        bpy.ops.anim.change_frame("INVOKE_DEFAULT")
    return 0.3


def scrub_drag():
    if outcome.get("scrub") == "skipped":
        return 0.1
    x, y = outcome["scrub_point"]
    window().event_simulate("MOUSEMOVE", "NOTHING", x=x + 80, y=y)
    return 0.3


def scrub_check():
    if outcome.get("scrub") == "skipped":
        return 0.1
    scrubbing = any(w.screen.is_scrubbing for w in bpy.context.window_manager.windows)
    print(f"SCRUB: is_scrubbing {scrubbing}, is_animation_playing {blender_playing()}, "
          f"loopcut playing {inspection.playing()}, frame {scene.frame_current}", flush=True)
    if not (scrubbing and blender_playing()):
        failures.append(f"the drag did not scrub (is_scrubbing {scrubbing}, is_animation_playing {blender_playing()})")
    if inspection.playing() or inspection.pause_playback() or inspection.paused():
        failures.append("a scrub counted as playing, or was paused")
    x, y = outcome["scrub_point"]
    window().event_simulate("LEFTMOUSE", "RELEASE", x=x + 80, y=y)
    return 0.3


def scrub_after():
    if outcome.get("scrub") == "skipped":
        return 0.1
    agent._resume_playback(LEAD)
    return 0.6


def scrub_done():
    if outcome.get("scrub") != "skipped":
        if blender_playing():
            failures.append("playback started after a scrub")
        print(f"SCRUB done: playing afterwards {blender_playing()}", flush=True)
    scene.frame_set(1)
    return 0.2


# ------------------------------------------------------------------ play

def look():
    """The agent thread's part, as run_loop does it for one call in a batch."""
    try:
        result = outcome["first"] = agent._run_tool_on_main("inspect_scene", ARGUMENTS)
        card = {"status": "done" if result.ok else "failed", "output": result.text}
        message = {"role": "tool", "content": result.text}
        images = [("image", "look", False, result.evidence_token, card, message)] if result.image_path else []
        kept = agent._fresh_images(images)
        outcome["playing_before_resume"] = mainthread.run_on_main(blender_playing).result()
        agent._resume_playback(LEAD)
        outcome.update(result=result, card=card, kept=kept)
    except BaseException as ex:  # Reported by the checks below.
        outcome["error"] = repr(ex)


def play():
    with window_override():
        bpy.ops.screen.animation_play()
    outcome["frame_at_start"] = scene.frame_current
    return 0.4


def play_look():
    outcome["playing_seen"] = blender_playing() and scene.frame_current != outcome["frame_at_start"]
    threading.Thread(target=look, daemon=True).start()
    bpy.app.timers.register(watch, first_interval=0.5)
    return None


def watch():
    if time.monotonic() - started > 150:
        failures.append(f"no result after 150 s: {outcome}")
        finish()
    if "result" not in outcome and "error" not in outcome:
        return 0.25
    if "resumed_at" not in outcome:
        outcome["resumed_at"] = scene.frame_current
        return 1.0  # Let it play a second, then look again.
    if "error" in outcome:
        failures.extend([outcome["error"], f"result: {getattr(outcome.get('first'), 'text', '')[:300]}"])
        finish()
    result, card = outcome["result"], outcome["card"]
    text = result.text
    print("RESULT:", text.splitlines()[0][:120], "|", " / ".join(line[:120] for line in text.splitlines()[2:]),
          flush=True)
    rows = [row for row in jobs.list_jobs(root / "data") if row.get("kind") == "inspection"]
    if not (result.ok and result.image_path and Path(result.image_path).is_file()):
        failures.append(f"the look is not attached: {text[:300]}")
    if "stale" in text.lower() or inspection.RETRIED_NOTE.strip() in text:
        failures.append(f"stale, or a second try: {text[:300]}")
    if len(rows) != 1:
        failures.append(f"{len(rows)} renders, not 1: playback was not paused before the first try")
    if inspection.PAUSED_NOTE.strip() not in text:
        failures.append("no note that playback was paused")
    if "left out: 10." not in text or not all(row["frames"] == [1, 4, 7, 13, 40] for row in rows):
        failures.append(f"frames not spread from first to last: {[row['frames'] for row in rows]}")
    if any(row["id"] in text for row in rows):
        failures.append("a job id in the result text")
    if card["status"] != "done" or len(outcome["kept"]) != 1:
        failures.append(f"the card after the freshness check: {card['status']}, images kept {len(outcome['kept'])}")
    if not outcome.get("playing_seen"):
        failures.append("the animation never played before the look")
    if outcome.get("playing_before_resume"):
        failures.append("playing again before the batch's freshness check")
    if not blender_playing() or scene.frame_current == outcome["resumed_at"]:
        failures.append(f"not playing again after the step (frame {scene.frame_current})")
    print(f"JOBS: {len(rows)} inspection(s), {sum(r['state'] == 'complete' for r in rows)} complete; "
          f"playing again: {blender_playing()}, frame {outcome['resumed_at']} -> {scene.frame_current}", flush=True)
    with window_override():
        bpy.ops.screen.animation_cancel(restore_frame=False)
    finish()


bpy.app.timers.register(steps(scrub_press, scrub_drag, scrub_check, scrub_after, scrub_done, play, play_look),
                        first_interval=2.0)
