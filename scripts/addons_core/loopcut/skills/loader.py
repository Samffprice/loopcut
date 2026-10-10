"""Load skill files without YAML or network dependencies.

The format deliberately supports only unquoted, single-line scalars, positive version integers,
nonnegative order integers, and inline lists for roles, requires_tools and everyday. Regex fields
are raw strings (contract 0.3 strips replaces, as this parser always has): backslashes, colons, brackets and trailing spaces are never YAML-decoded. A file
adds exactly one final LF to its body; parsing removes only that LF. Built-ins are cached once,
while an explicit extra directory is reread so harness edits need no process restart.

Fetched entries may be flat dictionaries or {front_matter, body, sha256}. The caller owns fetching
and checking the file hash before handing them here; without the original file bytes its SHA-256
cannot be reconstructed from parsed metadata. No HTTP or premium text lives in this module.
"""

import hashlib
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

CONTRACT_KEYS = frozenset({'id', 'version', 'title', 'when', 'roles', 'requires_tools', 'tier', 'kind'})
LOCAL_KEYS = frozenset({'craft', 'part', 'name', 'order', 'everyday', 'words', 'leads', 'replaces', 'symbol'})
LIST_KEYS = frozenset({'roles', 'requires_tools', 'everyday'})
INTEGER_KEYS = frozenset({'version', 'order'})
REGEX_KEYS = frozenset({'words', 'leads'})
ROLES = frozenset({'single', 'workspace', 'orchestrator', 'worker'})
CRAFT_NAMES = ('modeling', 'materials', 'lighting', 'camera', 'animation')
PARTS = frozenset({'core', 'topic', 'assets', 'swap', 'shot', 'single', 'fragment'})
ROOT = Path(__file__).resolve().parent


class SkillError(ValueError):
    """A malformed file or fetched entry, with its source in the error message."""


@dataclass(frozen=True)
class Skill:
    front_matter: Mapping
    body: str
    sha256: str
    source: str = ''
    built_in: bool = False

    @property
    def id(self):
        return self.front_matter['id']

    @property
    def version(self):
        return self.front_matter['version']


def _fail(source, message):
    raise SkillError(f'{source}: {message}')


def _validate(fields, source):
    missing = CONTRACT_KEYS - fields.keys()
    if missing:
        _fail(source, f"missing contract keys: {', '.join(sorted(missing))}")
    unknown = fields.keys() - CONTRACT_KEYS - LOCAL_KEYS
    if unknown:
        _fail(source, f"unsupported keys: {', '.join(sorted(unknown))}")
    for key, value in fields.items():
        if key in LIST_KEYS:
            if not isinstance(value, (list, tuple)) or any(not isinstance(v, str) or not v for v in value):
                _fail(source, f'{key} must be a list of nonempty strings')
            if len(value) != len(set(value)):
                _fail(source, f'{key} has duplicate items')
        elif key in INTEGER_KEYS:
            if type(value) is not int or value < int(key == 'version'):
                _fail(source, f'{key} must be an integer >= {int(key == "version")}')
        elif not isinstance(value, str) or not value or '\n' in value or '\r' in value:
            _fail(source, f'{key} must be a nonempty single-line string')
    if not re.fullmatch(r'[a-z0-9_-]+(?:/[a-z0-9_-]+)*', fields['id']):
        _fail(source, 'id must be a slash-separated skill identifier')
    if not fields['roles'] or set(fields['roles']) - ROLES:
        _fail(source, 'roles must contain contract agent roles')
    if fields['kind'] not in ('gen', 'assist') or fields['tier'] not in ('free', 'paid'):
        _fail(source, 'kind must be gen/assist and tier must be free/paid')
    for tool in fields['requires_tools']:
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', tool):
            _fail(source, f'invalid required tool: {tool!r}')
    if 'craft' in fields and fields['craft'] not in CRAFT_NAMES:
        _fail(source, 'craft must name an existing worker craft')
    if 'part' in fields and fields['part'] not in PARTS:
        _fail(source, 'unknown part')
    part = fields.get('part')
    required = {'core': ('craft',), 'assets': ('craft',), 'topic': ('craft', 'words'),
                'swap': ('craft', 'words', 'replaces'), 'shot': ('craft',),
                'single': ('words',), 'fragment': ('symbol',)}.get(part, ())
    for key in required:
        if key not in fields:
            _fail(source, f'{part} requires {key}')
    for key in REGEX_KEYS & fields.keys():
        try:
            re.compile(fields[key], re.I)
        except re.error as ex:
            raise SkillError(f'{source}: invalid {key} regex: {ex}') from ex
    return MappingProxyType({key: tuple(value) if key in LIST_KEYS else value for key, value in fields.items()})


def _value(key, raw, source):
    if key in REGEX_KEYS:
        return raw
    value = raw.strip()
    if key in LIST_KEYS:
        if not value.startswith('[') or not value.endswith(']'):
            _fail(source, f'{key} requires an inline [a, b] list')
        content = value[1:-1]
        items = [item.strip() for item in content.split(',')] if content.strip() else []
        if any(not re.fullmatch(r'[A-Za-z0-9_.-]+', item) for item in items):
            _fail(source, f'malformed {key} list')
        return items
    if key in INTEGER_KEYS:
        if not re.fullmatch(r'0|[1-9][0-9]*', value):
            _fail(source, f'{key} must be an integer')
        return int(value)
    if not value or value in ('|', '>') or value.startswith(('[', '{', "'", '"', '#')):
        _fail(source, f'{key} requires an unquoted single-line scalar')
    return value


