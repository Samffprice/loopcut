"""One real agent turn against the configured model, in a throwaway factory scene:
    LOOPCUT_AUTO_RUN=true Blender --factory-startup --python harness/live_check.py -- "prompt"
Writes out/live.png, out/live.panel.png and prints the transcript. Costs real tokens."""
import os
import sys
import time
import traceback
from pathlib import Path

import bpy

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "extension"))
import loopcut  # noqa: E402
from loopcut import agent, state  # noqa: E402

PROMPT = sys.argv[sys.argv.index("--") + 1] if "--" in sys.argv else "Add a red sphere above the cube."
TIMEOUT = 420
STATE: dict = {}


def write_transcript(session: dict, out: Path) -> None:
    """Everything the model wrote and saw, for diagnosing a bad run."""
    import base64
    lines, shots = [], 0
    for message in session["messages"]:
        role, content = message["role"], message.get("content")
        if isinstance(content, list):
            for part in content:
                if part["type"] == "image_url":
                    shots += 1
                    data = part["image_url"]["url"].split(",", 1)[1]
                    (out / ("live_capture_%d.png" % shots)).write_bytes(base64.b64decode(data))
                    lines.append("## image -> live_capture_%d.png\n" % shots)
            continue
        if content:
            lines.append("## %s\n%s\n" % (role, content))
        for call in message.get("tool_calls") or []:
            function = call["function"]
            try:
                arguments = __import__("json").loads(function["arguments"])
            except ValueError:
                arguments = {"raw": function["arguments"]}
            code = arguments.pop("code", None)
            lines.append("## call %s %s\n" % (function["name"], arguments))
            if code:
                lines.append("```python\n%s\n```\n" % code)
    (out / "live_transcript.md").write_text("\n".join(lines), encoding="utf-8")


def finish(code: int):
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


def split():
    window = bpy.context.window_manager.windows[0]
    view = next(a for a in window.screen.areas if a.type == "VIEW_3D")
    region = next(r for r in view.regions if r.type == "WINDOW")
    with bpy.context.temp_override(window=window, area=view, region=region):
        bpy.ops.screen.area_split(direction="VERTICAL", factor=0.7)
    STATE["window"] = window
    bpy.app.timers.register(start, first_interval=0.3)


def start():
    window = STATE["window"]
    panel = max((a for a in window.screen.areas if a.type == "VIEW_3D"), key=lambda a: a.x)
    with bpy.context.temp_override(window=window, area=panel):
        bpy.ops.loopcut.open()
    STATE["panel"], STATE["t0"] = panel, time.monotonic()
    state.reset()
    assert agent.send(PROMPT)
    bpy.app.timers.register(poll, first_interval=0.5)


def poll():
    session = state.session()
    elapsed = time.monotonic() - STATE["t0"]
    if session["busy"] and elapsed < TIMEOUT:
        return 0.5
    try:
        for item in session["items"]:
            if item["kind"] == "tool":
                print(f"[tool:{item['status']}] {item['name']}: {item['summary']}")
                if item["status"] == "failed":
                    print("    " + item["output"][-300:].replace("\n", "\n    "))
            else:
                print(f"[{item['kind']}] {item['text']}")
        print(f"objects: {sorted(o.name for o in bpy.data.objects)}")
        from loopcut import checkpoints
        entries = checkpoints._store(session).entries
        print(f"checkpoints: {[(e['kind'], round(e['size'] / 1e3), 'KB') for e in entries]}, "
              f"user items linked: {sum(1 for i in session['items'] if i.get('checkpoint'))}")
        images = sum(1 for m in session["messages"] if isinstance(m.get("content"), list))
        print(f"elapsed {elapsed:.0f}s, api messages {len(session['messages'])}, viewport images sent {images}")
        out = REPO / "out"
        out.mkdir(exist_ok=True)
        write_transcript(session, out)
        with bpy.context.temp_override(window=STATE["window"]):
            bpy.ops.screen.screenshot(filepath=str(out / "live.png"))
        ok = not session["busy"] and not any(i["kind"] == "error" for i in session["items"])
        print("LIVE OK" if ok else "LIVE FAILED")
        finish(0 if ok else 1)
    except Exception:
        traceback.print_exc()
        finish(1)


bpy.context.preferences.view.show_splash = False
loopcut.register()
bpy.app.timers.register(split, first_interval=0.5)
