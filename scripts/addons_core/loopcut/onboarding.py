"""First run of the Loopcut build, drawn in the splash screen: bring your Blender setup along,
create a Loopcut account, choose a model, see what is sent where, then try a first project.

The fork's splash menus (WM_MT_splash_quick_setup, WM_MT_splash in bl_operators/wm.py) call
draw_splash() and draw themselves when it returns False. Which of the two menus is on screen is
Blender's decision: the quick setup until preferences are saved, the normal splash after.

Steps: "import" (only if stock Blender settings were found), "setup" (Blender's own quick setup,
for a fresh start), "account" (required: "Continue without an account" shows only after a sign-in
failed or took too long), "how" (how Loopcut works: what the ChatGPT plan and the Loopcut account each
do, and with sales open what Free and Pro include), "connect" (one model), "privacy", "done" (Try it: three
first projects). Without an account the setup goes from "account" straight to "connect".
While a step is on screen the splash is modal (WindowManager.splash_modal, a fork addition): the
window behind is dimmed and only the splash's own buttons close it.

Progress is saved to onboarding.json in Loopcut's config folder, so someone who quits half way
resumes at that step on the next start, preferences or not. Finishing with an account and a model
marks it done; without an account it comes back. Installs from before this flow (preferences but no
progress file) that already have a model or an account are marked done without seeing it.
"""

import json
import os
import shutil
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import bpy

from . import account, blender_import, chatgpt_auth, config, settings
from .try_it import CARDS

# left_chatgpt: "Use your own API key" moved the provider off a ChatGPT plan; "Back to model choices" puts it back.
# no_account: "Continue without an account" was taken after a failed sign-in; the setup is not done this time.
# region: the splash's popup region when a step last drew, for the watchdog.
_state = {"step": None, "imported": None, "failed": [], "error": "", "own_key": False, "left_chatgpt": False,
          "no_account": False, "claimed": False, "waiting_since": 0.0, "credits_asked": 0.0, "region": None,
          "finished": False, "starting_since": 0.0, "drawn": None, "existing": False, "change_model": False}
_popups = set()  # Splash regions to repaint while a sign-in is in flight; see _refresh_popups.
_REFRESH_SECONDS = 0.5
_WATCH_SECONDS = 1.0
PROGRESS_FILE = "onboarding.json"
PROGRESS_VERSION = 1    # The flow that wrote the file: 1 is 0.2.6's.
PRIVACY_NOTICE = 1      # onboarding.json's privacy_seen: the version of what conversation logs send that was shown.
# Installs from before the setup (set up in 0.2.5 or older) go through it too, each step showing what they have.
# Without a Loopcut account they sign in like new installs (the owner, 2026-10-21); True lets them skip with "Not now".
EXISTING_MAY_SKIP_SIGN_IN = False
SIGN_IN_PATIENCE = 120  # Seconds a browser sign-in may take before "Continue without an account" shows.
CREDITS_PATIENCE = 15   # Seconds to wait for the account's credits before carrying on without them.
CLAIM_WINDOW = 2 * 24 * 3600  # Only a grant this recent is offered as "Claim": an older one may be spent.
NUMBERED = ("account", "how", "connect", "privacy", "done")  # "Step n of 5"; Blender's settings come first.
# Every operator a step draws. UILayout.operator() draws nothing for one that is not registered (an add-on
# half way through re-registering), so a step is only drawn, and only modal, once all of them exist.
OPERATORS = ("import_blender_settings", "onboarding_step", "onboarding_own_key", "onboarding_no_account",
             "onboarding_claim", "onboarding_try", "sign_in", "cancel_sign_in", "open_sign_in_page",
             "copy_sign_in_link", "chatgpt_sign_in", "chatgpt_cancel", "chatgpt_copy_link", "fetch_models",
             "onboarding_change_model", "chatgpt_check")
STARTING_PATIENCE = 15  # Seconds of "Loopcut is starting…" before it says to restart.
_registered = False  # Between register() and unregister(): the watchdog never opens a setup outside it.
MEDIA = Path(__file__).parent / "media"
# Our own generated concept art of a dwarf warrior (made for the 2026-10-21 image to 3D comparison): ours to
# ship, and no one's product photo or likeness.
SAMPLE_PHOTO = MEDIA / "dwarf-warrior.jpg"


def _source():
    return blender_import.find_source(blender_import.stock_root(), bpy.app.version[:2])


def _connected() -> bool:
    prefs = settings.preferences()
    if prefs is not None and prefs.provider == "CHATGPT" and not _on_chatgpt_plan():
        return False
    try:
        config.load()
    except config.ConfigError:
        return False
    return True


def hints_dismissed() -> bool:
    """Keep the first-message hints dismissed across files, chats and app launches."""
    from . import state
    prefs = settings.preferences()
    return bool(state.ui.get("hints_dismissed") or getattr(prefs, "first_message_sent", False))


def dismiss_hints() -> None:
    from . import state
    if hints_dismissed():
        return
    state.ui["hints_dismissed"] = True
    prefs = settings.preferences()
    if prefs is not None:
        prefs.first_message_sent = True
        try:
            bpy.ops.wm.save_userpref()
        except RuntimeError as ex:
            print(f"Loopcut: could not save first-message preference: {ex}")


# --- Progress on disk ---------------------------------------------------------------------------------------

def _progress_path() -> Path:
    from . import credentials
    return credentials.folder() / PROGRESS_FILE


def load_progress() -> dict | None:
    """{"version", "step", "done", "privacy_seen"}, or None when there is no file or it cannot be read."""
    try:
        data = json.loads(_progress_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("step") not in (*NUMBERED, "import", "setup"):
        return None
    return {"version": data.get("version"), "step": data["step"], "done": data.get("done") is True,
            "privacy_seen": data.get("privacy_seen") == PRIVACY_NOTICE, "existing": data.get("existing") is True}


def save_progress(step: str, done: bool = False, privacy_seen: bool | None = None, existing: bool | None = None) -> None:
    """privacy_seen, existing: None keeps what the file says."""
    path = _progress_path()
    saved = load_progress() or {}
    if privacy_seen is None:
        privacy_seen = bool(saved.get("privacy_seen"))
    if existing is None:
        existing = bool(saved.get("existing"))
    data = {"version": PROGRESS_VERSION, "step": step, "done": done}
    if privacy_seen:
        data["privacy_seen"] = PRIVACY_NOTICE
    if existing:
        data["existing"] = True
    try:
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(data), encoding="utf-8")
        os.replace(temporary, path)
    except OSError as ex:
        print(f"Loopcut: could not save the setup's progress: {ex}")


def privacy_seen() -> bool:
    """Whether this install's user has seen what conversation logs send (the setup's privacy step, which installs
    from before the setup go through too): until then nothing of a conversation is uploaded
    (session_logs.sharing_on). A background run never counts: nobody saw anything."""
    if getattr(bpy.app, "background", True):
        return False
    return bool((load_progress() or {}).get("privacy_seen"))


def mark_privacy_seen() -> None:
    """The user went past the screen that says what is shared (the privacy step). Never headless."""
    if getattr(bpy.app, "background", True):
        return
    progress = load_progress()
    if progress is None:
        save_progress(_state["step"] if _state["step"] in NUMBERED else "account", privacy_seen=True)
    else:
        save_progress(progress["step"], progress["done"], privacy_seen=True)


def is_existing() -> bool:
    """An install from before this setup (0.2.5 or older, set up already): its steps show what it has."""
    return bool(_state["existing"] or (load_progress() or {}).get("existing"))


def _resume_step(saved: str) -> str:
    """The saved step, or an earlier one whose requirement is missing now (a sign-out, a removed key)."""
    if not account.signed_in():
        return "account"
    if saved in ("privacy", "done") and not _connected():
        return "connect"
    return saved if saved in NUMBERED else "account"


def _start_step(first_run: bool) -> str | None:
    """Where the setup opens this session; None once it is done."""
    progress = load_progress()
    if settings.preferences() is None:
        return None  # A dev checkout not enabled as an add-on: nothing to set up.
    if progress is not None and progress["done"] and progress["privacy_seen"]:
        return None  # Through the setup, privacy step included.
    if first_run:  # No preferences: Blender's own settings first, wherever a saved setup stood.
        can_copy_prev = bpy.types.PREFERENCES_OT_copy_prev.poll(bpy.context)
        step = "import" if not can_copy_prev and _source() else "setup"
    else:
        # Set up before this setup existed (a model or an account, no progress), or done before the privacy step
        # was part of it: the whole setup too, from the account, each step showing what is already there.
        starting = (progress is None and (_connected() or account.signed_in())) or bool(progress and progress["done"])
        _state["existing"] = starting or bool(progress and progress["existing"])
        step = _resume_step("account" if starting or progress is None else progress["step"])
    save_progress(step, existing=True if _state["existing"] else None)
    if progress and not progress["done"]:
        _report("welcome", None, False, "abandoned_" + progress["step"])
    if _state["existing"]:  # Told apart from new installs in the funnel.
        _report("welcome", None, code="existing_resumed" if progress and not progress["done"] else "existing")
    else:
        _report("welcome", None, code="resumed" if progress else None)  # The first-run setup window opened.
    return step


def _begin(first_run: bool) -> None:
    """Once a session, at the first splash draw or the startup timer, whichever comes first."""
    _state["step"] = _start_step(first_run)
    _state["finished"] = _state["step"] is None


def _after_setup() -> str | None:
    if settings.preferences() is None:
        return None  # Not enabled as an add-on: nothing to set up here.
    progress = load_progress()
    return _resume_step(progress["step"] if progress and progress["step"] in NUMBERED else "account")


def _go(step: str) -> None:
    _state["step"] = step
    save_progress(step)


# --- The modal splash ---------------------------------------------------------------------------------------

def _forced() -> bool:
    """Factory-startup runs are the harness's and the eval drivers': no setup is pressed on them."""
    return not getattr(bpy.app, "factory_startup", False)


def _set_modal(context, modal: bool) -> None:
    wm = getattr(context, "window_manager", None)
    modal = modal and _forced()
    if wm is not None and hasattr(wm, "splash_modal") and wm.splash_modal != modal:
        wm.splash_modal = modal  # Writable while drawing: WindowManager is exempt from the draw-time rule.


def _splash_open() -> bool:
    region = _state["region"]
    return region is not None and _region_exists(region)


