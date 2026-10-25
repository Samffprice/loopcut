"""The shared library plumbing and the Blender-asset-library tools' pure parts: ranking, verified
downloads (over file:// so no network), listing parsing, refs, index metadata. Real libraries are
covered by harness/asset_libraries_check.py."""

import contextlib
import hashlib
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

fake_bpy = types.ModuleType("bpy")
fake_bpy.app = types.SimpleNamespace(driver_namespace={})
sys.modules.setdefault("bpy", fake_bpy)

from loopcut import asset_common, asset_libraries, assets, tools  # noqa: E402
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

    def test_a_body_that_breaks_off_is_the_tools_error_not_the_turns(self):
        # 2026-10-25: a BlenderKit thumbnail's read timed out, escaped thumbnail() and ended the turn as
        # "the connection to the model dropped".
        import http.client
        from unittest import mock

        class Breaking:
            def __init__(self, error):
                self.error, self.sent = error, False

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, size=-1):
                if not self.sent and size > 0:  # A download's first chunk arrives; a whole-body read breaks.
                    self.sent = True
                    return b"RIFF"
                raise self.error

        for error in (TimeoutError("The read operation timed out"), ConnectionResetError(54, "Connection reset by peer"),
                      http.client.IncompleteRead(b"x", 10)):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as tmp:
                dest = Path(tmp) / "thumbs" / "a.webp"
                with mock.patch.object(asset_common.urllib.request, "urlopen", lambda *a, **k: Breaking(error)):
                    with self.assertRaises(tools.ToolError) as caught:
                        asset_common.download("https://www.blenderkit.com/thumbs/a.webp", dest)
                    self.assertTrue(str(caught.exception).startswith(
                        "Lost the connection to www.blenderkit.com while reading its answer ("
                        f"{type(error).__name__}: "), str(caught.exception))
                    self.assertFalse(dest.exists() or dest.with_suffix(".webp.part").exists(), "no half file left")
                    self.assertIsNone(asset_common.thumbnail("https://www.blenderkit.com/thumbs/a.webp", dest))
                    with self.assertRaises(tools.ToolError) as caught:
                        asset_common.get_json("https://www.blenderkit.com/api/v1/search/")
                    self.assertIn("Lost the connection to www.blenderkit.com", str(caught.exception))


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
        self.assertEqual(line, "#3 Oak (library: Ess); Worn planks; wood, floor; CC0; id Ess::m/oak.blend::MATERIAL::Oak")
        # Matches what Blender 5.2 made for this URL in a live session.
        self.assertEqual(asset_libraries.remote_cache_dirname("https://blender.ambientcg.com/"), "6f18014bec4cad69")

    def test_labels_and_bad_arguments(self):
        self.assertEqual(assets.describe("import_asset", {"source": "library", "id": "L::f.blend::WORLD::City"}),
                         ("Import asset City", ""))
        self.assertEqual(assets.describe("search_assets", {"source": "library", "query": "oak"}),
                         ("Search asset libraries: oak", ""))
        self.assertIn("import_asset", tools.CHANGES_SCENE)
        result = tools.execute("import_asset", json.dumps({"source": "library", "id": "garbage"}))
        self.assertFalse(result.ok)
        self.assertIn("library::file::TYPE::name", result.text)
        result = tools.execute("search_assets", json.dumps({"source": "library", "query": "x", "type": "BRUSH"}))
        self.assertFalse(result.ok)
        self.assertIn("OBJECT, COLLECTION", result.text)



class UndoStepTest(unittest.TestCase):
    def test_an_import_is_one_undo_step_from_after_its_download_also_when_its_load_fails_part_way(self):
        """0.2.18: a read-only step, then import_asset, then Undo crashed Blender: nothing pushed after the
        import. Every library's load pushes its Before (tools.undo_before) and execute the one after."""
        order = []
        push = mock.Mock(side_effect=lambda message: order.append(message))
        ref = "Kit::kit.blend::OBJECT::Stool"
        fetched = {"ref": ref, "library": "Kit", "blend": "/nowhere/kit.blend", "id_type": "OBJECT", "name": "Stool",
                   "link": False, "reuse": False, "license": "CC0", "author": ""}

        def fetch(ref, apply_to=None):
            order.append("download")
            return fetched

        load = mock.Mock(side_effect=tools.ToolError("Blender could not load 'Stool' from kit.blend"))
        with mock.patch.object(tools, "bpy", types.SimpleNamespace(ops=types.SimpleNamespace(
                    ed=types.SimpleNamespace(undo_push=push)))), \
                mock.patch.object(tools, "_editor_context", side_effect=lambda _: contextlib.nullcontext()), \
                mock.patch.object(asset_libraries, "bpy", types.SimpleNamespace(data=types.SimpleNamespace(
                    objects=[], libraries=types.SimpleNamespace(load=load)))):
            with mock.patch.object(asset_libraries, "fetch_asset", side_effect=fetch):
                result = tools.execute("import_asset", json.dumps({"source": "library", "id": ref}))
            self.assertFalse(result.ok)
            self.assertEqual(order, ["download", "Before Loopcut: import asset", "Loopcut: import asset"])
            order.clear()
            with mock.patch.object(asset_libraries, "fetch_asset", side_effect=tools.ToolError("download failed")):
                self.assertFalse(tools.execute("import_asset", json.dumps({"source": "library", "id": ref})).ok)
            self.assertEqual(order, [], "a failed download changed nothing: no undo step")
        self.assertEqual(tools._undo_owed, [])


if __name__ == "__main__":
    unittest.main()
