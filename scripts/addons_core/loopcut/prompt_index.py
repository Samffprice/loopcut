"""Proposal 006 metadata only: validation, harness override and expanded character counts."""

import json
import os
import re
import threading
from copy import deepcopy
from pathlib import Path

ID = r'[a-z0-9_-]+(?:/[a-z0-9_-]+)*'
PLACEHOLDER = re.compile(r'<loopcut-skills ids="(' + ID + r'(?:,' + ID + r')*)"/>')
_cached = None
_lock = threading.Lock()


class IndexError(ValueError):
    """An unusable hosted prompt index; never fall back to bundled worker text."""


def _chars(value):
    return type(value) is int and value >= 0


def validate(value):
    if not isinstance(value, dict) or not isinstance(value.get('skills'), list):
        raise IndexError('Loopcut prompt index: expected an object with a skills list')
    prompts = value.get('prompts')
    if prompts is not None:
        if (not isinstance(prompts, dict) or not isinstance(prompts.get('version'), str)
                or not prompts['version'] or not isinstance(prompts.get('items'), dict)
                or any(not isinstance(item, dict) or not _chars(item.get('chars'))
                       for item in prompts['items'].values())):
            raise IndexError('Loopcut prompt index: invalid prompts version/items/chars')
    seen = set()
    for entry in value['skills']:
        if not isinstance(entry, dict) or not isinstance(entry.get('id'), str) or not re.fullmatch(ID, entry['id']):
            raise IndexError('Loopcut prompt index: invalid skill id')
        if entry['id'] in seen:
            raise IndexError(f"Loopcut prompt index: duplicate skill {entry['id']}")
        seen.add(entry['id'])
        for key in ('roles', 'requires_tools', 'everyday'):
            if key in entry and (not isinstance(entry[key], (list, tuple))
                                 or any(not isinstance(v, str) or not v for v in entry[key])):
                raise IndexError(f"Loopcut prompt index: {entry['id']} has invalid {key}")
        allowed = {'roles': {'single', 'workspace', 'orchestrator', 'worker'},
                   'craft': {'modeling', 'materials', 'lighting', 'camera', 'animation'},
                   'part': {'core', 'topic', 'assets', 'swap', 'shot', 'single', 'fragment'},
                   'kind': {'gen', 'assist'}, 'tier': {'free', 'paid'}}
        for key, choices in allowed.items():
            if key in entry and (any(v not in choices for v in entry[key]) if key == 'roles'
                                 else not isinstance(entry[key], str) or entry[key] not in choices):
                raise IndexError(f"Loopcut prompt index: {entry['id']} has invalid {key}")
        for key in ('chars', 'order'):
            if key in entry and not _chars(entry[key]):
                raise IndexError(f"Loopcut prompt index: {entry['id']} has invalid {key}")
        if 'name' in entry and (not isinstance(entry['name'], str) or not entry['name']
                                or '\n' in entry['name'] or '\r' in entry['name']):
            raise IndexError(f"Loopcut prompt index: {entry['id']} has invalid name")
        for key in ('words', 'leads'):
            if key not in entry:
                continue
            pattern = entry[key]
            if not isinstance(pattern, str) or len(pattern) > 1024:
                raise IndexError(f"Loopcut prompt index: {entry['id']} has invalid {key}")
            try:
                re.compile(pattern, re.I)
            except re.error as ex:
                raise IndexError(f"Loopcut prompt index: {entry['id']} has invalid {key}: {ex}") from ex
        if entry.get('part') in ('core', 'topic', 'assets', 'swap', 'shot') and 'craft' not in entry:
            raise IndexError(f"Loopcut prompt index: {entry['id']} is missing craft")
        if entry.get('part') in ('topic', 'swap', 'single') and not entry.get('words'):
            raise IndexError(f"Loopcut prompt index: {entry['id']} is missing words")
        sizes = entry.get('swapped_chars', {})
        if (not isinstance(sizes, dict) or any(not isinstance(k, str)
                or not re.fullmatch(ID + r'(?:,' + ID + r')*', k) or not _chars(v) for k, v in sizes.items())):
            raise IndexError(f"Loopcut prompt index: {entry['id']} has invalid swapped_chars")
    return deepcopy({'prompts': prompts, 'skills': value['skills']})


def override():
    """Read once per path/mtime change. An explicitly configured bad file is fatal at use."""
    global _cached
    path = os.environ.get('LOOPCUT_PROMPT_INDEX')
    if not path:
        return None
    try:
        with _lock:
            source = Path(path)
            stamp = (path, source.stat().st_mtime_ns)
            if _cached is None or _cached[0] != stamp:
                try:
                    value = validate(json.loads(source.read_text(encoding='utf-8')))
                    if value['prompts'] is None:
                        raise IndexError('missing prompts')
                    _cached = (stamp, value, None)
                except (OSError, UnicodeError, ValueError) as ex:
                    _cached = (stamp, None, str(ex))
            if _cached[2] is not None:
                raise IndexError(_cached[2])
            return deepcopy(_cached[1])
    except (OSError, UnicodeError, ValueError) as ex:
        raise IndexError(f'LOOPCUT_PROMPT_INDEX {path}: {ex}') from ex


def prompt_chars(index, prompt_id):
    try:
        return index['prompts']['items'][prompt_id]['chars']
    except (KeyError, TypeError) as ex:
        raise IndexError(f'Loopcut prompt index is missing {prompt_id} prompt chars') from ex


def expansion_chars(ids, entries):
    """Count a placeholder using post-swap core sizes and the server's LF join."""
    sizes, cores, swaps = [], {}, {}
    for identity in ids:
        try:
            entry = entries[identity]
            size = entry['chars']
        except KeyError as ex:
            raise IndexError(f'Loopcut prompt index is missing skill chars: {identity}') from ex
        craft = entry.get('craft')
        if entry.get('part') == 'swap':
            if craft not in cores:
                raise IndexError(f'Loopcut skill swap has no preceding core: {identity}')
            position, core = cores[craft]
            swaps.setdefault(craft, []).append(identity)
            key = ','.join(swaps[craft])
            try:
                sizes[position] = core['swapped_chars'][key]
            except KeyError as ex:
                raise IndexError(f"Loopcut prompt index: {core['id']} missing swapped_chars[{key}]") from ex
        else:
            if entry.get('part') == 'core':
                cores[craft] = (len(sizes), entry)
            sizes.append(size)
    return sum(sizes) + max(0, len(sizes) - 1)
