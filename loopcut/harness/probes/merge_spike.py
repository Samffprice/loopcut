"""M0 merge spike: can a worker's result .blend be merged into the live scene by ownership?

Parent (this file, headless):
    tools/Blender.app/Contents/MacOS/Blender -b --factory-startup \
        --python blender/loopcut/harness/probes/merge_spike.py [-- --keep]

1. builds a "live" scene and saves it (live.blend) plus the wave's spawn copy (compressed);
2. records the identity map (spawn name -> live session_uid) and a per-datablock token;
3. starts two background Blender workers on the spawn copy (this file again, "-- worker <brief>");
   worker A builds a Chair (and is assigned Lamp_Light), worker B a Table (and illegally scales the Rug);
   each stamps every pre-existing datablock with its spawn name first, then writes owned + created
   datablocks with bpy.data.libraries.write (dependencies ride along as local copies);
4. while they run, the "user" renames the Rug in the live scene;
5. merge S1: append each result into an unlinked staging collection, remap copies of live datablocks
   to the live originals (user_remap), replace the owned Lamp_Light (user_remap), prefix created names,
   quarantine out-of-ownership changes, one undo step around the merge;
6. merge S2: reload live.blend, the "user" edits Lamp_Light, merge worker A again -> staleness conflict;
7. prints PASS/FAIL per property, worker startup / wall time / peak RSS.
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

import bpy

STAMP = "lc_spawn_name"
ID_COLLECTIONS = ("objects", "meshes", "materials", "lights", "cameras", "collections", "node_groups",
                  "images", "worlds", "curves", "actions", "textures")
DOT_NUM = re.compile(r"\.\d{3}$")
WORKER_TIMEOUT = 180
MASK_DRIVEN_OFF = "--no-driven-mask" in sys.argv   # rerun with this to see the advisory false positive
HEAVY = "--heavy" in sys.argv                       # adds a ~1M-vertex mesh + 2K packed image to the live scene

BRIEFS = {
    "A": {"worker": "A", "prefix": "Chair", "owned": ["OBJECT:Lamp_Light", "LIGHT:Lamp_Light"]},
    "B": {"worker": "B", "prefix": "Table", "owned": []},
}


# ------------------------------------------------------------------ shared helpers

def all_ids():
    for attr in ID_COLLECTIONS:
        for idb in getattr(bpy.data, attr):
            yield attr, idb


def key_of(idb) -> str:
    return f"{idb.id_type}:{idb.get(STAMP) or idb.name}"


UID2SPAWN = {}  # parent only: live session_uid -> spawn name, so renamed live datablocks keep identity


def ref(idb):
    """Identity of a referenced datablock that is stable across processes and renames."""
    if idb is None:
        return None
    if idb.get(STAMP):
        return idb[STAMP]
    return UID2SPAWN.get(idb.session_uid, idb.name)


def rnd(v):
    try:
        return tuple(round(float(x), 4) for x in v)
    except TypeError:
        try:
            return round(float(v), 4)
        except (TypeError, ValueError):
            return str(v)


def token(idb) -> str:
    """Content token (the live_token idea, per datablock). Names of referenced IDs go through ref()."""
    t, d = idb.id_type, {}
    if t == "OBJECT":
        # Driven channels are outputs of other datablocks, not edits of this one: mask them, or a legal
        # change elsewhere (B scaling the Rug drives the Box's rotation) reads as a modification here.
        driven = {fc.data_path for fc in idb.animation_data.drivers} if idb.animation_data else set()
        d = {"loc": rnd(idb.location), "rot": rnd(idb.rotation_euler), "scale": rnd(idb.scale),
             "parent": ref(idb.parent), "data": ref(idb.data), "hide": idb.hide_render,
             "cons": [(c.type, ref(getattr(c, "target", None))) for c in idb.constraints],
             "mods": [(m.type, ref(getattr(m, "object", None))) for m in idb.modifiers],
             "mats": [ref(s.material) for s in idb.material_slots]}
        for path, short in (("location", "loc"), ("rotation_euler", "rot"), ("scale", "scale")):
            if path in driven and not MASK_DRIVEN_OFF:
                d[short] = "driven"
    elif t == "IMAGE":
        d = {"size": tuple(idb.size), "packed": idb.packed_file is not None, "path": idb.filepath}
    elif t == "LIGHT":
        d = {"type": idb.type, "energy": rnd(idb.energy), "color": rnd(idb.color)}
    elif t == "MATERIAL":
        nodes = idb.node_tree.nodes if idb.node_tree else []
        d = {"diffuse": rnd(idb.diffuse_color),
             "nodes": sorted((n.name, [(s.identifier, rnd(s.default_value)) for s in n.inputs
                                        if hasattr(s, "default_value")]) for n in nodes)}
    elif t == "MESH":
        co = [0.0] * (len(idb.vertices) * 3)
        idb.vertices.foreach_get("co", co)
        d = {"v": len(idb.vertices), "co": hashlib.sha1(",".join(f"{c:.4f}" for c in co).encode()).hexdigest()}
    return hashlib.sha1(json.dumps(d, sort_keys=True, default=str).encode()).hexdigest()[:16]


def describe(idb) -> dict:
    """Small human-readable view, for conflict and quarantine reports."""
    if idb.id_type == "OBJECT":
        return {"location": rnd(idb.location), "rotation": rnd(idb.rotation_euler), "scale": rnd(idb.scale)}
    if idb.id_type == "LIGHT":
        return {"energy": rnd(idb.energy), "color": rnd(idb.color)}
    return {}


def window_override():
    """Background Blender has windows in the WM but no active one; undo operators poll for a screen."""
    wm = bpy.context.window_manager
    if not wm.windows:
        return None
    win = wm.windows[0]
    return bpy.context.temp_override(window=win, screen=win.screen)


def undo_push(message) -> tuple:
    t0 = time.time()
    try:
        ov = window_override()
        if ov is None:
            return False, "no window in WM", 0.0
        with ov:
            bpy.ops.ed.undo_push(message=message)
        return True, "", time.time() - t0
    except Exception as e:  # noqa: BLE001
        return False, str(e).splitlines()[0][:160], time.time() - t0


def undo_op(name) -> tuple:
    try:
        ov = window_override()
        if ov is None:
            return False, "no window in WM"
        with ov:
            getattr(bpy.ops.ed, name)()
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, str(e).splitlines()[0][:160]


def add_mesh_object(name, mesh_name, size, loc, collection):
    import bmesh
    me = bpy.data.meshes.new(mesh_name)
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    for v in bm.verts:
        v.co.x *= size[0]
        v.co.y *= size[1]
        v.co.z *= size[2]
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new(name, me)
    ob.location = loc
    collection.objects.link(ob)
    return ob


def new_material(name, rgb):
    mat = bpy.data.materials.new(name)
    mat.diffuse_color = (*rgb, 1.0)
    if mat.node_tree is None:  # 5.x materials come with nodes; older API needed use_nodes.
        mat.use_nodes = True
    bsdf = next((n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
    if bsdf:
        bsdf.inputs["Base Color"].default_value = (*rgb, 1.0)
    return mat


def add_driver(ob, path, index, target, transform_type, expr):
    fc = ob.driver_add(path, index)
    drv = fc.driver
    drv.type = "SCRIPTED"
    var = drv.variables.new()
    var.name = "v"
    var.type = "TRANSFORMS"
    var.targets[0].id = target
    var.targets[0].transform_type = transform_type
    drv.expression = expr
    return fc


# ------------------------------------------------------------------ worker (child process)

def worker_main(brief_path):
    t_start = time.time()
    brief = json.loads(open(brief_path).read())
    w, prefix = brief["worker"], brief["prefix"]
    owned = set(brief["owned"])
    log = {"worker": w, "t_script_start": t_start}

    # 1. Stamp identity before any model code, and remember each datablock's token for advisory detection.
    t0 = time.time()
    spawn_tokens = {}
    for _attr, idb in all_ids():
        if idb.library is None:
            idb[STAMP] = idb.name
            spawn_tokens[key_of(idb)] = token(idb)
    log["stamp_s"] = round(time.time() - t0, 4)
    log["stamped"] = len(spawn_tokens)

    # 2. Per-call rollback options, measured: an undo push (needs an explicitly created stack in -b)
    #    and a per-call save.
    ok, err, dt = undo_push(f"{w}: before call 1")
    log["undo_push"] = {"ok": ok, "err": err, "s": round(dt, 4)}
    if ok:
        floor = bpy.data.objects["Floor"]
        floor.location.x = 99.0                           # a "bad call" touching a non-owned object
        ok2, err2, dt2 = undo_push(f"{w}: call 1")
        okundo, errundo = undo_op("undo")
        restored = bpy.data.objects["Floor"].location.x   # re-fetch: memfile undo invalidates ID pointers
        log["undo_rollback"] = {"push2_ok": ok2, "push2_s": round(dt2, 4), "undo_ok": okundo,
                                "undo_err": errundo, "floor_x_after_undo": round(restored, 3)}
        if restored != 0.0:
            bpy.data.objects["Floor"].location.x = 0.0
    t0 = time.time()
    tmp_save = os.path.join(os.path.dirname(brief_path), f"percall_{w}.blend")
    bpy.ops.wm.save_as_mainfile(filepath=tmp_save, copy=True, compress=False)
    log["percall_save_s"] = round(time.time() - t0, 4)
    os.remove(tmp_save)

    # 3. "Model code".
    scene_col = bpy.context.scene.collection
    rug = bpy.data.objects["Rug"]
    created_objects = []
    if w == "A":
        root = bpy.data.objects.new("Chair_Root", None)
        root.location = (0.6, 0.0, 0.0)
        scene_col.objects.link(root)
        wood = bpy.data.materials["Palette_Wood"]
        fabric = new_material("Chair_Fabric", (0.2, 0.3, 0.7))
        legs = []
        for i, (x, y) in enumerate(((-.2, -.2), (.2, -.2), (-.2, .2), (.2, .2)), 1):
            leg = add_mesh_object(f"Chair_Leg_{i}", f"Chair_LegMesh_{i}", (.04, .04, .45), (0.6 + x, y, .225),
                                  scene_col)
            leg.data.materials.append(wood)
            leg.parent = root
            leg.matrix_parent_inverse = root.matrix_world.inverted()
            legs.append(leg)
        c = legs[0].constraints.new("COPY_LOCATION")
        c.target = rug
        c.use_offset = True
        c.influence = 0.0            # present but inert; the point is the reference
        seat = add_mesh_object("Chair_Seat", "Chair_SeatMesh", (.5, .5, .06), (0.6, 0, .48), scene_col)
        seat.data.materials.append(fabric)
        seat.data.materials.append(next(m for m in bpy.data.materials if m.name == "Lib_Varnish"))  # linked
        seat.parent = root
        seat.matrix_parent_inverse = root.matrix_world.inverted()
        boolean = seat.modifiers.new("Chair_Notch", "BOOLEAN")
        boolean.object = bpy.data.objects["Cutter"]        # modifier target outside the root
        add_driver(seat, "rotation_euler", 2, bpy.data.objects["Floor"], "LOC_X", "v*0")  # driver outside root
        created_objects = [root, *legs, seat]
        lamp = bpy.data.lights["Lamp_Light"]               # assigned: colour and power
        lamp.color = (1.0, 0.8, 0.6)
        lamp.energy = 250.0
    else:
        root = bpy.data.objects.new("Table_Root", None)
        root.location = (-1.5, 0.0, 0.0)
        scene_col.objects.link(root)
        c = root.constraints.new("COPY_ROTATION")
        c.target = rug                                     # so the (modified) Rug copy rides into the result
        c.influence = 0.0
        fabric = new_material("Chair_Fabric", (0.7, 0.2, 0.2))   # name collision with worker A
        top = add_mesh_object("Table_Top", "Table_TopMesh", (1.0, .6, .05), (-1.5, 0, .72), scene_col)
        top.data.materials.append(fabric)
        leg = add_mesh_object("Leg", "Leg", (.05, .05, .7), (-1.9, -.25, .35), scene_col)   # unprefixed
        leg.data.materials.append(bpy.data.materials["Palette_Wood"])
        for ob in (top, leg):
            ob.parent = root
            ob.matrix_parent_inverse = root.matrix_world.inverted()
        created_objects = [root, top, leg]
        rug.scale = (2.0, 2.0, 1.0)                        # ILLEGAL: the Rug is not owned by B

    # 4. Harness post-pass (advisory): what changed that isn't owned, what's unprefixed.
    bpy.context.view_layer.update()
    changed_not_owned, unprefixed = [], []
    for _attr, idb in all_ids():
        k = key_of(idb)
        if idb.get(STAMP):
            if k not in owned and spawn_tokens.get(k) != token(idb):
                changed_not_owned.append(k)
        elif idb.library is None and not idb.name.startswith(prefix + "_"):
            unprefixed.append(f"{idb.id_type}:{idb.name}")
    log["advisory_changed_not_owned"] = changed_not_owned
    log["advisory_unprefixed"] = unprefixed

    # 5. Result: owned + created datablocks. libraries.write adds dependencies (Rug, Palette_Wood, ...)
    #    as local copies; they carry their stamps, which is how the merge recognises them.
    created = {idb for _a, idb in all_ids() if not idb.get(STAMP) and idb.id_type != "COLLECTION"}
    owned_ids = {idb for _a, idb in all_ids() if idb.get(STAMP) and key_of(idb) in owned}
    log["world_locations"] = {ob.name: rnd(ob.matrix_world.translation) for ob in created_objects}
    log["created"] = sorted(f"{i.id_type}:{i.name}" for i in created)
    log["owned_written"] = sorted(f"{i.id_type}:{i.name}" for i in owned_ids)
    result = os.path.join(os.path.dirname(brief_path), f"result_{w}.blend")
    t0 = time.time()
    bpy.data.libraries.write(result, created | owned_ids, compress=True)
    log["result_write_s"] = round(time.time() - t0, 4)
    log["result_bytes"] = os.path.getsize(result)
    log["result"] = result
    log["t_script_end"] = time.time()
    with open(os.path.join(os.path.dirname(brief_path), f"manifest_{w}.json"), "w") as f:
        json.dump(log, f, indent=1)
    print(f"[worker {w}] done: created={len(created)} advisory_changed_not_owned={changed_not_owned}")


# ------------------------------------------------------------------ parent: live scene

def build_library(lib_path):
    """An external asset library: the live scene links the material Lib_Varnish from it."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    mat = new_material("Lib_Varnish", (0.9, 0.85, 0.7))
    bpy.data.libraries.write(lib_path, {mat}, fake_user=True)


