"""Loopcut's AI paid from credits: the `credits` arm of the free_mimo_pool experiment (contract v1, 2026-10-23).

The server puts an account in the arm and says so in the manifest's account object:
- account.billing: "credits" (hosted chat comes out of account.credits) or "allowance" (the session and week
  windows, today's rule). Servers before the contract send neither, and then nothing here shows: every caller
  checks mode() or the ui's "billing" first, so an older server or another arm sees 0.2.13's panel exactly.
- account.chat_credits.per_step: what an agent step costs, for the "≈1 credit / step" labels.

On chat, a credits account's replies carry x-loopcut-billing and x-loopcut-credits-balance (the balance before
that request's charge), and an empty balance is HTTP 402 insufficient_credits with the account's reset date and
the upgrade object. This module reads those, works out what each turn cost (the balance before the turn less
the balance after it, which takes in the turn's tool credits too), and holds the copy and pictures of the
out-of-credits pop-up and the setup's "Everything Loopcut does" step. No bpy at import: the layout tests load it.

0.2.15 (contract 0.9, persona tests 2026-10-23/24): the weekly top-up (chat_credits.refill and the 402's refill) is
named when it comes first; upgrade.builds puts offers in builds, and plan credits carry their dollars; a low-credit
heads-up (agent._warn_low_credits) comes once a refill period. Every one of these needs a field only the experiment
sends, so other accounts see 0.2.14 (test_chat_credits.FlagOff0214Test).
"""

import math
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import state

CREDITS, ALLOWANCE = "credits", "allowance"
CODE = "insufficient_credits"  # The 402's error.code on chat (and on cloud tools, cloud_account.error_details).
MEDIA = Path(__file__).parent / "media"
SETTLE_SECONDS = 1.5  # After a turn, before reading the balance: the last request's charge is booked as it ends.
REFRESH_TRIES = 4     # A manifest refresh already running blocks ours; try again this many times, a second apart.
TYPICAL_BUILD = 12    # Credits a typical first build uses, unless the server says (its figure at the 1x markup, 0.2.16).
LOW_BALANCE = 20      # The heads-up before the hard stop (0.2.15): at about this many credits, once a refill period.
# The plans as loopcut.org/pricing sets them (2026-10-23); onboarding.PLANS is this table. When a price or a monthly
# grant changes, change it here. The free plan's 100 a month starts in the month after sign-up (the sign-up month
# has its 100 sign-up credits instead, and account.credits.monthly is 0 then), so copy uses the plan's figure.
PLANS = {"free": {"credits": 100, "price": 0}, "pro": {"credits": 2200, "price": 20},
         "max": {"credits": 6600, "price": 60}}


