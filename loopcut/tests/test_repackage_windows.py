"""loopcut/scripts/repackage_windows.py: the checks behind the release workflow's Windows repackage.
Temporary Git repositories, packages and .msi dumps; no network, Windows or Blender."""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'loopcut/scripts/repackage_windows.py'
spec = importlib.util.spec_from_file_location('repackage_windows', SCRIPT)
repack = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repack)

POINTER = b'version https://git-lfs.github.com/spec/v1\noid sha256:' + b'0' * 64 + b'\nsize 12\n'


def quiet(func, *args):
    """Runs FUNC; returns (exit code or None, stderr)."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        try:
            func(*args)
        except SystemExit as e:
            return e.code, err.getvalue()
    return None, err.getvalue()


class GitRepoTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / 'repo'
        self.repo.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.email', 'test@example.com')
        self.git('config', 'user.name', 'test')
        self.git('config', 'core.autocrlf', 'false')
        old = repack.REPO
        repack.REPO = str(self.repo)
        self.addCleanup(setattr, repack, 'REPO', old)

    def git(self, *args):
        return subprocess.run(('git',) + args, cwd=self.repo, check=True, capture_output=True, text=True).stdout.strip()

    def write(self, files):
        for rel, data in files.items():
            path = self.repo / rel
            if data is None:
                path.unlink()
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data if isinstance(data, bytes) else data.encode())

    def commit(self, files, message='c'):
        self.write(files)
        self.git('add', '-A')
        self.git('commit', '-q', '-m', message)
        return self.git('rev-parse', 'HEAD')


class CheckTest(GitRepoTest):
    def setUp(self):
        super().setUp()
        self.base = self.commit({'scripts/addons_core/loopcut/__init__.py': 'v1\n', 'source/x.cc': 'int x;\n',
                                 'source/creator/CMakeLists.txt': 'install(\n  PATTERN ".git" EXCLUDE\n)\n'})

    def test_python_and_packaging_pass(self):
        head = self.commit({'scripts/addons_core/loopcut/__init__.py': 'v2\n', 'loopcut/notes.md': 'x\n',
                            '.github/workflows/r.yml': 'x\n',
                            'source/creator/CMakeLists.txt':
                                'install(\n  PATTERN ".git" EXCLUDE\n  PATTERN "*.pyc" EXCLUDE\n)\n'})
        self.assertEqual(quiet(repack.check, self.base, head)[0], None)

    def test_native_change_refuses(self):
        head = self.commit({'source/x.cc': 'int y;\n'})
        code, err = quiet(repack.check, self.base, head)
        self.assertEqual(code, 1)
        self.assertIn('source/x.cc', err)

    def test_rename_into_scripts_is_seen(self):
        self.git('mv', 'source/x.cc', 'scripts/x.cc')
        self.git('commit', '-q', '-m', 'move')
        code, err = quiet(repack.check, self.base, 'HEAD')
        self.assertEqual(code, 1)
        self.assertIn('source/x.cc', err)

    def test_other_install_rule_change_refuses(self):
        head = self.commit({'source/creator/CMakeLists.txt': 'install(\n  PATTERN ".git" EXCLUDE\n  DESTINATION x\n)\n'})
        code, err = quiet(repack.check, self.base, head)
        self.assertEqual(code, 1)
        self.assertIn('DESTINATION x', err)

    def test_datafiles_icon_and_installer_refuse(self):
        head = self.commit({'release/datafiles/a.txt': 'x\n', 'release/loopcut/loopcut.ico': b'\0',
                            'release/windows/installer_wix/WIX.template': '<Wix/>\n',
                            'build_files/cmake/packaging.cmake': 'x\n'})
        code, err = quiet(repack.check, self.base, head)
        self.assertEqual(code, 1)
        for path in ('release/loopcut/loopcut.ico', 'release/windows/installer_wix/WIX.template',
                     'build_files/cmake/packaging.cmake', 'release/datafiles/a.txt'):
            self.assertIn(path, err)


class ApplyTest(GitRepoTest):
    def setUp(self):
        super().setUp()
        self.base = self.commit({
            'scripts/addons_core/loopcut/__init__.py': 'version 1\n',
            'scripts/addons_core/loopcut/sub/b.py': 'b\n',
            'scripts/site/sitecustomize.py': 'not installed\n',
            'scripts/addons_core/bl_pkg/tests/t.py': 'not installed\n',
            'scripts/startup/template/startup.blend': POINTER,
            'release/extensions/system/ext/blender_manifest.toml': 'id = "ext"\n',
        })
        self.package = Path(self.temp.name) / 'package'
        files = {
            '5.2/scripts/addons_core/loopcut/__init__.py': b'version 1\n',
            '5.2/scripts/addons_core/loopcut/sub/b.py': b'b\n',
            '5.2/scripts/startup/template/startup.blend': b'real blend!!',
            '5.2/scripts/addons_core/cycles/lib/kernel.zst': b'built',
            '5.2/extensions/system/ext/blender_manifest.toml': b'id = "ext"\n',
            'blender.exe': b'MZ',
        }
        for rel, data in files.items():
            path = self.package / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.changes = Path(self.temp.name) / 'changes.txt'

    def test_applies_the_python_change(self):
        head = self.commit({
            'scripts/addons_core/loopcut/__init__.py': 'version 2\n',
            'scripts/addons_core/loopcut/sub/b.py': None,
            'scripts/addons_core/loopcut/c.py': 'c\n',
            'release/extensions/system/ext/blender_manifest.toml': 'id = "ext"\nversion = "2"\n',
        })
        self.assertEqual(quiet(repack.apply, self.base, head, str(self.package), str(self.changes))[0], None)
        p = self.package / '5.2'
        self.assertEqual((p / 'scripts/addons_core/loopcut/__init__.py').read_bytes(), b'version 2\n')
        self.assertEqual((p / 'scripts/addons_core/loopcut/c.py').read_bytes(), b'c\n')
        self.assertFalse((p / 'scripts/addons_core/loopcut/sub').exists())
        self.assertEqual((p / 'scripts/startup/template/startup.blend').read_bytes(), b'real blend!!')
        self.assertEqual((p / 'scripts/addons_core/cycles/lib/kernel.zst').read_bytes(), b'built')
        self.assertFalse((p / 'scripts/site').exists())
        self.assertEqual(sorted(self.changes.read_text().splitlines()), sorted([
            'M\t5.2/scripts/addons_core/loopcut/__init__.py', 'A\t5.2/scripts/addons_core/loopcut/c.py',
            'D\t5.2/scripts/addons_core/loopcut/sub/b.py', 'D\t5.2/scripts/addons_core/loopcut/sub/',
            'M\t5.2/extensions/system/ext/blender_manifest.toml']))

    def test_no_change_is_identity(self):
        self.assertEqual(quiet(repack.apply, self.base, self.base, str(self.package), str(self.changes))[0], None)
        self.assertEqual(self.changes.read_text(), '')

    def test_package_not_from_base_refuses(self):
        (self.package / '5.2/scripts/addons_core/loopcut/__init__.py').write_bytes(b'version 0\n')
        code, err = quiet(repack.apply, self.base, self.base, str(self.package), str(self.changes))
        self.assertEqual(code, 1)
        self.assertIn('differs: 5.2/scripts/addons_core/loopcut/__init__.py', err)

    def test_attributes_apply_as_in_a_checkout(self):
        # With core.autocrlf, text files get CRLF as in a Windows checkout, but not where .gitattributes says -text.
        self.git('config', 'core.autocrlf', 'true')
        base = self.commit({'.gitattributes': '/release/extensions/system/** -text\n',
                            'release/extensions/system/ext/data.json': '{\n}\n', 'scripts/a.py': 'a\nb\n'})
        self.assertEqual(repack.installed(base, 'extensions')['system/ext/data.json'], b'{\n}\n')
        self.assertEqual(repack.installed(base, 'scripts')['a.py'], b'a\r\nb\r\n')

    def test_crlf_pointer_is_a_pointer(self):
        self.assertTrue(repack.is_pointer(POINTER.replace(b'\n', b'\r\n')))
        self.assertFalse(repack.is_pointer(b'version 1\n'))

    def test_changed_lfs_file_refuses(self):
        head = self.commit({'scripts/startup/template/startup.blend': POINTER.replace(b'size 12', b'size 13')})
        code, err = quiet(repack.apply, self.base, head, str(self.package), str(self.changes))
        self.assertEqual(code, 1)
        self.assertIn('LFS', err)


def make_zip(path, top, files):
    with zipfile.ZipFile(path, 'w') as z:
        for rel, data in files.items():
            z.writestr(f'{top}/{rel}', data)


class ZipDiffTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        d = Path(self.temp.name)
        self.base, self.new, self.changes = d / 'base.zip', d / 'new.zip', d / 'changes.txt'
        make_zip(self.base, 'loopcut-v0.2.5-windows64', {'blender.exe': b'MZ', '5.2/scripts/a.py': b'1', '5.2/x/': b''})
        self.changes.write_text('M\t5.2/scripts/a.py\nA\t5.2/scripts/new/b.py\n')

    def test_only_changes_differ(self):
        make_zip(self.new, 'loopcut-v0.2.6-windows64', {'blender.exe': b'MZ', '5.2/scripts/a.py': b'2',
                                                         '5.2/scripts/new/': b'', '5.2/scripts/new/b.py': b'b',
                                                         '5.2/x/': b''})
        self.assertEqual(quiet(repack.zipdiff, str(self.base), str(self.new), str(self.changes))[0], None)

    def test_other_difference_refuses(self):
        make_zip(self.new, 'loopcut-v0.2.6-windows64', {'blender.exe': b'MZ!', '5.2/scripts/a.py': b'2', '5.2/x/': b''})
        code, err = quiet(repack.zipdiff, str(self.base), str(self.new), str(self.changes))
        self.assertEqual(code, 1)
        self.assertIn('differs: blender.exe', err)


def msi(product_code, files, upgrade='{UPG}', shortcut_target='[#CM_FP_Blender.blender_launcher.exe]', package_code='{P1}',
        hashes=None):
    """A small dump in msi_tables.ps1's format: FILES maps a path under INSTALL_ROOT to its size; the files'
    contents are that many b'x', unless HASHES gives a path other hash parts."""
    dirs = [['TARGETDIR', None, 'SourceDir'], ['ProgramFiles64Folder', 'TARGETDIR', '.'],
            ['INSTALL_PREFIX_1', 'ProgramFiles64Folder', 'Loopcut'], ['INSTALL_ROOT', 'INSTALL_PREFIX_1', 'LOOPCU~1|Loopcut 5.2'],
            ['ProgramMenuFolder', 'TARGETDIR', '.']]
    comps, file_rows, feature_comps, hash_rows = [], [], [], []
    for path, size in sorted(files.items()):
        folder = 'INSTALL_ROOT'
        parts = path.split('/')
        for i in range(len(parts) - 1):
            key = 'CM_DP_' + '.'.join(parts[:i + 1])
            if not any(d[0] == key for d in dirs):
                dirs.append([key, folder, parts[i]])
            folder = key
        component = ('Blender' if '/' not in path and path.startswith('blender') else
                     'Libraries' if path.startswith('blender.crt/') or path == 'ucrtbase.dll' else 'Unspecified')
        fid = f'CM_FP_{component}.' + path.replace('/', '.').replace('-', '_')
        comps.append([f'CM_CP_{fid[6:]}', '{GUID-' + path + '}', folder, 0, None, fid])
        file_rows.append([fid, f'CM_CP_{fid[6:]}', 'SHORT|' + parts[-1], size, None, None, 512, len(file_rows) + 1])
        feature_comps.append([f'CM_C_{component}', f'CM_CP_{fid[6:]}'])
        hash_rows.append([fid, 0] + (hashes or {}).get(path, repack.msi_hash(b'x' * size)))
    table = lambda columns, rows: {'columns': columns, 'types': [], 'rows': rows}
    return {'summary': {'2': 'Installation Database', '9': package_code, '12': 'now', '18': 'WiX 3.14'}, 'tables': {
        'Property': table(['Property', 'Value'], [['ProductCode', product_code], ['UpgradeCode', upgrade],
                                                  ['ProductVersion', '5.2.2'], ['ProductName', 'Loopcut']]),
        'Upgrade': table(['UpgradeCode', 'VersionMin', 'VersionMax', 'Language', 'Attributes', 'Remove', 'ActionProperty'],
                         [[upgrade, None, '5.2.2', None, 769, None, 'WIX_UPGRADE_DETECTED']]),
        'Directory': table(['Directory', 'Directory_Parent', 'DefaultDir'], dirs),
        'Component': table(['Component', 'ComponentId', 'Directory_', 'Attributes', 'Condition', 'KeyPath'], comps),
        'File': table(['File', 'Component_', 'FileName', 'FileSize', 'Version', 'Language', 'Attributes', 'Sequence'], file_rows),
        'FeatureComponents': table(['Feature_', 'Component_'], feature_comps),
        'MsiFileHash': table(['File_', 'Options', 'HashPart1', 'HashPart2', 'HashPart3', 'HashPart4'], hash_rows),
        'Shortcut': table(['Shortcut', 'Directory_', 'Name', 'Component_', 'Target'],
                          [['CM_S1', 'ProgramMenuFolder', 'Loopcut 5.2', 'CM_SHORTCUT', shortcut_target]]),
        'Media': table(['DiskId', 'LastSequence', 'Cabinet'], [[1, len(file_rows), '#media1.cab']]),
    }}


class MsiCompareTest(unittest.TestCase):
    FILES = {'blender.exe': 100, 'blender-launcher.exe': 10, '5.2/scripts/addons_core/loopcut/__init__.py': 5,
             '5.2/scripts/addons_core/loopcut/old.py': 3, '5.2/datafiles/x.dat': 7}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.changes = self.dir / 'changes.txt'
        self.changes.write_text('M\t5.2/scripts/addons_core/loopcut/__init__.py\nD\t5.2/scripts/addons_core/loopcut/old.py\n'
                                'A\t5.2/scripts/addons_core/loopcut/new/n.py\n')

    def compare(self, base, new, zip_files=None):
        (self.dir / 'base.json').write_text(json.dumps(base))
        (self.dir / 'new.json').write_text(json.dumps(new))
        make_zip(self.dir / 'base.zip', 'loopcut-v0.2.5-windows64',
                 {rel: b'x' * size for rel, size in (zip_files or self.FILES).items()})
        return quiet(repack.msi_compare, str(self.dir / 'base.json'), str(self.dir / 'new.json'), str(self.changes),
                     str(self.dir / 'base.zip'))

    def changed_files(self):
        files = dict(self.FILES)
        files['5.2/scripts/addons_core/loopcut/__init__.py'] = 9
        del files['5.2/scripts/addons_core/loopcut/old.py']
        files['5.2/scripts/addons_core/loopcut/new/n.py'] = 4
        return files

    def test_changed_files_and_new_codes_pass(self):
        self.assertEqual(self.compare(msi('{A}', self.FILES), msi('{B}', self.changed_files(), package_code='{P2}'))[0], None)

    def test_same_product_code_fails(self):
        code, err = self.compare(msi('{A}', self.FILES), msi('{A}', self.FILES, package_code='{P2}'))
        self.assertEqual(code, 1)

    def test_same_package_code_fails(self):
        code, err = self.compare(msi('{A}', self.FILES), msi('{B}', self.FILES))
        self.assertEqual(code, 1)

    def test_other_upgrade_code_fails(self):
        code, err = self.compare(msi('{A}', self.FILES), msi('{B}', self.FILES, upgrade='{OTHER}', package_code='{P2}'))
        self.assertEqual(code, 1)

    def test_unchanged_file_size_fails(self):
        files = dict(self.FILES)
        files['5.2/datafiles/x.dat'] = 8
        code, err = self.compare(msi('{A}', self.FILES), msi('{B}', files, package_code='{P2}'))
        self.assertEqual(code, 1)

    def test_file_signed_apart_in_the_base_follows_the_zip(self):
        # The base .msi has another copy of x.dat (7 bytes) than the base .zip (8 bytes); the new .msi has the .zip's.
        zip_files = dict(self.FILES, **{'5.2/datafiles/x.dat': 8})
        base = msi('{A}', self.FILES, hashes={'5.2/datafiles/x.dat': [1, 2, 3, 4]})
        self.assertEqual(self.compare(base, msi('{B}', zip_files, package_code='{P2}'), zip_files)[0], None)
        # But not when the new .msi matches neither.
        other = dict(self.FILES, **{'5.2/datafiles/x.dat': 9})
        self.assertEqual(self.compare(base, msi('{B}', other, package_code='{P2}'), zip_files)[0], 1)

    def test_shortcut_change_fails(self):
        code, err = self.compare(msi('{A}', self.FILES), msi('{B}', self.FILES, shortcut_target='x', package_code='{P2}'))
        self.assertEqual(code, 1)

    def test_split_by_the_base_components(self):
        files = dict(self.FILES, **{'blender.crt/msvcp140.dll': 2, 'ucrtbase.dll': 2})
        (self.dir / 'base.json').write_text(json.dumps(msi('{A}', files)))
        root, out = self.dir / 'root', self.dir / 'staged'
        for rel in list(files) + ['5.2/scripts/addons_core/loopcut/new.py']:
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_bytes(b'x')
        (root / '5.2/empty').mkdir()
        self.assertEqual(quiet(repack.split, str(root), str(self.dir / 'base.json'), str(out))[0], None)
        found = sorted(str(p.relative_to(out)).replace(os.sep, '/') for p in out.rglob('*') if p.is_file())
        self.assertEqual(found, ['Blender/blender-launcher.exe', 'Blender/blender.exe', 'Libraries/blender.crt/msvcp140.dll',
                                 'Libraries/ucrtbase.dll', 'Unspecified/5.2/datafiles/x.dat',
                                 'Unspecified/5.2/scripts/addons_core/loopcut/__init__.py',
                                 'Unspecified/5.2/scripts/addons_core/loopcut/new.py',
                                 'Unspecified/5.2/scripts/addons_core/loopcut/old.py'])
        self.assertTrue((out / 'Unspecified/5.2/empty').is_dir())
        self.assertFalse((out / 'Unspecified/blender.crt').exists())
        self.assertEqual(list(root.iterdir()), [])

if __name__ == '__main__':
    unittest.main()
