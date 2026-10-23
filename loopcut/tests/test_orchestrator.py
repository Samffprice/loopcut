"""The orchestrator's side of M4: roles, wave validation and the layout dry-run, the prompt's fixed
cost, level inference and the finish gate. No Blender: the live scene is a dict.

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

from loopcut import acceptance, agent, config, context, conversations, orchestrator, state, workers  # noqa: E402

from prompt_index_fixture import index_patch


def setUpModule():
    patcher = index_patch()
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


TOOL_SCHEMAS = agent._tool_schemas  # test_agent_loop stubs it; keep the real one.
USER = ("Put an armchair and a side table on the rug, with a lamp on the side table. Keep the bottle and "
        "the box unchanged. The camera should show the whole set.")
LIVE = {
    "objects": {"Rug": {"type": "MESH", "box": [[-2.0, -1.0, 0.0], [2.0, 2.0, 0.015]]},
                "Bottle": {"type": "MESH", "box": None}, "Box": {"type": "MESH", "box": None},
                "Floor": {"type": "MESH", "box": None}, "Camera": {"type": "CAMERA", "box": None}},
    "materials": ["Pal_Wood", "Pal_Brass"],
    "names": ["Rug", "Bottle", "Box", "Floor", "Camera", "Pal_Wood", "Pal_Brass", "Lamp_Old"],
}


def reference_wave() -> dict:
    """shared_support, as a good orchestrator would brief it."""
    return {
        "shared": {"keep": {"names": ["Bottle", "Box"], "quote": "Keep the bottle and the box unchanged"},
                   "palette": ["Pal_Wood"], "supports": [{"name": "Rug", "top_z": 0.015}]},
        "workers": [
            {"id": "armchair", "deliverable": "Armchair", "role": "modeling", "prefix": "Armchair",
             "requirements": [{"quote": "an armchair", "do": "armchair, seat about 0.45 m"}],
             "defaults": ["seat height 0.45 m", "fabric in Pal_Wood tones"],
             "bounds": [[-0.9, 0.2, 0.015], [-0.1, 1.0, 0.95]], "rests_on": "Rug",
             "checks": [{"kind": "contact", "a": "Armchair", "relation": "rests_on", "b": "Rug",
                         "quote": "an armchair and a side table on the rug"},
                        {"kind": "in_frame", "camera": "Camera", "names": ["Armchair"],
                         "quote": "the camera should show the whole set"}]},
            {"id": "table", "deliverable": "SideTable", "role": "modeling", "prefix": "SideTable",
             "requirements": [{"quote": "a side table", "do": "side table about 0.55 m tall"}],
             "bounds": [[0.2, 0.2, 0.015], [0.7, 0.7, 0.57]], "rests_on": "Rug",
             "checks": [{"kind": "contact", "a": "SideTable", "relation": "rests_on", "b": "Rug",
                         "quote": "a side table on the rug"}]}]}


def fake_camera(camera, points):
    """A camera at y = -5 looking along +y: the frame spans x -1..1 and z 0..1.5 at any depth."""
    return [((x + 1) / 2, z / 1.5, y + 5) for x, y, z in points]


def refusals(wave: dict, words: str = USER, live: dict = LIVE) -> list[str]:
    return orchestrator.validate_wave(orchestrator.normalise_wave(wave), words, live)


class RolesTest(unittest.TestCase):
    BASE = {"LOOPCUT_API_KEY": "k-main", "LOOPCUT_BASE_URL": "https://api.example.com/v1", "LOOPCUT_MODEL": "glm-5.3-flash"}

    def setUp(self):
        self.empty = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
        self.empty.close()
        self.addCleanup(os.unlink, self.empty.name)

    def roles(self, **env) -> config.Roles:
        clean = {k: v for k, v in os.environ.items() if not k.startswith("LOOPCUT_")}
        with mock.patch.dict(os.environ, {**clean, "LOOPCUT_ENV_FILE": self.empty.name, **self.BASE, **env}, clear=True):
            return config.load_roles()

    def test_defaults_orchestrate_with_the_main_values_for_both_roles(self):
        roles = self.roles(LOOPCUT_BASE_URL=config.SERVICE_BASE_URL, LOOPCUT_WORKER_MODEL="glm-5.3-flash")
        self.assertTrue(roles.orchestrate)
        self.assertEqual((roles.max_workers, roles.job_max_usd, roles.worker_max_seconds), (3, 0.40, 1800))
        for cfg in (roles.orchestrator, roles.worker):
            self.assertEqual((cfg.base_url, cfg.model, cfg.api_key), (config.SERVICE_BASE_URL, "glm-5.3-flash", "k-main"))
            self.assertEqual((cfg.max_output_tokens, cfg.call_max_seconds), (32_000, 0.0), "no wall-clock limit unless set")

    def test_a_worker_runs_seconds_are_its_own_and_the_old_wave_name_still_sets_them(self):
        self.assertEqual(self.roles(LOOPCUT_WORKER_MAX_SECONDS="900").worker_max_seconds, 900)
        self.assertEqual(self.roles(LOOPCUT_WAVE_MAX_SECONDS="600").worker_max_seconds, 600, "the old name")
        self.assertEqual(self.roles(LOOPCUT_WAVE_MAX_SECONDS="600", LOOPCUT_WORKER_MAX_SECONDS="900").worker_max_seconds,
                         900, "the new name wins")

    def test_turn_step_limit_is_opt_in_for_every_provider(self):
        for url in (self.BASE["LOOPCUT_BASE_URL"], config.SERVICE_BASE_URL, config.CHATGPT_PLAN_BASE_URL):
            for override, expected in ((None, 0), ("0", 0), ("5", 5), ("10000", 10000)):
                with self.subTest(url=url, override=override), mock.patch.object(config, "_preference_values", return_value={}):
                    env = {"LOOPCUT_BASE_URL": url}
                    if override is not None:
                        env["LOOPCUT_MAX_STEPS"] = override
                    roles = self.roles(**env)
                    self.assertEqual(roles.orchestrator.max_steps, expected)
                    self.assertFalse(hasattr(roles, "worker_max_steps") or hasattr(roles, "job_max_steps"))

    def test_old_worker_and_job_step_env_vars_are_ignored_even_when_invalid(self):
        roles = self.roles(LOOPCUT_WORKER_MAX_STEPS="not a number", LOOPCUT_JOB_MAX_STEPS="-1")
        self.assertFalse(hasattr(roles, "worker_max_steps") or hasattr(roles, "job_max_steps"))
        self.assertEqual((roles.job_max_usd, roles.worker_max_seconds), (0.40, 1800))

    def test_invalid_turn_step_limits_are_clear_config_errors(self):
        for value in ("-1", "1.5", "five", "10001", "²", "9" * 5000):
            with self.subTest(value=value[:20]), self.assertRaisesRegex(
                    config.ConfigError, r"LOOPCUT_MAX_STEPS must be a non-negative integer.*0 = unlimited"):
                self.roles(LOOPCUT_MAX_STEPS=value)

    def test_each_role_overrides_its_provider_model_key_and_effort(self):
        roles = self.roles(LOOPCUT_ORCHESTRATE="false", LOOPCUT_ORCH_BASE_URL="http://127.0.0.1:8765/v1",
                           LOOPCUT_ORCH_MODEL="opus", LOOPCUT_ORCH_API_KEY="k-orch",
                           LOOPCUT_WORKER_MODEL="glm-5.3-flash", LOOPCUT_WORKER_REASONING_EFFORT="low",
                           LOOPCUT_MAX_WORKERS="2", LOOPCUT_JOB_MAX_USD="0")
        self.assertFalse(roles.orchestrate)
        self.assertEqual((roles.orchestrator.base_url, roles.orchestrator.model, roles.orchestrator.api_key),
                         ("http://127.0.0.1:8765/v1", "opus", "k-orch"))
        self.assertEqual((roles.worker.model, roles.worker.reasoning_effort, roles.worker.api_key),
                         ("glm-5.3-flash", "low", "k-main"))
        self.assertEqual((roles.max_workers, roles.job_max_usd), (2, 0.0))

    def test_each_role_overrides_its_output_cap_and_call_limit(self):
        roles = self.roles(LOOPCUT_ORCH_MAX_OUTPUT_TOKENS="60000", LOOPCUT_WORKER_MAX_OUTPUT_TOKENS="20000",
                           LOOPCUT_WORKER_CALL_MAX_SECONDS="120")
        self.assertEqual(roles.orchestrator.max_output_tokens, 60000)
        self.assertEqual((roles.worker.max_output_tokens, roles.worker.call_max_seconds), (20000, 120.0))
        self.assertEqual(roles.orchestrator.call_max_seconds, 0.0, "unset: inherits the main value (none)")

    def test_on_the_loopcut_service_workers_run_fast_whatever_the_orchestrator_runs(self):
        roles = self.roles(LOOPCUT_BASE_URL=config.SERVICE_BASE_URL, LOOPCUT_MODEL="pro")
        self.assertEqual((roles.orchestrator.model, roles.worker.model), ("pro", "fast"))
        self.assertEqual(roles.worker.base_url, config.SERVICE_BASE_URL)

    def test_out_of_range_values_are_refused_by_name(self):
        for key, value in (("LOOPCUT_MAX_WORKERS", "7"), ("LOOPCUT_WORKER_MAX_SECONDS", "4"),
                           ("LOOPCUT_JOB_MAX_USD", "cheap"), ("LOOPCUT_ORCH_BASE_URL", "http://example.com")):
            with self.assertRaises(config.ConfigError) as caught:
                self.roles(**{key: value})
            self.assertIn(key, str(caught.exception))


class ItemsTest(unittest.TestCase):
    def test_wave_and_merge_items_survive_a_save_and_running_rows_reload_as_stopped(self):
        with tempfile.TemporaryDirectory() as data, mock.patch.dict(os.environ, {"LOOPCUT_DATA_DIR": data}):
            session = state.new_session()
            rows = [state.wave_row("armchair", "Armchair", "modeling"), state.wave_row("lamp", "Lamp", "modeling")]
            rows[1]["status"] = "done"
            preview = str(Path(data) / "helpers" / "armchair" / "images" / ("a" * 64 + ".png"))
            rows[0]["image_path"] = preview
            session["items"] += [state.item_user("a room"), state.item_wave("w1", 1, rows),
                                 state.item_merge("w1", "Merged 2 workers", ["14 created"], "", ["LIGHT Lamp_Test"])]
            conversations.save(session)
            loaded = conversations.load(session["id"])
        wave = loaded["items"][1]
        self.assertEqual(wave["text"], "Wave 1 · 2 workers")
        self.assertEqual([r["status"] for r in wave["rows"]], ["stopped", "done"])
        self.assertEqual(wave["rows"][0]["image_path"], preview)
        self.assertEqual(loaded["items"][2]["quarantined"], ["LIGHT Lamp_Test"])
        self.assertEqual(loaded["turn_number"], 1)

    def test_wave_preview_paths_follow_the_same_rules_as_tool_images(self):
        with tempfile.TemporaryDirectory() as data, mock.patch.dict(os.environ, {"LOOPCUT_DATA_DIR": data}):
            for path in ("/tmp/anything.png", "../images/" + "a" * 64 + ".png", 12):
                session = state.new_session()
                row = state.wave_row("a", "A", "modeling")
                row["image_path"] = path
                session["items"] = [state.item_wave("w1", 1, [row])]
                conversations.save(session)
                with mock.patch("builtins.print") as log:
                    loaded = conversations.load(session["id"])
                expected = {**row, "status": "stopped"}
                del expected["image_path"]
                self.assertEqual(loaded["items"][0]["rows"], [expected])
                log.assert_called_once()
                self.assertIn("preview", log.call_args.args[0].lower())

    def test_a_worker_conversation_lives_in_its_own_folder(self):
        with tempfile.TemporaryDirectory() as data, tempfile.TemporaryDirectory() as job, \
                mock.patch.dict(os.environ, {"LOOPCUT_DATA_DIR": data}):
            session = state.new_session()
            session["items"].append(state.item_user("brief"))
            conversations.FOLDERS[session["id"]] = Path(job) / "armchair"
            try:
                conversations.save(session)
            finally:
                conversations.FOLDERS.pop(session["id"])
            self.assertTrue((Path(job) / "armchair" / "conversation.json").is_file())
            self.assertFalse((Path(data) / "conversations").exists(), "not listed with the user's conversations")


class QuoteTest(unittest.TestCase):
    def test_punctuation_case_and_spacing_never_decide_a_match(self):
        self.assertTrue(orchestrator.quote_found("keep the Bottle,  and the box", "Keep the bottle and the box!"))
        self.assertTrue(orchestrator.quote_found("seat 0,45 m", "a seat 0.45 m high"))

    def test_whole_words_in_order_and_fragments_around_an_ellipsis(self):
        self.assertTrue(orchestrator.quote_found("an armchair ... on the rug", USER))
        self.assertFalse(orchestrator.quote_found("on the rug ... an armchair", USER), "fragments keep their order")
        self.assertFalse(orchestrator.quote_found("arm", USER), "part of a word is not a word")
        self.assertFalse(orchestrator.quote_found("", USER))

    def test_the_users_words_are_their_messages_without_the_scene_context(self):
        session = state.new_session()
        session["items"] += [state.item_user("a red lamp"), state.item_assistant()]
        session["messages"] += [{"role": "user", "content": "<scene_context>\nobjects: Camera\n</scene_context>\n\na red lamp"},
                                {"role": "user", context.REFERENCE_CARD: True, "content": [
                                    {"type": "text", "text": "x <reference_card images=\"a.png\">\nA brass lamp.\n</reference_card>"}]}]
        words = orchestrator.user_words(session)
        self.assertIn("a red lamp", words)
        self.assertIn("A brass lamp.", words, "the card of an image they attached stands for their picture")
        self.assertNotIn("Camera", words)


class ValidateTest(unittest.TestCase):
    def test_the_reference_wave_passes_and_numbers_its_checks(self):
        self.assertEqual(refusals(reference_wave()), [])
        plan = orchestrator.normalise_wave(reference_wave())
        self.assertEqual([c["id"] for c in plan["workers"][0]["checks"]], ["c1", "c2"])
        self.assertEqual(plan["workers"][1]["owns"], [])

    def test_a_relation_check_on_regions_of_one_mesh_is_a_brief_check(self):
        """What GPT-6.1 Sol wished for after the asset pack's sign sat on the ground (20260929-230207):
        "hanging" stated as a relation, with regions of a joined mesh, and hard because it quotes the user."""
        wave = reference_wave()
        wave["workers"][0]["checks"].append({"kind": "suspended", "a": "Armchair", "a_part": "material:Pal_Wood",
                                             "b": "Armchair", "b_part": "group:Chains", "quote": "an armchair"})
        self.assertEqual(refusals(wave), [])
        wave["workers"][0]["checks"][-1]["b_part"] = "Chains"
        self.assertIn("b_part: material:NAME, group:NAME or attribute:NAME", refusals(wave)[0])

    def test_a_check_for_a_render_nobody_asked_for_is_refused(self):
        """M0: the spec invented final renders, and its own checks then passed them."""
        wave = reference_wave()
        wave["workers"][0]["checks"].append({"kind": "file", "path": "renders/final.png", "quote": "a final render"})
        errors = refusals(wave)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("workers[0] (armchair).checks[2]", errors[0])
        self.assertIn("not in the user's messages", errors[0])

    def test_a_requirement_needs_the_users_words_and_a_worker_needs_a_requirement(self):
        wave = reference_wave()
        wave["workers"][0]["requirements"] = [{"quote": "a cosy reading nook", "do": "nook"}]
        wave["workers"][1]["requirements"] = []
        errors = refusals(wave)
        self.assertTrue(any("requirements[0]" in e and "not in the user's messages" in e for e in errors), errors)
        self.assertTrue(any("(table).requirements: at least one" in e for e in errors), errors)

    def test_owning_a_kept_object_is_refused(self):
        """M0: the spec moved objects the brief said to keep."""
        wave = reference_wave()
        wave["workers"][1]["owns"] = ["Bottle"]
        self.assertIn("workers[1] (table).owns: 'Bottle' is kept (shared.keep); nobody may own it", refusals(wave))

    def test_a_missing_palette_material_or_support_is_refused(self):
        wave = reference_wave()
        wave["shared"]["palette"].append("Pal_Velvet")
        wave["shared"]["supports"].append({"name": "Shelf", "top_z": 1.2})
        errors = refusals(wave)
        self.assertIn("shared.palette: material 'Pal_Velvet' is not in the scene; create it yourself first", errors)
        self.assertTrue(any(e.startswith("shared.supports[1]: 'Shelf' is not in the scene") for e in errors), errors)

    def test_a_support_height_that_was_not_measured_is_refused_with_the_measurement(self):
        """M0: numbers never checked against each other."""
        wave = reference_wave()
        wave["shared"]["supports"][0]["top_z"] = 0.05
        errors = refusals(wave)
        self.assertTrue(any("top_z 0.050 but Rug's top is at 0.015" in e for e in errors), errors)

    def test_prefixes_owners_aspects_and_limits(self):
        wave = reference_wave()
        wave["workers"][1].update(prefix="armchair", owns=["Floor"], aspects=["camera"])
        wave["workers"].append({"id": "cam", "deliverable": "Shot", "role": "camera", "prefix": "Lamp", "owns": ["Floor"],
                                "aspects": ["camera"], "requirements": [{"quote": "the camera", "do": "frame the set"}],
                                "checks": [{"kind": "exists", "name": "Shot", "quote": "the camera"}] * 9,
                                "defaults": [{"kind": "dims"}]})
        errors = refusals(wave)
        for expected in ("(table).prefix: 'armchair' is also armchair's", "(cam).prefix: 'Lamp' is taken by Lamp_Old",
                         "(cam).owns: 'Floor' is already owned by table", "(cam).aspects: camera is already table's",
                         "(cam).checks: at most 8, got 9", "(cam).defaults: plain strings"):
            self.assertTrue(any(expected in e for e in errors), f"{expected!r} not in {errors}")

    def test_one_prefix_starting_another_in_the_same_wave_is_refused(self):
        """A name may carry a prefix with no underscore (merge.prefixed), so "Potion" and "PotionExtra"
        in one wave would own each other's names; refuse before the wave starts, either order."""
        wave = reference_wave()
        wave["workers"][0]["prefix"] = "Potion"
        wave["workers"][1]["prefix"] = "PotionExtra"
        errors = [e for e in refusals(wave) if ".prefix" in e]
        self.assertTrue(any("'PotionExtra'" in e and "'Potion'" in e and "collide" in e for e in errors), errors)

        wave = reference_wave()
        wave["workers"][0]["prefix"] = "PotionExtra"
        wave["workers"][1]["prefix"] = "Potion"
        errors = [e for e in refusals(wave) if ".prefix" in e]
        self.assertTrue(any("'Potion'" in e and "'PotionExtra'" in e and "collide" in e for e in errors), errors)

        wave = reference_wave()
        wave["workers"][0]["prefix"] = "Potion"
        wave["workers"][1]["prefix"] = "Shop"
        self.assertFalse([e for e in refusals(wave) if ".prefix" in e], "unrelated prefixes share no leading run")

    def test_what_a_worker_owns_does_not_take_its_own_prefix(self):
        """perfume_ad: prefix "Cam" was refused because Cam_Hero exists, though that worker owned it."""
        live = {**LIVE, "objects": {**LIVE["objects"], "Cam_Hero": {"type": "CAMERA", "box": None, "data": "Cam_Hero"},
                                    "Lit_Key": {"type": "LIGHT", "box": None, "data": "Lit_KeyData"}},
                "names": LIVE["names"] + ["Cam_Hero", "Cam_Hero", "Cam_Rig", "Lit_Key", "Lit_KeyData", "Lit_World"],
                "world": "Lit_World"}
        wave = reference_wave()
        wave["workers"].append({"id": "cam", "deliverable": "Cam_Hero", "role": "camera", "prefix": "Cam",
                                "owns": ["Cam_Hero"], "requirements": [{"quote": "the camera", "do": "frame the set"}]})
        errors = refusals(wave, live=live)
        self.assertEqual([e for e in errors if ".prefix" in e],
                         ["workers[2] (cam).prefix: 'Cam' is taken by Cam_Rig; to rework those, list them in owns and "
                          "use a new prefix"], "its own Cam_Hero (object and data) never blocks it; Cam_Rig does")
        wave["workers"][2]["owns"] = ["Cam_Hero", "Cam_Rig"]
        self.assertFalse([e for e in refusals(wave, live=live) if ".prefix" in e])
        wave["workers"].append({"id": "lit", "deliverable": "Lit_Rig", "role": "lighting", "prefix": "Lit",
                                "aspects": ["lighting"], "requirements": [{"quote": "the camera", "do": "light it"}]})
        self.assertFalse([e for e in refusals(wave, live=live) if ".prefix" in e],
                         "the lighting aspect owns every light, its data and the world")
        wave["workers"][3]["aspects"] = []
        self.assertIn("'Lit' is taken by Lit_Key, Lit_KeyData, Lit_World", " ".join(refusals(wave, live=live)))

    def test_the_palette_and_the_supports_block_no_prefix(self):
        """A palette material Chest_Wood made the prefix Chest invalid; they are everyone's, not the worker's."""
        live = {**LIVE, "objects": {**LIVE["objects"], "Chest_Base": {"type": "MESH", "box": [[0, 0, 0], [1, 1, 0.1]],
                                                                       "data": "Chest_BaseMesh"}},
                "materials": LIVE["materials"] + ["Chest_Wood"],
                "names": LIVE["names"] + ["Chest_Wood", "Chest_Base", "Chest_BaseMesh"]}
        wave = reference_wave()
        wave["shared"].update(palette=["Pal_Wood", "Chest_Wood"], supports=[{"name": "Rug", "top_z": 0.015},
                                                                            {"name": "Chest_Base", "top_z": 0.1}])
        wave["workers"][1]["prefix"] = "Chest"
        self.assertFalse([e for e in refusals(wave, live=live) if ".prefix" in e])
        live["names"].append("Chest_Lid")
        self.assertIn("'Chest' is taken by Chest_Lid;", " ".join(refusals(wave, live=live)), "anything else still does")
        schema = orchestrator.SCHEMAS[0]["function"]["parameters"]["properties"]["wave"]["properties"]
        self.assertIn("unless it owns it or shared lists it", schema["workers"]["items"]["properties"]["prefix"]["description"])

    def test_a_deliverable_named_like_something_live_must_be_owned(self):
        """The merge keeps the root's name, so a second "Rug" would be "Rug.001" and the checks would
        measure the old one."""
        wave = reference_wave()
        wave["workers"][1].update(deliverable="Rug")
        self.assertIn("workers[1] (table).deliverable: 'Rug' is already in the scene; to rework it, list it in owns; "
                      "for a new one, give it a new name", refusals(wave))
        wave["workers"][1]["owns"] = ["Rug"]
        self.assertFalse(any(".deliverable" in e for e in refusals(wave)))

    def test_malformed_checks_are_named(self):
        wave = reference_wave()
        wave["workers"][0]["checks"] = [{"kind": "contact", "a": "Armchair", "relation": "on", "b": "Rug", "quote": "on the rug"},
                                        {"kind": "glow", "quote": "on the rug"}, {"kind": "count", "quote": "on the rug"},
                                        {"kind": "dims", "name": "Armchair", "size": [1, 2], "quote": "an armchair"}]
        errors = " | ".join(refusals(wave))
        for expected in ("checks[0] (contact) relation: one of rests_on", "checks[1] kind: 'glow' is not one of",
                         "checks[2] (count) prefix or collection: exactly one", "checks[3] (dims) size: [x, y, z]"):
            self.assertIn(expected, errors)
        self.assertIn("checks[2] (count) min or max: at least one", errors, "the rules the checks run by (acceptance)")

    def test_resting_on_something_built_in_the_same_wave_is_refused(self):
        wave = reference_wave()
        wave["workers"].append({"id": "lamp", "deliverable": "Lamp", "role": "modeling", "prefix": "Lamp",
                                "requirements": [{"quote": "a lamp on the side table", "do": "table lamp"}],
                                "bounds": [[0.3, 0.3, 0.57], [0.6, 0.6, 1.0]], "rests_on": "SideTable"})
        live = {**LIVE, "names": [n for n in LIVE["names"] if n != "Lamp_Old"]}
        errors = refusals(wave, live=live)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("'SideTable' is built in this same wave", errors[0])