def _amount(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _n(value) -> str:
    return f"{round(value):,}" if _amount(value) else ""


# --- What the server says ------------------------------------------------------------------------------------

def billing(manifest, signed_in: bool) -> str:
    """"credits", "allowance", or "" when the manifest does not say (older servers, signed out)."""
    account = (manifest or {}).get("account") if signed_in else None
    value = account.get("billing") if isinstance(account, dict) else None
    return value if value in (CREDITS, ALLOWANCE) else ""


def _account() -> tuple[dict, bool]:
    """The manifest's account object and whether the runtime is signed in; ({}, False) without a runtime."""
    from . import cloud
    runtime = cloud._runtime
    if runtime is None:
        return {}, False
    with runtime.lock:
        account = runtime.manifest.get("account")
        return (dict(account) if isinstance(account, dict) else {}), bool(runtime.signed_in)


def mode() -> str:
    """The signed-in account's billing as the manifest says it; else what the gateway's replies said."""
    try:
        account, signed_in = _account()
    except Exception:  # Display only: a runtime half way through starting is not a credits account yet.
        account, signed_in = {}, False
    value = billing({"account": account}, signed_in)
    if value:
        return value
    reported = (state.ui.get("account") or {}).get("billing")
    return reported if reported in (CREDITS, ALLOWANCE) else ""


def active() -> bool:
    """Hosted chat comes out of the credit balance: the credits arm."""
    return mode() == CREDITS


def per_step(account: dict) -> float | None:
    data = account.get("chat_credits") if isinstance(account, dict) else None
    value = data.get("per_step") if isinstance(data, dict) else None
    return float(value) if _amount(value) and value > 0 else None


def step_credits(account: dict) -> int:
    """per_step rounded to the nearest whole credit, never under 1 (the contract's display rule)."""
    value = per_step(account)
    return max(1, round(value)) if value is not None else 1


def step_label(account: dict, short: bool = False) -> str:
    """"≈1 credit / step", or "≈1 / step" on a tile's price."""
    n = step_credits(account)
    return f"≈{n} / step" if short else f"≈{n} credit{'' if n == 1 else 's'} / step"


def typical_build(account: dict) -> int:
    """Credits a typical first build uses, for the showcase: the account's agent terms say (account.agent.typical_build,
    config.agent_terms), else TYPICAL_BUILD."""
    from . import config
    return config.agent_terms(account if isinstance(account, dict) else {}).get("typical_build", TYPICAL_BUILD)


def plan_of(account: dict) -> str:
    plan = (account or {}).get("plan")
    return plan.lower() if isinstance(plan, str) else ""


def monthly(account: dict) -> int:
    """The account's monthly grant as copy names it: the plan's figure, or account.credits.monthly when larger."""
    credits = (account or {}).get("credits") or {}
    reported = credits.get("monthly") if isinstance(credits, dict) else None
    plan = PLANS.get(plan_of(account), PLANS["free"])["credits"]
    return max(round(reported), plan) if _amount(reported) else plan


def balance(account: dict):
    credits = (account or {}).get("credits") or {}
    value = credits.get("balance") if isinstance(credits, dict) else None
    return value if _amount(value) else None


def refill_of(value) -> dict | None:
    """The weekly top-up, {credits, at} (manifest account.chat_credits.refill or the chat 402's refill, contract
    0.9); None when absent or malformed."""
    if not isinstance(value, dict):
        return None
    credits, at = value.get("credits"), value.get("at")
    if not (_amount(credits) and credits > 0 and isinstance(at, str) and _moment(at) is not None):
        return None
    return {"credits": credits, "at": at}


def account_refill(account: dict) -> dict | None:
    """The manifest account's next weekly top-up."""
    data = account.get("chat_credits") if isinstance(account, dict) else None
    return refill_of(data.get("refill")) if isinstance(data, dict) else None


def panel_data(manifest, signed_in: bool) -> dict:
    """What the panel's layout needs, as plain values (layout.py never reads the runtime)."""
    account = (manifest or {}).get("account") if signed_in else None
    account = account if isinstance(account, dict) else {}
    credits = account.get("credits") if isinstance(account.get("credits"), dict) else {}
    data = {"billing": billing(manifest, signed_in), "plan": plan_of(account), "monthly": monthly(account),
            "resets_at": credits.get("resets_at") if isinstance(credits.get("resets_at"), str) else "",
            "step": step_label(account)}
    refill = account_refill(account)
    if refill:  # Only when the server names one, so every other account's data is as before.
        data["refill"] = refill
    return data


def day_label(resets_at, now: float | None = None) -> str:
    """"Sun 1 Nov": the day credits come back. A reset at midnight UTC is a calendar boundary (the free grant
    renews on the 1st), so it keeps its own date; any other moment is shown on this computer's calendar."""
    if not isinstance(resets_at, str) or not resets_at:
        return ""
    try:
        moment = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
        moment = moment.replace(tzinfo=moment.tzinfo or timezone.utc)
        if moment.timestamp() <= (time.time() if now is None else now):
            return ""
        utc = moment.astimezone(timezone.utc)
        day = utc if (utc.hour, utc.minute, utc.second) == (0, 0, 0) else moment.astimezone()
        return f"{day:%a} {day.day} {day:%b}"
    except (ValueError, OverflowError, OSError):  # Windows refuses far-off dates in local time.
        return ""


def short_reset(resets_at, now: float | None = None) -> str:
    """"in about 3 h" within a day, else "Sun 1 Nov"; "" when unknown or past."""
    try:
        moment = datetime.fromisoformat(str(resets_at).replace("Z", "+00:00"))
        seconds = moment.replace(tzinfo=moment.tzinfo or timezone.utc).timestamp() - (time.time() if now is None
                                                                                     else now)
    except (ValueError, TypeError, OverflowError, OSError):
        return ""
    if seconds <= 0:
        return ""
    if seconds < 86400:
        return f"in about {max(1, round(seconds / 3600))} h" if seconds >= 3600 else "in under an hour"
    return day_label(resets_at, now)


def expired(resets_at, now: float | None = None) -> bool:
    """A refusal whose reset has passed is over: its card and pop-up stop showing."""
    try:
        moment = datetime.fromisoformat(str(resets_at).replace("Z", "+00:00"))
        return moment.replace(tzinfo=moment.tzinfo or timezone.utc).timestamp() <= (time.time() if now is None
                                                                                    else now)
    except (ValueError, TypeError, OverflowError, OSError):
        return False


def _moment(value) -> float | None:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return moment.replace(tzinfo=moment.tzinfo or timezone.utc).timestamp()
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def next_refill(item: dict, data: dict, now: float | None = None) -> dict | None:
    """The weekly top-up an out-of-credits card names: the refusal's own refill, else the manifest's, when it is
    still ahead and comes before the monthly reset. None otherwise (and always without the weekly top-up)."""
    item, data = item if isinstance(item, dict) else {}, data if isinstance(data, dict) else {}
    refill = refill_of(item.get("refill")) or refill_of(data.get("refill"))
    if refill is None:
        return None
    now = time.time() if now is None else now
    at = _moment(refill["at"])
    if at is None or at <= now:
        return None
    reset = _moment(item.get("resets_at") or data.get("resets_at"))
    return None if reset is not None and now < reset <= at else refill


def refused_until_passed(item: dict, now: float | None = None) -> bool:
    """An out-of-credits refusal is over at its monthly reset, or at the weekly top-up it named."""
    refill = refill_of((item or {}).get("refill"))
    return expired((item or {}).get("resets_at"), now) or (refill is not None and expired(refill["at"], now))


# --- What each turn cost -------------------------------------------------------------------------------------

def turn_start(session: dict, turn) -> None:
    """Main thread, as a turn launches: the balance it starts from. Never raises."""
    try:
        account, signed_in = _account()
        if billing({"account": account}, signed_in) != CREDITS:
            return
        cfg = getattr(turn, "config", None)
        if cfg is not None:
            from . import config
            if not config.on_service(cfg.base_url):
                return  # A ChatGPT plan's or a key's turn spends no chat credits: no "Done · N credits" (0.2.15).
        from . import spending
        with spending.lock:
            tools = spending.ledger(session).get("credits", 0)
        turn.credits = {"before": balance(account), "header": None, "tools": tools}
    except Exception as ex:
        print(f"Loopcut: could not note the balance before this turn: {ex!r}")


def note_balance(info: dict) -> None:
    """Main thread, for every gateway reply: the first x-loopcut-credits-balance of a turn is its exact start."""
    value = (info or {}).get("credits_balance")
    if not _amount(value):
        return
    turn = state.session().get("turn")
    data = getattr(turn, "credits", None)
    if isinstance(data, dict) and data.get("header") is None:
        data["header"] = value


def _start(data: dict):
    """The balance before the turn: the first reply's header (exact by contract, but whole credits), unless the
    manifest's figure from the turn's start agrees to within a credit (it keeps the fraction)."""
    header, before = data.get("header"), data.get("before")
    if _amount(header) and not (_amount(before) and abs(header - before) < 1):
        return header
    return before if _amount(before) else None


def turn_end(session: dict, turn) -> None:
    """Agent thread, after the turn's last item: read the balance once the charges settle and put what the turn
    used under its last reply. Never raises; nothing happens outside the credits arm."""
    data = getattr(turn, "credits", None)
    if not isinstance(data, dict) or getattr(turn, "returned_submission", False):
        return
    items = list(session.get("items") or [])
    first = getattr(turn, "first_item", 0) or 0
    newer = session.get("turn")  # A queued note can start the next turn before this line runs.
    last = getattr(newer, "first_item", None) if newer is not None and newer is not turn else None
    target = next((item for item in reversed(items[first:last]) if item.get("kind") == "assistant"), None)
    if target is None:
        return
    from . import spending
    with spending.lock:  # This turn's tools, before a next turn can add its own.
        data["tools_after"] = spending.ledger(session).get("credits", 0)
    data["turn"] = turn
    stopped = getattr(turn, "outcome", "") != "completed"
    threading.Thread(target=_settle, args=(session, target, data, stopped), name="loopcut-turn-credits",
                     daemon=True).start()


def _settle(session: dict, target: dict, data: dict, stopped: bool, delay: float = SETTLE_SECONDS) -> None:
    from . import cloud, mainthread
    try:
        time.sleep(delay)
        runtime = cloud._runtime
        if runtime is None:
            return
        for attempt in range(REFRESH_TRIES):
            if runtime.refresh():  # ETag: a 304 when nothing changed. False while another refresh runs or fails.
                mainthread.run_on_main(lambda: record(session, target, data, stopped))
                return
            time.sleep(1.0)
        print("Loopcut: no fresh balance after this turn; its credits are not shown")  # Never a wrong "0 credits".
    except Exception as ex:
        print(f"Loopcut: could not read what this turn used: {ex!r}")


def record(session: dict, target: dict, data: dict, stopped: bool, after=None) -> None:
    """Main thread: the turn's credits on its last reply ("Done · 6 credits"), and the chat part of them, which
    the composer's "chat N" adds to the tools' spend."""
    if after is None:
        account, _ = _account()
        after = balance(account)
    before = _start(data)
    if before is None or not _amount(after) or not any(item is target for item in session.get("items", [])):
        return
    # The next turn may already be running (a queued note): its first reply's balance is where this turn ended,
    # and a next turn with no such header yet starts from here, so no credit is counted twice.
    newer = session.get("turn")
    later = getattr(newer, "credits", None) if newer is not None and newer is not data.get("turn") else None
    if isinstance(later, dict) and later is not data:
        if _amount(later.get("header")):
            after = max(float(after), float(later["header"]))
        else:
            later["before"] = after
    from . import spending
    with spending.lock:
        ended = data.get("tools_after", spending.ledger(session).get("credits", 0))
    tools = max(0, ended - (data.get("tools") or 0))
    used = max(0.0, float(before) - float(after))
    target["turn_credits"] = round(used, 2)
    target["chat_credits"] = round(max(0.0, used - tools), 2)
    if stopped:
        target["turn_stopped"] = True
    try:
        from . import conversations
        conversations.save(session)
    except Exception as ex:
        print(f"Loopcut: could not save this turn's credits: {ex!r}")
    try:
        from . import mainthread
        mainthread.request_redraw()
    except Exception:
        pass


def chat_total(session: dict):
    """What this chat used: the tools' spend (spending.py) and the chat credits of each turn."""
    from . import spending
    spend = session.get("spend")
    tools = spend.get("credits", 0) if spending.valid_ledger(spend) else 0
    chat = sum(item["chat_credits"] for item in session.get("items", []) if _amount(item.get("chat_credits")))
    return tools + chat


def credits_text(value) -> str:
    """"6 credits", "1 credit", "<1 credit" for a turn that used a fraction."""
    if not _amount(value):
        return ""
    if 0 < value < 0.5:
        return "<1 credit"
    n = round(value)
    return f"{n:,} credit{'' if n == 1 else 's'}"


def turn_line(item: dict) -> str:
    """The line under a finished turn's reply: "Done · 6 credits"; "" for replies that have none."""
    used = item.get("turn_credits")
    if not _amount(used):
        return ""
    return f"{'Stopped' if item.get('turn_stopped') else 'Done'} · {credits_text(used)}"


# --- The pop-ups' copy ---------------------------------------------------------------------------------------

# The out-of-credits pop-up's locked tiles (round3 #3): (label, picture in media/credits, or "" for a plain tile).
# Released tools only (0.2.15): nothing marked Soon, such as Auto-rig.
LOCKED_TILES = (("AI agent", "credits/mini-agent.jpg"), ("Photo → 3D", "credits/mini-photo3d.jpg"),
                ("Motion", "credits/mini-motion.jpg"), ("Renders", "credits/mini-render.jpg"),
                ("Sound and voice", ""), ("Retopology", ""))
AGENT_TILE = "AI agent"
# The free way on, one line (persona rerun 2026-10-24: "Keep chatting for free" read as false without Plus).
KEEP_CHATTING = "Have ChatGPT Plus or Pro? Chat on it at no cost."
# Variant B's rows (round2): (icon in media/icons, tint of its circle).
OFFER_ICONS = {"chat": ("message-circle-fg", (0.18, 0.27, 0.45, 1.0)), "model": ("sparkles-fg", (0.30, 0.22, 0.45, 1.0)),
               "credits": ("cloud-fg", (0.16, 0.33, 0.25, 1.0)), "render": ("aperture-fg", (0.42, 0.18, 0.20, 1.0))}


def media_paths() -> set[str]:
    """Every picture the pop-ups draw, so the panel keeps them loaded while one is open (draw.retain_images)."""
    paths = {str(MEDIA / name) for _, name in LOCKED_TILES if name}
    paths.update(str(MEDIA / "icons" / (icon + ".png")) for icon, _ in OFFER_ICONS.values())
    return paths


def upgrade_button(upgrade, short: bool = False) -> str:
    """"Get Pro · 2,200 credits a month · $20" from the upgrade object's plan ("Get Pro · $20 a month" short, for a
    narrow panel); the upgrade's own label for a plan this table does not know."""
    if not isinstance(upgrade, dict):
        return ""
    plan = str(upgrade.get("plan") or "").lower()
    if plan in PLANS and PLANS[plan]["price"]:
        if short:
            return f"Get {plan.capitalize()} · ${PLANS[plan]['price']} a month"
        return f"Get {plan.capitalize()} · {PLANS[plan]['credits']:,} credits a month · ${PLANS[plan]['price']}"
    return str(upgrade.get("label") or "See plans")


def plan_dollars(credits) -> str:
    """"$22" for a plan's 2,200 credits (1 credit = $0.01): whole dollars beside a plan's grant."""
    return f"${credits / 100:,.0f}" if credits % 100 == 0 else f"${credits / 100:,.2f}"


def balance_dollars(credits) -> str:
    """"$0.94": a balance's value, as the panel's other credit lines show it."""
    return f"${credits / 100:,.2f}"


def _builds(upgrade) -> int | None:
    value = upgrade.get("builds") if isinstance(upgrade, dict) else None
    return value if type(value) is int and value > 0 else None


def offer_line(upgrade, tools_only: bool = False) -> str:
    """The out-of-credits pop-up's price line: "Pro: 2,200 credits a month ($22), about 110 builds" (upgrade.builds,
    contract 0.9); for a chat on the user's own ChatGPT plan, what the credits buy instead. "" for a plan this table
    does not know."""
    plan = str((upgrade or {}).get("plan") or "").lower() if isinstance(upgrade, dict) else ""
    if plan not in PLANS or not PLANS[plan]["price"]:
        return ""
    credits = PLANS[plan]["credits"]
    line = f"{plan.capitalize()}: {credits:,} credits a month ({plan_dollars(credits)})"
    if tools_only:
        return line + " for renders, motion and 3D models"
    builds = _builds(upgrade)
    return line + (f", about {builds:,} builds" if builds else "")


def come_back(data: dict, resets_at: str, now: float | None = None, short: bool = False,
              refill: dict | None = None) -> str:
    """"100 free credits come back Sun 1 Nov" (the free plan), "2,200 credits come back …" (a paid one); short, for
    the slim card, without the amount. refill (next_refill): the sooner weekly top-up, "25 free credits come back
    Fri 30 Oct" or "… in about 3 h"."""
    if refill:
        what = "Free credits" if short else f"{_n(refill['credits'])} free credits"
        when = short_reset(refill["at"], now)
        return f"{what} come back {when}" if when else f"{what} came back"  # Past: a card kept as history.
    amount = data.get("monthly") or PLANS["free"]["credits"]
    free = (data.get("plan") or "free") == "free"
    what = f"{'Free credits' if free else 'Credits'} come back" if short else \
        f"{amount:,} {'free ' if free else ''}credits come back"
    day = day_label(resets_at or data.get("resets_at") or "", now)
    return f"{what} {day}" if day else f"{what} next month"


def offer_rows(item: dict) -> list[tuple[str, str]]:
    """Variant B's four rows (round2): what the next plan adds, as (icon key, line). The chat line uses the
    upgrade's times (how many times more hosted chat) when the server sends it."""
    upgrade = item.get("upgrade") or {}
    plan = str(upgrade.get("plan") or "pro").lower()
    times, builds = upgrade.get("times"), _builds(upgrade)
    if builds:  # Concrete units (0.2.15): personas read "17×" as "17× of what?".
        chat = f"About {builds:,} builds a month"
    else:
        chat = f"{times}× more chat every month" if type(times) is int and times > 1 else "Much more chat every month"
    rows = [("chat", chat)]
    if plan == "pro":
        rows.append(("model", "Loopcut Pro, the stronger model"))
    credits = PLANS.get(plan, PLANS["pro"])["credits"]
    rows.append(("credits", f"{credits:,} cloud credits a month ({plan_dollars(credits)})"))
    if (item.get("plan") or "free") == "free":  # Pro and Max both have these already.
        rows.append(("render", "Cycles renders and top-ups"))
    return rows


def offer_plan(item: dict) -> tuple[str, str]:
    """("Loopcut Pro", "$20 / month") for variant B's band."""
    plan = str((item.get("upgrade") or {}).get("plan") or "pro").lower()
    price = PLANS.get(plan, {}).get("price")
    return f"Loopcut {plan.capitalize()}", (f"${price} / month" if price else "")


def offer_used(item: dict) -> str:
    """Variant B's first line: what ran out."""
    free = (item.get("plan") or "free") == "free"
    if item.get("reason") == "week_limit":
        return f"You've used this week's {'free ' if free else ''}chat."
    return f"You've used this 5-hour window's {'free ' if free else ''}chat."


def low_key(account: dict) -> str:
    """The refill period a low-credit heads-up belongs to: the next weekly top-up, else the monthly reset."""
    refill = account_refill(account)
    if refill:
        return refill["at"]
    credits = (account or {}).get("credits")
    reset = credits.get("resets_at") if isinstance(credits, dict) else None
    return reset if isinstance(reset, str) and reset else "balance"


def low_text(balance, account: dict, now: float | None = None) -> str:
    """The heads-up's two lines: "18 credits left ($0.18)", then "25 free credits come back Fri 30 Oct"."""
    credits = (account or {}).get("credits")
    reset = credits.get("resets_at") if isinstance(credits, dict) else None
    data = {"plan": plan_of(account), "monthly": monthly(account), "resets_at": reset if isinstance(reset, str) else ""}
    refill = next_refill({}, {**data, "refill": account_refill(account)}, now)
    back = come_back(data, data["resets_at"], now, refill=refill)
    return f"{_n(balance)} credit{'' if round(balance) == 1 else 's'} left ({balance_dollars(balance)})\n{back}"


def used_title(item: dict) -> str:
    """The slim card's title after a variant B pop-up closes (round2 D)."""
    return "Free chat used" if (item.get("plan") or "free") == "free" else "Chat allowance used"
