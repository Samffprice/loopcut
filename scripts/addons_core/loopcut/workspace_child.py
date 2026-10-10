"""The workspace's background Blender (workspace.py), one op per run, no model, no keys, no network:

    Blender -b --factory-startup --disable-autoexec <file.blend> --python-exit-code 1 \\
        --python workspace_child.py -- <op> <request.json> <reply.json>

- stamp, on a new snapshot: every local datablock stamped with its name (ownership.stamp_all) and
  every scene with the snapshot's number (workspace.SNAPSHOT_PROP), saved in place. A build that
  opens it, from run_blender or from the agent's own shell, keeps the stamps in what it saves.
- prepare, on a build's result: copies of a source datablock lose the stamp (unstamp_copies); one the
  build made anew under a source datablock's name and type, the original gone from the result, takes
  its stamp (a rebuilt TreasureChest is the TreasureChest, not TreasureChest.001 beside it); what an
  earlier apply created and the build made again is stamped and owned, so it replaces the live one;
  owns is resolved in the result (ownership.expand_owns), and a name the result no longer has in the
  snapshot the build came from (_removed): the build removed it, and the merge removes it live; images
  loaded from the workspace are packed;
  then ownership.write_result writes what was created and what is owned, and the reply carries its
  manifest, the result scene's settings (workspace.scene_settings), which snapshot it was built from,
  what was packed and any problem (a name in owns the result does not have: nothing is applied).
"""

import json
import os
import sys
import traceback
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
import worker_child  # noqa: E402  (its package loader; nothing of it runs on import)


def _snapshot_number():
    for scene in [bpy.context.scene, *bpy.data.scenes]:
        if scene is not None and scene.get("lc_snapshot") is not None:
            try:
                return int(scene["lc_snapshot"])
            except (TypeError, ValueError):
                return None
    return None


def stamp(loopcut, request: dict) -> dict:
    ownership = loopcut.ownership
    ownership.stamp_all()
    for scene in bpy.data.scenes:
        scene[loopcut.workspace.SNAPSHOT_PROP] = int(request["number"])
    bpy.context.preferences.filepaths.save_version = 0  # No source.blend1 beside it.
    bpy.ops.wm.save_mainfile(compress=True)
    return {"stamped": sum(1 for _ in ownership.all_ids())}


def _inside(path: str, root: Path) -> bool:
    try:
        resolved = Path(bpy.path.abspath(path)).resolve()
    except (OSError, ValueError):
        return False
    return resolved == root or root in resolved.parents


def _removed(ownership, names: list, tokens: dict) -> tuple[list, set, list]:
    """owns split into the names the result has (for expand_owns) and the keys of those it no longer has
    but the snapshot did: the build removed them (the tool's owns: "changed or removed"). A bare name
    means the object of that name, else the one datablock of that name; "TYPE:Name" means that one.
    Returns (present names, removed keys, problems)."""
    present, removed, problems = [], set(), []
    for name in names:
        if ownership.find(name) is not None:
            present.append(name)
            continue
        kind, _, rest = name.partition(":")
        if rest and f"{kind}:{rest}" in tokens:
            removed.add(f"{kind}:{rest}")
            continue
        found = [key for key in tokens if key.split(":", 1)[1] == name]
        found = [key for key in found if key.startswith("OBJECT:")] or found
        if len(found) == 1:
            removed.add(found[0])
        elif found:
            problems.append(f"{name!r} is not in the result and names {', '.join(sorted(found))} in the source: "
                            f"say which, as TYPE:Name")
        else:
            present.append(name)  # expand_owns says it is unknown.
    return present, removed, problems


