"""Build a reviewable public snapshot without checking out or publishing anything.

Only Git objects and refs/loopcut-release/dry-run are written. A temporary index
keeps every worktree's staged changes intact. Private comparison text is loaded
at runtime; reports contain locations and hashes, never matched text or keys.
Single-commit leak checks use --base; a BASE..TIP argument supplies its own base.
Dry runs also require the platform leak checker on the whole snapshot export,
isolated history repository and shipped add-on ZIP. Both scanners check that ZIP
and any caller-supplied ZIPs. --no-leak-check bypasses the platform check for tests.

Example: python3 loopcut/scripts/public_release.py --dry-run --source HEAD
    --base origin/main --out /private/tmp/loopcut-release-review
This snapshots the source commit, not uncommitted working-tree changes.
The eval harness stays private: private_path blocks any harness/evals path
segment pair in the snapshot. private_import uses ASTs to block public Python
tests under loopcut/tests/ that add evals directories to sys.path or load modules
under harness/evals through importlib. local_path blocks public tests that name
an absolute path only this machine has (the private disk, a private repository
checkout, macOS /private/tmp); CI clones on Linux and has none of them. None of
these categories nor withheld-prompt can be allowlisted. Comments and docstrings
do not count as imports.
When present, prompts/manifest.json declares open prompt files using validated
relative paths. These files are not fingerprint sources. Any private-text
fingerprint found anywhere in an open file's whitespace-normalized text is
dropped, including overlaps from skills; source coverage counts these drops.
Without a manifest, prompt and skill fingerprinting is unchanged.
"""

import argparse
import ast
import base64
import bisect
import fnmatch
import hashlib
import io
import json
import math
import os
import re
import runpy
import shlex
import subprocess
import sys
import tarfile
import tempfile
import time
import tokenize
from types import SimpleNamespace
import zipfile
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
PRIVATE_HOME = Path("/Volumes/BackupFiles/MasterCoding/BlenderCursor")
PRIVATE_WEB = PRIVATE_HOME / "loopcut-web"
LEAK_CHECK_PATHS = (PRIVATE_HOME / "loopcut-web-platform/scripts/leak-check.sh",
                    PRIVATE_WEB / "scripts/leak-check.sh")
# Competitor-research folders beside the repository are named only in a private file: loopcut/track/ never
# enters a snapshot (public_release_exclude.txt), so no public file spells them. A checkout without that file
# (CI, the public repository) has no such folders to protect.
RESEARCH_FILE = REPO / "loopcut/track/research_folders.txt"


