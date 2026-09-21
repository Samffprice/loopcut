"""One eval task, one or two real agent turns, inside a throwaway Blender. Started by run.py:
    Blender --factory-startup --python harness/evals/run_task.py -- <task_id> <result_dir>
Needs a window (capture_viewport renders the viewport). The start scene is saved as
<result_dir>/<task_id>.work.blend first, so "next to this file" means the result folder for
both this arm and a manual one. Writes <task_id>.json, <task_id>.md (everything the model
wrote and was shown), <task_id>.stage1.blend after the first brief of a two-turn task, and
<task_id>.final.blend. Costs real tokens."""

import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path
from types import SimpleNamespace

import bpy

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import checkout  # noqa: E402
checkout.use()
# Eval runs must not write conversations or checkpoints into the user's real Loopcut data.
os.environ.setdefault("LOOPCUT_DATA_DIR", tempfile.mkdtemp(prefix="loopcut-eval-"))
os.environ["LOOPCUT_AUTO_RUN"] = "true"

import loopcut  # noqa: E402
import tasks  # noqa: E402
from loopcut import agent, conversations, state  # noqa: E402

TASK = tasks.BY_ID[sys.argv[sys.argv.index("--") + 1]]
OUT = Path(sys.argv[sys.argv.index("--") + 2])
TIMEOUT = float(os.environ.get("LOOPCUT_EVAL_TIMEOUT", "300"))
RUN: dict = {"stage": 1, "problems": [], "follow_up_problems": [], "errors": [], "stage1_seconds": None}


def transcript(session: dict) -> str:
    lines, shots = [f"# {TASK.id}\n\n> {TASK.prompt}\n"], 0
    if TASK.follow_up:
        lines.append(f"> then: {TASK.follow_up}\n")
    for message in session["messages"]:
        content = message.get("content")
        if isinstance(content, list):
            for part in content:
                if part["type"] == "image_url":
                    shots += 1
                    name = f"{TASK.id}.capture{shots}.png"
                    source = conversations.image_path(session["id"], part["image_url"]["url"])
                    (OUT / name).write_bytes(source.read_bytes())
                    lines.append(f"## image\n![]({name})\n")
            continue
        if content:
            lines.append(f"## {message['role']}\n{content}\n")
        for call in message.get("tool_calls") or []:
            function = call["function"]
            try:
                arguments = json.loads(function["arguments"])
            except ValueError:
                arguments = {"raw": function["arguments"]}
            code = arguments.pop("code", None) if isinstance(arguments, dict) else None
            lines.append(f"## call {function['name']} {arguments}\n")
            if code:
                lines.append(f"```python\n{code}\n```\n")
    return "\n".join(lines)


def finish(result: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{TASK.id}.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)  # Blender's own exit would wait on the agent thread and the window.


def fail(text: str) -> None:
    finish({"id": TASK.id, "passed": False, "problems": ["harness: " + text]})


def start():
    try:
        OUT.mkdir(parents=True, exist_ok=True)
        if TASK.setup:
            TASK.setup()
        RUN["before"] = tasks.snapshot()
        bpy.ops.wm.save_as_mainfile(filepath=str(OUT / f"{TASK.id}.work.blend"))
        session = state.reset()
        if TASK.attachments:
            from loopcut import attachments
            problems = attachments.add(session, TASK.attachments)
            if problems:
                raise RuntimeError(f"could not attach: {problems}")
        RUN["t0"] = RUN["t_stage"] = time.monotonic()
        if not agent.send(TASK.prompt):
            raise RuntimeError("agent.send refused the prompt")
    except Exception:
        fail(traceback.format_exc())
    bpy.app.timers.register(poll, first_interval=0.5)


def context() -> SimpleNamespace:
    return SimpleNamespace(before=RUN["before"], session=state.session(),
                           stage1=RUN.get("stage1"), memory=RUN.get("memory"))


def poll():
    session = state.session()
    elapsed = time.monotonic() - RUN["t_stage"]
    timed_out = session["busy"] and elapsed >= TIMEOUT
    if session["busy"] and not timed_out:
        return 0.5
    if timed_out:
        agent.stop()
        RUN["errors"].append(f"stage {RUN['stage']} timed out after {TIMEOUT:.0f}s")
    try:
        if RUN["stage"] == 1:
            try:
                RUN["problems"] = TASK.check(context())
            except Exception:
                RUN["problems"] = ["check crashed: " + traceback.format_exc()]
            RUN["stage1_seconds"] = round(elapsed, 1)
            RUN["items_after_stage1"] = len(session["items"])
            if TASK.follow_up and not timed_out:
                bpy.ops.wm.save_as_mainfile(filepath=str(OUT / f"{TASK.id}.stage1.blend"), copy=True)
                RUN["stage1"] = tasks.snapshot()
                RUN["memory"] = TASK.remember(context()) if TASK.remember else {}
                RUN["stage"] = 2
                RUN["t_stage"] = time.monotonic()
                if not agent.send(TASK.follow_up):
                    raise RuntimeError("agent.send refused the follow-up")
                return 0.5
            if TASK.follow_up:
                RUN["follow_up_problems"] = ["not sent: the first brief timed out"]
        else:
            try:
                RUN["follow_up_problems"] = TASK.follow_up_check(context())
            except Exception:
                RUN["follow_up_problems"] = ["check crashed: " + traceback.format_exc()]
        items = session["items"]
        errors = [i["text"] for i in items if i["kind"] == "error"] + RUN["errors"]
        calls = [i for i in items if i["kind"] == "tool"]
        by_name: dict[str, int] = {}
        for call in calls:
            by_name[call["name"]] = by_name.get(call["name"], 0) + 1
        (OUT / f"{TASK.id}.md").write_text(transcript(session), encoding="utf-8")
        # The raw conversation (messages, tool results, captures) and the ordered steps, so
        # replay.py can re-enact the run and the conversation can be replayed through the model.
        folder = conversations._folder(session["id"])
        if folder.is_dir():
            shutil.copytree(folder, OUT / f"{TASK.id}.conversation", dirs_exist_ok=True)
        steps = [{"name": i["name"], "summary": i.get("summary", ""), "code": i.get("code"), "status": i["status"],
                  "output": (i.get("output") or "")[:2000]} for i in items if i["kind"] == "tool"]
        # The finished scene, so grade.py can score and render it the same way as a scene made
        # in any other tool (see compare.py).
        bpy.ops.wm.save_as_mainfile(filepath=str(OUT / f"{TASK.id}.final.blend"), copy=True)
        finish({
            "id": TASK.id, "tags": TASK.tags, "prompt": TASK.prompt, "follow_up": TASK.follow_up,
            "passed": not RUN["problems"] and not RUN["follow_up_problems"] and not errors,
            "problems": RUN["problems"], "follow_up_problems": RUN["follow_up_problems"], "errors": errors,
            "seconds": round(time.monotonic() - RUN["t0"], 1), "stage1_seconds": RUN["stage1_seconds"],
            "prompts": 2 if TASK.follow_up else 1,
            "model_steps": sum(1 for m in session["messages"] if m["role"] == "assistant"),
            "tool_calls": by_name,
            "failed_tool_calls": sum(1 for c in calls if c["status"] == "failed"),
            "usage": session.get("usage"),
            "steps": steps,
            "stage1_items": RUN.get("items_after_stage1"),
        })
    except Exception:
        fail(traceback.format_exc())


bpy.context.preferences.view.show_splash = False
loopcut.register()
# After lifecycle's start-up timer, which swaps the session for the open file's conversation.
bpy.app.timers.register(start, first_interval=1.0)
