"""The Loopcut build's snapshot operators, which checkpoints restore in place with:
    build/lite/bin/Blender.app/Contents/MacOS/Blender -b --factory-startup --python harness/snapshot_check.py
After a restore the open file keeps its path, is marked unsaved, is untouched on disk, and its
relative paths still resolve. Stock Blender has no such operators and is skipped."""
import hashlib
import sys
import tempfile
from pathlib import Path

import bpy

if not "loopcut_snapshot_restore" in dir(bpy.ops.wm):
    print("SNAPSHOT SKIPPED: not the Loopcut build")
    sys.exit(0)

work = Path(tempfile.mkdtemp(prefix="loopcut-snap-"))
project, store = work / "project", work / "store"
project.mkdir(); store.mkdir()
(project / "tex.png").write_bytes(b"")
blend = project / "desk.blend"

image = bpy.data.images.new("tex", 4, 4); image.filepath_raw = "//tex.png"; image.use_fake_user = True; image.source = "FILE"
bpy.ops.wm.save_as_mainfile(filepath=str(blend))
digest = hashlib.sha256(blend.read_bytes()).hexdigest()

snap = store / "a.blend"
assert bpy.ops.wm.loopcut_snapshot_write(filepath=str(snap)) == {"FINISHED"}
assert bpy.data.filepath == str(blend), bpy.data.filepath
assert not bpy.data.is_dirty, "writing a snapshot must not mark or clean anything"

bpy.data.objects.remove(bpy.data.objects["Cube"])
assert "Cube" not in bpy.data.objects
assert bpy.ops.wm.loopcut_snapshot_restore(filepath=str(snap)) == {"FINISHED"}
assert "Cube" in bpy.data.objects, "scene is back"
assert bpy.data.filepath == str(blend), f"path must stay the user's file, got {bpy.data.filepath}"
assert bpy.data.is_dirty, "restored state is not what is on disk"
assert bpy.data.images["tex"].filepath == "//tex.png", bpy.data.images["tex"].filepath
assert Path(bpy.path.abspath(bpy.data.images["tex"].filepath)).resolve() == (project / "tex.png").resolve()
assert hashlib.sha256(blend.read_bytes()).hexdigest() == digest, "the user's file was written"

# Saved under a new name in another folder after the snapshot: the open file stays the open file,
# and the relative path still finds the texture.
moved = work / "elsewhere" / "desk2.blend"
moved.parent.mkdir()
bpy.ops.wm.save_as_mainfile(filepath=str(moved))
bpy.ops.wm.loopcut_snapshot_restore(filepath=str(snap))
assert bpy.data.filepath == str(moved), bpy.data.filepath
assert Path(bpy.path.abspath(bpy.data.images["tex"].filepath)).resolve() == (project / "tex.png").resolve(), \
    bpy.data.images["tex"].filepath

# Unsaved scene: goes back to being unsaved; the store never becomes the open file.
bpy.ops.wm.read_factory_settings()
snap2 = store / "b.blend"
bpy.ops.wm.loopcut_snapshot_write(filepath=str(snap2))
bpy.data.objects.remove(bpy.data.objects["Cube"])
bpy.ops.wm.loopcut_snapshot_restore(filepath=str(snap2))
assert "Cube" in bpy.data.objects and bpy.data.filepath == "", repr(bpy.data.filepath)
print("SNAPSHOT OK")
