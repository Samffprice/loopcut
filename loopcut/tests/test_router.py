"""The router (router.py): its rules, the model's JSON, the note the orchestrator gets, the one nudge,
what is recorded, and its configuration. No Blender and no network: the model is a function.

    python3 -m unittest discover -s loopcut/tests
"""

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

from loopcut import agent, config, context, conversations, llm, router, state, worker_ipc  # noqa: E402

# Stand-ins for credentials, so a test can tell whose reached where. Not secrets.
MAIN, WORKER, ROUTER = "fake-main", "fake-worker", "fake-router"
SCENE = ("<scene_context>\nfile: corner.blend | mode: OBJECT | frame: 1 | objects: 2 | active: none\n"
         "Blender 5.2.2 LTS | render engines: BLENDER_EEVEE, CYCLES | current: BLENDER_EEVEE | camera: Camera\n"
         "output: 1920x1080 at 100% | PNG | frames: 1-250 | fps: 24\n"
         "media type: IMAGE | video encoding available: True | editors: VIEW_3D\n"
         "selected: nothing\nother objects: Light (LIGHT), Camera (CAMERA)\n</scene_context>")
CORNER = "Make a reading corner: a round rug, an armchair, a side table and a floor lamp."
LEVEL_3 = json.dumps(dict(level=3, deliverables=[dict(name='Armchair', quote='an armchair', role='modeling'),
    dict(name='SideTable', quote='a side table', role='modeling'),
    dict(name='FloorLamp', quote='a floor lamp', role='modeling')], why='three pieces of furniture'))
CFG = config.Config(ROUTER, "https://router.example/v1", "glm-5.3-flash", "low", True, False, 25, 40_000)


def setUpModule():
    from prompt_index_fixture import index_patch
    patcher = index_patch()
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    patcher = mock.patch.dict(os.environ, {"LOOPCUT_SERVICE_URLS": CFG.base_url})
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


class FakeModel:
    """stream_chat's stand-in: replies in order, records each request."""

    def __init__(self, *replies, usage=None):
        self.replies, self.requests = list(replies), []
        self.usage = usage if usage is not None else {"input": 900, "cached": 0, "output": 60}

    def __call__(self, **request):
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return llm.Completion(text=reply, usage=dict(self.usage))


def session_with(*turns) -> dict:
    """A session whose messages hold these user texts as turns, each answered by 'Done.'; the last
    is the turn now starting."""
    session = state.new_session()
    for number, text in enumerate(turns, 1):
        if number > 1:
            session["messages"].append({"role": "assistant", "content": "Done."})
        session["messages"].append({"role": "user", "content": f"{SCENE}\n\n{text}"})
        session["items"].append(state.item_user(text))
    session["turn_number"] = len(turns)
    return session


def a_turn() -> agent.Turn:
    turn = agent.Turn()
    turn.spec = agent.ORCH_SPEC
    return turn


