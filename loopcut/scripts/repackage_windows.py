#!/usr/bin/env python3
"""Windows add-on-only releases without a compile: the release workflow's `mode: repackage`.

A full Windows build takes about 1.5 h, though most releases change only Python. This script lets the
workflow take an earlier release's signed .zip (the complete installed tree, programs already signed),
swap in this commit's Python, and package it again with the same CPack configuration
(loopcut/scripts/windows_repackage/CMakeLists.txt). The Mac counterpart is repackage_mac.sh.

    repackage_windows.py check BASE HEAD
        Refuses (exit 1) when anything changed between the commits that the swap cannot carry: native
        code, data files, libraries, the installer. Only Python and files Loopcut does not ship may differ.
    repackage_windows.py apply BASE HEAD ROOT CHANGES
        ROOT is the unpacked base package (the folder holding blender.exe). Proves that ROOT's scripts/
        and extensions/ are BASE's under the install rules, then applies BASE..HEAD to them and writes
        the changed paths (relative to ROOT, "A|M|D<tab>path") to CHANGES.
    repackage_windows.py zipdiff BASE_ZIP NEW_ZIP CHANGES
        The two packages must hold the same files byte for byte (by CRC and size), apart from CHANGES.
    repackage_windows.py split ROOT MSI_JSON OUT
        Moves ROOT's files into OUT/<component>/ by the CPack component the base .msi (dumped by
        msi_tables.ps1) has each in, as the full build stages them.
    repackage_windows.py msi-compare BASE_JSON NEW_JSON CHANGES BASE_ZIP [REPORT]
        The new .msi must equal the base one table by table, apart from a fresh ProductCode and
        PackageCode and the rows of CHANGES: same UpgradeCode, version, install folder, shortcuts,
        registration actions and components. File contents follow the base .zip.

Files come from the commits (git archive), never from the working tree. Git LFS files (Blender's
templates and images) are only pointers in a checkout without LFS: unchanged ones keep the base
package's real copy, a changed one refuses.
"""

import fnmatch
import hashlib
import io
import json
import os
import re
import struct
import subprocess
import sys
import tarfile
import zipfile

# Paths that may differ between the base build and the new commit. Everything the build compiles or
# installs from elsewhere (source/, intern/, extern/, lib/, release/datafiles/, release/license/, the
# icons compiled into blender.exe) is missing on purpose, and so are the installer's inputs
# (packaging.cmake, release/windows/installer_wix/, the WiX images): the new .msi must equal the base
# one table by table, so a change to the installer needs a full build.
ALLOWED = re.compile(
    r"^(scripts/|release/extensions/|loopcut/|\.github/"
    r"|README\.md$|LICENSE$|\.gitignore$|\.gitattributes$"
    # The bytecode clean-up CPack runs (the package comparison catches any other effect), and the
    # Mac/Linux archive script.
    r"|build_files/cmake/loopcut_release_hygiene\.cmake$|build_files/package_spec/build_archive\.py$"
    r"|release/loopcut/make_assets\.py$"
    # Install rules: only the exclusion patterns may change (checked line by line below).
    r"|source/creator/CMakeLists\.txt$)"
)
CMAKE_LINE_OK = re.compile(r'PATTERN "(__pycache__|\*\.pyc|\*\.pyo)" EXCLUDE|^\s*#|^\s*$')

# source/creator/CMakeLists.txt: install(DIRECTORY scripts ...) and install(DIRECTORY release/extensions ...).
# A CMake PATTERN matches the end of a file's or folder's full path; an excluded folder drops its contents.
EXCLUDE = {
    "scripts": [".git", ".gitignore", ".gitea", ".github", ".arcconfig", "__pycache__", "*.pyc", "*.pyo",
                "site", "addons_core/bl_pkg/Makefile", "addons_core/bl_pkg/tests",
                "addons_core/bl_pkg/example_extension"],
    "extensions": ["__pycache__", "*.pyc", "*.pyo"],
}
SOURCE = {"scripts": "scripts", "extensions": "release/extensions"}
# Git LFS on Windows writes pointers with CRLF line ends.
LFS_POINTER = re.compile(rb"version https://git-lfs\.github\.com/spec/v1\r?\n")
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def fail(message, details=()):
    print(message, file=sys.stderr)
    for line in list(details)[:40]:
        print("  " + line, file=sys.stderr)
    sys.exit(1)


