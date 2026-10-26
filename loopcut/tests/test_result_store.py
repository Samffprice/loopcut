"""Long tool results cut as they are made: the head and a pointer for the model, the whole in the conversation's
store, read_result for the rest. Pure: no Blender."""

import json
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

fake_bpy = types.ModuleType("bpy")
fake_bpy.app = types.SimpleNamespace(driver_namespace={})
sys.modules.setdefault("bpy", fake_bpy)

from loopcut import context, result_store, scene_diff  # noqa: E402

LIMIT = result_store.LIMIT
POINTER = re.compile(r'\[Loopcut: ([\d,]+) more characters stored as (r\d+|r\x00): read_result\("\2", grep="…"\) '
                     r'or lines="(\d+)-(\d+)"\]')


def prints(count: int, width: int = 60) -> str:
    return "\n".join(f"row {n:03d} " + "x" * (width - 8) for n in range(1, count + 1))


def changes(*lines: str) -> str:
    return "Scene changes (+ added, - removed, ~ changed; bounds are world space):\n" + "\n".join(lines)


def rotated(count: int) -> list[str]:
    """count objects given one rotation, each with its own bounds, as scene_diff writes them."""
    return [f"~ Rig part {n}: rotation_deg (0.0, 0.0, 0.0) -> (0.0, 0.0, 90.0); "
            f"bounds now [{n}.0, 0.0, 0.0]..[{n}.5, 1.0, {n % 3}.25]" for n in range(count)]


