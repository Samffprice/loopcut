"""Hosts the panel inside a Blender area: draw handler, input operators, keymap.

In the Loopcut build of Blender the panel lives in its own editor type (SpaceLoopcut). In stock
Blender, which the dev loop and the evals use, it borrows Text Editor areas that were opted in
with `loopcut.open`. Everything below the HOST_* constants is the same for both.
"""

import json
import sys
import time
from pathlib import Path

import bpy

from .. import account, agent, checkpoints, config, conversations, scene_context, settings, state, update
from . import draw, layout, textedit

NATIVE = hasattr(bpy.types, "SpaceLoopcut")
HOST_SPACE = bpy.types.SpaceLoopcut if NATIVE else bpy.types.SpaceTextEditor
HOST_UI_TYPE = "LOOPCUT" if NATIVE else "TEXT_EDITOR"
HOST_KEYMAP = ("Loopcut", "LOOPCUT") if NATIVE else ("Text", "TEXT_EDITOR")
SCROLL_STEP = 60
DOCK_FACTOR = 0.72           # Share of the 3D viewport's width that stays a viewport.
CONFIG_TTL = 2.0             # Seconds the model label is trusted before config is read again.
COMMAND = "oskey" if sys.platform == "darwin" else "ctrl"

_displays: dict[int, dict] = {}
_runtime: dict = {}


def hosts() -> set:
    return state.hosts


def is_host(area) -> bool:
    if area is None:
        return False
    return area.type == HOST_UI_TYPE if NATIVE else area.as_pointer() in hosts()


def tag_redraw_all() -> None:
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if is_host(area):
                area.tag_redraw()


def config_changed() -> None:
    """Settings were edited: show the new model (or the lack of a key) on the next redraw."""
    _runtime.pop("model", None)
    tag_redraw_all()


def _model_label() -> tuple[str, bool]:
    """(what to show in the input's footer, whether Loopcut still needs setting up)."""
    return _config_cached()[:2]


def _config_cached() -> tuple[str, bool, int]:
    """(model label, whether Loopcut still needs setting up, context budget), reread every CONFIG_TTL."""
    cached = _runtime.get("model")
    if cached is None or time.monotonic() - cached[3] > CONFIG_TTL:
        try:
            cfg = config.load()
            cached = (cfg.model, False, cfg.context_budget, time.monotonic())
        except config.ConfigError:
            cached = ("Set up Loopcut", True, config.DEFAULT_CONTEXT_BUDGET, time.monotonic())
        _runtime["model"] = cached
    return cached[0], cached[1], cached[2]


def _checkpoint_statuses(session: dict) -> dict:
    """id -> status for the checkpoints on screen. Re-read only when the index file changes."""
    index = checkpoints.data_root() / "checkpoints" / session["id"] / checkpoints.INDEX_NAME
    stamp = index.stat().st_mtime_ns if index.is_file() else None
    if _runtime.get("checkpoint_stamp") != (session["id"], stamp):
        # No index means the store is gone (dropped after 30 idle days) while the conversation,
        # which is kept, still points at it: those checkpoints show as expired.
        ids = [i["checkpoint"] for i in session["items"] if i.get("checkpoint")]
        _runtime["checkpoint_statuses"] = {i: checkpoints.status(session, i) if stamp else "missing" for i in ids}
        _runtime["checkpoint_stamp"] = (session["id"], stamp)
    return _runtime["checkpoint_statuses"]


def _restore_later(checkpoint_id: str) -> None:
    """Loading a file from inside a running operator frees that operator under its own feet,
    so the restore runs from a timer, after the click has been fully handled."""
    def run():
        session = state.session()
        try:
            checkpoints.restore(session, checkpoint_id)
        except checkpoints.CheckpointError as ex:
            session["items"].append(state.item_error(str(ex)))
        session["confirm_restore"] = None
        _runtime.pop("checkpoint_stamp", None)
        tag_redraw_all()
    state.session()["focused"] = False
    bpy.app.timers.register(run, first_interval=0.01)


