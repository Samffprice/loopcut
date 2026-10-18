"""What a real scene's input paths are, which field each comes from, and what the packer flags.

    <blender> -b --factory-startup --python loopcut/harness/bundle_inputs_check.py -- \
        <outdir> <old bundles.py> <scene copy.blend>...

For each scene copy (never an original: collection only reads it, but it is opened here too):
print every path bpy.utils.blend_paths(absolute=True, local=True) lists, the datablock and
field it comes from, then run bundles.collect from the old packer and from this checkout into
fresh staging folders under outdir and print what each flags. Only local staging is written;
nothing is uploaded and no cloud client is created. Staging is removed once printed.
"""

import importlib.util
import os
import shutil
import sys
import traceback
from collections import Counter, defaultdict
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
import checkout  # noqa: E402


def id_collections():
    for name in dir(bpy.data):
        collection = getattr(bpy.data, name, None)
        if not name.startswith("_") and isinstance(collection, bpy.types.bpy_prop_collection):
            yield name, collection


def fields(bundles, base):
    """Resolved path -> 'datablock: field' labels, from the RNA fields that can hold paths."""
    found = defaultdict(list)

    def note(raw, owner, label, library=None):
        if raw is None:
            return
        key = "" if not raw else str(bundles._resolve_path(raw, bpy.path.abspath(library.filepath)
                                                            if library else base))
        found[key].append(f"{owner}: {label}")

    def weak_references():
        for collection_name, collection in id_collections():
            for item in collection:
                weak = getattr(item, "library_weak_reference", None)
                if weak is not None:
                    note(weak.filepath, f"{collection_name}[{item.name!r}]",
                         "library_weak_reference.filepath" + ("" if weak.filepath else " (empty)"),
                         getattr(item, "library", None))

    def datablocks():
        for collection_name, attribute in (("images", "filepath_raw"), ("sounds", "filepath"),
                                           ("movieclips", "filepath"), ("fonts", "filepath"),
                                           ("volumes", "filepath"), ("cache_files", "filepath"),
                                           ("libraries", "filepath"), ("texts", "filepath")):
            for item in getattr(bpy.data, collection_name):
                packed = getattr(item, "packed_file", None) or getattr(item, "packed_files", None)
                note(getattr(item, attribute, None), f"{collection_name}[{item.name!r}]",
                     attribute + (" (packed)" if packed else ""), getattr(item, "library", None))

    def strips():
        for scene in bpy.data.scenes:
            for strip in scene.sequence_editor.strips_all if scene.sequence_editor else ():
                owner = f"scenes[{scene.name!r}] strip {strip.name!r} ({strip.type})"
                if strip.type == "SOUND" and strip.sound:
                    note(strip.sound.filepath, owner, "stored copy of its sound's path, sound "
                         f"{strip.sound.name!r}" + (" (packed)" if strip.sound.packed_file else ""))
                elif strip.type == "MOVIE":
                    note(strip.filepath, owner, "filepath")
                elif strip.type == "IMAGE":
                    for element in strip.elements:
                        note(os.path.join(strip.directory, element.filename), owner, "directory + element")

    def modifiers():
        for obj in bpy.data.objects:
            for mod in obj.modifiers:
                owner = f"objects[{obj.name!r}] modifier {mod.name!r} ({mod.type})"
                domain = getattr(mod, "domain_settings", None)
                if domain:
                    note(domain.cache_directory, owner, "domain_settings.cache_directory")
                if mod.type in {"OCEAN", "MESH_CACHE", "MULTIRES"}:
                    note(mod.filepath, owner, "filepath")
                if mod.type == "NODES":
                    note(mod.bake_directory, owner, f"bake_directory (bake_target {mod.bake_target})")
                    for bake in mod.bakes:
                        note(bake.directory, owner, f"bakes[{bake.bake_id}].directory")
                cache = getattr(mod, "point_cache", None)
                if cache is not None and cache.use_disk_cache:
                    note(cache.filepath, owner, "point_cache.filepath")

    for section in (weak_references, datablocks, strips, modifiers):
        try:
            section()
        except Exception as ex:
            print(f"  (attribution of {section.__name__} failed: {type(ex).__name__}: {ex})", flush=True)
    return found


