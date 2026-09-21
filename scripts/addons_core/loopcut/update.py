"""Keeps the Loopcut build current: finds a newer release, downloads it in the background, and
swaps it in when the user restarts.

Each release carries an update.json (written by loopcut/scripts/publish_release.sh from the
checksums GitHub computes for the uploaded installers). This module fetches the one on the latest
release, compares its version with bl_info, streams the installer for this platform into a
temporary folder while checking its SHA-256, and on "Restart to update" hands over to a small
helper script that waits for Blender to exit, installs, and relaunches. Only a Loopcut that sits
where the installers put it (/Applications on Mac, Program Files on Windows) is swapped in place;
anywhere else the banner links to the download page.

Everything that does not touch Blender (versions, the manifest, the install target, the helper
scripts, the banner's words) is a plain function, so loopcut/tests/test_update.py runs it outside
Blender; bpy and the main-thread pump are imported where they are used.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = "Samffprice/loopcut"
# "off" disables the updater, e.g. for harness runs. http on 127.0.0.1 is for trying a local manifest.
MANIFEST_URL = os.environ.get("LOOPCUT_UPDATE_URL", f"https://github.com/{REPO}/releases/latest/download/update.json")
FIRST_CHECK_AFTER = 20.0      # Seconds after startup: the first check should not compete with loading.
CHECK_EVERY = 6 * 3600.0      # While Blender stays open.
_TIMEOUT = 15.0
_MANIFEST_MAX = 100_000       # Bytes; update.json is a few hundred.
_INSTALLER_MAX = 2_000_000_000
_CHUNK = 1 << 20
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
INSTALLER_SUFFIX = {"darwin-arm64": ".dmg", "darwin-x86_64": ".dmg", "windows-x64": ".msi"}

# status: idle | checking | current | available | downloading | ready | installing | error
# release: parsed manifest of a newer version, or None. path: the verified installer.
# error: with status "error" a failed download; otherwise the last failed check (prefs only).
_state = {"status": "idle", "release": None, "progress": 0.0, "path": "", "error": "",
          "dismissed": False, "checked_at": 0.0}
_cancel = threading.Event()
_lock = threading.Lock()   # One check or download at a time.


class UpdateError(RuntimeError):
    pass


# ---- Pure: versions, the manifest, where Loopcut is installed ----------------------------------

def parse_version(text) -> tuple[int, int, int]:
    text = str(text).strip()
    if text.startswith("v"):
        text = text[1:]
    if not _VERSION_RE.match(text):
        raise UpdateError(f"Not a version: {text!r}")
    major, minor, patch = (int(part) for part in text.split("."))
    return major, minor, patch


def label(version: tuple) -> str:
    return ".".join(str(part) for part in version)


def current_version() -> tuple[int, int, int]:
    from . import bl_info
    return tuple(bl_info["version"])


def platform_key(platform: str = sys.platform, machine: str = "") -> str:
    if not machine:
        import platform as platform_module
        machine = platform_module.machine()
    machine = machine.lower()
    if platform == "darwin":
        return "darwin-arm64" if machine in ("arm64", "aarch64") else "darwin-x86_64"
    if platform == "win32":
        return "windows-x64" if machine in ("amd64", "x86_64") else f"windows-{machine}"
    return f"{platform}-{machine}"


def _https(url, what: str) -> str:
    if not isinstance(url, str) or not (url.startswith("https://") or url.startswith("http://127.0.0.1")):
        raise UpdateError(f"update.json: {what} must be an https URL")  # http only for a local trial server.
    return url


def parse_manifest(payload, platform: str) -> dict:
    """The manifest as this platform needs it: {version, label, notes_url, asset}. asset is None
    when the release has no installer for this platform (the banner then links to notes_url)."""
    if not isinstance(payload, dict):
        raise UpdateError("update.json is not an object")
    version = parse_version(payload.get("version", ""))
    notes_url = _https(payload.get("notes_url"), "notes_url")
    assets = payload.get("assets")
    if not isinstance(assets, dict):
        raise UpdateError("update.json: assets must be an object")
    asset = assets.get(platform) if platform in INSTALLER_SUFFIX else None
    if asset is not None:
        if not isinstance(asset, dict):
            raise UpdateError(f"update.json: assets.{platform} must be an object")
        name, sha256, size = asset.get("name"), asset.get("sha256"), asset.get("size")
        if not isinstance(name, str) or not _NAME_RE.match(name) or not name.endswith(INSTALLER_SUFFIX[platform]):
            raise UpdateError(f"update.json: assets.{platform}.name is not an installer name")
        if not isinstance(sha256, str) or not _SHA256_RE.match(sha256):
            raise UpdateError(f"update.json: assets.{platform}.sha256 must be 64 hex digits")
        if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= _INSTALLER_MAX:
            raise UpdateError(f"update.json: assets.{platform}.size is out of range")
        asset = {"name": name, "url": _https(asset.get("url"), f"assets.{platform}.url"), "sha256": sha256,
                 "size": size}
    return {"version": version, "label": label(version), "notes_url": notes_url, "asset": asset}


def install_target(binary_path: str, platform: str = sys.platform, home: str = "", program_files: str = "") -> str:
    """What the helper replaces: the .app bundle on Mac, the executable to relaunch on Windows.
    Empty when this Loopcut is not where the installers put it (a dev build, a zip unpacked
    somewhere, the app run from its disk image), so an in-place update is not offered."""
    path = Path(binary_path)
    if platform == "darwin":
        bundle = next((p for p in path.parents if p.suffix == ".app"), None)
        if bundle is None:
            return ""
        home = home or os.path.expanduser("~")
        return str(bundle) if bundle.parent in (Path("/Applications"), Path(home) / "Applications") else ""
    if platform == "win32":
        program_files = program_files or os.environ.get("ProgramFiles", "")
        if not program_files:
            return ""
        try:
            path.relative_to(Path(program_files) / "Loopcut")
        except ValueError:
            return ""
        launcher = path.with_name("blender-launcher.exe")
        return str(launcher) if launcher.exists() else str(path)
    return ""


# ---- Pure: the helper scripts ------------------------------------------------------------------

# Arguments: the PID to wait for, the installer, the install target, a log file. Paths arrive as
# arguments and are only ever quoted, never pasted into the script text.
HELPER_SH = r"""#!/bin/sh
# Loopcut updater. Waits for Blender to quit, swaps the app bundle, relaunches. Written by update.py.
pid="$1"; dmg="$2"; app="$3"; log="$4"
exec >>"$log" 2>&1
echo "waiting for $pid"
n=0
while kill -0 "$pid" 2>/dev/null; do
  sleep 1; n=$((n + 1))
  [ "$n" -gt 86400 ] && { echo "gave up waiting"; exit 1; }
