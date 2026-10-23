"""The tools the model is offered. The asset libraries are one search_assets / import_asset pair
and render jobs one render_job: each call must reach the same implementation with the same
arguments, and keep the approval it had. Calls to the old names in saved conversations stay
readable but are never dispatched. No Blender, no network."""

import io
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

from loopcut import (agent, asset_libraries, assets, blenderkit, camera, config, contact, context,  # noqa: E402
                     conversations, files, inspection, job_tools, llm, polyhaven, polypizza, state, texture_tools, tools,
                     uv_tools)

TOOL_SCHEMAS = agent._tool_schemas  # test_agent_loop stubs these three; keep the real ones.
CHANGES_SCENE = agent._changes_scene
SAME_WHILE_UNCHANGED = agent._same_while_unchanged
FOUND = tools.ToolResult("found")
# Every tool offered, in the order the model sees it, and its class for a plain call: "change" takes a
# checkpoint and asks first; "read" is same-while-unchanged, under the step budget's identical-call rule.
OFFERED = [
    ("run_python", "change"), ("get_scene_info", "read"), ("get_object_info", "read"), ("inspect_api", "read"),
    ("compare_with_reference", ""), ("look_at_reference", "read"), ("capture_viewport", ""),
    ("check_contact", "read"), ("list_files", ""), ("read_file", ""), ("write_file", ""), ("move_file", ""),
    ("see_render", ""), ("render_job", ""), ("inspect_scene", ""), ("search_assets", "read"),
    ("import_asset", "change"), ("frame_camera", "change"), ("inspect_uv", "read"), ("uv_unwrap", "change"),
    ("transfer_textures", "change"), ("inspect_texture", "read"),
]


def execute(name: str, **arguments) -> tools.ToolResult:
    return tools.execute(name, json.dumps(arguments))


def call(call_id: str, name: str, arguments) -> dict:
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": raw}}


