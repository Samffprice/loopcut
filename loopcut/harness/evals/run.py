#!/usr/bin/env python3
"""Run the eval suite against the configured model and report pass rate, steps and time.

    python3 harness/evals/run.py                 # every task
    python3 harness/evals/run.py table camera    # some tasks
    python3 harness/evals/run.py --tag spatial --jobs 3 --label "bounds in scene info"
    python3 harness/evals/run.py perfume_ad --jobs 1 --record    # plus a screen recording, for demos

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
import signal
import subprocess
import sys
import time
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import evidence

REPO = Path(__file__).resolve().parents[3]   # The repository; see harness/checkout.py for the layout.
WORKSPACE = REPO.parent
EVALS = WORKSPACE / "out" / "evals"


def task_index() -> dict[str, list[str]]:
    """Task ids and tags, read from tasks.py and projects.py without importing them (they need bpy)."""
    index = {}
    TIMEOUTS.clear()
    for name in ("tasks.py", "projects.py", "showcase.py"):
        tree = ast.parse((Path(__file__).parent / name).read_text(encoding="utf-8"))
        index.update(_task_calls(tree))
    return index


TIMEOUTS: dict[str, float] = {}  # Per-task overrides from Task(timeout=...), filled by task_index.


def _task_calls(tree) -> dict[str, list[str]]:
    index = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and (getattr(node.func, "id", "") == "Task"
                                           or getattr(node.func, "attr", "") == "Task") and node.args:
            tags = next((ast.literal_eval(k.value) for k in node.keywords if k.arg == "tags"), [])
            index[ast.literal_eval(node.args[0])] = tags
            timeout = next((ast.literal_eval(k.value) for k in node.keywords if k.arg == "timeout"), 0)
            if timeout:
                TIMEOUTS[ast.literal_eval(node.args[0])] = float(timeout)
    return index


def blender() -> str:
    path = os.environ.get("LOOPCUT_BLENDER") or str(WORKSPACE / "tools/Blender.app/Contents/MacOS/Blender")
    if not Path(path).is_file():
        sys.exit(f"No Blender at {path}; set LOOPCUT_BLENDER.")
    return path


def run_one(task_id: str, out: Path, timeout: float, record: bool = False, approve_heavy: bool = False) -> dict:
    command = [blender(), "--factory-startup", "--python", str(Path(__file__).parent / "run_task.py"),
               "--", task_id, str(out)]
    env = {**os.environ, "LOOPCUT_EVAL_TIMEOUT": str(timeout),
           "LOOPCUT_EVAL_APPROVE_HEAVY": "1" if approve_heavy else "0", "LOOPCUT_UPDATE_URL": "off"}
    log = out / f"{task_id}.log"
    recorder = None
    if record:  # macOS screen recording of the whole run; needs Screen Recording permission for the terminal.
        recorder = subprocess.Popen(["screencapture", "-v", "-x", str(out / f"{task_id}.mov")],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        with tempfile.TemporaryDirectory(prefix=f".{task_id}-", dir=out) as runtime:
            env.update(TMPDIR=runtime, LOOPCUT_DATA_DIR=str(Path(runtime) / "data"))
            with log.open("w", encoding="utf-8") as handle:
                process = subprocess.run(command, stdout=handle, stderr=subprocess.STDOUT, env=env,
                                         timeout=2 * timeout + 60)
    except subprocess.TimeoutExpired:
        return {"id": task_id, "passed": False, "problems": ["harness: Blender did not exit"]}
    finally:
        if recorder is not None:
            recorder.send_signal(signal.SIGINT)
            try:
                recorder.wait(timeout=30)
            except subprocess.TimeoutExpired:
                recorder.kill()
                recorder.wait()
    if process.returncode:
        return {"id": task_id, "passed": False,
                "problems": [f"harness: Blender exited with {process.returncode}, see {log.name}"]}
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
        old = {r["id"]: r for r in previous["results"]}
        then = {r["id"]: old[r["id"]]["passed"] for r in results
                if r["id"] in old and evidence.comparable(r, old[r["id"]])}
        excluded = [r["id"] for r in results if r["id"] in old and r["id"] not in then]
        won = [r["id"] for r in results if r["passed"] and then.get(r["id"]) is False]
        lost = [r["id"] for r in results if not r["passed"] and then.get(r["id"]) is True]
        lines += [f"Against the previous run ({previous['started']}, `{previous['model']}`, "
                  f"{previous['label'] or 'no label'}): won {won or 'nothing'}, lost {lost or 'nothing'}.", ""]
        if excluded:
            lines += [f"Not comparable (changed or unrecorded task contract): {', '.join(excluded)}.", ""]
    lines += ["| task | result | s | steps | tools | failed calls | what is wrong |", "|---|---|---|---|---|---|---|"]
    for r in results:
        tools = ", ".join(f"{k} x{v}" for k, v in (r.get("tool_calls") or {}).items())
        wrong = "; ".join((r.get("problems") or []) + [f"then: {p}" for p in r.get("follow_up_problems") or []]
                          + (r.get("errors") or [])).replace("\n", " ")[:300]
        lines.append(f"| [{r['id']}]({r['id']}.md) | {'pass' if r['passed'] else '**FAIL**'} | "
                     f"{r.get('seconds', '')} | {r.get('model_steps', '')} | {tools} | "
                     f"{r.get('failed_tool_calls', '')} | {wrong} |")
    return "\n".join(lines) + "\n"


def configured_model() -> str:
    sys.path.insert(0, str(REPO / "scripts" / "addons_core" / "loopcut"))
    import config  # The module has no bpy imports, so it loads outside Blender.
    return config.load().model


def main() -> int:
    index = task_index()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tasks", nargs="*", help="task ids; default all")
    parser.add_argument("--tag", action="append", default=[], help="only tasks with this tag")
    parser.add_argument("--jobs", type=int, default=1, help="Blenders at once (default 1 for stable GPU timing)")
    parser.add_argument("--timeout", type=float, help="override seconds per brief, including task-specific limits")
    parser.add_argument("--approve-heavy", action="store_true", help="allow renders/bakes in disposable evals")
    parser.add_argument("--label", default="", help="what changed, for the comparison line")
    parser.add_argument("--list", action="store_true", help="list tasks and exit")
    parser.add_argument("--record", action="store_true", help="screen-record each task to <task>.mov (macOS)")
    args = parser.parse_args()
    if args.timeout is not None and (not 0 < args.timeout < float("inf")):
        parser.error("--timeout must be a positive finite number")
    if args.record and args.jobs != 1:
        parser.error("--record requires --jobs 1 so recordings do not mix task windows")

    unknown = [t for t in args.tasks if t not in index]
    if unknown:
        sys.exit(f"Unknown task(s): {', '.join(unknown)}. Known: {', '.join(index)}")
    chosen = [t for t in index if (not args.tasks or t in args.tasks)
              and (not args.tag or set(args.tag) & set(index[t]))]
    if args.list:
        for task_id in chosen:
            print(f"{task_id:18} {', '.join(index[task_id])}")
        return 0
    if not chosen:
        parser.error("no tasks match the selected tags")

    started = time.strftime("%Y%m%d-%H%M%S")
    out = EVALS / started
    out.mkdir(parents=True)
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = {task_id: pool.submit(run_one, task_id, out,
                                       args.timeout if args.timeout is not None else TIMEOUTS.get(task_id, 300),
                                       args.record, args.approve_heavy)
                   for task_id in chosen}
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
