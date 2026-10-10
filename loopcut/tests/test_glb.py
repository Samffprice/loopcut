"""export_glb (glb.py): the .glb read back and checked against the scene before it replaces anything.

GPT-6.1 Sol (20260929-230207 O0011, O0021) wrote its own GLB parser twice and overwrote the file
before checking it. The file side is pure and tested here on hand-built files; the export itself
runs in harness/glb_check.py.
"""

import json
import struct
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

from loopcut import agent, files, glb, orchestrator  # noqa: E402
from loopcut.tools import ToolError  # noqa: E402


def glb_bytes(gltf: dict, binary: bytes = b"") -> bytes:
    text = json.dumps(gltf).encode()
    text += b" " * (-len(text) % 4)
    binary += b"\0" * (-len(binary) % 4)
    body = struct.pack("<II", len(text), glb.JSON_CHUNK) + text
    if binary:
        body += struct.pack("<II", len(binary), glb.BIN_CHUNK) + binary
    return struct.pack("<III", glb.MAGIC, 2, 12 + len(body)) + body


def crate(tris: int = 12, scale=(2, 1, 1)) -> dict:
    """A root empty with a box mesh child, as Blender's exporter writes one: Y-up, the child scaled."""
    return {
        "asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}],
        "nodes": [{"name": "Pack", "children": [1], "translation": [1, 0, 0]},
                  {"name": "Crate", "mesh": 0, "scale": list(scale)}],
        "meshes": [{"name": "Crate", "primitives": [{"attributes": {"POSITION": 0}, "indices": 1, "material": 0}]}],
        "materials": [{"name": "Wood"}],
        "accessors": [{"bufferView": 0, "componentType": 5126, "count": 24, "type": "VEC3",
                       "min": [-0.5, -0.25, -0.1], "max": [0.5, 0.25, 0.1]},
                      {"bufferView": 1, "componentType": 5123, "count": tris * 3, "type": "SCALAR"}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": 288},
                        {"buffer": 0, "byteOffset": 288, "byteLength": 72}],
        "buffers": [{"byteLength": 360}]}


BINARY = bytes(360)


class FileTest(unittest.TestCase):
    def test_a_good_file_says_what_it_holds_in_blenders_axes(self):
        gltf, binary = glb.parse(glb_bytes(crate(), BINARY))
        self.assertEqual(glb.validate(gltf, binary), [])
        found = glb.summary(gltf)
        self.assertEqual((found["meshes"], found["tris"], found["materials"], found["images"]), (1, 12, 1, 0))
        self.assertEqual(found["nodes"]["Pack"], {"tris": 0, "size": None})
        self.assertEqual([round(v, 6) for v in found["nodes"]["Crate"]["size"]], [2.0, 0.2, 0.5],
                         "glTF is Y-up: its z depth is Blender's y, its height Blender's z")
        self.assertEqual(glb.compare(found, {"Crate": {"tris": 12, "size": [2.0, 0.2, 0.5]}}), [])

    def test_what_differs_from_the_scene_is_said_per_object(self):
        found = glb.summary(glb.parse(glb_bytes(crate(tris=10), BINARY))[0])
        problems = glb.compare(found, {"Crate": {"tris": 12, "size": [2.0, 0.5, 0.2]}, "Barrel": {"tris": 4, "size": None}})
        self.assertEqual(problems, ["Crate: 10 triangles in the file, 12 in the scene",
                                    "Crate: 2 x 0.2 x 0.5 m in the file, 2 x 0.5 x 0.2 m in the scene",
                                    "Barrel: not in the file"])

    def test_a_broken_or_incomplete_file_is_named(self):
        gltf = crate()
        gltf["accessors"][1]["count"] = 60  # 120 bytes in a 72-byte view.
        gltf["images"] = [{"name": "Grain", "uri": "grain.png"}, {"name": "Paint", "bufferView": 7}]
        gltf["nodes"].append({"name": "Ghost", "mesh": 3})
        problems = glb.validate(*glb.parse(glb_bytes(gltf, BINARY)))
        self.assertEqual(problems, ["accessor 1 (60 x SCALAR) runs past its buffer view",
                                    "image Grain is outside the file (grain.png)", "image Paint has no data in the file",
                                    "node Ghost names no mesh"])
        with self.assertRaisesRegex(ValueError, "no glTF header"):
            glb.parse(b"PK\x03\x04" + bytes(40))
        whole = glb_bytes(crate(), BINARY)
        with self.assertRaisesRegex(ValueError, "the header says"):
            glb.parse(whole[:-8])
        short = glb_bytes(crate(), BINARY[:100])
        self.assertIn("buffer 0 is 360 bytes; the binary chunk has 100", glb.validate(*glb.parse(short)))

    def test_lines_and_points_are_warned_about(self):
        gltf = crate()
        gltf["meshes"][0]["primitives"].append({"attributes": {"POSITION": 0}, "mode": 1})
        found = glb.summary(gltf)
        self.assertEqual((found["tris"], found["warnings"]), (12, ["Crate: lines (not triangles)"]))

    def test_the_report_is_evidence_measured_in_the_file(self):
        found = glb.summary(crate())
        text = glb.report("/p/pack.glb", 4096, found, {"Crate": {"tris": 12, "size": [2, 0.2, 0.5]}},
                          ["glTF leaves out Wood: Noise Texture (bake it to an image to keep the look)"], True)
        self.assertEqual(text.splitlines(), [
            "Exported /p/pack.glb (4 KB), replacing the earlier file: 1 meshes, 12 triangles, 1 materials, 0 images "
            "embedded; 1 objects match the scene in name, triangles and size.",
            "  Crate: 12 triangles, 2 x 0.2 x 0.5 m",
            "Warning: glTF leaves out Wood: Noise Texture (bake it to an image to keep the look)"])