class OfferedTest(unittest.TestCase):
    def test_activity_titles_stay_local_and_do_not_change_the_prompt_budget(self):
        schemas = [{"type": "function", "function": {
            "name": "read_data", "title": "Reading data", "description": "Read the stored data.",
            "parameters": {"type": "object", "properties": {"field": {"type": "string", "title": "Field"}}}}}]
        expected = json.loads(json.dumps(schemas))
        expected[0]["function"].pop("title")
        response = io.BytesIO(b'data: {"choices":[{"delta":{"content":"Done"}}]}\n\ndata: [DONE]\n\n')
        response.headers = {}
        with mock.patch.object(llm.urllib.request, "urlopen", return_value=response) as opened:
            llm.stream_chat(base_url="https://api.example.com/v1", api_key="k", model="test", messages=[],
                            tools=schemas, reasoning_effort="low", on_text=lambda text: None,
                            is_cancelled=lambda: False)
        sent = json.loads(opened.call_args.args[0].data)
        self.assertEqual(sent["tools"], expected)
        self.assertEqual(schemas[0]["function"]["title"], "Reading data", "the UI registry must stay intact")
        self.assertEqual(context.fixed_tokens("prompt", schemas), context.fixed_tokens("prompt", expected))

    def test_every_tool_has_a_short_activity_title_in_every_loop(self):
        from loopcut import cloud, orchestrator, video_attach, workers
        from loopcut.byok import catalog
        descriptors = catalog.descriptors(no_generation=False, private_mode=False, keys_set={})
        registries = [TOOL_SCHEMAS(), agent._orchestrator_schemas(), agent._workspace_schemas(), workers.schemas(),
                      video_attach.SCHEMAS, [orchestrator.WAIT_SCHEMA, cloud.JOB_SCHEMA],
                      [cloud.schema(d) for d in descriptors],
                      [cloud.offer_schema([{"name": "test", "title": "Example"}])]]
        for schemas in registries:
            for schema in schemas:
                function = schema["function"]
                with self.subTest(tool=function["name"]):
                    title = function.get("title", "").strip()
                    self.assertTrue(title, "Every callable tool needs an explicit activity title")
                    self.assertLessEqual(len(title), 70)
                    self.assertNotIn("\n", title)
                    self.assertNotIn("_", title)
                    self.assertEqual(agent._tool_title(function["name"], lambda: schemas), title)

    def test_every_offered_tool_is_dispatched_and_no_old_name_is_offered(self):
        schemas = {s["function"]["name"]: s["function"] for s in TOOL_SCHEMAS()}
        self.assertEqual(len(schemas), len(TOOL_SCHEMAS()), "a tool offered twice")
        dispatched = set().union(tools._DISPATCH, contact.DISPATCH, files.DISPATCH, job_tools.DISPATCH,
                                 inspection.DISPATCH, assets.DISPATCH, camera.DISPATCH, uv_tools.DISPATCH,
                                 texture_tools.DISPATCH)
        self.assertEqual(set(schemas), dispatched)
        self.assertFalse((set(tools.RENAMED) - {"search_assets", "import_asset"}) & set(schemas))
        for old, (current, implied, _) in tools.RENAMED.items():
            for key, value in implied.items():
                self.assertIn(value, schemas[current]["parameters"]["properties"][key]["enum"], old)

    def test_transfer_textures_is_the_single_loops_only(self):
        from loopcut import workers
        names = lambda schemas: {s["function"]["name"] for s in schemas}  # noqa: E731
        self.assertIn("transfer_textures", names(TOOL_SCHEMAS()))
        self.assertNotIn("transfer_textures", names(agent._orchestrator_schemas()))
        self.assertNotIn("transfer_textures", names(workers.schemas()))
        self.assertIn("uv_unwrap", names(agent._orchestrator_schemas()) & names(workers.schemas()))

    def test_the_fixed_cost_default_covers_what_is_sent(self):
        self.assertLessEqual(context.fixed_tokens(agent.SYSTEM_PROMPT, TOOL_SCHEMAS()), context.FIXED_TOKENS)

    def test_the_guidance_each_library_had_is_still_offered(self):
        search = json.dumps(assets.SCHEMAS[0]) + agent.SYSTEM_PROMPT
        for words in ("CC0", "CC-BY", "Full-plan key", "free with no account", "Online Essentials", "ambientCG",
                      "attribution line"):
            self.assertIn(words, search)
        imported = json.dumps(assets.SCHEMAS[1])
        for words in ("target_size", "attribution", "bounds", "apply_to", "2k final renders"):
            self.assertIn(words, imported)


