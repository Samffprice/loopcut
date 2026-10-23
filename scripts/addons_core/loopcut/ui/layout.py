"""Turns session state into a display list. No bpy and no GPU in here.

Coordinates are region pixels with the origin at the TOP-left and y growing down; draw.py
flips them. Every primitive carries an id, so the list can be dumped as JSON to debug a frame
as text: an invisible or misplaced element shows up as a bad rect or a color equal to its
background.
"""

import json
import math
import re
from pathlib import Path

from .. import allowance, config, spending, state
from . import textedit
from . import theme as T
from .markdown import REFERENCE_LIMIT, split_markdown

_TOKEN = re.compile(r"\S+\s*|\s+")

STATUS_GLYPH = {
    "awaiting": ("●", T.WARN),
    "running": ("●", T.ACCENT),
    "done": ("✓", T.OK),
    "failed": ("✕", T.ERROR),
    "rejected": ("–", T.TEXT_FAINT),
}

CODE_TOOLS = {"run_python", "run_blender"}


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


class Frame:
    def __init__(self, width: int, height: int, scale: float, measure, session=None, checkpoints=None,
                 ui=None):
        self.width, self.height, self.scale, self.measure = width, height, scale, measure
        self.session = session or {}
        self.checkpoints = checkpoints or {}  # checkpoint id -> 'ok' | 'expired' | 'missing'
        self.ui = ui or {}

        self.prims: list[dict] = []
        self.hits: list[dict] = []
        self.input: dict | None = None

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
        """`action` None draws a disabled button: no hit area."""
        size, pad_x = self.px(T.FONT_SIZE_SMALL), self.px(10)
        height = self.px(24)
        width = round(self.measure("ui", size, label)) + 2 * pad_x
        self.rect(id, x, y, width, height, fill, self.px(T.RADIUS_SMALL))
        self.text(f"{id}.label", x + pad_x, y + (height - self.line_height(size)) // 2, label, size, color)
        if action is not None:
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
        edit_x = label_x - f.px(16) - round(f.measure("ui", small, "Edit"))
        f.text(f"item{n}.checkpoint.edit", edit_x, y + f.px(6), "Edit", small, T.TEXT_MUTED)
        f.hit(f"item{n}.checkpoint.edit", edit_x - f.px(4), y, label_x - edit_x - f.px(4), row + f.px(10),
              ("edit_ask", n))
        return row + f.px(6)
    cursor = y + f.px(8)
    # Editing a sent message is a restore to before it: the conversation is cut there and the message comes
    # back into the box; Undo restore brings it all back.
    ask = ("Edit this message? The scene goes back to before it, the conversation from here is set aside, and "
           "the message returns to the box to change and send. Your current state is saved first, so Undo "
           "restore brings it all back."
           if f.session.get("confirm_edit") else
           "Restore the scene to before this message? Your current state is saved first, so this can be undone.")
    cursor += f.paragraph(f"item{n}.checkpoint.ask", x, cursor, width, ask, small, T.TEXT_MUTED)
    cursor += f.px(6)
    used = f.button(f"item{n}.checkpoint.confirm", x, cursor, "Edit" if f.session.get("confirm_edit") else "Restore",
                    T.BUTTON, T.BUTTON_TEXT, ("restore_confirm", n))
    f.button(f"item{n}.checkpoint.cancel", x + used + f.px(8), cursor, "Cancel", T.BUTTON_GHOST, T.TEXT,
             ("restore_cancel", n))
    return cursor + f.px(24) - y


def _item_user(f: Frame, n: int, item: dict, x, y, width) -> int:
    pad, size, small = f.px(T.CARD_PAD), f.px(T.FONT_SIZE), f.px(T.FONT_SIZE_SMALL)
    mark = len(f.prims)
    text_width = width - 2 * pad
    deliveries = list(item.get("helper_deliveries") or [])
    if item.get("queued"):  # Helper delivery and the main agent's pending reply are separate.
        tag = "sent" if deliveries else "pending reply"
        tag_width = round(f.measure("ui", small, tag))
        f.text(f"item{n}.queued", x + width - pad - tag_width, y + pad, tag, small, T.TEXT_FAINT)
        text_width -= tag_width + f.px(8)
    text_height = f.paragraph(f"item{n}.user", x + pad, y + pad, text_width, item["text"], size, T.TEXT) \
        if item["text"] else 0
    if item.get("images"):
        text_height += f.px(4) if item["text"] else 0
        text_height += _image_chips(f, f"item{n}.image", x + pad, y + pad + text_height, width - 2 * pad,
                                    item["images"], removable=False)
    for k, delivery in enumerate(deliveries):
        note = f"Sent to the helper building {delivery['deliverable']}"
        note += (f" (it read it at step {delivery['step']})" if "step" in delivery else " (not read yet)")
        text_height += f.px(6) + f.paragraph(f"item{n}.helper_delivery{k}", x + pad,
                                            y + pad + text_height + f.px(6), width - 2 * pad,
                                            note, small, T.TEXT_MUTED)
    if item.get("queued"):
        helpers = sum(row.get("status") in state.WAVE_RUNNING
                      for card in f.session["items"] if card.get("kind") == "wave"
                      for row in card.get("rows", [])) if f.session.get("busy") else 0
        sending = item.get("sending")
        helper = "helper" if helpers == 1 else "helpers"
        note = (f"Stopping {helper}; Loopcut will reply after they stop" if sending else
                "Loopcut will reply when the helpers report back" if helpers else
                "Waiting for Loopcut to reply")
        text_height += f.px(6) + f.paragraph(f"item{n}.delivery", x + pad, y + pad + text_height + f.px(6),
                                            width - 2 * pad, note, small, T.TEXT_MUTED)
        if helpers and not sending:
            text_height += f.px(6)
            action_height = f.paragraph(f"item{n}.send_now.label", x + pad, y + pad + text_height,
                                        width - 2 * pad, f"Stop {helper} and send now", small, T.ACCENT)
            f.hit(f"item{n}.send_now", x + pad, y + pad + text_height, width - 2 * pad, action_height,
                  ("send_queued_now", n))
            text_height += action_height
    if item.get("checkpoint"):
        text_height += _checkpoint_row(f, n, item, x + pad, y + pad + text_height, width - 2 * pad)
    height = text_height + 2 * pad
    f.prims.insert(mark, {"t": "rect", "id": f"item{n}.user.card", "x": x, "y": y, "w": width, "h": height,
                          "color": T.CARD, "radius": f.px(T.RADIUS), "border": 1,
                          "border_color": T.CARD_BORDER})
    return height


SHIMMER_PERIOD = 1.8   # Seconds for the highlight to sweep the word once.
SHIMMER_WIDTH = 0.3    # Of the word's length, the bright part of the sweep.
DOT_PERIOD = 1.35      # Seconds per pulse of the ellipsis after the word.


def _mix(a, b, t: float):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(4))


def thinking(f: Frame, id: str, x, y, size, word: str = "Thinking") -> int:
    """The waiting line, animated the way people expect: a highlight sweeps along the word and the
    ellipsis after it pulses (a still frame reads "Working…", never "Working."). Driven by ui["now"];
    mainthread redraws while a turn runs. Returns the line height."""
    now = float(f.ui.get("now") or 0.0)
    sweep = (now % SHIMMER_PERIOD) / SHIMMER_PERIOD * (1 + 2 * SHIMMER_WIDTH) - SHIMMER_WIDTH
    cx = x
    for i, ch in enumerate(word):
        along = i / max(1, len(word) - 1)
        bright = max(0.0, 1 - abs(along - sweep) / SHIMMER_WIDTH)
        f.text(f"{id}.c{i}", cx, y, ch, size, _mix(T.TEXT_FAINT, T.TEXT, bright))
        cx += f.measure("ui", size, ch)
    pulse = 0.5 - 0.5 * math.cos(2 * math.pi * (now % DOT_PERIOD) / DOT_PERIOD)
    f.text(f"{id}.dots", cx, y, "…", size, _mix(T.TEXT_FAINT, T.TEXT, pulse))
    return f.line_height(size)


def _item_assistant(f: Frame, n: int, item: dict, x, y, width) -> int:
    size, cursor = f.px(T.FONT_SIZE), y
    text, verification = item["text"], item.get("verification")
    if isinstance(verification, str) and verification and text.endswith(verification):
        text = text[:-len(verification)].rstrip()
    else:
        verification = ""
    segments = split_markdown(text)
    for k, (kind, body) in enumerate(segments):
        if k:
            cursor += f.px(6)
        if kind == "code":
            cursor += f.code_block(f"item{n}.code{k}", x, cursor, width, body)
        else:
            cursor += f.paragraph(f"item{n}.p{k}", x, cursor, width, body, size, T.TEXT)
    if verification:
        cursor += f.px(8)
        f.rect(f"item{n}.verification.rule", x, cursor, width, f.px(1), T.CARD_BORDER)
        cursor += f.px(6)
        cursor += f.paragraph(f"item{n}.verification", x, cursor, width, verification,
                              f.px(T.FONT_SIZE_SMALL), T.TEXT_MUTED)
    if not item.get("streaming"):
        cursor += _reference_rows(f, n, item.get("file_references", []), x, cursor, width)
    return cursor - y


def _reference_rows(f: Frame, n, references, x, y, width) -> int:
    small, gap, cursor = f.px(T.FONT_SIZE_SMALL), f.px(6), y
    for k, ref in enumerate(references[:REFERENCE_LIMIT]):
        id = f"item{n}.ref{k}"
        target = ref["target"]
        missing = target in f.ui.get("missing_paths", ())
        buttons = [] if missing else [("Open", "reference_url" if ref["kind"] == "url" else "reference_open")]
        if not missing and ref["kind"] == "file":
            buttons.append(("Show in folder", "reference_folder"))
        actions_width = sum(round(f.measure("ui", small, label)) + f.px(20) + gap for label, _ in buttons)
        stacked = width - actions_width < f.px(70)
        name_width = width if stacked else width - actions_width
        label = wrap(ref["name"], "ui", small, max(1, name_width - f.px(12)), f.measure)[0][1].rstrip()
        if len(label) < len(ref["name"]):
            label += "…"
        cursor += gap
        f.text(id + ".name", x, cursor + f.px(3), label, small, T.TEXT_FAINT if missing else T.TEXT_MUTED)
        if stacked and buttons:
            cursor += f.line_height(small) + gap
        button_x = x if stacked else x + width - actions_width
        for label, action in buttons:
            button_width = round(f.measure("ui", small, label)) + f.px(20)
            if button_x > x and button_x + button_width > x + width:
                button_x, cursor = x, cursor + f.px(28)
            used = f.button(id + "." + action, button_x, cursor, label, T.BUTTON_GHOST, T.TEXT,
                            (action, target))
            f.hits[-1]["tip"] = target
            button_x += used + gap
        cursor += f.px(24)
    if len(references) > REFERENCE_LIMIT:
        cursor += f.paragraph(f"item{n}.refs.more", x, cursor + gap, width,
                              f"+{len(references) - REFERENCE_LIMIT} more", small, T.TEXT_FAINT) + gap
    return cursor - y


# On a step that installs packages or runs downloaded code (agent._approval "install"): why it asks
# although auto-run is on, and why it has no "Always allow".
INSTALL_NOTE = "This installs software from the internet. Loopcut asks every time."


def failure_reason(output: str, exit_code=None) -> str:
    """Pick a diagnostic for a collapsed card without changing its stored output."""
    lines = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line or line == "(no output)":
            continue
        if re.match(r"^(?:[^:]+:\d+:\s*)?\[?(?:\w*warning|warn)\b", line, re.IGNORECASE):
            continue
        # Shell results store the exit code in a header rather than on the card.
        header = re.fullmatch(r"exit (-?\d+|None) in [\d.]+ s(?:;.*)?", line)
        if header:
            if exit_code is None and header[1] != "None":
                exit_code = int(header[1])
            continue
        lines.append(line)
    # Prefer explicit errors over log footers, including footers with a colon.
    for line in reversed(lines):
        if (re.search(r"\b(?:[\w.]*Error|[\w.]*Exception|SystemExit|KeyboardInterrupt)(?::|$)",
                      line, re.IGNORECASE)
                or re.match(r"^(?:\S*/)?(?:bash|dash|fish|ksh|sh|zsh):", line)):
            return line
    for line in reversed(lines):
        if re.match(r"^[\w./-]+:\s+\S", line):  # Other command diagnostics: "command: message".
            return line
    if lines:
        return lines[-1]
    suffix = f" (exit {exit_code})" if exit_code is not None else ""
    return f"Failed with no error output{suffix}."


