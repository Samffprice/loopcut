#!/bin/sh
# Render a fixture to out/<name>.png, out/<name>.panel.png and out/<name>.json.
#   scripts/shot.sh full
set -eu
root="$(cd "$(dirname "$0")/.." && pwd)"
name="${1:-full}"
blender="${LOOPCUT_BLENDER:-$root/tools/Blender.app/Contents/MacOS/Blender}"
exec "$blender" --factory-startup -p 0 0 1440 900 \
  --python "$root/harness/shot.py" -- \
  --fixture "$root/harness/fixtures/$name.json" --out "$root/out/$name"
