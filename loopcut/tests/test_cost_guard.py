"""cost_guard's estimate and plan are pure; reading Blender runs here on stand-in objects. The real
thing, operators and snapshots and renders included, is loopcut/harness/guard_check.py."""

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

from loopcut import cost_guard as G  # noqa: E402


def mod(kind, name=None, **settings) -> dict:
    return {"name": name or kind.title(), "type": kind, "show_viewport": True, "show_render": True, **settings}


def mesh(*modifiers, faces=1000, verts=1000, dims=None, shown=True, renders=True) -> dict:
    return {"kind": "MESH", "faces": faces, "verts": verts, "dims": dims, "shown": shown, "renders": renders,
            "modifiers": list(modifiers)}


def faces(*modifiers, render=False, **view) -> int:
    return G.estimate(mesh(*modifiers, **view), render)[0]


class EstimateTest(unittest.TestCase):
    def test_subdivision_multiplies_by_four_per_level_viewport_or_render(self):
        subsurf = mod("SUBSURF", levels=2, render_levels=6)
        self.assertEqual(faces(subsurf), 16_000)
        self.assertEqual(faces(subsurf, render=True), 4_096_000)
        self.assertEqual(faces(mod("MULTIRES", levels=3, render_levels=1)), 64_000)
        self.assertEqual(faces(mod("SUBSURF", levels=0, render_levels=0)), 1000)

    def test_array_counts_and_fit_length(self):
        self.assertEqual(faces(mod("ARRAY", fit_type="FIXED_COUNT", count=1000, cap_faces=12)), 1_000_012)
        # 2 m wide object, relative offset 1 along X: a 10 m fit holds 10 / 2 + 1 = 6 copies.
        fit = mod("ARRAY", fit_type="FIT_LENGTH", fit_length=10.0, use_relative_offset=True,
                  relative_offset_displace=(1.0, 0.0, 0.0), use_constant_offset=True,
                  constant_offset_displace=(0.0, 0.0, 0.0), cap_faces=0)
        self.assertEqual(faces(fit, dims=(2.0, 2.0, 2.0)), 6000)
        # No offset at all: Blender makes one copy.
        self.assertEqual(faces({**fit, "use_relative_offset": False}, dims=(2.0, 2.0, 2.0)), 1000)

    def test_array_along_a_curve_is_unknown_and_counts_as_one(self):
        total, unknown = G.estimate(mesh(mod("ARRAY", fit_type="FIT_CURVE", count=5)))
        self.assertEqual((total, [m["type"] for m in unknown]), (1000, ["ARRAY"]))

    def test_mirror_doubles_per_axis_and_solidify_doubles_plus_rim(self):
        self.assertEqual(faces(mod("MIRROR", use_axis=(True, True, False))), 4000)
        self.assertEqual(faces(mod("SOLIDIFY", use_rim=False)), 2000)
        self.assertEqual(faces(mod("SOLIDIFY", use_rim=True), faces=1, verts=4), 6)  # A plane: a box.

    def test_bevel_by_segments_and_limit_method(self):
        # A cube with every edge beveled once: 6 faces, 12 edge strips, 8 corners.
        cube = {"faces": 6, "verts": 8}
        self.assertEqual(faces(mod("BEVEL", segments=1, affect="EDGES", limit_method="NONE"), **cube), 26)
        edges = faces(mod("BEVEL", segments=4, affect="EDGES", limit_method="NONE"))
        angle = faces(mod("BEVEL", segments=4, affect="EDGES", limit_method="ANGLE"))
        self.assertEqual(edges, 1000 + 2000 * 4 + 1000 * 16)
        self.assertLess(angle, edges)
        self.assertEqual(faces(mod("BEVEL", segments=1, affect="VERTICES", limit_method="NONE"), **cube), 14)

    def test_screw_uses_steps_or_render_steps_times_iterations(self):
        screw = mod("SCREW", steps=16, render_steps=64, iterations=2)
        profile = {"faces": 0, "verts": 32}
        self.assertEqual(faces(screw, **profile), 32 * 16 * 2)
        self.assertEqual(faces(screw, render=True, **profile), 32 * 64 * 2)

    def test_deform_only_modifiers_keep_the_count_and_decimate_lowers_it(self):
        self.assertEqual(faces(mod("ARMATURE"), mod("DISPLACE"), mod("SMOOTH"), mod("WEIGHTED_NORMAL")), 1000)
        self.assertEqual(faces(mod("DECIMATE", decimate_type="COLLAPSE", ratio=0.25, iterations=0)), 250)
        self.assertEqual(faces(mod("DECIMATE", decimate_type="UNSUBDIV", ratio=1.0, iterations=1)), 250)
        self.assertEqual(faces(mod("TRIANGULATE")), 2000)

    def test_remesh_and_geometry_nodes_are_unknown_and_the_rest_still_counts(self):
        total, unknown = G.estimate(mesh(mod("REMESH"), mod("NODES", "GeometryNodes"), mod("SUBSURF", levels=1)))
        self.assertEqual(total, 4000)  # A lower bound: the unknowns count as x1.
        self.assertEqual([m["name"] for m in unknown], ["Remesh", "GeometryNodes"])
        self.assertEqual(G.estimate(mesh(mod("SOMETHING_NEW")))[1][0]["type"], "SOMETHING_NEW")

    def test_boolean_adds_the_operand_or_is_unknown(self):
        self.assertEqual(faces(mod("BOOLEAN", operand_type="OBJECT", operand_faces=500)), 1500)
        self.assertEqual(len(G.estimate(mesh(mod("BOOLEAN", operand_type="COLLECTION", operand_faces=None)))[1]), 1)

    def test_switched_off_modifiers_do_not_count_in_their_own_mode(self):
        hidden = {**mod("SUBSURF", levels=3, render_levels=3), "show_viewport": False}
        self.assertEqual(faces(hidden), 1000)
        self.assertEqual(faces(hidden, render=True), 64_000)
        unrendered = {**mod("ARRAY", fit_type="FIXED_COUNT", count=10, cap_faces=0), "show_render": False}
        self.assertEqual((faces(unrendered), faces(unrendered, render=True)), (10_000, 1000))

    def test_stack_order_matters(self):
        subsurf, mirror = mod("SUBSURF", levels=2), mod("MIRROR", use_axis=(True, False, False))
        self.assertEqual(faces(mirror, subsurf), faces(subsurf, mirror))  # Both multiply...
        array = mod("ARRAY", fit_type="FIXED_COUNT", count=3, cap_faces=100)
        self.assertEqual(faces(array, subsurf), (3000 + 100) * 16)  # ...caps are subdivided after.
        self.assertEqual(faces(subsurf, array), 16_000 * 3 + 100)


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.limits = patch.multiple(G, MAX_OBJECT_FACES=2_000_000, MAX_SCENE_FACES=6_000_000,
                                     MAX_RENDER_OBJECT_FACES=4_000_000, MAX_RENDER_SCENE_FACES=12_000_000)
        self.limits.start()
        self.addCleanup(self.limits.stop)

    def test_heavy_subsurf_is_switched_off_and_named(self):
        views = {"Chair_Seat": mesh(mod("SUBSURF", "Subsurf", levels=6, render_levels=6), faces=3000)}
        switches, unknown = G.plan(views, ["Chair_Seat"])
        self.assertEqual([(s["object"], s["modifier"]) for s in switches], [("Chair_Seat", "Subsurf")])
        self.assertEqual(G.line(switches[0]),
                         "Subsurf on Chair_Seat, est. 12M faces: viewport display off; lower the viewport level")
        self.assertEqual(unknown, [])
        self.assertTrue(views["Chair_Seat"]["modifiers"][0]["show_viewport"], "plan never changes its input")

    def test_under_the_limit_nothing_happens(self):
        views = {"Cube": mesh(mod("SUBSURF", levels=2), mod("BEVEL", segments=3, affect="EDGES", limit_method="ANGLE"))}
        self.assertEqual(G.plan(views, ["Cube"]), ([], []))

    def test_the_most_expensive_modifier_goes_first_and_only_until_it_fits(self):
        # 1000 faces, x2 mirror, x1000 array, x4 subsurf: 8M. Without the array 8000, without the
        # subsurf 2M, without the mirror 4M: the array goes, and nothing else.
        views = {"Fence": mesh(mod("MIRROR", use_axis=(True, False, False)),
                               mod("ARRAY", fit_type="FIXED_COUNT", count=1000, cap_faces=0),
                               mod("SUBSURF", levels=1))}
        switches, _ = G.plan(views, ["Fence"])
        self.assertEqual([s["type"] for s in switches], ["ARRAY"])
        self.assertEqual(G.line(switches[0]), "Array on Fence, est. 8M faces: viewport display off; lower the count")

    def test_only_changed_objects_are_switched_but_all_count_toward_the_scene(self):
        heavy = lambda: mesh(mod("SUBSURF", levels=5), faces=1500)  # 1.5M each: under the object limit.
        views = {f"Rock{i}": heavy() for i in range(4)}  # 6.1M together: over the scene limit.
        views["Rock0"]["faces"] = 1600
        switches, _ = G.plan(views, ["Rock3"])
        self.assertEqual([(s["object"], s["scene"]) for s in switches], [("Rock3", 6_246_400)])
        self.assertIn("scene est. 6.2M", G.line(switches[0]))
        self.assertEqual(G.plan(views, [])[0], [], "the user's own scene is never touched")

    def test_hidden_objects_do_not_count(self):
        views = {"Hidden": mesh(mod("SUBSURF", levels=8, render_levels=8), shown=False)}
        self.assertEqual(G.plan(views, ["Hidden"]), ([], []))
        self.assertEqual(len(G.plan(views, ["Hidden"], render=True)[0]), 1)
        views["Hidden"]["renders"] = False
        self.assertEqual(G.plan(views, ["Hidden"], render=True), ([], []))

    def test_unknown_modifiers_are_reported_and_never_switched_off(self):
        views = {"Rock": mesh(mod("REMESH"), mod("SUBSURF", levels=6), faces=1000)}
        switches, unknown = G.plan(views, ["Rock"])
        self.assertEqual([s["modifier"] for s in switches], ["Subsurf"])
        self.assertEqual(unknown, ["Remesh on Rock"])
        self.assertIn("est. at least 4.1M faces", G.line(switches[0]))
        views = {"Blob": mesh(mod("NODES", "GeometryNodes"), faces=5_000_000)}
        self.assertEqual(G.plan(views, ["Blob"]), ([], ["GeometryNodes on Blob"]))

    def test_non_mesh_objects_are_unknown(self):
        views = {"Title": {"kind": "FONT", "faces": None, "verts": None, "dims": None, "shown": True,
                           "renders": True, "modifiers": [mod("SUBSURF", levels=6)]}}
        self.assertEqual(G.plan(views, ["Title"]), ([], ["modifiers on Title (a font object)"]))

    def test_render_plan_uses_render_levels_and_render_limits(self):
        views = {"Chair_Seat": mesh(mod("SUBSURF", "Subsurf", levels=1, render_levels=6), faces=3000)}
        self.assertEqual(G.plan(views, ["Chair_Seat"]), ([], []))
        switches, _ = G.plan(views, list(views), render=True)
        self.assertEqual(G.line(switches[0], render=True),
                         "Subsurf on Chair_Seat renders at est. 12M faces; lower its render_levels")

    def test_counts_read_short(self):
        self.assertEqual([G.count_text(n) for n in (999, 1500, 384_000, 1_500_000, 12_000_000, 3 * 10 ** 9)],
                         ["999", "1.5K", "384K", "1.5M", "12M", "3B"])


