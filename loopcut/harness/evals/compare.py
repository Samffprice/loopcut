#!/usr/bin/env python3
"""Compare arms that did the same project tasks: Loopcut runs (out/evals/<time>, from run.py) and
manual runs in other tools (a folder from start_scene.py with <task>.final.blend and
<task>.run.json filled in).

    python3 harness/evals/compare.py --arm loopcut=out/evals/20260921-120000 \\
                                     --arm chatgpt=out/projects/chatgpt --judge [task ids]

Every arm's finished scenes are graded and rendered the same way (grade.py, headless Blender),
then written up in out/projects/<time>/comparison.md: requirements met per task, the checklist
side by side, effort (time, prompts, manual fixes, model steps, tokens), the judge's blind scores
when --judge is given, and the renders next to each other. blind.md shows the same renders with
the arms shuffled per task and unnamed, for people to vote on; blind_key.json says which was which.
"""

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import evidence

REPO = Path(__file__).resolve().parents[3]
WORKSPACE = REPO.parent
HERE = Path(__file__).resolve().parent


def blender() -> str:
    path = os.environ.get("LOOPCUT_BLENDER") or str(WORKSPACE / "tools/Blender.app/Contents/MacOS/Blender")
    if not Path(path).is_file():
        sys.exit(f"No Blender at {path}; set LOOPCUT_BLENDER.")
    return path


