"""M0 probe: does GLM-5.3-Flash (Z.ai direct) share a cached prefix across *fresh* conversations?

Every request carries the same ~7k-token system prompt and the same 10 tool schemas, and a different
short user message, so each one looks like a worker's first call. Phases:

  a) 3 requests fired at the same moment
  b) after a pause, 3 requests staggered 3 s apart
  c) 1 request 30 s after (b)
  d) control: an exact repeat of request a1 (same user message), to see whether caching works at all

A random run nonce opens the system prompt, so every run starts from a cold prefix.
--cold-stagger instead fires 3 requests staggered on a cold prefix, then 1 sequential (4 requests).

Key: UPSTREAM_ZAI_BASE_URL / UPSTREAM_ZAI_API_KEY from loopcut-web/.env.local (or the environment).
The key is never printed.

    python3 loopcut/harness/probes/cache_probe.py [--pause-b 20] [--pause-c 30] [--json out.json]
"""
import argparse
import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[4]           # .../BlenderCursor/loopcut
ENV_FILE = WORKSPACE.parent / "loopcut-web" / ".env.local"
MODEL = "glm-5.3-flash"
EXTRA = {"reasoning_effort": "low"}                        # Same as the gateway row and the research capture tap.
MAX_TOKENS = 24


def read_env(path: Path) -> dict:
    values = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"').strip("'").strip()
    values.update({k: v for k, v in os.environ.items() if k.startswith("UPSTREAM_ZAI")})
    return values


# ------------------------------------------------------------------ the shared prefix

TOOL_NAMES = ["run_python", "scene_digest", "inspect_object", "capture_view", "measure", "set_material",
              "add_primitive", "apply_modifier", "list_assets", "report"]


def tool_schemas() -> list:
    tools = []
    for i, name in enumerate(TOOL_NAMES):
        tools.append({"type": "function", "function": {
            "name": name,
            "description": f"{name.replace('_', ' ').capitalize()} in the worker's private copy of the scene. "
                           f"Returns JSON. Tool {i + 1} of {len(TOOL_NAMES)}; see the system prompt for rules.",
            "parameters": {"type": "object", "properties": {
                "target": {"type": "string", "description": "Object or datablock name; must be owned unless read-only."},
                "args": {"type": "object", "description": "Tool-specific arguments.", "additionalProperties": True},
                "reason": {"type": "string", "description": "One line on why this call is needed."},
            }, "required": ["target"]}}})
    return tools


def system_prompt(nonce: str) -> str:
    parts = [f"Run {nonce}. You are a Loopcut worker: a Blender modelling agent working in a private copy of the "
             "user's scene. You own what you create and what your brief assigns; everything else is read-only.\n"]
    topics = ["units and scale", "naming and prefixes", "ownership", "materials", "modifiers", "parenting",
              "constraints", "lighting", "cameras", "measurement", "captures", "reports"]
    for n in range(1, 43):
        tool = TOOL_NAMES[n % len(TOOL_NAMES)]
        topic = topics[n % len(topics)]
        parts.append(
            f"## Rule {n}: {tool} and {topic}\n"
            f"When you call `{tool}` for {topic}, pass the exact datablock name, never a guess. Check the scene "
            f"digest first; objects listed there with owner != you are read-only. Rule {n} exists because earlier "
            f"workers edited shared data (for example the floor, walls or a palette material) and the merge had "
            f"to throw the change away. Keep dimensions in metres, keep new names under your deliverable prefix, "
            f"and prefer one {tool} call that does the whole step over many small ones. If {topic} is outside "
            f"your role, file a change request (object, change, one-line reason) instead of acting.\n")
    return "\n".join(parts)


USER_MESSAGES = {
    "a1": "Brief: build a wooden chair under Chair_Root near the rug. Reply with one word: ready.",
    "a2": "Brief: build a side table under Table_Root by the lamp. Reply with one word: ready.",
    "a3": "Brief: add a bookshelf under Shelf_Root on the back wall. Reply with one word: ready.",
    "b1": "Brief: model a floor lamp under FloorLamp_Root. Reply with one word: ready.",
    "b2": "Brief: create a coffee mug under Mug_Root on the table. Reply with one word: ready.",
    "b3": "Brief: add a picture frame under Frame_Root above the sofa. Reply with one word: ready.",
    "c1": "Brief: make a potted plant under Plant_Root by the window. Reply with one word: ready.",
}


# ------------------------------------------------------------------ requests