class RoutingTest(unittest.TestCase):
    def assert_routes(self, name, module, function, arguments, expected):
        with mock.patch.object(module, function, autospec=True, return_value=FOUND) as target:
            result = execute(name, **arguments)
        self.assertEqual(result.text, "found", result.text)
        target.assert_called_once_with(**expected)

    def test_search_reaches_each_library_in_its_own_words(self):
        self.assert_routes("search_assets", polyhaven, "search_polyhaven",
                           {"source": "polyhaven", "query": "oak", "type": "material", "limit": 3},
                           {"query": "oak", "type": "textures", "limit": 3})
        self.assert_routes("search_assets", polyhaven, "search_polyhaven",
                           {"source": "polyhaven", "query": "sky", "type": "hdris"}, {"query": "sky", "type": "hdris"})
        self.assert_routes("search_assets", blenderkit, "search_blenderkit",
                           {"source": "blenderkit", "query": "sky", "type": "hdri"}, {"query": "sky", "type": "hdr"})
        self.assert_routes("search_assets", asset_libraries, "search_assets",
                           {"source": "library", "query": "city", "type": "WORLD"}, {"query": "city", "type": "WORLD"})
        self.assert_routes("search_assets", polypizza, "search_polypizza",
                           {"source": "polypizza", "query": "tree", "category": "Nature", "licence": "CC0",
                            "animated": True, "limit": 4},
                           {"query": "tree", "category": "Nature", "licence": "CC0", "animated": True, "limit": 4})

    def test_import_passes_the_id_under_each_library_s_own_name(self):
        self.assert_routes("import_asset", polyhaven, "import_polyhaven",
                           {"source": "polyhaven", "id": "oak_01", "resolution": "2k", "apply_to": ["Cube"]},
                           {"asset_id": "oak_01", "resolution": "2k", "apply_to": ["Cube"]})
        self.assert_routes("import_asset", blenderkit, "import_blenderkit",
                           {"source": "blenderkit", "id": "abc"}, {"asset_id": "abc"})
        self.assert_routes("import_asset", asset_libraries, "import_asset",
                           {"source": "library", "id": "L::f.blend::WORLD::City", "apply_to": []},
                           {"ref": "L::f.blend::WORLD::City", "apply_to": []})
        self.assert_routes("import_asset", polypizza, "import_polypizza",
                           {"source": "polypizza", "id": "abc", "target_size": 2.0}, {"model_id": "abc", "target_size": 2.0})

    def test_render_job_actions_reach_the_job_tools(self):
        cases = [("start", {"output_dir": "/out", "frames": [1, 2], "budget_seconds": 60, "samples": 8}),
                 ("status", {}), ("status", {"job_id": "a", "preview": True}),
                 ("cancel", {"job_id": "a"}), ("resume", {"job_id": "a"})]
        for action, arguments in cases:
            target = mock.create_autospec(job_tools.ACTIONS[action], return_value=FOUND)
            with mock.patch.dict(job_tools.ACTIONS, {action: target}):
                result = execute("render_job", action=action, **arguments)
            self.assertEqual(result.text, "found", result.text)
            target.assert_called_once_with(**arguments)

    def test_the_cost_guard_still_refuses_a_render_job_start(self):
        from loopcut import cost_guard
        # The functions the guard hooks are the ones the actions call; a resume renders the copy start checked.
        self.assertEqual(job_tools.ACTIONS, {"start": job_tools.start_render_job, "status": job_tools.render_job_status,
                                             "cancel": job_tools.cancel_render_job, "resume": job_tools.resume_render_job})
        refusal = "Render refused by the cost guard: ...\n- Subsurf on Seat renders at est. 16M faces; lower its render_levels"
        with mock.patch.object(job_tools, "create_spec", return_value=({}, Path("job.json"))), \
                mock.patch.object(cost_guard, "render_refusal", return_value=refusal), \
                mock.patch.object(job_tools, "save_snapshot") as save, mock.patch.object(job_tools.jobs, "launch") as launch:
            result = execute("render_job", action="start", output_dir="/out", frames=[1], budget_seconds=60)
        self.assertFalse(result.ok)
        self.assertEqual(result.text, refusal)
        save.assert_not_called()
        launch.assert_not_called()

    def test_mistakes_are_named_in_the_words_the_model_sees(self):
        cases = [(("search_assets", {"source": "", "query": "oak"}),
                  "search source must be one of blenderkit, polyhaven, library, polypizza, or omitted"),
                 (("search_assets", {"source": "polyhaven", "query": "oak"}), "source polyhaven needs type"),
                 (("search_assets", {"source": "polyhaven", "query": "oak", "type": "model", "category": "Nature"}),
                  "source polyhaven does not take category"),
                 (("import_asset", {"source": "library", "id": "L::f::WORLD::City", "target_size": 1}),
                  "source library does not take target_size"),
                 (("render_job", {}), "action must be one of start, status, cancel, resume"),
                 (("render_job", {"action": "start", "frames": [1]}), "render_job start needs budget_seconds"),
                 (("render_job", {"action": "status", "output_dir": "/out"}), "render_job status does not take output_dir")]
        for (name, arguments), expected in cases:
            result = execute(name, **arguments)
            self.assertFalse(result.ok, name)
            self.assertEqual(result.text, expected)

    def test_old_names_are_not_dispatched(self):
        for old in set(tools.RENAMED) - {"search_assets", "import_asset"}:
            result = execute(old, query="oak")
            self.assertFalse(result.ok)
            self.assertTrue(result.text.startswith(f"Unknown tool {old!r}"), result.text)


