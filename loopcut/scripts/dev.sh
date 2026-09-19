#!/bin/sh
# Blender with the Loopcut panel open and hot reload on. Edit any file under
# scripts/addons_core/loopcut/ and save.
set -eu
repo="$(cd "$(dirname "$0")/../.." && pwd)"   # The repository. Builds, tools/, out/ and .env sit next to it.
work="$(dirname "$repo")"
blender="${LOOPCUT_BLENDER:-$work/tools/Blender.app/Contents/MacOS/Blender}"
LOOPCUT_DEV=1 exec "$blender" --python "$repo/loopcut/harness/dev.py" "$@"
