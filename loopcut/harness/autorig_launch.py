#!/usr/bin/env python3
"""Launch this checkout's add-on in an isolated Loopcut, under the heavy lock."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def running_blenders():
    """The shared helper's default scanner only recognizes its bundled Blender."""
    rows = subprocess.check_output(['ps', '-axo', 'pid=,comm='], text=True).splitlines()
    return [int(parts[0]) for row in rows if len(parts := row.strip().split(None, 1)) == 2
            and parts[1].endswith(('/Contents/MacOS/Loopcut', '/Contents/MacOS/Blender'))]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', help='Platform origin for an interactive persona session; no job starts automatically')
    parser.add_argument('--m1', type=Path, help='Recorded M1 directory for the automatic offline GUI check')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--file', type=Path)
    parser.add_argument('--fixture', default='B2', help='Recorded fixture for offline GUI tests')
    parser.add_argument('--credential-env', help='Read a test platform token from this environment variable, never the command line')
    parser.add_argument('--blender', type=Path, default=Path('/Applications/Loopcut.app/Contents/MacOS/Loopcut'))
    parser.add_argument('--heavy-lock', type=Path)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    lock = args.heavy_lock or repo.parent / 'wt-loop/loopcut/track/loop/tools/heavy_lock.py'
    if not lock.is_file() or not args.blender.is_file():
        parser.error('A Loopcut executable and the shared heavy_lock.py are required')
    if not args.server and args.m1 is None:
        parser.error('Provide --server for interactive use or --m1 for an offline GUI check')
    root = Path(tempfile.mkdtemp(prefix='loopcut-autorig-profile-'))
    (root / 'empty.env').touch()
    env = os.environ.copy()
    env.update(BLENDER_USER_RESOURCES=str(root / 'blender-user'), LOOPCUT_CONFIG_DIR=str(root / 'config'),
               LOOPCUT_DATA_DIR=str(root / 'data'), LOOPCUT_ENV_FILE=str(root / 'empty.env'), LOOPCUT_TELEMETRY='off',
               LOOPCUT_SERVICE_URL=args.server or 'http://127.0.0.1:9')
    if args.server:
        env['LOOPCUT_SERVICE_URL'] = args.server
    if args.credential_env:
        token = os.environ.get(args.credential_env)
        if not token:
            parser.error('The named test credential environment variable is empty')
        env['LOOPCUT_AUTORIG_TEST_KEY'] = token
    command = [str(args.blender), '--factory-startup', '--enable-event-simulate', '-p', '0', '0', '1440', '900',
               '--python', str(repo / 'loopcut/harness/autorig_gui.py'), '--', '--out', str(args.out.resolve())]
    for option in ('server', 'm1', 'file', 'fixture'):
        value = getattr(args, option)
        if value:
            command += ['--' + option, str(value)]
    print('Isolated profile:', root, flush=True)
    sys.path.insert(0, str(lock.parent))
    import heavy_lock
    with heavy_lock.HeavyLock('autorig-client-gui', floor_mb=1500, poll=10,
                              no_other_blender=True, blenders=running_blenders):
        return subprocess.call(command, env=env, cwd=repo)


if __name__ == '__main__':
    raise SystemExit(main())