def parse_skill(text, source='<skill>', *, built_in=False):
    """Parse a complete markdown file, preserving every body byte except its final format LF."""
    if not isinstance(text, str) or not text.startswith('---\n'):
        _fail(source, 'expected opening --- on its own LF-terminated line')
    header, separator, body = text[4:].partition('\n---\n')
    if not separator:
        _fail(source, 'missing closing front-matter delimiter')
    if not body.endswith('\n'):
        _fail(source, 'body must end with the file format LF')
    fields = {}
    for line in header.split('\n'):
        match = re.fullmatch(r'([a-z][a-z0-9_]*): (.*)', line)
        if match is None:
            _fail(source, f'malformed front-matter line: {line!r}')
        key, raw = match.groups()
        if key in fields:
            _fail(source, f'duplicate key: {key}')
        fields[key] = _value(key, raw, source)
    metadata = _validate(fields, source)
    return Skill(metadata, body[:-1], hashlib.sha256(text.encode('utf-8')).hexdigest(), str(source), built_in)


def read_skill(path, *, built_in=False):
    path = Path(path)
    # read_text's universal-newline conversion would silently alter skill bodies.
    return parse_skill(path.read_bytes().decode('utf-8'), str(path), built_in=built_in)


@lru_cache(maxsize=1)
def builtins():
    """Read the packaged files once. An empty baseline is valid; malformed files are fatal."""
    files = sorted(ROOT.rglob('*.md'))
    records = tuple(read_skill(path, built_in=True) for path in files)
    ids = [record.id for record in records]
    if len(ids) != len(set(ids)):
        _fail(ROOT, 'duplicate built-in skill id')
    return records


def _skill_from_dict(entry, source):
    if not isinstance(entry, dict):
        _fail(source, 'entry must be a dictionary')
    fields = entry.get('front_matter')
    if fields is None:
        fields = {key: value for key, value in entry.items() if key not in ('body', 'sha256')}
    if not isinstance(fields, dict):
        _fail(source, 'front_matter must be a dictionary')
    body, digest = entry.get('body'), entry.get('sha256')
    if not isinstance(body, str):
        _fail(source, 'body must be a string')
    if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
        _fail(source, 'sha256 must be a lowercase hexadecimal SHA-256')
    return Skill(_validate(fields, source), body, digest, source)


def skills_from_dicts(entries):
    """Validate already-fetched entries, warning and skipping each malformed entry."""
    result = []
    for n, entry in enumerate(entries):
        try:
            result.append(_skill_from_dict(entry, f'fetched skill {n}'))
        except SkillError as ex:
            print(f"Loopcut: skipping invalid server skill: {ex}")
    return tuple(result)


def merge_skills(baseline, extras=()):
    """Highest version per id wins; equal versions prefer built-ins, then the first source."""
    merged = {}
    for skill in (*baseline, *extras):
        old = merged.get(skill.id)
        if old is None or skill.version > old.version or (
                skill.version == old.version and skill.built_in and not old.built_in):
            merged[skill.id] = skill
    return tuple(merged.values())


def eligible(skill, *, role=None, available_tools=(), no_generation=False):
    fields = skill.front_matter
    return ((role is None or role in fields['roles'])
            and not (skill.source.startswith('fetched skill ') and not fields.get('words')
                     and not fields['requires_tools'])
            and set(fields['requires_tools']).issubset(available_tools)
            and not (no_generation and fields['kind'] == 'gen'))


def local_skills(extra_dir=None):
    """Optional directory records, before id merging and eligibility filtering."""
    strict = extra_dir is not None
    if extra_dir is None:
        from .. import config
        extra_dir = config.skills_dir()
    extras = []
    if extra_dir is not None:
        directory = Path(extra_dir)
        if not directory.is_dir():
            if strict:
                _fail(directory, 'extra skill directory does not exist')
            print(f"Loopcut: skipping extra skill directory {directory}: directory does not exist")
        else:
            for path in sorted(directory.rglob('*.md')):
                try:
                    extras.append(read_skill(path))
                except (SkillError, OSError, UnicodeError) as ex:
                    if strict:
                        raise
                    print(f"Loopcut: skipping extra skill file {path}: {ex}")
    return tuple(extras)


def load_skills(extra_dir=None, *, server_skills=(), role=None, available_tools=(), no_generation=False):
    """Merge before filtering; an unavailable newer version does not fall back to an older one.

    None uses config's LOOPCUT_SKILLS_DIR lookup, warning and skipping bad optional sources.
    An explicit directory bypasses that setting and stays strict for harness validation.
    Directory files are sorted by path; server entries follow them. Added topics are placed after
    baseline topics by the selector, even when their optional order is smaller.
    """
    if role is not None and role not in ROLES:
        raise SkillError(f'unknown agent role: {role!r}')
    extras = list(local_skills(extra_dir))
    extras.extend(skills_from_dicts(server_skills))
    tools = frozenset(available_tools)
    return tuple(skill for skill in merge_skills(builtins(), extras)
                 if eligible(skill, role=role, available_tools=tools, no_generation=no_generation))
