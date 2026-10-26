"""Flag off == 0.2.14. What an account outside the credits experiment sees, taken from the 0.2.14 add-on (its credits
package as it shipped, feat/credits-package 75f6845c1ca) and compared by test_chat_credits with this add-on: no
account.billing, no chat_credits or refill, no upgrade.times or builds. flag_off_snapshot's panel and setup scenes,
plus the surfaces 0.2.15 changes: a ChatGPT chat's and a Loopcut chat's footer, the conversion cards a manifest
refresh queues (trial, low credits), and the card a refusal builds.

To write the fixture again from another add-on folder (0.2.14's, from git archive 75f6845c1ca):

    python3 loopcut/tests/flag_off_0_2_14.py <folder holding loopcut/__init__.py> loopcut/tests/fixtures/flag_off_0.2.14.json

Deterministic, like flag_off_snapshot: a measure by character count, date-only resets, no network, no Blender.
"""

import importlib
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import flag_off_snapshot as base  # noqa: E402

FIXTURE = base.HERE / "fixtures" / "flag_off_0.2.14.json"


def extra(folder: Path = base.PACKAGE, name: str = "_flag_off_0214") -> dict:
    base._load(folder, name)
    layout = importlib.import_module(name + ".ui.layout")
    state = importlib.import_module(name + ".state")
    nudges = importlib.import_module(name + ".nudges")
    llm = importlib.import_module(name + ".llm")
    reset = "2099-10-21"
    account = {"plan": "free", "session": {"used": 0.3, "resets_at": reset}, "week": {"used": 0.2, "resets_at": reset}}
    shots = {}
    for provider, plan in (("CHATGPT", True), ("LOOPCUT", False)):
        session = state.new_session()
        session["id"] = "e" * 32
        reply = state.item_assistant()
        reply.update(text="Added brass key caps.", streaming=False)
        session["items"] = [state.item_user("make more details"), reply]
        session["spend"] = {"credits": 12, "auto": 12, "allowed": 0}
        ui = {"selection": [], "context_budget": 24000, "account": account, "credits": "94 credits ($0.94)",
              "credit_balance": 94, "today": "Today: 6 credits", "now": 0.0, "chatgpt_plan": plan,
              "provider": provider}
        with patch.object(layout.config, "billing_open", return_value=True):
            for width in (420, 240):
                display = layout.build(session, width, 800, 1.0, base.measure, "fast", None, ui)
                shots[f"reply_{provider.lower()}_{width}"] = {"prims": display["prims"], "hits": display["hits"]}
    cards = {}
    granted = 1792144800.0  # 2026-10-16T10:00:00Z. An hour later and still whole, the trial card shows in 0.2.14 and
    # since 0.2.19 alike; 0.2.19 drops it for an older or spent grant (test_nudges).
    for label, credits in (("trial_and_low", {"balance": 100, "monthly": 1000, "resets_at": reset,
                                              "trial": {"amount": 100, "granted_at": "2026-10-16T10:00:00Z"}}),
                           ("trial_pack", {"balance": 24}), ("plenty", {"balance": 90, "monthly": 100})):
        with nudges._lock:
            nudges._pending.clear()
        with patch.object(nudges.config, "billing_open", return_value=True), \
                patch.object(nudges.time, "time", return_value=granted + 3600):
            nudges.observe({"account": {"plan": "free", "credits": credits}}, True, ["sfx", "image_to_3d"])
        with nudges._lock:
            cards[label] = [[kind, key, card] for card, kind, key in nudges._pending]
            nudges._pending.clear()
    refusals = {}
    upgrade = {"url": "https://loopcut.org/pricing", "plan": "pro", "label": "Upgrade to Pro", "note": "Pro: more."}
    for code in ("session_limit", "week_limit", "free_daily_limit"):
        error = llm.LLMError("Chat allowance used.", 402, code, {"message": "Chat allowance used.", "plan": "free",
                                                                 "resets_at": reset, "upgrade": upgrade})
        try:
            agent = importlib.import_module(name + ".agent")
            refusals[code] = agent._limit_item(error)
        except ImportError as ex:  # Never expected: the add-on imports without bpy for its tests.
            refusals[code] = f"import failed: {ex}"
    return base._plain({"panel": shots, "nudges": cards, "refusals": refusals}, base._version(name))


def snapshot(folder: Path = base.PACKAGE) -> dict:
    return {**base.snapshot(folder), "extra": extra(folder)}


if __name__ == "__main__":
    folder, out = Path(sys.argv[1]), Path(sys.argv[2])
    out.write_text(json.dumps(snapshot(folder), indent=0, sort_keys=True))
    print(f"wrote {out}")