done
dir="$(dirname "$app")"
staged="$dir/.Loopcut-update.app"
old="$dir/.Loopcut-old.app"
mnt="$(mktemp -d "${TMPDIR:-/tmp}/loopcut-update.XXXXXX")"
fail() {
  echo "failed: $1"
  hdiutil detach "$mnt" -quiet 2>/dev/null
  rm -rf "$staged"
  [ -d "$old" ] && [ ! -d "$app" ] && mv "$old" "$app"
  osascript -e "display alert \"Loopcut could not update\" message \"$1 The log is at $log\"" >/dev/null 2>&1
  open -n "$app"
  exit 1
}
[ -f "$dmg" ] || fail "The downloaded installer is missing."
# The image carries a license agreement that hdiutil asks about; there is no terminal to answer, so yes does.
# -noverify: update.py checked the SHA-256 already.
yes | hdiutil attach -nobrowse -readonly -noautoopen -noverify -mountpoint "$mnt" "$dmg" || fail "The disk image could not be opened."
src="$(ls -d "$mnt"/*.app 2>/dev/null | head -1)"
[ -n "$src" ] || fail "The disk image holds no app."
rm -rf "$staged"
ditto "$src" "$staged" || fail "The new version could not be copied."
hdiutil detach "$mnt" -quiet || hdiutil detach "$mnt" -force -quiet
rm -rf "$old"
mv "$app" "$old" || fail "The installed app could not be moved aside."
mv "$staged" "$app" || fail "The new version could not be moved into place."
rm -rf "$old" "$dmg" "$mnt"
echo "installed $app"
open -n "$app"
"""

HELPER_CMD = r"""@echo off
rem Loopcut updater. Waits for Blender to quit, runs the installer, relaunches. Written by update.py.
set "pid=%~1"
set "msi=%~2"
set "exe=%~3"
set "log=%~4"
powershell -NoProfile -NonInteractive -Command "Wait-Process -Id %pid% -Timeout 86400 -ErrorAction SilentlyContinue"
if not exist "%msi%" goto fail
msiexec /i "%msi%" /passive /norestart /l*v "%log%"
if %errorlevel% equ 0 goto done
if %errorlevel% equ 3010 goto done
goto fail
:done
del "%msi%"
if not exist "%exe%" for /d %%d in ("%ProgramFiles%\Loopcut\*") do if exist "%%d\blender-launcher.exe" set "exe=%%d\blender-launcher.exe"
start "" "%exe%"
exit /b 0
:fail
start "" notepad.exe "%log%"
start "" "%exe%"
exit /b 1
"""


def helper_script(platform: str = sys.platform) -> tuple[str, str]:
    """(file name, contents) of the helper for `platform`."""
    if platform == "darwin":
        return "apply.sh", HELPER_SH
    if platform == "win32":
        return "apply.cmd", HELPER_CMD
    raise UpdateError(f"No updater for {platform}")


def helper_command(script: str, pid: int, installer: str, target: str, log: str,
                   platform: str = sys.platform) -> list[str]:
    args = [str(pid), installer, target, log]
    if platform == "darwin":
        return ["/bin/sh", script, *args]
    return ["cmd.exe", "/c", script, *args]


# ---- Pure: what the panel and the preferences say ----------------------------------------------

def banner_for(state: dict, installable: bool) -> dict | None:
    """The one-line notice under the header: {text, button: (label, action) | None, dismiss,
    progress}. None when there is nothing to say. `installable`: whether Restart to update can
    do its job here; otherwise the buttons lead to the download page."""
    release, status = state["release"], state["status"]
    if release is None or state["dismissed"] or status in ("idle", "checking", "current"):
        return None
    version = release["label"]
    if status == "downloading":
        return {"text": f"Downloading Loopcut {version}… {int(state['progress'] * 100)}%", "button": None,
                "dismiss": False, "progress": state["progress"]}
    if status == "ready":
        return {"text": f"Loopcut {version} is ready.", "button": ("Restart to update", "update_restart"),
                "dismiss": True, "progress": None}
    if status == "installing":
        return {"text": f"Loopcut {version} installs when Blender closes.", "button": None, "dismiss": False,
                "progress": None}
    in_place = installable and release["asset"] is not None
    if status == "error":
        button = ("Try again", "update_download") if in_place else ("Download", "update_page")
        return {"text": f"Update failed: {state['error']}", "button": button, "dismiss": True, "progress": None}
    button = ("Update", "update_download") if in_place else ("Download", "update_page")
    return {"text": f"Loopcut {version} is available.", "button": button, "dismiss": True, "progress": None}


def status_line(state: dict, current: tuple) -> str:
    """One line for the preferences page."""
    status, release = state["status"], state["release"]
    version = release["label"] if release else ""
    if status == "checking":
        return "Checking for updates…"
    if status == "current":
        return f"Loopcut {label(current)} is up to date."
    if status == "available":
        return f"Loopcut {version} is available."
    if status == "downloading":
        return f"Downloading Loopcut {version}… {int(state['progress'] * 100)}%"
    if status == "ready":
        return f"Loopcut {version} is downloaded. Restart to update."
    if status == "installing":
        return f"Loopcut {version} installs when Blender closes."
    if status == "error":
        return f"Update failed: {state['error']}"
    if state["error"]:
        return f"Could not check for updates: {state['error']}"
    return f"Loopcut {label(current)}"


# ---- Network and files, on worker threads ------------------------------------------------------

def _download_dir() -> Path:
    path = Path(tempfile.gettempdir()) / "loopcut-update"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _fetch_manifest() -> dict:
    if not (MANIFEST_URL.startswith("https://") or MANIFEST_URL.startswith("http://127.0.0.1")):
        raise UpdateError("LOOPCUT_UPDATE_URL must be https (http is allowed only for 127.0.0.1)")
    request = urllib.request.Request(MANIFEST_URL, headers={"Accept": "application/json", "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            payload = json.loads(response.read(_MANIFEST_MAX))
    except urllib.error.HTTPError as ex:
        raise UpdateError(f"HTTP {ex.code} fetching update.json") from ex
    except (urllib.error.URLError, TimeoutError, ValueError) as ex:
        raise UpdateError(f"Could not fetch update.json: {ex}") from ex
    return parse_manifest(payload, platform_key())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stream(asset: dict, part: Path) -> None:
    """The installer into `part`, checked against the manifest's size and SHA-256 as it arrives."""
    request = urllib.request.Request(asset["url"], headers={"Accept": "application/octet-stream"})
    digest, received, reported = hashlib.sha256(), 0, -1
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response, open(part, "wb") as out:
            length = response.headers.get("Content-Length", "")
            if length.isdigit() and int(length) != asset["size"]:
                raise UpdateError(f"{asset['name']} is {length} bytes, update.json says {asset['size']}")
            while chunk := response.read(_CHUNK):
                if _cancel.is_set():
                    raise UpdateError("Cancelled")
                received += len(chunk)
                if received > asset["size"]:
                    raise UpdateError(f"{asset['name']} is larger than update.json says")
                digest.update(chunk)
                out.write(chunk)
                percent = received * 100 // asset["size"]
                if percent != reported:
                    reported = percent
                    _set(progress=received / asset["size"])
    except urllib.error.HTTPError as ex:
        raise UpdateError(f"HTTP {ex.code} downloading {asset['name']}") from ex
    except (urllib.error.URLError, TimeoutError, OSError) as ex:
        raise UpdateError(f"Could not download {asset['name']}: {ex}") from ex
    if received != asset["size"]:
        raise UpdateError(f"{asset['name']} ended early ({received} of {asset['size']} bytes)")
    if digest.hexdigest() != asset["sha256"]:
        raise UpdateError(f"{asset['name']} does not match its checksum")