def _open_splash() -> None:
    windows = bpy.context.window_manager.windows
    if not windows:
        return
    try:
        with bpy.context.temp_override(window=windows[0]):
            bpy.ops.wm.splash("INVOKE_DEFAULT")
    except RuntimeError as ex:
        print(f"Loopcut: could not open the setup: {ex}")


def missing_operators() -> list[str]:
    return [name for name in OPERATORS if not hasattr(bpy.types, "LOOPCUT_OT_" + name)]


def _ready() -> bool:
    """Registered, with every operator the steps draw: not in the middle of an add-on reload."""
    return _registered and not missing_operators()


def _refresh_splash() -> None:
    region = _state["region"]
    if region is not None and _region_exists(region):
        region.tag_refresh_ui()
        region.tag_redraw()


def _signature() -> tuple:
    """What the open step shows, as data. Timers rebuild the splash only when it changed: a rebuild while the
    pointer rests on a button drops the button's hover state, so the next press only re-arms it. Rebuilding on
    every 1 s tick made people press every button twice."""
    from . import chatgpt_ui
    step, now = _state["step"], time.monotonic()
    status, chatgpt, signed_in = account.status(), chatgpt_ui.status(), account.signed_in()
    credits = _credits() if signed_in else None
    shown = (step, _ready(), _state["own_key"], _state["no_account"], _state["claimed"], _state["error"],
             _state["change_model"],
             tuple(_state["failed"]), status["status"], status.get("user_code"), status.get("error"), signed_in,
             chatgpt["status"], chatgpt.get("error"), _credits_pending(),
             bool(_state["waiting_since"]) and now - _state["waiting_since"] > SIGN_IN_PATIENCE,
             bool(_state["starting_since"]) and now - _state["starting_since"] >= STARTING_PATIENCE,
             None if credits is None else repr(sorted(credits.items())))
    if step == "connect":
        ready = chatgpt_ui.readiness()
        shown += (_on_chatgpt_plan(), ready["status"], ready["code"], ready.get("note", ""))
    elif step == "how":
        shown += (_account_plan(), _unit_prices(_descriptors()))
    elif step == "done":
        shown += (_connected(), tuple(card_cost(card, _descriptors()) for card in CARDS))
    return shown


def _changed() -> bool:
    return _state["drawn"] != _signature()


def _watch():
    """Timer while the setup is open: a splash that closed some other way (a file opened at startup, the
    splash switched off, a script) comes back, as Blender's own quick setup would on the next start; an open
    one is rebuilt when what it shows changed, so a layout made while something was missing heals. Nothing
    while add-ons reload."""
    if _state["step"] is None or _state["finished"]:
        return None
    if not _ready():
        return _WATCH_SECONDS
    if not _splash_open():
        _open_splash()
    elif not _state["own_key"] and _state["step"] != "setup" and _changed():  # Not under a text field or
        _refresh_splash()                                                     # Blender's own menus.
    return _WATCH_SECONDS


def _rebuild_when_ready():
    """Timer after a step was drawn without its operators: rebuild the splash once they exist (or once the
    wait for them runs out and the message changes)."""
    if _state["step"] is None or _state["finished"]:
        return None
    if _changed():
        _refresh_splash()
    return None if _ready() else _REFRESH_SECONDS


def start():
    """Startup timer: an unfinished setup opens even when Blender showed no splash, and stays open."""
    if bpy.app.background or not _forced():
        return None
    if _state["step"] is None and not _state["finished"]:
        user = Path(bpy.utils.resource_path("USER"))
        _begin(first_run=not (user / "config" / "userpref.blend").is_file())
    if _state["step"] is not None and not bpy.app.timers.is_registered(_watch):
        bpy.app.timers.register(_watch, first_interval=0.0, persistent=True)
    return None


def draw_splash(layout, context, first_run: bool) -> bool:
    """True when a step was drawn in place of the menu that called. Never traps: whatever goes wrong, the
    splash stops being modal and shows a way out."""
    try:
        drawn = _draw_splash(layout, context, first_run)
        _state["drawn"] = _signature() if _state["step"] is not None else None
        return drawn
    except Exception:
        traceback.print_exc()
        _report("welcome", None, False, "abandoned_" + (_state["step"] or "unknown"))
        _state.update(step=None, finished=True)  # This session only: the saved step comes back next start.
        if bpy.app.timers.is_registered(_watch):
            bpy.app.timers.unregister(_watch)
        try:
            _set_modal(context, False)
            _set_banner(context, True)
        finally:
            _draw_way_out(layout, "Something went wrong in Loopcut's setup. It will open again next time.")
        return True


def _draw_way_out(layout, text: str) -> None:
    column = layout.column()
    column.label(text=text, icon="ERROR")
    column.operator("wm.splash_close", text="Close")
    layout.separator(factor=2.0)


def _draw_splash(layout, context, first_run: bool) -> bool:
    if _state["step"] is None and not _state["finished"]:
        _begin(first_run)
    step = _state["step"]
    _set_modal(context, step is not None)
    _set_banner(context, step in (None, "import", "setup"))  # The image on the first screens only.
    if step is None:
        return False  # Done, or preferences existed and nothing is missing: the normal splash.
    _report_once("welcome", None, "at_" + step)
    region = getattr(context, "region_popup", None)  # Set when the splash is built, not on every refresh.
    if region is not None:
        _state["region"] = region
    if step != "setup" and missing_operators():  # Blender's quick setup draws only its own operators.
        _set_modal(context, False)
        _set_banner(context, True)
        if not _state["starting_since"]:
            _state["starting_since"] = time.monotonic()
        if time.monotonic() - _state["starting_since"] < STARTING_PATIENCE:
            _draw_way_out(layout, "Loopcut is starting…")
            if not bpy.app.timers.is_registered(_rebuild_when_ready):
                bpy.app.timers.register(_rebuild_when_ready, first_interval=_REFRESH_SECONDS, persistent=True)
        else:
            print(f"Loopcut: setup operators missing: {', '.join(missing_operators())}")
            _draw_way_out(layout, "Loopcut could not finish starting. Close this and restart Loopcut.")
        return True
    _state["starting_since"] = 0.0
    if first_run:
        if step not in ("import", "setup"):  # Preferences went missing half way: Blender's settings first.
            step = _state["step"] = "setup"
        if step != "import":
            return False  # Blender's quick setup, modal like the rest.
        _draw_import(layout)
        return True
    if step in ("import", "setup"):  # Preferences were just saved or imported.
        step = _after_setup()
        if step is None:
            _state.update(step=None, finished=True)
            _set_modal(context, False)
            _set_banner(context, True)
            return False
        _go(step)
        _set_banner(context, False)
    if step == "account":
        _draw_account(layout, context)
    elif step == "how":
        _draw_how(layout, context)
    elif step == "connect":
        _draw_connect(layout, context)
    elif step == "privacy":
        _draw_privacy(layout)
    else:
        _draw_try(layout, context)
    return True


# --- Drawing --------------------------------------------------------------------------------------------------
# One system for every screen, in loopcut.org's palette: the fork draws the modal splash with brand widget
# colors (wm_splash_screen.cc) and the text here sets its own (UILayout.label_styled). A compact header on the
# numbered steps (the mark, "Set up Loopcut", step dots); one headline and at most one muted line; groups in
# boxes padded 24 px (at 2x); rows of an outline icon, a label and at most one muted line; exactly one primary
# action (filled, full width, a verb, no icon); then one footer row: "← Back" at the left edge and the quiet
# actions (skip, cancel) at the right, muted. Teal only for links that open the browser (↗). Only the welcome
# screen shows the splash image (WindowManager.splash_banner).

PRIMARY_SCALE = 1.6
OPTION_SCALE = 1.3     # The step-2 and try-it card buttons: 52 px at 2x.
SECTION_GAP = 1.0      # Separator factor between a screen's sections.
BOX_PADDING = 12       # Pixels at UI scale 1 inside every box (UILayout.box_padded).
CARD_PADDING = 9       # Inside the try-it cards: with the picture's own inset, BOX_PADDING like every box.
PREVIEW_INSET = 3      # template_icon's inset around its picture (PREVIEW_PAD), at UI scale 1.
CARD_GAP = 0.67        # Separator factor between stacked cards: 16 px at 2x with the column's own spacing.
THUMBNAIL_UNITS = 4.0  # The try-it picture's cell, as tall as the card's text column.
FOOTER_SCALE = 0.85    # The footer row hugs its text: the bottom margin then matches the top one.


def _rgb(value: int) -> tuple[float, float, float]:
    return ((value >> 16) / 255, ((value >> 8) & 255) / 255, (value & 255) / 255)


FOREGROUND = _rgb(0xF2EFE9)
MUTED = _rgb(0x9C9CA3)
PENDING = _rgb(0x38383A)  # White at 16% on the splash's background.
ORANGE = _rgb(0xDF8B37)
TEAL = _rgb(0x51A7AE)
SUCCESS = _rgb(0x6FBF8A)
DANGER = _rgb(0xD4574A)

# One outline icon set (Lucide, ISC: THIRD-PARTY-NOTICES.txt), drawn as images in the palette's colors; the Blender
# icon is the fallback outside the Loopcut build.
ICONS = {"search": ("search-fg", "VIEWZOOM"), "credits": ("sparkles-fg", "SOLO_ON"),
         "chat": ("message-circle-fg", "WORDWRAP_ON"), "wait": ("loader-circle-muted", "TIME"),
         "error": ("circle-alert-danger", "ERROR"), "done": ("check-success", "CHECKMARK"),
         "person": ("user-fg", "USER"), "key": ("key-round-fg", "KEYINGSET"), "cloud": ("cloud-fg", "WORLD"),
         "computer": ("laptop-fg", "DESKTOP"), "upgrade": ("circle-arrow-up-fg", "TRIA_UP"),
         "thinking": ("brain-fg", "LIGHT"), "yes": ("check-fg", "CHECKMARK"), "no": ("minus-muted", "REMOVE"),
         "help": ("circle-help-fg", "QUESTION")}


def _set_banner(context, banner: bool) -> None:
    wm = getattr(context, "window_manager", None)
    if wm is not None and hasattr(wm, "splash_banner") and wm.splash_banner != banner:
        wm.splash_banner = banner


def _styled(layout) -> bool:
    return hasattr(layout, "label_styled")


