#!/bin/sh
# Run by .github/workflows/release.yml: when RELEASE_TAG names a tag that exists, the commit being built
# must be the tagged one, so the installers match the release's source. That lets a release run be
# dispatched from main (`--ref main`, which saves compiler cache entries every later run can read)
# as well as from the tag itself. A tag that does not exist yet is not checked here.
set -eu
[ -n "${RELEASE_TAG:-}" ] || exit 0
repo="$(cd "$(dirname "$0")/../.." && pwd)"
refs="$(git -C "$repo" ls-remote origin "refs/tags/$RELEASE_TAG" "refs/tags/$RELEASE_TAG^{}")"
tagged="$(printf '%s\n' "$refs" | awk -v t="refs/tags/$RELEASE_TAG" '$2 == t "^{}" {peeled = $1} $2 == t {plain = $1} END {print (peeled ? peeled : plain)}')"
head="$(git -C "$repo" rev-parse HEAD)"
if [ -n "$tagged" ] && [ "$tagged" != "$head" ]; then
  echo "::error::$RELEASE_TAG is $tagged but this run builds $head; dispatch on the tag, or on main once main is at it"
  exit 1
fi
[ -z "$tagged" ] || echo "Building $RELEASE_TAG's commit $head"
