"""Merging a wave of workers' results into the live scene, by ownership. Parent, main thread.

The algorithm is the M0 merge spike's (loopcut/harness/probes/merge_spike.py, 17 of 17 checks):
per worker, append everything in its result, drop the Library the append leaves (and the weak
references to the result it gives what arrived), and sort what arrived. Unstamped datablocks are created: they keep the
worker's own names (append itself resolves a name already taken, by a live datablock or an earlier
worker's created one this wave, with Blender's own .001; _rename_created only reports that) and go back
into the collections the worker put them in on purpose (_worker_homes), else into the collection the
brief names, else the one named after the prefix when the user asked for collections, else the
scene's main collection (_home). Stamped ones the worker owns replace their live originals with user_remap, so live
references follow, unless the original changed in the live scene during the wave: then the user's
version stays and it is a conflict. Stamped ones it doesn't own are copies that libraries.write
pulled in: they are pointed back at the live originals and deleted, and any change in them is
reported as a dropped edit. Every such swap goes through _replace, which hands a mesh's shape keys
(a Key datablock of its own) over with it, so drivers aimed at them keep a live target. Identity goes
through the spawn map (key -> live session_uid), so a datablock the user renamed during the wave is
still found.

Added for production: a checkpoint first (checkpoints.ensure_for_wave), one undo step around each
merge, an exception undoing back to before the merge and raising MergeError, scene.camera and
scene.world taken from a worker with that aspect when the live pointer is still what it was at
spawn, created datablocks outside the worker's role put in a hidden quarantine collection until
accept_quarantine, datablocks an owner removed removed live, and the temporary .blend files deleted.

A wave's workers may merge in several calls, as they finish (waves.py): each call is its own undo
step, the spawn copy stays until the last (keep_spawn), and Spawn.moved carries what an earlier call
replaced, so a later worker's copy of it still finds the live replacement. What changed live between
the spawn and a later call, the orchestrator's own edits included, is a conflict as it always was.

Text blocks and images (ownership.SUPPORT) merge like the rest: a worker's new ones as created (with
a fake user when nothing uses them, so a save keeps them, as Blender gives every new text), an owned
one replacing the live one. Each the worker's manifest lists (ownership.support_entry) is checked
again live by its token (_support), so one that did not arrive says so in the worker's report
instead of being found missing a wave later (Arcane chest eval 20260930-154037: ArcaneRecipe).
"""

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import ownership
from .ownership import COLLECTIONS, HOME, POINTER, QUARANTINE, STAMP, VARIANT

DOT_NUM = re.compile(r"\.\d{3}$")
QUARANTINE_COLLECTION = "LC_Quarantine"
STAGING = "LC_Staging_"
_POINTERS = (("camera", "camera"), ("lighting", "world"))  # (aspect, scene pointer)


class MergeError(Exception):
    """A failed merge; scene_unchanged confirms rollback or failure before any edits."""

    def __init__(self, message: str, *, scene_unchanged: bool = False):
        super().__init__(message)
        self.scene_unchanged = scene_unchanged


@dataclass
class Spawn:
    wave: str        # Unique in the conversation: "<job folder>-<wave folder>"; quarantine tags carry it.
    path: Path       # The spawn copy every worker of the wave starts from.
    ids: dict        # key -> live session_uid at spawn.
    tokens: dict     # key -> token at spawn.
    pointers: dict   # {"camera": key | None, "world": key | None} at spawn.
    moved: dict = field(default_factory=dict)  # key -> session_uid of what an earlier merge replaced it with.
    # key -> what the live datablock is expected to be now, where that is not its token at spawn: the
    # workspace's earlier applies (workspace.py) replace what a later build's copy still has as at spawn.
    # Only the live side reads it (changed or deleted live since); the result's own copies are measured
    # against tokens, the copy they were built from.
    live_tokens: dict = field(default_factory=dict)

    def uid2spawn(self) -> dict:
        return {uid: key.split(":", 1)[1] for key, uid in self.ids.items()}

    def expected(self, key: str):
        """What the live datablock should still be for its replacement to go ahead (live_tokens)."""
        return self.live_tokens.get(key, self.tokens.get(key))


def created_text(entries: list) -> str:
    """"7 objects and 9 other datablocks created": objects are what the user counts (a count of
    everything read 14 for 7 props with their meshes). entries: dicts with id_type, or "TYPE:name"."""
    kinds = [e.get("id_type") if isinstance(e, dict) else str(e).split(":", 1)[0] for e in entries]
    objects = kinds.count("OBJECT")
    other = len(kinds) - objects
    text = f"{objects} object{'s' if objects != 1 else ''}"
    return text + (f" and {other} other datablock{'s' if other != 1 else ''}" if other else "") + " created"


def unlinked_text(entry: dict) -> str:
    """A driver the merge left without its target ({owner, path, variable, target, why})."""
    return f"{entry['owner']} {entry['path']} (variable {entry['variable']}) {entry['why']}"


def retargeted_text(entry: dict) -> str:
    """A driver the merge re-aimed ({owner, path, variable, old, new})."""
    return (f"{entry['owner']} {entry['path']} (variable {entry['variable']}) re-aimed from {entry['old']}, "
            f"removed, to {entry['new']}, which have the same key blocks")


def _held_note(entry: dict) -> str:
    return f" ({', '.join(entry['held'])} held back)" if entry.get("held") else ""


