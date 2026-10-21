"""Conversion cards: what Loopcut's account adds, said at the moment it matters, and no more often than that.

loopcut-web docs/platform/onboarding-value-and-conversion.md §4 is the spec. The cards here:
- locked_tool (M1): the user asked for what a live cloud tool does and the tool is locked (signed out, no
  credits, wrong plan). The model calls loopcut_cloud.offer_tool; the add-on draws the card, never the model.
- chatgpt (M2): the ChatGPT plan stopped (usage limit, or an account that can't share its plan); carry on
  with Loopcut's models. Blocking, so never capped.
- trial (M3) and low_credits (M5): noticed on a manifest refresh, shown at the end of the next turn.

Rules that bind every card:
- Purchase cards show only while the server sells (config.billing_open()).
- A tool that No generation or Private mode blocks is never offered (cloud.locked_tools leaves it out).
- Soft cards (trial, low_credits) wait for the end of a turn, so never interrupt a running one or come before
  the first reply. At most one soft card a day; "Not now" puts that kind off for a week; the ⚙ menu's
  "Tips about Loopcut tools" switches all of them off. Blocking cards and receipts are not tips.
- locked_tool: once per tool per conversation and at most 3 a week.
State lives in <data root>/nudges.json: timestamps and keys only, never content.
"""

import json
import threading
import time

from . import config, state

DAY = 24 * 3600
WEEK = 7 * DAY
LOCKED_PER_WEEK = 3
SOFT = ("trial", "low_credits")       # Waits for the end of a turn; one a day; the tips switch silences it.
LOW_FLOOR = 25                        # Trial and pack balances: warn below this many credits.
LOW_SHARE = 0.2                       # Monthly balances: warn below this share of the monthly grant.
DISMISSED = "Not now. This waits a week."  # What a card says once "Not now" was clicked.
CHATGPT_CODES = ("subscription_sharing_usage_limit_exceeded", "subscription_sharing_user_not_eligible")

_lock = threading.Lock()
_pending: list[tuple[dict, str, str]] = []  # (card, kind, key) noticed during a refresh, for the next turn's end.


def dollars(credits) -> str:
    """1 credit = $0.01; credits are always shown beside dollars."""
    return f"${credits / 100:,.2f}"


# --- Storage ------------------------------------------------------------------------------------------------

def _path():
    from . import checkpoints
    return checkpoints.data_root() / "nudges.json"


def _load() -> dict:
    data = {"shown": {}, "once": [], "snoozed": {}, "tips_off": False}
    try:
        loaded = json.loads(_path().read_text(encoding="utf-8"))
    except Exception:  # Missing, unreadable, or no data folder yet (bpy not ready): start from nothing.
        return data
    if isinstance(loaded, dict):
        if isinstance(loaded.get("shown"), dict):
            data["shown"] = {k: [t for t in v if isinstance(t, (int, float))]
                             for k, v in loaded["shown"].items() if isinstance(v, list)}
        if isinstance(loaded.get("once"), list):
            data["once"] = [k for k in loaded["once"] if isinstance(k, str)][-200:]
        if isinstance(loaded.get("snoozed"), dict):
            data["snoozed"] = {k: v for k, v in loaded["snoozed"].items() if isinstance(v, (int, float))}
        data["tips_off"] = loaded.get("tips_off") is True
    return data


def _save(data: dict) -> None:
    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(data), encoding="utf-8")
        temporary.replace(path)
    except Exception as ex:  # The limits are a courtesy; never a reason for a draw or a turn to fail.
        print(f"Loopcut: could not save card limits: {ex!r}")


def tips_off() -> bool:
    with _lock:
        return _load()["tips_off"]


def set_tips_off(value: bool) -> None:
    with _lock:
        data = _load()
        data["tips_off"] = bool(value)
        _save(data)


def snooze(kind: str, now: float | None = None) -> None:
    """"Not now": this kind of card waits a week."""
    with _lock:
        data = _load()
        data["snoozed"][kind] = (now or time.time()) + WEEK
        _save(data)