class RepairWaveTest(unittest.TestCase):
    """M4: the GLM orchestrator lost most of its delegate calls to refusals over small, mechanical
    mistakes. repair_wave fixes those before validate_wave ever sees them; a refusal is kept only for
    what would break the wave or the scope lock."""

    def repair(self, wave: dict, words: str = USER, live: dict = LIVE) -> tuple[dict, list]:
        plan = orchestrator.normalise_wave(wave)
        return orchestrator.repair_wave(plan, words, live)

    def test_a_check_with_no_quote_or_a_non_user_quote_becomes_advisory_not_refused(self):
        """Measurable, just not the user's words: it still measures, advisory (never failing)."""
        wave = reference_wave()
        wave["workers"][0]["checks"].append({"kind": "exists", "name": "Armchair",
                                              "quote": "Name the pieces Armchair"})
        plan, repairs = self.repair(wave)
        self.assertEqual(len(plan["workers"][0]["checks"]), 2, "the bad-quote check moved, the good ones kept")
        self.assertTrue(any("checks[2]: moved to advisory (exists Armchair)" in r for r in repairs), repairs)
        self.assertEqual(plan["workers"][0]["advisory"], [{"kind": "exists", "name": "Armchair",
                                                           "quote": "Name the pieces Armchair", "id": "a1"}])
        self.assertEqual(orchestrator.validate_wave(plan, USER, LIVE), [], "no longer refused")

    def test_dropping_a_check_never_erases_the_requirements_guarantee(self):
        """A worker with nothing to build is still refused: that guarantee comes from requirements,
        untouched by repairing checks."""
        wave = reference_wave()
        wave["workers"][1]["requirements"] = []
        plan, _ = self.repair(wave)
        errors = orchestrator.validate_wave(plan, USER, LIVE)
        self.assertTrue(any("requirements: at least one" in e for e in errors), errors)

    def test_no_overlap_a_b_is_aliased_to_names(self):
        wave = reference_wave()
        wave["workers"][0]["checks"] = [{"kind": "no_overlap", "a": "Armchair", "b": "SideTable",
                                         "quote": "an armchair and a side table on the rug"}]
        plan, repairs = self.repair(wave)
        check = plan["workers"][0]["checks"][0]
        self.assertEqual(check.get("names"), ["Armchair", "SideTable"])
        self.assertNotIn("a", check)
        self.assertNotIn("b", check)
        self.assertTrue(any("aliased" in r for r in repairs), repairs)

    def test_contact_field_named_contact_or_type_is_aliased_to_relation(self):
        for field in ("contact", "type"):
            wave = reference_wave()
            wave["workers"][0]["checks"] = [{"kind": "contact", "a": "Armchair", field: "rests_on", "b": "Rug",
                                             "quote": "an armchair and a side table on the rug"}]
            plan, _ = self.repair(wave)
            check = plan["workers"][0]["checks"][0]
            self.assertEqual(check.get("relation"), "rests_on", field)
            self.assertNotIn(field, check)

    def test_light_inside_in_or_b_is_aliased_to_container(self):
        for field in ("in", "b"):
            wave = reference_wave()
            wave["workers"][0]["checks"] = [{"kind": "light_inside", "light": "Armchair_Lamp", field: "Armchair",
                                             "quote": "a lamp on the side table"}]
            plan, _ = self.repair(wave)
            check = plan["workers"][0]["checks"][0]
            self.assertEqual(check.get("container"), "Armchair", field)
            self.assertNotIn(field, check)

    def test_setting_with_both_equals_and_min_max_keeps_equals(self):
        wave = reference_wave()
        wave["workers"][0]["checks"] = [{"kind": "setting", "path": "render.engine", "equals": "CYCLES",
                                         "min": 1, "quote": "an armchair"}]
        plan, repairs = self.repair(wave)
        check = plan["workers"][0]["checks"][0]
        self.assertEqual(check.get("equals"), "CYCLES")
        self.assertNotIn("min", check)
        self.assertTrue(any("aliased" in r for r in repairs), repairs)

    def test_in_frame_region_given_0_100_is_scaled_to_fractions(self):
        wave = reference_wave()
        wave["workers"][0]["checks"][1]["region"] = [10, 20, 90, 80]
        plan, repairs = self.repair(wave)
        self.assertEqual(plan["workers"][0]["checks"][1]["region"], [0.1, 0.2, 0.9, 0.8])
        self.assertTrue(any("aliased" in r for r in repairs), repairs)

    def test_prefix_with_non_alphanumerics_is_sanitised_and_capitalised(self):
        wave = reference_wave()
        wave["workers"][0]["prefix"] = "arm_chair2000"
        plan, repairs = self.repair(wave)
        self.assertEqual(plan["workers"][0]["prefix"], "Armchair2000")
        self.assertTrue(any("prefix" in r and "sanitised" in r for r in repairs), repairs)
        self.assertEqual(orchestrator.validate_wave(plan, USER, LIVE), [])

    def test_a_prefix_with_nothing_letter_first_left_is_still_refused(self):
        wave = reference_wave()
        wave["workers"][0]["prefix"] = "123"
        plan, repairs = self.repair(wave)
        self.assertEqual(plan["workers"][0]["prefix"], "123", "nothing survives sanitising; unrepaired")
        self.assertFalse([r for r in repairs if ".prefix" in r])
        errors = orchestrator.validate_wave(plan, USER, LIVE)
        self.assertTrue(any(".prefix: a letter" in e for e in errors), errors)

    def test_invalid_aspects_are_dropped_not_refused(self):
        wave = reference_wave()
        wave["workers"][0]["aspects"] = ["modeling", "lighting"]
        plan, repairs = self.repair(wave)
        self.assertEqual(plan["workers"][0]["aspects"], ["lighting"])
        self.assertTrue(any("aspects" in r and "dropped" in r for r in repairs), repairs)
        self.assertEqual(orchestrator.validate_wave(plan, USER, LIVE), [])

    def test_palette_names_that_dont_exist_are_dropped(self):
        wave = reference_wave()
        wave["shared"]["palette"].append("Pal_Velvet")
        plan, repairs = self.repair(wave)
        self.assertEqual(plan["shared"]["palette"], ["Pal_Wood"])
        self.assertTrue(any("Pal_Velvet" in r for r in repairs), repairs)
        self.assertEqual(orchestrator.validate_wave(plan, USER, LIVE), [])

    def test_keep_without_a_quote_still_keeps_but_gets_no_check(self):
        wave = reference_wave()
        wave["shared"]["keep"]["quote"] = "not the users words at all"
        plan, repairs = self.repair(wave)
        self.assertEqual(plan["shared"]["keep"]["names"], ["Bottle", "Box"])
        self.assertEqual(plan["shared"]["keep"]["quote"], "")
        self.assertTrue(any("shared.keep.quote" in r for r in repairs), repairs)
        self.assertEqual(orchestrator.validate_wave(plan, USER, LIVE), [])

    def test_keep_given_as_a_list_of_names_is_normalised(self):
        wave = reference_wave()
        wave["shared"]["keep"] = ["Bottle", "Box"]
        plan, repairs = self.repair(wave)
        self.assertEqual(plan["shared"]["keep"]["names"], ["Bottle", "Box"])
        self.assertEqual(plan["shared"]["keep"]["quote"], "", "a plain list carries no quote")
        self.assertNotIn(orchestrator._KEEP_LIST_SHAPE, plan["shared"]["keep"], "the marker never leaks out")
        self.assertTrue(any("shared.keep: a list given" in r for r in repairs), repairs)
        self.assertTrue(any("shared.keep.quote" in r for r in repairs), "unquoted: still kept, no check: " + str(repairs))
        self.assertEqual(orchestrator.validate_wave(plan, USER, LIVE), [])

    def test_keep_given_as_a_list_of_objects_is_normalised(self):
        wave = reference_wave()
        wave["shared"]["keep"] = [{"name": "Bottle"}, {"name": "Box"}, {"not_a_name": "nope"}]
        plan, repairs = self.repair(wave)
        self.assertEqual(plan["shared"]["keep"]["names"], ["Bottle", "Box"])
        self.assertTrue(any("shared.keep: a list given" in r for r in repairs), repairs)
        self.assertEqual(orchestrator.validate_wave(plan, USER, LIVE), [])

    def test_a_role_synonym_is_mapped_to_the_real_role(self):
        for synonym, role in (("model", "modeling"), ("modeler", "modeling"), ("modelling", "modeling"),
                              ("props", "modeling"), ("geometry", "modeling"), ("light", "lighting"),
                              ("lights", "lighting"), ("cam", "camera"), ("cameras", "camera"),
                              ("framing", "camera"), ("anim", "animation"), ("animate", "animation"),
                              ("motion", "animation"), ("material", "materials"), ("shading", "materials"),
                              ("Model", "modeling")):
            wave = reference_wave()
            wave["workers"][0]["role"] = synonym
            plan, repairs = self.repair(wave)
            self.assertEqual(plan["workers"][0]["role"], role, synonym)
            self.assertTrue(any(".role:" in r and "mapped" in r for r in repairs), (synonym, repairs))
            self.assertEqual(orchestrator.validate_wave(plan, USER, LIVE), [])

    def test_an_unrecognised_role_defaults_to_modeling_not_refused(self):
        wave = reference_wave()
        wave["workers"][0]["role"] = "sculpting"
        plan, repairs = self.repair(wave)
        self.assertEqual(plan["workers"][0]["role"], "modeling", "no synonym: defaulted, not left broken")
        self.assertTrue(any(".role:" in r and "defaulted" in r for r in repairs), repairs)
        self.assertEqual(orchestrator.validate_wave(plan, USER, LIVE), [])

    def test_a_missing_role_also_defaults_to_modeling(self):
        wave = reference_wave()
        del wave["workers"][0]["role"]
        plan, repairs = self.repair(wave)
        self.assertEqual(plan["workers"][0]["role"], "modeling")
        self.assertTrue(any(".role:" in r and "defaulted" in r for r in repairs), repairs)
        self.assertEqual(orchestrator.validate_wave(plan, USER, LIVE), [])

    def test_a_count_check_with_name_instead_of_prefix_is_aliased(self):
        wave = reference_wave()
        wave["workers"][0]["checks"] = [{"kind": "count", "name": "Armchair", "min": 1, "quote": "an armchair"}]
        plan, repairs = self.repair(wave)
        check = plan["workers"][0]["checks"][0]
        self.assertEqual(check.get("prefix"), "Armchair")
        self.assertNotIn("name", check)
        self.assertTrue(any("checks[0]: fields aliased to count Armchair" in r for r in repairs), repairs)
        self.assertEqual(orchestrator.validate_wave(plan, USER, LIVE), [])

    def test_check_fields_nested_under_the_kind_key_are_unwrapped(self):
        wave = reference_wave()
        wave["workers"][0]["checks"] = [{"kind": "contact", "contact": {"a": "Armchair", "b": "Rug",
                                         "relation": "rests_on"}, "quote": "an armchair and a side table on the rug"}]
        plan, repairs = self.repair(wave)
        check = plan["workers"][0]["checks"][0]
        self.assertEqual((check.get("a"), check.get("b"), check.get("relation")), ("Armchair", "Rug", "rests_on"))
        self.assertNotIn("contact", check)
        self.assertTrue(any("fields aliased" in r for r in repairs), repairs)
        self.assertEqual(orchestrator.validate_wave(plan, USER, LIVE), [])

    def test_check_fields_nested_under_a_generic_check_key_are_unwrapped(self):
        wave = reference_wave()
        wave["workers"][0]["checks"] = [{"kind": "exists", "check": {"name": "Armchair", "type": "MESH"},
                                         "quote": "an armchair"}]
        plan, repairs = self.repair(wave)
        check = plan["workers"][0]["checks"][0]
        self.assertEqual(check.get("name"), "Armchair")
        self.assertNotIn("check", check)
        self.assertEqual(orchestrator.validate_wave(plan, USER, LIVE), [])

    def test_a_bare_kind_wrapper_scalar_is_read_as_the_kinds_one_clear_field(self):
        wave = reference_wave()
        wave["workers"][0]["checks"] = [{"kind": "exists", "exists": "Armchair", "quote": "an armchair"}]
        plan, repairs = self.repair(wave)
        self.assertEqual(plan["workers"][0]["checks"][0].get("name"), "Armchair")
        self.assertNotIn("exists", plan["workers"][0]["checks"][0])
        self.assertEqual(orchestrator.validate_wave(plan, USER, LIVE), [])

    def test_dims_dimensions_is_aliased_to_size(self):
        wave = reference_wave()
        wave["workers"][0]["checks"] = [{"kind": "dims", "name": "Armchair", "dimensions": [0.9, 0.9, 0.95],
                                         "quote": "an armchair"}]
        plan, repairs = self.repair(wave)
        check = plan["workers"][0]["checks"][0]
        self.assertEqual(check.get("size"), [0.9, 0.9, 0.95])
        self.assertNotIn("dimensions", check)
        self.assertEqual(orchestrator.validate_wave(plan, USER, LIVE), [])

    def test_animated_and_setting_property_channel_data_path_are_aliased_to_path(self):
        wave = reference_wave()
        wave["workers"][0]["checks"] = [{"kind": "animated", "name": "Armchair", "property": "location",
                                         "frames": [1, 20], "quote": "an armchair"}]
        plan, repairs = self.repair(wave)
        check = plan["workers"][0]["checks"][0]
        self.assertEqual(check.get("path"), "location")
        self.assertNotIn("property", check)
        self.assertEqual(orchestrator.validate_wave(plan, USER, LIVE), [])

    def test_contact_object_on_and_subject_support_alias_to_a_b_and_imply_rests_on(self):
        wave = reference_wave()
        wave["workers"][0]["checks"] = [{"kind": "contact", "object": "Armchair", "on": "Rug",
                                         "quote": "an armchair and a side table on the rug"}]
        plan, repairs = self.repair(wave)
        check = plan["workers"][0]["checks"][0]
        self.assertEqual((check.get("a"), check.get("b"), check.get("relation")), ("Armchair", "Rug", "rests_on"))
        self.assertEqual(orchestrator.validate_wave(plan, USER, LIVE), [])

    def test_a_check_that_still_fails_once_aliased_is_dropped_not_refused(self):
        wave = reference_wave()
        wave["workers"][0]["checks"] = [{"kind": "dims", "name": "Armchair", "size": "wide", "quote": "an armchair"}]
        plan, repairs = self.repair(wave)
        self.assertEqual(plan["workers"][0]["checks"], [])
        self.assertTrue(any("checks[0]: dropped (dims Armchair)" in r for r in repairs), repairs)
        self.assertEqual(orchestrator.validate_wave(plan, USER, LIVE), [])

    def test_owning_a_kept_name_keeps_it_kept_not_owned(self):
        wave = reference_wave()
        wave["workers"][0]["owns"] = ["Bottle"]  # Bottle is already in shared.keep
        plan, repairs = self.repair(wave)
        self.assertEqual(plan["workers"][0]["owns"], [])
        self.assertTrue(any("owns" in r and "keeping wins" in r for r in repairs), repairs)
        self.assertEqual(orchestrator.validate_wave(plan, USER, LIVE), [])

    def test_a_flat_six_number_bounds_is_reshaped_to_the_real_shape(self):
        wave = reference_wave()
        wave["workers"][0]["bounds"] = [-0.9, 0.2, 0.015, -0.1, 1.0, 0.95]
        plan, repairs = self.repair(wave)
        self.assertEqual(plan["workers"][0]["bounds"], [[-0.9, 0.2, 0.015], [-0.1, 1.0, 0.95]])
        self.assertTrue(any(".bounds: reshaped" in r for r in repairs), repairs)
        self.assertEqual(orchestrator.validate_wave(plan, USER, LIVE), [])

    def test_bounds_that_cannot_be_reshaped_are_dropped_not_refused(self):
        wave = reference_wave()
        wave["workers"][0]["bounds"] = [1, 2, 3]  # neither a box nor 6 flat numbers
        wave["workers"][0]["rests_on"] = ""  # rests_on needs bounds; cleared so dropping doesn't newly refuse
        plan, repairs = self.repair(wave)
        self.assertIsNone(plan["workers"][0]["bounds"])
        self.assertTrue(any(".bounds: malformed; dropped" in r for r in repairs), repairs)
        self.assertEqual(orchestrator.validate_wave(plan, USER, LIVE), [])

    def test_rests_on_names_a_real_object_not_declared_a_support_its_top_is_measured(self):
        wave = reference_wave()
        wave["shared"]["supports"] = []  # Rug never declared, though both workers rest on it
        plan, repairs = self.repair(wave)
        self.assertEqual(plan["shared"]["supports"], [{"name": "Rug", "top_z": 0.015}])
        self.assertTrue(any("shared.supports: added 'Rug'" in r for r in repairs), repairs)
        self.assertEqual(orchestrator.validate_wave(plan, USER, LIVE), [])

    def test_a_requirement_with_a_bad_quote_is_dropped_not_refused(self):
        wave = reference_wave()
        wave["workers"][0]["requirements"].append({"quote": "", "do": "Do not add extra lights"})
        plan, repairs = self.repair(wave)
        self.assertEqual(len(plan["workers"][0]["requirements"]), 1, "the bad-quote one dropped, the real one kept")
        self.assertTrue(any("requirements[1]: dropped" in r for r in repairs), repairs)
        self.assertIn("Do not add extra lights (not required:", plan["workers"][0]["defaults"][-1])
        self.assertEqual(orchestrator.validate_wave(plan, USER, LIVE), [])

    def test_recolour_workers_claiming_aspect_materials_are_scoped_to_what_they_own(self):
        """GPT-6.1 Sol, 20260929-230207 O0014: recolour workers each given aspects ["materials"] made five
        dry-run conflicts; owning their objects was all they needed (O0015)."""
        wave = {"shared": {}, "workers": [
            {"id": "rug", "deliverable": "Rug", "role": "materials", "prefix": "RugIce", "owns": ["Rug"],
             "aspects": ["materials"], "requirements": [{"quote": "an armchair", "do": "recolour"}]},
            {"id": "floor", "deliverable": "Floor", "role": "modeling", "prefix": "FloorIce", "owns": ["Floor"],
             "aspects": ["materials", "lighting"], "requirements": [{"quote": "an armchair", "do": "recolour"}]}]}
        plan, notes = self.repair(wave)
        self.assertEqual([w["aspects"] for w in plan["workers"]], [[], ["lighting"]])
        self.assertEqual(len(notes), 2, notes)
        self.assertIn("workers[0] (rug).aspects: dropped materials (2 briefs claimed it); aspect materials is global",
                      notes[0])
        self.assertIn("needs only owns, so it still recolours Rug", notes[0])
        self.assertEqual(orchestrator.validate_wave(plan, USER, LIVE), [])
        one = {"shared": {}, "workers": wave["workers"][:1]}
        self.assertEqual(self.repair(one)[0]["workers"][0]["aspects"], ["materials"], "one claim, no conflict: kept")
        live = dict(LIVE, busy={"old": {"aspects": ["materials"], "owns": [], "prefix": "Old", "deliverable": "X"}})
        plan, notes = self.repair(one, live=live)
        self.assertEqual((plan["workers"][0]["aspects"], "running worker old holds it" in notes[0]), ([], True))

    def test_a_tool_workers_are_not_offered_is_replaced_by_the_one_that_does_its_job(self):
        """GPT-6.1 Sol's lighting brief (20260929-235231 W0100-W0106) asked for inspect_scene, which workers
        are not offered; the worker said so in every step."""
        wave = {"shared": {"notes": "Judge looks with inspect_scene."}, "workers": [
            {"id": "light", "deliverable": "KeyLight", "role": "lighting", "prefix": "Key", "aspects": ["lighting"],
             "requirements": [{"quote": "the camera should show the whole set",
                               "do": "Light the set; use inspect_scene with purpose final_lighting to judge it"}],
             "defaults": ["Render a preview with render_job and write_file the notes"],
             "style": "moody; compare_with_reference against the card"}]}
        with mock.patch.object(agent, "_tool_schemas", TOOL_SCHEMAS):  # test_agent_loop stubs it.
            plan, notes = self.repair(wave)
            warnings = orchestrator.tool_warnings(plan)
        worker = plan["workers"][0]
        self.assertEqual(worker["requirements"][0]["do"],
                         "Light the set; use capture_viewport angle final (camera, view_layer) with purpose final_lighting to "
                         "judge it")
        self.assertEqual(worker["style"], "moody; look_at_reference against the card")
        self.assertEqual(plan["shared"]["notes"], "Judge looks with capture_viewport angle final (camera, view_layer).")
        self.assertIn("workers[0] (light).requirements[0]: inspect_scene is not a worker tool; replaced with "
                      "capture_viewport angle final (camera, view_layer)", notes)
        self.assertIn("shared.notes: inspect_scene is not a worker tool; replaced with capture_viewport angle final "
                      "(camera, view_layer)", notes)
        self.assertEqual(worker["requirements"][0]["quote"], "the camera should show the whole set")
        self.assertEqual(warnings,
                         ["workers[0] (light).defaults[0]: render_job, write_file are not worker tools (workers build; "
                          "renders, exports and files stay yours, after the last wave)"])
        self.assertTrue(set(orchestrator.WORKER_INSTEAD.values()) <= {"capture_viewport angle final (camera, view_layer)",
                                                                "look_at_reference"})
        self.assertIn("capture_viewport", workers.tool_names())
        self.assertNotIn("inspect_scene", workers.tool_names())

    def test_two_palette_claims_with_nothing_owned_are_refused_saying_why(self):
        wave = {"shared": {}, "workers": [
            {"id": f"m{n}", "deliverable": f"Look{n}", "role": "materials", "prefix": f"Look{chr(65 + n)}",
             "aspects": ["materials"], "requirements": [{"quote": "an armchair", "do": "palette"}]} for n in range(2)]}
        plan, notes = self.repair(wave)
        self.assertEqual(notes, [])
        errors = orchestrator.validate_wave(plan, USER, LIVE)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("workers[1] (m1).aspects: materials is already m0's (aspect materials is global", errors[0])
        self.assertIn("needs only owns; drop it here)", errors[0])

    def test_long_shared_notes_are_trimmed_not_refused(self):
        wave = reference_wave()
        wave["shared"]["notes"] = "x" * (orchestrator.MAX_NOTES + 50)
        plan, repairs = self.repair(wave)
        self.assertLessEqual(len(plan["shared"]["notes"]), orchestrator.MAX_NOTES)
        self.assertTrue(any("shared.notes:" in r and "trimmed" in r for r in repairs), repairs)
        self.assertEqual(orchestrator.validate_wave(plan, USER, LIVE), [])


