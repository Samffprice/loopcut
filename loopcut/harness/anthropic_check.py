"""The Anthropic provider against the real API, outside Blender: a few tiny calls through the add-on's own
anthropic_messages.py, then (with --compat) one through the Custom provider's chat-completions path to
Anthropic's OpenAI-compatible endpoint, for comparison. Costs a few cents.

    python3 loopcut/harness/anthropic_check.py [--model claude-opus-5-5] [--compat]
    python3 loopcut/harness/anthropic_check.py --env-file ../loopcut-web-platform/.env.local --key-name VENDOR_ANTHROPIC_API_KEY

The key comes from ANTHROPIC_API_KEY, or from the --key-name line of --env-file; it is read in-process and
never printed. Checks, each printed as PASS or FAIL with its usage:
1. a tool call (a ~1.5k-token system prompt so the prefix is cacheable), with a cache write;
2. the tool's result sent back with the reply's thinking blocks, read back from the cache;
3. the same history with no thinking blocks (as after a restart, or a folded step): still accepted;
4. --compat: the same first request through the Custom provider (chat completions); Anthropic says that
   endpoint has no prompt caching.
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

from loopcut import anthropic_messages, config, llm, pricing  # noqa: E402

TOOLS = [{"type": "function", "function": {
    "name": "add_object", "description": "Add a primitive object to the user's Blender scene.",
    "parameters": {"type": "object", "properties": {
        "kind": {"type": "string", "enum": ["cube", "sphere", "cylinder"]},
        "name": {"type": "string", "description": "The new object's name"}}, "required": ["kind", "name"]}}}]
# Deterministic padding past the 512-token minimum a prefix needs to be cached.
SYSTEM = "You are Loopcut, an assistant inside Blender. Use the tools to change the scene; be brief.\n\n" + "\n".join(
    f"Rule {n}: keep names short, keep objects at the origin unless told otherwise, and say what you did." for n in range(60))


def _key(args) -> str:
    if args.env_file:
        for line in Path(args.env_file).read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == args.key_name:
                return value.strip().strip("'\"")
        return ""
    return os.environ.get("ANTHROPIC_API_KEY", "").strip()


def _line(label: str, ok: bool, completion, model: str, note: str = "") -> float:
    usage = completion.usage or {}
    cost = pricing.usd(model, usage) or 0.0
    print(f"{'PASS' if ok else 'FAIL'}  {label}: finish={completion.finish_reason} calls={[c.name for c in completion.tool_calls]} "
          f"usage={usage} ${cost:.4f}{'  ' + note if note else ''}")
    return cost


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--model", default=config.ANTHROPIC_MODEL)
    parser.add_argument("--effort", default="low")
    parser.add_argument("--env-file")
    parser.add_argument("--key-name", default="ANTHROPIC_API_KEY")
    parser.add_argument("--compat", action="store_true")
    args = parser.parse_args()
    key = _key(args)
    if not key:
        print("SKIPPED  no Anthropic key (ANTHROPIC_API_KEY, or --env-file with --key-name)")
        return 0

    def call(messages, base_url=config.ANTHROPIC_BASE_URL):
        return llm.stream_chat(base_url=base_url, api_key=key, model=args.model, messages=messages, tools=TOOLS,
                               reasoning_effort=args.effort, on_text=lambda _: None, is_cancelled=lambda: False,
                               max_tokens=4000)

    start = [{"role": "system", "content": SYSTEM},
             {"role": "user", "content": "Add a cube named Box with the add_object tool, then tell me it is done."}]
    spent, failed = 0.0, 0
    first = call(start)
    ok = bool(first.tool_calls) and (first.usage or {}).get("cache_write", 0) + (first.usage or {}).get("cached", 0) > 0
    spent += _line("1 tool call, prefix cached", ok, first, args.model)
    failed += not ok
    if not first.tool_calls:
        return 1
    history = start + [first.as_message()] + [
        {"role": "tool", "tool_call_id": c.id, "content": "Added Box at the origin."} for c in first.tool_calls]
    second = call(history)
    ok = not second.tool_calls and (second.usage or {}).get("cached", 0) > 0
    spent += _line("2 result back with thinking, cache read", ok, second, args.model, repr(second.text[:80]))
    failed += not ok
    anthropic_messages.replay.clear()   # As after a restart: the same history, no thinking blocks to replay.
    try:
        third = call(history)
        spent += _line("3 the same history without thinking blocks", True, third, args.model, repr(third.text[:80]))
    except llm.LLMError as ex:
        failed += 1
        print(f"FAIL  3 the same history without thinking blocks: {ex}")
    if args.compat:
        compat_model = args.model
        try:
            fourth = llm.stream_chat(base_url="https://api.anthropic.com/v1", api_key=key, model=compat_model,
                                     messages=start, tools=TOOLS, reasoning_effort=args.effort, on_text=lambda _: None,
                                     is_cancelled=lambda: False, max_tokens=4000)
            spent += _line("4 Custom: OpenAI-compatible endpoint", bool(fourth.tool_calls), fourth, compat_model,
                           "cached tokens reported: " + str((fourth.usage or {}).get("cached", 0)))
        except llm.LLMError as ex:
            failed += 1
            print(f"FAIL  4 Custom: OpenAI-compatible endpoint: {ex}")
    print(f"Spent about ${spent:.4f} on {args.model}.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
