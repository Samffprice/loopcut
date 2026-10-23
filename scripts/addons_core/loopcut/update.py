"""Keeps the Loopcut build current: finds a newer release, downloads it in the background, and
installs it after Blender exits. Verified downloads survive a restart.

Each release carries an update.json (written by loopcut/scripts/publish_release.sh from the
checksums GitHub computes for the uploaded installers). This module fetches the one on the latest
release, compares its version with bl_info, streams the installer for this platform into a
private cache while checking its SHA-256. A detached helper waits for Blender to exit, then
installs without relaunching unless a restart was requested. A cached update on a clean startup
gets a cancellable countdown; its durable restart guard prevents failed-install restart loops.
Only installer-managed locations are updated in place; other installs link to the download page.

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
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from pathlib import Path, PurePosixPath, PureWindowsPath

from . import config

REPO = "Samffprice/loopcut"
# "off" disables the updater, e.g. for harness runs. http on 127.0.0.1 is for trying a local manifest.
MANIFEST_URL = os.environ.get("LOOPCUT_UPDATE_URL", f"https://github.com/{REPO}/releases/latest/download/update.json")
STARTUP_NOTICE_SECONDS = 10.0
RETRY_AFTER = 24 * 3600.0
FIRST_CHECK_AFTER = 20.0      # Seconds after startup: the first check should not compete with loading.
CHECK_EVERY = 6 * 3600.0      # While Blender stays open.
HELPER_ACK_TIMEOUT = 30.0     # Cold Windows PowerShell/module startup and antivirus can take seconds.
_TIMEOUT = 15.0
_MANIFEST_MAX = 100_000       # Bytes; update.json is a few hundred.
_INSTALLER_MAX = 2_000_000_000
_CHUNK = 1 << 20
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
INSTALLER_SUFFIX = {"darwin-arm64": ".dmg", "darwin-x86_64": ".dmg", "windows-x64": ".msi"}

# status: idle | checking | current | available | downloading | preparing | ready | installing | applying | waiting | error
# release: parsed manifest of a newer version, or None. path: the verified installer.
# error: with status "error" a failed download; otherwise the last failed check (prefs only).
_state = {"status": "idle", "release": None, "progress": 0.0, "path": "", "error": "",
          "dismissed": False, "checked_at": 0.0, "critical": False, "armed": False,
          "pickup": False, "automatic_restart": False, "legacy_fallback": False}
_pending = {}
_cache = None
_target = ""
_job = None
_helper_process = None
_pickup_until = 0.0
_started_at = 0.0
_automatic = True
_cancel = threading.Event()
_lock = threading.Lock()   # One check or download at a time.


class UpdateError(RuntimeError):
    pass


# ---- Pure: versions, the manifest, where Loopcut is installed ----------------------------------

def parse_version(text) -> tuple[int, int, int]:
    text = str(text).strip()
    if text.startswith("v"):
        text = text[1:]
    if len(text) > 20 or not _VERSION_RE.fullmatch(text):
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
    try:
        parts = urlsplit(url) if isinstance(url, str) else None
        valid = parts and parts.hostname and not parts.username and not parts.password and (
            parts.scheme == "https" or (parts.scheme == "http" and parts.hostname == "127.0.0.1"))
    except ValueError:
        valid = False
    if not valid:
        raise UpdateError(f"update.json: {what} must be an https URL")
    return url


def parse_manifest(payload, platform: str) -> dict:
    """The manifest as this platform needs it: {version, label, notes_url, asset}. asset is None
    when the release has no installer for this platform (the banner then links to notes_url)."""
    if not isinstance(payload, dict):
        raise UpdateError("update.json is not an object")
    version = parse_version(payload.get("version", ""))
    minimum = parse_version(payload["min_version"]) if "min_version" in payload else None
    if minimum and minimum > version:
        raise UpdateError("update.json: min_version is newer than version")
    headline = payload.get("headline", "")
    if not isinstance(headline, str) or len(headline) > 120 or any(ord(c) < 32 for c in headline):
        raise UpdateError("update.json: headline must be one short line (at most 120 characters)")
    notes_url = _https(payload.get("notes_url"), "notes_url")
    assets = payload.get("assets")
    if not isinstance(assets, dict):
        raise UpdateError("update.json: assets must be an object")
    asset = assets.get(platform) if platform in INSTALLER_SUFFIX else None
    if asset is not None:
        if not isinstance(asset, dict):
            raise UpdateError(f"update.json: assets.{platform} must be an object")
        name, sha256, size = asset.get("name"), asset.get("sha256"), asset.get("size")
        if not isinstance(name, str) or not _NAME_RE.fullmatch(name) or not name.endswith(INSTALLER_SUFFIX[platform]):
            raise UpdateError(f"update.json: assets.{platform}.name is not an installer name")
        if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
            raise UpdateError(f"update.json: assets.{platform}.sha256 must be 64 hex digits")
        if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= _INSTALLER_MAX:
            raise UpdateError(f"update.json: assets.{platform}.size is out of range")
        asset = {"name": name, "url": _https(asset.get("url"), f"assets.{platform}.url"), "sha256": sha256,
                 "size": size}
    return {"version": version, "label": label(version), "notes_url": notes_url, "asset": asset,
            "min_version": minimum, "headline": headline}


def install_target(binary_path: str, platform: str = sys.platform, home: str = "", program_files: str = "") -> str:
    """What the helper replaces: the .app bundle on Mac, the executable to relaunch on Windows.
    Empty when this Loopcut is not where the installers put it (a dev build, a zip unpacked
    somewhere, the app run from its disk image), so an in-place update is not offered."""
    if platform == "darwin":
        path = PurePosixPath(binary_path)
        bundle = next((p for p in path.parents if p.suffix == ".app"), None)
        if bundle is None:
            return ""
        home = home or os.path.expanduser("~")
        return str(bundle) if bundle.parent in (PurePosixPath("/Applications"), PurePosixPath(home) / "Applications") else ""
    if platform == "win32":
        path = PureWindowsPath(binary_path)
        program_files = program_files or os.environ.get("ProgramFiles", "")
        if not program_files:
            return ""
        try:
            path.relative_to(PureWindowsPath(program_files) / "Loopcut")
        except ValueError:
            return ""
        launcher = path.with_name("blender-launcher.exe")
        return str(launcher) if Path(str(launcher)).is_file() else str(path)
    return ""


# ---- Pure: the helper scripts ------------------------------------------------------------------

# Paths are data: POSIX argv on Mac, a JSON job on Windows. No cmd.exe interpolation.
HELPER_SH = r"""#!/bin/sh
pid="$1"; dmg="$2"; app="$3"; log="$4"; restart="$5"; result="$6"
version="$7"; from="$8"; sha="$9"; size="${10}"; ack="${11}"
exec >>"$log" 2>&1
touch "$ack" || exit 1
while kill -0 "$pid" 2>/dev/null; do sleep 1; done
# A lock shared by all helpers prevents two open Blenders from swapping the same app.
lock="$(dirname "$result")/install.lock"
if ! mkdir "$lock" 2>/dev/null; then
  owner="$(cat "$lock/pid" 2>/dev/null)"
  if [ -n "$owner" ]; then
    kill -0 "$owner" 2>/dev/null && exit 0
  else
    # A killed helper may have made the directory but not yet written its PID.
    age="$(( $(date +%s) - $(stat -f %m "$lock") ))"
    [ "$age" -ge 60 ] || exit 0
  fi
  rm -f "$lock/pid"; rmdir "$lock" 2>/dev/null || exit 0
  mkdir "$lock" 2>/dev/null || exit 0