def diagnose(bundles, blend):
    status = bpy.ops.wm.open_mainfile(filepath=str(blend), load_ui=False, use_scripts=False)
    assert status == {"FINISHED"}, "could not open " + str(blend)
    base = bpy.data.filepath
    owners = defaultdict(set)

    def visit(owner, path, meta):
        owners[str(bundles._resolve_path(path, bpy.path.abspath(owner.library.filepath)
                                         if owner.library else base))].add(f"{owner.id_type} {owner.name!r}")

    try:
        bpy.data.file_path_foreach(visit, flags={"SKIP_PACKED", "SKIP_LINKED"})
    except Exception as ex:
        print(f"  (file_path_foreach failed: {type(ex).__name__}: {ex})", flush=True)
    labels = fields(bundles, base)
    listed = Counter(bpy.utils.blend_paths(absolute=True, local=True))
    print(f"\n== {blend.name}: blend_paths(local=True) lists {sum(listed.values())} entries, "
          f"{len(listed)} distinct", flush=True)
    for raw, count in sorted(listed.items()):
        key = "" if not raw else str(bundles._resolve_path(raw, base))
        print(f"  x{count} {raw!r}", flush=True)
        print(f"      exists: {bool(raw) and Path(key).exists()}; owner IDs (file_path_foreach): "
              f"{sorted(owners.get(key, ())) or 'none (weak reference, or empty path)'}", flush=True)
        for label in sorted(set(labels.get(key, ())))[:6]:
            print(f"      field: {label}", flush=True)
        if len(set(labels.get(key, ()))) > 6:
            print(f"      field: ... and {len(set(labels[key])) - 6} more", flush=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)


def collect(module, label, blend, outdir):
    stage = outdir / f"stage-{label}-{blend.stem}"
    shutil.rmtree(stage, ignore_errors=True)
    try:
        info = module.collect(blend, stage)
    except Exception as ex:
        print(f"  [{label}] collect raised {type(ex).__name__}: {str(ex)[-600:]}", flush=True)
        return
    finally:
        size = sum(p.stat().st_size for p in stage.rglob("*") if p.is_file()) if stage.exists() else 0
    print(f"  [{label}] missing {len(info['missing'])}, unsupported {len(info['unsupported'])}, "
          f"system {len(info['system'])}, staged {size / 1e6:.1f} MB", flush=True)
    for key in ("missing", "unsupported"):
        for row in info[key]:
            print(f"  [{label}] {key}: " + " ".join(str(row[k]) for k in ("type", "name", "path") if row.get(k))
                  + (f": {row['reason']}" if row.get("reason") else ""), flush=True)
    shutil.rmtree(stage, ignore_errors=True)


def run(outdir, before_path, blends):
    outdir.mkdir(parents=True, exist_ok=True)
    os.environ["LOOPCUT_DATA_DIR"] = str(outdir / "data")
    os.environ["LOOPCUT_CONFIG_DIR"] = str(outdir / "config")
    checkout.use()
    from loopcut import bundles
    spec = importlib.util.spec_from_file_location("loopcut._bundles_before", before_path)
    before = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = before
    spec.loader.exec_module(before)
    for blend in blends:
        diagnose(bundles, blend)
        print(f"-- {blend.name}: packer results", flush=True)
        collect(before, "before", blend, outdir)
        collect(bundles, "after", blend, outdir)
    print("DONE bundle inputs check", flush=True)


if __name__ == "__main__":
    try:
        args = sys.argv[sys.argv.index("--") + 1:]
        run(Path(args[0]), Path(args[1]), [Path(p).resolve() for p in args[2:]])
    except Exception:
        traceback.print_exc()
        print("FAIL bundle inputs check", flush=True)
        sys.stderr.flush()
        os._exit(1)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)