class ExportTest(unittest.TestCase):
    """The transaction, with the exporter and the scene stubbed: the file on disk changes only after the
    new one checks out."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.target = Path(self.folder.name).resolve() / "pack.glb"
        self.written = glb_bytes(crate(), BINARY)
        for name, value in (("_targets", lambda objects: ["Crate"]), ("dropped_nodes", lambda objects: []),
                            ("_expected", lambda objects: {"Crate": {"tris": 12, "size": [2.0, 0.2, 0.5]}}),
                            ("_export", lambda path, objects: Path(path).write_bytes(self.written))):
            patcher = mock.patch.object(glb, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def leftovers(self) -> list:
        return sorted(p.name for p in Path(self.folder.name).iterdir() if p.name != "pack.glb")

    def test_a_good_export_lands_under_its_name_and_says_so(self):
        result = glb.export_glb(str(self.target))
        self.assertTrue(result.ok, result.text)
        self.assertEqual(self.target.read_bytes(), self.written)
        self.assertIn("1 objects match the scene", result.text)
        self.assertEqual(self.leftovers(), [], "no temporary file is left")
        with self.assertRaisesRegex(ToolError, "pack.glb already exists"):  # No extension: the .glb.
            glb.export_glb(str(self.target.with_suffix("")))

    def test_an_existing_file_needs_overwrite_and_a_failed_check_leaves_it_as_it_was(self):
        self.target.write_bytes(b"the earlier export")
        with self.assertRaisesRegex(ToolError, "already exists; pass overwrite=true"):
            glb.export_glb(str(self.target))
        self.written = glb_bytes(crate(tris=10), BINARY)
        result = glb.export_glb(str(self.target), overwrite=True)
        self.assertFalse(result.ok)
        self.assertEqual(result.text.splitlines(), [f"Not exported: the new file failed its checks, so {self.target} "
                                                    f"is as it was.", "- Crate: 10 triangles in the file, 12 in the scene"])
        self.assertEqual(self.target.read_bytes(), b"the earlier export")
        self.assertEqual(self.leftovers(), [])
        self.written = b"not a glb at all, just some bytes"
        self.assertIn("- not a .glb (no glTF header)", glb.export_glb(str(self.target), overwrite=True).text)
        self.written = glb_bytes(crate(), BINARY)
        replaced = glb.export_glb(str(self.target), overwrite=True)
        self.assertIn("replacing the earlier file", replaced.text)
        self.assertEqual(self.target.read_bytes(), self.written)

    def test_an_exporter_failure_leaves_nothing_behind(self):
        def fails(path, objects):
            Path(path).write_bytes(b"half")
            raise RuntimeError("no mesh data")
        with mock.patch.object(glb, "_export", fails):
            result = glb.export_glb(str(self.target))
        self.assertEqual((result.ok, self.target.exists(), self.leftovers()), (False, False, []))
        self.assertIn("the exporter failed (no mesh data)", result.text)
        with self.assertRaisesRegex(ToolError, "writes .glb files"):
            glb.export_glb(str(Path(self.folder.name) / "pack.fbx"))


class OfferTest(unittest.TestCase):
    def test_the_orchestrator_is_offered_it_asked_first_and_told_to_use_it(self):
        names = [s["function"]["name"] for s in orchestrator.SCHEMAS]
        self.assertIn("export_glb", names)
        self.assertIs(orchestrator.DISPATCH["export_glb"], glb.export_glb)
        self.assertNotIn("export_glb", [s["function"]["name"] for s in agent._tool_schemas()],
                         "the orchestrator's: workers and the single agent are not offered it")
        self.assertTrue(files.needs_approval("export_glb", {"path": "//pack.glb"}, ()), "it writes a file")
        self.assertEqual(orchestrator.describe("export_glb", {"path": "//pack.glb", "overwrite": True}),
                         ("Export (replacing) every deliverable to //pack.glb", ""))



if __name__ == "__main__":
    unittest.main()