def prepare(loopcut, request: dict) -> dict:
    ownership, workspace = loopcut.ownership, loopcut.workspace
    number = _snapshot_number()
    snapshot = (request.get("snapshots") or {}).get(str(number)) if number is not None else None
    tokens = (snapshot or {}).get("tokens") or {}
    recreated = (snapshot or {}).get("recreated") if snapshot else request.get("recreated")
    ownership.SPAWN_TOKENS.clear()
    ownership.SPAWN_TOKENS.update(tokens)
    ownership.unstamp_copies()
    owned, problems, found = set(), [], []
    recreated = set(recreated or ())
    stamped = {ownership.key_of(idb) for idb in ownership.all_ids() if idb.get(ownership.STAMP)}
    for idb in list(ownership.all_ids()):
        key = f"{idb.id_type}:{idb.name}"
        if idb.get(ownership.STAMP) or key in stamped or (key not in tokens and key not in recreated):
            continue
        idb[ownership.STAMP] = idb.name  # Made anew under the name of one the result no longer has.
        stamped.add(key)
        found.append(key)
        if key in recreated:
            owned.add(key)
    present, removed, unclear = _removed(ownership, request.get("owns") or [], tokens)
    owned |= removed
    problems += unclear
    try:
        owned |= ownership.expand_owns(present, [])
    except ValueError as ex:
        problems.append(f"owns: {ex} in the result or the source it was built from")
    # The scene the build came from, active again, so the pointers and settings are read from it: a build
    # that made a studio scene of its own and saved with it active must not change the user's camera.
    source_scene = bpy.data.scenes.get((snapshot or {}).get("scene") or "") if snapshot else None
    active = bpy.context.scene
    moved_from = ""
    if source_scene is not None and active is not None and source_scene != active and bpy.context.window is not None:
        bpy.context.window.scene = source_scene
        moved_from = active.name
    # Scenes the build made (no snapshot number): a studio for its previews, as Sol and Astra made one. Each
    # comes in as a scene of its own (workspace._mirror_scenes): what is in it, by key, and what only it has.
    scenes = []
    if snapshot is not None:
        source_objects = {ob.name for ob in bpy.context.scene.objects}
        for scene in bpy.data.scenes:
            if scene.get(workspace.SNAPSHOT_PROP) is not None:
                continue
            scenes.append({
                "name": scene.name, "objects": sorted(ownership.key_of(ob) for ob in scene.objects),
                "only_here": sorted(f"OBJECT:{ob.name}" for ob in scene.objects
                                    if not ob.get(ownership.STAMP) and ob.name not in source_objects),
                "camera": ownership.key_of(scene.camera) if scene.camera else None,
                "world": ownership.key_of(scene.world) if scene.world else None,
                "settings": workspace.scene_settings(scene)})
    root = Path(request["workspace"]).resolve()
    packed = []
    for image in bpy.data.images:
        if (image.library is None and image.packed_file is None and image.source in ("FILE", "SEQUENCE", "TILED")
                and image.filepath and _inside(image.filepath, root)):
            try:
                image.pack()
                packed.append(image.name)
            except RuntimeError as ex:
                problems.append(f"image {image.name} could not be packed ({ex})")
    if problems:
        return {"problems": problems}
    manifest = ownership.write_result(request["out"], owned, "", "", (), None)
    return {"manifest": manifest, "snapshot": number, "settings": workspace.scene_settings(bpy.context.scene),
            "scene": bpy.context.scene.name, "active_scene": moved_from, "scenes": scenes, "packed": packed,
            "recreated": found,
            "problems": []}


def main() -> None:
    args = sys.argv[sys.argv.index("--") + 1:]
    op, request_path, reply_path = args[0], Path(args[1]), Path(args[2])
    request = json.loads(request_path.read_text(encoding="utf-8"))
    loopcut = worker_child._load_package()
    import importlib
    for name in ("ownership", "workspace"):
        setattr(loopcut, name, importlib.import_module(f"loopcut.{name}"))
    reply = {"stamp": stamp, "prepare": prepare}[op](loopcut, request)
    temporary = reply_path.with_name(reply_path.name + ".tmp")
    temporary.write_text(json.dumps(reply, default=str), encoding="utf-8")
    os.replace(temporary, reply_path)


try:
    main()
except Exception:
    traceback.print_exc()
    raise  # --python-exit-code 1: Blender exits with 1.
