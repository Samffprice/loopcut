"""Display cleanup and reference resolution, without Blender or a provider."""

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))
sys.modules.setdefault('bpy', types.ModuleType('bpy'))

from loopcut import files, state  # noqa: E402
from loopcut.ui import layout, markdown as md  # noqa: E402


class MarkdownTest(unittest.TestCase):
    def test_links_images_emphasis_and_code_are_readable(self):
        self.assertEqual(md.clean_prose(
            '[a clip](out/a.mp4) ![](out/b.png) ![an image](out/c.png) '
            '**bold** __strong__ `inline_code` and file__name.txt'),
            'a clip b.png an image bold strong inline_code and file__name.txt')

    def test_spaced_link_targets_titles_and_parentheses(self):
        self.assertEqual(md.clean_prose(
            '[](<out/a b.png>) [**open**](out/a(b).png "title")'), 'a b.png open')

    def test_truncated_markdown_never_raises_in_cleanup_or_layout(self):
        examples = ['[image](<out/a.png>)', '[](<out/a(b).png>)',
                    '[image](out/a(b).png "title")', '[image](out/a(b(c)).png)',
                    '[**image**](<out/a(b).png)', '`[image](<out/a.png)`',
                    '````text\n```\n[image](<out/a.png)\n````',
                    '~~~text\n| a | b |\n|---|---|\n~~~']
        session = state.new_session()
        for source in examples:
            for end in range(len(source) + 1):
                prefix = source[:end]
                with self.subTest(prefix=prefix):
                    self.assertIsInstance(md.clean_prose(prefix), str)
                    md.collect_references(prefix, None, exists=lambda path: False)
                    session['items'] = [{'kind': 'assistant', 'text': prefix, 'streaming': True}]
                    layout.build(session, 420, 600, 1.0, lambda font, size, text: len(text) * size * 0.6)

    def test_incomplete_or_unsupported_links_are_literal(self):
        for source in ('[image](<out/a.png)', '[**image**](<out/a(b).png)',
                       '[image](out/a(b(c)).png)', '[image](out/a(b)', '[image](out/a.png "title)',
                       '[image](<out/a.png> "title)', '[**image**]('):
            with self.subTest(source=source):
                self.assertEqual(md.clean_prose(source), source)

    def test_emphasis_requires_word_boundaries_and_unambiguous_delimiters(self):
        literal = 'my__file__v2 2**3**4 a**word** **word**b a__word__ __word__b ***word*** ___word___'
        self.assertEqual(md.clean_prose(literal), literal)
        self.assertEqual(md.clean_prose('(**word**) __two words__ **two words**.'), '(word) two words two words.')
        self.assertEqual(md.clean_prose('**word**suffix **other**'), '**word**suffix other')

    def test_inline_code_content_is_never_cleaned(self):
        contents = '[image](out/a.png) **bold** __strong__ a | b'
        self.assertEqual(md.clean_prose('`' + contents + '`'), contents)
        self.assertEqual(md.clean_prose('``a `tick` **bold** | b``'), 'a `tick` **bold** | b')
        self.assertEqual(md.clean_prose('`unfinished **bold** | b'), '`unfinished **bold** | b')
        multiline = 'before\na | b\n--- | ---\n**bold** | c\nafter'
        self.assertEqual(md.clean_prose('``' + multiline + '``'), multiline)

    def test_tables_omit_alignment_rules_and_keep_rows(self):
        self.assertEqual(md.clean_prose('| a | b |\n| :--- | ---: |\n| **c** | `d` |'),
                         'a    b\nc    d')
        self.assertEqual(md.clean_prose('a | b\n--- | :---:\nc | d'), 'a    b\nc    d')

    def test_pipes_need_a_header_and_matching_delimiter_row(self):
        for source in ('a | b', '| a | b |', '|---|---|', 'a | b\n--- | --- | ---',
                       'a | b\nordinary prose', r'a \| b'):
            with self.subTest(source=source):
                self.assertEqual(md.clean_prose(source), source)
        self.assertEqual(md.clean_prose('Use `a | b` here.'), 'Use a | b here.')
        self.assertEqual(md.clean_prose('| `a | b` | c |\n|---|---|\n| `**d**` | e |'),
                         'a | b    c\n**d**    e')

    def test_fenced_code_contents_are_untouched(self):
        source = '[a](out/a.png) **b** `c`\n| a | b |\n|---|---|'
        self.assertEqual(md.split_markdown('**before**\n```text\n' + source + '\n```\n__after__'),
                         [('p', 'before'), ('code', source), ('p', 'after')])
        self.assertEqual(md.split_markdown('```\n**still code**'), [('code', '**still code**')])

    def test_tilde_and_long_fences_preserve_all_code(self):
        body = '[image](<out/a.png) **bold** `inline`\n| a | b |\n|---|---|\n\n'
        for fence in ('```', '~~~', '````', '~~~~~'):
            source = fence + 'text\n' + body + '\n' + fence
            with self.subTest(fence=fence):
                self.assertEqual(md.clean_prose(source), source)
                self.assertEqual(md.split_markdown(source), [('code', body)])
        body = '**bold**\n```\n~~~\n`inline`'
        source = '````text\n' + body + '\n````'
        self.assertEqual(md.split_markdown(source), [('code', body)])
        self.assertEqual(md.clean_prose(source), source)
        self.assertEqual(md.split_markdown('~~~\n**unfinished**\n'), [('code', '**unfinished**\n')])


class ReferenceTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        (self.base / 'out').mkdir()
        for name in ('a.mp4', 'a b.png', 'c.png', 'd(v1).png'):
            (self.base / 'out' / name).touch()

    def targets(self, text, **kwargs):
        return [ref['target'] for ref in md.collect_references(text, self.base, **kwargs)]

    def test_links_resolve_with_tool_rules_and_deduplicate(self):
        text = '[clip](out/a.mp4) [again](//out/a.mp4) ![](<out/a b.png>) [folder](out)'
        refs = md.collect_references(text, self.base)
        self.assertEqual([r['target'] for r in refs],
                         [str(files.resolve(p, self.base)) for p in ('out/a.mp4', 'out/a b.png', 'out')])
        self.assertEqual([r['name'] for r in refs], ['a.mp4', 'a b.png', 'out'])

    def test_bare_absolute_relative_folder_and_quoted_paths(self):
        absolute = self.base / 'out' / 'a b.png'
        (self.base / 'out' / 'subdir').mkdir()
        text = f'Files: out/a.mp4, and "out/c.png".\n{absolute}\nFolder: out/subdir/\n`out/d(v1).png`'
        self.assertEqual(set(self.targets(text)),
                         {str(self.base / p) for p in ('out/a.mp4', 'out/a b.png', 'out/c.png', 'out/subdir', 'out/d(v1).png')})

    def test_existence_alone_does_not_make_prose_or_roots_paths(self):
        exists = mock.Mock(return_value=True)
        for token in ('/', '//', '\\', './', '../', '~/', 'C:/', 'C:\\', '.', '..', 'out/', 'out'):
            for text in (token, f'`{token}`', f'"{token}"'):
                with self.subTest(text=text):
                    self.assertEqual(self.targets(text, exists=exists), [])
        for token in ('/', '//', './', '../', '~/', 'C:/'):
            self.assertEqual(self.targets(f'[folder]({token})', exists=exists), [])
        exists.assert_not_called()
        self.assertEqual(self.targets('[folder](out)'), [str(self.base / 'out')])
        self.assertEqual(self.targets('a.txt', exists=exists), [str(self.base / 'a.txt')])

    def test_long_and_tilde_fences_do_not_produce_references(self):
        for fence in ('~~~', '````'):
            self.assertEqual(self.targets(f'{fence}\n```\nout/a.mp4\n[image](out/c.png)\n{fence}'), [])

    def test_filename_alone_and_punctuation(self):
        (self.base / 'a.mp4').touch()
        self.assertEqual(self.targets('Saved a.mp4. Also (out/d(v1).png).'),
                         [str(self.base / 'a.mp4'), str(self.base / 'out/d(v1).png')])

    def test_urls_are_separate_missing_paths_and_fences_are_ignored(self):
        refs = md.collect_references('[docs](https://example.com/a) [same](https://example.com/a) '
                                     '[missing](out/missing.png) out/no.png\n'
                                     'https://example.com/out/c.png\n```\nout/a.mp4\n```', self.base)
        self.assertEqual(refs, [{'kind': 'url', 'name': 'docs', 'target': 'https://example.com/a'}])

    def test_percent_encoded_link_and_home_path(self):
        self.assertEqual(self.targets('[image](out/a%20b.png)'), [str(self.base / 'out/a b.png')])
        home_path = Path('~/out/c.png').expanduser().resolve()
        self.assertEqual(self.targets('~/out/c.png', exists=lambda p: p == home_path), [str(home_path)])

    def test_windows_drive_paths_are_not_mistaken_for_urls(self):
        target = self.base / 'out/c.png'
        with mock.patch.object(files, 'resolve', return_value=target) as resolve:
            self.assertEqual(self.targets(r'C:\out\c.png'), [str(target)])
        resolve.assert_called_once_with(r'C:\out\c.png', base=self.base)

    def test_injected_existence_and_base_do_not_use_process_working_directory(self):
        paths = {self.base / 'relative.txt'}
        with mock.patch.object(files, 'relative_base', side_effect=AssertionError('must use injected base')):
            self.assertEqual(self.targets('`relative.txt`', exists=paths.__contains__),
                             [str(self.base / 'relative.txt')])

    def test_completion_is_cached_without_changing_the_reply(self):
        item = {'text': '**Saved** [clip](out/a.mp4)'}
        original = item['text']
        md.finish_reply(item, self.base)
        refs = item['file_references']
        (self.base / 'out/a.mp4').unlink()
        with mock.patch.object(md, 'collect_references', side_effect=AssertionError('already collected')):
            md.finish_reply(item, self.base)
        self.assertIs(item['file_references'], refs)
        self.assertEqual(item['text'], original)
        self.assertEqual(refs[0]['name'], 'a.mp4')


if __name__ == '__main__':
    unittest.main()
