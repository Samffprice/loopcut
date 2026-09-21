"""Turns session state into a display list. No bpy and no GPU in here.

Coordinates are region pixels with the origin at the TOP-left and y growing down; draw.py
flips them. Every primitive carries an id, so the list can be dumped as JSON to debug a frame
as text: an invisible or misplaced element shows up as a bad rect or a color equal to its
background.
"""

import re

from .. import state
from . import theme as T

_FENCE = re.compile(r"```[^\n]*\n(.*?)(?:```|\Z)", re.DOTALL)
_TOKEN = re.compile(r"\S+\s*|\s+")

STATUS_GLYPH = {
    "awaiting": ("●", T.WARN),
    "running": ("●", T.ACCENT),
    "done": ("✓", T.OK),
    "failed": ("✕", T.ERROR),
    "rejected": ("–", T.TEXT_FAINT),
}


def wrap(text: str, font: str, size: int, max_width: float, measure) -> list[tuple[int, str]]:
    """Greedy word wrap. Returns (offset of the line in `text`, line) so a caret can be placed."""
    lines: list[tuple[int, str]] = []
    offset = 0
    for paragraph in text.split("\n"):
        start, line = offset, ""
        for token in _TOKEN.findall(paragraph):
            if line and measure(font, size, (line + token).rstrip()) > max_width:
                lines.append((start, line))
                start, line = start + len(line), ""
            while len(token) > 1 and measure(font, size, token.rstrip()) > max_width:
                low, high = 1, len(token) - 1
                while low < high:  # Longest prefix that fits.
                    mid = (low + high + 1) // 2
                    if measure(font, size, token[:mid]) <= max_width:
                        low = mid
                    else:
                        high = mid - 1
                lines.append((start, token[:low]))
                start, token = start + low, token[low:]
            line += token
        lines.append((start, line))
        offset += len(paragraph) + 1
    return lines


def split_markdown(text: str) -> list[tuple[str, str]]:
    """[('p', prose) | ('code', source)], split on fenced code blocks."""
    segments, last = [], 0
    for match in _FENCE.finditer(text):
        prose = text[last:match.start()].strip("\n")
        if prose:
            segments.append(("p", prose))
        segments.append(("code", match.group(1).rstrip("\n")))
        last = match.end()
    tail = text[last:].strip("\n")
    if tail:
        segments.append(("p", tail))
    return segments


class Frame:
    def __init__(self, width: int, height: int, scale: float, measure, session=None, checkpoints=None,
                 ui=None):
        self.width, self.height, self.scale, self.measure = width, height, scale, measure
        self.session = session or {}
        self.checkpoints = checkpoints or {}  # checkpoint id -> 'ok' | 'expired' | 'missing'
        self.ui = ui or {}

        self.prims: list[dict] = []
        self.hits: list[dict] = []

    def px(self, value: float) -> int:
        return round(value * self.scale)

    def line_height(self, size: int) -> int:
        return round(size * T.LINE_HEIGHT)

    def rect(self, id, x, y, w, h, color, radius=0, border=0, border_color=T.NONE):
        self.prims.append({"t": "rect", "id": id, "x": x, "y": y, "w": w, "h": h, "color": color,
                           "radius": radius, "border": border, "border_color": border_color})

    def text(self, id, x, y, text, size, color, font="ui"):
        """`y` is the top of the line box; the box is line_height(size) tall."""
        if text:
            self.prims.append({"t": "text", "id": id, "x": x, "y": y, "h": self.line_height(size),
                               "text": text, "size": size, "color": color, "font": font})

    def hit(self, id, x, y, w, h, action, tip: str = ""):
        """`tip` is shown when the mouse rests on the area; lines are separated by newlines."""
        self.hits.append({"id": id, "x": x, "y": y, "w": w, "h": h, "action": action, "tip": tip})

    def ring(self, id, x, y, diameter, share, thickness, color, track=T.CARD_BORDER):
        """A donut, filled clockwise from the top for `share` (0-1) of the way."""
        self.prims.append({"t": "ring", "id": id, "x": x, "y": y, "w": diameter, "h": diameter,
                           "share": max(0.0, min(1.0, share)), "thickness": thickness, "color": color, "track": track})

    def paragraph(self, id, x, y, width, text, size, color, font="ui") -> int:
        line_height = self.line_height(size)
        for n, (_, line) in enumerate(wrap(text, font, size, width, self.measure)):
            self.text(f"{id}.l{n}", x, y + n * line_height, line.rstrip(), size, color, font)
        return (n + 1) * line_height

    def code_block(self, id, x, y, width, source, max_lines=None) -> int:
        size, pad = self.px(T.FONT_SIZE_CODE), self.px(8)
        lines = wrap(source, "mono", size, width - 2 * pad, self.measure)
        hidden = 0
        if max_lines and len(lines) > max_lines:
            hidden, lines = len(lines) - max_lines, lines[:max_lines]
        line_height = self.line_height(size)
        height = len(lines) * line_height + 2 * pad + (line_height if hidden else 0)
        self.rect(f"{id}.bg", x, y, width, height, T.CODE_BG, self.px(T.RADIUS_SMALL), 1, T.CARD_BORDER)
        for n, (_, line) in enumerate(lines):
            self.text(f"{id}.l{n}", x + pad, y + pad + n * line_height, line.rstrip(), size,
                      T.CODE_TEXT, "mono")
        if hidden:
            self.text(f"{id}.more", x + pad, y + pad + len(lines) * line_height,
                      f"… {hidden} more lines", size, T.TEXT_FAINT, "mono")
        return height

    def button(self, id, x, y, label, fill, color, action) -> int:
        size, pad_x = self.px(T.FONT_SIZE_SMALL), self.px(10)
        height = self.px(24)
        width = round(self.measure("ui", size, label)) + 2 * pad_x
        self.rect(id, x, y, width, height, fill, self.px(T.RADIUS_SMALL))
        self.text(f"{id}.label", x + pad_x, y + (height - self.line_height(size)) // 2, label, size, color)
        self.hit(id, x, y, width, height, action)
        return width


def _checkpoint_row(f: Frame, n: int, item: dict, x, y, width) -> int:
    """Under a user message: the way back to the scene as it was before that message."""
    small = f.px(T.FONT_SIZE_SMALL)
    row = f.line_height(small)
    if f.checkpoints.get(item["checkpoint"], "ok") != "ok":
        f.text(f"item{n}.checkpoint.expired", x, y + f.px(6), "Checkpoint expired", small, T.TEXT_FAINT)
        return row + f.px(6)
    if f.session.get("confirm_restore") != n:
        label = "Restore checkpoint"
        label_x = x + width - round(f.measure("ui", small, label))
        f.text(f"item{n}.checkpoint.restore", label_x, y + f.px(6), label, small, T.TEXT_MUTED)
        f.hit(f"item{n}.checkpoint.restore", label_x - f.px(4), y, x + width - label_x + f.px(8),
              row + f.px(10), ("restore_ask", n))
        return row + f.px(6)
    cursor = y + f.px(8)
    cursor += f.paragraph(f"item{n}.checkpoint.ask", x, cursor, width,
                          "Restore the scene to before this message? Your current state is saved first, "
                          "so this can be undone.", small, T.TEXT_MUTED)
    cursor += f.px(6)
    used = f.button(f"item{n}.checkpoint.confirm", x, cursor, "Restore", T.BUTTON, T.BUTTON_TEXT,
                    ("restore_confirm", n))
    f.button(f"item{n}.checkpoint.cancel", x + used + f.px(8), cursor, "Cancel", T.BUTTON_GHOST, T.TEXT,
             ("restore_cancel", n))
    return cursor + f.px(24) - y


def _item_user(f: Frame, n: int, item: dict, x, y, width) -> int:
    pad, size, small = f.px(T.CARD_PAD), f.px(T.FONT_SIZE), f.px(T.FONT_SIZE_SMALL)
    mark = len(f.prims)
    text_width = width - 2 * pad
    if item.get("queued"):  # Sent while the agent was working: it goes in at the agent's next step.
        tag = "queued"
        tag_width = round(f.measure("ui", small, tag))
        f.text(f"item{n}.queued", x + width - pad - tag_width, y + pad, tag, small, T.TEXT_FAINT)
        text_width -= tag_width + f.px(8)
    text_height = f.paragraph(f"item{n}.user", x + pad, y + pad, text_width, item["text"], size, T.TEXT) \
        if item["text"] else 0
    if item.get("images"):
        text_height += f.px(4) if item["text"] else 0
        text_height += _image_chips(f, f"item{n}.image", x + pad, y + pad + text_height, width - 2 * pad,
                                    item["images"], removable=False)
    if item.get("checkpoint"):
        text_height += _checkpoint_row(f, n, item, x + pad, y + pad + text_height, width - 2 * pad)
    height = text_height + 2 * pad
    f.prims.insert(mark, {"t": "rect", "id": f"item{n}.user.card", "x": x, "y": y, "w": width, "h": height,
                          "color": T.CARD, "radius": f.px(T.RADIUS), "border": 1,
                          "border_color": T.CARD_BORDER})
    return height


SHIMMER_PERIOD = 1.8   # Seconds for the highlight to sweep the word once.
SHIMMER_WIDTH = 0.3    # Of the word's length, the bright part of the sweep.
DOT_PERIOD = 0.45      # Seconds per step of the dots: . .. ...


def _mix(a, b, t: float):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(4))


