"""Editing the input box: caret, selection, word moves, clipboard, undo. Pure: no bpy.

The panel draws its own text field, so everything a native one gives for free has to live
somewhere; it lives here so it can be tested without Blender. State is the session's `input`,
`cursor`, `anchor` (the other end of the selection, or None) and `input_undo`.
"""

import time

UNDO_LIMIT = 100
_UNDO_PAUSE = 0.8  # Seconds. Typing without a longer pause than this is one undo step.


def selection(session: dict) -> tuple[int, int] | None:
    anchor = session.get("anchor")
    if anchor is None or anchor == session["cursor"]:
        return None
    return min(anchor, session["cursor"]), max(anchor, session["cursor"])


def selected_text(session: dict) -> str:
    span = selection(session)
    return session["input"][span[0]:span[1]] if span else ""


def _remember(session: dict, kind: str, now: float | None = None) -> None:
    """Call before changing the text. Runs of the same kind of edit collapse into one step."""
    now = time.monotonic() if now is None else now
    stack = session.setdefault("input_undo", [])
    last = session.get("input_undo_mark")
    if last and last[0] == kind and now - last[1] < _UNDO_PAUSE:
        session["input_undo_mark"] = (kind, now)
        return
    stack.append((session["input"], session["cursor"]))
    del stack[:-UNDO_LIMIT]
    session["input_undo_mark"] = (kind, now)


def undo(session: dict) -> bool:
    stack = session.get("input_undo") or []
    if not stack:
        return False
    session["input"], session["cursor"] = stack.pop()
    session["anchor"], session["input_undo_mark"] = None, None
    return True


def set_text(session: dict, text: str) -> None:
    """Replace everything (sending, restoring a message, picking a history entry)."""
    _remember(session, "set")
    session["input"], session["cursor"], session["anchor"] = text, len(text), None


def _delete_selection(session: dict) -> bool:
    span = selection(session)
    if not span:
        session["anchor"] = None
        return False
    text = session["input"]
    session["input"], session["cursor"], session["anchor"] = text[:span[0]] + text[span[1]:], span[0], None
    return True


def insert(session: dict, text: str, now: float | None = None) -> None:
    if not text:
        return
    _remember(session, "type" if len(text) == 1 and not selection(session) else "paste", now)
    _delete_selection(session)
    cursor = session["cursor"]
    session["input"] = session["input"][:cursor] + text + session["input"][cursor:]
    session["cursor"] = cursor + len(text)


def replace_range(session: dict, start: int, end: int, text: str) -> None:
    """Swap text[start:end] for `text` and put the caret after it (completing an @mention)."""
    _remember(session, "complete")
    session["input"] = session["input"][:start] + text + session["input"][end:]
    session["cursor"], session["anchor"] = start + len(text), None


def word_left(text: str, cursor: int) -> int:
    while cursor > 0 and not text[cursor - 1].isalnum():
        cursor -= 1
    while cursor > 0 and text[cursor - 1].isalnum():
        cursor -= 1
    return cursor


def word_right(text: str, cursor: int) -> int:
    while cursor < len(text) and not text[cursor].isalnum():
        cursor += 1
    while cursor < len(text) and text[cursor].isalnum():
        cursor += 1
    return cursor


def delete(session: dict, backwards: bool, word: bool = False, now: float | None = None) -> None:
    text, cursor = session["input"], session["cursor"]
    if selection(session):
        _remember(session, "delete-selection", now)
        _delete_selection(session)
        return
    if backwards:
        start, end = (word_left(text, cursor) if word else max(0, cursor - 1)), cursor
    else:
        start, end = cursor, (word_right(text, cursor) if word else min(len(text), cursor + 1))
    if start == end:
        return
    _remember(session, "delete", now)
    session["input"], session["cursor"], session["anchor"] = text[:start] + text[end:], start, None


def move(session: dict, target: str, select: bool = False) -> None:
    """target: left | right | word_left | word_right | home | end | line_home | line_end"""
    text, cursor = session["input"], session["cursor"]
    span = selection(session)
    if span and not select and target in ("left", "right"):
        new = span[0] if target == "left" else span[1]  # Collapse to that edge, as text fields do.
    else:
        new = {
            "left": lambda: max(0, cursor - 1),
            "right": lambda: min(len(text), cursor + 1),
            "word_left": lambda: word_left(text, cursor),
            "word_right": lambda: word_right(text, cursor),
            "home": lambda: 0,
            "end": lambda: len(text),
            "line_home": lambda: text.rfind("\n", 0, cursor) + 1,
            "line_end": lambda: len(text) if text.find("\n", cursor) < 0 else text.find("\n", cursor),
        }[target]()
    if select:
        if session.get("anchor") is None:
            session["anchor"] = cursor
    else:
        session["anchor"] = None
    session["cursor"] = new
    session["input_undo_mark"] = None  # Moving the caret ends a run of typing.


def select_all(session: dict) -> None:
    session["anchor"], session["cursor"] = 0, len(session["input"])


def cut(session: dict) -> str:
    text = selected_text(session)
    if text:
        _remember(session, "cut")
        _delete_selection(session)
    return text