def draw_area() -> None:
    context = bpy.context
    area, region = context.area, context.region
    if not is_host(area):
        return
    session = state.session()
    model, needs_setup, budget = _config_cached()
    selection = [o.name for o in context.view_layer.objects.selected] if context.view_layer else []
    options = settings.model_options() if state.ui.get("model_menu") else []
    display = layout.build(session, region.width, region.height, context.preferences.system.ui_scale,
                           draw.measure, model, _checkpoint_statuses(session),
                           {**state.ui, "now": time.time(), "needs_setup": needs_setup, "selection": selection,
                            "context_budget": budget, "model_options": options, "update": update.banner()})
    session["scroll"] = min(max(session["scroll"], 0.0), display["max_scroll"])
    draw.render(display)
    _displays[area.as_pointer()] = display


def _draw_trampoline() -> None:
    # Global lookup on purpose: after a hot reload this resolves to the new draw_area.
    draw_area()


def dump_display(path: str) -> None:
    """Write the last drawn frame as JSON, for debugging layout without looking at pixels."""
    if not _displays:
        raise RuntimeError("No Loopcut panel has been drawn yet")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(next(iter(_displays.values())), handle, indent=1, ensure_ascii=False)


# ---------------------------------------------------------------- input editing

def _update_mentions(session: dict) -> None:
    """Refresh the completion list for the @name under the caret, if there is one."""
    prefix = scene_context.mention_prefix(session["input"], session["cursor"])
    rows = [] if prefix is None else scene_context.candidates_from(scene_context.all_names(), prefix)
    state.ui["mentions"] = rows
    session["mention"] = min(session.get("mention", 0), max(0, len(rows) - 1))


def _pick_mention(session: dict, index: int) -> None:
    rows = state.ui.get("mentions") or []
    if not 0 <= index < len(rows):
        return
    start = session["input"].rfind("@", 0, session["cursor"])
    textedit.replace_range(session, start, session["cursor"], scene_context.mention_text(rows[index][0]) + " ")
    state.ui["mentions"], session["mention"] = [], 0


def _submit(session: dict) -> None:
    if agent.send(session["input"]):
        textedit.set_text(session, "")
    state.ui["mentions"] = []


# ---------------------------------------------------------------- the + menu

PICKER_ROWS = 8


def _picker_names() -> list[tuple[str, str]]:
    """Everything the + menu can add: an image file, then what @ completes."""
    return [("Image file…", "image")] + scene_context.all_names()


def _open_picker() -> None:
    state.ui["picker"] = {"query": "", "rows": [], "active": 0}
    state.ui["mentions"], state.ui["model_menu"] = [], False
    _filter_picker()


def _filter_picker() -> None:
    picker = state.ui.get("picker")
    if picker is None:
        return
    names = _picker_names()
    query = picker["query"]
    picker["rows"] = scene_context.candidates_from(names, query, PICKER_ROWS) if query else names[:PICKER_ROWS]
    picker["active"] = min(picker.get("active", 0), max(0, len(picker["rows"]) - 1))


def _close_popups() -> None:
    state.ui["picker"], state.ui["model_menu"] = None, False


def _pick_from_picker(session: dict, index: int) -> None:
    picker = state.ui.get("picker") or {}
    rows = picker.get("rows") or []
    if not 0 <= index < len(rows):
        return
    name, kind = rows[index]
    _close_popups()
    if kind == "image":
        bpy.ops.loopcut.attach_images("INVOKE_DEFAULT")
        return
    textedit.insert(session, scene_context.mention_text(name) + " ")
    _update_mentions(session)


def _picker_key(session: dict, event) -> bool:
    """Keys while the + menu is open: they search it. True when the key was used."""
    picker = state.ui["picker"]
    kind = event.type
    if kind == "ESC":
        _close_popups()
    elif kind in {"UP_ARROW", "DOWN_ARROW"}:
        if picker["rows"]:
            picker["active"] = (picker["active"] + (1 if kind == "DOWN_ARROW" else -1)) % len(picker["rows"])
    elif kind in {"RET", "NUMPAD_ENTER", "TAB"}:
        _pick_from_picker(session, picker["active"])
    elif kind == "BACK_SPACE":
        picker["query"] = picker["query"][:-1]
        _filter_picker()
    elif event.unicode and event.unicode.isprintable() and not (event.oskey or event.ctrl):
        picker["query"] += event.unicode
        _filter_picker()
    else:
        return False
    return True


