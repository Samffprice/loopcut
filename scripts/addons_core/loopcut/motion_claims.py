"""A final reply that says scene objects move when they do not (0.2.18). rel017-app: asked for "a few small moons
flying around", the agent built the moons, never animated them, and its recap listed them as "orbiting".

agent._motion_claim reads the reply that would end a turn of the single loop or the orchestrator, never a worker's
(nor a helper's report_back): claims() finds the scene objects a sentence says move, check() looks at them on the
main thread, and the model goes back once a user turn with nudge(); a later reply that still says so, or one that
cannot go back, ends the turn with notice() for the user. English only, and narrow on purpose: a missed claim leaves
the reply as it was; a false one costs one model round.
"""

import math
import re

from . import contact
from .completion_guard import _plain

NUDGE_HEAD = "[Loopcut] Your reply says "  # agent.send drops a nudge a Stop left last, as it does CONTINUE.
MAX_TRIANGLES = 2_000_000  # Past this, stepping two frames is a freeze: the check is skipped.
MOVE_TOLERANCE = 0.001     # 1 mm
TURN_TOLERANCE = math.radians(0.5)
# Present forms and "animated": "I moved the lamp" and "rotated the chair" are edits, not motion.
_MOVES = re.compile(r"\b(?:orbit(?:s|ing)?|spin(?:s|ning)?|rotat(?:e|es|ing)|fl(?:y|ies|ying)|mov(?:e|es|ing)|"
                    r"animated|bounc(?:e|es|ing)|walk(?:s|ing)?|run(?:s|ning)?|gallop(?:s|ing)?|swing(?:s|ing)?|"
                    r"float(?:s|ing)?\s+(?:around|up))\b", re.I)
# Conditional, future, offered or denied: "could orbit", "Want me to animate the orbit next?", "they don't move yet".
_UNSURE = re.compile(r"\?|'(?:ll|d)\b|n't\b|\b(?:can|could|will|would|shall|should|may|might|if|next|wants?|"
                     r"options?|offers?|not|never|no)\b", re.I)
# Between the names of one list: "Moon_1, Moon_2 and Moon_3", "Moons_1–5".
_LINK = re.compile(r"(?:[\s,&/+–—-]|\d|\b(?:and|or|plus|to|through)\b)*", re.I)
# After "orbiting" or "animated", a name is what moves ("orbiting moons"), unless these lead to it ("orbiting the
# planet").
_LEADS = re.compile(r"\b(?:the|a|an|its|their|his|her|to|around|about|along|across|over|past|through|into|onto|in|on|"
                    r"at|by|with|from|of|near|above|below|under|between|behind|towards?)\b", re.I)
_SIMULATIONS = frozenset({"CLOTH", "SOFT_BODY", "FLUID", "PARTICLE_SYSTEM"})
# Geometry Nodes that read the frame, as Blender's depsgraph counts a node tree time dependent
# (nodes/intern/dependencies.cc): Scene Time and a simulation zone's input.
_TIME_NODES = frozenset({"GeometryNodeInputSceneTime", "GeometryNodeSimulationInput"})


def _sentences(text: str):
    text = re.sub(r"(?:```|~~~).*?(?:```|~~~|\Z)", "\n", text.replace("’", "'"), flags=re.S)
    for line in text.splitlines():
        yield from (s for s in re.split(r"(?<=[.!?])\s+", _plain(line)) if s.strip())


def may_claim(text) -> bool:
    """A sentence with a motion word and nothing that makes it conditional: worth reading the scene for."""
    return isinstance(text, str) and any(_MOVES.search(s) and not _UNSURE.search(s) for s in _sentences(text))


