#!/bin/sh
# Build the Loopcut release for this Mac (Apple silicon) and package it as a .dmg.
#   scripts/release_mac.sh v0.1.0            build/release/loopcut-v0.1.0-*.dmg
#   scripts/release_mac.sh v0.1.0 --upload   also add it to the GitHub release (a draft if new)
# A full release build, Cycles included: the first run takes a while; later ones are incremental.
# The build folder stays next to build/lite, on this disk. Windows comes from
# .github/workflows/release.yml.
set -eu
root="$(cd "$(dirname "$0")/.." && pwd)"
version="${1:?usage: scripts/release_mac.sh v0.1.0 [--upload]}"
printf '%s' "$version" | grep -Eq '^v[0-9A-Za-z._-]+$' || { echo "version must look like v0.1.0" >&2; exit 1; }
build="$root/build/release"

# -D after -C on purpose: the release configuration force-sets the options it names.
cmake -S "$root/blender" -B "$build" -G Ninja \
  -C "$root/blender/build_files/cmake/config/blender_release.cmake" \
  -DWITH_ASSERT_ABORT=OFF -DWITH_ASSERT_RELEASE=OFF \
  -DCPACK_OVERRIDE_PACKAGENAME="loopcut-$version"
ninja -C "$build" install
"$build/bin/Blender.app/Contents/MacOS/Blender" -b --factory-startup --python-expr \
  "import bpy, loopcut; assert hasattr(bpy.types, 'SpaceLoopcut'); print('Loopcut build OK', bpy.app.version_string)"
rm -f "$build"/loopcut-*.dmg
(cd "$build" && cpack -G DragNDrop)
dmg="$(ls "$build"/loopcut-"$version"-*.dmg)"
echo "Built $dmg"

if [ "${2:-}" = "--upload" ]; then
  gh release view "$version" >/dev/null 2>&1 ||
    gh release create "$version" --draft --generate-notes --title "Loopcut $version"
  gh release upload "$version" "$dmg" --clobber
fi