@dataclass
class MergeReport:
    """created: {worker, name, id_type, type, parent, held?}; replaced: {worker, key, name, removed?,
    pointer?, held?}; renamed: "worker: TYPE old -> new"; quarantined: {worker, wave, name, id_type,
    type, hidden}; dropped_edits: {worker, key, name, worker_value, live_value}; conflicts:
    {worker, keys | pointer, reason, live_now, worker_value}; errors: text; checkpoint: its id.
    quarantined entries also carry reason: why it is held back (what the role makes and what would let
    it through). admitted: {worker, name, id_type, type, reason}: created outside the worker's role but
    merged because the brief asks for it (a lamp's bulb light, a light's own flicker, a name the
    brief gives). held: what a merged object hangs on that is in quarantine ("parent Lit_Spot_root"). reused:
    {worker, name (the live one), made, id_type, copy?}: a material the worker made again, or copied and
    left as it was, the same as a live one, which its objects now wear instead of a copy. A created
    material the worker copied from a live one and changed carries variant_of: its own finish, on its
    own parts, merged as the worker's. support: {worker, key, kind, name (live), state (new | updated |
    skipped | missing | held), about, hash?, why?} or {worker, more}: the worker's text blocks and
    images, checked live (_support). unlinked: {worker, owner, path, variable, target, why}: a driver
    whose target this merge removed (now unset) or whose key block the replacing shape keys lack;
    retargeted: {worker, owner, path, variable, old, new}: one re-aimed at the replacement's shape keys
    (_settle)."""
    created: list = field(default_factory=list)
    replaced: list = field(default_factory=list)
    renamed: list = field(default_factory=list)
    reused: list = field(default_factory=list)
    quarantined: list = field(default_factory=list)
    admitted: list = field(default_factory=list)
    dropped_edits: list = field(default_factory=list)
    conflicts: list = field(default_factory=list)
    fixed: list = field(default_factory=list)  # {worker, name}: a worker's mesh Mesh.validate() had to fix.
    support: list = field(default_factory=list)
    unlinked: list = field(default_factory=list)
    retargeted: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    checkpoint: str = ""
    seconds: float = 0.0

    def headline(self) -> str:
        parts = [created_text(self.created)]
        for count, word in ((len(self.replaced), "replaced"), (len(self.quarantined), "quarantined"),
                            (len(self.dropped_edits), "dropped edit"), (len(self.conflicts), "conflict"),
                            (len(self.unlinked), "driver left without its target")):
            if count:
                plural = {"dropped edit": "dropped edits", "conflict": "conflicts",
                          "driver left without its target": "drivers left without their target"}
                parts.append(f"{count} {plural.get(word, word) if count > 1 else word}")
        return "Merged: " + ", ".join(parts)

    def lines(self) -> list[str]:
        """One line per thing the orchestrator or the user should know about."""
        lines = [f"{r['worker']}: scene.{r['pointer']} set to {r['name']}" if r.get("pointer") else
                 f"{r['worker']}: {r['key']} {'removed' if r.get('removed') else 'replaced'}{_held_note(r)}"
                 for r in self.replaced]
        lines += [f"{c['worker']}: {c['name']}{_held_note(c)}" for c in self.created if c.get("held")]
        lines += [f"{r['worker']}: material {r['made']} is " + (f"an unchanged copy of the live {r['name']}" if r.get("copy")
                  else f"the live {r['name']} made again (same name and settings)") + ": the live one is used, no copy merged"
                  for r in self.reused]
        lines += [f"{c['worker']}: material {c['name']} is its own variant of {c['variant_of']} (on its own parts)"
                  for c in self.created if c.get("variant_of")]
        lines += [f"{q['worker']}: {q['name']} ({(q['type'] or q['id_type']).lower()}) quarantined: "
                  f"{q.get('reason') or 'outside its role'}" for q in self.quarantined]
        lines += [f"{a['worker']}: {a['name']} ({(a['type'] or a['id_type']).lower()}) merged though outside its role: "
                  f"{a['reason']}" for a in self.admitted]
        lines += [f"{d['worker']}: edit to {d['name']} dropped (not owned" + (
            "; a palette material changes only with aspect materials, a worker's own copy merges)"
            if d["key"].startswith("MATERIAL:") else ")") for d in self.dropped_edits]
        lines += [f"{c['worker']}: {c.get('pointer') or ', '.join(c.get('keys', []))} not merged: {c['reason']}"
                  for c in self.conflicts]
        lines += [f"{f['worker']}: mesh {f['name']} had invalid geometry (a bad loop index would crash a later "
                  f"bmesh read); fixed before merging" for f in self.fixed]
        lines += [f"{s['worker']}: {s['kind'].lower()} {s['name']} is missing after the merge: {s.get('why', '')}"
                  for s in self.support if s.get("state") == "missing" and not s.get("conflict")]
        lines += [f"{u['worker']}: driver {unlinked_text(u)}" for u in self.unlinked]
        lines += [f"{r['worker']}: driver {retargeted_text(r)}" for r in self.retargeted]
        return lines + list(self.errors)

    def log(self) -> dict:
        """The JobLog "merge" entry."""
        return {"created": len(self.created), "reused": [r["name"] for r in self.reused],
                "replaced": [r["key"] for r in self.replaced], "fixed": [f["name"] for f in self.fixed],
                "quarantined": [q["name"] for q in self.quarantined], "admitted": [a["name"] for a in self.admitted],
                "conflicts": self.conflicts,
                "dropped_edits": [d["key"] for d in self.dropped_edits],
                "support": [f"{s['state']} {s['key']}" for s in self.support if s.get("key")],
                "unlinked": self.unlinked, "retargeted": self.retargeted, "seconds": self.seconds}


# ------------------------------------------------------------------ Blender helpers

def _window_override():
    """Undo operators poll for a window; a timer or background Blender has no active one."""
    import bpy
    wm = bpy.context.window_manager
    window = bpy.context.window or (wm.windows[0] if wm.windows else None)
    if window is None:
        return None
    return bpy.context.temp_override(window=window, screen=window.screen)


def _undo(op: str, message: str = "") -> bool:
    import bpy
    try:
        override = _window_override()
        if override is None:
            return False
        with override:
            if op == "push":
                bpy.ops.ed.undo_push(message=message)
            else:
                getattr(bpy.ops.ed, op)()
        return True
    except (RuntimeError, AttributeError):
        return False


def _collection(name: str):
    """The local collection called name, linked under the scene (made if missing)."""
    import bpy
    scene = bpy.context.scene
    collection = bpy.data.collections.get(name)
    if collection is None or collection.library is not None:
        collection = bpy.data.collections.new(name)
    if collection not in scene.collection.children_recursive:
        scene.collection.children.link(collection)
    return collection


def _quarantine_collection():
    """LC_Quarantine: under the scene, excluded from every view layer, not rendered."""
    import bpy
    collection = _collection(QUARANTINE_COLLECTION)
    collection.hide_render = True
    for layer in bpy.context.scene.view_layers:
        stack = [layer.layer_collection]
        while stack:
            current = stack.pop()
            if current.collection == collection:
                current.exclude = True
            stack.extend(current.children)
    return collection


def _abs(path: str) -> str:
    import bpy
    return os.path.realpath(bpy.path.abspath(path))


def _datablocks():
    """Every datablock in bpy.data, of every kind (ownership.all_ids covers the kinds workers touch)."""
    import bpy
    if hasattr(bpy.data, "all_ids"):
        yield from bpy.data.all_ids
        return
    for prop in bpy.data.bl_rna.properties:
        if prop.type == "COLLECTION":
            for idb in getattr(bpy.data, prop.identifier):
                if isinstance(idb, bpy.types.ID):
                    yield idb


# ------------------------------------------------------------------ spawn

KEEP_UNUSED = ("materials", "node_groups", "images", "texts")  # Kinds the copy keeps though nothing uses them yet.


def _unused(palette=()) -> list:
    """What a saved copy would leave out: Blender writes no datablock nobody uses, so the palette
    the orchestrator makes before a wave (materials no object wears yet) was in no worker's copy;
    every worker remade it and the merge prefixed the duplicates (Crystal_Glow_Blue), failing the
    live material_on check. Local materials, node groups, images and text blocks with no users, and
    the palette's materials whatever uses them (an orphan mesh does not get written either); none
    with a fake user."""
    import bpy
    wanted, found = set(palette or ()), []
    for attr in KEEP_UNUSED:
        for idb in getattr(bpy.data, attr):
            if idb.library is not None or idb.use_fake_user or (attr == "images" and idb.type in ("RENDER_RESULT", "COMPOSITING")):
                continue
            if idb.users == 0 or (attr == "materials" and idb.name in wanted):
                found.append(idb)
    return found


def save_spawn(folder: Path, palette=()) -> Spawn:
    """Main thread, before a wave: a compressed copy of the scene with relative paths remapped,
    and its identity map, tokens and scene pointers. The open file is not touched: what nothing uses
    (and the palette) goes into the copy on a fake user that is taken off again once it is saved."""
    import bpy
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "spawn.blend"
    unused = _unused(palette)
    for idb in unused:
        idb.use_fake_user = True
    try:
        bpy.ops.wm.save_as_mainfile(filepath=str(path), copy=True, compress=True, relative_remap=True)
    finally:
        for idb in unused:
            idb.use_fake_user = False
    ids, tokens = ownership.spawn_state()
    scene = bpy.context.scene
    pointers = {attr: ownership.key_of(getattr(scene, attr)) if getattr(scene, attr) is not None else None
                for attr in ("camera", "world")}
    wave = f"{folder.parent.name}-{folder.name}" if folder.parent.name else folder.name
    return Spawn(wave, path, ids, tokens, pointers)


# ------------------------------------------------------------------ one worker