def fake_modifier(kind, name=None, **settings):
    return types.SimpleNamespace(name=name or kind.title(), type=kind, show_viewport=True, show_render=True, **settings)


class Collection(list):
    def get(self, name):
        return next((item for item in self if item.name == name), None)


def fake_object(name, *modifiers, polygons=6, vertices=8, kind="MESH", bound_box=None):
    data = types.SimpleNamespace(is_editmode=False, polygons=[None] * polygons, vertices=[None] * vertices)
    return types.SimpleNamespace(name=name, type=kind, data=data if kind == "MESH" else None,
                                 modifiers=Collection(modifiers), hide_viewport=False, hide_render=False,
                                 bound_box=bound_box or [(-1, -1, -1), (1, 1, 1)])


class ReadTest(unittest.TestCase):
    def test_reads_settings_counts_caps_and_fit_length_size(self):
        cap = fake_object("Cap", polygons=10)
        array = fake_modifier("ARRAY", fit_type="FIT_LENGTH", count=2, fit_length=10.0, use_relative_offset=True,
                              relative_offset_displace=[1.0, 0.0, 0.0], use_constant_offset=False,
                              constant_offset_displace=[0.0, 0.0, 0.0], start_cap=cap, end_cap=None)
        view = G.read_object(fake_object("Rail", array, fake_modifier("MIRROR", use_axis=[True, False, True])))
        self.assertEqual((view["faces"], view["verts"], view["dims"]), (6, 8, (2, 2, 2)))
        self.assertEqual(view["modifiers"][0]["cap_faces"], 10)
        self.assertEqual(view["modifiers"][0]["relative_offset_displace"], (1.0, 0.0, 0.0))
        self.assertEqual(view["modifiers"][1]["use_axis"], (True, False, True))
        self.assertEqual(G.estimate(view)[0], (6 * 6 + 10) * 4)

    def test_objects_without_modifiers_are_skipped_and_boolean_operands_counted(self):
        self.assertIsNone(G.read_object(fake_object("Plain")))
        cutter = fake_object("Cutter", polygons=500)
        boolean = fake_modifier("BOOLEAN", operand_type="OBJECT", object=cutter)
        self.assertEqual(G.read_object(fake_object("Box", boolean))["modifiers"][0]["operand_faces"], 500)