class GLMEvidenceFixturesTest(unittest.TestCase):
    """Real delegate calls the GLM orchestrator sent and had refused, 2026-09-28
    (out/evals/20260928-005053/dungeon_modules.conversation): the field names, values and quotes are
    verbatim (the 16-check brief trimmed to 4, still all missing a quote, for a readable test). Each
    must now pass with repairs, or be refused only for what genuinely still needs fixing."""

    WORDS = ('Build a small low-poly dungeon kit and assemble a room from it. Make four reusable '
             'modules on a 2 m grid: a floor tile (2 x 2 m), a wall segment, a doorway segment and a '
             'pillar, each modelled once and reused as linked duplicates sharing one mesh. Assemble '
             'one enclosed room of at least 4 x 4 tiles with exactly one doorway, put a treasure '
             'chest in the centre and a few glowing crystals around the room. Name every object by '
             'its module (Floor, Wall, Doorway, Pillar, Chest, Crystal). Add a camera walkthrough '
             'that starts outside the doorway and ends inside the room, 120 frames at 24 fps. Set '
             'the scene up for Cycles and export everything as dungeon.glb next to this .blend '
             'file, for Godot.')

    def test_the_kit_worker_prefix_DK_and_quoteless_checks(self):
        """GLM's call refused for workers[0].prefix ("a letter, then letters and digits"), "checks:
        at most 8, got 16", 16 x "quote: the user's words this check comes from", plus a genuine
        "shared.supports[0]: 'Ground' is not in the scene" — the scene had only Light and Camera."""
        wave = {"shared": {"supports": [{"name": "Ground", "top_z": 0}], "units": "meters",
                           "palette": ["Stone", "StoneDark", "Wood", "Metal", "Gold", "Crystal"],
                           "notes": "Grid origin (0,0,0); tiles occupy [0,2]x[0,2] in XY, floor tops at z=0."},
                "workers": [{"id": "kit", "role": "modeling", "prefix": "DK_", "deliverable": "DungeonKit",
                            "bounds": [[0, 0, -0.3], [8, 2, 2.5]],
                            "requirements": [{"quote": "four reusable modules on a 2 m grid: a floor tile (2 x "
                                              "2 m), a wall segment, a doorway segment and a pillar, each "
                                              "modelled once and reused as linked duplicates sharing one mesh",
                                              "do": "build the four module masters"}],
                            "checks": [{"kind": "count", "prefix": "DK_", "type": "MESH", "min": 8},
                                       {"kind": "exists", "name": "DK_Floor", "type": "MESH"},
                                       {"kind": "exists", "name": "DK_Wall", "type": "MESH"},
                                       {"kind": "exists", "name": "DK_Doorway", "type": "MESH"}]}]}
        live = {"objects": {"Light": {"type": "LIGHT", "box": None}, "Camera": {"type": "CAMERA", "box": None}},
                "materials": [], "names": ["Light", "Camera"]}
        plan = orchestrator.normalise_wave(wave)
        plan, repairs = orchestrator.repair_wave(plan, self.WORDS, live)
        self.assertEqual(plan["workers"][0]["prefix"], "DK", "the underscore sanitised, first letter kept")
        self.assertEqual(plan["workers"][0]["checks"], [], "every check had no quote; all advisory now")
        self.assertEqual([c["id"] for c in plan["workers"][0]["advisory"]], ["a1", "a2", "a3", "a4"])
        self.assertEqual(plan["workers"][0]["defaults"], [])
        self.assertEqual(plan["shared"]["palette"], [], "none of the 6 palette materials exist yet; dropped")
        errors = orchestrator.validate_wave(plan, self.WORDS, live)
        self.assertEqual(errors, ["shared.supports[0]: 'Ground' is not in the scene; build supports first"],
                         "the one refusal left is real: the orchestrator never built Ground")

    def test_the_cam_walk_worker_aspects_camera_animation(self):
        """A later call in the same run: refused only for "workers[0] (cam-walk).aspects: only
        lighting, camera" (animation is a role, not an aspect). Everything else in the real brief —
        prefix WC, the animated/exists/in_frame checks with real quotes — was already fine."""
        wave = {"shared": {"supports": [{"name": "Ground", "top_z": 0}], "units": "meters", "palette": [],
                           "notes": "Room footprint x 4..12, y 4..12; doorway 'Doorway_S' opening is in the "
                                    "south wall."},
                "workers": [{"id": "cam-walk", "aspects": ["camera", "animation"],
                            "bounds": [[3, 0, 0], [13, 13, 2.2]],
                            "checks": [
                                {"kind": "animated", "name": "WalkCamera", "path": "location", "frames": [1, 120],
                                 "quote": "Add a camera walkthrough that starts outside the doorway and ends "
                                          "inside the room, 120 frames at 24 fps"},
                                {"kind": "exists", "name": "WalkCamera", "type": "CAMERA",
                                 "quote": "Add a camera walkthrough"},
                                {"kind": "in_frame", "camera": "WalkCamera", "frame": 1, "names": ["Doorway_S"],
                                 "quote": "starts outside the doorway"},
                                {"kind": "in_frame", "camera": "WalkCamera", "frame": 120,
                                 "names": ["Chest", "Pillar_1"], "quote": "ends inside the room"}],
                            "deliverable": "CameraWalkthrough", "prefix": "WC", "role": "animation", "steps": 15,
                            "requirements": [{"quote": "Add a camera walkthrough that starts outside the "
                                              "doorway and ends inside the room, 120 frames at 24 fps",
                                              "do": "animate WalkCamera"}]}]}
        live = {"objects": {"Light": {"type": "LIGHT", "box": None}, "Camera": {"type": "CAMERA", "box": None},
                            "Ground": {"type": "MESH", "box": [[0, 0, -0.3], [16, 16, 0.0]]},
                            "Doorway_S": {"type": "MESH", "box": None}, "Chest": {"type": "MESH", "box": None},
                            "Pillar_1": {"type": "MESH", "box": None}},
                "materials": [], "names": ["Light", "Camera", "Ground", "Doorway_S", "Chest", "Pillar_1"]}
        plan = orchestrator.normalise_wave(wave)
        plan, repairs = orchestrator.repair_wave(plan, self.WORDS, live)
        self.assertEqual(plan["workers"][0]["aspects"], ["camera"])
        self.assertTrue(any("aspects" in r and "dropped" in r for r in repairs), repairs)
        self.assertEqual(orchestrator.validate_wave(plan, self.WORDS, live), [], "accepted, nothing left refused")