def grade(task_id: str, final: Path, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    log = out / f"{task_id}.grade.log"
    stage1 = final.with_name(f"{task_id}.stage1.blend")
    command = [blender(), "-b", "--factory-startup", "--python", str(HERE / "grade.py"), "--",
               task_id, str(final), str(out)] + ([str(stage1)] if stage1.is_file() else [])
    with log.open("w", encoding="utf-8") as handle:
        subprocess.run(command, stdout=handle, stderr=subprocess.STDOUT, timeout=1800)
    result_file = out / f"{task_id}.grade.json"
    if not result_file.is_file():
        return {"id": task_id, "requirements": [], "met": 0, "problems": [f"grade.py wrote nothing, see {log.name}"], "renders": []}
    return json.loads(result_file.read_text(encoding="utf-8"))


def effort(arm_dir: Path, task_id: str, run_model: str) -> dict:
    """Time and effort, from run.py's per-task result or the manual run's run.json."""
    live, manual = arm_dir / f"{task_id}.json", arm_dir / f"{task_id}.run.json"
    if live.is_file():
        r = json.loads(live.read_text(encoding="utf-8"))
        usage = r.get("usage") or {}
        return {"tool": "loopcut", "model": run_model, "seconds": r.get("seconds"), "prompts": r.get("prompts", 1),
                "manual_fixes": 0, "steps": r.get("model_steps"), "tokens_in": usage.get("input"),
                "tokens_out": usage.get("output"), "notes": "; ".join(r.get("errors") or [])}
    if manual.is_file():
        r = json.loads(manual.read_text(encoding="utf-8"))
        return {"tool": r.get("tool") or "?", "model": r.get("model") or "?", "seconds": r.get("seconds"),
                "prompts": r.get("prompts_sent"), "manual_fixes": r.get("manual_fixes"), "steps": None,
                "tokens_in": None, "tokens_out": None, "notes": r.get("notes", "")}
    return {"tool": "?", "model": "?", "seconds": None, "prompts": None, "manual_fixes": None, "steps": None,
            "tokens_in": None, "tokens_out": None, "notes": "no run.json"}


def prompt_of(task_id: str, follow_up: bool = False) -> str:
    import ast
    for name in ("tasks.py", "projects.py", "showcase.py"):
        tree = ast.parse((HERE / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and (getattr(node.func, "id", "") == "Task" or getattr(node.func, "attr", "") == "Task") \
                    and node.args and ast.literal_eval(node.args[0]) == task_id:
                if not follow_up:
                    return ast.literal_eval(node.args[1])
                keyword = next((k for k in node.keywords if k.arg == "follow_up"), None)
                return ast.literal_eval(keyword.value) if keyword else ""
    return ""


def cell(value) -> str:
    return "-" if value is None else str(value)


def write_report(out: Path, arms: list[str], tasks: list[str], grades: dict, efforts: dict, judge: dict | None) -> None:
    lines = [f"# Project comparison: {', '.join(arms)}", "",
             "Requirements are objective checks on the saved scene (grade.py); the judge scores renders of "
             "the same frames blind, in both orders. Effort is what it took: seconds, prompts typed, edits "
             "made by hand, model steps, tokens.", ""]
    lines += ["| task | " + " | ".join(arms) + " |", "|---|" + "---|" * len(arms)]
    for task_id in tasks:
        lines.append(f"| {task_id} | " + " | ".join(
            f"{grades[a][task_id]['met']}/{len(grades[a][task_id]['requirements'])}" for a in arms) + " |")
    lines.append("")
    verdicts = {r["id"]: r for r in (judge or {}).get("results", [])}
    for task_id in tasks:
        lines += [f"## {task_id}", "", f"> {prompt_of(task_id)}", ""]
        if prompt_of(task_id, follow_up=True):
            lines += [f"> then: {prompt_of(task_id, follow_up=True)}", ""]
        lines += ["| arm | tool / model | requirements | seconds | prompts | manual fixes | model steps | tokens in / out | notes |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for arm in arms:
            e, g = efforts[arm][task_id], grades[arm][task_id]
            label = f"[{arm}]({e['transcript']})" if e.get("transcript") else arm
            lines.append(f"| {label} | {e['tool']} / {e['model']} | {g['met']}/{len(g['requirements'])} | "
                         f"{cell(e['seconds'])} | {cell(e['prompts'])} | {cell(e['manual_fixes'])} | {cell(e['steps'])} | "
                         f"{cell(e['tokens_in'])} / {cell(e['tokens_out'])} | {e['notes'][:120]} |")
        lines += ["", "| requirement | " + " | ".join(arms) + " |", "|---|" + "---|" * len(arms)]
        requirements = next((grades[a][task_id]["requirements"] for a in arms if grades[a][task_id]["requirements"]), [])
        for requirement in requirements:
            row = []
            for arm in arms:
                failed = next((p for p in grades[arm][task_id]["problems"] if p.startswith(requirement + ":")), None)
                row.append("ok" if failed is None else "**no** " + failed[len(requirement) + 1:].strip().replace("|", "/")[:160])
            lines.append(f"| {requirement} | " + " | ".join(row) + " |")
        for arm in arms:
            for note in grades[arm][task_id].get("notes") or []:
                lines.append(f"| note | {arm}: {note} |" + " |" * (len(arms) - 1))
        lines.append("")
        verdict = verdicts.get(task_id)
        if verdict and "scores" in verdict:
            lines += [f"Judge (`{judge['judge_model']}`, blind, both orders): prefers **{verdict['preferred']}**. "
                      f"{verdict['reasons'][0]}", "",
                      "| axis | " + " | ".join(arms) + " |", "|---|" + "---|" * len(arms)]
            for axis in judge["axes"]:
                lines.append(f"| {axis} | " + " | ".join(cell(verdict["scores"].get(a, {}).get(axis)) for a in arms) + " |")
            lines.append("| mean | " + " | ".join(
                f"{sum(verdict['scores'][a].values()) / len(judge['axes']):.1f}" if a in verdict["scores"] else "-" for a in arms) + " |")
            lines.append("")
        elif verdict:
            lines += [f"Judge: {verdict.get('error')}", ""]
        for key, label in (("stage_renders", "after brief, frame"), ("renders", "final, frame")):
            frames = max((len(grades[a][task_id].get(key) or []) for a in arms), default=0)
            if frames:
                lines += ["| frame | " + " | ".join(arms) + " |", "|---|" + "---|" * len(arms)]
                for i in range(frames):
                    row = []
                    for arm in arms:
                        renders = grades[arm][task_id].get(key) or []
                        row.append(f"![]({arm}/{renders[i]})" if i < len(renders) else "-")
                    lines.append(f"| {label} {i + 1} | " + " | ".join(row) + " |")
                lines.append("")
        lines += ["Human verdict: ", ""]
    (out / "comparison.md").write_text("\n".join(lines), encoding="utf-8")


def write_blind(out: Path, arms: list[str], tasks: list[str], grades: dict) -> None:
    identity_key, lines = {}, ["# Blind comparison", "", "Same brief, names hidden and order shuffled per task. "
                      "For each task write which column you would rather hand to a client and why; "
                      "blind_key.json says which was which.", ""]
    labels = ["Left", "Right", "Third", "Fourth"][:len(arms)]
    labels += [f"Column{n + 1}" for n in range(4, len(arms))]
    assets = out / "blind_assets"
    assets.mkdir(exist_ok=True)
    for task_id in tasks:
        order = arms[:]
        random.shuffle(order)
        identity_key[task_id] = dict(zip(labels, order))
        lines += [f"## {task_id}", "", f"> {prompt_of(task_id)}", ""]
        if prompt_of(task_id, follow_up=True):
            lines += [f"> then: {prompt_of(task_id, follow_up=True)}", ""]
        lines += ["| frame | " + " | ".join(labels) + " |", "|---|" + "---|" * len(arms)]
        for render_key in ("stage_renders", "renders"):
            frames = max(len(grades[a][task_id].get(render_key) or []) for a in arms)
            for i in range(frames):
                row = []
                for label, arm in zip(labels, order):
                    renders = grades[arm][task_id].get(render_key) or []
                    if i < len(renders):
                        name = f"{task_id}.{label.lower()}.{render_key}.{i + 1}.png"
                        shutil.copyfile(out / arm / renders[i], assets / name)
                        row.append(f"![](blind_assets/{name})")
                    else:
                        row.append("-")
                stage = "stage 1" if render_key == "stage_renders" else "final"
                lines.append(f"| {stage} {i + 1} | " + " | ".join(row) + " |")
        lines += ["", "Prefer: ", ""]
    (out / "blind.md").write_text("\n".join(lines), encoding="utf-8")
    (out / "blind_key.json").write_text(json.dumps(identity_key, indent=1), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--arm", action="append", required=True, help="name=dir, at least one")
    parser.add_argument("--judge", action="store_true", help="also ask the judge model (two arms)")
    parser.add_argument("--allow-unversioned", action="store_true", help="allow clearly labelled historical regrades without manifests")
    parser.add_argument("--out", default="", help="report folder; default out/projects/<time>")
    parser.add_argument("tasks", nargs="*", help="task ids; default every task both arms finished")
    args = parser.parse_args()
    arm_dirs = {}
    for spec in args.arm:
        name, _, folder = spec.partition("=")
        arm_dirs[name] = Path(folder).resolve()
    out = Path(args.out).resolve() if args.out else WORKSPACE / "out" / "projects" / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)

    finished = None
    for folder in arm_dirs.values():
        have = {p.name[:-len(".final.blend")] for p in folder.glob("*.final.blend")}
        finished = have if finished is None else finished & have
    tasks = sorted(t for t in (finished or set()) if not args.tasks or t in args.tasks)
    if not tasks:
        sys.exit("no task has a <task>.final.blend in every arm")

    warnings = {}
    for name, folder in arm_dirs.items():
        for task_id in tasks:
            task = SimpleNamespace(id=task_id, prompt=prompt_of(task_id), follow_up=prompt_of(task_id, follow_up=True))
            try:
                warnings[name, task_id] = evidence.validate_arm(folder, task, args.allow_unversioned)
            except ValueError as error:
                parser.error(str(error))

    grades, efforts = {}, {}
    for name, folder in arm_dirs.items():
        run_model = ""
        if (folder / "results.json").is_file():
            run_model = json.loads((folder / "results.json").read_text(encoding="utf-8")).get("model", "")
        grades[name], efforts[name] = {}, {}
        for task_id in tasks:
            grades[name][task_id] = grade(task_id, folder / f"{task_id}.final.blend", out / name)
            grades[name][task_id]["prompt"] = prompt_of(task_id)
            grades[name][task_id]["follow_up"] = prompt_of(task_id, follow_up=True)
            (out / name / f"{task_id}.grade.json").write_text(json.dumps(grades[name][task_id], indent=1), encoding="utf-8")
            efforts[name][task_id] = effort(folder, task_id, run_model)
            if warnings[name, task_id]:
                efforts[name][task_id]["notes"] = warnings[name, task_id] + "; " + efforts[name][task_id]["notes"]
            for suffix in (".md", ".transcript.md"):  # run.py's transcript, or the other tool's own log
                if (folder / f"{task_id}{suffix}").is_file():
                    shutil.copy(folder / f"{task_id}{suffix}", out / name / f"{task_id}.transcript.md")
                    efforts[name][task_id]["transcript"] = f"{name}/{task_id}.transcript.md"
            g = grades[name][task_id]
            print(f"{name:10} {task_id:18} {g['met']}/{len(g['requirements'])}", flush=True)

    judge = None
    if args.judge:
        if len(arm_dirs) != 2:
            sys.exit("--judge compares exactly two arms")
        names = list(arm_dirs)
        subprocess.run([sys.executable, str(HERE / "judge.py"), str(out / names[0]), str(out / names[1]),
                        "--names", *names, "--out", str(out / "judge.json"), *tasks], check=False)
        if (out / "judge.json").is_file():
            judge = json.loads((out / "judge.json").read_text(encoding="utf-8"))
    write_report(out, list(arm_dirs), tasks, grades, efforts, judge)
    write_blind(out, list(arm_dirs), tasks, grades)
    print(f"\n{out / 'comparison.md'}\n{out / 'blind.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
