"""Committed contract 0.3 metadata; public tests never need hosted bodies."""

import json
import re
from functools import lru_cache
from pathlib import Path
from unittest import mock


@lru_cache(maxsize=1)
def built_index():
    return json.loads((Path(__file__).parent / 'fixtures/craft-index.json').read_text(encoding='utf-8'))


def index_patch():
    return mock.patch('loopcut.cloud.hosted_index', return_value=built_index())


def selected_ids(text):
    return [identity for ids in re.findall(r'<loopcut-skills ids="([^"]+)"/>', text)
            for identity in ids.split(',')]


def select(role, brief, notes='', **options):
    from loopcut import skills
    options.setdefault('skill_index', built_index()['skills'])
    return skills.skills_for(role, brief, notes, **options)


def topics_for(role, brief, notes='', **options):
    from loopcut import skills
    chosen = []
    original = skills._topics

    def capture(*args, **kwargs):
        result = original(*args, **kwargs)
        chosen.extend(result)
        return result

    with mock.patch.object(skills, '_topics', side_effect=capture):
        select(role, brief, notes, **options)
    return chosen


def expanded_chars(text, index=None):
    """Expected length from the committed per-core swap-combination table."""
    entries = {e['id']: e for e in (index or built_index()['skills'])}

    def size(match):
        ids = match[1].split(',')
        total, pieces = 0, 0
        for identity in ids:
            entry = entries[identity]
            if entry.get('part') == 'swap':
                continue
            swaps = [i for i in ids if entries[i].get('part') == 'swap'
                     and entries[i].get('craft') == entry.get('craft')]
            total += (entry['swapped_chars'][','.join(swaps)]
                      if entry.get('part') == 'core' and swaps else entry['chars'])
            pieces += 1
        return 'x' * (total + max(0, pieces - 1))

    return len(re.sub(r'<loopcut-skills ids="([^"]+)"/>', size, text))
