#!/bin/sh
# Package a Mac release from an app already built and this commit's Python, without compiling.
#   loopcut/scripts/repackage_mac.sh v0.2.0 v0.1.6 ../build/release/bin/Loopcut.app [OUT_DIR]
# Arguments: the new version (must equal bl_info), the tag the base app was built from, the base app.
# Output (default ../build/rc-<version>): loopcut-<version>-arm64.dmg, the name publish_release.sh
# expects, and update.json as a dry run (`publish_release.sh <version> --dry-run <dmg>` writes the same).
#
# Only valid when the native code has not changed since the base tag: the updater swaps the whole app,
# so the app must be exactly what release_mac.sh would build. The script refuses when anything changed
# between the base tag and HEAD outside scripts/ (Python the build only copies), the bundled extensions,
# loopcut/, repository text files and packaging-only files (CPack hygiene hook, archive script, CI
# workflows), apart from the install exclusions in source/creator/CMakeLists.txt
# (which this script applies itself: no __pycache__ or *.pyc ships). It first proves the mapping: the
# base app's scripts/ and extensions/ must equal the base tag's git tree under the install rules' exclusions
# (build products such as addons_core/cycles may only be extra). It then applies the base..HEAD change to
# them. Files come from the commits (git archive), never from the working tree, so untracked and ignored
# files cannot ship.
# The image is plain UDZO: no license prompt (the updater's `yes | hdiutil attach` copes with both).
set -eu
repo="$(cd "$(dirname "$0")/../.." && pwd)"
work="$(dirname "$repo")"
version="${1:?usage: loopcut/scripts/repackage_mac.sh vX.Y.Z BASE_TAG BASE_APP [OUT_DIR]}"
base_tag="${2:?base tag, e.g. v0.1.6}"
base_app="${3:?base app, e.g. ../build/release/bin/Loopcut.app}"
out="${4:-$work/build/rc-$version}"
base_app="$(cd "$base_app" && pwd)"
mkdir -p "$out" && out="$(cd "$out" && pwd)"
printf '%s' "$version" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$' || { echo "version must look like v0.2.0" >&2; exit 1; }
addon="$("$repo/loopcut/scripts/addon_version.sh")"
[ "$version" = "$addon" ] || { echo "the add-on says $addon (bl_info); bump it first" >&2; exit 1; }
res="Contents/Resources/5.2"
[ -x "$base_app/Contents/MacOS/Loopcut" ] && [ -d "$base_app/$res/scripts/addons_core/loopcut" ] ||
  { echo "$base_app is not a Loopcut app" >&2; exit 1; }
base_version="$(grep -Eo '"version": \([0-9]+, [0-9]+, [0-9]+\)' "$base_app/$res/scripts/addons_core/loopcut/__init__.py" |
  tr -d '(),' | awk '{print "v" $2 "." $3 "." $4}')"
[ "$base_version" = "$base_tag" ] || { echo "the base app is $base_version, not $base_tag" >&2; exit 1; }

cd "$repo"
git rev-parse -q --verify "$base_tag^{commit}" >/dev/null || { echo "no tag $base_tag" >&2; exit 1; }
# release/loopcut/wix-*: the Windows installer's images (and the script that draws them), not in the Mac app.
native="$(git diff --name-only "$base_tag" HEAD | grep -Ev \
  '^(scripts/|release/extensions/|loopcut/|README\.md$|LICENSE$|\.gitignore$|\.gitattributes$|source/creator/CMakeLists\.txt$|\.github/workflows/[^/]+\.yml$|build_files/cmake/(packaging|loopcut_release_hygiene)\.cmake$|build_files/package_spec/build_archive\.py$|release/loopcut/(wix-banner\.bmp|wix-dialog\.png|make_assets\.py)$)' || true)"
# LOOPCUT_REPACKAGE_WITHOUT: native changes the owner chose to ship without, as space-separated paths (0.2.2: the
# ICON_LOOPCUT editor icon; the add-on's Python never names it). Each path must be listed; anything else still refuses.
without=""; needs_build=""
for f in $native; do
  case " ${LOOPCUT_REPACKAGE_WITHOUT:-} " in *" $f "*) without="$without $f" ;; *) needs_build="$needs_build $f" ;; esac
done
[ -z "$needs_build" ] || { echo "changed since $base_tag outside the add-on (needs release_mac.sh):" >&2; printf '%s\n' $needs_build >&2; exit 1; }
[ -z "$without" ] || echo "shipping without these native changes (LOOPCUT_REPACKAGE_WITHOUT):$without" >&2
# The one allowed CMake change is the install exclusions; anything else in that file needs a build.
cmake_change="$(git diff -U0 "$base_tag" HEAD -- source/creator/CMakeLists.txt | grep -E '^[-+][^-+]' |
  grep -Ev 'PATTERN "(__pycache__|\*\.pyc|\*\.pyo)" EXCLUDE|^[-+] *#|^[-+] *$' || true)"
[ -z "$cmake_change" ] || { echo "source/creator/CMakeLists.txt changed beyond install exclusions:" >&2; echo "$cmake_change" >&2; exit 1; }
commit="$(git rev-parse --short=11 HEAD)"

