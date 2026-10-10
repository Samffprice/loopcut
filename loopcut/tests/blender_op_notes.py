"""Real RNA regression and timing checks, run by the user (no model or window needed):
    Blender -b --factory-startup --python loopcut/tests/blender_op_notes.py

Measures exact boundary distances on axis-aligned rectangular islands. The median
neighbour gap is the median of each island's nearest boundary-to-boundary gap,
not the median of distant, non-neighbouring pairs (also printed for comparison).
"""

import math
import os
import statistics
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

RESOLUTION = 2048
# AABB packs padded rectangles directly; allow 0.25 px for UV float/solver error.
# CONCAVE also tries bitmap packing: allow 3 px for its discretization (still
# distinguishes the 8/16 and 4/8 px hypotheses at both requested margins).
TOLERANCES = {"AABB": 0.25, "CONCAVE": 3.0}


def read_uvs(obj):
    return tuple(tuple(uv.vector) for uv in obj.data.uv_layers.active.uv)


def make_fixture():
    import bpy

    bpy.ops.object.select_all(action="DESELECT")
    vertices, faces = [], []
    sizes = ((1.6, 1.1), (1.3, 0.9), (1.0, 0.7), (0.8, 0.6), (0.6, 0.4), (0.4, 0.3))
    for index, (width, height) in enumerate(sizes):
        start, x = len(vertices), index * 3
        vertices.extend(((x, 0, 0), (x + width, 0, 0), (x + width, height, 0), (x, height, 0)))
        faces.append(tuple(range(start, start + 4)))
    mesh = bpy.data.meshes.new("LoopcutOpNotes")
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    obj = bpy.data.objects.new("LoopcutOpNotes", mesh)
    bpy.context.scene.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.context.scene.tool_settings.use_uv_select_sync = True
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    assert bpy.ops.uv.unwrap(method="ANGLE_BASED", margin=0) == {"FINISHED"}
    bpy.ops.object.mode_set(mode="OBJECT")

    # Unwrap chooses each island's orientation. Rigidly align its first edge to
    # X so bounds measure actual boundaries, preserving the unwrapped dimensions.
    layer = mesh.uv_layers.active.uv
    for face in mesh.polygons:
        points = [tuple(layer[i].vector) for i in face.loop_indices]
        origin = points[0]
        dx, dy = points[1][0] - origin[0], points[1][1] - origin[1]
        length = math.hypot(dx, dy)
        assert length > 0
        cosine, sine = dx / length, dy / length
        for index, (x, y) in zip(face.loop_indices, points):
            x, y = x - origin[0], y - origin[1]
            layer[index].vector = (x * cosine + y * sine, -x * sine + y * cosine)
    return obj, read_uvs(obj)


def reset_uvs(obj, initial):
    assert obj.mode == "OBJECT"
    obj.data.uv_layers.active.uv.foreach_set("vector", [v for point in initial for v in point])
    obj.data.update()


def pack_code(margin, method="FRACTION", shape="AABB"):
    return ("bpy.ops.object.mode_set(mode='EDIT')\n"
            "bpy.ops.mesh.select_all(action='SELECT')\n"
            "try:\n"
            f"    result = bpy.ops.uv.pack_islands(margin_method={method!r}, margin={margin!r}, "
            f"rotate=False, scale=True, shape_method={shape!r}, merge_overlap=False, udim_source='ACTIVE_UDIM')\n"
            "    assert result == {'FINISHED'}, result\n"
            "finally:\n"
            "    bpy.ops.object.mode_set(mode='OBJECT')\n")


def pack_fixture(obj, initial, margin, method="FRACTION", shape="AABB"):
    import bpy

    reset_uvs(obj, initial)
    exec(compile(pack_code(margin, method, shape), "<op-notes-fixture>", "exec"), {"bpy": bpy})
    return read_uvs(obj)


def measure(obj):
    uvs = read_uvs(obj)
    boxes = []
    for face in obj.data.polygons:
        points = [uvs[i] for i in face.loop_indices]
        low = tuple(min(p[axis] for p in points) for axis in (0, 1))
        high = tuple(max(p[axis] for p in points) for axis in (0, 1))
        assert min(high[axis] - low[axis] for axis in (0, 1)) > 0
        for point in points:
            assert all(min(abs(point[axis] - edge[axis]) for edge in (low, high)) < 1e-5
                       for axis in (0, 1)), ("fixture is not axis-aligned", points)
        boxes.append((low, high))
    nearest, all_pairs = [math.inf] * len(boxes), []
    for i, (a, b) in enumerate(boxes):
        for j in range(i + 1, len(boxes)):
            c, d = boxes[j]
            dx, dy = (max(a[axis] - d[axis], c[axis] - b[axis], 0) for axis in (0, 1))
            assert dx > 0 or dy > 0, ("overlapping islands", i, j)
            gap = math.hypot(dx, dy) * RESOLUTION
            nearest[i], nearest[j] = min(nearest[i], gap), min(nearest[j], gap)
            all_pairs.append(gap)
    border = min(min(u, v, 1 - u, 1 - v) for u, v in uvs) * RESOLUTION
    return min(nearest), statistics.median(nearest), border, statistics.median(all_pairs)


def measure_fraction(obj, initial):
    for shape, tolerance in TOLERANCES.items():
        for pixels in (8, 4):
            pack_fixture(obj, initial, pixels / RESOLUTION, shape=shape)
            minimum, median, border, pair_median = measure(obj)
            print(f"{shape} margin={pixels}/2048: min gap={minimum:.4f} px, "
                  f"median nearest-neighbour gap={median:.4f} px, min border={border:.4f} px; "
                  f"all-pairs median={pair_median:.4f} px. "
                  f"Expected gap={2 * pixels} px, border={pixels} px, tolerance=+/-{tolerance} px.", flush=True)
            assert abs(minimum - 2 * pixels) <= tolerance, (shape, pixels, minimum)
            assert abs(median - 2 * pixels) <= tolerance, (shape, pixels, median)
            assert abs(border - pixels) <= tolerance, (shape, pixels, border)


