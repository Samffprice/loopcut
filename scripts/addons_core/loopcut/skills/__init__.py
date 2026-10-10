"""Select craft skills over Loopcut's served index (contract 0.3).

Hosted bodies stay on the service; local skills are inlined. The shared file loader
also supports open baseline skills, LOOPCUT_SKILLS_DIR and distilled drafts.
Single/workspace notes use fetched and local skills through that loader.
"""

import re
from dataclasses import dataclass

from . import loader
from .loader import SkillError, load_skills, skills_from_dicts

MAX_CHARS = 3060    # About 900 tokens at context.CHARS_PER_TOKEN (3.4 characters a token).
PAINT_MAX_CHARS = 5920  # The painterly recipe on a modeling core, about 1,740 tokens.
MAX_TOPICS = 3
SHOT_MAX_CHARS = MAX_CHARS + 900


@dataclass(frozen=True)
class Topic:
    name: str
    words: str
    text: "_Piece"
    leads: str = ''
    request_match: bool = False

    def matches(self, text: str) -> int:
        return len(re.findall(rf'\b(?:{self.words})', text, re.I))


@dataclass(frozen=True)
class Craft:
    core: "_Piece"
    topics: tuple
    everyday: tuple
    assets: "_Piece"


def _ordered(records):
    return sorted(records, key=lambda s: (s.front_matter.get('order', 0), s.id))


def words_of(brief) -> str:
    """Every string in a brief, joined, except ownership, identity and aspect names."""
    found = []

    def walk(value, key=''):
        if key in ('owns', 'owned', 'prefix', 'id', 'aspects', 'role'):
            return
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, dict):
            for k, v in value.items():
                walk(v, str(k))
        elif isinstance(value, (list, tuple)):
            for v in value:
                walk(v, key)
    walk(brief if isinstance(brief, (dict, list, tuple, str)) else '')
    return ' '.join(found)


def _is_shot(role: str, brief) -> bool:
    return role == 'lighting' and isinstance(brief, dict) and 'camera' in (brief.get('aspects') or ())


def _said(brief, notes) -> str:
    return f"{words_of(brief)} {notes if isinstance(notes, str) else ''}"


def _topics(role, craft, brief, notes, shot_text, paint_words):
    text, said = words_of(brief), _said(brief, notes)
    counted = [(topic.matches(said if topic.request_match else text), n, topic)
               for n, topic in enumerate(craft.topics)]
    matched = [t for count, _n, t in sorted(counted, key=lambda c: (-c[0], c[1])) if count]
    chosen = matched or [t for name in craft.everyday for t in craft.topics if t.name == name]
    leading = [t for t in craft.topics if t.leads and re.search(t.leads, said, re.I)
               and (not t.request_match or t.matches(said))]
    chosen = leading + [t for t in chosen if t not in leading]
    shot = _is_shot(role, brief) and bool(shot_text)
    # Painterly/toon overrides retain the expanded recipe budget.
    paint_leads = any(t.name in ('painterly', 'toon') and t.words in paint_words for t in leading)
    limit, size = ((SHOT_MAX_CHARS, len(shot_text)) if shot
                   else (PAINT_MAX_CHARS if paint_leads else MAX_CHARS, 0))
    size += len(craft.core) + len(craft.assets)
    names = []
    for topic in chosen[:MAX_TOPICS - int(shot)]:
        if size + len(topic.text) + 1 > limit and names:
            break
        names.append(topic.name)
        size += len(topic.text) + 1
    return names


def skills_for(role: str, brief: dict, notes: str = '', *, extra_dir=None, server_skills=(),
               available_tools=(), no_generation=False, agent_role='worker', skill_index=None) -> str:
    """Select hosted ids and inline local skills within the craft budgets."""
    # An explicitly empty served index is the service's premium-skills off switch.
    if agent_role != 'worker' or (skill_index is not None and not skill_index):
        return ''
    return _indexed_skills(role, brief, notes, skill_index or (), extra_dir,
                           available_tools, no_generation, server_skills)


@dataclass(frozen=True)
class _Piece:
    """A contribution whose text is either local or represented by consecutive hosted ids."""
    chars: int
    ids: tuple = ()
    text: str = ''

    def __len__(self):
        return self.chars


def _join_pieces(pieces):
    # Preserve even empty local contributions: the old join counted their separating LFs.
    output, pending = [], []

    def flush():
        if pending:
            output.append('<loopcut-skills ids="' + ','.join(pending) + '"/>')
            pending.clear()

    for piece in pieces:
        if piece.ids:
            pending.extend(piece.ids)
        else:
            flush()
            output.append(piece.text)
    flush()
    return '\n'.join(output)


