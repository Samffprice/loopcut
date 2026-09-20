"""One eval task, one real agent turn, inside a throwaway Blender. Started by run.py:
    Blender --factory-startup --python harness/evals/run_task.py -- <task_id> <result_dir>
Needs a window (capture_viewport renders the viewport). Writes <result_dir>/<task_id>.json and
<task_id>.md (everything the model wrote and was shown). Costs real tokens."""

import json
import os
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
RUN: dict = {}


def transcript(session: dict) -> str:
    lines, shots = [f"# {TASK.id}\n\n> {TASK.prompt}\n"], 0
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


def start():
    try:
        if TASK.setup:
            TASK.setup()
        RUN["before"] = tasks.snapshot()
        session = state.reset()
        if TASK.attachments:
            from loopcut import attachments
            problems = attachments.add(session, TASK.attachments)
            if problems:
                raise RuntimeError(f"could not attach: {problems}")
        RUN["t0"] = time.monotonic()
        if not agent.send(TASK.prompt):
            raise RuntimeError("agent.send refused the prompt")
    except Exception:
        finish({"id": TASK.id, "passed": False, "problems": ["harness: " + traceback.format_exc()]})
    bpy.app.timers.register(poll, first_interval=0.5)


def poll():
    session = state.session()
    elapsed = time.monotonic() - RUN["t0"]
    timed_out = session["busy"] and elapsed >= TIMEOUT
    if session["busy"] and not timed_out:
        return 0.5
    if timed_out:
        agent.stop()
    try:
        items = session["items"]
        try:
            problems = TASK.check(SimpleNamespace(before=RUN["before"], session=session))
        except Exception:
            problems = ["check crashed: " + traceback.format_exc()]
        errors = [i["text"] for i in items if i["kind"] == "error"]
        if timed_out:
            errors.append(f"timed out after {TIMEOUT:.0f}s")
        calls = [i for i in items if i["kind"] == "tool"]
        by_name: dict[str, int] = {}
        for call in calls:
            by_name[call["name"]] = by_name.get(call["name"], 0) + 1
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / f"{TASK.id}.md").write_text(transcript(session), encoding="utf-8")
        finish({
            "id": TASK.id, "tags": TASK.tags, "prompt": TASK.prompt,
            "passed": not problems and not errors,
            "problems": problems, "errors": errors,
            "seconds": round(elapsed, 1),
            "model_steps": sum(1 for m in session["messages"] if m["role"] == "assistant"),
            "tool_calls": by_name,
            "failed_tool_calls": sum(1 for c in calls if c["status"] == "failed"),
            "usage": session.get("usage"),
        })
    except Exception:
        finish({"id": TASK.id, "passed": False, "problems": ["harness: " + traceback.format_exc()]})


bpy.context.preferences.view.show_splash = False
loopcut.register()
# After lifecycle's start-up timer, which swaps the session for the open file's conversation.
bpy.app.timers.register(start, first_interval=1.0)