def research_folders(path=RESEARCH_FILE):
    """(folder names under PRIVATE_HOME, the comparison captures path or "") from the private file's
    `folder NAME` and `sources RELATIVE/PATH` lines; nothing when the file is absent."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return (), ""
    folders, sources = [], ""
    for raw in text.splitlines():
        key, _, value = raw.split("#", 1)[0].strip().partition(" ")
        value = value.strip()
        if not key:
            continue
        parts = PurePosixPath(value).parts
        if (key not in ("folder", "sources") or not value or value.startswith("/") or ".." in parts or
                "\\" in value or (key == "folder" and len(parts) != 1)):
            raise ValueError(f"{path.name}: expected 'folder NAME' or 'sources RELATIVE/PATH' lines")
        if key == "folder":
            folders.append(value)
        else:
            sources = value
    return tuple(folders), sources


def research_exclusions(folders):
    """Snapshot exclusions for the research folders, at the root and at any depth."""
    return [pattern for name in folders for pattern in (f"{name}/", f"*/{name}/*")]


RESEARCH_FOLDERS, RESEARCH_SOURCES = research_folders()
PRIVATE_REPOS = ("loopcut-web", "loopcut-web-platform", "loopcut-web-client-shared", *RESEARCH_FOLDERS)
# Absolute paths a public test cannot depend on: the private disk, any private repository
# checkout and macOS's /private folders. URLs and relative names do not match.
LOCAL_PATH_REGEX = re.compile(r"(?<![\w.:/-])(?:" + re.escape(PRIVATE_HOME.parents[1].as_posix()) +
                              r"|/private/(?:tmp|var)|(?:/[\w.-]+)*/(?:" +
                              "|".join(map(re.escape, PRIVATE_REPOS)) + r"))(?![\w.-])")
ADDON_PATH = "scripts/addons_core/loopcut"
DRY_RUN_REF = "refs/loopcut-release/dry-run"
BYTECODE_PATTERNS = ("__pycache__", "*.pyc", "*.pyo")
# CMake PATTERNs match path suffixes, including ancestor directories. Release
# configuration enables Freestyle; its conditional exclusion is a dummy path.
BUILD_INPUT_PATTERNS = {
    "scripts": (".git", ".gitignore", ".gitea", ".github", ".arcconfig",
                *BYTECODE_PATTERNS, "site", "${FREESTYLE_EXCLUDE_CONDITIONAL}",
                "addons_core/bl_pkg/Makefile", "addons_core/bl_pkg/tests",
                "addons_core/bl_pkg/example_extension"),
    "release/extensions": BYTECODE_PATTERNS,
    # Upstream rules without exclusions: any bytecode here would ship, so the scan blocks on it.
    **dict.fromkeys(("release/datafiles/fonts", "release/datafiles/colormanagement",
                     "release/datafiles/studiolights", "release/license", "assets"), ()),
    "release/darwin/Blender.app/Contents/Resources": (
        ".git", ".gitignore", ".gitea", ".github", *BYTECODE_PATTERNS,
        "*.orig", "*.rej", "__MACOSX", ".DS_Store", "config-${PYTHON_VERSION}/*.a",
        "lib2to3", "tkinter", "lib-dynload/_tkinter.*", "idlelib", "test", "turtledemo",
        "turtle.py", "wininst*.exe"),
}
BUILD_INPUT_FILES = ("scripts/site/sitecustomize.py",)
OPTIONAL_BUILD_INPUTS = ("assets",)
RESEARCH_KINDS = ("system_prompts", "skills", "skill_listings", "tools",
                  "context_messages", "tool_results", "backend_scripts")
WITHHELD = {"orchestrator.py": "PROMPT", "workers.py": "PROMPT",
            "ownership.py": "MATERIALS_RULE", "router.py": "PROMPT"}
WITHHELD_DETAIL = ("withheld prompt still in tree (server-held-prompts plan "
                   "section 1, not yet moved)")
POLICY = ("Fingerprints: first line and every fifth physical line, whitespace normalized, "
          "first 40 characters; imports, punctuation-only lines, short JSON fields and "
          "lines shorter than 40 characters are skipped. Prompt/skill fingerprints require "
          "prose with at least 8 words, mostly alphabetic characters and no leading code; "
          "Markdown fences are excluded and Python/JavaScript sources contribute only "
          "comments and strings. prompts/manifest.json validates open source paths as "
          "canonical relative paths without '..' and verifies declared prompt parts. "
          "Open files and the manifest are not fingerprint sources; private-text candidates "
          "from prompts and skills found anywhere in any open file's whitespace-normalized "
          "text are dropped. Source coverage counts candidates dropped as open per root, "
          "before any deduplication; skipped open files contribute no candidates. "
          "No manifest preserves existing fingerprinting. Economics fingerprints retain code/table lines. "
          "Cost review requires financial phrases, not bare margin or markup. Token "
          "candidates require at least 20 characters, 8 distinct characters and 3 bits/character of entropy; "
          "obvious placeholder words are skipped. PEM headers and service-role JWTs "
          "are structural checks. .env.example is exempt only from the filename rule; "
          "populated secrets are still checked. Hashes use SHA-256 of normalized text. "
          "Withheld constants, private_path, private_import and local_path cannot be allowlisted. "
          "private_path checks every snapshot path for the segment pair harness/evals. "
          "private_import checks every Python file under loopcut/tests/ with ASTs for "
          "sys.path insert/append/assignment referring to an evals directory and importlib "
          "loads under harness/evals; comments and docstrings are ignored. Literal path "
          "segments, assigned paths and import aliases are followed without executing code; "
          "runtime-generated paths are not resolved. local_path checks every line of every Python "
          "file under loopcut/tests/ for an absolute path on the private disk, into a private "
          "repository checkout or under /private/tmp or /private/var. Binary blobs are searched for text "
          "too; this is not a detector for encoded, encrypted or paraphrased leaks.")

# Context is required for providers that issue otherwise indistinguishable keys.
UUID_PATTERN = r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}"
SECRET_PATTERNS = {
    "openai": r"(?<![\w-])sk-(?!ant-)[A-Za-z0-9_-]{20,}(?![\w-])",
    "loopcut": r"(?<!\w)lc_[A-Za-z0-9_-]{20,}(?!\w)",
    "anthropic": r"(?<![\w-])sk-ant-[A-Za-z0-9_-]{20,}(?![\w-])",
    "google": r"(?<!\w)AIza[A-Za-z0-9_-]{35}(?![\w-])",
    "fal": r"(?<![\w-])(?:" + UUID_PATTERN + r"|[a-fA-F0-9]{32}):(?:" +
           UUID_PATTERN + r"|[a-fA-F0-9]{32,64})(?![\w-])",
    "elevenlabs": r"(?<!\w)(?:xi-[A-Za-z0-9_-]{20,}|sk_[A-Za-z0-9]{32,})(?!\w)|"
                  r"(?i:(?:xi-api-key|elevenlabs(?:_api)?[_ -]?key))[\"']?\s*[:=]\s*"
                  r"[\"']?([A-Za-z0-9_-]{32,128})(?![\w-])",
    "github": r"(?<!\w)(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})(?!\w)",
    "stripe": r"(?<!\w)[sr]k_(?:live|test)_[A-Za-z0-9]{16,}(?!\w)",
    "aws": r"(?<!\w)(?:AKIA|ASIA)[A-Z0-9]{16}(?!\w)",
    "supabase-service-role": r"(?<![\w-])eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\."
                             r"[A-Za-z0-9_-]{16,}(?![\w-])",
    "pem-private-key": r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----",
}
SECRET_REGEXES = {name: re.compile(pattern) for name, pattern in SECRET_PATTERNS.items()}
COST_REGEX = re.compile(
    r"\b(?:gross[\s_-]+margin|margin[\s_-]+per[\s_-]+(?:job|credit)|"
    r"markup[\s_-]+(?:on|of)|cost[\s_-]+per[\s_-]+credit|"
    r"credits[\s_-]+per[\s_-]+(?:dollar|usd)|our[\s_-]+cost|unit[\s_-]+economics)\b", re.I)
PLACEHOLDER = re.compile(r"(?:^|[-_])(?:example|placeholder|dummy|changeme|replace|your|"
                         r"insert|not)(?:[-_]|$)", re.I)


class ReleaseError(Exception):
    """An incomplete check must never look like a clean release."""


def normalize(text):
    return " ".join(text.split())


def digest(text):
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Fingerprint:
    text: str
    category: str
    source: str
    line: int


def prose_line(line):
    """Shared API calls are not private prose, even inside a comment or string."""
    if len(line.split()) < 8:
        return False
    characters = [character for character in line if not character.isspace()]
    if not characters or sum(character.isalpha() for character in characters) / len(characters) < 0.7:
        return False
    return not re.match(r"^(?:[\w$]+(?:\.[\w$]+)*\s*(?:[.(=]|,\s*[\w$])|"
                        r"(?:async\s+)?(?:def|class|function|const|let|var|return|import|from)\b)", line)


def python_text_lines(text):
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.COMMENT:
                value = token.string[1:]
            elif token.type == tokenize.STRING:
                quote = re.match(r"(?i)[rubf]*(\"\"\"|'''|\"|')", token.string)
                value = token.string[quote.end():-len(quote.group(1))]
            elif token.type == getattr(tokenize, "FSTRING_MIDDLE", -1):
                value = token.string
            else:
                continue
            for offset, line in enumerate(value.splitlines()):
                yield token.start[0] + offset, line
    except (tokenize.TokenError, IndentationError):
        # Captured snippets can be incomplete; never fall back to executable lines.
        return


def javascript_text_lines(text):
    tokens = re.compile(r"//[^\n]*|/\*[\s\S]*?\*/|"
                        r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`')
    number, previous = 1, 0
    for match in tokens.finditer(text):
        number += text.count("\n", previous, match.start())
        previous = match.start()
        token = match.group()
        if token.startswith("//"):
            value = token[2:]
        elif token.startswith("/*"):
            value = token[2:-2]
        else:
            value = token[1:-1]
        for offset, line in enumerate(value.splitlines()):
            yield number + offset, line.lstrip().removeprefix("*").lstrip()


def prose_lines(text, source):
    suffix = Path(source).suffix.lower()
    if suffix == ".py":
        yield from python_text_lines(text)
        return
    if suffix in (".js", ".mjs", ".cjs", ".ts", ".mts", ".cts"):
        yield from javascript_text_lines(text)
        return
    fence = ""
    for number, line in enumerate(text.splitlines(), 1):
        if suffix in (".md", ".markdown"):
            marker = re.match(r"^\s*(`{3,}|~{3,})(.*)$", line)
            if marker:
                run, rest = marker.groups()
                if not fence:
                    fence = run
                elif run[0] == fence[0] and len(run) >= len(fence) and not rest.strip():
                    fence = ""
                continue
            if fence:
                continue
        yield number, line


def fingerprints(text, category, source):
    result = []
    prose_only = category in ("private-text", "research-text")
    lines = prose_lines(text, source) if prose_only else enumerate(text.splitlines(), 1)
    for number, line in lines:
        if number != 1 and number % 5:
            continue
        line = normalize(line)
        if prose_only and not prose_line(line):
            continue
        if len(line) < 40 or not any(character.isalpha() for character in line):
            continue
        if re.match(r"(?:from\s+\S+\s+import\b|import\s|(?:export\s+)?import\b)", line):
            continue
        if re.fullmatch(r"[\"']?[\w.-]{1,30}[\"']?\s*:\s*[\"']?[\w.-]{0,20}[\"']?[,{}]*", line):
            continue
        result.append(Fingerprint(line[:40], category, str(source), number))
    return result


def load_allowlist(path):
    reviewed = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2 or not re.fullmatch(r"[a-f0-9]{64}", parts[0]) or not parts[1].strip():
            raise ReleaseError(f"Invalid allowlist entry at line {number}; expected SHA-256 and reason")
        reviewed[parts[0]] = parts[1].strip()
    return reviewed


def allowlist_add(argv, repo):
    command = argparse.ArgumentParser(description="Allow an exact upstream blob line without copying its text")
    command.add_argument("--rev", required=True)
    command.add_argument("--path", required=True)
    command.add_argument("--line", type=int, required=True)
    command.add_argument("--reason", required=True)
    command.add_argument("--allowlist", type=Path, default=HERE / "public_release_allowlist.txt")
    args = command.parse_args(argv)
    try:
        name = PurePosixPath(args.path)
        if (not args.path or name.is_absolute() or name.as_posix() != args.path or
                any(part in ("..", ".git") for part in name.parts) or
                any(ord(char) < 32 for char in args.path) or "\\" in args.path):
            raise ReleaseError("--path must be a canonical repository-relative path")
        if args.path in ("loopcut", ADDON_PATH) or loopcut_path(args.path):
            raise ReleaseError("Loopcut and add-on paths cannot be allowlisted by this command")
        if not args.reason.strip():
            raise ReleaseError("--reason must not be empty")
        if args.line < 1:
            raise ReleaseError("--line must be positive")
        revision = resolve(repo, args.rev)
        entry = tree_entries(repo, revision).get(args.path)
        if entry is None or entry[1] != "blob":
            raise ReleaseError("--path must identify a blob in --rev")
        lines = git(repo, "cat-file", "blob", entry[2]).decode("utf-8").split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        if args.line > len(lines):
            raise ReleaseError("--line is outside the blob")
        # digest is the scanner's whitespace normalization and full-line hash.
        line_hash = digest(lines[args.line - 1])
        load_allowlist(args.allowlist)
        separator = "" if not args.allowlist.stat().st_size or args.allowlist.read_bytes().endswith(b"\n") else "\n"
        with args.allowlist.open("a", encoding="utf-8") as stream:
            stream.write(f"{separator}{line_hash}  # {args.path}:{args.line} {normalize(args.reason)}\n")
        print(f"Added line SHA-256: {line_hash}")
        return 0
    except (ReleaseError, OSError, UnicodeError) as exc:
        print(str(exc) if isinstance(exc, ReleaseError) else "Could not add allowlist entry", file=sys.stderr)
        return 2


def token_is_real(token):
    if len(token) < 20 or len(set(token)) < 8 or PLACEHOLDER.search(token):
        return False
    counts = Counter(token)
    entropy = -sum((count / len(token)) * math.log2(count / len(token)) for count in counts.values())
    return entropy >= 3.0


def service_role(token):
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return isinstance(claims, dict) and claims.get("role") == "service_role"
    except (ValueError, UnicodeError):
        return False


def secret_hits(line):
    for name, pattern in SECRET_REGEXES.items():
        for match in pattern.finditer(line):
            token = match.group(1) if match.lastindex else match.group()
            if name == "pem-private-key" or (name == "supabase-service-role" and service_role(token)):
                yield name, token
            elif name != "supabase-service-role" and token_is_real(token):
                yield name, token


def redact(text):
    for _, token in list(secret_hits(text)):
        text = text.replace(token, "<redacted:" + digest(token)[:12] + ">")
    return text


def redact_report(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [redact_report(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_report(item) for key, item in value.items()}
    return value


def sensitive_filename(name):
    filename = PurePosixPath(name).name.lower()
    return filename == "credentials.json" or filename == ".env" or (
        filename.startswith(".env.") and filename != ".env.example")


@dataclass
class Hit:
    scope: str
    file: str
    line: int
    category: str
    detail: str
    fingerprint_sha256: str
    line_sha256: str
    source: str = ""
    source_line: int = 0
    commit: str = ""
    side: str = ""
    allowed: bool = False
    reason: str = ""


def private_path(parts):
    return any(pair == ("harness", "evals") for pair in zip(parts, parts[1:]))


class PrivateImports(ast.NodeVisitor):
    """Follow static path values only at import sinks, never prose or sample code."""

    def __init__(self):
        self.paths = {}
        self.aliases = {}
        self.nodes = []

    def qualified(self, node):
        if isinstance(node, ast.Name):
            return self.aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return self.qualified(node.value) + "." + node.attr
        return ""

    def strings(self, node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node.value]
        if isinstance(node, ast.Name):
            return self.paths.get(node.id, [])
        return [value for child in ast.iter_child_nodes(node) for value in self.strings(child)]

    def parts(self, node, dotted=False):
        return tuple(part for value in self.strings(node)
                     for part in PurePosixPath(value.replace("\\", "/").replace(".", "/")
                                              if dotted else value.replace("\\", "/")).parts)

    def visit_Import(self, node):
        for item in node.names:
            self.aliases[item.asname or item.name.split(".")[0]] = item.name if item.asname else item.name.split(".")[0]

    def visit_ImportFrom(self, node):
        for item in node.names:
            self.aliases[item.asname or item.name] = (node.module or "") + "." + item.name

    def visit_Call(self, node):
        function = self.qualified(node.func)
        if function in ("sys.path.insert", "sys.path.append"):
            arguments = node.args[1:] if function.endswith("insert") else node.args
            arguments = [*arguments, *(item.value for item in node.keywords)]
            if any("evals" in self.parts(value) for value in arguments):
                self.nodes.append(node)
        else:
            loaders = {"importlib.util.spec_from_file_location": (1, "location"),
                       "importlib.machinery.SourceFileLoader": (1, "path"),
                       "importlib.machinery.SourcelessFileLoader": (1, "path"),
                       "importlib.machinery.ExtensionFileLoader": (1, "path"),
                       "importlib.import_module": (0, "name")}
            if function in loaders:
                index, keyword = loaders[function]
                arguments = node.args[index:index + 1] + [item.value for item in node.keywords if item.arg == keyword]
                if function == "importlib.import_module":
                    arguments += node.args[1:2] + [item.value for item in node.keywords if item.arg == "package"]
                if any(private_path(self.parts(value, dotted=function == "importlib.import_module"))
                       for value in arguments):
                    self.nodes.append(node)
        self.generic_visit(node)

    def visit_Assign(self, node):
        self.generic_visit(node)
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            path = target.value if isinstance(target, ast.Subscript) else target
            if not isinstance(target, ast.Name) and self.qualified(path) == "sys.path" and node.value is not None:
                if "evals" in self.parts(node.value):
                    self.nodes.append(node)
                    break
        if node.value is not None:
            values, alias = self.strings(node.value), self.qualified(node.value)
            for target in targets:
                if isinstance(target, ast.Name):
                    self.paths[target.id] = values
                    self.aliases[target.id] = alias or target.id

    visit_AnnAssign = visit_Assign
    visit_AugAssign = visit_Assign

    def visit_FunctionDef(self, node):
        paths, aliases = self.paths, self.aliases
        self.paths, self.aliases = paths.copy(), aliases.copy()
        # Local parameters shadow any same-named module paths/imports.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for argument in ast.walk(node.args):
                if isinstance(argument, ast.arg):
                    self.paths.pop(argument.arg, None)
                    self.aliases.pop(argument.arg, None)
        self.generic_visit(node)
        self.paths, self.aliases = paths, aliases

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_ClassDef = visit_FunctionDef


class Scanner:
    def __init__(self, samples, allowlist):
        self.samples = samples
        self.allowlist = allowlist
        self.hits = []
        self.errors = []
        self.incomplete = False
        self.submodules = []
        self.coverage = {category: {"files": set(), "lines": 0} for category in (
            "secret-file", "cost-review", "private-text", "research-text", "cost-fingerprint",
            "withheld-prompt", "private_path", "private_import", "local_path", "bytecode",
            *("secret:" + name for name in SECRET_PATTERNS))}

    def counted(self, categories, name, scope, lines=0, commit="", side=""):
        for category in categories:
            item = self.coverage[category]
            item["files"].add((scope, name, commit, side))
            item["lines"] += lines

    def summary(self):
        return [{"category": category, "files": len(item["files"]), "lines": item["lines"]}
                for category, item in self.coverage.items()]

    def check(self, function, *args, context=""):
        try:
            return function(*args)
        except (ReleaseError, OSError, UnicodeError, tarfile.TarError,
                zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
            error = str(exc) if isinstance(exc, ReleaseError) else f"{type(exc).__name__}: check could not complete"
            self.errors.append(context + ": " + error if context else error)
            self.incomplete = True

    def hit(self, scope, file, line, category, detail, matched, original,
            source="", source_line=0, commit="", side=""):
        fingerprint_hash, line_hash = digest(matched), digest(original)
        reason = "" if category in ("withheld-prompt", "private_path", "private_import", "local_path",
                                    "bytecode") else (
            self.allowlist.get(fingerprint_hash) or self.allowlist.get(line_hash, ""))
        self.hits.append(Hit(scope, redact(file), line, category, detail, fingerprint_hash, line_hash,
                             redact(source), source_line, commit, side, bool(reason), redact(reason)))

    def filename(self, name, scope, commit="", side=""):
        self.counted(["secret-file", *("secret:" + key for key in SECRET_PATTERNS)],
                     name, scope, commit=commit, side=side)
        if sensitive_filename(name):
            self.hit(scope, name, 1, "secret-file", "sensitive configuration filename",
                     name, name, commit=commit, side=side)
        for category, token in secret_hits(name):
            self.hit(scope, name, 1, "secret:" + category, "credential pattern in filename",
                     token, name, commit=commit, side=side)

    def bytecode(self, name, scope):
        self.counted(["bytecode"], name, scope)
        if bytecode_path(name):
            self.hit(scope, name, 1, "bytecode", "Python cache in release output or unexcluded build input",
                     name, name)

    def private_path(self, name, scope):
        self.counted(["private_path"], name, scope)
        if private_path(PurePosixPath(name).parts):
            self.hit(scope, name, 1, "private_path", "private eval harness path in snapshot",
                     name, name)

    def private_import(self, text, name, scope):
        try:
            tree = ast.parse(text, filename=name)
        except (SyntaxError, ValueError) as exc:
            raise ReleaseError(f"Cannot parse public test: {name}") from exc
        lines = text.splitlines()
        self.counted(["private_import"], name, scope, len(lines))
        visitor = PrivateImports()
        visitor.visit(tree)
        for node in visitor.nodes:
            self.hit(scope, name, node.lineno, "private_import", "public test imports from the private eval harness",
                     ast.get_source_segment(text, node), lines[node.lineno - 1])

    def local_path(self, text, name, scope):
        lines = text.splitlines()
        self.counted(["local_path"], name, scope, len(lines))
        for number, line in enumerate(lines, 1):
            for match in LOCAL_PATH_REGEX.finditer(line):
                self.hit(scope, name, number, "local_path", "public test names a path only this machine has",
                         match.group(), line)

    def text(self, text, name, scope, commit="", side="", first_line=1):
        lines = text.splitlines()
        self.counted(["cost-review", *("secret:" + key for key in SECRET_PATTERNS),
                      *{sample.category for sample in self.samples}],
                     name, scope, len(lines), commit, side)
        for number, line in enumerate(lines, first_line):
            for category, token in secret_hits(line):
                self.hit(scope, name, number, "secret:" + category, "credential pattern",
                         token, line, commit=commit, side=side)
            for match in COST_REGEX.finditer(line):
                self.hit(scope, name, number, "cost-review", "cost terminology; needs review",
                         match.group(), line, commit=commit, side=side)
        # Matching the whole normalized document also catches a wrapped fingerprint.
        chunks, starts, numbers = [], [], []
        offset = 0
        for number, line in enumerate(lines, first_line):
            for token in line.split():
                starts.append(offset)
                numbers.append(number)
                chunks.append(token)
                offset += len(token) + 1
        normalized = " ".join(chunks)
        for sample in self.samples:
            start = normalized.find(sample.text)
            while start >= 0:
                number = numbers[bisect.bisect_right(starts, start) - 1]
                self.hit(scope, name, number, sample.category, "private source fingerprint",
                         sample.text, lines[number - first_line], sample.source, sample.line,
                         commit, side)
                start = normalized.find(sample.text, start + 1)

    def withheld(self, text, name, scope):
        filename = PurePosixPath(name).name
        constant = WITHHELD.get(filename)
        if not constant:
            return
        try:
            tree = ast.parse(text, filename=name)
        except (SyntaxError, ValueError) as exc:
            raise ReleaseError(f"Cannot parse withheld-prompt module: {name}") from exc
        self.counted(["withheld-prompt"], name, scope, len(text.splitlines()))
        for node in tree.body:
            targets = node.targets if isinstance(node, ast.Assign) else (
                [node.target] if isinstance(node, ast.AnnAssign) else [])
            if not any(isinstance(target, ast.Name) and target.id == constant for target in targets):
                continue
            if isinstance(node.value, ast.Constant) and node.value.value in (None, ""):
                continue
            # The AST folds implicit concatenation and backslash continuations.
            # Fingerprint f-string literals without evaluating interpolations.
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                value = node.value.value
            elif isinstance(node.value, ast.JoinedStr):
                value = "\n".join(part.value for part in node.value.values
                                  if isinstance(part, ast.Constant) and isinstance(part.value, str))
            else:
                value = ast.get_source_segment(text, node.value) if node.value else constant
            self.hit(scope, name, node.lineno, "withheld-prompt",
                     f"{filename}:{constant}: {WITHHELD_DETAIL}", value or constant,
                     text.splitlines()[node.lineno - 1])


def git_environment():
    # Ignore inherited repository/index overrides, including from hook environments.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_AUTHOR_NAME="Loopcut release review", GIT_AUTHOR_EMAIL="release@localhost",
               GIT_COMMITTER_NAME="Loopcut release review", GIT_COMMITTER_EMAIL="release@localhost")
    return env


def git(repo, *args, input=None, index=None, stdout=subprocess.PIPE):
    env = git_environment()
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    command = ["git", "--literal-pathspecs", "-c", "core.fsmonitor=false",
               "-c", "gc.auto=0", "-c", "maintenance.auto=false", "-c", "core.hooksPath=" + os.devnull,
               "-C", str(repo), *args]
    result = subprocess.run(command, input=input, stdout=stdout, stderr=subprocess.PIPE, env=env)
    if result.returncode:
        raise ReleaseError(f"Git {args[0]} failed (exit {result.returncode})")
    return result.stdout


def resolve(repo, revision):
    return git(repo, "rev-parse", "--verify", "--end-of-options", revision + "^{commit}").decode().strip()


def tree_entries(repo, revision):
    entries = {}
    for raw in git(repo, "ls-tree", "-r", "-z", revision).split(b"\0"):
        if raw:
            metadata, name = raw.split(b"\t", 1)
            mode, kind, oid = metadata.decode().split()
            entries[os.fsdecode(name)] = (mode, kind, oid)
    return entries


def exclusions(path):
    patterns = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        pattern = raw.split("#", 1)[0].strip()
        if not pattern:
            continue
        if pattern.startswith("/") or ".." in PurePosixPath(pattern).parts or "\\" in pattern:
            raise ReleaseError("Exclusions must be repository-relative paths or globs")
        patterns.append(pattern)
    return patterns


def excluded(name, patterns):
    return any(fnmatch.fnmatchcase(name, pattern) or name == pattern.rstrip("/") or
               name.startswith(pattern.rstrip("/") + "/") for pattern in patterns)


def cmake_excluded(name, patterns):
    """CMake matches a full filename at any depth, and excludes its descendants."""
    parts = PurePosixPath(name).parts
    return any(fnmatch.fnmatchcase("/".join(parts[start:end]), pattern)
               for pattern in patterns for end in range(1, len(parts) + 1) for start in range(end))


def bytecode_path(name):
    return cmake_excluded(name, BYTECODE_PATTERNS)


def cmake_build_patterns(root):
    """Fail closed if the audited install rules drift, including in another checkout."""
    source = (root / "source/creator/CMakeLists.txt").read_text(encoding="utf-8")
    source = re.sub(r"#[^\n]*", "", source)
    blocks = re.findall(r"\binstall\(\s*DIRECTORY\s+([^)]*)\)", source)
    result = {}
    for name in BUILD_INPUT_PATTERNS:
        variable = ("${ASSET_BUNDLE_DIR}" if name == "assets" else "${from}" if
                    name.startswith("release/darwin/") else "${CMAKE_SOURCE_DIR}/" + name)
        selected = [block for block in blocks if block.split()[0] == variable]
        if len(selected) != 1 or re.search(r"\b(?:REGEX|FILES_MATCHING)\b", selected[0]):
            raise ReleaseError(f"Unsupported build-input install rule: {name}")
        result[name] = tuple(re.findall(r'PATTERN\s+"([^"]+)"\s+EXCLUDE', selected[0]))
    return result


def scan_build_inputs(root, source, scanner, report):
    """Inspect the install sources on disk, not Git's tracked/ignored file list."""
    root = root.resolve()
    head = resolve(root, "HEAD")
    if Path(os.fsdecode(git(root, "rev-parse", "--show-toplevel")).strip()).resolve() != root:
        raise ReleaseError("--build-root must be a checkout root")
    result = {"root": str(root), "head": head, "source": source, "warnings": [],
              "directories": list(BUILD_INPUT_PATTERNS), "explicit_files": list(BUILD_INPUT_FILES),
              "patterns": BUILD_INPUT_PATTERNS, "files_scanned": 0, "excluded": [], "absent_optional": []}
    report["build_inputs"] = result
    if head != source:
        result["warnings"].append("Build-root HEAD differs from the source revision; working-tree bytes were scanned.")
    actual = cmake_build_patterns(root)
    if any(set(actual[name]) != set(patterns) for name, patterns in BUILD_INPUT_PATTERNS.items()):
        raise ReleaseError("Build-input exclusions differ from the audited CMake rules")

    def inspect(path, patterns):
        name = path.relative_to(root).as_posix()
        # FILES exceptions are inspected separately, even through an excluded
        # parent symlink that the directory walker must not follow.
        if patterns and name in BUILD_INPUT_FILES:
            return
        omitted = cmake_excluded(name, patterns)
        if path.is_symlink():
            if not omitted:
                raise ReleaseError(f"Unresolved build-input symlink: {name}")
            data = os.fsencode(os.readlink(path))
        elif path.is_dir():
            if not omitted:
                scanner.bytecode(name, "build-inputs")
            for child in sorted(path.iterdir()):
                scanner.check(inspect, child, patterns, context=child.relative_to(root).as_posix())
            return
        elif path.is_file():
            data = path.read_bytes()
        else:
            raise ReleaseError(f"Missing or special build input: {name}")
        if omitted:
            result["excluded"].append({"file": name, "sha256": hashlib.sha256(data).hexdigest()})
            return
        scanner.bytecode(name, "build-inputs")
        scanner.filename(name, "build-inputs")
        scanner.private_path(name, "build-inputs")
        text = data.decode("utf-8", "replace")
        scanner.text(text, name, "build-inputs")
        if name.startswith(ADDON_PATH + "/"):
            scanner.withheld(text, name, "build-inputs")
        result["files_scanned"] += 1

    # PYTHON_VERSION affects only static-library pruning in the bundle template.
    version_source = (root / "build_files/cmake/Modules/FindPythonLibsUnix.cmake").read_text(encoding="utf-8")
    version = re.search(r'set\(_PYTHON_VERSION_SUPPORTED\s+(\d+\.\d+)', version_source)
    if version is None:
        raise ReleaseError("Cannot determine the release Python version for build-input exclusions")
    for name, patterns in BUILD_INPUT_PATTERNS.items():
        path = root / name
        if name in OPTIONAL_BUILD_INPUTS and not path.exists():
            result["absent_optional"].append(name)
            continue
        expanded = tuple(p.replace("${FREESTYLE_EXCLUDE_CONDITIONAL}", "_freestyle/*")
                         .replace("${PYTHON_VERSION}", version[1]) for p in patterns)
        scanner.check(inspect, path, expanded, context=name)
    for name in BUILD_INPUT_FILES:
        scanner.check(inspect, root / name, (), context=name)