def build_live(lib_path):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    col = scene.collection
    wood = new_material("Palette_Wood", (0.45, 0.28, 0.12))
    grain = bpy.data.images.new("Wood_Grain", 2048 if HEAVY else 128, 2048 if HEAVY else 128)
    grain.generated_type = "UV_GRID"
    import numpy as np
    buf = np.empty(len(grain.pixels), dtype=np.float32)
    grain.pixels.foreach_get(buf)
    grain.pixels.foreach_set(buf)                         # generated images only pack once the buffer is dirty
    grain.pack()                                          # packed image inside a shared material
    assert grain.packed_file is not None
    tex = wood.node_tree.nodes.new("ShaderNodeTexImage")
    tex.image = grain
    with bpy.data.libraries.load(lib_path, link=True) as (_src, dst):
        dst.materials = ["Lib_Varnish"]                   # linked (read-only) datablock
    wool = new_material("Rug_Wool", (0.6, 0.55, 0.4))
    floor = add_mesh_object("Floor", "FloorMesh", (8, 8, .02), (0, 0, -.01), col)
    floor.data.materials.append(wood)
    rug = add_mesh_object("Rug", "RugMesh", (2, 1.4, .01), (0, 0, .005), col)
    rug.data.materials.append(wool)
    lamp_data = bpy.data.lights.new("Lamp_Light", "POINT")
    lamp_data.energy = 100.0
    lamp = bpy.data.objects.new("Lamp_Light", lamp_data)
    lamp.location = (1.5, 1.5, 2.0)
    col.objects.link(lamp)
    shade = add_mesh_object("Lamp_Shade", "Lamp_ShadeMesh", (.3, .3, .2), (1.5, 1.5, 2.1), col)
    shade.parent = lamp                                   # object parented to another
    shade.matrix_parent_inverse = lamp.matrix_world.inverted()
    cam_data = bpy.data.cameras.new("Cam_Main")
    cam = bpy.data.objects.new("Cam_Main", cam_data)
    cam.location = (5, -5, 3)
    col.objects.link(cam)
    scene.camera = cam
    track = cam.constraints.new("TRACK_TO")                # a live reference TO the owned object
    track.target = lamp
    box = add_mesh_object("Box", "BoxMesh", (.6, .6, .6), (-2.5, 2, .3), col)
    box.data.materials.append(wood)
    box.data.materials.append(dst.materials[0])           # give the linked material a live user
    if HEAVY:
        bpy.ops.mesh.primitive_grid_add(x_subdivisions=1000, y_subdivisions=1000, size=40, location=(0, 0, -.5))
        bpy.context.active_object.name = "Terrain"
    cutter = add_mesh_object("Cutter", "CutterMesh", (.3, .3, 1), (-2.5, 2, .5), col)
    cutter.display_type = "WIRE"
    mod = box.modifiers.new("Cut", "BOOLEAN")
    mod.object = cutter
    add_driver(box, "rotation_euler", 2, rug, "SCALE_X", "v*0.1")   # driver pointing at a live object
    bpy.context.view_layer.update()