def may_show(kind: str, key: str = "", now: float | None = None) -> bool:
    now = now or time.time()
    with _lock:
        data = _load()
    if data["snoozed"].get(kind, 0) > now:
        return False
    if kind in SOFT:
        if data["tips_off"] or f"{kind}:{key}" in data["once"]:
            return False
        recent = [t for k in SOFT for t in data["shown"].get(k, []) if now - t < DAY]
        return not recent
    if kind == "locked_tool":
        if data["tips_off"]:
            return False
        return len([t for t in data["shown"].get(kind, []) if now - t < WEEK]) < LOCKED_PER_WEEK
    return True


def mark_shown(kind: str, key: str = "", now: float | None = None) -> None:
    now = now or time.time()
    with _lock:
        data = _load()
        data["shown"][kind] = [t for t in data["shown"].get(kind, []) if now - t < WEEK] + [now]
        if kind in SOFT and key:
            data["once"] = (data["once"] + [f"{kind}:{key}"])[-200:]
        _save(data)


# --- Cards --------------------------------------------------------------------------------------------------

def _action(label: str, do: str, arg: str = "", when: str = "") -> dict:
    """do: sign_in_loopcut | sign_in_chatgpt | web (arg: URL) | continue_loopcut | manage_chatgpt | own_key |
    vendor_keys | prompt (arg: text for the input) | dismiss. when: "" | loopcut_in | loopcut_out (drawn only
    then, so a card answers a sign-in that happens while it shows)."""
    return {"label": label, "do": do, "arg": arg, "when": when}


def _not_now() -> dict:
    return _action("Not now", "dismiss")


def locked_tool_card(tool: dict, balance=None) -> dict:
    """M1. tool: cloud.locked_tools() row {name, title, why: sign_in|credits|plan, credits, own_key}."""
    title, why, cost = tool["title"], tool["why"], tool.get("credits")
    price = f"This uses about {cost:g} credits ({dollars(cost)})." if isinstance(cost, (int, float)) and cost else ""
    own_key = [_action("Use my own key", "vendor_keys")] if tool.get("own_key") else []
    if why == "sign_in":
        return state.item_offer("locked_tool", f"{title} needs a free Loopcut account",
                                " ".join(filter(None, ["New accounts get trial credits.", price])),
                                [_action("Create free account", "sign_in_loopcut", when="loopcut_out")]
                                + own_key + [_not_now()])
    if why == "plan":
        return state.item_offer("locked_tool", f"{title} needs a paid plan", "The Pro and Max plans include it.",
                                [_action("See plans ↗", "web", config.pricing_url("locked_tool", "pro")), _not_now()])
    have = f" You have {balance:g}." if isinstance(balance, (int, float)) else ""
    return state.item_offer("locked_tool", f"Not enough credits for {title}", (price + have).strip(),
                            [_action("Get credits ↗", "web", config.credits_url("locked_tool")),
                             _action("See plans ↗", "web", config.pricing_url("locked_tool", "pro"))]
                            + own_key + [_not_now()])


def model_note(tool: dict) -> str:
    """What the model is told after offer_tool, so its reply matches the card instead of repeating it."""
    need = {"sign_in": "a free Loopcut account", "plan": "a paid Loopcut plan"}.get(tool["why"], "Loopcut credits")
    return f"{tool['title']} needs {need}."


def offer_locked(session: dict, tool: dict, balance=None) -> str:
    """Main thread. Shows the M1 card unless this conversation already had it or the week's cap is used.
    Returns the tool result for the model."""
    offered = session.setdefault("offered_tools", [])
    note = model_note(tool)
    if tool["name"] in offered or not may_show("locked_tool"):
        return f"Not shown again. {note} Say so in one sentence and carry on with what you can do."
    offered.append(tool["name"])
    mark_shown("locked_tool")
    session["items"].append(locked_tool_card(tool, balance))
    return (f"The user now sees a card with the way to unlock it. {note} Mention it in one short sentence; "
            "do not repeat the card or push it.")


def chatgpt_card(code: str) -> dict | None:
    """M2: the ChatGPT plan stopped this turn. Loopcut's models can carry the same conversation on."""
    keep_going = [_action("Continue on Loopcut Fast", "continue_loopcut", when="loopcut_in"),
                  _action("Sign in to Loopcut (free)", "sign_in_loopcut", when="loopcut_out")]
    if code == "subscription_sharing_usage_limit_exceeded":
        return state.item_offer(
            "chatgpt", "Your ChatGPT plan's limit for Loopcut is reached",
            "New requests wait until your plan resets. You can carry on with Loopcut's models; from here this "
            "chat goes through Loopcut's server.",
            keep_going + [_action("Manage ChatGPT usage ↗", "manage_chatgpt")])
    if code == "subscription_sharing_user_not_eligible":
        return state.item_offer(
            "chatgpt", "This ChatGPT account can't share its plan",
            "OpenAI decides that by plan, workspace and country. Loopcut's Fast model is free with an account.",
            keep_going + [_action("Use my own key", "own_key")])
    return None