def thinking(f: Frame, id: str, x, y, size, word: str = "Thinking") -> int:
    """The waiting line, animated the way people expect: a highlight sweeps along the word and
    the dots count up. Driven by ui["now"]; mainthread redraws while a turn runs. Returns the
    line height."""
    now = float(f.ui.get("now") or 0.0)
    sweep = (now % SHIMMER_PERIOD) / SHIMMER_PERIOD * (1 + 2 * SHIMMER_WIDTH) - SHIMMER_WIDTH
    cx = x
    for i, ch in enumerate(word):
        along = i / max(1, len(word) - 1)
        bright = max(0.0, 1 - abs(along - sweep) / SHIMMER_WIDTH)
        f.text(f"{id}.c{i}", cx, y, ch, size, _mix(T.TEXT_FAINT, T.TEXT, bright))
        cx += f.measure("ui", size, ch)
    f.text(f"{id}.dots", cx, y, "." * (int(now / DOT_PERIOD) % 3 + 1), size, T.TEXT_FAINT)
    return f.line_height(size)


def _item_assistant(f: Frame, n: int, item: dict, x, y, width) -> int:
    size, cursor = f.px(T.FONT_SIZE), y
    segments = split_markdown(item["text"])
    if not segments and item.get("streaming"):
        return thinking(f, f"item{n}.thinking", x, y, size)
    for k, (kind, body) in enumerate(segments):
        if k:
            cursor += f.px(6)
        if kind == "code":
            cursor += f.code_block(f"item{n}.code{k}", x, cursor, width, body)
        else:
            cursor += f.paragraph(f"item{n}.p{k}", x, cursor, width, body, size, T.TEXT)
    return cursor - y