class CutTest(unittest.TestCase):
    def test_a_short_result_is_shown_whole_and_not_stored(self):
        self.assertEqual(result_store.cut("Cube added.", "run_python", "r1"), ("Cube added.", None))
        text = prints(19)
        self.assertLessEqual(len(text), LIMIT)
        self.assertEqual(result_store.cut(text, "shell", "r1"), (text, None))

    def test_a_long_result_shows_whole_lines_then_one_pointer_to_the_rest(self):
        text = prints(200)
        shown, stored = result_store.cut(text, "shell", "r4")
        self.assertEqual(stored, text)
        self.assertLessEqual(len(shown), LIMIT + 120)
        head, mark = shown.rsplit("\n", 1)
        self.assertTrue(text.startswith(head + "\n"), "the head is the result's own first lines, whole")
        found = POINTER.fullmatch(mark)
        self.assertIsNotNone(found, mark)
        more, ref, first, last = found.groups()
        self.assertEqual(ref, "r4")
        self.assertEqual(int(first), head.count("\n") + 2, "the range starts at the first line not shown")
        self.assertEqual(int(last) - int(first) + 1, result_store.READ_LINES)
        self.assertEqual(int(more.replace(",", "")), len(text) - len(head))

    def test_a_failed_call_keeps_its_exception_and_last_frames_after_the_pointer(self):
        trace = ("Traceback (most recent call last):\n  File \"<loopcut>\", line 212, in <module>\n"
                 "    bevel.segments = 'three'\nTypeError: bpy_struct: item.attr = val: expected an int\n"
                 "NOTE: Closest on BevelModifier: segments.")
        text = prints(100) + "\n" + trace
        shown, stored = result_store.cut(text, "run_python", "r2", failed=True)
        self.assertEqual(stored, text)
        self.assertTrue(shown.endswith(trace), "the error, its frames and Loopcut's note stay in view")
        self.assertTrue(shown.startswith("row 001"))
        self.assertEqual(len(POINTER.findall(shown)), 1)
        self.assertLess(shown.index("[Loopcut:"), shown.index("Traceback"))
        self.assertLessEqual(len(shown), LIMIT + 120)

    def test_a_short_closing_paragraph_stays_in_view(self):
        note = "This call changed Rug, which your brief doesn't own, and it was rolled back."
        shown, _ = result_store.cut(prints(100) + "\n\n" + note, "uv_unwrap", "r3")
        self.assertTrue(shown.endswith("\n" + note))
        self.assertIn("[Loopcut:", shown)

    def test_run_python_keeps_its_scene_changes_and_what_follows_them(self):
        text = (prints(80) + "\n\nLoopcut note: Mesh.from_pydata takes faces as tuples.\n\n"
                + changes("+ Shelf (mesh) at [0.0, 0.0, 0.0]..[1.0, 0.3, 2.0]") + "\n\nImage attached: three_quarter")
        shown, stored = result_store.cut(text, "run_python", "r5")
        self.assertEqual(stored, text)
        self.assertIn("\n\nLoopcut note: Mesh.from_pydata", shown, "Loopcut's note on the code stays")
        self.assertTrue(shown.endswith(changes("+ Shelf (mesh) at [0.0, 0.0, 0.0]..[1.0, 0.3, 2.0]")
                                       + "\n\nImage attached: three_quarter"))
        outcome, why, printed, lines = context._run_python_parts(shown)
        self.assertEqual((outcome, lines), ("ok", ["+ Shelf (mesh) at [0.0, 0.0, 0.0]..[1.0, 0.3, 2.0]"]),
                         "a step's record still reads the result")
        self.assertEqual(result_store.ref_in(shown), "r5")

    def test_a_folded_steps_record_names_where_its_whole_result_is(self):
        def record(name, arguments, text):
            call = {"id": "c1", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
            return context._call_lines(call, {"content": result_store.cut(text, name, "r5")[0]})[0]
        line = record("inspect_api", {"path": "Object"}, "bpy.types.Object(ID)\n" + prints(100))
        self.assertTrue(line.endswith(" Its whole result: r5."), line)
        self.assertNotIn("[Loopcut:", line)
        line = record("run_python", {"summary": "list"}, prints(100) + "\n\n" + changes("+ Shelf (mesh)"))
        self.assertIn("Scene changes: + Shelf (mesh). Its whole result: r5.", line)
        self.assertNotIn("Its whole", record("inspect_api", {"path": "Object"}, "bpy.types.Object(ID)"))

    def test_nothing_changed_is_never_cut_away(self):
        none = scene_diff.for_model({"added": [], "removed": [], "changed": [], "other": []})
        shown, _ = result_store.cut(prints(100) + "\n\n" + none, "run_python", "r1")
        self.assertTrue(shown.endswith("\n\n" + none))
        self.assertEqual(context._run_python_parts(shown)[3], None, "the record still says nothing changed")

    def test_many_scene_changes_are_grouped_and_capped_and_every_kind_is_named(self):
        listed = ([f"+ Pipe{'.%03d' % n if n else ''} (curve) at [{n}.0, 0.0, 0.0]..[{n}.5, 0.2, 3.0]"
                   for n in range(25)] + rotated(12)
                  + [f"~ Crate {n}: location (0.0, 0.0, 0.0) -> ({n}.0, 1.0, 0.0); bounds now [{n}.0, 0.0, 0.0]..[{n}.5, "
                     f"2.0, 1.0]" for n in range(30)]
                  + ["~ material Wood: Principled BSDF.Roughness 0.5 -> 0.3", "- Old lamp (light)",
                     "~ scene frame_end 250 -> 120"])
        text = "OK (no output)\n\n" + changes(*listed)
        shown, stored = result_store.cut(text, "run_python", "r9")
        self.assertEqual(stored, text)
        section = shown.split("\n\nScene changes", 1)[1]
        self.assertIn("+ 25 curve objects (Pipe … Pipe.024) at [0.0, 0.0, 0.0]..[24.5, 0.2, 3.0] together", section)
        self.assertIn("~ 12 objects (Rig part 0, Rig part 1, Rig part 2 and 9 more): rotation_deg (0.0, 0.0, 0.0) -> "
                      "(0.0, 0.0, 90.0); bounds now within [0.0, 0.0, 0.0]..[11.5, 1.0, 2.25]", section)
        overflow = section.splitlines()[-2]
        self.assertTrue(overflow.startswith("... and "), overflow)
        self.assertIn(" in r9: ", overflow)
        for kind in ("changed", "location", "material", "scene", "removed 1 (light)"):
            self.assertIn(kind, overflow)
        self.assertLessEqual(len(section), result_store.CHANGES_LIMIT + 400)
        self.assertEqual(len(POINTER.findall(shown)), 1)
        self.assertTrue(shown.rstrip().endswith("]"), "the pointer follows the changes it stands for")

    def test_two_objects_sharing_a_change_keep_their_own_lines(self):
        listed = rotated(2) + [f"+ Box {n} (mesh)" for n in range(2)]
        self.assertEqual(scene_diff._grouped(listed), listed)

    def test_tools_whose_result_must_stay_whole(self):
        text = prints(100)
        for tool in ("search_assets", "read_result", "delegate", "checks", "check_brief", "loopcut__asset_search"):
            self.assertEqual(result_store.cut(text, tool, "r1"), (text, None), tool)

    def test_one_line_json_is_numbered_in_pieces_cut_between_values(self):
        text = json.dumps([{"name": f"Part {n}", "inputs": {"Roughness": 0.5, "Base Color": [0.8, 0.8, 0.8, 1.0]}}
                           for n in range(80)], separators=(",", ":"))
        pieces = result_store.spans(text)
        self.assertEqual("".join(text[a:b] for a, b in pieces), text, "the pieces are the text")
        self.assertTrue(all(b - a <= result_store.WRAP for a, b in pieces))
        self.assertTrue(all(text[b - 1] in ",;" for a, b in pieces[:-1]), "cut after a value, not inside one")
        shown, _ = result_store.cut(text, "get_object_info", "r1")
        self.assertTrue(shown.split("\n[Loopcut:")[0].endswith(","))


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp()) / "results"
        result_store._next.clear()

    def test_ids_count_up_survive_a_restart_and_short_results_take_none(self):
        self.assertEqual(result_store.keep(lambda: self.folder, "short", "run_python"), "short")
        first = result_store.keep(lambda: self.folder, prints(100), "shell")
        second = result_store.keep(lambda: self.folder, prints(120), "shell")
        self.assertEqual([result_store.ref_in(first), result_store.ref_in(second)], ["r1", "r2"])
        result_store._next.clear()  # Blender restarted.
        self.assertEqual(result_store.ref_in(result_store.keep(lambda: self.folder, prints(90), "shell")), "r3")
        body = json.loads((self.folder / "r2.json").read_text())
        self.assertEqual((body["tool"], body["text"]), ("shell", prints(120)))

    def test_lines_from_where_the_cut_ended_and_where_to_go_on(self):
        shown = result_store.keep(lambda: self.folder, prints(200), "shell")
        first = int(POINTER.search(shown).group(3))
        answer = result_store.read(self.folder, "r1")
        self.assertTrue(answer.startswith(f"r1, lines {first}-"), answer.splitlines()[0])
        self.assertIn(f"\n{first}: row {first:03d} ", answer)
        self.assertLessEqual(len(answer), LIMIT)
        self.assertRegex(answer.splitlines()[-1], r'^Next: read_result\("r1", lines="\d+-\d+"\)\.$')
        self.assertEqual(result_store.read(self.folder, "r1", lines="150-151"),
                         f"r1, lines 150-151 of 200:\n150: row 150 {'x' * 52}\n151: row 151 {'x' * 52}")
        self.assertIn("199: row 199", result_store.read(self.folder, "r1", lines="199-"))

    def test_grep_takes_a_regex_or_plain_words_case_insensitive(self):
        text = "\n".join([f"  segments: int = {n}" for n in range(3)] + ["  width: float = 0.1", "  Profile (shape)"]
                         + [prints(40)])
        result_store.keep(lambda: self.folder, text, "inspect_api")
        self.assertEqual(result_store.read(self.folder, "r1", grep=r"SEGMENTS: int = [12]"),
                         "r1: 2 of 45 lines match 'SEGMENTS: int = [12]':\n2:   segments: int = 1\n3:   segments: int = 2")
        self.assertIn("5:   Profile (shape)", result_store.read(self.folder, "r1", grep="profile ("))  # Not a regex.
        self.assertEqual(result_store.read(self.folder, "r1", grep="bevel"), "No line of r1 (45 lines) matches 'bevel'.")

    def test_many_matches_fit_the_limit_and_say_where_the_next_one_is(self):
        result_store.keep(lambda: self.folder, prints(300), "shell")
        answer = result_store.read(self.folder, "r1", grep="row")
        self.assertLessEqual(len(answer), LIMIT)
        self.assertIn("more matches, the next on line", answer.splitlines()[-1])

    def test_reads_that_cannot_be_answered_say_why(self):
        result_store.keep(lambda: self.folder, prints(100), "shell")
        for ref, grep, lines, says in (("../conversation", None, None, "ids look like r12"),
                                       ("r7", None, None, "it has r1 to r1"),
                                       ("r1", None, "lots", 'a range such as "41-70"'),
                                       ("r1", None, "500-510", "has 100 lines")):
            with self.subTest(ref=ref, lines=lines), self.assertRaises(result_store.StoreError) as raised:
                result_store.read(self.folder, ref, grep, lines)
            self.assertIn(says, str(raised.exception))
        with self.assertRaises(result_store.StoreError) as raised:
            result_store.read(Path(tempfile.mkdtemp()) / "results", "r1")
        self.assertIn("nothing was cut short", str(raised.exception))