def _strikes() -> bool:
    """Whether this build's label_styled can strike its text (the fork from 0.2.8). Asked, never assumed: an
    unknown keyword would break the modal setup's drawing."""
    try:
        return "strike" in bpy.types.UILayout.bl_rna.functions["label_styled"].parameters
    except (AttributeError, KeyError, TypeError):
        return False


def _text(column, text: str, color=FOREGROUND, scale: float = 1.0, bold: bool = False, center: bool = False,
          tight: bool = False, code: bool = False, strike: bool = False) -> None:
    """Text in a palette color (the fork's UILayout.label_styled); a plain label elsewhere. Lines split on "\\n";
    tight leaves only the text's own height, for a caption set close under its label; code sets hyphens tight;
    strike draws a line through it where the build can."""
    if _styled(column):
        row = column.row()
        row.alignment = "CENTER" if center else "LEFT"
        extra = {"strike": True} if strike and _strikes() else {}
        row.label_styled(text=text, scale=scale, bold=bold, color=color, tight=tight, center=center, code=code,
                         **extra)
        return
    for line in text.split("\n"):
        row = column.row()
        row.alignment = "CENTER" if center else "LEFT"
        row.label(text=line)


def _icon(name: str) -> dict:
    """The keyword arguments that draw one of ICONS on a label."""
    file, fallback = ICONS[name]
    value = _thumbnail(f"icons/{file}.png")
    return {"icon_value": value} if value else {"icon": fallback}


def _mark() -> dict:
    value = account.mark_icon()
    return {"icon_value": value} if value else {"icon": "LOOPCUT"}


def _row_mark() -> dict:
    """The mark as a row icon: the size of the outline icons' strokes and, like them, at the image's left edge
    (make_setup_icons.py), so its title starts on the text column with the others."""
    try:
        collection = _previews()
        preview = collection.get("row-mark")
        if preview is None:
            import numpy as np
            from .ui import brand
            canvas = np.zeros((64, 64, 4), dtype=np.float32)
            canvas[8:56, :48] = brand.square(48, margin=0.0)
            preview = collection.new("row-mark")
            preview.icon_size = (64, 64)
            preview.icon_pixels_float = canvas[::-1].ravel().tolist()  # Previews start at the bottom row.
    except Exception as ex:  # The header's mark instead.
        print(f"Loopcut: could not draw the mark: {ex!r}")
        return _mark()
    return {"icon_value": preview.icon_id}


def _page(layout, step: str | None = None):
    """The screen's column, the splash's full width inside its own margins, under the compact header."""
    column = layout.column()
    if step in NUMBERED:
        header = column.row()
        header.label(text="Set up Loopcut", **_mark())
        dots = header.row(align=True)
        dots.alignment = "RIGHT"
        index = NUMBERED.index(step)
        if _styled(dots):  # Done muted, current orange, the rest faint.
            for n in range(len(NUMBERED)):
                dots.label_styled(text="●", color=ORANGE if n == index else MUTED if n < index else PENDING)
        else:
            dots.label(text=_dots(step))
        column.separator(factor=SECTION_GAP)
    return column


def _dots(step: str) -> str:
    index = NUMBERED.index(step)
    return " ".join("●" if n <= index else "○" for n in range(len(NUMBERED)))


def _headline(column, title: str, line: str = "") -> None:
    _text(column, title, scale=1.7, bold=True)
    if line:
        _text(column, line, MUTED)
    column.separator(factor=SECTION_GAP)


def _item(column, icon: str, label: str, detail: str = "", color=FOREGROUND, detail_color=MUTED) -> None:
    """A row: an outline icon, a short label, and at most one muted line set close under the label."""
    block = column.column(align=True)
    _text(_icon_row(block, _icon(icon)), label, color)
    if detail:
        _text(_icon_row(block), detail, detail_color, tight=True)


def _icon_row(layout, icon: dict | None = None, center: bool = False):
    """A row that starts with the icon column (an icon, or nothing under one) and the gap after it: what is added
    to it next starts on the setup's one text column, 16 px (at 2x) right of every icon and checkbox."""
    row = layout.row(align=True)
    row.alignment = "CENTER" if center else "LEFT"
    cell = row.row()
    cell.ui_units_x = ICON_UNITS
    cell.label(text="", **(icon or {}))
    row.separator(factor=ICON_GAP)
    return row


ICON_UNITS = 0.85  # The icon column: an icon's strokes, or a checkbox, from the content's left edge.
ICON_GAP = 0.667   # Separator factor after it: 8 px at 2x, so the text is 16 px from the icon's right edge.
ITEM_GAP = 0.7     # Separator factor between rows that have a caption: 24 px at 2x.


def _group(column, padding: float = BOX_PADDING):
    """A box with the same padding on every side, everywhere."""
    if hasattr(column, "box_padded"):
        return column.box_padded(padding=padding).column()
    return column.box().column()


def _primary(column, operator: str, text: str, enabled: bool = True, **options):
    """The screen's one primary action: filled, full width, a verb."""
    column.separator(factor=SECTION_GAP)
    sub = column.column()
    sub.enabled = enabled
    sub.scale_y = PRIMARY_SCALE
    return sub.operator(operator, text=text, depress=True, **options)


def _wide(column, operator: str, text: str, enabled: bool = True, **options):
    """A secondary action in the primary's place: raised, not filled."""
    column.separator(factor=SECTION_GAP)
    sub = column.column()
    sub.enabled = enabled
    sub.scale_y = PRIMARY_SCALE
    return sub.operator(operator, text=text, **options)


def _link(layout, operator: str, text: str, color=MUTED, hover: bool = True):
    """A quiet action drawn as text (the fork's UILayout.operator_link), aligned to its layout's side. Without
    hover it shows nothing while hovered: a label that acts (a checkbox's)."""
    if hasattr(layout, "operator_link"):
        return layout.operator_link(operator=operator, text=text, color=color, hover=hover)
    return layout.operator(operator, text=text, emboss=False)


def _web_link(layout, text: str, url: str) -> None:
    """A teal link that opens the browser (↗)."""
    _link(layout, "wm.url_open", text, TEAL).url = url


def _footer(column):
    """The footer row: (left, for "← Back"; right, for the quiet actions and ↗ links). The screen's last row."""
    column.separator(factor=SECTION_GAP)
    row = column.row()
    row.scale_y = FOOTER_SCALE
    left = row.row()
    left.alignment = "LEFT"
    right = row.row()
    right.alignment = "RIGHT"
    return left, right


def _back(left, step: str = "", operator: str = "loopcut.onboarding_step"):
    """"← Back", at the left content edge of every step that has one."""
    props = _link(left, operator, "← Back")
    if step:
        props.step, props.save = step, False
    return props


def _step_link(layout, text: str, step: str) -> None:
    props = _link(layout, "loopcut.onboarding_step", text)
    props.step, props.save = step, False


def _switch(layout, prefs, name: str, text: str) -> None:
    """A checkbox in the icon column and its label on the text column, as the rows above it; the label toggles
    it too."""
    row = layout.row(align=True)
    row.alignment = "LEFT"
    cell = row.row()
    cell.ui_units_x = ICON_UNITS
    cell.prop(prefs, name, text="")
    row.separator(factor=ICON_GAP)
    label = row.row()
    label.alignment = "LEFT"
    _link(label, "wm.context_toggle", text, FOREGROUND, hover=False).data_path = \
        f'preferences.addons["{settings.PACKAGE}"].preferences.{name}'


def _waiting(column, text: str = "Waiting for your browser…", center: bool = True) -> None:
    """The one waiting pattern: a spinner and a muted line."""
    _text(_icon_row(column, _icon("wait"), center), text, MUTED)


def _sentence(text: str, limit: int = 70) -> str:
    """A message as a sentence that fits a row: shortened with "…", else ending with a full stop."""
    text = " ".join(text.split())
    if len(text) > limit:
        return text[:limit - 1].rstrip() + "…"
    return text if text.endswith((".", "!", "?", "…")) else text + "."


def _end(layout) -> None:
    """The welcome screen's bottom margin: its image spans the splash, so the splash adds none of its own."""
    layout.separator(factor=BANNER_BOTTOM)


BANNER_BOTTOM = 3.0  # Separator factor: with the column's spacing, the side margins' 52 px at 2x.


def _draw_import(layout) -> None:
    version, source = _source() or ((0, 0), None)
    column = _page(layout)
    _headline(column, "Welcome to Loopcut", "Bring your Blender setup with you?")
    if source is None:  # Removed since the step was chosen.
        props = _primary(column, "loopcut.onboarding_step", "Continue")
        props.step, props.save = "setup", False
        _end(layout)
        return
    _primary(column, "loopcut.import_blender_settings", "Import Blender {:d}.{:d} settings".format(*version))
    count = blender_import.addon_count(source)
    copies = f"Copies preferences, keymap, themes and {count} add-ons." if count else \
        "Copies preferences, keymap and themes."
    column.separator(factor=0.5)
    _text(column, f"{copies} Blender itself isn't changed.", MUTED, center=True)
    if _state["error"]:
        _item(column, "error", _state["error"], color=DANGER)
    _, right = _footer(column)
    _step_link(right, "Start fresh", "setup")
    _end(layout)


def _draw_connect(layout, context) -> None:
    prefs = settings.preferences()
    if not _state["own_key"]:
        _draw_sign_in(layout, context)
        return
    column = _page(layout, "connect")
    _headline(column, "Add your own API key", "Pick the provider, paste the key, choose a model.")
    group = _group(column)
    group.use_property_split = True
    group.use_property_decorate = False
    group.prop(prefs, "provider")
    if prefs.provider == "LOOPCUT":
        account.draw(group.column(align=True))
    elif prefs.provider == "CHATGPT":
        from . import chatgpt_ui
        chatgpt_ui.draw(group.column(align=True), prefs)
    else:
        if prefs.provider == "CUSTOM":
            group.prop(prefs, "base_url")
        if not prefs.base_url.startswith("http://127.0.0.1"):
            settings.draw_key(group, prefs)
        row = group.row(align=True)
        row.prop(prefs, "model")
        row.operator("loopcut.fetch_models", text="", icon="FILE_REFRESH")
        if settings.has_models():
            row.menu("LOOPCUT_MT_models", text="", icon="DOWNARROW_HLT")
    if not _connected():
        try:
            config.load()
        except config.ConfigError as ex:
            column.separator()
            _item(column, "error", str(ex)[:70], "Fix the fields above to continue.", color=DANGER)
    props = _primary(column, "loopcut.onboarding_step", "Continue", enabled=_connected())
    props.step, props.save = "privacy", True
    left, _ = _footer(column)
    _back(left, operator="loopcut.onboarding_own_key").enable = False


