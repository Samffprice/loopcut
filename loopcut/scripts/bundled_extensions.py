"""Verify shipped extension sources offline, or fetch the exact archives pinned in bundled.json.

    python3 loopcut/scripts/bundled_extensions.py verify
    python3 loopcut/scripts/bundled_extensions.py fetch looptools
    python3 loopcut/scripts/bundled_extensions.py fetch --all --write

Fetching compares by default. --write replaces only the selected package after both archive
and extracted tree hashes pass. Updating a version starts with reviewing its manifest pin and
licences; this script never selects a newer version or rewrites the pins.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

EXTENSIONS = Path(__file__).resolve().parents[2] / "release" / "extensions"


def manifest(root: Path) -> list[dict]:
    data = json.loads((root / "bundled.json").read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or data.get("schema") != "loopcut.bundled-extensions/1"
            or not isinstance(data.get("packages"), list)):
        raise ValueError("Invalid bundled extensions manifest")
    seen = set()
    for p in data["packages"]:
        if (not isinstance(p, dict) or not isinstance(p.get("id"), str)
                or not re.fullmatch(r"[a-z][a-z0-9_]*", p["id"]) or p["id"] in seen
                or not isinstance(p.get("files"), int) or p["files"] < 1
                or any(not isinstance(p.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", p[key])
                       for key in ("archive_sha256", "tree_sha256"))):
            raise ValueError("Invalid bundled extension pin")
        seen.add(p["id"])
    return data["packages"]


def tree_hash(directory: Path) -> tuple[int, str]:
    """Hash sorted '<relative posix path>\\0<file sha256>\\n' lines; bytecode caches are not source."""
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError(f"Missing package directory or symlink: {directory}")
    lines = []
    for current, dirs, files in os.walk(directory):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in [*dirs, *files]:
            path = Path(current) / name
            if path.is_symlink():
                raise ValueError(f"Symlink in package: {path}")
        for name in files:
            path = Path(current) / name
            if not path.is_file():
                raise ValueError(f"Not a regular file: {path}")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            lines.append(f"{path.relative_to(directory).as_posix()}\0{digest}\n")
    return len(lines), hashlib.sha256("".join(sorted(lines)).encode("utf-8")).hexdigest()


def check_tree(directory: Path, package: dict) -> None:
    count, digest = tree_hash(directory)
    if count != package["files"] or digest != package["tree_sha256"]:
        raise ValueError(f"{package['id']}: tree mismatch: files {count}/{package['files']}, "
                         f"sha256 {digest}/{package['tree_sha256']}")


def verify(root: Path = EXTENSIONS) -> bool:
    packages = manifest(root)
    expected = {p["id"] for p in packages}
    system = root / "system"
    actual = {p.name for p in system.iterdir() if p.is_dir() and p.name != "__pycache__"} if system.is_dir() else set()
    ok = True
    for package_id in sorted(actual - expected):
        print(f"VERIFY {package_id}: FAIL extra package")
        ok = False
    for p in packages:
        try:
            check_tree(system / p["id"], p)
        except (ValueError, OSError) as ex:
            print(f"VERIFY {p['id']}: FAIL {ex}")
            ok = False
        else:
            print(f"VERIFY {p['id']}: ok ({p['files']} files)")
    return ok


def extract(archive: Path, directory: Path) -> None:
    """Extract only regular files/directories; never follow archive paths outside the staging dir."""
    with zipfile.ZipFile(archive) as bundle:
        seen = set()
        for entry in bundle.infolist():
            name = entry.filename
            path = PurePosixPath(name)
            mode = entry.external_attr >> 16
            if (not name or "\\" in name or ":" in name or path.is_absolute()
                    or any(part in ("", ".", "..") for part in name.rstrip("/").split("/"))
                    or path in seen or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                raise ValueError(f"Unsafe or duplicate archive entry: {name!r}")
            seen.add(path)
            target = directory.joinpath(*path.parts)
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(entry) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)


def fetch(package: dict, root: Path = EXTENSIONS, *, write: bool = False) -> None:
    url = package.get("archive_url", "")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "extensions.blender.org":
        raise ValueError("Archive URL must use HTTPS on extensions.blender.org")
    # Stage on the destination filesystem so a verified replacement uses directory renames.
    with tempfile.TemporaryDirectory(prefix=".bundled-fetch-", dir=root) as temporary:
        stage = Path(temporary)
        archive, extracted = stage / "archive.zip", stage / "package"
        request = urllib.request.Request(url, headers={"User-Agent": "Blender/5.2.2 (Loopcut bundled extensions)"})
        with urllib.request.urlopen(request, timeout=120) as response, archive.open("wb") as output:
            if urllib.parse.urlparse(response.geturl()).scheme != "https":
                raise ValueError("Archive redirected away from HTTPS")
            shutil.copyfileobj(response, output)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != package["archive_sha256"]:
            raise ValueError(f"{package['id']}: archive SHA-256 mismatch; package left unchanged")
        extracted.mkdir()
        extract(archive, extracted)
        check_tree(extracted, package)
        destination = root / "system" / package["id"]
        if not write:
            check_tree(destination, package)
            print(f"FETCH {package['id']}: ok, pinned archive matches disk")
            return
        if destination.is_symlink():
            raise ValueError("Refusing to replace a symlink package directory")
        destination.parent.mkdir(parents=True, exist_ok=True)
        backup = stage / "previous"
        if destination.exists():
            destination.rename(backup)
        try:
            extracted.rename(destination)
        except OSError:
            if backup.exists():
                backup.rename(destination)
            raise
        print(f"FETCH {package['id']}: ok, replaced with verified pinned sources")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("verify", help="offline tree hashes, counts, and package inventory")
    download = sub.add_parser("fetch", help="download a pinned archive and compare with disk")
    download.add_argument("id", nargs="?")
    download.add_argument("--all", action="store_true")
    download.add_argument("--write", action="store_true", help="replace selected package(s) after verification")
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            return 0 if verify() else 1
        if bool(args.id) == bool(args.all):
            parser.error("fetch needs exactly one package id or --all")
        packages = manifest(EXTENSIONS)
        selected = [p for p in packages if args.all or p["id"] == args.id]
        if not selected:
            parser.error(f"Unknown package {args.id!r}")
        for p in selected:
            fetch(p, write=args.write)
        return 0
    except (OSError, ValueError, zipfile.BadZipFile) as ex:
        print(f"FAIL {ex}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
