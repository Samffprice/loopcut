"""Long tool results: the model is shown their start, the conversation keeps all of them, read_result finds the rest.

Every request re-sends the conversation, and providers bill new tokens at about ten times the cached price: a long
result is paid in full once, then re-sent on every later step of the turn (a workspace turn's 200k budget keeps
every one of them whole). In 12 local conversations (2026-10-26) run_python was 41% of tool-result tokens and shell
22%, its "Scene changes" averaged ~1.1k tokens, often a line per object for one change, and inspect_api,
get_scene_info and get_object_info often filled their 8,000 characters. So a result is cut as it is made
(agent.run_loop, in every loop: single, orchestrator, workspace and workers) and never edited after, which keeps
the history append-only for the cache:

- Past LIMIT characters the model is shown the head, whole lines first, and one pointer line naming where the rest
  is: `[Loopcut: 6,412 more characters stored as r12: read_result("r12", grep="…") or lines="41-70"]`.
- A failed call keeps its end too: the exception, the last frames and Loopcut's NOTE come last, so the pointer goes
  between the head and ERROR_TAIL characters of tail. Any result keeps short closing paragraphs (NOTE_TAIL): what
  a worker's Blender appends after a tool ran (an undone change, a repaired mesh) or a guard's note.
- run_python's "Scene changes" always stays in view (a step's record reads it: context._run_python_parts), compact
  past CHANGES_LIMIT (scene_diff.compact): one line for a change objects share, lines in order while they fit, then
  one line saying how many more and of which kinds; only the output before it is cut.
- What Loopcut appends to a result after the tool (the ledger's repeat notes, the look notes, the step budget's)
  is not the tool's text and is never cut: agent passes the tool's own text only.
- Kept whole (WHOLE): read_result's own answers, search_assets' picks and credits (asset_search: an import id or
  the attribution must not go), the orchestrator's reports and checks, which waves already budget per worker, and
  a worker's check_brief, whose every failing check it must see.

The whole text is stored with the conversation, as <conversation folder>/results/r<n>.json, so it survives a
restart and goes with the conversation; a worker's in its own job folder (conversations.FOLDERS), where its own
loop reads it. Ids count up per folder and are never reused, also after a checkpoint restore cut the messages
that named them. Lines are numbered as display lines: a line over WRAP characters (one-line JSON, a long print)
counts as several, cut after a comma or space, so a range or a match is never a whole 8,000-character line.

Pure Python, no bpy: the context replay (out/lean-context/tools/replay_cost_t.py) prices stored conversations
with cut().
"""

import json
import re
import threading
from pathlib import Path

from . import scene_diff

LIMIT = 1200          # Characters of a tool's own text shown before it is cut; read_result returns at most as many.
ERROR_TAIL = 500      # Characters of a failed call's end shown after the pointer: its exception and last frames.
NOTE_TAIL = 400       # Closing paragraphs any result keeps after the pointer, up to this many characters.
CHANGES_LIMIT = 900   # Characters of "Scene changes" lines shown when there are more (scene_diff.compact).
WRAP = 300            # A line longer than this is numbered, shown and searched as several.
READ_LINES = 30       # Lines a pointer suggests reading at once.
TOOL = "read_result"
FOLDER = "results"
WHOLE = frozenset({TOOL, "search_assets", "asset_search", "delegate", "checks", "check_brief", "report_back"})

SCHEMA = {"type": "function", "function": {
    "name": TOOL,
    "title": "Reading an earlier result",
    "description": (
        "The rest of a result Loopcut cut short, by the id its [Loopcut: ...] line names: `grep` finds lines (words "
        "or a regex, any case), `lines` reads a range (\"41-70\"). At most 1,200 characters a call."),
    "parameters": {"type": "object", "properties": {
        "id": {"type": "string", "description": "e.g. r12"},
        "grep": {"type": "string"},
        "lines": {"type": "string"},
    }, "required": ["id"]},
}}