class Round4EvidenceFixturesTest(unittest.TestCase):
    """Real refused delegate calls from round 4 (2026-09-28, GLM orchestrator; out/evals/20260928-050245
    and .../20260928-063457, both dungeon_modules.conversation), field names, values and quotes verbatim.
    Cross-checked separately (not a test, a script over the extracted delegate arguments of every
    refused call in both run folders, 2 reps x 6 tasks): 34 delegate calls were refused; after this
    round's repairs, 32 of 34 now validate with no errors and the other 2 are refused only for what
    genuinely still needs fixing (both fall under the still-refused list: a wave that can't run)."""

    WORDS = ('Build a small low-poly dungeon kit and assemble a room from it. Make four reusable '
             'modules on a 2 m grid: a floor tile (2 x 2 m), a wall segment, a doorway segment and a '
             'pillar, each modelled once and reused as linked duplicates sharing one mesh. Assemble '
             'one enclosed room of at least 4 x 4 tiles with exactly one doorway, put a treasure '
             'chest in the centre and a few glowing crystals around the room. Name every object by '
             'its module (Floor, Wall, Doorway, Pillar, Chest, Crystal). Add a camera walkthrough '
             'that starts outside the doorway and ends inside the room, 120 frames at 24 fps. Set '
             'the scene up for Cycles and export everything as dungeon.glb next to this .blend '
             'file, for Godot.\n'
             'Using the same modules, add a connected treasure room through a second doorway, without '
             'changing anything else in the original room (the wall segment where the rooms connect '
             'becomes the doorway).')

    def test_dims_and_exists_checks_double_nested_under_their_own_kind_key_are_unwrapped(self):
        """out/evals/20260928-063457/dungeon_modules.conversation: GLM had already written every
        check's real fields flat and correct, but also duplicated them nested under a key named for
        the check's own kind ({"kind": "dims", "dims": {...}}); refused for four counts of "not a
        field of ...". The duplicate is noise once unwrapped; nothing else needed fixing."""
        wave = {"shared": {"notes": "Grid: cell (i,j) centre at (2i, 2j, 0); floor top z=0. Keep meshes "
                           "low-poly (<1500 tris each), stone grey palette. Model each module once; reuse "
                           "will be linked duplicates."},
                "workers": [{"id": "dungeonmodules", "role": "modeling", "prefix": "DungeonModules",
                            "deliverable": "DungeonModules", "aspects": [], "bounds": [[-3, -3, 0], [3, 3, 3]],
                            "steps": 30,
                            "requirements": [
                                {"do": "Floor: exactly 2x2 m tile, thin slab ~0.2 m thick, top at z=0 (origin "
                                 "at centre of top face). Stone look.",
                                 "quote": "a floor tile (2 x 2 m), a wall segment, a doorway segment and a pillar"},
                                {"do": "Wall: segment filling a 2 m cell: 2 m wide, 2 m tall from floor "
                                 "(z=0..2), 0.2 m thick, bevelled stone-block relief, low-poly.",
                                 "quote": "a wall segment"},
                                {"do": "Doorway: 2 m wide x 2 m tall x 0.2 m wall with arched opening ~1 m "
                                 "wide x 1.8 m tall centred, fits the grid cell like the wall.",
                                 "quote": "a doorway segment"},
                                {"do": "Pillar: vertical column ~0.4 m square, base+cap plates, 2 m tall "
                                 "resting on floor (z=0..2).", "quote": "and a pillar"}],
                            "checks": [
                                {"kind": "dims", "name": "Floor", "size": [2, 2, None], "tol": 0.02,
                                 "dims": {"name": "Floor", "size": [2, 2, None], "tol": 0.02},
                                 "quote": "a floor tile (2 x 2 m), a wall segment, a doorway segment and a pillar"},
                                {"kind": "exists", "name": "Wall", "exists": {"name": "Wall", "type": "MESH"},
                                 "quote": "a wall segment"},
                                {"kind": "exists", "name": "Doorway",
                                 "exists": {"name": "Doorway", "type": "MESH"}, "quote": "a doorway segment"},
                                {"kind": "exists", "name": "Pillar", "exists": {"name": "Pillar", "type": "MESH"},
                                 "quote": "and a pillar"}]}]}
        live = {"objects": {}, "materials": [], "names": []}
        plan = orchestrator.normalise_wave(wave)
        plan, repairs = orchestrator.repair_wave(plan, self.WORDS, live)
        self.assertTrue(any("fields aliased" in r for r in repairs), repairs)
        for check in plan["workers"][0]["checks"]:
            self.assertNotIn(check["kind"], check, "the duplicate wrapper is gone")
        self.assertEqual(orchestrator.validate_wave(plan, self.WORDS, live), [])

    def test_flat_bounds_and_an_undeclared_support_are_repaired_the_genuine_collision_stays_refused(self):
        """out/evals/20260928-050245/dungeon_modules.conversation: a dry run with 25 problems, most of
        them the checks' fields nested under "contact"/"dims"/"count" (unwrapped), both workers'
        bounds given as flat [x0,y0,z0,x1,y1,z1] lists (reshaped), and treasureroom.rests_on "Floor"
        never declared in shared.supports (its measured top_z, 0.0, added). What is left is real: the
        doorway worker's deliverable, "Doorway", already names a live object it does not own."""
        wave = {"workers": [
            {"id": "treasureroom", "role": "modeling", "prefix": "TreasureRoom", "deliverable": "TreasureRoom",
             "owns": [], "rests_on": "Floor", "steps": 40,
             "bounds": [3.8, -4.2, 0, 12.2, 4.2, 3.2],
             "requirements": [
                 {"do": "Build a 4x4-tile treasure room, outer bounds x 4.2..12.2, y -4.2..4.2, floor top "
                  "z=0, wall height 3 m, using LINKED DUPLICATES of the existing module meshes Floor, "
                  "Wall, Pillar.", "quote": "add a connected treasure room"},
                 {"do": "Contents, all named with TreasureRoom_ prefix: a chest at room centre resting on "
                  "the centre floor tile, and 3-4 glowing crystals around the room.",
                  "quote": "put a treasure chest in the centre"},
                 {"do": "Do NOT move, edit or delete any existing object in the original room.",
                  "quote": "without changing anything else in the original room"}],
             "checks": [
                 {"kind": "dims", "quote": "a floor tile (2 x 2 m)",
                  "dims": {"name": "TreasureRoom_FloorCenter", "size": [2, 2, 0.2], "tol": 0.1}},
                 {"kind": "count", "quote": "a connected treasure room",
                  "count": {"collection": "TreasureRoom", "min": 10, "max": 40, "type": "MESH"}},
                 {"kind": "contact", "quote": "put a treasure chest in the centre",
                  "contact": {"a": "TreasureRoom_Chest", "b": "TreasureRoom_FloorCenter", "relation": "rests_on"}}]},
            {"id": "doorway", "role": "modeling", "prefix": "TreasureDoorway", "deliverable": "Doorway",
             "owns": ["Wall.007"], "steps": 8, "bounds": [3.6, -2.2, 0, 4.4, 0.2, 3.2],
             "requirements": [
                 {"do": "Replace the existing east-wall segment Wall.007 with a LINKED DUPLICATE of the "
                  "existing Doorway module mesh, so the passage opening is 1.2 x 2.2 m.",
                  "quote": "the wall segment where the rooms connect becomes the doorway"}],
             "checks": [
                 {"kind": "exists", "quote": "the wall segment where the rooms connect becomes the doorway",
                  "exists": {"name": "Wall.007", "type": None}},
                 {"kind": "dims", "quote": "a doorway segment",
                  "dims": {"name": "Wall.007", "size": [0.4, 2, 3], "tol": 0.15}},
                 {"kind": "contact", "quote": "the wall segment where the rooms connect becomes the doorway",
                  "contact": {"a": "Wall.007", "b": "Wall.005", "relation": "touches"}}]}]}
        live = {"objects": {"Floor": {"type": "MESH", "box": [[-1, -1, -0.2], [17, 17, 0.0]]},
                            "Doorway": {"type": "MESH", "box": None}, "Wall.007": {"type": "MESH", "box": None}},
                "materials": [], "names": ["Floor", "Doorway", "Wall.007", "Wall.005"]}
        plan = orchestrator.normalise_wave(wave)
        plan, repairs = orchestrator.repair_wave(plan, self.WORDS, live)
        self.assertEqual(plan["workers"][0]["bounds"], [[3.8, -4.2, 0], [12.2, 4.2, 3.2]], "reshaped, not dropped")
        self.assertIn({"name": "Floor", "top_z": 0.0}, plan["shared"]["supports"], "Floor's top measured, not guessed")
        self.assertTrue(any("fields aliased" in r for r in repairs), repairs)
        errors = orchestrator.validate_wave(plan, self.WORDS, live)
        self.assertEqual(errors, ["workers[1] (doorway).deliverable: 'Doorway' is already in the scene; to "
                                  "rework it, list it in owns; for a new one, give it a new name"],
                         "the one refusal left is real: the second worker never claimed the object it renames")


class ClaudeAcceptedFixturesTest(unittest.TestCase):
    """Claude's round-1 briefs (out/evals/20260927-221551/perfume_ad.conversation) were accepted
    first try, no dry_run needed: the real delegate arguments below, verbatim. Both workers validate
    with no refusals and no repairs: no quote fixes, no alias, no sanitised prefix, nothing to snap."""

    WORDS = ('Use the perfume bottle and its box in this scene to create a luxury product photograph, '
             'set up for a final-quality Cycles render. Keep the bottle, the box and their materials '
             'unchanged. Place them on dark polished stone with soft reflections, light them with a '
             'warm spotlight, and put a subtle amber gradient in the background. Set up three cameras: '
             'a hero shot, a close-up, and an overhead composition, and leave the hero camera active. '
             'Output 1080x1080 PNG.')

    LIVE = {"objects": {"verre": {"type": "MESH", "box": None}, "ouverture": {"type": "MESH", "box": None},
                        "liquide": {"type": "MESH", "box": None}, "bouchon": {"type": "MESH", "box": None},
                        "boite": {"type": "MESH", "box": None}, "Light": {"type": "LIGHT", "box": None},
                        "Cam_Hero": {"type": "CAMERA", "box": None},
                        "Stone_Slab": {"type": "MESH", "box": [[-0.8, -0.6, -0.06], [0.8, 0.6, 0.0]]}},
            "materials": ["Stone_DarkPolished"],
            "names": ["verre", "ouverture", "liquide", "bouchon", "boite", "Light", "Cam_Hero", "Stone_Slab",
                     "Stone_DarkPolished"]}

    SHARED = {"keep": {"names": ["verre", "bouchon", "liquide", "boite", "ouverture"],
                       "quote": "Keep the bottle, the box and their materials unchanged"},
              "supports": [{"name": "Stone_Slab", "top_z": 0.0}], "palette": ["Stone_DarkPolished"],
              "units": "meters", "notes": "Luxury perfume product photo, Cycles, square 1080x1080."}

    LIGHTING_WORKER = {
        "id": "lighting", "deliverable": "Lit_Spot", "role": "lighting", "prefix": "Lit", "owns": ["Light"],
        "aspects": ["lighting"],
        "requirements": [
            {"quote": "light them with a warm spotlight", "do": "Key light is one warm SPOT light named "
             "Lit_Spot aimed at the bottle and box."},
            {"quote": "a subtle amber gradient in the background", "do": "A new world named Lit_World, "
             "assigned to the scene: a dark-to-amber gradient behind the products."},
            {"quote": "Keep the bottle, the box and their materials unchanged",
             "do": "Change nothing on verre, bouchon, liquide, boite, ouverture or their materials."}],
        "checks": [{"kind": "exists", "name": "Lit_Spot", "quote": "light them with a warm spotlight"},
                   {"kind": "setting", "path": "world", "equals": "Lit_World",
                    "quote": "a subtle amber gradient in the background"},
                   {"kind": "unchanged", "names": ["verre", "bouchon", "liquide", "boite", "ouverture"],
                    "quote": "Keep the bottle, the box and their materials unchanged"}]}

    CAMERAS_WORKER = {
        "id": "cameras", "deliverable": "Cam_Hero", "role": "camera", "prefix": "Shot", "owns": ["Cam_Hero"],
        "aspects": ["camera"],
        "requirements": [
            {"quote": "Set up three cameras: a hero shot, a close-up, and an overhead composition",
             "do": "Exactly three cameras: reframe the existing Cam_Hero, create Shot_CloseUp and "
                  "Shot_Overhead."},
            {"quote": "a hero shot", "do": "Cam_Hero: bottle and box together, low front three-quarter view."},
            {"quote": "a close-up", "do": "Shot_CloseUp: tight on the bottle."},
            {"quote": "an overhead composition", "do": "Shot_Overhead: looking straight down at bottle and "
             "box centred together."},
            {"quote": "leave the hero camera active", "do": "scene.camera = Cam_Hero when done."}],
        "checks": [{"kind": "count", "prefix": "Shot", "min": 2, "max": 2, "quote": "Set up three cameras"},
                   {"kind": "exists", "name": "Shot_CloseUp", "quote": "a close-up"},
                   {"kind": "exists", "name": "Shot_Overhead", "quote": "an overhead composition"},
                   {"kind": "in_frame", "camera": "Cam_Hero", "names": ["verre", "boite"], "min_inside": 1.0,
                    "quote": "a hero shot"},
                   {"kind": "in_frame", "camera": "Shot_CloseUp", "names": ["verre"], "min_inside": 0.95,
                    "quote": "a close-up"},
                   {"kind": "in_frame", "camera": "Shot_Overhead", "names": ["verre", "boite"],
                    "min_inside": 1.0, "quote": "an overhead composition"},
                   {"kind": "setting", "path": "camera", "equals": "Cam_Hero",
                    "quote": "leave the hero camera active"}]}

    def test_both_workers_validate_with_no_refusals_and_no_repairs(self):
        wave = {"shared": self.SHARED, "workers": [self.LIGHTING_WORKER, self.CAMERAS_WORKER]}
        plan = orchestrator.normalise_wave(wave)
        plan, repairs = orchestrator.repair_wave(plan, self.WORDS, self.LIVE)
        self.assertEqual(repairs, [], "a clean brief needs no repairs")
        self.assertEqual(orchestrator.validate_wave(plan, self.WORDS, self.LIVE), [])


class LayoutTest(unittest.TestCase):
    def layout(self, wave: dict):
        return orchestrator.layout(orchestrator.normalise_wave(wave), LIVE, fake_camera)

    def test_the_reference_layout_has_no_problems_and_says_its_numbers(self):
        problems, repairs, warnings, notes = self.layout(reference_wave())
        self.assertEqual((problems, repairs), ([], []))
        self.assertIn("armchair: base at 0.015 on Rug (top_z 0.015)", notes)
        self.assertIn("armchair c2: Armchair's box is 100% inside the frame of Camera", notes)

    def test_overlapping_planned_bounds_are_a_problem_with_the_overlap(self):
        wave = reference_wave()
        wave["workers"][1]["bounds"] = [[-0.3, 0.5, 0.015], [0.3, 1.1, 0.57]]
        problems, _, _, _ = self.layout(wave)
        self.assertEqual(problems, ["armchair and table: planned bounds overlap by 0.20 x 0.50 x 0.55 m; move one "
                                    "apart or shrink it"])

    def test_a_base_off_its_support_within_5cm_is_snapped_not_refused(self):
        """M4: most refused delegate calls were small mistakes like this; a repair, not a refusal."""
        wave = reference_wave()
        wave["workers"][1]["bounds"] = [[0.2, 0.2, 0.0], [0.7, 0.7, 0.57]]  # 15 mm off: within Z0_SNAP.
        plan = orchestrator.normalise_wave(wave)
        problems, repairs, _, _ = orchestrator.layout(plan, LIVE, fake_camera)
        self.assertEqual(problems, [])
        self.assertEqual(repairs, ["table: bounds z0 0.000 snapped to 0.015 (rests on Rug, was -15 mm off)"])
        self.assertEqual(plan["workers"][1]["bounds"][0][2], 0.015, "the wave itself is corrected")

    def test_a_base_too_far_off_its_support_is_still_refused(self):
        wave = reference_wave()
        wave["workers"][1]["bounds"] = [[0.2, 0.2, -0.2], [0.7, 0.7, 0.57]]  # 215 mm off: past Z0_SNAP.
        problems, repairs, _, _ = self.layout(wave)
        self.assertEqual(problems, ["table: bounds z0 -0.200 but it rests on Rug, top_z 0.015 (-215 mm); too far to "
                                    "snap, set z0 to 0.015"])
        self.assertEqual(repairs, [])

    def test_a_camera_that_cannot_frame_the_planned_boxes_is_a_problem(self):
        """M0: a camera that could not frame its own spec's layout."""
        wave = reference_wave()
        wave["workers"][0]["bounds"] = [[-1.4, 0.2, 0.015], [-0.6, 1.0, 0.95]]
        problems, _, _, _ = self.layout(wave)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("armchair c2: Armchair's box is 50% inside the frame of Camera (needs 100%); corners past left",
                      problems[0])
        region = reference_wave()
        region["workers"][0]["checks"][1]["region"] = [0.5, 0.0, 1.0, 1.0]
        problems, _, _, _ = self.layout(region)
        self.assertIn("0% inside the region of Camera", problems[0])

    def test_an_in_frame_check_at_a_frame_projects_at_that_frame(self):
        wave = reference_wave()
        wave["workers"][0]["checks"][1]["frame"] = 120
        frames = []

        def project(camera, points, frame=None):
            frames.append(frame)
            return fake_camera(camera, points)
        problems, _, _, _ = orchestrator.layout(orchestrator.normalise_wave(wave), LIVE, project)
        self.assertEqual((problems, frames), ([], [120]))
        self.assertEqual(workers.check_sentence(wave["workers"][0]["checks"][1]),
                         "?: Armchair 100% inside camera Camera's frame, at frame 120 (user: \"the camera should show "
                         "the whole set\")")
        light = {"id": "c1", "kind": "light", "name": "Key", "type": "SPOT", "energy": [100, None],
                 "spot_size": [30, 45], "quote": "a spot"}
        self.assertEqual(workers.check_sentence(light),
                         "c1: light Key: a spot, energy 100.. W, spot_size 30..45 deg (user: \"a spot\")")

    def test_relation_checks_read_as_sentences(self):
        hanging = {"id": "c3", "kind": "suspended", "a": "Sign", "a_part": "material:Wood", "b": "Sign",
                   "b_part": "material:Gold", "quote": "a hanging sign"}
        self.assertEqual(workers.check_sentence(hanging),
                         "c3: Sign[material:Wood] hangs from Sign[material:Gold]: attached in its top half, its lowest "
                         "point at least 0.05 m above the ground (z 0) ([...]: the faces of that object with that "
                         "material, vertex group or attribute) (user: \"a hanging sign\")")
        self.assertEqual(workers.check_sentence({"id": "c4", "kind": "above", "a": "Lamp", "b": "Table", "gap": 0.3,
                                                 "quote": "x"}), "c4: Lamp is above Table, over it, at least 0.3 m apart "
                                                                 "(user: \"x\")")

    def test_a_prefix_count_says_the_name_itself_counts(self):
        """Workers read "objects named PotionBottleOne_*" as excluding the one mesh named PotionBottleOne
        their brief asked for (20260929-230207 W0002-W0004); the matcher counts it (startswith, as
        ownership.prefixed), so the sentence says so, in the words the result uses."""
        count = {"id": "c2", "kind": "count", "prefix": "PotionBottleOne", "type": "MESH", "min": 1, "max": 1,
                 "quote": "a separate mesh"}
        self.assertEqual(workers.check_sentence(count),
                         "c2: at least 1 and at most 1 objects named PotionBottleOne or starting with PotionBottleOne "
                         "(MESH) (user: \"a separate mesh\")")

    def test_a_camera_made_in_this_wave_is_checked_after_the_wave(self):
        """M4: refused in the wild even though a camera another worker of this wave builds is meant
        to be deferred to the live run after the merge, never refused."""
        wave = reference_wave()
        wave["workers"][0]["checks"][1]["camera"] = "Shot"
        problems, repairs, _, notes = self.layout(wave)
        self.assertEqual((problems, repairs), ([], []))
        self.assertIn("armchair c2: camera 'Shot' is not in the scene yet; framing is checked after the wave", notes)

    def test_a_slight_overlap_between_modeling_neighbours_is_a_warning_not_a_refusal(self):
        wave = reference_wave()
        wave["workers"][1]["bounds"] = [[-0.11, 0.2, 0.015], [0.39, 0.7, 0.57]]  # 1 cm into the armchair's box.
        problems, repairs, warnings, _ = self.layout(wave)
        self.assertEqual((problems, repairs), ([], []))
        self.assertTrue(any("armchair and table" in w and "not refused" in w for w in warnings), warnings)

    def test_lighting_and_camera_worker_boxes_are_exempt_from_overlap(self):
        """M4: refused in the wild between a lighting worker's room-sized throw and a camera worker's
        frustum box, both standing for something non-physical; only modeling boxes are checked."""
        wave = reference_wave()
        wave["workers"].append({"id": "lit", "deliverable": "Lit", "role": "lighting", "prefix": "Lit",
                                "aspects": ["lighting"],
                                "requirements": [{"quote": "a lamp on the side table", "do": "light the set"}],
                                "bounds": [[-2, -1, 0], [2, 2, 3]]})  # Room-sized: overlaps everything.
        wave["workers"].append({"id": "cam", "deliverable": "Shot", "role": "camera", "prefix": "Shot",
                                "aspects": ["camera"],
                                "requirements": [{"quote": "the camera should show the whole set", "do": "frame it"}],
                                "bounds": [[-3, -3, 0], [3, 3, 3]]})  # A frustum-sized box, same story.
        problems, repairs, warnings, _ = self.layout(wave)
        self.assertEqual((problems, repairs), ([], []))
        self.assertFalse([w for w in warnings if "Lit" in w or "Shot" in w], warnings)


