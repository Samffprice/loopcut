"""Loopcut support and identity without copying Blender's splash/About menus.

Report a Problem (session_logs.py, contract SPEC §3.12) is drawn only in Preferences › Add-ons › Loopcut
(settings.draw_privacy), never in the chat panel: the owner's call, so the chat stays about the work."""

import bpy

from . import config


class LOOPCUT_OT_report_bug(bpy.types.Operator):
    """Open Loopcut's beta bug-report destination"""
    bl_idname = "loopcut.report_bug"
    bl_label = "Report a Loopcut Bug"

    def execute(self, context):
        bpy.ops.wm.url_open(url=config.bug_report_url())
        return {"FINISHED"}


class LOOPCUT_OT_report_problem(bpy.types.Operator):
    """Send Loopcut a problem report: your note, the add-on's recent log and, if you choose, your latest
    conversations as text. Never images, .blend files or keys. You get a code to quote"""
    bl_idname = "loopcut.report_problem"
    bl_label = "Report a Problem"

    note: bpy.props.StringProperty(name="What happened", maxlen=4000,
                                   description="What you asked for, what went wrong, and anything else that helps")
    contact_email: bpy.props.StringProperty(name="Email (optional)", maxlen=320,
                                            description="Where Loopcut can reply. Leave it empty to stay anonymous")
    include_conversations: bpy.props.BoolProperty(
        name="Include my 3 latest conversations", default=True,
        description="Add the text of your three most recent conversations, from any file, so we can see what "
                    "happened. Images are left out")

    @classmethod
    def poll(cls, context):
        from . import session_logs
        return not bpy.app.background and session_logs.report_state()["status"] != "sending"

    def invoke(self, context, event):
        from . import session_logs
        if not self.contact_email:
            self.contact_email = session_logs.account_email()
        return context.window_manager.invoke_props_dialog(self, width=480, title="Report a Problem",
                                                          confirm_text="Send Report")

    def draw(self, context):
        layout = self.layout
        layout.label(text="What happened?")
        layout.prop(self, "note", text="")
        layout.prop(self, "contact_email")
        layout.prop(self, "include_conversations")
        notes = layout.column(align=True)
        notes.scale_y = 0.75
        notes.active = False
        for line in ("Also sent: the add-on's recent log, and your Blender and Loopcut versions.",
                     "Never images, .blend files or keys. Kept 90 days, never used for training."):
            notes.label(text=line)

    def execute(self, context):
        from . import session_logs
        if bpy.app.background:
            return {"CANCELLED"}
        if not session_logs.start_report(self.note, self.contact_email, self.include_conversations,
                                         on_done=report_finished):
            self.report({"ERROR"}, f"Could not send the report. Please email {session_logs.SUPPORT_EMAIL} instead.")
            return {"CANCELLED"}
        self.report({"INFO"}, "Sending the report…")
        _redraw_preferences()
        return {"FINISHED"}


def _redraw_preferences() -> None:
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "PREFERENCES":
                    area.tag_redraw()
    except Exception:  # A redraw is a nicety; the result is printed and kept for the page anyway.
        pass


def report_finished(state: dict) -> None:
    """Main thread, once the report went or failed: the code on the clipboard, and a popup that says so."""
    manager = bpy.context.window_manager
    if state.get("code"):
        manager.clipboard = state["code"]
    _redraw_preferences()
    message = state.get("message") or ""
    title = f"Report sent: {state['code']}" if state.get("code") else "Report not sent"

    def draw(menu, _context):
        menu.layout.label(text=message)

    try:
        window = bpy.context.window or (manager.windows[0] if manager.windows else None)
        if window is None:
            return
        with bpy.context.temp_override(window=window):
            manager.popup_menu(draw, title=title, icon="CHECKMARK" if state.get("code") else "ERROR")
    except Exception as ex:  # The preferences page shows the same line.
        print(f"Loopcut: report result popup not shown: {type(ex).__name__}")


def _draw_info(self, context):
    if getattr(getattr(context, "window_manager", None), "splash_modal", False):
        # Not under the first-run setup: while it is modal, every operator button on it must be a way on
        # (the fork drops the modal when none is left), and this one only opens a page.
        return
    self.layout.label(text=f"Loopcut {config.version_label()}")
    self.layout.operator("loopcut.report_bug", text="Report a Bug", icon="URL")


# Not the first-run quick setup: onboarding.py owns those steps.
_MENUS = ("WM_MT_splash", "WM_MT_splash_about")
_CLASSES = (LOOPCUT_OT_report_bug, LOOPCUT_OT_report_problem)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    if hasattr(bpy.types, "SpaceLoopcut"):
        for name in _MENUS:
            getattr(bpy.types, name).append(_draw_info)


def unregister():
    if hasattr(bpy.types, "SpaceLoopcut"):
        for name in _MENUS:
            getattr(bpy.types, name).remove(_draw_info)
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