def build_snapshot(repo, source, base, patterns):
    source, base = resolve(repo, source), resolve(repo, base)
    names = tree_entries(repo, source)
    dropped = sorted(name for name in names if excluded(name, patterns) or bytecode_path(name))
    with tempfile.TemporaryDirectory(prefix="loopcut-release-index-") as temporary:
        index = Path(temporary) / "index"
        git(repo, "read-tree", source, index=index)
        if dropped:
            git(repo, "update-index", "--force-remove", "-z", "--stdin", index=index,
                input=b"".join(os.fsencode(name) + b"\0" for name in dropped))
        tree = git(repo, "write-tree", index=index).decode().strip()
        message = f"Public release snapshot (dry run)\n\nSquashed source: {source}\n"
        snapshot = git(repo, "-c", "commit.gpgSign=false", "commit-tree", tree, "-p", base,
                       input=message.encode(), index=index).decode().strip()
    # --no-deref prevents even a pre-existing symbolic private ref from moving a branch.
    git(repo, "update-ref", "--no-deref", DRY_RUN_REF, snapshot)
    return base, source, snapshot, dropped


def changed_files(repo, base, snapshot):
    return [os.fsdecode(name) for name in git(repo, "diff", "--name-only", "--no-renames", "--no-ext-diff", "--no-textconv",
                                             "-z", base, snapshot, "--").split(b"\0") if name]