class ParseTest(unittest.TestCase):
    def test_the_json_asked_for(self):
        decision = router.parse(LEVEL_3)
        self.assertEqual(decision.level, 3)
        self.assertEqual(decision.deliverables, [("Armchair", "an armchair", "modeling"),
                                                 ("SideTable", "a side table", "modeling"),
                                                 ("FloorLamp", "a floor lamp", "modeling")])
        self.assertEqual(decision.why, "three pieces of furniture")

    def test_a_role_is_kept_when_valid_else_modeling(self):
        reply = json.dumps({"level": 3, "deliverables": [
            {"name": "Sunset", "quote": "sunset lighting", "role": "lighting"},
            {"name": "Shot", "quote": "a camera move", "role": "camera"},
            {"name": "Weird", "quote": "a weird one", "role": "not_a_role"},
            {"name": "Bare", "quote": "a bare one"}]})
        roles = [role for _, _, role in router.parse(reply).deliverables]
        self.assertEqual(roles, ["lighting", "camera", "modeling", "modeling"])

    def test_fences_and_words_around_it_do_not_matter(self):
        for text in (f"```json\n{LEVEL_3}\n```", f"Here you go: {LEVEL_3} Hope that helps.",
                     '{"note": "no level here"} ' + LEVEL_3):
            self.assertEqual(router.parse(text).level, 3, text)
        self.assertEqual(router.parse('{"level": "2", "deliverables": ["Table"]}').deliverables,
                         [("Table", "", "modeling")])

    def test_anything_else_is_no_decision(self):
        for text in ("", "level 3", '{"level": 3, "deliverables": [', '{"level": 7}', '{"level": -1}',
                     '{"level": true}', '{"level": "three"}', '{"deliverables": []}', "[3]", None):
            self.assertIsNone(router.parse(text), text)

    def test_names_are_cleaned_capped_and_unique_and_small_levels_have_none(self):
        many = [{"name": f"Prop {i}!", "quote": "  a\nprop  "} for i in range(12)]
        decision = router.parse(json.dumps({"level": 3, "deliverables": [
            {"name": "Arm-chair", "quote": "x" * 200}, {"name": "armchair"}, {"name": ""}, 42, *many]}))
        names = [name for name, *_ in decision.deliverables]
        self.assertEqual(names[:2], ["Armchair", "42"])
        self.assertEqual(len(names), router.MAX_DELIVERABLES)
        self.assertEqual(len(decision.deliverables[0][1]), router.MAX_QUOTE)
        self.assertEqual(decision.deliverables[2], ("Prop0", "a prop", "modeling"))
        self.assertEqual(router.parse('{"level": 1, "deliverables": ["Chair"]}').deliverables, [])


class NoteTest(unittest.TestCase):
    def test_level_3_names_the_deliverables_with_the_users_words(self):
        text = router.note(router.parse(LEVEL_3))
        self.assertTrue(text.startswith(
            '<routing>level 3 — deliverables: Armchair ("an armchair"), SideTable ("a side table"), '
            'FloorLamp ("a floor lamp"). Delegate these, one worker each, with a style (look, parts, detail, '
            'finish), planned bounds and a dry_run first; do shared setup (palette, supports, render and output settings) yourself. Skeleton: workers: ['))
        self.assertTrue(text.endswith("]</routing>"))
        self.assertIn('{"id": "armchair", "deliverable": "Armchair", "role": "modeling", "prefix": "Armchair", '
                      '"quote": "an armchair"}', text)

    def test_the_skeleton_gives_ids_roles_prefixes_and_quotes_to_fill_in(self):
        decision = router.Decision(3, [("Armchair", "an armchair", "modeling"), ("Sunset", "warm lighting", "lighting")])
        self.assertEqual(router.skeleton(decision), (
            'workers: [{"id": "armchair", "deliverable": "Armchair", "role": "modeling", "prefix": "Armchair", '
            '"quote": "an armchair"}, {"id": "sunset", "deliverable": "Sunset", "role": "lighting", '
            '"prefix": "Sunset", "quote": "warm lighting"}]'))
        self.assertEqual(router.skeleton(router.Decision(3, [("Arm Chair!", "x")])),  # No role: defaults modeling.
                         'workers: [{"id": "armchair", "deliverable": "Arm Chair!", "role": "modeling", '
                         '"prefix": "ArmChair", "quote": "x"}]')

    def test_a_deliverable_an_earlier_worker_built_goes_back_to_it(self):
        threads = {"chair2": {"deliverable": "Armchair", "prefix": "Chair", "wave": 2},
                   "chair": {"deliverable": "Armchair", "prefix": "Arm", "wave": 1}}
        decision = router.Decision(3, [("Armchair", "make it icy", "materials"), ("Rug", "and the rug", "modeling")])
        self.assertEqual(router.skeleton(decision, threads), (
            'workers: [{"id": "chair2", "deliverable": "Armchair", "role": "materials", "prefix": "Chair", '
            '"continues": "chair2", "quote": "make it icy"}, {"id": "rug", "deliverable": "Rug", "role": "modeling", '
            '"prefix": "Rug", "quote": "and the rug"}]'), "the newest worker on it, its id and prefix")
        self.assertIn('"continues": "chair2"', router.note(decision, threads))



    def test_the_other_levels(self):
        self.assertEqual(router.note(router.Decision(0)), "<routing>level 0 — a question: answer it; change nothing.</routing>")
        self.assertEqual(router.note(router.Decision(1)), "<routing>level 1 — a small edit: do it yourself.</routing>")
        self.assertEqual(router.note(router.Decision(2, [("Table", "a simple table")])), (
            '<routing>level 2 — one deliverable: Table ("a simple table"). Build it yourself if it takes about 8 '
            'steps, else delegate it to one worker with a style (look, parts, detail, finish). Skeleton: workers: '
            '[{"id": "table", "deliverable": "Table", '
            '"role": "modeling", "prefix": "Table", "quote": "a simple table"}]</routing>'))
        self.assertIn("level 3 — deliverables: several.", router.note(router.Decision(3)))
        self.assertNotIn("Skeleton:", router.note(router.Decision(3)), "no deliverables, no skeleton to fill in")
        parked = router.note(router.Decision(3, [("Armchair", "")], source="parked"))
        self.assertTrue(parked.startswith("<routing>level 3 — the work your question was about: deliverables: Armchair."))
        self.assertIn('"id": "armchair"', parked)


