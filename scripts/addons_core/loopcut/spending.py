"""What a chat may spend without asking (C47, the owner's "Balanced" rules, 2026-10-16).

The owner: "better ux revolving around credits and credit use ... we probably shouldn't have it ask every
single time, but we also don't want it just going crazy and one agent just doing hella shit in the background".

- A paid call costing at most min(the server's approval.confirm_over_credits, the user's "Ask above") runs on its
  own; above that its card asks, Approve / Cancel, with the cost and the balance before and after. The server
  sends 0 to add-ons older than 0.2.4, so every priced call asks there. A quoted tool is decided on its quote.
- Auto-approved spend in one chat is capped by the chat budget. The call that would cross it asks once: "This
  chat has used 150 credits. Allow 150 more?" Allow adds the setting's amount again.
- More than BURST paid calls in one turn ask before the next: a looping agent pauses here.
- The server's own limits (an optional daily limit, a burst guard) refuse with a single-use confirm_token; the
  card asks, and Approve retries the same request with it (cloud_account.submit).
- Owner, 2026-10-16: "all those shouldn't just end the turn if it hits, but have an approval prompt". Every ask
  pauses the turn: Approve / Allow runs the call; "Skip this" skips only that call, and the model is told the
  user declined it and why, so the turn goes on. The panel's Stop still stops the turn.
- Own-key (BYOK) calls ask once per chat per vendor, then run within the burst guard. The user's own vendor
  account pays, so they never count against credits.
- "Ask before every paid call" asks for each one. "Run without asking" changes none of this.
- Paid extras (owner, 2026-10-26; Preferences, or set_extras from what the user says in chat): "Use when it helps"
  (AUTO) is the rules above; "Ask me first" (ASK) asks for every paid call, whatever its price; "Only when I ask"
  (ONLY_ASKED) refuses, with no card, a paid call the user did not ask for (asked_for: their latest message names
  its kind or the result it makes, or they approved its card or clicked its button). A free call never asks.
- Workers spend from the same chat ledger and turn; their asks show in the user's panel (agent._Consent).
- image_to_3d on a picture Loopcut made (image_generate) asks as any paid call does (owner, 2026-10-26, for 0.2.20:
  it replaced that day's "always asks" card). Loopcut checks the picture instead: the model must have looked at it
  (look_at_reference) before converting it, or the call is refused with no card (cloud_jobs.made_unseen).
- Undone means no (owner, 2026-10-26): a paid output the model added unasked (no card approved it and the user's
  message did not ask for it) that the user then deletes or undoes is not added again in that chat: the model's
  next unasked call of that kind is refused with no card (note_implied, cloud.execute). Per chat, not across chats.
- A running paid job is never canceled by the model without the user's say (rel019-cloud, 2026-10-26: the model
  canceled the user's approved 29-credit render at 93% by itself): its card asks "Cancel it" or "Keep it running",
  unless the user's latest message asked to stop (asked_to_stop). Jobs that ran without a card count the same. The
  panel's own Cancel and Stop never ask.

Pure apart from a lock that serializes the agent's and the workers' threads. The ledger is a plain dict saved
with the conversation (conversations.py); the approved own-key vendors are consent, so they are not saved.
"""

import math
import re
import threading
from dataclasses import dataclass

ASK_ABOVE = 100     # Credits: image 6, HD image 20, SFX 4, voice 10, retopology 5, image→3D final 80 run (owner, 2026-10-26).
CHAT_BUDGET = 150   # Credits one chat may spend without asking, before "Allow 150 more?".
BURST = 5           # Paid calls one turn may make before the next asks.
MAX_SETTING = 100_000

RUN, COST, VENDOR, BURST_ASK, BUDGET = "run", "cost", "vendor", "burst", "budget"
NOT_ASKED = "not_asked"  # ONLY_ASKED: a paid call the user did not ask for, refused with no card.
AUTO, ASK, ONLY_ASKED = "AUTO", "ASK", "ONLY_ASKED"  # The extras preference (settings.extras_mode, set_extras).
EXTRAS = (AUTO, ASK, ONLY_ASKED)
CANCEL = "cancel_job"  # The model's cancel of a running paid job (cloud._job_status): {title, held, pct}.
DAILY, RATE = "daily_limit_reached", "rate_limited"  # The server's refusals that carry a confirm_token.
SERVER_LIMITS = (DAILY, RATE)

lock = threading.RLock()


@dataclass(frozen=True)
class Settings:
    ask_above: int = ASK_ABOVE
    chat_budget: int = CHAT_BUDGET
    ask_every: bool = False
    extras: str = AUTO

    @property
    def asks_each(self) -> bool:
        """Every paid call asks: "Ask before every paid call", or the extras preference's "Ask me first"."""
        return self.ask_every or self.extras == ASK


