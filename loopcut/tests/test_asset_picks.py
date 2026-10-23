"""Pick numbers on asset search results (asset_picks): numbers run on across searches and libraries,
import_asset takes one as pick (or "#9" as id) without source, a mistyped id that nearly matches one
recent result imports that result, and the numbers are saved with the conversation. Hosted Fast
copied BlenderKit UUIDs wrongly and built nothing (2026-10-23). No network or Blender."""

import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))
sys.modules.setdefault("bpy", types.ModuleType("bpy"))

from loopcut import (agent, asset_common, asset_picks, assets, blenderkit, conversations, polyhaven,  # noqa: E402
                     state, tools)

SWORD = "e6518593-7da8-4ae8-91a3-f189dd327d6d"
TABLE = "1d00af94-b833-43ee-ae6e-53e40a3fb2c1"
LAMP = "66afb9b4-b3a3-4e12-8c4d-a4f8e4e4d5b6"
KIT = [{"id": SWORD, "name": "Green Snake Sword", "isFree": True},
       {"id": TABLE, "name": "Gaming Table", "isFree": True},
       {"id": LAMP, "name": "Desk Lamp", "isFree": True}]
HAVEN = {"ArmChair_01": {"name": "Arm Chair", "type": 2, "categories": ["furniture"]},
         "rock_01": {"name": "Rock 01", "type": 2}, "rock_02": {"name": "Rock 02", "type": 2}}


def missing(library):
    def fail(asset_id, *args, **kwargs):
        raise tools.ToolError(f"{library} has no asset {asset_id!r}. Search first and use the id it gives.")
    return fail


