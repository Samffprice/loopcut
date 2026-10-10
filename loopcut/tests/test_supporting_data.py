"""Text blocks and images: what ownership lets a worker make and own, the merge's manifest of them as
the orchestrator reads it, and Scene changes for text edits and paint (eval 20260930-154037: the Arcane
chest painter's ArcaneRecipe never reached the live scene, owns refused its name, and paint packed in
the same call read "Scene changes: none"). No Blender: the real append, pack and verification are
harness/merge_check.py's support wave, the child's tracking harness/worker_host_check.py's."""

import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

fake_bpy = types.ModuleType("bpy")
fake_bpy.app = types.SimpleNamespace(driver_namespace={})
sys.modules.setdefault("bpy", fake_bpy)

import test_orchestrator as base  # noqa: E402  (its wave and live scene; importing it registers no tests here)
from loopcut import merge, orchestrator, ownership, scene_diff, tools, waves  # noqa: E402


def made(id_type: str, kind: str = "", **fields):
    return types.SimpleNamespace(id_type=id_type, type=kind, **fields)


class OwnershipTest(unittest.TestCase):
    def test_any_role_makes_text_blocks_and_ownership_knows_them(self):
        for role in ownership.ROLES:
            self.assertTrue(ownership.allowed(role, made("TEXT")), role)
        self.assertIn("texts", ownership.ID_COLLECTIONS, "stamped, tokened, written and appended like the rest")
        self.assertEqual(ownership._ATTR_OF["TEXT"], "texts", "find('TEXT:ArcaneRecipe')")
        self.assertEqual(ownership.SUPPORT, ("TEXT", "IMAGE"))
        self.assertIn("texts", merge.KEEP_UNUSED, "a text nothing uses is still in the worker's copy")

    def test_a_text_or_a_painted_image_merges_though_nothing_uses_it(self):
        image = dict(users=0, packed_file=None, is_dirty=False)
        self.assertTrue(ownership._kept(made("TEXT", users=0)))
        self.assertTrue(ownership._kept(made("IMAGE", **{**image, "is_dirty": True})), "unsaved paint: packed at finish")
        self.assertTrue(ownership._kept(made("IMAGE", **{**image, "packed_file": object()})))
        self.assertFalse(ownership._kept(made("IMAGE", **image)), "a blank image never used: skipped, and said so")
        self.assertFalse(ownership._kept(made("MATERIAL", users=0)), "as before: a material nobody wears")

    def test_about_says_lines_or_size_and_where(self):
        text = made("TEXT", lines=[1, 2, 3])
        self.assertEqual(ownership.support_about(text), "3 lines")
        atlas = made("IMAGE", size=(4096, 4096), has_data=True, packed_file=object(), filepath="", source="FILE")
        self.assertEqual(ownership.support_about(atlas), "4096x4096 packed")
        unloaded = made("IMAGE", size=(0, 0), has_data=False, packed_file=None, filepath="//t.png", source="FILE")
        self.assertEqual(ownership.support_about(unloaded), "file")


class OwnsTest(unittest.TestCase):
    """O0014-O0016: "ArcaneRecipe was refused in owns; it was unclear whether it was absent or unsupported"."""

    def test_owns_accepts_a_text_block_the_scene_has(self):
        self.assertIn("texts", orchestrator._ID_COLLECTIONS, "live_facts' names include text blocks")
        wave = base.reference_wave()
        wave["workers"][0]["owns"] = ["ArcaneRecipe"]
        live = {**base.LIVE, "names": base.LIVE["names"] + ["ArcaneRecipe"]}
        self.assertEqual(base.refusals(wave, live=live), [])
        missing = base.refusals(wave)
        self.assertEqual(len(missing), 1, missing)
        self.assertIn("'ArcaneRecipe' is not in the scene", missing[0])


def report(**support_fields) -> dict:
    """render_worker's input, with nothing but the texts and images to say."""
    return {"worker": "painter", "deliverable": "Chest", "role": "materials", "status": "done", "steps": 9,
            "cap_hit": False, "note": "", "checks": {"passed": 1, "failed": 0, "fail_lines": [], "deferred": []},
            "rollbacks": 0, "question": None, "change_requests": [], "facts": {}, "flags": [], "advisory": [],
            "summary": "Painted the chest.", "synthesised": False, "outside_role": [],
            "merge": {"conflicts": [], "dropped_edits": []}, "created": ["Chest", "MAT Chest_Paint"], "numbers": {},
            **support_fields}