_SLOT = "r\x00"  # Stands for the id in a cut result until the store gives it one.
_REF = re.compile(r"^r\d+$")
_POINTER = re.compile(r'\n?\[Loopcut: [\d,]+ more characters stored as (r\d+): read_result\("r\d+", grep="…"\) '
                      r'or lines="\d+-\d+"\]')
_CHANGES = re.compile(r"^Scene changes(?: \(|: none)", re.MULTILINE)  # run_python's section (scene_diff.for_model).
_lock = threading.Lock()
_next: dict[Path, int] = {}  # Folder -> the next id's number, read from the folder the first time.


class StoreError(Exception):
    """A read the model asked for that cannot be answered; the message says why, in its words."""


# ------------------------------------------------------------------ lines

def spans(text: str) -> list[tuple[int, int]]:
    """(start, end) of each display line of text: its lines, each over WRAP characters cut after the last comma
    or semicolon before WRAP, else the last space, past half of it (else at WRAP: one-line JSON breaks between
    values, not inside a name). The pieces join back into the text exactly, so a head ends on a piece's end and
    a range reads what the text said."""
    out, start = [], 0
    for line in text.split("\n"):
        end = start + len(line)
        while end - start > WRAP:
            piece = text[start:start + WRAP]
            cut = max(piece.rfind(","), piece.rfind(";"))
            cut = cut if cut > WRAP // 2 else piece.rfind(" ")
            step = cut + 1 if cut > WRAP // 2 else WRAP
            out.append((start, start + step))
            start += step
        out.append((start, end))
        start = end + 1
    return out


def _line_at(lines: list, offset: int) -> int:
    """The 1-based number of the display line that holds offset."""
    return next((n for n, (start, end) in enumerate(lines, 1) if offset <= end), len(lines))


def _head_end(lines: list, budget: int) -> int:
    """How many display lines from the top fit budget characters: at least one."""
    count = 1
    while count < len(lines) and lines[count][1] <= budget:
        count += 1
    return count


def _tail_start(text: str, lines: list, failed: bool) -> int:
    """The first display line a cut shows after its pointer, len(lines) for none: its closing paragraphs (each
    after a blank line) within NOTE_TAIL characters, and for a failed call at least its last ERROR_TAIL."""
    end, index = len(text), len(lines)
    for start in range(len(lines) - 1, 0, -1):
        if end - lines[start][0] > max(NOTE_TAIL, ERROR_TAIL if failed else 0):
            break
        if failed and end - lines[start][0] <= ERROR_TAIL or text[lines[start][0] - 2:lines[start][0]] == "\n\n":
            index = start
    return index


def pointer(ref: str, more: int, first: int, last: int) -> str:
    """The one line a cut result carries: how much is not shown, where it is, and the first range not shown."""
    return (f'[Loopcut: {more:,} more characters stored as {ref}: read_result("{ref}", grep="…") or '
            f'lines="{first}-{min(last, first + READ_LINES - 1)}"]')


def ref_in(text: str) -> str:
    """The id a result's pointer names, or ""."""
    found = _POINTER.search(text or "")
    return found.group(1) if found else ""


def unpointed(text: str) -> tuple[str, str]:
    """(text without its pointer line, the id it named or ""): a step's record says the id once, in its own words
    (context._call_lines), rather than a share of the pointer."""
    ref = ref_in(text)
    return (_POINTER.sub("", text, count=1), ref) if ref else (text, "")


# ------------------------------------------------------------------ cutting

def _changes(text: str, start: int, ref: str) -> tuple[str, int] | None:
    """The "Scene changes" section starting at `start` as the model is shown it (scene_diff.compact), and where it
    ends in text; None when it is short enough to show as it is."""
    end = text.find("\n\n", start)
    end = len(text) if end < 0 else end
    section = text[start:end]
    header, _, body = section.partition("\n")
    if len(section) <= CHANGES_LIMIT or not body:
        return None
    shown, left, kinds = scene_diff.compact(body.split("\n"), CHANGES_LIMIT)
    if left:
        shown.append(f"... and {left} more line{'s' if left > 1 else ''} in {ref}: {kinds}")
    compacted = "\n".join([header, *shown])
    return (compacted, end) if len(compacted) < len(section) else None