def _open_settings() -> None:
    if settings.preferences() is None:
        state.session()["items"].append(state.item_error(
            "Loopcut is running from a checkout, so it has no preferences page. Set LOOPCUT_API_KEY, "
            "LOOPCUT_BASE_URL and LOOPCUT_MODEL in .env."))
        return
    bpy.ops.preferences.addon_show(module=settings.PACKAGE)


def _do_action(session: dict, action) -> None:
    kind, index = action
    if kind == "approve":
        agent.decide(True)
    elif kind == "approve_always":
        agent.decide(True, always=True)
    elif kind == "keep_changes":
        session["items"][index]["resolved"] = "kept"
        conversations.save(session)
    elif kind == "mention_pick":
        _pick_mention(session, index)
    elif kind == "picker_open":
        _close_popups() if state.ui.get("picker") is not None else _open_picker()
    elif kind == "picker_pick":
        _pick_from_picker(session, index)
    elif kind == "model_menu":
        state.ui["model_menu"] = not state.ui.get("model_menu")
        state.ui["picker"] = None
    elif kind == "model_pick":
        _close_popups()
        settings.choose_model(index)
        config_changed()
    elif kind == "noop":
        pass
    elif kind == "open_settings":
        _close_popups()
        _open_settings()
    elif kind == "open_url":
        account.open_browser(session["items"][index]["url"])
    elif kind == "open_account":
        account.open_browser(config.account_url())
    elif kind == "limit_upgrade":
        _start_upgrade(session, session["items"][index])
    elif kind == "limit_fast":
        settings.set_tier("fast")
        agent.resume()
    elif kind == "resume":
        agent.resume()
    elif kind == "attach":
        bpy.ops.loopcut.attach_images("INVOKE_DEFAULT")
    elif kind == "remove_attachment":
        del session["attachments"][index]
    elif kind == "unpin_reference":
        pinned = [r for r in session["references"] if r.get("pinned", True)]
        if index < len(pinned):
            pinned[index]["pinned"] = False
            conversations.save(session)
    elif kind == "reject":
        agent.decide(False)
    elif kind == "toggle":
        item = session["items"][index]
        item["expanded"] = not item.get("expanded", False)
    elif kind == "restore_ask":
        session["confirm_restore"] = index
    elif kind == "restore_cancel":
        session["confirm_restore"] = None
    elif kind == "restore_confirm":
        _restore_later(session["items"][index]["checkpoint"])
    elif kind == "restore":
        _restore_later(index)
    elif kind == "history_open":
        state.ui["history"] = conversations.list_for(conversations.project_key(bpy.data.filepath))
        state.ui["view"] = "history"
    elif kind == "history_close":
        state.ui["view"] = "chat"
    elif kind == "jobs_open":
        from .. import job_tools
        job_tools.refresh_ui()
        state.ui["view"] = "jobs"
        state.ui["jobs_page"] = 0
    elif kind == "jobs_page":
        state.ui["jobs_page"] = index
    elif kind in {"job_cancel", "job_resume", "job_preview", "job_open"}:
        from .. import job_tools
        job_tools.ui_action(kind, index)
    elif kind == "open_conversation":
        _switch(session, lambda: conversations.load(index))
    elif kind == "new_chat":
        def fresh():
            new = state.new_session()
            conversations.add_project(new, bpy.data.filepath)
            return new
        _switch(session, fresh)
    elif kind == "close_panel":
        _close_area_later(bpy.context.window, bpy.context.area)
    elif kind == "update_download":
        update.download()
    elif kind == "update_restart":
        update.restart_to_update()
    elif kind == "update_page":
        update.open_notes()
    elif kind == "update_dismiss":
        update.dismiss()


