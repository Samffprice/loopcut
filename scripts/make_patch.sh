#!/bin/sh
# Write the fork's changes as patches/blender.patch, for CI to apply on a clean upstream checkout
# (.github/workflows/release.yml). The fork itself is not in this repository; its whole difference
# from upstream is this patch. Run before tagging a release, and commit the result.
set -eu
root="$(cd "$(dirname "$0")/.." && pwd)"
fork="$root/blender"
base="$(git -C "$fork" describe --tags --abbrev=0)"

# A throwaway index, so new files are included without staging anything in the fork.
index="$(mktemp)"
trap 'rm -f "$index" "$index.check"' EXIT
cp "$(git -C "$fork" rev-parse --absolute-git-dir)/index" "$index"
GIT_INDEX_FILE="$index" git -C "$fork" add -A
# lib/ holds the precompiled libraries (submodules CI fetches itself), never part of the patch.
GIT_INDEX_FILE="$index" git -C "$fork" diff --cached --binary "$base" -- . ':(exclude)lib' > "$root/patches/blender.patch"
echo "$base" > "$root/patches/BASE"

# Prove it applies to the untouched base before anyone waits two hours for CI to say so.
GIT_INDEX_FILE="$index.check" git -C "$fork" read-tree "$base"
GIT_INDEX_FILE="$index.check" git -C "$fork" apply --cached --check "$root/patches/blender.patch"
echo "patches/blender.patch: $(grep -c '^diff --git' "$root/patches/blender.patch") files against $base"