class RequestTest(unittest.TestCase):
    def test_what_the_router_is_told(self):
        session = session_with("Add a red cube.", CORNER)
        session["turn_levels"] = [{"turn": 1, "first": 1, "final": 1}]
        model = FakeModel(LEVEL_3)
        router.route(session, session["messages"][-1], CFG, lambda: False, model)
        request = model.requests[0]
        self.assertEqual((request["model"], request["reasoning_effort"], request["tools"], request["timeout"]),
                         ("glm-5.3-flash", "low", [], router.TIMEOUT))
        self.assertEqual(request["headers"], {"x-loopcut-conversation": session["id"], "x-loopcut-role": "router"},
                         "the gateway correlates this stream's usage row (llm.correlation_headers)")
        user, = request["messages"]
        self.assertEqual(request["prompt_id"], "router")
        text = user["content"]
        self.assertIn("other objects: Light (LIGHT), Camera (CAMERA)", text)
        self.assertNotIn("render engines", text, "the build and editors say nothing about the work")
        self.assertNotIn("media type", text)
        self.assertIn('Previous turn: the user asked "Add a red cube."; Loopcut worked at level 1; its reply ended "Done."', text)
        self.assertTrue(text.endswith(f"Request: {CORNER}"))
        self.assertNotIn("attached an image", text)

    def test_a_first_turn_has_no_previous_and_a_long_scene_is_cut(self):
        session = session_with(CORNER)
        session["messages"][-1]["content"] = SCENE.replace("selected: nothing", "x" * 3000) + "\n\n" + CORNER
        model = FakeModel(LEVEL_3)
        router.route(session, session["messages"][-1], CFG, lambda: False, model)
        text = model.requests[0]["messages"][0]["content"]
        self.assertIn("Previous turn: none", text)
        self.assertLess(len(text), router.MAX_SCENE + len(CORNER) + 200)