def live_by_uid():
    return {idb.session_uid: idb for _a, idb in all_ids()}


def spawn_state():
    ids = {key_of(idb): idb.session_uid for _a, idb in all_ids()}
    tokens = {key_of(idb): token(idb) for _a, idb in all_ids()}
    return ids, tokens


# ------------------------------------------------------------------ parent: merge

def merge_worker(manifest, brief, spawn_ids, spawn_tokens, report):
    w, prefix, owned = brief["worker"], brief["prefix"], set(brief["owned"])
    t0 = time.time()
    by_uid = live_by_uid()
    before = set(by_uid)

    # 1. Append everything in the result.
    with bpy.data.libraries.load(manifest["result"], link=False) as (src, dst):
        for attr in ID_COLLECTIONS:
            if hasattr(src, attr):
                setattr(dst, attr, list(getattr(src, attr)))
    # Append via bpy.data.libraries.load leaves the result's Library ID behind (users=1, nothing linked).
    for lib in list(bpy.data.libraries):
        if os.path.realpath(bpy.path.abspath(lib.filepath)) == os.path.realpath(manifest["result"]):
            bpy.data.libraries.remove(lib)
            report["libraries_removed"] = report.get("libraries_removed", 0) + 1
    new = [idb for _a, idb in all_ids() if idb.session_uid not in before]
    staging = bpy.data.collections.new(f"LC_Staging_{w}")           # not linked to the scene

    created, copies, owned_new, linked_dups = [], [], [], []
    live_linked = {(i.library.filepath, i.id_type, i.name): i for i in by_uid.values() if i.library}
    for idb in new:
        stamp = idb.get(STAMP)
        if idb.library is not None:                   # linked data can't be stamped; identity = (lib, name)
            live = live_linked.get((idb.library.filepath, idb.id_type, idb.name))
            if live is not None:
                linked_dups.append((idb, live))
            else:
                report["errors"].append(f"{w}: new linked datablock {idb.name} from {idb.library.filepath}")
        elif not stamp:
            created.append(idb)
        elif f"{idb.id_type}:{stamp}" in owned:
            owned_new.append(idb)
        else:
            copies.append(idb)
    for idb in created + owned_new:
        if idb.id_type == "OBJECT":
            staging.objects.link(idb)

    to_remove = []
    for idb, live in linked_dups:                     # only reached if append duplicated a linked ID
        idb.user_remap(live)
        to_remove.append(idb)
        report["errors"].append(f"{w}: append duplicated linked {idb.name}; remapped")

    def live_original(idb):
        uid = spawn_ids.get(f"{idb.id_type}:{idb[STAMP]}")
        return by_uid.get(uid)

    # 2. Copies of datablocks the worker doesn't own: quarantine any change, then remap to the live original.
    for idb in copies:
        k = f"{idb.id_type}:{idb[STAMP]}"
        live = live_original(idb)
        if token(idb) != spawn_tokens.get(k):
            report["quarantined"].append({"worker": w, "id": k, "worker_value": describe(idb),
                                          "live_value": describe(live) if live else None})
        if live is None:
            report["errors"].append(f"{w}: live original of {k} is gone; copy kept")
            continue
        idb.user_remap(live)
        to_remove.append(idb)

    # 3. Owned originals: staleness per unit (object + its data), then replace with user_remap.
    units = {}
    for idb in owned_new:
        units.setdefault("unit", []).append(idb)   # one owned unit in this spike (Lamp_Light object + data)
    for members in units.values():
        stale = []
        for idb in members:
            k = f"{idb.id_type}:{idb[STAMP]}"
            live = live_original(idb)
            if live is not None and token(live) != spawn_tokens.get(k):
                stale.append(k)
        if stale:
            report["conflicts"].append({"worker": w, "stale": stale,
                                        "live_now": {k: describe(live_original(i)) for i in members
                                                     for k in [f"{i.id_type}:{i[STAMP]}"]},
                                        "worker_value": {f"{i.id_type}:{i[STAMP]}": describe(i) for i in members}})
            for idb in members:                                # keep the user's edit; drop the worker's
                live = live_original(idb)
                idb.user_remap(live)
                to_remove.append(idb)
            continue
        for idb in members:
            live = live_original(idb)
            live.user_remap(idb)                               # live references now follow the new one
            to_remove.append(live)
            report["replaced"].append(f"{w}: {idb.id_type}:{idb[STAMP]}")

    # 4. Remove the copies and replaced originals, then fix names.
    removed = {idb.session_uid for idb in to_remove}
    owned_new = [idb for idb in owned_new if idb.session_uid not in removed]   # conflict -> dropped
    for idb in to_remove:
        if idb.id_type == "OBJECT":
            for c in list(idb.users_collection):
                c.objects.unlink(idb)
    bpy.data.batch_remove(to_remove)
    for idb in owned_new:
        if idb.name != idb[STAMP]:
            idb.name = idb[STAMP]
            if idb.name != idb[STAMP]:
                report["errors"].append(f"{w}: could not restore name {idb[STAMP]} (got {idb.name})")
    for idb in created:
        base = DOT_NUM.sub("", idb.name)
        want = base if base.startswith(prefix + "_") else f"{prefix}_{base}"
        if idb.name != want:
            old = idb.name
            idb.name = want
            report["renamed"].append(f"{w}: {idb.id_type} {old} -> {idb.name}")

    # 5. Link created objects into a deliverable collection; stamps off; staging gone.
    dest = bpy.data.collections.get(prefix) or bpy.data.collections.new(prefix)
    if dest.name not in bpy.context.scene.collection.children:
        bpy.context.scene.collection.children.link(dest)
    for ob in list(staging.objects):
        if not ob.get(STAMP):
            dest.objects.link(ob)
        staging.objects.unlink(ob)
    bpy.data.collections.remove(staging)
    for idb in created + owned_new:
        if STAMP in idb:
            del idb[STAMP]
    report["merge_s"][w] = round(time.time() - t0, 4)


