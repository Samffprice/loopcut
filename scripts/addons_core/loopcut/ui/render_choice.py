"""The cloud-or-laptop card (render_offer.py): a paid render's approval card when the same frames here would take
over 15 minutes. The recommended option first and emphasised, each with its time, its price, what it means for the
user's evening and its one button; the two stack, since side by side at 420 px every bullet wraps. Built from
layout.py's own primitives (f0's composed card, cloud-render-offer/proposed_cards.py)."""

from .. import render_offer
from . import theme as T
from .layout import _ellipsize, _mix, _wide_button, _waveform

PICK_BG = _mix(T.CARD, T.ACCENT, 0.07)  # The recommended option's fill.


def _badge(f, id, right, y, line_h, label, color) -> int:
    """A pill at the right edge. Returns its width."""
    small = f.px(T.FONT_SIZE_SMALL)
    bw, bh = round(f.measure("ui", small, label)) + f.px(12), f.px(18)
    bx, by = right - bw, y + (line_h - bh) // 2
    f.rect(id, bx, by, bw, bh, _mix(T.CARD, color, 0.18), bh // 2)
    f.text(id + ".label", bx + f.px(6), by + (bh - f.line_height(small)) // 2, label, small, color)
    return bw


def _wide_ghost(f, id, x, y, width, height, label, action) -> None:
    """_wide_button's secondary twin: same size, ghost fill."""
    size = f.px(T.FONT_SIZE)
    label = _ellipsize(f, label, size, width - f.px(16))
    f.rect(id, x, y, width, height, T.BUTTON_GHOST, f.px(T.RADIUS_SMALL), 1, T.INPUT_BORDER)
    f.text(id + ".label", x + (width - round(f.measure("ui", size, label))) // 2,
           y + (height - f.line_height(size)) // 2, label, size, T.TEXT)
    f.hit(id, x, y, width, height, action)


def _action(n, option):
    return ("paid_upgrade", n) if option["action"] == "upgrade" else ("render_choice", (n, option["action"]))


def _option(f, n, option, x, y, width) -> int:
    size, small, pad = f.px(T.FONT_SIZE), f.px(T.FONT_SIZE_SMALL), f.px(10)
    id = f"item{n}.choice.{option['key']}"
    primary = option.get("primary", False)
    mark = len(f.prims)
    ix, iw, cursor = x + pad, width - 2 * pad, y + pad
    row = f.line_height(size)
    badge_w = (_badge(f, id + ".badge", ix + iw, cursor, row, option["badge"], T.ACCENT) + f.px(8)
               if option.get("badge") else 0)
    f.text(id + ".title", ix, cursor, _ellipsize(f, option["title"], size, iw - badge_w), size, T.TEXT)
    cursor += row + f.px(2)
    for j, line in enumerate(option.get("facts", ())):
        cursor += f.paragraph(f"{id}.fact{j}", ix, cursor, iw, line, small, T.TEXT)
    if option.get("warn"):
        cursor += f.px(4) + f.paragraph(id + ".warn", ix, cursor + f.px(4), iw, option["warn"], small, T.WARN)
    if option.get("note"):
        cursor += f.px(2) + f.paragraph(id + ".note", ix, cursor + f.px(2), iw, option["note"], small, T.TEXT_MUTED)
    bullets = option.get("bullets", ())
    if bullets:
        cursor += f.px(6)
    glyph, glyph_color = ("✓", T.OK) if primary else ("•", T.TEXT_FAINT)
    for j, line in enumerate(bullets):
        f.text(f"{id}.bullet{j}.glyph", ix, cursor, glyph, small, glyph_color)
        cursor += f.paragraph(f"{id}.bullet{j}", ix + f.px(14), cursor, iw - f.px(14), line, small,
                              T.TEXT_MUTED) + f.px(2)
    button_h = f.px(30)
    if option.get("button") and primary:
        _wide_button(f, id + ".go", ix, cursor + f.px(8), iw, button_h, option["button"], _action(n, option),
                     option.get("short", ""))
    elif option.get("button"):
        _wide_ghost(f, id + ".go", ix, cursor + f.px(8), iw, button_h, option["button"], _action(n, option))
    cursor += (f.px(8) + button_h if option.get("button") else 0) + pad
    height = cursor - y
    f.prims.insert(mark, {"t": "rect", "id": id + ".box", "x": x, "y": y, "w": width, "h": height,
                          "color": PICK_BG if primary else T.BG, "radius": f.px(T.RADIUS_SMALL),
                          "border": f.px(1) if primary else 1, "border_color": T.ACCENT if primary else T.CARD_BORDER})
    return height


def card(f, n: int, item: dict, x, y, width) -> int:
    """The waiting card itself: the question, both answers, the balance. Esc and Enter work as on any paid card."""
    data = item["render_choice"]
    pad, size, small = f.px(T.CARD_PAD), f.px(T.FONT_SIZE), f.px(T.FONT_SIZE_SMALL)
    mark = len(f.prims)
    ix, iw, cursor = x + pad, width - 2 * pad, y + pad
    cursor += f.paragraph(f"item{n}.choice.title", ix, cursor, iw, render_offer.title(data), size, T.TEXT)
    cursor += f.paragraph(f"item{n}.choice.detail", ix, cursor, iw, render_offer.detail(data), small, T.TEXT_MUTED)
    cursor += f.px(10)
    balance = f.ui.get("credit_balance")  # The live balance, while the card waits; else the one it showed.
    options, footer = render_offer.options(data, balance)
    for k, option in enumerate(options):
        if k:
            cursor += f.px(8)
        cursor += _option(f, n, option, ix, cursor, iw)
    row = [h for h in f.hits if h["id"] == f"item{n}.choice.cloud.go"]  # Enter's button, when it has one.
    if footer:
        cursor += f.px(8) + f.paragraph(f"item{n}.choice.footer", ix, cursor + f.px(8), iw, footer, small,
                                        T.TEXT_MUTED)
    # Neither: Esc does the same. The paid call is declined and the model asks which the user wants.
    f.button(f"item{n}.choice.skip", ix, cursor + f.px(8), "Not now · esc", T.BUTTON_GHOST, T.TEXT_MUTED, ("reject", n))
    cursor += f.px(8) + f.px(24)
    row.append(f.hits[-1])
    for hit in row:  # The card's answer row for Enter and Esc (layout._chat): the cloud button and Not now.
        hit.update(answer=n, answer_row=len(row))
    height = cursor + pad - y
    f.prims.insert(mark, {"t": "rect", "id": f"item{n}.choice.card", "x": x, "y": y, "w": width, "h": height,
                          "color": T.CARD, "radius": f.px(T.RADIUS), "border": 1, "border_color": T.CARD_BORDER})
    return height


def local_progress(f, n: int, item: dict, x, y, width) -> int:
    """After "Render on my laptop": the local job's frames on its card, from the Jobs list (job_tools.refresh_ui,
    every second). 0 when the job is not listed, and the card keeps its outcome line."""
    row = next((r for r in f.ui.get("jobs", []) if r.get("id") == item.get("local_job")), None)
    if row is None:
        return 0
    small = f.px(T.FONT_SIZE_SMALL)
    progress = row.get("progress") or {}
    done, total = progress.get("completed", 0), progress.get("total", len(row.get("frames") or ()))
    running = row.get("state") in ("queued", "running")
    if running:
        line = f"On this laptop · {done:,} of {total:,} frames"
    elif row.get("state") == "complete":
        line = f"Rendered {total:,} frames on this laptop"
    else:
        line = f"{str(row.get('state', '')).capitalize()} at {done:,} of {total:,} frames on this laptop"
    height = f.paragraph(f"item{n}.tool.local", x, y, width, line, small, T.TEXT_MUTED)
    if running and total:
        height += f.px(4) + _waveform(f, f"item{n}.tool.local_bar", x, y + height + f.px(4), width, [],
                                      done / total, False) + f.px(4)
    return height
