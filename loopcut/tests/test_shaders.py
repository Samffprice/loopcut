"""C32: a look's shaders compile in a preview job before its EEVEE capture, waited for off the main
thread; Subprocess shader compilation once on Windows and Linux. The real GPU side (the same
image, the interface live) is measured in loopcut/track/FREEZE-2026-10-10.md's probe."""

import contextlib
import io
import sys
import tempfile
import types
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

fake_bpy = types.ModuleType("bpy")
fake_bpy.app = types.SimpleNamespace(driver_namespace={})
sys.modules.setdefault("bpy", fake_bpy)

from loopcut import llm, mainthread, shaders, tools  # noqa: E402


class FakeID:
    def __init__(self, name, uid, content="v1", id_type="MATERIAL", library=None, custom=False, gp=False):
        self.name, self.session_uid, self.content, self.id_type = name, uid, content, id_type
        self.library, self.override_library = library, None
        self.preview = types.SimpleNamespace(is_image_custom=custom)
        self.is_grease_pencil = gp


def obj(name, *materials, visible=True, kind="MESH"):
    return types.SimpleNamespace(name=name, type=kind, visible_get=lambda: visible,
                                 material_slots=[types.SimpleNamespace(material=m) for m in materials])


class Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def time(self):
        return 1_700_000_000.0

    def sleep(self, seconds):
        self.now += seconds


class Base(unittest.TestCase):
    def setUp(self):
        self.world = FakeID("World", 900, id_type="WORLD")
        self.metal, self.copper = FakeID("Metal", 1), FakeID("Copper", 2)
        self.scene = types.SimpleNamespace(render=types.SimpleNamespace(engine="BLENDER_EEVEE"), world=self.world,
                                           objects=[obj("Cube", self.metal), obj("Floor", self.copper)])
        self.overrides, self.started, self.refuse = [], [], set()
        self.bpy = types.SimpleNamespace(
            app=types.SimpleNamespace(background=False, is_job_running=lambda kind: False),
            context=types.SimpleNamespace(scene=self.scene, temp_override=self.temp_override),
            ops=types.SimpleNamespace(ed=types.SimpleNamespace(lib_id_generate_preview=self.generate_preview)))
        self.enterContext(mock.patch.object(shaders, "bpy", self.bpy))
        self.enterContext(mock.patch.object(shaders, "signature", lambda idb: hash((idb.name, idb.content))))
        self.enterContext(mock.patch.object(shaders, "_seen", {}))
        self.enterContext(mock.patch.object(shaders, "_engine_ready", False))
        self.enterContext(mock.patch.object(tools, "_view3d_override", return_value={"window": "main window"}))
        self.out = self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.context = {}

    @contextlib.contextmanager
    def temp_override(self, **kw):
        self.overrides.append(kw)
        self.context = kw
        try:
            yield
        finally:
            self.context = {}

    def generate_preview(self):
        """Generate Preview: the context's "id", a job started."""
        idb = self.context["id"]
        if idb.name in self.refuse:
            raise RuntimeError("Operator bpy.ops.ed.lib_id_generate_preview.poll() failed")
        self.started.append(idb.name)
        return {"FINISHED"}

    @staticmethod
    def names(plan):
        return [idb.name for idb in plan.stale]


