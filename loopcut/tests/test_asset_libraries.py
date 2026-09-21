"""The shared library plumbing and the Blender-asset-library tools' pure parts: ranking, verified
downloads (over file:// so no network), listing parsing, refs, index metadata. Real libraries are
covered by harness/asset_libraries_check.py."""

import hashlib
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

fake_bpy = types.ModuleType("bpy")
fake_bpy.app = types.SimpleNamespace(driver_namespace={})
sys.modules.setdefault("bpy", fake_bpy)

from loopcut import asset_common, asset_libraries, tools  # noqa: E402
from loopcut.asset_libraries import Entry  # noqa: E402

PAGE = {
    "assets": [
        {"name": "City", "id_type": "WORLD", "files": ["world_hdri/city.blend"], "bl_versions": {"min": "5.2"},
         "thumbnail": {"url": "world_hdri/thumbs/city.webp", "hash": "SHA256:ab"},
         "meta": {"author": "Greg Zaal", "license": "CC0 - Public Domain", "tags": ["urban", "day"]}},
        {"name": "Oak Floor", "id_type": "MATERIAL", "files": ["materials/oak.blend"], "bl_versions": {"min": "5.2"},
         "meta": {"description": "Worn oak planks"}},
        {"name": "Sculpt Clay", "id_type": "BRUSH", "files": ["brushes/clay.blend"], "bl_versions": {"min": "5.2"}},
        {"name": "Loose", "id_type": "OBJECT", "files": ["x.usd"], "bl_versions": {"min": "5.2"}},
    ],
    "files": [
        {"path": "world_hdri/city.blend", "size_in_bytes": 12, "hash": "SHA256:cd", "blender_version": "5.2.0"},
    ],
}


class RankAndDownloadTest(unittest.TestCase):
    def test_rank_counts_word_hits_then_name_hits_then_weight(self):
        items = [("a", "wooden chair oak", 5), ("b", "wooden table", 1), ("c", "table wooden top", 9)]
        found = asset_common.rank("wooden table", items, text_of=lambda i: i[1], name_of=lambda i: i[0],
                                  weight_of=lambda i: i[2])
        self.assertEqual([i[0] for i in found], ["c", "b", "a"])
        self.assertEqual(asset_common.rank("zzz", items, text_of=lambda i: i[1]), [])

    def test_download_over_file_url_verifies_sha256_and_md5_and_reuses_a_good_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "src.bin"
            source.write_bytes(b"hello asset")
            sha = hashlib.sha256(b"hello asset").hexdigest()
            dest = Path(tmp) / "cache" / "a.bin"
            got = asset_common.download(source.as_uri(), dest, f"SHA256:{sha}", 11)
            self.assertEqual(got.read_bytes(), b"hello asset")
            stamp = dest.stat().st_mtime_ns
            asset_common.download(source.as_uri(), dest, f"md5:{hashlib.md5(b'hello asset').hexdigest()}")
            self.assertEqual(dest.stat().st_mtime_ns, stamp, "a verified copy is not fetched again")
            with self.assertRaises(tools.ToolError):
                asset_common.download(source.as_uri(), Path(tmp) / "b.bin", "SHA256:" + "0" * 64)
            self.assertFalse((Path(tmp) / "b.bin").exists())
            self.assertFalse((Path(tmp) / "b.bin.part").exists())

    def test_missing_file_is_a_readable_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(tools.ToolError):
                asset_common.download((Path(tmp) / "nope").as_uri(), Path(tmp) / "x")


class ListingTest(unittest.TestCase):
    def test_page_entries_keep_scene_assets_in_blend_files_only(self):
        entries = asset_libraries.entries_from_page("Ess", "https://x.test/lib/", PAGE)
        self.assertEqual([(e.id_type, e.name) for e in entries], [("WORLD", "City"), ("MATERIAL", "Oak Floor")])
        city = entries[0]
        self.assertEqual(city.thumbnail_url, "https://x.test/lib/world_hdri/thumbs/city.webp")
        self.assertEqual(city.files, [{"path": "world_hdri/city.blend", "url": "https://x.test/lib/world_hdri/city.blend",
                                       "hash": "SHA256:cd", "size": 12}])
        self.assertEqual(city.ref, "Ess::world_hdri/city.blend::WORLD::City")
        self.assertIn("urban", city.text())
        self.assertEqual(entries[1].files[0]["hash"], None, "a file the table omits still has a URL")

    def test_ref_round_trip_and_rejections(self):
        self.assertEqual(asset_libraries.parse_ref("Ess::world_hdri/city.blend::WORLD::City"),
                         ("Ess", "world_hdri/city.blend", "WORLD", "City"))
        for bad in ("nope", "a::b::BRUSH::c", "a::../b.blend::WORLD::c", "a::/etc/x.blend::WORLD::c", "::b::WORLD::c"):
            with self.assertRaises(tools.ToolError, msg=bad):
                asset_libraries.parse_ref(bad)

    def test_index_metadata_strips_id_codes(self):
        index = {"entries": [{"name": "MAOak", "tags": ["wood"], "description": "planks", "license": "CC0"},
                             {"name": "BRClay", "tags": ["x"]}, {"name": "OBChair", "author": "me"}]}
        found = asset_libraries.index_metadata(index)
        self.assertEqual(set(found), {("MATERIAL", "Oak"), ("OBJECT", "Chair")})
        self.assertEqual(found[("MATERIAL", "Oak")]["tags"], ["wood"])
        self.assertEqual(found[("OBJECT", "Chair")]["author"], "me")

    def test_description_and_remote_dirname(self):
        entry = Entry("Ess", "m/oak.blend", "MATERIAL", "Oak", tags=["wood", "floor"], description="Worn planks", license="CC0")
        line = asset_libraries.describe_entry(3, entry)
        self.assertTrue(line.startswith("3. Ess::m/oak.blend::MATERIAL::Oak; Worn planks; wood, floor; CC0"), line)
        # Matches what Blender 5.2 made for this URL in a live session.
        self.assertEqual(asset_libraries.remote_cache_dirname("https://blender.ambientcg.com/"), "6f18014bec4cad69")

    def test_labels_and_bad_arguments(self):
        self.assertEqual(asset_libraries.describe("import_asset", {"ref": "L::f.blend::WORLD::City"}), ("Import asset City", ""))
        self.assertEqual(asset_libraries.describe("search_assets", {"query": "oak"}), ("Search asset libraries: oak", ""))
        self.assertIn("import_asset", tools.CHANGES_SCENE)
        result = tools.execute("import_asset", json.dumps({"ref": "garbage"}))
        self.assertFalse(result.ok)
        self.assertIn("library::file::TYPE::name", result.text)
        result = tools.execute("search_assets", json.dumps({"query": "x", "type": "BRUSH"}))
        self.assertFalse(result.ok)
        self.assertIn("OBJECT, COLLECTION", result.text)


if __name__ == "__main__":
    unittest.main()