def _download(asset: dict) -> Path:
    """The verified installer in the download folder, fetched unless it is already there."""
    folder = _download_dir()
    for stale in folder.iterdir():  # Other versions, half downloads, old helpers and logs.
        if stale.name != asset["name"]:
            try:
                stale.unlink()
            except OSError:
                pass
    final = folder / asset["name"]
    if final.is_file() and _sha256(final) == asset["sha256"]:
        return final  # Downloaded by an earlier session.
    part = folder / (asset["name"] + ".part")
    try:
        _stream(asset, part)
    except UpdateError:
        part.unlink(missing_ok=True)
        raise
    part.replace(final)
    return final


def _set(**fields) -> None:
    _state.update(fields)
    from . import mainthread
    mainthread.request_redraw()


def _run_check(then_download: bool) -> None:
    if not _lock.acquire(blocking=False):
        return  # A check or download is under way.
    try:
        _set(status="checking", error="")
        try:
            release = _fetch_manifest()
        except UpdateError as ex:
            print(f"Loopcut: update check failed: {ex}")
            _set(status="idle" if _state["release"] is None else "available", error=str(ex), checked_at=time.time())
            return
        _state["checked_at"] = time.time()
        if release["version"] <= current_version():
            _set(status="current", release=None, path="")
            return
        previous = _state["release"]
        if previous is None or previous["version"] != release["version"]:
            _set(release=release, dismissed=False, path="", progress=0.0)
        if _state["path"] and Path(_state["path"]).is_file():
            _set(status="ready")
        elif then_download and release["asset"] and installable():
            _download_locked()
        else:
            _set(status="available")
    finally:
        _lock.release()


