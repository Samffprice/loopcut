"""Generated sound effects in a cloud render's bundle: what each strip keeps, and what each packer says.

    <blender> -b --factory-startup --python loopcut/harness/sfx_bundle_check.py -- \
        <outdir> [<old bundles.py>...]

The add-on's own path minus the upload. A short WAV is written into a new OS temp folder named
the way cloud_jobs._download names a job's download; cloud_jobs._add_audio adds and packs it.
Each case then takes the snapshot a cloud render takes (checkpoints.for_bundle), prints the sound
strips and their sounds, every path bpy.utils.blend_paths lists with the IDs file_path_map says
list it, and collects the snapshot with each old packer (bundles.collect) and with this checkout
(checkpoints.bundle_from_checkpoint). Only local staging under outdir is written; no cloud client
is created. PASS/FAIL per case is this checkout's: a packed sound never blocks, an unpacked one
is copied, a missing one is reported missing.

Start it with LOOPCUT_CONFIG_DIR and BLENDER_USER_RESOURCES set to empty folders, so the Loopcut
build's bundled add-on starts signed out.
"""

import hashlib
import importlib.util
import math
import os
import shutil
import struct
import sys
import tempfile
import traceback
import uuid
import wave
from collections import defaultdict
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
import checkout  # noqa: E402

failures, downloads = [], []


def tone(path, seconds=0.5, rate=22050):
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * n / rate)))
                                 for n in range(int(seconds * rate))))


def add_sfx(cloud_jobs, prompt):
    """A finished sfx job's output where _download leaves it, applied by the add-on's handler."""
    job_id = "job_" + uuid.uuid4().hex
    folder = Path(tempfile.mkdtemp(prefix=job_id + "-"))
    downloads.append(folder)
    scratch = folder / "tone.wav"
    tone(scratch)
    digest = hashlib.sha256(scratch.read_bytes()).hexdigest()
    path = folder / f"0000-{digest[:12]}.wav"
    scratch.rename(path)
    job = {"id": job_id, "tool": "sfx", "status": "succeeded", "input": {"prompt": prompt},
           "outputs": [{"name": "sound", "type": "audio", "mime": "audio/wav", "sha256": digest,
                        "size": path.stat().st_size, "path": str(path)}],
           "provenance": {"job_id": job_id, "tool": "sfx", "vendor": "harness", "model": "tone",
                          "kind": "gen", "created_at": "2026-10-17T00:00:00Z"}}
    print(f"  added: {cloud_jobs._add_audio(job)} from {path}", flush=True)
    return path


def sound_strips():
    return [strip for scene in bpy.data.scenes if scene.sequence_editor
            for strip in scene.sequence_editor.strips_all if strip.type == "SOUND"]


def describe(bundles):
    base = bpy.data.filepath
    for strip in sound_strips():
        sound = strip.sound
        print(f"  strip {strip.name!r}: sound {sound.name!r} filepath {sound.filepath!r} "
              f"({'packed' if sound.packed_file else 'not packed'}; file there: "
              f"{Path(bpy.path.abspath(sound.filepath)).is_file()})", flush=True)
    owners = defaultdict(list)
    for owner, raws in bpy.data.file_path_map().items():
        for raw in raws:
            owners[str(bundles._resolve_path(raw, base))].append(f"{owner.id_type} {owner.name!r} {raw!r}")
    for raw in bpy.utils.blend_paths(absolute=True, local=True):
        path = bundles._resolve_path(raw, base) if raw else None
        print(f"  blend_paths {raw!r}: exists {bool(path) and path.exists()}; listed by "
              f"{owners.get(str(path)) or 'no ID (weak reference or empty)'}", flush=True)