class ScatteredBoundsTest(unittest.TestCase):
    """Props spread over a room: one box around them all overlapped everything, so bounds may be a box
    per prop, and the dry run checks each and says every number it checked."""

    @staticmethod
    def wave(boxes) -> dict:
        wave = reference_wave()
        wave["workers"].append({"id": "books", "deliverable": "Books", "role": "modeling", "prefix": "Books",
                                "requirements": [{"quote": "a side table", "do": "books"}], "bounds": boxes,
                                "rests_on": "Rug", "checks": [{"kind": "in_frame", "camera": "Camera", "names": ["Books"],
                                                               "quote": "the camera should show the whole set"}]})
        return wave

    def test_bounds_may_be_a_box_per_prop_and_the_dry_run_checks_each(self):
        boxes = [[[-1.8, -0.9, 0.015], [-1.6, -0.7, 0.2]], [[0.9, -0.9, 0.0], [1.1, -0.7, 0.2]],
                 [[-0.5, 0.5, 0.015], [-0.3, 0.7, 0.2]], [[0.8, 1.2, 0.015], [1.4, 1.4, 0.2]]]
        wave = self.wave(boxes)
        self.assertEqual(refusals(wave), [])
        problems, repairs, warnings, notes = orchestrator.layout(orchestrator.normalise_wave(wave), LIVE, fake_camera)
        self.assertEqual(repairs, ["books box 2: bounds z0 0.000 snapped to 0.015 (rests on Rug, was -15 mm off)"])
        self.assertEqual(problems, [
            "armchair and books box 3: planned bounds overlap by 0.20 x 0.20 x 0.18 m; move one apart or shrink it",
            "books c1: Books's box 1 is 0% inside the frame of Camera (needs 100%); corners past left. Move the camera "
            "(frame_camera report_only measures it) or the planned bounds",
            "books c1: Books's box 2 is 50% inside the frame of Camera (needs 100%); corners past right. Move the camera "
            "(frame_camera report_only measures it) or the planned bounds",
            "books c1: Books's box 4 is 50% inside the frame of Camera (needs 100%); corners past right. Move the camera "
            "(frame_camera report_only measures it) or the planned bounds"])
        for note in ("books box 1: planned x -1.8..-1.6, y -0.9..-0.7, z 0.015..0.2", "books: bases at 0.015, 0.015, "
                     "0.015, 0.015 on Rug (top_z 0.015)", "table and books: 0.500 m apart at the closest",
                     "armchair and table: 0.300 m apart at the closest",
                     "books c1: Books's box 3 is 100% inside the frame of Camera", "armchair: planned x -0.9..-0.1, "
                     "y 0.2..1, z 0.015..0.95"):
            self.assertIn(note, notes)
        self.assertFalse([n for n in notes if n.startswith("armchair and books")], "a pair that overlaps is a problem only")
        self.assertEqual(workers._bounds(boxes[:2]), "box 1: x -1.8..-1.6, y -0.9..-0.7, z 0.015..0.2; "
                                                     "box 2: x 0.9..1.1, y -0.9..-0.7, z 0..0.2")
        self.assertEqual(workers._bounds(boxes[0]), "x -1.8..-1.6, y -0.9..-0.7, z 0.015..0.2")

    def test_more_than_eight_boxes_or_a_malformed_one_is_refused(self):
        box = [[0, 0, 0.015], [0.1, 0.1, 0.1]]
        self.assertIn("up to 8 such boxes", " ".join(refusals(self.wave([box] * 9))))
        self.assertIn("up to 8 such boxes", " ".join(refusals(self.wave([box, [[0, 0, 1], [0.1, 0.1, 0.5]]]))))
        schema = orchestrator.SCHEMAS[0]["function"]["parameters"]["properties"]["wave"]["properties"]
        self.assertIn("or up to 8 (one per prop)", schema["workers"]["items"]["properties"]["bounds"]["description"])

    def test_the_dry_run_says_whether_a_real_delegate_would_be_refused_and_the_numbers(self):
        text = orchestrator.dry_run_report(["books box 2: bounds z0 0.000 ..."],
                                           ["books: prefix 'books' sanitised to 'Books'"],
                                           ["books: 'a lamp' needs a light"],
                                           ["books: bases at 0.015 on Rug (top_z 0.015)"])
        self.assertEqual(text.splitlines(), [
            "Dry run, nothing started: a real delegate would be refused (1 problem).",
            "Repairs: books: prefix 'books' sanitised to 'Books'",
            "- refused: books box 2: bounds z0 0.000 ...",
            "- not refused (warning): books: 'a lamp' needs a light",
            "Numbers checked:", "  books: bases at 0.015 on Rug (top_z 0.015)"])
        self.assertTrue(orchestrator.dry_run_report([], [], [], ["n"]).startswith(
            "Dry run, nothing started: a real delegate would start.\nNumbers checked:"))
        self.assertIn("The layout is checked once these are fixed.",
                      orchestrator.dry_run_report(["workers[0].id: ..."], [], [], [], layout_checked=False))


class RoleTypesTest(unittest.TestCase):
    """What a worker may create: its role's types, empties for every role, and its aspects' types."""

    @staticmethod
    def made(id_type: str, kind: str = ""):
        return types.SimpleNamespace(id_type=id_type, type=kind)

    def test_every_role_may_create_empties_and_aspects_add_their_types(self):
        from loopcut import ownership
        empty, light, camera = self.made("OBJECT", "EMPTY"), self.made("OBJECT", "LIGHT"), self.made("OBJECT", "CAMERA")
        for role in ownership.ROLES:
            self.assertTrue(ownership.allowed(role, empty), f"{role}: a pivot or aim empty")
        self.assertFalse(ownership.allowed("modeling", light), "a modeling worker's light is quarantined...")
        self.assertTrue(ownership.allowed("modeling", light, ["lighting"]), "...unless its brief has aspect lighting")
        self.assertTrue(ownership.allowed("modeling", self.made("LIGHT"), ["lighting"]), "the light's data too")
        self.assertTrue(ownership.allowed("materials", self.made("WORLD"), ["lighting"]))
        self.assertFalse(ownership.allowed("lighting", camera))
        self.assertTrue(ownership.allowed("lighting", camera, ["camera"]))
        self.assertFalse(ownership.allowed("camera", light, ["camera"]), "the camera aspect grants cameras only")
        self.assertTrue(ownership.allowed("camera", self.made("OBJECT", "CAMERA")))

    def test_a_requirement_that_needs_a_type_the_worker_cannot_create_is_warned_about(self):
        wave = orchestrator.normalise_wave(reference_wave())
        self.assertEqual(orchestrator.role_warnings(wave), [])
        wave["workers"][1]["requirements"].append({"quote": "a lamp", "do": "a lamp with a warm bulb light in the shade"})
        warnings = orchestrator.role_warnings(wave)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("table: 'bulb light' needs a light, which a modeling worker can't create", warnings[0])
        self.assertIn("give it aspect lighting", warnings[0])
        wave["workers"][1]["aspects"] = ["lighting"]
        self.assertEqual(orchestrator.role_warnings(wave), [], "aspect lighting lets it make the light")
        wave["workers"][0]["requirements"] = [{"quote": "an armchair", "do": "armchair in a light grey fabric"},
                                              {"quote": "an armchair", "do": "add a camera for the shot"}]
        warnings = orchestrator.role_warnings(wave)
        self.assertEqual([w.split(":")[0] for w in warnings], ["armchair"], "light grey is a colour, not a light")
        self.assertIn("needs a camera", warnings[0])


class PromptTest(unittest.TestCase):
    def test_the_orchestrators_fixed_cost_stays_under_10545_tokens(self):
        """Service prompt size plus the public schemas stays within the fixed-cost cap."""
        from prompt_index_fixture import built_index
        index = built_index()
        fixed = context.fixed_tokens("", agent._orchestrator_schemas(), prompt_id="orchestrator", index=index)
        self.assertLessEqual(fixed, 10545)  # 10,534 on 2026-10-17 with the captures' labels switch.
        self.assertLessEqual(fixed, context.fixed_tokens(agent.SYSTEM_PROMPT, TOOL_SCHEMAS()) * 1.3 + 200)


    def test_the_delegate_schema_says_the_limits_it_refuses_by(self):
        """Every check kind with its fields (optional ones marked), every setting path, the notes cap and
        the counts: said up front instead of learnt from refusals."""
        from loopcut import acceptance
        wave = orchestrator.SCHEMAS[0]["function"]["parameters"]["properties"]["wave"]["properties"]
        worker = wave["workers"]["items"]["properties"]
        text = worker["checks"]["description"]
        segments = {part.split(" ")[0]: part for part in text.split(": ", 1)[1].split("; ")}
        self.assertEqual(set(segments), set(acceptance.KINDS))
        for kind, (required, optional) in acceptance.KINDS.items():
            words = segments[kind].replace("|", " | ").replace("(", " ").split()
            for field in required:
                self.assertIn(field, words, f"{kind}: {field}")
            for field in optional:
                if (kind, field) in (("unchanged", "tokens"), ("preserved", "baseline")):  # Loopcut's own.
                    continue
                self.assertTrue(f"{field}?" in words or field in words and "|" in words, f"{kind}: {field}?")
        render = text.split("render.{", 1)[1].split("}", 1)[0].split(",")
        for path in acceptance.SETTINGS:
            self.assertTrue(path[len("render."):] in render if path.startswith("render.") else f" {path}," in text
                            or f" {path})" in text, path)
        self.assertIn(f"notes: at most {orchestrator.MAX_NOTES} characters", wave["shared"]["description"])
        self.assertEqual(worker["checks"]["maxItems"], orchestrator.MAX_CHECKS)
        self.assertNotIn("maxItems", wave["workers"], "any number of briefs: past the limit they queue")
        self.assertNotIn("steps", worker)
        self.assertIn("letters and digits", worker["prefix"]["description"])

    def test_the_workers_fixed_cost_stays_under_6255_tokens(self):
        """Service worker prompt size and public schemas stay within the worker cap."""
        from prompt_index_fixture import built_index
        fixed = context.fixed_tokens("", workers.schemas(), prompt_id="worker", index=built_index())
        self.assertLessEqual(fixed, 6255)  # 6,243 on 2026-10-17 with capture_viewport's labels.

    def test_uv_and_texture_tools_reach_every_role_and_run_in_the_child(self):
        from loopcut import tools, worker_host
        names = {s["function"]["name"] for s in workers.schemas()}
        for name in ("inspect_uv", "uv_unwrap", "inspect_texture"):
            self.assertIn(name, names)
            self.assertIn(name, worker_host.CHILD_TOOLS)
        self.assertIn("uv_unwrap", tools.CHANGES_SCENE)

    def test_the_worker_prompt_and_tools_are_the_same_for_every_role(self):
        spec = workers.SPEC("lighting", save=lambda s: None)
        self.assertEqual(spec.system({}), workers.SPEC("modeling", save=lambda s: None).system({}))
        names = [s["function"]["name"] for s in workers.schemas()]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("report_back", names)
        for absent in ("delegate", "render_job", "write_file", "inspect_scene", "see_render"):
            self.assertNotIn(absent, names)
        self.assertFalse(spec.asks or spec.checkpoints or spec.changes_card)


    def test_the_dollar_budget_bounds_priced_worker_usage_across_models(self):
        from loopcut import pricing, waves
        self.assertEqual((config.Roles.job_max_usd, config.Roles.worker_max_seconds), (0.40, 1800))
        budget = waves.JobBudget(config.Roles.job_max_usd, config.Roles.worker_max_seconds)
        usage = {"input": 1_000_000, "output": 0}
        budget.add("fast", usage)  # $0.15 of worker usage.
        self.assertEqual(budget.spent(), "")
        budget.add("pro", usage)  # A later wave's model is charged to the same job.
        self.assertAlmostEqual(budget.usd, pricing.usd("fast", usage) + pricing.usd("pro", usage))
        self.assertEqual(budget.spent(), "the job's $0.40 is used")

    def test_the_dry_run_says_the_dollars_left_and_each_workers_time(self):
        roles = types.SimpleNamespace(job_max_usd=0.4, worker_max_seconds=1800)
        plan = orchestrator.normalise_wave(reference_wave())
        self.assertEqual(orchestrator.budget_line(plan, roles),
                         "Budget: $0.400 of $0.40 left for this turn's workers; 1800 s per worker run.")
        job = types.SimpleNamespace(budget=types.SimpleNamespace(usd=0.15, max_usd=0.4, seconds=1800))
        line = orchestrator.budget_line(plan, roles, job)
        self.assertEqual(line, "Budget: $0.250 of $0.40 left for this turn's workers; 1800 s per worker run.")
        report = orchestrator.dry_run_report([], [], [], ["a: planned x"], budget=line)
        self.assertLess(report.index("Budget:"), report.index("Numbers checked:"))
        job.budget.usd = 0.5
        self.assertIn("$0.000 of $0.40 left", orchestrator.budget_line(plan, roles, job))
        roles.job_max_usd = 0
        self.assertEqual(orchestrator.budget_line(plan, roles), "Budget: 1800 s per worker run.")

    def test_old_brief_steps_are_silently_dropped_whatever_the_value(self):
        for value in (30, 1, 999, "no limit", None, {"invalid": True}):
            with self.subTest(value=value):
                wave = reference_wave()
                wave["workers"][0]["steps"] = value
                plan = orchestrator.normalise_wave(wave)
                self.assertNotIn("steps", plan["workers"][0])
                self.assertEqual(orchestrator.validate_wave(plan, USER, LIVE), [])

    def test_any_number_of_briefs_validates_and_the_schedule_says_who_queues(self):
        """GPT-6.1 Sol, 20260929-230207: seven props went as six and one, twice, because a call took six
        while three ran at once; the orchestrator did the scheduling. Now the queue is Loopcut's."""
        wave = {"shared": {}, "workers": [
            {"id": f"p{n}", "deliverable": f"Potion{n}", "role": "modeling", "prefix": f"Potion{chr(65 + n)}",
             "requirements": [{"quote": "an armchair", "do": "x"}]} for n in range(7)]}
        self.assertEqual(refusals(wave), [])
        plan = orchestrator.normalise_wave(wave)
        roles = types.SimpleNamespace(max_workers=3)
        self.assertEqual(orchestrator.schedule_line(plan, roles, {}),
                         "Schedule: p0, p1, p2 start now; p3, p4, p5, p6 queue in that order, each starting as a "
                         "worker finishes (3 run at once).")
        busy = {"a": {"state": "running"}, "b": {"state": "queued"}, "c": {"state": "finished"}}
        self.assertEqual(orchestrator.schedule_line(plan, roles, busy),
                         "Schedule: p0 start now; p1, p2, p3, p4, p5, p6 queue in that order behind 2 already "
                         "running or queued, each starting as a worker finishes (3 run at once).")
        plan["workers"] = plan["workers"][:2]
        self.assertEqual(orchestrator.schedule_line(plan, roles, {}), "Schedule: all 2 start now.")

    def approval(self, base_url, usd=0.4, seconds=1800):
        roles = types.SimpleNamespace(job_max_usd=usd, worker_max_seconds=seconds,
                                      worker=types.SimpleNamespace(base_url=base_url))
        with mock.patch.object(orchestrator, "_roles", return_value=roles):
            return orchestrator.describe("delegate", {"wave": reference_wave()})

    def test_hosted_approval_explains_allowance_and_keeps_internal_cost_in_details(self):
        summary, code = self.approval(config.SERVICE_BASE_URL)
        self.assertIn("Start 2 helpers", summary)
        self.assertIn("up to 30 minutes per helper", summary)
        self.assertIn("Uses your chat allowance, not credits.", summary)
        self.assertNotIn("$", summary)
        self.assertIn("run scripts without asking again", summary)
        self.assertIn("read and write your files", summary)
        self.assertIn("$0.40 shared by all helpers in this turn", code)
        self.assertIn("not a price per approval", code)
        self.assertIn('"Armchair"', code)
        self.assertEqual(orchestrator.describe("delegate", {"wave": reference_wave(), "dry_run": True})[0],
                         "Check a helper plan: Armchair, SideTable")

    def test_own_key_approval_labels_the_shared_api_spending_limit(self):
        summary, _ = self.approval("https://api.example.com/v1", usd=1.25, seconds=900)
        self.assertIn("API spending limit: $1.25 shared by all helpers in this turn.", summary)
        self.assertIn("up to 15 minutes per helper", summary)
        self.assertNotIn("chat allowance", summary)

    def test_approval_uses_the_helpers_provider(self):
        # The main loop can use a hosted plan while helpers use the user's key, or the reverse.
        for helper_url, hosted in ((config.SERVICE_BASE_URL, True), ("https://api.example.com/v1", False)):
            summary, _ = self.approval(helper_url)
            self.assertEqual("Uses your chat allowance" in summary, hosted)
            self.assertEqual("API spending limit" in summary, not hosted)
        summary, _ = self.approval(config.CHATGPT_PLAN_BASE_URL)
        self.assertIn("Uses your ChatGPT plan allowance.", summary)
        self.assertNotIn("$", summary)

    def test_approval_without_a_dollar_budget_explains_time_and_usage(self):
        for url in (config.SERVICE_BASE_URL, "https://api.example.com/v1"):
            summary, _ = self.approval(url, usd=0, seconds=900)
            self.assertIn("up to 15 minutes per helper", summary)
            self.assertNotIn("$", summary)
        self.assertIn("no API spending limit is set", summary)

    def test_a_dry_run_is_a_read_and_a_wave_asks_first(self):
        self.assertFalse(agent._changes_scene("delegate", json.dumps({"wave": {}, "dry_run": True})))
        self.assertTrue(agent._same_while_unchanged("delegate", json.dumps({"wave": {}, "dry_run": True})))
        self.assertEqual(agent._approval("delegate", json.dumps({"wave": {}}), ()), "gate")

    def test_waiting_for_the_running_workers_starts_nothing_so_it_is_not_asked(self):
        """The delegate that started them was approved; the wait only merges what they built."""
        self.assertEqual(agent._approval("delegate", json.dumps({"wait": True}), ()), "")
        self.assertEqual(orchestrator.describe("delegate", {"wait": True}), ("Wait for the helpers' next results", ""))
        self.assertEqual(agent._approval("delegate", json.dumps({"wait": True, "wave": reference_wave()}), ()), "gate",
                         "briefs start workers, wait or not")

    def test_a_wait_with_nothing_running_says_so_at_once(self):
        session = state.reset()
        self.addCleanup(state.reset)
        turn = agent.Turn()
        turn.roles = types.SimpleNamespace(orchestrate=True)
        session["turn"] = turn
        result = orchestrator.delegate(wait=True)
        self.assertEqual((result.text, result.ok, result.then), ("Nothing to wait for: no worker results are available.", True,
                                                                 None))
        refused = orchestrator.delegate()
        self.assertFalse(refused.ok)
        self.assertIn("or wait true, with no wave, for the running workers' next results", refused.text)

    def test_wait_reads_all_waves_of_the_latest_saved_job_without_claiming_completion(self):
        session = state.reset()
        self.addCleanup(state.reset)
        # Legacy cards have no explicit job field. Keep earlier results in the same job,
        # but do not pull unrelated helpers from an older job into the response.
        for wave, status, merged in (("2-1-w1", "failed", False), ("3-1-w1", "done", True),
                                     ("3-1-w2", "stopped", False)):
            row = state.wave_row(wave, wave, "modeling")
            row.update(status=status, merged=merged)
            session["items"].append(state.item_wave(wave, 1, [row]))
        result = orchestrator.delegate(wait=True)
        self.assertNotIn("2-1-w1", result.text)
        self.assertIn("3-1-w1", result.text)
        self.assertIn("completed and merged", result.text)
        self.assertIn("3-1-w2", result.text)
        self.assertIn("stopped before finishing (nothing merged)", result.text)
        self.assertNotIn("every worker has reported", result.text.lower())

    def test_the_brief_reads_as_fields_with_the_users_words(self):
        plan = orchestrator.normalise_wave(reference_wave())
        brief = {**plan["workers"][0], "steps": 20}
        text = workers.opening(brief, plan["shared"], [plan["workers"][1]], "<scene_context>\nx\n</scene_context>")
        for line in ("keep (the user's; never change): Bottle, Box (user: \"Keep the bottle and the box unchanged\")",
                     "support: Rug, top_z 0.015",
                     "prefix: Armchair: for materials and other internal/helper datablocks (Armchair_Name); "
                     "name parts for what they are",
                     "- armchair, seat about 0.45 m (user: \"an armchair\")",
                     "defaults (the orchestrator's choice, never checked): seat height 0.45 m; fabric in Pal_Wood tones",
                     "- c1: Armchair rests on Rug (user: \"an armchair and a side table on the rug\")",
                     "also in this wave (not yours): SideTable by table (modeling), planned x 0.2..0.7, y 0.2..0.7, z 0.015..0.57"):
            self.assertIn(line, text)
        self.assertNotIn("steps:", text)
        self.assertTrue(text.endswith("</scene_context>"))

    def test_a_workers_words_are_clipped(self):
        report = workers.parse_report({"summary": "One. Two. Three. Four.", "change_requests": [
            {"target": "Rug", "change": "raise it", "reason": "r"}] * 5 + [{"target": "", "change": "x"}], "question": ""})
        self.assertEqual(report["summary"], "One. Two. Three. …", "a marker says the worker said more")
        self.assertEqual(workers.parse_report({"summary": "One. Two."})["summary"], "One. Two.")
        self.assertEqual(len(report["change_requests"]), 3)
        self.assertIsNone(report["question"])
        self.assertLessEqual(len(workers.parse_report({"summary": "word " * 1000})["summary"]), workers.MAX_SUMMARY)
        # Eval: "said" was cut inside a number. Whole sentences while they fit, else between items.
        said = ("Lamp built at the table's back left corner. " + "Shade 0.42 m wide, base 0.18 m, pole 1.46 m, "
                "bulb at z 1.52, all on Pal_Brass, " * 5 + "done.")
        self.assertEqual(workers.parse_report({"summary": said})["summary"], "Lamp built at the table's back left corner. …")
        long_one = "Measured " + ", ".join(f"part {i} at ({i}.25, 0.5, 1.75) m" for i in range(20))
        clipped = workers.parse_report({"summary": long_one})["summary"]
        self.assertLessEqual(len(clipped), workers.MAX_SUMMARY)
        self.assertRegex(clipped, r"part \d+ at \(\d+\.25, 0\.5, 1\.75\) m…$", "cut after a whole item")

    def test_numbers_and_lists_are_clipped_between_items(self):
        numbers = {"Armchair": {"size": [0.78, 0.82, 0.94], "tris": 1840}, "Cushion": {"size": [0.5, 0.5, 0.1]}, "legs": 4}
        self.assertEqual(workers.clip_items(numbers, 60), '{"Armchair":{"size":[0.78,0.82,0.94],"tris":1840},…+2}')
        many = workers.clip_items({f"Part{i}": {"size": [1.123456] * 3} for i in range(50)}, 140)
        self.assertLessEqual(len(many), 140)
        self.assertTrue(many.endswith(',…+48}'), many)
        self.assertEqual(workers.clip_items({"a": 1}, 140), '{"a":1}')
        self.assertEqual(workers.clip_items({"Lamp": {"height": 1.52, "shade": {"top": 0.3, "bottom": 0.42}}}, 45),
                         '{"Lamp":{"height":1.52,…+1}}', "a nested dict is clipped the same way")
        self.assertEqual(workers.clip_list(["Lamp", "Lamp_Shade", "Lamp_Base", "MAT Lamp_Brass"], 30), "Lamp, Lamp_Shade, +2 more")
        self.assertEqual(workers.clip("Fix: move A by (0, 0, -0.012) m; then B", 30), "Fix: move A by (0, 0, -0.012)…")
        self.assertEqual(workers.clip("Fix: move A by (0, 0, -0.012) m; then B", 26), "Fix: move A by…", "not in a tuple")


