#!/bin/sh
# The GPL source archive for a release: the public snapshot's tree from a clean public_release.py dry run.
#   loopcut/scripts/source_archive.sh v0.2.0 /private/tmp/loopcut-public-review [OUT_DIR]
# Arguments: the version, the dry run's --out folder (its report.json must say clean). Output
# (default ../build/rc-<version>): loopcut-<version>-source.tar.gz and its .sha256.
# The tree is the squashed snapshot (no loopcut/track/, no eval harness, no __pycache__ or *.pyc). Git LFS
# pointers are replaced by their objects from this repository's LFS store, except under tests/: Blender's
# test data (about 780 MB) is not needed to build and stays as pointers to projects.blender.org's LFS.
# The archive must have a GPL LICENSE at its root; the script refuses otherwise.
set -eu
repo="$(cd "$(dirname "$0")/../.." && pwd)"
work="$(dirname "$repo")"
version="${1:?usage: loopcut/scripts/source_archive.sh vX.Y.Z DRY_RUN_OUT [OUT_DIR]}"
review="$(cd "${2:?the public_release.py --dry-run --out folder}" && pwd)"
out="${3:-$work/build/rc-$version}"
printf '%s' "$version" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$' || { echo "version must look like v0.2.0" >&2; exit 1; }
[ -d "$review/tree" ] && [ -f "$review/report.json" ] || { echo "$review has no tree/ and report.json" >&2; exit 1; }
python3 - "$review/report.json" <<'PY' || { echo "the dry run is not clean" >&2; exit 1; }
import json, sys
report = json.load(open(sys.argv[1]))
status = str(report.get("status") or report.get("result") or "").lower()
sys.exit(0 if status in ("clean", "reviewed") else 1)
PY
head -2 "$review/tree/LICENSE" 2>/dev/null | grep -q "GNU GENERAL PUBLIC LICENSE" ||
  { echo "the snapshot has no GPL LICENSE at its root" >&2; exit 1; }
left="$(find "$review/tree" \( -name __pycache__ -o -name '*.pyc' -o -name '*.pyo' \) | head -1)"
[ -z "$left" ] || { echo "bytecode in the snapshot: $left" >&2; exit 1; }

lfs="$(cd "$repo" && git rev-parse --git-common-dir)/lfs/objects"
case "$lfs" in /*) ;; *) lfs="$repo/$lfs" ;; esac
stage="$(mktemp -d "${TMPDIR:-/tmp}/loopcut-source.XXXXXX")"
trap 'rm -rf "$stage"' EXIT
name="loopcut-$version"
ditto "$review/tree" "$stage/$name"
resolved=0
for f in $(cd "$stage/$name" && find . -type f -size -1k ! -path './tests/*' -exec grep -l -m1 '^version https://git-lfs.github.com/spec/v1' {} +); do
  oid="$(sed -n 's/^oid sha256:\([0-9a-f]\{64\}\)$/\1/p' "$stage/$name/$f")"
  object="$lfs/$(printf %s "$oid" | cut -c1-2)/$(printf %s "$oid" | cut -c3-4)/$oid"
  [ -n "$oid" ] && [ -f "$object" ] || { echo "no LFS object for $f" >&2; exit 1; }
  cp "$object" "$stage/$name/$f"
  resolved=$((resolved + 1))
done
cat > "$stage/$name/SOURCE.txt" <<EOF2
Loopcut $version: the complete source of the release, as the public snapshot built by
loopcut/scripts/public_release.py (dry run report: report.json beside the archive's maker).
Git LFS files outside tests/ are included ($resolved files). Files under tests/ that are Git LFS pointers are
Blender's test data, not needed to build; they are in Blender's LFS on projects.blender.org.
License: GNU GPL v3 or later (LICENSE, COPYING, doc/license/).
EOF2
mkdir -p "$out"
archive="$out/$name-source.tar.gz"
tar -czf "$archive" -C "$stage" "$name"
(cd "$out" && shasum -a 256 "$(basename "$archive")" > "$(basename "$archive").sha256")
echo "Built $archive ($resolved LFS files resolved)"
cat "$archive.sha256"
