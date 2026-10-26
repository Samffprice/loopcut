"""Keeps the conversation on screen in step with the file that is open."""

import bpy
from bpy.app.handlers import persistent

from . import agent, conversations, state


def switch_to(session: dict) -> None:
    """Make `session` the one on screen. The outgoing one is stopped and saved first."""
    agent.stop()  # A turn must not carry on into a different scene.
    agent.end_click_asks(state.session()["id"])  # Nor wait off screen on a Turn it into 3D click's ask.
    conversations.save(state.session())
    state.replace(session)
    state.ui["view"] = "chat"
    from . import cloud_restore
    try:
        cloud_restore.pickup(session)  # Its cloud jobs, back behind job_status; a finished one gets its card.
    except Exception:  # Never a reason the conversation does not open.
        import traceback
        traceback.print_exc()
    from .ui import host
    host.tag_redraw_all()


def resume_for_open_file() -> None:
    current = state.session()
    project = conversations.project_key(bpy.data.filepath)
    if current["items"] and project in current["projects"]:
        # Already showing a conversation for this file, e.g. after a hot reload, or opened from History while
        # another file was open: its cloud jobs for this file are followed now (cloud_restore.pickup, once each).
        from . import cloud_restore
        try:
            cloud_restore.pickup(current)
        except Exception:  # Never a reason the file does not open.
            import traceback
            traceback.print_exc()
        return
    switch_to(conversations.resume_or_new(bpy.data.filepath))


@persistent
def _on_load_post(*_):
    if state.restoring:  # checkpoints.restore loads a file too; that is not the user switching files.
        _on_undo_redo()  # But what it loaded may lack a cloud job's outputs, or have them back.
        return
    resume_for_open_file()


@persistent
def _on_undo_redo(*_):
    """Undo after a cloud job's Load takes its outputs out of the file: its card offers Load again (Redo: loaded)."""
    from . import cloud_restore
    try:
        cloud_restore.recheck(state.session())
    except Exception:  # Never a reason an undo step fails.
        import traceback
        traceback.print_exc()


_saving_from = ""  # The file's path as a save starts (_on_save_pre), for Save As.


@persistent
def _on_save_pre(*_):
    global _saving_from
    _saving_from = bpy.data.filepath


@persistent
def _on_save_post(*_):
    if state.restoring:
        return
    # Save As, or the first save of an untitled scene: the conversation follows the file, and so do its cloud jobs
    # whose outputs are not in it yet (cloud_restore.file_saved).
    session = state.session()
    conversations.add_project(session, bpy.data.filepath)
    from . import cloud_restore
    try:
        cloud_restore.file_saved(session, _saving_from, bpy.data.filepath)
    except Exception:  # Never a reason a save is not recorded.
        import traceback
        traceback.print_exc()
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
    # Saved System add-ons must have finished loading before repairing incidental prefs saves.
    from . import bundled_addons
    try:
        bundled_addons.startup_cleanup()
    except (bundled_addons.BundledAddonError, OSError, ValueError):
        import traceback
        traceback.print_exc()  # Keep the journal, and leave the panel usable for recovery.
    resume_for_open_file()
    _dock_at_startup()
    return None


def _poll_jobs():
    """Each step on its own: one that raises (an OSError listing jobs) neither skips the others nor ends the timer,
    which only its return keeps registered."""
    import traceback
    from . import job_tools, workspace
    from .ui import host
    # poll_left: what a shell command left running, ended or due a Stop's SIGKILL.
    for step in (job_tools.refresh_ui, workspace.poll_left, host.tag_redraw_all):
        try:
            step()
        except Exception:
            traceback.print_exc()
    return 1.0


@persistent
def _on_exit(*_):
    from . import workspace
    workspace.stop_all_left()  # Blender quits: what a shell command left running goes with it.


def _handlers() -> tuple:
    from . import files
    return ((bpy.app.handlers.load_post, _on_load_post), (bpy.app.handlers.save_pre, _on_save_pre),
            (bpy.app.handlers.save_post, _on_save_post), (bpy.app.handlers.render_complete, files.on_render_complete),
            (bpy.app.handlers.undo_post, _on_undo_redo), (bpy.app.handlers.redo_post, _on_undo_redo),
            (bpy.app.handlers.exit_pre, _on_exit))


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