def _replace(old, new, missing: dict | None = None) -> list:
    """Everything that uses old uses new instead, shape keys included; returns what is left to delete:
    old, and old's shape keys when new took over their users.

    A mesh's shape keys (a curve's, a lattice's) are a datablock of their own, a Key, that Blender
    deletes with the mesh. Drivers elsewhere aim at the Key: Rain's gums and tongue open with the face's
    key_blocks["mouth_open"].value, a bone or an empty can follow it. Remapping only the mesh left them
    aimed at a Key about to go, and deleting it set every such target to None (the G1 rigging study,
    in both the Loopcut and the Codex loop). So the Key is remapped too. Remapping it also points old
    at new's Key (a mesh's link to its Key is an ordinary user), and deleting old would then delete
    new's Key with it; so old is detached from it first (_detach_key), and old's own Key, which nothing
    uses any more, is deleted beside old. When new has no shape keys, old's go with old (_settle reports
    what aimed at them). missing: new Key's session_uid -> names of old's key blocks it lacks, collected
    for _settle (a driver reading one of them no longer resolves)."""
    old_key, new_key = getattr(old, "shape_keys", None), getattr(new, "shape_keys", None)
    old.user_remap(new)
    if old_key is None or new_key is None or old_key == new_key:
        return [old]
    return [old, *_hand_over_key(old, new_key, missing)]


def _hand_over_key(owner, new_key, missing: dict | None = None) -> list:
    """owner's shape keys' users given to new_key (another mesh's), owner detached from it (_replace);
    returns owner's Key, which nothing uses now, to delete."""
    old_key = owner.shape_keys
    lacking = {kb.name for kb in old_key.key_blocks} - {kb.name for kb in new_key.key_blocks}
    if missing is not None and lacking:
        missing.setdefault(new_key.session_uid, set()).update(lacking)
    old_key.user_remap(new_key)
    _detach_key(owner)
    if getattr(owner, "shape_keys", None) is not None or new_key.user is None:
        raise RuntimeError(f"the shape keys of {owner.name} could not be handed over")
    return [old_key]


def _detach_key(idb) -> None:
    """idb's link to its shape keys cut, the Key left to its other users: Object.shape_key_clear, on a
    stand-in object that wears idb for the moment, frees the Key only when idb was its last user.
    Python has no other way to clear the link (Mesh.shape_keys is read-only)."""
    import bpy
    holder = bpy.data.objects.new("LC_Detach", idb)
    try:
        holder.shape_key_clear()
    finally:
        bpy.data.objects.remove(holder)


def _driver_targets() -> list:
    """(owner ID, holder, fcurve, variable, target) for every target a driver variable uses; holder is the
    owner or its embedded node tree."""
    found = []
    for idb in _datablocks():
        for holder in (idb, getattr(idb, "node_tree", None)):
            if holder is None or (holder is not idb and not getattr(holder, "is_embedded_data", False)):
                continue
            anim = getattr(holder, "animation_data", None)
            for fcurve in anim.drivers if anim is not None else ():
                for var in fcurve.driver.variables:
                    used = {"LOC_DIFF": 2, "ROTATION_DIFF": 2, "CONTEXT_PROP": 0}.get(var.type, 1)
                    found += [(idb, holder, fcurve, var, target) for target in list(var.targets)[:used]]
    return found


def _describe(idb, owners: dict | None = None, final: dict | None = None) -> str:
    """'object Indicator', 'the shape keys of mesh Face'. owners: Key session_uid -> its mesh, said, for a
    Key whose owner pointer a remap already moved; final: session_uid -> the live name a replacement
    takes once what it replaced is gone (FaceNew, not FaceNew.001)."""
    final = final or {}
    if idb.id_type == "KEY":
        name = (owners or {}).get(idb.session_uid)
        if name is None and idb.user is not None:
            name = f"{idb.user.id_type.lower()} {final.get(idb.user.session_uid, idb.user.name)}"
        return f"the shape keys of {name}" if name else f"shape keys {idb.name}"
    return f"{idb.id_type.lower()} {final.get(idb.session_uid, idb.name)}"


def _driver_path(holder, fcurve) -> str:
    """The driven property, with its index when it is an array: location[1], key_blocks["smile"].value."""
    try:
        value = holder.path_resolve(fcurve.data_path)
        is_array = hasattr(value, "__len__") and not isinstance(value, str)
    except (ValueError, TypeError):
        is_array = fcurve.array_index > 0
    return f"{fcurve.data_path}[{fcurve.array_index}]" if is_array else fcurve.data_path


_BLOCK = re.compile(r'key_blocks\["((?:[^"\\]|\\.)*)"\]')


def _key_read(target):
    """(Key, key block name) a SINGLE_PROP target reads through shape keys, else (None, None)."""
    idb, path = target.id, target.data_path or ""
    key = None
    if idb.id_type == "KEY":
        key = idb
    elif idb.id_type == "OBJECT" and path.startswith("data.shape_keys."):
        key, path = getattr(idb.data, "shape_keys", None), path[len("data.shape_keys."):]
    elif path.startswith("shape_keys."):
        key, path = getattr(idb, "shape_keys", None), path[len("shape_keys."):]
    found = _BLOCK.match(path) if key is not None else None
    return (key, found.group(1).replace('\\"', '"')) if found else (None, None)


def _settle(to_remove: list, live: set, successors: dict, missing: dict, uid2spawn: dict, final: dict,
            worker: str, report: MergeReport) -> list:
    """Main thread, right before the merge deletes what it replaced: nothing aimed at it may go to None
    unsaid. Returns more to delete with it.

    Live object data that only removed objects wore goes too (live: session_uids before the append): an
    owned object rebuilt on a new mesh left the old one with no users, and a save drops such data, so
    the drivers aimed at its shape keys died on save instead of now. Shape keys that go with their mesh
    are handed to the mesh that took its place when there is exactly one, through the object that
    wore it (successors: old session_uid -> what replaced it), and that one has every key block they
    have: the driver reads the same block on the new face. Then every driver left aimed at something
    removed is reported (report.unlinked: now unset), and so is one that reads a key block the
    replacing shape keys lack (missing, from _replace: it no longer resolves), so the agent can re-link
    it or accept the loss knowingly. final: session_uid -> the live name a replacement will take."""
    import bpy
    doomed = {idb.session_uid for idb in to_remove}
    objects = [idb for idb in to_remove if idb.id_type == "OBJECT"]
    extra = []
    for data in {ob.data for ob in objects if ob.data is not None}:
        wearers = sum(1 for ob in objects if ob.data == data)
        if (data.session_uid in live and data.session_uid not in doomed and data.library is None
                and not data.use_fake_user and data.users == wearers):
            extra.append(data)
            doomed.add(data.session_uid)
            report.replaced.append({"worker": worker, "key": f"{data.id_type}:{uid2spawn.get(data.session_uid, data.name)}",
                                    "name": data.name, "removed": True})
    targets = _driver_targets()
    owners = {}  # Key session_uid -> its mesh's description, taken before anything is handed over.
    going = []   # (Key, its mesh) that would go with the mesh.
    for idb in to_remove + extra:
        key = getattr(idb, "shape_keys", None)
        if key is not None and key.session_uid not in doomed:
            owners[key.session_uid] = f"{idb.id_type.lower()} {idb.name}"
            going.append((key, idb))
    for key, mesh in going:
        names = {kb.name for kb in key.key_blocks}
        heirs = []
        for ob in [ob for ob in bpy.data.objects if ob.data == mesh]:
            heir = getattr(getattr(successors.get(ob.session_uid), "data", None), "shape_keys", None)
            ok = heir is not None and heir.session_uid not in doomed and names <= {kb.name for kb in heir.key_blocks}
            heirs.append(heir if ok else None)
        heir_key = heirs[0] if heirs and heirs[0] is not None and all(h == heirs[0] for h in heirs) else None
        if heir_key is None:
            doomed.add(key.session_uid)
            continue
        old = _describe(key, owners)
        for owner, holder, fcurve, var, target in targets:
            if target.id == key and owner.session_uid not in doomed and owner != key:
                report.retargeted.append({"worker": worker, "owner": _describe(owner, final=final),
                                          "path": _driver_path(holder, fcurve), "variable": var.name, "old": old,
                                          "new": _describe(heir_key, final=final)})
        extra += _hand_over_key(mesh, heir_key)
        doomed.add(key.session_uid)
    for owner, holder, fcurve, var, target in targets:
        if owner.session_uid in doomed or target.id is None:
            continue
        entry = {"worker": worker, "owner": _describe(owner, final=final) + ("" if holder is owner else "'s node tree"),
                 "path": _driver_path(holder, fcurve), "variable": var.name}
        if target.id.session_uid in doomed:
            what = _describe(target.id, owners)
            report.unlinked.append({**entry, "target": what, "why": f"was aimed at {what}, which this apply removed: "
                                                                    f"now unset"})
        elif var.type == "SINGLE_PROP":
            key, block = _key_read(target)
            if key is not None and block in missing.get(key.session_uid, ()):
                what = _describe(key, final=final)
                report.unlinked.append({**entry, "target": what, "why": f'reads key block "{block}", which {what} '
                                                                        f"no longer have: it does not resolve"})
    return extra