class PrepareTest(Base):
    def test_the_first_look_plans_a_preview_of_every_material_it_draws_and_starts_none(self):
        plan = shaders.prepare("material", "three_quarter")
        self.assertEqual(self.names(plan), ["Metal", "Copper"])
        self.assertEqual((self.started, plan.warmed), ([], []))
        self.assertEqual(set(plan.signatures), {1, 2})

    def test_after_a_look_unchanged_materials_need_nothing_and_a_changed_one_is_previewed_again(self):
        shaders.remember(shaders.prepare())
        self.assertEqual(self.names(shaders.prepare()), [])  # The engine's shaders too: no world preview.
        self.copper.content = "darker"
        self.assertEqual(self.names(shaders.prepare()), ["Copper"])

    def test_a_new_material_on_a_new_object_is_previewed(self):
        shaders.remember(shaders.prepare())
        self.scene.objects.append(obj("Sphere", FakeID("Wave", 3), self.metal))
        self.assertEqual(self.names(shaders.prepare()), ["Wave"])

    def test_the_first_look_with_nothing_new_still_compiles_eevees_own_shaders_through_the_world(self):
        self.scene.objects = [obj("Empty mesh")]
        self.assertEqual(self.names(shaders.prepare()), ["World"])
        shaders.remember(shaders.prepare())
        self.assertEqual(self.names(shaders.prepare()), [])

    def test_no_warm_up_for_solid_looks_the_users_view_a_background_blender_or_other_engines(self):
        self.assertIsNone(shaders.prepare("distinct", "front"))
        self.assertIsNone(shaders.prepare("material", "user"))
        for engine in ("CYCLES", "BLENDER_WORKBENCH"):  # Their previews compile nothing the look uses.
            self.scene.render.engine = engine
            self.assertIsNone(shaders.prepare("material", "front"))
        self.scene.render.engine = "BLENDER_EEVEE"
        self.bpy.app.background = True
        self.assertIsNone(shaders.prepare("material", "front"))

    def test_linked_hand_made_previews_hidden_objects_and_grease_pencil_are_left_alone(self):
        linked, custom, hidden, pencil = (FakeID("Linked", 5, library="lib.blend"), FakeID("Custom", 6, custom=True),
                                          FakeID("Hidden", 7), FakeID("Pencil", 8, gp=True))
        self.scene.objects = [obj("A", linked, custom), obj("B", hidden, visible=False), obj("C", pencil),
                              obj("Lamp", self.metal, kind="LIGHT")]
        plan = shaders.prepare()
        self.assertEqual(self.names(plan), ["World"])  # Nothing previewable, first EEVEE look of the session.
        self.assertEqual(set(plan.signatures), {5, 6})  # Still drawn, so remembered after the look.

    def test_the_focus_goes_first_and_previews_are_capped(self):
        many = [FakeID(f"M{i}", 100 + i) for i in range(shaders.MAX_WARMED + 3)]
        self.scene.objects = [obj(f"O{i}", m) for i, m in enumerate(many)] + [obj("Hero", self.metal)]
        plan = shaders.prepare(focus=["Hero"])
        self.assertEqual(len(plan.stale), shaders.MAX_WARMED)
        self.assertEqual(plan.stale[0].name, "Metal")
        self.assertEqual(len(plan.signatures), shaders.MAX_WARMED + 4)  # The rest compile in the draw, then count as seen.

    def test_a_rendered_look_also_warms_a_changed_world(self):
        shaders.remember(shaders.prepare("rendered"))
        self.assertEqual(self.names(shaders.prepare("rendered")), [])
        self.world.content = "sunset"
        self.assertEqual(self.names(shaders.prepare("rendered")), ["World"])
        self.assertEqual(self.names(shaders.prepare("material")), [])  # Material Preview draws a studio light instead.


class StartTest(Base):
    def test_a_preview_starts_through_generate_preview_in_the_users_window(self):
        self.assertTrue(shaders.start(self.metal))
        self.assertEqual(self.started, ["Metal"])
        self.assertEqual(self.overrides, [{"window": "main window", "id": self.metal}])

    def test_a_preview_that_cannot_start_is_skipped_with_a_line(self):
        self.refuse.add("Metal")
        self.assertFalse(shaders.start(self.metal))
        self.assertIn("could not start compiling shaders for a look", self.out.getvalue())


class SignatureTest(unittest.TestCase):
    def test_groups_count_once_and_a_world_reads_as_a_world(self):
        from loopcut import scene_diff
        group = types.SimpleNamespace(name="Grain", session_uid=50, nodes=[])
        node = types.SimpleNamespace(type="GROUP", node_tree=group)
        material = types.SimpleNamespace(id_type="MATERIAL", node_tree=types.SimpleNamespace(nodes=[node, node]))
        world = types.SimpleNamespace(id_type="WORLD", color=(0, 0, 0), node_tree=types.SimpleNamespace(nodes=[]))
        with mock.patch.object(scene_diff, "_material", return_value={"x": 1}) as material_fields, \
                mock.patch.object(scene_diff, "node_tree", side_effect=lambda tree, *a: {"tree": id(tree)}) as tree_fields:
            first = shaders.signature(material)
            self.assertEqual(first, shaders.signature(material))
            shaders.signature(world)
        self.assertEqual(material_fields.call_count, 2)
        self.assertEqual([c.args[0] for c in tree_fields.call_args_list], [group, group, world.node_tree])


class WaitTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.out = self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def run_wait(self, states, limit=shaders.WAIT_SECONDS, cancel_after=None):
        states = iter(states)
        calls = []

        def on_main(fn):
            calls.append(mainthread.task_name(fn))
            return fn()

        cancelled = (lambda: len(calls) >= cancel_after) if cancel_after else (lambda: False)
        with mock.patch.object(shaders, "busy", lambda: next(states, False)):
            settled = shaders.wait(cancelled, on_main, self.clock.now + limit, self.clock.monotonic, self.clock.sleep)
        return settled, calls

    def test_waits_until_nothing_compiles_for_the_settle_time(self):
        settled, calls = self.run_wait([True, True, False, False, False, False])
        self.assertTrue(settled)
        self.assertAlmostEqual(self.clock.now - 1000.0, 0.2 + shaders.SETTLE_SECONDS)
        self.assertEqual(set(calls), {"a check on the look's shaders"})

    def test_a_suspended_preview_job_between_two_running_ones_does_not_end_the_wait(self):
        self.run_wait([True, False, True, False, False, False, False])
        self.assertAlmostEqual(self.clock.now - 1000.0, 0.3 + shaders.SETTLE_SECONDS)

    def test_the_deadline_ends_the_wait(self):
        settled, _ = self.run_wait(iter(lambda: True, None), limit=1.0)
        self.assertFalse(settled)
        self.assertAlmostEqual(self.clock.now - 1000.0, 1.0)

    def test_stop_ends_the_wait(self):
        with self.assertRaises(llm.Cancelled):
            self.run_wait(iter(lambda: True, None), cancel_after=3)


class WarmTest(Base):
    """The previews run one at a time: each starts once the compiler has been idle for the settle
    time, so the redraw a finished job asks for is drawn before the next job holds the draw lock."""

    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.log = []  # ("busy", bool) per check, ("start", name) per preview started
        self.jobs = []  # The running job's remaining checks, per start.

    def busy(self):
        running = bool(self.jobs) and self.jobs[0] > 0
        if running:
            self.jobs[0] -= 1
        self.log.append(("busy", running))
        return running

    def on_main(self, fn):
        result = fn()
        if getattr(fn, "func", None) is shaders.start:  # A started preview runs for three checks.
            self.log.append(("start", fn.args[0].name))
            self.jobs[:] = [3]
        return result

    def warm(self, plan, limit=shaders.WAIT_SECONDS, cancelled=lambda: False):
        with mock.patch.object(shaders, "busy", self.busy):
            return shaders.warm(plan, cancelled, self.on_main, limit, self.clock.monotonic, self.clock.sleep)

    def test_previews_start_one_at_a_time_after_the_last_one_settled(self):
        plan = shaders.prepare()
        taken = self.warm(plan)
        self.assertEqual(plan.warmed, ["Metal", "Copper"])
        starts = [i for i, entry in enumerate(self.log) if entry[0] == "start"]
        self.assertEqual([self.log[i][1] for i in starts], ["Metal", "Copper"])
        settle = round(shaders.SETTLE_SECONDS / shaders.POLL_SECONDS) + 1  # Idle checks before a start.
        for i in starts:
            self.assertEqual(self.log[i - settle:i], [("busy", False)] * settle)
        self.assertEqual(self.log[-settle:], [("busy", False)] * settle)  # And before the capture.
        self.assertAlmostEqual(taken, self.clock.now - 1000.0)
        self.assertEqual(mainthread.background_work, "")

    def test_a_preview_that_cannot_start_is_skipped(self):
        self.refuse.add("Metal")
        plan = shaders.prepare()
        self.warm(plan)
        self.assertEqual(plan.warmed, ["Copper"])

    def test_the_limit_stops_starting_previews_and_says_so(self):
        plan = shaders.prepare()
        self.jobs[:] = [10_000]  # Someone else's preview never ends.
        self.assertIsNone(self.warm(plan, limit=1.0))
        self.assertEqual(plan.warmed, [])
        self.assertIn("still compiling after 1 s; taking it anyway", self.out.getvalue())
        self.assertEqual(mainthread.background_work, "")

    def test_stop_ends_the_warm_up(self):
        plan = shaders.prepare()
        with self.assertRaises(llm.Cancelled):
            self.warm(plan, cancelled=lambda: len(self.log) > 6)
        self.assertEqual(mainthread.background_work, "")

    def test_warming_logs_no_freeze_and_names_a_jobs_hitch_as_during_the_wait(self):
        """The real pump, a fake clock: checks and starts are quick tasks; a late pump while a preview
        job runs (its own main-thread hitch) is a true line, said to be during the wait."""
        self.enterContext(mock.patch.object(mainthread, "time", self.clock))
        slow = self.enterContext(mock.patch.object(mainthread, "slow", mainthread.collections.deque(maxlen=20)))
        self.enterContext(mock.patch.object(mainthread, "_animating", return_value=None))
        self.enterContext(mock.patch.object(mainthread, "_redraw_requested", False))
        self.enterContext(mock.patch.object(mainthread, "_turn_running", return_value=True))
        self.enterContext(mock.patch.object(mainthread, "_pumped_at", self.clock.now))
        tasks = []

        def on_main(fn):  # The next pump runs the task: on time, except once after a 420 ms hitch.
            future = mainthread.run_on_main(fn)
            self.clock.now += 0.45 if len(tasks) == 6 else mainthread._INTERVAL
            tasks.append(mainthread.task_name(fn))
            mainthread._pump()
            result = future.result(timeout=0)
            if getattr(fn, "func", None) is shaders.start:
                self.jobs[:] = [3]
            return result

        def sleep(seconds):  # The pump keeps running while the agent thread sleeps.
            for _ in range(round(seconds / mainthread._INTERVAL)):
                self.clock.now += mainthread._INTERVAL
                mainthread._pump()

        plan = shaders.prepare()
        with mock.patch.object(shaders, "busy", self.busy):
            self.assertIsNotNone(shaders.warm(plan, lambda: False, on_main, clock=self.clock.monotonic, sleep=sleep))
        self.assertIn("starting a look's shader preview", tasks)
        self.assertEqual([(what, ms) for _, what, ms in slow],
                         [("Blender's main thread outside Loopcut's tasks, while shaders compiled for a look "
                           "(the pump ran late)", 420)])


