"""Final contact evidence, without Blender, captures, model requests or local sockets."""

import copy
import dataclasses
import sys
import types
import unittest
from array import array
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))
fake_bpy = types.ModuleType("bpy")
fake_bpy.app = types.SimpleNamespace(driver_namespace={})
sys.modules.setdefault("bpy", fake_bpy)

from loopcut import agent, config, contact, llm, nudges, orchestrator, state  # noqa: E402


class FreshChecksTest(unittest.TestCase):
    def setUp(self):
        self.session = state.reset()
        self.turn = agent.Turn()
        self.session["turn"] = self.turn
        self.pair = {"a": "Socket", "relation": "touches", "b": "Haft"}
        self.scene = {name: types.SimpleNamespace(name=name, labels=[name], note="", fingerprint=1)
                      for name in ("Socket", "Haft", "Chest")}
        self.distance = 0
        self.depth = 0
        self.shapes = types.SimpleNamespace(of=lambda name: self.scene.get(name, contact.Unavailable("missing " + name)),
                                            fingerprint=lambda name: self.scene[name].fingerprint if name in self.scene
                                            else contact.Unavailable("missing " + name),
                                            free=mock.Mock())
        for patcher in (mock.patch.object(contact, "_Shapes", return_value=self.shapes),
                        mock.patch.object(agent, "_on_main", side_effect=lambda fn, *_: fn())):
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(contact, "_measure", side_effect=self.measure)
        self.measured = patcher.start()
        self.addCleanup(patcher.stop)
        self.spec = dataclasses.replace(agent.SINGLE_SPEC, on_final=None, save=lambda s: None, changes_card=False)

    def measure(self, pair, a, b, tolerance):
        fn = contact.clear_of if pair["relation"] == "clear_of" else contact.touches
        return fn(pair, {"distance": self.distance, "depth": self.depth, "meet": False,
                         "toward": (0, 0, -self.distance), "out": (0, 0, 0)}, tolerance)

    def check(self):
        return contact.check_contact([self.pair])

    def edit(self, distance=0.034):
        self.scene["Socket"].fingerprint += 1
        self.distance = distance

    def reply(self, text="Connected and verified."):
        self.session["items"].append(dict(state.item_assistant(), text=text, streaming=False))
        self.session["messages"].append({"role": "assistant", "content": text})
        return llm.Completion(text=text)

    def finish(self):
        return agent._final_reply(self.session, self.turn, self.reply(), self.spec)

    def test_pass_changed_fail_bounces_once_and_names_the_gap_in_the_panel(self):
        self.check()  # watch defaults to False.
        saved = self.turn.contact_checks[0]
        self.assertEqual(saved["result"]["fingerprint"], (1, 1, contact.DEFAULT_TOLERANCE))
        self.assertEqual((saved["pair"]["a"], saved["pair"]["b"]), ("Socket", "Haft"))
        self.edit()
        bounce = self.finish()
        self.assertIn("FAIL: Socket touches Haft: gap 34.0 mm", bounce)
        self.assertEqual(self.session["items"][-1]["text"],
                         "Re-checked after later edits: Failed: socket does not meet the required contact with haft. It is too far from haft.")
        self.assertTrue(self.turn.contact_bounced)
        self.assertIsNone(self.finish())
        self.assertEqual(self.measured.call_count, 2, "unchanged failed result is reused too")
        final = self.session["items"][-1]["text"]
        self.assertIn("Verified by Loopcut on this scene: 0 of 1 checks.", final)
        self.assertIn("Failed: socket does not meet the required contact with haft. It is too far from haft.", final)
        self.assertIn(final, self.session["messages"][-1]["content"])

    def test_penetration_is_a_failed_contact_in_notice_and_final_reply(self):
        self.check()
        self.edit(0)
        self.depth = 0.034
        self.assertIn("34.0 mm into Haft", self.finish())
        wording = ("Failed: socket does not meet the required contact with haft. "
                   "It extends too far into haft.")
        self.assertEqual(self.session["items"][-1]["text"], "Re-checked after later edits: " + wording)
        self.assertIsNone(self.finish())
        final = self.session["items"][-1]["text"]
        self.assertIn("Verified by Loopcut on this scene: 0 of 1 checks.\n" + wording, final)
        self.assertNotIn("does not touch", final)

    def test_clearance_failure_explains_overlap_or_insufficient_space(self):
        self.pair.update(relation="clear_of", gap=0.02)
        for distance, depth, reason in [(0, 0.034, "It should be clear of haft but overlaps it."),
                                        (0.01, 0, "It is too close to haft."),
                                        (0, 0, "It is too close to haft.")]:
            with self.subTest(distance=distance, depth=depth):
                self.edit(distance)
                self.depth = depth
                self.check()
                self.assertIsNone(self.finish())
                self.assertIn("Failed: socket does not have the required clearance from haft. " + reason,
                              self.session["items"][-1]["text"])

    def test_unchanged_objects_and_unrelated_edits_do_not_measure_again(self):
        self.check()
        self.scene["Chest"].fingerprint += 1
        self.assertIsNone(self.finish())
        self.measured.assert_called_once()
        self.assertIn("1 of 1 checks.\nPassed: socket touches haft.", self.session["items"][-1]["text"])

    def test_changed_but_still_passing_does_not_bounce(self):
        self.check()
        self.edit(0.001)
        self.assertIsNone(self.finish())
        self.assertEqual(self.measured.call_count, 2)
        self.assertFalse(self.turn.contact_bounced)
        self.assertIn("gap 1.0 mm", self.turn.contact_checks[0]["result"]["line"])
        self.assertIn("Passed: socket touches haft.", self.session["items"][-1]["text"])

    def test_clearance_uses_the_original_gap_and_tolerance_after_an_edit(self):
        self.pair.update(relation="clear_of", gap=0.02)
        self.distance = 0.03
        contact.check_contact([self.pair], tolerance=0.001)
        self.edit(0.01)
        self.assertIn("10.0 mm apart, needs 20.0 mm", self.finish())
        self.assertEqual(self.measured.call_args.args[-1], 0.001)
        self.assertIn("Failed: socket does not have the required clearance from haft", self.session["items"][-1]["text"])

    def test_failure_to_read_one_pair_does_not_discard_another_fresh_pass(self):
        self.check()
        contact.check_contact([{"a": "Chest", "relation": "touches", "b": "Haft"}])
        del self.scene["Socket"]
        self.assertIsNone(self.finish())
        final = self.session["items"][-1]["text"]
        self.assertIn("1 of 2 checks", final)
        self.assertIn("Could not check: whether socket touches haft", final)
        self.assertIn("Passed: chest touches haft.", final)

    def test_new_pass_then_another_edit_cannot_bounce_twice(self):
        self.check()
        self.edit()
        self.assertIsNotNone(self.finish())
        self.edit(0)
        self.check()
        self.assertEqual(len(self.turn.contact_checks), 1)
        self.edit(0.05)
        self.assertIsNone(self.finish())
        self.assertIn("gap 50.0 mm", self.turn.contact_checks[0]["result"]["line"])
        self.assertIn("Failed: socket does not meet the required contact with haft.", self.session["items"][-1]["text"])
        self.assertEqual(self.measured.call_count, 4, "the cap never skips freshness checks")

    def test_fixed_after_bounce_is_fresh_and_passing(self):
        self.check()
        self.edit()
        self.finish()
        self.edit(0)
        self.assertIsNone(self.finish())
        self.assertIn("1 of 1 checks.\nPassed: socket touches haft.", self.session["items"][-1]["text"])

    def test_a_later_explicit_failure_replaces_the_old_pass(self):
        self.check()
        self.edit()
        self.check()
        self.assertIsNone(self.finish(), "the model already received this failure")
        self.assertIn("0 of 1 checks", self.session["items"][-1]["text"])

    def test_deleted_object_and_evaluation_errors_fail_open_without_verified_passes(self):
        self.check()
        del self.scene["Socket"]
        self.assertIsNone(self.finish())
        self.assertIn("Could not check: whether socket touches haft", self.session["items"][-1]["text"])
        self.assertIn("0 of 1 checks", self.session["items"][-1]["text"])
        for method in ("_Shapes", "_evaluate"):
            with self.subTest(method=method), mock.patch.object(contact, method, side_effect=RuntimeError("bad mesh")):
                self.assertIsNone(self.finish())
                self.assertIn("0 of 1 checks", self.session["items"][-1]["text"])
                self.assertIn("Could not check: whether socket touches haft", self.session["items"][-1]["text"])

    def test_main_thread_failure_and_cleanup_error_do_not_stop_completion(self):
        self.check()
        with mock.patch.object(agent, "_on_main", side_effect=RuntimeError("scheduler")):
            self.assertIsNone(self.finish())
            self.assertIn("0 of 1 checks", self.session["items"][-1]["text"])
        self.shapes.free.side_effect = RuntimeError("cleanup")
        self.assertIsNone(self.finish())

    def test_no_checks_new_turn_and_cancellation_read_no_geometry(self):
        with mock.patch.object(contact, "_Shapes") as shapes:
            self.assertIsNone(self.finish())
            shapes.assert_not_called()
        self.check()
        self.turn.cancel.set()
        with mock.patch.object(contact, "_Shapes") as shapes:
            self.assertIsNone(self.finish())
            shapes.assert_not_called()
        self.turn = self.session["turn"] = agent.Turn()
        self.assertEqual(self.turn.contact_checks, [])
        self.assertFalse(self.turn.contact_bounced)

    def test_cancellation_during_refresh_never_notices_bounces_or_changes_the_reply(self):
        self.check()
        self.edit()
        for outcome in ("regression", "pass", "error"):
            with self.subTest(outcome=outcome):
                self.turn.cancel.clear()
                self.turn.contact_bounced = False
                completion = self.reply()
                before = copy.deepcopy((self.session["items"], self.session["messages"]))

                def cancel_during_refresh():
                    self.turn.cancel.set()
                    if outcome == "error":
                        raise RuntimeError("scheduler stopped")
                    result = self.measure(self.pair, None, None, contact.DEFAULT_TOLERANCE)
                    result = {**self.pair, **result, "status": "fail" if outcome == "regression" else "pass"}
                    return [result], [result] if outcome == "regression" else []

                self.assertIsNone(contact.finish_gate(self.session, self.turn, completion, cancel_during_refresh))
                self.assertFalse(self.turn.contact_bounced)
                self.assertEqual((self.session["items"], self.session["messages"]), before)

    def test_contact_summary_is_plain_and_caps_coverage_at_three_descriptions(self):
        self.check()
        pairs = [("Socket", "touches", "Haft"), ("Shovel", "clear_of", "Chest"),
                 ("SideTable", "rests_on", "Floor_Panel"), ("Coin", "inside", "Box")]
        results = [{"a": a, "relation": relation, "b": b, "status": "pass",
                    "line": "gap 1.0 mm", "details": ["move by 0.001 m"]} for a, relation, b in pairs]
        results += [dict(results[-1], a=f"Coin_{i}") for i in range(3)]
        for count, coverage in [(1, "Passed: socket touches haft."),
                                (3, "Passed: socket touches haft.\nPassed: shovel clears chest.\nPassed: side table rests on floor panel."),
                                (4, "Passed: socket touches haft.\nPassed: shovel clears chest.\nPassed: side table rests on floor panel.\n"
                                 "Plus 1 other check."),
                                (7, "Passed: socket touches haft.\nPassed: shovel clears chest.\nPassed: side table rests on floor panel.\n"
                                 "Plus 4 other checks.")]:
            with self.subTest(count=count):
                self.assertIsNone(contact.finish_gate(self.session, self.turn, self.reply(),
                                                     lambda: (results[:count], [])))
                expected = (f"Connected and verified.\n\nVerified by Loopcut on this scene: {count} of {count} checks.\n"
                            f"{coverage}")
                self.assertEqual(self.session["items"][-1]["text"], expected)
                self.assertEqual(self.session["messages"][-1]["content"], expected)

    def test_recheck_notice_uses_plain_object_names_and_keeps_measurements_in_details(self):
        result = {"a": "MedShovel_Socket", "relation": "rests_on", "b": "TreasureChest_Body",
                  "status": "fail", "line": "gap 34.0 mm. Fix: move MedShovel_Socket by (0, 0, -0.034) m"}
        self.assertEqual(contact.rechecked_line(result),
                         "Re-checked after later edits: Failed: med shovel socket does not have the required support from treasure chest body.")
        self.assertIn("MedShovel_Socket rests_on TreasureChest_Body: gap 34.0 mm", contact.pair_line(result))

    def test_passing_scene_summary_does_not_list_raw_check_identifiers(self):
        checks = [(f"worker c{i}", {"kind": "no_overlap", "names": [f"Raw_Object_{i}", "Other"]},
                   {"status": "pass", "detail": "1.0 mm"}) for i in range(12)]
        self.assertEqual(orchestrator.verified_block(checks), "Verified by Loopcut on this scene: 12 of 12 checks.")

    def test_stopped_turn_reports_fresh_failure_without_bouncing(self):
        for outcome in ("budget", "step_limit", "no_progress"):
            self.check()
            self.edit()
            self.turn.outcome = outcome
            self.assertIsNone(self.finish())
            self.assertFalse(self.turn.contact_bounced)
            self.assertIn("0 of 1 checks", self.session["items"][-1]["text"])
            self.distance = 0

    def test_stopped_wrap_up_still_refreshes_when_the_other_gate_wants_to_bounce(self):
        self.check()
        self.edit()
        self.turn.outcome = "budget"
        self.spec = dataclasses.replace(self.spec, on_final=lambda *_: "another check failed")
        self.assertIsNone(self.finish())
        self.assertIn("0 of 1 checks", self.session["items"][-1]["text"])

    def test_late_merge_runs_before_freshness_and_existing_bounce_is_preserved(self):
        self.check()
        self.spec = dataclasses.replace(self.spec, on_final=lambda *_: self.edit())
        self.assertIn("34.0 mm", self.finish())
        self.spec = dataclasses.replace(self.spec, on_final=lambda *_: "Review the late workers first")
        with mock.patch.object(contact, "refresh") as refresh:
            self.assertEqual(self.finish(), "Review the late workers first")
            refresh.assert_not_called()

    def test_real_reply_loop_delivers_one_bounce_then_accepts_a_fresh_summary(self):
        self.check()
        self.edit()
        self.turn.config = config.Config("test-key", "https://example.invalid", "fake", "low",
                                         False, False, 3, 24000)
        def request(*_):
            text = "Connected and verified." if not self.turn.contact_bounced else "The socket still has a gap."
            self.session["items"].append(dict(state.item_assistant(), text=text, streaming=False))
            return llm.Completion(text=text)
        with mock.patch.object(agent, "_request", side_effect=request) as requests, \
                mock.patch.object(agent, "_redraw"), mock.patch.object(agent, "_record_level"), \
                mock.patch.object(nudges, "flush"):
            agent.run_loop(self.session, self.turn, self.spec)
        self.assertEqual(self.turn.outcome, "completed")
        self.assertEqual(requests.call_count, 2)
        feedback = [m for m in self.session["messages"] if m["role"] == "user"]
        self.assertEqual(len(feedback), 1)
        self.assertIn("34.0 mm", feedback[0]["content"])
        self.assertIn("0 of 1 checks", self.session["messages"][-1]["content"])

    def test_unrelated_acceptance_pass_does_not_list_contact_coverage(self):
        block = orchestrator.verified_block([("typewriter c1", {"kind": "exists", "name": "Typewriter"},
                                              {"status": "pass"})])
        self.assertEqual(block, "Verified by Loopcut on this scene: 1 of 1 checks.")
        self.assertNotIn("Socket", block)