def _set_name(idb, name: str) -> None:
    """Keep Blender's assigned name when RNA forbids renaming (for example, built-in fonts)."""
    if not idb.is_property_readonly("name"):
        idb.name = name


def _drop_weak_references(path: str) -> None:
    """Appending gives every datablock of a kind Blender can reuse on a later append (mesh,
    material, light, camera, world, image, node group, collection...) a weak reference to the file
    it came from: here the result.blend, deleted after the merge. The reference would stay in the
    user's file, and everything that walks the file's paths (bpy.utils.blend_paths) finds a missing
    file: every inspect_scene and render_job after the perfume_ad eval's merge refused the scene.
    Python can't clear one (read-only; emptying the path leaves "" behind), but a copy never
    carries it, so each such datablock is swapped for its copy, which takes its users and, when
    editable, its name. A mesh's copy has a copy of its shape keys, which takes their users too (_replace)."""
    import bpy
    source = os.path.realpath(path)
    marked = [idb for idb in _datablocks() if idb.library_weak_reference is not None
              and _abs(idb.library_weak_reference.filepath) == source]
    swaps = [(idb, idb.copy(), idb.name, getattr(getattr(idb, "shape_keys", None), "name", None)) for idb in marked]
    doomed = []
    for old, new, _name, _keys in swaps:
        doomed += _replace(old, new)
    bpy.data.batch_remove(doomed)
    for _old, new, name, keys in swaps:
        _set_name(new, name)
        if keys:  # The copy's own shape keys, named after the original's.
            _set_name(new.shape_keys, keys)


def _append(path: str) -> list:
    """Everything in a result, appended and made wholly local: the Library the append leaves
    behind removed, and the weak references to the result dropped. Returns the session_uids that
    arrived."""
    import bpy
    before = {idb.session_uid for idb in ownership.all_ids(local=False)}
    with bpy.data.libraries.load(path, link=False) as (source, target):
        for attr in ownership.ID_COLLECTIONS:
            if hasattr(source, attr):
                setattr(target, attr, list(getattr(source, attr)))
    for library in list(bpy.data.libraries):
        if _abs(library.filepath) == os.path.realpath(path):
            bpy.data.libraries.remove(library)
    _drop_weak_references(path)
    return [idb.session_uid for idb in ownership.all_ids(local=False) if idb.session_uid not in before]


def _units(owned_new: list) -> list:
    """Owned datablocks in units that stand or fall together: an object with its owned data."""
    uids = {idb.session_uid for idb in owned_new}
    units, used = [], set()
    for idb in owned_new:
        if idb.id_type == "OBJECT":
            unit = [idb]
            if idb.data is not None and idb.data.session_uid in uids:
                unit.append(idb.data)
            used |= {i.session_uid for i in unit}
            units.append(unit)
    return units + [[idb] for idb in owned_new if idb.session_uid not in used]


def _settings(material) -> tuple:
    """A material as someone making it again would set it: its values, its nodes by type with their
    enum settings, datablocks and unlinked inputs, and its links; node names and places, and a
    datablock's .001, aside. Rounded to 0.001."""
    import bpy

    def value(v):
        if isinstance(v, bpy.types.ID):
            return DOT_NUM.sub("", v.name)
        if isinstance(v, float):
            return round(v, 3)
        if isinstance(v, (set, frozenset)):  # An enum flag.
            return tuple(sorted(v))
        if hasattr(v, "__len__") and not isinstance(v, str):
            return tuple(value(x) for x in v)
        return v
    nodes, links, tree = [], [], material.node_tree
    for node in tree.nodes if tree is not None else ():
        inputs = tuple((s.identifier, value(s.default_value)) for s in node.inputs
                       if not s.is_linked and hasattr(s, "default_value"))
        settings = tuple((p.identifier, value(getattr(node, p.identifier, None))) for p in node.bl_rna.properties
                         if p.type == "ENUM" and not p.is_readonly or p.type == "POINTER" and p.identifier in ("image", "node_tree"))
        nodes.append(repr((node.bl_idname, settings, inputs)))
    if tree is not None:
        links = sorted((l.from_node.bl_idname, l.from_socket.identifier, l.to_node.bl_idname, l.to_socket.identifier)
                       for l in tree.links)
    return (value(material.diffuse_color), value(material.metallic), value(material.roughness),
            getattr(material, "blend_method", None), tuple(sorted(nodes)), tuple(links))


def _reuse_live_materials(created: list, live: dict, worker: str, report: MergeReport) -> list:
    """A material the worker made again under a live one's name (Glow_Blue.001 by Glow_Blue), or
    copied from one (VARIANT) and left as it was, the same in every setting, is the live one: what
    wears it is pointed there. The safety net behind the palette in the copy (save_spawn): a
    duplicate merged under the prefix fails the live checks that name the palette. A copy that was
    changed is the worker's own and merges. live: name -> the scene's local materials before this
    worker. Returns the copies."""
    copies = []
    for idb in [i for i in created if i.id_type == "MATERIAL"]:
        base = idb.get(VARIANT)
        original = live.get(base) if base else None
        original = original if original is not None else live.get(DOT_NUM.sub("", idb.name))
        if original is not None and original.session_uid != idb.session_uid and _settings(idb) == _settings(original):
            report.reused.append({"worker": worker, "name": original.name, "made": idb.name, "id_type": "MATERIAL",
                                  **({"copy": True} if base else {})})
            idb.user_remap(original)
            copies.append(idb)
    return copies


def _rename_created(created: list, before_names: set, worker: str, report: MergeReport) -> None:
    """The worker's own names are kept: the merge works by identity, not by name, so a prefix is not
    needed for correctness. The one thing that must not happen is two datablocks of the same type
    sharing a name in the live scene; _append (bpy.data.libraries.load) already resolves that itself
    the moment a created datablock's name was already taken, by a live datablock or an earlier
    worker's created one this wave, giving it Blender's own .001 suffix. This only notices that and
    puts it in the report; before_names ((id_type, name) pairs present before this worker's result
    was appended) tells a real collision from a worker that numbered its own three tables itself."""
    for idb in created:
        base = DOT_NUM.sub("", idb.name)
        if idb.name != base and (idb.id_type, base) in before_names:
            report.renamed.append(f"{worker}: {idb.id_type} {base} -> {idb.name}")


