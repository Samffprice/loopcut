"""Real offline source verification plus archive refusal tests; no network or Blender."""

import contextlib
import hashlib
import importlib.util
import io
import json
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'loopcut/scripts/bundled_extensions.py'
spec = importlib.util.spec_from_file_location('bundled_extensions', SCRIPT)
bundles = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bundles)


class VerifyTest(unittest.TestCase):
    def test_real_shipped_tree_verify_cli_offline(self):
        result = subprocess.run([sys.executable, str(SCRIPT), 'verify'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count(': ok'), 5)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.package = self.root / 'system/example'
        self.package.mkdir(parents=True)
        (self.package / 'blender_manifest.toml').write_bytes(b'id = "example"\r\n')
        (self.package / '__init__.py').write_bytes(b'# untouched bytes\r\n')
        self.archive = self.root / 'source.zip'
        with zipfile.ZipFile(self.archive, 'w') as archive:
            for path in self.package.iterdir():
                archive.writestr(path.name, path.read_bytes())
        count, digest = bundles.tree_hash(self.package)
        self.pin = {'id': 'example', 'files': count, 'tree_sha256': digest,
                    'archive_sha256': hashlib.sha256(self.archive.read_bytes()).hexdigest(),
                    'archive_url': 'https://extensions.blender.org/download/pinned.zip'}
        self.write_manifest()

    def write_manifest(self):
        (self.root / 'bundled.json').write_text(json.dumps({
            'schema': 'loopcut.bundled-extensions/1', 'packages': [self.pin]}))

    def verify(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            ok = bundles.verify(self.root)
        return ok, output.getvalue()

    def response(self):
        response = io.BytesIO(self.archive.read_bytes())
        response.geturl = lambda: self.pin['archive_url']
        return response

    def test_hash_uses_exact_path_null_hash_newline_contract(self):
        expected = ''.join(f'{p.name}\0{hashlib.sha256(p.read_bytes()).hexdigest()}\n'
                           for p in sorted(self.package.iterdir()))
        self.assertEqual(bundles.tree_hash(self.package), (2, hashlib.sha256(expected.encode()).hexdigest()))

    def test_ignores_only_pycache_and_detects_modified_bytes(self):
        cache = self.package / '__pycache__'
        cache.mkdir()
        (cache / 'anything.pyc').write_bytes(b'bytecode')
        self.assertTrue(self.verify()[0])
        (self.package / '__init__.py').write_bytes(b'# changed\n')
        ok, output = self.verify()
        self.assertFalse(ok)
        self.assertIn('tree mismatch', output)

    def test_missing_files_and_extra_missing_packages_are_reported(self):
        (self.package / '__init__.py').unlink()
        (self.root / 'system/extra').mkdir()
        ok, output = self.verify()
        self.assertFalse(ok)
        self.assertIn('extra package', output)
        self.assertIn('files 1/2', output)
        (self.package / 'blender_manifest.toml').unlink()
        self.package.rmdir()
        self.assertIn('Missing package directory', self.verify()[1])

    def test_fetch_compares_without_modifying_disk_and_uses_blender_user_agent(self):
        before = bundles.tree_hash(self.package)
        with mock.patch.object(bundles.urllib.request, 'urlopen', return_value=self.response()) as download:
            bundles.fetch(self.pin, self.root)
        self.assertEqual(bundles.tree_hash(self.package), before)
        self.assertIn('Blender/', download.call_args.args[0].get_header('User-agent'))
        (self.package / '__init__.py').write_text('local modification')
        with mock.patch.object(bundles.urllib.request, 'urlopen', return_value=self.response()):
            with self.assertRaisesRegex(ValueError, 'tree mismatch'):
                bundles.fetch(self.pin, self.root)
        self.assertEqual((self.package / '__init__.py').read_text(), 'local modification')

    def test_fetch_write_replaces_only_after_both_hashes_pass(self):
        (self.package / 'stray.txt').write_text('stray')
        with mock.patch.object(bundles.urllib.request, 'urlopen', return_value=self.response()):
            bundles.fetch(self.pin, self.root, write=True)
        self.assertFalse((self.package / 'stray.txt').exists())
        self.assertTrue(self.verify()[0])
        self.pin['tree_sha256'] = '0' * 64
        before = bundles.tree_hash(self.package)
        with mock.patch.object(bundles.urllib.request, 'urlopen', return_value=self.response()):
            with self.assertRaisesRegex(ValueError, 'tree mismatch'):
                bundles.fetch(self.pin, self.root, write=True)
        self.assertEqual(bundles.tree_hash(self.package), before)

    def test_archive_hash_mismatch_never_extracts_or_writes(self):
        self.pin['archive_sha256'] = '0' * 64
        before = bundles.tree_hash(self.package)
        with mock.patch.object(bundles.urllib.request, 'urlopen', return_value=self.response()), \
                mock.patch.object(bundles, 'extract') as extract:
            with self.assertRaisesRegex(ValueError, 'archive SHA-256 mismatch'):
                bundles.fetch(self.pin, self.root, write=True)
        extract.assert_not_called()
        self.assertEqual(bundles.tree_hash(self.package), before)

    def test_extract_refuses_traversal_absolute_windows_paths_and_symlinks(self):
        for name in ('../escape', '/absolute', 'C:/windows', 'folder\\escape', 'a/../../escape'):
            with zipfile.ZipFile(self.archive, 'w') as archive:
                archive.writestr(name, b'bad')
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'Unsafe'):
                bundles.extract(self.archive, self.root / 'staging')
        entry = zipfile.ZipInfo('link')
        entry.create_system = 3
        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(self.archive, 'w') as archive:
            archive.writestr(entry, '../escape')
        with self.assertRaisesRegex(ValueError, 'Unsafe'):
            bundles.extract(self.archive, self.root / 'staging')

    def test_tree_rejects_symlinks(self):
        (self.package / 'link').symlink_to(self.archive)
        self.assertIn('Symlink', self.verify()[1])

    def test_manifest_cannot_select_outside_system(self):
        self.pin['id'] = '../outside'
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, 'Invalid bundled extension pin'):
            bundles.manifest(self.root)


if __name__ == '__main__':
    unittest.main()
