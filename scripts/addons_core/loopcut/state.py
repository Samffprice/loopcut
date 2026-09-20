"""Session state. Plain dicts and lists only, held in this module, which dev_reload never
reloads, so a hot reload of everything else leaves the conversation on screen untouched.

Not bpy.app.driver_namespace: Blender wipes that whenever a .blend is opened, which would
take the conversation with it.
"""

import time
import uuid

_session: dict | None = None
restoring = False  # True while checkpoints.restore loads a file, so that load is not taken for the user's.
# view: "chat" | "history"; history rows are loaded when the view opens. mentions: (name, kind)
# completions for the @name being typed.
ui = {"view": "chat", "history": [], "mentions": []}
hosts: set[int] = set()  # Pointers of the areas showing the panel. Same lifetime rules as the session.


def new_session() -> dict:
    return {
        "items": [],        # What the UI shows; see the item_* constructors.
        "messages": [],     # What the API sees.
        "input": "",
        "attachments": [],  # Images for the next message: {"ref": stored image, "name": file name}.
        "cursor": 0,
        "anchor": None,     # Other end of the input's selection; see ui/textedit.py.
        "scroll": 0.0,      # Pixels scrolled up from the bottom of the chat.
        "focused": False,
        "busy": False,
        "turn": None,       # agent.Turn while a request is in flight.
        "id": uuid.uuid4().hex,          # Names this conversation's checkpoint folder.
        "pending_checkpoint": None,      # Set per turn; see checkpoints.begin_turn.
        "confirm_restore": None,         # Index of the user item whose restore is being confirmed.
        "projects": [],                  # conversations.project_key of every file this has worked in.
        "title": "",
        "created": time.time(),
        "auto_run": False,               # "Always allow" for this conversation; never saved.
        # Tokens, as far as the API reports them: input and output over the conversation's life,
        # context is the size of the latest request.
        "usage": {"input": 0, "output": 0, "context": 0},
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


def item_notice(text: str, action_label: str = "", checkpoint: str = "") -> dict:
    return {"kind": "notice", "text": text, "action_label": action_label, "checkpoint": checkpoint}


def item_changes(text: str, lines: list[str], checkpoint: str) -> dict:
    # What a turn did to the scene. resolved: "" while the Keep / Undo buttons show, then "kept".
    return {"kind": "changes", "text": text, "lines": lines, "checkpoint": checkpoint, "resolved": ""}


def item_error(text: str) -> dict:
    return {"kind": "error", "text": text}
