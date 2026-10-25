"""The version guard: no module of a newer (or older) Loopcut is loaded into this one.

Loopcut imports most of its modules on first use, so one can be read from disk hours after Blender
started. When the files were replaced in the meantime, by an update installed while this Blender was
still open (another Blender quitting ran the installer, or the installer was run by hand), that module
is the new version and everything loaded before it the old one, and the first call between them fails.
On 2026-10-25 a 0.2.15 Blender loaded 0.2.17's chatgpt_responses for its first ChatGPT request and the
turn ended in "Internal error: AttributeError(module 'loopcut.context' has no attribute 'is_record')".

register() puts a finder first on sys.meta_path. Before Python loads a module of this package that is
not loaded yet, it reads bl_info's version from __init__.py on disk, and when that is no longer the
version this Blender started with, the import fails with RestartNeeded, whose text is the chat's card.
A file it cannot read or parse lets the import through. Modules already loaded never come here, so it
costs one small file read per module, once.
"""

import importlib.abc
import re
import sys
from pathlib import Path

MESSAGE = "Loopcut was updated while Blender was open. Restart Blender to finish the update."
_VERSION = re.compile(r'"version":\s*\((\d+),\s*(\d+),\s*(\d+)\)')
# The version the first register() saw. Kept through unregister and register: Blender reloads __init__.py when
# its file changed (addon_utils.enable) but not the modules it imported, so a later bl_info can be newer than them.
_started: tuple | None = None


class RestartNeeded(ImportError):
    """A module's file on disk is from another Loopcut version than the one running. Not a
    ModuleNotFoundError, which `from . import name` would turn into a vaguer error."""


def version_on_disk(root: Path) -> tuple | None:
    """bl_info's version as root/__init__.py says it now, or None when it cannot be read."""
    try:
        found = _VERSION.search((root / "__init__.py").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None
    return tuple(int(part) for part in found.groups()) if found else None


class _Finder(importlib.abc.MetaPathFinder):
    def __init__(self, package: str, root: Path, version: tuple):
        self.package, self.root, self.version = package, root, version

    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(self.package + ".") and fullname not in sys.modules:  # Not a reload.
            found = version_on_disk(self.root)
            if found is not None and found != self.version:
                print(f"Loopcut: {fullname} on disk is {'.'.join(map(str, found))}, this Blender runs "
                      f"{'.'.join(map(str, self.version))}; not loading it")
                raise RestartNeeded(MESSAGE, name=fullname)
        return None  # The usual finders load it.


def register(package: str, root: Path, version) -> None:
    global _started
    _started = _started or tuple(version)
    unregister(package)
    sys.meta_path.insert(0, _Finder(package, Path(root), _started))


def unregister(package: str) -> None:
    sys.meta_path[:] = [f for f in sys.meta_path if not (isinstance(f, _Finder) and f.package == package)]