def _download_locked() -> None:
    release = _state["release"]
    _set(status="downloading", progress=0.0, error="")
    try:
        path = _download(release["asset"])
    except UpdateError as ex:
        print(f"Loopcut: update download failed: {ex}")
        _set(status="error", error=str(ex), progress=0.0)
        return
    _set(status="ready", path=str(path), progress=1.0)


def _run_download() -> None:
    if not _lock.acquire(blocking=False):
        return
    try:
        if _state["release"] and _state["release"]["asset"]:
            _download_locked()
    finally:
        _lock.release()


# ---- Entry points, main thread -----------------------------------------------------------------

def state() -> dict:
    return dict(_state)


def installable() -> bool:
    import bpy
    return bool(install_target(bpy.app.binary_path))


def banner() -> dict | None:
    return banner_for(_state, installable())


def preferences_line() -> str:
    return status_line(_state, current_version())


def check(then_download: bool | None = None) -> None:
    """Look for a newer release on a worker thread. then_download defaults to the preference."""
    if MANIFEST_URL == "off":
        return
    if then_download is None:
        then_download = _auto_update()
    threading.Thread(target=_run_check, args=(then_download,), name="loopcut-update-check", daemon=True).start()


def download() -> None:
    threading.Thread(target=_run_download, name="loopcut-update-download", daemon=True).start()


