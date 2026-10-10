"""Real RNA regression and timing checks, run by the user (no model or window needed):
    Blender -b --factory-startup --python loopcut/tests/blender_api_notes.py
"""

import os
import sys
import time
import traceback
from pathlib import Path


def main():
    # Always test this worktree, even in a Blender build with another Loopcut copy enabled.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "harness"))
    import checkout
    checkout.use()
    import bpy
    from loopcut import api_docs, tools

    assert bpy.app.version >= (5, 0, 0), bpy.app.version_string
    print("Blender", bpy.app.version_string)
    armature = bpy.data.armatures.new("LoopcutApiNotes")
    rig = bpy.data.objects.new("LoopcutApiNotes", armature)
    bpy.context.scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    rig.select_set(True)
    try:
        bpy.ops.object.mode_set(mode="EDIT")
        bone = armature.edit_bones.new("Probe")
        bone.head, bone.tail = (0, 0, 0), (0, 0, 1)
        bpy.ops.object.mode_set(mode="OBJECT")

        def capture(code, namespace):
            try:
                exec(compile(code, "<loopcut>", "exec"), namespace)
            except AttributeError as ex:
                started = time.perf_counter()
                note = api_docs.error_note(ex)
                elapsed_ms = (time.perf_counter() - started) * 1000
                print(f"{ex}\nNOTE: {note}\nfirst call: {elapsed_ms:.3f} ms")
                assert note and len(note.splitlines()) <= 2, note
                assert all(len(line) <= 320 for line in note.splitlines()), note
                assert "NOTE: " + note in tools._model_traceback(ex)
                return ex, note
            raise AssertionError(f"Expected a real AttributeError from: {code}")

        select_error, select_note = capture("rig.data.bones['Probe'].select = True", {"rig": rig})
        assert str(select_error) == "'Bone' object has no attribute 'select'", str(select_error)
        for member in ("PoseBone.select", "EditBone.select"):
            assert member in select_note, select_note
        assert "PoseBone.select via Object.pose.bones[...]" in select_note, select_note
        assert "EditBone.select via Armature.edit_bones[...]" in select_note, select_note
        # Verify that the reported paths and attributes exist on actual instances as well.
        assert isinstance(rig.pose.bones["Probe"].select, bool)
        bpy.ops.object.mode_set(mode="EDIT")
        assert isinstance(armature.edit_bones["Probe"].select, bool)
        bpy.ops.object.mode_set(mode="OBJECT")

        # Blender 5.0 rename, independent of the Bone selection move:
        # https://developer.blender.org/docs/release_notes/5.0/modeling/#python-api-compatibility-changes
        sculpt = bpy.context.scene.tool_settings.uv_sculpt
        rename_error, rename_note = capture("sculpt.curve_preset", {"sculpt": sculpt})
        assert str(rename_error) == "'UvSculpt' object has no attribute 'curve_preset'", str(rename_error)
        assert "Closest on UvSculpt:" in rename_note, rename_note
        assert "curve_distance_falloff_preset" in rename_note, rename_note
        assert "curve_distance_falloff_preset" in sculpt.bl_rna.properties
        assert isinstance(sculpt.curve_distance_falloff_preset, str)

        # Warm the index and both source-type path caches before measuring. Every call still
        # performs difflib matching; this is not a cache of the final note strings.
        for ex, expected in ((select_error, select_note), (rename_error, rename_note)):
            for _ in range(5):
                assert api_docs.error_note(ex) == expected
            samples = []
            for _ in range(200):
                started = time.perf_counter()
                result = api_docs.error_note(ex)
                samples.append((time.perf_counter() - started) * 1000)
                assert result == expected, result
            print(f"warm {ex}: mean={sum(samples) / len(samples):.3f} ms, max={max(samples):.3f} ms")
            assert max(samples) < 50, f"Warm error_note exceeded 50 ms: {max(samples):.3f} ms"
    finally:
        if rig.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        bpy.data.objects.remove(rig, do_unlink=True)
        bpy.data.armatures.remove(armature)


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        traceback.print_exc()
        sys.stdout.flush()
        sys.stderr.flush()
        # Blender can swallow script exceptions without --python-exit-code; this command must
        # return 1 on failure even when invoked exactly as documented above.
        os._exit(1)
    print("BLENDER_API_NOTES OK", flush=True)
