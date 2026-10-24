"""Recorded M1 rig regression. Run under heavy_lock, never starts a paid service."""
import argparse
import os
from pathlib import Path
import sys
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parent))
import checkout
checkout.use()
import bpy
from loopcut import autorig, rig_transport


def load(root, fid):
    bpy.ops.wm.open_mainfile(filepath=str(root / 'prepared' / fid / 'original.blend'), load_ui=False, use_scripts=False)
    objects = [o for o in bpy.context.scene.objects if o.type == 'MESH']
    autorig._select(objects)
    return objects


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--m1', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    args.out.mkdir(parents=True, exist_ok=True)
    for fid in ['B1', 'B2', 'R2', 'R4', 'G3']:
        objects = load(args.m1, fid)
        print('FIXTURE', fid, [(o.name, len(o.data.vertices), list(o.rotation_euler), list(o.scale)) for o in objects], flush=True)
        try:
            print('INSPECT', fid, autorig.inspect([o.name for o in objects])[3], flush=True)
        except Exception as ex:
            print('REFUSED', fid, type(ex).__name__, str(ex), flush=True)
        rows = rig_transport.skin(args.m1 / 'results' / fid / 'native' / 'rigged.glb')
        print('SKIN', fid, len(rows[0]), len(rows[1]), len(rows[2]), flush=True)
    print('DONE diagnostics', flush=True)


if __name__ == '__main__':
    try:
        main()
    except BaseException:
        traceback.print_exc()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)