class ApprovalTest(unittest.TestCase):
    def test_each_call_keeps_the_approval_it_had(self):
        for source in assets.SOURCES:
            self.assertEqual(agent._approval("import_asset", json.dumps({"source": source, "id": "x"}), ()), "gate")
            self.assertEqual(agent._approval("search_assets", json.dumps({"source": source, "query": "x"}), ()), "")
        self.assertIn("import_asset", tools.CHANGES_SCENE, "an import takes a checkpoint")
        self.assertFalse({"search_assets", "render_job"} & tools.CHANGES_SCENE)
        for action, approval in (("start", "heavy"), ("resume", "heavy"), ("status", ""), ("cancel", "")):
            self.assertEqual(agent._approval("render_job", json.dumps({"action": action}), ()), approval, action)
        self.assertEqual(agent._approval("render_job", "not json", ()), "")

    def test_each_tool_is_offered_in_order_with_its_class(self):
        self.assertEqual([s["function"]["name"] for s in TOOL_SCHEMAS()], [name for name, _ in OFFERED])
        for name, kind in OFFERED:
            changes, read = CHANGES_SCENE(name, "{}"), SAME_WHILE_UNCHANGED(name, "{}")
            self.assertFalse(changes and read, name)
            self.assertEqual("change" if changes else "read" if read else "", kind, name)
            self.assertEqual(name in tools.NEEDS_APPROVAL, changes, name)

    def test_a_call_that_only_measures_is_a_read(self):
        report = json.dumps({"report_only": True, "objects": ["Table"]})
        watch = json.dumps({"pairs": [{"a": "Lamp", "relation": "rests_on", "b": "Table"}], "watch": True})
        for name, arguments in (("frame_camera", report), ("check_contact", watch)):
            self.assertFalse(CHANGES_SCENE(name, arguments), name)
            self.assertTrue(SAME_WHILE_UNCHANGED(name, arguments), name)
            self.assertEqual(agent._approval(name, arguments, ()), "", name)
        self.assertEqual(agent._approval("frame_camera", json.dumps({"objects": ["Table"]}), ()), "gate")

    def test_always_allow_renders_still_covers_a_render_job(self):
        session, cfg = {"auto_heavy": True, "auto_run": False}, types.SimpleNamespace(auto_run=False)
        self.assertFalse(agent._asks("heavy", "render_job", session, cfg))
        self.assertTrue(agent._asks("heavy", "render_job", {**session, "auto_heavy": False}, cfg))


