"""Finds and copies the user's stock Blender settings, for "Import from Blender" on first run.

The Loopcut build keeps its settings in its own folder (GHOST_SystemPaths* in the fork), so
Blender's "copy previous settings" finds nothing to offer. This looks where stock Blender keeps
them instead. Copy only: nothing here writes to Blender's folder. No bpy, so tests can run it.
"""

import os
import re
import shutil
import sys
from pathlib import Path

_VERSION = re.compile(r"(\d+)\.(\d+)")


def stock_root(platform: str = sys.platform, environ=os.environ) -> Path | None:
    """The folder holding stock Blender's per-version settings folders."""
    if platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Blender"
    if platform == "win32":
        appdata = environ.get("APPDATA")
        return Path(appdata) / "Blender Foundation" / "Blender" if appdata else None
    config = environ.get("XDG_CONFIG_HOME")
    return (Path(config) if config else Path.home() / ".config") / "blender"


def find_source(root: Path | None, current: tuple[int, int]) -> tuple[tuple[int, int], Path] | None:
    """The newest settings folder this build can read: saved preferences, not newer than this
    build, and no older than the previous major release (the range Blender itself imports)."""
    if root is None or not root.is_dir():
        return None
    found = []
    for entry in root.iterdir():
        match = _VERSION.fullmatch(entry.name)
        if not match:
            continue
        version = (int(match[1]), int(match[2]))
        if current[0] - 1 <= version[0] and version <= current and (entry / "config" / "userpref.blend").is_file():
            found.append((version, entry))
    return max(found) if found else None


def addon_count(source: Path) -> int:
    """Installed add-ons and extensions, for the line under the import button."""
    count = 0
    for folder in [source / "scripts" / "addons", *(p for p in (source / "extensions").glob("*") if p.is_dir())]:
        if folder.is_dir():
            count += sum(1 for p in folder.iterdir()
                         if not p.name.startswith((".", "_")) and (p.is_dir() or p.suffix == ".py"))
    return count


def copy_settings(source: Path, target: Path) -> None:
    """Raises OSError or shutil.Error. Links are copied as links, never followed."""
    shutil.copytree(source, target, dirs_exist_ok=True, symlinks=True)
