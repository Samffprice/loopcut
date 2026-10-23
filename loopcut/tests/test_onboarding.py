"""First-run route regressions with a recording splash layout and a private fake bpy.

Load Blender-facing modules under a private package so the suite's other bpy stubs and
agent fixtures are untouched. No browser, HTTP, preferences file or Blender is used.
"""

import contextlib
import json
import importlib
import importlib.util
import io
import tempfile
import threading
import time
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
import sys
import types
import unittest
import unittest.mock
from datetime import datetime, timezone
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

    def prop(self, prefs, name, **kwargs):
        self.records.append(("prop", name))

    def operator(self, name, **kwargs):
        props = types.SimpleNamespace()
        self.records.append(("operator", name, kwargs.get("text", ""), props, self, kwargs))
        return props

    def menu(self, name, **kwargs):
        self.records.append(("menu", name))

    box = column

    def template_icon(self, **kwargs):
        self.records.append(("icon", kwargs.get("icon_value")))

    def progress(self, **kwargs):
        self.records.append(("progress", kwargs.get("factor")))

    def grid_flow(self, **kwargs):
        return type(self)(self.records)

    def operators(self):
        return [r for r in self.records if r[0] == "operator"]


class StyledSplash(Splash):
    """The fork's layout API: styled text, padded boxes and operators drawn as text links."""
    def column(self, **kwargs):
        return StyledSplash(self.records)

    row = split = box = column

    def box_padded(self, padding=5.0):
        self.records.append(("box", padding))
        return StyledSplash(self.records)

    def label_styled(self, text="", color=(1.0, 1.0, 1.0), **kwargs):
        self.records.append(("label", text, tuple(color), kwargs))

    def operator_link(self, operator, text, color=(0.61, 0.61, 0.64), hover=True):
        props = types.SimpleNamespace()
        self.records.append(("operator", operator, text, props, self, {"color": tuple(color), "hover": hover}))
        return props


class OnboardingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = types.ModuleType("bpy")
        cls.fake.types = types.SimpleNamespace(**{name: type(name, (), {}) for name in
            ("Operator", "AddonPreferences", "Menu", "FileHandler", "SpaceTextEditor", "OperatorFileListElement")})
        cls.fake.types.AddonPreferences = AddonPreferences
        cls.fake.props = Mock()
        cls.fake.ops = Mock()
        cls.fake.app = types.SimpleNamespace(version=(5, 2, 0), timers=Mock(), background=False)
        cls.fake.context = types.SimpleNamespace()
        cls.fake.utils = types.ModuleType("bpy.utils")
        cls.fake.utils.previews = types.ModuleType("bpy.utils.previews")
        cls.modules = patch.dict(sys.modules, {"bpy": cls.fake, "bpy.utils": cls.fake.utils,
            "bpy.utils.previews": cls.fake.utils.previews, NAME + ".ui.draw": types.ModuleType(NAME + ".ui.draw")})
        cls.modules.start()
        cls.addClassCleanup(cls.modules.stop)  # Runs even when the imports below fail.
        spec = importlib.util.spec_from_file_location(NAME, PACKAGE / "__init__.py",
                                                     submodule_search_locations=[str(PACKAGE)])
        package = importlib.util.module_from_spec(spec)
        sys.modules[NAME] = package
        spec.loader.exec_module(package)
        for name in ("onboarding", "account", "chatgpt_ui", "settings", "state", "cloud", "ui.host"):
            setattr(cls, name.replace("ui.", ""), importlib.import_module(NAME + "." + name))

    def setUp(self):
        self.fake.app.background = False  # An interactive Blender; the headless tests say otherwise.
        self.fake.ops.reset_mock()
        self.fake.app.timers.reset_mock()
        self.onboarding._state.update(step="connect", own_key=False, left_chatgpt=False, error="", no_account=False,
                                      claimed=False, waiting_since=0.0, credits_asked=0.0, region=None,
                                      finished=False, imported=None, failed=[], drawn=None, existing=False,
                                      change_model=False)
        self.onboarding._popups.clear()
        self.onboarding._reported.clear()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.progress = Path(folder.name) / "onboarding.json"
        p = patch.object(self.onboarding, "_progress_path", return_value=self.progress)
        p.start()
        self.addCleanup(p.stop)
        self.account._state.update(status="idle", error="", url="", user_code="")
        self.account._cancel.clear()
        for name, value in (("pending_device", None), ("store_pending_device", None)):
            p = patch.object(self.account.credentials, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        for name, value in (("_thumbnail", 0), ("_row_mark", {"icon": "LOOPCUT"})):  # No previews outside Blender.
            p = patch.object(self.onboarding, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        self.chatgpt_ui._state.update(status="idle", error="", url="")
        self.chatgpt_ui._cancel.clear()
        self.chatgpt_ui._checked.clear()
        self.chatgpt_ui._ready.update(status="idle", code="", account=(), model="", selected="", note="")
        self.state.ui.clear()
        self.host._runtime.clear()
        self.prefs = types.SimpleNamespace(provider="LOOPCUT", first_message_sent=False, share_conversations=True,
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
        p = patch.object(self.cloud, "refresh_async")  # The account's credits, fetched on the account step.
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(self.onboarding, "missing_operators", return_value=[])  # The fake bpy registers none.
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(self.onboarding, "_registered", True)
        p.start()
        self.addCleanup(p.stop)
        self.onboarding._state["starting_since"] = 0.0

    def press_step(self, splash, text):
        record = next(r for r in splash.operators() if r[1] == 'loopcut.onboarding_step' and r[2] == text)
        op = self.onboarding.LOOPCUT_OT_onboarding_step()
        op.step, op.save = record[3].step, record[3].save
        op.provider = getattr(record[3], 'provider', 'CURRENT')
        self.assertTrue(record[4].enabled)
        self.assertEqual(op.execute(self.context), {'FINISHED'})

    def test_setup_and_import_lead_to_the_required_account_step_even_with_a_model(self):
        for previous in ('setup', 'import'):
            self.onboarding._state['step'] = previous
            with patch.object(self.onboarding, '_connected', return_value=True):
                splash = Splash()
                self.assertTrue(self.onboarding.draw_splash(splash, self.context, first_run=False))
            self.assertEqual(self.onboarding._state['step'], 'account')
            self.assertEqual(json.loads(self.progress.read_text())['step'], 'account')
            self.assertIn(('label', 'Create your free account'), splash.records)
            self.assertIn(('label', '● ○ ○ ○ ○'), splash.records)
            self.assertIn('loopcut.sign_in', [r[1] for r in splash.operators()])
            self.assertNotIn('loopcut.chatgpt_sign_in', [r[1] for r in splash.operators()])
            texts = [r[2] for r in splash.operators()]
            self.assertNotIn('Skip for now', texts)  # Required: no way past it until a sign-in fails.
            self.assertNotIn('Continue without an account', texts)
            self.assertNotIn('Continue', texts)

    def test_screenshot_onboarding_fixtures_draw_the_requested_screens(self):
        spec = importlib.util.spec_from_file_location('c11_fixtures', PACKAGE.parents[2] / 'loopcut/harness/c11_shots.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        fixtures = module.fixtures(self.state)
        for name, title in (('onboarding_account', 'Create your free account'),
                            ('onboarding_account_failed', 'Create your free account'),
                            ('onboarding_connect', 'Choose the model for your chat')):
            ui = fixtures[name]['ui']
            self.onboarding._state['step'] = ui['onboarding_step']
            splash = Splash()
            status = ui.get('sign_in', {}).get('loopcut', {'status': 'idle'})
            with patch.object(self.account, 'status', return_value=status):
                self.onboarding.draw_splash(splash, self.context, first_run=False)
            labels = [r[1] for r in splash.records if r[0] == 'label']
            self.assertIn(title, labels)
            self.assertEqual("Couldn't reach Loopcut" in ' '.join(labels), name == 'onboarding_account_failed')
            self.assertNotIn('HTTP 404', str(labels))

    def test_existing_accounts_show_continue_and_model_choices_select_the_provider(self):
        self.prefs.provider = 'OPENAI'
        self.onboarding._state['step'] = 'account'
        with patch.object(self.account, 'signed_in', return_value=True), \
                patch.object(self.account, 'account_line', return_value='person@example.test'):
            splash = Splash()
            self.onboarding.draw_splash(splash, self.context, first_run=False)
            self.assertIn(('label', "You're signed in"), splash.records)
            self.assertIn(('label', 'person@example.test'), splash.records)
            self.assertNotIn('loopcut.sign_in', [r[1] for r in splash.operators()])
            self.press_step(splash, 'Continue')
            self.assertEqual(self.onboarding._state['step'], 'how')  # How Loopcut works, then the model.
            how = Splash()
            self.onboarding._draw_how(how, self.context)
            self.press_step(how, 'Continue')
            self.assertEqual(self.onboarding._state['step'], 'connect')
            model = Splash()
            self.onboarding._draw_connect(model, self.context)
            self.press_step(model, "Use Loopcut")
            self.assertEqual(self.prefs.provider, 'LOOPCUT')
            self.assertEqual(self.onboarding._state['step'], 'privacy')
        self.onboarding._state['step'] = 'connect'
        with patch.object(self.chatgpt_ui.chatgpt_auth, 'status', return_value={
                'signed_in': True, 'plan_usage': True, 'email': 'person@example.test'}):
            model = Splash()
            self.onboarding._draw_connect(model, self.context)
            self.assertIn(('label', 'person@example.test'), model.records)  # The ChatGPT option, signed in.
            self.assertTrue(any(r[0] == "operator" and r[2] == "Use ChatGPT" for r in model.records))
            with patch.object(self.chatgpt_ui, "request_readiness"):
                self.chatgpt_ui.LOOPCUT_OT_chatgpt_check().execute(self.context)
            with patch.object(self.chatgpt_ui, "can_continue", return_value=True):
                model = Splash()
                self.onboarding._draw_connect(model, self.context)
                self.press_step(model, "Continue")
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
                self.account._cancel.clear()
                with patch.object(self.account.urllib.request, 'urlopen', side_effect=failure), \
                        patch.object(self.account, 'open_browser') as browser, \
                        redirect_stdout(io.StringIO()) as log, redirect_stderr(log):
                    self.account._run_sign_in()
                browser.assert_not_called()
                self.assertEqual(self.account.status()['status'], 'error')
                self.assertTrue(log.getvalue())
                self.onboarding._state.update(step='account', no_account=False)
                splash = Splash()
                self.onboarding.draw_splash(splash, self.context, first_run=False)
                labels = [r[1] for r in splash.records if r[0] == 'label']
                self.assertEqual(labels[:3], ['Set up Loopcut', '● ○ ○ ○ ○', 'Create your free account'])
                self.assertIn("Couldn't reach Loopcut", labels)  # Never the raw error.
                self.assertNotIn('HTTP', ' '.join(labels))
                self.assertNotIn('refused', ' '.join(labels))
                self.assertIn('Try again', [r[2] for r in splash.operators() if r[1] == 'loopcut.sign_in'])
                with patch.object(self.onboarding, '_report') as report:
                    self.press_no_account(splash)
                report.assert_called_once_with('loopcut_account', 'skip', False, 'sign_in_failed')
                self.assertTrue(self.onboarding._state['no_account'])
                model = Splash()
                self.onboarding.draw_splash(model, self.context, first_run=False)
                names = [r[1] for r in model.operators()]
                self.assertIn('loopcut.chatgpt_sign_in', names)
                self.assertIn('loopcut.onboarding_own_key', names)
                self.assertNotIn("Use Loopcut", [r[2] for r in model.operators()])
                self.assertIn('Sign in', [r[2] for r in model.operators()])
                self.assertNotIn("Couldn't reach Loopcut", str(model.records))

    def test_account_poll_failure_is_quiet_and_skip_suppresses_a_late_key(self):
        reply = {'device_code': 'd' * 43, 'user_code': 'TEST-CODE', 'verification_url': self.account.SERVICE_URL + '/link?code=TEST-CODE', 'expires_at': datetime.fromtimestamp(time.time() + 900, timezone.utc).isoformat()}
        self.onboarding._state['step'] = 'account'
        with patch.object(self.account, '_post', side_effect=[reply, self.account.AccountError('Sign-in was denied')]), \
                patch.object(self.account._cancel, 'wait', return_value=False), \
                patch.object(self.account, 'open_browser'), redirect_stdout(io.StringIO()) as log:
            self.account._run_sign_in()
        self.assertIn('Sign-in was denied', log.getvalue())
        splash = Splash()
        self.onboarding._draw_account(splash, self.context)
        self.assertIn(('label', "Couldn't reach Loopcut"), splash.records)
        self.press_no_account(splash)
        self.assertEqual(self.onboarding._state['step'], 'connect')
        # Approval can already be queued on the main thread when the user carries on without it.
        self.account._cancel.clear()
        self.onboarding._state['step'] = 'account'
        def skip_before_store(callback):
            self.press_no_account(splash)
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

    def test_the_device_link_reports_the_add_on_blender_and_platform(self):
        """The server records these with the link (activation-web's linkMetadata): the contract header on every
        device-flow request, os and arch in the poll body."""
        requests = []
        with patch.object(self.account, '_send', side_effect=lambda request: requests.append(request) or {}):
            self.account._post('/api/device/poll', {'device_code': 'x', **self.account._platform()})
        [request] = requests
        self.assertTrue(request.get_header('X-loopcut-client', '').startswith('loopcut-addon/'))
        body = json.loads(request.data)
        self.assertIn(body['os'], ('mac', 'windows', 'linux'))
        self.assertIn(body['arch'], ('arm64', 'x86_64', 'other'))
        reply = {'device_code': 'd' * 43, 'user_code': 'TEST-CODE', 'verification_url': self.account.SERVICE_URL + '/link?code=TEST-CODE', 'expires_at': datetime.fromtimestamp(time.time() + 900, timezone.utc).isoformat()}
        with patch.object(self.account, '_post', side_effect=[reply, {'status': 'denied'}]) as post, \
                patch.object(self.account._cancel, 'wait', return_value=False), patch.object(self.account, 'open_browser'):
            self.account._cancel.clear()
            self.account._run_sign_in()
        self.assertEqual(set(post.call_args_list[1].args[1]), {'device_code', 'os', 'arch'})

    def device_reply(self, seconds=900):
        return {"device_code": "d" * 43, "user_code": "TEST-CODE",
                "verification_url": self.account.SERVICE_URL + "/link?code=TEST-CODE",
                "expires_at": datetime.fromtimestamp(time.time() + seconds, timezone.utc).isoformat(),
                "expires_local": time.time() + seconds, "interval": 3}

    def test_a_fresh_code_uses_a_local_deadline_when_this_clock_is_off(self):
        """Server time says the code expired an hour ago by this clock; it is still polled and the key stored."""
        reply = {**self.device_reply(-3600)}
        del reply["expires_local"]
        self.account._cancel.clear()
        with patch.object(self.account, "_post", side_effect=[reply, {"status": "approved", "api_key": "fixture-key"}]) as post, \
                patch.object(self.account._cancel, "wait", return_value=False), patch.object(self.account, "open_browser"), \
                patch.object(self.account, "_store_key") as store, patch.object(self.account, "_refresh_account"), \
                patch.object(self.account.mainthread, "run_on_main", side_effect=lambda callback: (callback(), Mock())[1]), \
                redirect_stdout(io.StringIO()):
            self.account._run_sign_in()
        self.assertEqual(post.call_args_list[1].args[0], "/api/device/poll")
        store.assert_called_once_with("fixture-key")

    def test_account_transient_poll_errors_retry_the_same_code_then_store_the_key(self):
        for code in ("unreachable", "http_500", "http_503", "http_429"):
            self.account._cancel.clear()
            with self.subTest(code=code), \
                    patch.object(self.account, "_post", side_effect=[self.device_reply(),
                        self.account.AccountError("temporary", code), {"status": "pending"},
                        {"status": "approved", "api_key": "fixture-key"}]) as post, \
                    patch.object(self.account._cancel, "wait", return_value=False), \
                    patch.object(self.account, "open_browser"), patch.object(self.account, "_store_key", return_value=3) as store, \
                    patch.object(self.account, "_refresh_account"), \
                    patch.object(self.account.mainthread, "run_on_main", side_effect=lambda fn: types.SimpleNamespace(result=fn)):
                self.account._run_sign_in()
                store.assert_called_once_with("fixture-key")
                self.assertEqual([call.args[0] for call in post.call_args_list],
                                 ["/api/device/start"] + ["/api/device/poll"] * 3)
                self.assertEqual(self.account.status()["error"], "")
                self.assertEqual(self.account.status()["status"], "idle")

    def test_an_old_cancelled_worker_cannot_change_a_new_attempt(self):
        old = self.account._cancel
        def replaced_attempt(*args):
            old.set()
            self.account._cancel = threading.Event()
            self.account._state.update(status="waiting", user_code="NEW1-CODE", error="")
            return self.device_reply()
        with patch.object(self.account, "_post", side_effect=replaced_attempt), \
                patch.object(self.account, "open_browser") as browser:
            self.account._run_sign_in(cancel=old)
        browser.assert_not_called()
        self.assertEqual(self.account.status()["user_code"], "NEW1-CODE")
        self.account.credentials.store_pending_device.assert_not_called()

    def test_cancel_still_works_when_saved_code_cleanup_fails(self):
        self.account.credentials.store_pending_device.side_effect = OSError("read-only")
        with redirect_stdout(io.StringIO()):
            self.account.cancel_sign_in()
        self.assertTrue(self.account._cancel.is_set())

    def test_restart_resumes_an_approved_code_without_a_new_code_or_browser(self):
        reply = self.device_reply(180)
        self.account.credentials.pending_device.return_value = reply
        with patch.object(self.account, "_post", return_value={"status": "approved", "api_key": "fixture-key"}) as post, \
                patch.object(self.account._cancel, "wait", return_value=False), \
                patch.object(self.account, "open_browser") as browser, \
                patch.object(self.account, "_store_key", return_value=3) as store, \
                patch.object(self.account, "_refresh_account"), \
                patch.object(self.account.mainthread, "run_on_main", side_effect=lambda fn: types.SimpleNamespace(result=fn)):
            self.account._run_sign_in(resume_only=True)
        browser.assert_not_called()
        store.assert_called_once_with("fixture-key")
        self.assertEqual(post.call_args.args[0], "/api/device/poll")
        self.account.credentials.store_pending_device.assert_called_with(self.account.SERVICE_URL, None)

    def test_restart_discards_expired_or_foreign_saved_codes_without_creating_one(self):
        for reply in (self.device_reply(-1), {**self.device_reply(), "verification_url": "https://other.test/link"},
                      {**self.device_reply(), "interval": float("nan")}, {"device_code": "bad"}):
            self.account.credentials.pending_device.return_value = reply
            with self.subTest(reply=reply), patch.object(self.account, "_post") as post:
                self.account._run_sign_in(resume_only=True)
                post.assert_not_called()
                self.assertEqual(self.account.status()["status"], "idle")

    def test_poll_transport_failure_is_bounded_by_the_code_lifetime(self):
        """Retries back off and stop when the code's 15 minutes (by this clock) are up."""
        start = time.time()
        clock = [start]
        def wait(seconds):
            clock[0] += seconds
            return False
        def post(path, body):
            if path == "/api/device/start":
                return self.device_reply(10)
            raise self.account.AccountError("offline", "unreachable")
        with patch.object(self.account.time, "time", side_effect=lambda: clock[0]), \
                patch.object(self.account._cancel, "wait", side_effect=wait), \
                patch.object(self.account, "_post", side_effect=post) as posted, \
                patch.object(self.account, "open_browser"):
            self.account._run_sign_in()
        self.assertEqual(self.account.status()["status"], "error")
        self.assertIn("expired", self.account.status()["error"])
        polls = [call for call in posted.call_args_list if call.args[0] == "/api/device/poll"]
        self.assertTrue(5 < len(polls) < 60)
        self.assertGreaterEqual(clock[0] - start, 15 * 60 - 1)

    def test_account_waiting_allows_cancel_and_only_a_slow_sign_in_offers_to_go_on(self):
        for status in ('waiting', 'starting'):
            self.account._state.update(status=status, user_code='TEST', url='https://sign-in.test')
            self.onboarding._state.update(step='account', waiting_since=0.0)
            splash = Splash()
            with patch.object(self.onboarding, '_watch_popup') as watch:
                self.onboarding._draw_account(splash, self.context)
                watch.assert_called_once_with(self.context)
            self.assertIn('loopcut.cancel_sign_in', [r[1] for r in splash.operators()])
            self.assertNotIn('loopcut.onboarding_no_account', [r[1] for r in splash.operators()])
            self.onboarding._state['waiting_since'] -= self.onboarding.SIGN_IN_PATIENCE + 1
            splash = Splash()
            with patch.object(self.onboarding, '_watch_popup'), patch.object(self.onboarding, '_report') as report:
                self.onboarding._draw_account(splash, self.context)
                self.press_no_account(splash)
            report.assert_called_with('loopcut_account', 'skip', False, 'sign_in_slow')
            self.assertEqual(self.onboarding._state['step'], 'connect')
            self.assertTrue(self.account._cancel.is_set())
            self.account._cancel.clear()

    def press_no_account(self, splash):
        record = next(r for r in splash.operators() if r[1] == 'loopcut.onboarding_no_account')
        self.assertEqual(record[2], 'Continue without an account')
        self.assertEqual(self.onboarding.LOOPCUT_OT_onboarding_no_account().execute(self.context), {'FINISHED'})

    def test_eleven_entries_reach_the_vote_view_from_the_strip_and_back(self):
        from coming_soon_fixture import manifest
        measure = lambda font, size, text: len(text) * size * .6
        with tempfile.TemporaryDirectory() as directory:
            runtime = self.cloud.Runtime(types.SimpleNamespace(api_key=''), directory)
            runtime.manifest = manifest()
            with patch.object(self.cloud, '_runtime', runtime), patch.object(self.cloud, 'refresh_votes') as refresh, \
                    patch.object(self.cloud, 'vote') as vote:
                session = self.state.new_session()
                panel = self.host._cloud_panel_state(session)
                self.assertEqual(len(panel['coming_soon']), 11)
                self.assertEqual(panel['vote_notice'], '')
                display = self.host.layout.build(session, 420, 800, 1, measure, ui={**self.state.ui, **panel})
                self.host._do_action(session, next(h['action'] for h in display['hits'] if h['id'] == 'vote.strip'))
                refresh.assert_called_once_with()
                self.assertEqual(self.state.ui['view'], 'vote')
                panel = self.host._cloud_panel_state(session)
                display = self.host.layout.build(session, 420, 800, 1, measure, ui={**self.state.ui, **panel})
                actions = [h['action'] for h in display['hits'] if h['action'][0] == 'vote']
                self.assertEqual({name for _, name in actions}, {row['name'] for row in panel['coming_soon']})
                self.host._do_action(session, actions[0])
                vote.assert_called_once_with(actions[0][1])
                self.host._do_action(session, next(h['action'] for h in display['hits'] if h['id'] == 'vote.back'))
                self.assertEqual(self.state.ui['view'], 'chat')
                self.fake.ops.wm.save_userpref.assert_not_called()

    def test_service_model_cache_can_be_unloaded_on_the_first_turn(self):
        with patch.dict(self.account._state, models=None):
            self.assertFalse(self.account.keeps_context(self.account.BASE_URL, 'fast'))
        with patch.dict(self.account._state, models=[{'id': 'fast', 'keep_context': True}]):
            self.assertTrue(self.account.keeps_context(self.account.BASE_URL, 'fast'))
            self.assertFalse(self.account.keeps_context('https://byok.example/v1', 'fast'))

    def test_model_step_offers_three_routes_and_requires_one(self):
        with patch.object(self.account, "signed_in", return_value=True), \
                patch.object(self.onboarding.config, "billing_open", return_value=True):
            splash = Splash()
            self.assertTrue(self.onboarding.draw_splash(splash, self.context, first_run=False))
        names = [r[1] for r in splash.operators()]
        texts = [r[2] for r in splash.operators()]
        labels = " ".join(r[1] for r in splash.records if r[0] == "label")
        self.assertIn("● ● ● ○ ○", labels)
        self.assertIn("Recommended", labels)
        self.assertIn("Uses your ChatGPT Plus or Pro plan. Cloud tools still use credits.", labels)  # Never paid.
        self.assertEqual(texts[:3], ["Sign in with ChatGPT", "Use Loopcut", "Add a key"])
        self.assertIn("loopcut.onboarding_own_key", names)
        self.assertNotIn("Skip for now", texts)
        self.assertNotIn("loopcut.sign_in", names)
        # Signed out (only after "Continue without an account"): a way back to the account, and a way on.
        splash = Splash()
        self.onboarding._draw_sign_in(splash, self.context)
        texts = [r[2] for r in splash.operators()]
        self.assertNotIn("Use Loopcut", texts)
        back = next(r for r in splash.operators() if r[2] == "Sign in")
        self.assertEqual(back[3].step, "account")
        self.assertIn("Skip for now", texts)

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
        self.prefs.provider = "OPENAI"  # Chosen, but with no key: nothing that works to keep.
        self.onboarding._state["step"] = "account"
        with patch.object(self.account.credentials, "store") as store, \
                patch.object(self.account.config, "load", side_effect=self.account.config.ConfigError("no key")):
            self.account.on_signed_in.append(self.onboarding._on_signed_in)
            try:
                self.account._store_key("fixture-only")
            finally:
                self.account.on_signed_in.clear()
        store.assert_called_once_with(self.account.BASE_URL, "fixture-only")
        self.assertEqual(self.prefs.provider, "LOOPCUT")
        self.assertEqual(self.onboarding._state["step"], "account")  # Stays for the credits and Continue.
        self.fake.ops.wm.save_userpref.assert_called_once()

    def test_chatgpt_sign_in_selects_provider_saves_and_checks_before_advancing(self):
        with patch.object(self.chatgpt_ui, "request_readiness") as check:
            self.chatgpt_ui._finished({"signed_in": True, "plan_usage": True}, "")
        check.assert_called_once_with(force=True)
        self.assertEqual(self.prefs.provider, "CHATGPT")
        self.assertEqual(self.onboarding._state["step"], "connect")
        self.fake.ops.wm.save_userpref.assert_called_once()

    def test_readiness_refusals_show_recovery_and_keep_a_working_way_out(self):
        self.prefs.provider = "CHATGPT"
        for code, enabled in (("ready", True), ("offline", True), ("unsupported_model", False),
                              ("unsupported_country_region_territory", False), ("invalid_grant", False)):
            with self.subTest(code=code), self.chatgpt_plan(), \
                    patch.object(self.account, "signed_in", return_value=True), \
                    patch.object(self.chatgpt_ui, "readiness", return_value={"status": "done", "code": code, "note": ""}), \
                    patch.object(self.chatgpt_ui, "request_readiness"):
                splash = Splash()
                self.onboarding._draw_sign_in(splash, self.context)
                operators = [r for r in splash.records if r[0] == "operator"]
                proceed = next(r for r in operators if r[2] == "Continue")
                self.assertEqual(proceed[4].enabled, enabled)
                self.assertTrue(any(r[2] == "Use Loopcut" and r[4].enabled for r in operators))
                self.assertTrue(any(r[0] == "prop" and r[1] == "model" for r in splash.records))
                if code != "ready":
                    self.assertTrue(any(r[1] == "loopcut.chatgpt_check" for r in operators))

    def test_readiness_runs_in_a_worker_and_discards_stale_results(self):
        ready = self.chatgpt_ui
        self.prefs.provider, self.prefs.model = "CHATGPT", "gpt-6.1-sol"
        identity = ("client", "issuer", "subject")
        with patch.object(ready.chatgpt_ready, "account_key", return_value=identity), self.chatgpt_plan(), \
                patch.object(ready.chatgpt_ready, "verified", return_value=False), \
                patch.object(ready.chatgpt_ready, "check") as check, patch.object(ready.threading, "Thread") as worker:
            ready._start_readiness()
            check.assert_not_called()  # Not on Blender's main thread.
            worker.assert_called_once()
            worker.return_value.start.assert_called_once()
            self.assertEqual(ready.readiness()["status"], "checking")
            self.prefs.model = "gpt-6-sol"
            ready._readiness_finished({"code": "ready", "model": "gpt-6.1-sol", "account": identity},
                                      "gpt-6.1-sol", threading.Event())
            self.assertEqual(ready.readiness()["status"], "idle")
            self.assertEqual(self.prefs.model, "gpt-6-sol")

    def test_blender_online_access_off_still_checks_chatgpt(self):
        ready = self.chatgpt_ui
        self.prefs.provider = "CHATGPT"
        with patch.object(ready.chatgpt_ready, "account_key", return_value=("client", "issuer", "subject")), \
                self.chatgpt_plan(), patch.object(self.fake.app, "online_access", False, create=True), \
                patch.object(ready.chatgpt_ready, "verified", return_value=False), \
                patch.object(ready.threading, "Thread") as worker:
            ready._start_readiness()
            worker.assert_called_once()
            self.assertEqual(ready.readiness()["status"], "checking")

    def test_only_a_definite_refusal_or_a_short_check_holds_setup_back(self):
        ready = self.chatgpt_ui
        self.prefs.provider, self.prefs.model = "CHATGPT", "gpt-6.1-sol"
        identity = ("client", "issuer", "subject")
        with patch.object(ready.chatgpt_ready, "account_key", return_value=identity), self.chatgpt_plan(), \
                patch.object(ready.time, "monotonic", return_value=100.0):
            for code, allowed in (("ready", True), ("check_failed", True), ("http_503", True), ("check_incomplete", True),
                                  ("invalid_grant", False), ("unsupported_country_region_territory", False),
                                  ("unsupported_model", False), ("chatgpt_not_signed_in", False)):
                with self.subTest(code=code):
                    ready._ready.update(status="done", code=code, model="gpt-6.1-sol", account=identity,
                                        selected="gpt-6.1-sol", note="")
                    self.assertEqual(ready.can_continue(), allowed)
            for started, allowed in ((95.0, False), (100.0 - ready.CHECK_WAIT, True)):
                with self.subTest(started=started):
                    ready._ready.update(status="checking", code="", model="gpt-6.1-sol", account=identity,
                                        selected="gpt-6.1-sol", note="", started=started)
                    self.assertEqual(ready.can_continue(), allowed)

    def test_switching_models_back_restarts_a_cancelled_readiness_check(self):
        ready = self.chatgpt_ui
        self.prefs.provider, self.prefs.model = "CHATGPT", "gpt-6.1-sol"
        identity = ("client", "issuer", "subject")
        with patch.object(ready.chatgpt_ready, "account_key", return_value=identity), self.chatgpt_plan(), \
                patch.object(ready.chatgpt_ready, "verified", return_value=False), \
                patch.object(ready.threading, "Thread") as worker:
            ready._start_readiness()
            old_cancel = ready._ready_cancel
            self.prefs.model = "gpt-6-sol"
            ready._start_readiness()
            self.prefs.model = "gpt-6.1-sol"
            ready._start_readiness()
            self.assertEqual(worker.call_count, 3)
            ready._readiness_finished({"account": identity, "model": "gpt-6.1-sol", "code": "cancelled"},
                                      "gpt-6.1-sol", old_cancel)
            ready._start_readiness()
            self.assertEqual(worker.call_count, 3, "an old worker must not cancel the current check")

    def test_continue_preserves_the_verified_chatgpt_model(self):
        class Preferences:
            provider_writes = 0
            model = "gpt-6-sol"

            @property
            def provider(self):
                return "CHATGPT"

            @provider.setter
            def provider(self, value):
                self.provider_writes += 1
                self.model = "gpt-6.1-sol"

        prefs = Preferences()
        op = self.onboarding.LOOPCUT_OT_onboarding_step()
        op.provider, op.step, op.save = "CHATGPT", "privacy", False
        with patch.object(self.settings, "preferences", return_value=prefs):
            op.execute(self.context)
        self.assertEqual(prefs.provider_writes, 0)
        self.assertEqual(prefs.model, "gpt-6-sol")

    def test_typing_records_one_content_free_event(self):
        session = self.state.reset()
        op = self.host.LOOPCUT_OT_interact()
        event = types.SimpleNamespace(type="A", unicode="a", shift=False, oskey=False, ctrl=False, alt=False)
        telemetry = importlib.import_module(NAME + ".telemetry")
        with patch.object(telemetry, "first_use") as record, patch.object(self.host, "_update_mentions"):
            op._key(self.context, session, event)
            op._key(self.context, session, event)
        record.assert_called_once_with("prompt_typed")
        self.assertEqual(session["input"], "aa")

    def test_unfinished_setup_reports_the_previous_step_on_resume(self):
        self.onboarding.save_progress("connect")
        with patch.object(self.onboarding, "_report") as report:
            self.onboarding._start_step(False)
        self.assertIn(unittest.mock.call("welcome", None, False, "abandoned_connect"), report.call_args_list)

    # "For the LLM we always prefer their account, unless they select the Loopcut models" (owner, 2026-10-16).
    def chatgpt_plan(self, signed_in=True, plan_usage=True):
        return patch.object(self.chatgpt_ui.chatgpt_auth, "status",
                            return_value={"signed_in": signed_in, "plan_usage": plan_usage})

    def test_loopcut_sign_in_keeps_a_signed_in_chatgpt_plan(self):
        self.prefs.provider = "CHATGPT"
        with self.chatgpt_plan(), patch.object(self.account.credentials, "store") as store:
            self.account._store_key("fixture-only")
        store.assert_called_once_with(self.account.BASE_URL, "fixture-only")
        self.assertEqual(self.prefs.provider, "CHATGPT")
        self.fake.ops.wm.save_userpref.assert_called_once()

    def test_loopcut_sign_in_selects_loopcut_when_no_chatgpt_plan_is_signed_in(self):
        for signed_in, plan_usage in ((False, False), (True, False)):  # Signed out, or no plan permission.
            with self.subTest(signed_in=signed_in, plan_usage=plan_usage):
                self.prefs.provider = "CHATGPT"
                with self.chatgpt_plan(signed_in, plan_usage), patch.object(self.account.credentials, "store"):
                    self.account._store_key("fixture-only")
                self.assertEqual(self.prefs.provider, "LOOPCUT")

    def test_explicit_loopcut_pick_sticks_while_signed_in_to_chatgpt(self):
        self.prefs.provider = "CHATGPT"
        with self.chatgpt_plan(), patch.object(self.account, "signed_in", return_value=True):
            splash = Splash()
            self.onboarding._draw_sign_in(splash, self.context)
            self.press_step(splash, "Use Loopcut")
            self.assertEqual(self.prefs.provider, "LOOPCUT")
            # Nothing automatic moves it back: a later Loopcut sign-in, the account refresh, ChatGPT's idle finish.
            with patch.object(self.account.credentials, "store"):
                self.account._store_key("fixture-only")
            with patch.object(self.account, "_get", return_value={}), patch.object(self.account, "_redraw"), \
                    patch.object(self.account.mainthread, "run_on_main", side_effect=lambda fn: fn()):
                self.account._refresh_account("fixture-only")
            self.chatgpt_ui._finished(None, "")
        self.assertEqual(self.prefs.provider, "LOOPCUT")

    def test_account_refresh_and_chatgpt_sign_out_keep_the_chatgpt_provider(self):
        self.prefs.provider = "CHATGPT"
        with self.chatgpt_plan(), patch.object(self.account, "_get", return_value={}), \
                patch.object(self.account, "_redraw"), \
                patch.object(self.account.mainthread, "run_on_main", side_effect=lambda fn: fn()):
            self.account._refresh_account("fixture-only")
        self.assertEqual(self.prefs.provider, "CHATGPT")
        with self.chatgpt_plan(signed_in=False):
            self.chatgpt_ui._finished(None, "")  # What sign-out reports; the provider stays as it was.
        self.assertEqual(self.prefs.provider, "CHATGPT")

    def test_back_from_the_own_key_form_restores_a_signed_in_chatgpt_plan(self):
        def own_key(enable):
            op = self.onboarding.LOOPCUT_OT_onboarding_own_key()
            op.enable = enable
            self.assertEqual(op.execute(self.context), {"FINISHED"})
        with self.chatgpt_plan():
            self.prefs.provider = "CHATGPT"
            own_key(True)
            self.assertEqual(self.prefs.provider, "OPENAI")  # The key form opens on a key provider.
            own_key(False)
            self.assertEqual(self.prefs.provider, "CHATGPT")
            own_key(True)
            self.prefs.provider = "ANTHROPIC"  # The user's own pick on that form stands.
            own_key(False)
            self.assertEqual(self.prefs.provider, "ANTHROPIC")
            self.prefs.provider = "LOOPCUT"  # Unchanged for the Loopcut route.
            own_key(True)
            own_key(False)
            self.assertEqual(self.prefs.provider, "OPENAI")
        self.prefs.provider = "CHATGPT"
        own_key(True)
        with self.chatgpt_plan(signed_in=False):  # Signed out meanwhile: nothing to go back to.
            own_key(False)
        self.assertEqual(self.prefs.provider, "OPENAI")

    def test_a_key_prompt_only_for_an_own_key_provider_and_its_way_back(self):
        """F0287: what the panel asks for when the config does not load (layout._key_prompt)."""
        self.prefs.provider = "OPENAI"
        self.assertEqual(self.host._key_needed(True), "OpenAI")
        self.assertEqual(self.host._key_needed(False), "")
        for provider in ("CHATGPT", "LOOPCUT"):
            self.prefs.provider = provider
            self.assertEqual(self.host._key_needed(True), "", "these routes use the sign-in card")
        self.prefs.provider = "OPENAI"
        session = self.state.new_session()
        with patch.object(self.host, "config_changed") as changed:
            self.host._do_action(session, ("use_provider", "CHATGPT"))
            self.assertEqual(self.prefs.provider, "CHATGPT")
            changed.assert_called_once_with()
            self.host._do_action(session, ("use_provider", "OLLAMA"))
            self.assertEqual(self.prefs.provider, "CHATGPT", "only the two account routes")

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

    def test_chatgpt_error_offers_a_retry_and_another_account_even_with_a_saved_loopcut_account(self):
        self.prefs.provider = "CHATGPT"
        message = self.chatgpt_ui.chatgpt_auth.SIGN_IN_MESSAGES["invalid_grant"]
        self.chatgpt_ui._state.update(status="error", error=message)
        with patch.object(self.account, "signed_in", return_value=True):
            splash = Splash()
            self.onboarding._draw_sign_in(splash, self.context)
        self.assertIn(("label", message), splash.records, "the whole message, not cut short")
        retry = next(r for r in splash.operators() if r[2] == "Try again")
        self.assertEqual(retry[1], "loopcut.chatgpt_sign_in")
        self.assertFalse(getattr(retry[3], "new_account", False))  # The plain sign-in keeps an issued client id.
        another = next(r for r in splash.operators() if r[2] == "Another account")
        self.assertTrue(another[3].new_account)
        self.assertIn("loopcut.onboarding_own_key", [r[1] for r in splash.operators()])

    def test_a_failed_chatgpt_sign_in_reports_where_it_failed_and_shows_the_message_alone(self):
        auth = self.chatgpt_ui.chatgpt_auth
        telemetry = importlib.import_module(NAME + ".telemetry")
        for stage, code in (("token", "token_invalid_grant"), ("", "invalid_grant")):
            with self.subTest(stage=stage):
                error = auth.ChatGPTAuthError(auth.SIGN_IN_MESSAGES["invalid_grant"], "invalid_grant", 400, stage)
                with patch.object(auth, "sign_in", side_effect=error), \
                        patch.object(telemetry, "setup_step") as event, \
                        patch.object(self.chatgpt_ui.mainthread, "run_on_main", side_effect=lambda fn: fn()):
                    self.chatgpt_ui._run_sign_in(False)
                event.assert_called_once_with("chatgpt", "chatgpt", False, code)
                self.assertEqual(self.chatgpt_ui.status()["error"], auth.SIGN_IN_MESSAGES["invalid_grant"])

    def test_own_key_on_anthropic_links_to_its_keys_lists_its_models_and_shows_its_privacy_note(self):
        self.onboarding._state["own_key"] = True
        self.prefs.provider, self.prefs.base_url = "ANTHROPIC", "anthropic://api.anthropic.com/v1"
        splash = Splash()
        with patch.object(self.onboarding.config, "load", return_value=object()), \
                patch.object(self.onboarding, "_connected", return_value=True):
            self.onboarding._draw_connect(splash, self.context)
        self.assertIn(("prop", "api_key"), splash.records)
        self.assertIn("loopcut.anthropic_get_key", [r[1] for r in splash.operators()])
        self.assertIn(("menu", "LOOPCUT_MT_models"), splash.records, "Anthropic's models, before any fetch")
        self.assertTrue(next(r for r in splash.operators() if r[2] == "Continue")[4].enabled)
        splash = Splash()
        self.onboarding._draw_privacy(splash)
        labels = " ".join(r[1] for r in splash.records if r[0] == "label")
        self.assertIn("Anthropic (Claude)", labels)  # Where chats go, by name.

    def test_privacy_screen_says_what_is_logged_and_shows_the_switch(self):
        """Proposal 015: the conversation is logged for every route, so the old "never sees the conversation" and
        "never the content" lines are gone, and the switch sits beside the usage-stats one."""
        for provider, signed_in in (("LOOPCUT", True), ("CHATGPT", True), ("CHATGPT", False), ("OPENAI", False),
                                    ("OLLAMA", False)):
            with self.subTest(provider=provider, signed_in=signed_in), \
                    patch.object(self.account, "signed_in", return_value=signed_in):
                self.prefs.provider = provider
                splash = Splash()
                self.onboarding._draw_privacy(splash)
                labels = " ".join(r[1] for r in splash.records if r[0] == "label")
                self.assertIn("What leaves your computer", labels)
                self.assertIn("A picture, a mesh or a prompt.", labels)
                self.assertIn("Loopcut never uploads your .blend.", labels)
                self.assertNotIn("Never your .blend.", labels)  # Said once, in its own row.
                self.assertNotIn("never sees the conversation", labels)
                self.assertNotIn("never the content", labels)
                self.assertNotIn("Nothing leaves this computer", labels)
                props = [r[1] for r in splash.records if r[0] == "prop"]
                self.assertEqual(props[-3:], ["auto_run", "share_conversations", "share_usage_stats"])
                self.assertTrue(all(len(r[1]) <= 60 for r in splash.records if r[0] == "label"))  # No paragraphs.
        for provider, line in (("LOOPCUT", "Loopcut models"), ("CHATGPT", "ChatGPT, on your plan"),
                               ("OPENAI", "OpenAI, with your key")):
            self.assertEqual(self.onboarding._route_line(provider), line)
        for share in (True, False):  # What the chat logs are is said under their switch, on or off.
            self.prefs.share_conversations = share
            splash = Splash()
            self.onboarding._draw_privacy(splash)
            records = [r[:2] for r in splash.records]
            switch = records.index(("prop", "share_conversations"))
            self.assertIn(("label", "Text only, kept 90 days. Never used for training."), records[switch:switch + 4])
            label = splash.operators()[[r[1] for r in splash.operators()].index("wm.context_toggle") + 1]
            self.assertEqual((label[2], label[3].data_path), ("Share conversations",  # The label toggles it.
                             f'preferences.addons["{NAME}"].preferences.share_conversations'))
            self.assertFalse([r for r in records if "Chat logs" in str(r[1])])

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
        self.assertIn("Fix the fields above to continue.", labels)

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
        reply = self.device_reply()
        with patch.object(self.account, "_post", return_value=reply), patch.object(self.account, "open_browser") as browser:
            self.account._run_sign_in()
        browser.assert_not_called()
        self.assertEqual(self.account.status()["status"], "idle")

    def test_a_starter_sends_in_one_click_and_dismisses_the_first_run_note_for_good(self):
        session = self.state.new_session()
        with patch.object(self.host, "_config_cached", return_value=("fast", False, 1000)), \
                patch.object(self.host.agent, "send", return_value=True) as send:
            self.host._do_action(session, ("example_request", 1))
            send.assert_called_once_with(self.host.layout.EXAMPLE_REQUESTS[1])
        self.assertEqual(session["input"], "")
        self.assertTrue(self.prefs.first_message_sent)
        self.state.ui.clear()  # Simulate reopening the app with the saved preference.
        self.assertTrue(self.onboarding.hints_dismissed())

    def test_a_starter_only_fills_the_input_while_setup_is_needed(self):
        session = self.state.new_session()
        with patch.object(self.host, "_config_cached", return_value=("Set up Loopcut", True, 1000)), \
                patch.object(self.host.agent, "send", return_value=True) as send:
            self.host._do_action(session, ("example_request", 0))
        send.assert_not_called()
        self.assertEqual(session["input"], self.host.layout.EXAMPLE_REQUESTS[0])
        self.assertTrue(session["focused"])

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

    def draw(self, session, runtime, max_scroll=0):
        """draw_area() with Blender, the cloud and the layout stubbed; returns the mocks it called."""
        context = types.SimpleNamespace(area=Mock(), region=types.SimpleNamespace(width=420, height=600),
            scene={}, view_layer=None, preferences=types.SimpleNamespace(system=types.SimpleNamespace(ui_scale=1)))
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
                patch.object(self.host.draw, "retain_images", create=True), \
                patch.object(self.host.layout, "build", return_value={"max_scroll": max_scroll}) as build:
            self.host.draw_area()
        return build, service, catalog

    def test_draw_passes_both_feature_sets_with_one_cloud_snapshot(self):
        session = self.state.new_session()
        runtime = types.SimpleNamespace(signed_in=True, lock=threading.RLock(), vote_notice="All 3 votes used.")
        build, service, catalog = self.draw(session, runtime)
        ui = build.call_args.args[-1]
        self.assertEqual(ui["cloud_message"], "Saving…")
        self.assertEqual(ui["vote_notice"], "All 3 votes used.")
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

    def test_draw_passes_provider_to_filter_chat_allowance_even_with_a_connected_account(self):
        session = self.state.new_session()
        runtime = types.SimpleNamespace(signed_in=True, lock=threading.RLock(), vote_notice="")
        for provider in ("LOOPCUT", "CHATGPT", "OPENAI", "CUSTOM"):
            with self.subTest(provider=provider):
                self.prefs.provider = provider
                build, _, _ = self.draw(session, runtime)
                self.assertEqual(build.call_args.args[-1]["provider"], provider)

    def test_service_actions_keep_settings_project_and_upgrade_routes(self):
        session = self.state.new_session()
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
        with patch.object(self.account, "open_browser") as browser, patch.object(self.host.config, "billing_open", return_value=True):
            self.host._do_action(session, ("cloud_upgrade", 0))
            browser.assert_called_once_with("https://loopcut.org/pricing")
            session["items"][0]["cloud_error"]["upgrade"] = "file:///invalid"
            with self.assertRaises(ValueError):
                self.host._do_action(session, ("cloud_upgrade", 0))
            self.assertEqual(browser.call_count, 1)

    def test_offer_buttons_continue_on_loopcut_open_the_site_and_snooze(self):
        nudges = importlib.import_module(NAME + ".nudges")
        session = self.state.new_session()
        session["items"] = [nudges.chatgpt_card("subscription_sharing_usage_limit_exceeded")]
        choice = {a["do"]: i for i, a in enumerate(session["items"][0]["actions"])}
        self.prefs.provider = "CHATGPT"
        with patch.object(self.host.agent, "resume") as resume, patch.object(self.host, "config_changed"), \
                patch.object(self.account, "default_model", return_value="fast"):
            session["busy"] = True
            self.host._do_action(session, ("offer", (0, choice["continue_loopcut"])))
            self.assertEqual(self.prefs.provider, "CHATGPT", "never switches under a running turn")
            session["busy"] = False
            self.host._do_action(session, ("offer", (0, choice["continue_loopcut"])))
            self.assertEqual(self.prefs.provider, "LOOPCUT")
            resume.assert_called_once_with()
            self.assertTrue(session["items"][0]["resolved"].startswith("Carrying on with Loopcut Fast"))
        self.host._do_action(session, ("offer", (0, choice["sign_in_loopcut"])))
        self.fake.ops.loopcut.sign_in.assert_called_once_with()
        card = nudges.locked_tool_card({"name": "sfx", "title": "Sound effects", "why": "credits", "credits": 4,
                                        "own_key": False}, balance=0)
        session["items"].append(card)
        with patch.object(self.account, "open_browser") as browser:
            self.host._do_action(session, ("offer", (1, 0)))
            browser.assert_called_once_with(card["actions"][0]["arg"])
            card["actions"][0]["arg"] = "file:///etc/passwd"
            with self.assertRaises(ValueError):
                self.host._do_action(session, ("offer", (1, 0)))
        with patch.object(nudges, "snooze") as snooze:
            self.host._do_action(session, ("offer", (1, len(card["actions"]) - 1)))
            snooze.assert_called_once_with("locked_tool")
        self.assertEqual(card["resolved"], nudges.DISMISSED)
        self.host._do_action(session, ("offer", (9, 0)))  # A stale hit after the list changed: nothing.

    def limit_card(self):
        return self.state.item_limit("Used up.", "session_limit", "free", "",
                                     {"label": "Upgrade to Pro", "url": "https://loopcut.org/pricing"})

    def test_limit_card_switches_to_the_chatgpt_plan_and_goes_on(self):
        session = self.state.reset()
        session["items"] = [self.limit_card()]
        session["messages"] = [{"role": "tool", "content": "Stopped here."}]
        with patch.object(self.host.agent, "resume") as resume, patch.object(self.host, "config_changed"):
            self.host._do_action(session, ("limit_chatgpt", 0))
        self.assertEqual(self.prefs.provider, "CHATGPT")
        resume.assert_called_once_with()

    def test_foreign_card_actions_change_account_or_provider_and_preserve_the_current_draft(self):
        for kind in ("limit_upgrade", "limit_chatgpt", "limit_fast", "limit_retry", "sign_in", "own_key"):
            for stopped in (False, True):
                with self.subTest(kind=kind, stopped=stopped), patch.object(self.host.config, "billing_open", return_value=True):
                    origin = self.state.new_session()
                    card = self.limit_card()
                    origin["items"] = [card]
                    session = self.state.reset()
                    session.update(input="  current draft  ", attachments=[{"name": "ref.png", "ref": "ref.png"}])
                    if stopped:
                        session["messages"] = [{"role": "user", "content": "Other stopped turn."}]
                    self.state.ui["allowance_notice"] = {"item": card, "conversation": origin["id"]}
                    self.prefs.provider = "LOOPCUT"
                    with patch.object(self.host.agent, "resume") as resume, \
                            patch.object(self.account, "watch_plan") as watch, \
                            patch.object(self.account, "open_browser") as browser, \
                            patch.object(self.account, "refresh_account") as refresh, \
                            patch.object(self.settings, "set_tier") as tier, \
                            patch.object(self.host, "_open_settings"), patch.object(self.host, "config_changed"), \
                            patch.object(self.host.conversations, "save") as save:
                        action = (kind, "chatgpt" if kind == "sign_in" else None)
                        self.host._do_action(session, action)
                        if kind == "limit_upgrade":
                            browser.assert_called_once_with(card["upgrade"]["url"])
                            watch.assert_called_once()
                            watch.call_args.args[1]({"plan": "pro"})
                            save.assert_not_called()  # No foreign card is saved into this conversation.
                        elif kind == "limit_chatgpt":
                            self.assertEqual(self.prefs.provider, "CHATGPT")
                        elif kind == "limit_fast":
                            tier.assert_called_once()
                        elif kind == "sign_in":
                            self.fake.ops.loopcut.chatgpt_sign_in.assert_called_once_with()
                        resume.assert_not_called()
                        refresh.assert_not_called()
                        self.assertEqual(session["input"], "  current draft  ")
                        self.assertEqual(session["attachments"], [{"name": "ref.png", "ref": "ref.png"}])
                    self.fake.ops.reset_mock()

    def test_retry_refreshes_usage_before_resuming_only_the_same_stopped_turn(self):
        session = self.state.reset()
        session["items"] = [self.limit_card()]
        session["messages"] = [{"role": "tool", "content": "Stopped here."}]
        session["input"] = "keep this draft"
        with patch.object(self.host.agent, "resume") as resume, patch.object(self.account, "refresh_account") as refresh:
            self.host._do_action(session, ("limit_retry", 0))
            refresh.assert_called_once()
            resume.assert_not_called()
            refresh.call_args.args[0]()
            resume.assert_called_once()
            resume.reset_mock()
            self.host._do_action(session, ("limit_retry", 0))
            self.state.reset()  # Changing chats during the HTTP refresh never resumes the new chat.
            refresh.call_args.args[0]()
            resume.assert_not_called()
        self.assertEqual(session["input"], "keep this draft")

    def test_retry_refreshes_same_plan_available_usage_then_dispatches_resume(self):
        session = self.state.reset()
        session["items"] = [self.limit_card()]
        session["messages"] = [{"role": "tool", "content": "Stopped here."}]
        session["input"] = "keep the draft"
        events = []
        def get(path, key):
            events.append(path)
            return {"plan": "free", "session": {"used": 0.2}} if path == "/v1/me" else {"data": [{"id": "fast"}]}
        def resumed():
            self.assertEqual(self.state.ui["account"]["session"]["used"], 0.2)
            events.append("resume")
        with patch.dict(self.account._state, account={"plan": "free", "session": {"used": 1.0}}), \
                patch.object(self.account.credentials, "api_key", return_value="fixture"), \
                patch.object(self.account, "_get", side_effect=get), patch.object(self.account, "_redraw"), \
                patch.object(self.account.threading, "Thread", side_effect=lambda target, args=(), **kw:
                             types.SimpleNamespace(start=lambda: target(*args))), \
                patch.object(self.account.mainthread, "run_on_main", side_effect=lambda fn: fn()), \
                patch.object(self.host.agent, "resume", side_effect=resumed):
            self.host._do_action(session, ("limit_retry", 0))
        self.assertEqual(events, ["/v1/me", "/v1/models", "resume"])
        self.assertEqual(session["input"], "keep the draft")

    def test_upgrade_watch_resumes_only_its_original_stopped_turn(self):
        for change in ("none", "conversation", "turn"):
            with self.subTest(change=change), patch.object(self.host.config, "billing_open", return_value=True):
                session = self.state.reset()
                session["items"] = [self.limit_card()]
                session["messages"] = [{"role": "tool", "content": "Stopped here."}]
                with patch.object(self.host.agent, "resume") as resume, patch.object(self.account, "watch_plan") as watch, \
                        patch.object(self.account, "open_browser"), patch.object(self.host.conversations, "save"):
                    self.host._do_action(session, ("limit_upgrade", 0))
                    if change == "conversation":
                        self.state.reset()["messages"] = [{"role": "user", "content": "Another stopped chat."}]
                    elif change == "turn":
                        session["messages"].append({"role": "user", "content": "A newer turn."})
                    watch.call_args.args[1]({"plan": "pro"})
                    self.assertEqual(resume.call_count, int(change == "none"))

    def test_locked_model_upgrade_dispatch_starts_the_same_watch_without_resuming_a_turn(self):
        with patch.object(self.host.config, "billing_open", return_value=True), patch.object(self.settings, "_changed"), \
                patch.object(self.account, "models", return_value=[{"id": "pro", "label": "Pro", "available": False,
                                                                   "unlock_plan": {"id": "pro", "name": "Pro"}}]):
            session = self.state.reset()
            session["messages"] = [{"role": "user", "content": "An older stopped turn."}]
            self.state.ui["account"] = {"plan": "free"}
            self.settings.set_tier("pro")
            notice = session["items"][-1]
            with patch.object(self.account, "open_browser") as browser, patch.object(self.account, "watch_plan") as watch, \
                    patch.object(self.host.agent, "resume") as resume, patch.object(self.host.conversations, "save"):
                self.host._do_action(session, ("open_url", 0))
                browser.assert_called_once_with(notice["url"])
                self.assertEqual(watch.call_args.args[0], "free")
                watch.call_args.args[1]({"plan": "pro"})
                resume.assert_not_called()

    def test_plan_watch_refreshes_models_before_reporting_the_upgrade(self):
        jobs = []
        catalogue = {"data": [{"id": "pro", "available": True}], "default": "pro"}
        with patch.dict(self.account._state, account={"plan": "free"}, models=[{"id": "pro", "available": False}]), \
                patch.object(self.account.credentials, "api_key", return_value="fixture"), \
                patch.object(self.account, "_get", side_effect=[{"plan": "pro"}, catalogue]) as get, \
                patch.object(self.account.threading, "Thread", side_effect=lambda target, **kw: types.SimpleNamespace(start=target)), \
                patch.object(self.account.time, "sleep"), patch.object(self.account.mainthread, "run_on_main", side_effect=jobs.append), \
                patch.object(self.account, "_redraw"):
            changed = Mock(side_effect=lambda payload: self.assertTrue(self.account.models()[0]["available"]))
            self.account.watch_plan("free", changed)
            self.assertEqual([c.args[0] for c in get.call_args_list], ["/v1/me", "/v1/models"])
            changed.assert_not_called()
            self.state.clear_allowance_notice(invalidate=True)  # Switching providers keeps the account/model refresh alive.
            jobs.pop(0)()
            changed.assert_called_once_with({"plan": "pro"})
            self.assertEqual(self.account.default_model(), "pro")
            self.settings.choose_model("pro")
            self.assertEqual(self.prefs.tier, "pro")

    def test_account_refresh_is_authoritative_and_ignores_a_late_reply_after_sign_out(self):
        for sign_out in (False, True):
            with self.subTest(sign_out=sign_out), patch.dict(self.account._state, account={"plan": "free"}), \
                    patch.object(self.account, "_redraw"):
                jobs = []
                card = self.limit_card()
                self.state.ui["allowance_notice"] = {"item": card, "conversation": "old"}
                payload = {"plan": "free", "session": {"used": 0.2}}
                done = Mock()
                with patch.object(self.account, "_get", side_effect=[payload, {"data": [{"id": "fast"}]}]), \
                        patch.object(self.account.mainthread, "run_on_main", side_effect=jobs.append):
                    self.account._refresh_account("fixture", done)
                if sign_out:
                    with patch.object(self.account.credentials, "store"):
                        self.account.sign_out()
                jobs.pop(0)()
                if sign_out:
                    self.assertIsNone(self.state.ui.get("account"))
                    self.assertNotIn("allowance_notice", self.state.ui)
                    done.assert_not_called()
                else:
                    self.assertEqual(self.state.ui["account"], payload)
                    done.assert_called_once_with()
                    self.assertIn("allowance_notice", self.state.ui, "same-plan refresh is display only and never gates a request")

    def test_changing_accounts_clears_both_allowance_warning_windows(self):
        for change in ("sign_out", "sign_in"):
            with self.subTest(change=change), patch.object(self.account.credentials, "store"), \
                    patch.object(self.account, "on_signed_in", []), patch.object(self.account, "_redraw"):
                self.state.ui.update(warned_session_allowance=["same session reset"],
                                     warned_week_allowance=["same weekly reset"])
                if change == "sign_out":
                    self.account.sign_out()
                else:
                    self.account._store_key("different-account")
                self.assertNotIn("warned_session_allowance", self.state.ui)
                self.assertNotIn("warned_week_allowance", self.state.ui)

    def test_changed_plan_clears_notice_and_refreshes_cached_model_locks(self):
        with patch.dict(self.account._state, account={"plan": "free"}), \
                patch.object(self.account, "refresh_account") as refresh:
            self.state.ui["allowance_notice"] = {"item": self.limit_card(), "conversation": "old"}
            self.account.update_usage({"plan": "pro"})
            self.assertNotIn("allowance_notice", self.state.ui)
            refresh.assert_called_once_with()

    def test_expired_usage_refreshes_after_idle_reset_and_late_helper_report(self):
        from datetime import datetime
        reset = "2099-10-21T18:00:00Z"
        now = datetime.fromisoformat(reset).timestamp()
        for window in ("session", "week"):
            with self.subTest(window=window), patch.dict(self.account._state, account=None), \
                    patch.object(self.account, "refresh_account") as refresh, \
                    patch.object(self.host, "_refresh_missing_paths"), patch.object(self.host, "_prune_images"), \
                    patch.object(self.fake.context, "window_manager", types.SimpleNamespace(windows=[]), create=True), \
                    patch.object(self.account.time, "time", return_value=now - 1) as clock:
                self.account._set_account(None)
                payload = {"plan": "free", window: {"used": 0.84, "resets_at": reset}}
                self.account._set_account(payload)
                self.host.tag_redraw_all()
                refresh.assert_not_called()
                clock.return_value = now
                self.host.tag_redraw_all()
                refresh.assert_called_once()
                self.assertEqual(self.account.usage_lines(), [])
                self.host.tag_redraw_all()
                self.account.update_usage(payload)
                refresh.assert_called_once()  # No request storm while an expired report remains cached.
                self.account._set_account(None)
                self.account._set_account({"plan": "free"})
                refresh.reset_mock()
                self.account.update_usage(payload)  # A helper can deliver an already expired window.
                refresh.assert_called_once()
                self.assertEqual(self.account.usage_lines(), [])

    def test_expired_usage_offline_backs_off_and_stops_over_a_simulated_hour(self):
        now = [0.0]
        attempts = []

        def offline(path, key):
            self.assertEqual(path, "/v1/me")
            attempts.append(now[0])
            raise self.account.AccountError("Offline")

        def immediate_thread(target, args=(), **kwargs):
            return types.SimpleNamespace(start=lambda: target(*args))

        with patch.dict(self.account._state), \
                patch.object(self.account.credentials, "api_key", return_value="fixture"), \
                patch.object(self.account, "_get", side_effect=offline), \
                patch.object(self.account.threading, "Thread", side_effect=immediate_thread), \
                patch.object(self.account.mainthread, "run_on_main", side_effect=lambda fn: fn()), \
                patch.object(self.account.time, "monotonic", side_effect=lambda: now[0]), \
                patch.object(self.account, "_redraw"):
            self.account._set_account(None)
            stale = {"plan": "free", "session": {"used": 0.84, "resets_at": "2000-01-01T00:00:00Z"}}
            self.account._set_account(stale)
            for second in range(3600):
                now[0] = float(second)
                self.account.refresh_expired_usage()
                self.account.update_usage(stale)  # Late helper reports do not restart the retry budget.
            self.assertEqual(attempts, [0, 30, 150, 750])
            self.account.refresh_account()  # An explicit user action still works after automatic retries stop.
            self.assertEqual(len(attempts), 5)
            fresh = {"plan": "free", "session": {"used": 0.1, "resets_at": "2099-10-21T18:00:00Z"}}
            with patch.object(self.account, "_get", side_effect=[fresh, {"data": []}]):
                self.account.refresh_account()
            self.assertEqual(self.state.ui["account"], fresh)
            self.account.refresh_expired_usage()
            self.assertEqual(len(attempts), 5)
            self.account.update_usage(stale)  # Fresh usage rearms the budget for the next expiry.
            self.assertEqual(len(attempts), 6)

    def test_expired_usage_has_one_in_flight_refresh_until_its_main_thread_callback(self):
        now = [0.0]
        workers, callbacks = [], []

        def queued_thread(target, args=(), **kwargs):
            return types.SimpleNamespace(start=lambda: workers.append(lambda: target(*args)))

        with patch.dict(self.account._state), \
                patch.object(self.account.credentials, "api_key", return_value="fixture"), \
                patch.object(self.account.threading, "Thread", side_effect=queued_thread), \
                patch.object(self.account.mainthread, "run_on_main", side_effect=callbacks.append), \
                patch.object(self.account.time, "monotonic", side_effect=lambda: now[0]), \
                patch.object(self.account, "_get", side_effect=self.account.AccountError("Offline")), \
                patch.object(self.account, "_redraw"):
            self.account._set_account(None)
            self.account._set_account({"session": {"used": 0.84, "resets_at": "2000-01-01T00:00:00Z"}})
            self.account.refresh_expired_usage()
            now[0] = 3600.0
            self.account.refresh_expired_usage()
            self.assertEqual(len(workers), 1, "a slow request cannot overlap another refresh")
            workers.pop(0)()
            self.account.refresh_expired_usage()
            self.assertEqual(workers, [], "wait for the main-thread result before retrying")
            callbacks.pop(0)()
            now[0] += 29
            self.account.refresh_expired_usage()
            self.assertEqual(workers, [])
            now[0] += 1
            self.account.refresh_expired_usage()
            self.assertEqual(len(workers), 1)

    def test_account_refresh_preserves_reservations_and_refusal_until_reset_or_plan_change(self):
        reset = "2099-10-21T18:00:00Z"
        for window in ("session", "week"):
            for change in ("same", "reset", "plan", "sign_out"):
                with self.subTest(window=window, change=change), patch.dict(self.account._state, account=None):
                    card = self.limit_card()
                    card.update(reason=window + "_limit", resets_at=reset)
                    self.state.ui["allowance_notice"] = {"item": card}
                    payload = {"plan": "free", window: {"used": 0.62, "used_with_reservations": 0.82, "resets_at": reset}}
                    with patch.object(self.account, "_get", side_effect=[payload, {"data": [{"id": "fast"}]}]), \
                            patch.object(self.account.mainthread, "run_on_main", side_effect=lambda fn: fn()), \
                            patch.object(self.account, "_redraw"):
                        self.account._refresh_account("fixture")
                    self.assertEqual(self.state.ui["account"], payload)
                    self.assertIn(f"{window.capitalize()}: 100% used", self.account.usage_lines())
                    if change == "reset":
                        payload = {**payload, window: {**payload[window], "resets_at": "2099-10-22T18:00:00Z"}}
                    elif change == "plan":
                        payload = {**payload, "plan": "pro"}
                    elif change == "sign_out":
                        payload = None
                    self.account._set_account(payload)
                    if change == "same":
                        self.assertIn("allowance_notice", self.state.ui)
                    else:
                        self.assertNotIn("allowance_notice", self.state.ui)
                        if payload:
                            self.assertIn(f"{window.capitalize()}: 82% used", self.account.usage_lines())
                        else:
                            self.assertEqual(self.account.usage_lines(), [])

    def test_account_reservation_fields_are_optional_and_parse_defensively(self):
        for reserved, expected in ((0.82, 82), ("0.82", 82), (0, 0), (1.2, 100),
                                   (None, 62), ("bad", 62), (float("nan"), 62), (float("inf"), 62),
                                   (-1, 62), (True, 62), ([], 62), ({}, 62)):
            with self.subTest(reserved=reserved), patch.dict(self.account._state, account=None):
                self.account._set_account({"plan": "free", "session": {"used": 0.62, "used_with_reservations": reserved},
                                           "week": {"used": 0.42}})
                self.assertEqual(self.account.usage_lines(), [f"Session: {expected}% used", "Week: 42% used"])

    # --- 0.2.6: a required, modal and resumable setup ---------------------------------------------------------

    def draw_step(self, first_run=False, wm=None, styled=False):
        splash = StyledSplash() if styled else Splash()
        context = types.SimpleNamespace(region_popup=None, window_manager=wm or types.SimpleNamespace(splash_modal=False))
        drawn = self.onboarding.draw_splash(splash, context, first_run=first_run)
        return splash, drawn, context.window_manager

    def fresh_session(self):
        self.onboarding._state.update(step=None, finished=False)

    def test_quitting_half_way_resumes_at_the_saved_step_with_preferences(self):
        self.onboarding.save_progress("privacy")
        self.fresh_session()
        with patch.object(self.account, "signed_in", return_value=True), \
                patch.object(self.onboarding, "_connected", return_value=True):
            splash, drawn, wm = self.draw_step()
        self.assertTrue(drawn)
        self.assertEqual(self.onboarding._state["step"], "privacy")
        self.assertIn(("label", "● ● ● ● ○"), splash.records)
        self.assertTrue(wm.splash_modal)

    def test_resuming_goes_back_to_a_step_whose_requirement_is_gone(self):
        for saved, signed_in, connected, expected in (("done", True, False, "connect"), ("privacy", False, True, "account"),
                                                      ("connect", False, False, "account"), ("done", True, True, "done")):
            with self.subTest(saved=saved, signed_in=signed_in, connected=connected):
                self.onboarding.save_progress(saved)
                self.fresh_session()
                with patch.object(self.account, "signed_in", return_value=signed_in), \
                        patch.object(self.onboarding, "_connected", return_value=connected):
                    self.draw_step()
                self.assertEqual(self.onboarding._state["step"], expected)
                self.assertEqual(json.loads(self.progress.read_text()), {"version": 1, "step": expected, "done": False})

    def test_installs_from_before_the_setup_go_through_it_from_the_account(self):
        """0.2.5 installs with a model or an account no longer skip the setup (conversation logs are new, and on):
        they start at the account, modal, told apart from new installs in the funnel, and once through the privacy
        step and finished, never again."""
        on = self.onboarding
        for signed_in, connected in ((True, False), (False, True), (True, True)):
            with self.subTest(signed_in=signed_in, connected=connected):
                self.progress.unlink(missing_ok=True)
                self.fresh_session()
                with patch.object(self.account, "signed_in", return_value=signed_in), \
                        patch.object(self.account, "account_line", return_value="person@example.test"), \
                        patch.object(on, "_connected", return_value=connected), \
                        patch.object(on, "_report") as report:
                    splash, drawn, wm = self.draw_step(wm=types.SimpleNamespace(splash_modal=False))
                self.assertTrue(drawn)
                self.assertEqual(on._state["step"], "account")
                self.assertTrue(wm.splash_modal)
                self.assertTrue(on.is_existing())
                self.assertEqual(on.load_progress()["existing"], True)
                self.assertFalse(on.privacy_seen())
                self.assertIn(unittest.mock.call("welcome", None, code="existing"), report.call_args_list)
                labels = [r[1] for r in splash.records if r[0] == "label"]
                self.assertIn("You're signed in" if signed_in else "Create your free account", labels)
                if not signed_in:  # Like a new install: no way past the sign-in until it fails.
                    self.assertNotIn("Not now", [r[2] for r in splash.operators()])
                on.save_progress("done", done=True, privacy_seen=True)  # Through privacy and finished.
                self.fresh_session()
                with patch.object(self.account, "signed_in", return_value=signed_in), \
                        patch.object(on, "_connected", return_value=connected):
                    splash, drawn, wm = self.draw_step(wm=types.SimpleNamespace(splash_modal=True))
                self.assertFalse(drawn)  # Never again: the normal splash.
                self.assertFalse(wm.splash_modal)

    def test_an_install_from_before_resumes_as_one(self):
        on = self.onboarding
        on.save_progress("connect", existing=True)
        self.fresh_session()
        with patch.object(self.account, "signed_in", return_value=True), \
                patch.object(on, "_connected", return_value=False), \
                patch.object(on, "_report") as report:
            self.draw_step()
        self.assertEqual(on._state["step"], "connect")
        self.assertTrue(on._state["existing"])
        report.assert_any_call("welcome", None, code="existing_resumed")
        on.save_progress("done", done=True)  # Done in a build before the privacy step: once more, as existing.
        self.fresh_session()
        with patch.object(self.account, "signed_in", return_value=True), \
                patch.object(on, "_connected", return_value=False):
            self.draw_step()
        self.assertEqual(on._state["step"], "account")
        self.assertTrue(on._state["existing"])

    def test_an_install_from_before_keeps_its_model_at_the_model_step(self):
        """It says which model is connected, Continue keeps it (reported as kept), "Change model" shows the
        choices."""
        on = self.onboarding
        on._state.update(step="connect", existing=True)
        for provider, name, detail in (("LOOPCUT", "Loopcut models", "Loopcut Fast"),
                                       ("CHATGPT", "ChatGPT", "person@example.test"),
                                       ("OPENAI", "OpenAI, with your key", "test-model")):
            with self.subTest(provider=provider), \
                    patch.object(self.account, "signed_in", return_value=True), \
                    patch.object(on, "_connected", return_value=True), \
                    patch.object(on.settings, "model_label", return_value="Loopcut Fast"), \
                    patch.object(self.chatgpt_ui, "account_line", return_value="person@example.test"):
                self.prefs.provider = provider
                on._state["change_model"] = False
                splash = Splash()
                on._draw_connect(splash, self.context)
                labels = [r[1] for r in splash.records if r[0] == "label"]
                for text in ("Your model is connected", name, "Connected", detail):
                    self.assertIn(text, labels)
                self.assertEqual([r[2] for r in splash.operators()], ["Continue", "← Back", "Change model"])
                with patch.object(on, "_report") as report:
                    on._report_move_now("connect", "privacy", "CURRENT")
                report.assert_called_once_with("chatgpt", on._choice(), code="existing_kept")
                op = on.LOOPCUT_OT_onboarding_change_model()
                with patch.object(on, "_report"):
                    self.assertEqual(op.execute(self.context), {"FINISHED"})
                splash = Splash()
                on._draw_connect(splash, self.context)
                self.assertIn("Add a key", [r[2] for r in splash.operators()])  # The choices, to change.
        on._state.update(existing=False, change_model=False)
        with patch.object(self.account, "signed_in", return_value=True), \
                patch.object(on, "_connected", return_value=True):
            splash = Splash()
            on._draw_connect(splash, self.context)
        self.assertNotIn("Your model is connected", [r[1] for r in splash.records])  # New installs choose.

    def test_the_sign_in_switch_for_installs_from_before_is_off(self):
        """The owner: they sign in like new installs. The switch exists, off; on, it offers "Not now"."""
        on = self.onboarding
        self.assertFalse(on.EXISTING_MAY_SKIP_SIGN_IN)
        on._state.update(step="account", existing=True)
        with patch.object(on, "EXISTING_MAY_SKIP_SIGN_IN", True):
            splash = Splash()
            on._draw_account(splash, self.context)
        self.assertIn("Not now", [r[2] for r in splash.operators()])
        self.assertIn("loopcut.onboarding_no_account", [r[1] for r in splash.operators()])

    def test_a_working_key_of_ones_own_survives_a_loopcut_sign_in(self):
        self.prefs.provider = "OPENAI"
        with patch.object(self.account.credentials, "store"), \
                patch.object(self.account.config, "load", return_value=object()):
            self.account._store_key("fixture-only")
        self.assertEqual(self.prefs.provider, "OPENAI")

    def test_a_new_install_sees_privacy_in_the_setup_once(self):
        on = self.onboarding
        self.progress.unlink(missing_ok=True)
        on._state["step"] = "privacy"
        on.save_progress("privacy")
        self.assertFalse(on.privacy_seen())
        splash = Splash()
        on._draw_privacy(splash)
        self.press_step(splash, "Continue")
        self.assertEqual(on._state["step"], "done")
        self.assertTrue(on.privacy_seen())
        on.save_progress("done", done=True)  # Finishing keeps it.
        self.assertTrue(on.load_progress()["privacy_seen"])
        self.assertFalse(on.load_progress()["existing"])
        self.assertEqual(json.loads(self.progress.read_text())["privacy_seen"], on.PRIVACY_NOTICE)
        self.fresh_session()
        splash, drawn, wm = self.draw_step(wm=types.SimpleNamespace(splash_modal=True))
        self.assertFalse(drawn)  # Done: never the setup or its privacy step again.

    def test_headless_runs_never_count_as_having_seen_the_privacy_screen(self):
        on = self.onboarding
        on.save_progress("done", done=True, privacy_seen=True)
        self.fake.app.background = True
        self.assertFalse(on.privacy_seen())
        on.save_progress("done", done=True, privacy_seen=False)
        on.mark_privacy_seen()  # Nobody saw anything.
        self.fake.app.background = False
        self.assertFalse(on.privacy_seen())

    def test_an_unconnected_install_from_before_gets_the_setup_and_a_done_one_never_again(self):
        self.progress.unlink(missing_ok=True)
        self.fresh_session()
        with patch.object(self.onboarding, "_connected", return_value=False):
            splash, drawn, wm = self.draw_step()
        self.assertTrue(drawn)
        self.assertEqual(self.onboarding._state["step"], "account")
        self.assertTrue(wm.splash_modal)
        self.onboarding.save_progress("done", done=True, privacy_seen=True)  # Through its privacy step.
        self.fresh_session()
        with patch.object(self.onboarding, "_connected", return_value=False):
            splash, drawn, wm = self.draw_step(wm=types.SimpleNamespace(splash_modal=True))
        self.assertFalse(drawn)
        self.assertFalse(wm.splash_modal)

    def test_factory_startup_runs_are_never_held_by_the_setup(self):
        self.onboarding._state["step"] = "account"
        with patch.object(self.fake.app, "factory_startup", True, create=True), \
                patch.object(self.fake.app, "background", False, create=True):
            splash, drawn, wm = self.draw_step()
            self.assertIsNone(self.onboarding.start())
        self.assertTrue(drawn)  # Drawn, as the harness screenshots it, but not modal.
        self.assertFalse(wm.splash_modal)
        self.fake.app.timers.register.assert_not_called()

    def test_the_watchdog_reopens_a_closed_splash_until_the_setup_ends(self):
        self.onboarding._state.update(step="account", region=Mock())
        with patch.object(self.onboarding, "_region_exists", return_value=False), \
                patch.object(self.onboarding, "_open_splash") as reopen:
            self.assertEqual(self.onboarding._watch(), self.onboarding._WATCH_SECONDS)
            reopen.assert_called_once_with()
        with patch.object(self.onboarding, "_region_exists", return_value=True), \
                patch.object(self.onboarding, "_open_splash") as reopen:
            self.onboarding._watch()
            reopen.assert_not_called()
        self.onboarding._state.update(step=None, finished=True)
        self.assertIsNone(self.onboarding._watch())

    def grant(self, hours_ago=1.0, amount=100, balance=100):
        granted = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - hours_ago * 3600))
        return {"balance": balance, "trial": {"amount": amount, "granted_at": granted}}

    def signed_in_account_step(self, credits, billing=True):
        self.onboarding._state["step"] = "account"
        with patch.object(self.account, "signed_in", return_value=True), \
                patch.object(self.account, "account_line", return_value="person@example.test"), \
                patch.object(self.onboarding, "_credits", return_value=credits), \
                patch.object(self.onboarding.config, "billing_open", return_value=billing):
            splash, _, _ = self.draw_step()
        return splash, [r[1] for r in splash.records if r[0] == "label"], [r[2] for r in splash.operators()]

    def test_a_new_accounts_trial_is_claimed_with_a_button_and_shown_in_dollars(self):
        splash, labels, texts = self.signed_in_account_step(self.grant())
        self.assertEqual(texts[0], "Claim credits")
        self.assertIn("100 free credits", labels)
        self.assertIn("$1.00 for cloud tools", labels)
        self.assertIn("Credits pay for cloud tools; Loopcut chat has a separate "
                      "free allowance that refreshes every 5 hours.", " ".join(labels))
        self.assertIn("Skip", texts)  # Cosmetic: never a gate.
        with patch.object(self.onboarding, "_credits", return_value=self.grant()), \
                patch.object(self.onboarding.nudges if hasattr(self.onboarding, "nudges") else
                             importlib.import_module(NAME + ".nudges"), "mark_shown") as seen, \
                patch.object(self.onboarding, "_report") as report:
            self.assertEqual(self.onboarding.LOOPCUT_OT_onboarding_claim().execute(self.context), {"FINISHED"})
        seen.assert_called_once()
        self.assertEqual(seen.call_args.args[0], "trial")  # The panel's trial card would only repeat it.
        report.assert_called_once_with("loopcut_account", "loopcut", code="claim")
        splash, labels, texts = self.signed_in_account_step(self.grant())
        self.assertIn("100 credits added", labels)  # The claim box's shape: Continue lands where Claim was.
        self.assertIn("$1.00 for cloud tools", labels)
        self.assertNotIn("Claim credits", texts)
        self.assertIn("See plans ↗", texts)  # In the slot Skip had: the screen keeps its height.
        self.press_step(splash, "Continue")
        self.assertEqual(self.onboarding._state["step"], "how")

    def test_claim_step_omits_hosted_chat_allowance_for_other_providers(self):
        for provider in ("CHATGPT", "OPENAI", "CUSTOM"):
            with self.subTest(provider=provider):
                self.prefs.provider = provider
                _, labels, buttons = self.signed_in_account_step(self.grant())
                self.assertIn("Claim credits", buttons)
                self.assertFalse(any("allowance" in label for label in labels))

    def test_paid_claim_step_does_not_describe_its_chat_allowance_as_free(self):
        for plan in ("pro", "max"):
            with self.subTest(plan=plan), patch.object(self.onboarding, "_account_plan", return_value=plan):
                _, labels, _ = self.signed_in_account_step(self.grant())
                self.assertIn("chat allowance that refreshes every 5 hours.", " ".join(labels))
                self.assertNotIn("free allowance", " ".join(labels))

    def test_no_claim_without_a_fresh_unspent_grant_and_the_copy_stays_honest(self):
        for credits, billing, expected in (
                ({"balance": 0}, True, "No free credits on this account right now"),  # Deferred or refused.
                (self.grant(hours_ago=24 * 30, balance=40), True, "You have 40 credits ($0.40)"),
                (self.grant(balance=60), True, "You have 60 credits ($0.60)"),  # Already spent.
                (self.grant(), False, "Asset search and Loopcut Fast are free")):  # No sales: no credits.
            with self.subTest(credits=credits, billing=billing):
                splash, labels, texts = self.signed_in_account_step(credits, billing)
                self.assertFalse([t for t in texts if t.startswith("Claim")])
                self.assertIn(expected, " ".join(labels))
                self.assertIn("Continue", texts)

    def test_with_the_forks_layout_teal_is_only_for_web_links_and_filled_buttons_have_no_icon(self):
        """The design bar: quiet actions (Back, Skip, Cancel, Start fresh) are muted text links, teal marks only a
        link that opens the browser (↗), no filled button carries an icon, and every box has the same padding."""
        on = self.onboarding
        live = [{"name": "motion_from_text", "backend": "managed", "cost": {"credits_estimate": 10}},
                {"name": "image_to_3d", "backend": "managed", "cost": {"credits_estimate": 80}}]
        screens = (
            ("account", False, lambda: None, on._draw_account),
            ("waiting", False, lambda: self.account._state.update(status="waiting", user_code="TQ7-4KD"),
             on._draw_account),
            ("failed", False, lambda: self.account._state.update(status="error", user_code=""), on._draw_account),
            ("claim", True, lambda: self.account._state.update(status="idle"), on._draw_account),
            ("claimed", True, lambda: on._state.update(claimed=True), on._draw_account),
            ("model", True, lambda: None, on._draw_sign_in),
            ("model waiting", True, lambda: self.chatgpt_ui._state.update(status="waiting"), on._draw_sign_in),
            ("model error", True, lambda: self.chatgpt_ui._state.update(status="error", error="No Plus or Pro plan"),
             on._draw_sign_in),
            ("privacy", True, lambda: None, lambda layout, context: on._draw_privacy(layout)),
            ("how", True, lambda: None, on._draw_how),
            ("try", True, lambda: None, on._draw_try))
        for name, signed_in, arrange, draw in screens:
            with self.subTest(screen=name), \
                    patch.object(self.account, "signed_in", return_value=signed_in), \
                    patch.object(self.account, "account_line", return_value="person@example.test"), \
                    patch.object(on, "_credits", return_value=self.grant()), \
                    patch.object(on, "_descriptors", return_value=live), \
                    patch.object(on, "_connected", return_value=True), \
                    patch.object(on.config, "billing_open", return_value=True):
                arrange()
                splash = StyledSplash()
                draw(splash, self.context)
                for record in splash.operators():
                    options = record[5]
                    if "color" in options:
                        expected = (on.TEAL if record[2].endswith("↗") else
                                    on.FOREGROUND if record[1] == "wm.context_toggle" else on.MUTED)  # A label.
                        self.assertEqual(options["color"], expected, record[2])
                        # Only actions look like links when hovered; a checkbox's label does not.
                        self.assertEqual(options["hover"], record[1] != "wm.context_toggle", record[2])
                    else:
                        self.assertFalse(record[2].endswith("↗"), record[2])
                    if options.get("depress"):
                        self.assertFalse({"icon", "icon_value"} & set(options), record[2])
                self.assertTrue([r for r in splash.records if r[0] == "box"])
                self.assertEqual({r[1] for r in splash.records if r[0] == "box"} - {on.CARD_PADDING},
                                 {on.BOX_PADDING})

    def test_the_panel_names_loopcut_models_as_the_site_does_even_for_a_legacy_tier(self):
        """A preference saved by 0.2.4 can hold the "mimo" tier, which the gateway maps to Pro: the model button
        said "mimo". It says "Loopcut Pro" now, and a known tier "Loopcut Fast"."""
        with patch.object(self.settings, "preferences", return_value=types.SimpleNamespace(provider="LOOPCUT")), \
                patch.object(self.account, "_state", {**self.account._state, "models": []}):
            self.assertEqual(self.settings.model_label("mimo"), "Loopcut Pro")
            self.assertEqual(self.settings.model_label("fast"), "Loopcut Fast")
            self.assertEqual(self.account.model_label("mimo"), "Pro")

    def test_messages_in_the_setup_read_as_sentences(self):
        sentence = self.onboarding._sentence
        self.assertEqual(sentence("This ChatGPT account has no Plus or Pro plan"),
                         "This ChatGPT account has no Plus or Pro plan.")
        self.assertEqual(sentence("Try again later."), "Try again later.")
        self.assertEqual(sentence("x " * 60), "x " * 34 + "x…")  # Shortened, not given a full stop.

    LIVE = [{"name": "motion_from_text", "backend": "managed", "cost": {"credits_estimate": 10}},
            {"name": "image_to_3d", "backend": "managed", "cost": {"credits_estimate": 80}}]

    def draw_how(self, plan="", billing=True, descriptors=None):
        on = self.onboarding
        with patch.object(self.account, "signed_in", return_value=True), \
                patch.object(on, "_account_plan", return_value=plan), \
                patch.object(on, "_descriptors", return_value=self.LIVE if descriptors is None else descriptors), \
                patch.object(on.config, "billing_open", return_value=billing):
            splash = Splash()
            on._draw_how(splash, self.context)
        labels = " | ".join(r[1] for r in splash.records if r[0] == "label")
        return splash, labels, [r[2] for r in splash.operators()]

    def test_how_loopcut_works_sits_between_the_account_and_the_model_and_resumes(self):
        on = self.onboarding
        self.assertEqual(on.NUMBERED, ("account", "how", "connect", "privacy", "done"))
        on._state["step"] = "how"
        splash, labels, texts = self.draw_how()
        self.assertIn("● ● ○ ○ ○", labels)  # Step 2 of 5.
        self.assertEqual(texts, ["Continue", "← Back", "See plans ↗"])
        self.press_step(splash, "Continue")
        self.assertEqual(on._state["step"], "connect")
        with patch.object(on, "_report") as report:  # What Continue reports when usage stats are on.
            on._report_move_now("how", "connect", "CURRENT")
        report.assert_called_once_with("chatgpt", None, code="how_continue")
        with patch.object(self.account, "signed_in", return_value=True):  # The model step's Back returns here.
            model = Splash()
            on._draw_sign_in(model, self.context)
            self.press_step(model, "← Back")
        self.assertEqual(on._state["step"], "how")
        self.press_step(splash, "← Back")
        self.assertEqual(on._state["step"], "account")
        model = Splash()  # Without an account the setup never showed it: Back goes to the account.
        on._state["step"] = "connect"
        on._draw_sign_in(model, self.context)
        self.press_step(model, "← Back")
        self.assertEqual(on._state["step"], "account")
        on.save_progress("how")
        self.assertEqual(on.load_progress()["step"], "how")
        with patch.object(self.account, "signed_in", return_value=True):
            self.assertEqual(on._resume_step("how"), "how")
        self.assertEqual(on._resume_step("how"), "account")  # Signed out since: the account first.

    def test_how_loopcut_works_splits_the_parts_and_compares_free_and_pro(self):
        on = self.onboarding
        with patch.object(on, "_report") as report:
            splash, labels, texts = self.draw_how()
            self.draw_how()
        report.assert_called_once_with("chatgpt", None, code="how_shown")  # Once a session.
        for text in ("How Loopcut works", "Your ChatGPT plan", "The thinking", "Plan", "Build", "Check", "Fix",
                     "Runs on your paid ChatGPT plan", "Never uses Loopcut credits", "Your Loopcut account",
                     "The cloud tools", "3D model", "Motion", "Voice", "Sound", "Render", "Asset search is free",
                     "Others use credits, price shown first", "No ChatGPT plan? Everything still works.",
                     "Loopcut Fast does the thinking instead, or add your own API key.", "Free", "No card needed",
                     "Pro", "$20/mo", "Weekly limit", "Higher weekly limit", "Loopcut Pro model", "Credit top-ups",
                     "100 credits: 1 model from a photo, or 50 s of motion",
                     "2,200 credits: 27 models from photos, or 18 min of motion"):
            self.assertIn(text, labels)
        for text in ("Two parts work together", "Works with your ChatGPT plan", "Asset search |", "Plus or Pro",
                     "Credits every month", "Max"):  # No subtitle; only what differs; Max only for Max.
            self.assertNotIn(text, labels)
        self.assertNotIn("Your plan", labels)
        self.assertEqual([r[1] for r in splash.records if r[0] == "progress"], [on.BAR_LEAST, 1.0])
        self.assertNotIn("connect", [getattr(r[3], "step", "") for r in splash.operators()][1:])  # Explains only.

    def test_how_loopcut_works_without_sales_or_for_a_paid_plan(self):
        splash, labels, texts = self.draw_how(billing=False)
        for text in ("The thinking", "The cloud tools", "More coming", "Runs on your paid ChatGPT plan",
                     "Writes and runs each Blender step",
                     "Loopcut models do the thinking instead, or add your own API key."):
            self.assertIn(text, labels)
        for text in ("Free", "Pro ·", "Loopcut Pro", "$", "credits", "Your plan"):  # No plans, prices, credits.
            self.assertNotIn(text, labels)
        self.assertEqual(texts, ["Continue", "← Back"])
        self.assertFalse([r for r in splash.records if r[0] == "progress"])
        for plan in ("pro", "max", "free"):
            splash, labels, texts = self.draw_how(plan)
            self.assertEqual(labels.count("Your plan"), 1)
            self.assertEqual("See plans ↗" in texts, plan == "free")  # No upsell to a paid plan.

    def test_a_max_account_compares_free_with_max_not_pro(self):
        splash, labels, texts = self.draw_how("max")
        head = labels.split(" | ")
        self.assertEqual(head[head.index("No card needed") + 1:head.index("No card needed") + 3], ["Max", "Your plan"])
        for text in ("Highest weekly limit", "6,600 credits: 82 models from photos, or 55 min of motion",
                     "100 credits: 1 model from a photo, or 50 s of motion"):
            self.assertIn(text, labels)
        for text in ("Higher weekly limit", "$20/mo", "2,200 credits", "You have Max"):
            self.assertNotIn(text, labels)
        self.assertEqual([r[1] for r in splash.records if r[0] == "progress"], [self.onboarding.BAR_LEAST, 1.0])
        self.assertEqual(self.onboarding.PLANS["max"], {"credits": 6600, "price": 60})

    def test_the_pro_upsell_claims_only_what_pro_adds(self):
        """Cloud renders (Cycles and EEVEE) run on any plan's credits: the Pro line must not sell them."""
        on = self.onboarding
        self.assertEqual(on.PRO_LINE, "Pro plan: 2,200 credits a month, Loopcut Pro and top-ups")
        self.assertNotIn("render", on.PRO_LINE.lower())
        self.assertIn(f"{on.PLANS['pro']['credits']:,} credits", on.PRO_LINE)

    def test_the_plans_and_what_their_credits_make(self):
        on = self.onboarding
        self.assertEqual(on.PLANS, {"free": {"credits": 100, "price": 0}, "pro": {"credits": 2200, "price": 20},
                                    "max": {"credits": 6600, "price": 60}})  # The plans tables, 2026-10-21.
        self.assertEqual(on._makes(6600, 80, 2.0), "82 models from photos, or 55 min of motion")
        self.assertEqual(on._unit_prices(self.LIVE), (80, 2.0))
        self.assertEqual(on._unit_prices([{"name": "image_to_3d", "backend": "byok"}]), (None, None))
        self.assertEqual(on._makes(100, 80, 2.0), "1 model from a photo, or 50 s of motion")
        self.assertEqual(on._makes(2200, 80, 2.0), "27 models from photos, or 18 min of motion")
        self.assertEqual(on._makes(2200, None, 2.0), "18 min of motion")  # A missing price drops its part.
        self.assertEqual(on._makes(50, 80, None), "")
        splash, labels, texts = self.draw_how(descriptors=[])
        self.assertIn("| 2,200 credits |", f"| {labels} |")  # No prices at all: the credits themselves.

    def test_credits_still_loading_say_so_keep_repainting_and_never_block(self):
        self.onboarding._state["credits_asked"] = time.monotonic()
        with patch.object(self.onboarding, "_watch_popup") as watch:
            splash, labels, texts = self.signed_in_account_step(None)
        watch.assert_called()
        self.assertIn("Checking your free credits…", labels)
        self.assertEqual(texts, ["Continue", "See plans ↗"])
        with patch.object(self.account, "signed_in", return_value=True), \
                patch.object(self.onboarding, "_credits", return_value=None):
            self.assertTrue(self.onboarding._credits_pending())
            self.onboarding._state["credits_asked"] -= self.onboarding.CREDITS_PATIENCE + 1
            self.assertFalse(self.onboarding._credits_pending())

    def try_screen(self, descriptors, signed_in=True, billing=True, connected=True, styled=False):
        self.onboarding._state["step"] = "done"
        with patch.object(self.account, "signed_in", return_value=signed_in), \
                patch.object(self.onboarding, "_connected", return_value=connected), \
                patch.object(self.onboarding, "_descriptors", return_value=descriptors), \
                patch.object(self.onboarding, "_credits", return_value={"balance": 100}), \
                patch.object(self.onboarding, "_thumbnail", return_value=7), \
                patch.object(self.onboarding.settings, "route_label", return_value="your ChatGPT plan"), \
                patch.object(self.onboarding.config, "billing_open", return_value=billing):
            splash, drawn, _ = self.draw_step(styled=styled)
        self.assertTrue(drawn)
        return splash, " ".join(r[1] for r in splash.records if r[0] == "label")

    LIVE = [{"name": "motion_from_text", "backend": "managed", "cost": {"credits_estimate": 10}},
            {"name": "image_to_3d", "backend": "managed", "cost": {"credits_estimate": 80}}]

    def test_first_project_cards_show_their_price_struck_for_0_credits_and_both_paid_ones_fit_the_trial(self):
        with patch.object(self.onboarding, "_strikes", return_value=True):
            splash, labels = self.try_screen(self.LIVE, styled=True)
        parts = [r for r in splash.operators() if r[1] == "loopcut.onboarding_try"]
        texts = [r[2] for r in parts]
        # Each card: a picture, its title, line and price, and its Start; then Skip.
        self.assertEqual([r[3].card for r in parts], ["motion", "photo_3d", "model", ""])
        self.assertEqual(texts, ["Start", "Start", "Start", "Skip for now"])
        for title in ("Make it move", "Photo to 3D model", "Model a treasure chest"):
            self.assertIn(title, labels)
        styled = [r for r in splash.records if r[0] == "label" and len(r) > 2]
        for price in ("6 credits", "80 credits"):  # 3 of 5 s of motion; on the house (try_samples).
            struck = [r for r in styled if r[1] == price]
            self.assertEqual(len(struck), 1, price)
            self.assertEqual(struck[0][2], tuple(self.onboarding.DANGER))
            self.assertIs(struck[0][3].get("strike"), True)
        self.assertEqual([r[1] for r in styled].count("0 credits"), 2)
        self.assertIn("Free", labels)  # The modelling card runs on the model alone.
        self.assertTrue(all(r[4].enabled for r in parts))
        self.assertEqual(splash.records.count(("icon", 7)), 3)  # Every card has its picture, the same size.
        self.assertIn("● ● ● ●", labels)
        self.assertIn("You have 100 credits ($1.00)", labels)
        costs = [self.onboarding.card_cost(card, self.LIVE)[1] for card in self.onboarding.CARDS]
        self.assertLessEqual(sum(costs), 100)
        self.assertIn(self.onboarding.PRO_LINE, labels)
        self.assertIn("wm.url_open", [r[1] for r in splash.operators()])
        self.assertEqual(texts[-1], "Skip for now")
        for paragraph in ("Cmd+L", "jumps to the chat", "ChatGPT runs the conversation"):
            self.assertNotIn(paragraph, labels)

    def test_a_build_without_strike_still_draws_the_prices_without_the_keyword(self):
        with patch.object(self.onboarding, "_strikes", return_value=False):
            splash, labels = self.try_screen(self.LIVE, styled=True)
        price = next(r for r in splash.records if r[0] == "label" and r[1] == "80 credits")
        self.assertNotIn("strike", price[3])  # An unknown keyword would break the modal screen's drawing.
        self.assertIn("0 credits", labels)

    def test_a_card_on_the_users_own_key_shows_no_struck_price(self):
        splash, labels = self.try_screen([{**self.LIVE[1], "backend": "byok"}])
        self.assertIn("Your own key", labels)
        self.assertNotIn("80 credits", labels)

    def test_cards_follow_what_the_account_can_run_and_the_pro_strip_follows_sales(self):
        splash, labels = self.try_screen(self.LIVE[1:], billing=False)
        self.assertNotIn("Make it move", labels)  # Not offered to this account: no card.
        self.assertNotIn(self.onboarding.PRO_LINE, labels)
        self.assertNotIn("wm.url_open", [r[1] for r in splash.operators()])
        splash, labels = self.try_screen([], signed_in=False)  # After "Continue without an account".
        cards = {r[3].card: r for r in splash.operators() if r[1] == "loopcut.onboarding_try"}
        self.assertFalse(cards["motion"][4].enabled)
        self.assertIn("Needs a Loopcut account", labels)
        self.assertTrue(cards["model"][4].enabled)
        own_key = [{**self.LIVE[1], "backend": "byok"}]
        self.assertEqual(self.onboarding.card_cost(self.onboarding.CARDS[1], own_key), (True, None))

    def finish_with(self, card_id, signed_in=True, connected=True):
        self.onboarding._state["step"] = "done"
        op = self.onboarding.LOOPCUT_OT_onboarding_try()
        op.card = card_id
        with patch.object(self.account, "signed_in", return_value=signed_in), \
                patch.object(self.onboarding, "_connected", return_value=connected), \
                patch.object(self.onboarding, "_report") as report:
            self.assertEqual(op.execute(self.context), {"FINISHED"})
        return report, self.fake.app.timers.register.call_args.args[0]

    def test_a_card_ends_the_setup_closes_the_splash_and_sends_its_request_with_the_photo(self):
        report, later = self.finish_with("photo_3d")
        self.assertEqual(report.call_args_list, [unittest.mock.call("done", "loopcut", True, "card_photo_3d"),
                                                unittest.mock.call("done", "loopcut", True, "setup_finished")])
        self.assertEqual(json.loads(self.progress.read_text()), {"version": 1, "step": "done", "done": True})
        self.assertIsNone(self.onboarding._state["step"])
        window = object()
        wm = types.SimpleNamespace(splash_modal=True, windows=[window])
        with patch.object(self.fake, "context", types.SimpleNamespace(
                    window_manager=wm, temp_override=Mock(return_value=contextlib.nullcontext())), create=True), \
                patch.object(self.host, "start_request") as start:
            later()
        self.assertFalse(wm.splash_modal)
        self.fake.ops.wm.splash_close.assert_called_once_with()
        start.assert_called_once()
        self.assertIs(start.call_args.args[0], window)
        self.assertIn("attached photo", start.call_args.args[1])
        self.assertEqual(start.call_args.args[2], (str(self.onboarding.SAMPLE_PHOTO),))
        self.assertEqual(start.call_args.kwargs, {"try_it": "photo_3d"})  # Its cloud call is on the house.
        self.assertTrue(self.onboarding.SAMPLE_PHOTO.is_file())

    def test_finishing_without_an_account_brings_the_setup_back_next_start(self):
        report, _ = self.finish_with("", signed_in=False)
        self.assertEqual(report.call_args_list, [unittest.mock.call("done", "loopcut", True, "no_card"),
                                                unittest.mock.call("done", "loopcut", False, "abandoned_done")])
        self.assertEqual(json.loads(self.progress.read_text()), {"version": 1, "step": "account", "done": False})
        self.fresh_session()
        with patch.object(self.onboarding, "_connected", return_value=True):
            splash, drawn, _ = self.draw_step()
        self.assertTrue(drawn)
        self.assertEqual(self.onboarding._state["step"], "account")

    def test_each_screen_reports_being_shown_once(self):
        self.onboarding._state["step"] = "account"
        with patch.object(self.onboarding, "_report") as report:
            self.draw_step()
            self.draw_step()
        self.assertEqual(report.call_args_list, [unittest.mock.call("welcome", None, code="at_account"),
                                                unittest.mock.call("loopcut_account", None, code="shown")])

    # --- P0: the setup never traps, whatever is missing or failing --------------------------------------------

    def test_unregister_and_register_survive_a_preferences_reload(self):
        """An import rereads preferences, which disables and re-enables every add-on: a failing unregister()
        left Loopcut half registered, its next register() refused, and the account step without its button."""
        utils = self.fake.utils
        with patch.object(utils, "register_class", Mock(), create=True), \
                patch.object(utils, "unregister_class", Mock(), create=True) as unregister_class, \
                patch.object(utils.previews, "remove", Mock(), create=True) as remove:
            self.onboarding._preview["collection"] = object()
            self.onboarding.unregister()
            self.assertFalse(self.onboarding._registered)
            remove.assert_called_once()
            self.assertEqual(unregister_class.call_count, len(self.onboarding._CLASSES))
            self.onboarding.register()
            self.assertTrue(self.onboarding._registered)
            # Even a failing cleanup unregisters every class, so the next register() is not refused.
            unregister_class.reset_mock()
            with patch.object(self.onboarding, "_popups", Mock(clear=Mock(side_effect=RuntimeError("cleanup")))):
                with self.assertRaises(RuntimeError):
                    self.onboarding.unregister()
            self.assertEqual(unregister_class.call_count, len(self.onboarding._CLASSES))
        self.account.on_signed_in.clear()

    def test_no_function_makes_bpy_local_with_a_dotted_import(self):
        """`import bpy.utils.previews` inside a function binds `bpy` locally for the whole function: the earlier
        `bpy.app...` in it raised UnboundLocalError. That broke unregister() and trapped the setup."""
        import ast
        for path in sorted(PACKAGE.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for function in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
                for node in ast.walk(function):
                    if isinstance(node, ast.Import):
                        dotted = [a.name for a in node.names if a.name.startswith("bpy.") and a.asname is None]
                        self.assertFalse(dotted, f"{path.name}:{node.lineno} {function.name}: use `from bpy... import`")

    def test_any_error_while_drawing_a_step_leaves_a_way_out_and_drops_the_modal(self):
        self.onboarding._go("account")
        with patch.object(self.onboarding, "_draw_account", side_effect=KeyError("boom")), \
                redirect_stderr(io.StringIO()) as log:
            splash, drawn, wm = self.draw_step(wm=types.SimpleNamespace(splash_modal=True))
        self.assertTrue(drawn)
        self.assertFalse(wm.splash_modal)
        self.assertIn("KeyError", log.getvalue())
        self.assertIn(("operator", "wm.splash_close"), [r[:2] for r in splash.operators()])
        self.assertTrue(any("Something went wrong" in r[1] for r in splash.records if r[0] == "label"))
        self.assertIsNone(self.onboarding._state["step"])  # This session only; the saved step stays.
        self.assertEqual(json.loads(self.progress.read_text())["step"], "account")

    def test_a_step_whose_operators_are_missing_is_never_modal_and_rebuilds_when_they_return(self):
        self.onboarding._state["step"] = "account"
        self.fake.app.timers.is_registered.return_value = False
        self.addCleanup(setattr, self.fake.app.timers, "is_registered", Mock())
        with patch.object(self.onboarding, "missing_operators", return_value=["sign_in"]):
            splash, drawn, wm = self.draw_step(wm=types.SimpleNamespace(splash_modal=True))
            self.assertTrue(drawn)
            self.assertFalse(wm.splash_modal)
            labels = [r[1] for r in splash.records if r[0] == "label"]
            self.assertEqual(labels, ["Loopcut is starting…"])
            self.assertEqual([r[1] for r in splash.operators()], ["wm.splash_close"])
            self.fake.app.timers.register.assert_called_with(self.onboarding._rebuild_when_ready,
                                                             first_interval=self.onboarding._REFRESH_SECONDS,
                                                             persistent=True)
            self.onboarding._state["starting_since"] -= self.onboarding.STARTING_PATIENCE + 1
            with redirect_stdout(io.StringIO()):
                splash, _, wm = self.draw_step()
            self.assertIn("Loopcut could not finish starting", " ".join(r[1] for r in splash.records))
            self.assertFalse(wm.splash_modal)
        splash, drawn, wm = self.draw_step()  # Registered again: the step and its modal come back.
        self.assertIn("loopcut.sign_in", [r[1] for r in splash.operators()])
        self.assertTrue(wm.splash_modal)

    def test_the_watchdog_waits_out_a_reload_and_heals_an_open_splash(self):
        region = Mock()
        self.onboarding._state.update(step="account", region=region)
        with patch.object(self.onboarding, "_open_splash") as reopen, \
                patch.object(self.onboarding, "_region_exists", return_value=False):
            with patch.object(self.onboarding, "missing_operators", return_value=["sign_in"]):
                self.assertEqual(self.onboarding._watch(), self.onboarding._WATCH_SECONDS)
            with patch.object(self.onboarding, "_registered", False):
                self.onboarding._watch()
            reopen.assert_not_called()  # Never a setup drawn half way through re-registering.
        with patch.object(self.onboarding, "_region_exists", return_value=True):
            self.onboarding._watch()
        region.tag_refresh_ui.assert_called_once_with()  # A stale build heals on the next tick.
        self.onboarding._state["own_key"] = True  # Not under a text field being typed in.
        with patch.object(self.onboarding, "_region_exists", return_value=True):
            self.onboarding._watch()
        region.tag_refresh_ui.assert_called_once_with()

    def test_the_watchdog_rebuilds_only_when_what_the_step_shows_changed(self):
        """A rebuild under a resting pointer drops the hovered button's state, so the next press only re-arms it:
        rebuilt on every tick, every button took two clicks (P0, 2026-10-20). Now an unchanged step is left alone."""
        region = Mock()
        self.onboarding._state.update(step="account", region=region)
        self.onboarding._state["drawn"] = self.onboarding._signature()  # As draw_splash leaves it.
        with patch.object(self.onboarding, "_region_exists", return_value=True):
            for _ in range(5):
                self.onboarding._watch()
                self.onboarding._refresh_popups()
            region.tag_refresh_ui.assert_not_called()
            self.account._state.update(status="error", error="Offline")  # The sign-in thread finished.
            self.onboarding._watch()
            region.tag_refresh_ui.assert_called_once_with()
        drawn = Splash()
        context = types.SimpleNamespace(region_popup=region, window_manager=types.SimpleNamespace(splash_modal=False))
        self.onboarding.draw_splash(drawn, context, first_run=False)
        self.assertFalse(self.onboarding._changed())  # Drawing records what it showed.

    def test_report_a_bug_stays_off_the_modal_setup_so_every_button_there_is_a_way_on(self):
        support = importlib.import_module(NAME + ".support")
        for modal, drawn in ((True, []), (False, ["loopcut.report_bug"])):
            menu = types.SimpleNamespace(layout=Splash())
            support._draw_info(menu, types.SimpleNamespace(window_manager=types.SimpleNamespace(splash_modal=modal)))
            self.assertEqual([r[1] for r in menu.layout.operators()], drawn)

    def test_after_an_import_the_setup_reopens_only_once_registered_again(self):
        with patch.object(self.onboarding, "_open_splash") as reopen, \
                patch.object(self.host, "has_panel", return_value=True), \
                patch.object(self.onboarding, "_splash_open", return_value=False), \
                patch.object(self.fake.context, "window_manager", types.SimpleNamespace(windows=[object()]),
                             create=True):
            with patch.object(self.onboarding, "_registered", False):
                self.assertIsNone(self.onboarding._reopen())
            reopen.assert_not_called()
            retry = self.fake.app.timers.register.call_args
            self.assertEqual(retry.kwargs["first_interval"], 0.25)
            retry.args[0]()  # Registered by then.
            reopen.assert_called_once_with()
        context = types.SimpleNamespace(preferences=types.SimpleNamespace(addons={}))
        addon_utils = types.ModuleType("addon_utils")
        addon_utils.enable = Mock()
        with patch.dict(sys.modules, {"addon_utils": addon_utils}):
            self.onboarding._ensure_enabled(context)
        addon_utils.enable.assert_called_once_with(self.onboarding.settings.PACKAGE, default_set=True)

    def test_preferences_show_the_other_connection_whichever_is_the_model(self):
        class Box(Splash):
            def box(self, **kwargs):
                return Box(self.records)
            column = row = box
        for provider, chatgpt_row, loopcut_row in (("OPENAI", True, True), ("CHATGPT", False, True),
                                                   ("LOOPCUT", True, False)):
            with self.subTest(provider=provider):
                layout = Box()
                self.prefs.provider = provider
                self.settings.draw_connections(layout, self.prefs)
                names = [r[1] for r in layout.operators()]
                self.assertEqual("loopcut.chatgpt_sign_in" in names, chatgpt_row)
                self.assertEqual("loopcut.sign_in" in names, loopcut_row)
                self.assertTrue(any("Best setup: both" in r[1] for r in layout.records if r[0] == "label"))
        layout = Box()
        self.prefs.provider = "LOOPCUT"
        with patch.object(self.settings.chatgpt_auth, "status", return_value={
                "signed_in": True, "plan_usage": True, "email": "person@example.test"}):
            self.settings.draw_connections(layout, self.prefs)
        use = next(r for r in layout.operators() if r[1] == "loopcut.use_provider")
        self.assertEqual(use[3].provider, "CHATGPT")

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
        rows = [{"name": "future", "title": "Fixture title", "state": "ready", "voted": False, "level": None, "badge": None}]
        vote = {"state": "sending", "voted": True, "level": 0.5, "badge": "popular"}
        with patch.object(self.cloud, "coming_soon", return_value=rows) as catalog, \
                patch.object(self.cloud, "ui_message", return_value="") as message:
            first = self.host._cloud_panel_state(session)
            session["busy"] = True
            catalog.return_value = [{**rows[0], **vote, "title": "Renamed"}, {**rows[0], "name": "late", "title": "Later"}]
            message.return_value = "Update Loopcut"
            during = self.host._cloud_panel_state(session)
            self.assertEqual(len(during["coming_soon"]), len(first["coming_soon"]))
            self.assertEqual({key: during["coming_soon"][0][key] for key in vote}, vote, "votes update in place")
            self.assertEqual(during["coming_soon"][0]["title"], "Fixture title", "the catalog waits")
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

    def test_opening_the_vote_view_starts_it_fresh_and_asks_for_votes(self):
        session = self.state.new_session()
        self.state.ui.update(view="chat", picker={}, model_menu=True, service_settings=True, vote_order=["old"],
                             vote_scroll=120.0, vote_anim={"old": (0.0, 1.0, 0.0)})
        with patch.object(self.cloud, "refresh_votes") as refresh:
            self.host._do_action(session, ("votes_open", None))
        refresh.assert_called_once_with()
        keys = ("view", "vote_order", "vote_scroll", "vote_anim", "picker", "model_menu", "service_settings")
        self.assertEqual({key: self.state.ui[key] for key in keys},
                         {"view": "vote", "vote_order": None, "vote_scroll": 0.0, "vote_anim": {}, "picker": None,
                          "model_menu": False, "service_settings": False})
        with patch.object(self.cloud, "vote") as vote:
            self.host._do_action(session, ("vote", "render"))
        vote.assert_called_once_with("render")
        self.assertEqual(self.state.ui["view"], "vote")

    def test_rows_are_ranked_once_when_levels_arrive_and_never_jump_while_voting(self):
        session = self.state.new_session()
        measure = lambda font, size, text: len(text) * size * .6
        rows = [{"name": name, "title": name.title(), "summary": "", "state": "ready", "voted": False, "level": None,
                 "badge": None} for name in ("render", "sfx", "rig")]
        self.state.ui["view"] = "vote"
        with patch.object(self.cloud, "coming_soon", return_value=rows) as catalog, \
                patch.object(self.host, "time", types.SimpleNamespace(time=lambda: 100.0)):
            self.host._cloud_panel_state(session)
            self.assertFalse(self.state.ui.get("vote_order"), "nothing to rank before the levels")
            catalog.return_value = [{**rows[0], "level": 0.2}, {**rows[1], "level": 1.0}, {**rows[2], "level": 0.5}]
            self.host._cloud_panel_state(session)
            self.assertEqual(self.state.ui["vote_order"], ["sfx", "rig", "render"])
            catalog.return_value = [{**rows[0], "level": 1.0, "voted": True}, {**rows[1], "level": 0.3}, {**rows[2], "level": 0.5}]
            panel = self.host._cloud_panel_state(session)
            self.assertEqual(self.state.ui["vote_order"], ["sfx", "rig", "render"])
            display = self.host.layout.build(session, 420, 800, 1, measure, ui={**self.state.ui, **panel})
            self.assertEqual([h["action"][1] for h in display["hits"] if h["action"][0] == "vote"], ["sfx", "rig", "render"])
            # The chat view's strip neither ranks nor eases.
            self.state.ui.clear()
            panel = self.host._cloud_panel_state(session)
        self.assertNotIn("vote_order", self.state.ui)
        self.assertNotIn("animate_until", self.state.ui)
        self.assertEqual([row["level"] for row in panel["coming_soon"]], [1.0, 0.3, 0.5])

    def test_bars_grow_in_rank_order_then_ease_from_where_they_are_drawn(self):
        clock = [100.0]
        ease, stagger = self.host.VOTE_EASE_SECONDS, self.host.VOTE_INTRO_STAGGER
        self.state.ui.update(view="vote", vote_order=["a", "b"], vote_anim={})
        rows = [{"name": "a", "level": 1.0}, {"name": "b", "level": 0.5}, {"name": "c", "level": None}]
        with patch.object(self.host, "time", types.SimpleNamespace(time=lambda: clock[0])):
            levels = lambda: {row["name"]: row["level"] for row in self.host._eased_levels(rows)}
            self.assertEqual(levels(), {"a": 0.0, "b": 0.0, "c": None}, "grown from nothing")
            self.assertAlmostEqual(self.state.ui["animate_until"], 100.0 + stagger + ease)
            clock[0] = 100.0 + stagger
            self.assertGreater(levels()["a"], 0.0)
            self.assertEqual(levels()["b"], 0.0, "the second rank starts a beat later")
            clock[0] = 100.0 + ease / 2
            halfway = levels()
            self.assertAlmostEqual(halfway["a"], 0.875, msg="ease out: most of the way at half time")
            self.assertLess(halfway["b"] / 0.5, halfway["a"])
            clock[0] = 100.0 + stagger + ease
            self.assertEqual(levels(), {"a": 1.0, "b": 0.5, "c": None})
            # A new level from the server eases from the drawn bar, not from zero.
            clock[0] = 200.0
            rows[0]["level"] = 0.4
            self.assertEqual(levels()["a"], 1.0)
            self.assertAlmostEqual(self.state.ui["animate_until"], 200.0 + ease)
            clock[0] = 200.0 + ease / 2
            drawn = levels()["a"]
            self.assertAlmostEqual(drawn, 1.0 + (0.4 - 1.0) * 0.875)
            rows[0]["level"] = 0.9  # Another one mid-move: no jump.
            self.assertAlmostEqual(levels()["a"], drawn)
            self.assertAlmostEqual(self.state.ui["animate_until"], 200.0 + ease / 2 + ease)
            clock[0] = 200.0 + ease / 2 + ease
            self.assertAlmostEqual(levels()["a"], 0.9)
            self.assertEqual(levels()["b"], 0.5)
            # Opening the view again (vote_anim cleared) grows the bars once more.
            self.state.ui["vote_anim"] = {}
            self.assertEqual(levels()["a"], 0.0)

    def test_the_wheel_reads_down_the_vote_list_and_draw_clamps_it(self):
        session = self.state.new_session()
        session["scroll"] = 30.0
        self.state.ui.update(view="vote", vote_scroll=0.0)
        event = lambda kind, prev=0, y=0: types.SimpleNamespace(type=kind, mouse_prev_y=prev, mouse_y=y)
        self.host.scroll(session, event("WHEELDOWNMOUSE"))
        self.assertEqual(self.state.ui["vote_scroll"], self.host.SCROLL_STEP, "down the list, unlike the chat")
        self.host.scroll(session, event("TRACKPADPAN", prev=50, y=20))
        self.assertEqual(self.state.ui["vote_scroll"], self.host.SCROLL_STEP + 30)
        self.host.scroll(session, event("WHEELUPMOUSE"))
        self.host.scroll(session, event("WHEELUPMOUSE"))
        self.assertEqual(self.state.ui["vote_scroll"], 0.0)
        self.assertEqual(session["scroll"], 30.0, "the chat keeps its place")
        runtime = types.SimpleNamespace(signed_in=True, lock=threading.RLock(), vote_notice="")
        self.state.ui["vote_scroll"] = 500.0
        self.draw(session, runtime, max_scroll=120)
        self.assertEqual((self.state.ui["vote_scroll"], session["scroll"]), (120, 30.0))
        self.state.ui["view"] = "chat"
        self.draw(session, runtime, max_scroll=10)
        self.assertEqual((self.state.ui["vote_scroll"], session["scroll"]), (120, 10))

    def test_redraws_run_fast_while_bars_ease_then_at_the_thinking_pace(self):
        mainthread = importlib.import_module(NAME + ".mainthread")
        session = {"busy": False}
        with patch.object(mainthread, "time", types.SimpleNamespace(time=lambda: 100.0)), \
                patch.object(self.state, "session", return_value=session):
            self.assertIsNone(mainthread._animating())
            session["busy"] = True
            self.assertEqual(mainthread._animating(), mainthread.ANIMATE_EVERY)
            self.state.ui["animate_until"] = 100.5
            self.assertEqual(mainthread._animating(), mainthread.EASE_EVERY, "easing wins over the thinking pace")
            session["busy"] = False
            self.assertEqual(mainthread._animating(), mainthread.EASE_EVERY)
            self.state.ui["animate_until"] = 100.0
            self.assertIsNone(mainthread._animating(), "an ended ease stops the redraws")
        self.assertLess(mainthread.EASE_EVERY, mainthread.ANIMATE_EVERY)
        for period, redraws in ((None, 0), (mainthread.EASE_EVERY, 1)):
            with self.subTest(period=period), patch.object(mainthread, "_animating", return_value=period), \
                    patch.object(mainthread, "_redraw_requested", False), patch.object(mainthread, "_animated_at", 0.0), \
                    patch.object(self.host, "tag_redraw_all") as tag:
                mainthread._pump()
                self.assertEqual(tag.call_count, redraws)


if __name__ == "__main__":
    unittest.main()