class LazyShapesTest(unittest.TestCase):
    """Exercise the real shape cache with 200 child meshes and counted Blender construction calls."""

    def setUp(self):
        from loopcut import view_layers

        class Collection(list):
            def __init__(self, values, width=1):
                super().__init__(values)
                self.width = width

            def __len__(self):
                return super().__len__() // self.width

            def foreach_get(self, field, target):
                target[:] = array(target.typecode, self)

        class Matrix(list):
            def determinant(self):
                return 1

        self.meshes, self.parts = [], []
        for i in range(201):
            mesh = types.SimpleNamespace(vertices=Collection([0, 0, 0, 1, 0, 0, 0, 1, 0], 3),
                                         loops=Collection([0, 1, 2]), polygons=Collection([3]),
                                         edges=Collection([], 2), transform=mock.Mock(), validate=mock.Mock())
            obj = types.SimpleNamespace(name=f"Key_{i}", type="MESH", data=mesh,
                                        visible_get=lambda: True, to_mesh=mock.Mock(return_value=mesh),
                                        to_mesh_clear=mock.Mock())
            self.meshes.append(mesh)
            self.parts.append((obj.name, obj, Matrix([[1, 0, 0, 0], [0, 1, 0, 0],
                                                    [0, 0, 1, 0], [0, 0, 0, 1]])))
        parent = types.SimpleNamespace(name="Keyboard", type="EMPTY",
                                       children_recursive=[p[1] for p in self.parts[:200]])
        floor = self.parts[-1][1]
        parts = {p[0]: p for p in self.parts}
        layer = object()
        context = types.SimpleNamespace(scene=types.SimpleNamespace(objects={"Keyboard": parent, floor.name: floor}),
                                        evaluated_depsgraph_get=lambda: types.SimpleNamespace(view_layer=layer))
        self.bmesh = types.SimpleNamespace(new=mock.Mock(side_effect=mock.MagicMock))
        for patcher in (mock.patch.object(sys.modules["bpy"], "context", context, create=True),
                        mock.patch.dict(sys.modules, {"bmesh": self.bmesh}),
                        mock.patch.object(view_layers, "home", return_value=layer),
                        mock.patch.object(contact, "_own", side_effect=lambda obj, _: [parts[obj.name]]
                                          if obj.name in parts else [])):
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(contact, "_measure", return_value={"status": "pass", "line": "touching"})
        self.measure = patcher.start()
        self.addCleanup(patcher.stop)
        self.pair = contact.parse([{"a": "Keyboard", "relation": "touches", "b": floor.name}])[0]
        self.turn = agent.Turn()
        self.turn.contact_checks = [{"pair": self.pair, "tolerance": contact.DEFAULT_TOLERANCE,
                                     "result": contact.evaluate([self.pair], contact.DEFAULT_TOLERANCE)[0]}]
        self.assertEqual(self.turn.contact_checks[0]["result"]["status"], "pass")
        self.assertEqual(self.bmesh.new.call_count, 2)
        self.bmesh.new.reset_mock()
        self.measure.reset_mock()
        for _, obj, _ in self.parts:
            obj.to_mesh.reset_mock()
            obj.to_mesh_clear.reset_mock()
            obj.data.transform.reset_mock()
            obj.data.validate.reset_mock()

    def test_unchanged_200_object_parent_constructs_no_meshes(self):
        previous = self.turn.contact_checks[0]["result"]
        results, regressed = contact.refresh(self.turn)
        self.assertIs(results[0], previous)
        self.assertEqual(regressed, [])
        self.bmesh.new.assert_not_called()
        self.measure.assert_not_called()
        for _, obj, _ in self.parts:
            obj.to_mesh.assert_not_called()
            obj.data.transform.assert_not_called()
            obj.data.validate.assert_not_called()

    def test_changed_child_builds_shapes_and_remeasures(self):
        self.parts[99][2][0][3] = 0.034
        self.measure.return_value = {"status": "fail", "line": "gap 34.0 mm"}
        results, regressed = contact.refresh(self.turn)
        self.assertEqual(regressed, results)
        self.assertEqual(results[0]["status"], "fail")
        self.assertEqual(self.bmesh.new.call_count, 2)
        self.measure.assert_called_once()
        self.bmesh.new.reset_mock()
        self.measure.reset_mock()
        self.assertEqual(contact.refresh(self.turn), (results, []))
        self.bmesh.new.assert_not_called()
        self.measure.assert_not_called()

    def test_child_coordinates_and_equal_size_topology_edits_remeasure(self):
        for collection, values in [(self.meshes[99].vertices, [0, 0, 0.034, 1, 0, 0, 0, 1, 0]),
                                   (self.meshes[99].loops, [0, 2, 1])]:
            with self.subTest(values=values):
                previous = self.turn.contact_checks[0]["result"]
                collection[:] = values
                results, regressed = contact.refresh(self.turn)
                self.assertNotEqual(results[0]["fingerprint"], previous["fingerprint"])
                self.assertEqual(regressed, [])
                self.measure.assert_called_once()
                self.measure.reset_mock()

    def test_material_edit_does_not_build_shapes_or_measure(self):
        self.meshes[99].materials = ["new material"]
        previous = self.turn.contact_checks[0]["result"]
        self.assertIs(contact.refresh(self.turn)[0][0], previous)
        self.bmesh.new.assert_not_called()
        self.measure.assert_not_called()

    def test_non_mesh_conversion_is_cleared_without_building_measurement_shapes(self):
        obj = self.parts[-1][1]
        obj.type = "CURVE"
        contact.refresh(self.turn)
        obj.to_mesh.assert_called_once()
        obj.to_mesh_clear.assert_called_once()
        self.bmesh.new.assert_not_called()
        self.measure.assert_not_called()
        with mock.patch.object(contact, "_mesh_fingerprint", side_effect=ValueError("bad mesh")):
            self.assertEqual(contact.refresh(self.turn)[0][0]["status"], "unavailable")
        # The parent raises before the curve is read; check the curve's failure cleanup directly too.
        with mock.patch.object(contact, "_mesh_fingerprint", side_effect=ValueError("bad curve")):
            with self.assertRaisesRegex(ValueError, "bad curve"):
                contact._fingerprint(obj.name, None)
        self.assertEqual(obj.to_mesh_clear.call_count, 2)


class FingerprintTest(unittest.TestCase):
    def test_equal_counts_with_different_topology_or_transforms_change_fingerprint(self):
        class Collection(list):
            def foreach_get(self, field, target):
                target[:] = array("i", self)

        mesh = types.SimpleNamespace(loops=Collection([0, 1, 2]), polygons=Collection([3]), edges=Collection())
        matrix = [[1, 0], [0, 1]]
        coords = array("f", [0, 0, 0, 1, 0, 0, 0, 1, 0])
        before = contact._mesh_fingerprint("Socket", matrix, mesh, coords)
        self.assertEqual(before, contact._mesh_fingerprint("Socket", matrix, mesh, coords))
        mesh.loops[:] = [0, 2, 1]
        self.assertNotEqual(before, contact._mesh_fingerprint("Socket", matrix, mesh, coords))
        mesh.loops[:] = [0, 1, 2]
        matrix[0][1] = 2
        self.assertNotEqual(before, contact._mesh_fingerprint("Socket", matrix, mesh, coords))
        matrix[0][1] = 0
        coords[0] = 0.034
        self.assertNotEqual(before, contact._mesh_fingerprint("Socket", matrix, mesh, coords))


if __name__ == "__main__":
    unittest.main()