def _centered(layout):
    row = layout.row()
    row.alignment = "CENTER"
    return row


def _region_exists(region) -> bool:
    """Popup regions are freed when the popup closes and bpy does not invalidate the handle; the
    context override is the one safe probe (the same workaround Blender's extension notifier uses)."""
    try:
        with bpy.context.temp_override(region=region):
            return True
    except TypeError:
        return False


def _refresh_popups():
    """Timer: a popup only repaints on input, so while the sign-in thread works, rebuild it here when what it
    shows changed (never on every tick: see _signature)."""
    changed = _changed()
    for region in list(_popups):
        if not _region_exists(region):
            _popups.discard(region)
            continue
        if changed:
            region.tag_refresh_ui()
            region.tag_redraw()
    from . import chatgpt_ui
    busy = (account.status()["status"] in ("starting", "waiting")
            or chatgpt_ui.status()["status"] == "waiting" or _credits_pending())
    return _REFRESH_SECONDS if busy and _popups else None


def _watch_popup(context) -> None:
    region = getattr(context, "region_popup", None)
    if region is not None:
        _popups.add(region)
    if not bpy.app.timers.is_registered(_refresh_popups):
        bpy.app.timers.register(_refresh_popups, first_interval=_REFRESH_SECONDS, persistent=True)


def _on_chatgpt_plan() -> bool:
    prefs = settings.preferences()
    info = chatgpt_auth.status()
    return prefs is not None and info.get("signed_in") and info.get("plan_usage")


ACCOUNT_UNLOCKS = (("search", "Search asset libraries from chat"),
                   ("credits", "Starter credits for 3D, motion and sound"),
                   ("chat", "Chat with Loopcut Fast"))
ACCOUNT_UNLOCKS_NO_SALES = (ACCOUNT_UNLOCKS[0], ("credits", "Cloud tools as they open"), ACCOUNT_UNLOCKS[2])
CONNECT_LINE = "Uses your ChatGPT Plus or Pro plan. Cloud tools still use credits."
CONNECT_LINE_NO_SALES = "Uses your ChatGPT Plus or Pro plan."

def _credits() -> dict | None:
    """The signed-in account's credits from a manifest fetched this session; None until one arrives."""
    from . import cloud
    runtime = cloud._runtime
    if runtime is None or not runtime.signed_in or not runtime.refreshed_at:
        return None
    with runtime.lock:
        credits = (runtime.manifest.get("account") or {}).get("credits")
    return dict(credits) if isinstance(credits, dict) else {}


def _ask_for_credits() -> None:
    """A fresh manifest for the account just signed in (or resumed with), at most every CREDITS_PATIENCE."""
    if time.monotonic() - _state["credits_asked"] < CREDITS_PATIENCE:
        return
    _state["credits_asked"] = time.monotonic()
    try:
        from . import cloud
        cloud.refresh_async()
    except Exception as ex:  # The step carries on without credits; the panel fetches them later.
        print(f"Loopcut: could not fetch the account's credits: {ex!r}")


def _credits_pending() -> bool:
    return (account.signed_in() and _credits() is None
            and time.monotonic() - _state["credits_asked"] < CREDITS_PATIENCE)


def _claimable(credits: dict, now: float | None = None) -> int | None:
    """The trial grant to show as "Claim": reported by the server, recent and unspent; else None. The server
    grants it at sign-up; the button only shows it was received."""
    trial = credits.get("trial")
    if not config.billing_open() or not isinstance(trial, dict):
        return None
    amount = trial.get("amount")
    if type(amount) not in (int, float) or amount <= 0:
        return None
    try:
        granted = datetime.fromisoformat(str(trial.get("granted_at")).replace("Z", "+00:00"))
        if granted.tzinfo is None:
            granted = granted.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    if (now or time.time()) - granted.timestamp() > CLAIM_WINDOW:
        return None
    balance = credits.get("balance")
    if type(balance) in (int, float) and balance < amount:
        return None  # Some of it is spent already: this is not a new account's first look.
    return amount


def _credit_rows(credits: dict) -> tuple[str, str]:
    """Honest copy when there is nothing to claim: (label, line)."""
    from . import nudges
    balance = credits.get("balance")
    if not config.billing_open():
        return "Your free account is ready", "Asset search and Loopcut Fast are free."
    if type(balance) in (int, float) and balance > 0:
        return f"You have {balance:,g} credits ({nudges.dollars(balance)})", "They pay for cloud tools."
    return "No free credits on this account right now", "Asset search and Loopcut Fast are free."


def _draw_account(layout, context) -> None:
    """Required: the cloud tools, credits and Loopcut's own models hang off the account."""
    if account.signed_in():
        _draw_signed_in(layout, context)
        return
    status = account.status()
    column = _page(layout, "account")
    _report_once("loopcut_account", None, "shown")
    if status["status"] in ("starting", "waiting"):
        _watch_popup(context)
        if not _state["waiting_since"]:
            _state["waiting_since"] = time.monotonic()
    else:
        _state["waiting_since"] = 0.0
    slow = _state["waiting_since"] and time.monotonic() - _state["waiting_since"] > SIGN_IN_PATIENCE
    _headline(column, "Create your free account", "Takes a minute in your browser.")
    # Every state's box is three rows tall, so the button below it never moves.
    group = _group(column)
    if status["status"] == "waiting":
        _text(group, "Approve this code in your browser", MUTED, center=True)
        _text(group, status["user_code"], scale=2.0, bold=True, center=True, code=True)
    elif status["status"] == "starting":
        group.label(text="")
        _waiting(group, "Contacting Loopcut…")
        group.label(text="")
    elif status["status"] == "error":
        group.separator(factor=SPLIT_TOP)
        _item(group, "error", "Couldn't reach Loopcut", "Check your connection and try again.", color=DANGER)
        group.separator(factor=SPLIT_BOTTOM)
    else:
        for icon, line in ACCOUNT_UNLOCKS if config.billing_open() else ACCOUNT_UNLOCKS_NO_SALES:
            _item(group, icon, line)
    if status["status"] in ("starting", "waiting"):
        column.separator(factor=0.5)
        _waiting(column)
        _wide(column, "loopcut.copy_sign_in_link", "Copy link", enabled=status["status"] == "waiting")
    elif status["status"] == "error":
        _primary(column, "loopcut.sign_in", "Try again")
    else:
        _primary(column, "loopcut.sign_in", "Create account")
        column.separator(factor=0.5)
        _text(column, "Already have an account? The same button signs you in.", MUTED, center=True)
    if slow:
        column.separator(factor=0.5)
        _text(column, "Taking a while? You can sign in next time.", MUTED, center=True)
    if EXISTING_MAY_SKIP_SIGN_IN and is_existing() and status["status"] not in ("starting", "waiting", "error"):
        _, right = _footer(column)
        _link(right, "loopcut.onboarding_no_account", "Not now")
    if status["status"] in ("starting", "waiting", "error"):  # Else no footer: the line above ends the screen.
        _, right = _footer(column)
        if slow or status["status"] == "error":
            _link(right, "loopcut.onboarding_no_account", "Continue without an account")
        if status["status"] != "error":
            _link(right, "loopcut.cancel_sign_in", "Cancel")


# Separator factors around the failed state's row and caption: centred, in a box as tall as the three rows of the
# other states (measured at 2x: the button below stays where it is).
SPLIT_TOP, SPLIT_BOTTOM = 1.92, 1.42


def _draw_signed_in(layout, context) -> None:
    """Signed in: the trial credits the server granted, as a Claim the user presses, then Continue. The
    credits box keeps one shape in every state, so the primary action stays where it is."""
    from . import nudges
    column = _page(layout, "account")
    _headline(column, "You're signed in", account.account_line())
    credits = _credits()
    pending = credits is None
    if pending:
        _ask_for_credits()
        pending = _credits_pending()
        if pending:
            _watch_popup(context)
        credits = {}
    amount = None if pending else _claimable(credits)
    group = _group(column)
    if pending:
        _text(group, "Checking your free credits…", scale=1.5, bold=True, center=True)
        _text(group, "This takes a moment.", MUTED, center=True)
    elif amount and not _state["claimed"]:
        _text(group, f"{amount:g} free credits", scale=1.5, bold=True, center=True)
        _text(group, f"{nudges.dollars(amount)} for cloud tools", MUTED, center=True)
    elif amount:
        _text(_icon_row(group, _icon("done"), center=True), f"{amount:g} credits added", SUCCESS, scale=1.5,
              bold=True)
        _text(group, f"{nudges.dollars(amount)} for cloud tools", MUTED, center=True)
    else:
        label, line = _credit_rows(credits)
        _text(group, label, scale=1.2, bold=True, center=True)
        _text(group, line, MUTED, center=True)
    prefs = settings.preferences()
    if prefs is None or prefs.provider == "LOOPCUT":
        kind = "chat" if _account_plan() in ("pro", "max") else "free"
        _text(column, "Credits pay for cloud tools; Loopcut chat has a separate\n"
                      f"{kind} allowance that refreshes every 5 hours.", MUTED, center=True)
    if amount and not _state["claimed"]:
        _primary(column, "loopcut.onboarding_claim", "Claim credits")
        _, right = _footer(column)
        _step_link(right, "Skip", "how")
    else:  # Never waits on the credits: they show again in the panel.
        props = _primary(column, "loopcut.onboarding_step", "Continue")
        props.step, props.save = "how", False
        if config.billing_open():
            _, right = _footer(column)
            _web_link(right, "See plans ↗", config.pricing_url("onboarding", "pro"))


# --- How Loopcut works ---------------------------------------------------------------------------------------
# Between the account and the model choice: what each part does, then (sales open) what Free and Pro include. It
# explains; the next step chooses.

# The plans as the plans and plan_credit_grants tables set them (loopcut.org/pricing), on 2026-10-21. The manifest
# names the account's own plan but carries no catalogue: when a price or a monthly grant changes, change it here.
PLANS = {"free": {"credits": 100, "price": 0}, "pro": {"credits": 2200, "price": 20},
         "max": {"credits": 6600, "price": 60}}