class RulesTest(unittest.TestCase):
    def attached(self, reply) -> tuple[dict, "router.Decision", FakeModel]:
        session = session_with("make the vase this colour")
        message = session["messages"][-1]
        message.update({context.ATTACHED: True, "content": [{"type": "text", "text": message["content"]},
                                                             {"type": "image_url", "image_url": {"url": "ref"}}]})
        model = FakeModel(reply)
        return session, router.route(session, message, CFG, lambda: False, model), model

    def test_an_attached_image_means_level_2_or_more(self):
        _, decision, model = self.attached('{"level": 1, "why": "a colour change"}')
        self.assertEqual((decision.level, decision.source), (2, "rules"))
        self.assertIn("The user attached an image with this request.", model.requests[0]["messages"][0]["content"])
        _, decision, _ = self.attached('{"level": 3, "deliverables": ["Walls", "Sofa"]}')
        self.assertEqual((decision.level, decision.source), (3, "model"), "the model may go higher")
        _, decision, _ = self.attached("not json")
        self.assertEqual((decision.level, decision.source), (2, "rules"), "the rule stands without the model")

    def asked_first(self, reply: str, final: int) -> tuple["router.Decision | None", FakeModel]:
        session = session_with(CORNER, "oak, please")
        session["messages"][-2]["content"] = reply
        session["turn_levels"] = [{"turn": 1, "first": final, "final": final, "routed": 3}]
        session["routing"] = {**router.parse(LEVEL_3).record(), "turn": 1}
        model = FakeModel('{"level": 1}')
        return router.route(session, session["messages"][-1], CFG, lambda: False, model), model

    def test_the_answer_to_a_question_goes_back_to_the_parked_work_without_a_call(self):
        decision, model = self.asked_first("Should the armchair be oak or walnut?", final=0)
        self.assertEqual((decision.level, decision.source, len(decision.deliverables)), (3, "parked", 3))
        self.assertEqual(model.requests, [])

    def test_after_work_was_done_or_without_a_question_the_model_decides(self):
        for reply, final in (("Built all three. Want me to render it?", 3), ("Which wood do you like? Oak.", 1),
                             ("I need the wood.", 0)):
            decision, model = self.asked_first(reply, final)
            self.assertEqual((decision.level, decision.source, len(model.requests)), (1, "model", 1), reply)
        _, model = self.asked_first("Built all three. Want me to render it?", 3)
        self.assertIn('its reply ended "Built all three. Want me to render it?"', model.requests[0]["messages"][0]["content"])

    def test_a_question_before_the_verified_block_still_counts(self):
        self.assertTrue(router.asked("Which one do you mean?**"))
        reply = router._last_reply([{"role": "assistant", "content": "Oak or walnut?\n\nVerified by Loopcut on this "
                                                                   "scene: 2 of 2 checks.\n- not met: x"}])
        self.assertEqual(reply, "Oak or walnut?")
        self.assertTrue(router.asked(reply))
        self.assertFalse(router.asked("Is it done? Yes, all three are built."))


class StartTurnTest(unittest.TestCase):
    def test_the_note_is_added_the_message_marked_and_the_usage_counted(self):
        session, turn = session_with(CORNER), a_turn()
        router.start_turn(session, turn, CFG, lambda: False,
                          FakeModel(LEVEL_3, usage={"input": 900, "cached": 100, "output": 60}))
        message = session["messages"][-1]
        self.assertTrue(message["content"].endswith(f"{CORNER}\n\n{router.note(turn.routed)}"))
        self.assertEqual(message[router.ROUTED], 3)
        self.assertEqual(session["routing"]["deliverables"][0], ["Armchair", "an armchair", "modeling"])
        self.assertEqual(session["usage_by_role"], {"router": {"input": 900, "cached": 100, "output": 60, "requests": 1}})
        self.assertEqual((session["usage"]["input"], session["usage"]["output"]), (900, 60), "the sum the UI shows")
        self.assertNotIn(router.ROUTED, conversations.wire_messages(session["id"], [message])[0],
                         "the marker never reaches the API")

    def test_an_attached_message_gets_the_note_in_its_text_part(self):
        session, turn = session_with(CORNER), a_turn()
        message = session["messages"][-1]
        message.update({context.ATTACHED: True, "content": [{"type": "text", "text": message["content"]},
                                                             {"type": "image_url", "image_url": {"url": "r"}}]})
        router.start_turn(session, turn, CFG, lambda: False, FakeModel(LEVEL_3))
        self.assertTrue(message["content"][0]["text"].endswith("]</routing>"))
        self.assertIn("yourself. Skeleton:", message["content"][0]["text"])
        self.assertEqual(message["content"][1]["type"], "image_url")

    def test_a_bad_reply_or_a_failed_call_is_no_note_and_the_turn_goes_on(self):
        for reply in ('{"level": 3, "deliv', "Level three.", llm.LLMError("HTTP 500"), TimeoutError("slow"),
                      ValueError("odd")):
            session, turn = session_with(CORNER), a_turn()
            router.start_turn(session, turn, CFG, lambda: False, FakeModel(reply))
            self.assertEqual(session["messages"][-1]["content"], f"{SCENE}\n\n{CORNER}", reply)
            self.assertEqual((turn.routed, turn.route_tried, session["messages"][-1][router.ROUTED]), (None, True, -1))
            self.assertIsNone(session["routing"])

    def test_hosted_allowance_refusal_ends_the_turn_without_trying_the_orchestrator(self):
        for reason in state.HOSTED_LIMITS:
            with self.subTest(reason=reason):
                session, turn = session_with(CORNER), a_turn()
                model = FakeModel(llm.LLMError("Used up.", 402, reason))
                with self.assertRaises(llm.LLMError) as error:
                    router.start_turn(session, turn, CFG, lambda: False, model)
                self.assertEqual(error.exception.code, reason)
                self.assertEqual(len(model.requests), 1)
                self.assertNotIn(router.ROUTED, session["messages"][-1])

    def test_a_stop_during_the_call_stops_the_turn(self):
        session, turn = session_with(CORNER), a_turn()
        with self.assertRaises(llm.Cancelled):
            router.start_turn(session, turn, CFG, lambda: True, FakeModel(llm.Cancelled()))

    def test_off_means_no_call_and_a_resumed_turn_is_not_routed_again(self):
        session, turn = session_with(CORNER), a_turn()
        router.start_turn(session, turn, None, lambda: False, FakeModel())
        self.assertEqual((turn.route_tried, router.ROUTED in session["messages"][-1]), (False, False))
        router.start_turn(session, turn, CFG, lambda: False, FakeModel(LEVEL_3))
        session["messages"].append({"role": "assistant", "content": "", "tool_calls": [{"id": "c"}]})
        session["messages"].append({"role": "tool", "tool_call_id": "c", "content": "ok"})
        resumed, model = a_turn(), FakeModel()
        router.start_turn(session, resumed, CFG, lambda: False, model)
        self.assertEqual((model.requests, resumed.routed.level, resumed.route_tried), ([], 3, True))
        self.assertEqual(session["messages"][-3]["content"].count("<routing>"), 1)