def _start_upgrade(session: dict, item: dict) -> None:
    """Open the plans page and wait for the plan to change; then the turn goes on by itself,
    so the user comes back to Blender to find the work under way."""
    account.open_browser(item["upgrade"]["url"])
    item["status"] = "waiting"

    def upgraded(payload: dict) -> None:
        if item.get("status") != "waiting":
            return
        item["status"] = "done"
        item["text"] = f"You are on the {str(payload.get('plan') or '').capitalize()} plan now. Continuing."
        conversations.save(session)
        if state.session() is session:
            agent.resume()

    account.watch_plan(item["plan"], upgraded)


def _close_area_later(window, area) -> None:
    """Join the panel's area into its neighbour, from a timer: the click arrives inside a modal
    operator that runs in this very area, which must finish before the area is freed."""
    def close():
        if area is None or window is None:
            return None
        try:
            with bpy.context.temp_override(window=window, area=area):
                bpy.ops.screen.area_close()
        except RuntimeError as ex:  # The last area, or full screen: Blender says no.
            print(f"Loopcut: could not close the panel: {ex}")
        return None
    bpy.app.timers.register(close, first_interval=0.05)


def _switch(session: dict, make_session) -> None:
    """Show another conversation. The one on screen is stopped and saved, never dropped."""
    from .. import lifecycle
    try:
        replacement = make_session()
    except conversations.ConversationError as ex:
        session["items"].append(state.item_error(str(ex)))
        state.ui["view"] = "chat"
        return
    replacement["focused"] = session["focused"]
    session["focused"] = False
    lifecycle.switch_to(replacement)
    _runtime.pop("checkpoint_stamp", None)


def _find_area(context, pointer: int):
    for area in context.window.screen.areas:
        if area.as_pointer() == pointer:
            return area
    return None


def _window_region(area):
    return next(r for r in area.regions if r.type == "WINDOW")


def _local(region, event) -> tuple[int, int, bool]:
    x, y = event.mouse_x - region.x, event.mouse_y - region.y
    return x, region.height - y, 0 <= x < region.width and 0 <= y < region.height


def _show_in(area) -> None:
    area.ui_type = HOST_UI_TYPE
    if not NATIVE:  # The borrowed Text Editor's own regions would sit around the panel.
        space = area.spaces.active
        space.show_region_header = False
        space.show_region_footer = False
        space.show_region_ui = False
        hosts().add(area.as_pointer())
    area.tag_redraw()


_docking: set = set()  # Windows with a dock() in flight; the new area is not a host until finish().


def find_panel(window):
    return next((a for a in window.screen.areas if is_host(a)), None)


def has_panel(window) -> bool:
    """True when the window shows Loopcut or is about to: dock() finishes on a later tick, and
    two callers racing it (startup and the onboarding import did) must not both dock."""
    return window.as_pointer() in _docking or find_panel(window) is not None


def dock(window, then=None) -> None:
    """Split the largest 3D viewport and put the panel on its right, where Cursor keeps its chat.
    The new area only has its geometry a tick later, so the panel is opened (and `then(area)`
    called) from a timer."""
    views = [a for a in window.screen.areas if a.type == "VIEW_3D"]
    if not views:
        raise RuntimeError("Loopcut docks next to a 3D viewport, and this window has none.")
    view = max(views, key=lambda a: a.width * a.height)
    region = next(r for r in view.regions if r.type == "WINDOW")
    before = {a.as_pointer() for a in window.screen.areas}
    key = window.as_pointer()
    _docking.add(key)
    try:
        with bpy.context.temp_override(window=window, area=view, region=region):
            bpy.ops.screen.area_split(direction="VERTICAL", factor=DOCK_FACTOR)
    except Exception:
        _docking.discard(key)
        raise

    def finish():
        try:
            fresh = [a for a in window.screen.areas if a.as_pointer() not in before]
            panel = max(fresh + [view], key=lambda a: a.x)
            _show_in(panel)
            if then:
                then(panel)
        finally:
            _docking.discard(key)
    bpy.app.timers.register(finish, first_interval=0.1)


