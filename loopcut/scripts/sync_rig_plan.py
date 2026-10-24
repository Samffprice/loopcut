#!/usr/bin/env python3
"""Vendor the exact shared planner into an explicitly supplied worker checkout.

Usage: python3 loopcut/scripts/sync_rig_plan.py WORKER_CHECKOUT [--check]
Only workers/autorig/rig_plan.py is written. Review and commit that file in the
worker lane; the client lane tests this script against temporary directories.
"""
import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'scripts/addons_core/loopcut/rig_plan.py'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('worker_checkout', type=Path)
    parser.add_argument('--check', action='store_true', help='Check exact bytes without writing')
    args = parser.parse_args()
    directory = args.worker_checkout.resolve() / 'workers/autorig'
    if not directory.is_dir() or directory.is_symlink():
        parser.error('WORKER_CHECKOUT must contain a real workers/autorig directory')
    destination = directory / SOURCE.name
    if destination.is_symlink():
        parser.error('Refusing a symlink destination')
    data = SOURCE.read_bytes()
    if args.check:
        if not destination.is_file() or destination.read_bytes() != data:
            parser.exit(1, 'Vendored rig_plan.py differs from the client source.\n')
    else:
        shutil.copyfile(SOURCE, destination)
    commit = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'],
                            capture_output=True, text=True, check=True).stdout.strip()
    print(('Verified' if args.check else 'Copied') + f' {destination}\n'
          f'Source commit: {commit}\nSHA-256: {hashlib.sha256(data).hexdigest()}\n'
          'Record the source commit, PLANNER_VERSION and SHA-256 in the worker vendor notes.')


if __name__ == '__main__':
    main()