def _validate_meshes(idbs: list, worker: str, report: MergeReport) -> None:
    """Mesh.validate() on a worker's mesh datablocks before they reach the live scene: a bad loop
    index (from model code, or an import) segfaulted bm.from_mesh later, in a render or a check, far
    from where it was made. Fixed in place; noted in the merge report so it is never silent."""
    for idb in idbs:
        if idb.id_type == "MESH" and idb.validate(verbose=False, clean_customdata=True):
            report.fixed.append({"worker": worker, "name": idb.name})


def _name_meshes(created: list) -> None:
    """A created object's own mesh takes the object's name. Joining parts keeps the active part's
    mesh, so the Sign held the mesh Sign_FootA, and exports name what they write by the mesh. Only a
    mesh the worker made and only this object uses, and never onto another mesh's name."""
    import bpy
    made = {idb.session_uid for idb in created}
    for ob in [idb for idb in created if idb.id_type == "OBJECT" and idb.type == "MESH" and idb.data is not None]:
        data = ob.data
        if data.session_uid not in made or data.users - int(data.use_fake_user) != 1 or data.name == ob.name:
            continue
        if bpy.data.meshes.get(ob.name) in (None, data):
            _set_name(data, ob.name)


def _wants_collections(brief: dict) -> bool:
    """Whether the user asked for collections: a requirement of the brief quotes the word. A workspace
    build (workspace.py) keeps the collections it made, as the single agent's own decision."""
    return bool(brief.get("keep_collections")) or any(
        isinstance(r, dict) and "collection" in str(r.get("quote") or "").lower() for r in brief.get("requirements") or ())


def _main_collection():
    """The collection the scene's own objects are in: the one holding most of them (the startup
    file's "Collection"), else the scene collection."""
    import bpy
    scene = bpy.context.scene
    counts, by_pointer = {scene.collection.as_pointer(): 0}, {scene.collection.as_pointer(): scene.collection}
    for ob in scene.objects:
        if ob.get(QUARANTINE):
            continue
        for collection in ob.users_collection:
            if collection.library is None and collection.name != QUARANTINE_COLLECTION and not collection.name.startswith(STAGING):
                key = collection.as_pointer()
                counts[key], by_pointer[key] = counts.get(key, 0) + 1, collection
    return by_pointer[max(counts, key=lambda k: (counts[k], k == scene.collection.as_pointer()))]


def _counted(brief: dict) -> str:
    """The collection a count check of the brief names, or ""."""
    return next((c["collection"] for c in brief.get("checks") or () if isinstance(c, dict) and c.get("kind") == "count"
                 and isinstance(c.get("collection"), str) and c["collection"].strip()), "")


def _home_name(brief: dict, prefix: str) -> str:
    """Where a worker's created objects go when it put them in no collection of its own choosing
    (_worker_homes): the collection a count check of its brief names; the one named after its prefix
    when the user asked for collections; else ("") the scene's main collection. A collection per
    worker that nobody asked for left every wave's props scattered."""
    return _counted(brief) or (prefix if _wants_collections(brief) else "")


def _home(name: str):
    """The collection _home_name names (made if missing), or the scene's main collection for ""."""
    import bpy
    scene = bpy.context.scene
    if name == scene.collection.name:
        return scene.collection
    return _collection(name) if name else _main_collection()


def _asked(brief: dict, shared: dict) -> Callable[[list], bool]:
    """Whether a collection the worker made is wanted: the user asked for collections, or the brief or
    the wave's shared notes name it (20260929-230207: "Put each final mesh in ShopAssetPack")."""
    notes = {"defaults": [str(shared.get("notes") or "")]} if isinstance(shared, dict) else {}
    wants = _wants_collections(brief)
    return lambda names: wants or any(ownership.pinned(n, brief) or ownership.pinned(n, notes) for n in names)


def _made_collection(name: str, parent, worker: str, report: "MergeReport"):
    """The live collection a worker's made one becomes: one of that name already here (an earlier
    worker of the wave made it too), else a new one under parent, reported as created."""
    import bpy
    live = bpy.data.collections.get(name)
    if live is not None and live.library is None and not live.name.startswith((QUARANTINE_COLLECTION, STAGING)):
        return live
    live = bpy.data.collections.new(name)
    parent.children.link(live)
    report.created.append({"worker": worker, "name": live.name, "id_type": "COLLECTION", "type": "",
                           "parent": parent.name})
    return live


def _worker_homes(objects: list, live_of, asked, worker: str, report: "MergeReport") -> dict:
    """session_uid -> the live collections each of these created objects goes back into: where the
    worker put it (ownership.collections_of, its COLLECTIONS tag, removed here). A spawn collection it
    was linked into on purpose stays its home; the one that was active, where every new object lands
    by default, only when asked for by name (else home, _home_name, as always). A collection the worker
    made is made live, or found by name, only when asked for (_asked); else home too."""
    import bpy
    homes = {}
    for ob in objects:
        raw = ob.get(COLLECTIONS)
        if raw is None:
            continue
        del ob[COLLECTIONS]
        try:
            entries = json.loads(raw)
        except ValueError:
            continue
        found = {}
        for entry in entries if isinstance(entries, list) else ():
            live = None
            if isinstance(entry, dict) and entry.get("spawn") and (not entry.get("active") or asked([entry["spawn"]])):
                live = live_of(f"COLLECTION:{entry['spawn']}")
            elif isinstance(entry, dict) and entry.get("made") and asked(entry["made"]):
                live = live_of(f"COLLECTION:{entry['under']}") if entry.get("under") else bpy.context.scene.collection
                for name in entry["made"] if live is not None else ():
                    live = _made_collection(str(name), live, worker, report)
            if live is not None and live.library is None and not live.name.startswith((QUARANTINE_COLLECTION, STAGING)):
                found[live.session_uid] = live
        if found:
            homes[ob.session_uid] = list(found.values())
    return homes


def _worker_collections(created: list, keep) -> set:
    """Collections the worker made that arrived with its result (a child of a collection it owns, an
    instancer's source). One that something other than a collection uses (an instancer, a modifier)
    has a purpose: it stays, and what is in it is shown through it. Any other is dropped, its
    objects going home with the rest, unless it is wanted (keep([name]), _asked): then it stays,
    under the scene when nothing holds it. Returns the session_uids of the objects shown through
    one, and of the collections dropped."""
    import bpy
    shown, dropped, scene = set(), set(), bpy.context.scene
    made = [idb for idb in created if idb.id_type == "COLLECTION"]
    users = bpy.data.user_map(subset=made) if made else {}
    for collection in made:
        if any(not isinstance(u, (bpy.types.Collection, bpy.types.Scene)) for u in users.get(collection, ())):
            shown |= {ob.session_uid for ob in collection.all_objects}
        elif keep([collection.name]):  # Asked for: its objects are shown through it, under the scene when nothing holds it.
            if not collection.users:
                scene.collection.children.link(collection)
            shown |= {ob.session_uid for ob in collection.all_objects}
        else:
            for parent in [c for c in bpy.data.collections if collection.name in c.children] + [scene.collection]:
                if collection.name in parent.children:
                    parent.children.unlink(collection)
            dropped.add(collection.session_uid)
            bpy.data.collections.remove(collection)
    return shown, dropped


