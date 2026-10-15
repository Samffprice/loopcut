"""Editing the input box: caret, selection, word moves, clipboard, undo. Pure: no bpy.

The panel draws its own text field, so everything a native one gives for free has to live
somewhere; it lives here so it can be tested without Blender. State is the session's `input`,
`cursor`, `anchor` (the other end of the selection, or None) and `input_undo`; `goal_x` remembers
the column up and down aim for, and `input_first` the first wrapped row in view.

Positions in the box come from layout.wrap()'s rows, (offset in the text, row text), recomputed
from the text on every draw, so the caret and selection are kept as character indexes and survive
a resize that rewraps the text. `width(text)` measures a prefix of a row in pixels.
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
    session.pop("goal_x", None)  # An edit ends a run of up and down moves.
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
    session.pop("goal_x", None)
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


def place_caret(session: dict, index: int) -> None:
    session["cursor"] = max(0, min(len(session["input"]), index))
    session["anchor"], session["input_undo_mark"] = None, None
    session.pop("goal_x", None)  # Any other move ends a run of up and down moves; vertical() sets it again.


def extend_selection(session: dict, index: int) -> None:
    anchor = session.get("anchor")
    if anchor is None:
        anchor = session["cursor"]
    place_caret(session, index)
    session["anchor"] = anchor


def select_word(session: dict, index: int) -> None:
    """Select an alphanumeric word, or the separator run at the caret."""
    text = session["input"]
    index = max(0, min(len(text), index))
    if not text:
        place_caret(session, 0)
        return
    if index == len(text) or (index > 0 and not text[index].isalnum() and text[index - 1].isalnum()):
        index -= 1  # A click on the right edge of a word still selects that word.
    if text[index].isalnum():
        start, end = word_left(text, index + 1), word_right(text, index)
    else:
        start, end = index, index + 1
        while start > 0 and not text[start - 1].isalnum():
            start -= 1
        while end < len(text) and not text[end].isalnum():
            end += 1
    place_caret(session, start)
    extend_selection(session, end)


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
        extend_selection(session, new)
    else:
        place_caret(session, new)


def select_all(session: dict) -> None:
    session["anchor"], session["cursor"] = 0, len(session["input"])
    session.pop("goal_x", None)


def cut(session: dict) -> str:
    text = selected_text(session)
    if text:
        _remember(session, "cut")
        _delete_selection(session)
    return text


def select_line(session: dict, index: int) -> None:
    """Triple-click: the whole line (between newlines) holding index."""
    text = session["input"]
    end = text.find("\n", index)
    place_caret(session, text.rfind("\n", 0, index) + 1)
    extend_selection(session, len(text) if end < 0 else end)


# ---- Rows of the wrapped box ----

def locate(lines: list, index: int) -> tuple[int, int]:
    """(row, column) of a character index: the last row starting at or before it. An index at a
    soft wrap belongs to the next row, which is where the caret draws."""
    row = max((n for n, (start, _) in enumerate(lines) if start <= index), default=0)
    return row, index - lines[row][0] if lines else 0


def row_end(lines: list, row: int) -> int:
    """The last caret column on a row: before a soft wrap's trailing spaces, or one before the end of a
    word split across rows (its end is the next row's start), after a hard line's last character."""
    start, line = lines[row]
    if not (row + 1 < len(lines) and lines[row + 1][0] == start + len(line)):
        return len(line)
    stripped = line.rstrip()
    return len(stripped) if stripped != line else max(0, len(line) - 1)


def index_at(lines: list, row: int, x: float, width) -> int:
    """The index whose caret position on `row` is nearest x; rows past either end are clamped."""
    if not lines:
        return 0
    row = min(max(row, 0), len(lines) - 1)
    start, line = lines[row]
    best, distance = 0, abs(x)
    for column in range(1, row_end(lines, row) + 1):
        gap = abs(width(line[:column]) - x)
        if gap > distance:
            break  # Widths only grow along a row.
        best, distance = column, gap
    return start + best


def caret_x(lines: list, index: int, width) -> float:
    row, column = locate(lines, index)
    return width(lines[row][1][:column]) if lines else 0.0


def vertical(session: dict, lines: list, width, down: bool, select: bool = False) -> None:
    """Up or down one wrapped row, aiming at the column the caret had when the run of up and down
    moves began (the goal column), as text editors do; any other move or edit ends the run. Past
    the first row it goes to the start, past the last to the end."""
    cursor = session["cursor"]
    goal = session.get("goal_x")
    goal_x = goal[1] if goal and goal[0] == cursor else caret_x(lines, cursor, width)
    row = locate(lines, cursor)[0] + (1 if down else -1)  # From the caret; without shift it drops a selection.
    new = 0 if row < 0 else len(session["input"]) if row >= len(lines) else index_at(lines, row, goal_x, width)
    if select:
        extend_selection(session, new)
    else:
        place_caret(session, new)
    session["goal_x"] = (session["cursor"], goal_x)


def first_row(first: int, caret_row: int, visible: int, total: int) -> int:
    """The first row to show so that the caret's row is in view, moving as little as possible."""
    first = max(0, min(first, total - visible))
    if caret_row < first:
        return caret_row
    if caret_row >= first + visible:
        return caret_row - visible + 1
    return first