# ------------------------------------------------------------------ parent: workers

def run_workers(tmp, spawn_copy):
    procs = {}
    for w, brief in BRIEFS.items():
        bp = os.path.join(tmp, f"brief_{w}.json")
        with open(bp, "w") as f:
            json.dump(brief, f)
        logf = open(os.path.join(tmp, f"worker_{w}.log"), "w")
        t_spawn = time.time()
        p = subprocess.Popen([bpy.app.binary_path, "-b", "--factory-startup", spawn_copy, "--python", __file__,
                              "--", "worker", bp, *(["--no-driven-mask"] if MASK_DRIVEN_OFF else [])], stdout=logf, stderr=subprocess.STDOUT)
        procs[w] = {"p": p, "t_spawn": t_spawn, "log": logf}

    def reap(w):
        info = procs[w]
        timer = threading.Timer(WORKER_TIMEOUT, info["p"].kill)
        timer.start()
        _pid, status, ru = os.wait4(info["p"].pid, 0)
        timer.cancel()
        info["p"].returncode = os.waitstatus_to_exitcode(status)
        info["t_exit"] = time.time()
        rss = ru.ru_maxrss if sys.platform == "darwin" else ru.ru_maxrss * 1024
        info["peak_rss_mb"] = round(rss / 2**20, 1)
        info["log"].close()

    threads = [threading.Thread(target=reap, args=(w,)) for w in procs]
    for t in threads:
        t.start()
    return procs, threads