def call(env: dict, system: str, tools: list, user: str) -> dict:
    body = {"model": MODEL, "stream": False, "max_tokens": MAX_TOKENS, "temperature": 0.2,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "tools": tools, **EXTRA}
    req = urllib.request.Request(env["UPSTREAM_ZAI_BASE_URL"].rstrip("/") + "/chat/completions",
                                 data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + env["UPSTREAM_ZAI_API_KEY"]})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read())
            status = r.status
            headers = {k.lower(): v for k, v in r.headers.items() if "limit" in k.lower() or "request-id" in k.lower()}
    except urllib.error.HTTPError as e:
        return {"t0": t0, "latency": time.time() - t0, "status": e.code, "error": e.read().decode()[:300]}
    except Exception as e:  # noqa: BLE001
        return {"t0": t0, "latency": time.time() - t0, "status": None, "error": repr(e)[:300]}
    usage = data.get("usage") or {}
    details = usage.get("prompt_tokens_details") or {}
    cached = details.get("cached_tokens", usage.get("cached_tokens", usage.get("prompt_cache_hit_tokens")))
    return {"t0": t0, "latency": time.time() - t0, "status": status, "prompt_tokens": usage.get("prompt_tokens"),
            "cached_tokens": cached, "completion_tokens": usage.get("completion_tokens"),
            "usage_keys": sorted(usage.keys()), "details_keys": sorted(details.keys()), "rate_headers": headers}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pause-b", type=float, default=20.0, help="seconds between phase a and phase b")
    ap.add_argument("--pause-c", type=float, default=30.0, help="seconds between phase b and phase c")
    ap.add_argument("--no-control", action="store_true", help="skip the exact-repeat control request")
    ap.add_argument("--cold-stagger", action="store_true",
                    help="skip phase a: 3 requests on a COLD prefix staggered --gap s apart, then 1 sequential")
    ap.add_argument("--gap", type=float, default=3.0, help="stagger gap for --cold-stagger")
    ap.add_argument("--json", help="write the raw results here")
    args = ap.parse_args()

    env = read_env(ENV_FILE)
    assert env.get("UPSTREAM_ZAI_BASE_URL") and env.get("UPSTREAM_ZAI_API_KEY"), "UPSTREAM_ZAI_* not found"
    nonce = uuid.uuid4().hex[:10]
    system, tools = system_prompt(nonce), tool_schemas()
    print(f"model={MODEL} nonce={nonce} system_chars={len(system)} tools={len(tools)} max_tokens={MAX_TOKENS}")

    results = {}
    start = time.time()

    def fire(label):
        results[label] = call(env, system, tools, USER_MESSAGES.get(label, USER_MESSAGES["a1"]))

    def staggered(labels, gap):
        threads = []
        for lab in labels:
            t = threading.Thread(target=fire, args=(lab,))
            t.start()
            threads.append(t)
            time.sleep(gap)
        for t in threads:
            t.join()

    if args.cold_stagger:
        # Cold prefix, staggered starts: does request n+1 hit the cache that request n is still building?
        staggered(("b1", "b2", "b3"), args.gap)
        time.sleep(3.0)
        fire("c1")                                  # sequential, after all three finished
    else:
        # a) simultaneous
        staggered(("a1", "a2", "a3"), 0.0)
        time.sleep(args.pause_b)
        # b) staggered 3 s apart (fired without waiting for the previous reply)
        staggered(("b1", "b2", "b3"), 3.0)
        time.sleep(args.pause_c)
        # c) one more
        fire("c1")
        # d) control: exact repeat of a1
        if not args.no_control:
            fire("d_repeat_a1")

    print(f"{'req':<12}{'start_s':>8}{'status':>7}{'prompt':>8}{'cached':>8}{'cached%':>8}{'latency_s':>10}")
    for lab, r in results.items():
        if r.get("error"):
            print(f"{lab:<12}{r['t0'] - start:>8.1f}{str(r['status']):>7}  ERROR {r['error']}")
            continue
        pct = (100.0 * (r["cached_tokens"] or 0) / r["prompt_tokens"]) if r["prompt_tokens"] else 0.0
        print(f"{lab:<12}{r['t0'] - start:>8.1f}{r['status']:>7}{r['prompt_tokens']:>8}{str(r['cached_tokens']):>8}"
              f"{pct:>7.0f}%{r['latency']:>10.2f}")
    first = next((r for r in results.values() if not r.get("error")), {})
    print("usage keys:", first.get("usage_keys"), "details keys:", first.get("details_keys"))
    print("rate-limit-ish headers:", sorted({k for r in results.values() for k in (r.get('rate_headers') or {})}))
    if args.json:
        Path(args.json).write_text(json.dumps({"nonce": nonce, "results": results}, indent=1))


if __name__ == "__main__":
    main()