class PickTest(unittest.TestCase):
    def setUp(self):
        state.reset()
        self.addCleanup(state.reset)
        for target, attribute, value in ((blenderkit, "_headers", {}), (blenderkit, "_thumbnail", None),
                                         (polyhaven, "_thumbnail", None)):
            patcher = mock.patch.object(target, attribute, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.sheets = []
        for module in (blenderkit, polyhaven):
            patcher = mock.patch.object(module, "sheet", side_effect=lambda *a, **k: self.sheets.append(k) or None)
            patcher.start()
            self.addCleanup(patcher.stop)

    def blenderkit_search(self, results=KIT):
        with mock.patch.object(blenderkit, "get_json", return_value={"results": results, "count": len(results)}):
            return assets.search_assets(source="blenderkit", query="room props", type="model")

    def polyhaven_search(self, query="chair rock"):
        with mock.patch.object(polyhaven, "assets", return_value=HAVEN):
            return assets.search_assets(source="polyhaven", query=query, type="model")

    def rows(self, text):
        return [line for line in text.splitlines() if line.startswith("#")]

    def blenderkit_import(self, known=(SWORD, TABLE, LAMP)):
        def run(asset_id, resolution="1k", apply_to=None):
            if asset_id not in known:
                missing("BlenderKit")(asset_id)
            return tools.ToolResult(f"Imported {asset_id}")
        return mock.patch.object(blenderkit, "import_blenderkit", autospec=True, side_effect=run)

    # ------------------------------------------------------------------ numbering

    def test_numbers_run_on_across_searches_and_libraries(self):
        first = self.blenderkit_search().text
        second = self.polyhaven_search().text
        self.assertEqual([r.split(" (")[0] for r in self.rows(first)],
                         ["#1 Green Snake Sword", "#2 Gaming Table", "#3 Desk Lamp"])
        self.assertTrue(self.rows(first)[0].endswith(f"; id {SWORD}"), "the real id is still shown, last")
        self.assertEqual([r.split(";")[0] for r in self.rows(second)],
                         ["#4 Arm Chair (polyhaven)", "#5 Rock 01 (polyhaven)", "#6 Rock 02 (polyhaven)"])
        self.assertIn("import_asset(pick=N)", first)
        self.assertIn("import_asset(pick=N)", second)
        self.assertNotIn("numbered as on the sheet", first + second)
        self.assertEqual([k["labels"] for k in self.sheets], [[1, 2, 3], [4, 5, 6]], "the sheets carry the numbers")

    def test_an_asset_shown_again_keeps_its_number_so_a_repeat_reads_the_same(self):
        first = self.blenderkit_search().text
        self.polyhaven_search()
        self.assertEqual(self.blenderkit_search().text, first)
        newer = self.blenderkit_search(KIT[1:] + [{"id": "ab12cd34-0000-4000-8000-000000000000", "name": "Mug"}]).text
        self.assertEqual([r.split(" ")[0] for r in self.rows(newer)], ["#2", "#3", "#7"])

    def test_old_rows_are_dropped_but_their_numbers_never_reused(self):
        with mock.patch.object(asset_picks, "KEEP", 2):
            for asset_id in ("a", "b", "c"):
                asset_picks.number("polyhaven", asset_id)
            self.assertIsNone(asset_picks.find(1))
            self.assertEqual(asset_picks.number("polyhaven", "a"), 4)

    # ------------------------------------------------------------------ importing by number

    def test_import_by_pick_needs_no_source_or_id(self):
        self.blenderkit_search()
        self.polyhaven_search()
        with self.blenderkit_import() as imported:
            result = tools.execute("import_asset", json.dumps({"pick": 2, "resolution": "2k"}))
        self.assertTrue(result.ok, result.text)
        self.assertEqual(result.text, f"Imported {TABLE}")
        imported.assert_called_once_with(asset_id=TABLE, resolution="2k")
        with mock.patch.object(polyhaven, "import_polyhaven", autospec=True,
                               return_value=tools.ToolResult("ok")) as haven:
            for given in ({"id": "#4"}, {"id": "4"}, {"pick": "#4"}, {"pick": 4.0}, {"source": "polyhaven", "id": "#4"}):
                with self.subTest(given=given):
                    self.assertEqual(tools.execute("import_asset", json.dumps(given)).text, "ok")
        self.assertEqual({c.kwargs["asset_id"] for c in haven.call_args_list}, {"ArmChair_01"})

    def test_a_pick_wins_over_a_wrong_source_and_says_so(self):
        self.blenderkit_search()
        self.polyhaven_search()
        with mock.patch.object(polyhaven, "import_polyhaven", autospec=True, return_value=tools.ToolResult("ok")):
            result = assets.import_asset(source="blenderkit", pick=5)
        self.assertEqual(result.text, "#5 is a polyhaven result, not blenderkit: imported it.\nok")

    def test_an_unknown_pick_says_search_again(self):
        self.blenderkit_search()
        for given in ({"pick": 40}, {"id": "#40"}, {"source": "blenderkit", "id": "40"}):
            with self.subTest(given=given):
                result = tools.execute("import_asset", json.dumps(given))
                self.assertFalse(result.ok)
                self.assertEqual(result.text, "No result #40; search again.")
        for given, expected in (({"pick": "nine"}, "pick is a result's number"), ({}, "Give pick")):
            result = tools.execute("import_asset", json.dumps(given))
            self.assertFalse(result.ok)
            self.assertTrue(result.text.startswith(expected), result.text)
            self.assertNotIn("Traceback", result.text)

    def test_a_worker_fetches_by_pick_in_the_parent_and_loads_in_the_child(self):
        self.blenderkit_search()
        fetched = {"id": SWORD, "kind": "model", "path": "/cache/x.blend", "file_type": "blend", "name": "Sword"}
        with mock.patch.object(blenderkit, "fetch_blenderkit", autospec=True, return_value=fetched) as fetch:
            record = assets.fetch_asset(pick=1)
        fetch.assert_called_once_with(asset_id=SWORD)
        self.assertEqual(record["source"], "blenderkit")
        self.assertNotIn("note", record)
        json.dumps(record)  # Crosses to the child as JSON.
        with mock.patch.object(blenderkit, "load_blenderkit", autospec=True, return_value=tools.ToolResult("loaded")):
            self.assertEqual(assets.load_fetched(record, pick=1).text, "loaded")

    def test_labels_and_the_gate(self):
        self.blenderkit_search()
        self.polyhaven_search()
        self.assertEqual(assets.describe("import_asset", {"pick": 4}), ("Import Poly Haven asset #4 Arm Chair (1k)", ""))
        self.assertEqual(assets.describe("import_asset", {"id": "#1", "resolution": "2k"}),
                         ("Import BlenderKit asset #1 Green Snake Sword (2k)", ""))
        self.assertEqual(assets.describe("import_asset", {"pick": 99}), ("Import asset #99", ""))
        self.assertEqual(agent._approval("import_asset", json.dumps({"pick": 4}), ()), "gate")

    # ------------------------------------------------------------------ a mistyped id

    def test_the_mistyped_ids_from_the_failed_runs_import_the_result_they_meant(self):
        self.blenderkit_search()
        self.polyhaven_search()
        # Copied as hosted Fast copied them on 2026-10-23: characters dropped from the middle and the end.
        for typed, meant, number in (("1d00af94-b833-43e-ae6e-53e40a3f", TABLE, 2),
                                     ("66afb9b4-b3a3-4e12-8c-a4f8e4e4d5b6", LAMP, 3),
                                     ("e6518539-7da8-4ae8-91a3-f189dd327d6d", SWORD, 1)):
            with self.subTest(typed=typed), self.blenderkit_import() as imported:
                result = tools.execute("import_asset", json.dumps({"source": "blenderkit", "id": typed}))
                self.assertTrue(result.ok, result.text)
                self.assertEqual([c.kwargs["asset_id"] for c in imported.call_args_list], [typed, meant])
                note, rest = result.text.split("\n", 1)
                self.assertTrue(note.startswith(f"Read id {typed!r} as #{number} "), note)
                self.assertIn(meant, note)
                self.assertEqual(rest, f"Imported {meant}")

    def test_an_id_close_to_several_results_lists_them_by_number(self):
        twins = [{"id": "aaaaaaaa-1111-4000-8000-000000000001", "name": "Chair A"},
                 {"id": "aaaaaaaa-2222-4000-8000-000000000002", "name": "Chair B"}]
        self.blenderkit_search(twins)
        with self.blenderkit_import(known=[t["id"] for t in twins]) as imported:
            result = tools.execute("import_asset", json.dumps({"source": "blenderkit",
                                                               "id": "aaaaaaaa-3333-4000-8000-0000000000"}))
        self.assertFalse(result.ok)
        self.assertEqual(imported.call_count, 1, "nothing guessed")
        self.assertEqual(result.text, "BlenderKit has no asset 'aaaaaaaa-3333-4000-8000-0000000000'. Search first and "
                                      "use the id it "
                                      "gives. Closest results: #1 Chair A, #2 Chair B. Import by number: "
                                      "import_asset(pick=N).")

    def test_a_short_id_must_match_but_for_case_and_an_exact_failure_is_never_swapped(self):
        self.polyhaven_search()
        with mock.patch.object(polyhaven, "import_polyhaven", autospec=True,
                               side_effect=missing("Poly Haven")) as imported:
            for typed in ("rock_01", "rock_9"):
                with self.subTest(typed=typed):
                    imported.reset_mock()
                    result = tools.execute("import_asset", json.dumps({"source": "polyhaven", "id": typed}))
                    self.assertFalse(result.ok)
                    self.assertEqual(imported.call_count, 1, "rock_01 is right (it was shown); rock_9 is another id")
        with mock.patch.object(polyhaven, "import_polyhaven", autospec=True,
                               side_effect=lambda asset_id, **k: tools.ToolResult("ok") if asset_id == "ArmChair_01"
                               else missing("Poly Haven")(asset_id)):
            result = assets.import_asset(source="polyhaven", id="armchair_01")
        self.assertEqual(result.text, "Read id 'armchair_01' as #1 Arm Chair (polyhaven id ArmChair_01).\nok")

    def test_an_id_shown_under_another_source_imports_from_that_source(self):
        self.polyhaven_search()
        with self.blenderkit_import(), mock.patch.object(polyhaven, "import_polyhaven", autospec=True,
                                                         side_effect=lambda **k: tools.ToolResult("ok")) as haven:
            wrong = assets.import_asset(source="blenderkit", id="ArmChair_01")
            bare = assets.import_asset(id="ArmChair_01")
        self.assertTrue(wrong.text.startswith("Read id 'ArmChair_01' as #1 Arm Chair (polyhaven id ArmChair_01)."))
        self.assertEqual(bare.text, "ok", "no source: the result shown with that id")
        self.assertEqual(haven.call_count, 2)

    def test_source_and_id_work_as_before(self):
        self.blenderkit_search()
        with self.blenderkit_import() as imported:
            result = tools.execute("import_asset", json.dumps({"source": "blenderkit", "id": TABLE}))
        self.assertEqual(result.text, f"Imported {TABLE}")
        imported.assert_called_once_with(asset_id=TABLE)
        offline = tools.ToolError("Could not reach www.blenderkit.com: offline")
        typo = TABLE[:-1] + "0"
        with mock.patch.object(blenderkit, "import_blenderkit", autospec=True, side_effect=offline) as imported:
            result = tools.execute("import_asset", json.dumps({"source": "blenderkit", "id": typo}))
        self.assertEqual(result.text, str(offline), "a network failure is not an unknown id")
        self.assertEqual(imported.call_count, 1)
        state.reset()  # A conversation that never searched: the library's own words, nothing added.
        with self.blenderkit_import():
            result = tools.execute("import_asset", json.dumps({"source": "blenderkit", "id": typo}))
        self.assertEqual(result.text, f"BlenderKit has no asset {typo!r}. Search first and use the id it gives.")

    def test_near_matches_need_case_spacing_or_for_a_uuid_a_few_edits_or_its_prefix(self):
        asset_picks.number("blenderkit", SWORD, "Sword")
        asset_picks.number("polyhaven", "ArmChair_01", "Arm Chair")
        self.assertEqual([r["id"] for r in asset_picks.near(SWORD.upper() + " ")], [SWORD])
        self.assertEqual([r["id"] for r in asset_picks.near(SWORD[:8] + "-ffff-ffff-ffff")], [SWORD])
        self.assertEqual([r["id"] for r in asset_picks.near(" armchair_01", "polyhaven")], ["ArmChair_01"])
        self.assertEqual(asset_picks.near("ArmChair_1", "polyhaven"), [], "another id, not a slip")
        self.assertEqual(asset_picks.near("armchair_01", "blenderkit"), [], "only that source's results")
        self.assertEqual(asset_picks.near(SWORD), [], "the same id is not a near miss of itself")
        self.assertEqual(asset_picks.near("f" * 36), [])


class SafeRescueTest(unittest.TestCase):
    """Review of 8e5bdcc53f2: the rescue must never import a different asset. Only a mangled UUID may be
    off by edits or matched by its first 8 hex characters; any other id must be the same but for case and
    spacing. Only a library's own "no such id" words start a rescue."""

    def setUp(self):
        state.reset()
        self.addCleanup(state.reset)

    def test_a_close_but_different_non_uuid_id_is_not_a_near_miss(self):
        shown = [("library", "Ess::metals.blend::MATERIAL::Gold", "Gold"), ("ambientcg", "Wood049", "Wood 049"),
                 ("polyhaven", "wooden_chair_01", "Wooden Chair 01"), ("polyhaven", "Facade001", "Facade 001")]
        for source, asset_id, name in shown:
            asset_picks.number(source, asset_id, name)
        for typed, source in (("Ess::metals.blend::MATERIAL::Rust", "library"), ("Wood048", "ambientcg"),
                              ("wooden_chair_02", "polyhaven"), ("Facade0099", "polyhaven"), ("Facade0099", "")):
            with self.subTest(typed=typed):
                self.assertEqual(asset_picks.near(typed, source), [])
        self.assertEqual([r["id"] for r in asset_picks.near(" wood049 ", "ambientcg")], ["Wood049"], "case, spacing")
        self.assertEqual([r["id"] for r in asset_picks.near("Ess::metals.blend::MATERIAL::gold", "library")],
                         ["Ess::metals.blend::MATERIAL::Gold"])

    def test_a_library_ref_one_word_off_imports_nothing(self):
        asset_picks.number("library", "Ess::metals.blend::MATERIAL::Gold", "Gold")
        def refuse(ref, **kwargs):
            raise tools.ToolError(f"Ess has no MATERIAL {ref.split('::')[-1]!r} in metals.blend; search again.")
        with mock.patch("loopcut.asset_libraries.import_asset", autospec=True, side_effect=refuse) as imported:
            result = tools.execute("import_asset", json.dumps({"source": "library",
                                                               "id": "Ess::metals.blend::MATERIAL::Rust"}))
        self.assertFalse(result.ok)
        self.assertEqual(imported.call_count, 1, "Rust is not Gold")

    def test_only_a_librarys_no_such_id_words_start_a_rescue(self):
        from loopcut import asset_libraries, indexed_assets, polypizza
        def words(call):
            try:
                call()
            except tools.ToolError as ex:
                return str(ex)
            self.fail("no error")
        gone = mock.Mock(side_effect=asset_common.NotFound("Nothing at https://x.test/a (HTTP 404)"))
        with mock.patch.object(blenderkit, "get_json", gone), \
                mock.patch.object(blenderkit, "_headers", return_value={}):
            unknown = [words(lambda: blenderkit.fetch_blenderkit(SWORD)),
                       words(lambda: blenderkit.fetch_blenderkit("../x"))]
        with mock.patch.object(polyhaven, "get_json", gone):
            unknown.append(words(lambda: polyhaven.fetch_polyhaven("nope_01")))
        with mock.patch.object(polypizza, "get_json", gone), mock.patch.object(polypizza, "_headers", return_value={}):
            unknown.append(words(lambda: polypizza.fetch_polypizza("abc123")))
        library = asset_libraries.Library("Ess", Path(tempfile.gettempdir()))
        with mock.patch.object(asset_libraries, "libraries", return_value=[library]), \
                mock.patch.object(asset_libraries, "local_entries", return_value=[]):
            unknown += [words(lambda: asset_libraries._find("Ess::m.blend::MATERIAL::Gold")),
                        words(lambda: asset_libraries._find("Other::m.blend::MATERIAL::Gold")),
                        words(lambda: asset_libraries._find("not a ref"))]
        unknown += [words(lambda: indexed_assets.fetch_indexed("Wood049", "ambientcg")),
                    words(lambda: assets.import_asset(source="nowhere", id="x"))]
        for message in unknown:
            with self.subTest(message=message):
                self.assertTrue(assets._unknown_id(tools.ToolError(message)), message)
        for message in ("Wood049 has no 4k download in this search result; search again or choose another resolution",
                        "Asset archive has no supported PBR maps", "Asset download has no supported scene data",
                        "rock_01 has no 4k HDR; available: 1k, 2k", "Could not reach www.blenderkit.com: offline"):
            with self.subTest(message=message):
                self.assertFalse(assets._unknown_id(tools.ToolError(message)), message)
        self.assertFalse(assets._unknown_id(asset_common.NotFound("Nothing at https://x.test/f.zip (HTTP 404)")),
                         "a file the library lists but cannot serve is not an unknown id")


class ReviewFixTest(unittest.TestCase):
    """The rest of the review of 8e5bdcc53f2."""

    def setUp(self):
        state.reset()
        self.addCleanup(state.reset)

    def test_a_pick_call_is_the_current_spelling_and_survives_a_reload(self):
        for arguments in ({"pick": 9}, {"id": "#9"}, {"pick": 9, "apply_to": ["Cube"]}):
            with self.subTest(arguments=arguments):
                self.assertEqual(tools.current_call("import_asset", arguments), ("import_asset", arguments))
        self.assertEqual(tools.current_call("import_asset", {"ref": "L::f::WORLD::City"}),
                         ("import_asset", {"source": "library", "id": "L::f::WORLD::City"}),
                         "an old call still is respelled")
        messages = [{"role": "assistant", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "import_asset", "arguments": '{"pick": 9}'}}]}]
        conversations.rename_merged_tools(messages)
        self.assertEqual(json.loads(messages[0]["tool_calls"][0]["function"]["arguments"]), {"pick": 9})

    def test_a_pick_and_an_id_naming_two_results_are_refused(self):
        asset_picks.number("blenderkit", SWORD, "Green Snake Sword")
        asset_picks.number("blenderkit", TABLE, "Gaming Table")
        with mock.patch.object(blenderkit, "import_blenderkit", autospec=True,
                               return_value=tools.ToolResult("imported")) as imported:
            result = tools.execute("import_asset", json.dumps({"pick": 1, "id": TABLE}))
            self.assertFalse(result.ok)
            self.assertEqual(result.text, f"pick #1 is Green Snake Sword but id {TABLE} is #2 Gaming Table: "
                                          "give one of them.")
            imported.assert_not_called()
            self.assertEqual(tools.execute("import_asset", json.dumps({"pick": 1, "id": SWORD})).text, "imported",
                             "both naming the same result is fine")

    def test_an_index_pick_from_before_a_restart_says_search_again(self):
        from loopcut import indexed_assets
        indexed_assets._rows.clear()  # A fresh Blender: the index's rows were never fetched in this session.
        asset_picks.number("ambientcg", "Wood049", "Wood 049")
        result = tools.execute("import_asset", json.dumps({"pick": 1}))
        self.assertFalse(result.ok)
        self.assertEqual(result.text, "#1 Wood 049 came from a search before Blender restarted; search again "
                                      "to import it.")

    def test_a_workers_fetch_and_search_use_the_waves_conversation_after_a_switch(self):
        from loopcut import waves
        wave_session = state.new_session()
        with asset_picks.bound(wave_session):
            asset_picks.number("blenderkit", SWORD, "Green Snake Sword")
        state.replace(state.new_session())  # The user opened another conversation (lifecycle.switch_to).
        asset_picks.number("blenderkit", TABLE, "Gaming Table")  # Its own #1.
        fetched = {"id": SWORD, "kind": "model", "path": "/x.blend", "file_type": "blend", "name": "Sword"}
        with mock.patch.object(blenderkit, "fetch_blenderkit", autospec=True, return_value=fetched) as fetch:
            waves._fetch_asset({"pick": 1}, wave_session)
        fetch.assert_called_once_with(asset_id=SWORD)
        wave = types.SimpleNamespace(on_main=lambda fn: fn(), session=wave_session,
                                     turn=types.SimpleNamespace(cancel=mock.Mock(is_set=lambda: False)))
        mug = {"id": "ab12cd34-0000-4000-8000-000000000000", "name": "Mug", "isFree": True}
        with mock.patch.object(blenderkit, "get_json", return_value={"results": [mug], "count": 1}), \
                mock.patch.object(blenderkit, "_headers", return_value={}), \
                mock.patch.object(blenderkit, "_thumbnail", return_value=None), \
                mock.patch.object(blenderkit, "sheet", return_value=None):
            found = waves._HostRuntime._parent_tool(wave, "search_assets")(
                json.dumps({"source": "blenderkit", "query": "mug", "type": "model"}))
        self.assertIn("#2 Mug (blenderkit)", found.text, "numbered in the wave's conversation")
        self.assertEqual([r["n"] for r in wave_session["asset_picks"]["rows"]], [1, 2])
        self.assertEqual([r["name"] for r in state.session()["asset_picks"]["rows"]], ["Gaming Table"])


class SavedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["LOOPCUT_DATA_DIR"] = self.tmp.name
        self.addCleanup(os.environ.pop, "LOOPCUT_DATA_DIR", None)
        self.session = state.replace(state.new_session())
        self.addCleanup(state.reset)
        self.session["items"].append(state.item_user("a cosy room"))

    def test_numbers_survive_a_reload_and_carry_on(self):
        asset_picks.number("blenderkit", SWORD, "Green Snake Sword")
        asset_picks.number("polyhaven", "ArmChair_01", "Arm Chair")
        conversations.save(self.session)
        state.reset()
        loaded = state.replace(conversations.load(self.session["id"]))
        self.assertEqual(loaded["asset_picks"]["next"], 3)
        with mock.patch.object(polyhaven, "import_polyhaven", autospec=True,
                               return_value=tools.ToolResult("ok")) as imported:
            self.assertEqual(assets.import_asset(pick=2).text, "ok")
        imported.assert_called_once_with(asset_id="ArmChair_01")
        self.assertEqual(asset_picks.number("polypizza", "x1", "Tree"), 3)

    def test_a_conversation_from_before_pick_numbers_starts_at_101(self):
        # Saved by 0.2.12: its search results read "3. <uuid>: Gaming Table", so a 3 may mean that row.
        self.session["messages"].append({"role": "tool", "tool_call_id": "c1",
                                         "content": "3 of 9 BlenderKit models:\n3. 1d00af94-b833: Gaming Table"})
        conversations.save(self.session)
        path = conversations.root() / self.session["id"] / "conversation.json"
        body = json.loads(path.read_text())
        body.pop("asset_picks")
        path.write_text(json.dumps(body))
        state.replace(conversations.load(self.session["id"]))
        self.assertEqual(asset_picks.number("blenderkit", SWORD, "Sword"), 101)
        self.assertEqual(tools.execute("import_asset", json.dumps({"pick": 3})).text, "No result #3; search again.")

    def test_a_damaged_or_missing_table_starts_over_at_101(self):
        conversations.save(self.session)
        path = conversations.root() / self.session["id"] / "conversation.json"
        body = json.loads(path.read_text())
        for damaged in ({"next": 1, "rows": [{"n": 5, "source": "x", "id": "y", "name": "z"}]}, [], None):
            body["asset_picks"] = damaged
            path.write_text(json.dumps(body))
            self.assertEqual(conversations.load(self.session["id"])["asset_picks"], {"next": 101, "rows": []})
        body.pop("asset_picks")
        body["messages"].append({"role": "tool", "tool_call_id": "c9", "content": [
            {"type": "text", "text": "2 asset(s):\n#130 Mug (blenderkit); id x\n#131 Cup (blenderkit); id y"}]})
        path.write_text(json.dumps(body))
        self.assertEqual(conversations.load(self.session["id"])["asset_picks"], {"next": 132, "rows": []},
                         "past any number its results already show")


