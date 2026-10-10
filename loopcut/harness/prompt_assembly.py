"""Local implementation of contract 006 prompt injection (stdlib only).

    python3 prompt_assembly.py index --prompts DIR --skills DIR --out index.json
    python3 prompt_assembly.py assemble --prompt worker < request.json
    python3 prompt_assembly.py export-addon --out /tmp/loopcut-prompts

Sources stay outside the repository. Skill.body is the add-on parser's injected
text, not the fetch endpoint's body (which still includes the format LF).
"""

import argparse
import copy
import hashlib
import importlib
import importlib.util
import itertools
import json
import os
from pathlib import Path
import re
import sys
import types
from dataclasses import dataclass, field

REPO = Path(__file__).resolve().parents[2]
WEB = Path('/Volumes/BackupFiles/MasterCoding/BlenderCursor/loopcut-web')
PROMPT_IDS = ('single', 'orchestrator', 'worker', 'router')
SKILL_ID = r'[a-z0-9_-]+(?:/[a-z0-9_-]+)*'
PLACEHOLDER = re.compile(r'<loopcut-skills ids="(' + SKILL_ID + r'(?:,' + SKILL_ID + r')*)"/>')
INDEX_KEYS = frozenset(('id', 'version', 'title', 'when', 'roles', 'requires_tools', 'tier', 'kind',
                        'words', 'leads', 'craft', 'part', 'name', 'order', 'everyday'))


class AssemblyError(ValueError):
    def __init__(self, code, message, extra=None):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, dict(extra or {})

    def envelope(self):
        return {'error': {'message': self.message, 'type': self.code,
                          'code': self.code, **self.extra}}


@dataclass
class Source:
    prompts: dict
    skills: dict  # id -> loader.Skill (front_matter, body, sha256)
    prompt_files: dict = field(default_factory=dict)


def skill_loader():
    """Load the real parser by path, as distill.py does, without importing bpy."""
    name = '_loopcut_prompt_skill_loader'
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, REPO / 'scripts/addons_core/loopcut/skills/loader.py')
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _read_prompts(directory):
    """Isolated layout adapter: full <id>.md files, or manifest prompts[id].{parts,join}.

    Each part is relative to the manifest. Strip exactly one format LF from each
    file before joining; preserve all other whitespace, including CRs.
    """
    if not directory.is_dir():
        raise ValueError(f'Prompts directory does not exist: {directory}; set LOOPCUT_PROMPTS_DIR or --prompts')
    manifest = directory / 'manifest.json'
    if manifest.exists():
        document = json.loads(manifest.read_text(encoding='utf-8'))
        entries = document.get('prompts') if isinstance(document, dict) else None
        if not isinstance(entries, dict):
            raise ValueError(f'{manifest}: expected a prompts object mapping ids to parts and join strings')
    else:
        entries = {key: {'parts': [f'{key}.md'], 'join': ''} for key in PROMPT_IDS}
    prompts, files = {}, {}
    for key in PROMPT_IDS:
        entry = entries.get(key)
        if (not isinstance(entry, dict) or not isinstance(entry.get('parts'), list) or not entry['parts']
                or any(not isinstance(p, str) for p in entry['parts'])
                or not isinstance(entry.get('join', ''), str)):
            raise ValueError(f'{manifest}: expected prompts.{key} with parts and a join string')
        paths = [(directory / part).resolve() for part in entry['parts']]
        if any(not path.is_relative_to(directory.resolve()) for path in paths):
            raise ValueError(f'{manifest}: prompt parts must be inside the prompts directory')
        try:
            texts = [path.read_bytes().decode('utf-8').removesuffix('\n') for path in paths]
        except OSError as ex:
            raise ValueError(f'Cannot read prompt {key} in {directory}; set LOOPCUT_PROMPTS_DIR or --prompts: {ex}') from ex
        prompts[key] = entry.get('join', '').join(texts)
        files[key] = tuple(str(path) for path in paths)
    return prompts, files


