"""Finding and copying stock Blender's settings; needs no Blender."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "extension"))

from loopcut import blender_import as bi  # noqa: E402


class BlenderImportTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "Blender"

    def settings(self, version: str, saved: bool = True) -> Path:
        folder = self.root / version
        (folder / "config").mkdir(parents=True)
        if saved:
            (folder / "config" / "userpref.blend").write_bytes(b"prefs " + version.encode())
        return folder

    def test_newest_readable_version_wins(self):
        for version in ("4.4", "4.5", "5.2", "5.10"):
            self.settings(version)
        self.assertEqual(bi.find_source(self.root, (5, 2)), ((5, 2), self.root / "5.2"))
        self.assertEqual(bi.find_source(self.root, (5, 1)), ((4, 5), self.root / "4.5"))
        self.assertEqual(bi.find_source(self.root, (5, 11)), ((5, 10), self.root / "5.10"))  # Not a string sort.

    def test_skips_newer_too_old_unsaved_and_stray_folders(self):
        self.settings("5.3")            # Newer than this build: may not load.
        self.settings("3.6")            # Older than the previous major release.
        self.settings("5.1", saved=False)
        (self.root / "5.0-backup" / "config").mkdir(parents=True)
        self.assertIsNone(bi.find_source(self.root, (5, 2)))
        self.assertIsNone(bi.find_source(self.root / "missing", (5, 2)))
        self.assertIsNone(bi.find_source(None, (5, 2)))

    def test_stock_root_per_platform(self):
        self.assertEqual(bi.stock_root("win32", {"APPDATA": "C:/Users/a/AppData/Roaming"}),
                         Path("C:/Users/a/AppData/Roaming") / "Blender Foundation" / "Blender")
        self.assertIsNone(bi.stock_root("win32", {}))
        self.assertEqual(bi.stock_root("linux", {"XDG_CONFIG_HOME": "/x"}), Path("/x/blender"))
        self.assertEqual(bi.stock_root("linux", {}), Path.home() / ".config" / "blender")
        self.assertEqual(bi.stock_root("darwin", {}), Path.home() / "Library" / "Application Support" / "Blender")

    def test_copy_leaves_the_source_alone_and_keeps_links_as_links(self):
        source = self.settings("5.2")
        addon = source / "scripts" / "addons" / "my_addon"
        addon.mkdir(parents=True)
        (addon / "__init__.py").write_text("bl_info = {}")
        (source / "scripts" / "addons" / "single.py").write_text("")
        (source / "extensions" / "blender_org" / "node_wrangler").mkdir(parents=True)
        (source / "extensions" / "blender_org" / ".blender_ext").mkdir()
        outside = self.root.parent / "outside"
        outside.mkdir()
        (source / "scripts" / "linked").symlink_to(outside)
        self.assertEqual(bi.addon_count(source), 3)

        before = sorted(p.relative_to(source) for p in source.rglob("*"))
        target = self.root.parent / "Loopcut" / "5.2"
        bi.copy_settings(source, target)
        self.assertEqual((target / "config" / "userpref.blend").read_bytes(), b"prefs 5.2")
        self.assertTrue((target / "scripts" / "addons" / "my_addon" / "__init__.py").is_file())
        self.assertTrue((target / "scripts" / "linked").is_symlink())
        self.assertEqual(sorted(p.relative_to(source) for p in source.rglob("*")), before)


if __name__ == "__main__":
    unittest.main()