class NudgeTest(unittest.TestCase):
    def test_once_for_a_level_3_turn_that_delegated_nothing(self):
        turn = a_turn()
        turn.routed = router.parse(LEVEL_3)
        completion = llm.Completion(text="Built it all myself.")
        self.assertEqual(router.nudge(turn, completion), (
            "[Loopcut] Routing planned workers for Armchair, SideTable, FloorLamp; delegate them, or say in one "
            "line why you did it yourself."))
        self.assertIsNone(router.nudge(turn, completion), "then the reply is accepted")
        self.assertTrue(turn.nudged)

    def test_not_below_level_3_after_a_delegate_or_at_the_limit(self):
        for level, delegations, outcome in ((2, [], "running"), (3, [["Armchair"]], "running"),
                                            (3, [], "step_limit"), (3, [], "budget"), (3, [], "no_progress")):
            turn = a_turn()
            turn.routed, turn.delegations, turn.outcome = router.Decision(level), delegations, outcome
            self.assertIsNone(router.nudge(turn, llm.Completion(text="Done.")), (level, delegations, outcome))
        turn = a_turn()
        turn.routed = router.Decision(3)
        self.assertIsNone(router.nudge(turn, None), "the wrap-up at the limit has no completion to follow")
        self.assertIsNone(router.nudge(a_turn(), llm.Completion(text="Done.")), "no routing, no nudge")


class RecordTest(unittest.TestCase):
    def test_routed_and_nudged_join_the_turns_levels_and_a_resume_keeps_them(self):
        session = session_with(CORNER)
        turn = a_turn()
        turn.route_tried, turn.routed, turn.nudged = True, router.Decision(3), True
        turn.level_first, turn.level_max = 1, 3
        agent._record_level(session, turn)
        self.assertEqual(session["turn_levels"], [{"turn": 1, "first": 1, "final": 3, "routed": 3, "nudged": True}])
        resumed = a_turn()
        resumed.level_max = 1
        agent._record_level(session, resumed)
        self.assertEqual(session["turn_levels"], [{"turn": 1, "first": 1, "final": 3, "routed": 3, "nudged": True}])
        failed = a_turn()
        failed.route_tried = True
        session["turn_number"] = 2
        agent._record_level(session, failed)
        self.assertEqual(session["turn_levels"][-1], {"turn": 2, "first": 0, "final": 0, "routed": None})
        session["turn_number"] = 3
        agent._record_level(session, a_turn())
        self.assertEqual(session["turn_levels"][-1], {"turn": 3, "first": 0, "final": 0}, "router off: no key")


