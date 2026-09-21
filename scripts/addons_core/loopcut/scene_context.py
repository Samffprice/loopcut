"""What the user is looking at, attached to every message they send.

Cursor knows which file is open and what is selected; "make this shinier" has to work here too.
The block is added to the message the model receives, not to what the chat shows. @Name in a
message pulls in that object, material or collection by name: @Table, @"Left Chair".

Main thread only, except parse_mentions and candidates_from which are pure.
"""

import json
import re

MAX_SELECTED = 8
MAX_MENTIONS = 5
MAX_CHANGES = 12     # Lines of what the user changed since the agent's last step.
DIGEST_OBJECTS = 20  # Up to this many objects, the block names them all: cheaper than a get_scene_info call.
_MENTION = re.compile(r'(?<![\w@])@(?:"([^"\n]+)"|([\w.\-]+))')


def parse_mentions(text: str) -> list[str]:
    """Names after @, in order, without duplicates."""
    names = []
    for quoted, bare in _MENTION.findall(text):
        name = quoted or bare.rstrip(".-")  # "move @Cube." means Cube.
        if name and name not in names:
            names.append(name)
    return names


def mention_prefix(text: str, cursor: int) -> str | None:
    """The partial name being typed when the cursor sits right after `@par`, else None."""
    start = text.rfind("@", 0, cursor)
    if start < 0 or (start > 0 and not text[start - 1].isspace()):
        return None
    typed = text[start + 1:cursor]
    if typed.startswith('"'):
        return None if '"' in typed[1:] else typed[1:]
    return typed if all(c.isalnum() or c in "._-" for c in typed) else None


def candidates_from(names: list[tuple[str, str]], prefix: str, limit: int = 6) -> list[tuple[str, str]]:
    """(name, kind) pairs matching what was typed: names starting with it first, then containing it."""
    lowered = prefix.lower()
    starts = [n for n in names if n[0].lower().startswith(lowered)]
    contains = [n for n in names if lowered in n[0].lower() and n not in starts]
    return (starts + contains)[:limit]


def mention_text(name: str) -> str:
    return f"@{name}" if all(c.isalnum() or c in "._-" for c in name) else f'@"{name}"'


# ------------------------------------------------------------------ Blender side

def all_names() -> list[tuple[str, str]]:
    import bpy
    scene = bpy.context.scene
    selected = {o.name for o in bpy.context.view_layer.objects.selected}
    objects = sorted(scene.objects, key=lambda o: (o.name not in selected, o.name.lower()))
    from . import addons
    return ([(o.name, "object") for o in objects]
            + [(m.name, "material") for m in bpy.data.materials]
            + [(c.name, "collection") for c in bpy.data.collections]
            + [(a["name"], "add-on") for a in addons.enabled()])


def _resolve(name: str):
    import bpy
    from . import addons
    for kind, store in (("object", bpy.data.objects), ("material", bpy.data.materials),
                        ("collection", bpy.data.collections)):
        if name in store:
            return kind, store[name]
    for entry in addons.enabled():
        if entry["name"] == name or entry["module"] == name:
            return "add-on", entry
    return None, None


def _describe_mention(name: str) -> str:
    from . import object_info, tools
    kind, found = _resolve(name)
    if found is None:
        return f"@{name}: nothing in this file has that name"
    if kind == "object":
        body = {**tools.object_summary(found), **object_info.describe(found)}
    elif kind == "material":
        body = object_info.describe_material(found)
    elif kind == "add-on":
        body = {"module": found["module"], "description": found["description"],
                "operators": found["operators"][:40], "note": "inspect_api describes each operator"}
        return f"@{name} is the add-on {found['name']!r}: {json.dumps(body, separators=(',', ':'))}"
    else:
        body = {"objects": [o.name for o in found.objects][:60],
                "children": [c.name for c in found.children]}
    text = json.dumps(body, separators=(",", ":"), default=str)
    if len(text) > 3000:
        text = text[:3000] + "... (get_object_info has the rest)"
    return f"@{name} is the {kind} {found.name!r}: {text}"


def _digest(objects, selected) -> str:
    """Every other object by name for a small scene; counts by type for a big one."""
    chosen = {o.name for o in selected}
    if len(objects) <= DIGEST_OBJECTS:
        others = [f"{o.name} ({o.type})" for o in objects if o.name not in chosen]
        return "other objects: " + (", ".join(others) if others else "none")
    counts: dict[str, int] = {}
    for o in objects:
        counts[o.type] = counts.get(o.type, 0) + 1
    listed = ", ".join(f"{t} {n}" for t, n in sorted(counts.items(), key=lambda c: -c[1]))
    return f"objects by type: {listed}. get_scene_info lists them."


def _since_last_step(since: dict | None) -> list[str]:
    """What changed in the scene since the agent last acted: the user's own edits, which the
    model would otherwise learn of only by reading the scene again. A delta, not a listing."""
    from . import scene_diff
    if since is None:
        return []
    changes = scene_diff.diff(since, scene_diff.snapshot())
    if scene_diff.is_empty(changes):
        return []
    listed = scene_diff.lines(changes)
    if len(listed) > MAX_CHANGES:
        listed = listed[:MAX_CHANGES] + [f"... and {len(listed) - MAX_CHANGES} more; get_scene_info changed_only=true lists them"]
    return ["changed since your last step (by the user, not you):"] + ["  " + line for line in listed]


def for_message(text: str, since: dict | None = None) -> str:
    """The context block for one user message. Small on purpose: it is paid for on every turn.
    `since` is the scene_diff snapshot from the end of the agent's previous step, if any."""
    import bpy
    from . import tools
    scene = bpy.context.scene
    view_layer = bpy.context.view_layer
    active = view_layer.objects.active
    selected = list(view_layer.objects.selected)
    file_name = bpy.path.basename(bpy.data.filepath) or "unsaved scene"
    lines = [f"file: {file_name} | mode: {bpy.context.mode} | frame: {scene.frame_current} | "
             f"objects: {len(scene.objects)} | active: {active.name if active else 'none'}"]
    if selected:
        lines.append(f"selected ({len(selected)}), which is what \"this\", \"it\" and \"these\" mean:")
        lines += ["  " + json.dumps(tools.object_summary(o), separators=(",", ":")) for o in selected[:MAX_SELECTED]]
        if len(selected) > MAX_SELECTED:
            lines.append(f"  ... and {len(selected) - MAX_SELECTED} more")
    else:
        lines.append("selected: nothing")
    lines.append(_digest(scene.objects, selected))
    lines += _since_last_step(since)
    lines += [_describe_mention(name) for name in parse_mentions(text)[:MAX_MENTIONS]]
    return "<scene_context>\n" + "\n".join(lines) + "\n</scene_context>"