class BeforeLookTest(Base):
    def setUp(self):
        super().setUp()
        self.captures = []

    def capture(self):
        self.captures.append(1)
        return tools.ToolResult("Image attached: front view", image_path=Path("/tmp/viewport.png"))

    def test_outside_deferring_the_look_is_taken_at_once(self):
        with mock.patch.object(shaders, "prepare") as prepare:
            result = shaders.before_look(self.capture)
        prepare.assert_not_called()
        self.assertEqual((result.text, result.then, len(self.captures)), ("Image attached: front view", None, 1))

    def test_nothing_to_compile_takes_the_look_at_once_and_remembers_it(self):
        shaders.remember(shaders.prepare())
        with shaders.deferring():
            result = shaders.before_look(self.capture)
        self.assertIsNone(result.then)
        self.assertEqual(len(self.captures), 1)

    def test_with_shaders_to_compile_the_look_waits_in_then_and_keeps_its_freeze_log_name(self):
        with shaders.deferring():
            pending = shaders.before_look(self.capture, focus=["Cube"])
        self.assertFalse(shaders._deferring)
        self.assertEqual((self.captures, self.started), ([], []))  # Nothing starts on the tool's own task.
        self.assertIsNotNone(pending.then)
        names = []

        def on_main(fn):
            names.append(mainthread.task_name(fn))
            return fn()

        with mock.patch.object(shaders, "warm", return_value=0.4) as warm:
            result = pending.then(lambda: False, on_main)
        self.assertEqual([idb.name for idb in warm.call_args.args[0].stale], ["Metal", "Copper"])
        self.assertEqual(names, ["the capture_viewport tool"])
        self.assertEqual((result.text, len(self.captures)), ("Image attached: front view", 1))
        self.assertEqual(set(shaders._seen), {1, 2})
        self.assertTrue(shaders._engine_ready)

    def test_a_timed_out_warm_up_still_takes_the_same_look(self):
        with shaders.deferring():
            pending = shaders.before_look(self.capture)
        with mock.patch.object(shaders, "warm", return_value=None):
            result = pending.then(lambda: False, lambda fn: fn())
        self.assertEqual((result.ok, len(self.captures)), (True, 1))

    def test_a_warm_up_that_breaks_still_takes_the_look(self):
        with shaders.deferring(), mock.patch.object(shaders, "prepare", side_effect=RuntimeError("odd node")):
            result = shaders.before_look(self.capture)
        self.assertEqual((result.then, len(self.captures)), (None, 1))
        self.assertIn("could not prepare the look's shaders", self.out.getvalue())

    def test_a_look_that_fails_after_the_wait_is_a_failed_result_and_not_remembered(self):
        def gone():
            raise tools.ToolError("No such object(s): Cube")
        with shaders.deferring():
            pending = shaders.before_look(gone, focus=["Cube"])
        with mock.patch.object(shaders, "warm", return_value=0.1):
            result = pending.then(lambda: False, lambda fn: fn())
        self.assertEqual((result.ok, result.text), (False, "No such object(s): Cube"))
        self.assertEqual(shaders._seen, {})