def read_private_text(path):
    data = path.read_bytes()
    for marker, encoding in ((b"\xff\xfe\x00\x00", "utf-32"), (b"\x00\x00\xfe\xff", "utf-32"),
                             (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16")):
        if data.startswith(marker):
            return data.decode(encoding)
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def read_prompt_source(path):
    """Use the platform's strict decoding for manifest-authorized public text."""
    raw = path.read_bytes()
    encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    try:
        text = raw.decode(encoding)
    except UnicodeError as exc:
        raise ReleaseError("Invalid prompts manifest or open source encoding") from exc
    if "\0" in text:
        raise ReleaseError("NUL in prompts manifest or open source text")
    return text


def open_prompt_texts(prompts):
    """Match the platform's manifest validation and full-text open exemption."""
    manifest = prompts / "manifest.json"
    if not manifest.exists():
        return {}
    try:
        data = json.loads(read_prompt_source(manifest))
    except ValueError as exc:
        raise ReleaseError("malformed prompts manifest.json") from exc
    if not isinstance(data, dict):
        raise ReleaseError("prompts manifest.json must be an object")
    entries = data.get("open", [])
    if not isinstance(entries, list) or any(not isinstance(entry, str) for entry in entries):
        raise ReleaseError("prompts manifest.json open must be an array of paths")
    opened = set()
    for entry in entries:
        path = Path(entry)
        if not entry or path.is_absolute() or ".." in path.parts or path.as_posix() != entry:
            raise ReleaseError("prompts manifest.json open paths must be canonical relative paths")
        if not (prompts / path).is_file() or path.parts[0] == "fixtures" or entry in ("README.md", "manifest.json"):
            raise ReleaseError("prompts manifest.json open entry must identify a source file")
        opened.add(entry)
    prompt_map = data.get("prompts", {})
    if not isinstance(prompt_map, dict):
        raise ReleaseError("manifest prompts must be an object")
    for item in prompt_map.values():
        if not isinstance(item, dict) or not isinstance(item.get("parts"), list) or not item["parts"]:
            raise ReleaseError("manifest prompt parts must be a nonempty array")
        for part in item["parts"]:
            if (not isinstance(part, str) or Path(part).is_absolute() or ".." in Path(part).parts or
                    not (prompts / part).is_file()):
                raise ReleaseError("missing or invalid manifest prompt part")
    return {name: normalize(read_prompt_source(prompts / name)) for name in sorted(opened)}


def private_samples(args):
    prompts = Path(args.prompts_dir)
    open_text = open_prompt_texts(prompts)
    roots = [(Path(args.skills_dir), "private-text"), (prompts, "private-text")]
    # Captures may be stored by type or nested below a session directory. No research folder configured
    # (a checkout without loopcut/track/research_folders.txt) is reported as missing coverage.
    roots.append((Path(args.research_dir) if args.research_dir else None, "research-text"))
    roots.append((Path(args.economics_file), "cost-fingerprint"))
    roots.extend((Path(path), "cost-fingerprint") for path in args.cost_docs)
    samples, coverage = [], []
    for root, category in roots:
        if root is None or not root.exists():
            coverage.append({"path": str(root) if root else "(research folder not configured)",
                             "status": "missing", "files": 0, "binary_files": 0,
                             "fingerprints": 0, "open_fingerprints_dropped": 0})
            continue
        files = [root] if root.is_file() else sorted(path for path in root.rglob("*") if path.is_file())
        if category == "research-text":
            files = [path for path in files if any(kind in path.relative_to(root).parts or
                     path.stem == kind for kind in RESEARCH_KINDS)]
        if root == prompts and root.is_dir():
            files = [path for path in files if path.relative_to(root).as_posix() not in open_text and
                     path != prompts / "manifest.json"]
        count, binary_count, dropped, start = 0, 0, 0, len(samples)
        for path in files:
            text = read_private_text(path)
            if text is None:
                binary_count += 1
                continue
            count += 1
            for sample in fingerprints(text, category, path):
                if category == "private-text" and any(sample.text in public for public in open_text.values()):
                    dropped += 1
                else:
                    samples.append(sample)
        coverage.append({"path": str(root), "status": "read", "files": count,
                         "binary_files": binary_count, "fingerprints": len(samples) - start,
                         "open_fingerprints_dropped": dropped})
    return samples, coverage


def patch_path(raw):
    if raw.startswith('"'):
        try:
            raw = ast.literal_eval(raw)
        except (ValueError, SyntaxError) as exc:
            raise ReleaseError("Cannot decode Git patch path") from exc
    return raw[2:] if raw.startswith(("a/", "b/")) else raw


def scan_history(repo, base, snapshot, scanner):
    patch = git(repo, "-c", "core.quotePath=false", "log", "--format=commit %H%n%B", "-p",
                "--no-renames", "--no-color", "--no-ext-diff", "--no-textconv",
                "--src-prefix=a/", "--dst-prefix=b/", f"{base}..{snapshot}", "--").decode("utf-8", "replace")
    commit, old_file, new_file = "", "(git log)", "(git log)"
    old_line, new_line, in_hunk = 0, 0, False
    before, after = [], []

    def flush():
        # Scan both complete sides of a hunk so wrapped, deleted text is checked too.
        if before:
            scanner.text("\n".join(before), old_file, "history", commit, "before", old_line)
        if after:
            scanner.text("\n".join(after), new_file, "history", commit, "after", new_line)
        before.clear()
        after.clear()

    for number, line in enumerate(patch.splitlines(), 1):
        if re.fullmatch(r"commit [a-f0-9]{40,64}", line):
            flush()
            commit, in_hunk = line[7:], False
            old_file = new_file = "(git log)"
        elif line.startswith("diff --git "):
            flush()
            in_hunk = False
            scanner.text(line, "(git log)", "history", commit, first_line=number)
        elif not in_hunk and line.startswith("--- "):
            old_file = patch_path(line[4:])
            scanner.filename(old_file, "history", commit, "removed")
        elif not in_hunk and line.startswith("+++ "):
            new_file = patch_path(line[4:])
            scanner.filename(new_file, "history", commit, "added")
        elif line.startswith("@@ "):
            flush()
            match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            if not match:
                raise ReleaseError("Cannot parse Git patch hunk")
            old_line, new_line = map(int, match.groups())
            in_hunk = True
        elif in_hunk and line[:1] in ("+", "-", " "):
            if not line.startswith("+"):
                before.append(line[1:])
            if not line.startswith("-"):
                after.append(line[1:])
        else:
            scanner.text(line, "(git log)", "history", commit, first_line=number)
    flush()


def scan_tree(repo, base, snapshot, scanner):
    entries = tree_entries(repo, snapshot)
    baseline = tree_entries(repo, base)
    changed = changed_files(repo, base, snapshot)
    links = {name for name, entry in baseline.items() if entry[0] == "160000"}
    links.update(name for name, entry in entries.items() if entry[0] == "160000")
    for name in sorted(links):
        old, new = baseline.get(name), entries.get(name)
        differs = old != new
        scanner.submodules.append({"file": name, "base": old[2] if old else None,
                                   "snapshot": new[2] if new else None, "changed": differs,
                                   "status": "submodule pointers, not inspected"})
        if differs:
            scanner.errors.append(f"Submodule pointer differs from base: {name}")
    # Known constants, private test imports and all paths are checked even when unchanged.
    known = {"scripts/addons_core/loopcut/" + name for name in WITHHELD}

    def inspect(name, entry):
        mode, kind, oid = entry
        scanner.bytecode(name, "tree")
        scanner.private_path(name, "tree")
        if mode == "160000":
            return
        scanner.filename(name, "tree")
        if kind != "blob":
            raise ReleaseError(f"Cannot inspect non-blob tree entry: {name}")
        public_test = name.startswith("loopcut/tests/") and name.endswith(".py")
        if name in changed or name in known or public_test:
            text = git(repo, "cat-file", "blob", oid).decode("utf-8", "replace")
            if name in changed:
                scanner.text(text, name, "tree")
            if name in known:
                scanner.withheld(text, name, "tree")
            if public_test:
                scanner.private_import(text, name, "tree")
                scanner.local_path(text, name, "tree")

    for name, entry in entries.items():
        scanner.check(inspect, name, entry, context=name)
    return changed


def scan_zip(path, scanner):
    count, total = 0, 0
    def inspect(archive, info):
        scanner.bytecode(info.filename, "zip")
        scanner.filename(info.filename, "zip")
        if info.file_size > 256 * 1024 ** 2 or total > 1024 ** 3:
            raise ReleaseError("Zip exceeds scan limits (256 MiB/file, 1 GiB total)")
        text = archive.read(info).decode("utf-8", "replace")
        scanner.text(text, info.filename, "zip")
        scanner.withheld(text, info.filename, "zip")
        return True

    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    scanner.bytecode(info.filename, "zip")
                    continue
                total += info.file_size
                if scanner.check(inspect, archive, info, context=info.filename):
                    count += 1
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise ReleaseError("Cannot inspect extension zip") from exc
    return count


def export_tree(repo, snapshot, out):
    target = out / "tree"
    target.mkdir()
    # The public release is the whole fork, including unchanged upstream files.
    # Stream the archive through disk instead of retaining the entire fork in RAM.
    with tempfile.TemporaryFile() as data:
        git(repo, "archive", "--format=tar", snapshot, stdout=data)
        data.seek(0)
        with tarfile.open(fileobj=data, mode="r|") as archive:
            extract_tree(archive, target)


def extract_tree(archive, target):
    for member in archive:
        path = PurePosixPath(member.name)
        if path.is_absolute() or any(part in ("..", ".") or part.lower() == ".git" for part in path.parts):
            raise ReleaseError("Unsafe path in snapshot archive")
        destination = target.joinpath(*path.parts)
        if member.isdir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Symlinks are shown as text files; never follow them into another worktree.
        if member.issym():
            payload = member.linkname.encode("utf-8")
        elif member.isfile():
            with archive.extractfile(member) as source:
                payload = source.read()
        else:
            raise ReleaseError("Unsupported entry in snapshot archive")
        with destination.open("xb") as stream:
            stream.write(payload)


def default_leak_check():
    override = os.getenv("LOOPCUT_RELEASE_LEAK_CHECK")
    if override is not None:
        return Path(override)
    return next((path for path in LEAK_CHECK_PATHS if path.is_file()), LEAK_CHECK_PATHS[0])


def platform_runs(args):
    """Report commands use placeholders so private input paths are never recorded."""
    command = [str(args.leak_check.resolve()), "--prompts-dir", "<prompts-dir>",
               "--skills-dir", "<skills-dir>"]
    zip_args = ["--zip", "<shipped-addon.zip>"]
    for number in range(1, len(args.extension_zips) + 1):
        zip_args.extend(["--zip", f"<caller-zip-{number}>"])
    targets = {"tree": ["<snapshot-tree>"], "history": ["<history-repo>", "<base>..<snapshot>"],
               "zip": [*zip_args, "<empty-dir>"]}
    return [{"mode": mode, "command": [*command, *target], "exit_code": None,
             "hit_count": None, "skip_count": None, "skip_counts": {}, "skips": [],
             "strict": False, "locations": [], "passes": [], "allowed_owners": [],
             "status": "not run",
             "required": not args.no_leak_check,
             "reason": "disabled by --no-leak-check (tests only)" if args.no_leak_check else
                       "snapshot or scan preparation did not complete"}
            for mode, target in targets.items()]


# The checker escapes paths with unicode_escape, and can redact either endpoint.
PLATFORM_HIT = re.compile(
    r"HIT (?:(tree|zip|history) (.+)|(<redacted location>)):([1-9][0-9]*)"
    r" <- (.+:[1-9][0-9]*|<redacted location>)")
PLATFORM_SKIP = re.compile(r"SKIP (gitlink|lfs) (.+)")


def run_platform_check(run, command):
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=git_environment())
    except OSError as exc:
        run["reason"] = f"could not execute platform checker ({type(exc).__name__})"
        return
    # Keep only the location protocol, never diagnostics, source text or summaries.
    lines = result.stdout.decode("utf-8", "replace").splitlines()
    locations = [line for line in lines if PLATFORM_HIT.fullmatch(line)]
    skips = [{"kind": match[1], "location": match[2]} for line in lines
             if (match := PLATFORM_SKIP.fullmatch(line))]
    malformed = any(line.startswith(("HIT ", "SKIP ")) and
                    not PLATFORM_HIT.fullmatch(line) and not PLATFORM_SKIP.fullmatch(line) for line in lines)
    code = 2 if malformed else result.returncode
    run.update(exit_code=code, hit_count=len(locations), locations=locations,
               skip_count=len(skips), skip_counts=dict(Counter(item["kind"] for item in skips)), skips=skips,
               status="clean" if code == 0 and not locations else "blocked" if code in (0, 1) else "error",
               reason="invalid platform location protocol" if malformed else
                      "" if code in (0, 1) else "platform configuration/scan error")


def platform_display(value):
    return str(value).encode("unicode_escape").decode("ascii")


def platform_path(value):
    return value.encode("ascii").decode("unicode_escape")


def loopcut_path(name):
    return name.startswith(("loopcut/", ADDON_PATH + "/"))


def platform_history(repo, commit):
    """Both stored blob sides, including every merge parent; never rendered patches."""
    if not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", commit):
        raise ReleaseError("Invalid platform history commit")
    records = iter(git(repo, "diff-tree", "--root", "-r", "-m", "--no-commit-id", "--raw", "-z",
                       "--no-abbrev", "--no-renames", "--no-ext-diff", "--no-textconv",
                       "--ignore-submodules=none", commit, "--").split(b"\0"))
    blobs = {}
    for record in records:
        if not record:
            continue
        fields = record.removeprefix(b":").split()
        name = next(records, None)
        if not record.startswith(b":") or len(fields) != 5 or name is None:
            raise ReleaseError("Invalid platform history change record")
        oldmode, newmode, old, new, _ = fields
        sides = blobs.setdefault(os.fsdecode(name), set())
        for side, mode, oid in (("old", oldmode, old), ("new", newmode, new)):
            if mode != b"160000" and oid.strip(b"0"):
                sides.add((side, oid.decode("ascii")))
    return blobs


class PlatformClassifier:
    """Use the selected checker's fingerprint/decoding code; report only hashes."""

    def __init__(self, repo, base, args, temporary, archives, allowlist):
        self.repo, self.base, self.temporary = repo, base, temporary
        self.roots = [Path(args.prompts_dir).resolve(), Path(args.skills_dir).resolve()]
        self.script = args.leak_check.resolve()
        self.archives = {path.resolve() for path in archives}
        self.allowlist = allowlist
        self.base_text = None
        self.public_windows, self.changes, self.hits, self.contents = {}, {}, {}, {}
        self.module = None

    def checker(self):
        if self.module is None:
            # The shell entrypoint delegates to this importable Python companion.
            # run_path avoids writing bytecode into the private checker repository.
            module = SimpleNamespace(**runpy.run_path(str(self.script.with_suffix(".py"))))
            try:
                owners, _, _, _ = module.fingerprints(*self.roots, None)
            except module.CheckError as exc:
                raise ReleaseError("Platform fingerprints could not be recovered") from exc
            self.windows = list(owners)
            self.matcher = module.Scanner(owners)
            self.module = module
        return self.module

    def public(self, window):
        if self.base_text is None:
            blobs = {oid for name, (_, kind, oid) in tree_entries(self.repo, self.base).items()
                     if kind == "blob" and loopcut_path(name)}
            self.base_text = [normalize(git(self.repo, "cat-file", "blob", oid).decode("utf-8", "replace")).encode()
                              for oid in sorted(blobs)]
        if window not in self.public_windows:
            self.public_windows[window] = any(window in text for text in self.base_text)
        return self.public_windows[window]

    def evidence(self, stream):
        """Recover windows ending on each decoded LF line, including cross-line hits.

        Reusing the scanner also preserves manifest-open suppression, first-owner
        deduplication, encoding and literal-escape handling as its protocol evolves.
        """
        module = self.checker()
        matches = {}

        def emit(locations, number, indices):
            if indices:
                matches.setdefault(number, set()).update(indices)

        self.matcher.emit = emit
        try:
            self.matcher.scan_stream(stream, [])
        except module.CheckError as exc:
            raise ReleaseError("Platform content could not be recovered") from exc
        stream.seek(0)
        evidence, chunks = {}, []

        def record(number):
            line = "".join(chunks)
            views = [re.sub(r"\s+", " ", text).encode("utf-8", errors="surrogateescape")
                     for text in (line, module.unescape(line))]
            # An allowlisted ending line cannot authorize private text inserted
            # on the preceding line of a cross-line match.
            contained = {index for index in matches[number]
                         if any(self.windows[index] in view for view in views)}
            evidence[number] = (digest(line), matches[number], contained)

        for number, part, end in module.text_parts(stream):
            if number in matches:
                chunks.append(part)
                if end:
                    record(number)
                    chunks = []
        if chunks:
            record(number)
        return evidence

    def content(self, key, read):
        if key not in self.contents:
            with read() as stream:
                self.contents[key] = self.evidence(stream)
        return self.contents[key]

    def blob(self, kind, oid):
        return self.content((kind, oid), lambda: io.BytesIO(git(self.repo, "cat-file", kind, oid)))

    def recover(self, mode, location):
        """Return (side, repository path, metadata-only, line evidence) for all sides."""
        if mode == "history":
            commit, name = location.split(" ", 1)
            if not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", commit):
                raise ReleaseError("Invalid platform history commit")
            if name == "<commit>":
                return [("commit", None, True, self.blob("commit", commit))]
            metadata = name.endswith(" <path>")
            name = platform_path(name.removesuffix(" <path>"))
            if commit not in self.changes:
                self.changes[commit] = platform_history(self.repo, commit)
            if name not in self.changes[commit]:
                raise ReleaseError("Platform history path unavailable")
            if metadata:
                return [("path", name, True, self.evidence(io.BytesIO(os.fsencode(name))))]
            return [(side, name, False, self.blob("blob", oid))
                    for side, oid in sorted(self.changes[commit][name])]
        if mode == "tree":
            metadata = location.endswith(" <path>")
            path = Path(platform_path(location.removesuffix(" <path>"))).resolve()
            root = self.temporary / "tree"
            if not path.is_relative_to(root):
                raise ReleaseError("Platform tree location outside export")
            name = path.relative_to(root).as_posix()
            evidence = (self.evidence(io.BytesIO(os.fsencode(name))) if metadata else
                        self.content(path, lambda: path.open("rb")))
            return [("path" if metadata else "", name, metadata, evidence)]
        outer, *members = location.split("!")
        if outer.startswith("history "):
            # Archive blob/metadata locations are deliberately fail-closed until
            # they can be mapped unambiguously to both stored archive sides.
            raise ReleaseError("Platform history ZIP recovery unavailable")
        path = Path(platform_path(outer)).resolve()
        if path not in self.archives and not path.is_relative_to(self.temporary / "tree"):
            raise ReleaseError("Platform ZIP location outside scan targets")
        if not members or len(members) > 4:
            raise ReleaseError("Platform ZIP member unavailable")
        source = path
        remaining = 512 * 1024 ** 2
        for index, member in enumerate(members):
            with zipfile.ZipFile(source) as archive:
                info = archive.getinfo(platform_path(member))
                if info.file_size > remaining:
                    raise ReleaseError("Platform ZIP recovery exceeds scan limits")
                remaining -= info.file_size
                with archive.open(info) as stream:
                    if index == len(members) - 1:
                        key = (path, tuple(members))
                        if key not in self.contents:
                            self.contents[key] = self.evidence(stream)
                        return [("", info.filename, False, self.contents[key])]
                    source = io.BytesIO(stream.read())
        raise ReleaseError("Platform ZIP member unavailable")

    def classify(self, location):
        if location in self.hits:
            return self.hits[location]
        mode, target, redacted, number, owner = PLATFORM_HIT.fullmatch(location).groups()
        hit = {"location": location, "owner": owner, "class": "unexplained", "line_sha256": None,
               "line_sha256s": [], "fingerprint_sha256": [], "side": "", "reason": "",
               "mode": mode, "path": None, "line": int(number)}
        try:
            if redacted or "<redacted location>" in (target, owner):
                raise ReleaseError("Redacted platform location")
            windows, hashes, sides, public_scope, allow_scope = set(), set(), set(), True, True
            for side, name, metadata, evidence in self.recover(mode, target):
                if int(number) not in evidence:
                    continue  # Absent/shorter/nonmatching sides contribute no windows.
                line_hash, indices, contained = evidence[int(number)]
                indices = {index for index in indices if self.matcher.matcher.owners[index] == owner}
                matched = {self.windows[index] for index in indices}
                if not matched:
                    continue
                windows.update(matched)
                hashes.add(line_hash)
                sides.add(side)
                hit["path"] = platform_display(name) if name is not None else None
                public_scope &= metadata or loopcut_path(name)
                allow_scope &= not metadata and not loopcut_path(name) and indices <= contained
            hit.update(side=",".join(sorted(sides)), line_sha256s=sorted(hashes),
                       line_sha256=next(iter(hashes)) if len(hashes) == 1 else None,
                       fingerprint_sha256=sorted(digest(window.decode("utf-8")) for window in windows))
            if windows and public_scope and all(self.public(window) for window in windows):
                hit.update({"class": "public-in-base", "reason": "all matching windows occur in public Loopcut base"})
            elif windows and allow_scope and all(value in self.allowlist for value in hashes):
                hit.update({"class": "allowlisted", "reason": "; ".join(sorted({self.allowlist[value] for value in hashes}))})
            else:
                hit["reason"] = "matching windows are not all public-in-base or exact-line allowlisted"
        except (ReleaseError, OSError, UnicodeError, KeyError, ValueError, AttributeError,
                zipfile.BadZipFile, RuntimeError, NotImplementedError):
            hit["reason"] = "could not recover or verify platform hit"
        self.hits[location] = hit
        return hit


def classify_platform_pass(result, classifier):
    # The platform prints the same owner/location once per matching window.
    # Preserve those counts without serializing its full evidence set repeatedly.
    occurrences = Counter(result["locations"])
    result["hits"] = [dict(classifier.classify(location), occurrences=count)
                      for location, count in occurrences.items()]
    counts = Counter()
    for hit in result["hits"]:
        counts[hit["class"]] += hit["occurrences"]
    result["class_counts"] = {name: counts[name] for name in ("public-in-base", "allowlisted", "unexplained")}


def check_platform_mode(run, command, classifier, temporary):
    first = {}
    run_platform_check(first, command)
    if "exit_code" not in first:
        run.update(first)
        return
    classify_platform_pass(first, classifier)
    run.update(first)
    run["passes"] = [first]
    if first["exit_code"] != 1:
        return
    owners = {}
    for hit in first["hits"]:
        owners.setdefault(hit["owner"], set()).add(hit["class"])
    allowed = [{"owner": owner, "classes": sorted(classes)} for owner, classes in sorted(owners.items())
               if "unexplained" not in classes]
    run["allowed_owners"] = allowed
    if not allowed:
        return
    # Even mixed runs can verify explained owners, but any unexplained hit blocks.
    with tempfile.TemporaryDirectory(prefix="owners-", dir=temporary) as directory:
        path = Path(directory) / "allow.txt"
        path.write_text("".join(item["owner"] + "\n" for item in allowed), encoding="utf-8")
        second = {}
        run_platform_check(second, [*command, "--allow", str(path)])
    if "locations" in second:
        classify_platform_pass(second, classifier)
    run["passes"].append(second)
    run["exit_code"] = second.get("exit_code")
    run["reason"] = second["reason"]
    run["status"] = ("error" if second.get("exit_code") not in (0, 1) else
                     "clean" if second["exit_code"] == 0 and second["status"] == "clean"
                     and not first["class_counts"]["unexplained"] else "blocked")


def archive_addon(repo, snapshot, temporary):
    # leak-check can target an unsanitized commit, unlike build_snapshot.
    if any(bytecode_path(name) for name in tree_entries(repo, snapshot) if name.startswith(ADDON_PATH + "/")):
        raise ReleaseError("Cannot archive add-on containing Python bytecode/cache paths")
    path = temporary / "shipped-addon.zip"
    path.write_bytes(git(repo, "archive", "--format=zip", snapshot, "--", ADDON_PATH))
    return path


def scan_release_artifacts(repo, base, snapshot, args, report, scanner):
    """Temporary targets never register a worktree or write refs in the source repo."""
    runs = report["platform_runs"]
    runs[1]["command"][-1] = f"{base}..{snapshot}"
    with tempfile.TemporaryDirectory(prefix="loopcut-release-platform-") as directory:
        temporary = Path(directory).resolve()
        shipped_zip = scanner.check(archive_addon, repo, snapshot, temporary, context="shipped add-on ZIP")
        if shipped_zip is not None:
            report["shipped_addon_zip_files"] = scanner.check(scan_zip, shipped_zip, scanner,
                                                              context="shipped add-on ZIP")
        if args.no_leak_check:
            return
        script = args.leak_check.resolve()
        if not script.is_file():
            for run in runs:
                run["reason"] = "platform leak-check script is missing"
            return
        command = [str(script), "--prompts-dir", str(Path(args.prompts_dir).resolve()),
                   "--skills-dir", str(Path(args.skills_dir).resolve())]
        archives = [*args.extension_zips, *([shipped_zip] if shipped_zip is not None else [])]
        classifier = PlatformClassifier(repo, base, args, temporary, archives, scanner.allowlist)
        for run in runs:
            try:
                if run["mode"] == "tree":
                    export_tree(repo, snapshot, temporary)
                    target = temporary / "tree"
                    # git archive emits empty directories for gitlinks. Give the
                    # checker an isolated index so it can report them as SKIPs.
                    links = [(name, entry) for name, entry in tree_entries(repo, snapshot).items()
                             if entry[0] == "160000"]
                    if links:
                        git(target, "init")
                        git(target, "update-index", "-z", "--index-info", input=b"".join(
                            f"160000 {entry[2]}\t".encode() + os.fsencode(name) + b"\0" for name, entry in links))
                    arguments = [str(target)]
                elif run["mode"] == "history":
                    target = temporary / "history"
                    target.mkdir()
                    git(target, "init")
                    git(target, "fetch", "--no-tags", str(Path(repo).resolve()), snapshot, base)
                    arguments = [str(target), f"{base}..{snapshot}"]
                else:
                    if shipped_zip is None:
                        run["reason"] = "shipped add-on ZIP could not be built"
                        continue
                    target = temporary / "empty"
                    target.mkdir()
                    arguments = []
                    for path in (shipped_zip, *args.extension_zips):
                        arguments.extend(["--zip", str(path.resolve())])
                    arguments.append(str(target))
                check_platform_mode(run, [*command, *arguments], classifier, temporary)
            except (ReleaseError, OSError, UnicodeError, tarfile.TarError) as exc:
                detail = str(exc) if isinstance(exc, ReleaseError) else type(exc).__name__
                run["reason"] = f"platform target preparation failed ({detail})"


def output_directory(path):
    if path is None:
        return Path(tempfile.mkdtemp(prefix="loopcut-public-release-"))
    out = Path(path).resolve()
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ReleaseError("--out must be a new or empty directory; existing files are never overwritten")
    if any(part.lower() == ".git" for part in out.parts):
        raise ReleaseError("--out must be outside Git metadata")
    out.mkdir(parents=True, exist_ok=True)
    return out


def write_report(out, report, scanner):
    report["errors"].extend(scanner.errors)
    runs = report.get("platform_runs", [])
    incomplete_runs = [run for run in runs if run["required"] and run["exit_code"] not in (0, 1)]
    for run in incomplete_runs:
        report["errors"].append(f"Platform {run['mode']}: {run['status']} — {run['reason']}")
    report["scan_complete"] = report.get("scan_complete", False) and not scanner.incomplete and not incomplete_runs
    report["platform_hit_count"] = sum(run["hit_count"] or 0 for run in runs)
    report["platform_skip_count"] = sum(run["skip_count"] or 0 for run in runs)
    report["platform_class_counts"] = {name: sum(run.get("class_counts", {}).get(name, 0) for run in runs)
                                       for name in ("public-in-base", "allowlisted", "unexplained")}
    platform_blocked = any(run["required"] and run["status"] == "blocked" for run in runs)
    report["scan_summary"] = scanner.summary()
    report["submodules"] = scanner.submodules
    report["hits"] = [asdict(hit) for hit in scanner.hits]
    observed = sum(not hit.allowed for hit in scanner.hits)
    report["observed_unexplained_hits"] = observed
    report["unexplained_hits"] = observed if report["scan_complete"] else None
    report["status"] = "error" if report["errors"] or not report["scan_complete"] else (
        "blocked" if observed or platform_blocked else "clean")
    report.update(redact_report(report))
    with (out / "report.json").open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, ensure_ascii=True)
        stream.write("\n")
    completion = "complete" if report["scan_complete"] else "INCOMPLETE — results are partial"
    hit_count = str(observed) if report["scan_complete"] else f"unknown (scan incomplete; {observed} observed so far)"
    lines = ["# Public release leak check", "", f"Scan: **{completion}**",
             f"Status: **{report['status']}**", f"Unexplained hits: {hit_count}", "", POLICY, "",
             f"Base: `{report.get('base', '')}`", f"Snapshot: `{report.get('snapshot', '')}`",
             "", "## Scan coverage by category", "",
             "Files count distinct scope/path/commit/side combinations; lines count inspected text, "
             "including both history hunk sides. secret-file and private_path check filenames only. "
             "Tree content checks changed blobs, the known withheld modules and all public Python tests; "
             "unchanged filenames are checked. "
             "Private fingerprint checks depend on the source coverage below.", ""]
    for item in report["scan_summary"]:
        lines.append(f"- {item['category']}: {item['files']} files, {item['lines']} lines scanned.")
    if "build_inputs" in report:
        inputs = report["build_inputs"]
        lines.extend(["", "## Build inputs", "", f"Working tree: {json.dumps(inputs['root'])}",
                      f"HEAD: `{inputs['head']}`; source: `{inputs['source']}`.",
                      "Includes untracked and ignored files. Release configuration (Freestyle enabled); "
                      "external dependency libraries and generated build products are outside this pass.",
                      f"Directories: {json.dumps(inputs['directories'])}.",
                      f"Explicit FILES exceptions: {json.dumps(inputs['explicit_files'])}.",
                      f"Files scanned: {inputs['files_scanned']}; excluded files: {len(inputs['excluded'])}."])
        lines.extend("WARNING: " + warning for warning in inputs["warnings"])
        lines.extend(f"- EXCLUDED {json.dumps(item['file'])}; SHA-256: `{item['sha256']}`."
                     for item in inputs["excluded"])
    if runs:
        lines.extend(["", "## Platform leak check", "",
                      "Commands use placeholders for private sources and temporary targets. "
                      "Hit counts are observed location lines; error results may be partial. "
                      "Tree mode scans the whole snapshot fork. All modes run non-strict: "
                      "gitlinks and unresolved LFS pointers remain unscanned and are counted as SKIPs, not hits. "
                      "A clean result does not certify that skipped content. "
                      "Public-in-base uses only windows from loopcut/ and scripts/addons_core/loopcut/; "
                      "upstream file hits require exact-line allowlisting. History combines both blob sides.", ""])
        for run in report["platform_runs"]:
            lines.extend([f"### {run['mode']}", "", f"Command: `{shlex.join(run['command'])}`", "",
                          f"Status: **{run['status']}**; exit code: {run['exit_code']}; "
                          f"hits: {run['hit_count'] if run['hit_count'] is not None else 'unknown'}; "
                          f"SKIPs: {run['skip_count']} ({json.dumps(run['skip_counts'], sort_keys=True)})."])
            if run["reason"]:
                lines.append(f"Reason: {run['reason']}.")
            for number, result in enumerate(run["passes"], 1):
                lines.extend(["", f"Pass {number}: exit {result.get('exit_code')}; "
                              f"classes: {json.dumps(result.get('class_counts', {}), sort_keys=True)}; "
                              f"SKIPs: {result.get('skip_count')} ({json.dumps(result.get('skip_counts', {}), sort_keys=True)})."])
                for item in result.get("skips", []):
                    lines.append(f"- SKIP {item['kind']} {json.dumps(item['location'])}.")
                for hit in result.get("hits", []):
                    lines.append(f"- {json.dumps(hit['location'])}: {hit['class']}; {hit['reason']}; "
                                 f"occurrences: {hit['occurrences']}; "
                                 f"side: {hit['side'] or 'n/a'}; line SHA-256: {json.dumps(hit['line_sha256s'])}; "
                                 f"fingerprint SHA-256: {json.dumps(hit['fingerprint_sha256'])}.")
            lines.extend(["", "Owners allowed in pass 2:"])
            for item in run["allowed_owners"]:
                lines.append(f"- {json.dumps(item['owner'])}: {', '.join(item['classes'])}.")
            if len(run["passes"]) < 2:
                lines.append("Pass 2 not run (no explained owners to suppress or pass 1 did not exit 1).")
    lines.extend(["", "## Private harness blockers", ""])
    for category in ("private_path", "private_import", "local_path"):
        hits = [hit for hit in scanner.hits if hit.category == category]
        examples = ", ".join(json.dumps(name) for name in sorted({hit.file for hit in hits})[:5])
        lines.append(f"- {category}: {len(hits)} blocking hits" + (f"; first paths: {examples}." if hits else ".")
                     + (" Scan incomplete; count is partial." if not report["scan_complete"] else ""))
    lines.extend(["", "## Submodules", ""])
    for item in report["submodules"]:
        state = "differs from base; ERROR" if item["changed"] else "unchanged from base"
        lines.append(f"- {json.dumps(item['file'])}: {item['status']}; {state}.")
    lines.extend(["", "## Private source coverage", ""])
    for item in report.get("coverage", []):
        lines.append(f"- {json.dumps(item['path'])}: {item['status']}; {item['files']} text files, "
                     f"{item['fingerprints']} fingerprints, {item['binary_files']} binary sources skipped, "
                     f"{item['open_fingerprints_dropped']} fingerprints dropped as open")
    lines.extend(["", "Missing private sources do not fail the check, but reduce its coverage.",
                  "Deleted files are checked through history. Exported symlinks are text files."])
    for title, withheld in (("Known expected blocker: withheld prompts", True), ("Other findings", False)):
        lines.extend(["", "## " + title, ""])
        hits = [hit for hit in scanner.hits if (hit.category == "withheld-prompt") == withheld]
        if not hits:
            lines.append("No findings observed; scan incomplete." if not report["scan_complete"] else "No hits.")
        for hit in hits:
            state = "reviewed: " + hit.reason if hit.allowed else "BLOCKED"
            location = f"{hit.scope} {json.dumps(hit.file)}:{hit.line}"
            if hit.commit:
                location += f" ({hit.commit}, {hit.side})"
            lines.append(f"- {location} — {hit.category}: {hit.detail}; {state}. "
                         f"Fingerprint SHA-256: `{hit.fingerprint_sha256}`; line SHA-256: `{hit.line_sha256}`.")
            print(f"{location}: {hit.category} [{state}]")
    if report["errors"]:
        lines.extend(["", "## Incomplete checks", "", *["- " + error for error in report["errors"]]])
    with (out / "report.md").open("x", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")


def parser(leak_only):
    command = argparse.ArgumentParser(description=__doc__)
    if leak_only:
        command.add_argument("revision", help="BASE..TIP or a commit (relative to --base)")
    else:
        command.add_argument("--dry-run", required=True, action="store_true")
        command.add_argument("--source", default="track/client")
        command.add_argument("--exclude", type=Path, default=HERE / "public_release_exclude.txt")
        command.add_argument("--leak-check", type=Path, default=default_leak_check(),
                             help="platform checker (default: LOOPCUT_RELEASE_LEAK_CHECK or local platform script)")
        command.add_argument("--no-leak-check", action="store_true", help="skip platform checks (tests only)")
    command.add_argument("--base", default="origin/main")
    command.add_argument("--build-root", type=Path,
                         help="checkout whose working-tree install inputs are scanned (default: this repository)")
    command.add_argument("--out", help="new or empty output directory; defaults to a temporary directory")
    command.add_argument("--zip", dest="extension_zips", type=Path, action="append", default=[],
                         help="additional extension ZIP to scan (repeatable)")
    command.add_argument("--allowlist", type=Path, default=HERE / "public_release_allowlist.txt")
    command.add_argument("--skills-dir", default=os.getenv("LOOPCUT_RELEASE_SKILLS_DIR", str(PRIVATE_WEB / "skills")))
    command.add_argument("--prompts-dir", default=os.getenv("LOOPCUT_RELEASE_PROMPTS_DIR", str(PRIVATE_WEB / "prompts")))
    command.add_argument("--research-dir", default=os.getenv(
        "LOOPCUT_RELEASE_RESEARCH_DIR", str(PRIVATE_HOME / RESEARCH_SOURCES) if RESEARCH_SOURCES else ""))
    command.add_argument("--economics-file", default=os.getenv("LOOPCUT_RELEASE_ECONOMICS_FILE", str(PRIVATE_WEB / "scripts/economics.mts")))
    command.add_argument("--cost-doc", dest="cost_docs", action="append", default=[], help="additional private cost document (repeatable)")
    return command


def main(argv=None, repo=REPO):
    started = time.monotonic()
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "allowlist-add":
        return allowlist_add(argv[1:], repo)
    leak_only = bool(argv and argv[0] == "leak-check")
    args = parser(leak_only).parse_args(argv[1:] if leak_only else argv)
    report = {"mode": "leak-check" if leak_only else "dry-run", "policy": POLICY, "errors": []}
    if not leak_only:
        report["platform_runs"] = platform_runs(args)
    scanner = Scanner([], {})
    out = None
    try:
        out = output_directory(args.out)
        scanner.allowlist = load_allowlist(args.allowlist)
        scanner.samples, report["coverage"] = private_samples(args)
        if leak_only:
            revision, base = args.revision, args.base
            if ".." in revision:
                parts = revision.split("..")
                if len(parts) != 2 or not all(parts) or "..." in revision:
                    raise ReleaseError("Use a complete two-dot range BASE..TIP")
                base, revision = parts
            base, snapshot = resolve(repo, base), resolve(repo, revision)
        else:
            rules = exclusions(args.exclude) + research_exclusions(RESEARCH_FOLDERS)
            base, source, snapshot, dropped = build_snapshot(repo, args.source, args.base, rules)
            report.update(source=source, excluded_paths=dropped, private_ref=DRY_RUN_REF)
            print(f"Snapshot: {snapshot} ({DRY_RUN_REF})", flush=True)
        report.update(base=base, snapshot=snapshot)
        scanner.check(scan_build_inputs, args.build_root or Path(repo), snapshot if leak_only else source,
                      scanner, report, context="build inputs")
        changed = scanner.check(scan_tree, repo, base, snapshot, scanner, context="tree")
        if changed is not None:
            report["changed_files"] = changed
        scanner.check(scan_history, repo, base, snapshot, scanner, context="history")
        if args.extension_zips:
            report["zip_files"] = sum(scanner.check(scan_zip, path, scanner, context="zip") or 0
                                      for path in args.extension_zips)
        if not leak_only:
            scan_release_artifacts(repo, base, snapshot, args, report, scanner)
        report["scan_complete"] = not scanner.incomplete
        if not leak_only and args.out is not None and changed is not None:
            scanner.check(export_tree, repo, snapshot, out, context="export")
    except (ReleaseError, OSError, UnicodeError, tarfile.TarError) as exc:
        # Exception messages from file/Git operations may contain private input.
        error = str(exc) if isinstance(exc, ReleaseError) else f"{type(exc).__name__}: check could not complete"
        report["errors"].append(error)
        if out is None:
            print(redact(error), file=sys.stderr)
            return 2
    try:
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        write_report(out, report, scanner)
    except OSError:
        print("Could not write release reports", file=sys.stderr)
        return 2
    result = f"{report['unexplained_hits']} unexplained hits" if report["scan_complete"] else "scan INCOMPLETE; results are partial"
    print(f"Report: {redact(str(out / 'report.md'))} ({report['status']}; {result}; "
          f"{report['platform_hit_count']} platform hits observed)")
    return {"error": 2, "blocked": 1, "clean": 0}[report["status"]]


if __name__ == "__main__":
    sys.exit(main())