class WorkerStoreTest(unittest.TestCase):
    def test_a_workers_results_live_in_its_job_folder_and_follow_its_continued_brief(self):
        """A worker's loop runs here with a conversation of its own, kept in its job folder (waves.Worker); a brief
        sent back to it starts a new folder that carries the earlier one's images and results (waves._copy_images)."""
        from loopcut import agent, conversations, state, waves
        job = Path(tempfile.mkdtemp())
        first, again = state.new_session(), state.new_session()
        conversations.FOLDERS[first["id"]] = job / "w1" / "lamp"
        conversations.FOLDERS[again["id"]] = job / "w2" / "lamp"
        self.addCleanup(conversations.FOLDERS.pop, first["id"])
        self.addCleanup(conversations.FOLDERS.pop, again["id"])
        shown = agent._shown(first, "run_python", prints(100), False)
        self.assertEqual(result_store.ref_in(shown), "r1")
        self.assertTrue((job / "w1" / "lamp" / "results" / "r1.json").is_file())
        waves._copy_images(job / "w1" / "lamp", job / "w2" / "lamp")
        answer = agent._read_result(again, json.dumps({"id": "r1", "lines": "99-100"}))
        self.assertEqual((answer.ok, answer.text.splitlines()[0]), (True, "r1, lines 99-100 of 100:"))
        self.assertEqual(result_store.ref_in(agent._shown(again, "run_python", prints(90), False)), "r2")
        refused = agent._read_result(again, json.dumps({"id": "r1", "line": "3"}))
        self.assertFalse(refused.ok)
        self.assertIn("not line", refused.text)
        self.assertIn("needs the id", agent._read_result(again, "{}").text)


if __name__ == "__main__":
    unittest.main()