# (what, Free, Pro, Max), only where the plans differ: True a check, False a dash, text as it is. The second
# column is Pro, or Max for a Max account; the monthly credits are in the bars under the table.
PLAN_ROWS = (("Loopcut Fast", "Weekly limit", "Higher weekly limit", "Highest weekly limit"),
             ("Loopcut Pro model", False, True, True),
             ("Credit top-ups", False, True, True))
# The two parts: (icon, who, what it is). Each shows what it does as a row of icons over captions, then two lines.
THINKING = ("thinking", "Your ChatGPT plan", "The thinking")
CLOUD = ("mark", "Your Loopcut account", "The cloud tools")
THINKING_STEPS = (("list-checks-fg", "Plan"), ("hammer-fg", "Build"), ("scan-eye-fg", "Check"), ("wrench-fg", "Fix"))
TOOLS = (("box-fg", "3D model"), ("person-standing-fg", "Motion"), ("mic-fg", "Voice"),
         ("audio-lines-fg", "Sound"), ("aperture-fg", "Render"))
THINKING_LINES = ("Runs on your paid ChatGPT plan", "Never uses Loopcut credits")
THINKING_LINES_NO_SALES = ("Runs on your paid ChatGPT plan", "Writes and runs each Blender step")
CLOUD_LINES = ("Asset search is free", "Others use credits, price shown first")
CLOUD_LINES_NO_SALES = ("Asset search included", "More coming")
NO_PLAN = ("No ChatGPT plan? Everything still works.", "Loopcut Fast does the thinking instead, or add your own API key.")
NO_PLAN_NO_SALES = (NO_PLAN[0], "Loopcut models do the thinking instead, or add your own API key.")
JOIN_UNITS = 1.0       # The "+" between the two parts, in units.
JOIN_OFFSET = 8.9      # Separator factor above the "+": its middle on the parts' middle (measured at 2x).
CAPTION = 0.85         # The icon rows' captions, of the body text's size: five fit one part's width.
PLAN_LABELS = 0.262    # The comparison's first column, of the box's width; the bars' captions start where it ends,
#                        with room for the longest ("6,600 credits: 82 models from photos, or 55 min of motion").
BAR_NAME = 24          # The plan's name before its bar, in pixels at UI scale 1 ("Free" and its image margin).
BAR_GAP = 3
BAR_SCALE = 0.4        # The credit bars' height, of a row.
BAR_OFFSET = 0.5       # Separator factor above a bar: its middle on its row's middle.
BAR_LEAST = 0.1        # The shortest fill, of the track: about 12 px still shows at 2x once the bar's 2 px inset
#                        each side is taken off (a plan's few credits stay a visible bar).


def _content_px() -> float:
    """The splash's content width in pixels at UI scale 1: 45 widget font sizes (wm_splash_screen.cc) less its
    26 px margins."""
    try:
        points = bpy.context.preferences.ui_styles[0].widget.points
    except (AttributeError, IndexError):
        points = 11
    return points * 45 - 2 * 26


def _account_plan() -> str:
    """The signed-in account's plan from this session's manifest ("free", "pro", "max"); "" until one arrives."""
    from . import cloud
    runtime = cloud._runtime
    if runtime is None or not runtime.signed_in or not runtime.refreshed_at:
        return ""
    with runtime.lock:
        plan = (runtime.manifest.get("account") or {}).get("plan")
    return plan.lower() if isinstance(plan, str) else ""


def _unit_prices(descriptors: list[dict]) -> tuple[int | None, float | None]:
    """(credits per model from a photo, credits per second of motion) from the live tools' estimates, as the
    try-it cards price them; None for a tool that is not offered or not priced."""
    def estimate(name: str) -> int | None:
        descriptor = next((d for d in descriptors if d.get("name") == name), None)
        if descriptor is None or descriptor.get("backend") != "managed":
            return None
        value = (descriptor.get("cost") or {}).get("credits_estimate")
        return value if type(value) is int and value > 0 else None
    model, motion = estimate("image_to_3d"), estimate("motion_from_text")
    return model, (motion / MOTION_DEFAULT_SECONDS if motion else None)


def _makes(credits: int, per_model: int | None, per_second: float | None) -> str:
    """What a month of credits makes, in real output: "27 models from photos, or 18 min of motion". A price that
    is missing drops its part rather than guess."""
    parts = []
    if per_model and credits >= per_model:
        models = credits // per_model
        parts.append("1 model from a photo" if models == 1 else f"{models:,} models from photos")
    if per_second:
        seconds = int(credits / per_second)
        parts.append(f"{seconds // 60} min of motion" if seconds >= 120 else f"{seconds} s of motion")
    return ", or ".join(parts)


def _draw_how(layout, context) -> None:
    sales = config.billing_open()
    plan = _account_plan()
    paid = plan in ("pro", "max")
    column = _page(layout, "how")
    _report_once("chatgpt", None, "how_shown")
    _headline(column, "How Loopcut works")
    _parts(column, sales)
    column.separator(factor=CARD_GAP)
    title, line = NO_PLAN if sales else NO_PLAN_NO_SALES
    _item(column, "help", title, line)
    if sales:
        column.separator(factor=CARD_GAP)
        _plans(column, plan)
    props = _primary(column, "loopcut.onboarding_step", "Continue")
    props.step, props.save = "connect", False
    left, right = _footer(column)
    _back(left, "account")
    if sales and not paid:  # Already on a paid plan: no upsell.
        _web_link(right, "See plans ↗", config.pricing_url("onboarding", "pro"))


def _parts(column, sales: bool) -> None:
    """The split: the two parts side by side, joined by a "+", as boxes of one build: who, what, a row of icons
    over captions (the same size and spacing in both), two lines."""
    card = (_content_px() - JOIN_UNITS * 20) / 2 / 20
    inner = card * 20 - 2 * BOX_PADDING
    gap = _icon_gap(inner)
    row = column.row(align=True)
    row.alignment = "LEFT"
    for n, (icon, who, what) in enumerate((THINKING, CLOUD)):
        if n:
            join = row.column()
            join.ui_units_x = JOIN_UNITS
            join.separator(factor=JOIN_OFFSET)
            _text(join, "+", scale=2.2, center=True)
        side = row.column()
        side.ui_units_x = card
        box = _group(side)
        _text(_icon_row(box, _row_mark() if icon == "mark" else _icon(icon)), who, MUTED)
        _text(box, what, scale=1.35, bold=True)
        box.separator(factor=0.5)
        _captioned_icons(box, TOOLS if icon == "mark" else THINKING_STEPS, gap)
        box.separator(factor=0.5)
        lines = (CLOUD_LINES if sales else CLOUD_LINES_NO_SALES) if icon == "mark" else \
            (THINKING_LINES if sales else THINKING_LINES_NO_SALES)
        _text(box, "\n".join(lines), MUTED, tight=True)


def _caption_widths(captions) -> list[float]:
    """The captions' widths in pixels at UI scale 1, as label_styled draws them; None outside Blender."""
    try:
        import blf
        points = bpy.context.preferences.ui_styles[0].widget.points
        blf.size(0, points * CAPTION)
        return [blf.dimensions(0, caption)[0] for caption in captions]
    except (ImportError, AttributeError, IndexError):
        return None


def _icon_gap(inner: float) -> float:
    """The space between captions in pixels at UI scale 1: the cloud tools' five captions spread across the box,
    and the thinking's four at the same spacing."""
    widths = _caption_widths([caption for _, caption in TOOLS])
    return max((inner - sum(widths)) / len(TOOLS), 4.0) if widths else inner / len(TOOLS) / 4  # As before: 12-15 px.


ICON_PX = 16  # An icon's width in pixels at UI scale 1.


def _captioned_icons(layout, icons, gap: float) -> None:
    """Outline icons each over a short muted caption, from the box's text column: every cell as wide as its
    caption (or its icon), the same gap between cells in both parts."""
    widths = _caption_widths([caption for _, caption in icons]) or [None] * len(icons)
    row = layout.row(align=True)
    row.alignment = "LEFT"
    for n, ((icon, caption), width) in enumerate(zip(icons, widths)):
        if n:
            row.separator(factor=gap / 6)  # A separator is 6 px per factor at UI scale 1.
        cell = row.column(align=True)
        if width is not None:
            cell.ui_units_x = max(width + 1, ICON_PX) / 20
        mark = cell.row()
        mark.alignment = "CENTER"
        value = _thumbnail(f"icons/{icon}.png")
        mark.label(text="", **({"icon_value": value} if value else {"icon": "DOT"}))
        _text(cell, caption, MUTED, scale=CAPTION, tight=True, center=True)


def _plans(column, plan: str) -> None:
    """Free and Pro (Max for a Max account) side by side where they differ, then a bar of what a month of each
    one's credits makes."""
    second = "max" if plan == "max" else "pro"
    box = _group(column)
    head = box.split(factor=PLAN_LABELS)
    head.label(text="")
    names = head.split(factor=0.5)
    for key in ("free", second):
        cell = names.column(align=True)
        _text(cell, key.capitalize(), bold=True)
        note = "No card needed" if key == "free" else f"${PLANS[key]['price']}/mo"
        _text(cell, "Your plan" if plan == key else note, ORANGE if plan == key else MUTED, scale=0.85, tight=True)
    for what, free, pro, most in PLAN_ROWS:
        row = box.split(factor=PLAN_LABELS)
        _text(row, what)
        cells = row.split(factor=0.5)
        for value in (free, most if second == "max" else pro):
            _plan_cell(cells.row(), value)
    per_model, per_second = _unit_prices(_descriptors())
    top = PLANS[second]["credits"]
    for key in ("free", second):
        credits = PLANS[key]["credits"]
        row = box.split(factor=PLAN_LABELS)
        bar = row.row(align=True)  # The name, then the track to the end of the column: exact widths, no split gap.
        bar.alignment = "LEFT"
        name = bar.row()
        name.ui_units_x = BAR_NAME / 20
        _text(name, key.capitalize(), MUTED)
        bar.separator(factor=BAR_GAP / 6)
        track = bar.column()
        track.ui_units_x = max(_bar_px() - BAR_NAME - BAR_GAP, 20) / 20
        track.separator(factor=BAR_OFFSET)
        line = track.row()
        line.scale_y = BAR_SCALE
        line.progress(factor=max(credits / top, BAR_LEAST), type="BAR", text="")
        makes = _makes(credits, per_model, per_second)
        _text(row, f"{credits:,} credits" + (f": {makes}" if makes else ""), MUTED)


