#!/bin/sh
# First-run onboarding of the Loopcut build, in a throwaway home folder. See harness/onboarding_check.py.
#   LOOPCUT_BLENDER=build/lite/bin/Loopcut.app/Contents/MacOS/Loopcut scripts/onboarding_check.sh
set -eu
root="$(cd "$(dirname "$0")/.." && pwd)"
loopcut="${LOOPCUT_BLENDER:-$root/build/lite/bin/Loopcut.app/Contents/MacOS/Loopcut}"
stock="${LOOPCUT_STOCK_BLENDER:-$root/tools/Blender.app/Contents/MacOS/Blender}"
home="$(mktemp -d)"
trap 'rm -rf "$home"' EXIT
# macOS resolves Application Support from CFFIXED_USER_HOME, everything else from HOME.
export HOME="$home" CFFIXED_USER_HOME="$home" XDG_CONFIG_HOME="$home/.config" APPDATA="$home/AppData"
unset LOOPCUT_API_KEY LOOPCUT_ENV_FILE

# Stock Blender settings to import, with one preference that differs from the default.
"$stock" -b --factory-startup --python-expr \
  "import bpy; bpy.context.preferences.view.show_tooltips_python = True; bpy.ops.wm.save_userpref()" >/dev/null

# Click through a fresh start first (it saves preferences), in a home folder of its own.
# The click heights are for a 2x display; pass others with LOOPCUT_CLICK_Y="start_fresh continue".
fresh="$(mktemp -d)"
trap 'rm -rf "$home" "$fresh"' EXIT
cp -R "$home/." "$fresh/"
HOME="$fresh" CFFIXED_USER_HOME="$fresh" "$loopcut" -p 0 0 1440 900 --enable-event-simulate \
  --python "$root/harness/onboarding_check.py" -- --stage fresh --out "$root/out/onboarding" --click-y ${LOOPCUT_CLICK_Y:-473 425}

for stage in first connect privacy done; do
  "$loopcut" -p 0 0 1440 900 --python "$root/harness/onboarding_check.py" -- --stage "$stage" --out "$root/out/onboarding"
done
