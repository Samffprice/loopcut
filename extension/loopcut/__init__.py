"""Loopcut: an AI agent that works inside Blender."""

import os


def register() -> None:
    from . import mainthread
    from .ui import host
    mainthread.register()
    host.register()
    try:
        from . import checkpoints
        checkpoints.remove_stale_sessions()
    except OSError as ex:
        print(f"Loopcut: could not clean up old checkpoints: {ex}")
    if os.environ.get("LOOPCUT_DEV") == "1":
        from . import dev_reload
        dev_reload.register()


def unregister() -> None:
    from . import agent, dev_reload, mainthread
    from .ui import host
    agent.stop()
    dev_reload.unregister()
    host.unregister()
    mainthread.unregister()