class Ops:
    """A stand-in for Blender's bpy.ops module: operators come from _op_create_function."""

    def __init__(self, log):
        self.log = log

    def _op_create_function(self, module, name):
        log = self.log

        class Op:
            __doc__ = f"{module}.{name} docs"

            def idname_py(self):
                return f"{module}.{name}"

            def poll(self):
                return True

            def __call__(self, *args, **kwargs):
                log.append(("ran", f"{module}.{name}", args, kwargs))
                return {"FINISHED"}
        return Op()


class WatchingOpsTest(unittest.TestCase):
    def setUp(self):
        self.log = []
        self.module = types.ModuleType("bpy.ops")
        self.module._op_create_function = Ops(self.log)._op_create_function
        self.real = self.module._op_create_function
        modules = patch.dict(sys.modules, {"bpy.ops": self.module})
        modules.start()
        self.addCleanup(modules.stop)
        for name, fake in (("check", lambda: self.log.append(("check",))),
                           ("render_refusal", lambda: self.refusal)):
            patcher = patch.object(G, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.refusal = ""
        G._notes.clear()
        G._unknown.clear()

    def test_every_operator_checks_before_it_runs_and_is_otherwise_itself(self):
        with G.watching_ops():
            op = self.module._op_create_function("object", "shade_smooth")
            self.assertTrue(op.poll())
            self.assertEqual(op.__doc__, "object.shade_smooth docs")
            self.assertEqual(op("EXEC_DEFAULT", keep=True), {"FINISHED"})
        self.assertEqual(self.log, [("check",), ("ran", "object.shade_smooth", ("EXEC_DEFAULT",), {"keep": True})])
        self.assertIs(self.module._op_create_function, self.real)

    def test_an_operator_kept_past_the_run_just_runs(self):
        with G.watching_ops():
            kept = self.module._op_create_function("object", "select_all")
        kept()
        self.assertEqual(self.log, [("ran", "object.select_all", (), {})])

    def test_restored_even_when_the_code_fails(self):
        with self.assertRaises(KeyError), G.watching_ops():
            raise KeyError("model bug")
        self.assertIs(self.module._op_create_function, self.real)

    def test_a_bake_over_the_limit_is_refused_before_it_runs_and_reported(self):
        self.refusal = "Render refused by the cost guard: ...\n- Subsurf on Chair renders at est. 12M faces"
        with G.watching_ops():
            with self.assertRaises(RuntimeError) as caught:
                self.module._op_create_function("object", "bake")(type="COMBINED")
            self.module._op_create_function("object", "select_all")()
        self.assertIn("Subsurf on Chair", str(caught.exception))
        self.assertNotIn(("ran", "object.bake", (), {"type": "COMBINED"}), self.log)
        text, notes = G.take()
        self.assertIn("Render refused", text)
        self.assertEqual(notes, [{"text": self.refusal, "modifiers": []}])

    def test_a_light_bake_runs(self):
        with G.watching_ops():
            self.module._op_create_function("object", "bake")(type="COMBINED")
        self.assertEqual(self.log, [("check",), ("ran", "object.bake", (), {"type": "COMBINED"})])

    def test_a_final_render_in_the_code_is_refused_however_light_and_points_to_the_tools(self):
        self.assertEqual(self.refusal, "", "the scene fits the render limits")
        with G.watching_ops():
            for name in ("render", "play_rendered_anim"):
                op = self.module._op_create_function("render", name)
                self.assertTrue(op.poll(), "only the call is refused")
                with self.assertRaises(RuntimeError) as caught:
                    op(write_still=True)
                self.assertIn(f"render.{name} in run_python", str(caught.exception))
                for tool in ("see_render", "render_job"):
                    self.assertIn(tool, str(caught.exception))
            self.module._op_create_function("render", "opengl")()
            self.module._op_create_function("render", "view_show")()
        self.assertEqual([entry[1] for entry in self.log if entry[0] == "ran"], ["render.opengl", "render.view_show"])
        text, notes = G.take()
        self.assertEqual([n["text"] for n in notes], [G.in_process_refusal("render.render"),
                                                      G.in_process_refusal("render.play_rendered_anim")])
        self.assertTrue(text.startswith("Render refused by the cost guard: render.render"), text)


class RefusalsAndTheStepBudgetTest(unittest.TestCase):
    """A refused render is a failed call like any other to repeats.Ledger, keyed by its message with
    the numbers blanked: the third refusal of the same render is "not working", whatever the estimate."""

    def refusal(self, render_levels: int) -> str:
        seat = mesh(mod("SUBSURF", levels=0, render_levels=render_levels), faces=4000)
        with patch.object(G, "read_scene", lambda: {"Seat": seat}):
            return G.render_refusal()

    def test_a_render_job_refused_again_and_again(self):
        from loopcut import repeats
        ledger = repeats.Ledger()
        key = repeats.call_key("render_job", '{"action": "start", "output_dir": "/out", "frames": [1], '
                                             '"budget_seconds": 60}')
        texts = [self.refusal(levels) for levels in (6, 5, 6)]  # What tools.execute returns for the ToolError.
        self.assertIn("Seat renders at est. 16M faces", texts[0])
        self.assertIn("Seat renders at est. 4.1M faces", texts[1])
        notes = [ledger.record(step, key, text, ok=False) for step, text in enumerate(texts, 1)]
        self.assertEqual(notes[0], "")
        self.assertIn("Same error as before", notes[1])
        self.assertIn("This approach is not working", notes[2])

    def test_a_render_refused_in_run_python_again_and_again(self):
        from loopcut import repeats
        ledger = repeats.Ledger()
        notes = []
        for step, levels in enumerate((6, 5, 6), 1):
            refusal = self.refusal(levels)  # As run_python reports it: the traceback, then cost_guard.take().
            text = (f"Traceback (most recent call last):\n  File \"<loopcut>\", line {step}, in <module>\n"
                    f"RuntimeError: {refusal}\n\n{refusal}\n\nScene changes: none. (Tracked: objects)")
            key = repeats.call_key("run_python", f'{{"code": "render({step})", "summary": "Render {step}"}}')
            notes.append(ledger.record(step, key, text, ok=False))
        self.assertIn('Already tried: step 1 "Render 1"', notes[1])
        self.assertIn("This approach is not working", notes[2])


class TakeTest(unittest.TestCase):
    def test_report_for_the_model_and_notes_for_the_panel(self):
        G._notes[:] = [{"text": "Subsurf on Chair_Seat, est. 12M faces: viewport display off; lower the viewport level",
                        "modifiers": [["Chair_Seat", "Subsurf"]]}]
        G._unknown[:] = ["Remesh on Rock"]
        text, notes = G.take()
        self.assertIn("switched off show_viewport", text)
        self.assertIn("- Subsurf on Chair_Seat, est. 12M faces", text)
        self.assertIn("could not estimate, left on: Remesh on Rock.", text)
        self.assertTrue(text.endswith("\n\n"))
        self.assertEqual(notes[0]["modifiers"], [["Chair_Seat", "Subsurf"]])
        self.assertEqual(G.take(), ("", []), "taken once")


if __name__ == "__main__":
    unittest.main()