class CodeTurnTest(unittest.TestCase):
    def test_loop_scopes_corrections_to_one_turn_without_changing_approval_or_results(self):
        from loopcut.ui import layout
        session = state.new_session()
        saved_turns = []
        for continues in (False, True):
            with self.subTest(continues=continues):
                session['items'].append(state.item_user('Build a chest'))
                session['messages'].append({'role': 'user', 'content': 'Build a chest'})
                session['busy'] = True
                turn = agent.Turn()
                turn.config = config.Config('test-key', 'https://example.invalid/v1', 'fake', 'low',
                                            False, False, 0, 24000)
                spec = agent.LoopSpec(role='worker', system=lambda s: '', schemas=lambda: [], save=lambda s: None,
                                      asks=True, checkpoints=False, changes_card=False)
                failed_output = 'SyntaxError: invalid syntax\n\nScene changes: none.'
                results = iter([tools.ToolResult(failed_output, ok=False), tools.ToolResult('OK')])
                calls = [llm.Completion(tool_calls=[llm.ToolCall('bad', 'run_python', '{"code": "invalid?"}')])]
                if continues:
                    calls.append(llm.Completion(tool_calls=[llm.ToolCall('later', 'run_python', '{"code": "pass"}')]))
                calls.append(llm.Completion())
                replies = iter(calls)

                def request(*args):
                    cards = [i for i in session['items'] if i.get('turn_id') == session['active_turn_id']]
                    if cards:
                        self.assertEqual(cards[0]['output'], failed_output)
                        index = session['items'].index(cards[0])
                        repairs = layout._code_repairs(session['items'], True, session['active_turn_id'])
                        self.assertEqual(repairs[index], 'correcting')
                    return next(replies)

                with mock.patch.object(agent, '_request', side_effect=request), \
                        mock.patch.object(agent, '_redraw'), \
                        mock.patch.object(agent, '_wait_for_approval', return_value=True) as approval:
                    agent.run_loop(session, turn, spec, run_tool=lambda *args: next(results))
                self.assertEqual(turn.outcome, 'completed')
                self.assertEqual(approval.call_count, 2 if continues else 1)
                cards = [i for i in session['items'] if i.get('kind') == 'tool'][-(2 if continues else 1):]
                self.assertEqual(cards[0]['status'], 'failed')
                self.assertEqual(cards[0]['output'], failed_output)
                self.assertTrue(all(c['turn_id'] == cards[0]['turn_id'] for c in cards))
                saved_turns.append(cards[0]['turn_id'])
                self.assertNotIn('active_turn_id', session)
                self.assertFalse(session['busy'])
                self.assertEqual(cards[-1]['turn_outcome'], 'completed')
        self.assertNotEqual(*saved_turns)
        failed = [n for n, i in enumerate(session['items']) if i.get('status') == 'failed']
        self.assertEqual(layout._code_repairs(session['items'], False), {failed[-1]: 'continued'})

    def test_completed_and_error_outcomes_keep_the_same_correction_status_after_save_and_restore(self):
        from loopcut.ui import layout
        for ends_with_error in (False, True):
            with self.subTest(ends_with_error=ends_with_error), tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.dict(os.environ, {'LOOPCUT_DATA_DIR': tmp}):
                session = state.new_session()
                session['items'].append(state.item_user('Fix the lid'))
                session['messages'].append({'role': 'user', 'content': 'Fix the lid'})
                session['busy'] = True
                turn = agent.Turn()
                turn.config = config.Config('test-key', 'https://example.invalid/v1', 'fake', 'low',
                                            False, False, 0, 24000)
                spec = agent.LoopSpec(role='worker', system=lambda s: '', schemas=lambda: [], save=conversations.save,
                                      asks=False, checkpoints=False, changes_card=False)
                replies = [llm.Completion(tool_calls=[llm.ToolCall('bad', 'run_python', '{"code": "invalid?"}')]),
                           llm.Completion(tool_calls=[llm.ToolCall('read', 'run_python',
                                                                   '{"code": "print(scene.frame_current)"}')]),
                           llm.LLMError('Connection dropped') if ends_with_error else llm.Completion()]
                results = iter([tools.ToolResult('SyntaxError: invalid syntax\n\nScene changes: none.', ok=False),
                                tools.ToolResult('1')])
                with mock.patch.object(agent, '_request', side_effect=replies), mock.patch.object(agent, '_redraw'):
                    agent.run_loop(session, turn, spec, run_tool=lambda *args: next(results))
                self.assertEqual(turn.outcome, 'error' if ends_with_error else 'completed')
                expected = {} if ends_with_error else {1: 'continued'}
                self.assertEqual(layout._code_repairs(session['items'], False), expected)
                loaded = conversations.load(session['id'])
                self.assertEqual(loaded['items'], session['items'])
                self.assertEqual(layout._code_repairs(loaded['items'], False), expected)
                if ends_with_error:
                    error = next(i for i in loaded['items'] if i['kind'] == 'error')
                    self.assertEqual(error, state.item_error('Connection dropped'))
                loaded['items'] = loaded['items'][:2]  # Restore before the unrelated successful call.
                self.assertEqual(layout._code_repairs(loaded['items'], False), {})