class ArtDirectionTest(unittest.TestCase):
    """M4 quality audit 2026-09-28, item 3: brief quality tracked the planner. Muse's 118-character
    briefs got boxes; Claude's listed parts (staves, hoops, lock plate) and the worker built exactly
    those. Every worker now reads the user's own messages and the orchestrator's art direction."""

    def brief(self, **fields) -> dict:
        plan = orchestrator.normalise_wave(reference_wave())
        return {**plan["workers"][0], "steps": 30, **fields}, plan


    def test_the_brief_carries_the_style_and_the_users_whole_messages(self):
        brief, plan = self.brief(style="A 1950s club armchair: rolled arms, tufted back, tapered walnut legs.")
        text = workers.opening(brief, plan["shared"], [], "<scene_context>\nx\n</scene_context>",
                               ["Put an armchair on the rug.\n\nMake it cosy,  like my grandfather's.", "Now keep the bottle."])
        self.assertIn("style (art direction: the quality bar for your craft pass, not extra scope): A 1950s club "
                      "armchair: rolled arms, tufted back, tapered walnut legs.", text)
        shared = text.split("<shared>", 1)[1].split("</shared>", 1)[0]
        self.assertIn('- "Put an armchair on the rug. Make it cosy, like my grandfather\'s."\n- "Now keep the bottle."',
                      shared, "every message whole, oldest first, one line each")
        self.assertNotIn("style (", workers.opening(self.brief()[0], plan["shared"], [], "<scene_context>\n</scene_context>"))

    def test_the_users_words_keep_the_newest_messages_whole(self):
        older = [f"message {n} " + "word " * 300 for n in range(4)]
        said = workers.user_request(older + ["the newest one"])
        self.assertLessEqual(len(said), workers.MAX_USER_WORDS + 40)
        self.assertTrue(said.endswith('- "the newest one"'))
        self.assertTrue(said.startswith("(3 earlier messages left out)"), said[:60])
        long_one = workers.user_request(["x" * 10 + " word" * 2000])
        self.assertLessEqual(len(long_one), workers.MAX_USER_WORDS + 4, "a newest message too long alone is clipped")
        self.assertEqual(workers.user_request([]), "")

    def test_the_role_notes_come_from_the_craft_skills_when_they_are_built(self):
        brief, plan = self.brief()
        with mock.patch.dict(sys.modules, {"loopcut.skills": None}):
            with self.assertRaises(ImportError):
                workers.role_notes("modeling", brief)
        seen = []
        skills = types.ModuleType("loopcut.skills")
        skills.skills_for = lambda role, b, notes="", **options: seen.append((role, b["deliverable"])) or "Model a real one: parts list."
        with mock.patch.dict(sys.modules, {"loopcut.skills": skills}):
            text = workers.opening(brief, plan["shared"], [], "<scene_context>\n</scene_context>")
        self.assertIn("<role_notes>\nModel a real one: parts list.\n</role_notes>", text)
        self.assertEqual(seen, [("modeling", "Armchair")])

        def broken(role, b, notes="", **options):
            raise KeyError(role)
        for skills_for in (broken, lambda role, b, notes="", **options: ""):
            skills.skills_for = skills_for
            with mock.patch.dict(sys.modules, {"loopcut.skills": skills}):
                if skills_for is broken:
                    with self.assertRaises(KeyError):
                        workers.role_notes("camera", brief)
                else:
                    self.assertEqual(workers.role_notes("camera", brief), "")

    def test_a_style_given_in_the_wrong_shape_is_repaired(self):
        wave = reference_wave()
        wave["workers"][0]["style"] = ["mid-century", "walnut legs"]
        wave["workers"][1]["style"] = "slender " * 200
        plan, repairs = orchestrator.repair_wave(orchestrator.normalise_wave(wave), USER, LIVE)
        self.assertEqual(plan["workers"][0]["style"], "mid-century; walnut legs")
        self.assertLessEqual(len(plan["workers"][1]["style"]), orchestrator.MAX_STYLE)
        self.assertTrue(any("style: a list given" in r for r in repairs) and any("trimmed" in r for r in repairs))
        self.assertEqual(orchestrator.validate_wave(plan, USER, LIVE), [])
        wave["workers"][0]["style"] = 7
        self.assertIn("workers[0] (armchair).style: one text, at most 800 characters", refusals(wave))


class KeepConflictTest(unittest.TestCase):
    """M4 quality audit 2026-09-28, §5: a reveal worker was told to parent the kept product meshes; it
    spent two rollbacks, finished partial, and the orchestrator did the parenting itself. Nothing
    before the wave caught a brief that changes something kept."""

    KEPT = ["Camera_body", "Camera_lens", "Camera_strap"]
    WORDS = "Keep the product's geometry and materials unchanged. Create a reveal while the product slowly rotates."

    def wave(self, **worker) -> dict:
        return {"shared": {"keep": {"names": self.KEPT, "quote": "Keep the product's geometry and materials unchanged"}},
                "workers": [{"id": "reveal", "deliverable": "RevealAnimation", "role": "animation", "prefix": "Reveal",
                             "requirements": [{"quote": "while the product slowly rotates", "do": worker.pop("do")}],
                             **worker}]}

    def live(self) -> dict:
        return {"objects": {name: {"type": "MESH", "box": None} for name in self.KEPT}, "materials": [],
                "names": list(self.KEPT)}

    def refused(self, **worker) -> list:
        return [e for e in orchestrator.validate_wave(orchestrator.normalise_wave(self.wave(**worker)), self.WORDS,
                                                      self.live()) if "shared.keep keeps" in e]

    def test_the_round_4_reveal_brief_is_refused_with_the_way_out(self):
        errors = self.refused(do="Parent all Camera_* meshes under empty RevealAnimation at origin and animate slow "
                                 "Z rotation over frames 1-192")
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("workers[0] (reveal).requirements[0]: 'Parent all Camera_* meshes", errors[0])
        self.assertIn("changes Camera_* (Camera_body, Camera_lens, Camera_strap), which shared.keep keeps (user: "
                      "\"Keep the product's geometry and materials unchanged\")", errors[0])
        self.assertIn("take Camera_* (Camera_body, Camera_lens, Camera_strap) out of keep and list it in this "
                      "worker's owns", errors[0])

    def test_what_changes_a_kept_object_and_what_only_mentions_it(self):
        kept = {"Bottle", "Box"}
        for text in ("parent Bottle and Box under RevealRig", "Bottle rotates 360 degrees", "the Box is scaled to 0.4 m",
                     "recolour Bottle to deep blue", "animate the Bottle's rotation", "hide Box during frames 1-10"):
            self.assertTrue(orchestrator._changes_kept(text, kept), text)
        for text in ("lamp beside the Bottle, 0.3 m tall", "move the lamp next to Box", "parent the Lamp to Box",
                     "rotate the camera around the Bottle", "frame Bottle and Box, 50 mm lens", "resize the box lid",
                     "don't move Bottle", "leave Bottle as it is; move Lamp", "Key light aimed at Bottle, rotated 30 deg",
                     "do not alter product geometry or materials"):
            self.assertEqual(orchestrator._changes_kept(text, kept), "", text)

    def test_defaults_and_animated_checks_count_too(self):
        self.assertTrue(self.refused(do="an empty that turns", defaults=["rotate Camera_body 180 degrees"]))
        self.assertTrue(self.refused(do="an empty that turns", checks=[
            {"kind": "animated", "name": "Camera_body", "path": "rotation_euler", "frames": [1, 192],
             "quote": "while the product slowly rotates"}]))
        self.assertEqual(self.refused(do="an empty RevealAnimation that turns, the camera orbiting the product"), [])


class AdvisoryRepairTest(unittest.TestCase):
    """E2: the brief's own targets as advisory checks. Repaired, never refused: a wave is never held up
    by one, and a worker's status or the finish gate never sees them."""

    def repair(self, advisory=None, checks=None) -> tuple[dict, list]:
        wave = reference_wave()
        if advisory is not None:
            wave["workers"][0]["advisory"] = advisory
        if checks is not None:
            wave["workers"][0]["checks"] += checks
        return orchestrator.repair_wave(orchestrator.normalise_wave(wave), USER, LIVE)

    def test_the_brief_fills_in_the_name_and_the_planned_bounds(self):
        plan, repairs = self.repair([{"kind": "flat_shaded"}, {"kind": "inside_bounds"},
                                     {"kind": "triangles", "object": "Armchair", "max": 1000},
                                     {"kind": "dims", "name": "Armchair", "size": [None, None, 0.95], "tol": 0.03}])
        advisory = plan["workers"][0]["advisory"]
        self.assertEqual([(c["id"], c["kind"], c["name"]) for c in advisory],
                         [("a1", "flat_shaded", "Armchair"), ("a2", "inside_bounds", "Armchair"),
                          ("a3", "triangles", "Armchair"), ("a4", "dims", "Armchair")])
        self.assertEqual(advisory[1]["bounds"], [[[-0.9, 0.2, 0.015], [-0.1, 1.0, 0.95]]])
        self.assertEqual(repairs, [])
        self.assertEqual(orchestrator.validate_wave(plan, USER, LIVE), [])

    def test_an_advisory_kind_among_the_checks_moves_to_advisory(self):
        plan, repairs = self.repair(checks=[{"kind": "no_modifiers", "name": "Armchair", "quote": "an armchair"}])
        self.assertEqual(len(plan["workers"][0]["checks"]), 2)
        self.assertEqual(plan["workers"][0]["advisory"][0]["kind"], "no_modifiers")
        self.assertTrue(any("checks[2]: moved to advisory (no_modifiers Armchair)" in r for r in repairs), repairs)

    def test_a_bad_one_is_dropped_with_a_note_never_refused(self):
        plan, repairs = self.repair([{"kind": "triangles", "name": "Armchair"}, "flat please",
                                     {"kind": "bigness", "name": "Armchair"}]
                                    + [{"kind": "no_modifiers"}] * 9)
        self.assertEqual(len(plan["workers"][0]["advisory"]), acceptance.MAX_ADVISORY)
        for words in ("advisory[0]: dropped (triangles Armchair) - min or max: at least one",
                      "advisory[1]: dropped (?) - not a check object", "advisory[2]: dropped (bigness Armchair)",
                      f"advisory[11]: dropped (no_modifiers Armchair) - at most {acceptance.MAX_ADVISORY}"):
            self.assertTrue(any(words in r for r in repairs), (words, repairs))
        self.assertEqual(orchestrator.validate_wave(plan, USER, LIVE), [])
        plan, repairs = self.repair("flat shading")
        self.assertEqual(plan["workers"][0]["advisory"], [])
        self.assertIn("workers[0] (armchair).advisory: not a list; dropped", repairs)

    def test_the_schema_describes_advisory_checks(self):
        worker = orchestrator.SCHEMAS[0]["function"]["parameters"]["properties"]["wave"]["properties"]["workers"]
        text = worker["items"]["properties"]["advisory"]["description"]
        segments = {part.split(" ")[0]: part for part in text.split(", or ", 1)[1].split("; ")}
        self.assertEqual(set(segments), set(acceptance.ADVISORY_KINDS))
        for kind, (required, optional) in acceptance.ADVISORY_KINDS.items():
            for field in required:
                self.assertIn(field, segments[kind].split())
            for field in optional:
                if field != "bounds":  # The brief's own planned bounds, filled in by repair_wave.
                    self.assertIn(f"{field}?", segments[kind].split())

    def test_the_users_words_pick_the_craft_topics(self):
        # Eval 20260930-151649: the orchestrator paraphrased an Arcane request into "designed paint" and
        # "tapered patches", and its modeling worker would have painted without the painterly recipe.
        plan = orchestrator.normalise_wave(reference_wave())
        brief = {**plan["workers"][0], "steps": 20}
        scene = "<scene_context>\nx\n</scene_context>"
        plain = workers.opening(brief, plan["shared"], [], scene, ["an armchair"])
        arcane = workers.opening(brief, plan["shared"], [], scene, ["an armchair in the look of the animated series Arcane"])
        self.assertNotIn("craft/modeling/painterly", plain)
        self.assertIn("craft/modeling/painterly", arcane)

    def test_the_worker_reads_them_apart_from_its_checks(self):
        plan, _ = self.repair([{"kind": "flat_shaded"}, {"kind": "triangles", "max": 1000}])
        brief = {**plan["workers"][0], "steps": 30}
        text = workers.opening(brief, plan["shared"], [], "<scene_context>\nx\n</scene_context>", ["an armchair"])
        self.assertIn("advisory (the brief's own targets: check_brief measures them and the orchestrator reads each "
                      "miss; they never fail your work):\n- a1: Armchair is flat shaded (at most 5% of faces smooth)\n"
                      "- a2: Armchair has at most 1,000 triangles\n", text)


