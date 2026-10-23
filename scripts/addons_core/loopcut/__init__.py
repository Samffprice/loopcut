"""Loopcut: an AI agent that works inside Blender."""

import os

# Read by Blender when Loopcut ships inside the Loopcut build as a core add-on. As an extension
# for stock Blender, blender_manifest.toml says the same.
bl_info = {
    "name": "Loopcut",
    "description": "An AI agent that works inside Blender",
    "author": "Loopcut",
    "version": (0, 2, 13),
    "blender": (5, 2, 0),
    "location": "Editor Type > Loopcut",
    "category": "Interface",
}


def register() -> None:
    try:
        from . import session_logs
        session_logs.register()  # First: the console lines conversation logs and problem reports carry.
    except Exception as ex:
        print(f"Loopcut: conversation logs not set up: {type(ex).__name__}")
    from . import account, cloud, lifecycle, mainthread, settings, support
    from .ui import host
    settings.register()
    support.register()
    mainthread.register()
    account.register()
    cloud.register()
    host.register()
    lifecycle.register()
    if host.NATIVE:  # The Loopcut build: its first run, and its updates. Stock Blender has its own.
        from . import onboarding, shaders, update
        onboarding.register()
        update.register()
        shaders.register()  # Subprocess shader compilation on Windows/Linux, once.
    try:
        from . import telemetry
        telemetry.register()  # app_start a few seconds later, off the startup path; nothing when switched off.
    except Exception as ex:
        print(f"Loopcut: usage stats not set up: {type(ex).__name__}")
    try:
        from . import checkpoints
        checkpoints.remove_stale_sessions()
    except OSError as ex:
        print(f"Loopcut: could not clean up old checkpoints: {ex}")
    try:
        from . import waves
        waves.reap()  # Workers and copies an earlier Blender left behind.
    except OSError as ex:
        print(f"Loopcut: could not clean up old worker jobs: {ex}")
    if os.environ.get("LOOPCUT_DEV") == "1":
        from . import dev_reload
        dev_reload.register()


def unregister() -> None:
    from . import (account, agent, cloud, dev_reload, lifecycle, mainthread, session_logs, settings, support, telemetry,
                   worker_host)
    from .ui import host
    agent.stop()
    telemetry.unregister()
    session_logs.unregister()  # The console's own streams back.
    worker_host.shutdown_all()
    cloud.unregister()
    account.unregister()
    if host.NATIVE:
        from . import onboarding, shaders, update
        shaders.unregister()
        update.unregister()
        onboarding.unregister()
    lifecycle.unregister()
    dev_reload.unregister()
    host.unregister()
    mainthread.unregister()
    support.unregister()
    settings.unregister()