def _item_tool(f: Frame, n: int, item: dict, x, y, width) -> int:
    pad, size, small = f.px(T.CARD_PAD), f.px(T.FONT_SIZE), f.px(T.FONT_SIZE_SMALL)
    status = item["status"]
    glyph, glyph_color = STATUS_GLYPH[status]
    plan_repair = item.get("plan_repair")
    code_failure = (status == "failed" and item["name"] in CODE_TOOLS
                    and not item.get("output", "").startswith("Not run."))
    code_repair = item.get("code_repair") if code_failure else None
    if plan_repair in {"resolved", "retried"}:
        glyph, glyph_color = "✓", T.TEXT_MUTED
    elif plan_repair == "adjusting":
        glyph, glyph_color = "…", T.TEXT_MUTED
    if code_repair:
        glyph, glyph_color = ("–" if code_repair == "continued" else "↻"), T.TEXT_MUTED
    awaiting = status == "awaiting"
    expanded = awaiting or item.get("expanded", False)
    mark = len(f.prims)
    row = f.line_height(size)

    inner_x, inner_w, cursor = x + pad, width - 2 * pad, y + f.px(7)
    f.text(f"item{n}.tool.glyph", inner_x, cursor, glyph, size, glyph_color)
    label_x = inner_x + f.px(18)
    summary = item["summary"] or item["name"]
    if plan_repair:
        summary = {"resolved": "Helper started", "retried": "Adjusted the helper plan and retried",
                   "adjusting": "Adjusting the helper plan…"}.get(
            plan_repair, "Helper plan needs adjustment")
    show_name = expanded and item["name"].strip().casefold() != summary.strip().casefold()
    if awaiting:
        label_width = inner_w - f.px(18)
        cursor += f.paragraph(f"item{n}.tool.summary", label_x, cursor, label_width, summary, size, T.TEXT)
        if show_name:
            cursor += f.paragraph(f"item{n}.tool.name", label_x, cursor, label_width,
                                  item["name"], small, T.TEXT_FAINT)
    else:
        name_width = round(f.measure("ui", small, item["name"])) if show_name else 0
        detail_width = name_width + pad + f.px(10) if show_name else 0
        label = wrap(summary, "ui", size, inner_w - f.px(18) - detail_width, f.measure)[0][1].rstrip()
        if len(label) < len(summary.strip()):
            label += "…"
        f.text(f"item{n}.tool.summary", label_x, cursor, label, size, T.TEXT_MUTED)
        if show_name:
            f.text(f"item{n}.tool.name", x + width - pad - name_width, cursor + (row - f.line_height(small)) // 2,
                   item["name"], small, T.TEXT_FAINT)
        cursor += row

    if code_failure:
        output = item.get("output") or ""
        # The measured report is appended after stdout; an earlier printed claim is not evidence.
        reports = re.findall(r"(?m)^Scene changes(: none\.| \(\+ added)", output)
        effect = (" and didn't change the scene" if reports and reports[-1] == ": none."
                  else "; some scene changes remain" if reports else "")
        if code_repair == "continued":
            note = ("This step changed the scene before failing; Loopcut carried on."
                    if reports and reports[-1] != ": none."
                    else "This step had a mistake; Loopcut carried on.")
        elif code_repair == "correcting":
            note = f"This step had a mistake{effect}; Loopcut is correcting it."
        else:
            note = f"This step failed{effect}."
        cursor += f.paragraph(f"item{n}.tool.correction", inner_x, cursor, inner_w, note, small,
                              T.TEXT_MUTED if code_repair else T.ERROR)
        cursor += f.px(4)
        f.button(f"item{n}.tool.details", inner_x, cursor, "Hide details" if expanded else "Details",
                 T.BUTTON_GHOST, T.TEXT_MUTED, ("toggle", n))
        cursor += f.px(24)
    if status == "failed" and not expanded and not plan_repair and not code_failure:
        reason = failure_reason(item.get("output") or "", item.get("exit_code"))
        f.text(f"item{n}.tool.reason", inner_x, cursor, _ellipsize(f, reason, small, inner_w),
               small, T.TEXT_MUTED)
        cursor += f.line_height(small)

    # Provider credit must remain visible beside the search summary when its card is collapsed.
    if "Powered by Poly Haven" in item.get("output", "") and item["name"] in {
            "search_assets", "loopcut_cloud__asset_search", "loopcut_cloud.asset_search"}:
        cursor += f.paragraph(f"item{n}.tool.credit", inner_x, cursor, inner_w,
                              "Powered by Poly Haven", small, T.TEXT_MUTED)
    if item.get("cost"):
        cursor += f.paragraph(f"item{n}.tool.cost", inner_x, cursor, inner_w, item["cost"], small, T.TEXT)
    if awaiting and item.get("ask"):  # A paid step's ask (spending.py): what was hit, the balance before → after.
        for k, line in enumerate(item.get("ask_lines") or ()):
            cursor += f.paragraph(f"item{n}.tool.ask{k}", inner_x, cursor, inner_w, line, small,
                                  T.TEXT_MUTED if k == 0 and item.get("worker") else T.TEXT)
    delegate = item["name"] == "delegate"
    if expanded and delegate and (item["code"] or item["output"]):
        cursor += f.px(6)
        f.button(f"item{n}.tool.details", inner_x, cursor,
                 "Hide details" if item.get("details_expanded") else "Details", T.BUTTON_GHOST, T.TEXT_MUTED,
                 ("toggle_details", n))
        cursor += f.px(24)
    if expanded and item["code"] and (not delegate or item.get("details_expanded")):
        cursor += f.px(6)
        cursor += f.code_block(f"item{n}.tool.code", inner_x, cursor, inner_w, item["code"],
                               T.CODE_PREVIEW_LINES if awaiting else None)
    image_path = item.get("image_path")
    # An image shows until the user collapses its card; the code and output stay behind the toggle.
    if item.get("expanded", True) and image_path and image_path not in f.ui.get("missing_paths", ()):
        cursor += f.px(6)
        if item["name"] == "capture_viewport":
            cursor += _image_preview(f, f"item{n}.tool.image", image_path, item.get("summary") or "Scene preview",
                                     inner_x, cursor, inner_w, 220, button=True)
        else:
            # Delivered files keep their original card and default-app action, even with a hashed stored path.
            height = min(inner_w, f.px(220))
            f.prims.append({"t": "image", "id": f"item{n}.tool.image", "path": image_path,
                            "x": inner_x, "y": cursor, "w": inner_w, "h": height})
            cursor += height + f.px(6)
            f.button(f"item{n}.tool.image.open", inner_x, cursor, "Open image", T.BUTTON_GHOST, T.TEXT,
                     ("reference_open", image_path))
            cursor += f.px(24)
    if expanded and item["output"] and (not delegate or item.get("details_expanded")):
        cursor += f.px(6)
        cursor += f.paragraph(f"item{n}.tool.output", inner_x, cursor, inner_w, item["output"],
                              f.px(T.FONT_SIZE_CODE),
                              T.ERROR if status == "failed" and not code_repair
                              and plan_repair not in {"resolved", "retried", "adjusting"}
                              else T.TEXT_MUTED, "mono")
    if expanded and (item.get("cloud_error") or {}).get("message"):
        cursor += f.paragraph(f"item{n}.tool.account_error", inner_x, cursor, inner_w,
                              item["cloud_error"]["message"], small, T.ERROR)
    if config.billing_open() and expanded and (item.get("cloud_error") or {}).get("upgrade"):
        cursor += _button_row(f, inner_x, cursor, inner_w, [  # Out of credits (M6): buying happens on the site.
            (f"item{n}.tool.upgrade", "Get credits ↗", T.BUTTON, T.BUTTON_TEXT, ("cloud_upgrade", n)),
            (f"item{n}.tool.plans", "See plans ↗", T.BUTTON_GHOST, T.TEXT,
             ("open_web", config.pricing_url("out_of_credits", "pro")))]) + f.px(4)
    error = item.get("cloud_error") or {}
    if expanded and not awaiting and (error.get("link") or error.get("stop") and f.session.get("busy")):
        buttons = []  # A server limit's card: the account page for the daily limit; Stop for the burst guard.
        if error.get("link"):
            buttons.append((f"item{n}.tool.link", error.get("link_label") or "Account ↗", T.BUTTON_GHOST, T.TEXT,
                            ("open_web", error["link"])))
        if error.get("stop") and f.session.get("busy"):
            buttons.append((f"item{n}.tool.stop", "Stop", T.BUTTON_GHOST, T.TEXT, ("stop_turn", n)))
        cursor += f.px(4) + _button_row(f, inner_x, cursor + f.px(4), inner_w, buttons) + f.px(4)
    if awaiting and item.get("install"):
        cursor += f.px(6)
        cursor += f.paragraph(f"item{n}.tool.install", inner_x, cursor, inner_w, INSTALL_NOTE, small, T.TEXT)
    if awaiting and item.get("ask"):
        # Every limit pauses here (owner, 2026-10-16): Approve / Allow runs this call; "Skip this" declines only
        # this call and the turn goes on. The panel's Stop still stops the whole turn.
        cursor += f.px(8)
        buttons = [(f"item{n}.tool.run", f"{item.get('ask_label') or 'Approve'}  ⏎", T.BUTTON, T.BUTTON_TEXT,
                    ("approve", n)),
                   (f"item{n}.tool.reject", f"{spending.SKIP}  esc", T.BUTTON_GHOST, T.TEXT, ("reject", n))]
        if item.get("ask_link"):
            buttons.append((f"item{n}.tool.link", "Change daily limit ↗", T.BUTTON_GHOST, T.TEXT,
                            ("open_web", item["ask_link"])))
        cursor += _button_row(f, inner_x, cursor, inner_w, buttons)
    if awaiting and not item.get("ask"):
        cursor += f.px(8)
        run_width = f.button(f"item{n}.tool.run", inner_x, cursor, "Run  ⏎", T.BUTTON, T.BUTTON_TEXT,
                             ("approve", n))
        reject_width = f.button(f"item{n}.tool.reject", inner_x + run_width + f.px(8), cursor, "Reject  esc",
                                T.BUTTON_GHOST, T.TEXT, ("reject", n))
        always_x = inner_x + run_width + reject_width + f.px(16)
        # A render or bake has its own "always", apart from the one for ordinary steps.
        always = "Always allow renders" if item.get("heavy") else "Always allow"
        if not item.get("paid_approval") and not item.get("install") and always_x + f.measure("ui", small, always) + f.px(20) <= inner_x + inner_w:
            f.button(f"item{n}.tool.always", always_x, cursor, always, T.BUTTON_GHOST, T.TEXT_MUTED,
                     ("approve_always", n))
            f.hits[-1]["tip"] = (
                "Allow only renders, bakes and simulations in this chat.\n"
                "Code approval is separate; installs and paid steps still ask.\n"
                "Resets when you reopen this chat or start a new chat."
                if item.get("heavy") else
                "Allow ordinary steps (edits, code, files) in this chat.\n"
                "Renders/bakes ask separately; installs and paid steps ask.\n"
                "Resets when you reopen this chat or start a new chat.")
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
    # A dropped connection: Try again sends the same request (agent.resume), while it is the newest item.
    if item.get("retry") and n == len(f.session["items"]) - 1 and not f.session.get("busy"):
        height += _button_row(f, x + pad, y + height - pad, width - 2 * pad,
                              [(f"item{n}.error.retry", "Try again", T.BUTTON_GHOST, T.TEXT, ("resume", n))]) + f.px(4)
    f.prims.insert(mark, {"t": "rect", "id": f"item{n}.error.card", "x": x, "y": y, "w": width, "h": height,
                          "color": T.ERROR_BG, "radius": f.px(T.RADIUS), "border": 1,
                          "border_color": T.ERROR_BORDER})
    return height