class LOOPCUT_OT_open(bpy.types.Operator):
    """Show Loopcut in this area"""
    bl_idname = "loopcut.open"
    bl_label = "Open Loopcut Here"

    def execute(self, context):
        _show_in(context.area)
        return {"FINISHED"}


class LOOPCUT_OT_focus(bpy.types.Operator):
    """Start typing to Loopcut, opening its panel if this window has none"""
    bl_idname = "loopcut.focus"
    bl_label = "Ask Loopcut"

    def execute(self, context):
        window = context.window

        def focus(area):
            with bpy.context.temp_override(window=window, area=area, region=_window_region(area)):
                bpy.ops.loopcut.interact("INVOKE_DEFAULT", keyboard=True)

        panel = find_panel(window)
        if panel:
            focus(panel)
        else:
            try:
                dock(window, then=focus)
            except RuntimeError as ex:
                self.report({"WARNING"}, str(ex))
                return {"CANCELLED"}
        return {"FINISHED"}


class LOOPCUT_OT_scroll(bpy.types.Operator):
    bl_idname = "loopcut.scroll"
    bl_label = "Scroll Loopcut"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context):
        return is_host(context.area)

    def invoke(self, context, event):
        scroll(state.session(), event)
        context.area.tag_redraw()
        return {"FINISHED"}


def scroll(session: dict, event) -> None:
    if event.type == "WHEELUPMOUSE":
        session["scroll"] += SCROLL_STEP
    elif event.type == "WHEELDOWNMOUSE":
        session["scroll"] -= SCROLL_STEP
    elif event.type == "TRACKPADPAN":
        session["scroll"] += event.mouse_y - event.mouse_prev_y
    session["scroll"] = max(session["scroll"], 0.0)  # Upper bound is clamped at draw time.