def _bar_px() -> float:
    """The comparison's first column in pixels at UI scale 1: PLAN_LABELS of the box's inner width less the split's
    8 px."""
    return PLAN_LABELS * (_content_px() - 2 * BOX_PADDING - 8)


def _plan_cell(layout, value) -> None:
    layout.alignment = "LEFT"
    if isinstance(value, str):
        _text(layout, value)
    else:
        _icon_row(layout, _icon("yes" if value else "no"))


def _option(column, name: str, line: str, icon: dict, tag: str = "", line_icon: str = "", line_color=MUTED):
    """A model choice in its box: the name (and tag) with its action beside it, then one line across the box.
    Returns the action's column: every action at the same x and width, and as tall as the try-it Starts."""
    box = _group(column)
    top = box.split(factor=0.6)
    left = top.column()
    left.separator(factor=TITLE_OFFSET)  # The title's middle on the button's middle.
    title = left.row()
    title.alignment = "LEFT"
    _text(_icon_row(title, icon), name)
    if tag:
        if _styled(title):
            title.label_styled(text=tag, color=ORANGE)
        else:
            title.label(text=tag)
    right = top.column()
    right.scale_y = OPTION_SCALE
    if line_icon:
        _text(_icon_row(box, _icon(line_icon)), line, line_color)
    elif line:
        _text(box, line, line_color)
    return right


TITLE_OFFSET = 0.17  # Separator factor: with the column's spacing, half the difference of a row and a button.


def _draw_sign_in(layout, context) -> None:
    """One model is required. Loopcut's own models come with every account, so nobody signed in is stuck."""
    from . import chatgpt_ui
    if is_existing() and _connected() and not _state["change_model"]:
        _draw_connected(layout)  # An install from before the setup keeps the model it has.
        return
    chatgpt = chatgpt_ui.status()
    column = _page(layout, "connect")
    _headline(column, "Choose the model for your chat", "You can change it later in Preferences.")
    if chatgpt["status"] == "waiting":
        _watch_popup(context)
        right = _option(column, "ChatGPT", "Waiting for your browser…", _icon("person"), "Recommended", "wait")
        row = right.row(align=False)
        row.operator("loopcut.chatgpt_copy_link", text="Copy link")
        cancel = row.row()
        cancel.alignment = "RIGHT"
        _link(cancel, "loopcut.chatgpt_cancel", "Cancel")
    elif chatgpt["status"] == "error" and chatgpt["error"]:
        right = _option(column, "ChatGPT", _sentence(chatgpt["error"]), _icon("person"), "Recommended", "error",
                        DANGER)
        row = right.split(factor=0.45)  # A plain row gave the link the width and cut the button to "Try ag…".
        row.operator("loopcut.chatgpt_sign_in", text="Try again", depress=True)  # Keeps an issued client id.
        another = row.row()
        another.alignment = "RIGHT"
        _link(another, "loopcut.chatgpt_sign_in", "Another account").new_account = True
    elif _on_chatgpt_plan():
        right = _option(column, "ChatGPT", chatgpt_ui.account_line(), _icon("done"), "Recommended")
        if settings.preferences().provider != "CHATGPT":
            right.operator("loopcut.chatgpt_check", text="Use ChatGPT", depress=True)
        else:
            chatgpt_ui.request_readiness()
            chatgpt_ui.draw_readiness(right)
            right.prop(settings.preferences(), "model")
            proceed = right.row()
            proceed.enabled = chatgpt_ui.can_continue()
            props = proceed.operator("loopcut.onboarding_step", text="Continue", depress=True)
            props.step, props.save, props.provider = "privacy", True, "CHATGPT"
    else:
        _report_once("chatgpt", None, "shown")
        right = _option(column, "ChatGPT", CONNECT_LINE if config.billing_open() else CONNECT_LINE_NO_SALES,
                        _icon("person"), "Recommended")
        right.operator("loopcut.chatgpt_sign_in", text="Sign in with ChatGPT", depress=True)
    column.separator(factor=CARD_GAP)
    if account.signed_in():
        right = _option(column, "Loopcut models", "Loopcut Fast is free. Loopcut Pro comes with the Pro plan."
                        if config.billing_open() else "Loopcut Fast and Loopcut Pro run on your account.",
                        _row_mark())
        props = right.operator("loopcut.onboarding_step", text="Use Loopcut")
        props.step, props.save, props.provider = "privacy", True, "LOOPCUT"
    else:  # Only after "Continue without an account": a way back to it.
        right = _option(column, "Loopcut models", "Needs a Loopcut account.", _row_mark())
        props = right.operator("loopcut.onboarding_step", text="Sign in")
        props.step, props.save = "account", False
    column.separator(factor=CARD_GAP)
    right = _option(column, "Your own API key", "OpenAI, Anthropic, OpenRouter or a local model.", _icon("key"))
    right.operator("loopcut.onboarding_own_key", text="Add a key").enable = True
    left, right = _footer(column)
    _back(left, "how" if account.signed_in() else "account")  # Without an account the setup skipped "how".
    if not account.signed_in():  # And a way on: the setup comes back next start.
        _step_link(right, "Skip for now", "privacy")


def _connected_route(prefs) -> tuple[dict, str, str]:
    """(icon, name, detail) of the model already connected, as the model step's choices name it."""
    from . import chatgpt_ui
    if prefs.provider == "CHATGPT":
        return _icon("person"), "ChatGPT", chatgpt_ui.account_line()
    if prefs.provider == "LOOPCUT":
        return _row_mark(), "Loopcut models", settings.model_label(prefs.model)
    if prefs.provider == "OLLAMA":
        return _icon("computer"), settings.provider_label(prefs.provider), prefs.model
    return _icon("key"), f"{settings.provider_label(prefs.provider)}, with your key", prefs.model


def _draw_connected(layout) -> None:
    """The model step for an install from before the setup: the model it already has, kept with Continue."""
    prefs = settings.preferences()
    column = _page(layout, "connect")
    _report_once("chatgpt", _choice(), "existing_shown")
    _headline(column, "Your model is connected", "Loopcut keeps using it.")
    icon, name, detail = _connected_route(prefs)
    box = _group(column)
    title = box.row()
    title.alignment = "LEFT"
    _text(_icon_row(title, icon), name)
    if _styled(title):
        title.label_styled(text="Connected", color=SUCCESS)
    else:
        title.label(text="Connected")
    if detail:
        _text(_icon_row(box), detail, MUTED, tight=True)
    if prefs.provider == "CHATGPT":
        from . import chatgpt_ui
        chatgpt_ui.request_readiness()
        chatgpt_ui.draw_readiness(box)
        box.prop(prefs, "model")
    props = _primary(column, "loopcut.onboarding_step", "Continue",
                     enabled=prefs.provider != "CHATGPT" or chatgpt_ui.can_continue())
    props.step, props.save = "privacy", False
    left, right = _footer(column)
    _back(left, "how" if account.signed_in() else "account")
    _link(right, "loopcut.onboarding_change_model", "Change model")


def _on_signed_in() -> None:
    """A Loopcut account stays on its step to show its credits; a ChatGPT connection stays for its readiness result."""
    if _state["step"] == "account":
        _state.update(no_account=False, credits_asked=0.0)
        _ask_for_credits()
    elif _state["step"] == "connect" and getattr(settings.preferences(), "provider", None) == "CHATGPT":
        from . import chatgpt_ui
        chatgpt_ui.request_readiness()  # Stay on model setup so the result is visible before the first prompt.


def _privacy_url() -> str:
    return f"{config.SERVICE_URL}/privacy"


def _route_line(provider: str) -> str:
    """Where chats go, in the privacy screen's words."""
    if provider == "LOOPCUT":
        return "Loopcut models"
    if provider == "CHATGPT":
        return "ChatGPT, on your plan"
    if provider == "OLLAMA":
        return settings.provider_label(provider)  # On this computer: no key.
    return f"{settings.provider_label(provider)}, with your key"


def _draw_privacy(layout) -> None:
    prefs = settings.preferences()
    column = _page(layout, "privacy")
    _headline(column, "Your data", "What leaves your computer")
    group = _group(column)
    _item(group, "chat", "Chats go to the model you picked", _route_line(prefs.provider))
    group.separator(factor=ITEM_GAP)
    _item(group, "cloud", "Cloud tools send only what they need", "A picture, a mesh or a prompt.")
    group.separator(factor=ITEM_GAP)
    _item(group, "computer", "Your files stay on your computer", "Loopcut never uploads your .blend.")
    column.separator(factor=CARD_GAP)
    group = _group(column)
    for n, (name, text, line) in enumerate((
            ("auto_run", "Run code without asking", "Every step can be undone."),
            ("share_conversations", "Share conversations", "Text only, kept 90 days. Never used for training."),
            ("share_usage_stats", "Share anonymous usage stats", "Versions, steps and timings."))):
        if n:
            group.separator(factor=ITEM_GAP)
        block = group.column(align=True)
        _switch(block, prefs, name, text)
        _text(_icon_row(block), line, MUTED, tight=True)
    props = _primary(column, "loopcut.onboarding_step", "Continue")
    props.step, props.save = "done", True
    left, right = _footer(column)
    _back(left, "connect")
    _web_link(right, "Privacy policy ↗", _privacy_url())


# CARDS is shared with the agent (try_it.py). A per-second tool's manifest estimate covers its default
# length, so the card's "seconds" scales that price, shown struck through beside 0 credits for a free card.
MOTION_DEFAULT_SECONDS = 5  # motion_from_text bills per second and defaults to 5 (its manifest description).
# True of Pro and not of Free, from PLANS (renders and every cloud tool run on any plan's credits).
PRO_LINE = f"Pro plan: {PLANS['pro']['credits']:,} credits a month, Loopcut Pro and top-ups"
_preview = {"collection": None}


