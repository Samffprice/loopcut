#!/bin/sh
# Build the Loopcut release for this Mac (Apple silicon) and package it as a .dmg.
#   loopcut/scripts/release_mac.sh v0.1.0            ../build/release/loopcut-v0.1.0-*.dmg
#   loopcut/scripts/release_mac.sh v0.1.0 --upload   also add it to the GitHub release (a draft if new)
# A full release build, Cycles included: the first run takes a while; later ones are incremental.
# The build folder is ../build/release, next to the repository. Windows comes from
# .github/workflows/release.yml. The version must be the one in bl_info (bump
# scripts/addons_core/loopcut/__init__.py first): installed apps compare it with the release to
# know when to update. Once both installers are on the release, publish_release.sh publishes it.
set -eu
repo="$(cd "$(dirname "$0")/../.." && pwd)"   # The repository. Builds, tools/, out/ and .env sit next to it.
work="$(dirname "$repo")"
version="${1:?usage: loopcut/scripts/release_mac.sh v0.1.0 [--upload]}"
printf '%s' "$version" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$' || { echo "version must look like v0.1.0" >&2; exit 1; }
addon="$("$repo/loopcut/scripts/addon_version.sh")"
[ "$version" = "$addon" ] || { echo "the add-on says $addon (bl_info in scripts/addons_core/loopcut/__init__.py); bump it first" >&2; exit 1; }
build="$work/build/release"

# -D after -C on purpose: the release configuration force-sets the options it names.
cmake -S "$repo" -B "$build" -G Ninja \
  -C "$repo/build_files/cmake/config/blender_release.cmake" \
  -DWITH_ASSERT_ABORT=OFF -DWITH_ASSERT_RELEASE=OFF \
  -DCPACK_OVERRIDE_PACKAGENAME="loopcut-$version"
ninja -C "$build" install
"$build/bin/Loopcut.app/Contents/MacOS/Loopcut" -b --factory-startup --python-expr \
  "import bpy, loopcut; assert hasattr(bpy.types, 'SpaceLoopcut'); print('Loopcut build OK', bpy.app.version_string)"
rm -f "$build"/loopcut-*.dmg
(cd "$build" && cpack -G DragNDrop)
dmg="$(ls "$build"/loopcut-"$version"-*.dmg)"
echo "Built $dmg"

if [ "${2:-}" = "--upload" ]; then
  cd "$repo"  # gh finds the GitHub repository from the checkout it runs in.
  gh release view "$version" >/dev/null 2>&1 ||
    gh release create "$version" --draft --generate-notes --title "Loopcut $version"
  gh release upload "$version" "$dmg" --clobber
fi