def git(*args, binary=False):
    # LFS files as their pointers: a runner's Git would otherwise run the LFS download filter on them.
    env = dict(os.environ, GIT_LFS_SKIP_SMUDGE="1")
    out = subprocess.run(("git",) + args, check=True, stdout=subprocess.PIPE, cwd=REPO, env=env).stdout
    return out if binary else out.decode()


def changed_paths(base, head):
    """Every path that differs; renames listed as both their old and new names."""
    out = git("diff", "--no-renames", "--name-only", "-z", base, head)
    return [p for p in out.split("\0") if p]


def check(base, head):
    blocked = [p for p in changed_paths(base, head) if not ALLOWED.match(p)]
    diff = git("diff", "--no-renames", "-U0", base, head, "--", "source/creator/CMakeLists.txt")
    cmake = [line for line in diff.splitlines()
             if line[:1] in "+-" and line[:3] not in ("+++", "---") and not CMAKE_LINE_OK.search(line[1:])]
    if blocked or cmake:
        fail("Changed since the base build in ways a repackage cannot carry (use mode: full):",
             blocked + ["source/creator/CMakeLists.txt: " + line for line in cmake])
    print(f"Between {base} and {head} only Python and files Loopcut does not ship changed.")


def excluded(rel, patterns):
    parts = rel.split("/")
    for i in range(1, len(parts) + 1):
        prefix = "/".join(parts[:i])
        for pattern in patterns:
            if "/" in pattern:
                if prefix == pattern or prefix.endswith("/" + pattern):
                    return True
            elif fnmatch.fnmatchcase(parts[i - 1], pattern):
                return True
    return False


def installed(rev, part):
    """{path: bytes}: what the build installs from REV's tree for PART (scripts or extensions)."""
    # The whole commit's tree, limited to the folder: its .gitattributes (which keep the bundled extensions'
    # line ends, for one) only apply to paths from the top, as in a checkout.
    prefix = SOURCE[part] + "/"
    data = git("archive", "--format=tar", rev, "--", SOURCE[part], binary=True)
    files = {}
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for member in tar:
            if member.issym() or member.islnk():
                fail(f"{member.name} at {rev} is a link; links are not supported")
            name = member.name[len(prefix):] if member.name.startswith(prefix) else None
            if member.isfile() and name and not excluded(name, EXCLUDE[part]):
                files[name] = tar.extractfile(member).read()
    return files


def is_pointer(data):
    return len(data) < 1024 and LFS_POINTER.match(data) is not None


def version_dir(root):
    found = [d for d in os.listdir(root)
             if re.fullmatch(r"\d+\.\d+", d) and os.path.isdir(os.path.join(root, d, "scripts"))]
    if len(found) != 1:
        fail(f"{root} is not an unpacked Loopcut package (version folders with scripts/: {found})")
    return found[0]


def read(path):
    with open(path, "rb") as f:
        return f.read()