class ToolsGlueTest(unittest.TestCase):
    def test_the_capture_tools_go_through_before_look(self):
        self.assertIs(tools._DISPATCH["capture_viewport"], tools.capture_viewport_tool)
        self.assertIs(tools._DISPATCH["compare_with_reference"], tools.compare_with_reference_tool)
        with mock.patch.object(shaders, "before_look", return_value="deferred") as before:
            self.assertEqual(tools.capture_viewport_tool(focus=["Cube"], angle="front", style="rendered"), "deferred")
        self.assertEqual(before.call_args.args[1:], ("rendered", "front", ["Cube"]))

    def run_python(self, shot):
        from loopcut import config, contact, cost_guard, op_notes, scene_diff, shape_key_guard
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(tools, "_python_namespace", side_effect=lambda: {"bpy": tools.bpy}))
            for owner, name, value in (
                    (tools, "_editor_context", contextlib.nullcontext()),
                    (config, "run_timeout", 60), (cost_guard, "begin", None),
                    (cost_guard, "check", None), (cost_guard, "take", ("", [])),
                    (shape_key_guard, "install", None), (shape_key_guard, "check", None),
                    (shape_key_guard, "take", []), (scene_diff, "snapshot", {"objects": {}}),
                    (scene_diff, "diff", {"other": []}), (scene_diff, "is_empty", False),
                    (scene_diff, "for_model", "Scene changes: Metal added"), (contact, "recheck", [])):
                stack.enter_context(mock.patch.object(owner, name, return_value=value))
            stack.enter_context(mock.patch.object(op_notes, "_notices", []))
            stack.enter_context(mock.patch.object(tools.bpy, "ops", types.SimpleNamespace(
                ed=types.SimpleNamespace(undo_push=lambda **kw: None)), create=True))
            before = stack.enter_context(mock.patch.object(shaders, "before_look", return_value=shot))
            result = tools.run_python("x = 1", "Metal", capture="front")
        self.assertEqual(before.call_args.args[1:], ("material", "front"))
        return result

    def test_run_python_with_a_capture_keeps_the_step_and_adds_the_look_after_the_wait(self):
        look = tools.ToolResult("Image attached: front view", image_path=Path("/tmp/viewport.png"))
        result = self.run_python(tools.ToolResult("Compiling…", then=lambda is_cancelled, on_main: look))
        self.assertIsNone(result.image_path)
        self.assertIs(result.then(lambda: False, None), result)
        self.assertEqual(result.image_path, Path("/tmp/viewport.png"))
        self.assertIn("Scene changes: Metal added\n\nImage attached: front view", result.text)
        self.assertEqual(result.scene_after, {"objects": {}})

    def test_stop_during_the_wait_keeps_the_steps_result(self):
        def stopped(is_cancelled, on_main):
            raise llm.Cancelled()
        result = self.run_python(tools.ToolResult("Compiling…", then=stopped))
        self.assertIs(result.then(lambda: True, None), result)
        self.assertIn("Capture skipped: stopped.", result.text)
        self.assertTrue(result.ok)

    def test_a_look_taken_at_once_or_failed_is_added_as_before(self):
        result = self.run_python(tools.ToolResult("Image attached: front view", image_path=Path("/tmp/v.png")))
        self.assertIsNone(result.then)
        self.assertEqual(result.image_path, Path("/tmp/v.png"))
        result = self.run_python(tools.ToolResult("No 3D viewport is open", ok=False))
        self.assertIn("Capture failed: No 3D viewport is open", result.text)

    def test_the_agent_runs_tools_inside_deferring_and_only_there(self):
        from loopcut import agent
        seen = []

        def execute(name, arguments):
            seen.append(shaders._deferring)
            return tools.ToolResult("ok")

        def run_now(fn):
            future = Future()
            future.set_result(fn())
            return future

        with mock.patch.object(tools, "execute", execute), mock.patch.object(mainthread, "run_on_main", run_now):
            agent._run_tool_on_main("capture_viewport", "{}")
        self.assertEqual(seen, [True])
        self.assertFalse(shaders._deferring)


