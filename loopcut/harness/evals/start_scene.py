"""Export a task's start scene for a run in some other tool (the manual arm of a comparison):
    Blender -b --factory-startup --python harness/evals/start_scene.py -- <arm_dir> [task ids...]
Writes, per task, <arm_dir>/<task>.start.blend, <task>.prompt.txt (paste it verbatim as the first
message), <task>.followup.txt for two-turn tasks (paste it once the first brief is done and
<task>.stage1.blend is saved) and, unless one exists, <task>.run.json to fill in afterwards. Do the task in the other
tool starting from the .start.blend, save the result as <task>.final.blend next to it, and hand
the folder to compare.py. Default: every task tagged `project`."""

import json
import sys
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tasks  # noqa: E402

RUN_TEMPLATE = {
    "tool": "",                # e.g. "ChatGPT app + blender-mcp"
    "model": "",               # e.g. "astra, medium"
    "seconds": None,           # from sending the first prompt to the tool saying it is done
    "prompts_sent": None,      # every message you typed, the first one included
    "manual_fixes": None,      # every edit you made in Blender yourself
    "notes": "",
}


def main() -> None:
    args = sys.argv[sys.argv.index("--") + 1:]
    arm = Path(args[0]).resolve()
    chosen = args[1:] or [task.id for task in tasks.TASKS if "project" in task.tags]
    arm.mkdir(parents=True, exist_ok=True)
    for task_id in chosen:
        task = tasks.BY_ID[task_id]
        bpy.ops.wm.read_factory_settings(use_empty=False)
        if task.setup:
            task.setup()
        if task.attachments:
            print(f"{task_id}: attach {task.attachments} to the first message as well")
        bpy.ops.wm.save_as_mainfile(filepath=str(arm / f"{task_id}.start.blend"))
        (arm / f"{task_id}.prompt.txt").write_text(task.prompt + "\n", encoding="utf-8")
        if task.follow_up:
            (arm / f"{task_id}.followup.txt").write_text(task.follow_up + "\n", encoding="utf-8")
        run_file = arm / f"{task_id}.run.json"
        if not run_file.is_file():
            run_file.write_text(json.dumps(RUN_TEMPLATE, indent=1) + "\n", encoding="utf-8")
        print(f"START {task_id}: {arm / f'{task_id}.start.blend'}")


main()
sys.stdout.flush()
