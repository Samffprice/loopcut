#!/bin/sh
# Finish a release: write update.json from the installers on its GitHub release, add it, and
# publish the release (until then it is a draft nobody sees).
#   loopcut/scripts/publish_release.sh v0.1.3            publish
#   loopcut/scripts/publish_release.sh v0.1.3 --draft    only add update.json; the release stays a draft
# Run once release_mac.sh --upload and the Windows workflow have both added their installers.
# update.json is what the app's updater reads (scripts/addons_core/loopcut/update.py): the
# version, and for each platform the installer's name, URL, size and the SHA-256 GitHub computed
# for the uploaded file. Installs check the download against it.
set -eu
repo="$(cd "$(dirname "$0")/../.." && pwd)"
github="Samffprice/loopcut"
version="${1:?usage: loopcut/scripts/publish_release.sh v0.1.3 [--draft]}"
printf '%s' "$version" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$' || { echo "version must look like v0.1.3" >&2; exit 1; }
addon="$("$repo/loopcut/scripts/addon_version.sh")"
[ "$version" = "$addon" ] || { echo "the add-on says $addon (bl_info in scripts/addons_core/loopcut/__init__.py); bump it first" >&2; exit 1; }

cd "$repo"  # gh finds the GitHub repository from the checkout it runs in.
# The releases list, not the tag: a draft has no tag yet. One JSON object per line.
release="$(gh api "repos/$github/releases" --paginate -q "map(select(.tag_name == \"$version\"))[0] | select(. != null) | tostring")"
[ -n "$release" ] || { echo "no release $version on GitHub; run release_mac.sh $version --upload first" >&2; exit 1; }
printf '%s\n' "$release" | head -1 | python3 - "$github" "$version" > update.json <<'PY'
import json, sys
github, version = sys.argv[1], sys.argv[2]
release = json.loads(sys.stdin.readline())
installers = {"darwin-arm64": f"loopcut-{version}-arm64.dmg", "windows-x64": f"loopcut-{version}-windows64.msi"}
assets = {}
for platform, name in installers.items():
    asset = next((a for a in release["assets"] if a["name"] == name), None)
    if asset is None:
        sys.exit(f"{name} is not on the release yet (macOS: release_mac.sh --upload; Windows: the Release workflow)")
    digest = str(asset.get("digest") or "")
    if not digest.startswith("sha256:") or len(digest) != 71:
        sys.exit(f"GitHub reports no SHA-256 for {name}; re-upload it")
    assets[platform] = {"name": name, "url": f"https://github.com/{github}/releases/download/{version}/{name}",
                        "sha256": digest[7:], "size": int(asset["size"])}
json.dump({"version": version[1:], "tag": version, "notes_url": f"https://github.com/{github}/releases/tag/{version}",
           "assets": assets}, sys.stdout, indent=2)
sys.stdout.write("\n")
PY
cat update.json
gh release upload "$version" update.json --clobber
rm -f update.json
if [ "${2:-}" = "--draft" ]; then
  echo "update.json added; $version stays a draft"
else
  gh release edit "$version" --draft=false --latest
  echo "published $version: the app's next check offers it"
fi