[ ! -e "$out" ] || [ -z "$(ls -A "$out")" ] || { echo "$out must be new or empty" >&2; exit 1; }
mkdir -p "$out/stage" "$out/tree/base" "$out/tree/head"
# The install rules for scripts/ and release/extensions/ (source/creator/CMakeLists.txt), as removals.
installed_tree() {  # REV DIR: what the build installs from REV's scripts/ and release/extensions/.
  mkdir -p "$2/scripts" "$2/extensions"
  git archive --format=tar "$1:scripts" | tar -x -C "$2/scripts"
  git archive --format=tar "$1:release/extensions" | tar -x -C "$2/extensions"
  find "$2" \( -name .git -o -name .gitignore -o -name .gitea -o -name .github -o -name .arcconfig \
    -o -name __pycache__ -o -name '*.pyc' -o -name '*.pyo' \) -prune -exec rm -rf {} +
  rm -rf "$2/scripts/site" "$2/scripts/addons_core/bl_pkg/Makefile" "$2/scripts/addons_core/bl_pkg/tests" \
    "$2/scripts/addons_core/bl_pkg/example_extension"
}
installed_tree "$base_tag" "$out/tree/base"
installed_tree HEAD "$out/tree/head"
# Git LFS files (startup.blend templates) come out of git archive as pointers in a checkout that skips
# LFS. Unchanged ones keep the base app's real copy; a changed or new one needs the real content.
lfs_pointers() { (cd "$1" && find . -type f -size -1k -exec grep -l -m1 '^version https://git-lfs.github.com/spec/v1' {} + || true); }
for f in $(lfs_pointers "$out/tree/head"); do
  cmp -s "$out/tree/base/$f" "$out/tree/head/$f" || { echo "LFS file $f changed since $base_tag: build with release_mac.sh" >&2; exit 1; }
done
for f in $(lfs_pointers "$out/tree/base"); do rm -f "$out/tree/base/$f" "$out/tree/head/$f"; done
mismatch="$( (diff -rq -x __pycache__ "$out/tree/base/scripts" "$base_app/$res/scripts";
              diff -rq -x __pycache__ "$out/tree/base/extensions" "$base_app/$res/extensions") 2>&1 |
  grep -v "^Only in $base_app/" || true)"
[ -z "$mismatch" ] || { echo "the base app is not $base_tag's tree under the install rules:" >&2; echo "$mismatch" | head -20 >&2; exit 1; }
app="$out/stage/Loopcut.app"
ditto "$base_app" "$app"
for part in scripts extensions; do
  # Deleted since the base tag, then added or changed; build products the tree never had stay.
  (cd "$out/tree/base/$part" && find . -type f) | while IFS= read -r f; do
    [ -e "$out/tree/head/$part/$f" ] || rm -f "$app/$res/$part/$f"
  done
  rsync -a --checksum "$out/tree/head/$part/" "$app/$res/$part/"
done
rm -rf "$out/tree"
strip_bytecode() {
  find "$app/$res/scripts" "$app/$res/extensions" -name __pycache__ -type d -prune -exec rm -rf {} +
  find "$app/$res/scripts" "$app/$res/extensions" \( -name '*.pyc' -o -name '*.pyo' \) -type f -delete
}
strip_bytecode

# The same check release_mac.sh runs, plus the version and the bundled extensions.
smoke="$out/smoke.log"
"$app/Contents/MacOS/Loopcut" -b --factory-startup --python-exit-code 1 --python-expr "
import bpy, loopcut
from loopcut import bundled_addons
assert hasattr(bpy.types, 'SpaceLoopcut')
assert 'v%d.%d.%d' % tuple(loopcut.bl_info['version']) == '$version', loopcut.bl_info['version']
found = [p['id'] for p in bundled_addons.packages() if p['present']]
assert len(found) == 5, found
print('Loopcut build OK', bpy.app.version_string, 'bundled:', ' '.join(found))
" > "$smoke" 2>&1 && grep -q "Loopcut build OK" "$smoke" || { tail -20 "$smoke" >&2; echo "smoke check failed" >&2; exit 1; }
grep "Loopcut build OK" "$smoke"
strip_bytecode  # The smoke check compiles what it imported.
left="$(find "$app/$res/scripts" "$app/$res/extensions" \( -name __pycache__ -o -name '*.pyc' -o -name '*.pyo' \) | head -1)"
[ -z "$left" ] || { echo "bytecode left in the app: $left" >&2; exit 1; }
# Ad-hoc signature over the whole bundle (no identity attached), after the last Blender run: Blender
# writes bytecode into the app, which would break the seal. The base app carries only the linker's
# signature on its binaries, which a downloaded (quarantined) copy fails as "damaged"; signed ad hoc,
# Gatekeeper offers Open Anyway instead. A Developer ID signature plus notarization would replace this.
codesign --force --deep --sign - "$app"
codesign --verify --deep --strict "$app"

ln -s /Applications "$out/stage/Applications"
dmg="$out/loopcut-$version-arm64.dmg"
hdiutil create -quiet -volname "Loopcut $version" -srcfolder "$out/stage" -fs HFS+ -format UDZO -ov "$dmg"
rm -rf "$out/stage"
printf '%s\n' "repackaged $version from $base_tag's app and $commit" > "$out/SOURCE.txt"
[ -z "$without" ] || printf '%s\n' "without native changes:$without" >> "$out/SOURCE.txt"
"$repo/loopcut/scripts/publish_release.sh" "$version" --dry-run "$dmg" > "$out/update.json"
echo "Built $dmg"
cat "$out/update.json"