def _link_created(staging, created: list, home: str, verdicts: dict, worker: str, wave: str,
                  report: MergeReport, shown: set = frozenset(), homes: dict | None = None) -> None:
    """Created objects into the collections the worker put them in (homes, _worker_homes) or else
    home (_home_name), or quarantine when held back (verdicts, from
    ownership.verdicts: outside what the role and the brief's aspects may create, and not asked for
    by the brief); staging gone. What merged though outside the role is reported as admitted, with
    the brief's words that let it through; what is held back, with the reason. An object shown
    through a collection the worker made (an instancer's source) stays only there.
    Only what was created: an owned object the worker left as it was (or that conflicted) was
    remapped to the live one, which so came into staging too, unstamped. Taking it for created
    put the user's own Light into LC_Quarantine, untagged, so no acceptance ever took it out
    (shared_support eval: a modeling worker owning the scene light)."""
    import bpy
    dest, made = None, {idb.session_uid for idb in created}

    def merges(idb) -> bool:
        return verdicts.get(idb.session_uid, (True, ""))[0]
    for ob in list(staging.objects):
        if ob.session_uid in made:
            if not merges(ob):
                _quarantine_collection().objects.link(ob)
                ob[HOME] = home  # Where accept_quarantine puts it.
            elif ob.session_uid in (homes or {}):
                for collection in homes[ob.session_uid]:
                    if ob.name not in collection.objects:
                        collection.objects.link(ob)
            elif ob.session_uid not in shown:
                dest = dest or _home(home)
                if ob.name not in dest.objects:
                    dest.objects.link(ob)
        staging.objects.unlink(ob)
    bpy.data.collections.remove(staging)
    quarantined = [idb for idb in created if not merges(idb)]
    objects = [idb for idb in quarantined if idb.id_type == "OBJECT"]
    for ob in objects:
        if ob.data is not None and ob.data.library is None and not ob.data.get(STAMP):
            ob.data[QUARANTINE] = f"{wave}/{worker}"  # Goes, and is accepted, with its object.
    for idb in quarantined:
        idb[QUARANTINE] = f"{wave}/{worker}"
        if idb.id_type != "OBJECT" and any(ob.data == idb for ob in objects):
            continue
        report.quarantined.append({"worker": worker, "wave": wave, "name": idb.name, "id_type": idb.id_type,
                                   "type": idb.type if idb.id_type == "OBJECT" else "",
                                   "hidden": idb.id_type == "OBJECT", "reason": verdicts[idb.session_uid][1]})
    admitted = [idb for idb in created if merges(idb) and verdicts.get(idb.session_uid, (True, ""))[1]]
    merged_objects = {ob.session_uid for ob in admitted if ob.id_type == "OBJECT"}
    for idb in admitted:
        if idb.id_type != "OBJECT" and any(ob.data == idb and ob.session_uid in merged_objects for ob in admitted
                                           if ob.id_type == "OBJECT"):
            continue  # A light's data goes with its light: one line.
        report.admitted.append({"worker": worker, "name": idb.name, "id_type": idb.id_type,
                                "type": idb.type if idb.id_type == "OBJECT" else "",
                                "reason": verdicts[idb.session_uid][1]})


def _held(ob) -> list[str]:
    """What a merged object hangs on that sits in quarantine: its parent, a constraint's or a
    modifier's target ("parent Lit_Spot_root", "Track To target Lit_Aim")."""
    import bpy

    def kept(target) -> bool:
        return isinstance(target, bpy.types.Object) and target != ob and bool(target.get(QUARANTINE))
    held = [f"parent {ob.parent.name}"] if kept(ob.parent) else []
    for item in list(ob.constraints) + list(ob.modifiers):
        targets = [getattr(item, p.identifier, None) for p in item.bl_rna.properties if p.type == "POINTER"]
        targets += [getattr(t, "target", None) for t in getattr(item, "targets", ())]  # Armature constraint.
        if isinstance(item, bpy.types.NodesModifier):
            targets += [item[key] for key in item.keys()]  # Its inputs.
        for target in dict.fromkeys(t for t in targets if kept(t)):
            held.append(f"{item.name} target {target.name}")
    return held


