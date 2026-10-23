"""Where the API key lives: one file in Blender's user config folder, readable only by the user.

Not in Blender's preferences: userpref.blend is a file people back up, sync and hand to others
to reproduce a bug, and a secret in it travels with it. Not in any .blend, not in the add-on
folder. The environment and a dev checkout's .env still work and take priority (see config.py).

Stored as plain text, like ssh keys and most CLI tokens are. An OS keychain would be better and
is not reachable from Blender's Python without shipping native wheels per platform.
"""

import json
import os
import tempfile
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

FILE_NAME = "credentials.json"
VENDOR_KEY_NAMES = ("OPENAI_API_KEY", "FAL_KEY", "GEMINI_API_KEY", "ELEVENLABS_API_KEY")
_WRITE_LOCK = threading.Lock()


def folder() -> Path:
    override = os.environ.get("LOOPCUT_CONFIG_DIR")
    if override:
        return Path(override)
    if threading.current_thread() is not threading.main_thread():
        from . import mainthread
        return mainthread.run_on_main(folder).result(timeout=30)
    import bpy
    return Path(bpy.utils.user_resource("CONFIG", path="loopcut", create=True))


def _read(*, target=None, strict=False) -> dict:
    try:
        data = json.loads(((target or folder()) / FILE_NAME).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        if strict:
            raise OSError("Could not read credentials; existing file was left unchanged") from None
        return {}
    if strict and (not isinstance(data, dict) or any(
            section in data and (not isinstance(data[section], dict)
                or any(not isinstance(k, str) or not isinstance(v, str) for k, v in data[section].items()))
            for section in ("api_keys", "vendor_keys"))):
        raise OSError("Invalid credentials file; existing file was left unchanged")
    return data if isinstance(data, dict) else {}


@contextmanager
def _transaction():
    """Lock the whole update, including across Blender processes sharing this config.

    The lock file is permanent: unlinking it would let waiters lock different inodes.
    Resolve Blender's config directory before locking so the UI never waits on itself.
    """
    target = folder()
    target.mkdir(parents=True, exist_ok=True)
    with _WRITE_LOCK:
        path = target / (FILE_NAME + ".lock")
        if path.is_symlink():
            raise OSError("Symlink in credentials lock path")
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "r+b") as handle:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield target
            finally:
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)


def _keys(data: dict, section: str) -> dict[str, str]:
    keys = data.get(section)
    return {k: v for k, v in keys.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(keys, dict) else {}


def load() -> dict[str, str]:
    """{base_url: api_key}. A missing or damaged file is no credentials, never a crash."""
    return _keys(_read(), "api_keys")


def api_key(base_url: str) -> str:
    return load().get(base_url.rstrip("/"), "")


def store(base_url: str, key: str) -> None:
    """Keys are per endpoint, so switching provider never sends one provider's key to another."""
    with _transaction() as target:
        data = _read(target=target, strict=True)
        keys = _keys(data, "api_keys")
        base_url = base_url.rstrip("/")
        if key:
            keys[base_url] = key
        else:
            keys.pop(base_url, None)
        data["api_keys"] = keys
        _write(data, target=target)


def pending_device(service_url: str) -> dict | None:
    """A pending device code is a secret too: keep it in the private credentials file, never a .blend."""
    record = _read().get("pending_device")
    return dict(record) if isinstance(record, dict) and record.get("service_url") == service_url else None


def store_pending_device(service_url: str, record: dict | None, *, cancelled=None) -> bool:
    with _transaction() as target:
        # Serialize cancellation with saving: a slow start reply cannot resurrect a cancelled code.
        if cancelled is not None and cancelled.is_set():
            return False
        data = _read(target=target, strict=True)
        if record is None:
            if isinstance(data.get("pending_device"), dict) and data["pending_device"].get("service_url") == service_url:
                data.pop("pending_device")
        else:
            data["pending_device"] = {**record, "service_url": service_url}
        _write(data, target=target)
        return True


def _write(data: dict, *, target=None, filename=FILE_NAME) -> None:
    target = target or folder()
    target.mkdir(parents=True, exist_ok=True)
    path = target / filename
    # Created with the final permissions, so the key is never on disk world-readable.
    descriptor, temporary = tempfile.mkstemp(prefix=filename + ".", suffix=".tmp", dir=target)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def interest_install_id() -> str | None:
    """Random identity used only by signed-out interest events, never by background workers.

    Share the config transaction lock so concurrent interactive processes reuse one ID.
    A damaged record can be replaced; a write failure is handled by the click's retry state.
    """
    import bpy
    if getattr(bpy.app, "background", True):  # Workers, and anything that cannot say it is interactive.
        return None
    with _transaction() as target:
        try:
            with (target / "install.json").open("rb") as handle:
                raw = handle.read(4097)
            if len(raw) > 4096:
                raise ValueError("Install record is too large")
            record = json.loads(raw)
            value = record.get("install_id") if isinstance(record, dict) else None
            if isinstance(value, str):
                parsed = uuid.UUID(value)
                if str(parsed) == value and parsed.version == 4:
                    return value
        except (OSError, ValueError, RecursionError):
            pass  # No account data lives here; a corrupt/unreadable ID is safe to replace.
        value = str(uuid.uuid4())
        _write({"install_id": value}, target=target, filename="install.json")
        return value


def _vendor_name(name: str) -> None:
    if name not in VENDOR_KEY_NAMES:
        raise ValueError("Unknown BYOK vendor key name")


def vendor_key(name: str) -> str:
    """Lookup order: process environment, then vendor_keys in credentials.json. No .env lookup."""
    _vendor_name(name)
    return os.environ.get(name, "").strip() or _keys(_read(), "vendor_keys").get(name, "").strip()


def set_vendor_key(name: str, key: str) -> None:
    """Save a vendor key without changing the endpoint keys used by existing integrations."""
    _vendor_name(name)
    with _transaction() as target:
        data = _read(target=target, strict=True)
        keys = _keys(data, "vendor_keys")
        key = key.strip()
        if key:
            keys[name] = key
        else:
            keys.pop(name, None)
        data["vendor_keys"] = keys
        _write(data, target=target)


def clear_vendor_key(name: str) -> None:
    """Clear the saved key; an environment variable still takes precedence."""
    set_vendor_key(name, "")


def vendor_keys_set() -> dict[str, bool]:
    """Only presence flags, including environment keys; safe for UI and catalog consumers."""
    return {name: bool(vendor_key(name)) for name in VENDOR_KEY_NAMES}
