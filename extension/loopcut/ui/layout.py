"""Turns session state into a display list. No bpy and no GPU in here.

Coordinates are region pixels with the origin at the TOP-left and y growing down; draw.py
flips them. Every primitive carries an id, so the list can be dumped as JSON to debug a frame
as text: an invisible or misplaced element shows up as a bad rect or a color equal to its
background.
"""

import re

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
    def __init__(self, width: int, height: int, scale: float, measure, session=None, checkpoints=None):
        self.width, self.height, self.scale, self.measure = width, height, scale, measure
        self.session = session or {}
        self.checkpoints = checkpoints or {}  # checkpoint id -> 'ok' | 'expired' | 'missing'

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

    def hit(self, id, x, y, w, h, action):
        self.hits.append({"id": id, "x": x, "y": y, "w": w, "h": h, "action": action})

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
    pad, size = f.px(T.CARD_PAD), f.px(T.FONT_SIZE)
    mark = len(f.prims)
    text_height = f.paragraph(f"item{n}.user", x + pad, y + pad, width - 2 * pad, item["text"], size, T.TEXT)
    if item.get("checkpoint"):
        text_height += _checkpoint_row(f, n, item, x + pad, y + pad + text_height, width - 2 * pad)
    height = text_height + 2 * pad
    f.prims.insert(mark, {"t": "rect", "id": f"item{n}.user.card", "x": x, "y": y, "w": width, "h": height,
                          "color": T.CARD, "radius": f.px(T.RADIUS), "border": 1,
                          "border_color": T.CARD_BORDER})
    return height


def _item_assistant(f: Frame, n: int, item: dict, x, y, width) -> int:
    size, cursor = f.px(T.FONT_SIZE), y
    segments = split_markdown(item["text"])
    if not segments and item.get("streaming"):
        segments = [("p", "…")]
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
        f.button(f"item{n}.tool.reject", inner_x + run_width + f.px(8), cursor, "Reject  esc",
                 T.BUTTON_GHOST, T.TEXT, ("reject", n))
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
    height = cursor + pad - y
    f.prims.insert(mark, {"t": "rect", "id": f"item{n}.notice.card", "x": x, "y": y, "w": width, "h": height,
                          "color": T.BG, "radius": f.px(T.RADIUS), "border": 1, "border_color": T.CARD_BORDER})
    return height


_ITEM_LAYOUT = {"notice": _item_notice, "user": _item_user, "assistant": _item_assistant, "tool": _item_tool, "error": _item_error}


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
        f.text("chat.working", x, cursor, "Working…", f.px(T.FONT_SIZE), T.TEXT_MUTED)
        cursor += f.line_height(f.px(T.FONT_SIZE)) + gap

    if not items:
        size = f.px(T.FONT_SIZE)
        middle = (top + bottom) // 2
        for n, (line, color) in enumerate((("Loopcut", T.TEXT), ("Ask for anything in your scene.", T.TEXT_MUTED))):
            line_width = f.measure("ui", size, line)
            f.text(f"chat.empty{n}", round((f.width - line_width) / 2), middle + (n - 1) * f.line_height(size),
                   line, size, color)
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


def _input_lines(f: Frame, session: dict, width: int):
    size = f.px(T.FONT_SIZE)
    lines = wrap(session["input"], "ui", size, width, f.measure)
    visible = min(max(len(lines), T.INPUT_MIN_LINES), T.INPUT_MAX_LINES)
    return size, lines, visible


def input_height(f: Frame, session: dict) -> int:
    pad, card_pad = f.px(T.PAD), f.px(T.CARD_PAD)
    size, _, visible = _input_lines(f, session, f.width - 2 * pad - 2 * card_pad)
    return visible * f.line_height(size) + 2 * card_pad + f.px(26) + 2 * pad