def check_live(obj, initial, op_notes, tools):
    original = sys.modules["bpy.ops"]._op_create_function
    notice = f"{op_notes.NOTICE_PREFIX} {op_notes.NOTES['uv.pack_islands'][0][1]}"
    assert not op_notes.take(), "direct pack calls must not install a live hook"

    def run(method):
        reset_uvs(obj, initial)
        result = tools.run_python(pack_code(8 / RESOLUTION, method))
        assert sys.modules["bpy.ops"]._op_create_function is original
        assert result.ok, result.text
        return result

    scaled = run("SCALED")
    assert op_notes.NOTICE_PREFIX not in scaled.text, scaled.text
    baseline = pack_fixture(obj, initial, 8 / RESOLUTION)
    first = run("FRACTION")
    assert first.text.count(notice) == 1, first.text
    assert read_uvs(obj) == baseline, "hook changed packed UVs"
    second = run("FRACTION")
    assert op_notes.NOTICE_PREFIX not in second.text, second.text
    assert read_uvs(obj) == baseline, "second pack changed UVs"
    scaled = run("SCALED")
    assert op_notes.NOTICE_PREFIX not in scaled.text, scaled.text
    failed = tools.run_python("raise ValueError('op_notes restoration probe')")
    assert not failed.ok
    assert sys.modules["bpy.ops"]._op_create_function is original
    assert not op_notes.take()
    print("run_python: once per session, SCALED quiet, UVs identical, hook restored after success/error.", flush=True)


def check_startup(op_notes):
    import bpy

    with tempfile.TemporaryDirectory(prefix="loopcut-op-notes-") as tmp:
        script = Path(tmp) / "pack.py"
        script.write_text(
            "import runpy\n"
            f"fixture = runpy.run_path({str(Path(__file__).resolve())!r}, run_name='op_notes_fixture')\n"
            "obj, initial = fixture['make_fixture']()\n"
            "print('BEFORE FRACTION', flush=True)\n"
            "fixture['pack_fixture'](obj, initial, 8 / 2048)\n"
            "print('AFTER FRACTION', flush=True)\n"
            "fixture['pack_fixture'](obj, initial, 4 / 2048)\n"
            "print('CHILD OP_NOTES OK', flush=True)\n", encoding="utf-8")
        result = subprocess.run(
            [bpy.app.binary_path, "-b", "--factory-startup", "--python-exit-code", "1",
             "--python", str(Path(op_notes.__file__).resolve()), "--python", str(script)],
            cwd=tmp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=180)
        print(result.stdout, flush=True)
        assert result.returncode == 0, result.stdout
        lines = result.stdout.splitlines()
        notices = [line for line in lines if line.startswith(op_notes.NOTICE_PREFIX)]
        expected = f"{op_notes.NOTICE_PREFIX} {op_notes.NOTES['uv.pack_islands'][0][1]}"
        assert notices == [expected], notices
        assert lines.index("BEFORE FRACTION") < lines.index(expected) < lines.index("AFTER FRACTION")
        assert "CHILD OP_NOTES OK" in lines


def time_lookups(op_notes):
    import bpy

    original = sys.modules["bpy.ops"]._op_create_function
    count = 20000
    for name, lookup in (("noted", lambda: bpy.ops.uv.pack_islands),
                         ("other", lambda: bpy.ops.object.select_all)):
        samples = {False: [], True: []}
        for _ in range(5):
            for enabled in (False, True):
                restore = op_notes.install() if enabled else lambda: None
                try:
                    started = time.perf_counter()
                    for _ in range(count):
                        lookup()
                    samples[enabled].append((time.perf_counter() - started) * 1e6 / count)
                finally:
                    restore()
        plain, hooked = (statistics.median(samples[enabled]) for enabled in (False, True))
        print(f"{name} operator lookup ({count} x 5): plain={plain:.3f} us, hooked={hooked:.3f} us, "
              f"overhead={hooked - plain:.3f} us/lookup", flush=True)
    assert sys.modules["bpy.ops"]._op_create_function is original


def main():
    # Always test this worktree, even in a Blender build with another Loopcut copy enabled.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "harness"))
    import checkout
    checkout.use()
    import bpy
    from loopcut import api_docs, op_notes, tools

    assert bpy.app.version >= (5, 2, 0), bpy.app.version_string
    print("Blender", bpy.app.version_string, flush=True)
    assert op_notes.NOTES["uv.pack_islands"][0][1] in api_docs.inspect_api("bpy.ops.uv.pack_islands")
    sync = bpy.context.scene.tool_settings.use_uv_select_sync
    obj, initial = make_fixture()
    try:
        measure_fraction(obj, initial)
        check_live(obj, initial, op_notes, tools)
        if os.environ.get("LOOPCUT_SKIP_CHILD_BLENDER"):  # Machines capped at 2 Blenders: run the child check alone.
            print("startup-file check skipped (LOOPCUT_SKIP_CHILD_BLENDER)", flush=True)
        else:
            check_startup(op_notes)
        time_lookups(op_notes)
    finally:
        if obj.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        mesh = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        bpy.data.meshes.remove(mesh)
        bpy.context.scene.tool_settings.use_uv_select_sync = sync


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
    print("BLENDER_OP_NOTES OK", flush=True)