def _item_tool(f: Frame, n: int, item: dict, x, y, width) -> int:
    pad, size, small = f.px(T.CARD_PAD), f.px(T.FONT_SIZE), f.px(T.FONT_SIZE_SMALL)
    status = item["status"]
    glyph, glyph_color = STATUS_GLYPH[status]
    awaiting = status == "awaiting"
    expanded = awaiting or item.get("expanded", False)
    mark = len(f.prims)
    row = f.line_height(size)

    inner_x, inner_w, cursor = x + pad, width - 2 * pad, y + f.px(7)
    f.text(f"item{n}.tool.glyph", inner_x, cursor, glyph, size, glyph_color)
    name_width = round(f.measure("ui", small, item["name"]))
    label_x = inner_x + f.px(18)
    summary = item["summary"] or item["name"]
    label = wrap(summary, "ui", size, inner_w - f.px(18) - name_width - pad - f.px(10),
                 f.measure)[0][1].rstrip()
    if len(label) < len(summary.strip()):
        label += "…"
    f.text(f"item{n}.tool.summary", label_x, cursor, label, size, T.TEXT if awaiting else T.TEXT_MUTED)
    f.text(f"item{n}.tool.name", x + width - pad - name_width, cursor + (row - f.line_height(small)) // 2,
           item["name"], small, T.TEXT_FAINT)
    cursor += row

    if expanded and item["code"]:
        cursor += f.px(6)
        cursor += f.code_block(f"item{n}.tool.code", inner_x, cursor, inner_w, item["code"],
                               T.CODE_PREVIEW_LINES if awaiting else None)
    if expanded and item["output"]:
        cursor += f.px(6)
        cursor += f.paragraph(f"item{n}.tool.output", inner_x, cursor, inner_w, item["output"],
                              f.px(T.FONT_SIZE_CODE), T.ERROR if status == "failed" else T.TEXT_MUTED, "mono")
    if awaiting:
        cursor += f.px(8)
        run_width = f.button(f"item{n}.tool.run", inner_x, cursor, "Run  ⏎", T.BUTTON, T.BUTTON_TEXT,
                             ("approve", n))
        reject_width = f.button(f"item{n}.tool.reject", inner_x + run_width + f.px(8), cursor, "Reject  esc",
                                T.BUTTON_GHOST, T.TEXT, ("reject", n))
        always_x = inner_x + run_width + reject_width + f.px(16)
        # A render or bake asks every time: there is no "always" for it.
        if not item.get("heavy") and always_x + f.measure("ui", small, "Always allow") + f.px(20) <= inner_x + inner_w:
            f.button(f"item{n}.tool.always", always_x, cursor, "Always allow", T.BUTTON_GHOST, T.TEXT_MUTED,
                     ("approve_always", n))
        cursor += f.px(24)
    cursor += f.px(7)

    height = cursor - y
    f.prims.insert(mark, {"t": "rect", "id": f"item{n}.tool.card", "x": x, "y": y, "w": width, "h": height,
                          "color": T.CARD if awaiting else T.BG, "radius": f.px(T.RADIUS), "border": 1,
                          "border_color": T.WARN if awaiting else T.CARD_BORDER})
    if not awaiting:
        f.hit(f"item{n}.tool.card", x, y, width, row + f.px(14), ("toggle", n))
    return height


def _item_error(f: Frame, n: int, item: dict, x, y, width) -> int:
    pad, size = f.px(T.CARD_PAD), f.px(T.FONT_SIZE)
    mark = len(f.prims)
    height = f.paragraph(f"item{n}.error", x + pad, y + pad, width - 2 * pad, item["text"], size, T.ERROR) + 2 * pad
    f.prims.insert(mark, {"t": "rect", "id": f"item{n}.error.card", "x": x, "y": y, "w": width, "h": height,
                          "color": T.ERROR_BG, "radius": f.px(T.RADIUS), "border": 1,
                          "border_color": T.ERROR_BORDER})
    return height


def _item_notice(f: Frame, n: int, item: dict, x, y, width) -> int:
    pad, small = f.px(T.CARD_PAD), f.px(T.FONT_SIZE_SMALL)
    mark = len(f.prims)
    cursor = y + pad
    cursor += f.paragraph(f"item{n}.notice", x + pad, cursor, width - 2 * pad, item["text"], small, T.TEXT_MUTED)
    if item["checkpoint"] and f.checkpoints.get(item["checkpoint"], "ok") == "ok":
        cursor += f.px(8)
        f.button(f"item{n}.notice.action", x + pad, cursor, item["action_label"], T.BUTTON_GHOST, T.TEXT,
                 ("restore", item["checkpoint"]))
        cursor += f.px(24)
    elif item.get("url") and item["action_label"]:
        cursor += f.px(8)
        f.button(f"item{n}.notice.action", x + pad, cursor, item["action_label"], T.BUTTON_GHOST, T.TEXT,
                 ("open_url", n))
        cursor += f.px(24)
    height = cursor + pad - y
    f.prims.insert(mark, {"t": "rect", "id": f"item{n}.notice.card", "x": x, "y": y, "w": width, "h": height,
                          "color": T.BG, "radius": f.px(T.RADIUS), "border": 1, "border_color": T.CARD_BORDER})
    return height


_LIMIT_TITLES = {"session_limit": "Session allowance used", "week_limit": "Weekly allowance used",
                 "model_not_in_plan": "Pro is not in your plan"}


def _item_limit(f: Frame, n: int, item: dict, x, y, width) -> int:
    """Out of allowance: what ran out, what the next plan offers, and the button that gets it.
    The buttons show on the newest card only; older ones are history."""
    pad, size, small = f.px(T.CARD_PAD), f.px(T.FONT_SIZE), f.px(T.FONT_SIZE_SMALL)
    mark = len(f.prims)
    inner_x, inner_w, cursor = x + pad, width - 2 * pad, y + pad
    status, upgrade = item.get("status", ""), item.get("upgrade")
    if status == "done":
        title, body, color = "Upgraded", item["text"], T.OK
    elif status == "waiting":
        title, color = "Waiting for your upgrade", T.WARN
        body = "Finish in the browser. Loopcut picks this conversation up by itself once the plan is active."
    else:
        title, color = _LIMIT_TITLES.get(item["reason"], "Allowance used"), T.WARN
        body = item["text"] + (f"\n{upgrade['note']}" if upgrade and upgrade.get("note") else "")
    f.text(f"item{n}.limit.title", inner_x, cursor, title, size, color)
    cursor += f.line_height(size) + f.px(4)
    cursor += f.paragraph(f"item{n}.limit.text", inner_x, cursor, inner_w, body, small, T.TEXT)
    newest = n == len(f.session["items"]) - 1
    if newest and status != "done" and not f.session.get("busy"):
        cursor += f.px(8)
        bx = inner_x
        if status == "" and upgrade:
            bx += f.button(f"item{n}.limit.upgrade", bx, cursor, upgrade.get("label") or "Upgrade", T.BUTTON,
                           T.BUTTON_TEXT, ("limit_upgrade", n)) + f.px(8)
        if item["reason"] == "model_not_in_plan":
            bx += f.button(f"item{n}.limit.fast", bx, cursor, "Use Fast instead", T.BUTTON_GHOST, T.TEXT,
                           ("limit_fast", n)) + f.px(8)
        f.button(f"item{n}.limit.retry", bx, cursor, "Try again", T.BUTTON_GHOST, T.TEXT_MUTED, ("resume", n))
        cursor += f.px(24)
    height = cursor + pad - y
    f.prims.insert(mark, {"t": "rect", "id": f"item{n}.limit.card", "x": x, "y": y, "w": width, "h": height,
                          "color": T.CARD, "radius": f.px(T.RADIUS), "border": 1, "border_color": T.CARD_BORDER})
    return height