def apply(base, head, root, changes_path):
    ver = version_dir(root)
    changes = []
    for part in ("scripts", "extensions"):
        old, new = installed(base, part), installed(head, part)
        target = os.path.join(root, ver, part)
        # The base package must be BASE's tree under the install rules; build products (Cycles' add-on
        # and GPU kernels, numpy) may only be extra.
        mismatch = []
        for rel, data in sorted(old.items()):
            path = os.path.join(target, rel)
            if not os.path.isfile(path):
                mismatch.append(f"missing: {ver}/{part}/{rel}")
            elif is_pointer(data):
                if is_pointer(read(path)):
                    mismatch.append(f"an LFS pointer, not the file: {ver}/{part}/{rel}")
            elif read(path) != data:
                mismatch.append(f"differs: {ver}/{part}/{rel} ({os.path.getsize(path)} bytes, git {len(data)}: "
                                f"{data[:40]!r})")
        if mismatch:
            fail(f"The base package is not {base}'s {SOURCE[part]}/ under the install rules:", mismatch)
        for rel in sorted(set(old) - set(new)):
            os.remove(os.path.join(target, rel))
            changes.append(("D", f"{ver}/{part}/{rel}"))
        for rel, data in sorted(new.items()):
            if old.get(rel) == data:
                continue
            if is_pointer(data):
                fail(f"Git LFS file {SOURCE[part]}/{rel} changed since the base build; use mode: full")
            path = os.path.join(target, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)
            changes.append(("A" if rel not in old else "M", f"{ver}/{part}/{rel}"))
        # Folders emptied by deletions go too, as a build from HEAD would not create them.
        dirs = {os.path.dirname(rel) for rel in new}
        kept = {"/".join(d.split("/")[:i]) for d in dirs for i in range(1, d.count("/") + 2)}
        for _, rel in [c for c in changes if c[0] == "D"]:
            folder = os.path.dirname(rel.split("/", 2)[2])
            while folder and folder not in kept:
                path = os.path.join(target, folder)
                if os.path.isdir(path) and not os.listdir(path):
                    os.rmdir(path)
                    changes.append(("D", f"{ver}/{part}/{folder}/"))
                folder = os.path.dirname(folder)
    with open(changes_path, "w", encoding="utf-8", newline="\n") as f:
        f.writelines(f"{kind}\t{rel}\n" for kind, rel in changes)
    print(f"Applied {len(changes)} changes ({base} -> {head}):")
    for kind, rel in changes[:60]:
        print(f"  {kind} {rel}")
    if len(changes) > 60:
        print(f"  ... and {len(changes) - 60} more")


def load_changes(path):
    with open(path, encoding="utf-8") as f:
        return {line.split("\t", 1)[1].rstrip("\n").rstrip("/") for line in f if "\t" in line}


def covered(rel, changes):
    """A changed path, or a folder holding one (new folders appear, emptied ones go)."""
    rel = rel.rstrip("/")
    return rel in changes or any(c.startswith(rel + "/") for c in changes)


def zip_entries(path):
    with zipfile.ZipFile(path) as z:
        names = z.infolist()
        tops = {i.filename.split("/", 1)[0] for i in names}
        if len(tops) != 1:
            fail(f"{path}: expected one top folder, found {sorted(tops)[:5]}")
        return {i.filename.split("/", 1)[1]: (i.CRC, i.file_size)
                for i in names if "/" in i.filename and i.filename.split("/", 1)[1]}


def zipdiff(base_zip, new_zip, changes_path):
    changes = load_changes(changes_path)
    old, new = zip_entries(base_zip), zip_entries(new_zip)
    unexpected, expected = [], []
    for rel in sorted(set(old) | set(new)):
        if old.get(rel) == new.get(rel):
            continue
        state = "only in base" if rel not in new else "only in new" if rel not in old else "differs"
        (expected if covered(rel, changes) else unexpected).append(f"{state}: {rel}")
    print(f"zip: {len(old)} entries in the base, {len(new)} in the new package; "
          f"{len(expected)} differ as the changes say, {len(unexpected)} otherwise")
    for line in expected[:60]:
        print("  " + line)
    if unexpected:
        fail("The packages differ outside the changes:", unexpected)


def load_msi(path):
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def table_rows(msi, name):
    table = msi["tables"].get(name)
    if not table:
        return []
    return [dict(zip(table["columns"], row)) for row in table["rows"]]


