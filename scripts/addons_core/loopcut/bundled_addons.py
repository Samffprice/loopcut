"""The fork's optional System extensions. All Blender access is on the main thread.

The manifest beside the System repository is authoritative: stock Blender without it has
no Loopcut bundles. Hints advertise capabilities, not recipes. Session enables need a prefs
entry (some upstream add-ons require AddonPreferences), but must not dirty user preferences.
The interactive journal repairs entries incidentally saved by a later preference change.
"""

import json
import os
import re
import tempfile
from pathlib import Path

from . import credentials

HINTS = {
    "looptools": ("mesh.looptools_*", "mesh cleanup: relax, circle, bridge, flatten, space"),
    "univ": ("uv.univ_*", "unwrap/straighten/quadrify/texel density/pack; interactive Blender only"),
    "node_arrange": ("node.na_*", "tidy node trees"),
    "bool_tool": ("object.boolean_*", "boolean cutters"),
    "mpfb": ("mpfb.* and HumanService", "parametric rigged humans"),
}
RECORD_FILE = "bundled_addons_session.json"
_session_enabled: set[str] = set()


class BundledAddonError(Exception):
    """A bundle could not be read, enabled, or restored safely."""


def packages() -> list[dict]:
    """Manifest entries with live presence/enabled flags; does not import or enable packages."""
    import bpy
    preferences = getattr(getattr(bpy, "context", None), "preferences", None)
    repos = getattr(getattr(preferences, "extensions", None), "repos", ())
    repo = next((r for r in repos if r.source == 'SYSTEM' and r.module == "system"), None)
    if repo is None:
        return []
    directory = Path(repo.directory)
    try:
        data = json.loads((directory.parent / "bundled.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as ex:
        raise BundledAddonError("Could not read the bundled extensions manifest") from ex
    if (not isinstance(data, dict) or data.get("schema") != "loopcut.bundled-extensions/1"
            or not isinstance(data.get("packages"), list)):
        raise BundledAddonError("Invalid bundled extensions manifest")
    import addon_utils
    result, seen = [], set()
    for entry in data["packages"]:
        if (not isinstance(entry, dict) or not isinstance(entry.get("id"), str)
                or entry["id"] not in HINTS or entry["id"] in seen
                or not isinstance(entry.get("name"), str) or not entry["name"].strip()):
            raise BundledAddonError("Invalid bundled extension entry")
        package_id = entry["id"]
        seen.add(package_id)
        module = f"bl_ext.system.{package_id}"
        prefixes, description = HINTS[package_id]
        result.append({"id": package_id, "name": entry["name"], "module": module,
                       "prefixes": prefixes, "description": description,
                       "present": (directory / package_id / "blender_manifest.toml").is_file(),
                       "enabled": bool(addon_utils.check(module)[1])})
    return result


def hint(package: dict) -> str:
    return f"{package['name']} [{package['id']}] {package['prefixes']}: {package['description']}"


def matching_path(path: str) -> tuple[dict | None, str]:
    """Return a bundle and canonical path. Only exact package names or owned operators match."""
    operator = path.removeprefix("bpy.ops.")
    for package in packages():
        if path in (package["id"], package["module"]):
            return package, package["module"]
        prefix = HINTS[package["id"]][0].split("*", 1)[0]
        if operator.startswith(prefix) and re.fullmatch(r"[a-zA-Z_]\w*\.[a-zA-Z_]\w*", operator):
            return package, "bpy.ops." + operator
    return None, path


def search_hints(query: str) -> list[str]:
    """Read-only suggestions when RNA has no complete match for these words."""
    terms = query.lower().split()
    return ["Bundled, off until inspect_api names it: " + hint(p)
            for p in packages() if p["present"] and not p["enabled"]
            and terms and all(t in hint(p).lower() for t in terms)]


def _read_record(path: Path) -> set[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set()
    if (not isinstance(data, dict) or data.get("schema") != 1
            or not isinstance(data.get("modules"), list)
            or any(not isinstance(m, str) or m not in {f"bl_ext.system.{p}" for p in HINTS}
                   for m in data["modules"])):
        raise BundledAddonError("Invalid bundled add-on session record; left unchanged")
    return set(data["modules"])


def _write_record(path: Path, modules: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=RECORD_FILE + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"schema": 1, "modules": sorted(modules)}, handle)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _addon_error(ex: Exception) -> None:
    # addon_utils otherwise prints and swallows registration/unregistration failures.
    import traceback
    traceback.print_exception(ex)
    raise BundledAddonError("Bundled add-on registration or cleanup failed") from ex


def ensure_enabled(package: dict) -> str:
    """Return an activation notice (or the UniV limitation); never save userpref.blend."""
    import addon_utils
    import bpy
    module = package["module"]
    if package["id"] == "univ" and bpy.app.background:
        return "UniV registers only in an interactive Blender; it is not available here."
    if addon_utils.check(module)[1]:
        return ""
    if not package["present"]:
        raise BundledAddonError(f"Bundled {package['name']} is missing from the System repository.")
    preferences = bpy.context.preferences
    was_in_preferences = module in preferences.addons
    dirty = preferences.is_dirty
    # Write intent before registration: even a partial registration or a crash can be repaired.
    # An already enabled user preference belongs to the user, including one that failed to load.
    if not bpy.app.background and not was_in_preferences:
        path = credentials.folder() / RECORD_FILE
        _write_record(path, _read_record(path) | {module})
    try:
        addon_utils.enable(module, default_set=True, handle_error=_addon_error)
        if not addon_utils.check(module)[1]:
            raise BundledAddonError(f"Could not enable bundled {package['name']}.")
    except Exception:
        # Transaction boundary: undo our new prefs entry even if upstream register() failed.
        # Keep the journal until a later startup observes it off.
        if not was_in_preferences:
            addon_utils.disable(module, default_set=True, handle_error=_addon_error)
        raise
    finally:
        preferences.is_dirty = dirty
    _session_enabled.add(module)
    notice = f"Turned on the bundled {package['name']} add-on for this session."
    if bpy.app.background:  # A worker's own child: on where it runs; nothing more to say.
        return notice
    background = ("UniV works only in the live Blender (run_python)." if package["id"] == "univ" else
                  'A background Blender (run_blender, blender -b) enables it separately in its script with '
                  f'import addon_utils; addon_utils.enable("{module}", default_set=True); preferences are not '
                  'saved on background exit, but an explicit save_userpref() writes them.')
    return f"{notice} {background}"


def startup_cleanup() -> None:
    """After Blender loads its saved add-ons, repair incidental saves from earlier sessions.

    Keep an entry after disabling: quitting without saving must not forget the repair. Only a
    later startup which finds it already off can drop it. Never touch unrecorded user add-ons.
    Background children share config paths, but must neither consume nor write this journal.
    """
    import addon_utils
    import bpy
    if bpy.app.background:
        return
    path = credentials.folder() / RECORD_FILE
    recorded = _read_record(path)
    remaining = set(recorded)
    for module in sorted(recorded - _session_enabled):
        if module in bpy.context.preferences.addons or addon_utils.check(module)[1]:
            try:
                addon_utils.disable(module, default_set=True, handle_error=_addon_error)
            finally:
                bpy.context.preferences.is_dirty = True
        else:
            remaining.remove(module)
    if remaining != recorded:
        _write_record(path, remaining)
