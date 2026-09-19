"""Where the API key lives: one file in Blender's user config folder, readable only by the user.

Not in Blender's preferences: userpref.blend is a file people back up, sync and hand to others
to reproduce a bug, and a secret in it travels with it. Not in any .blend, not in the add-on
folder. The environment and a dev checkout's .env still work and take priority (see config.py).

Stored as plain text, like ssh keys and most CLI tokens are. An OS keychain would be better and
is not reachable from Blender's Python without shipping native wheels per platform.
"""

import json
import os
from pathlib import Path

FILE_NAME = "credentials.json"


def folder() -> Path:
    override = os.environ.get("LOOPCUT_CONFIG_DIR")
    if override:
        return Path(override)
    import bpy
    return Path(bpy.utils.user_resource("CONFIG", path="loopcut", create=True))


def load() -> dict[str, str]:
    """{base_url: api_key}. A missing or damaged file is no credentials, never a crash."""
    try:
        data = json.loads((folder() / FILE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    keys = data.get("api_keys") if isinstance(data, dict) else None
    return {k: v for k, v in keys.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(keys, dict) else {}


def api_key(base_url: str) -> str:
    return load().get(base_url.rstrip("/"), "")


def store(base_url: str, key: str) -> None:
    """Keys are per endpoint, so switching provider never sends one provider's key to another."""
    keys = load()
    base_url = base_url.rstrip("/")
    if key:
        keys[base_url] = key
    else:
        keys.pop(base_url, None)
    target = folder()
    target.mkdir(parents=True, exist_ok=True)
    path, temporary = target / FILE_NAME, target / (FILE_NAME + ".tmp")
    # Created with the final permissions, so the key is never on disk world-readable.
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump({"api_keys": keys}, handle, indent=1)
    os.replace(temporary, path)