class WorkerEditTest(unittest.TestCase):
    """P2: the orchestrator's own run_python changed the mesh or materials of an object a worker built:
    one line pointing back to that worker, and a record for the evals (M1). Never blocking."""

    SESSION = {"worker_threads": {"barrel": {"created": ["Barrel", "Barrel_Wood"]},
                                  "chest": {"created": ["Chest", "ChestLid"]}}, "turn_number": 2}

    @staticmethod
    def entry(**more) -> dict:
        return {"type": "MESH", "location": (0, 0, 0), "verts": 96, "faces": 90, "shape": "a1", "data": "Mesh",
                "materials": ("Shop_WoodLight",), **more}

    def result(self, after: dict):
        before = {"objects": {name: self.entry() for name in ("Barrel", "Chest", "ChestLid", "Floor")}}
        return types.SimpleNamespace(scene_before=before, scene_after={"objects": {**before["objects"], **after}})

    def test_a_mesh_or_material_change_gets_one_line_per_call(self):
        session = json.loads(json.dumps(self.SESSION))
        result = self.result({"Barrel": self.entry(shape="b2"), "Chest": self.entry(materials=("Shop_Gold",)),
                              "ChestLid": self.entry(verts=120, faces=110), "Floor": self.entry(shape="z9")})
        self.assertEqual(orchestrator.edit_note(session, "run_python", result),
                         "\n\nBarrel was built by worker barrel; Chest, ChestLid were built by worker chest: send fixes "
                         "back with delegate continues.")
        self.assertEqual(session["worker_edits"], [{"turn": 2, "objects": {"Barrel": "barrel", "Chest": "chest",
                                                                           "ChestLid": "chest"}}])

    def test_arranging_and_other_tools_say_nothing(self):
        session = json.loads(json.dumps(self.SESSION))
        moved = self.result({"Barrel": self.entry(location=(1, 2, 0)), "Floor": self.entry(shape="z9")})
        self.assertEqual(orchestrator.edit_note(session, "run_python", moved), "", "whole-object moves are the orchestrator's")
        edited = self.result({"Barrel": self.entry(shape="b2")})
        self.assertEqual(orchestrator.edit_note(session, "get_object_info", edited), "")
        self.assertEqual(orchestrator.edit_note({"turn_number": 1}, "run_python", edited), "", "no workers, no note")
        self.assertEqual(orchestrator.edit_note(session, "run_python", types.SimpleNamespace(text="x")), "")
        self.assertNotIn("worker_edits", session)

    def test_the_orchestrator_loop_appends_it(self):
        session = json.loads(json.dumps(self.SESSION))
        note = agent.ORCH_SPEC.after_tool(session, "run_python", self.result({"Barrel": self.entry(shape="b2")}))
        self.assertIn("Barrel was built by worker barrel", note)
        self.assertIsNone(agent.SINGLE_SPEC.after_tool)
        self.assertIsNone(workers.SPEC("modeling", save=lambda s: None).after_tool)


class RunningClashTest(unittest.TestCase):
    """A brief sent while earlier workers still run (delegate returns as each finishes): it may not go
    back to one of them, nor take one's id, prefix or deliverable, nor own what one owns."""

    BUSY = {"table": {"deliverable": "SideTable", "prefix": "SideTable", "owns": ["Rug"], "wave": 1}}

    def test_briefs_beside_running_workers(self):
        wave = reference_wave()
        armchair = wave["workers"][0]
        wave["workers"] = [armchair]
        self.assertEqual(refusals(wave, live={**LIVE, "busy": self.BUSY}), [], "a new deliverable goes alongside")
        clash = {**armchair, "id": "table", "prefix": "Sidetable", "deliverable": "SideTable", "owns": ["Rug"],
                 "checks": []}
        problems = [p for p in refusals({**wave, "workers": [clash]}, live={**LIVE, "busy": self.BUSY})
                    if "running worker" in p]
        self.assertEqual(problems, ["workers[0] (table).id: 'table' is a running worker's; use another id",
                                    "workers[0] (table).prefix: 'Sidetable' is running worker table's",
                                    "workers[0] (table).deliverable: running worker table builds 'SideTable'",
                                    "workers[0] (table).owns: Rug is running worker table's"])

    def test_a_new_box_may_not_be_planned_into_a_running_workers_box(self):
        wave = reference_wave()
        wave["workers"] = [wave["workers"][0]]  # The armchair: x -0.9..-0.1, y 0.2..1.0.
        running = {"lamp": {"deliverable": "Lamp", "prefix": "Lamp", "owns": [], "role": "modeling",
                            "bounds": [[-0.5, 0.5, 0.015], [-0.2, 0.8, 1.6]]},
                   "light": {"deliverable": "Key", "prefix": "Key", "owns": [], "role": "lighting",
                             "bounds": [[-2, -2, 0], [2, 2, 3]]}}
        problems, _, _, _ = orchestrator.layout(orchestrator.normalise_wave(wave), {**LIVE, "busy": running})
        self.assertEqual(problems, ["armchair and lamp (running): planned bounds overlap by 0.30 x 0.30 x 0.93 m; move "
                                    "one apart or shrink it"], "a lighting worker's box may share space, as in a wave")

    def test_going_back_to_a_running_worker_waits_for_its_result(self):
        wave = reference_wave()
        wave["workers"] = [{**wave["workers"][1], "id": "table2", "continues": "table", "prefix": "Table2",
                            "deliverable": "SideTable2"}]
        live = {**LIVE, "busy": self.BUSY, "threads": {}}
        plan, notes = orchestrator.repair_wave(orchestrator.normalise_wave(wave), USER, live)
        self.assertEqual(plan["workers"][0]["continues"], "table", "not wiped as an unknown worker")
        self.assertIn("workers[0] (table2).continues: 'table' is still running; delegate wait true for its result, "
                      "then send this", orchestrator.validate_wave(plan, USER, live))


class ContinueTest(unittest.TestCase):
    """Audit §5: a re-brief was a fresh worker with no memory. A brief with continues goes back to the
    worker that built the deliverable (waves.remember), owning what it built."""

    THREADS = {"armchair": {"deliverable": "Armchair", "role": "modeling", "prefix": "Armchair", "wave": 1,
                            "created": ["Armchair", "Cushion", "Armchair_Fabric", "Pal_Wood", "Bottle", "Gone"]}}

    def live(self) -> dict:
        objects = {**LIVE["objects"], "Armchair": {"type": "MESH", "box": None}, "Cushion": {"type": "MESH", "box": None}}
        return {**LIVE, "objects": objects, "names": LIVE["names"] + ["Armchair", "Cushion", "Armchair_Fabric"],
                "threads": self.THREADS}

    def repair(self, worker: dict) -> tuple[dict, list]:
        wave = {"shared": {"keep": {"names": ["Bottle"], "quote": "Keep the bottle and the box unchanged"},
                           "palette": ["Pal_Wood"]},
                "workers": [{"id": "armchair", "requirements": [{"quote": "an armchair", "do": "roll the arms"}],
                             "style": "rolled arms, deeper tufting", **worker}]}
        return orchestrator.repair_wave(orchestrator.normalise_wave(wave), USER, self.live())

    def test_effective_role_matches_repair_with_continuations_defaults_and_aliases(self):
        for fields, thread_role, busy, expected in (
                ({}, "lighting", False, "lighting"),
                ({"role": None}, "lighting", False, "lighting"),
                ({"role": ""}, "lighting", False, "lighting"),
                ({}, " lights ", False, "lighting"),
                ({}, "shot", False, "lighting"),
                ({}, None, False, "modeling"),
                ({"role": "cam"}, "lighting", False, "camera"),
                ({"role": "sculpting"}, "lighting", False, "modeling"),
                ({"continues": "unknown"}, "lighting", False, "modeling"),
                ({}, "lighting", True, "modeling")):
            with self.subTest(fields=fields, thread_role=thread_role, busy=busy):
                worker = {"id": "key", "continues": "key", **fields}
                before = dict(worker)
                live = {"threads": {"key": {"role": thread_role}}, "busy": {"key": {}} if busy else {}}
                plan, _ = orchestrator.repair_wave(orchestrator.normalise_wave({"workers": [worker]}), "", live)
                self.assertEqual(plan["workers"][0]["role"], expected)
                self.assertEqual(orchestrator.effective_role(worker, live), expected)
                self.assertEqual(worker, before, "identity lookup must not change the submitted brief")

    def test_a_brief_sent_back_owns_what_that_worker_built(self):
        plan, repairs = self.repair({"continues": "armchair"})
        worker = plan["workers"][0]
        self.assertEqual((worker["deliverable"], worker["role"], worker["prefix"]), ("Armchair", "modeling", "Armchair"))
        self.assertEqual(worker["owns"], ["Armchair", "Cushion", "Armchair_Fabric"],
                         "not the palette, not what is kept, not what is gone")
        self.assertIn("workers[0] (armchair).owns: added what armchair built in wave 1 (Armchair, Cushion, "
                      "Armchair_Fabric)", repairs)
        self.assertEqual(orchestrator.validate_wave(plan, USER, self.live()), [])

    def test_an_unknown_worker_starts_fresh_and_one_worker_takes_one_brief(self):
        plan, repairs = self.repair({"continues": "sofa", "deliverable": "Sofa", "role": "modeling", "prefix": "Sofa"})
        self.assertEqual(plan["workers"][0]["continues"], "")
        self.assertIn("workers[0] (armchair).continues: no earlier worker 'sofa' this session (earlier workers: "
                      "armchair); it starts fresh", repairs)
        wave = {"workers": [{"id": "a", "continues": "armchair"}, {"id": "b", "continues": "armchair"}]}
        errors = orchestrator.validate_wave(orchestrator.normalise_wave(wave), USER, self.live())
        self.assertIn("workers[1] (b).continues: 'armchair' is also a's; one brief per worker", errors)


    def test_results_come_as_workers_finish_and_fixes_go_back_at_once(self):
        """Waiting uses the existing delegate tool without requiring a new wave."""
        schema = orchestrator.SCHEMAS[0]["function"]
        self.assertNotIn("required", schema["parameters"], "a wait has no wave")
        self.assertEqual(schema["parameters"]["properties"]["wait"]["type"], "boolean")
        self.assertIn("returns once any finish", schema["description"])

    def test_a_workers_object_goes_back_to_that_worker(self):
        """The open finish-gate nudge retains its worker continuation instruction."""
        self.assertIn("Send a fix to a worker's object back to that worker (delegate, continues)", orchestrator.BOUNCE)


class ReferencesTest(unittest.TestCase):
    """A brief's references: which of the user's attached images its worker sees (the owner's Arcane stills
    reached the orchestrator only). Names are the attachments' own; an unknown one is a refusal the
    orchestrator can fix, and a brief sent back keeps its worker's unless it says otherwise."""

    ATTACHED = ["ref1.jpg", "Ref2.PNG"]

    def live(self, **more) -> dict:
        return {**LIVE, "references": list(self.ATTACHED), **more}

    def repair(self, references=None, live=None, **worker) -> tuple[dict, list]:
        wave = reference_wave()
        if references is not None:
            wave["workers"][0]["references"] = references
        wave["workers"][0].update(worker)
        return orchestrator.repair_wave(orchestrator.normalise_wave(wave), USER, live or self.live())

    def test_the_schema_field_is_short(self):
        worker = orchestrator.SCHEMAS[0]["function"]["parameters"]["properties"]["wave"]["properties"]["workers"]
        field = worker["items"]["properties"]["references"]
        self.assertEqual((field["type"], field["items"]), ("array", {"type": "string"}))
        self.assertLessEqual(len(field["description"]), 40)

    def test_names_resolve_to_the_attachments_own(self):
        plan, _ = self.repair(["REF1", "ref2", "ref1.jpg"])
        self.assertEqual(plan["workers"][0]["references"], ["ref1.jpg", "Ref2.PNG"], "case, extension, repeats")
        self.assertEqual(orchestrator.validate_wave(plan, USER, self.live()), [])
        plan, _ = self.repair("ref1.jpg")
        self.assertEqual(plan["workers"][0]["references"], ["ref1.jpg"], "one name given as a string")
        plan, _ = self.repair()
        self.assertEqual([w["references"] for w in plan["workers"]], [[], []], "none unless named")

    def test_an_unknown_name_is_a_refusal_that_names_the_attachments(self):
        plan, _ = self.repair(["arcane_still.jpg"])
        errors = orchestrator.validate_wave(plan, USER, self.live())
        self.assertEqual(errors, ["workers[0] (armchair).references: 'arcane_still.jpg' not attached; the user "
                                  "attached ref1.jpg, Ref2.PNG"])
        plan, _ = self.repair(["ref1.jpg"], live=LIVE)
        self.assertEqual(orchestrator.validate_wave(plan, USER, LIVE),
                         ["workers[0] (armchair).references: 'ref1.jpg' not attached; the user attached no images"])
        plan, _ = self.repair([{"name": "ref1"}])
        self.assertEqual(plan["workers"][0]["references"], ["ref1.jpg"], "[{name}] read as names")
        plan, _ = self.repair([3])
        self.assertIn("a list of the names", orchestrator.validate_wave(plan, USER, self.live())[0])
        live = self.live(references=[f"r{n}.png" for n in range(5)])
        plan, _ = self.repair([f"r{n}.png" for n in range(4)], live=live)
        self.assertEqual(orchestrator.validate_wave(plan, USER, live),
                         [f"workers[0] (armchair).references: at most {orchestrator.MAX_REFERENCES}, got 4"])

    def test_a_dry_run_names_an_unknown_reference_instead_of_failing(self):
        session = state.reset()
        self.addCleanup(state.reset)
        turn = agent.Turn()
        turn.roles = types.SimpleNamespace(orchestrate=True)
        session["turn"] = turn
        session["items"].append(state.item_user(USER))
        session["references"] = [{"ref": "loopcut-image:" + "a" * 64 + ".png", "name": "ref1.jpg", "pinned": True}]
        wave = reference_wave()
        wave["workers"][0]["references"] = ["ref9"]
        for dry_run in (True, False):
            with mock.patch.object(orchestrator, "live_facts", lambda plan: json.loads(json.dumps(LIVE))):
                result = orchestrator.delegate(wave, dry_run=dry_run)
            self.assertFalse(result.ok)
            self.assertTrue(result.plan_refused)
            self.assertIn("workers[0] (armchair).references: 'ref9' not attached; the user attached ref1.jpg", result.text)

    def test_a_brief_sent_back_keeps_its_workers_references_unless_it_names_others(self):
        threads = {"armchair": {"deliverable": "Armchair", "role": "modeling", "prefix": "Armchair", "wave": 1,
                                "created": [], "references": ["ref1.jpg"]}}
        live = self.live(threads=threads)
        plan, _ = self.repair(live=live, continues="armchair")
        self.assertEqual(plan["workers"][0]["references"], ["ref1.jpg"])
        plan, _ = self.repair(["ref2"], live=live, continues="armchair")
        self.assertEqual(plan["workers"][0]["references"], ["Ref2.PNG"])
        plan, _ = self.repair([], live=live, continues="armchair")
        self.assertEqual(plan["workers"][0]["references"], [])
        session = {"worker_threads": {"armchair": {"deliverable": "Armchair", "references": [
            {"name": "ref1.jpg", "pinned": True}, {"name": "old.png", "pinned": False}]}}}
        self.assertEqual(orchestrator.threads_of(session)["armchair"]["references"], ["ref1.jpg"],
                         "what its last brief showed it")

    def test_the_worker_reads_which_images_are_its_references(self):
        plan = orchestrator.normalise_wave(reference_wave())
        brief = {**plan["workers"][0], "steps": 20, "references": ["ref1.jpg", "Ref2.PNG"]}
        text = workers.opening(brief, plan["shared"], [], "<scene_context>\nx\n</scene_context>")
        self.assertIn("references (the user's images, attached: the look your style asks for; look_at_reference "
                      "for detail): ref1.jpg, Ref2.PNG", text)
        self.assertNotIn("references (", workers.opening({**brief, "references": []}, plan["shared"], [],
                                                         "<scene_context>\n</scene_context>"))