class SheetLabelTest(unittest.TestCase):
    def test_each_tile_shows_its_number_in_its_top_left_corner(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("needs numpy (Blender's Python has it)")
        saved = {}
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        def grey(path, height=None):  # An opaque mid-grey preview.
            tile = np.full((height or 160, height or 160, 4), 0.5, dtype=np.float32)
            tile[..., 3] = 1.0
            return tile
        with mock.patch.object(asset_common, "_pixels", side_effect=grey), \
                mock.patch.object(asset_common, "_save_pixels", side_effect=lambda canvas, out: saved.update(c=canvas)):
            asset_common.sheet(["a.png"] * 5, "s.png", folder=folder, fit=True, labels=[9, 10, 11, 12, 13])
        canvas = saved["c"]
        tile = 160 + asset_common.SHEET_GAP
        self.assertEqual(canvas.shape[:2], (2 * 160 + asset_common.SHEET_GAP, 4 * 160 + 3 * asset_common.SHEET_GAP))
        for index, label in enumerate(["9", "10", "11", "12", "13"]):
            row, column = divmod(index, 4)
            top, left = canvas.shape[0] - row * tile, column * tile
            box = canvas[top - 28:top, left:left + 16 * len(label) + 4, :3]
            with self.subTest(label=label):
                self.assertTrue((box == 1.0).any() and (box == 0.0).any(), "white digits on black")
                self.assertTrue(np.allclose(canvas[top - 60:top - 30, left + 60:left + 100, :3], 0.5), "the rest is the tile")
        # Rows run bottom-up: a 9's open lower-left (row 4 of 5, column 0) is black, its top bar white.
        top = canvas.shape[0]
        self.assertTrue(np.allclose(canvas[top - 4 - 4:top - 4, 4:16, :3], 1.0), "the 9's top bar")
        self.assertTrue(np.allclose(canvas[top - 4 - 16:top - 4 - 12, 4:8, :3], 0.0), "the 9's lower left is open")


if __name__ == "__main__":
    unittest.main()
