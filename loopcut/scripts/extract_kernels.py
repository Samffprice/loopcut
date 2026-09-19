"""Copy Cycles' precompiled GPU kernels out of an official Blender zip:
    extract_kernels.py blender-5.2.2-windows-x64.zip <folder>
Used by .github/workflows/release.yml, and by hand to repair a package built without them.
Only the file names are taken from the archive, never its paths.
"""

import sys
import zipfile
from pathlib import Path, PurePosixPath

KERNEL_FOLDER = ("scripts", "addons_core", "cycles", "lib")


def kernels(archive: zipfile.ZipFile):
    for info in archive.infolist():
        path = PurePosixPath(info.filename)
        if path.suffix == ".zst" and path.parts[-5:-1] == KERNEL_FOLDER:
            yield info, path.name


def main() -> int:
    source, target = sys.argv[1], Path(sys.argv[2])
    target.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(source) as archive:
        for info, name in kernels(archive):
            (target / name).write_bytes(archive.read(info))
            count += 1
    print(f"{count} kernel files from {source}")
    return 0 if count else 1


if __name__ == "__main__":
    sys.exit(main())
