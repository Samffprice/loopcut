"""Proves the eval checks without a model or network:
    Blender -b --factory-startup --python harness/evals/selfcheck.py
For every task: the check must fail on the untouched scene (unless doing nothing is correct) and
pass after the task's reference solution has run. ~5s for the whole suite."""

import sys
import traceback
from pathlib import Path
from types import SimpleNamespace

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tasks  # noqa: E402


def run(task) -> list[str]:
    bpy.ops.wm.read_factory_settings(use_empty=False)
    if task.setup:
        task.setup()
    ctx = SimpleNamespace(before=tasks.snapshot(), session=None, stage1=None, memory=None)
    complaints = []
    untouched = task.check(ctx)
    if task.passes_untouched and untouched:
        complaints.append(f"should pass untouched, but: {untouched}")
    if not task.passes_untouched and not untouched:
        complaints.append("passes on the untouched scene, so it checks nothing")
    exec(compile(task.solution, f"<solution:{task.id}>", "exec"), {"__name__": "__solution__"})
    bpy.context.view_layer.update()
    solved = task.check(ctx)
    if solved:
        complaints.append(f"fails on the reference solution: {solved}")
    if task.follow_up:
        ctx.stage1 = tasks.snapshot()
        ctx.memory = task.remember(ctx) if task.remember else {}
        if not task.follow_up_check(ctx):
            complaints.append("the follow-up check passes before the follow-up was done, so it checks nothing")
        exec(compile(task.follow_up_solution, f"<follow_up:{task.id}>", "exec"), {"__name__": "__solution__"})
        bpy.context.view_layer.update()
        solved = task.follow_up_check(ctx)
        if solved:
            complaints.append(f"follow-up fails on the reference solution: {solved}")
    return complaints


def main() -> int:
    failed = 0
    for task in tasks.TASKS:
        try:
            complaints = run(task)
        except Exception:
            complaints = [traceback.format_exc()]
        failed += bool(complaints)
        print(f"{'FAIL' if complaints else 'ok  '} {task.id}")
        for complaint in complaints:
            print("     " + complaint.replace("\n", "\n     "))
    print(f"SELFCHECK {'FAILED' if failed else 'OK'}: {len(tasks.TASKS) - failed}/{len(tasks.TASKS)}")
    return 1 if failed else 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.exit(code)
