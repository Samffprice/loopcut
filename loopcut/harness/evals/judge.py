#!/usr/bin/env python3
"""Blind pairwise judgement of two graded arms, from their renders, by a vision model:
    python3 harness/evals/judge.py <graded_a> <graded_b> --out judge.json [--names loopcut chatgpt] [task ids]
For each task the judge sees the brief and the same frames of both scenes, scores each on five
axes (1-5) and picks one; it is asked twice with the scenes swapped, and a preference only
counts when both orders agree, so position bias cancels. The judge endpoint comes from
LOOPCUT_JUDGE_BASE_URL / LOOPCUT_JUDGE_API_KEY / LOOPCUT_JUDGE_MODEL (OpenAI-compatible
/chat/completions); without them it falls back to the add-on's own configured model, which is
then also a contestant, and says so. No bpy: plain python3."""

import argparse
import base64
import json
import os
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
AXES = ["brief", "composition", "lighting", "materials", "polish"]

SYSTEM = (
    "You judge 3D scenes made in Blender from the same brief by two different tools. You see the brief "
    "and the same rendered frames from scene 1 and scene 2. Judge only what the renders show. Score each "
    "scene 1-5 on: brief (does it do what was asked), composition (framing, camera, layout), lighting, "
    "materials (surfaces, colors, readability), polish (would a client accept this as a draft). "
    "Then pick the scene a client would rather receive, or tie. Answer with JSON only: "
    '{"scene1": {"brief": n, "composition": n, "lighting": n, "materials": n, "polish": n}, '
    '"scene2": {...}, "preferred": "1" | "2" | "tie", "reason": "one or two sentences"}'
)


def endpoint() -> tuple[str, str, str, str]:
    base, key, model = (os.environ.get(k, "") for k in ("LOOPCUT_JUDGE_BASE_URL", "LOOPCUT_JUDGE_API_KEY", "LOOPCUT_JUDGE_MODEL"))
    if base and key and model:
        return base.rstrip("/"), key, model, ""
    sys.path.insert(0, str(REPO / "scripts" / "addons_core" / "loopcut"))
    import config
    cfg = config.load()
    return cfg.base_url, cfg.api_key, cfg.model, "judge is the add-on's own model (set LOOPCUT_JUDGE_* for an independent one)"


def image_part(path: Path) -> dict:
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}}


def ask(base: str, key: str, model: str, prompt: str, first: list[tuple[str, Path]], second: list[tuple[str, Path]]) -> dict:
    content = [{"type": "text", "text": f"The brief:\n{prompt}\n\nScene 1:"}]
    for label, path in first:
        content += [{"type": "text", "text": f"scene 1, {label}"}, image_part(path)]
    content.append({"type": "text", "text": "Scene 2:"})
    for label, path in second:
        content += [{"type": "text", "text": f"scene 2, {label}"}, image_part(path)]
    # Room for hidden reasoning: a reasoning model that runs out of budget returns no content at all.
    body = json.dumps({"model": model, "temperature": 0, "max_tokens": 6000, "reasoning_effort": "low",
                       "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": content}]})
    request = urllib.request.Request(f"{base}/chat/completions", data=body.encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(request, timeout=180) as response:
        reply = json.loads(response.read().decode("utf-8"))
    choice = reply["choices"][0]
    text = choice["message"].get("content")
    if isinstance(text, list):  # Some endpoints return content parts.
        text = "".join(part.get("text", "") for part in text)
    if not text or "{" not in text:
        raise RuntimeError(f"judge returned no JSON (finish_reason={choice.get('finish_reason')}): {str(text)[:200]}")
    return json.loads(text[text.index("{"):text.rindex("}") + 1])


def load(arm: Path, task_id: str) -> tuple[dict, list[tuple[str, Path]]]:
    grade = json.loads((arm / f"{task_id}.grade.json").read_text(encoding="utf-8"))
    frames = [("after the first brief", arm / name) for name in grade.get("stage_renders") or []]
    final = "after the follow-up" if grade.get("follow_up") else "frame"
    return grade, frames + [(final, arm / name) for name in grade.get("renders") or []]


def judge_task(base, key, model, task_id: str, prompt: str, arms: dict[str, Path]) -> dict:
    (name_a, dir_a), (name_b, dir_b) = arms.items()
    _, frames_a = load(dir_a, task_id)
    _, frames_b = load(dir_b, task_id)
    if not frames_a or not frames_b:
        return {"id": task_id, "error": "an arm has no renders"}
    first = ask(base, key, model, prompt, frames_a, frames_b)     # 1 = a
    second = ask(base, key, model, prompt, frames_b, frames_a)    # 1 = b
    scores = {name_a: {}, name_b: {}}
    for axis in AXES:
        scores[name_a][axis] = (first["scene1"][axis] + second["scene2"][axis]) / 2
        scores[name_b][axis] = (first["scene2"][axis] + second["scene1"][axis]) / 2
    pick = {"1": name_a, "2": name_b, "tie": "tie"}.get(str(first.get("preferred")), "tie")
    pick_swapped = {"1": name_b, "2": name_a, "tie": "tie"}.get(str(second.get("preferred")), "tie")
    preferred = pick if pick == pick_swapped else "unclear (order-dependent)"
    return {"id": task_id, "scores": scores, "preferred": preferred,
            "orders": [pick, pick_swapped], "reasons": [first.get("reason", ""), second.get("reason", "")]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("a")
    parser.add_argument("b")
    parser.add_argument("--names", nargs=2, default=["a", "b"])
    parser.add_argument("--out", required=True)
    parser.add_argument("tasks", nargs="*")
    args = parser.parse_args()
    arms = {args.names[0]: Path(args.a), args.names[1]: Path(args.b)}
    base, key, model, warning = endpoint()
    if warning:
        print(f"warning: {warning}", file=sys.stderr)
    graded = {p.name[:-len(".grade.json")] for p in Path(args.a).glob("*.grade.json")}
    graded &= {p.name[:-len(".grade.json")] for p in Path(args.b).glob("*.grade.json")}
    chosen = [t for t in sorted(graded) if not args.tasks or t in args.tasks]
    results = []
    for task_id in chosen:
        grade = json.loads((Path(args.a) / f"{task_id}.grade.json").read_text(encoding="utf-8"))
        prompt = grade.get("prompt", "")
        if grade.get("follow_up"):
            prompt += f"\n\nThen, as a follow-up on the same scene: {grade['follow_up']}"
        try:
            results.append(judge_task(base, key, model, task_id, prompt, arms))
        except Exception as ex:
            results.append({"id": task_id, "error": f"{type(ex).__name__}: {ex}"})
        print(f"JUDGE {task_id}: {results[-1].get('preferred') or results[-1].get('error')}")
    Path(args.out).write_text(json.dumps({"judge_model": model, "warning": warning, "axes": AXES,
                                          "results": results}, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