class OldCallsTest(unittest.TestCase):
    def describe(self, name, arguments):
        return agent._describe(llm.ToolCall("c1", name, json.dumps(arguments)))

    def test_old_and_new_calls_get_the_same_label(self):
        self.assertEqual(self.describe("import_asset", {"source": "library", "id": "L::f.blend::WORLD::City"})[0],
                         "Import asset City")
        pairs = [(("search_polyhaven", {"query": "oak", "type": "models"}),
                  ("search_assets", {"source": "polyhaven", "query": "oak", "type": "model"}), "Search Poly Haven models: oak"),
                 (("import_blenderkit", {"asset_id": "abc"}),
                  ("import_asset", {"source": "blenderkit", "id": "abc"}), "Import BlenderKit asset abc (1k)"),
                 (("search_polypizza", {"query": "tree"}),
                  ("search_assets", {"source": "polypizza", "query": "tree"}), "Search Poly Pizza: tree"),
                 (("cancel_render_job", {"job_id": "a"}),
                  ("render_job", {"action": "cancel", "job_id": "a"}), "Cancel render job a")]
        for old, new, label in pairs:
            self.assertEqual(self.describe(*new)[0], label)
            self.assertEqual(self.describe(*old)[0], label)
        # Omitted source searches the user's libraries and the managed index together.
        self.assertEqual(self.describe("search_assets", {"query": "oak"}), ("Search assets: oak", ""))

    def test_malformed_arguments_are_refused_not_raised(self):
        for name, arguments in (("render_job", {"action": ["start"]}), ("search_assets", {"source": ["polyhaven"]}),
                                ("search_assets", {"source": "polyhaven", "query": "x", "type": ["model"]})):
            self.assertEqual(agent._approval(name, json.dumps(arguments), ()), "")
            self.assertIsInstance(self.describe(name, arguments), tuple)
            self.assertFalse(execute(name, **arguments).ok)

    def test_a_render_approval_card_shows_the_job_settings(self):
        settings = {"output_dir": "/out", "frames": [1, 2], "budget_seconds": 60}
        summary, code = self.describe("render_job", {"action": "start", **settings})
        self.assertEqual(summary, "Background PNG render · 2 frames · 60s budget")
        self.assertEqual(json.loads(code), settings)
        self.assertEqual(self.describe("start_render_job", settings), (summary, code))

    def test_saved_calls_are_respelled_for_the_model(self):
        messages = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": None, "tool_calls": [
                call("c1", "search_polyhaven", {"query": "oak", "type": "models"}),
                call("c2", "import_polypizza", {"model_id": "abc", "target_size": 2}),
                call("c3", "search_assets", {"query": "kit"}),
                call("c4", "import_asset", {"ref": "L::f::WORLD::City"}),
                call("c5", "start_render_job", {"output_dir": "/out", "frames": [1], "budget_seconds": 9}),
                call("c6", "search_assets", {"source": "blenderkit", "query": "sofa", "type": "model"}),
                call("c7", "run_python", {"code": "x = 1"}),
                call("c8", "import_polyhaven", "{not json"),
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": "1. oak_01"},
        ]
        untouched = [json.dumps(messages[1]["tool_calls"][i]) for i in (5, 6, 7)]
        conversations.rename_merged_tools(messages)
        calls = [(c["function"]["name"], c["function"]["arguments"]) for c in messages[1]["tool_calls"]]
        self.assertEqual([(name, json.loads(raw)) for name, raw in calls[:5]], [
            ("search_assets", {"source": "polyhaven", "query": "oak", "type": "models"}),
            ("import_asset", {"source": "polypizza", "id": "abc", "target_size": 2}),
            ("search_assets", {"source": "library", "query": "kit"}),
            ("import_asset", {"source": "library", "id": "L::f::WORLD::City"}),
            ("render_job", {"action": "start", "output_dir": "/out", "frames": [1], "budget_seconds": 9})])
        self.assertEqual([json.dumps(messages[1]["tool_calls"][i]) for i in (5, 6, 7)], untouched)
        self.assertEqual([c["id"] for c in messages[1]["tool_calls"]], [f"c{i}" for i in range(1, 9)])


if __name__ == "__main__":
    unittest.main()