class SubprocessDefaultTest(unittest.TestCase):
    def prefs(self, method="THREAD", dirty=False, once=""):
        preferences = types.SimpleNamespace(system=types.SimpleNamespace(shader_compilation_method=method),
                                            is_dirty=dirty)
        return preferences, types.SimpleNamespace(shader_compilation_once=once)

    def test_windows_and_linux_switch_thread_to_subprocess_once_and_mark_preferences_modified(self):
        for platform in ("win32", "linux"):
            preferences, addon = self.prefs()
            self.assertEqual(shaders.default_to_subprocess(preferences, addon, platform), "subprocess")
            self.assertEqual(preferences.system.shader_compilation_method, "SUBPROCESS")
            self.assertEqual(addon.shader_compilation_once, "subprocess")
            self.assertTrue(preferences.is_dirty)  # Blender's Save on Exit decides; nothing saved here.

    def test_macos_is_left_alone(self):
        preferences, addon = self.prefs()
        self.assertEqual(shaders.default_to_subprocess(preferences, addon, "darwin"), "")
        self.assertEqual((preferences.system.shader_compilation_method, addon.shader_compilation_once,
                          preferences.is_dirty), ("THREAD", "", False))

    def test_a_user_who_switched_back_to_thread_keeps_it(self):
        preferences, addon = self.prefs(once="subprocess")
        self.assertEqual(shaders.default_to_subprocess(preferences, addon, "win32"), "")
        self.assertEqual((preferences.system.shader_compilation_method, preferences.is_dirty), ("THREAD", False))

    def test_a_user_who_chose_subprocess_is_remembered_so_a_later_thread_stays(self):
        preferences, addon = self.prefs(method="SUBPROCESS")
        self.assertEqual(shaders.default_to_subprocess(preferences, addon, "linux"), "kept")
        self.assertEqual(addon.shader_compilation_once, "kept")
        preferences.system.shader_compilation_method = "THREAD"
        self.assertEqual(shaders.default_to_subprocess(preferences, addon, "linux"), "")
        self.assertEqual(preferences.system.shader_compilation_method, "THREAD")

    def test_before_the_first_save_the_modified_mark_is_put_back(self):
        """No preferences file yet: an exit before the first-run setup must not write one."""
        for dirty in (False, True):
            preferences, addon = self.prefs(dirty=dirty)
            self.assertEqual(shaders.default_to_subprocess(preferences, addon, "win32", prefs_saved=False), "subprocess")
            self.assertEqual((preferences.system.shader_compilation_method, preferences.is_dirty), ("SUBPROCESS", dirty))

    def test_no_add_on_preferences_means_nothing_is_done(self):
        preferences, _ = self.prefs()
        self.assertEqual(shaders.default_to_subprocess(preferences, None, "win32"), "")
        self.assertEqual(preferences.system.shader_compilation_method, "THREAD")

    def test_prefs_saved_looks_for_userpref_blend(self):
        with tempfile.TemporaryDirectory() as config:
            fake = types.SimpleNamespace(utils=types.SimpleNamespace(user_resource=lambda kind: config))
            with mock.patch.object(shaders, "bpy", fake):
                self.assertFalse(shaders._prefs_saved())
                (Path(config) / "userpref.blend").write_bytes(b"BLENDER")
                self.assertTrue(shaders._prefs_saved())


if __name__ == "__main__":
    unittest.main()
