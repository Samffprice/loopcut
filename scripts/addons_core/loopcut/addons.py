"""Installed add-ons and extensions, named to the model once per request.

inspect_api already describes whatever an add-on registers: its operators land in bpy.ops, its
panels, properties and preferences in bpy.types. What the model cannot do is look up something it
does not know exists, so each request names the user-installed add-ons with their operator
prefixes. Blender's own bundled add-ons are left out: the model knows those, and they are always
there.

Main thread only, except `line` which is pure.
"""

import os
import sys

MAX_LISTED = 12       # More than this and the line stops being cheap; the rest is a count.
MAX_OP_GROUPS = 3     # Operator prefixes shown per add-on, largest groups first.
MAX_DESCRIPTION = 70
_cache: tuple[tuple, list[dict]] | None = None  # (enabled add-on names, entries) for that set.


def enabled() -> list[dict]:
    """User-installed add-ons that are enabled and running, with the operators each registered."""
    global _cache
    import bpy
    from . import settings
    key = tuple(bpy.context.preferences.addons.keys())
    if _cache is not None and _cache[0] == key:
        return _cache[1]
    # system_resource can come back relative to the working directory; module paths are absolute.
    core = os.path.realpath(os.path.join(bpy.utils.system_resource("SCRIPTS"), "addons_core")) + os.sep
    candidates = {}
    for name in key:
        module = sys.modules.get(name)
        if module is None or not getattr(module, "__addon_enabled__", False):
            continue  # Enabled in the preferences but not running here.
        file = os.path.realpath(getattr(module, "__file__", "") or "")
        if name == settings.PACKAGE or file.startswith(core):
            continue
        candidates[name] = module
    operators = _operators_by_addon(candidates)
    entries = [{"module": name, "name": info.get("name") or name,
                "description": info.get("description") or "",
                "operators": operators.get(name, [])}
               for name, info in ((n, _info(m)) for n, m in candidates.items())]
    entries.sort(key=lambda e: e["name"].lower())
    _cache = (key, entries)
    return entries


def _info(module) -> dict:
    """bl_info for a legacy add-on, the manifest for an extension; addon_utils reads both."""
    import addon_utils
    for fake in addon_utils.modules(refresh=False):
        if fake.__name__ == module.__name__:
            return addon_utils.module_bl_info(fake)
    return addon_utils.module_bl_info(module)


def _operators_by_addon(modules: dict) -> dict[str, list[str]]:
    """bl_idname of every registered operator, keyed by the add-on whose package defined it.
    One pass over bpy.types, a few milliseconds."""
    import bpy
    found: dict[str, list[str]] = {}
    if not modules:
        return found
    for name in dir(bpy.types):
        cls = getattr(bpy.types, name, None)
        if not isinstance(cls, type) or not issubclass(cls, bpy.types.Operator):
            continue
        owner = cls.__module__ or ""
        for addon in modules:
            if owner == addon or owner.startswith(addon + "."):
                idname = getattr(cls, "bl_idname", "")
                if idname:
                    found.setdefault(addon, []).append(idname)
                break
    return found


def _op_groups(idnames: list[str]) -> str:
    """'bpy.ops.node.nw_*, bpy.ops.wm.nw_import' from the operators' bl_idnames."""
    by_module: dict[str, list[str]] = {}
    for idname in idnames:
        module, _, name = idname.partition(".")
        by_module.setdefault(module, []).append(name)
    groups = sorted(by_module.items(), key=lambda g: (-len(g[1]), g[0]))
    parts = []
    for module, names in groups[:MAX_OP_GROUPS]:
        if len(names) == 1:
            parts.append(f"bpy.ops.{module}.{names[0]}")
            continue
        common = os.path.commonprefix(names)
        common = common[:common.rfind("_") + 1] if "_" in common else ""  # nw_a* would mislead.
        parts.append(f"bpy.ops.{module}.{common}* ({len(names)})")
    if len(groups) > MAX_OP_GROUPS:
        parts.append(f"+{len(groups) - MAX_OP_GROUPS} more modules")
    return ", ".join(parts)


def line(entries: list[dict]) -> str:
    """One line for the system prompt, or "" when nothing is installed."""
    if not entries:
        return ""
    parts = []
    for entry in entries[:MAX_LISTED]:
        text = f"{entry['name']} [{entry['module']}]"
        ops = _op_groups(entry.get("operators") or [])
        if ops:
            text += f" {ops}"
        description = " ".join(str(entry.get("description") or "").split())
        if description:
            if len(description) > MAX_DESCRIPTION:
                description = description[:MAX_DESCRIPTION - 1].rstrip() + "…"
            text += f": {description}"
        parts.append(text)
    if len(entries) > MAX_LISTED:
        parts.append(f"+{len(entries) - MAX_LISTED} more")
    return "Installed add-ons: " + "; ".join(parts)
