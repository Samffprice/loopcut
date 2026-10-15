"""Session state. Plain dicts and lists only, held in this module, which dev_reload never
reloads, so a hot reload of everything else leaves the conversation on screen untouched.

Not bpy.app.driver_namespace: Blender wipes that whenever a .blend is opened, which would
take the conversation with it.
"""

import time
import uuid
from urllib.parse import urlsplit

from . import config

_session: dict | None = None
restoring = False  # True while checkpoints.restore loads a file, so that load is not taken for the user's.
# view: "chat" | "history"; history rows are loaded when the view opens. mentions: (name, kind)
# completions for the @name being typed. picker: {"query", "rows", "active"} while the + menu is
# open. hover: id of the hit under the mouse, for highlights and tooltips.
ui = {"view": "chat", "history": [], "mentions": [], "picker": None, "model_menu": False,
      "hover": None, "mouse": None,
      "account": None}  # Plan and window usage as the gateway last reported it; account.update_usage.
hosts: set[int] = set()  # Pointers of the areas showing the panel. Same lifetime rules as the session.


def new_session() -> dict:
    return {
        "items": [],        # What the UI shows; see the item_* constructors.
        "messages": [],     # What the API sees.
        "input": "",
        "attachments": [],  # Images for the next message: {"ref": stored image, "full": stored image, "name": file name}.
        "references": [],   # Every image attached so far, same shape plus "pinned": sent with each request while pinned.
        "cursor": 0,
        "anchor": None,     # Other end of the input's selection; see ui/textedit.py.
        "scroll": 0.0,      # Pixels scrolled up from the bottom of the chat.
        "focused": False,
        "busy": False,
        "turn": None,       # agent.Turn while a request is in flight.
        "id": uuid.uuid4().hex,          # Names this conversation's checkpoint folder.
        "pending_checkpoint": None,      # Set per turn; see checkpoints.begin_turn.
        "confirm_restore": None,         # Index of the user item whose restore is being confirmed.
        "confirm_edit": False,           # That confirmation is for editing the message (a restore that says so).
        "projects": [],                  # conversations.project_key of every file this has worked in.
        "title": "",
        "created": time.time(),
        "mode": "",                      # "workspace": this conversation's single agent has a workspace (agent._configure).
        "codex_thread": "",              # LOOPCUT_CODEX_APP: the Codex thread this conversation continues (codex_app.py).
        "auto_run": False,               # "Always allow" for this conversation; never saved.
        "auto_heavy": False,             # "Always allow renders" (and bakes) likewise.
        "contact_watch": [],             # check_contact pairs run_python measures again; never saved.
        # Tokens, as far as the API reports them: input and output over the conversation's life,
        # context is the size of the latest request.
        "usage": {"input": 0, "cached": 0, "output": 0, "context": 0},
        # The same by role once the orchestrator hands work to workers: {role: {input, cached, output,
        # requests}}; "usage" stays the sum. turn_levels: {turn, first, final} per turn (agent.py);
        # jobs: one JobLog per turn that delegated (waves.py). Read by the evals; never saved.
        "usage_by_role": {},
        "turn_levels": [],
        "routing": None,                 # The router's last decision (router.Decision.record); never saved.
        "jobs": [],
        # The orchestrator's own run_python edits to worker-built objects' meshes or materials
        # (orchestrator.edit_note): {turn, objects: {name: worker}} each. Read by the evals; never saved.
        "worker_edits": [],
        "checks": {},                    # Checks the finish gate carries to later turns: label -> check (waves.Job). Never saved.
        "snapshots": {},                 # The checks tool's snapshots: id -> {turn, objects: {name: record}}. Never saved.
        "turn_number": 0,                # Turns started with send(); what turn_levels and jobs call "turn".
        "token_ratio": 0.0,              # context.calibrate: reported / estimated tokens. Never saved.
        "mention": 0,                    # Highlighted row of the @-mention list.
    }


def session() -> dict:
    global _session
    if _session is None:
        _session = new_session()
    return _session


def replace(session: dict) -> dict:
    global _session
    _session = session
    return _session


def reset() -> dict:
    global _session
    _session = new_session()
    return _session