def _clock(seconds: float) -> str:
    seconds = max(0, round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _waveform(f: Frame, id: str, x, y, width, peaks: list, share: float, muted: bool) -> int:
    """A sound's peaks as bars, the played `share` in the accent color; a plain progress bar without peaks."""
    if not peaks:
        height = f.px(4)
        f.rect(f"{id}.track", x, y, width, height, T.CARD_BORDER, height // 2)
        if share > 0:
            f.rect(f"{id}.played", x, y, max(height, round(width * share)), height, T.ACCENT, height // 2)
        return height
    height, step = f.px(28), width / len(peaks)
    bar = max(1, round(step * 0.6))
    for k, peak in enumerate(peaks):
        bar_h = max(f.px(2), round(peak * height))
        color = T.CARD_BORDER if muted else T.ACCENT if k < share * len(peaks) else T.TEXT_FAINT
        f.rect(f"{id}.b{k}", round(x + k * step), y + (height - bar_h) // 2, bar, bar_h, color)
    return height


def _item_audio(f: Frame, n: int, item: dict, x, y, width) -> int:
    """A generated sound under the call that made it (state.item_audio): Play / Stop, its name and length, and
    a waveform that fills in as it plays. One sound plays at a time (playback.py); a file that is gone is a
    disabled Play with a note."""
    audio = item["audio"]
    pad, size, small = f.px(T.CARD_PAD), f.px(T.FONT_SIZE), f.px(T.FONT_SIZE_SMALL)
    mark = len(f.prims)
    inner_x, inner_w, cursor = x + pad, width - 2 * pad, y + pad
    missing = audio["path"] in f.ui.get("missing_paths", ())
    now = f.ui.get("audio_playing") or {}
    playing = not missing and now.get("path") == audio["path"]
    if playing:
        used = f.button(f"item{n}.audio.stop", inner_x, cursor, "■ Stop", T.BUTTON, T.BUTTON_TEXT,
                        ("audio_stop", audio["path"]))
    else:
        used = f.button(f"item{n}.audio.play", inner_x, cursor, "▶ Play", T.BUTTON_GHOST,
                        T.TEXT_FAINT if missing else T.TEXT, None if missing else ("audio_play", audio["path"]))
    button_h = f.px(24)
    length = f"{_clock(now['position'])} / {_clock(audio['seconds'])}" if playing else _clock(audio["seconds"])
    length_w = round(f.measure("ui", small, length))
    f.text(f"item{n}.audio.length", inner_x + inner_w - length_w, cursor + (button_h - f.line_height(small)) // 2,
           length, small, T.TEXT_MUTED)
    name_x = inner_x + used + f.px(10)
    name_w = inner_x + inner_w - length_w - f.px(10) - name_x
    if name_w < f.px(48):  # A narrow panel: the name gets its own row under the button.
        cursor += button_h + f.px(4)
        name_x, name_w, button_h = inner_x, inner_w, f.line_height(size)
    f.text(f"item{n}.audio.name", name_x, cursor + (button_h - f.line_height(size)) // 2,
           _ellipsize(f, audio["name"], size, name_w), size, T.TEXT_FAINT if missing else T.TEXT)
    cursor += button_h + f.px(8)
    share = min(1.0, now["position"] / audio["seconds"]) if playing and audio["seconds"] > 0 else 0.0
    cursor += _waveform(f, f"item{n}.audio.wave", inner_x, cursor, inner_w, audio["peaks"], share, missing)
    if missing:
        cursor += f.px(6) + f.paragraph(f"item{n}.audio.missing", inner_x, cursor + f.px(6), inner_w,
                                        "The sound file is no longer on disk.", small, T.TEXT_FAINT)
    height = cursor + pad - y
    f.prims.insert(mark, {"t": "rect", "id": f"item{n}.audio.card", "x": x, "y": y, "w": width, "h": height,
                          "color": T.CARD, "radius": f.px(T.RADIUS), "border": 1, "border_color": T.CARD_BORDER})
    return height


def _item_notice(f: Frame, n: int, item: dict, x, y, width) -> int:
    if item.get("audio"):
        return _item_audio(f, n, item, x, y, width)
    item = state.notice_for_display(item)
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
    elif item.get("modifiers") and item["action_label"]:
        cursor += f.px(8)
        f.button(f"item{n}.notice.action", x + pad, cursor, item["action_label"], T.BUTTON_GHOST, T.TEXT,
                 ("guard_show", n))
        cursor += f.px(24)
    height = cursor + pad - y
    f.prims.insert(mark, {"t": "rect", "id": f"item{n}.notice.card", "x": x, "y": y, "w": width, "h": height,
                          "color": T.BG, "radius": f.px(T.RADIUS), "border": 1, "border_color": T.CARD_BORDER})
    return height


_LIMIT_TITLES = {"session_limit": "Session allowance used", "week_limit": "Weekly allowance used",
                 "model_not_in_plan": "Pro is not in your plan", "free_daily_limit": "Free Fast is used up for today"}
HOSTED_LIMITS = state.HOSTED_LIMITS


def _button_row(f: Frame, x, y, width, buttons) -> int:
    """Buttons left to right, wrapping onto another row when the panel is too narrow; one too wide for
    the panel on its own is shortened. buttons: (id, label, fill, color, action). Returns the height."""
    size, gap, row_h = f.px(T.FONT_SIZE_SMALL), f.px(8), f.px(30)
    bx, by = x, y
    for id, label, fill, color, action in buttons:
        if round(f.measure("ui", size, label)) + f.px(20) > width:
            label = _ellipsize(f, label, size, width - f.px(20))
        used = round(f.measure("ui", size, label)) + f.px(20)
        if bx > x and bx + used > x + width:
            bx, by = x, by + row_h
        bx += f.button(id, bx, by, label, fill, color, action) + gap
    return by - y + f.px(24)


def _item_offer(f: Frame, n: int, item: dict, x, y, width) -> int:
    """A conversion card (nudges.py): what the account adds at this moment, and its buttons. A button with
    "when" shows only for that sign-in state, so the card follows a sign-in made while it is open."""
    pad, size, small = f.px(T.CARD_PAD), f.px(T.FONT_SIZE), f.px(T.FONT_SIZE_SMALL)
    mark = len(f.prims)
    inner_x, inner_w, cursor = x + pad, width - 2 * pad, y + pad
    cursor += f.paragraph(f"item{n}.offer.title", inner_x, cursor, inner_w, item["title"], size, T.TEXT)
    if item.get("text"):
        cursor += f.px(2) + f.paragraph(f"item{n}.offer.text", inner_x, cursor + f.px(2), inner_w, item["text"],
                                        small, T.TEXT_MUTED)
    if item.get("resolved"):
        cursor += f.px(4) + f.paragraph(f"item{n}.offer.resolved", inner_x, cursor + f.px(4), inner_w,
                                        item["resolved"], small, T.OK)
    else:
        signed_in, busy = bool(f.ui.get("loopcut_signed_in")), f.session.get("busy")
        buttons = []
        for i, action in enumerate(item.get("actions", [])):
            when = action.get("when", "")
            if (when == "loopcut_in" and not signed_in) or (when == "loopcut_out" and signed_in):
                continue
            if busy and action["do"] in {"continue_loopcut", "prompt"}:
                continue  # These start or fill the next turn; they wait for this one to end.
            primary = not buttons and action["do"] != "dismiss"
            buttons.append((f"item{n}.offer.a{i}", action["label"], T.BUTTON if primary else T.BUTTON_GHOST,
                            T.BUTTON_TEXT if primary else T.TEXT_MUTED if action["do"] == "dismiss" else T.TEXT,
                            ("offer", (n, i))))
        if buttons:
            cursor += f.px(8) + _button_row(f, inner_x, cursor + f.px(8), inner_w, buttons)
    height = cursor + pad - y
    f.prims.insert(mark, {"t": "rect", "id": f"item{n}.offer.card", "x": x, "y": y, "w": width, "h": height,
                          "color": T.CARD, "radius": f.px(T.RADIUS), "border": 1, "border_color": T.CARD_BORDER})
    return height


def _limit_buttons(f: Frame, n: int | str | None, item: dict, id: str) -> list:
    status, upgrade = item.get("status", ""), item.get("upgrade")
    if not config.billing_open():
        status = ""
    buttons = []
    if status == "" and upgrade and config.billing_open():
        buttons.append((id + ".upgrade", upgrade.get("label") or "Upgrade", T.BUTTON, T.BUTTON_TEXT,
                        ("limit_upgrade", n)))
    if item["reason"] in HOSTED_LIMITS and status == "":
        buttons.append((id + ".chatgpt", "Switch to ChatGPT plan", T.BUTTON_GHOST, T.TEXT, ("limit_chatgpt", n))
                       if f.ui.get("chatgpt_plan") else
                       (id + ".chatgpt", "Sign in with ChatGPT", T.BUTTON_GHOST, T.TEXT, ("sign_in", "chatgpt")))
        buttons.append((id + ".key", "Use my own API key", T.BUTTON_GHOST, T.TEXT, ("own_key", None)))
    if item["reason"] == "model_not_in_plan":
        buttons.append((id + ".fast", "Use Fast instead", T.BUTTON_GHOST, T.TEXT, ("limit_fast", n)))
    if n is not None and state.can_resume(f.session):
        buttons.append((id + ".retry", "Try again", T.BUTTON_GHOST, T.TEXT_MUTED, ("limit_retry", n)))
    return buttons


def _item_limit(f: Frame, n: int, item: dict, x, y, width) -> int:
    """Out of allowance: what ran out, what the next plan offers, and the button that gets it.
    The buttons show on the newest unresolved limit; older ones are history."""
    pad, size, small = f.px(T.CARD_PAD), f.px(T.FONT_SIZE), f.px(T.FONT_SIZE_SMALL)
    mark = len(f.prims)
    inner_x, inner_w, cursor = x + pad, width - 2 * pad, y + pad
    status, upgrade = item.get("status", ""), item.get("upgrade")
    if not config.billing_open():
        status, upgrade = "", None  # Saved cards cannot offer a purchase while billing is closed.
        item = state.item_limit(item["text"], item["reason"], item["plan"], item["resets_at"], None)
    if status == "done":
        title, body, color = "Upgraded", item["text"], T.OK
    elif status == "waiting":
        title, color = "Waiting for your upgrade", T.WARN
        body = "Finish in the browser. Loopcut refreshes your plan and available models when it is active."
    else:
        title, color = _LIMIT_TITLES.get(item["reason"], "Allowance used"), T.WARN
        if not config.billing_open() and item["reason"] == "model_not_in_plan":
            title = "Model unavailable"
        body = item["text"] + (f"\n{upgrade['note']}" if upgrade and upgrade.get("note") else "")
        if item.get("resets_at"):
            reset = state.reset_label(item["resets_at"], now=f.ui.get("now"))
            body = body.replace(item["resets_at"], reset)
            if reset not in body:
                body += f"\nResets {reset}."
        if item["reason"] in HOSTED_LIMITS:  # M7: a ChatGPT plan can carry on where Loopcut's allowance stopped.
            if f.ui.get("chatgpt_plan"):
                body += "\nYour ChatGPT plan can carry on now."
            elif item["reason"] != "free_daily_limit":  # The server's free_daily_limit text already says it.
                body += "\nHave ChatGPT Plus or Pro? It can run Loopcut now."
        balance = f.ui.get("credit_balance")
        if item["reason"] in HOSTED_LIMITS and isinstance(balance, (int, float)) and balance > 0:
            body += "\nYour credits pay for cloud tools and do not add chat time."
    f.text(f"item{n}.limit.title", inner_x, cursor, title, size, color)
    cursor += f.line_height(size) + f.px(4)
    cursor += f.paragraph(f"item{n}.limit.text", inner_x, cursor, inner_w, body, small, T.TEXT)
    newest = n == state.latest_limit(f.session["items"])
    if newest and status != "done" and not f.session.get("busy"):
        cursor += f.px(8)
        buttons = _limit_buttons(f, n, item, f"item{n}.limit")
        cursor += _button_row(f, inner_x, cursor, inner_w, buttons)
    height = cursor + pad - y
    f.prims.insert(mark, {"t": "rect", "id": f"item{n}.limit.card", "x": x, "y": y, "w": width, "h": height,
                          "color": T.CARD, "radius": f.px(T.RADIUS), "border": 1, "border_color": T.CARD_BORDER})
    return height


_CHANGE_COLORS = {"+": T.OK, "-": T.ERROR, "~": T.TEXT_MUTED}
CHANGES_PREVIEW_LINES = 6


def _change_summary(lines: list[str]) -> str:
    """Names from the saved diff; all measured values stay in the expandable original lines."""
    names = []
    for line in lines:
        if line[:2] not in {"+ ", "- ", "~ "}:
            continue
        text = line[2:]
        name = ("Scene settings" if text.startswith("scene ") else
                "Interaction mode" if text.startswith("mode ") else
                re.split(r": | \(|; | at \[", text, maxsplit=1)[0])
        name = name[:45].rstrip() + "…" if len(name) > 48 else name
        if name and name not in names:
            names.append(name)
    return ", ".join(names[:3]) + (", and more." if len(names) > 3 else "." if names else "")


def _item_changes(f: Frame, n: int, item: dict, x, y, width) -> int:
    """What the turn did to the scene, and the way back: the panel's diff view."""
    pad, size, small = f.px(T.CARD_PAD), f.px(T.FONT_SIZE), f.px(T.FONT_SIZE_SMALL)
    mark = len(f.prims)
    inner_x, inner_w, cursor = x + pad, width - 2 * pad, y + pad
    f.text(f"item{n}.changes.title", inner_x, cursor, "Scene changes", size, T.TEXT)
    cursor += f.line_height(size) + f.px(4)
    cursor += f.paragraph(f"item{n}.changes.count", inner_x, cursor, inner_w, item["text"], small, T.TEXT)

    lines, expanded = item.get("lines", []), item.get("expanded", False)
    summary = _change_summary(lines)
    if summary:
        cursor += f.paragraph(f"item{n}.changes.summary", inner_x, cursor, inner_w, summary, small, T.TEXT_MUTED)
    code = f.px(T.FONT_SIZE_CODE)
    rows = []
    if expanded:
        for k, line in enumerate(lines):
            signed = line[:2] in {"+ ", "- ", "~ "}
            rows.extend((k, row, line[:1] if signed else "", text)
                        for row, (_, text) in enumerate(wrap(line[2:] if signed else line, "mono", code,
                                                             inner_w - f.px(14), f.measure)))
    for k, row, sign, text in rows:
        if row == 0:
            f.text(f"item{n}.changes.sign{k}", inner_x, cursor, sign, code,
                   _CHANGE_COLORS.get(sign, T.TEXT_MUTED), "mono")
        f.text(f"item{n}.changes.l{k}.l{row}", inner_x + f.px(14), cursor, text.rstrip(), code,
               T.TEXT if sign in {"+", "-"} else T.TEXT_MUTED, "mono")
        cursor += f.line_height(code)
    if lines:
        label = "Show less" if expanded else "Show more"
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


# A worker row's status: glyph, color, and whether the row can still be cancelled on its own.
# Merging is one main-thread call that completes or rolls back, so it has no Cancel.
WORKER_STATUS = {
    "queued": ("●", T.TEXT_FAINT, True),
    "starting": ("●", T.ACCENT, True),
    "working": ("●", T.ACCENT, True),
    "checking": ("●", T.ACCENT, True),
    "merging": ("●", T.ACCENT, False),
    "done": ("✓", T.OK, False),
    "partial": ("✓", T.WARN, False),     # Built, but some of its checks fail, or a budget stopped it.
    "failed": ("✕", T.ERROR, False),
    "cancelled": ("–", T.TEXT_FAINT, False),
    "stopped": ("–", T.TEXT_FAINT, False),  # A row that was running when the conversation was saved.
    "question": ("?", T.WARN, False),
}


def _worker_status(row: dict) -> str:
    """The row's status word; a worker whose result is in the scene says merged (the orchestrator has
    it while the others still run)."""
    status = row.get("status", "")
    if row.get("unavailable"):
        return "unavailable"
    if any(row.get(key) for key in ("merge_error", "quarantined", "conflicts")):
        if row.get("merged"):
            return "partly merged"
        if not row.get("merge_error"):
            return "held back" if row.get("quarantined") else "conflicted"
    if row.get("merge_error"):
        return "failed"
    if row.get("cancelled_before_merge") and not row.get("merged"):
        return "stopped"
    if not row.get("merged"):
        return status
    return "merged" if status == "done" else f"{status} · merged"


def _waiting_first_reply(row: dict) -> bool:
    timing = row.get("timing") or {}
    return (row.get("status") == "working" and not row.get("steps")
            and timing.get("first_request") is not None and timing.get("first_reply") is None)


def _worker_detail(row: dict, now: float = 0) -> str:
    """Elapsed time, role, steps and checks. A saved duration stays frozen."""
    steps = row.get("steps") or 0
    parts = [row.get("role", "")]
    if not _waiting_first_reply(row) and (steps or row.get("status") not in ("queued", "starting")):
        parts.append(f"{steps} step{'s' if steps != 1 else ''}")
    elapsed = row.get("elapsed")
    if elapsed is None and row.get("started_at") is not None and row.get("status") in state.WAVE_RUNNING:
        elapsed = max(0, now - row["started_at"])
    if elapsed is not None and elapsed >= 1:
        minutes, seconds = divmod(round(elapsed), 60)
        parts.insert(0, f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s")
    if row.get("checks"):
        parts.append(f"checks {row['checks']}")
    return " · ".join(p for p in parts if p)


def _item_wave(f: Frame, n: int, item: dict, x, y, width) -> int:
    """One delegate call's workers, a row each: what it builds, where it is, its steps and checks.
    A running row can be cancelled by itself; the other workers carry on."""
    pad, size, small = f.px(T.CARD_PAD), f.px(T.FONT_SIZE), f.px(T.FONT_SIZE_SMALL)
    mark = len(f.prims)
    inner_x, inner_w, cursor = x + pad, width - 2 * pad, y + pad
    f.text(f"item{n}.wave.title", inner_x, cursor, _ellipsize(f, item["text"], size, inner_w), size, T.TEXT)
    cursor += f.line_height(size) + f.px(4)
    row_height, small_height = f.line_height(size), f.line_height(small)
    for k, row in enumerate(item.get("rows", [])):
        cursor += f.px(6) if k else 0
        id, status = f"item{n}.wave.row{k}", row.get("status", "")
        if row.get("merge_error"):
            status = "failed"
        elif row.get("quarantined") or row.get("conflicts"):
            status = "partial"
        elif row.get("cancelled_before_merge") and not row.get("merged"):
            status = "stopped"
        glyph, color, cancellable = WORKER_STATUS.get(status, ("●", T.TEXT_FAINT, False))
        name_x = inner_x + f.px(18)
        status = _worker_status(row)
        status_width = round(f.measure("ui", small, status))
        f.text(f"{id}.glyph", inner_x, cursor, glyph, size, color)
        f.text(f"{id}.name", name_x, cursor,
               _ellipsize(f, row.get("deliverable") or row.get("id", ""), size,
                          inner_x + inner_w - name_x - status_width - f.px(10)), size, T.TEXT)
        f.text(f"{id}.status", inner_x + inner_w - status_width, cursor + (row_height - small_height) // 2,
               status, small, color)
        cursor += row_height
        right = inner_x + inner_w
        if cancellable and f.session.get("busy"):
            label = "Cancel"
            label_width = round(f.measure("ui", small, label))
            right -= label_width
            f.text(f"{id}.cancel", right, cursor, label, small, T.TEXT_MUTED)
            f.hit(f"{id}.cancel", right - f.px(4), cursor - f.px(2), label_width + f.px(8), small_height + f.px(4),
                  ("worker_cancel", (item["wave"], row["id"])), "Stop this worker; the others carry on")
            right -= f.px(10)
        f.text(f"{id}.detail", name_x, cursor,
               _ellipsize(f, _worker_detail(row, f.ui.get("now", 0)), small, right - name_x), small,
               T.TEXT_MUTED)
        cursor += small_height
        if _waiting_first_reply(row):
            cursor += f.paragraph(f"{id}.waiting", name_x, cursor, inner_x + inner_w - name_x,
                                  "Waiting for the model's first reply…", small, T.TEXT_MUTED)
        outcome = state.worker_outcome(row)
        if outcome:
            cursor += f.paragraph(f"{id}.outcome", name_x, cursor, inner_x + inner_w - name_x,
                                  outcome, small, T.TEXT)
        if row.get("merged_changes"):
            cursor += f.paragraph(f"{id}.merged_changes", name_x, cursor, inner_x + inner_w - name_x,
                                  "Merged: " + "; ".join(row["merged_changes"]), small, T.TEXT_MUTED)
        if row.get("summary"):
            cursor += f.paragraph(f"{id}.summary", name_x, cursor, inner_x + inner_w - name_x,
                                  row["summary"], small, T.TEXT_MUTED)
        if row.get("note"):
            cursor += f.paragraph(f"{id}.note", name_x, cursor, inner_x + inner_w - name_x, row["note"], small,
                                  T.TEXT_FAINT)
        image_path = row.get("image_path")
        if image_path and image_path not in f.ui.get("missing_paths", ()):
            cursor += f.px(6)
            cursor += _image_preview(f, f"{id}.image", image_path, row.get("deliverable") or "Helper preview",
                                     name_x, cursor, inner_x + inner_w - name_x, 180)
    height = cursor + pad - y
    f.prims.insert(mark, {"t": "rect", "id": f"item{n}.wave.card", "x": x, "y": y, "w": width, "h": height,
                          "color": T.CARD, "radius": f.px(T.RADIUS), "border": 1, "border_color": T.CARD_BORDER})
    return height


_MERGE_COLORS = {**_CHANGE_COLORS, "!": T.WARN}  # "!" marks a conflict or a quarantined datablock.


def _item_merge(f: Frame, n: int, item: dict, x, y, width) -> int:
    """A wave's results merged into the live scene: what came in, what was replaced, what was held
    back and what conflicted. Held-back datablocks wait in a hidden collection until accepted; the
    wave's checkpoint is the way back to the scene before the merge."""
    pad, size, small = f.px(T.CARD_PAD), f.px(T.FONT_SIZE), f.px(T.FONT_SIZE_SMALL)
    mark = len(f.prims)
    inner_x, inner_w, cursor = x + pad, width - 2 * pad, y + pad
    cursor += f.paragraph(f"item{n}.merge.title", inner_x, cursor, inner_w, item["text"], size, T.TEXT) + f.px(4)

    lines, expanded = item.get("lines", []), item.get("expanded", False)
    shown = lines if expanded or len(lines) <= CHANGES_PREVIEW_LINES else lines[:CHANGES_PREVIEW_LINES - 1]
    code = f.px(T.FONT_SIZE_CODE)
    for k, line in enumerate(shown):
        signed = line[:2] in ("+ ", "- ", "~ ", "! ")
        if signed:
            f.text(f"item{n}.merge.sign{k}", inner_x, cursor, line[:1], code, _MERGE_COLORS[line[:1]], "mono")
        cursor += f.paragraph(f"item{n}.merge.l{k}", inner_x + f.px(14), cursor, inner_w - f.px(14),
                              line[2:] if signed else line, code,
                              T.TEXT if signed and line[:1] in "+-!" else T.TEXT_MUTED, "mono")
    if len(shown) < len(lines) or expanded and len(lines) > CHANGES_PREVIEW_LINES:
        label = "Show less" if expanded else f"Show {len(lines) - len(shown)} more"
        f.text(f"item{n}.merge.more", inner_x + f.px(14), cursor + f.px(2), label, small, T.ACCENT)
        f.hit(f"item{n}.merge.more", inner_x, cursor, inner_w, f.line_height(small) + f.px(4), ("toggle", n))
        cursor += f.line_height(small) + f.px(4)

    quarantined = item.get("quarantined", [])
    if item.get("accepted") or quarantined and item.get("resolved") == "accepted":
        cursor += f.px(4)
        cursor += f.paragraph(f"item{n}.merge.accepted", inner_x, cursor, inner_w,
                              ("Accepted into the scene: " + "; ".join(item["accepted"]) + "."
                               if item.get("accepted") else "Held-back items accepted into the scene."), small, T.TEXT_FAINT)
    restorable = item.get("checkpoint") and f.checkpoints.get(item["checkpoint"], "ok") == "ok"
    accept = quarantined and not item.get("resolved")
    if (accept or restorable) and not f.session.get("busy"):
        cursor += f.px(8)
        bx = inner_x
        if accept:
            bx += f.button(f"item{n}.merge.accept", bx, cursor, f"Accept {len(quarantined)} held back", T.BUTTON,
                           T.BUTTON_TEXT, ("accept_quarantine", n)) + f.px(8)
        if restorable:
            f.button(f"item{n}.merge.restore", bx, cursor, "Restore before merge", T.BUTTON_GHOST, T.TEXT,
                     ("restore", item["checkpoint"]))
        cursor += f.px(24)
    height = cursor + pad - y
    f.prims.insert(mark, {"t": "rect", "id": f"item{n}.merge.card", "x": x, "y": y, "w": width, "h": height,
                          "color": T.CARD, "radius": f.px(T.RADIUS), "border": 1,
                          "border_color": T.WARN if accept else T.CARD_BORDER})
    return height


_ITEM_LAYOUT = {"changes": _item_changes, "notice": _item_notice, "user": _item_user, "assistant": _item_assistant,
                "tool": _item_tool, "error": _item_error, "limit": _item_limit, "wave": _item_wave,
                "merge": _item_merge, "offer": _item_offer}


# Starter prompts on an empty conversation: one click sends one (or fills the input while no model is connected).
# Each one shows what Loopcut does well and finishes on the free tier with no credits: Loopcut Fast, local tools,
# and the free asset search (Loopcut's index when signed in, BlenderKit's free assets and Poly Haven otherwise).
# Verified 2026-10-20 on Fast's model (glm-5.3-flash with the hosted prompts) from the default scene, clicked in the
# panel: chest 151 s, chair 41 s (BlenderKit free, signed out), ball 102 s, rocks 30 s, each finished by the
# orchestrator itself. Lighting plus camera ("light it like a sunset and frame it") was dropped: it delegated four
# worker waves and ran past 5 minutes. Change one only after running it on the default model again.
EXAMPLE_REQUESTS = (
    "Model a stylized treasure chest with gold trim",
    "Find a wooden chair in the free asset libraries and add it",
    "Animate a rubber ball bouncing, with squash and stretch",
    "Scatter twenty low-poly rocks of different sizes around the cube",
)


SETUP_BOTH = "Best setup: both. ChatGPT runs the chat; Loopcut adds assets and cloud tools."
SETUP_TIPS = {"loopcut": "Free Loopcut account: asset search and cloud tools.\nIts Fast model also runs the agent.",
              "chatgpt": "Runs the agent on your ChatGPT Plus or Pro plan,\nwith no charge from Loopcut."}


def _setup_routes(f: Frame, x, cursor, width) -> int:
    """All three routes stay reachable after a failed first message or browser sign-in."""
    small = f.px(T.FONT_SIZE_SMALL)
    statuses = f.ui.get("sign_in") or {}
    loopcut, chatgpt = statuses.get("loopcut", {}), statuses.get("chatgpt", {})
    if loopcut.get("status") in {"starting", "waiting"}:
        text = (f"Approve code {loopcut.get('user_code', '')} in your browser."
                if loopcut["status"] == "waiting" else "Contacting Loopcut…")
        cursor += f.paragraph("chat.signin.waiting", x, cursor, width, text, small, T.TEXT_MUTED) + f.px(6)
        if loopcut.get("url"):
            used = f.button("chat.signin.copy", x, cursor, "Copy Link", T.BUTTON_GHOST, T.TEXT,
                            ("sign_in_copy", "loopcut"))
        else:
            used = 0
        f.button("chat.signin.cancel", x + used + f.px(6), cursor, "Cancel", T.BUTTON_GHOST, T.TEXT,
                 ("sign_in_cancel", "loopcut"))
        cursor += f.px(30)
    elif chatgpt.get("status") == "waiting":
        cursor += f.paragraph("chat.signin.waiting", x, cursor, width, "Finish signing in with ChatGPT in your browser.",
                              small, T.TEXT_MUTED) + f.px(6)
        used = (f.button("chat.signin.copy", x, cursor, "Copy Link", T.BUTTON_GHOST, T.TEXT,
                         ("sign_in_copy", "chatgpt")) if chatgpt.get("url") else 0)
        f.button("chat.signin.cancel", x + used + f.px(6), cursor, "Cancel", T.BUTTON_GHOST, T.TEXT,
                 ("sign_in_cancel", "chatgpt"))
        cursor += f.px(30)
    else:
        cursor += f.paragraph("chat.signin.both", x, cursor, width, SETUP_BOTH, small, T.TEXT_MUTED) + f.px(6)
        labels = (("loopcut", "Sign in to Loopcut"), ("chatgpt", "Sign in with ChatGPT"))
        button_w = max(round(f.measure("ui", small, label)) for _, label in labels) + f.px(20)
        stacked = 2 * button_w + f.px(8) > width
        button_w = min(button_w, width)
        for n, (route, label) in enumerate(labels):
            left, y = (x, cursor + n * f.px(30)) if stacked else (x + n * (button_w + f.px(8)), cursor)
            id = "chat.signin." + route
            f.rect(id, left, y, button_w, f.px(24), T.BUTTON_GHOST, f.px(T.RADIUS_SMALL), 1, T.CARD_BORDER)
            f.text(id + ".label", left + f.px(10), y + f.px(3),
                   _ellipsize(f, label, small, button_w - f.px(20)), small, T.TEXT)
            f.hit(id, left, y, button_w, f.px(24), ("sign_in", route), tip=SETUP_TIPS[route])
        cursor += f.px(60 if stacked else 30)
        for route, status in (("loopcut", loopcut), ("chatgpt", chatgpt)):
            if status.get("error"):
                cursor += f.paragraph("chat.signin." + route + ".error", x, cursor, width,
                                      status["error"], small, T.ERROR) + f.px(4)
        if chatgpt.get("error"):
            f.button("chat.signin.another", x, cursor, "Use another ChatGPT account", T.BUTTON_GHOST, T.TEXT_MUTED,
                     ("sign_in_new", None))
            cursor += f.px(30)
    if loopcut.get("url") or chatgpt.get("url"):
        cursor += f.paragraph("chat.signin.browser", x, cursor, width,
                              "Browser did not open? Copy Link and paste it into your browser.", small, T.TEXT_FAINT)
    label = "or use your own key"
    f.text("chat.setup.label", x, cursor + f.px(3), label, small, T.TEXT_MUTED)
    f.hit("chat.setup", x, cursor, width, f.px(24), ("own_key", None),
          tip="Preferences: choose a model provider, enter its API key and model name. Cloud tool keys are separate.")
    return cursor + f.px(30)


def _key_prompt(f: Frame, x, cursor, width) -> int:
    """An own-key provider chosen without its key (F0287): ask for that key, and offer the connected accounts back.
    Not the first-run sign-in card, which read as if both accounts had been disconnected."""
    small, provider = f.px(T.FONT_SIZE_SMALL), f.ui["key_needed"]
    text = f"{provider} is chosen as the model provider and needs its API key. Chats then bill that key's account."
    cursor += f.paragraph("chat.key.text", x, cursor, width, text, small, T.TEXT_MUTED) + f.px(6)
    buttons = [("chat.key.add", f"Add your {provider} key", T.BUTTON, T.BUTTON_TEXT, ("own_key", None))]
    if f.ui.get("chatgpt_plan"):
        buttons.append(("chat.key.chatgpt", "Use ChatGPT plan", T.BUTTON_GHOST, T.TEXT, ("use_provider", "CHATGPT")))
    if f.ui.get("loopcut_signed_in"):
        buttons.append(("chat.key.loopcut", "Use Loopcut", T.BUTTON_GHOST, T.TEXT, ("use_provider", "LOOPCUT")))
    return cursor + _button_row(f, x, cursor, width, buttons) + f.px(6)


def _first_run(f: Frame, x, cursor, width) -> int:
    """The starter prompts on an empty conversation, under the scope note until the first message is sent."""
    small = f.px(T.FONT_SIZE_SMALL)
    if not f.ui.get("hints_dismissed"):
        cursor += f.paragraph("chat.hints.scope", x, cursor, width,
                              "Loopcut can inspect and edit this scene; it can't see other apps. "
                              "Code asks for approval by default.", small, T.TEXT_FAINT) + f.px(8)
    sends = not f.ui.get("needs_setup")
    cursor += f.paragraph("chat.hints.title", x, cursor, width, "Try one:" if sends else "Then try one:",
                          small, T.TEXT_MUTED) + f.px(4)
    for n, request in enumerate(EXAMPLE_REQUESTS):
        id = f"chat.hint{n}"
        height = len(wrap(request, "ui", small, width - f.px(28), f.measure)) * f.line_height(small) + f.px(10)
        hover = f.ui.get("hover") == id
        f.rect(id + ".bg", x, cursor, width, height, T.BUTTON_GHOST if hover else T.NONE,
               f.px(T.RADIUS_SMALL), 1, T.ACCENT if hover else T.CARD_BORDER)
        f.text(id + ".mark", x + f.px(8), cursor + f.px(5), "›", small, T.ACCENT)
        f.paragraph(id + ".label", x + f.px(20), cursor + f.px(5), width - f.px(28), request, small,
                    T.TEXT if hover else T.TEXT_MUTED)
        f.hit(id, x, cursor, width, height, ("example_request", n),
              tip="Send this to Loopcut" if sends else "Fill the input; connect a model above to send it")
        cursor += height + f.px(4)
    return cursor


def _plan_names(item: dict) -> list:
    """Fallback identity from saved arguments when the call did not supply worker ids."""
    names = item.get("plan_names")
    if isinstance(names, list) and all(isinstance(pair, list) and len(pair) == 2
                                       and all(isinstance(s, str) for s in pair) for pair in names):
        return names

    def decoded(value):
        if isinstance(value, str):
            try:
                return json.loads(value)
            except ValueError:
                return None
        return value

    args = decoded(item.get("code"))
    if not isinstance(args, dict):
        return []
    wave = decoded(args.get("wave"))
    crew = decoded(wave.get("workers") if isinstance(wave, dict) else args.get("workers"))
    if not isinstance(crew, list):
        return []
    from .. import orchestrator
    names = [tuple(value.strip() if isinstance(value, str) else "" for value in
                   (w.get("deliverable"), w.get("role"))) for w in crew if isinstance(w, dict)]
    return [(name, orchestrator.normalise_role(role) if role else "") for name, role in names]


def _plan_names_match(wanted: list, started: list) -> bool:
    """Each named helper needs a distinct start matching every name it supplied."""
    matched = {}

    def assign(index, seen):
        for n, candidate in enumerate(started):
            if n in seen or not all(not name or name == value for name, value in zip(wanted[index], candidate)):
                continue
            seen.add(n)
            if n not in matched or assign(matched[n], seen):
                matched[n] = index
                return True
        return False

    return all(assign(n, set()) for n in range(len(wanted)))


def _plan_repairs(items: list, active_turn: str = "") -> dict:
    """Recovery is a view of the current transcript, so restore, undo and reload need no repair.
    A deliverable can change during repair or belong to different helpers; match worker ids and roles.
    Without ids, retain any deliverable/role names. Without names either, only the very next
    delegate call in that turn can recover the attempt, and only if it actually starts."""
    successful, repairs, started, next_delegate = {}, {}, {}, {}
    for n in reversed(range(len(items))):
        item = items[n]
        if item.get("kind") != "tool" or item.get("name") != "delegate":
            continue
        turn = item.get("plan_turn") or item.get("turn_id")
        if item.get("plan_repair"):
            repairs[n] = "adjusting" if active_turn and turn == active_turn else "needed"
        if not isinstance(turn, str) or not turn:
            continue
        if item.get("plan_repair") and item.get("plan_unidentified"):
            names = [pair for pair in _plan_names(item) if any(pair)]
            if names:
                recovered = any(_plan_names_match(names, crew) for crew in started.get(turn, []))
            else:
                retry = next_delegate.get(turn, {})
                recovered = retry.get("plan_started") and not retry.get("plan_repair")
            if recovered:
                repairs[n] = "resolved"
        next_delegate[turn] = item
        if item.get("plan_started") and not item.get("plan_repair"):
            started.setdefault(turn, []).append(_plan_names(item))
        crew = item.get("plan_workers")
        if not isinstance(crew, list) or not crew or any(
                not isinstance(w, list) or len(w) != 2 or not all(isinstance(s, str) for s in w)
                or not w[0].strip() for w in crew):
            continue
        key = (turn, tuple(sorted(tuple(w) for w in crew)))
        if item.get("plan_repair"):
            if key in successful:
                repairs[n] = successful[key]
        elif item.get("plan_started"):
            successful[key] = "resolved"
        elif item.get("status") == "done" and "plan_dry_run" not in item:
            # 0.2.11 also saved dry runs this way: completion proves a retry, not a helper boot.
            successful.setdefault(key, "retried")
    return repairs


def _code_repairs(items: list, busy: bool, active_turn_id=None) -> dict:
    """Describe whether the turn carried on, never whether later work repaired a failed step.
    Derive from the remaining transcript so reload and checkpoint restore give the same view."""
    turns, errors, last_step = [], set(), {}
    legacy = 0
    current = ("legacy", legacy)
    for n, item in enumerate(items):
        kind = item.get("kind")
        if kind == "user" and not item.get("queued"):
            legacy += 1
            current = ("legacy", legacy)
        turn = item.get("turn_id")
        if isinstance(turn, str) and turn:
            current = ("turn", turn)
        turns.append(current)
        if (kind in {"error", "limit"}
                or item.get("turn_outcome") in {"error", "cancelled", "step_limit", "budget", "no_progress"}):
            errors.add(current)
        if kind == "tool" and item.get("status") in {"done", "failed"}:
            last_step[current] = n
        if kind in {"changes", "error", "limit"}:
            legacy += 1
            current = ("legacy", legacy)
    active = ("turn", active_turn_id) if active_turn_id else current
    repairs = {}
    for n, item in enumerate(items):
        if (item.get("kind") != "tool" or item.get("name") not in CODE_TOOLS
                or item.get("status") != "failed" or item.get("output", "").startswith("Not run.")):
            continue
        if busy and turns[n] in {active, ("legacy", legacy)}:
            repairs[n] = "correcting"
        elif turns[n] not in errors and last_step.get(turns[n]) != n:
            repairs[n] = "continued"
    return repairs


def _chat(f: Frame, session: dict, top: int, bottom: int) -> float:
    """Lays the conversation out at y=0, then slides it into the viewport. Returns max scroll."""
    pad, gap = f.px(T.PAD), f.px(T.GAP)
    x, width = pad, f.width - 2 * pad
    items = list(session["items"])  # Snapshot: the worker thread appends while we draw.
    active_turn = getattr(session.get("turn"), "plan_turn", "") if session.get("busy") else ""
    repairs = _plan_repairs(items, active_turn)
    code_repairs = _code_repairs(items, session.get("busy", False), session.get("active_turn_id"))
    first_prim, first_hit, cursor = len(f.prims), len(f.hits), 0

    for n, item in enumerate(items):
        if n in repairs:
            item = {**item, "plan_repair": repairs[n]}
        if item.get("kind") == "tool" and item.get("name") in CODE_TOOLS:
            item = {**item, "code_repair": code_repairs.get(n)}
        cursor += _ITEM_LAYOUT[item["kind"]](f, n, item, x, cursor, width) + gap
    cursor = _cloud_jobs(f, cursor)
    # Queued notes do not replace the active stream or tool as the source of activity.
    tool_statuses = {item["status"] for item in items if item["kind"] == "tool"}
    if session["busy"] and "awaiting" not in tool_statuses:
        stream = next((n for n in reversed(range(len(items)))
                       if items[n]["kind"] == "assistant" and items[n].get("streaming")), None)
        if stream is not None and "running" not in tool_statuses:
            cursor += thinking(f, f"item{stream}.thinking", x, cursor, f.px(T.FONT_SIZE)) + gap
        else:
            cursor += thinking(f, "chat.working", x, cursor, f.px(T.FONT_SIZE), "Working") + gap

    empty = not items and not f.ui.get("cloud_jobs") and not session["busy"]
    if empty:
        cursor += f.paragraph("chat.empty0", x, cursor, width, "Ask for anything in your scene.",
                              f.px(T.FONT_SIZE), T.TEXT_MUTED) + gap
    if f.ui.get("needs_setup") and not session["busy"]:
        cursor = _key_prompt(f, x, cursor, width) if f.ui.get("key_needed") else _setup_routes(f, x, cursor, width)
    if empty and not f.ui.get("key_needed"):
        cursor = _first_run(f, x, cursor, width)

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


def input_rows(display: dict | None, text: str, measure):
    """The input's wrapped rows for this text at the width the last draw used, and a prefix measure;
    (None, None) before the box was drawn. Rewrapped from the text, so edits since the draw count."""
    box = (display or {}).get("input")
    if not box:
        return None, None
    size = box["size"]
    return wrap(text, "ui", size, box["width"], measure), (lambda prefix: measure("ui", size, prefix))


def input_index(display: dict, x: float, y_top: float, measure) -> int | None:
    """Nearest caret in the drawn input's visible lines, clamped at their edges."""
    box = display.get("input")
    if box is None:
        return None
    lines = box["lines"]
    row = max(0, min(len(lines) - 1, int((y_top - box["y"]) // box["line_height"])))
    start, line = lines[row]
    if row + 1 < len(lines) and lines[row + 1][0] == start + len(line):
        # The shared offset belongs to the next row: stop before hidden spaces or the last character.
        line = line.rstrip() if line[-1:].isspace() else line[:-1]
    left = 0.0
    for column in range(len(line)):
        right = measure("ui", box["size"], line[:column + 1])
        if x - box["x"] < (left + right) / 2:
            return start + column
        left = right
    return start + len(line)


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


def _input_notice(f: Frame, y: int) -> int:
    """Usage and transient feedback above the composer, with their own measured space."""
    pad, small = f.px(T.PAD), f.px(T.FONT_SIZE_SMALL)
    width = f.width - 2 * pad
    usage = ""
    if f.ui.get("provider", "LOOPCUT") == "LOOPCUT":
        usage, _ = usage_note(f.ui.get("account"), f.ui.get("allowance_notice"), f.ui.get("now"))
    height = 0
    if usage:
        usage += " · AI use includes helpers, not time; credits buy cloud tools, not more chat."
        height = f.paragraph("input.usage", pad, y, width, usage, small, T.TEXT_MUTED) + f.px(6)
        y += height
    notice = f.ui.get("allowance_notice")
    item = notice.get("item") if notice else None
    if item and item.get("reason") in allowance.LIMIT_WINDOWS \
            and allowance.refused_window(f.ui.get("account"), notice, f.ui.get("now")) is None:
        item = None
    if item and not f.session.get("busy"):
        reset = state.reset_label(item.get("resets_at", ""), now=f.ui.get("now"))
        line = f"Chat allowance used. Resets {reset}." if reset else "Chat allowance used."
        balance = f.ui.get("credit_balance")
        if not usage and isinstance(balance, (int, float)) and balance > 0:
            line += " Your credits pay for cloud tools and do not add chat time."
        limit_height = f.paragraph("input.limit.text", pad, y, width, line, small, T.WARN) + f.px(6)
        n = next((n for n, row in enumerate(f.session.get("items", [])) if row is item), None)
        if n is None and notice.get("conversation") == f.session.get("id"):
            n = f.session["id"]
        return height + limit_height + _button_row(f, pad, y + limit_height, width,
                                                  _limit_buttons(f, n, item, "input.limit")) + f.px(6)
    notice = f.ui.get("action_notice")
    if not notice or f.ui.get("now", 0.0) >= notice["until"]:
        return height
    return height + f.paragraph("input.notice", pad, y, width, notice["text"], small, T.OK) + f.px(6)


def input_height(f: Frame, session: dict) -> int:
    pad, card_pad = f.px(T.PAD), f.px(T.CARD_PAD)
    inner_w = f.width - 2 * pad - 2 * card_pad
    size, _, visible = _input_lines(f, session, inner_w)
    measuring = Frame(f.width, f.height, f.scale, f.measure, session=session, ui=f.ui)
    chips = _chip_rows(measuring, "measure", 0, 0, inner_w, session)
    chips += f.px(6) if chips else 0
    return (visible * f.line_height(size) + 2 * card_pad + f.px(26) + 2 * pad
            + _context_row_height(f) + chips + _cloud_height(f) + _credit_height(f, _credit_lines(f, inner_w))
            + _input_notice(measuring, 0))


def _credit_lines(f: Frame, width: int) -> list[str]:
    """The credits block above the input footer (C47): the balance, then this chat's spend and today's (server
    account.spend), on one line when they fit and one each when the panel is narrow."""
    small = f.px(T.FONT_SIZE_SMALL)
    spend = f.session.get("spend")
    chat = spending.chat_label(spend) if f.ui.get("credits") or (
        spending.valid_ledger(spend) and spend.get("credits")) else ""
    extras = [part for part in (chat, f.ui.get("today") or "") if part]
    lines = [f.ui["credits"]] if f.ui.get("credits") else []
    if extras and f.measure("ui", small, " · ".join(extras)) <= width:
        return lines + [" · ".join(extras)]
    return lines + extras


def _credit_height(f: Frame, lines: list[str]) -> int:
    return f.line_height(f.px(T.FONT_SIZE_SMALL)) * len(lines) + f.px(6) if lines else 0


_INTEREST_SIGN_IN = 'Sign in to hear when your picks launch.'
VOTE_TITLE = 'What should we build next?'
VOTE_STRIP = 'Vote on what we build next'
VOTE_SUB = 'Pick up to 3. You can change them any time.'
VOTE_FOOTNOTE = 'Bars compare ideas against the most wanted one. None of these run yet.'
VOTE_FOOTNOTE_OFFLINE = 'Your votes go to the Loopcut team. None of these run yet.'
VOTES_MAX = 3


def _vote_rows(f):
    """The coming_soon rows in the order the view opened with (most wanted first), new rows after."""
    rows = {row['name']: row for row in f.ui.get('coming_soon') or []}
    order = [name for name in f.ui.get('vote_order') or [] if name in rows]
    rest = sorted((row for name, row in rows.items() if name not in order), key=lambda row: -(row.get('level') or 0))
    return [rows[name] for name in order] + rest


def _cloud_height(f):
    message = f.px(24) if f.ui.get('cloud_message') or f.ui.get('cloud_message_reserved') else 0
    strip = f.px(28) if f.ui.get('coming_soon') and f.ui.get('view', 'chat') != 'vote' else 0
    return message + strip


def _cloud_buttons(f, top):
    x, size, y = f.px(T.PAD), f.px(T.FONT_SIZE_SMALL), top
    if f.ui.get('cloud_message'):
        message = f.ui['cloud_message']
        f.text('cloud.message', x, y, _ellipsize(f, message, size, f.width - 2 * x - f.px(28)), size, T.TEXT_MUTED)
        right = f.width - x - f.px(24)
        f.text('cloud.dismiss.label', right + f.px(6), y, '×', size, T.TEXT_FAINT)
        f.hit('cloud.dismiss', right, y, f.px(24), f.px(22), ('cloud_dismiss', message), tip=message + '\nDismiss')
    if f.ui.get('cloud_message') or f.ui.get('cloud_message_reserved'):
        y += f.px(24)
    if f.ui.get('coming_soon') and f.ui.get('view', 'chat') != 'vote':
        _vote_strip(f, x, y, f.width - 2 * x)


def _vote_strip(f, x, y, width):
    """Under the composer: one quiet line into the vote, naming what leads."""
    size, height = f.px(T.FONT_SIZE_SMALL), f.px(24)
    hover = f.ui.get('hover') == 'vote.strip'
    if hover:
        f.rect('vote.strip.bg', x, y, width, height, T.BUTTON_GHOST, f.px(T.RADIUS_SMALL))
    text_y = y + (height - f.line_height(size)) // 2
    f.text('vote.strip.glyph', x + f.px(8), text_y, '▲', size, T.ACCENT)
    label_x = x + f.px(24)
    f.text('vote.strip.label', label_x, text_y, VOTE_STRIP, size, T.TEXT if hover else T.TEXT_MUTED)
    rows = _vote_rows(f)
    leader = rows[0] if rows and rows[0].get('badge') == 'most_wanted' else None
    used = round(f.measure('ui', size, VOTE_STRIP)) + f.px(24 + 12)
    tail = (leader['title'] + ' leads  ›') if leader else '›'
    if f.measure('ui', size, tail) + used > width - f.px(8):
        tail = '›'
    tail_w = round(f.measure('ui', size, tail))
    f.text('vote.strip.tail', x + width - f.px(8) - tail_w, text_y, tail, size, T.TEXT_FAINT)
    f.hit('vote.strip', x, y, width, height, ('votes_open', None), tip='See what testers want next and add your votes')


VOTE_FOOTER_HEIGHT = 52


def _vote_tokens(f, id, x, y, used):
    """VOTES_MAX dots, filled for the votes placed; returns their width."""
    d, gap = f.px(9), f.px(5)
    for i in range(VOTES_MAX):
        filled = i < used
        f.ring(f'{id}.{i}', x + i * (d + gap), y, d, 1.0, d // 2 if filled else max(1, f.px(1.5)),
               T.ACCENT if filled else T.INPUT_BORDER)
    return VOTES_MAX * d + (VOTES_MAX - 1) * gap


def _vote_row(f, n, row, x, y, width, left) -> int:
    """One idea as a poll result: the fill behind it is its demand next to the leader's."""
    size, small, height = f.px(T.FONT_SIZE), f.px(T.FONT_SIZE_SMALL), f.px(38)
    id, voted, hover = 'vote.' + row['name'], bool(row.get('voted')), f.ui.get('hover') == 'vote.' + row['name']
    f.rect(id + '.row', x, y, width, height, _mix(T.CARD, T.TEXT, 0.03) if hover else T.CARD, f.px(T.RADIUS), 1,
           _mix(T.CARD_BORDER, T.ACCENT, 0.55) if voted else T.CARD_BORDER)
    level = row.get('level')
    if level is not None:
        fill_w = max(f.px(T.RADIUS * 2), round(width * max(0.0, min(1.0, level))))
        f.rect(id + '.fill', x, y, fill_w, height, _mix(T.CARD, T.ACCENT, 0.30 if voted else 0.16), f.px(T.RADIUS))
    toggle = f.px(24)
    tx, ty = x + width - f.px(8) - toggle, y + (height - toggle) // 2
    sending = row.get('state') == 'sending'
    if voted:
        f.rect(id + '.toggle', tx, ty, toggle, toggle, T.BUTTON, f.px(T.RADIUS_SMALL))
    else:
        f.rect(id + '.toggle', tx, ty, toggle, toggle, T.BUTTON_GHOST if hover else T.NONE, f.px(T.RADIUS_SMALL), 1,
               T.INPUT_BORDER if left or hover else T.CARD_BORDER)
    glyph = '…' if sending else '✓' if voted else '▲'
    glyph_w = round(f.measure('ui', small, glyph))
    f.text(id + '.toggle.glyph', tx + (toggle - glyph_w) // 2, ty + (toggle - f.line_height(small)) // 2, glyph, small,
           T.BUTTON_TEXT if voted else T.TEXT if hover else T.TEXT_MUTED if left else T.TEXT_FAINT)
    text_x = x + f.px(12)
    room = tx - f.px(10) - text_x
    badge = {'most_wanted': 'Most wanted', 'popular': 'Popular'}.get(row.get('badge'))
    badge_w = round(f.measure('ui', small, badge)) + f.px(12) if badge else 0
    if badge and f.measure('ui', size, row['title']) + f.px(8) + badge_w > room:
        badge = None  # A narrow panel keeps the whole title; the bar still shows the lead.
    title = _ellipsize(f, row['title'], size, room - (badge_w + f.px(8) if badge else 0))
    title_y = y + (height - f.line_height(size)) // 2
    f.text(id + '.title', text_x, title_y, title, size, T.TEXT)
    if badge:
        bx, bh = text_x + round(f.measure('ui', size, title)) + f.px(8), f.px(18)
        by = y + (height - bh) // 2
        f.rect(id + '.badge', bx, by, badge_w, bh, _mix(T.CARD, T.WARN, 0.18), bh // 2)
        f.text(id + '.badge.label', bx + f.px(6), by + (bh - f.line_height(small)) // 2, badge, small, T.WARN)
    action = 'Click to take your vote back' if voted else 'Click to vote' if left else \
        f'All {VOTES_MAX} votes are placed. Take one back to move it.'
    f.hit(id, x, y, width, height, ('vote', row['name']), tip='\n'.join(filter(None, (row.get('summary'), action))))
    return height


def _votes(f, session, top, bottom) -> float:
    """The vote view: every idea ranked as a poll, the leader's bar full. Never a count."""
    pad, small, title_size = f.px(T.PAD), f.px(T.FONT_SIZE_SMALL), f.px(T.FONT_SIZE_TITLE)
    x, width = pad, f.width - 2 * pad
    first_prim, first_hit, cursor = len(f.prims), len(f.hits), 0
    rows = _vote_rows(f)
    left = VOTES_MAX - sum(1 for row in rows if row.get('voted'))
    f.text('vote.title', x, cursor, VOTE_TITLE, title_size, T.TEXT)
    cursor += f.line_height(title_size)
    cursor += f.paragraph('vote.sub', x, cursor, width, VOTE_SUB, small, T.TEXT_MUTED)
    cursor += f.px(12)
    for n, row in enumerate(rows):
        cursor += _vote_row(f, n, row, x, cursor, width, left) + f.px(6)
    cursor += f.px(6)
    footnote = VOTE_FOOTNOTE if any(row.get('level') is not None for row in rows) else VOTE_FOOTNOTE_OFFLINE
    cursor += f.paragraph('vote.footnote', x, cursor, width, footnote, small, T.TEXT_FAINT)
    if not f.ui.get('service_signed_in', True):
        # The owner's order: a Loopcut account first. Votes count without one; launch news needs one.
        label = 'Sign in'
        button_w = round(f.measure('ui', small, label)) + f.px(20)
        cursor += f.px(10)
        note_h = f.paragraph('vote.signin.note', x, cursor + f.px(4), width - button_w - f.px(10), _INTEREST_SIGN_IN,
                             small, T.TEXT_MUTED)
        f.button('vote.signin', x + width - button_w, cursor, label, T.BUTTON_GHOST, T.TEXT, ('sign_in', 'loopcut'))
        cursor += max(note_h + f.px(4), f.px(24))
    content, view = cursor + 2 * pad, bottom - top
    max_scroll = max(0.0, content - view)
    scroll = min(max(f.ui.get('vote_scroll', 0.0), 0.0), max_scroll)
    shift = top + pad - round(scroll)
    for prim in f.prims[first_prim:]:
        prim['y'] += shift
    for hit in f.hits[first_hit:]:
        hit['y'] += shift
    f.prims[first_prim:] = [p for p in f.prims[first_prim:] if p['y'] + p['h'] > top and p['y'] < bottom]
    f.hits[first_hit:] = [h for h in f.hits[first_hit:] if h['y'] + h['h'] > top and h['y'] < bottom]
    return max_scroll


def _vote_footer(f, top) -> None:
    """In place of the composer on the vote view: the votes left, and the way back."""
    pad, small = f.px(T.PAD), f.px(T.FONT_SIZE_SMALL)
    f.rect('vote.footer.bg', 0, top, f.width, f.height - top, T.BG)
    f.rect('vote.footer.rule', 0, top, f.width, 1, T.CARD_BORDER)
    used = sum(1 for row in f.ui.get('coming_soon') or [] if row.get('voted'))
    mid = top + (f.height - top) // 2
    tokens_w = _vote_tokens(f, 'vote.token', pad, mid - f.px(9) // 2, used)
    left = VOTES_MAX - used
    notice = f.ui.get('vote_notice')
    status = notice or (f'{left} vote{"s" if left != 1 else ""} left' if left else 'All votes placed. Thank you!')
    text_x = pad + tokens_w + f.px(10)
    label = 'Back to chat'
    if f.width - pad - text_x - f.measure('ui', small, label) - f.px(30) < f.px(120):
        label = 'Back'  # A narrow panel keeps room for the status, which says more.
    button_w = round(f.measure('ui', small, label)) + f.px(20)
    room = f.width - pad - button_w - f.px(10) - text_x
    lines = wrap(status, 'ui', small, room, f.measure)
    if len(lines) > 2:
        lines = [lines[0], (lines[1][0], _ellipsize(f, lines[1][1].rstrip() + ' ' + lines[2][1], small, room))]
    line_h = f.line_height(small)
    for n, (_, line) in enumerate(lines):
        f.text(f'vote.footer.status.l{n}', text_x, mid - len(lines) * line_h // 2 + n * line_h, line.rstrip(), small,
               T.WARN if notice else T.TEXT_MUTED if left else T.OK)
    f.button('vote.back', f.width - pad - button_w, mid - f.px(12), label, T.BUTTON_GHOST, T.TEXT, ('history_close', None))


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


def usage_lines(usage: dict, budget: int, account: dict | None, notice: dict | None = None,
                now: float | None = None) -> list[str]:
    """The numbers behind the two rings, for their tooltip: the latest request against the
    budget it is compacted at, the conversation's total, and the account's windows."""
    context, total_in, total_out = usage.get("context", 0), usage.get("input", 0), usage.get("output", 0)
    share = f" ({round(100 * context / budget)}%)" if budget else ""
    lines = [f"Context: {_k(context)} of {_k(budget)} tokens{share} before compaction"]
    if total_in or total_out:
        lines.append(f"This conversation: {_k(total_in)} in, {_k(total_out)} out")
    refused = allowance.refused_windows(account, notice, now)
    for key, label in (("session", "Session"), ("week", "Week")):
        used = 1.0 if key in refused else allowance.used((account or {}).get(key), now)
        if used is not None:
            lines.append(f"{label} chat allowance: {round(used * 100)}% used, {round(max(0.0, 1 - used) * 100)}% left")
    return lines


def ring_shares(usage: dict, budget: int, account: dict | None, notice: dict | None = None,
                now: float | None = None) -> list[tuple[str, float, tuple]]:
    """(id, fill, color) per ring, left to right: context used, then what is left of the session
    window when the account meters one. A weekly refusal focuses the allowance ring on that window."""
    context = usage.get("context", 0)
    share = min(1.0, context / budget) if budget else 0.0
    rings = [("context", share, T.ERROR if share >= 1 else T.WARN if share >= 0.8 else T.ACCENT)]
    refused = allowance.refused_windows(account, notice, now)
    window = allowance.refused_window(account, notice, now) or next(iter(sorted(refused)), "session")
    used = 1.0 if refused else allowance.used((account or {}).get(window), now)
    if used is not None:
        left = max(0.0, min(1.0, 1 - used))
        rings.append((window, left, T.ERROR if left < 0.1 else T.WARN if left < 0.3 else T.OK))
    return rings


def _k(tokens: int) -> str:
    return f"{tokens / 1000:.1f}k" if tokens >= 1000 else str(tokens)


SHOW_USAGE_FROM = 0.7  # The notice names the fuller window once this much of it is used.


def usage_note(account: dict | None, notice: dict | None = None,
               now: float | None = None) -> tuple[str, float]:
    """(text, share) above the composer: the fuller of the account's two windows once it is worth a
    glance, else ("", share)."""
    best = ("", 0.0)
    refused = allowance.refused_windows(account, notice, now)
    for key in ("week", "session"):
        used = 1.0 if key in refused else allowance.used((account or {}).get(key), now)
        if isinstance(used, (int, float)) and used > best[1]:
            window = "5-hour window" if key == "session" else "week"
            best = (f"Chat {round(used * 100)}% used ({window})", float(used))
    return best if best[1] >= SHOW_USAGE_FROM else ("", best[1])


def _list_popup(f: Frame, id: str, x: int, bottom: int, width: int, rows: list, active: int,
                header: str | None = None, query: str = "", placeholder: str = "") -> None:
    """A card floating above `bottom`. With `header`, a title and a search field come first.
    Rows are (label, right label, action[, tip]); `active` is the keyboard-highlighted one. A row whose action
    is None is a faint caption for the rows under it, not a button."""
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
    for n, (label, right, action, *tip) in enumerate(rows):
        row_y = y + n * row_height
        if n == active:
            f.rect(f"{id}.row{n}.bg", x + pad, row_y, width - 2 * pad, row_height, T.BUTTON_GHOST, f.px(T.RADIUS_SMALL))
        right_width = round(f.measure("ui", small, right)) if right else 0
        caption = action is None
        font = small if caption else size
        line = wrap(label, "ui", font, width - right_width - f.px(40), f.measure)[0][1].rstrip()
        f.text(f"{id}.row{n}.name", x + f.px(12), row_y + (row_height - f.line_height(font)) // 2,
               line + ("…" if len(line) < len(label) else ""), font, T.TEXT_FAINT if caption else T.TEXT)
        if right:
            f.text(f"{id}.row{n}.kind", x + width - f.px(12) - right_width,
                   row_y + (row_height - f.line_height(small)) // 2, right, small, T.TEXT_FAINT)
        if not caption:
            f.hit(f"{id}.row{n}", x, row_y, width, row_height, action, tip=tip[0] if tip else "")


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


def _service_settings(f, x, bottom, width):
    if not f.ui.get("service_settings"):
        return
    policy = f.ui.get("service_policy") or {}
    # Short labels and right labels, so every row reads in full in a narrow panel (about 260 px); the longer
    # explanations are tooltips. The project's three choices sit under a caption, not a repeated prefix.
    signed_in = f.ui.get("service_signed_in")
    rows = [(("✓  " if policy.get(key) else "     ") + label, "Account" if signed_in else "Local", ("service_toggle", key),
             tip + (" Your Loopcut account's default." if signed_in else " This computer's default."))
            for key, label, tip in (("no_generation", "No generation", "Hide and refuse generative tools. Retopology stays."),
                                    ("private_mode", "Private mode", "Hide vendors that may train on your data (C or D)."))]
    rows.append(("This .blend file", "", None))
    for value, label, tip in (("inherit", "Use the default", "Follow the No generation setting above."),
                              ("on", "No generation", "Saved in this .blend: no generation in this file."),
                              ("off", "Allow generation", "Saved in this .blend. Account restrictions still apply.")):
        checked = "✓  " if f.ui.get("project_override", "inherit") == value else "     "
        rows.append((checked + label, "", ("project_generation", value), tip))
    if not f.ui.get("service_signed_in"):
        rows.append(("Sign in to Loopcut", "", ("sign_in", "loopcut")))
    if config.billing_open():  # The switch for every soft card (nudges.py); blocking cards are not tips.
        rows.append((("     " if f.ui.get("tips_off") else "✓  ") + "Tips about Loopcut tools", "",
                     ("tips_toggle", None)))
    rows.append(("All settings…", "", ("open_settings", None)))
    _list_popup(f, "service.settings", x, bottom, min(width, f.px(320)), rows, -1)


def model_button_label(model: str) -> str:
    return {"fast": "Fast", "pro": "Pro"}.get(model, model)


def _rings(f: Frame, session: dict, right: int, y: int, height: int) -> None:
    """The two donuts at the footer's right: the request's context against the budget it is
    compacted at, and what is left of the session's allowance. The tooltip has the numbers."""
    diameter, thickness, gap = f.px(14), f.px(3), f.px(6)
    usage, budget, account = session.get("usage") or {}, int(f.ui.get("context_budget") or 0), f.ui.get("account")
    notice, now = f.ui.get("allowance_notice"), f.ui.get("now")
    if f.ui.get("provider", "LOOPCUT") != "LOOPCUT":
        account, notice = None, None
    rings = ring_shares(usage, budget, account, notice, now)
    total = len(rings) * diameter + (len(rings) - 1) * gap
    left, top = right - total, y + (height - diameter) // 2
    for n, (id, share, color) in enumerate(rings):
        f.ring(f"input.ring.{id}", left + n * (diameter + gap), top, diameter, share, thickness, color)
    f.hit("input.rings", left - f.px(4), y, total + f.px(8), height, ("noop", None),
          tip="\n".join(usage_lines(usage, budget, account, notice, now)))


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
    notice_height = _input_notice(f, top + pad)
    cloud_height = _cloud_height(f)
    card_y = top + pad + notice_height
    card_h = f.height - top - 2 * pad - cloud_height - notice_height
    f.rect("input.card", x, card_y, width, card_h, T.INPUT_BG, f.px(T.RADIUS), 1,
           T.ACCENT if focused else T.INPUT_BORDER)
    credit_lines = _credit_lines(f, inner_w)
    # Only the context and typing rows focus the editor. Footer links have separate targets.
    focus_h = card_h - card_pad - f.px(26) - _credit_height(f, credit_lines)
    f.hit("input.card", x, card_y, width, focus_h, ("focus", None))

    text_y = card_y + card_pad
    _context_row(f, inner_x, text_y, inner_w)
    text_y += _context_row_height(f)
    chips = _chip_rows(f, "input.image", inner_x, text_y, inner_w, session)
    text_y += chips + (f.px(6) if chips else 0)
    # Keep the caret's row in view (the tail while typing at the end); only the rows in view are drawn.
    caret_row = textedit.locate(lines, session["cursor"])[0]
    first = textedit.first_row(session.get("input_first", len(lines)), caret_row, visible, len(lines))
    session["input_first"] = first
    shown = lines[first:first + visible]
    f.input = {"x": inner_x, "y": text_y, "size": size, "line_height": line_height, "lines": shown,
               "width": inner_w, "height": visible * line_height}
    anchor = session.get("anchor")
    if focused and anchor is not None and anchor != session["cursor"]:
        low, high = sorted((anchor, session["cursor"]))
        for n, (start, line) in enumerate(shown):
            a, b = max(low, start), min(high, start + len(line))
            # The second: a selected newline (a real one; a soft wrap's row end is the next row's start).
            if a < b or (a == b == start + len(line) and low <= a < high and session["input"][a:a + 1] == "\n"):
                left = inner_x + round(f.measure("ui", size, line[:a - start]))
                right = inner_x + round(f.measure("ui", size, line[:b - start])) + (f.px(4) if a == b else 0)
                f.rect(f"input.selection{n}", left, text_y + n * line_height + f.px(1), right - left,
                       line_height - f.px(2), T.SELECTION)
    if session["input"]:
        for n, (_, line) in enumerate(shown):
            f.text(f"input.l{n}", inner_x, text_y + n * line_height, line.rstrip(), size, T.TEXT)
    else:
        placeholder = "Say what to do with these" if session.get("attachments") else \
            "Add a note; Loopcut replies when it can" if busy else "Plan, build, fix anything in your scene"
        f.text("input.placeholder", inner_x, text_y, placeholder, size, T.TEXT_FAINT)

    if focused:
        row, column = textedit.locate(lines, session["cursor"])
        caret_x = inner_x + round(f.measure("ui", size, lines[row][1][:column]))
        if first <= row < first + visible:
            f.rect("input.caret", caret_x, text_y + (row - first) * line_height + f.px(2),
                   max(1, f.px(1.5)), line_height - f.px(4), T.ACCENT)

    # Footer: the model button, then the rings and the key hint at the right.
    button_h = f.px(22)
    button_y = card_y + card_h - card_pad - button_h
    text_y = button_y + (button_h - f.line_height(small)) // 2
    label, caret = f.ui.get("model_label") or model_button_label(model), "▾"
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
    if credit_lines:
        credit_y = button_y - _credit_height(f, credit_lines)
        for k, line in enumerate(credit_lines):
            f.text("input.credits" + (str(k) if k else ""), inner_x, credit_y + f.line_height(small) * k,
                   _ellipsize(f, line, small, inner_w), small, T.TEXT_MUTED)
        # The balance alone opens the account; neither the spend rows nor blank space are links.
        if f.ui.get("credits"):
            balance = _ellipsize(f, credit_lines[0], small, inner_w)
            f.hit("input.credits", inner_x, credit_y, round(f.measure("ui", small, balance)),
                  f.line_height(small), ("open_account", None))

    if f.ui.get("picker") is not None:
        _picker(f, x, card_y, width)
    elif focused:
        _mention_list(f, session, x, card_y, width)
    _model_menu(f, inner_x, button_y - f.px(2), width)
    _service_settings(f, inner_x, button_y - f.px(2), width)
    _cloud_buttons(f, card_y + card_h + f.px(6))


HEADER_BUTTONS = (  # Right to left: id, glyph, tooltip.
    ("header.close", "×", "Close panel"),
    ("header.settings", "⚙", "Settings"),
    ("header.history", "↺", "History"),
    ("header.new", "+", "New chat"),
    ("header.jobs", "▤", "Background jobs"),
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
    title_w = round(f.measure("ui", size, "Loopcut"))
    version = config.version_label()
    f.hit("header.title", title_x, 0, title_w, height, ("noop", None), tip=f"Loopcut {version}")
    crumb = {"history": "History", "jobs": "Jobs", "vote": "What's next"}.get(view)
    if crumb and f.width > f.px(370):
        f.text("header.crumb", title_x + round(f.measure("ui", size, "Loopcut")) + f.px(8), y, "›  " + crumb, size,
               T.TEXT_MUTED)
    # Icon buttons at the right: the editor's own header is hidden, so close lives here too.
    hover, box, gap = f.ui.get("hover"), f.px(24), f.px(2)
    right = f.width - pad
    for id, glyph, tip in HEADER_BUTTONS:
        bx, by = right - box, (height - box) // 2
        if id == "header.history":
            action, tip = (("history_close", None), "Back to chat") if view == "history" else (("history_open", None), tip)
        elif id == "header.jobs":
            action, tip = (("history_close", None), "Back to chat") if view == "jobs" else (("jobs_open", None), tip)
        else:
            action = {"header.close": ("close_panel", None), "header.settings": ("service_settings", None),
                      "header.new": ("new_chat", None)}[id]
        active = hover == id or (id == "header.history" and view == "history") or (id == "header.jobs" and view == "jobs")
        if active:
            f.rect(f"{id}.bg", bx, by, box, box, T.BUTTON_GHOST, f.px(T.RADIUS_SMALL))
        glyph_size = small if glyph in "⚙↺" else size  # The symbol glyphs come from a wider font.
        glyph_w = round(f.measure("ui", glyph_size, glyph))
        f.text(id, bx + (box - glyph_w) // 2, (height - f.line_height(glyph_size)) // 2, glyph, glyph_size,
               T.TEXT if active else T.TEXT_MUTED)
        f.hit(id, bx, 0, box, height, action, tip=tip)
        right = bx - gap
    version_x = title_x + title_w + f.px(8)
    if view == "chat" and version_x + f.measure("ui", small, version) <= right:
        f.text("header.version", version_x, (height - f.line_height(small)) // 2, version, small, T.TEXT_MUTED)
    return height


def _tooltip(f: Frame) -> None:
    """A bounded hover card that never covers the composer or its notices."""
    hover = f.ui.get("hover")
    hit = next((h for h in reversed(f.hits) if h["id"] == hover and h.get("tip")), None) if hover else None
    if hit is None:
        return
    small, pad_x, pad_y = f.px(T.FONT_SIZE_SMALL), f.px(8), f.px(5)
    lines = [line.rstrip() for _, line in wrap(hit["tip"], "ui", small,
                                              max(1, f.width - 2 * pad_x - f.px(8)), f.measure)]
    width = max(round(f.measure("ui", small, line)) for line in lines) + 2 * pad_x
    height = len(lines) * f.line_height(small) + 2 * pad_y
    x = max(f.px(4), min(f.width - width - f.px(4), hit["x"] + hit["w"] // 2 - width // 2))
    y = hit["y"] - height - f.px(6)
    if y < f.px(2):
        y = hit["y"] + hit["h"] + f.px(6)
    zone = next((p for p in f.prims if p["id"] == "input.zone"), None)
    if zone and y + height > zone["y"] and y < zone["y"] + zone["h"]:
        y = zone["y"] - height - f.px(6)
    if y < f.px(2) or y + height > f.height - f.px(2):
        return  # A short panel must keep typing usable even when a tooltip cannot fit.
    f.rect("tooltip.bg", x, y, width, height, T.CARD, f.px(T.RADIUS_SMALL), 1, T.INPUT_BORDER)
    for n, line in enumerate(lines):
        f.text(f"tooltip.l{n}", x + pad_x, y + pad_y + n * f.line_height(small), line, small, T.TEXT)


def _ellipsize(f: Frame, text: str, size: int, max_width: float) -> str:
    if f.measure("ui", size, text) <= max_width:
        return text
    while text and f.measure("ui", size, text + "…") > max_width:
        text = text[:-1]
    return text.rstrip() + "…"


def _update_banner_height(f: Frame) -> int:
    banner = f.ui.get("update")
    if not banner:
        return 0
    return f.px(T.BANNER_HEIGHT) + (f.line_height(f.px(T.FONT_SIZE_SMALL)) + f.px(4) if banner.get("detail") else 0)


def _update_banner(f: Frame, top: int) -> int:
    """The update notice under the header, with an optional second line for elevation."""
    banner = f.ui.get("update")
    if not banner:
        return 0
    height, pad, small = _update_banner_height(f), f.px(T.PAD), f.px(T.FONT_SIZE_SMALL)
    line_height = f.px(T.BANNER_HEIGHT)
    f.rect("update.bg", 0, top, f.width, height, T.CARD)
    f.rect("update.rule", 0, top + height - 1, f.width, 1, T.CARD_BORDER)
    right = f.width - pad
    if banner.get("dismiss"):
        box = f.px(24)
        bx, by = right - box, top + (line_height - box) // 2
        active = f.ui.get("hover") == "update.dismiss"
        if active:
            f.rect("update.dismiss.bg", bx, by, box, box, T.BUTTON_GHOST, f.px(T.RADIUS_SMALL))
        glyph_w = round(f.measure("ui", small, "×"))
        f.text("update.dismiss.glyph", bx + (box - glyph_w) // 2, top + (line_height - f.line_height(small)) // 2, "×",
               small, T.TEXT if active else T.TEXT_MUTED)
        f.hit("update.dismiss", bx, top, box, line_height, ("update_dismiss", None), tip="Hide update notice")
        right = bx - f.px(4)
    if banner.get("button"):
        label, kind = banner["button"]
        width = round(f.measure("ui", small, label)) + 2 * f.px(10)
        f.button("update.button", right - width, top + (line_height - f.px(24)) // 2, label, T.BUTTON, T.BUTTON_TEXT,
                 (kind, None))
        right -= width + f.px(8)
    text = _ellipsize(f, banner["text"], small, right - pad)
    f.text("update.text", pad, top + (line_height - f.line_height(small)) // 2, text, small, T.TEXT)
    if banner.get("detail"):
        detail = _ellipsize(f, banner["detail"], small, f.width - 2 * pad)
        f.text("update.detail", pad, top + line_height, detail, small, T.TEXT_MUTED)
    if banner.get("progress") is not None:
        bar = f.px(2)
        f.rect("update.progress.track", 0, top + height - 1 - bar, f.width, bar, T.CARD_BORDER)
        f.rect("update.progress", 0, top + height - 1 - bar, round(f.width * banner["progress"]), bar, T.ACCENT)
    return height


def _cloud_jobs(f: Frame, cursor):
    pad, small = f.px(T.PAD), f.px(T.FONT_SIZE_SMALL)
    for row in f.ui.get("cloud_jobs", []):
        id = row["id"]
        right = f.width - pad
        if row["cancelable"]:
            cancel_w = round(f.measure("ui", small, "Cancel")) + f.px(20)
            f.button(f"cloud.{id}.cancel", right - cancel_w, cursor, "Cancel", T.BUTTON_GHOST, T.TEXT,
                     ("cloud_job_cancel", id))
            right -= cancel_w + f.px(8)
        pct = f"{row['pct']}%"
        pct_w = round(f.measure("ui", small, pct))
        f.text(f"cloud.{id}.pct", right - pct_w, cursor + f.px(3), pct, small, T.TEXT_MUTED)
        title_w = right - pct_w - f.px(8) - pad
        f.text(f"cloud.{id}.title", pad, cursor + f.px(3), _ellipsize(f, row['title'], small, title_w), small, T.TEXT)
        cursor += f.px(26)
        cursor += f.paragraph(f"cloud.{id}.progress", pad, cursor, f.width - pad * 2, row["message"], small, T.TEXT_MUTED)
        if row.get("note"):  # "80 credits held. Cancel returns them." or why it can't be canceled now (C47).
            cursor += f.paragraph(f"cloud.{id}.note", pad, cursor, f.width - pad * 2, row["note"], small, T.TEXT_FAINT)
        cursor += pad
    return cursor


def _jobs(f: Frame, top, bottom):
    pad, small = f.px(T.PAD), f.px(T.FONT_SIZE_SMALL)
    cursor = _cloud_jobs(f, top + pad)
    for row in f.ui.get("local_processes", []):
        id = row["id"]
        minutes, seconds = divmod(int(row["elapsed"]), 60)
        elapsed = f"{minutes}m {seconds:02d}s"
        elapsed_w = round(f.measure("ui", small, elapsed))
        f.text(f"process.{id}.elapsed", f.width - pad - elapsed_w, cursor + f.px(3), elapsed, small, T.TEXT_MUTED)
        title_w = f.width - pad * 2 - elapsed_w - f.px(8)
        f.text(f"process.{id}.title", pad, cursor + f.px(3),
               _ellipsize(f, f"Running · {row['label']}", small, title_w), small, T.TEXT)
        cursor += f.px(26)
        f.text(f"process.{id}.log", pad, cursor,
               _ellipsize(f, Path(row["log"]).name, small, f.width - pad * 2), small, T.TEXT_MUTED)
        cursor += f.line_height(small) + pad
    error = f.ui.get("job_error")
    if error:
        cursor += f.paragraph("jobs.error", pad, cursor, f.width - 2 * pad, error, small, T.ERROR) + pad
    rows = f.ui.get("jobs", [])
    if not rows:
        if f.ui.get("cloud_jobs") or f.ui.get("local_processes"):
            return
        f.paragraph("jobs.empty", pad, cursor, f.width - 2 * pad,
                    "No background jobs yet. Ask Loopcut to render or export a scene.", small, T.TEXT_MUTED)
        return
    row_height = f.px(118)
    count = max(1, int((bottom - cursor - f.px(32)) // row_height))
    page = min(f.ui.get("jobs_page", 0), (len(rows) - 1) // count)
    for row in rows[page * count:(page + 1) * count]:
        id, progress = row["id"], row.get("progress", {})
        total = progress.get("total", len(row["frames"]))
        kind = row.get("purpose", row["format"]).replace("_", " ")
        unit = "views" if row.get("kind") == "inspection" else "frames"
        title = f"{row['state'].title()} · {progress.get('completed', 0)}/{total} {unit} · {kind}"
        f.text(f"job.{id}.title", pad, cursor, title, small, T.TEXT)
        detail = f"{row['camera']} · {row['width']}×{row['height']} · {row['samples']} samples · {row['budget_seconds']}s/attempt"
        f.text(f"job.{id}.detail", pad, cursor + f.px(22), _ellipsize(f, detail, small, f.width - pad * 2), small, T.TEXT_MUTED)
        note = row.get("error") or f"Saved revision {row.get('scene_revision', '')[:12]} · {id[:8]}"
        f.text(f"job.{id}.note", pad, cursor + f.px(42), _ellipsize(f, note, small, f.width - pad * 2), small, T.TEXT_FAINT)
        x = pad
        buttons = [("Cancel", "job_cancel")] if row["state"] in {"queued", "running", "cancelling"} else [("Resume", "job_resume")]
        if progress.get("files"):
            buttons.append(("Preview", "job_preview"))
        buttons.append(("Output", "job_open"))
        for label, action in buttons:
            x += f.button(f"job.{id}.{action}", x, cursor + f.px(65), label, T.BUTTON_GHOST, T.TEXT,
                          (action, id)) + f.px(6)
        cursor += row_height
    if page > 0:
        f.button("jobs.previous", pad, cursor, "Previous", T.BUTTON_GHOST, T.TEXT, ("jobs_page", page - 1))
    if (page + 1) * count < len(rows):
        f.button("jobs.next", pad + f.px(100), cursor, "Next", T.BUTTON_GHOST, T.TEXT, ("jobs_page", page + 1))


def _image_preview(f: Frame, id: str, path: str, title: str, x, y, width, thumbnail_size,
                   *, button=False) -> int:
    """Expand the existing cached thumbnail in its card; the chat and input remain usable."""
    title = " ".join(title.split())[:120] or "Scene preview"
    opened = f.ui.get("preview") == {"path": path, "title": title, "conversation": f.session.get("id")}
    cursor = y
    if opened:
        cursor += f.paragraph(id + ".title", x, cursor, width, title, f.px(T.FONT_SIZE_SMALL), T.TEXT) + f.px(6)
    image_w = width if opened else min(width * 2 // 3, f.px(thumbnail_size))
    image_h = min(width, f.px(520)) if opened else image_w
    action = ("preview_open", (path, title))
    f.prims.append({"t": "image", "id": id, "path": path, "x": x, "y": cursor, "w": image_w, "h": image_h})
    f.hit(id, x, cursor, image_w, image_h, action, "Close preview" if opened else "Enlarge preview")
    cursor += image_h + f.px(6)
    if button or opened:
        f.button(id + ".open", x, cursor, "Close preview" if opened else "Open preview", T.BUTTON_GHOST, T.TEXT, action)
        cursor += f.px(24)
    return cursor - y


def build(session: dict, width: int, height: int, scale: float, measure, model: str = "",
          checkpoints: dict | None = None, ui: dict | None = None) -> dict:
    ui = ui or {}
    view = ui.get("view", "chat")
    f = Frame(width, height, scale, measure, session, checkpoints, ui)
    f.rect("bg", 0, 0, width, height, T.BG)
    input_top = height - (f.px(VOTE_FOOTER_HEIGHT) if view == "vote" else input_height(f, session))
    header_bottom = f.px(T.HEADER_HEIGHT)
    top = header_bottom + _update_banner_height(f)
    if view == "jobs":
        max_scroll = 0.0
        _jobs(f, top, input_top)
    elif view == "history":
        max_scroll = 0.0
        _history(f, session, ui, top, input_top)
    elif view == "vote":
        max_scroll = _votes(f, session, top, input_top)
    else:
        max_scroll = _chat(f, session, top, input_top)
    _header(f, session, view)  # Header, banner and input go last so they paint over scrolled chat content.
    _update_banner(f, header_bottom)
    if view == "vote":
        _vote_footer(f, input_top)
    else:
        _input(f, session, input_top, model)
    _tooltip(f)
    return {"width": width, "height": height, "scale": scale, "max_scroll": max_scroll,
            "prims": f.prims, "hits": f.hits, "input": f.input}


def hit_at(display: dict, x: float, y_top: float) -> dict | None:
    for hit in reversed(display["hits"]):
        if hit["x"] <= x <= hit["x"] + hit["w"] and hit["y"] <= y_top <= hit["y"] + hit["h"]:
            return hit
    return None


def hit_test(display: dict, x: float, y_top: float):
    hit = hit_at(display, x, y_top)
    return hit["action"] if hit else None