def long_name(default_dir):
    target = default_dir.split(":", 1)[0]
    return target.split("|", 1)[-1]


def file_paths(msi):
    """{File key: path relative to INSTALL_ROOT}, and {Directory key: path} for the folders under it."""
    dirs = {r["Directory"]: r for r in table_rows(msi, "Directory")}
    cache = {}

    def path(key):
        if key not in cache:
            row = dirs[key]
            name = long_name(row["DefaultDir"])
            parent = row["Directory_Parent"]
            if key == "INSTALL_ROOT":
                cache[key] = ""
            elif not parent or parent == key or path(parent) is None:
                cache[key] = None  # Outside the install folder (TARGETDIR, ProgramFiles64Folder, menus).
            elif name == ".":
                cache[key] = path(parent)
            else:
                cache[key] = f"{path(parent)}/{name}" if path(parent) else name
        return cache[key]

    components = {r["Component"]: r for r in table_rows(msi, "Component")}
    files = {}
    for r in table_rows(msi, "File"):
        folder = path(components[r["Component_"]]["Directory_"])
        files[r["File"]] = (f"{folder}/" if folder else "") + long_name(r["FileName"])
    folders = {key: path(key) for key in dirs}
    return files, {k: v for k, v in folders.items() if v}


def split(root, msi_json, out):
    """Moves ROOT's files into OUT/<CPack component>/, each into the component the base .msi has it in.

    CPack stages every component in a folder of its own and names the .msi's rows after it (the WiX
    template refers to CM_FP_Blender.blender_launcher.exe), so the components must be the full build's:
    "Blender" for the programs, "Libraries" for the C runtime, "Unspecified" for the rest. Files the
    base .msi does not have (Python added since) belong to "Unspecified", as all of scripts/ does.
    """
    msi = load_msi(msi_json)
    paths, folders = file_paths(msi)
    feature_of = {r["Component_"]: r["Feature_"] for r in table_rows(msi, "FeatureComponents")}

    def component(key):
        feature = feature_of.get(key, "")
        if not feature.startswith("CM_C_"):
            fail(f"Component {key} of the base .msi is not in a CPack component's feature ({feature!r})")
        return feature[len("CM_C_"):]

    os.makedirs(out, exist_ok=True)
    owner = {paths[r["File"]]: component(r["Component_"]) for r in table_rows(msi, "File")}
    empty_owner = {folders[r["Directory_"]]: component(r["Component_"])
                   for r in table_rows(msi, "CreateFolder") if r["Directory_"] in folders}
    counts = {}
    for current, dirnames, filenames in os.walk(root, topdown=False):
        rel_dir = os.path.relpath(current, root).replace(os.sep, "/")
        rel_dir = "" if rel_dir == "." else rel_dir
        for name in filenames:
            rel = f"{rel_dir}/{name}" if rel_dir else name
            comp = owner.get(rel, "Unspecified")
            target = os.path.join(out, comp, *rel.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            os.replace(os.path.join(current, name), target)
            counts[comp] = counts.get(comp, 0) + 1
        if rel_dir and not os.listdir(current):
            if not any(os.path.isdir(os.path.join(out, c, *rel_dir.split("/"))) for c in os.listdir(out)):
                os.makedirs(os.path.join(out, empty_owner.get(rel_dir, "Unspecified"), *rel_dir.split("/")))
            os.rmdir(current)
    print("Files per CPack component: " + ", ".join(f"{c} {n}" for c, n in sorted(counts.items())))


# Columns that change whenever files are added or their content changes (compared separately).
IGNORED_COLUMNS = {"File": {"FileSize", "Version", "Language", "Sequence"}, "Media": {"LastSequence"}}
# Summary information: 9 is the PackageCode (must be new), 12 and 13 times, 18 the WiX version that built it.
VOLATILE_SUMMARY = {"9", "12", "13", "18"}


def zip_file_reader(path):
    """Reads a file of a release .zip by its path under the top folder."""
    z = zipfile.ZipFile(path)
    top = z.infolist()[0].filename.split("/", 1)[0]
    return lambda rel: z.read(f"{top}/{rel}") if f"{top}/{rel}" in z.NameToInfo else None


def msi_hash(data):
    """The MsiFileHash row Windows Installer computes for a file: its MD5 as four signed 32-bit parts."""
    return list(struct.unpack("<4i", hashlib.md5(data).digest()))


def msi_compare(base_json, new_json, changes_path, base_zip, report_path=None):
    changes = load_changes(changes_path)
    base, new = load_msi(base_json), load_msi(new_json)
    lines, unexpected = [], []

    def note(ok, text):
        (lines if ok else unexpected).append(text)

    for key in sorted(set(base["summary"]) | set(new["summary"])):
        a, b = base["summary"].get(key), new["summary"].get(key)
        if key == "9":
            note(bool(a and b and a != b), f"PackageCode: {a} -> {b}")
        elif key in VOLATILE_SUMMARY:
            if a != b:
                lines.append(f"summary {key}: {a!r} -> {b!r}")
        elif a != b:
            note(False, f"summary {key}: {a!r} -> {b!r}")

    props = ({r["Property"]: r["Value"] for r in table_rows(base, "Property")},
             {r["Property"]: r["Value"] for r in table_rows(new, "Property")})
    for key in sorted(set(props[0]) | set(props[1])):
        a, b = props[0].get(key), props[1].get(key)
        if key == "ProductCode":
            note(bool(a and b and a != b), f"ProductCode: {a} -> {b}")
        elif a != b:
            note(False, f"Property {key}: {a!r} -> {b!r}")
    for key in ("UpgradeCode", "ProductVersion", "ProductName", "Manufacturer"):
        lines.append(f"{key}: {props[1].get(key)} (same as the base)" if props[0].get(key) == props[1].get(key)
                     else f"{key}: differs")

    base_files, base_dirs = file_paths(base)
    new_files, new_dirs = file_paths(new)

    # The new .msi is packaged from the base release's .zip. Where the base release's own .msi and .zip hold
    # different bytes for a file (copies of one file signed apart: python3.dll twice, setuptools' launchers),
    # the new .msi must carry the .zip's: its size and hash are checked against the .zip itself.
    read_zip = zip_file_reader(base_zip)
    hashes = ({r["File_"]: [r["HashPart1"], r["HashPart2"], r["HashPart3"], r["HashPart4"]]
               for r in table_rows(base, "MsiFileHash")},
              {r["File_"]: [r["HashPart1"], r["HashPart2"], r["HashPart3"], r["HashPart4"]]
               for r in table_rows(new, "MsiFileHash")})
    sizes = ({r["File"]: r["FileSize"] for r in table_rows(base, "File")},
             {r["File"]: r["FileSize"] for r in table_rows(new, "File")})
    from_zip = set()
    for key, rel in new_files.items():
        if base_files.get(key) != rel or covered(rel, changes):
            continue
        if sizes[0].get(key) == sizes[1][key] and hashes[0].get(key) == hashes[1].get(key):
            continue
        data = read_zip(rel)
        if (data is not None and len(data) == sizes[1][key]
                and (key not in hashes[1] or hashes[1][key] == msi_hash(data))):
            from_zip.add(rel)
            lines.append(f"{rel}: the base release's .msi and .zip hold different copies "
                         f"({sizes[0].get(key)} and {len(data)} bytes); the new .msi has the .zip's")
    changes = changes | from_zip
    # Rows tied to a changed file or folder may differ; ids derive from paths, so the rest must not.
    path_of = {**{f"file:{k}": v for k, v in base_files.items()}, **{f"file:{k}": v for k, v in new_files.items()},
               **{f"dir:{k}": v for k, v in base_dirs.items()}, **{f"dir:{k}": v for k, v in new_dirs.items()}}
    comp_paths = {}
    for msi, files in ((base, base_files), (new, new_files)):
        comps = {r["Component"]: r for r in table_rows(msi, "Component")}
        for r in table_rows(msi, "File"):
            comp_paths.setdefault(r["Component_"], set()).add(files[r["File"]])
        for key, r in comps.items():
            folder = path_of.get(f"dir:{r['Directory_']}")
            if key not in comp_paths and folder:
                comp_paths.setdefault(key, set()).add(folder)  # A component with no file: a folder of its own.

    def row_paths(table, row):
        found = set()
        for column, value in row.items():
            if value is None:
                continue
            if column in ("File", "File_") and f"file:{value}" in path_of:
                found.add(path_of[f"file:{value}"])
            if column in ("Component", "Component_") and value in comp_paths:
                found |= comp_paths[value]
            if column in ("Directory", "Directory_") and f"dir:{value}" in path_of:
                found.add(path_of[f"dir:{value}"])
        return found

    tables = sorted(set(base["tables"]) | set(new["tables"]))
    for name in tables:
        if name == "Property":
            continue
        a_rows, b_rows = table_rows(base, name), table_rows(new, name)
        drop = IGNORED_COLUMNS.get(name, set())

        def key_of(row):
            return json.dumps({k: v for k, v in row.items() if k not in drop}, sort_keys=True)

        a_keys = {key_of(r): r for r in a_rows}
        b_keys = {key_of(r): r for r in b_rows}
        changed_ok = changed_bad = 0  # Rows that differ for a changed file, and the others.
        for side, keys, other in (("base", a_keys, b_keys), ("new", b_keys, a_keys)):
            for k, row in keys.items():
                if k in other:
                    continue
                paths = row_paths(name, row)
                if paths and all(covered(p, changes) for p in paths):
                    changed_ok += 1
                else:
                    changed_bad += 1
                    unexpected.append(f"{name}: only in {side}: {k[:300]}")
        if name == "File":  # Same rows; check content columns of files nobody changed.
            a_by_id = {r["File"]: r for r in a_rows}
            for r in b_rows:
                old = a_by_id.get(r["File"])
                if old is None or covered(new_files[r["File"]], changes):
                    continue
                for column in ("FileSize", "Version", "Language"):
                    if old.get(column) != r.get(column):
                        unexpected.append(f"File {new_files[r['File']]}: {column} {old.get(column)} -> {r.get(column)}")
        lines.append(f"{name}: {len(a_rows)} rows in the base, {len(b_rows)} new; "
                     + (f"{changed_bad} rows differ unexpectedly" if changed_bad else
                        f"{changed_ok} rows differ for changed files" if changed_ok else "identical"))
    lines.append(f"files: {len(base_files)} in the base, {len(new_files)} new; "
                 f"{len(set(base_files.values()) ^ set(new_files.values()))} added or removed")
    text = "\n".join(lines + ([""] + ["UNEXPECTED: " + u for u in unexpected] if unexpected else []))
    print(text if len(text) < 20000 else text[:20000] + "\n...")
    if report_path:
        with open(report_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text + "\n")
    if unexpected:
        fail(f"The new .msi differs from the base outside the changes ({len(unexpected)} rows).")
    print("The new .msi matches the base apart from its product and package codes and the changed files.")


def main(argv):
    commands = {"check": (check, 2), "apply": (apply, 4), "zipdiff": (zipdiff, 3),
                "split": (split, 3), "msi-compare": (msi_compare, (4, 5))}
    if len(argv) < 2 or argv[1] not in commands:
        fail(__doc__)
    func, count = commands[argv[1]]
    args = argv[2:]
    if len(args) not in (count if isinstance(count, tuple) else (count,)):
        fail(__doc__)
    func(*args)


if __name__ == "__main__":
    main(sys.argv)