# ------------------------------------------------------------------ parent: checks

def check(results, name, ok, detail=""):
    results.append((name, bool(ok), detail))


def obj(name):
    return bpy.data.objects.get(name)


def run_checks_s1(spawn_ids, report, manifests, results):
    by_uid = live_by_uid()
    dots = [f"{a}:{i.name}" for a, i in all_ids() if DOT_NUM.search(i.name)]
    check(results, "no .001 datablocks", not dots, ", ".join(dots))
    rug_uid = spawn_ids["OBJECT:Rug"]
    live_rug = by_uid.get(rug_uid)
    leg1, troot = obj("Chair_Leg_1"), obj("Table_Root")
    t1 = leg1.constraints[0].target if leg1 and leg1.constraints else None
    t2 = troot.constraints[0].target if troot and troot.constraints else None
    check(results, "constraint target is the live Rug (renamed by user mid-wave)",
          live_rug is not None and t1 == live_rug and t2 == live_rug,
          f"leg1->{t1.name if t1 else None}, table_root->{t2.name if t2 else None}, live={live_rug.name if live_rug else None}")
    box, cutter = obj("Box"), by_uid.get(spawn_ids["OBJECT:Cutter"])
    seat = obj("Chair_Seat")
    check(results, "Boolean cutters are the live Cutter (live Box + merged Chair_Seat)",
          box.modifiers["Cut"].object == cutter and seat and seat.modifiers[0].object == cutter,
          f"box->{box.modifiers['Cut'].object.name}, seat->{seat.modifiers[0].object.name if seat else None}")
    dvar_box = box.animation_data.drivers[0].driver.variables[0].targets[0].id
    dvar_seat = seat.animation_data.drivers[0].driver.variables[0].targets[0].id if seat else None
    floor = by_uid.get(spawn_ids["OBJECT:Floor"])
    check(results, "driver targets unchanged (Box->live Rug, Chair_Seat->live Floor)",
          dvar_box == live_rug and dvar_seat == floor,
          f"box->{dvar_box.name if dvar_box else None}, seat->{dvar_seat.name if dvar_seat else None}")
    root = obj("Chair_Root")
    legs_ok = all(obj(f"Chair_Leg_{i}") and obj(f"Chair_Leg_{i}").parent == root for i in range(1, 5))
    tb = obj("Table_Leg")
    lamp, shade = obj("Lamp_Light"), obj("Lamp_Shade")
    bpy.context.view_layer.update()
    want = {**manifests["A"]["world_locations"], **{("Table_Leg" if k == "Leg" else k): v
                                                    for k, v in manifests["B"]["world_locations"].items()}}
    moved = [k for k, v in want.items() if obj(k) is None or rnd(obj(k).matrix_world.translation) != tuple(v)]
    check(results, "parenting intact (legs->roots, Lamp_Shade->new Lamp_Light, world positions kept)",
          legs_ok and tb and tb.parent == troot and shade.parent == lamp and not moved,
          f"legs_ok={legs_ok} table_leg_parent={tb.parent.name if tb and tb.parent else None} "
          f"shade_parent={shade.parent.name if shade.parent else None} moved={moved}")
    woods = [m for m in bpy.data.materials if m.name.startswith("Palette_Wood")]
    wood_live = by_uid.get(spawn_ids["MATERIAL:Palette_Wood"])
    leg_mats = {obj(f"Chair_Leg_{i}").data.materials[0] for i in range(1, 5)} | {tb.data.materials[0]}
    check(results, "Palette_Wood is one datablock (the live one), used by merged legs",
          len(woods) == 1 and woods[0] == wood_live and leg_mats == {wood_live},
          f"materials={[m.name for m in woods]} users={wood_live.users if wood_live else None}")
    cf, tf = bpy.data.materials.get("Chair_Fabric"), bpy.data.materials.get("Table_Chair_Fabric")
    check(results, "both fabric materials exist with distinct names and the right users",
          cf and tf and cf != tf and seat.data.materials[0] == cf and obj("Table_Top").data.materials[0] == tf,
          f"{cf.name if cf else None} / {tf.name if tf else None}")
    check(results, "unprefixed 'Leg' became 'Table_Leg'", obj("Leg") is None and tb is not None)
    check(results, "Rug unchanged (scale 1,1,1; only the user's rename)",
          live_rug and rnd(live_rug.scale) == (1.0, 1.0, 1.0) and live_rug.name == "Rug_Persian",
          f"scale={rnd(live_rug.scale) if live_rug else None}")
    q = [x for x in report["quarantined"] if x["id"] == "OBJECT:Rug" and x["worker"] == "B"]
    check(results, "B's out-of-ownership Rug change quarantined + reported (not merged)", q,
          json.dumps(q[0]) if q else "")
    lamps = [o for o in bpy.data.objects if o.name.startswith("Lamp_Light")]
    cam = obj("Cam_Main")
    check(results, "owned Lamp_Light replaced (colour+power), live refs follow",
          len(lamps) == 1 and lamp.data.name == "Lamp_Light" and rnd(lamp.data.energy) == 250.0
          and rnd(lamp.data.color) == (1.0, 0.8, 0.6) and cam.constraints[0].target == lamp
          and lamp.users_collection and lamp.users_collection[0] == bpy.context.scene.collection,
          f"energy={lamp.data.energy} color={rnd(lamp.data.color)} cam_target={cam.constraints[0].target.name} "
          f"collections={[c.name for c in lamp.users_collection]}")
    leftovers = [c.name for c in bpy.data.collections if c.name.startswith("LC_Staging")]
    libs = [lib.name for lib in bpy.data.libraries]
    check(results, "no staging collections / stray libraries / stamps left",
          not leftovers and libs == ["lib_assets.blend"] and not [i for _a, i in all_ids() if i.get(STAMP)],
          f"staging={leftovers} libs={libs}")
    varnish = [m for m in bpy.data.materials if m.name.startswith("Lib_Varnish")]
    check(results, "linked Lib_Varnish reused (one linked datablock, used by merged seat)",
          len(varnish) == 1 and varnish[0].library is not None and seat.data.materials[1] == varnish[0],
          f"{[(m.name, m.library.name if m.library else None) for m in varnish]}")
    imgs = [i.name for i in bpy.data.images]
    grain_live = by_uid.get(spawn_ids["IMAGE:Wood_Grain"])
    check(results, "packed image Wood_Grain is one datablock, still packed, still in Palette_Wood",
          imgs == ["Wood_Grain"] and grain_live is not None and grain_live.packed_file is not None
          and any(getattr(n, "image", None) == grain_live for n in wood_live.node_tree.nodes),
          f"images={imgs} live={grain_live is not None} "
          f"packed={grain_live.packed_file is not None if grain_live else None} "
          f"in_wood={[getattr(n, 'image', None) and n.image.name for n in wood_live.node_tree.nodes]}")