def _input(f: Frame, session: dict, top: int, model: str) -> None:
    pad, card_pad = f.px(T.PAD), f.px(T.CARD_PAD)
    x, width = pad, f.width - 2 * pad
    inner_x, inner_w = x + card_pad, width - 2 * card_pad
    size, lines, visible = _input_lines(f, session, inner_w)
    line_height = f.line_height(size)
    small = f.px(T.FONT_SIZE_SMALL)
    focused = session["focused"]

    f.rect("input.zone", 0, top, f.width, f.height - top, T.BG)
    card_y, card_h = top + pad, f.height - top - 2 * pad
    f.rect("input.card", x, card_y, width, card_h, T.INPUT_BG, f.px(T.RADIUS), 1,
           T.ACCENT if focused else T.INPUT_BORDER)
    f.hit("input.card", x, card_y, width, card_h, ("focus", None))

    text_y = card_y + card_pad
    first = max(0, len(lines) - visible)  # Keep the tail in view once the box is full.
    if session["input"]:
        for n, (_, line) in enumerate(lines[first:]):
            f.text(f"input.l{n}", inner_x, text_y + n * line_height, line.rstrip(), size, T.TEXT)
    else:
        f.text("input.placeholder", inner_x, text_y, "Plan, build, fix anything in your scene", size, T.TEXT_FAINT)

    if focused:
        cursor = session["cursor"]
        row = max((n for n, (start, _) in enumerate(lines) if start <= cursor), default=0)
        start, line = lines[row]
        caret_x = inner_x + round(f.measure("ui", size, line[:cursor - start]))
        if row >= first:
            f.rect("input.caret", caret_x, text_y + (row - first) * line_height + f.px(2),
                   max(1, f.px(1.5)), line_height - f.px(4), T.ACCENT)

    footer_y = card_y + card_h - card_pad - f.line_height(small)
    f.text("input.model", inner_x, footer_y, model, small, T.TEXT_MUTED)
    hint = "esc  stop" if session["busy"] else "⏎  send"
    f.text("input.hint", inner_x + inner_w - round(f.measure("ui", small, hint)), footer_y, hint, small,
           T.TEXT_FAINT)


def _header(f: Frame, session: dict) -> int:
    height, pad, size = f.px(T.HEADER_HEIGHT), f.px(T.PAD), f.px(T.FONT_SIZE)
    f.rect("header.bg", 0, 0, f.width, height, T.HEADER_BG)
    f.rect("header.rule", 0, height - 1, f.width, 1, T.CARD_BORDER)
    y = (height - f.line_height(size)) // 2
    f.text("header.title", pad, y, "Loopcut", size, T.TEXT)
    label = "New chat"
    small = f.px(T.FONT_SIZE_SMALL)
    label_width = round(f.measure("ui", small, label))
    label_x = f.width - pad - label_width
    f.text("header.new", label_x, (height - f.line_height(small)) // 2, label, small, T.TEXT_MUTED)
    f.hit("header.new", label_x - f.px(6), 0, label_width + f.px(12), height, ("new_chat", None))
    return height


def build(session: dict, width: int, height: int, scale: float, measure, model: str = "",
          checkpoints: dict | None = None) -> dict:
    f = Frame(width, height, scale, measure, session, checkpoints)
    f.rect("bg", 0, 0, width, height, T.BG)
    input_top = height - input_height(f, session)
    header_bottom = f.px(T.HEADER_HEIGHT)
    max_scroll = _chat(f, session, header_bottom, input_top)
    _header(f, session)  # Header and input go last so they paint over scrolled chat content.
    _input(f, session, input_top, model)
    return {"width": width, "height": height, "scale": scale, "max_scroll": max_scroll,
            "prims": f.prims, "hits": f.hits}


def hit_test(display: dict, x: float, y_top: float):
    for hit in reversed(display["hits"]):
        if hit["x"] <= x <= hit["x"] + hit["w"] and hit["y"] <= y_top <= hit["y"] + hit["h"]:
            return hit["action"]
    return None
