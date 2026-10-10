"""First-run route regressions with a recording splash layout and a private fake bpy.

Load Blender-facing modules under a private package so the suite's other bpy stubs and
agent fixtures are untouched. No browser, HTTP, preferences file or Blender is used.
"""

import importlib
import importlib.util
import io
import tempfile
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fake_preferences import AddonPreferences

PACKAGE = Path(__file__).resolve().parents[2] / "scripts/addons_core/loopcut"
NAME = "_c11_loopcut"


class Splash:
    def __init__(self, records=None):
        self.records = records if records is not None else []
        self.enabled = True

    def column(self, **kwargs):
        return Splash(self.records)

    row = split = column

    def separator(self, **kwargs):
        pass

    def label(self, **kwargs):
        self.records.append(("label", kwargs.get("text", "")))

    def prop(self, prefs, name):
        self.records.append(("prop", name))

    def operator(self, name, **kwargs):
        props = types.SimpleNamespace()
        self.records.append(("operator", name, kwargs.get("text", ""), props, self))
        return props

    def operators(self):
        return [r for r in self.records if r[0] == "operator"]


class OnboardingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = types.ModuleType("bpy")
        cls.fake.types = types.SimpleNamespace(**{name: type(name, (), {}) for name in
            ("Operator", "AddonPreferences", "Menu", "FileHandler", "SpaceTextEditor")})
        cls.fake.types.AddonPreferences = AddonPreferences
        cls.fake.props = Mock()
        cls.fake.ops = Mock()
        cls.fake.app = types.SimpleNamespace(version=(5, 2, 0), timers=Mock())
        cls.fake.context = types.SimpleNamespace()
        cls.fake.utils = types.ModuleType("bpy.utils")
        cls.fake.utils.previews = types.ModuleType("bpy.utils.previews")
        cls.modules = patch.dict(sys.modules, {"bpy": cls.fake, "bpy.utils": cls.fake.utils,
            "bpy.utils.previews": cls.fake.utils.previews, NAME + ".ui.draw": types.ModuleType(NAME + ".ui.draw")})
        cls.modules.start()
        spec = importlib.util.spec_from_file_location(NAME, PACKAGE / "__init__.py",
                                                     submodule_search_locations=[str(PACKAGE)])
        package = importlib.util.module_from_spec(spec)
        sys.modules[NAME] = package
        spec.loader.exec_module(package)
        for name in ("onboarding", "account", "chatgpt_ui", "settings", "state", "cloud", "ui.host"):
            setattr(cls, name.replace("ui.", ""), importlib.import_module(NAME + "." + name))

    @classmethod
    def tearDownClass(cls):
        for name in list(sys.modules):
            if name == NAME or name.startswith(NAME + "."):
                del sys.modules[name]
        cls.modules.stop()

    def setUp(self):
        self.fake.ops.reset_mock()
        self.onboarding._state.update(step="connect", own_key=False, error="")
        self.onboarding._popups.clear()
        self.account._state.update(status="idle", error="", url="", user_code="")
        self.account._cancel.clear()
        self.chatgpt_ui._state.update(status="idle", error="", url="")
        self.chatgpt_ui._cancel.clear()
        self.state.ui.clear()
        self.host._runtime.clear()
        self.prefs = types.SimpleNamespace(provider="LOOPCUT", first_message_sent=False,
                                           base_url="https://model.test/v1", model="test-model")
        self.context = types.SimpleNamespace(region_popup=None)
        for target, name, value in ((self.settings, "preferences", self.prefs),
                                    (self.account, "signed_in", False),
                                    (self.account, "mark_icon", 0),
                                    (self.chatgpt_ui.chatgpt_auth, "status", {"signed_in": False}),
                                    (self.chatgpt_ui.chatgpt_auth, "welcomed", True)):
            p = patch.object(target, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(self.settings, "config_changed")
        p.start()
        self.addCleanup(p.stop)

    def press_step(self, splash, text):
        record = next(r for r in splash.operators() if r[1] == 'loopcut.onboarding_step' and r[2] == text)
        op = self.onboarding.LOOPCUT_OT_onboarding_step()
        op.step, op.save = record[3].step, record[3].save
        op.provider = getattr(record[3], 'provider', 'CURRENT')
        self.assertTrue(record[4].enabled)
        self.assertEqual(op.execute(self.context), {'FINISHED'})

    def test_setup_and_import_lead_to_optional_account_even_with_a_model(self):
        for previous in ('setup', 'import'):
            self.onboarding._state['step'] = previous
            with patch.object(self.onboarding, '_connected', return_value=True):
                splash = Splash()
                self.assertTrue(self.onboarding.draw_splash(splash, self.context, first_run=False))
            self.assertEqual(self.onboarding._state['step'], 'account')
            self.assertIn(('label', 'Create your free Loopcut account'), splash.records)
            self.assertIn('loopcut.sign_in', [r[1] for r in splash.operators()])
            self.assertNotIn('loopcut.chatgpt_sign_in', [r[1] for r in splash.operators()])
            self.press_step(splash, 'Skip for now')
            self.assertEqual(self.onboarding._state['step'], 'connect')
            self.assertTrue(self.account._cancel.is_set())
            model = Splash()
            self.assertTrue(self.onboarding.draw_splash(model, self.context, first_run=False))
            self.assertIn(('label', 'Connect a model'), model.records)
            self.press_step(model, 'Skip for now')
            privacy = Splash()
            self.assertTrue(self.onboarding.draw_splash(privacy, self.context, first_run=False))
            self.press_step(privacy, 'Continue')
            self.assertEqual(self.onboarding._state['step'], 'done')

    def test_screenshot_onboarding_fixtures_draw_the_requested_screens(self):
        spec = importlib.util.spec_from_file_location('c11_fixtures', PACKAGE.parents[2] / 'loopcut/harness/c11_shots.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        fixtures = module.fixtures(self.state)
        for name, title in (('onboarding_account', 'Create your free Loopcut account'),
                            ('onboarding_account_failed', 'Create your free Loopcut account'),
                            ('onboarding_connect', 'Connect a model')):
            ui = fixtures[name]['ui']
            self.onboarding._state['step'] = ui['onboarding_step']
            splash = Splash()
            status = ui.get('sign_in', {}).get('loopcut', {'status': 'idle'})
            with patch.object(self.account, 'status', return_value=status):
                self.onboarding.draw_splash(splash, self.context, first_run=False)
            labels = [r[1] for r in splash.records if r[0] == 'label']
            self.assertIn(title, labels)
            self.assertEqual('Loopcut accounts open soon' in labels, name == 'onboarding_account_failed')
            self.assertNotIn('HTTP 404', str(labels))

    def test_existing_accounts_show_continue_and_model_choices_select_the_provider(self):
        self.prefs.provider = 'OPENAI'
        self.onboarding._state['step'] = 'account'
        with patch.object(self.account, 'signed_in', return_value=True), \
                patch.object(self.account, 'account_line', return_value='person@example.test'):
            splash = Splash()
            self.onboarding.draw_splash(splash, self.context, first_run=False)
            self.assertIn(('label', 'Signed in as person@example.test'), splash.records)
            self.assertNotIn('loopcut.sign_in', [r[1] for r in splash.operators()])
            self.press_step(splash, 'Continue')
            model = Splash()
            self.onboarding._draw_connect(model, self.context)
            self.press_step(model, "Use Loopcut's models")
            self.assertEqual(self.prefs.provider, 'LOOPCUT')
            self.assertEqual(self.onboarding._state['step'], 'privacy')
        self.onboarding._state['step'] = 'connect'
        with patch.object(self.chatgpt_ui.chatgpt_auth, 'status', return_value={
                'signed_in': True, 'plan_usage': True, 'email': 'person@example.test'}):
            model = Splash()
            self.onboarding._draw_connect(model, self.context)
            self.assertIn(('label', 'Signed in with ChatGPT: person@example.test'), model.records)
            self.press_step(model, 'Continue')
            self.assertEqual(self.prefs.provider, 'CHATGPT')
            self.assertEqual(self.onboarding._state['step'], 'privacy')

    def test_failed_account_start_stays_quiet_and_both_exits_reach_model_choices(self):
        for failure in (urllib.error.URLError('connection refused'), ConnectionError('offline'),
                        urllib.error.HTTPError('https://service.test/api/device/start', 404, 'missing', {},
                                              io.BytesIO(b'not found')),
                        urllib.error.HTTPError('https://service.test/api/device/start', 503, 'unavailable', {},
                                              io.BytesIO(b'service unavailable'))):
            with self.subTest(failure=failure):
                self.onboarding._state['step'] = 'account'
                with patch.object(self.account.urllib.request, 'urlopen', side_effect=failure), \
                        patch.object(self.account, 'open_browser') as browser, \
                        redirect_stdout(io.StringIO()) as log, redirect_stderr(log):
                    self.account._run_sign_in()
                browser.assert_not_called()
                self.assertEqual(self.account.status()['status'], 'error')
                self.assertTrue(log.getvalue())
                for exit_label in ('Continue', 'Skip for now'):
                    self.onboarding._state['step'] = 'account'
                    splash = Splash()
                    self.onboarding.draw_splash(splash, self.context, first_run=False)
                    labels = [r[1] for r in splash.records if r[0] == 'label']
                    self.assertEqual(labels, ['Create your free Loopcut account', '', 'Loopcut accounts open soon'])
                    self.press_step(splash, exit_label)
                    model = Splash()
                    self.onboarding.draw_splash(model, self.context, first_run=False)
                    self.assertIn('loopcut.chatgpt_sign_in', [r[1] for r in model.operators()])
                    self.assertIn('loopcut.onboarding_own_key', [r[1] for r in model.operators()])
                    self.assertNotIn('Loopcut accounts open soon', str(model.records))

    def test_account_poll_failure_is_quiet_and_skip_suppresses_a_late_key(self):
        reply = {'device_code': 'fixture', 'user_code': 'TEST', 'verification_url': 'https://sign-in.test'}
        self.onboarding._state['step'] = 'account'
        with patch.object(self.account, '_post', side_effect=[reply, self.account.AccountError('Sign-in was denied')]), \
                patch.object(self.account._cancel, 'wait', return_value=False), \
                patch.object(self.account, 'open_browser'), redirect_stdout(io.StringIO()) as log:
            self.account._run_sign_in()
        self.assertIn('Sign-in was denied', log.getvalue())
        splash = Splash()
        self.onboarding._draw_account(splash, self.context)
        self.assertIn(('label', 'Loopcut accounts open soon'), splash.records)
        self.press_step(splash, 'Skip for now')
        self.assertEqual(self.onboarding._state['step'], 'connect')
        # Approval can already be queued on the main thread when Skip is clicked.
        self.account._cancel.clear()
        self.onboarding._state['step'] = 'account'
        def skip_before_store(callback):
            self.press_step(splash, 'Skip for now')
            callback()
            return Mock()
        with patch.object(self.account, '_post', side_effect=[reply, {'status': 'approved', 'api_key': 'fixture-only'}]), \
                patch.object(self.account._cancel, 'wait', return_value=False), \
                patch.object(self.account, 'open_browser'), patch.object(self.account, '_store_key') as store, \
                patch.object(self.account, '_refresh_account'), \
                patch.object(self.account.mainthread, 'run_on_main', side_effect=skip_before_store):
            self.account._run_sign_in()
        store.assert_not_called()
        self.assertEqual(self.onboarding._state['step'], 'connect')

    def test_account_waiting_and_starting_allow_cancel_and_skip(self):
        for status in ('waiting', 'starting'):
            self.account._state.update(status=status, user_code='TEST', url='https://sign-in.test')
            self.onboarding._state['step'] = 'account'
            splash = Splash()
            with patch.object(self.onboarding, '_watch_popup') as watch:
                self.onboarding._draw_account(splash, self.context)
                watch.assert_called_once_with(self.context)
            self.assertIn('loopcut.cancel_sign_in', [r[1] for r in splash.operators()])
            self.press_step(splash, 'Skip for now')
            self.assertEqual(self.onboarding._state['step'], 'connect')
            self.assertTrue(self.account._cancel.is_set())

    def test_eleven_entry_panel_collapse_survives_a_new_session(self):
        from coming_soon_fixture import manifest
        with tempfile.TemporaryDirectory() as directory:
            runtime = self.cloud.Runtime(types.SimpleNamespace(api_key=''), directory)
            runtime.manifest = manifest()
            with patch.object(self.cloud, '_runtime', runtime):
                session = self.state.new_session()
                initial = self.host._cloud_panel_state(session)
                self.assertEqual(len(initial['coming_soon']), 11)
                self.assertTrue(initial['coming_soon_expanded'])
                self.fake.ops.wm.save_userpref.assert_not_called()
                measure = lambda font, size, text: len(text) * size * .6
                display = self.host.layout.build(session, 420, 800, 1, measure, ui=initial)
                action = next(h['action'] for h in display['hits'] if h['id'] == 'cloud.toggle')
                self.host._do_action(session, action)
                self.assertFalse(self.prefs.coming_soon_expanded)
                self.fake.ops.wm.save_userpref.assert_called_once()
                self.state.ui.clear()
                self.host._runtime.clear()
                reopened = self.host._cloud_panel_state(self.state.new_session())
                self.assertFalse(reopened['coming_soon_expanded'])
                display = self.host.layout.build(session, 420, 800, 1, measure, ui=reopened)
                self.assertFalse([h for h in display['hits'] if h['action'][0] == 'cloud_interest'])
                self.host._do_action(session, action)
                self.state.ui.clear()
                self.assertTrue(self.host._cloud_panel_state(session)['coming_soon_expanded'])

    def test_service_model_cache_can_be_unloaded_on_the_first_turn(self):
        with patch.dict(self.account._state, models=None):
            self.assertFalse(self.account.keeps_context(self.account.BASE_URL, 'fast'))
        with patch.dict(self.account._state, models=[{'id': 'fast', 'keep_context': True}]):
            self.assertTrue(self.account.keeps_context(self.account.BASE_URL, 'fast'))
            self.assertFalse(self.account.keeps_context('https://byok.example/v1', 'fast'))

    def test_model_step_recommends_chatgpt_and_keeps_own_key(self):
        splash = Splash()
        self.onboarding._draw_sign_in(splash, self.context)
        names = [r[1] for r in splash.operators()]
        self.assertNotIn("loopcut.sign_in", names)
        self.assertIn(("label", "Recommended"), splash.records)
        self.assertIn("loopcut.chatgpt_sign_in", names)
        self.assertIn("loopcut.onboarding_own_key", names)
        self.assertNotIn("Use Loopcut's models", [r[2] for r in splash.operators()])

    def test_chatgpt_waiting_keeps_refreshing_the_splash(self):
        region = Mock()
        self.onboarding._popups.add(region)
        self.chatgpt_ui._state.update(status="waiting", url="https://sign-in.test")
        with patch.object(self.onboarding, "_region_exists", return_value=True):
            self.assertEqual(self.onboarding._refresh_popups(), self.onboarding._REFRESH_SECONDS)
        region.tag_refresh_ui.assert_called_once()
        self.chatgpt_ui._state["status"] = "idle"
        with patch.object(self.onboarding, "_region_exists", return_value=True):
            self.assertIsNone(self.onboarding._refresh_popups())

    def test_loopcut_sign_in_selects_provider_saves_and_advances(self):
        self.prefs.provider = "OPENAI"
        self.onboarding._state["step"] = "account"
        with patch.object(self.account.credentials, "store") as store:
            self.account.on_signed_in.append(self.onboarding._on_signed_in)
            try:
                self.account._store_key("fixture-only")
            finally:
                self.account.on_signed_in.clear()
        store.assert_called_once_with(self.account.BASE_URL, "fixture-only")
        self.assertEqual(self.prefs.provider, "LOOPCUT")
        self.assertEqual(self.onboarding._state["step"], "connect")
        self.fake.ops.wm.save_userpref.assert_called_once()

    def test_chatgpt_sign_in_selects_provider_saves_and_advances(self):
        self.chatgpt_ui._finished({"signed_in": True, "plan_usage": True}, "")
        self.assertEqual(self.prefs.provider, "CHATGPT")
        self.assertEqual(self.onboarding._state["step"], "privacy")
        self.fake.ops.wm.save_userpref.assert_called_once()

    def test_switching_to_own_key_cancels_a_late_chatgpt_completion(self):
        session = self.state.new_session()
        with patch.object(self.host, "_open_settings"):
            self.host._do_action(session, ("own_key", None))
        self.assertEqual(self.prefs.provider, "OPENAI")
        self.chatgpt_ui._finished({"signed_in": True, "plan_usage": True}, "")
        self.assertEqual(self.prefs.provider, "OPENAI")
        self.assertEqual(self.onboarding._state["step"], "connect")
        self.fake.ops.wm.save_userpref.assert_not_called()

    def test_no_plan_permission_never_shows_a_ready_route(self):
        self.prefs.provider = "CHATGPT"
        with patch.object(self.chatgpt_ui.chatgpt_auth, "status", return_value={"signed_in": True, "plan_usage": False}):
            self.assertFalse(self.onboarding._connected())
            splash = Splash()
            self.onboarding._draw_sign_in(splash, self.context)
        self.assertIn("loopcut.chatgpt_sign_in", [r[1] for r in splash.operators()])
        self.assertNotIn("Continue on your ChatGPT plan", [r[2] for r in splash.operators()])

    def test_chatgpt_error_offers_another_account_even_with_a_saved_loopcut_account(self):
        self.prefs.provider = "CHATGPT"
        self.chatgpt_ui._state.update(status="error", error="Use another account")
        with patch.object(self.account, "signed_in", return_value=True):
            splash = Splash()
            self.onboarding._draw_sign_in(splash, self.context)
        another = next(r for r in splash.operators() if r[2] == "Use another ChatGPT account")
        self.assertTrue(another[3].new_account)
        self.assertIn("loopcut.onboarding_own_key", [r[1] for r in splash.operators()])

    def test_own_key_switches_from_either_sign_in_route_and_can_continue(self):
        for provider in ("LOOPCUT", "CHATGPT"):
            self.prefs.provider = provider
            op = self.onboarding.LOOPCUT_OT_onboarding_own_key()
            op.enable = True
            self.assertEqual(op.execute(self.context), {"FINISHED"})
            self.assertEqual(self.prefs.provider, "OPENAI")
            splash = Splash()
            with patch.object(self.onboarding.config, "load", return_value=object()):
                self.onboarding._draw_connect(splash, self.context)
            continuation = next(r for r in splash.operators() if r[2] == "Continue")
            self.assertTrue(continuation[4].enabled)
            self.assertEqual(continuation[3].step, "privacy")
            self.assertIn(("prop", "api_key"), splash.records)
            self.assertIn(("prop", "model"), splash.records)

    def test_missing_model_explains_how_to_enable_continue(self):
        self.onboarding._state["own_key"] = True
        self.prefs.provider = "OPENAI"
        splash = Splash()
        with patch.object(self.onboarding.config, "load", side_effect=self.onboarding.config.ConfigError("Missing model")):
            self.onboarding._draw_connect(splash, self.context)
        continuation = next(r for r in splash.operators() if r[2] == "Continue")
        self.assertFalse(continuation[4].enabled)
        labels = " ".join(r[1] for r in splash.records if r[0] == "label")
        self.assertIn("Missing model", labels)
        self.assertIn("Correct the connection fields", labels)

    def test_invalid_model_url_does_not_send_a_key_and_explains_recovery(self):
        self.prefs.provider = "CUSTOM"
        self.prefs.base_url = "http://remote.test/v1"
        op = self.settings.LOOPCUT_OT_fetch_models()
        op.report = Mock()
        with patch.object(self.settings.urllib.request, "urlopen") as http:
            self.assertEqual(op.execute(self.context), {"CANCELLED"})
        http.assert_not_called()
        self.assertIn("Check the URL and key", op.report.call_args.args[1])

    def test_failed_device_sign_in_has_a_retry_and_recovery_instruction(self):
        with patch.object(self.account, "_post", side_effect=self.account.AccountError("Offline")):
            self.account._run_sign_in()
        self.assertEqual(self.account.status()["status"], "error")
        self.assertIn("Try Sign in to Loopcut again", self.account.status()["error"])
        with patch.object(self.account.threading, "Thread") as worker:
            self.assertTrue(self.account.sign_in())
            worker.return_value.start.assert_called_once()

    def test_cancel_during_device_start_does_not_open_a_browser(self):
        self.account._cancel.set()
        reply = {"device_code": "fixture", "user_code": "TEST", "verification_url": "https://sign-in.test"}
        with patch.object(self.account, "_post", return_value=reply), patch.object(self.account, "open_browser") as browser:
            self.account._run_sign_in()
        browser.assert_not_called()
        self.assertEqual(self.account.status()["status"], "idle")

    def test_example_only_fills_input_and_accepted_send_dismisses_for_good(self):
        session = self.state.new_session()
        with patch.object(self.host.agent, "send", return_value=True) as send:
            self.host._do_action(session, ("example_request", 1))
            send.assert_not_called()
            self.assertEqual(session["input"], self.host.layout.EXAMPLE_REQUESTS[1])
            self.assertTrue(session["focused"])
            self.host._submit(session)
        self.assertEqual(session["input"], "")
        self.assertTrue(self.prefs.first_message_sent)
        self.state.ui.clear()  # Simulate reopening the app with the saved preference.
        self.assertTrue(self.onboarding.hints_dismissed())

    def test_rejected_send_keeps_hints_and_the_draft(self):
        session = self.state.new_session()
        session["input"] = "Add a floor"
        with patch.object(self.host.agent, "send", return_value=False):
            self.host._submit(session)
        self.assertFalse(self.onboarding.hints_dismissed())
        self.assertEqual(session["input"], "Add a floor")

    def test_account_message_wins_and_credits_refresh_during_a_turn(self):
        session = self.state.new_session()
        service = {"credits": "100 credits", "message": "Saving account settings…"}
        with patch.object(self.cloud.cloud_account, "panel_data", return_value=service), \
                patch.object(self.cloud, "coming_soon", return_value=[]), \
                patch.object(self.cloud, "ui_message", return_value="Update Loopcut") as message:
            first = self.host._cloud_panel_state(session)
            self.assertEqual(first["cloud_message"], service["message"])
            self.assertEqual(first["credits"], "100 credits")
            message.assert_not_called()
            session["busy"] = True
            service.update(credits="90 credits", message="")
            during = self.host._cloud_panel_state(session)
            self.assertEqual(during["credits"], "90 credits")
            self.assertEqual(during["cloud_message"], "")
            self.assertTrue(during["cloud_message_reserved"])
            session["busy"] = False
            self.assertEqual(self.host._cloud_panel_state(session)["cloud_message"], "Update Loopcut")

    def test_draw_passes_both_feature_sets_with_one_cloud_snapshot(self):
        session = self.state.new_session()
        area = Mock()
        context = types.SimpleNamespace(area=area, region=types.SimpleNamespace(width=420, height=600),
            scene={}, view_layer=None, preferences=types.SimpleNamespace(system=types.SimpleNamespace(ui_scale=1)))
        runtime = types.SimpleNamespace(signed_in=True)
        with patch.object(self.fake, "context", context), \
                patch.object(self.cloud, "_runtime", runtime), \
                patch.object(self.cloud, "panel_seen"), \
                patch.object(self.cloud.cloud_account, "panel_data", return_value={"credits": "100 credits", "message": "Saving…"}) as service, \
                patch.object(self.cloud, "coming_soon", return_value=[]) as catalog, \
                patch.object(self.host, "is_host", return_value=True), \
                patch.object(self.state, "session", return_value=session), \
                patch.object(self.host, "_config_cached", return_value=("fast", False, 24000)), \
                patch.object(self.host, "_checkpoint_statuses", return_value={}), \
                patch.object(self.host.update, "banner", return_value=None), \
                patch.object(self.host.draw, "measure", Mock(), create=True), \
                patch.object(self.host.draw, "render", create=True), \
                patch.object(self.host.layout, "build", return_value={"max_scroll": 0}) as build:
            self.host.draw_area()
        ui = build.call_args.args[-1]
        self.assertEqual(ui["cloud_message"], "Saving…")
        self.assertEqual(ui["credits"], "100 credits")
        self.assertEqual(ui["service_policy"], self.cloud.cloud_account.DEFAULTS)
        self.assertTrue(ui["service_signed_in"])
        self.assertEqual(ui["project_override"], "inherit")
        self.assertEqual(set(ui["sign_in"]), {"loopcut", "chatgpt"})
        self.assertFalse(ui["hints_dismissed"])
        self.assertIn("update", ui)
        self.assertIn("cloud_jobs", ui)
        service.assert_called_once_with(runtime)
        catalog.assert_called_once_with()

    def test_service_actions_keep_settings_project_and_upgrade_routes(self):
        session = self.state.new_session()
        self.host._do_action(session, ("cloud_page", 4))
        self.assertEqual(self.state.ui["coming_soon_start"], 4)
        self.state.ui.update(model_menu=True, picker={})
        self.host._do_action(session, ("service_settings", None))
        self.assertTrue(self.state.ui["service_settings"])
        self.assertFalse(self.state.ui["model_menu"])
        self.assertIsNone(self.state.ui["picker"])
        self.prefs.private_mode = True
        self.host._do_action(session, ("service_toggle", "private_mode"))
        self.assertFalse(self.prefs.private_mode)
        with patch.object(self.fake, "context", types.SimpleNamespace(scene={})), \
                patch.object(self.cloud, "sync_settings") as sync:
            self.host._do_action(session, ("project_generation", "on"))
            self.assertEqual(self.fake.context.scene["loopcut_no_generation"], "on")
            sync.assert_called_once_with()
        session["items"].append({"cloud_error": {"upgrade": "https://loopcut.org/pricing"}})
        with patch.object(self.account, "open_browser") as browser, patch.object(self.host.config, "BETA", False):
            self.host._do_action(session, ("cloud_upgrade", 0))
            browser.assert_called_once_with("https://loopcut.org/pricing")
            session["items"][0]["cloud_error"]["upgrade"] = "file:///invalid"
            with self.assertRaises(ValueError):
                self.host._do_action(session, ("cloud_upgrade", 0))
            self.assertEqual(browser.call_count, 1)

    def test_sign_in_and_dismiss_actions_keep_each_route(self):
        session = self.state.new_session()
        for action, suffix in (("sign_in", "sign_in"), ("sign_in_copy", "copy_link"), ("sign_in_cancel", "cancel")):
            for provider in ("loopcut", "chatgpt"):
                with self.subTest(action=action, provider=provider):
                    self.fake.ops.reset_mock()
                    self.host._do_action(session, (action, provider))
                    name = "chatgpt_" + suffix if provider == "chatgpt" else {
                        "sign_in": "sign_in", "copy_link": "copy_sign_in_link", "cancel": "cancel_sign_in"}[suffix]
                    self.assertEqual(self.fake.ops.loopcut.mock_calls, [unittest.mock.call.__getattr__(name)()])
        self.host._do_action(session, ("sign_in_new", None))
        self.fake.ops.loopcut.chatgpt_sign_in.assert_called_once_with(new_account=True)
        with patch.object(self.cloud, "dismiss_ui_message") as dismiss:
            self.host._do_action(session, ("cloud_dismiss", "Update Loopcut"))
            dismiss.assert_called_once_with("Update Loopcut")

    def test_late_cloud_catalog_and_message_wait_until_the_turn_ends(self):
        session = self.state.new_session()
        rows = [{"name": "future", "title": "Fixture title", "state": "ready"}]
        with patch.object(self.cloud, "coming_soon", return_value=rows) as catalog, \
                patch.object(self.cloud, "ui_message", return_value="") as message:
            first = self.host._cloud_panel_state(session)
            session["busy"] = True
            catalog.return_value = [{**rows[0], "state": "thanks", "notify_sign_in": True}, {"name": "late", "title": "Later", "state": "ready"}]
            message.return_value = "Update Loopcut"
            during = self.host._cloud_panel_state(session)
            self.assertEqual(len(during["coming_soon"]), len(first["coming_soon"]))
            self.assertEqual(during["coming_soon"][0]["state"], "thanks")
            self.assertTrue(during["coming_soon"][0]["notify_sign_in"])
            self.assertEqual(during["cloud_message"], "")
            session["busy"] = False
            after = self.host._cloud_panel_state(session)
            self.assertEqual(len(after["coming_soon"]), 2)
            self.assertEqual(after["cloud_message"], "Update Loopcut")
            session["busy"] = True
            message.return_value = ""
            dismissed = self.host._cloud_panel_state(session)
            self.assertEqual(dismissed["cloud_message"], "")
            self.assertTrue(dismissed["cloud_message_reserved"])


if __name__ == "__main__":
    unittest.main()