class LOOPCUT_OT_interact(bpy.types.Operator):
    """Click and type in the Loopcut panel"""
    bl_idname = "loopcut.interact"
    bl_label = "Loopcut Input"
    bl_options = {"INTERNAL"}

    keyboard: bpy.props.BoolProperty(options={"SKIP_SAVE", "HIDDEN"})  # Started by the shortcut, not a click.

    @classmethod
    def poll(cls, context):
        return is_host(context.area)

    def invoke(self, context, event):
        session = state.session()
        self.area_pointer = context.area.as_pointer()
        if not self.keyboard:
            self._click(session, context.area, event)
        session["focused"] = True
        context.area.tag_redraw()
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def _click(self, session, area, event) -> bool:
        display = _displays.get(area.as_pointer())
        x, y, inside = _local(_window_region(area), event)
        if inside and display:
            action = layout.hit_test(display, x, y)
            kind = action[0] if action else None
            if kind not in POPUP_ACTIONS and kind not in ("picker_open", "model_menu"):
                _close_popups()  # A click anywhere else puts a menu away.
            if action and kind != "focus":
                _do_action(session, action)
        return inside

    def _finish(self, session, area):
        session["focused"] = False
        state.ui["mentions"] = []
        if area:
            area.tag_redraw()
        return {"FINISHED", "PASS_THROUGH"}

    def _key(self, context, session, event) -> bool:
        """One key press in the input box. False means: leave the panel."""
        kind, shift = event.type, event.shift
        command = event.oskey or event.ctrl       # Cmd on macOS, Ctrl elsewhere; both accepted.
        by_word = event.alt or (event.ctrl and sys.platform != "darwin")
        if state.ui.get("picker") is not None and _picker_key(session, event):
            return True
        if state.ui.get("model_menu") and kind == "ESC":
            _close_popups()
            return True
        mentions = state.ui.get("mentions") or []
        if mentions and kind in {"UP_ARROW", "DOWN_ARROW"}:
            session["mention"] = (session.get("mention", 0) + (1 if kind == "DOWN_ARROW" else -1)) % len(mentions)
            return True
        if mentions and kind in {"TAB", "RET", "NUMPAD_ENTER"}:
            _pick_mention(session, session.get("mention", 0))
            return True
        if kind == "ESC":
            if mentions:
                state.ui["mentions"] = []
            elif not agent.decide(False):
                if session["busy"]:
                    agent.stop()
                else:
                    return False
            return True
        if kind in {"RET", "NUMPAD_ENTER"}:
            if shift:
                textedit.insert(session, "\n")
            elif not (not session["input"].strip() and agent.decide(True)):
                _submit(session)
        elif kind in {"BACK_SPACE", "DEL"}:
            textedit.delete(session, backwards=kind == "BACK_SPACE", word=by_word)
        elif kind in {"LEFT_ARROW", "RIGHT_ARROW"}:
            side = "left" if kind == "LEFT_ARROW" else "right"
            target = f"line_{'home' if side == 'left' else 'end'}" if event.oskey else \
                (f"word_{side}" if by_word else side)
            textedit.move(session, target, select=shift)
        elif kind == "UP_ARROW" and not session["input"]:
            previous = next((i["text"] for i in reversed(session["items"]) if i["kind"] == "user"), "")
            textedit.set_text(session, previous)
        elif kind in {"HOME", "END"}:
            textedit.move(session, "line_home" if kind == "HOME" else "line_end", select=shift)
        elif command and kind == "A":
            textedit.select_all(session)
        elif command and kind == "C":
            if textedit.selected_text(session):
                context.window_manager.clipboard = textedit.selected_text(session)
        elif command and kind == "X":
            cut = textedit.cut(session)
            if cut:
                context.window_manager.clipboard = cut
        elif command and kind == "V":
            textedit.insert(session, context.window_manager.clipboard)
        elif command and kind == "Z":
            textedit.undo(session)
        elif event.unicode and event.unicode.isprintable() and not command:
            textedit.insert(session, event.unicode)
        else:
            return True  # Not ours, and nothing changed.
        _update_mentions(session)
        return True

    def modal(self, context, event):
        session = state.session()
        area = _find_area(context, self.area_pointer)
        if area is None or not session["focused"]:
            return self._finish(session, area)

        kind = event.type
        if kind in {"WHEELUPMOUSE", "WHEELDOWNMOUSE", "TRACKPADPAN"}:
            if not _local(_window_region(area), event)[2]:
                return {"PASS_THROUGH"}
            scroll(session, event)
        elif kind == "LEFTMOUSE":
            if event.value != "PRESS":
                return {"PASS_THROUGH"}
            if not self._click(session, area, event):
                return self._finish(session, area)
        elif event.value != "PRESS" or kind in {"MOUSEMOVE", "INBETWEEN_MOUSEMOVE", "TIMER", "RIGHTMOUSE",
                                                "MIDDLEMOUSE"}:
            return {"PASS_THROUGH"}
        elif not self._key(context, session, event):
            return self._finish(session, area)
        area.tag_redraw()
        return {"RUNNING_MODAL"}


POPUP_ACTIONS = {"noop", "picker_pick", "model_pick", "mention_pick", "open_settings"}


class LOOPCUT_OT_hover(bpy.types.Operator):
    """Follows the mouse over the panel for hover highlights and tooltips"""
    bl_idname = "loopcut.hover"
    bl_label = "Loopcut Hover"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context):
        # From the window keymap, so leaving the panel clears the highlight; cheap when neither applies.
        return is_host(context.area) or bool(state.ui.get("hover"))

    def invoke(self, context, event):
        area = context.area
        hovered = None
        if is_host(area):
            display = _displays.get(area.as_pointer())
            x, y, inside = _local(_window_region(area), event)
            hit = layout.hit_at(display, x, y) if inside and display else None
            hovered = hit["id"] if hit else None
            state.ui["mouse"] = (x, y)
        if state.ui.get("hover") != hovered:
            state.ui["hover"] = hovered
            tag_redraw_all()
        return {"PASS_THROUGH"}