def card_cost(card: dict, descriptors: list[dict]) -> tuple[bool, int | None]:
    """(whether the card can run now, its credits): (True, 0) needs only the model; (False, None) when its
    tool is not offered to this account; (True, None) for a tool on the user's own key (no credits)."""
    if card["tool"] is None:
        return True, 0
    descriptor = next((d for d in descriptors if d.get("name") == card["tool"]), None)
    if descriptor is None:
        return False, None
    if descriptor.get("backend") != "managed":
        return True, None
    estimate = (descriptor.get("cost") or {}).get("credits_estimate")
    if type(estimate) is not int:
        return True, None
    if card.get("seconds"):
        return True, -(-estimate * card["seconds"] // MOTION_DEFAULT_SECONDS)
    return True, estimate


def _descriptors() -> list[dict]:
    from . import cloud
    runtime = cloud._runtime
    if runtime is None or not account.signed_in():
        return []
    try:  # A free card's tool is offered at any balance: its card's call is on the house (try_samples).
        return runtime.descriptors(on_the_house={c["tool"] for c in CARDS if c.get("free") and c["tool"]})
    except Exception as ex:  # A card hides rather than the screen failing.
        print(f"Loopcut: could not read the cloud tools: {ex!r}")
        return []


def _price(credits: int | None) -> str:
    from . import nudges
    if credits is None:
        return "Your own key"
    if credits == 0:
        return "Free"
    return f"{credits:g} credits · {nudges.dollars(credits)}"


def _previews():
    if _preview["collection"] is None:
        from bpy.utils import previews
        _preview["collection"] = previews.new()
    return _preview["collection"]


def _thumbnail(name: str) -> int:
    """A card's picture from media/ as an icon; 0 when it cannot be loaded."""
    try:
        collection = _previews()
        item = collection.get(name) or collection.load(name, str(MEDIA / name), "IMAGE")
    except Exception as ex:
        print(f"Loopcut: could not load {name}: {ex!r}")
        return 0
    return item.icon_id


def _try_line(signed_in: bool, credits: dict | None) -> str:
    from . import nudges
    balance = (credits or {}).get("balance")
    if not signed_in:
        return "Sign in to Loopcut for the cloud projects."
    if not config.billing_open():
        return "Pick one to start."
    if type(balance) in (int, float) and balance > 0:
        return f"You have {balance:,g} credits ({nudges.dollars(balance)})"
    return "No credits yet: the free project still works."


def _draw_try(layout, context) -> None:
    """Last step: one click closes the setup, docks the panel and sends a first project."""
    signed_in, connected = account.signed_in(), _connected()
    credits = _credits() if signed_in else None
    column = _page(layout, "done")
    if signed_in and credits is None:
        _ask_for_credits()  # Resumed here after a restart: this session has not fetched the account yet.
        if _credits_pending():
            _watch_popup(context)
    _headline(column, "Try your first project", _try_line(signed_in, credits))
    if _state["failed"]:
        failed = _state["failed"]
        shown = ", ".join(failed[:3]) + (f" and {len(failed) - 3} more" if len(failed) > 3 else "")
        _item(column, "error", "Some imported add-ons did not load", shown, color=DANGER)
        column.separator()
    descriptors = _descriptors()
    first = True
    for card in CARDS:
        runnable, credits_needed = card_cost(card, descriptors)
        if card["tool"] is not None and not runnable and signed_in:
            continue  # Not offered to this account (not live, blocked by its settings): no card.
        if not first:
            column.separator(factor=CARD_GAP)
        first = False
        # Widths set exactly, so the picture is BOX_PADDING from the card's edges, the text as far from the
        # picture, and Start's right edge on the boxes' content edge (where "See plans ↗" ends).
        row = _group(column, CARD_PADDING).row(align=True)
        row.alignment = "LEFT"
        picture = row.column()
        picture.ui_units_x = THUMBNAIL_UNITS
        icon = _thumbnail(card["thumbnail"])
        if icon:
            picture.template_icon(icon_value=icon, scale=THUMBNAIL_UNITS)
        else:
            picture.label()
        row.separator(factor=(BOX_PADDING - PREVIEW_INSET) / 6)  # A separator is 6 px per factor at UI scale 1.
        text = row.column()
        text.ui_units_x = _card_text_units()
        row.separator(factor=PREVIEW_INSET / 6)
        text.separator(factor=CARD_TEXT_ABOVE)
        _text(text, card["title"], scale=1.1, bold=True)
        _text(text, card["line"], MUTED)
        foot = text.split(factor=0.62)
        price = foot.column()
        price.separator(factor=TITLE_OFFSET)  # The price's middle on the button's middle.
        locked = card["tool"] is not None and not signed_in
        if locked:
            _text(price, "Needs a Loopcut account", MUTED)
        elif card.get("free") and credits_needed:  # On the house: what it would cost, struck, then 0.
            line = price.row(align=True)
            line.alignment = "LEFT"  # Side by side, not spread across the price's column.
            _text(line, f"{credits_needed:g} credits", DANGER, strike=True)
            line.separator(factor=0.5)
            _text(line, "0 credits")
        else:
            _text(price, _price(credits_needed))
        start = foot.column()
        start.enabled = runnable and connected
        start.scale_y = OPTION_SCALE
        start.operator("loopcut.onboarding_try", text="Start").card = card["id"]
        text.separator(factor=CARD_TEXT_BELOW)
    if config.billing_open():
        column.separator(factor=CARD_GAP)
        strip = _group(column).row()
        _item(strip, "upgrade", PRO_LINE)
        link = strip.row()
        link.alignment = "RIGHT"
        _web_link(link, "See plans ↗", config.pricing_url("onboarding", "pro"))
    left, right = _footer(column)
    _back(left, "privacy")
    _link(right, "loopcut.onboarding_try", "Skip for now").card = ""


# Separator factors above and below a card's text (title to Start button), so it is centred on the picture as
# seen: the title's row has room above its letters, the button none below it (measured at 2x).
CARD_TEXT_ABOVE, CARD_TEXT_BELOW = 0.1, 0.92


def _card_text_units() -> float:
    """The width of a try-it card's text column in units: the splash is 45 widget font sizes wide
    (wm_splash_screen.cc), less its 26 px margins, the card's padding, the picture's cell and the separators
    either side of the text. In pixels at UI scale 1; a unit is 20."""
    try:
        points = bpy.context.preferences.ui_styles[0].widget.points
    except (AttributeError, IndexError):
        points = 11
    inner = points * 45 - 2 * 26 - 2 * CARD_PADDING
    return max(inner - THUMBNAIL_UNITS * 20 - (BOX_PADDING - PREVIEW_INSET) - PREVIEW_INSET, 100) / 20


def _choice() -> str | None:
    """The model route set up so far, as the usage events name it; None when nothing is connected."""
    if not _connected():
        return None
    provider = getattr(settings.preferences(), "provider", "")
    return "chatgpt" if provider == "CHATGPT" else "loopcut" if provider == "LOOPCUT" else "own_key"


def _report(step: str, choice: str | None, ok: bool = True, code: str | None = None) -> None:
    """One first-run setup outcome as a setup_step usage event (telemetry.py). Never raises. code is a failure's
    reason, or which action of the step was taken (shown, claim, a first-project card): the contract's steps and
    choices are a closed list, so the funnel's finer steps ride in its one free code."""
    try:
        from . import telemetry
        telemetry.setup_step(step, choice, ok, code)
    except Exception as ex:
        print(f"Loopcut: setup event not recorded: {type(ex).__name__}")


_reported: set = set()


def _report_once(step: str, choice: str | None, code: str) -> None:
    """A screen being shown, from its draw function: once a session."""
    if (step, choice, code) not in _reported:
        _reported.add((step, choice, code))
        _report(step, choice, code=code)


def _report_move(current: str | None, target: str, provider: str) -> None:
    """What a step button means for the setup's usage events: a pick, a skip, or the last screen. A sign-in that
    worked or failed reports itself (account._setup_event, chatgpt_ui._run_sign_in). Never raises."""
    from . import telemetry
    try:
        if telemetry.enabled():
            _report_move_now(current, target, provider)
    except Exception as ex:
        print(f"Loopcut: setup event not recorded: {type(ex).__name__}")


def _report_move_now(current: str | None, target: str, provider: str) -> None:
    if current == "connect" and target == "privacy" and _state["existing"] and not _state["change_model"] \
            and not _state["own_key"]:
        _report("chatgpt", _choice(), code="existing_kept")  # An install from before kept its model.
        return
    if current == "how" and target == "connect":
        _report("chatgpt", None, code="how_continue")  # The explainer before the model step, read.
    if current == "connect" and target == "privacy":
        if _state["own_key"]:
            _report("own_key", "own_key", _connected(), None if _connected() else "not_connected")
        elif provider == "LOOPCUT":
            _report("chatgpt", "loopcut")
        elif provider != "CHATGPT" and not _on_chatgpt_plan():
            _report("chatgpt", "skip")  # Only offered without an account.
    if target == "done":
        _report("done", _choice(), _connected(), None if _connected() else "not_connected")


def _failed_addons(context) -> list[str]:
    """Enabled in the imported preferences, but not running here (written for another version,
    or the module is missing)."""
    failed = []
    for name in context.preferences.addons.keys():
        module = sys.modules.get(name)
        if module is None or not getattr(module, "__addon_enabled__", False):
            failed.append(name.rpartition(".")[2])
    return sorted(failed)


def _disable_other_copies(context) -> None:
    """Someone who ran Loopcut as an extension in stock Blender imports it enabled; two copies
    would register the same operators. The bundled one stays."""
    import addon_utils
    others = [name for name in context.preferences.addons.keys()
              if name != settings.PACKAGE and name.rpartition(".")[2] == settings.PACKAGE.rpartition(".")[2]]
    for name in others:
        addon_utils.disable(name, default_set=True)
    if others:  # The copy that registered last owned the operators; put the bundled one back.
        addon_utils.disable(settings.PACKAGE)
        addon_utils.enable(settings.PACKAGE)


def _import_now() -> None:
    """From a timer, not the operator: rereading preferences re-registers add-ons, and an
    operator must not outlive its own class."""
    found = _source()
    window = bpy.context.window_manager.windows[0]
    if found is None:
        _state.update(step="setup", error="")
        return
    version, source = found
    try:
        blender_import.copy_settings(source, Path(bpy.utils.resource_path("USER")))
    except (OSError, shutil.Error) as ex:
        print(f"Loopcut: could not copy {source}: {ex}")
        _state["error"] = "Could not copy the settings. Try importing again, or choose Start Fresh."
        return
    _state.update(step="setup", imported=version, error="")  # draw_splash picks what follows.
    with bpy.context.temp_override(window=window):
        # The same follow-up as Blender's own "copy previous settings".
        bpy.ops.wm.read_userpref()
        _ensure_enabled(bpy.context)
        bpy.ops.wm.read_history()
        bpy.ops.wm.operator_presets_cleanup()
        _disable_other_copies(bpy.context)
        _state["failed"] = _failed_addons(bpy.context)
        if bpy.data.is_saved is bpy.data.is_dirty is False:
            bpy.ops.wm.read_homefile()  # Their startup file. Closes the splash.
    bpy.app.timers.register(_reopen, first_interval=0.1, persistent=True)


def _ensure_enabled(context) -> None:
    """Rereading preferences disables and re-enables every add-on (addon_utils.disable_all); stock preferences
    that do not list Loopcut would leave it off. The fork's versioning lists it, this makes sure."""
    if settings.PACKAGE not in context.preferences.addons:
        import addon_utils
        addon_utils.enable(settings.PACKAGE, default_set=True)


_REOPEN_TRIES = 80  # Quarter seconds: 20 s for the add-on to be registered again.


def _reopen(tries: int = _REOPEN_TRIES):
    """Timer after an import: the panel and the setup come back once the add-on is fully registered again,
    never while it is half way (a setup drawn then would miss its buttons)."""
    from .ui import host
    if not _ready():
        if tries > 0:
            bpy.app.timers.register(lambda: _reopen(tries - 1), first_interval=0.25, persistent=True)
        else:
            print(f"Loopcut: not reopening the setup, operators missing: {', '.join(missing_operators())}")
        return None
    window = bpy.context.window_manager.windows[0]
    if not host.has_panel(window):  # Their startup file has no Loopcut panel.
        try:
            host.dock(window)
        except RuntimeError as ex:
            print(f"Loopcut: {ex}")
    if not _splash_open():  # The watchdog may have been first.
        _open_splash()
    return None


class LOOPCUT_OT_import_blender_settings(bpy.types.Operator):
    """Copy preferences, add-ons, keymap, themes and the startup file from Blender. Blender's own settings are not changed"""
    bl_idname = "loopcut.import_blender_settings"
    bl_label = "Import Blender Settings"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context):
        # Only into a folder without preferences: never over someone's Loopcut settings.
        return not (Path(bpy.utils.resource_path("USER")) / "config" / "userpref.blend").is_file()

    def execute(self, context):
        bpy.app.timers.register(_import_now, first_interval=0.0)
        return {"FINISHED"}


