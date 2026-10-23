"""Pick numbers for asset search results, so the model imports #9 instead of copying an id.

Hosted Fast copied BlenderKit's 36-character ids wrongly ('1d00af94-b833-43e-ae6e-53e40a3f'): the library
answered "no asset" three or four times and the turn ended with nothing built (a vtuber room and a
typewriter, 2026-10-23). Every row of every search, whatever the library, takes the conversation's next
number (#1-#8, then #9-#16 from any source), so one number never means two assets; an asset shown
again keeps its number, so a repeated search reads the same (repeats.py answers an unchanged read
without running it). import_asset(pick=9) resolves here to the row's source and id; `near` finds
the one recent result a mistyped id was meant to be, and never a different asset: only a UUID may be
off by a few characters (Wood048 and Wood049 are two assets).

Kept in the session beside the conversation and saved with it (conversations._PERSISTED): numbers
outlive the turn and a reopened conversation. Workers' searches run in the parent and number into
their wave's conversation (`bound`), so theirs never collide with the orchestrator's either. Main
thread, but locked: saves read the table from the agent thread.
"""

import contextlib
import copy
import re
import threading

from . import state

KEEP = 400       # Rows kept, the latest shown; an older number is gone but never reused (`next` only grows).
RECENT = 48      # The latest rows a mistyped id is compared with: six searches.
DISTANCE = 4     # Edits a mangled UUID may be off by.
# A conversation saved before pick numbers showed rows "1." to "12." (each library's limit), positional
# per search: its new numbers start clear of those.
FIRST_AFTER_OLD = 101
HINT = "Import by number: import_asset(pick=N)."
_NUMBER = re.compile(r"#?\s*(\d{1,6})")
_UUID = re.compile(r"[0-9a-f]{8}[0-9a-f-]{24,28}")  # A shown id that is a UUID (BlenderKit's), 32-36 characters.
# A typed id that may be a UUID copied with characters dropped or doubled: hosted Fast's
# '1d00af94-b833-43e-ae6e-53e40a3f' (2026-10-23) is 31 characters.
_MANGLED = re.compile(r"[0-9a-f-]{20,40}")
_SHOWN = re.compile(r"^#(\d{1,6}) ", re.M)
_lock = threading.Lock()
_bound = threading.local()


def new() -> dict:
    return {"next": 1, "rows": []}


def valid(table) -> bool:
    """A saved table's shape: the file is outside our control once written."""
    if not isinstance(table, dict) or type(table.get("next")) is not int or not isinstance(table.get("rows"), list):
        return False
    return all(isinstance(r, dict) and type(r.get("n")) is int and 0 < r["n"] < table["next"]
               and all(isinstance(r.get(k), str) for k in ("source", "id", "name")) for r in table["rows"])


@contextlib.contextmanager
def bound(session: dict | None):
    """Numbers and picks inside go to `session`'s table: a worker's search or import runs on the main
    thread, where state.session() may since be another conversation (lifecycle.switch_to). None: the
    one on screen."""
    previous = getattr(_bound, "session", None)
    _bound.session = session
    try:
        yield
    finally:
        _bound.session = previous


def _table(session=None) -> dict:
    if session is None:
        session = getattr(_bound, "session", None)
    if session is None:
        session = state.session()
    table = session.get("asset_picks")
    if not isinstance(table, dict) or "rows" not in table:
        table = session["asset_picks"] = new()
    return table


def resumed(messages: list) -> dict:
    """The table for a loaded conversation without one (saved before pick numbers, or damaged): numbers
    start at FIRST_AFTER_OLD, and past any #N its search results already show."""
    shown = [int(n) for message in messages if isinstance(message, dict) and message.get("role") == "tool"
             for text in _texts(message.get("content")) for n in _SHOWN.findall(text)]
    return {"next": max([FIRST_AFTER_OLD, *(n + 1 for n in shown)]), "rows": []}


def _texts(content) -> list:
    if isinstance(content, str):
        return [content]
    parts = content if isinstance(content, list) else []
    return [p["text"] for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str)]


def snapshot(session: dict) -> dict:
    """A copy of the session's table to save, taken under the lock: searches add rows on the main thread."""
    with _lock:
        table = session.get("asset_picks")
        return copy.deepcopy(table) if valid(table) else new()


def number(source: str, asset_id: str, name: str = "") -> int:
    """The pick number of one search result: the number it already has, else the next."""
    asset_id, name = str(asset_id), " ".join(str(name or "").split())[:80]
    with _lock:
        table = _table()
        rows = table["rows"]
        for index, row in enumerate(rows):
            if row["source"] == source and row["id"] == asset_id:
                rows.append(rows.pop(index))  # Shown again: recent again.
                row["name"] = name or row["name"]
                return row["n"]
        row = {"n": table["next"], "source": source, "id": asset_id, "name": name or asset_id}
        table["next"] += 1
        rows.append(row)
        del rows[:-KEEP]
        return row["n"]


def parse(value) -> int | None:
    """A pick number as the model writes one: 9, 9.0, "9" or "#9". None when it is not one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        match = _NUMBER.fullmatch(value.strip())
        return int(match.group(1)) if match else None
    return None


def find(n: int) -> dict | None:
    with _lock:
        row = next((r for r in _table()["rows"] if r["n"] == n), None)
        return dict(row) if row else None


def with_id(asset_id) -> dict | None:
    """The one result shown with exactly this id, whatever its source; None when none or several."""
    with _lock:
        rows = [dict(r) for r in _table()["rows"] if r["id"] == asset_id]
    return rows[0] if len(rows) == 1 else None


def near(asset_id: str, source: str = "") -> list[dict]:
    """The recent results (of `source`, else any) a failed id may have been meant as, closest first:
    the same id but for case and spacing; or, for a shown UUID only, a hex id at most DISTANCE edits
    from it or starting with its first 8 characters. Never a different id that merely looks alike
    (a library ref one word off, Wood048 for Wood049), and not one shown with exactly this id: that id
    itself failed."""
    wanted = _spaced(asset_id)
    with _lock:
        rows = [dict(r) for r in _table()["rows"] if not source or r["source"] == source][-RECENT:]
    found = []
    for row in rows:
        if row["id"] == asset_id:
            continue
        have = _spaced(row["id"])
        if have == wanted:
            found.append((0, row["n"], row))
        elif _UUID.fullmatch(have) and _MANGLED.fullmatch(wanted):
            distance = _distance(wanted, have, DISTANCE)
            if distance <= DISTANCE or wanted[:8] == have[:8]:
                found.append((distance, row["n"], row))
    return [row for *_, row in sorted(found, key=lambda f: f[:2])]


def _spaced(value) -> str:
    return " ".join(str(value).split()).casefold()


def any_rows(source: str = "") -> bool:
    with _lock:
        return any(not source or r["source"] == source for r in _table()["rows"])


def _distance(a: str, b: str, limit: int) -> int:
    """Levenshtein distance between a and b, or limit + 1 as soon as it must be more than limit."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    previous = list(range(len(b) + 1))
    for i, char in enumerate(a, 1):
        current = [i]
        for j, other in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (char != other)))
        if min(current) > limit:
            return limit + 1
        previous = current
    return previous[-1]


def footer(image, blank: bool = False) -> str:
    """The line after a search's rows: the thumbnails carry the same numbers, and how to import one."""
    if not image:
        return HINT
    return ("Image attached: thumbnails labelled with these numbers" + (" (blank: no preview)" if blank else "")
            + ". " + HINT)