def _amount(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def dollars(credits) -> str:
    from .nudges import dollars as format_dollars
    return format_dollars(credits)


def new_ledger() -> dict:
    """credits: what the chat has spent (a running call's hold until its charge is known); auto: the part that
    ran without asking, which the chat budget caps; allowed: what Allow added to the budget."""
    return {"credits": 0, "auto": 0, "allowed": 0}


def valid_ledger(value) -> bool:
    return isinstance(value, dict) and all(_amount(value.get(key, 0)) for key in new_ledger())


def ledger(session: dict) -> dict:
    spend = session.get("spend")
    if not valid_ledger(spend):
        spend = session["spend"] = new_ledger()
    for key, value in new_ledger().items():
        spend.setdefault(key, value)
    return spend


def budget(spend: dict, settings: Settings) -> float:
    return settings.chat_budget + spend.get("allowed", 0)


def paid(details: dict) -> bool:
    credits = details.get("credits")
    return bool(details.get("vendor")) or credits is None or credits > 0


def decide(details: dict, spend: dict, settings: Settings, *, calls: int = 0, burst_limit: int = BURST,
           vendors=(), asked: bool = True) -> str:
    """RUN, or what the call's card asks: COST (this call), VENDOR (an own-key vendor, once per chat), BURST_ASK
    or BUDGET (Allow more); NOT_ASKED refuses it with no card. details is cloud_account.approval_details: required
    is the server's own threshold (credits > confirm_over_credits). asked: the user asked for this call
    (asked_for), which "Only when I ask" needs."""
    if not paid(details):
        return RUN  # Free calls never ask and never count.
    if settings.extras == ONLY_ASKED and not asked:
        return NOT_ASKED
    vendor, credits = details.get("vendor"), details.get("credits")
    if settings.asks_each:
        return COST
    if vendor:
        if vendor not in vendors:
            return VENDOR
    elif credits is None or details.get("required") or credits > settings.ask_above:
        return COST  # Over min(server, user); an unknown price always asks.
    if calls >= burst_limit:
        return BURST_ASK
    if not vendor and spend.get("auto", 0) + credits > budget(spend, settings):
        return BUDGET
    return RUN


def allow(kind: str, details: dict, spend: dict, settings: Settings, chat_turn, vendors: list) -> None:
    # chat_turn: the user's turn (agent.Turn), whose paid_calls and burst_limit the burst guard reads.
    """What an Allow / Approve adds beyond this one call: the vendor for the chat, five more calls in this turn,
    or another chat budget's worth of credits (counted from what the chat has already used)."""
    if kind == VENDOR and details.get("vendor") and details["vendor"] not in vendors:
        vendors.append(details["vendor"])
    elif kind == BURST_ASK:
        chat_turn.burst_limit = max(chat_turn.burst_limit, chat_turn.paid_calls) + BURST
    elif kind == BUDGET:
        # The new budget is max(budget, auto) + chat_budget, and budget() adds chat_budget to allowed.
        spend["allowed"] = max(budget(spend, settings), spend.get("auto", 0))


def start(spend: dict, details: dict, auto: bool) -> float:
    """The call may run: its credits are held in the chat's spend now, settled when its charge is known."""
    credits = details.get("credits") if not details.get("vendor") and _amount(details.get("credits")) else 0
    spend["credits"] = spend.get("credits", 0) + credits
    if auto:
        spend["auto"] = spend.get("auto", 0) + credits
    return credits


def settle(spend: dict, held: float, charged, auto: bool) -> None:
    """Replace a hold with what was charged (0 when nothing was, or the server returned it)."""
    if not _amount(charged):
        return
    spend["credits"] = max(0, spend.get("credits", 0) - held + charged)
    if auto:
        spend["auto"] = max(0, spend.get("auto", 0) - held + charged)


def _n(value) -> str:
    return f"{value:,g}"


def threshold(details: dict, settings: Settings):
    """The credits a call may cost without asking: min(server, "Ask above"); None when the server sent none."""
    server = details.get("threshold")
    return min(server, settings.ask_above) if _amount(server) else None


def ask_lines(kind: str, details: dict, spend: dict, settings: Settings, *, calls: int = 0,
              balance=None, worker: str = "") -> list[str]:
    """What the card says above its buttons: what was hit, then the balance before and after. Short lines that
    wrap at 260 px; the cost itself is the card's cost line."""
    lines = [f"Asked by {worker}, working in the background." if worker else ""]
    credits, vendor = details.get("credits"), details.get("vendor")
    if vendor:
        lines.append(f"Uses your own {vendor} key: your {vendor} account pays, not credits.")
        lines.append(f"Allow {vendor} calls in this chat?" if kind == VENDOR else "")
    elif kind == COST:
        limit = threshold(details, settings)
        lines.append("You asked to approve every paid call." if settings.asks_each
                     else "Its price is not known yet." if not _amount(credits)
                     else "Every paid call asks on this account." if limit == 0
                     else f"Over {_n(limit)} credits, so it asks first." if limit is not None
                     else "Paid calls this large ask first.")
    elif kind == BURST_ASK:
        lines.append(f"This turn has made {calls} paid calls. Allow {BURST} more?")
    elif kind == BUDGET:
        total, auto = spend.get("credits", 0), spend.get("auto", 0)
        used = f"This chat has used {_n(total)} credits" + (f" ({_n(auto)} without asking)" if auto != total else "")
        lines.append(f"{used}. Allow {_n(settings.chat_budget)} more?")
    elif kind == DAILY:  # HTTP 402: "Today's cloud spending limit of N credits is reached."
        lines.append(details.get("message") or "Today's credit limit is reached.")
        lines.append("Approve to go over it for this one job.")
    elif kind == CANCEL:
        pct, held = details.get("pct"), details.get("held")
        lines.append(f"{details.get('title') or 'This cloud job'}" + (f", {pct}% done" if _amount(pct) and pct else "")
                     + ". Loopcut's AI asked to cancel it.")
        lines.append(f"It finished and was charged {_n(details['charged'])} credits; canceling stops its download."
                     if _amount(details.get("charged")) and details["charged"]
                     else f"{_n(held)} credits held; canceling returns them." if _amount(held) and held
                     else "Canceling returns its unused credits.")
    elif kind == RATE:  # HTTP 429: the service's burst guard, more than 30 paid jobs in 10 minutes.
        lines.append("Many paid jobs in a short time.")
        lines.append(details.get("message") or "")
        lines.append("Run this one anyway?")
    if not vendor and _amount(balance) and _amount(credits) and credits:
        lines.append(f"Balance {_n(balance)} → {_n(balance - credits)} credits" if balance >= credits
                     else f"You have {_n(balance)} credits; this needs {_n(credits)}.")
    return [line for line in lines if line]


BUTTONS = {COST: "Approve", VENDOR: "Approve", BURST_ASK: f"Allow {BURST} more", DAILY: "Approve", RATE: "Approve",
           CANCEL: "Cancel it"}
SKIP = "Skip this"
SKIPS = {CANCEL: "Keep it running"}


def approve_label(kind: str, settings: Settings | None = None) -> str:
    if kind == BUDGET:
        return f"Allow {_n((settings or Settings()).chat_budget)} more"
    return BUTTONS.get(kind, "Approve")


def skip_label(kind: str) -> str:
    return SKIPS.get(kind, SKIP)


def ask_title(kind: str, details: dict) -> str:
    """The card's title while it asks, in place of the tool's: "Cancel this cloud job?"; ""."""
    return "Cancel this cloud job?" if kind == CANCEL else ""


def declined(kind: str, details: dict, settings: Settings, *, calls: int = 0) -> str:
    """The tool result after "Skip this": the user declined this one paid call, and why; the turn goes on."""
    vendor, credits = details.get("vendor"), details.get("credits")
    limit = threshold(details, settings)
    reason = {COST: (f"it costs {_n(credits)} credits" if _amount(credits) else "its price was not known")
                    + (", and they approve every paid call" if settings.asks_each
                       else f", over the {_n(limit)} credits that run without asking" if limit is not None else ""),
              VENDOR: f"it uses their own {vendor} key",
              BURST_ASK: f"it would be paid call {calls + 1} in this turn",
              BUDGET: f"it would take this chat past its {_n(settings.chat_budget)}-credit budget",
              DAILY: "it is over their daily credit limit",
              RATE: "the service saw too many paid jobs in a short time"}.get(kind, "they chose not to run it")
    if kind == COST and vendor:
        reason = f"it uses their own {vendor} key"
    if kind == NOT_ASKED:
        return ("Not run: the user wants paid tools only when they ask for one. Nothing ran and nothing was charged. "
                "Go on without it; you may say in one line what it would add.")
    if kind == CANCEL:
        return ("The user kept this cloud job running: it was not canceled, and it goes on and is charged as usual. "
                "Don't cancel it unless they ask; check it with job_status.")
    return (f"The user declined this paid call ({reason}). Nothing ran and nothing was charged. Go on without "
            "it: use another way, or finish the turn and say what it would have added.")


# The user's latest message asks to stop: a model's cancel of their paid job then needs no card. Conservative: English
# words only, and any negation, "keep" or "why" in the message makes it a no ("why did it stop?", "don't cancel it").
_STOP = re.compile(r"\b(?:stop|cancel|abort|halt|kill|terminate)\b", re.I)
_NOT_STOP = re.compile(r"\b(?:not|no|never|dont|keep|why|without|until|unless)\b|n['’]t\b", re.I)


def asked_to_stop(text) -> bool:
    return isinstance(text, str) and bool(_STOP.search(text)) and not _NOT_STOP.search(text)


# What each paid tool makes, in the user's words: "Only when I ask" runs a call whose kind or result the user's latest
# message names. Simple and conservative: English and the obvious words; a word after no, not, without or never (or
# a -n't) within a few words does not count ("no sound"). A tool not listed counts by its own name's words.
_PICTURE = r"pictures?|images?|photos?|illustrations?|drawings?|posters?"
_KINDS = {
    "image_generate": _PICTURE, "image_edit": _PICTURE,
    "image_to_3d": rf"(?:{_PICTURE})\b.*\b(?:3d|model|mesh)|\b(?:in|into|to) 3d",
    "sfx": r"sounds?|audio|sfx|ambien(?:ce|t)|music|soundtrack|noises?",
    "voice_line": r"voices?|voice-?overs?|narrat\w*|speech|dialogue|tts",
    "dialogue_from_recordings": r"voices?|dialogue|recordings?|lines",
    "lip_sync": r"lip-? ?sync\w*|lips",
    "retopology": r"retopo\w*|topology|game-? ?ready|quads?",
    "motion_from_text": r"motion|mocap|animat\w*", "motion_from_video": r"motion|mocap|animat\w*|videos?|clips?",
    "render": r"render\w*",
    "auto_rig": r"rig|rigs|rigged|rigging|armatures?|skeletons?", "character_rig": r"rig|rigs|rigged|rigging|armatures?",
}
_ANY = r"(?:cloud|paid) (?:tools?|steps?|extras?)"  # "use the paid tools": every kind.
_NEGATED = re.compile(r"(?:\b(?:no|not|without|never)|n['’]t)\W+(?:\w+\W+){0,2}$", re.I)


def asked_for(tool, text) -> bool:
    """The user's latest message asks for this paid tool's kind or its result (see _KINDS)."""
    if not isinstance(text, str) or not isinstance(tool, str) or not tool:
        return False
    words = _KINDS.get(tool) or re.escape(tool.replace("_", " "))
    for match in re.finditer(rf"\b(?:{words}|{_ANY})\b", text, re.I):
        if not _NEGATED.search(text[max(0, match.start() - 40):match.start()]):
            return True
    return False


# Undone means no: the paid outputs the model added unasked in a chat, by kind. A kind is what the user would call it.
IMPLIED_KEEP = 32  # Records a chat keeps, the newest.
_KIND_NOUNS = {"sfx": "sound", "voice_line": "voice line", "dialogue_from_recordings": "dialogue",
               "lip_sync": "lip sync", "image_generate": "picture", "image_edit": "picture edit",
               "image_to_3d": "3D model", "retopology": "retopology", "motion_from_text": "motion",
               "motion_from_video": "motion", "render": "render", "auto_rig": "rig", "character_rig": "rig"}


def kind_noun(tool: str) -> str:
    return _KIND_NOUNS.get(tool) or str(tool).replace("_", " ")


def implied_rows(value) -> list:
    """A chat's records, damaged ones dropped: [{"tool", "job", "removed"}]."""
    rows = [{"tool": r["tool"], "job": r["job"], "removed": bool(r.get("removed"))} for r in value
            if isinstance(r, dict) and isinstance(r.get("tool"), str) and isinstance(r.get("job"), str) and r["job"]
            ] if isinstance(value, list) else []
    return rows[-IMPLIED_KEEP:]


def note_implied(session: dict, tool: str, job: str) -> None:
    """The model added this job's outputs to the chat unasked."""
    with lock:
        rows = session["implied"] = implied_rows(session.get("implied"))
        if job and not any(r["job"] == job for r in rows):
            rows.append({"tool": tool, "job": job, "removed": False})
            del rows[:-IMPLIED_KEEP]


def undone_text(tool: str) -> str:
    return (f"Not run: the user removed the last {kind_noun(tool)} you added; don't add it again unless they ask. "
            "Nothing was charged.")


def chat_label(spend) -> str:
    """The panel's "This chat: 46 credits"."""
    credits = spend.get("credits", 0) if valid_ledger(spend) else 0
    return f"This chat: {_n(credits)} credit{'' if credits == 1 else 's'}"


def receipt_text(charged, *, canceled: bool = False) -> str:
    """A finished call's card line once its charge is known."""
    if not _amount(charged):
        return ""
    if charged:
        return f"Used {_n(charged)} credits ({dollars(charged)})"
    return "Canceled: its credits were returned" if canceled else "No charge"