def _merge_worker(spawn: Spawn, result: dict, uid2spawn: dict, report: MergeReport, found: list, moved: dict) -> None:
    """moved: key -> session_uid of what an earlier worker of the wave replaced it with."""
    import bpy
    brief, manifest = result.get("brief") or {}, result.get("manifest") or {}
    worker = str(brief.get("id") or brief.get("worker") or manifest.get("prefix") or "worker")
    prefix = str(brief.get("prefix") or manifest.get("prefix") or worker)
    role = str(brief.get("role") or manifest.get("role") or "")
    aspects = list(brief.get("aspects") or manifest.get("aspects") or ())
    owned = set(manifest.get("owned") or brief.get("owned") or ())
    path = manifest.get("result")
    if not path or not os.path.isfile(path):
        report.errors.append(f"{worker}: no result file, nothing merged")
        return
    by_uid = {idb.session_uid: idb for idb in ownership.all_ids(local=False)}
    before_names = {(idb.id_type, idb.name) for idb in by_uid.values() if idb.library is None}
    new = set(_append(path))
    arrived = [idb for idb in ownership.all_ids(local=False) if idb.session_uid in new]
    staging = bpy.data.collections.new(f"{STAGING}{worker}")  # Not linked to the scene.

    def live_of(key: str):  # A copy this worker has of what an earlier one replaced goes to the replacement.
        return by_uid.get(moved.get(key)) or by_uid.get(spawn.ids.get(key))  # The original again after an undo.

    successors, missing = {}, {}  # For _settle: old session_uid -> its replacement; Key -> block names it lacks.

    def swap(old, new) -> list:
        successors[old.session_uid] = new
        return _replace(old, new, missing)

    created, copies, owned_new, to_remove = [], [], [], []
    live_linked = {(_abs(i.library.filepath), i.id_type, i.name): i for i in by_uid.values() if i.library}
    pointed = {}
    for idb in arrived:
        stamp = idb.get(STAMP)
        if idb.get(POINTER):
            pointed[idb[POINTER]] = idb.session_uid
        if idb.library is not None:  # Linked data can't be stamped: identity is (library, name).
            live = live_linked.get((_abs(idb.library.filepath), idb.id_type, idb.name))
            if live is not None:
                to_remove += swap(idb, live)
            else:
                report.created.append({"worker": worker, "name": idb.name, "id_type": idb.id_type, "type": "",
                                       "parent": None, "linked": idb.library.filepath})
        elif not stamp:
            created.append(idb)
        elif f"{idb.id_type}:{stamp}" in owned:
            owned_new.append(idb)
        else:
            copies.append(idb)
    _validate_meshes(created + owned_new, worker, report)  # Before anything created reaches the scene.
    for idb in created + owned_new:
        if idb.id_type == "OBJECT":
            staging.objects.link(idb)
    live_materials = {i.name: i for i in by_uid.values() if i.id_type == "MATERIAL" and i.library is None}
    to_remove += _reuse_live_materials(created, live_materials, worker, report)

    # Copies of what the worker doesn't own: a change is dropped and reported; references go live.
    for idb in copies:
        key = f"{idb.id_type}:{idb[STAMP]}"
        live = live_of(key)
        if ownership.token(idb, uid2spawn) != spawn.tokens.get(key):
            report.dropped_edits.append({"worker": worker, "key": key, "name": live.name if live else idb[STAMP],
                                         "worker_value": ownership.describe(idb),
                                         "live_value": ownership.describe(live)})
        if live is None:
            report.errors.append(f"{worker}: {key} was deleted from the live scene during the wave; "
                                 f"what referred to it keeps a copy")
            del idb[STAMP]
            continue
        to_remove += swap(idb, live)

    # Owned: replace the live original unless the user changed or deleted it during the wave.
    # What the worker owns but left as it was stays the live one, whatever the user did to it.
    names, key_names = {}, {}
    for members in _units(owned_new):
        keys = [f"{i.id_type}:{i[STAMP]}" for i in members]
        if all(ownership.token(i, uid2spawn) == spawn.tokens.get(k) for k, i in zip(keys, members)):
            for key, idb in zip(keys, members):
                to_remove += swap(idb, live_of(key)) if live_of(key) is not None else [idb]
            continue
        gone = [k for k in keys if live_of(k) is None]
        stale = [k for k in keys if live_of(k) is not None and ownership.token(live_of(k), uid2spawn) != spawn.expected(k)]
        if gone or stale:
            report.conflicts.append({
                "worker": worker, "keys": keys,
                "reason": "deleted in the live scene during the wave" if gone else
                          "changed in the live scene during the wave; the live version was kept",
                "live_now": {k: ownership.describe(live_of(k)) for k in keys},
                "worker_value": {k: ownership.describe(i) for k, i in zip(keys, members)}})
            for idb in members:
                live = live_of(f"{idb.id_type}:{idb[STAMP]}")
                to_remove += swap(idb, live) if live is not None else [idb]
            continue
        for key, idb in zip(keys, members):
            live = live_of(key)
            names[idb.session_uid] = live.name
            if getattr(live, "shape_keys", None) is not None:
                key_names[idb.session_uid] = live.shape_keys.name
            to_remove += swap(live, idb)
            report.replaced.append({"worker": worker, "key": key, "name": live.name})

    # Owned datablocks the worker removed go from the live scene too, unless the user changed them.
    for key in manifest.get("owned_deleted") or ():
        live = live_of(key)
        if live is None:
            continue
        if ownership.token(live, uid2spawn) != spawn.expected(key):
            report.conflicts.append({"worker": worker, "keys": [key], "worker_value": {key: "removed"},
                                     "reason": "removed by the worker but changed in the live scene; kept",
                                     "live_now": {key: ownership.describe(live)}})
            continue
        report.replaced.append({"worker": worker, "key": key, "name": live.name, "removed": True})
        to_remove.append(live)

    to_remove += _settle(to_remove, set(by_uid), successors, missing, uid2spawn, names, worker, report)
    removed = {idb.session_uid for idb in to_remove}
    owned_new = [(idb.session_uid, idb) for idb in owned_new]  # Uids now: a removed datablock can't be asked.
    created = [(idb.session_uid, idb) for idb in created]
    for idb in to_remove:
        if idb.id_type == "OBJECT":
            for collection in list(idb.users_collection):
                collection.objects.unlink(idb)
    bpy.data.batch_remove(to_remove)
    del to_remove  # No references to removed datablocks past this point.
    owned_new = [idb for uid, idb in owned_new if uid not in removed]
    created = [idb for uid, idb in created if uid not in removed]
    replaced = {f"{idb.id_type}:{idb[STAMP]}": idb.session_uid for idb in owned_new}
    for idb in owned_new:
        if idb.name != names[idb.session_uid]:
            _set_name(idb, names[idb.session_uid])  # The live name: a rename the user made mid-wave stays.
            if idb.name != names[idb.session_uid]:
                report.errors.append(f"{worker}: could not restore the name {names[idb.session_uid]} (got {idb.name})")
        if idb.session_uid in key_names and getattr(idb, "shape_keys", None) is not None:
            _set_name(idb.shape_keys, key_names[idb.session_uid])  # The live shape keys' name, theirs gone with them.
    moved.update(replaced)
    _rename_created(created, before_names, worker, report)
    _name_meshes(created)
    uids = [(idb.session_uid, idb) for idb in created]  # Uids now: a dropped collection can't be asked.
    asked = _asked(brief, result.get("shared") or {})
    shown, dropped = _worker_collections(created, asked)
    created = [idb for uid, idb in uids if uid not in dropped]
    own_uids = {moved.get(k, spawn.ids.get(k)) for k in owned} | {idb.session_uid for idb in owned_new}
    verdicts = ownership.verdicts(created, role, aspects, brief, own_uids)
    merging = [idb for idb in created if idb.id_type == "OBJECT" and idb.session_uid not in shown
               and verdicts.get(idb.session_uid, (True, ""))[0]]
    homes = _worker_homes(merging, live_of, asked, worker, report)
    if homes and _counted(brief):  # A count check measures its collection: what it counts goes there too.
        homes = {uid: [_home(_counted(brief)), *found] for uid, found in homes.items()}
    for ob in [idb for idb in created if idb.id_type == "OBJECT" and COLLECTIONS in idb]:
        del ob[COLLECTIONS]  # Held back or shown through a collection: the tag goes all the same.
    _link_created(staging, created, _home_name(brief, prefix), verdicts, worker, spawn.wave, report, shown, homes)
    held = {idb.session_uid: _held(idb) for idb in created + owned_new
            if idb.id_type == "OBJECT" and not idb.get(QUARANTINE)}
    for entry in report.replaced:
        if entry["worker"] == worker and held.get(replaced.get(entry["key"])):
            entry["held"] = held[replaced[entry["key"]]]
    for idb in owned_new:
        del idb[STAMP]
    for idb in created:
        entry = {"worker": worker, "name": idb.name, "id_type": idb.id_type,
                 "type": idb.type if idb.id_type == "OBJECT" else "",
                 "parent": idb.parent.name if idb.id_type == "OBJECT" and idb.parent else None}
        if idb.id_type == "MATERIAL" and idb.get(VARIANT):
            entry["variant_of"] = idb[VARIANT]
        if held.get(idb.session_uid):
            entry["held"] = held[idb.session_uid]
        report.created.append(entry)
    for idb in created + owned_new:  # Nothing uses a text, or an image painted but not assigned yet.
        if idb.id_type in ownership.SUPPORT and (idb.id_type == "TEXT" or idb.users == 0) and not idb.get(QUARANTINE):
            idb.use_fake_user = True
    try:  # The check never costs the merge.
        _support(manifest, created + owned_new, worker, report)
    except Exception as ex:  # noqa: BLE001
        report.errors.append(f"{worker}: its text blocks and images could not be checked live ({type(ex).__name__}: {ex})")
    found.append({"worker": worker, "aspects": set(brief.get("aspects") or ()), "pointers": manifest.get("pointers") or {},
                  "pointed": {aspect: uid for aspect, uid in pointed.items() if uid not in removed},
                  "replaced": replaced})


def _support(manifest: dict, merged: list, worker: str, report: MergeReport) -> None:
    """The worker's text blocks and images as its manifest lists them (ownership.support_entry),
    checked live: a new or updated one is found among what merged by its token (its live name, and
    why when Blender's .001 renamed it), else it is missing, with why (a conflict kept the live one,
    or it did not arrive); held when it sits in quarantine; skipped ones as the child said.
    merged: what this worker's merge left live, created and replaced."""
    live = {}  # (type, token) -> what merged with that content, each matched once.
    for idb in merged:
        if idb.id_type in ownership.SUPPORT:
            live.setdefault((idb.id_type, ownership.token(idb)), []).append(idb)
    conflicts = {k for c in report.conflicts if c.get("worker") == worker for k in c.get("keys") or ()}
    reasons = {q["name"]: q.get("reason") or "outside its role" for q in report.quarantined if q.get("worker") == worker}
    for entry in manifest.get("support") or ():
        if not isinstance(entry, dict):
            continue
        if "more" in entry or "error" in entry:
            report.support.append({"worker": worker, **{k: entry[k] for k in ("more", "error") if k in entry}})
            continue
        key = str(entry.get("key") or "")
        kind, _, name = key.partition(":")
        out = {"worker": worker, "key": key, "kind": kind, "name": name, "state": entry.get("state") or "",
               "about": entry.get("about") or "", **{k: entry[k] for k in ("hash", "why") if entry.get(k)}}
        if out["state"] in ("new", "updated"):
            matches = live.get((kind, entry.get("token"))) or []
            found = matches.pop(0) if matches else None
            if found is None and key in conflicts:  # Its conflict line says it (lines); the orchestrator's too.
                out.update(state="missing", why="changed in the live scene during the wave; the live version was kept",
                           conflict=True)
            elif found is None:
                out.update(state="missing", why="not in the scene after the merge")
            else:
                if found.name != name and out["state"] == "new":
                    out["why"] = f"{name} was taken, so Blender named it {found.name}"
                out["name"] = found.name
                if found.get(QUARANTINE):
                    out["state"], out["why"] = "held", reasons.get(found.name, "outside its role")
        report.support.append(out)