_CHANGE_COLORS = {"+": T.OK, "-": T.ERROR, "~": T.TEXT_MUTED}
CHANGES_PREVIEW_LINES = 6


def _item_changes(f: Frame, n: int, item: dict, x, y, width) -> int:
    """What the turn did to the scene, and the way back: the panel's diff view."""
    pad, size, small = f.px(T.CARD_PAD), f.px(T.FONT_SIZE), f.px(T.FONT_SIZE_SMALL)
    mark = len(f.prims)
    inner_x, inner_w, cursor = x + pad, width - 2 * pad, y + pad
    f.text(f"item{n}.changes.title", inner_x, cursor, "Scene changes", size, T.TEXT)
    count_width = round(f.measure("ui", small, item["text"]))
    f.text(f"item{n}.changes.count", x + width - pad - count_width,
           cursor + (f.line_height(size) - f.line_height(small)) // 2, item["text"], small, T.TEXT_MUTED)
    cursor += f.line_height(size) + f.px(4)

    lines, expanded = item.get("lines", []), item.get("expanded", False)
    shown = lines if expanded or len(lines) <= CHANGES_PREVIEW_LINES else lines[:CHANGES_PREVIEW_LINES - 1]
    code = f.px(T.FONT_SIZE_CODE)
    for k, line in enumerate(shown):
        f.text(f"item{n}.changes.sign{k}", inner_x, cursor, line[:1], code, _CHANGE_COLORS.get(line[:1], T.TEXT_MUTED),
               "mono")
        cursor += f.paragraph(f"item{n}.changes.l{k}", inner_x + f.px(14), cursor, inner_w - f.px(14), line[2:],
                              code, T.TEXT if line[:1] in "+-" else T.TEXT_MUTED, "mono")
    if len(shown) < len(lines) or expanded and len(lines) > CHANGES_PREVIEW_LINES:
        label = "Show less" if expanded else f"Show {len(lines) - len(shown)} more"
        f.text(f"item{n}.changes.more", inner_x + f.px(14), cursor + f.px(2), label, small, T.ACCENT)
        f.hit(f"item{n}.changes.more", inner_x, cursor, inner_w, f.line_height(small) + f.px(4), ("toggle", n))
        cursor += f.line_height(small) + f.px(4)

    undoable = item["checkpoint"] and f.checkpoints.get(item["checkpoint"], "ok") == "ok"
    if not item.get("resolved") and undoable and not f.session.get("busy"):
        cursor += f.px(8)
        keep_width = f.button(f"item{n}.changes.keep", inner_x, cursor, "Keep", T.BUTTON, T.BUTTON_TEXT,
                              ("keep_changes", n))
        f.button(f"item{n}.changes.undo", inner_x + keep_width + f.px(8), cursor, "Undo all", T.BUTTON_GHOST,
                 T.TEXT, ("restore", item["checkpoint"]))
        cursor += f.px(24)
    height = cursor + pad - y
    f.prims.insert(mark, {"t": "rect", "id": f"item{n}.changes.card", "x": x, "y": y, "w": width, "h": height,
                          "color": T.CARD, "radius": f.px(T.RADIUS), "border": 1, "border_color": T.CARD_BORDER})
    return height


_ITEM_LAYOUT = {"changes": _item_changes, "notice": _item_notice, "user": _item_user, "assistant": _item_assistant,
                "tool": _item_tool, "error": _item_error, "limit": _item_limit}


def _chat(f: Frame, session: dict, top: int, bottom: int) -> float:
    """Lays the conversation out at y=0, then slides it into the viewport. Returns max scroll."""
    pad, gap = f.px(T.PAD), f.px(T.GAP)
    x, width = pad, f.width - 2 * pad
    items = list(session["items"])  # Snapshot: the worker thread appends while we draw.
    first_prim, first_hit, cursor = len(f.prims), len(f.hits), 0

    for n, item in enumerate(items):
        cursor += _ITEM_LAYOUT[item["kind"]](f, n, item, x, cursor, width) + gap
    last = items[-1] if items else None
    if session["busy"] and not (last and (last.get("streaming") or last.get("status") == "awaiting")):
        cursor += thinking(f, "chat.working", x, cursor, f.px(T.FONT_SIZE), "Working") + gap

    if not items:
        size = f.px(T.FONT_SIZE)
        middle = (top + bottom) // 2
        needs_setup = bool(f.ui.get("needs_setup"))
        second = "Add an API key to get started." if needs_setup else "Ask for anything in your scene."
        for n, (line, color) in enumerate((("Loopcut", T.TEXT), (second, T.TEXT_MUTED))):
            line_width = f.measure("ui", size, line)
            f.text(f"chat.empty{n}", round((f.width - line_width) / 2), middle + (n - 1) * f.line_height(size),
                   line, size, color)
        if needs_setup:
            label = "Open settings"
            button_width = round(f.measure("ui", f.px(T.FONT_SIZE_SMALL), label)) + 2 * f.px(10)
            f.button("chat.setup", round((f.width - button_width) / 2), middle + f.line_height(size) + f.px(10),
                     label, T.BUTTON, T.BUTTON_TEXT, ("open_settings", None))
        return 0.0

    content, view = cursor - gap + 2 * pad, bottom - top
    max_scroll = max(0.0, content - view)
    scroll = min(max(session["scroll"], 0.0), max_scroll)
    shift = top + pad if content <= view else bottom - content + pad + scroll
    for prim in f.prims[first_prim:]:
        prim["y"] += shift
    for hit in f.hits[first_hit:]:
        hit["y"] += shift
    # Cull what is scrolled away; the header and input paint over any partial overlap.
    f.prims[first_prim:] = [p for p in f.prims[first_prim:] if p["y"] + p["h"] > top and p["y"] < bottom]
    f.hits[first_hit:] = [h for h in f.hits[first_hit:] if h["y"] + h["h"] > top and h["y"] < bottom]
    return max_scroll


def relative_time(then: float, now: float) -> str:
    seconds = max(0, now - then)
    for limit, unit, name in ((60, 1, "just now"), (3600, 60, "min"), (86400, 3600, "h"), (86400 * 30, 86400, "d")):
        if seconds < limit:
            return name if unit == 1 else f"{int(seconds // unit)} {name} ago"
    return f"{int(seconds // (86400 * 30))} mo ago"


def _history(f: Frame, session: dict, ui: dict, top: int, bottom: int) -> None:
    pad, size, small = f.px(T.PAD), f.px(T.FONT_SIZE), f.px(T.FONT_SIZE_SMALL)
    x, width, cursor = pad, f.width - 2 * pad, top + pad
    f.text("history.heading", x, cursor, "Conversations for this file", small, T.TEXT_MUTED)
    cursor += f.line_height(small) + f.px(8)
    rows = ui.get("history") or []
    if not rows:
        f.text("history.empty", x, cursor, "Nothing saved yet.", size, T.TEXT_FAINT)
    card_pad = f.px(T.CARD_PAD)
    row_height = f.line_height(size) + f.line_height(small) + 2 * card_pad
    for n, row in enumerate(rows):
        if cursor + row_height > bottom:
            break
        current = row["id"] == session["id"]
        f.rect(f"history.row{n}", x, cursor, width, row_height, T.CARD if current else T.BG, f.px(T.RADIUS), 1,
               T.ACCENT if current else T.CARD_BORDER)
        title = row.get("title") or "Untitled conversation"
        line = wrap(title, "ui", size, width - 2 * card_pad - f.px(10), f.measure)[0][1].rstrip()
        f.text(f"history.row{n}.title", x + card_pad, cursor + card_pad,
               line + ("…" if len(line) < len(title.strip()) else ""), size, T.TEXT)
        turns = row.get("turns", 0)
        detail = f"{turns} message{'' if turns == 1 else 's'} · {relative_time(row.get('updated', 0), ui.get('now', 0))}"
        f.text(f"history.row{n}.detail", x + card_pad, cursor + card_pad + f.line_height(size),
               detail + ("  ·  open now" if current else ""), small, T.TEXT_MUTED)
        f.hit(f"history.row{n}", x, cursor, width, row_height, ("open_conversation", row["id"]))
        cursor += row_height + f.px(8)


def _input_lines(f: Frame, session: dict, width: int):
    size = f.px(T.FONT_SIZE)
    lines = wrap(session["input"], "ui", size, width, f.measure)
    visible = min(max(len(lines), T.INPUT_MIN_LINES), T.INPUT_MAX_LINES)
    return size, lines, visible


def _context_row_height(f: Frame) -> int:
    return f.px(26)


def _image_chips(f: Frame, id: str, x: int, y: int, width: int, names: list[str], removable: bool,
                 action: str = "remove_attachment") -> int:
    """One chip per attached image, wrapping to as many rows as they need. Returns the height used."""
    small, chip_h, gap = f.px(T.FONT_SIZE_SMALL), f.px(20), f.px(6)
    cross_w = f.px(16) if removable else 0
    left, top = x, y
    for n, name in enumerate(names):
        label = name if len(name) <= 24 else name[:14] + "…" + name[-9:]
        chip_w = min(width, round(f.measure("ui", small, label)) + f.px(14) + cross_w)
        if left > x and left + chip_w > x + width:
            left, top = x, top + chip_h + gap
        text_y = top + (chip_h - f.line_height(small)) // 2
        f.rect(f"{id}{n}", left, top, chip_w, chip_h, T.BUTTON_GHOST, f.px(T.RADIUS_SMALL), 1, T.CARD_BORDER)
        f.text(f"{id}{n}.label", left + f.px(7), text_y, label, small, T.TEXT_MUTED)
        if removable:
            f.text(f"{id}{n}.remove", left + chip_w - cross_w, text_y, "×", small, T.TEXT_FAINT)
            f.hit(f"{id}{n}.remove", left + chip_w - cross_w - f.px(2), top, cross_w + f.px(2), chip_h,
                  (action, n))
        left += chip_w + gap
    return top + chip_h - y if names else 0


def _attachment_names(session: dict) -> list[str]:
    return [a["name"] for a in session.get("attachments", [])]


def _reference_names(session: dict) -> list[str]:
    """Pinned references: sent with every request until their chip is closed."""
    return ["ref " + r["name"] for r in session.get("references", []) if r.get("pinned", True)]


def _chip_rows(f: Frame, id: str, x: int, y: int, width: int, session: dict) -> int:
    height = _image_chips(f, id + ".ref", x, y, width, _reference_names(session), True, "unpin_reference")
    if height:
        height += f.px(6)
    height += _image_chips(f, id, x, y + height, width, _attachment_names(session), True)
    return height


def input_height(f: Frame, session: dict) -> int:
    pad, card_pad = f.px(T.PAD), f.px(T.CARD_PAD)
    inner_w = f.width - 2 * pad - 2 * card_pad
    size, _, visible = _input_lines(f, session, inner_w)
    measuring = Frame(f.width, f.height, f.scale, f.measure)  # Chips wrap, so their height is laid out to be known.
    chips = _chip_rows(measuring, "measure", 0, 0, inner_w, session)
    chips += f.px(6) if chips else 0
    return visible * f.line_height(size) + 2 * card_pad + f.px(26) + 2 * pad + _context_row_height(f) + chips


def _context_row(f: Frame, x: int, y: int, width: int) -> None:
    """The row above the text: + adds context (the @ menu as a button, searchable), then what
    "this" means right now: the selection, which goes to the model with the message."""
    small, chip_h = f.px(T.FONT_SIZE_SMALL), f.px(20)
    text_y = y + (chip_h - f.line_height(small)) // 2
    hovered = f.ui.get("hover") == "input.add" or f.ui.get("picker") is not None
    f.rect("input.add", x, y, chip_h, chip_h, T.BUTTON_GHOST if hovered else T.NONE, f.px(T.RADIUS_SMALL), 1,
           T.CARD_BORDER)
    plus_w = round(f.measure("ui", small, "+"))
    f.text("input.add.label", x + (chip_h - plus_w) // 2, text_y, "+", small, T.TEXT if hovered else T.TEXT_MUTED)
    f.hit("input.add", x, y, chip_h, chip_h, ("picker_open", None), tip="Add context: objects, materials, images, add-ons")
    names = f.ui.get("selection") or []
    if not names:
        return
    left = x + chip_h + f.px(6)
    label = ", ".join(names[:3]) + (f"  +{len(names) - 3}" if len(names) > 3 else "")
    line = wrap(label, "ui", small, width - (left - x) - f.px(34), f.measure)[0][1].rstrip()
    label = line + ("…" if len(line) < len(label) else "")
    pill_width = round(f.measure("ui", small, label)) + f.px(30)
    f.rect("input.context", left, y, pill_width, chip_h, T.BUTTON_GHOST, f.px(T.RADIUS_SMALL), 1, T.CARD_BORDER)
    f.text("input.context.at", left + f.px(7), text_y, "@", small, T.ACCENT)
    f.text("input.context.label", left + f.px(21), text_y, label, small, T.TEXT_MUTED)


def usage_lines(usage: dict, budget: int, account: dict | None) -> list[str]:
    """The numbers behind the two rings, for their tooltip: the latest request against the
    budget it is compacted at, the conversation's total, and the account's windows."""
    context, total_in, total_out = usage.get("context", 0), usage.get("input", 0), usage.get("output", 0)
    share = f" ({round(100 * context / budget)}%)" if budget else ""
    lines = [f"Context: {_k(context)} of {_k(budget)} tokens{share} before compaction"]
    if total_in or total_out:
        lines.append(f"This conversation: {_k(total_in)} in, {_k(total_out)} out")
    for key, label in (("session", "Session"), ("week", "Week")):
        used = ((account or {}).get(key) or {}).get("used")
        if isinstance(used, (int, float)):
            lines.append(f"{label}: {round(used * 100)}% used, {round(max(0.0, 1 - used) * 100)}% left")
    return lines


def ring_shares(usage: dict, budget: int, account: dict | None) -> list[tuple[str, float, tuple]]:
    """(id, fill, color) per ring, left to right: context used, then what is left of the session
    window when the account meters one."""
    context = usage.get("context", 0)
    share = min(1.0, context / budget) if budget else 0.0
    rings = [("context", share, T.ERROR if share >= 1 else T.WARN if share >= 0.8 else T.ACCENT)]
    used = ((account or {}).get("session") or {}).get("used")
    if isinstance(used, (int, float)):
        left = max(0.0, min(1.0, 1 - used))
        rings.append(("session", left, T.ERROR if left < 0.1 else T.WARN if left < 0.3 else T.OK))
    return rings


def _k(tokens: int) -> str:
    return f"{tokens / 1000:.1f}k" if tokens >= 1000 else str(tokens)


SHOW_USAGE_FROM = 0.7  # The footer names the fuller window once this much of it is used.


def usage_note(account: dict | None) -> tuple[str, float]:
    """(text, share) for the footer: the fuller of the account's two windows once it is worth a
    glance, else ("", share)."""
    best = ("", 0.0)
    for key in ("week", "session"):
        used = ((account or {}).get(key) or {}).get("used")
        if isinstance(used, (int, float)) and used > best[1]:
            best = (f"{round(used * 100)}% of {key} used", float(used))
    return best if best[1] >= SHOW_USAGE_FROM else ("", best[1])


def _list_popup(f: Frame, id: str, x: int, bottom: int, width: int, rows: list, active: int,
                header: str | None = None, query: str = "", placeholder: str = "") -> None:
    """A card floating above `bottom`. With `header`, a title and a search field come first.
    Rows are (label, right label, action); `active` is the keyboard-highlighted one."""
    size, small = f.px(T.FONT_SIZE), f.px(T.FONT_SIZE_SMALL)
    row_height, pad = f.px(24), f.px(4)
    head_height = f.px(30) if header is not None else 0
    height = max(len(rows), 1) * row_height + 2 * pad + head_height
    top = bottom - height - f.px(4)
    f.rect(f"{id}.bg", x, top, width, height, T.CARD, f.px(T.RADIUS), 1, T.INPUT_BORDER)
    f.hit(f"{id}.bg", x, top, width, height, ("noop", None))  # A click inside the card is not a click away.
    y = top + pad
    if header is not None:
        f.text(f"{id}.title", x + f.px(12), y + (head_height - f.line_height(small)) // 2, header, small, T.TEXT_FAINT)
        query_x = x + f.px(12) + round(f.measure("ui", small, header)) + f.px(12)
        f.text(f"{id}.query", query_x, y + (head_height - f.line_height(size)) // 2, query or placeholder, size,
               T.TEXT if query else T.TEXT_FAINT)
        caret_x = query_x + round(f.measure("ui", size, query))
        f.rect(f"{id}.caret", caret_x, y + f.px(7), max(1, f.px(1.5)), head_height - f.px(14), T.ACCENT)
        f.rect(f"{id}.rule", x + pad, y + head_height - 1, width - 2 * pad, 1, T.CARD_BORDER)
        y += head_height
    if not rows:
        f.text(f"{id}.empty", x + f.px(12), y + (row_height - f.line_height(size)) // 2, "No matches", size, T.TEXT_FAINT)
    for n, (label, right, action) in enumerate(rows):
        row_y = y + n * row_height
        if n == active:
            f.rect(f"{id}.row{n}.bg", x + pad, row_y, width - 2 * pad, row_height, T.BUTTON_GHOST, f.px(T.RADIUS_SMALL))
        right_width = round(f.measure("ui", small, right)) if right else 0
        line = wrap(label, "ui", size, width - right_width - f.px(40), f.measure)[0][1].rstrip()
        f.text(f"{id}.row{n}.name", x + f.px(12), row_y + (row_height - f.line_height(size)) // 2,
               line + ("…" if len(line) < len(label) else ""), size, T.TEXT)
        if right:
            f.text(f"{id}.row{n}.kind", x + width - f.px(12) - right_width,
                   row_y + (row_height - f.line_height(small)) // 2, right, small, T.TEXT_FAINT)
        f.hit(f"{id}.row{n}", x, row_y, width, row_height, action)


def _mention_list(f: Frame, session: dict, x: int, bottom: int, width: int) -> None:
    """Completions for the @name being typed, floating above the input."""
    rows = f.ui.get("mentions") or []
    if not rows:
        return
    active = min(session.get("mention", 0), len(rows) - 1)
    _list_popup(f, "mentions", x, bottom, width, [(name, kind, ("mention_pick", n)) for n, (name, kind) in enumerate(rows)],
                active)


def _picker(f: Frame, x: int, bottom: int, width: int) -> None:
    """The + menu: everything that can be added to the message, searched as you type."""
    picker = f.ui.get("picker")
    if picker is None:
        return
    rows = [(name, kind, ("picker_pick", n)) for n, (name, kind) in enumerate(picker.get("rows") or [])]
    _list_popup(f, "picker", x, bottom, width, rows, picker.get("active", 0), header="Add context",
                query=picker.get("query", ""), placeholder="Search objects, materials, add-ons…")


def _model_menu(f: Frame, x: int, bottom: int, width: int) -> None:
    if not f.ui.get("model_menu"):
        return
    rows = [(("✓  " if current else "     ") + label, note, ("model_pick", id))
            for id, label, note, current in (f.ui.get("model_options") or [])]
    rows.append(("Settings…", "", ("open_settings", None)))
    _list_popup(f, "models", x, bottom, min(width, f.px(280)), rows, -1)


def model_button_label(model: str) -> str:
    return {"fast": "Fast", "pro": "Pro"}.get(model, model)


def _rings(f: Frame, session: dict, right: int, y: int, height: int) -> None:
    """The two donuts at the footer's right: the request's context against the budget it is
    compacted at, and what is left of the session's allowance. The tooltip has the numbers."""
    diameter, thickness, gap = f.px(14), f.px(3), f.px(6)
    usage, budget, account = session.get("usage") or {}, int(f.ui.get("context_budget") or 0), f.ui.get("account")
    rings = ring_shares(usage, budget, account)
    total = len(rings) * diameter + (len(rings) - 1) * gap
    left, top = right - total, y + (height - diameter) // 2
    for n, (id, share, color) in enumerate(rings):
        f.ring(f"input.ring.{id}", left + n * (diameter + gap), top, diameter, share, thickness, color)
    f.hit("input.rings", left - f.px(4), y, total + f.px(8), height, ("noop", None),
          tip="\n".join(usage_lines(usage, budget, account)))


def _input(f: Frame, session: dict, top: int, model: str) -> None:
    pad, card_pad = f.px(T.PAD), f.px(T.CARD_PAD)
    x, width = pad, f.width - 2 * pad
    inner_x, inner_w = x + card_pad, width - 2 * card_pad
    size, lines, visible = _input_lines(f, session, inner_w)
    line_height = f.line_height(size)
    small = f.px(T.FONT_SIZE_SMALL)
    focused, busy = session["focused"], session["busy"]
    hover = f.ui.get("hover")

    f.rect("input.zone", 0, top, f.width, f.height - top, T.BG)
    card_y, card_h = top + pad, f.height - top - 2 * pad
    f.rect("input.card", x, card_y, width, card_h, T.INPUT_BG, f.px(T.RADIUS), 1,
           T.ACCENT if focused else T.INPUT_BORDER)
    f.hit("input.card", x, card_y, width, card_h, ("focus", None))

    text_y = card_y + card_pad
    _context_row(f, inner_x, text_y, inner_w)
    text_y += _context_row_height(f)
    chips = _chip_rows(f, "input.image", inner_x, text_y, inner_w, session)
    text_y += chips + (f.px(6) if chips else 0)
    first = max(0, len(lines) - visible)  # Keep the tail in view once the box is full.
    anchor = session.get("anchor")
    if focused and anchor is not None and anchor != session["cursor"]:
        low, high = sorted((anchor, session["cursor"]))
        for n, (start, line) in enumerate(lines[first:]):
            a, b = max(low, start), min(high, start + len(line))
            if a < b or (a == b == start + len(line) and low <= a < high):  # The second: a selected newline.
                left = inner_x + round(f.measure("ui", size, line[:a - start]))
                right = inner_x + round(f.measure("ui", size, line[:b - start])) + (f.px(4) if a == b else 0)
                f.rect(f"input.selection{n}", left, text_y + n * line_height + f.px(1), right - left,
                       line_height - f.px(2), T.SELECTION)
    if session["input"]:
        for n, (_, line) in enumerate(lines[first:]):
            f.text(f"input.l{n}", inner_x, text_y + n * line_height, line.rstrip(), size, T.TEXT)
    else:
        placeholder = "Say what to do with these" if session.get("attachments") else \
            "Add a note; it goes in at the next step" if busy else "Plan, build, fix anything in your scene"
        f.text("input.placeholder", inner_x, text_y, placeholder, size, T.TEXT_FAINT)

    if focused:
        cursor = session["cursor"]
        row = max((n for n, (start, _) in enumerate(lines) if start <= cursor), default=0)
        start, line = lines[row]
        caret_x = inner_x + round(f.measure("ui", size, line[:cursor - start]))
        if row >= first:
            f.rect("input.caret", caret_x, text_y + (row - first) * line_height + f.px(2),
                   max(1, f.px(1.5)), line_height - f.px(4), T.ACCENT)

    # Footer: the model button, then the rings and the key hint at the right.
    button_h = f.px(22)
    button_y = card_y + card_h - card_pad - button_h
    text_y = button_y + (button_h - f.line_height(small)) // 2
    label, caret = model_button_label(model), "▾"
    label_w = round(f.measure("ui", small, label))
    button_w = label_w + round(f.measure("ui", small, caret)) + f.px(22)
    open_menu = bool(f.ui.get("model_menu"))
    f.rect("input.model", inner_x, button_y, button_w, button_h,
           T.BUTTON_GHOST if hover == "input.model" or open_menu else T.NONE, f.px(T.RADIUS_SMALL), 1, T.CARD_BORDER)
    f.text("input.model.label", inner_x + f.px(8), text_y, label, small, T.TEXT if open_menu else T.TEXT_MUTED)
    f.text("input.model.caret", inner_x + f.px(8) + label_w + f.px(6), text_y, caret, small, T.TEXT_FAINT)
    f.hit("input.model", inner_x, button_y, button_w, button_h, ("model_menu", None), tip="Choose the model")
    hint = "⏎  note   esc  stop" if busy else "⏎  send"
    hint_x = inner_x + inner_w - round(f.measure("ui", small, hint))
    f.text("input.hint", hint_x, text_y, hint, small, T.TEXT_FAINT)
    _rings(f, session, hint_x - f.px(16), button_y, button_h)

    if f.ui.get("picker") is not None:
        _picker(f, x, card_y, width)
    elif focused:
        _mention_list(f, session, x, card_y, width)
    _model_menu(f, inner_x, button_y - f.px(2), width)


HEADER_BUTTONS = (  # Right to left: id, glyph, tooltip.
    ("header.close", "×", "Close panel"),
    ("header.settings", "⚙", "Settings"),
    ("header.history", "↺", "History"),
    ("header.new", "+", "New chat"),
)


def _header(f: Frame, session: dict, view: str = "chat") -> int:
    height, pad, size, small = f.px(T.HEADER_HEIGHT), f.px(T.PAD), f.px(T.FONT_SIZE), f.px(T.FONT_SIZE_SMALL)
    f.rect("header.bg", 0, 0, f.width, height, T.HEADER_BG)
    f.rect("header.rule", 0, height - 1, f.width, 1, T.CARD_BORDER)
    y = (height - f.line_height(size)) // 2
    mark = f.px(18)
    f.prims.append({"t": "mark", "id": "header.mark", "x": pad, "y": (height - mark) // 2, "w": mark, "h": mark})
    title_x = pad + mark + f.px(8)
    f.text("header.title", title_x, y, "Loopcut", size, T.TEXT)
    if view == "history":
        f.text("header.crumb", title_x + round(f.measure("ui", size, "Loopcut")) + f.px(8), y, "›  History", size,
               T.TEXT_MUTED)
    # Icon buttons at the right: the editor's own header is hidden, so close lives here too.
    hover, box, gap = f.ui.get("hover"), f.px(24), f.px(2)
    right = f.width - pad
    for id, glyph, tip in HEADER_BUTTONS:
        bx, by = right - box, (height - box) // 2
        if id == "header.history":
            action, tip = (("history_close", None), "Back to chat") if view == "history" else (("history_open", None), tip)
        else:
            action = {"header.close": ("close_panel", None), "header.settings": ("open_settings", None),
                      "header.new": ("new_chat", None)}[id]
        active = hover == id or (id == "header.history" and view == "history")
        if active:
            f.rect(f"{id}.bg", bx, by, box, box, T.BUTTON_GHOST, f.px(T.RADIUS_SMALL))
        glyph_size = small if glyph in "⚙↺" else size  # The symbol glyphs come from a wider font.
        glyph_w = round(f.measure("ui", glyph_size, glyph))
        f.text(id, bx + (box - glyph_w) // 2, (height - f.line_height(glyph_size)) // 2, glyph, glyph_size,
               T.TEXT if active else T.TEXT_MUTED)
        f.hit(id, bx, 0, box, height, action, tip=tip)
        right = bx - gap
    return height


def _tooltip(f: Frame) -> None:
    """For the hovered hit that has a tip: a small card above it (below when there is no room)."""
    hover = f.ui.get("hover")
    hit = next((h for h in reversed(f.hits) if h["id"] == hover and h.get("tip")), None) if hover else None
    if hit is None:
        return
    small, pad_x, pad_y = f.px(T.FONT_SIZE_SMALL), f.px(8), f.px(5)
    lines = hit["tip"].split("\n")
    width = max(round(f.measure("ui", small, line)) for line in lines) + 2 * pad_x
    height = len(lines) * f.line_height(small) + 2 * pad_y
    x = max(f.px(4), min(f.width - width - f.px(4), hit["x"] + hit["w"] // 2 - width // 2))
    y = hit["y"] - height - f.px(6)
    if y < f.px(2):
        y = hit["y"] + hit["h"] + f.px(6)
    f.rect("tooltip.bg", x, y, width, height, T.CARD, f.px(T.RADIUS_SMALL), 1, T.INPUT_BORDER)
    for n, line in enumerate(lines):
        f.text(f"tooltip.l{n}", x + pad_x, y + pad_y + n * f.line_height(small), line, small, T.TEXT)


def build(session: dict, width: int, height: int, scale: float, measure, model: str = "",
          checkpoints: dict | None = None, ui: dict | None = None) -> dict:
    ui = ui or {}
    view = ui.get("view", "chat")
    f = Frame(width, height, scale, measure, session, checkpoints, ui)
    f.rect("bg", 0, 0, width, height, T.BG)
    input_top = height - input_height(f, session)
    header_bottom = f.px(T.HEADER_HEIGHT)
    if view == "history":
        max_scroll = 0.0
        _history(f, session, ui, header_bottom, input_top)
    else:
        max_scroll = _chat(f, session, header_bottom, input_top)
    _header(f, session, view)  # Header and input go last so they paint over scrolled chat content.
    _input(f, session, input_top, model)
    _tooltip(f)
    return {"width": width, "height": height, "scale": scale, "max_scroll": max_scroll,
            "prims": f.prims, "hits": f.hits}


def hit_at(display: dict, x: float, y_top: float) -> dict | None:
    for hit in reversed(display["hits"]):
        if hit["x"] <= x <= hit["x"] + hit["w"] and hit["y"] <= y_top <= hit["y"] + hit["h"]:
            return hit
    return None


def hit_test(display: dict, x: float, y_top: float):
    hit = hit_at(display, x, y_top)
    return hit["action"] if hit else None