class ConfigTest(unittest.TestCase):
    BASE = {"LOOPCUT_API_KEY": MAIN, "LOOPCUT_BASE_URL": config.SERVICE_BASE_URL, "LOOPCUT_MODEL": "pro",
            "LOOPCUT_WORKER_MODEL": "glm-5.3-flash", "LOOPCUT_WORKER_API_KEY": WORKER,
            "LOOPCUT_REASONING_EFFORT": "high"}

    def roles(self, **env) -> config.Roles:
        with tempfile.NamedTemporaryFile("w", suffix=".env") as empty:
            clean = {k: v for k, v in os.environ.items() if not k.startswith("LOOPCUT_")}
            with mock.patch.dict(os.environ, {**clean, "LOOPCUT_ENV_FILE": empty.name, **self.BASE, **env}, clear=True):
                return config.load_roles()

    def test_on_by_default_with_the_workers_provider_at_low_effort(self):
        cfg = self.roles().router
        self.assertEqual((cfg.base_url, cfg.model, cfg.api_key, cfg.reasoning_effort),
                         (config.SERVICE_BASE_URL, "glm-5.3-flash", WORKER, "low"))

    def test_its_own_settings_and_off(self):
        cfg = self.roles(LOOPCUT_ROUTER_BASE_URL="https://r.example/v1", LOOPCUT_ROUTER_MODEL="tiny",
                         LOOPCUT_SERVICE_URLS="https://r.example/v1",
                         LOOPCUT_ROUTER_API_KEY=ROUTER, LOOPCUT_ROUTER_REASONING_EFFORT="minimal").router
        self.assertEqual((cfg.base_url, cfg.model, cfg.api_key, cfg.reasoning_effort),
                         ("https://r.example/v1", "tiny", ROUTER, "minimal"))
        self.assertIsNone(self.roles(LOOPCUT_ROUTER="off").router)
        with self.assertRaises(config.ConfigError):
            self.roles(LOOPCUT_ROUTER="sometimes")

    def test_on_the_service_it_runs_fast_like_the_workers(self):
        roles = self.roles(LOOPCUT_BASE_URL=config.SERVICE_BASE_URL, LOOPCUT_WORKER_MODEL="", LOOPCUT_WORKER_API_KEY="")
        self.assertEqual((roles.router.base_url, roles.router.model), (config.SERVICE_BASE_URL, "fast"))

    def test_the_routers_key_never_reaches_a_child(self):
        env = worker_ipc.child_env("/job", "/job/w1", {"PATH": "/bin", "HOME": "/h", "LOOPCUT_ROUTER_API_KEY": ROUTER,
                                                       "LOOPCUT_ROUTER_MODEL": "glm", "LOOPCUT_ROUTER": "on"})
        self.assertNotIn(ROUTER, env.values())
        self.assertFalse([name for name in env if name.startswith("LOOPCUT_ROUTER")])

    def test_the_router_defaults_to_a_much_lower_output_cap_than_the_workers(self):
        roles = self.roles()
        self.assertEqual(roles.worker.max_output_tokens, 32_000)
        self.assertEqual(roles.router.max_output_tokens, 4_000)

    def test_the_routers_output_cap_and_call_limit_can_be_set_directly(self):
        cfg = self.roles(LOOPCUT_ROUTER_MAX_OUTPUT_TOKENS="9000", LOOPCUT_ROUTER_CALL_MAX_SECONDS="45").router
        self.assertEqual((cfg.max_output_tokens, cfg.call_max_seconds), (9000, 45.0))

    def test_out_of_range_output_cap_or_call_limit_is_refused_by_name(self):
        for key, value in (("LOOPCUT_MAX_OUTPUT_TOKENS", "0"), ("LOOPCUT_ROUTER_MAX_OUTPUT_TOKENS", "0"),
                           ("LOOPCUT_CALL_MAX_SECONDS", "7201"), ("LOOPCUT_ROUTER_CALL_MAX_SECONDS", "not-a-number")):
            with self.assertRaises(config.ConfigError) as caught:
                self.roles(**{key: value})
            self.assertIn(key, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