def _patterns(names, text: str):
    """(pattern, names) longest first: each name as it is, and its stem with digits, .001 and _n suffixes and a
    plural "s" stripped, which matches "moon" or "moons" for Moon_1…Moon_5. Under 3 letters, nothing; nor for a
    name whose stem's first word is not in the text (a scene of thousands of objects, on the main thread)."""
    found, stems, lowered = [], {}, text.lower()
    for name in names:
        stem = re.sub(r"(?:[._ -]?\d+)+$", "", name.lower()).strip(" ._-")
        stem = stem[:-1] if len(stem) > 3 and stem.endswith("s") else stem
        if len(stem) >= 3 and re.split(r"[\s_.-]+", stem)[0] in lowered:
            stems.setdefault(stem, []).append(name)
        if len(name) >= 3 and name.lower() in lowered:
            found.append((len(name), 0, re.escape(name), [name]))
    found += [(len(stem), 1, r"[\s_.-]+".join(map(re.escape, re.split(r"[\s_.-]+", stem))) + "(?:s|es)?", group)
              for stem, group in stems.items()]
    found.sort(key=lambda f: (-f[0], f[1]))  # Longest first; a name before a stem as long.
    return [(re.compile(r"(?<!\w)" + p + r"(?!\w)", re.I), group) for _, _, p, group in found]


def _subject(sentence: str, spans: list, move) -> list:
    """The names a motion word is said of: the nearest list of names before it ("five moons orbit the planet"), or,
    with none before, one right after a participle ("orbiting moons")."""
    before = [s for s in spans if s[1] <= move.start()]
    if before:
        group = [before[-1]]
        for span in reversed(before[:-1]):
            if not _LINK.fullmatch(sentence[span[1]:group[0][0]]):
                break
            group.insert(0, span)
    else:
        after = [s for s in spans if s[0] >= move.end()]
        gap = sentence[move.end():after[0][0]] if after else ""
        participle = move.group().lower().endswith(("ing", "animated"))
        if not after or not participle or _LEADS.search(gap) or len(gap.split()) > 2:
            return []
        group = [after[0]]
        for span in after[1:]:
            if not _LINK.fullmatch(sentence[group[-1][1]:span[0]]):
                break
            group.append(span)
    return [name for span in group for name in span[2]]


def claims(text: str, names) -> list:
    """[(the reply's motion word, [the scene names it is said of])] for each motion word in a sentence or list item
    that names scene objects (case-insensitive) and is not conditional, future, offered or denied (_UNSURE)."""
    if not isinstance(text, str):
        return []
    patterns, out = _patterns(names, text), []
    for sentence in _sentences(text):
        if _UNSURE.search(sentence) or not _MOVES.search(sentence):
            continue
        spans, taken = {}, [False] * len(sentence)
        for pattern, group in patterns:
            for match in pattern.finditer(sentence):
                if match.span() in spans:  # "moons": the object Moons and the stem of Moons_1…Moons_5.
                    spans[match.span()] += [n for n in group if n not in spans[match.span()]]
                elif not any(taken[match.start():match.end()]):
                    spans[match.span()] = list(group)
                    taken[match.start():match.end()] = [True] * (match.end() - match.start())
        ordered = sorted((start, end, group) for (start, end), group in spans.items())
        blanked = "".join(" " if t else c for c, t in zip(sentence, taken))  # A Swing that swings is a name.
        for move in _MOVES.finditer(blanked):
            group = list(dict.fromkeys(_subject(sentence, ordered, move)))
            if group:
                out.append((move.group().lower(), group))
    return out


def _animated(ob) -> bool:
    """Motion counted without stepping frames: contact.animated's (an action, NLA tracks or drivers on the object, a
    parent, a constraint target or the armature that deforms it; for an object with no geometry, its children), an
    action on its data or shape keys, a simulation, which a jump between frames does not run, or Geometry Nodes that
    read the frame (_timed), whose motion inside an unmoving object no world matrix shows. An action with no slot
    assigned (Blender 5's slotted actions) still counts: a missed claim is the cheaper mistake."""
    if contact.animated(ob):
        return True
    for owner in (ob.data, getattr(ob.data, "shape_keys", None)):
        data = getattr(owner, "animation_data", None)
        if data is not None and (data.action is not None or len(data.nla_tracks)):
            return True
    body = getattr(ob, "rigid_body", None)
    if (body is not None and body.type == "ACTIVE") or any(m.type in _SIMULATIONS for m in ob.modifiers):
        return True
    seen = set()
    return any(m.type == "NODES" and _timed(m.node_group, seen) for m in ob.modifiers)