class ShotTest(unittest.TestCase):
    """Audit item 8: on product and shot tasks a lighting worker and a camera worker never saw each
    other's work, and the orchestrator rebuilt the shot after the wave (M/product_reveal: 13 run_python).
    One worker owns a shot: its cameras and the light they need."""


    def test_a_shot_role_becomes_lighting_with_the_aspects_nobody_else_holds(self):
        wave = {"workers": [{"id": "hero", "deliverable": "HeroShot", "role": "shot", "prefix": "Hero",
                             "requirements": [{"quote": "The camera should show the whole set", "do": "hero shot"}]}]}
        plan, repairs = orchestrator.repair_wave(orchestrator.normalise_wave(wave), USER, LIVE)
        self.assertEqual((plan["workers"][0]["role"], plan["workers"][0]["aspects"]), ("lighting", ["lighting", "camera"]))
        self.assertIn("workers[0] (hero).role: 'shot' mapped to 'lighting', aspects lighting and camera added (a shot "
                      "owns its camera and its light)", repairs)
        wave["workers"].insert(0, {"id": "sun", "deliverable": "Sun", "role": "lighting", "prefix": "Sun",
                                   "aspects": ["lighting"], "requirements": []})
        plan, _ = orchestrator.repair_wave(orchestrator.normalise_wave(wave), USER, LIVE)
        self.assertEqual(plan["workers"][1]["aspects"], ["camera"], "an aspect has one owner per wave")

    def test_a_shot_worker_reads_how_to_work_a_shot(self):
        brief = {"role": "lighting", "aspects": ["lighting", "camera"], "deliverable": "HeroShot"}
        notes = workers.role_notes("lighting", brief)
        self.assertIn("craft/lighting/shot-camera", notes)
        self.assertNotIn(workers.SHOT_NOTE, workers.role_notes("lighting", {**brief, "aspects": ["lighting"]}))
        self.assertTrue(notes.endswith(workers.SHOT_NOTE))
        self.assertIn("capture_viewport angle camera", workers.SHOT_NOTE)
        skills = types.ModuleType("loopcut.skills")
        skills.skills_for = lambda role, b, notes="", **options: "Light like a studio."
        with mock.patch.dict(sys.modules, {"loopcut.skills": skills}):
            self.assertEqual(workers.role_notes("lighting", brief), f"Light like a studio.\n{workers.SHOT_NOTE}")


class FakeJob:
    crew, on_main = [], None  # No workers to wait for.

    def __init__(self, checks):
        self.checks, self.bounces, self.finished, self.retired = checks, 0, None, {}

    def final_checks(self):
        return self.checks

    def finish(self, bounces, results):
        self.finished = (bounces, results)


class FinishGateTest(unittest.TestCase):
    CHECKS = [("armchair c1", {"kind": "contact", "a": "Armchair", "relation": "rests_on", "b": "Rug", "id": "c1",
                               "quote": "an armchair on the rug"}),
              ("keep", {"kind": "unchanged", "names": ["Bottle"], "id": "k1", "quote": "keep the bottle"})]

    def setUp(self):
        self.session = state.new_session()
        self.turn = agent.Turn()
        self.turn.job = FakeJob(self.CHECKS)

    def reply(self, text: str):
        from loopcut import llm
        item = state.item_assistant()
        item["text"], item["streaming"] = text, False
        self.session["items"].append(item)
        self.session["messages"].append({"role": "assistant", "content": text})
        return llm.Completion(text=text)

    def test_failures_bounce_twice_then_the_verified_block_says_what_is_not_met(self):
        failing = lambda checks: [{"status": "fail", "detail": "2 of 4 legs 12 mm above (fix -0.012 z)"}, {"status": "pass"}]
        bounces = [orchestrator.finish_gate(self.session, self.turn, self.reply(f"Done {n}."), failing) for n in range(3)]
        self.assertTrue(bounces[0].startswith("[Loopcut] These checks fail on the live scene:"))
        self.assertIn('- id "armchair c1": contact Armchair rests_on Rug: 2 of 4 legs 12 mm above', bounces[0])
        self.assertIn("retire with the checks tool", bounces[0], "a wrong or overridden check is retired, not 'fixed'")
        self.assertIsNotNone(bounces[1])
        self.assertIsNone(bounces[2], "at most two bounces")
        final = self.session["items"][-1]["text"]
        self.assertTrue(final.startswith("Done 2.\n\nVerified by Loopcut on this scene: 1 of 2 checks."), final)
        self.assertEqual(self.session["items"][-1]["verification"], final.split("\n\n", 1)[1])
        self.assertIn('- not met: "an armchair on the rug" (contact Armchair rests_on Rug): 2 of 4 legs', final)
        self.assertIn("Verified by Loopcut on this scene:", self.session["messages"][-1]["content"], "the model sees it next turn too")
        self.assertEqual(self.turn.job.finished[0], 2)

    def test_all_passing_ends_at_once_with_the_block(self):
        passing = lambda checks: [{"status": "pass"} for _ in checks]
        self.assertIsNone(orchestrator.finish_gate(self.session, self.turn, self.reply("All set."), passing))
        self.assertEqual(self.session["items"][-1]["text"], "All set.\n\nVerified by Loopcut on this scene: 2 of 2 checks.\n"
                                                            "Requirements (the user's words): 2 of 2 covered.")

    def test_the_verified_block_names_the_requirements_no_check_covers(self):
        """GPT-6.1 Sol's asset pack (20260929-230207): "Verified 35/35" while the sign stood on the ground,
        "a hanging sign" verified by exists alone. The block counts the user's requirements, the briefs'
        and the checks', and names those no check measures; the gate still bounces only on failing checks."""
        self.turn.job = FakeJob([
            ("sign c1", {"kind": "exists", "name": "HangingSign", "id": "c1", "quote": "a hanging sign"}),
            ("sign c2", {"kind": "count", "prefix": "HangingSign", "max": 1, "id": "c2",
                         "quote": "Keep each prop a separate mesh object"}),
            ("o1", {"kind": "no_overlap", "names": ["HangingSign", "Barrel"], "id": "o1", "quote": "nothing overlapping"})])
        self.turn.job.waves = [types.SimpleNamespace(plan={"workers": [{"requirements": [
            {"quote": "a hanging sign", "do": "x"}, {"quote": "Keep each prop a separate mesh object with a clear name"},
            {"quote": "Use a consistent hand-painted look", "do": "x"},
            {"quote": "arrange them into an attractive preview with nothing overlapping", "do": "x"}]}]})]
        passing = lambda checks: [{"status": "pass"} for _ in checks]
        self.assertIsNone(orchestrator.finish_gate(self.session, self.turn, self.reply("Done."), passing), "no bounce")
        self.assertEqual(self.session["items"][-1]["text"].split("\n\n", 1)[1],
                         "Verified by Loopcut on this scene: 3 of 3 checks.\n"
                         "Requirements (the user's words): 2 of 4 covered, 1 partly covered, 1 unverified.\n"
                         "- partly covered: \"a hanging sign\": sign c1 pass; nothing measures \"hanging\" (a suspended "
                         "check would)\n"
                         "- unverified: \"Use a consistent hand-painted look\": no check")
        rows = [("c", {"kind": "suspended", "a": "S", "b": "S", "quote": "a hanging sign"}, {"status": "pass"}),
                ("d", {"kind": "contact", "a": "A", "relation": "rests_on", "b": "Rug", "quote": "an armchair on the rug"},
                 {"status": "fail"}),
                ("e", {"kind": "dims", "name": "Lamp", "size": [1, 1, 1], "quote": "a tall lamp"}, {"status": "unavailable"})]
        self.assertEqual([(e["quote"], e["status"]) for e in orchestrator.coverage(["under 2000 triangles"], rows)],
                         [("under 2000 triangles", "unverified"), ("a hanging sign", "covered"),
                          ("an armchair on the rug", "failed"), ("a tall lamp", "unverified")],
                         "a number after under is a budget, not a placement")

    def test_at_the_step_limit_it_never_bounces(self):
        self.turn.outcome = "step_limit"
        failing = lambda checks: [{"status": "fail", "detail": "no"} for _ in checks]
        self.assertIsNone(orchestrator.finish_gate(self.session, self.turn, None, failing))
        self.assertTrue(self.session["items"][-1]["text"].startswith("Verified by Loopcut on this scene: 0 of 2 checks."))

    def test_after_no_progress_it_reports_failed_checks_without_bouncing(self):
        self.turn.outcome = "no_progress"
        failing = lambda checks: [{"status": "fail", "detail": "no"} for _ in checks]
        self.assertIsNone(orchestrator.finish_gate(self.session, self.turn, self.reply("Blocked."), failing))
        self.assertEqual(self.turn.job.bounces, 0)
        self.assertEqual(self.turn.job.finished[0], 0)
        self.assertIn("Verified by Loopcut on this scene: 0 of 2 checks.", self.session["items"][-1]["text"])

    def test_after_no_progress_late_workers_are_drained_without_promising_a_review(self):
        from loopcut import waves
        self.turn.outcome = "no_progress"
        late = types.SimpleNamespace(scene_before={"objects": []}, scene_after={"objects": ["Chair"]})
        with mock.patch.object(waves, "drain", return_value=late) as drain:
            self.assertIsNone(orchestrator._late_workers(self.session, self.turn, self.turn.job))
        drain.assert_called_once()
        self.assertEqual(self.turn.scene_after, late.scene_after)
        self.assertIn("no-progress guard", self.session["items"][-1]["text"])
        self.assertIn("did not review them", self.session["items"][-1]["text"])

    def test_no_gate_without_a_job_or_after_a_stop(self):
        self.assertIsNone(orchestrator.finish_gate(self.session, agent.Turn(), self.reply("Hi."), None))
        self.turn.cancel.set()
        self.assertIsNone(orchestrator.finish_gate(self.session, self.turn, self.reply("Hi."), None))


class OwnChecksTest(unittest.TestCase):
    """The checks tool: the orchestrator's level-1 work verified like a worker's, and a check the user
    overrode (turn 1 wanted Pal_* materials, then they asked for an ice palette) retired with a reason,
    not "fixed" back."""

    def setUp(self):
        from loopcut import waves  # noqa: F401 - the tool makes the turn's job.
        data = tempfile.TemporaryDirectory()
        self.addCleanup(data.cleanup)
        patcher = mock.patch.dict(os.environ, {"LOOPCUT_DATA_DIR": data.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.session = state.reset()
        self.session["turn_number"] = 2
        self.session["items"] += [state.item_user("Crystals on the rug in the Pal_ colours."),
                                  state.item_user("Actually use an ice palette instead, and export it to out/crystals.glb")]
        self.session["checks"] = {"crystal c2": {"kind": "material_on", "name": "Crystal", "material": "Pal_Blue",
                                                 "quote": "in the Pal_ colours", "id": "c2"},
                                  "keep": {"kind": "unchanged", "names": ["Rug"], "quote": "on the rug", "id": "k1"}}
        self.turn = agent.Turn()
        self.turn.roles = types.SimpleNamespace(orchestrate=True, job_max_usd=0.25, worker_max_seconds=900,
                                                max_workers=3)
        self.session["turn"] = self.turn
        self.ran = []
        patcher = mock.patch.object(orchestrator.acceptance, "run", lambda checks, spawn=None: self.ran.append(checks) or [
            {"id": c["id"], "kind": c["kind"], "title": orchestrator.check_title(c), "status": "pass", "detail": "fine"}
            for c in checks])
        patcher.start()
        self.addCleanup(patcher.stop)

    EXPORT = {"kind": "file", "path": "out/crystals.glb", "quote": "export it to out/crystals.glb"}

    def test_added_checks_run_now_and_join_the_turns_gate_with_earlier_turns(self):
        result = orchestrator.checks(add=[self.EXPORT])
        self.assertTrue(result.ok, result.text)
        self.assertTrue(result.text.startswith("Added o1: 1 of 1 pass on the live scene now"), result.text)
        self.assertIn("- o1 file out/crystals.glb: pass, fine", result.text)
        self.assertEqual([c["id"] for c in self.ran[0]], ["o1"])
        labels = [label for label, _ in self.turn.job.final_checks()]
        self.assertEqual(labels, ["crystal c2", "o1"], "earlier turns' checks run too; keep's baseline is gone with its turn")
        self.assertEqual(orchestrator.checks(add=[dict(self.EXPORT, path="out/b.glb")]).text.split(":")[0], "Added o2")
        self.assertIsNotNone(orchestrator.describe("checks", {"add": [self.EXPORT]}))

    def test_an_added_check_quotes_the_user(self):
        result = orchestrator.checks(add=[dict(self.EXPORT, quote="a nice export")])
        self.assertFalse(result.ok)
        self.assertIn("is not in the user's messages", result.text)
        self.assertEqual(self.ran, [])
        self.assertIsNone(self.turn.job.own.get("o1"))

    def test_a_retired_check_leaves_the_gate_and_the_verified_block_says_why(self):
        for retire, problem in (([{"id": "crystal c9", "reason": "x"}], "not a check that runs; they are: crystal c2"),
                                ([{"id": "crystal c2", "reason": "wrong"}], "say why"),
                                ([{"id": "crystal c2", "reason": "the user wants something else"}], "quote the user"),
                                ([{"id": "crystal c2", "reason": 'they said "a frost palette"'}], "is not in the user's")):
            result = orchestrator.checks(retire=retire)
            self.assertFalse(result.ok)
            self.assertIn(problem, result.text)
        reason = 'the user changed it: "use an ice palette instead"'
        result = orchestrator.checks(retire=[{"id": "crystal c2", "reason": reason}], add=[self.EXPORT])
        self.assertTrue(result.ok, result.text)
        self.assertEqual([label for label, _ in self.turn.job.final_checks()], ["o1"])
        self.assertTrue(orchestrator.checks(retire=[{"id": "o1", "reason": "the export is 0 bytes by design, measured"}]).ok,
                        "measured numbers are a reason too")
        completion = types.SimpleNamespace(text="Done.")
        self.session["items"].append({**state.item_assistant(), "text": "Done.", "streaming": False})
        self.session["messages"].append({"role": "assistant", "content": "Done."})
        self.assertIsNone(orchestrator.finish_gate(self.session, self.turn, completion, lambda checks: []))
        final = self.session["items"][-1]["text"]
        self.assertIn(f"- retired: {reason} (material_on Crystal: Pal_Blue)", final)
        self.assertIn("Verified by Loopcut on this scene: 0 of 0 checks.", final)
        self.assertEqual(self.session["checks"], {}, "retired checks do not come back next turn")

    def test_standing_checks_carry_to_the_next_turn(self):
        orchestrator.checks(add=[self.EXPORT])
        completion = types.SimpleNamespace(text="Done.")
        self.session["items"].append({**state.item_assistant(), "text": "Done.", "streaming": False})
        results = lambda checks: [{"status": "pass"} for _ in checks]
        self.assertIsNone(orchestrator.finish_gate(self.session, self.turn, completion, results))
        self.assertEqual(sorted(self.session["checks"]), ["crystal c2", "o1"])
        self.assertEqual(self.turn.job.log["retired"], [])

    def test_workers_off_means_no_checks(self):
        self.turn.roles = None
        self.assertFalse(orchestrator.checks(add=[self.EXPORT]).ok)

    def test_a_snapshot_is_what_preserved_checks_compare_with(self):
        """GPT-6.1 Sol's wish (20260929-235231): a named baseline that survives merges, and a check that
        compares with it, less what the pass may change. The records ride in the check (baseline)."""
        records = {"Crystal": {"origin": [0, 0, 0]}, "Rug": {"origin": [1, 0, 0]}}
        with mock.patch.object(orchestrator.acceptance, "snapshot", lambda names: {n: records[n] for n in names}):
            result = orchestrator.checks(snapshot=["Crystal"])
            self.assertTrue(result.ok, result.text)
            self.assertTrue(result.text.startswith('Snapshot s1: Crystal. Compare with {"kind": "preserved", "names": '
                                                   '[...], "snapshot": "s1", "exclude": [...]}'), result.text)
            self.assertIn("of origin, transform, dims, bounds, topology, vertices, materials (a look pass: materials)",
                          result.text)
            self.assertEqual(orchestrator.checks(snapshot=["Crystal", "Rug"]).text.split(":")[0], "Snapshot s2")
            self.assertFalse(orchestrator.checks(snapshot=[]).ok)
        check = {"kind": "preserved", "names": ["Crystal"], "snapshot": "s1", "exclude": ["materials"],
                 "quote": "Crystals on the rug"}
        result = orchestrator.checks(add=[check])
        self.assertTrue(result.ok, result.text)
        self.assertEqual(self.turn.job.own["o1"]["baseline"], {"Crystal": {"origin": [0, 0, 0]}})
        self.assertNotIn("baseline", check, "the caller's check is left as it was")
        self.assertIn("'s9' is not a snapshot; they are: s1, s2", orchestrator.checks(add=[{**check, "snapshot": "s9"}]).text)
        self.assertIn("names: Rug not in s1", orchestrator.checks(add=[{**check, "names": ["Rug"]}]).text)
        with mock.patch.object(orchestrator.acceptance, "snapshot", side_effect=ValueError("no object named 'Crystl'")):
            refused = orchestrator.checks(snapshot=["Crystl"])
        self.assertEqual((refused.ok, refused.text), (False, "checks refused; nothing changed: no object named 'Crystl'"))
        self.assertEqual(orchestrator.describe("checks", {"snapshot": ["Crystal"]})[0], "Checks: snapshot")

    def test_no_arguments_runs_every_check_now(self):
        """GPT-6.1 Sol's checks({}) (20260929-230207 O0009): the schema has no required field and says
        "Run now", yet it was refused. Now it reruns what the finish gate would run, changing nothing."""
        orchestrator.checks(add=[self.EXPORT])
        self.ran.clear()
        result = orchestrator.checks()
        self.assertTrue(result.ok, result.text)
        self.assertEqual([c["id"] for c in self.ran[0]], ["c2", "o1"], "earlier turns' and this turn's")
        self.assertEqual(result.text, "Ran every check on the live scene now: 2 of 2 pass.\nPass: crystal c2, o1\n"
                                      "Requirements (the user's words): 2 of 2 covered.")
        self.assertEqual(orchestrator.checks(add=[], retire=[]).text, result.text)
        self.assertEqual(list(self.turn.job.own), ["o1"], "nothing added or retired")
        self.assertEqual(orchestrator.describe("checks", {})[0], "Checks: run all now")
        self.assertFalse(orchestrator.checks(add="all").ok)
        self.assertIn("No arguments: run every check now", json.dumps(orchestrator.SCHEMAS))


if __name__ == "__main__":
    unittest.main()