TRIAL_CHIPS = {  # Live tool → (chip, the request it writes into the input; nothing runs until the user sends it).
    "sfx": ("Sound effect", "Make a short sound effect that fits this shot."),
    "voice_line": ("Voice line", "Voice a short scratch line of dialogue for my animatic."),
    "image_to_3d": ("Picture to 3D", "Turn a reference picture into a 3D model in this scene."),
    "image_generate": ("Reference image", "Generate a reference image for this scene."),
    "retopology": ("Retopology", "Retopologize the selected mesh."),
    "render": ("Cloud render", "Render this scene on Loopcut's GPUs."),
    "motion_from_text": ("Text to motion", "Animate my character walking forward and waving."),
    "motion_from_video": ("Motion from video", "Capture the movement in my video and put it on my character."),
}


def trial_card(amount, live: list[str]) -> dict:
    """M3. live: names of the managed tools the account can run now (already without blocked ones)."""
    chips = [_action(TRIAL_CHIPS[name][0], "prompt", TRIAL_CHIPS[name][1]) for name in live if name in TRIAL_CHIPS][:3]
    text = "Try a cloud tool:" if chips else "They pay for Loopcut's cloud tools as they open."
    return state.item_offer("trial", f"{amount:g} trial credits added ({dollars(amount)})", text,
                            chips + [_action("What credits buy ↗", "web", config.pricing_url("trial"))])


def low_credits_card(balance, monthly, renews: str) -> dict:
    """M5."""
    if monthly:
        text = f"Your monthly credits renew {renews}." if renews else "Your monthly credits renew each month."
    else:
        text = "The Pro plan adds 2,200 credits every month: $22 of tools for $20."
    return state.item_offer("low_credits", f"{balance:,g} credits left ({dollars(balance)})", text,
                            [_action("Add credits ↗", "web", config.credits_url("low_credits")),
                             _action("See plans ↗", "web", config.pricing_url("low_credits", "pro")), _not_now()])


def observe(manifest: dict, signed_in: bool, live: list[str]) -> None:
    """After a manifest refresh (any thread): queue the trial and low-credit cards it calls for."""
    if not signed_in or not config.billing_open():
        return
    credits = (manifest.get("account") or {}).get("credits") or {}
    balance, monthly = credits.get("balance"), credits.get("monthly")
    trial = credits.get("trial")
    if isinstance(trial, dict) and type(trial.get("amount")) in (int, float) and trial["amount"] > 0:
        key = str(trial.get("granted_at") or trial["amount"])
        _queue(trial_card(trial["amount"], live), "trial", key)
    if type(balance) not in (int, float):
        return
    monthly = monthly if type(monthly) in (int, float) and monthly > 0 else 0
    low = balance < monthly * LOW_SHARE if monthly else 0 < balance < LOW_FLOOR
    if low:
        renews = str(credits.get("resets_at") or "")[:10]
        _queue(low_credits_card(balance, monthly, renews), "low_credits", f"{renews or 'balance'}:{monthly}")


def _queue(card: dict, kind: str, key: str) -> None:
    with _lock:
        if not any(k == kind and q == key for _, k, q in _pending):
            _pending.append((card, kind, key))


def flush(session: dict) -> None:
    """End of a turn (agent thread): show at most one waiting soft card, if the caps allow it now."""
    if not any(item.get("kind") == "assistant" for item in session["items"]):
        return  # Never before the first reply.
    with _lock:
        waiting = list(_pending)
        _pending.clear()
    # The rest are dropped: the next refresh queues again whatever still applies (the balance is still
    # low, the trial is still in the manifest), and "once" keeps a seen card from coming back.
    for card, kind, key in waiting:
        if may_show(kind, key):
            mark_shown(kind, key)
            session["items"].append(card)
            return
