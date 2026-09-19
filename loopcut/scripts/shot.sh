#!/bin/sh
# Render a fixture to out/<name>.png, out/<name>.panel.png and out/<name>.json.
#   loopcut/scripts/shot.sh full
set -eu
repo="$(cd "$(dirname "$0")/../.." && pwd)"   # The repository. Builds, tools/, out/ and .env sit next to it.
work="$(dirname "$repo")"
name="${1:-full}"
blender="${LOOPCUT_BLENDER:-$work/tools/Blender.app/Contents/MacOS/Blender}"
exec "$blender" --factory-startup -p 0 0 1440 900 \
  --python "$repo/loopcut/harness/shot.py" -- \
  --fixture "$repo/loopcut/harness/fixtures/$name.json" --out "$work/out/$name"
