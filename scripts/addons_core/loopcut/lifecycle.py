"""Keeps the conversation on screen in step with the file that is open."""

import bpy
from bpy.app.handlers import persistent

from . import agent, conversations, state


def switch_to(session: dict) -> None:
    """Make `session` the one on screen. The outgoing one is stopped and saved first."""
    agent.stop()  # A turn must not carry on into a different scene.
    conversations.save(state.session())
    state.replace(session)
    state.ui["view"] = "chat"
    from .ui import host
    host.tag_redraw_all()


def resume_for_open_file() -> None:
    current = state.session()
    project = conversations.project_key(bpy.data.filepath)
    if current["items"] and project in current["projects"]:
        return  # Already showing a conversation for this file, e.g. after a hot reload.
    switch_to(conversations.resume_or_new(bpy.data.filepath))


@persistent
def _on_load_post(*_):
    if state.restoring:  # checkpoints.restore loads a file too; that is not the user switching files.
        return
    resume_for_open_file()


@persistent
def _on_save_post(*_):
    if state.restoring:
        return
    # Save As, or the first save of an untitled scene: the conversation follows the file.
    session = state.session()
    conversations.add_project(session, bpy.data.filepath)
    conversations.save(session)


def _dock_at_startup() -> None:
    """The Loopcut build opens with the panel in place, once per start and only if the layout
    that loaded has none. Stock Blender is left as the user arranged it."""
    from . import settings
    from .ui import host
    prefs = settings.preferences()
    if not host.NATIVE or bpy.app.background or prefs is None or not prefs.dock_on_startup:
        return
    windows = bpy.context.window_manager.windows
    if windows and not any(host.has_panel(w) for w in windows):
        try:
            host.dock(windows[0])
        except RuntimeError as ex:
            print(f"Loopcut: {ex}")


def _initial_resume():
    # From a timer: bpy.data is not readable while an add-on is still registering.
    resume_for_open_file()
    _dock_at_startup()
    return None


def _poll_jobs():
    from . import job_tools
    from .ui import host
    job_tools.refresh_ui()
    host.tag_redraw_all()
    return 1.0


def _handlers() -> tuple:
    from . import files
    return ((bpy.app.handlers.load_post, _on_load_post), (bpy.app.handlers.save_post, _on_save_post),
            (bpy.app.handlers.render_complete, files.on_render_complete))


def register() -> None:
    from . import inspection
    inspection.register()
    for handlers, fn in _handlers():
        handlers.append(fn)
    bpy.app.timers.register(_initial_resume, first_interval=0.0, persistent=True)
    bpy.app.timers.register(_poll_jobs, first_interval=1.0, persistent=True)


def unregister() -> None:
    from . import inspection
    inspection.unregister()
    if bpy.app.timers.is_registered(_poll_jobs):
        bpy.app.timers.unregister(_poll_jobs)
    if bpy.app.timers.is_registered(_initial_resume):  # Disabled before its first tick.
        bpy.app.timers.unregister(_initial_resume)
    conversations.save(state.session())
    for handlers, fn in _handlers():
        if fn in handlers:
            handlers.remove(fn)