def _indexed_skills(role, brief, notes, index, extra_dir, available_tools, no_generation, server_skills=()):
    from .. import prompt_index
    if role not in loader.CRAFT_NAMES:
        return ''
    served = {entry['id']: entry for entry in index}
    fetched = skills_from_dicts(server_skills)
    fetched_ids = {s.id for s in fetched}
    # A served id always beats a bundled copy, regardless of version. Explicit local files win.
    records = {s.id: (dict(s.front_matter), s.body) for s in loader.builtins() if s.id not in served}
    records.update((entry['id'], (entry, None)) for entry in index)
    records.update((s.id, (dict(s.front_matter), s.body))
                   for s in loader.merge_skills((), (*loader.local_skills(extra_dir), *fetched)))
    positions = {entry['id']: n for n, entry in enumerate(index)}
    tools = set(available_tools)
    records = [(fields, body) for fields, body in records.values()
               if 'worker' in fields.get('roles', ())
               and set(fields.get('requires_tools', ())).issubset(tools)
               and not (no_generation and fields.get('kind') == 'gen')]
    records.sort(key=lambda r: (r[0]['id'] not in positions,
                               'part' not in r[0],
                               served.get(r[0]['id'], r[0]).get('order', 0),
                               positions.get(r[0]['id'], 0), r[0]['id']))

    def part(record):
        fields = record[0]
        return fields.get('part', 'topic' if fields.get('words') else None)

    def piece(record):
        fields, body = record
        if body is not None:
            return _Piece(len(body), text=body)
        if 'chars' not in fields:
            raise prompt_index.IndexError(f"Loopcut prompt index: {fields['id']} missing chars")
        return _Piece(fields['chars'], (fields['id'],))

    items = [r for r in records if r[0].get('craft') == role]
    cores, assets = ([r for r in items if part(r) == name] for name in ('core', 'assets'))
    if len(cores) > 1 or len(assets) > 1:
        raise SkillError(f'{role}: more than one core or assets skill')
    core = piece(cores[0]) if cores else _Piece(0)
    said = _said(brief, notes)
    matched = []
    for swap in (r for r in items if part(r) == 'swap'):
        fields, body = swap
        if not cores or not re.search(fields['words'], said, re.I):
            continue
        if (cores[0][1] is None) != (body is None):
            print(f"Loopcut: skipping skill swap {fields['id']}: core and swap must both be served or both local")
            continue
        if body is None:
            matched.append(fields['id'])
        else:
            text = core.text.replace(fields['replaces'], body)
            core = _Piece(len(text), text=text)
    if matched:
        ids = (*core.ids, *matched)
        core = _Piece(prompt_index.expansion_chars(ids, served), ids)
    topics = tuple(Topic(r[0].get('name', r[0]['id'].rsplit('/', 1)[-1]), r[0]['words'], piece(r),
                         r[0].get('leads', ''), 'part' not in r[0]) for r in items if part(r) == 'topic')
    if len({t.name for t in topics}) != len(topics):
        raise SkillError(f'{role}: duplicate topic name')
    craft = Craft(core, topics, cores[0][0].get('everyday', ()) if cores else (),
                  piece(assets[0]) if assets else _Piece(0))
    shot = next((piece(r) for r in items if part(r) == 'shot'), _Piece(0))
    text = ''
    if cores or assets or topics:
        by_name = {topic.name: topic.text for topic in topics}
        paint_words = {e.get('words') for e in index if e.get('craft') == 'modeling'
                       and e.get('name') in ('painterly', 'toon')}
        chosen = _topics(role, craft, brief, notes, shot, paint_words)
        if cores or assets or chosen or (_is_shot(role, brief) and shot):
            text = _join_pieces([core, *([shot] if _is_shot(role, brief) and shot else []),
                                 *(by_name[name] for name in chosen), craft.assets])
    extras = [r for r in records if r[0]['id'] in served.keys() | fetched_ids and not r[0].get('part')
              and r[0].get('craft', role) == role and (bool(re.search(r[0]['words'], said, re.I))
                  if r[0].get('words') else bool(r[0].get('requires_tools')))]
    extras.sort(key=lambda r: not (r[0].get('leads') and re.search(r[0]['leads'], said, re.I)))
    return text + ''.join('\n\n' + _join_pieces([piece(r)]) for r in extras)


def _request_matches(skill, text):
    words = skill.front_matter.get('words')
    return bool(re.search(words, text, re.I)) if words else bool(
        skill.source.startswith('fetched skill ') and skill.front_matter['requires_tools'])


def _leads(skill, text):
    pattern = skill.front_matter.get('leads')
    return bool(pattern and re.search(pattern, text, re.I))


def single_notes(text: str, *, extra_dir=None, server_skills=(), available_tools=(),
                 no_generation=False, role='single') -> str:
    """Style notes for a single/workspace request, followed by matching extra request skills."""
    if role not in ('single', 'workspace'):
        return ''
    said = text if isinstance(text, str) else ''
    records = load_skills(extra_dir, server_skills=server_skills, role=role,
                          available_tools=available_tools, no_generation=no_generation)
    selected = []
    for skill in _ordered(records):
        fields = skill.front_matter
        if fields.get('part') in (None, 'single') and _request_matches(skill, said):
            selected.append(skill)
    selected.sort(key=lambda s: not (s.source.startswith('fetched skill ') and _leads(s, said)))
    return '\n\n'.join(skill.body for skill in selected)
