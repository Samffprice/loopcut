"""Loopcut: an AI agent that works inside Blender."""

import os

# Read by Blender when Loopcut ships inside the Loopcut build as a core add-on. As an extension
# for stock Blender, blender_manifest.toml says the same.
bl_info = {
    "name": "Loopcut",
    "description": "An AI agent that works inside Blender",
    "author": "Loopcut",
    "version": (0, 1, 4),
    "blender": (5, 2, 0),
    "location": "Editor Type > Loopcut",
    "category": "Interface",
}


def register() -> None:
    from . import account, lifecycle, mainthread, settings
    from .ui import host
    settings.register()
    mainthread.register()
    account.register()
    host.register()
    lifecycle.register()
    if host.NATIVE:  # The Loopcut build: its first run, and its updates. Stock Blender has its own.
        from . import onboarding, update
        onboarding.register()
        update.register()
    try:
        from . import checkpoints
        checkpoints.remove_stale_sessions()
    except OSError as ex:
        print(f"Loopcut: could not clean up old checkpoints: {ex}")
    if os.environ.get("LOOPCUT_DEV") == "1":
        from . import dev_reload
        dev_reload.register()


def unregister() -> None:
    from . import account, agent, dev_reload, lifecycle, mainthread, settings
    from .ui import host
    agent.stop()
    account.unregister()
    if host.NATIVE:
        from . import onboarding, update
        update.unregister()
        onboarding.unregister()
    lifecycle.unregister()
    dev_reload.unregister()
    host.unregister()
    mainthread.unregister()
    settings.unregister()