def main_parent():
    keep = "--keep" in sys.argv
    tmp = tempfile.mkdtemp(prefix="lc_merge_spike_")
    t_all = time.time()
    results = []

    lib_path = os.path.join(tmp, "lib_assets.blend")
    build_library(lib_path)
    build_live(lib_path)
    live_path = os.path.join(tmp, "live.blend")
    bpy.ops.wm.save_as_mainfile(filepath=live_path)
    spawn_copy = os.path.join(tmp, "spawn_wave1.blend")
    t0 = time.time()
    bpy.ops.wm.save_as_mainfile(filepath=spawn_copy, copy=True, compress=True)
    spawn_save_s = time.time() - t0
    spawn_ids, spawn_tokens = spawn_state()
    UID2SPAWN.update({uid: k.split(":", 1)[1] for k, uid in spawn_ids.items()})

    procs, threads = run_workers(tmp, spawn_copy)
    # The user keeps working during the wave.
    bpy.data.objects["Rug"].name = "Rug_Persian"
    for t in threads:
        t.join()

    manifests = {}
    for w, info in procs.items():
        mp = os.path.join(tmp, f"manifest_{w}.json")
        if info["p"].returncode != 0 or not os.path.exists(mp):
            print(f"[parent] worker {w} failed rc={info['p'].returncode}; log tail:")
            print("".join(open(os.path.join(tmp, f"worker_{w}.log")).readlines()[-15:]))
            return
        manifests[w] = json.load(open(mp))

    # ---- S1: clean merge of A then B, one undo step.
    report = {"quarantined": [], "conflicts": [], "replaced": [], "renamed": [], "errors": [], "merge_s": {}}
    ok1, err1, _ = undo_push("Loopcut: checkpoint before merge")
    for w in ("A", "B"):
        merge_worker(manifests[w], BRIEFS[w], spawn_ids, spawn_tokens, report)
    ok2, err2, _ = undo_push("Loopcut: merge wave 1")
    run_checks_s1(spawn_ids, report, manifests, results)
    if ok1 and ok2:
        u_ok, u_err = undo_op("undo")
        undone = obj("Chair_Root") is None and obj("Table_Root") is None and \
            rnd(bpy.data.lights["Lamp_Light"].energy) == 100.0 and obj("Rug_Persian") is not None
        r_ok, r_err = undo_op("redo")
        redone = obj("Chair_Root") is not None and rnd(bpy.data.lights["Lamp_Light"].energy) == 250.0
        check(results, "one undo step spans the merge (undo -> pre-merge, redo -> merged)",
              u_ok and undone and r_ok and redone,
              f"undo_ok={u_ok} {u_err} undone={undone} redo_ok={r_ok} {r_err} redone={redone}")
    else:
        check(results, "one undo step spans the merge", False, f"undo_push failed headless: {err1 or err2}")

    # ---- S2: fresh live scene, user edits the owned Lamp_Light during the wave -> conflict.
    bpy.ops.wm.open_mainfile(filepath=live_path)
    UID2SPAWN.clear()
    s2_ids, s2_tokens = spawn_state()   # same content as the spawn copy; uids are per session
    UID2SPAWN.update({uid: k.split(":", 1)[1] for k, uid in s2_ids.items()})
    bpy.data.lights["Lamp_Light"].energy = 40.0          # the user's edit after spawn
    report2 = {"quarantined": [], "conflicts": [], "replaced": [], "renamed": [], "errors": [], "merge_s": {}}
    merge_worker(manifests["A"], BRIEFS["A"], s2_ids, s2_tokens, report2)
    lamp = bpy.data.lights.get("Lamp_Light")
    check(results, "staleness conflict detected; user's Lamp edit kept, worker's not applied",
          report2["conflicts"] and not report2["replaced"] and lamp and rnd(lamp.energy) == 40.0
          and len([o for o in bpy.data.objects if o.name.startswith("Lamp_Light")]) == 1,
          json.dumps(report2["conflicts"][0]) if report2["conflicts"] else "no conflict reported")
    dots = [f"{a}:{i.name}" for a, i in all_ids() if DOT_NUM.search(i.name)]
    check(results, "S2 also leaves no .001 datablocks", not dots, ", ".join(dots))

    # ---- output
    print("\n==== merge spike results")
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))
    print("\n==== report S1:", json.dumps({k: v for k, v in report.items() if k != "quarantined"}))
    print("==== quarantined S1:", json.dumps(report["quarantined"]))
    print("\n==== workers")
    import resource
    for w, info in procs.items():
        m = manifests[w]
        print(f"worker {w}: startup(spawn->script) {m['t_script_start'] - info['t_spawn']:.2f}s  "
              f"wall {info['t_exit'] - info['t_spawn']:.2f}s  peak RSS {info['peak_rss_mb']} MB  "
              f"stamp {m['stamp_s']}s ({m['stamped']} ids)  undo_push {m['undo_push']}  "
              f"rollback {m.get('undo_rollback')}  per-call save {m['percall_save_s']}s  "
              f"result write {m['result_write_s']}s {m['result_bytes']} B  "
              f"advisory not-owned {m['advisory_changed_not_owned']} unprefixed {m['advisory_unprefixed']}")
    print(f"spawn copy save {spawn_save_s:.3f}s {os.path.getsize(spawn_copy)} B; merge times {report['merge_s']}; "
          f"parent peak RSS {round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20, 1)} MB; "
          f"total {time.time() - t_all:.1f}s; tmp {tmp}{' (kept)' if keep else ''}")
    if not keep:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if argv[:1] == ["worker"]:
        worker_main(argv[1])
    else:
        main_parent()