def dismiss() -> None:
    _set(dismissed=True)


def open_notes() -> None:
    from . import account
    if _state["release"]:
        account.open_browser(_state["release"]["notes_url"])


def restart_to_update() -> None:
    """Start the helper, then ask Blender to quit the usual way (it offers to save first). If the
    user cancels, the helper still waits: the update lands whenever Blender next closes."""
    import bpy
    path = _state["path"]
    if _state["status"] != "ready" or not path or not Path(path).is_file():
        _set(status="error", error="The downloaded installer is missing.", path="")
        return
    target = install_target(bpy.app.binary_path)
    if not target:
        _set(status="error", error="Loopcut is not installed where the updater can replace it.")
        return
    folder = _download_dir()
    name, contents = helper_script()
    script, log = folder / name, folder / "update.log"
    try:
        script.write_text(contents, encoding="utf-8", newline="\r\n" if sys.platform == "win32" else "\n")
        script.chmod(0o700)
        if sys.platform == "win32":
            detach = {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
        else:
            detach = {"start_new_session": True}
        subprocess.Popen(helper_command(str(script), os.getpid(), path, target, str(log)),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True, **detach)
    except OSError as ex:
        _set(status="error", error=f"Could not start the updater: {ex}")
        return
    _set(status="installing")

    def quit_blender():
        # From a timer, after the click's operator has returned: quitting from inside it would
        # free the operator under its own feet. A window is needed for the save prompt.
        windows = bpy.context.window_manager.windows
        if windows:
            with bpy.context.temp_override(window=windows[0]):
                bpy.ops.wm.quit_blender("INVOKE_DEFAULT")
        return None
    bpy.app.timers.register(quit_blender, first_interval=0.05)


def _auto_update() -> bool:
    from . import settings
    prefs = settings.preferences()
    return True if prefs is None else bool(prefs.auto_update)


def _tick() -> float:
    import bpy
    if not bpy.app.background and _auto_update() and _state["status"] in ("idle", "current", "available", "error"):
        check()
    return CHECK_EVERY


def register() -> None:
    import bpy
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    _cancel.clear()
    if not bpy.app.timers.is_registered(_tick):
        bpy.app.timers.register(_tick, first_interval=FIRST_CHECK_AFTER, persistent=True)


def unregister() -> None:
    import bpy
    _cancel.set()
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)


def _operators() -> tuple:
    import bpy

    class LOOPCUT_OT_check_for_updates(bpy.types.Operator):
        """Look for a newer Loopcut now"""
        bl_idname = "loopcut.check_for_updates"
        bl_label = "Check for Updates"
        bl_options = {"INTERNAL"}

        def execute(self, context):
            check()
            return {"FINISHED"}

    class LOOPCUT_OT_restart_to_update(bpy.types.Operator):
        """Close Blender, install the downloaded Loopcut, and open it again"""
        bl_idname = "loopcut.restart_to_update"
        bl_label = "Restart to Update"
        bl_options = {"INTERNAL"}

        def execute(self, context):
            restart_to_update()
            return {"FINISHED"}

    return LOOPCUT_OT_check_for_updates, LOOPCUT_OT_restart_to_update


try:
    _CLASSES = _operators()
except ImportError:  # Outside Blender (tests).
    _CLASSES = ()
