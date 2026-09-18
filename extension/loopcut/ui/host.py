"""Hosts the panel inside a Blender area: draw handler, input operators, keymap.

Until the fork's own editor type exists, the panel borrows Text Editor areas that were
opted in with `loopcut.open`. Moving to the real editor means changing HOST_SPACE/HOST_UI_TYPE.
"""

import json

import bpy

from .. import agent, checkpoints, config, state
from . import draw, layout

HOST_SPACE = bpy.types.SpaceTextEditor
HOST_UI_TYPE = "TEXT_EDITOR"
HOST_KEYMAP = ("Text", "TEXT_EDITOR")
SCROLL_STEP = 60

_displays: dict[int, dict] = {}
_runtime: dict = {}


def hosts() -> set:
    return state.hosts


def is_host(area) -> bool:
    return area is not None and area.as_pointer() in hosts()


def tag_redraw_all() -> None:
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if is_host(area):
                area.tag_redraw()


def _model_label() -> str:
    if "model" not in _runtime:
        try:
            _runtime["model"] = config.load().model
        except config.ConfigError as ex:
            print(f"Loopcut: {ex}")
            _runtime["model"] = "not configured"
    return _runtime["model"]


def _checkpoint_statuses(session: dict) -> dict:
    """id -> status for the checkpoints on screen. Re-read only when the index file changes."""
    index = checkpoints.data_root() / "checkpoints" / session["id"] / checkpoints.INDEX_NAME
    stamp = index.stat().st_mtime_ns if index.is_file() else None
    if _runtime.get("checkpoint_stamp") != (session["id"], stamp):
        ids = [i["checkpoint"] for i in session["items"] if i.get("checkpoint")]
        _runtime["checkpoint_statuses"] = {i: checkpoints.status(session, i) for i in ids} if stamp else {}
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
    display = layout.build(session, region.width, region.height, context.preferences.system.ui_scale,
                           draw.measure, _model_label(), _checkpoint_statuses(session))
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

def _insert(session: dict, text: str) -> None:
    cursor = session["cursor"]
    session["input"] = session["input"][:cursor] + text + session["input"][cursor:]
    session["cursor"] = cursor + len(text)


def _delete(session: dict, backwards: bool) -> None:
    cursor, text = session["cursor"], session["input"]
    if backwards and cursor > 0:
        session["input"], session["cursor"] = text[:cursor - 1] + text[cursor:], cursor - 1
    elif not backwards and cursor < len(text):
        session["input"] = text[:cursor] + text[cursor + 1:]


def _submit(session: dict) -> None:
    if agent.send(session["input"]):
        session["input"], session["cursor"] = "", 0


def _do_action(session: dict, action) -> None:
    kind, index = action
    if kind == "approve":
        agent.decide(True)
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
    elif kind == "new_chat":
        agent.stop()
        focused = session["focused"]
        state.reset()["focused"] = focused


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


class LOOPCUT_OT_open(bpy.types.Operator):
    """Show Loopcut in this area"""
    bl_idname = "loopcut.open"
    bl_label = "Open Loopcut Here"

    def execute(self, context):
        area = context.area
        area.ui_type = HOST_UI_TYPE
        space = area.spaces.active
        space.show_region_header = False
        space.show_region_footer = False
        space.show_region_ui = False
        hosts().add(area.as_pointer())
        area.tag_redraw()
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

    @classmethod
    def poll(cls, context):
        return is_host(context.area)

    def invoke(self, context, event):
        session = state.session()
        self.area_pointer = context.area.as_pointer()
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
            if action and action[0] != "focus":
                _do_action(session, action)
        return inside

    def _finish(self, session, area):
        session["focused"] = False
        if area:
            area.tag_redraw()
        return {"FINISHED", "PASS_THROUGH"}

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
        elif kind == "ESC":
            if not agent.decide(False):
                if session["busy"]:
                    agent.stop()
                else:
                    return self._finish(session, area)
        elif kind in {"RET", "NUMPAD_ENTER"}:
            if event.shift:
                _insert(session, "\n")
            elif not (not session["input"].strip() and agent.decide(True)):
                _submit(session)
        elif kind == "BACK_SPACE":
            _delete(session, backwards=True)
        elif kind == "DEL":
            _delete(session, backwards=False)
        elif kind == "LEFT_ARROW":
            session["cursor"] = max(0, session["cursor"] - 1)
        elif kind == "RIGHT_ARROW":
            session["cursor"] = min(len(session["input"]), session["cursor"] + 1)
        elif kind == "HOME":
            session["cursor"] = 0
        elif kind == "END":
            session["cursor"] = len(session["input"])
        elif kind == "V" and (event.oskey or event.ctrl):
            _insert(session, context.window_manager.clipboard)
        elif event.unicode and event.unicode.isprintable() and not (event.ctrl or event.oskey):
            _insert(session, event.unicode)
        area.tag_redraw()
        return {"RUNNING_MODAL"}


_CLASSES = (LOOPCUT_OT_open, LOOPCUT_OT_scroll, LOOPCUT_OT_interact)


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
        _runtime["keymap"] = (keymap, items)


def unregister() -> None:
    state.session()["focused"] = False
    if "keymap" in _runtime:
        keymap, items = _runtime.pop("keymap")
        for item in items:
            keymap.keymap_items.remove(item)
    if "handler" in _runtime:
        HOST_SPACE.draw_handler_remove(_runtime.pop("handler"), "WINDOW")
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
    _displays.clear()
