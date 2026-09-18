"""Session state. Plain dicts and lists only, held in this module, which dev_reload never
reloads, so a hot reload of everything else leaves the conversation on screen untouched.

Not bpy.app.driver_namespace: Blender wipes that whenever a .blend is opened, which would
take the conversation with it.
"""

import uuid

_session: dict | None = None
hosts: set[int] = set()  # Pointers of the areas showing the panel. Same lifetime rules as the session.


def new_session() -> dict:
    return {
        "items": [],        # What the UI shows; see the item_* constructors.
        "messages": [],     # What the API sees.
        "input": "",
        "cursor": 0,
        "scroll": 0.0,      # Pixels scrolled up from the bottom of the chat.
        "focused": False,
        "busy": False,
        "turn": None,       # agent.Turn while a request is in flight.
        "id": uuid.uuid4().hex,          # Names this conversation's checkpoint folder.
        "pending_checkpoint": None,      # Set per turn; see checkpoints.begin_turn.
        "confirm_restore": None,         # Index of the user item whose restore is being confirmed.
    }


def session() -> dict:
    global _session
    if _session is None:
        _session = new_session()
    return _session


def reset() -> dict:
    global _session
    _session = new_session()
    return _session


def item_user(text: str) -> dict:
    return {"kind": "user", "text": text}


def item_assistant() -> dict:
    return {"kind": "assistant", "text": "", "streaming": True}


def item_tool(name: str, summary: str, code: str) -> dict:
    # status: awaiting | running | done | failed | rejected
    return {"kind": "tool", "name": name, "summary": summary, "code": code,
            "status": "running", "output": ""}


def item_notice(text: str, action_label: str = "", checkpoint: str = "") -> dict:
    return {"kind": "notice", "text": text, "action_label": action_label, "checkpoint": checkpoint}


def item_error(text: str) -> dict:
    return {"kind": "error", "text": text}