def _apply_pointers(spawn: Spawn, found: list, before: dict, report: MergeReport) -> None:
    """scene.camera / scene.world from a worker with the camera / lighting aspect that changed it,
    when the live pointer was still what it was at spawn; otherwise a conflict."""
    import bpy
    scene = bpy.context.scene
    by_uid = {idb.session_uid: idb for idb in ownership.all_ids(local=False)}
    for entry in found:
        for aspect, attr in _POINTERS:
            wanted = entry["pointers"].get(attr, spawn.pointers.get(attr))
            if aspect not in entry["aspects"] or wanted == spawn.pointers.get(attr):
                continue
            if wanted is None:
                target = None
            elif attr in entry["pointed"]:
                target = by_uid.get(entry["pointed"][attr])
            else:  # An existing datablock: its merged replacement when the worker owned it.
                target = by_uid.get(entry["replaced"].get(wanted) or spawn.ids.get(wanted))
            if wanted is not None and target is None:
                report.errors.append(f"{entry['worker']}: scene.{attr} target {wanted} did not arrive")
                continue
            if before[attr] != spawn.pointers.get(attr):
                current = getattr(scene, attr)
                report.conflicts.append({"worker": entry["worker"], "pointer": f"scene.{attr}",
                                         "reason": f"scene.{attr} was changed in the live scene during the wave; kept",
                                         "live_now": current.name if current else None,
                                         "worker_value": target.name if target else None})
                continue
            setattr(scene, attr, target)
            report.replaced.append({"worker": entry["worker"], "key": f"SCENE:{attr}",
                                    "name": target.name if target else None, "pointer": attr})


def _live_pointers(spawn: Spawn) -> dict:
    import bpy
    uid2key = {uid: key for key, uid in spawn.ids.items()}
    scene, pointers = bpy.context.scene, {}
    for attr in ("camera", "world"):
        current = getattr(scene, attr)
        pointers[attr] = None if current is None else uid2key.get(current.session_uid, f"new:{current.name}")
    return pointers


def _cleanup(spawn: Spawn, results: list, keep_spawn: bool = False) -> None:
    for path in ([] if keep_spawn else [spawn.path]) + [(r.get("manifest") or {}).get("result") for r in results]:
        if path:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass


def _number(spawn: Spawn) -> str:
    found = re.search(r"(\d+)$", Path(spawn.path).parent.name)
    return found.group(1) if found else spawn.wave


# ------------------------------------------------------------------ the wave

def merge_wave(spawn: Spawn, results: list[dict], checkpoint: Callable[[], str],
               keep_spawn: bool = False) -> MergeReport:
    """Main thread, one call: checkpoint, one undo step, each worker's result merged in order, the
    scene pointers, quarantine, then the temporary files deleted (every result, and spawn.blend
    unless keep_spawn: more of the wave's workers merge later), whether or not the merge succeeded.
    results[i] = {"brief": normalised brief (id, prefix, role, aspects), "manifest": the child's
    manifest (ownership.write_result), "shared"?: the wave's shared part (its notes may name a
    collection, _asked)}. Raises MergeError, recording whether the scene is unchanged."""
    t0 = time.time()
    report = MergeReport()
    try:
        try:
            report.checkpoint = checkpoint() or ""
        except Exception as ex:  # noqa: BLE001 - anything: without a checkpoint nothing is merged.
            raise MergeError(f"Not merged: no checkpoint could be saved first ({ex}).", scene_unchanged=True) from ex
        number = _number(spawn)
        pushed = _undo("push", f"Loopcut: before merge wave {number}")
        if not pushed:
            report.errors.append("No undo step before the merge (no window); the checkpoint covers it.")
        try:
            before, found, uid2spawn = _live_pointers(spawn), [], spawn.uid2spawn()
            moved = dict(getattr(spawn, "moved", None) or {})  # Kept only if this merge stands.
            for result in results:
                _merge_worker(spawn, result, uid2spawn, report, found, moved)
            _apply_pointers(spawn, found, before, report)
            for idb in ownership.all_ids():
                if idb.get(POINTER):
                    del idb[POINTER]
        except Exception as ex:  # noqa: BLE001 - any failure: back to before the merge.
            rolled = pushed and _undo("push", "Loopcut: failed merge") and _undo("undo")
            where = ("the scene is back as it was before the merge" if rolled else
                     f"the scene may be partly merged: restore checkpoint {report.checkpoint}")
            raise MergeError(f"Merge failed: {type(ex).__name__}: {ex}; {where}.", scene_unchanged=rolled) from ex
        spawn.moved = moved
        _undo("push", f"Loopcut: merge wave {number}")
    finally:
        _cleanup(spawn, results, keep_spawn)
    report.seconds = round(time.time() - t0, 3)
    return report


def _accepted_home(ob):
    """Where the merge put the rest of the object's worker (_home), as it tagged it; the scene's main
    collection for one tagged before it did."""
    return _home(str(ob.get(HOME) or ""))


def accept_quarantine(wave: str, names: list[str] | None = None, *, resolved: list | None = None) -> str:
    """Main thread: move a wave's quarantined objects (or only those named) from LC_Quarantine
    into the collection the rest of their worker's work went to, and clear the tags. Returns a
    sentence for a notice. If supplied, resolved receives the datablocks actually accepted, with
    their worker attribution, only after the operation succeeds."""
    import bpy
    tagged = [idb for idb in ownership.all_ids() if str(idb.get(QUARANTINE, "")).rsplit("/", 1)[0] == wave]
    if names:
        wanted = set(names)
        objects = [idb for idb in tagged if idb.id_type == "OBJECT" and idb.name in wanted]
        tagged = [idb for idb in tagged if idb.name in wanted or any(ob.data == idb for ob in objects)]
    if not tagged:
        return f"Nothing from wave {wave} is in quarantine" + (f" named {', '.join(names)}." if names else ".")
    quarantine = bpy.data.collections.get(QUARANTINE_COLLECTION)
    accepted = []
    entries = [{"worker": str(idb.get(QUARANTINE)).rsplit("/", 1)[-1],
                "name": idb.name, "id_type": idb.id_type} for idb in tagged]
    for idb in tagged:
        if idb.id_type == "OBJECT":
            dest = _accepted_home(idb)
            if quarantine is not None and idb.name in quarantine.objects:
                quarantine.objects.unlink(idb)
            if idb.name not in dest.objects:
                dest.objects.link(idb)
            accepted.append(f"{idb.name} ({idb.type.lower()}) into {dest.name}")
        elif not any(ob.data == idb for ob in tagged if ob.id_type == "OBJECT"):
            accepted.append(f"{idb.name} ({idb.id_type.lower()})")
        del idb[QUARANTINE]
        if HOME in idb:
            del idb[HOME]
    if quarantine is not None and not quarantine.objects and not quarantine.children:
        bpy.data.collections.remove(quarantine)
    _undo("push", "Loopcut: accept quarantine")
    if resolved is not None:
        resolved.extend(entries)
    return f"Accepted from wave {wave}: {', '.join(accepted)}."