def load(prompts_dir=None, skills_dir=None):
    prompts, files = _read_prompts(Path(prompts_dir or os.environ.get('LOOPCUT_PROMPTS_DIR') or WEB / 'prompts'))
    directory = Path(skills_dir or os.environ.get('LOOPCUT_SERVER_SKILLS_DIR') or WEB / 'skills')
    if not directory.is_dir():
        raise ValueError(f'Skills directory does not exist: {directory}; set LOOPCUT_SERVER_SKILLS_DIR or --skills')
    skills = {}
    for path in sorted(directory.rglob('*.md')):
        if path.name == 'README.md':
            continue
        skill = skill_loader().parse_skill(path.read_bytes().decode('utf-8'), str(path))
        if skill.id in skills:
            raise ValueError(f'{path}: duplicate skill id {skill.id}')
        skills[skill.id] = skill
    return Source(prompts, skills, files)


def assemble(messages, prompt_id, source):
    """Return independent messages, injecting only the system prefix and user placeholders."""
    if prompt_id not in PROMPT_IDS or prompt_id not in source.prompts:
        raise AssemblyError('unknown_prompt', 'Unknown prompt id.')
    if not isinstance(messages, list) or any(not isinstance(m, dict) for m in messages):
        raise AssemblyError('bad_system_message', 'Messages must be an array of message objects.')
    systems = [i for i, message in enumerate(messages) if message.get('role') == 'system']
    if systems and (systems != [0] or not isinstance(messages[0].get('content'), str)):
        raise AssemblyError('bad_system_message', 'Use at most one system message, first, with string content.')
    result = copy.deepcopy(messages)
    if systems:
        result[0]['content'] = source.prompts[prompt_id] + result[0]['content']
    else:
        result.insert(0, {'role': 'system', 'content': source.prompts[prompt_id]})
    seen, count, chars = set(), 0, 0

    def expand(text):
        nonlocal count, chars
        out, end = [], 0
        while (start := text.find('<loopcut-skills', end)) >= 0:
            match = PLACEHOLDER.match(text, start)
            if match is None:
                raise AssemblyError('bad_skill_placeholder', 'Malformed skill placeholder.')
            count += 1
            if count > 64:
                raise AssemblyError('skills_over_limit', 'Too many skill placeholders.')
            ids = match[1].split(',')
            contributions, cores, local_ids = [], {}, set()
            for skill_id in ids:
                if skill_id in local_ids:
                    raise AssemblyError('bad_skill_placeholder', 'Repeated skill id in one placeholder.')
                local_ids.add(skill_id)
                skill = source.skills.get(skill_id)
                if skill is None:
                    raise AssemblyError('unknown_skill', 'Unknown skill id.', {'id': skill_id})
                if skill_id not in seen:
                    seen.add(skill_id)
                    # Distinct ids count once per request, including swap bodies, before replacement.
                    chars += len(skill.body)
                    if len(seen) > 24 or chars > 32_000:
                        raise AssemblyError('skills_over_limit', 'Injected skills exceed the request limits.')
                fields = skill.front_matter
                craft = fields.get('craft')
                if fields.get('part') == 'swap':
                    if craft not in cores:
                        raise AssemblyError('bad_skill_placeholder', 'A swap needs an earlier core of the same craft.')
                    n = cores[craft]
                    contributions[n] = contributions[n].replace(fields['replaces'], skill.body)
                else:
                    if fields.get('part') == 'core':
                        cores[craft] = len(contributions)
                    contributions.append(skill.body)
            out.extend((text[end:start], '\n'.join(contributions)))
            end = match.end()
        return ''.join(out) + text[end:]

    for message in result:
        if message.get('role') != 'user':
            continue
        content = message.get('content')
        if isinstance(content, str):
            message['content'] = expand(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get('type') == 'text' and isinstance(part.get('text'), str):
                    part['text'] = expand(part['text'])
    return result


def version(source):
    # Framing and ids avoid concatenation collisions; swap metadata also affects injected text.
    texts = {'prompts': source.prompts, 'skills': {
        key: [skill.body, {k: skill.front_matter[k] for k in ('part', 'craft', 'replaces') if k in skill.front_matter}]
        for key, skill in source.skills.items()}}
    return hashlib.sha256(json.dumps(texts, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()[:12]


def index(source):
    skills = [source.skills[key] for key in sorted(source.skills)]
    entries = []
    for skill in skills:
        fields = skill.front_matter
        entry = {key: list(value) if isinstance(value, tuple) else copy.deepcopy(value)
                 for key, value in fields.items() if key in INDEX_KEYS}
        entry.update(sha256=skill.sha256, chars=len(skill.body))
        swaps = [s for s in skills if s.front_matter.get('part') == 'swap'
                 and s.front_matter.get('craft') == fields.get('craft')]
        if fields.get('part') == 'core' and swaps:
            entry['swapped_chars'] = {}
            for n in range(1, len(swaps) + 1):
                for combination in itertools.combinations(swaps, n):
                    text = skill.body
                    for swap in combination:
                        text = text.replace(swap.front_matter['replaces'], swap.body)
                    entry['swapped_chars'][','.join(s.id for s in combination)] = len(text)
        entries.append(entry)
    return {'prompts': {'version': version(source),
                        'items': {key: {'chars': len(text)} for key, text in source.prompts.items()}},
            'skills': entries}


def export_addon(out):
    """Temporary parity tool; fail before writing anything once any hosted constant is removed."""
    sys.path.insert(0, str(REPO / 'scripts/addons_core'))
    # Same import-only stand-in as test_orchestrator; no Blender operations are run.
    if 'bpy' not in sys.modules:
        fake_bpy = types.ModuleType('bpy')
        fake_bpy.app = types.SimpleNamespace(driver_namespace={})
        sys.modules['bpy'] = fake_bpy
    modules = {name: importlib.import_module(f'loopcut.{name}')
               for name in ('agent', 'orchestrator', 'workers', 'router')}
    required = (('agent', 'SYSTEM_PROMPT'), ('orchestrator', 'PROMPT'), ('workers', 'PROMPT'), ('router', 'PROMPT'))
    missing = [f'{name}.{attr}' for name, attr in required if not isinstance(getattr(modules[name], attr, None), str)]
    if missing:
        raise ValueError('Cannot export add-on prompts: constants removed: ' + ', '.join(missing)
                         + '. Use the hosted prompts directory (LOOPCUT_PROMPTS_DIR).')
    texts = {module: getattr(modules[module], name) for module, name in required}
    base = texts['agent']
    prompts = {'single': base, 'orchestrator': base + '\n\n' + texts['orchestrator'],
               'worker': texts['workers'], 'router': texts['router']}
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    for key, text in prompts.items():
        (out / f'{key}.md').write_bytes((text + '\n').encode('utf-8'))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    for command in ('index', 'assemble'):
        sub = commands.add_parser(command)
        sub.add_argument('--prompts', type=Path)
        sub.add_argument('--skills', type=Path)
        if command == 'index':
            sub.add_argument('--out', type=Path, required=True)
        else:
            sub.add_argument('--prompt', required=True)
    commands.add_parser('export-addon').add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'export-addon':
            export_addon(args.out)
        elif args.command == 'index':
            args.out.write_text(json.dumps(index(load(args.prompts, args.skills)), indent=2) + '\n', encoding='utf-8')
        else:
            body = json.load(sys.stdin)
            messages = body.get('messages') if isinstance(body, dict) else body
            print(json.dumps(assemble(messages, args.prompt, load(args.prompts, args.skills)), ensure_ascii=False, indent=2))
    except AssemblyError as ex:
        print(json.dumps(ex.envelope()), file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, ImportError) as ex:
        parser.exit(1, f'prompt_assembly: {ex}\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