class ManifestTest(unittest.TestCase):
    ENTRIES = [
        {"worker": "painter", "key": "TEXT:ArcaneRecipe", "kind": "TEXT", "name": "ArcaneRecipe", "state": "new",
         "about": "212 lines", "hash": "1a2b3c4d5e6f"},
        {"worker": "painter", "key": "IMAGE:Chest_Atlas", "kind": "IMAGE", "name": "Chest_Atlas", "state": "updated",
         "about": "4096x4096 packed", "hash": "9f8e7d6c5b4a"},
        {"worker": "painter", "key": "TEXT:Painter", "kind": "TEXT", "name": "Painter.001", "state": "new",
         "about": "40 lines", "hash": "abcdef123456", "why": "Painter was taken, so Blender named it Painter.001"},
        {"worker": "painter", "key": "IMAGE:Scratch", "kind": "IMAGE", "name": "Scratch", "state": "skipped",
         "about": "32x32 generated", "why": "unused and never painted"},
        {"worker": "painter", "key": "TEXT:Notes", "kind": "TEXT", "name": "Notes", "state": "missing",
         "about": "3 lines", "hash": "000111222333", "why": "not in the scene after the merge"},
    ]

    def test_each_line_says_what_happened_with_a_short_hash(self):
        lines = [waves.support_text(e) for e in self.ENTRIES]
        self.assertEqual(lines, [
            "text ArcaneRecipe new #1a2b3c (212 lines)",
            "image Chest_Atlas updated #9f8e7d (4096x4096 packed)",
            "text Painter.001 new #abcdef (40 lines, Painter was taken, so Blender named it Painter.001)",
            "image Scratch not merged: unused and never painted",
            "text Notes MISSING: not in the scene after the merge"])
        self.assertEqual(waves.support_text({"worker": "painter", "more": 3}), "+3 more")
        self.assertEqual(waves.support_text({"worker": "painter", "error": "MemoryError: x"}), "not listed (MemoryError: x)",
                         "a listing that failed costs the result nothing, and says so")

    def test_the_orchestrator_reads_one_line_and_created_no_longer_repeats_them(self):
        block = waves.render_worker(report(support=self.ENTRIES[:2]))
        self.assertIn("\n  texts and images: text ArcaneRecipe new #1a2b3c (212 lines); image Chest_Atlas updated "
                      "#9f8e7d (4096x4096 packed)\n", block)
        self.assertNotIn("texts and images", waves.render_worker(report(support=[])), "nothing to say: no line")
        worker = types.SimpleNamespace(id="painter", manifest={"created": ["OBJECT:Chest", "MATERIAL:Chest_Paint",
                                                                           "IMAGE:Chest_Atlas", "TEXT:ArcaneRecipe"]})
        self.assertEqual(waves._created(worker), ["Chest", "MAT Chest_Paint"])
        self.assertEqual(waves._created_names(worker), ["Chest", "Chest_Paint", "Chest_Atlas", "ArcaneRecipe"],
                         "a continued brief still owns them (orchestrator._repair_continues)")

    def test_with_no_merge_the_childs_own_list_says_it_is_not_live(self):
        worker = types.SimpleNamespace(id="painter", manifest={"support": [
            {"key": "TEXT:ArcaneRecipe", "state": "new", "about": "2 lines", "hash": "1a2b3c4d5e6f", "token": "t"}]})
        self.assertEqual([waves.support_text(e) for e in waves._support(worker)],
                         ["text ArcaneRecipe in its copy only, not merged #1a2b3c (2 lines)"])
        merged = types.SimpleNamespace(support=[{**self.ENTRIES[0], "worker": "other"}])
        self.assertEqual(waves._support(worker, merged), [], "after a merge: what the merge found, for this worker")

    def test_the_merge_report_names_what_went_missing_but_not_a_conflict_twice(self):
        conflict = {**self.ENTRIES[4], "key": "TEXT:Recipe", "name": "Recipe", "conflict": True,
                    "why": "changed in the live scene during the wave; the live version was kept"}
        result = merge.MergeReport(support=self.ENTRIES + [conflict])
        lines = result.lines()
        self.assertIn("painter: text Notes is missing after the merge: not in the scene after the merge", lines)
        self.assertFalse([line for line in lines if "Recipe" in line], "its conflict line says it already")
        self.assertIn("new TEXT:ArcaneRecipe", result.log()["support"])