class LOOPCUT_OT_onboarding_own_key(bpy.types.Operator):
    """Connect a provider of your own instead of a Loopcut account"""
    bl_idname = "loopcut.onboarding_own_key"
    bl_label = "Use Your Own Key"
    bl_options = {"INTERNAL"}

    enable: bpy.props.BoolProperty(default=True)

    def execute(self, context):
        if self.enable:
            from . import chatgpt_ui
            account.cancel_sign_in()
            chatgpt_ui.cancel_sign_in()
        if self.enable and not _state["own_key"]:
            _report("chatgpt", "own_key")
        _state["own_key"] = self.enable
        prefs = settings.preferences()
        if prefs is not None:
            if self.enable and prefs.provider in {"LOOPCUT", "CHATGPT"}:
                _state["left_chatgpt"] = prefs.provider == "CHATGPT"
                prefs.provider = "OPENAI"
            elif (not self.enable and _state["left_chatgpt"] and prefs.provider == "OPENAI"
                  and chatgpt_auth.plan_signed_in()):
                # Back without picking a provider: looking at the key form must not cost the ChatGPT plan.
                prefs.provider = "CHATGPT"
        if not self.enable:
            _state["left_chatgpt"] = False
        if context.region_popup is not None:
            context.region_popup.tag_refresh_ui()
        return {"FINISHED"}


class LOOPCUT_OT_onboarding_step(bpy.types.Operator):
    bl_idname = "loopcut.onboarding_step"
    bl_label = "Continue"
    bl_options = {"INTERNAL"}

    step: bpy.props.EnumProperty(items=[(s, s, "") for s in ("setup", *NUMBERED)])
    save: bpy.props.BoolProperty()
    provider: bpy.props.EnumProperty(items=[(s, s, "") for s in ("CURRENT", "LOOPCUT", "CHATGPT")],
                                     default="CURRENT", options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, context):
        if _state["step"] == "account":
            account.cancel_sign_in()  # Leaving must not let a pending account change the model later.
        if _state["step"] == "connect":
            from . import chatgpt_ui
            chatgpt_ui.cancel_sign_in()
        prefs = settings.preferences()
        if self.provider != "CURRENT" and prefs is not None and prefs.provider != self.provider:
            prefs.provider = self.provider
        _report_move(_state["step"], self.step, self.provider)
        if _state["step"] == "privacy" and self.step == "done":
            mark_privacy_seen()  # What conversation logs share was on screen: they may start.
        if self.step == "account":
            _state["no_account"] = False
        _go(self.step)
        if self.save:
            bpy.ops.wm.save_userpref()
        if context.region_popup is not None:  # The splash stays open and only redraws when told to.
            context.region_popup.tag_refresh_ui()
        return {"FINISHED"}


class LOOPCUT_OT_onboarding_no_account(bpy.types.Operator):
    """Choose a model now; the Loopcut sign-in comes back next time"""
    bl_idname = "loopcut.onboarding_no_account"
    bl_label = "Continue Without an Account"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        failed = account.status()["status"] == "error"
        account.cancel_sign_in()
        _report("loopcut_account", "skip", False, "sign_in_failed" if failed else "sign_in_slow")
        _state.update(no_account=True, waiting_since=0.0)
        _go("connect")
        if context.region_popup is not None:
            context.region_popup.tag_refresh_ui()
        return {"FINISHED"}


class LOOPCUT_OT_onboarding_change_model(bpy.types.Operator):
    """Choose another model for your chat"""
    bl_idname = "loopcut.onboarding_change_model"
    bl_label = "Change Model"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        _state["change_model"] = True
        _report("chatgpt", _choice(), code="existing_change")
        if context.region_popup is not None:
            context.region_popup.tag_refresh_ui()
        return {"FINISHED"}


class LOOPCUT_OT_onboarding_claim(bpy.types.Operator):
    """Add the free credits that came with your new account"""
    bl_idname = "loopcut.onboarding_claim"
    bl_label = "Claim Free Credits"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        from . import nudges
        trial = (_credits() or {}).get("trial") or {}
        _state["claimed"] = True
        # The panel's own "trial credits added" card would only say it again.
        nudges.mark_shown("trial", str(trial.get("granted_at") or trial.get("amount")))
        _report("loopcut_account", "loopcut", code="claim")
        if context.region_popup is not None:
            context.region_popup.tag_refresh_ui()
        return {"FINISHED"}


class LOOPCUT_OT_onboarding_try(bpy.types.Operator):
    """Close the setup and start this project in the Loopcut panel"""
    bl_idname = "loopcut.onboarding_try"
    bl_label = "Try It"
    bl_options = {"INTERNAL"}

    card: bpy.props.StringProperty(options={"SKIP_SAVE"})

    def execute(self, context):
        card = next((c for c in CARDS if c["id"] == self.card), None)
        _report("done", _choice(), True, f"card_{card['id']}" if card else "no_card")
        _finish()
        # From a timer: the splash this button sits in closes once the click is handled.
        bpy.app.timers.register(lambda: _close_and_start(card), first_interval=0.0)
        return {"FINISHED"}


def _finish() -> None:
    """The setup ends for this session: done with an account and a model, else it comes back next start."""
    done = account.signed_in() and _connected()
    _report("done", _choice(), done, "setup_finished" if done else "abandoned_" + (_state["step"] or "account"))
    save_progress("done" if done else "account", done=done)
    _state.update(step=None, finished=True, region=None)
    if bpy.app.timers.is_registered(_watch):
        bpy.app.timers.unregister(_watch)


def _close_and_start(card: dict | None):
    """Close the modal splash, dock the panel and send the card's request; no card only opens the panel."""
    wm = bpy.context.window_manager
    if hasattr(wm, "splash_modal"):
        wm.splash_modal = False
    if hasattr(wm, "splash_banner"):
        wm.splash_banner = True  # The normal splash next time it opens.
    if not wm.windows:
        return None
    window = wm.windows[0]
    with bpy.context.temp_override(window=window):
        try:
            bpy.ops.wm.splash_close()
        except (AttributeError, RuntimeError) as ex:  # Not the Loopcut build: no such operator.
            print(f"Loopcut: could not close the splash: {ex}")
        if card is None:
            bpy.ops.loopcut.focus()
            return None
    from .ui import host
    host.start_request(window, card["prompt"], (str(SAMPLE_PHOTO),) if card.get("photo") else (),
                       try_it=card["id"] if card.get("free") else "")
    return None


_CLASSES = (LOOPCUT_OT_import_blender_settings, LOOPCUT_OT_onboarding_own_key, LOOPCUT_OT_onboarding_step,
            LOOPCUT_OT_onboarding_no_account, LOOPCUT_OT_onboarding_claim, LOOPCUT_OT_onboarding_try,
            LOOPCUT_OT_onboarding_change_model)


def register() -> None:
    global _registered
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    account.on_signed_in.append(_on_signed_in)
    # After lifecycle's startup timer has docked the panel behind it.
    bpy.app.timers.register(start, first_interval=0.5, persistent=True)
    _registered = True


def unregister() -> None:
    """Runs on every preferences reload (an import re-enables all add-ons), so it must never fail half way: a
    failure here leaves the add-on half registered and its next register() refused."""
    global _registered
    _registered = False
    try:
        for timer in (_refresh_popups, _watch, start, _rebuild_when_ready):
            if bpy.app.timers.is_registered(timer):
                bpy.app.timers.unregister(timer)
        _popups.clear()
        if _preview["collection"] is not None:
            from bpy.utils import previews
            previews.remove(_preview["collection"])
            _preview["collection"] = None
        if _on_signed_in in account.on_signed_in:
            account.on_signed_in.remove(_on_signed_in)
    finally:
        for cls in reversed(_CLASSES):
            bpy.utils.unregister_class(cls)