def collect(checkpoints, session, packers, outdir, label, expect):
    checkpoint = checkpoints.for_bundle(session)
    store = checkpoints._store(session)
    entry, snapshot = store.get(checkpoint), store.snapshot_path(checkpoint)
    original = entry.get("source", "") if entry.get("in_place", False) else None
    print(f"  snapshot: {'recovery file, original ' + repr(original) if original is not None else 'copy save'}",
          flush=True)
    for name, module in packers:
        stage = outdir / f"stage-{label}-{name}"
        shutil.rmtree(stage, ignore_errors=True)
        try:
            if module is None:
                info = checkpoints.bundle_from_checkpoint(session, checkpoint, stage)
                staged = sorted(row["path"] for row in info["manifest"]["files"])
            else:
                info = module.collect(snapshot, stage, original_path=original)
                staged = sorted(p.relative_to(stage).as_posix() for p in stage.rglob("*") if p.is_file())
        except Exception as ex:
            print(f"  [{name}] raised {type(ex).__name__}: {str(ex)[-600:]}", flush=True)
            if module is None:
                failures.append(label)
            continue
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        print(f"  [{name}] missing {len(info['missing'])}, unsupported {len(info['unsupported'])}; staged {staged}",
              flush=True)
        for key in ("missing", "unsupported"):
            for row in info[key]:
                print(f"  [{name}] {key}: " + " ".join(str(row[k]) for k in ("type", "name", "path") if row.get(k))
                      + (f": {row['reason']}" if row.get("reason") else ""), flush=True)
        if module is None:
            got = ([row["type"] for row in info["missing"]], info["unsupported"],
                   any(path.startswith("sounds/") for path in staged))
            ok = got == expect
            print(f"{'PASS' if ok else 'FAIL'} {label}: missing {got[0]}, unsupported {got[1]}, "
                  f"sound copied {got[2]}", flush=True)
            if not ok:
                failures.append(label)
    checkpoints.discard(session, checkpoint)


def run(outdir, old_packers):
    outdir.mkdir(parents=True, exist_ok=True)
    os.environ["LOOPCUT_DATA_DIR"] = str(outdir / "data")
    os.environ.setdefault("LOOPCUT_CONFIG_DIR", str(outdir / "config"))
    # First: factory settings enable the Loopcut build's bundled add-on again, which would replace this checkout.
    bpy.ops.wm.read_factory_settings(use_empty=True)
    checkout.use()
    from loopcut import bundles, checkpoints, cloud_jobs, state
    packers = []
    for path in old_packers:
        spec = importlib.util.spec_from_file_location("loopcut._" + path.stem, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        packers.append((path.stem, module))
    packers.append(("this checkout", None))
    print(f"Blender {bpy.app.version_string}; temp folder {tempfile.gettempdir()}; "
          f"in-place snapshots {checkpoints.in_place_supported()}", flush=True)
    assert Path(cloud_jobs.__file__).resolve().is_relative_to(checkout.PACKAGE), cloud_jobs.__file__
    session = state.session()
    project = outdir / "project"
    shutil.rmtree(project, ignore_errors=True)
    project.mkdir()

    def case(label, expect=([], [], False)):
        print(f"\n== {label}", flush=True)
        try:
            describe(bundles)
        except Exception as ex:  # The listing is evidence; the packers' verdicts still count.
            print(f"  (could not list paths: {type(ex).__name__}: {ex})", flush=True)
        collect(checkpoints, session, packers, outdir, label.split(":")[0], expect)

    download = add_sfx(cloud_jobs, "door creak")
    case("unsaved: a generated sound, packed by the add-on")
    bpy.ops.wm.save_as_mainfile(filepath=str(project / "scene.blend"))
    case("saved: the project saved with relative paths")
    original = download.read_bytes()
    shutil.rmtree(download.parent)
    case("gone: the temp download cleared")
    download.parent.mkdir()
    download.write_bytes(original)  # Not yet cleared: the strip's old path names a real file.
    sound = sound_strips()[0].sound
    sound.unpack(method="USE_LOCAL")
    case("unpacked: Unpack Resources wrote it beside the project", ([], [], True))
    unpacked = Path(bpy.path.abspath(sound.filepath))
    kept = unpacked.read_bytes()
    unpacked.unlink()
    case("unpacked-missing: its unpacked file deleted", (["sounds"], [], False))
    unpacked.write_bytes(kept)
    sound.pack()
    case("repacked: packed again from the project folder")
    sound.unpack(method="REMOVE")
    sound.filepath = str(Path(add_sfx(cloud_jobs, "door slam")).resolve())
    sound.pack()
    case("relinked: pointed at another file and packed")
    for folder in downloads:
        shutil.rmtree(folder, ignore_errors=True)
    print("\n" + (f"FAIL sfx bundle check: {failures}" if failures else "DONE sfx bundle check: all PASS"), flush=True)
    return not failures


if __name__ == "__main__":
    try:
        args = sys.argv[sys.argv.index("--") + 1:]
        passed = run(Path(args[0]).resolve(), [Path(p).resolve() for p in args[1:]])
    except Exception:
        traceback.print_exc()
        print("FAIL sfx bundle check", flush=True)
        passed = False
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if passed else 1)