fi
echo "$$" > "$lock/pid"
staged="$app.loopcut-new.app"
mnt=""
write_result() {
  printf '{"version":"%s","from_version":"%s","status":"%s","reason":"%s","attempted_at":%s,"helper_pid":%s}\n' \
    "$version" "$from" "$1" "$2" "$(date +%s)" "$$" > "$result.tmp"
  mv "$result.tmp" "$result"
}
relaunch() {
  if [ -f "$restart" ]; then
    open -n "$app"
  fi
}
cleanup() {
  [ -z "$mnt" ] || { hdiutil detach "$mnt" -quiet 2>/dev/null; rmdir "$mnt" 2>/dev/null; }
  rm -f "$lock/pid"; rmdir "$lock" 2>/dev/null
}
fail() {
  echo "update failed: $1"
  rm -rf "$staged"
  write_result failed "$1"
  cleanup
  relaunch
  exit 1
}
trap 'fail interrupted' HUP INT TERM
last="$(sed -n 's/.*"attempted_at"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$result" 2>/dev/null)"
if [ -n "$last" ] && [ "$(( $(date +%s) - last ))" -lt 86400 ]; then cleanup; exit 0; fi
write_result installing interrupted || { cleanup; relaunch; exit 1; }
[ -f "$dmg" ] || fail missing
[ "$(stat -f %z "$dmg")" = "$size" ] || fail size
actual="$(shasum -a 256 "$dmg")" || fail checksum
[ "${actual%% *}" = "$sha" ] || fail checksum
[ -d "$app" ] || fail missing_app
mnt="$(mktemp -d "${TMPDIR:-/tmp}/loopcut-update.XXXXXX")" || fail mount
# The image's license agreement needs a yes; the checksum was checked again above.
yes | hdiutil attach -nobrowse -readonly -noautoopen -mountpoint "$mnt" "$dmg" || fail mount
src=""
for candidate in "$mnt"/*.app; do [ ! -d "$candidate" ] || { src="$candidate"; break; }; done
[ -n "$src" ] || fail missing_app
rm -rf "$staged" || fail permissions
ditto "$src" "$staged" || fail copy
[ -x "$staged/Contents/MacOS/Loopcut" ] || [ -x "$staged/Contents/MacOS/Blender" ] || fail invalid_app
hdiutil detach "$mnt" -quiet || fail unmount
rmdir "$mnt"; mnt=""
# RENAME_SWAP is atomic for directories on supported macOS volumes. If it is unavailable or
# denied, leave the original app untouched; never fall back to a two-rename gap.
/usr/bin/osascript -l JavaScript - "$staged" "$app" <<'JS' || fail swap
ObjC.bindFunction('renamex_np', ['int', ['char *', 'char *', 'unsigned int']]);
function run(args) {
    if ($.renamex_np(args[0], args[1], 2) !== 0) throw Error('Atomic app swap failed');
}
JS
write_result applied ok
rm -rf "$staged"
cleanup
relaunch
"""

HELPER_PS = r'''param([Parameter(Mandatory=$true)][string]$JobFile)
$ErrorActionPreference = 'Stop'
# stdout/stderr share helper.log, opened by the parent. Avoid a competing file writer.
$helperOutput = [Console]::OpenStandardOutput()
function Log([string]$message) {
    # Write UTF-8 bytes directly: a detached helper has no console code page to change.
    $line = [Text.Encoding]::UTF8.GetBytes([DateTime]::UtcNow.ToString('o') + ' ' + $message + [Environment]::NewLine)
    $helperOutput.Write($line, 0, $line.Length)
    $helperOutput.Flush()
}
try {
    Log ('start PowerShell=' + $PSVersionTable.PSVersion.ToString() + ' edition=' + $PSVersionTable.PSEdition)
    Log 'reading job file'
    $jobJson = [IO.File]::ReadAllText($JobFile, [Text.Encoding]::UTF8)
    Log 'job file read; parsing JSON'
    $job = $jobJson | ConvertFrom-Json
    # Acknowledge immediately after parsing, before waiting for Blender or touching the installer.
    [IO.File]::WriteAllText($job.ack, 'waiting')
    Log 'acknowledged; waiting for Blender to close'
    $msiExitCode = $null
    function Result([string]$status, [string]$reason) {
        $record = @{version=$job.version; from_version=$job.from_version; status=$status;
            reason=$reason; attempted_at=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds(); helper_pid=$PID}
        if ($null -ne $msiExitCode) { $record.exit_code = $msiExitCode }
        $tmp = $job.result + '.tmp'
        [IO.File]::WriteAllText($tmp, ($record | ConvertTo-Json -Compress), (New-Object Text.UTF8Encoding($false)))
        Move-Item -LiteralPath $tmp -Destination $job.result -Force
        Log ('result=' + $status + ' reason=' + $reason + ' msi_exit_code=' + $msiExitCode)
    }
    function Relaunch {
        if (Test-Path -LiteralPath $job.restart) {
            $exe = $job.target
            if (-not (Test-Path -LiteralPath $exe)) {
                $exe = Get-ChildItem -LiteralPath (Join-Path $env:ProgramFiles 'Loopcut') -Directory |
                    ForEach-Object { Join-Path $_.FullName 'blender-launcher.exe' } |
                    Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
            }
            if ($exe) {
                Log 'relaunch requested'
                Start-Process -FilePath $exe
            }
        }
    }
    # Wait-Process can fail on an already exited PID; check again instead of timing out into an install.
    while (Get-Process -Id $job.pid -ErrorAction SilentlyContinue) { Start-Sleep -Seconds 1 }
    if (Test-Path -LiteralPath ($JobFile + '.cancel')) { Log 'cancelled before install'; exit 0 }
    Log 'Blender closed; acquiring install lock'
    $mutex = New-Object Threading.Mutex($false, 'Local\LoopcutUpdateInstall')
    $locked = $false
    try {
        try { $locked = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $locked = $true }
        if (-not $locked) { Log 'another helper owns the install lock'; exit 0 }
        Log 'install lock acquired; checking cooldown'
        if (Test-Path -LiteralPath $job.result) {
            $last = Get-Content -LiteralPath $job.result -Raw -Encoding UTF8 | ConvertFrom-Json
            if (([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - $last.attempted_at) -lt 86400) {
                Log 'skipping recent install attempt'; exit 0
            }
        }
        Result 'installing' 'interrupted'
        Log 'verifying installer size'
        $file = Get-Item -LiteralPath $job.installer
        if ($file.Length -ne $job.size) { throw 'size' }
        Log 'installer size verified; hashing SHA-256'
        if ((Get-FileHash -LiteralPath $job.installer -Algorithm SHA256).Hash.ToLowerInvariant() -ne $job.sha256) {
            throw 'checksum'
        }
        Log 'installer SHA-256 verified'
        # /passive displays installer progress. RunAs explicitly requests the Program Files elevation.
        # ArgumentList is ONE string with quoted paths; it is never evaluated as PowerShell or cmd text.
        $arguments = '/i "' + $job.installer + '" /passive /norestart /l*v "' + $job.log + '"'
        Log 'starting elevated MSI'
        $process = Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\msiexec.exe') `
            -ArgumentList $arguments -Verb RunAs -Wait -PassThru
        $msiExitCode = [int]$process.ExitCode
        Log ('MSI exited with code=' + $msiExitCode)
        if ($msiExitCode -notin @(0, 3010)) { throw ('msi_' + $msiExitCode) }
        Result 'applied' 'ok'
    } catch {
        # Includes UAC cancellation. MSI owns rollback; never remove or rename the installed executable.
        Log ('install error: ' + $_.Exception.ToString() + [Environment]::NewLine +
            $_.InvocationInfo.PositionMessage + [Environment]::NewLine + $_.ScriptStackTrace)
        $reason = 'install'
        if ($_.Exception.Message -match '^(size|checksum|msi_[0-9]+)$') { $reason = $_.Exception.Message }
        Result 'failed' $reason
    } finally {
        if ($locked) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
    Relaunch
    Log 'finished'
} catch {
    Log ('fatal error: ' + $_.Exception.ToString() + [Environment]::NewLine +
        $_.InvocationInfo.PositionMessage + [Environment]::NewLine + $_.ScriptStackTrace)
    throw
}
'''


# 0.2.9's helper, for an explicit restart when the PowerShell helper can't start. One change: its wait.
# 0.2.4-0.2.10 ran Wait-Process -Timeout 86400, above the cmdlet's 32767 maximum, so parameter binding
# failed, nothing waited and msiexec started while Blender was still open. It now polls, still up to 24 h.
HELPER_CMD = r"""@echo off
rem Loopcut updater. Waits for Blender to quit, runs the installer, relaunches. Written by update.py.
set "pid=%~1"
set "msi=%~2"
set "exe=%~3"
set "log=%~4"
powershell -NoProfile -NonInteractive -Command "$n = 0; while ((Get-Process -Id %pid% -ErrorAction SilentlyContinue) -and $n -lt 86400) { Start-Sleep -Seconds 1; $n++ }"
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
    if platform == "darwin":
        return "apply.sh", HELPER_SH
    if platform == "win32":
        return "apply.ps1", HELPER_PS
    raise UpdateError(f"No updater for {platform}")


def helper_command(script: str, job: dict, job_path: str, platform: str = sys.platform) -> list[str]:
    if platform == "darwin":
        keys = ("pid", "installer", "target", "log", "restart", "result", "version", "from_version", "sha256", "size", "ack")
        return ["/bin/sh", script, *(str(job[key]) for key in keys)]
    if platform == "win32":
        return ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", script, "-JobFile", job_path]
    raise UpdateError(f"No updater for {platform}")


def windows_helper_flags() -> int:
    """A hidden console of the helper's own, in its own process group. On a real Windows runner the same
    PowerShell 5.1 helper launched with DETACHED_PROCESS (no console at all) died within 2 s without output,
    while the launch without it ran to the end. The legacy cmd helper keeps 0.2.9's DETACHED_PROCESS."""
    return subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP


def legacy_helper_command(script: str, pid: int, installer: str, target: str, log: str) -> list[str]:
    """The 0.2.9 command, including its cmd.exe /c launch form and positional arguments."""
    return ["cmd.exe", "/c", script, str(pid), installer, target, log]


def helper_environment(environ=None) -> dict:
    """Let Windows PowerShell 5.1 build its own module path instead of inheriting pwsh 7's.

    pwsh only sanitizes direct PowerShell children; Blender/Python is an intermediate process.
    Preserve the rest of the environment, including the native executable search path.
    """
    source = os.environ if environ is None else environ
    return {key: value for key, value in source.items() if key.upper() != "PSMODULEPATH"}


def startup_eligible(pending: dict, current: tuple, *, dirty: bool, filepath: str, argv: list, busy=False) -> bool:
    return (parse_version(pending["manifest"]["version"]) > current and
            not pending.get("auto_restart_attempted", False) and not pending.get("legacy_fallback", False) and not dirty and not filepath and not busy and
            not any(arg.lower().endswith(".blend") or arg in
                    ("-b", "--background", "-P", "--python", "--python-expr", "--python-console", "--command") for arg in argv[1:]))


def retry_due(result: dict, version: str, now: float) -> bool:
    return result.get("version") != version or now - result.get("attempted_at", 0) >= RETRY_AFTER


# ---- Pure: what the panel and the preferences say ----------------------------------------------

def banner_for(state: dict, installable: bool) -> dict | None:
    """The one-line notice under the header: {text, button: (label, action) | None, dismiss,
    progress}. None when there is nothing to say. `installable`: whether Restart to update can
    do its job here; otherwise the buttons lead to the download page."""
    release, status = state["release"], state["status"]
    critical = state.get("critical", False)
    if release is None or (state["dismissed"] and not critical) or status in ("idle", "checking", "current"):
        return None
    version = config.version_label(release["version"])
    headline = release.get("headline", "") if critical else ""
    def words(text):
        return f"{headline}. {text}" if headline else text
    if state.get("pickup"):
        return {"text": f"Updating Loopcut to {version}…", "button": ("Not now", "update_defer"),
                "dismiss": False, "progress": None}
    if not installable or release["asset"] is None:
        reason = ("Move Loopcut to Applications to update here." if sys.platform == "darwin" else
                  "Install Loopcut in Program Files to update here." if sys.platform == "win32" else
                  "This build can't update itself; download it from loopcut.org.") if not installable else "Download it from loopcut.org."
        return {"text": words(f"Loopcut {version} is available. {reason}"), "button": ("Download", "update_page"),
                "dismiss": not critical, "progress": None}
    if status == "downloading":
        return {"text": words(f"Downloading Loopcut {version}… {int(state['progress'] * 100)}%"), "button": None,
                "dismiss": False, "progress": state["progress"]}
    if status == "preparing":
        return {"text": words(f"Preparing Loopcut {version} to install…"), "button": None,
                "dismiss": False, "progress": None}
    if status == "ready" and state.get("legacy_fallback"):
        return {"text": words(f"Loopcut {version} is ready."), "button": ("Restart to update", "update_restart"),
                "dismiss": not critical, "progress": None}
    if status == "ready":
        return {"text": words(f"Loopcut {version} installs when you close Blender."), "button": ("Restart now", "update_restart"),
                "dismiss": not critical, "progress": None}
    if status == "installing":
        return {"text": words(f"Restarting to install Loopcut {version}…"), "button": None, "dismiss": False,
                "progress": None}
    in_place = installable and release["asset"] is not None
    if status == "error":
        button = ("Try again", "update_download") if in_place else ("Download", "update_page")
        return {"text": words("The update couldn't finish. You can keep working."), "button": button, "dismiss": not critical, "progress": None}
    if status == "waiting":
        return {"text": words("The update couldn't finish. We'll try again tomorrow."),
                "button": ("Download", "update_page"), "dismiss": not critical, "progress": None}
    if status == "applying":
        return {"text": words(f"Loopcut {version} is updating. You can keep working."),
                "button": None, "dismiss": not critical, "progress": None}
    button = ("Update", "update_download") if in_place else ("Download", "update_page")
    return {"text": words(f"Loopcut {version} is available."), "button": button, "dismiss": not critical, "progress": None}


def status_line(state: dict, current: tuple) -> str:
    """One line for the preferences page."""
    status, release = state["status"], state["release"]
    version = config.version_label(release["version"]) if release else ""
    if status == "checking":
        return "Checking for updates…"
    if status == "current":
        return f"Loopcut {config.version_label(current)} is up to date."
    if status == "available":
        return f"Loopcut {version} is available."
    if status == "downloading":
        return f"Downloading Loopcut {version}… {int(state['progress'] * 100)}%"
    if status == "preparing":
        return f"Preparing Loopcut {version} to install…"
    if status == "ready" and state.get("legacy_fallback"):
        return f"Loopcut {version} is downloaded. Restart to update."
    if status == "ready":
        return f"Loopcut {version} installs when you close Blender."
    if status == "waiting":
        return "The update couldn't finish. We'll try again tomorrow."
    if status == "applying":
        return f"Loopcut {version} is updating. You can keep working."
    if status == "installing":
        return f"Loopcut {version} installs when Blender closes."
    if status == "error":
        return f"Update failed: {state['error']}"
    if state["error"]:
        return f"Could not check for updates: {state['error']}"
    return f"Loopcut {config.version_label(current)}"


# ---- Network and files, on worker threads ------------------------------------------------------

def _download_dir() -> Path:
    if _cache is None:
        raise UpdateError("The update cache is not ready.")
    path = _cache
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def _read_json(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8-sig") as handle:
            text = handle.read(_MANIFEST_MAX + 1)
            if len(text) > _MANIFEST_MAX:
                raise UpdateError("Update record is too large")
            data = json.loads(text)
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise UpdateError("Invalid update record")
    return data


def _write_json(path: Path, data: dict) -> None:
    # Atomic replace: a quit or crash cannot leave half a pending record.
    temporary = path.with_name(path.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=True), encoding="utf-8")
    temporary.replace(path)


def _save_pending(release: dict) -> None:
    global _pending
    same = _pending.get("manifest", {}).get("version") == release["label"]
    if _pending and not same and not _state["armed"]:
        previous = parse_manifest(_pending["manifest"], platform_key())
        if previous["asset"] and previous["asset"]["name"] != release["asset"]["name"]:
            (_download_dir() / previous["asset"]["name"]).unlink(missing_ok=True)
    manifest = {"version": release["label"], "notes_url": release["notes_url"],
                "assets": {platform_key(): release["asset"]}, "headline": release.get("headline", "")}
    if release.get("min_version"):
        manifest["min_version"] = label(release["min_version"])
    _pending = {**(_pending if same else {}), "manifest": manifest, "platform": platform_key(),
                "from_version": _pending["from_version"] if same else label(current_version())}
    _write_json(_download_dir() / "pending.json", _pending)


def _result(version: str) -> dict:
    data = _read_json(_download_dir() / f"result-{version}.json")
    if data and (data.get("version") != version or data.get("status") not in ("applied", "failed", "installing") or
                 not isinstance(data.get("attempted_at"), (int, float)) or
                 isinstance(data.get("attempted_at"), bool)):
        raise UpdateError("Invalid update result")
    return data


def _helper_running(result: dict) -> bool:
    pid = result.get("helper_pid")
    if result.get("status") != "installing" or not isinstance(pid, int) or isinstance(pid, bool) or not 0 < pid < 2**32:
        return False
    if sys.platform == "win32":
        # os.kill(pid, 0) is NOT a harmless existence check on Windows. Query a handle instead.
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
        kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # Access denied: assume alive.
        try:
            code = ctypes.c_ulong()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def event_code(event: str, from_version: str, to_version: str, platform: str, installable: bool, reason: str = "") -> str:
    if event not in ("offered", "downloaded", "armed", "applied", "failed", "updated_from"):
        raise ValueError("Unknown update event")
    source, target = label(parse_version(from_version)), label(parse_version(to_version))
    os_name = {"darwin": "mac", "win32": "windows"}.get(platform, "linux")
    prefix = event if event == "updated_from" else f"update_{event}"
    code = f"{prefix}_{source}_to_{target}_{os_name}_{int(installable)}".replace(".", "_")
    if reason:
        if reason != "helper_noack" or event != "failed":
            raise ValueError("Unknown update failure reason")
        code += "_" + reason
    if len(code) > 64:
        raise ValueError("Update event exceeds the telemetry code limit")
    return code


def _event(event: str, from_version: str | None = None, to_version: str | None = None, *, reason: str = "") -> None:
    from . import telemetry
    release = _state["release"]
    try:
        code = event_code(event, from_version or label(current_version()),
                          to_version or release["label"], sys.platform, bool(_target), reason)
    except ValueError:
        return  # Telemetry's bounded code must never prevent an update with a long version label.
    # Existing server schema accepts short codes only. Keep using the normal opt-out and caps.
    telemetry.setup_step("done", None, event != "failed", code)


def _fetch_manifest() -> dict:
    _https(MANIFEST_URL, "manifest URL")
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
    final = folder / asset["name"]
    if final.is_file() and final.stat().st_size == asset["size"] and _sha256(final) == asset["sha256"]:
        return final  # Downloaded by an earlier session.
    part = folder / (asset["name"] + f".{os.getpid()}.part")
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
        previous_status = _state["status"]
        _set(status="checking", error="")
        try:
            release = _fetch_manifest()
        except UpdateError as ex:
            print(f"Loopcut: update check failed: {ex}")
            _set(status="idle" if _state["release"] is None else previous_status,
                 error=str(ex), checked_at=time.time())
            return
        _state["checked_at"] = time.time()
        if release["version"] <= current_version():
            _set(status="current", release=None, path="")
            return
        previous = _state["release"]
        if previous is None or previous["version"] != release["version"]:
            # An armed helper owns this session's verified update. Offer a later release next launch.
            if _state["armed"]:
                _set(status="ready")
                return
            _set(release=release, dismissed=False, path="", progress=0.0, legacy_fallback=False)
            _event("offered")
        _set(release=release, critical=bool(release["min_version"] and current_version() < release["min_version"]))
        if _state["armed"]:
            _set(status="ready")
        elif _state["path"] and Path(_state["path"]).is_file():
            _arm()
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
        _save_pending(release)
    except (UpdateError, OSError, ValueError) as ex:
        print(f"Loopcut: update download failed: {ex}")
        _set(status="error", error=str(ex), progress=0.0)
        _event("failed")
        return
    _set(status="preparing", path=str(path), progress=1.0)
    _event("downloaded")
    _arm()


def _arm() -> bool:
    """Worker thread, after checksum verification. No Blender access and no quit request."""
    global _job, _helper_process
    if _state["armed"]:
        return True
    release = _state["release"]
    if not _target or not release or not release["asset"]:
        return False
    if _pending.get("legacy_fallback") and _pending.get("manifest", {}).get("version") == release["label"]:
        _set(status="ready", armed=False, legacy_fallback=True)
        return False
    can_fallback = False
    process = None
    try:
        result = _result(release["label"])
        if _helper_running(result):
            _set(status="applying")
            return False
        if not retry_due(result, release["label"], time.time()):
            _set(status="waiting")
            return False
        path = Path(_state["path"])
        asset = release["asset"]
        if path.stat().st_size != asset["size"] or _sha256(path) != asset["sha256"]:
            raise UpdateError("The downloaded installer did not pass verification.")
        folder = _download_dir() / f"job-{release['label']}-{os.getpid()}"
        folder.mkdir(mode=0o700, exist_ok=True)
        can_fallback = sys.platform == "win32"
        name, contents = helper_script(sys.platform)
        script = folder / name
        script.write_text(contents, encoding="utf-8-sig" if sys.platform == "win32" else "utf-8",
                          newline="\r\n" if sys.platform == "win32" else "\n")
        script.chmod(0o700)
        restart = folder / "restart"
        restart.unlink(missing_ok=True)
        job = {"pid": os.getpid(), "installer": str(path), "target": _target,
               "log": str(folder / "update.log"), "restart": str(restart),
               "result": str(_download_dir() / f"result-{release['label']}.json"),
               "version": release["label"], "from_version": label(current_version()),
               "sha256": asset["sha256"], "size": asset["size"], "ack": str(folder / "waiting")}
        Path(job["ack"]).unlink(missing_ok=True)
        job_path = folder / "job.json"
        Path(str(job_path) + ".cancel").unlink(missing_ok=True)
        _write_json(job_path, job)
        detach = ({"creationflags": windows_helper_flags()} if sys.platform == "win32" else {"start_new_session": True})
        command = helper_command(str(script), job, str(job_path), sys.platform)
        environment = helper_environment() if sys.platform == "win32" else None
        with (folder / "helper.log").open("ab") as output:
            output.write((time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) +
                          f" starting helper; acknowledgement timeout={HELPER_ACK_TIMEOUT:g}s\n").encode("utf-8"))
            output.flush()
            deadline = time.monotonic() + HELPER_ACK_TIMEOUT
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                       stdout=output, stderr=subprocess.STDOUT, close_fds=True,
                                       env=environment, **detach)
        while not Path(job["ack"]).is_file():
            if process.poll() is not None:
                raise UpdateError("The updater did not start")
            if time.monotonic() >= deadline:
                raise UpdateError("The updater did not start")
            time.sleep(0.05)
        _job = job
        _helper_process = process
        _set(status="ready", armed=True, legacy_fallback=False)
        _event("armed")
        return True
    except (UpdateError, OSError, ValueError) as ex:
        print(f"Loopcut: could not arm updater: {ex}")
        if can_fallback:
            if process is not None:
                # Also disable a late-starting helper if the OS delays or denies termination.
                try:
                    Path(str(job_path) + ".cancel").touch()
                except OSError as cancel_error:
                    print(f"Loopcut: could not save updater cancellation: {cancel_error}")
            if process is not None and process.poll() is None:
                # Stop only the exact unacknowledged helper we started, before enabling the other path.
                try:
                    try:
                        process.terminate()
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=1)
                except (OSError, subprocess.TimeoutExpired) as stop_error:
                    print(f"Loopcut: could not stop unacknowledged updater: {stop_error}")
            _select_legacy_fallback()
            return False
        _set(status="error", error="The updater couldn't start. You can try again.")
        _event("failed")
        return False


def _select_legacy_fallback() -> None:
    """Worker under _lock. A failed PowerShell handoff must not remove the proven restart path."""
    global _job, _helper_process
    try:
        _save_pending(_state["release"])
        _pending["legacy_fallback"] = True
        _write_json(_download_dir() / "pending.json", _pending)
    except OSError as ex:
        print(f"Loopcut: could not save updater fallback: {ex}")
    _job, _helper_process = None, None
    _set(status="ready", armed=False, pickup=False, automatic_restart=False, legacy_fallback=True, error="")
    _event("failed", reason="helper_noack")


def _watch_helper() -> float:
    """Main thread: AV can kill an acknowledged helper while Blender is still open."""
    if (sys.platform == "win32" and _state["armed"] and not _state["legacy_fallback"] and
            _helper_process is not None and _helper_process.poll() is not None):
        def fallback():
            if not _lock.acquire(blocking=False):
                return
            try:
                if _state["armed"] and not _state["legacy_fallback"]:
                    _select_legacy_fallback()
            finally:
                _lock.release()
        threading.Thread(target=fallback, name="loopcut-update-fallback", daemon=True).start()
    return 2.0


def _restart_legacy() -> None:
    """Worker: only an explicit restart click starts the unchanged 0.2.9 Windows helper."""
    global _helper_process
    _lock.acquire()  # This explicit click must survive a concurrent check/fallback handoff.
    try:
        if not _state["legacy_fallback"]:
            _select_legacy_fallback()
        release, path = _state["release"], Path(_state["path"])
        asset = release["asset"]
        if not _target or path.stat().st_size != asset["size"] or _sha256(path) != asset["sha256"]:
            raise UpdateError("The downloaded installer did not pass verification.")
        _pending["auto_restart_attempted"] = True
        _save_pending(release)
        if not _state["armed"]:
            folder = _download_dir()
            script, log = folder / "apply.cmd", folder / "update.log"
            script.write_text(HELPER_CMD, encoding="utf-8", newline="\r\n")
            command = legacy_helper_command(str(script), os.getpid(), str(path), _target, str(log))
            _helper_process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, close_fds=True,
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP, env=helper_environment())
        _set(status="installing", armed=True, pickup=False, automatic_restart=False)
        _event("armed")
        from . import mainthread
        mainthread.run_on_main(_quit_legacy)
    except (UpdateError, OSError, ValueError) as ex:
        print(f"Loopcut: legacy updater could not start: {ex}")
        _set(status="error", error="The updater couldn't start. You can try again.")
        _event("failed")
    finally:
        _lock.release()


def _quit_legacy() -> None:
    import bpy
    def quit_blender():
        # The restart operator already confirmed unsaved work. Never exit inside that operator.
        try:
            bpy.ops.wm.quit_blender("EXEC_DEFAULT")
        except RuntimeError as ex:
            _set(status="ready", error=str(ex))
        return None
    bpy.app.timers.register(quit_blender, first_interval=0.05)


def _load_pending() -> None:
    """Worker: reconcile versions and helper results, then re-verify the durable installer."""
    global _pending
    folder = _download_dir()
    try:
        previous = _read_json(folder / "version.json").get("version")
        current = current_version()
        if previous and parse_version(previous) < current:
            _event("updated_from", previous, label(current))
        _write_json(folder / "version.json", {"version": label(current)})
        pending = _read_json(folder / "pending.json")
        if not pending:
            return
        release = parse_manifest(pending.get("manifest"), platform_key())
        parse_version(pending.get("from_version"))
        if any(not isinstance(pending.get(key, False), bool) for key in ("auto_restart_attempted", "legacy_fallback")):
            raise UpdateError("Invalid restart guard")
        result = _result(release["label"])
        if (result and (current >= release["version"] or not _helper_running(result)) and
                result.get("attempted_at") != pending.get("reported_at")):
            # A helper reporting success is only confirmed applied by running the new version.
            applied = current >= release["version"]
            _event("applied" if applied else "failed", pending["from_version"], release["label"])
            pending["reported_at"] = result["attempted_at"]
            _write_json(folder / "pending.json", pending)
        elif not result and current >= release["version"]:
            # The unchanged legacy helper has no result file; the running version confirms success.
            _event("applied", pending["from_version"], release["label"])
        if release["version"] <= current or pending.get("platform") != platform_key() or not release["asset"]:
            if release["asset"]:
                (folder / release["asset"]["name"]).unlink(missing_ok=True)
            (folder / "pending.json").unlink(missing_ok=True)
            return
        path = folder / release["asset"]["name"]
        if path.stat().st_size != release["asset"]["size"] or _sha256(path) != release["asset"]["sha256"]:
            raise UpdateError("The pending installer did not pass verification")
        _pending = pending
        _set(status="available", release=release, path=str(path), progress=1.0, legacy_fallback=pending.get("legacy_fallback", False),
             critical=bool(release["min_version"] and current < release["min_version"]))
        if _arm() and _automatic:
            from . import mainthread
            mainthread.run_on_main(_offer_startup)
    except (UpdateError, OSError, ValueError) as ex:
        print(f"Loopcut: pending update ignored: {ex}")
        (folder / "pending.json").unlink(missing_ok=True)


def _startup() -> None:
    if not _lock.acquire(blocking=False):
        return
    try:
        _load_pending()
    finally:
        _lock.release()


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
    if threading.current_thread() is not threading.main_thread():
        return bool(_target)
    import bpy
    return bool(install_target(bpy.app.binary_path))


def banner() -> dict | None:
    result = banner_for(_state, installable())
    if (result and sys.platform == "win32" and _state["status"] == "ready" and
            _state["armed"] and not _state["legacy_fallback"]):
        result["detail"] = "Windows will ask to allow the update after Blender closes."
    return result


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
    if not _state["critical"]:
        _set(dismissed=True)


def open_notes() -> None:
    from . import account
    account.open_browser("https://loopcut.org/download")


def restart_to_update() -> None:
    """The operator owns the unsaved-work confirmation; cancelling never arms a relaunch."""
    import bpy
    bpy.ops.loopcut.restart_to_update("INVOKE_DEFAULT")


def defer_startup() -> None:
    if _state.get("automatic_restart") and _job:
        Path(_job["restart"]).unlink(missing_ok=True)
        _set(status="ready", automatic_restart=False)
    _set(pickup=False)


def _safe_startup() -> bool:
    import bpy
    from . import state as sessions
    return (not bpy.app.background and bool(bpy.context.window_manager.windows) and
            time.monotonic() - _started_at < 60 and bool(_pending) and
            startup_eligible(_pending, current_version(), dirty=bpy.data.is_dirty, filepath=bpy.data.filepath,
                             argv=sys.argv, busy=bool(sessions.session().get("busy"))))


def _offer_startup() -> None:
    global _pickup_until
    import bpy
    if not _safe_startup() or not _state["armed"]:
        return
    _set(pickup=True)
    _pickup_until = time.monotonic() + STARTUP_NOTICE_SECONDS
    windows = bpy.context.window_manager.windows
    try:
        with bpy.context.temp_override(window=windows[0]):
            result = bpy.ops.loopcut.update_startup_notice("INVOKE_DEFAULT")
        if "RUNNING_MODAL" not in result:
            defer_startup()
            return
    except RuntimeError as ex:
        print(f"Loopcut: startup update notice unavailable: {ex}")
        defer_startup()
        return
    if not bpy.app.timers.is_registered(_pickup_tick):
        bpy.app.timers.register(_pickup_tick, first_interval=0.25)


def _pickup_tick() -> float | None:
    if not _state["pickup"]:
        return None
    if not _safe_startup():
        defer_startup()
        return None
    if time.monotonic() < _pickup_until:
        return 0.25
    _request_restart(automatic=True)
    return None


def _request_restart(automatic=False) -> None:
    import bpy
    dead_helper = (sys.platform == "win32" and _helper_process is not None and _helper_process.poll() is not None)
    if sys.platform == "win32" and (_state["legacy_fallback"] or dead_helper):
        if not automatic:
            threading.Thread(target=_restart_legacy, name="loopcut-update-legacy", daemon=True).start()
        return
    if not _state["armed"] or not _job or (automatic and not _safe_startup()):
        defer_startup()
        return
    try:
        # Commit BEFORE requesting exit, even if quitting or the installer subsequently fails.
        _pending["auto_restart_attempted"] = True
        _write_json(_download_dir() / "pending.json", _pending)
        Path(_job["restart"]).touch()
    except OSError as ex:
        print(f"Loopcut: update restart could not be saved: {ex}")
        defer_startup()
        return
    _set(status="installing", pickup=False, automatic_restart=automatic)

    def quit_blender():
        # Confirmation happened in our operator (or startup was clean). EXEC avoids leaving a
        # relaunch request behind a cancelled native save dialog. Never execute inside an operator.
        if automatic and (not _state["automatic_restart"] or bpy.data.is_dirty or bpy.data.filepath):
            Path(_job["restart"]).unlink(missing_ok=True)
            _set(status="ready", automatic_restart=False)
            return None
        try:
            bpy.ops.wm.quit_blender("EXEC_DEFAULT")
        except RuntimeError as ex:
            Path(_job["restart"]).unlink(missing_ok=True)
            _set(status="ready", error=str(ex))
        return None
    bpy.app.timers.register(quit_blender, first_interval=0.05)


def _auto_update() -> bool:
    from . import settings
    prefs = settings.preferences()
    return True if prefs is None else bool(prefs.auto_update)


def _tick() -> float:
    import bpy
    if not bpy.app.background and _auto_update() and _state["status"] in ("idle", "current", "available", "error", "waiting"):
        check()
    return CHECK_EVERY


def register() -> None:
    import bpy
    global _CLASSES, _cache, _target, _started_at, _automatic
    if not _CLASSES:
        _CLASSES = _operators()
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    _cancel.clear()
    _started_at = time.monotonic()
    _target = install_target(bpy.app.binary_path)
    _automatic = _auto_update()
    if not bpy.app.background and MANIFEST_URL != "off":
        from . import credentials
        _cache = credentials.folder() / "updates"
        threading.Thread(target=_startup, name="loopcut-update-pending", daemon=True).start()
    if not bpy.app.timers.is_registered(_tick):
        bpy.app.timers.register(_tick, first_interval=FIRST_CHECK_AFTER, persistent=True)
    if sys.platform == "win32" and not bpy.app.background and not bpy.app.timers.is_registered(_watch_helper):
        bpy.app.timers.register(_watch_helper, first_interval=2.0, persistent=True)


def unregister() -> None:
    import bpy
    _cancel.set()
    defer_startup()
    if bpy.app.timers.is_registered(_pickup_tick):
        bpy.app.timers.unregister(_pickup_tick)
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    if bpy.app.timers.is_registered(_watch_helper):
        bpy.app.timers.unregister(_watch_helper)
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

        def invoke(self, context, event):
            if bpy.data.is_dirty:
                return context.window_manager.invoke_props_dialog(self, width=420, title="Restart to update",
                                                                  confirm_text="Restart without saving",
                                                                  cancel_default=True)
            return self.execute(context)

        def draw(self, context):
            self.layout.label(text="Save your work before restarting.")
            self.layout.operator("wm.save_as_mainfile", text="Save first…")

        def execute(self, context):
            _request_restart()
            return {"FINISHED"}

    class LOOPCUT_OT_update_startup_notice(bpy.types.Operator):
        """Keep working; the update will install when you close Blender"""
        bl_idname = "loopcut.update_startup_notice"
        bl_label = "Not now"
        bl_options = {"INTERNAL"}

        def invoke(self, context, event):
            return context.window_manager.invoke_props_dialog(self, width=440,
                title=f"Updating Loopcut to {_state['release']['label']}…", confirm_text="Not now", cancel_default=True)

        def draw(self, context):
            self.layout.label(text="Blender will restart in 10 seconds.")
            self.layout.label(text="You can keep working and update when you close Blender.")
            if sys.platform == "win32":
                self.layout.label(text="Windows will ask to let Loopcut update your installed app.")

        def execute(self, context):
            defer_startup()
            return {"FINISHED"}

        def cancel(self, context):
            defer_startup()

    return LOOPCUT_OT_check_for_updates, LOOPCUT_OT_restart_to_update, LOOPCUT_OT_update_startup_notice


# Pure updater helpers are also imported outside Blender; construct operators at registration.
_CLASSES = ()
