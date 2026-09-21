#!/bin/sh
# Prints the add-on's version from bl_info as a release tag, like v0.1.3. The one place the
# version lives: release_mac.sh, the release workflow and publish_release.sh all check the tag
# they are given against it, and the app compares it with update.json to know when to update.
set -eu
init="$(cd "$(dirname "$0")/../.." && pwd)/scripts/addons_core/loopcut/__init__.py"
version="$(grep -Eo '"version": \([0-9]+, [0-9]+, [0-9]+\)' "$init" | tr -d '(),' | awk '{print "v" $2 "." $3 "." $4}')"
[ -n "$version" ] || { echo "no bl_info version in $init" >&2; exit 1; }
printf '%s\n' "$version"