def shot(images=None, texts=None) -> dict:
    """A snapshot as scene_diff.snapshot makes it; texts given: images lists every local image."""
    found = {"too_many": False, "objects": {}, "object_count": 0, "materials": {}, "collections": {},
             "scene": {}, "mode": "OBJECT", "images": images or {}}
    if texts is not None:
        found["texts"] = texts
    return found


def image(**fields) -> dict:
    return {"size": (4096, 4096), "pixels": None, "packed": "", "dirty": False, "materials": ("Chest_Paint",), **fields}


class SceneChangesTest(unittest.TestCase):
    def test_text_blocks_written_edited_and_removed(self):
        before = shot(texts={"Notes": (3, "aa"), "Old": (1, "bb")})
        after = shot(texts={"Notes": (5, "cc"), "ArcaneRecipe": (212, "dd")})
        changes = scene_diff.diff(before, after)
        self.assertIn("text ArcaneRecipe (212 lines)", changes["added"])
        self.assertIn("text Old", changes["removed"])
        self.assertIn("text Notes: edited (3 -> 5 lines)", changes["changed"])
        same_count = scene_diff.diff(shot(texts={"Notes": (3, "aa")}), shot(texts={"Notes": (3, "ab")}))
        self.assertEqual(same_count["changed"], ["text Notes: edited (3 lines)"])
        self.assertTrue(scene_diff.is_empty(scene_diff.diff(before, before)))

    def test_paint_packed_in_the_same_call_past_the_hash_budget(self):
        """W0039-W0041: a 4K atlas painted and packed in one call read "none"."""
        packed = scene_diff.diff(shot({"Atlas": image(packed="50:aa")}, {}), shot({"Atlas": image(packed="51:bb")}, {}))
        self.assertEqual(packed["changed"], ["image Atlas: pixels edited (packed again) (in Chest_Paint)"])
        first = scene_diff.diff(shot({"Atlas": image()}, {}), shot({"Atlas": image(packed="50:aa")}, {}))
        self.assertEqual(first["changed"], ["image Atlas: packed (in Chest_Paint)"])
        hashed = scene_diff.diff(shot({"Atlas": image(pixels="p1")}, {}), shot({"Atlas": image(pixels="p2")}, {}))
        self.assertEqual(hashed["changed"], ["image Atlas: pixels edited (in Chest_Paint)"])
        self.assertGreaterEqual(scene_diff.MAX_IMAGE_PIXELS, 4096 * 4096, "a 4K atlas is hashed")

    def test_new_and_unused_images_are_listed_once_both_snapshots_have_every_image(self):
        mask = image(size=(1024, 1024), materials=())
        added = scene_diff.diff(shot({}, {}), shot({"Mask": mask}, {}))
        self.assertEqual(added["added"], ["image Mask (1024x1024)"])
        painted = scene_diff.diff(shot({"Mask": mask}, {}), shot({"Mask": {**mask, "dirty": True}}, {}))
        self.assertEqual(painted["changed"], ["image Mask: pixels edited (unsaved)"], "no material to name")
        self.assertEqual(scene_diff.diff(shot({"Mask": mask}, {}), shot({}, {}))["removed"], ["image Mask"])
        self.assertTrue(scene_diff.is_empty(scene_diff.diff(shot({}), shot({"Mask": mask}))),
                        "an older snapshot lists only the images materials use: no added or removed from it")

    def test_what_cannot_be_compared_is_named_not_silent(self):
        dirty = image(dirty=True)
        changes = scene_diff.diff(shot({"Atlas": dirty}, {}), shot({"Atlas": dirty}, {}))
        self.assertTrue(scene_diff.is_empty(changes))
        text = scene_diff.for_model(changes)
        self.assertIn("Scene changes: none", text)
        self.assertIn("Not compared: the pixels of Atlas", text)
        self.assertIn("text blocks", text)
        self.assertIn("not custom properties, keyframes, drivers, constraints", text)
        self.assertNotIn("Not compared", scene_diff.for_model(scene_diff.diff(shot({}, {}), shot({}, {}))))
        hashed = image(dirty=True, pixels="p1")
        self.assertEqual(scene_diff.diff(shot({"Atlas": hashed}, {}), shot({"Atlas": hashed}, {}))["unseen"], [])


class RunPythonTest(unittest.TestCase):
    def test_the_description_says_each_call_is_a_new_namespace(self):
        """W0028-W0030: a retry assumed its variables persisted between calls."""
        description = next(s["function"]["description"] for s in tools.SCHEMAS if s["function"]["name"] == "run_python")
        self.assertIn("a new namespace each call", description)
        self.assertIn("Text block", description)


if __name__ == "__main__":
    unittest.main()
