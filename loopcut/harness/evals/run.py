#!/usr/bin/env python3
"""Run the eval suite against the configured model and report pass rate, steps and time.

    python3 harness/evals/run.py                 # every task
    python3 harness/evals/run.py table camera    # some tasks
    python3 harness/evals/run.py --tag spatial --jobs 3 --label "bounds in scene info"

Each task gets its own windowed Blender (LOOPCUT_BLENDER, default tools/Blender.app) and a real
agent turn, so a run costs tokens and a few minutes. Results land in out/evals/<time>/ with one
transcript per task; summary.md compares against the previous run so a change to a prompt, a tool
or the model shows up as tasks won and lost, not as a feeling.

Check the checks first, for free:  Blender -b --factory-startup --python harness/evals/selfcheck.py
"""

import argparse
import ast
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
EVALS = REPO / "out" / "evals"


def task_index() -> dict[str, list[str]]:
    """Task ids and tags, read from tasks.py without importing it (it needs bpy)."""
    tree = ast.parse((Path(__file__).parent / "tasks.py").read_text(encoding="utf-8"))
    index = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Task" and node.args:
            tags = next((ast.literal_eval(k.value) for k in node.keywords if k.arg == "tags"), [])
            index[ast.literal_eval(node.args[0])] = tags
    return index


def blender() -> str:
    path = os.environ.get("LOOPCUT_BLENDER") or str(REPO / "tools/Blender.app/Contents/MacOS/Blender")
    if not Path(path).is_file():
        sys.exit(f"No Blender at {path}; set LOOPCUT_BLENDER.")
    return path


def run_one(task_id: str, out: Path, timeout: float) -> dict:
    command = [blender(), "--factory-startup", "--python", str(Path(__file__).parent / "run_task.py"),
               "--", task_id, str(out)]
    env = {**os.environ, "LOOPCUT_EVAL_TIMEOUT": str(timeout)}
    log = out / f"{task_id}.log"
    try:
        with log.open("w", encoding="utf-8") as handle:
            subprocess.run(command, stdout=handle, stderr=subprocess.STDOUT, env=env, timeout=timeout + 60)
    except subprocess.TimeoutExpired:
        return {"id": task_id, "passed": False, "problems": ["harness: Blender did not exit"]}
    result_file = out / f"{task_id}.json"
    if not result_file.is_file():
        return {"id": task_id, "passed": False, "problems": [f"harness: no result written, see {log.name}"]}
    return json.loads(result_file.read_text(encoding="utf-8"))


def previous_run(current: Path) -> dict | None:
    runs = sorted(p for p in EVALS.glob("*/results.json") if p.parent != current)
    return json.loads(runs[-1].read_text(encoding="utf-8")) if runs else None


def summarize(run: dict, previous: dict | None) -> str:
    results = run["results"]
    passed = [r for r in results if r["passed"]]
    lines = [f"# Loopcut evals: {len(passed)}/{len(results)} passed",
             "", f"- model: `{run['model']}`", f"- label: {run['label'] or '-'}",
             f"- wall time: {run['wall_seconds']:.0f}s, "
             f"task time: {sum(r.get('seconds', 0) for r in results):.0f}s, "
             f"model steps: {sum(r.get('model_steps', 0) for r in results)}, "
             f"failed tool calls: {sum(r.get('failed_tool_calls', 0) for r in results)}", ""]
    if previous:
        then = {r["id"]: r["passed"] for r in previous["results"]}
        won = [r["id"] for r in results if r["passed"] and then.get(r["id"]) is False]
        lost = [r["id"] for r in results if not r["passed"] and then.get(r["id"]) is True]
        lines += [f"Against the previous run ({previous['started']}, `{previous['model']}`, "
                  f"{previous['label'] or 'no label'}): won {won or 'nothing'}, lost {lost or 'nothing'}.", ""]
    lines += ["| task | result | s | steps | tools | failed calls | what is wrong |", "|---|---|---|---|---|---|---|"]
    for r in results:
        tools = ", ".join(f"{k} x{v}" for k, v in (r.get("tool_calls") or {}).items())
        wrong = "; ".join((r.get("problems") or []) + (r.get("errors") or [])).replace("\n", " ")[:300]
        lines.append(f"| [{r['id']}]({r['id']}.md) | {'pass' if r['passed'] else '**FAIL**'} | "
                     f"{r.get('seconds', '')} | {r.get('model_steps', '')} | {tools} | "
                     f"{r.get('failed_tool_calls', '')} | {wrong} |")
    return "\n".join(lines) + "\n"


def configured_model() -> str:
    sys.path.insert(0, str(REPO / "extension" / "loopcut"))
    import config  # The module has no bpy imports, so it loads outside Blender.
    return config.load().model


def main() -> int:
    index = task_index()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tasks", nargs="*", help="task ids; default all")
    parser.add_argument("--tag", action="append", default=[], help="only tasks with this tag")
    parser.add_argument("--jobs", type=int, default=2, help="Blenders at once (default 2)")
    parser.add_argument("--timeout", type=float, default=300.0, help="seconds per task")
    parser.add_argument("--label", default="", help="what changed, for the comparison line")
    parser.add_argument("--list", action="store_true", help="list tasks and exit")
    args = parser.parse_args()

    unknown = [t for t in args.tasks if t not in index]
    if unknown:
        sys.exit(f"Unknown task(s): {', '.join(unknown)}. Known: {', '.join(index)}")
    chosen = [t for t in index if (not args.tasks or t in args.tasks)
              and (not args.tag or set(args.tag) & set(index[t]))]
    if args.list:
        for task_id in chosen:
            print(f"{task_id:18} {', '.join(index[task_id])}")
        return 0

    started = time.strftime("%Y%m%d-%H%M%S")
    out = EVALS / started
    out.mkdir(parents=True)
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = {task_id: pool.submit(run_one, task_id, out, args.timeout) for task_id in chosen}
        results = []
        for task_id, future in futures.items():
            result = future.result()
            results.append(result)
            print(f"{'pass' if result['passed'] else 'FAIL'} {task_id} "
                  f"{'; '.join((result.get('problems') or []) + (result.get('errors') or []))[:200]}", flush=True)
    run = {"started": started, "label": args.label, "model": configured_model(),
           "wall_seconds": time.monotonic() - t0, "results": results}
    previous = previous_run(out)
    (out / "results.json").write_text(json.dumps(run, indent=1), encoding="utf-8")
    (out / "summary.md").write_text(summarize(run, previous), encoding="utf-8")
    print(f"\n{sum(r['passed'] for r in results)}/{len(results)} passed. {out / 'summary.md'}")
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
