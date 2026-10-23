#!/bin/sh
# Finish a release: write update.json from the installers on its GitHub release, add it, and
# publish the release (until then it is a draft nobody sees).
#   loopcut/scripts/publish_release.sh v0.1.3            publish
#   loopcut/scripts/publish_release.sh v0.1.3 --draft    only add update.json; the release stays a draft
#   loopcut/scripts/publish_release.sh v0.1.3 --partial  publish with the installers present so far
#   loopcut/scripts/publish_release.sh v0.1.3 --dry-run FILE...  print the update.json these local
#                                                           installers would get; no GitHub call at all
# Optional metadata (before installer files): --min-version 0.2.10 --headline "Fixes the Windows helper crash"
# Flags can be combined, e.g. v0.2.11 --draft --partial --min-version 0.2.10.
# Run once release_mac.sh --upload and the Windows workflow have both added their installers, or
# with --partial to publish one platform first: the app on the other platform then shows the
# release notes without an installer until this runs again with the missing one uploaded.
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

shift
draft=0; partial=0; dry_run=0; minimum=""; headline=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --draft) draft=1; shift ;;
    --partial) partial=1; shift ;;
    --min-version|--headline)
      [ "$#" -ge 2 ] || { echo "$1 needs a value" >&2; exit 1; }
      if [ "$1" = "--min-version" ]; then minimum="$2"; else headline="$2"; fi
      shift 2 ;;
    --dry-run) dry_run=1; shift ;;
    --) shift; break ;;
    --*) echo "unknown option: $1" >&2; exit 1 ;;
    *) [ "$dry_run" -eq 1 ] || { echo "unexpected argument: $1" >&2; exit 1; }; break ;;
  esac
done
[ "$dry_run" -eq 1 ] || [ "$#" -eq 0 ] || { echo "installer files need --dry-run" >&2; exit 1; }
metadata="$(python3 - "$version" "$minimum" "$headline" <<'PYMETA'
import json, re, sys
version, minimum, headline = sys.argv[1:]
extra = {}
if minimum:
    minimum = minimum.removeprefix("v")
    if len(minimum) > 20 or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", minimum):
        sys.exit("--min-version must look like 0.2.9")
    if tuple(map(int, minimum.split("."))) > tuple(map(int, version[1:].split("."))):
        sys.exit("--min-version cannot be newer than the release")
    extra["min_version"] = minimum
if len(headline) > 120 or any(ord(c) < 32 for c in headline):
    sys.exit("--headline must be one short line (at most 120 characters)")
if headline:
    extra["headline"] = headline
print(json.dumps(extra))
PYMETA
)"

if [ "$dry_run" -eq 1 ]; then
  # The same manifest from local files: the SHA-256 and size GitHub would report for them once uploaded.
  [ "$#" -gt 0 ] || { echo "--dry-run needs the installer files" >&2; exit 1; }
  exec python3 - "$github" "$version" "$metadata" "$@" <<'PY'
import hashlib, json, os, sys
github, version, metadata, files = sys.argv[1], sys.argv[2], json.loads(sys.argv[3]), sys.argv[4:]
installers = {f"loopcut-{version}-arm64.dmg": "darwin-arm64", f"loopcut-{version}-windows64.msi": "windows-x64"}
assets = {}
for path in files:
    name = os.path.basename(path)
    if name not in installers:
        sys.exit(f"{name} is not an installer name for {version} ({', '.join(installers)})")
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    assets[installers[name]] = {"name": name, "url": f"https://github.com/{github}/releases/download/{version}/{name}",
                                "sha256": digest.hexdigest(), "size": os.path.getsize(path)}
json.dump({"version": version[1:], "tag": version, "notes_url": f"https://github.com/{github}/releases/tag/{version}",
           "assets": assets, **metadata}, sys.stdout, indent=2)
sys.stdout.write("\n")
PY
fi

cd "$repo"  # gh finds the GitHub repository from the checkout it runs in.
# The releases list, not the tag: a draft has no tag yet. One JSON object per line.
release="$(gh api "repos/$github/releases" --paginate -q "map(select(.tag_name == \"$version\"))[0] | select(. != null) | tostring")"
[ -n "$release" ] || { echo "no release $version on GitHub; run release_mac.sh $version --upload first" >&2; exit 1; }
# The release JSON travels in the environment: stdin carries the Python below, so a pipe into it
# would be shadowed by the heredoc.
RELEASE_JSON="$(printf '%s\n' "$release" | head -1)" python3 - "$github" "$version" "$partial" "$metadata" > update.json <<'PY'
import json, os, sys
github, version, partial = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
metadata = json.loads(sys.argv[4])
release = json.loads(os.environ["RELEASE_JSON"])
installers = {"darwin-arm64": f"loopcut-{version}-arm64.dmg", "windows-x64": f"loopcut-{version}-windows64.msi"}
assets = {}
for platform, name in installers.items():
    asset = next((a for a in release["assets"] if a["name"] == name), None)
    if asset is None:
        if partial:
            print(f"{name} is not on the release yet: {platform} left out of update.json", file=sys.stderr)
            continue
        sys.exit(f"{name} is not on the release yet (macOS: release_mac.sh --upload; Windows: the Release workflow; "
                 "or --partial to publish without it)")
    digest = str(asset.get("digest") or "")
    if not digest.startswith("sha256:") or len(digest) != 71:
        sys.exit(f"GitHub reports no SHA-256 for {name}; re-upload it")
    assets[platform] = {"name": name, "url": f"https://github.com/{github}/releases/download/{version}/{name}",
                        "sha256": digest[7:], "size": int(asset["size"])}
if not assets:
    sys.exit("no installer on the release at all; nothing to publish")
json.dump({"version": version[1:], "tag": version, "notes_url": f"https://github.com/{github}/releases/tag/{version}",
           "assets": assets, **metadata}, sys.stdout, indent=2)
sys.stdout.write("\n")
PY
cat update.json
gh release upload "$version" update.json --clobber --repo "$github"
rm -f update.json
if [ "$draft" -eq 1 ]; then
  echo "update.json added; $version stays a draft"
else
  gh release edit "$version" --draft=false --latest --repo "$github"
  echo "published $version: the app's next check offers it"
fi
