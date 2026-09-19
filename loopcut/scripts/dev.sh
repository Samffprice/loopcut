#!/bin/sh
# Blender with the Loopcut panel open and hot reload on. Edit any file under extension/ and save.
set -eu
root="$(cd "$(dirname "$0")/.." && pwd)"
blender="${LOOPCUT_BLENDER:-$root/tools/Blender.app/Contents/MacOS/Blender}"
LOOPCUT_DEV=1 exec "$blender" --python "$root/harness/dev.py" "$@"