def item_user(text: str, images: list[str] | None = None) -> dict:
    # images: file names of what was attached, for the chat; the message holds the images themselves.
    return {"kind": "user", "text": text, "images": list(images or [])}


def item_assistant() -> dict:
    return {"kind": "assistant", "text": "", "streaming": True}


def item_tool(name: str, summary: str, code: str) -> dict:
    # status: awaiting | running | done | failed | rejected
    return {"kind": "tool", "name": name, "summary": summary, "code": code,
            "status": "running", "output": ""}


def item_notice(text: str, action_label: str = "", checkpoint: str = "", url: str = "") -> dict:
    # The button restores `checkpoint`, or opens `url` in the browser.
    return {"kind": "notice", "text": text, "action_label": action_label, "checkpoint": checkpoint, "url": url}


def notice_for_display(item: dict) -> dict:
    """Old conversations may retain the pre-beta low-allowance purchase notice."""
    if config.BETA and urlsplit(item.get("url", "")).path.rstrip("/") in {"/pricing", "/checkout"}:
        return {**item, "text": item["text"].split(" Upgrading now", 1)[0], "url": "", "action_label": ""}
    return item


def item_guard(text: str, modifiers: list) -> dict:
    """The cost guard switched a modifier's viewport display off, or refused a render. modifiers:
    [[object, modifier]] the button shows again; its label is cleared once it has."""
    return {**item_notice(text, action_label="Show anyway" if modifiers else ""),
            "modifiers": [list(m) for m in modifiers]}


def item_limit(text: str, reason: str, plan: str, resets_at: str, upgrade: dict | None) -> dict:
    """The gateway refused a request for want of allowance. upgrade: {url, plan, label, note}
    from the gateway, or None on the top plan. status: "" | waiting (the plans page is open and
    the add-on polls for the new plan) | done (upgraded; the turn goes on by itself)."""
    if config.BETA and reason in {"week_limit", "session_limit", "model_not_in_plan"}:
        text = {"week_limit": "This week's allowance is used.",
                "session_limit": "This session's allowance is used.",
                "model_not_in_plan": "This model is unavailable."}[reason]
        if resets_at:
            text += f" It resets {resets_at}."
    return {"kind": "limit", "text": text, "reason": reason, "plan": plan, "resets_at": resets_at or "",
            "upgrade": dict(upgrade) if upgrade and not config.BETA else None, "status": ""}


def item_changes(text: str, lines: list[str], checkpoint: str) -> dict:
    # What a turn did to the scene. resolved: "" while the Keep / Undo buttons show, then "kept".
    return {"kind": "changes", "text": text, "lines": lines, "checkpoint": checkpoint, "resolved": ""}


def item_error(text: str) -> dict:
    return {"kind": "error", "text": text}


WAVE_RUNNING = ("queued", "starting", "working", "checking", "merging")  # A saved one reloads as "stopped".


def wave_row(worker: str, deliverable: str, role: str) -> dict:
    """One worker in a wave card. status: queued | starting | working | checking | merging | done |
    partial | failed | cancelled (its own Cancel) | stopped (Stop, the budget before it started, its
    time, or a reload while it ran) | question. checks: "4/5" once run. merged: its result is in the
    scene (each worker merges as it finishes, not with its whole wave)."""
    return {"id": worker, "deliverable": deliverable, "role": role, "status": "queued", "steps": 0,
            "checks": "", "note": "", "merged": False}


def item_wave(wave: str, number: int, rows: list[dict]) -> dict:
    """The workers of one delegate call; waves.py updates the rows as they run."""
    count = len(rows)
    return {"kind": "wave", "wave": wave, "number": number,
            "text": f"Wave {number} · {count} worker{'s' if count != 1 else ''}", "rows": rows}


def item_merge(wave: str, text: str, lines: list[str], checkpoint: str, quarantined: list[str]) -> dict:
    """What a wave's merge did, with the way back (checkpoint) and what it kept aside (quarantined).
    resolved: "" while its buttons show."""
    return {"kind": "merge", "wave": wave, "text": text, "lines": list(lines), "checkpoint": checkpoint,
            "quarantined": list(quarantined), "resolved": ""}