def _cut(text: str, tool: str, ref: str, failed: bool) -> tuple[str, int] | None:
    """(the cut result, the first display line it does not show) or None: see cut()."""
    if not text or len(text) <= LIMIT or tool.rsplit("__", 1)[-1].rsplit(".", 1)[-1] in WHOLE:
        return None
    lines = spans(text)
    found = [m.start() for m in _CHANGES.finditer(text)]  # The last: a print may start a line "Scene changes (".
    start = found[-1] if found else len(text)
    # The output is what comes before the section's paragraph: prints, a traceback, Loopcut's notes on them.
    para = max(text.rfind("\n\n", 0, start), 0) if found else len(text)
    output = [span for span in lines if span[1] <= para]
    kept = None
    if para > LIMIT and output:
        tail = _tail_start(text[:para], output, failed)
        room = LIMIT - (para - output[tail][0] if tail < len(output) else 0)
        head = _head_end(output, max(room, LIMIT // 2))
        kept = (head, tail) if head < tail else None
    section = _changes(text, start, ref) if found else None
    if kept is None and section is None:
        return None
    # Before the pointer, then after it: the output's head, and its tail with the rest; or, when only the scene
    # changes were cut, everything up to their compact lines, then what follows them.
    if kept:
        head, tail = kept
        before = text[:output[head - 1][1]]
        resumes = output[tail][0] if tail < len(output) else para
        after = ("\n\n" if text[resumes - 2:resumes] == "\n\n" else "\n") + text[resumes:para] if resumes < para else ""
        first, last = head + 1, tail
        if section:
            after += text[para:start] + section[0] + text[section[1]:]
        else:
            after += text[para:]
    else:
        before = text[:start] + section[0]
        after = text[section[1]:]
        first, last = _line_at(lines, start) + 1, _line_at(lines, section[1])
    mark = pointer(ref, max(len(text) - len(before) - len(after), 0), first, max(last, first))
    return f"{before}\n{mark}{after}", first


def cut(text: str, tool: str, ref: str, *, failed: bool = False) -> tuple[str, str | None]:
    """(what the model is shown, what the store keeps as `ref`: text, or None when nothing was cut). `tool` is the
    tool's name (a cloud tool's too); `failed` a call that failed, whose end is kept (module docstring). The
    pointer line names ref."""
    done = _cut(text, tool, ref, failed)
    return (text, None) if done is None else (done[0], text)


# ------------------------------------------------------------------ the store

def keep(where, text: str, tool: str, *, failed: bool = False) -> str:
    """text as the model is shown it, the whole of it stored when cut, in the folder `where()` returns (a
    conversation's results/, conversations.results_folder; asked only then). OSError when the store cannot be
    written: the caller shows the text whole rather than point at nothing."""
    done = _cut(text, tool, _SLOT, failed)
    if done is None:
        return text
    shown, first = done
    folder = where()
    with _lock:
        folder.mkdir(parents=True, exist_ok=True)
        if folder not in _next:
            numbers = [int(p.stem[1:]) for p in folder.glob("r*.json") if _REF.match(p.stem)]
            _next[folder] = max(numbers, default=0) + 1
        ref = f"r{_next[folder]}"
        _next[folder] += 1
        body = {"tool": tool, "text": text, "next": first}
        temporary = folder / f"{ref}.json.tmp"
        temporary.write_text(json.dumps(body), encoding="utf-8")
        temporary.replace(folder / f"{ref}.json")
    return shown.replace(_SLOT, ref)


def _load(folder: Path, ref: str) -> dict:
    ref = str(ref or "").strip()
    if not _REF.match(ref):
        raise StoreError(f"{ref!r} is not a stored result: ids look like r12, from a cut result's last line.")
    path = folder / f"{ref}.json"
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        held = sorted((int(p.stem[1:]) for p in folder.glob("r*.json") if _REF.match(p.stem))) if folder.is_dir() else []
        raise StoreError(f"No stored result {ref} in this conversation"
                         + (f"; it has r{held[0]} to r{held[-1]}." if held else "; nothing was cut short in it."))
    except (OSError, ValueError) as ex:
        raise StoreError(f"Could not read {ref}: {ex}") from ex
    if not isinstance(body, dict) or not isinstance(body.get("text"), str):
        raise StoreError(f"Could not read {ref}: the stored file is damaged.")
    return body


def _range(lines: str, total: int, default: int) -> tuple[int, int]:
    """(first, last) line, 1-based and within total, of a `lines` argument: "41-70", "41", "41-", "-70"."""
    if lines in (None, ""):
        return default, total
    found = re.match(r"^\s*(\d*)\s*(?:(-|–|:|\.\.)\s*(\d*))?\s*$", str(lines))
    if not found or not (found.group(1) or found.group(3)):
        raise StoreError(f'lines is a range such as "41-70", not {lines!r}.')
    first = int(found.group(1) or 1)
    last = int(found.group(3)) if found.group(3) else (total if found.group(2) else first)
    if first > total:
        raise StoreError(f"The result has {total} lines.")
    return max(first, 1), min(max(last, first), total)


def whole(folder: Path, ref: str) -> str:
    """The whole text stored as ref: what a conversation log sends beside the cut text the model saw
    (session_logs), so support reads every result as the tool gave it. StoreError when it cannot be read."""
    return _load(folder, ref)["text"]


def read(folder: Path, ref: str, grep: str | None = None, lines: str | None = None) -> str:
    """What read_result answers: the lines asked for, numbered, within LIMIT characters, then where to go on. With
    neither grep nor lines, from the first line the cut result did not show. StoreError for a read that cannot be
    answered."""
    body = _load(folder, ref)
    text, ref = body["text"], str(ref).strip()
    spans_ = spans(text)
    total = len(spans_)
    grep = str(grep or "").strip()
    first, last = _range(lines, total, 1 if grep else int(body.get("next") or 1))
    wanted = range(first, last + 1)
    if grep:
        try:
            pattern = re.compile(grep, re.IGNORECASE)
        except re.error:
            pattern = re.compile(re.escape(grep), re.IGNORECASE)
        wanted = [n for n in wanted if pattern.search(text[spans_[n - 1][0]:spans_[n - 1][1]])]
        if not wanted:
            within = f" in lines {first}-{last}" if lines else ""
            return f"No line of {ref} ({total} lines) matches {grep!r}{within}."
    room, out, shown = LIMIT - 160, [], 0
    for n in wanted:
        line = f"{n}: {text[spans_[n - 1][0]:spans_[n - 1][1]]}"
        if out and shown + len(line) + 1 > room:
            break
        out.append(line)
        shown += len(line) + 1
    left = list(wanted)[len(out):]
    if grep:
        head = f"{ref}: {len(wanted)} of {total} lines match {grep!r}" + (f" in lines {first}-{last}" if lines else "")
        more = (f'… {len(left)} more matches, the next on line {left[0]}: narrow grep, or read_result("{ref}", '
                f'grep={json.dumps(grep)}, lines="{left[0]}-{last}").') if left else ""
    else:
        head = f"{ref}, lines {first}-{first + len(out) - 1} of {total}"
        more = (f'Next: read_result("{ref}", lines="{left[0]}-{min(last, left[0] + READ_LINES - 1)}").'
                if left else "")
    return "\n".join([head + ":", *out] + ([more] if more else []))