class LOOPCUT_OT_attach_images(bpy.types.Operator):
    """Attach images to your next message"""
    bl_idname = "loopcut.attach_images"
    bl_label = "Attach Images"
    bl_options = {"INTERNAL"}

    # What a file drop fills in (see LOOPCUT_FH_images) and what the file browser returns.
    directory: bpy.props.StringProperty(subtype="DIR_PATH", options={"SKIP_SAVE", "HIDDEN"})
    files: bpy.props.CollectionProperty(type=bpy.types.OperatorFileListElement, options={"SKIP_SAVE", "HIDDEN"})
    filter_image: bpy.props.BoolProperty(default=True, options={"HIDDEN"})
    filter_folder: bpy.props.BoolProperty(default=True, options={"HIDDEN"})

    def invoke(self, context, event):
        if self.files:  # Dropped.
            return self.execute(context)
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        from .. import attachments
        session = state.session()
        paths = [str(Path(self.directory) / entry.name) for entry in self.files if entry.name]
        problems = attachments.add(session, paths)
        for problem in problems:
            session["items"].append(state.item_error(problem))
        if session["attachments"] and not session["focused"]:
            window = context.window  # Dropping a photo is followed by typing about it.

            def focus():
                with bpy.context.temp_override(window=window):
                    bpy.ops.loopcut.focus()
            bpy.app.timers.register(focus, first_interval=0.0)
        conversations.save(session)
        tag_redraw_all()
        return {"FINISHED"} if len(problems) < len(paths) else {"CANCELLED"}


class LOOPCUT_FH_images(bpy.types.FileHandler):
    bl_idname = "LOOPCUT_FH_images"
    bl_label = "Attach to Loopcut"
    bl_import_operator = LOOPCUT_OT_attach_images.bl_idname
    bl_file_extensions = ".png;.jpg;.jpeg;.webp;.bmp;.tif;.tiff;.tga"  # attachments.EXTENSIONS

    @classmethod
    def poll_drop(cls, context):
        return context.area is not None and is_host(context.area)


_CLASSES = (LOOPCUT_OT_open, LOOPCUT_OT_focus, LOOPCUT_OT_scroll, LOOPCUT_OT_interact, LOOPCUT_OT_hover,
            LOOPCUT_OT_attach_images,
            LOOPCUT_FH_images)


def register() -> None:
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    _runtime["handler"] = HOST_SPACE.draw_handler_add(_draw_trampoline, (), "WINDOW", "POST_PIXEL")
    keyconfig = bpy.context.window_manager.keyconfigs.addon
    if keyconfig:  # None in background mode.
        keymap = keyconfig.keymaps.new(name=HOST_KEYMAP[0], space_type=HOST_KEYMAP[1])
        items = [keymap.keymap_items.new(LOOPCUT_OT_interact.bl_idname, "LEFTMOUSE", "PRESS", head=True)]
        for wheel in ("WHEELUPMOUSE", "WHEELDOWNMOUSE", "TRACKPADPAN"):
            value = "ANY" if wheel == "TRACKPADPAN" else "PRESS"
            items.append(keymap.keymap_items.new(LOOPCUT_OT_scroll.bl_idname, wheel, value, head=True))
        # Cmd+L / Ctrl+Alt+L from anywhere: Cursor's shortcut, moved off Blender's Ctrl+L (Link Data).
        window_keymap = keyconfig.keymaps.new(name="Window", space_type="EMPTY")
        focus = window_keymap.keymap_items.new(LOOPCUT_OT_focus.bl_idname, "L", "PRESS", **(
            {"oskey": True} if COMMAND == "oskey" else {"ctrl": True, "alt": True}))
        hover = window_keymap.keymap_items.new(LOOPCUT_OT_hover.bl_idname, "MOUSEMOVE", "ANY")
        _runtime["keymap"] = [(keymap, items), (window_keymap, [focus, hover])]


def unregister() -> None:
    state.session()["focused"] = False
    if "keymap" in _runtime:
        for keymap, items in _runtime.pop("keymap"):
            for item in items:
                keymap.keymap_items.remove(item)
    if "handler" in _runtime:
        HOST_SPACE.draw_handler_remove(_runtime.pop("handler"), "WINDOW")
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
    _displays.clear()