def _timed(tree, seen: set) -> bool:
    """A node group with an unmuted time node (_TIME_NODES), or keys or drivers of its own, in it or in any group it
    nests; each group read once."""
    if tree is None or tree.name in seen:
        return False
    seen.add(tree.name)
    data = getattr(tree, "animation_data", None)
    if data is not None and (data.action is not None or len(data.nla_tracks) or len(data.drivers)):
        return True
    return any((node.bl_idname in _TIME_NODES and not node.mute) or _timed(getattr(node, "node_tree", None), seen)
               for node in tree.nodes)


def _moved(a, b) -> bool:
    """Two world matrices (rows) more than 1 mm or 0.5° apart; scale is not motion here."""
    if math.dist([a[i][3] for i in range(3)], [b[i][3] for i in range(3)]) > MOVE_TOLERANCE:
        return True
    def axes(m):
        return [[m[r][c] / (math.hypot(m[0][c], m[1][c], m[2][c]) or 1.0) for r in range(3)] for c in range(3)]
    cosine = (sum(x * y for p, q in zip(axes(a), axes(b)) for x, y in zip(p, q)) - 1) / 2
    return cosine < math.cos(TURN_TOLERANCE)


def still(names):
    """Main thread: (those of names that do not move, (frame N, frame M)), or None past MAX_TRIANGLES. The scene is
    evaluated at frame_start and mid-range (at least 24 frames on), and put back on its frame and subframe. An empty
    (a group, an orbit's centre) stands for what it holds: it moves when they do, and goes without saying when they
    are named."""
    import bpy
    scene = bpy.context.scene
    start = scene.frame_start
    frames = (start, start + max((scene.frame_end - start) // 2, 24))
    quiet = [o for o in map(scene.objects.get, names) if o is not None and not _animated(o)]
    moved = set()
    if quiet:
        triangles = sum(len(o.data.loop_triangles) for o in bpy.context.evaluated_depsgraph_get().objects
                        if o.type == "MESH")
        if triangles > MAX_TRIANGLES:
            print(f"Loopcut: motion check skipped: {triangles:,} triangles is too many to step through frames.")
            return None
        back, seen = (scene.frame_current, scene.frame_subframe), []
        try:
            for frame in frames:
                scene.frame_set(frame)
                graph = bpy.context.evaluated_depsgraph_get()
                seen.append([[tuple(row) for row in o.evaluated_get(graph).matrix_world] for o in quiet])
        finally:
            scene.frame_set(back[0], subframe=back[1])
        moved = {o.name for o, a, b in zip(quiet, *seen) if _moved(a, b)}
    stays = [o for o in quiet if o.name not in moved]
    held = moved | {o.name for o in stays}
    return [o.name for o in stays
            if not (o.type == "EMPTY" and any(c.name in held for c in o.children_recursive))], frames


def check(text: str) -> dict | None:
    """Main thread: {"names": the claimed objects that do not move, "move": the reply's word, "frames": (N, M)}, or
    None when the reply claims no motion of a scene object, each one claimed moves, or the scene is too heavy."""
    import bpy
    found = claims(text, [o.name for o in bpy.context.scene.objects])
    result = still(list(dict.fromkeys(n for _, group in found for n in group))) if found else None
    if not result or not result[0]:
        return None
    names, frames = result
    return {"names": names, "move": next(m for m, group in found if set(group) & set(names)), "frames": frames}


def _listed(names: list) -> str:
    shown = names if len(names) <= 5 else names[:4] + [f"{len(names) - 4} more"]
    return shown[0] if len(shown) == 1 else ", ".join(shown[:-1]) + " and " + shown[-1]


def nudge(found: dict) -> str:
    """The model's one message, never shown in the panel."""
    one = len(found["names"]) == 1
    return (NUDGE_HEAD + f"{_listed(found['names'])} {found['move']}, but {'it does' if one else 'they do'} not move "
            f"between frames {found['frames'][0]} and {found['frames'][1]}: animate {'it' if one else 'them'} now, or "
            f"say plainly that {'it doesn' if one else 'they don'}'t move yet.")


def notice(found: dict) -> str:
    """The user's muted line after a reply that still says so."""
    return f"{_listed(found['names'])} {'doesn' if len(found['names']) == 1 else 'don'}'t move yet."
