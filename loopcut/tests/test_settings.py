"""Exercise preference callbacks with saved RNA fields and no IDProperty mapping.

A private package keeps the suite's other bpy fakes intact. Registration installs
descriptors so reading UI properties calls their getters, just as Blender does.
"""

import importlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, call, patch

from fake_preferences import AddonPreferences

PACKAGE = Path(__file__).resolve().parents[2] / "scripts/addons_core/loopcut"
NAME = "_f1_loopcut"


class Property:
    def __init__(self, **options):
        self.options = options

    def __get__(self, instance, owner):
        if instance is None:
            return self
        if "get" in self.options:
            return self.options["get"](instance)
        return instance.__dict__.get(self.name, self.options.get("default", ""))

    def __set__(self, instance, value):
        if "set" in self.options:
            self.options["set"](instance, value)
        else:
            instance.__dict__[self.name] = value


class Preferences(AddonPreferences):
    def is_property_set(self, name):
        return name in self.__dict__


def register(cls):
    for name, field in cls.__annotations__.items():
        field.name = name
        setattr(cls, name, field)


class Layout:
    """Record draw order and the enabled state of each checkbox's own row."""

    def __init__(self, calls=None, parent=None, kind=""):
        self.calls = calls if calls is not None else []
        self.parent = parent
        self.kind = kind
        self.enabled = True

    def child(self, **kwargs):
        return Layout(self.calls, self)

    def box(self, **kwargs):
        return Layout(self.calls, self, "box")

    column = row = split = child

    def prop(self, prefs, name, **kwargs):
        self.calls.append(("prop", name, self))

    def label(self, *, text, **kwargs):
        self.calls.append(("label", text, self))

    def separator(self):
        pass

    def menu(self, *args, **kwargs):
        pass

    def operator(self, name, **kwargs):
        self.calls.append(("operator", name, self))
        return types.SimpleNamespace()


OWN_KEY = "sk-" + "test-settings-0123456789abcdef"


class Timers:
    """bpy.app.timers, recorded: fire() runs what is registered, as Blender would once its wait ends."""

    def __init__(self):
        self.registered, self.registrations, self.unregistrations = {}, 0, 0

    def is_registered(self, fn):
        return fn in self.registered

    def register(self, fn, first_interval=0.0):
        self.registered[fn] = first_interval
        self.registrations += 1

    def unregister(self, fn):
        del self.registered[fn]
        self.unregistrations += 1

    def fire(self):
        for fn in list(self.registered):
            del self.registered[fn]
            fn()
        deadline = time.monotonic() + 5  # The check's worker thread reports back.
        while threading.active_count() > 1 and any(t.name == "loopcut-own-key-check" for t in threading.enumerate()):
            if time.monotonic() > deadline:
                raise AssertionError("the key check did not finish")
            time.sleep(0.01)


class SettingsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fake = types.ModuleType("bpy")
        fake.types = types.SimpleNamespace(AddonPreferences=Preferences, Operator=type("Operator", (), {}),
                                           Menu=type("Menu", (), {}))
        fake.props = types.SimpleNamespace(**{name: Property for name in
            ("StringProperty", "EnumProperty", "BoolProperty", "IntProperty")})
        fake.context = types.SimpleNamespace(scene={}, preferences=types.SimpleNamespace(addons={}))
        # Background: register() schedules no readiness check (that needs bpy.app.timers).
        fake.app = types.SimpleNamespace(driver_namespace={}, background=True)
        fake.utils = types.ModuleType("bpy.utils")
        fake.utils.previews = types.ModuleType("bpy.utils.previews")
        fake.utils.register_class = register
        cls.modules = patch.dict(sys.modules, {"bpy": fake, "bpy.utils": fake.utils,
                                               "bpy.utils.previews": fake.utils.previews})
        cls.modules.start()
        cls.addClassCleanup(cls.modules.stop)  # Runs even when the imports below fail.
        spec = importlib.util.spec_from_file_location(NAME, PACKAGE / "__init__.py",
                                                     submodule_search_locations=[str(PACKAGE)])
        package = importlib.util.module_from_spec(spec)
        sys.modules[NAME] = package
        spec.loader.exec_module(package)
        for name in ("settings", "cloud", "cloud_account"):
            setattr(cls, name, importlib.import_module(NAME + "." + name))
        cls.settings.register()

    def setUp(self):
        self.prefs = self.settings.LoopcutPreferences()
        self.runtime = types.SimpleNamespace(signed_in=False, lock=threading.RLock(), manifest={})
        for context in (patch.object(self.cloud, "_runtime", self.runtime),
                        patch.object(self.cloud, "_snapshot", return_value=self.runtime),
                        patch.object(self.cloud, "sync_settings"), patch.object(self.settings, "_changed"),
                        patch.object(self.settings.credentials, "vendor_key", return_value=""),
                        patch.object(self.settings.account, "signed_in", return_value=False),
                        patch.object(self.settings.urllib.request, "urlopen", side_effect=AssertionError("Unexpected HTTP"))):
            context.start()
            self.addCleanup(context.stop)

    def test_signed_out_account_entry_is_visible_with_every_model_provider(self):
        for provider in ('LOOPCUT', 'CHATGPT', 'OPENAI', 'CUSTOM'):
            self.prefs.provider = provider
            self.prefs.layout = Layout()
            fake_host = types.SimpleNamespace(NATIVE=False)
            fake_chatgpt_ui = types.SimpleNamespace(draw=Mock())
            with patch.dict(sys.modules, {NAME + '.ui.host': fake_host, NAME + '.chatgpt_ui': fake_chatgpt_ui}), \
                    patch.object(self.cloud, '_runtime', None), \
                    patch.dict(self.settings.account._state, status='idle', error=''):
                self.prefs.draw(None)
            self.assertIn(('operator', 'loopcut.sign_in'), [entry[:2] for entry in self.prefs.layout.calls])

    def test_fake_preferences_refuse_idproperties(self):
        for access in (lambda: self.prefs.get("no_generation"),
                       lambda: self.prefs["no_generation"],
                       lambda: self.prefs.__setitem__("no_generation", True)):
            with self.assertRaisesRegex(TypeError, "doesn't support IDProperties"):
                access()

    def test_both_model_pickers_keep_current_model_when_plan_locks_the_choice(self):
        account = self.settings.account
        state = importlib.import_module(NAME + ".state")
        rows = [{"id": "fast", "display_name": "Fast", "available": True, "default": True},
                {"id": "pro", "display_name": "Pro", "available": False,
                 "unlock_plan": {"id": "pro", "name": "Pro"}},
                {"id": "max", "display_name": "Opus", "available": False,
                 "unlock_plan": {"id": "max", "name": "Max"}}]
        for billing in (False, True):
            for pick in (self.settings.choose_model, self.settings.set_tier):
                for model, plan in (("pro", "Pro"), ("max", "Max")):
                    with self.subTest(billing=billing, picker=pick.__name__, model=model), \
                            patch.object(self.settings, "preferences", return_value=self.prefs), \
                            patch.object(self.settings.config, "billing_open", return_value=billing), \
                            patch.dict(account._state, models=rows):
                        self.prefs.provider, self.prefs.tier, self.prefs.model = "LOOPCUT", "fast", "fast"
                        session = state.reset()
                        pick(model)
                        self.assertEqual((self.prefs.tier, self.prefs.model), ("fast", "fast"))
                        notice = session["items"][-1]
                        self.assertEqual(notice["text"], f"{account.model_label(model)} needs the {plan} plan.")
                        self.assertEqual(notice["action_label"], f"Upgrade to {plan}" if billing else "")
                        if billing:
                            self.assertIn(f"reason=model_not_in_plan&plan={model}", notice["url"])
                        else:
                            self.assertEqual(notice["url"], "")

    def test_available_hosted_model_and_own_provider_still_switch(self):
        with patch.object(self.settings, "preferences", return_value=self.prefs), \
                patch.object(self.settings.account, "models", return_value=[{"id": "pro", "available": True}]):
            self.prefs.provider = "LOOPCUT"
            self.settings.choose_model("pro")
            self.assertEqual(self.prefs.tier, "pro")
            self.prefs.provider = "OPENAI"
            self.settings.choose_model("own-model")
            self.assertEqual(self.prefs.model, "own-model")

    def test_sign_out_clears_the_old_accounts_allowance_notice(self):
        state = importlib.import_module(NAME + ".state")
        with patch.dict(state.ui, allowance_notice={"item": {}}, warned_session_allowance=("free", "reset")):
            self.settings.account._set_account(None)
            self.assertNotIn("allowance_notice", state.ui)
            self.assertNotIn("warned_session_allowance", state.ui)

    def test_loopcut_model_changes_keep_notice_but_other_providers_clear_it(self):
        state = importlib.import_module(NAME + ".state")
        card = state.item_limit("Used up.", "session_limit", "free", "", None)
        with patch.dict(state.ui, allowance_notice={"item": card}), \
                patch.object(self.settings, "_changed"):
            self.prefs.provider = "LOOPCUT"
            self.prefs.tier = "pro"
            self.settings._provider_changed(self.prefs, None)
            self.assertIs(state.ui["allowance_notice"]["item"], card)
            self.prefs.tier = "fast"
            self.settings._provider_changed(self.prefs, None)
            self.assertIs(state.ui["allowance_notice"]["item"], card)
            for provider in ("CHATGPT", "OPENAI"):
                state.ui["allowance_notice"] = {"item": card}
                self.prefs.provider = provider
                self.settings._provider_changed(self.prefs, None)
                self.assertNotIn("allowance_notice", state.ui)

    def test_signed_out_defaults_and_toggles_use_saved_hidden_fields(self):
        for runtime in (None, self.runtime):
            with self.subTest(runtime=runtime), patch.object(self.cloud, "_runtime", runtime):
                prefs = self.settings.LoopcutPreferences()
                for name, default in self.cloud_account.DEFAULTS.items():
                    local_name = "local_" + name
                    field = type(prefs).__annotations__[local_name]
                    self.assertEqual(field.options["options"], {"HIDDEN"})
                    self.assertEqual(getattr(prefs, name), default)
                    for value in (not default, default, not default):
                        setattr(prefs, name, value)
                        self.assertEqual(getattr(prefs, local_name), value)
                        self.assertEqual(getattr(prefs, name), value)
                        self.assertTrue(prefs.is_property_set(local_name))
                self.assertFalse(self.settings.LoopcutPreferences().no_generation)
                self.assertTrue(self.settings.LoopcutPreferences().private_mode)

    def test_signed_in_settings_do_not_overwrite_local_preferences(self):
        self.prefs.no_generation = True
        self.prefs.private_mode = False
        self.runtime.signed_in = True
        self.runtime.manifest = {"account": {"settings": {"no_generation": False, "private_mode": True}}}
        with patch.object(self.cloud_account, "change_setting") as change:
            for name, value in self.cloud_account.DEFAULTS.items():
                self.assertEqual(getattr(self.prefs, name), value)
                setattr(self.prefs, name, value)
                change.assert_called_with(self.runtime, name, value)
                self.assertEqual(getattr(self.prefs, "local_" + name), not value)
        self.runtime.account_pending = {"no_generation": True, "private_mode": False}
        self.assertTrue(self.prefs.no_generation)
        self.assertFalse(self.prefs.private_mode)
        self.runtime.signed_in = False
        self.assertTrue(self.prefs.no_generation)
        self.assertFalse(self.prefs.private_mode)

    def test_ask_above_moves_to_100_unless_the_user_set_their_own(self):
        """0.2.20: Blender saves an add-on preference only once it is set (an IDProperty, is_property_set), so a user
        who never changed Ask above reads the new default, 100, and one who set a value keeps it, 25 included."""
        field = type(self.prefs).__annotations__["ask_above_credits"]
        self.assertEqual(field.options["default"], 100)
        with patch.object(self.settings, "preferences", return_value=self.prefs), \
                patch.object(self.settings.credentials, "api_key", return_value=""):
            self.assertFalse(self.prefs.is_property_set("ask_above_credits"))
            self.assertEqual(self.settings.values()["LOOPCUT_ASK_ABOVE_CREDITS"], "100")
            for own in (25, 40, 0):
                self.prefs.ask_above_credits = own
                self.assertTrue(self.prefs.is_property_set("ask_above_credits"))
                self.assertEqual(self.settings.values()["LOOPCUT_ASK_ABOVE_CREDITS"], str(own))
        self.assertEqual(self.settings.values.__globals__["config"].spending.__doc__.count("default 100"), 1)

    def test_paid_extras_is_a_preference_beside_ask_above_and_reaches_config(self):
        """Owner, 2026-10-26: Use when it helps (default) / Ask me first / Only when I ask; lane H-b's context line
        reads getattr(prefs, "extras_mode", "AUTO"), so the name and ids are fixed."""
        field = type(self.prefs).__annotations__["extras_mode"]
        self.assertEqual([(i, label) for i, label, _ in field.options["items"]],
                         [("AUTO", "Use when it helps"), ("ASK", "Ask me first"), ("ONLY_ASKED", "Only when I ask")])
        self.assertEqual((field.options["name"], field.options["default"]), ("Paid extras", "AUTO"))
        self.assertEqual(self.prefs.extras_mode, "AUTO")
        layout = Layout()
        with patch.object(self.cloud, "_runtime", None):
            self.settings.draw_spending(layout, self.prefs)
        drawn = [name for kind, name, _ in layout.calls if kind == "prop"]
        self.assertEqual(drawn[:2], ["extras_mode", "ask_above_credits"], "right above Ask above")
        with patch.object(self.settings, "preferences", return_value=self.prefs), \
                patch.object(self.settings.credentials, "api_key", return_value=""):
            self.assertEqual(self.settings.values()["LOOPCUT_EXTRAS"], "AUTO")
            self.prefs.extras_mode = "ONLY_ASKED"
            self.assertEqual(self.settings.values()["LOOPCUT_EXTRAS"], "ONLY_ASKED")

    def test_set_extras_saves_each_mode_and_this_turns_settings(self):
        """What the user says in chat ("ask me before using paid tools" → ask): the preference, saved, and the
        running turn's settings, so its next paid call goes by it."""
        tools = importlib.import_module(NAME + ".tools")
        state = importlib.import_module(NAME + ".state")
        spending = importlib.import_module(NAME + ".spending")
        session = state.reset()
        session["turn"] = turn = types.SimpleNamespace(spending=spending.Settings(ask_above=40))
        save = Mock()
        fake = types.SimpleNamespace(ops=types.SimpleNamespace(wm=types.SimpleNamespace(save_userpref=save)))
        with patch.object(self.settings, "preferences", return_value=self.prefs), patch.object(tools, "bpy", fake), \
                patch.object(self.settings.credentials, "api_key", return_value=""):
            for mode, value, label in (("ask", "ASK", "Ask me first"), ("only_asked", "ONLY_ASKED", "Only when I ask"),
                                       ("auto", "AUTO", "Use when it helps")):
                with self.subTest(mode=mode):
                    result = tools.set_extras(mode)
                    self.assertTrue(result.ok)
                    self.assertEqual(result.text, f'Saved: paid extras "{label}". The user can change it in '
                                                  'Preferences.')
                    self.assertEqual(self.prefs.extras_mode, value)
                    self.assertEqual(turn.spending, spending.Settings(ask_above=40, extras=value))
                    self.assertEqual(self.settings.values()["LOOPCUT_EXTRAS"], value)
            self.assertEqual(save.call_count, 3)
            with self.assertRaises(tools.InputMistake):
                tools.set_extras("never")
        schema = next(s["function"] for s in tools.SCHEMAS if s["function"]["name"] == "set_extras")
        self.assertEqual(schema["parameters"]["properties"]["mode"]["enum"], ["auto", "ask", "only_asked"])
        self.assertTrue(schema["description"].startswith("Only when the user says how paid tools should be used"))
        self.assertIs(tools._DISPATCH["set_extras"], tools.set_extras)

    def test_local_no_generation_reaches_config_values(self):
        with patch.object(self.settings, "preferences", return_value=self.prefs), \
                patch.object(self.settings.credentials, "api_key", return_value=""):
            self.assertNotIn("LOOPCUT_NO_GENERATION", self.settings.values())
            for value in (True, False):
                self.prefs.no_generation = value
                self.prefs.private_mode = not value
                self.assertEqual(self.settings.values()["LOOPCUT_NO_GENERATION"], str(value).lower())
                self.assertEqual(self.settings.values()["LOOPCUT_PRIVATE_MODE"], str(not value).lower())

    def test_saved_steps_preference_is_neither_registered_drawn_nor_exported(self):
        self.assertNotIn("max_steps", type(self.prefs).__annotations__)
        self.prefs.max_steps = 25  # An old saved field must not cap new turns.
        calls = self.draw_preferences("LOOPCUT", True)
        self.assertFalse([entry for entry in calls if entry[:2] == ("prop", "max_steps")])
        with patch.object(self.settings, "preferences", return_value=self.prefs), \
                patch.object(self.settings.credentials, "api_key", return_value=""):
            self.assertNotIn("LOOPCUT_MAX_STEPS", self.settings.values())

    def test_drawing_policy_properties_reads_callbacks_without_tracebacks(self):
        layout = Mock()
        layout.box.return_value = layout
        layout.row.return_value = layout
        layout.column.return_value = layout
        layout.prop.side_effect = lambda prefs, name, **kwargs: getattr(prefs, name)
        errors = io.StringIO()
        with redirect_stderr(errors), patch.object(self.cloud, "_runtime", None):
            self.settings.draw_vendor_keys(layout, self.prefs)
        self.assertEqual(errors.getvalue(), "")
        self.assertIn(call(self.prefs, "no_generation"), layout.prop.call_args_list)
        self.assertIn(call(self.prefs, "private_mode"), layout.prop.call_args_list)

    def test_workers_checkbox_is_disabled_with_note_for_each_byok_provider(self):
        for provider in ("META", "OPENAI", "OPENROUTER", "OLLAMA", "CUSTOM", "ANTHROPIC", "GEMINI", "KIMI"):
            for stored in (True, False):
                with self.subTest(provider=provider, stored=stored):
                    calls = self.draw_preferences(provider, stored)
                    checkbox = next(n for n, entry in enumerate(calls) if entry[:2] == ("prop", "orchestrate"))
                    row = calls[checkbox][2]
                    self.assertIs(row.enabled, False)
                    self.assertEqual(calls[checkbox + 1][:2], ("label", "Workers and routing use the Loopcut service" if not self.settings.config.billing_open() else
                                                          "Workers and routing run on Loopcut plans"))
                    self.assertIs(calls[checkbox + 1][2], row.parent, "the note is directly under the checkbox row")
                    self.assertIs(self.prefs.orchestrate, stored)

    def test_workers_checkbox_is_enabled_without_note_on_loopcut(self):
        calls = self.draw_preferences("LOOPCUT", True)
        checkbox = [entry for entry in calls if entry[:2] == ("prop", "orchestrate")]
        self.assertEqual(len(checkbox), 1)
        self.assertIs(checkbox[0][2].enabled, True)
        self.assertFalse([entry for entry in calls if entry[:2] == ("label", "Workers and routing run on Loopcut plans")])
        self.assertIs(self.prefs.orchestrate, True)

    def test_workers_checkbox_and_note_are_hidden_on_chatgpt(self):
        calls = self.draw_preferences("CHATGPT", True)
        self.assertFalse([entry for entry in calls if entry[:2] == ("prop", "orchestrate")])
        self.assertFalse([entry for entry in calls if entry[:2] == ("label", "Workers and routing run on Loopcut plans")])
        self.assertIs(self.prefs.orchestrate, True)

    def test_providers_keep_their_stored_order_with_new_ones_appended(self):
        """Blender saves an enum by its item's position: a row inserted anywhere but the end would turn every saved
        provider after it into the next one."""
        order = ["LOOPCUT", "META", "OPENAI", "OPENROUTER", "OLLAMA", "CUSTOM", "CHATGPT", "ANTHROPIC", "GEMINI", "KIMI"]
        self.assertEqual([p[0] for p in self.settings.PROVIDERS], order,
                         "appended, so the stored enum values of the others stay what they were")
        items = type(self.prefs).__annotations__["provider"].options["items"]
        self.assertEqual([item[0] for item in items], order)

    def test_gemini_and_kimi_presets(self):
        by_id = self.settings._BY_ID
        self.assertEqual(by_id["GEMINI"][1:4], ("Google Gemini (API key)",
                                                "https://generativelanguage.googleapis.com/v1beta/openai", ""))
        self.assertEqual(by_id["KIMI"][1:4], ("Kimi Code (your membership)", "https://api.kimi.ai/coding/v1",
                                              "kimi-for-coding"))
        self.assertIn("Fetch models", by_id["GEMINI"][4])
        for provider in ("GEMINI", "KIMI"):
            self.prefs.provider = provider
            self.settings._provider_changed(self.prefs, None)
            self.assertEqual((self.prefs.base_url, self.prefs.model), by_id[provider][2:4])

    def test_a_refused_key_on_gemini_or_kimi_names_it_as_preferences_does(self):
        ui = importlib.import_module(NAME + ".own_key_ui")
        for provider, name in (("GEMINI", "Google Gemini"), ("KIMI", "Kimi Code")):
            self.prefs.provider, self.prefs.base_url = provider, self.settings._BY_ID[provider][2]
            self.assertEqual(ui.own_key.message(ui.own_key.REFUSED, ui._about(self.prefs, OWN_KEY)),
                             f"This key was refused by {name}. Check it or make a new one.")

    def test_fetch_models_says_loopcut_in_the_user_agent(self):
        sent = []

        def urlopen(request, timeout=None):
            sent.append(request)
            response = Mock()
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            response.read.return_value = b'{"data": [{"id": "kimi-for-coding"}]}'
            return response
        self.prefs.provider, self.prefs.base_url = "KIMI", "https://api.kimi.ai/coding/v1"
        operator = self.settings.LOOPCUT_OT_fetch_models()
        operator.report = Mock()
        with patch.object(self.settings, "preferences", return_value=self.prefs), \
                patch.object(self.settings.credentials, "api_key", return_value=OWN_KEY), \
                patch.object(self.settings.urllib.request, "urlopen", side_effect=urlopen):
            self.assertEqual(operator.execute(None), {"FINISHED"})
        (request,) = sent
        self.assertEqual((request.full_url, request.get_header("User-agent")),
                         ("https://api.kimi.ai/coding/v1/models", "Loopcut (Blender add-on)"))
        self.assertEqual(self.settings.listed_models("KIMI"), ["kimi-for-coding"])
        self.settings._models.clear()

    def test_anthropic_keeps_its_place_with_opus_by_default_and_a_class_a_note(self):
        _, label, base_url, model, note = self.settings._BY_ID["ANTHROPIC"]
        self.assertEqual((label, base_url, model), ("Anthropic (Claude)", "anthropic://api.anthropic.com/v1", "claude-opus-5-5"))
        self.assertIn("Private (A)", note)
        self.assertIn("30 days", note)

    def test_anthropic_draws_the_key_with_a_console_link_and_lists_its_models(self):
        calls = self.draw_preferences("ANTHROPIC", True)
        drawn = [entry[:2] for entry in calls]
        self.assertIn(("prop", "api_key"), drawn)
        self.assertIn(("operator", "loopcut.anthropic_get_key"), drawn)
        self.assertIn(("operator", "loopcut.fetch_models"), drawn)
        self.assertEqual(self.settings.listed_models("ANTHROPIC"),
                         ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"])
        self.assertEqual(self.settings.listed_models("OPENAI"), [])
        self.assertNotIn(("operator", "loopcut.anthropic_get_key"), [e[:2] for e in self.draw_preferences("OPENAI", True)])

    def test_an_anthropic_key_reaches_config_under_its_own_base_url(self):
        self.prefs.provider, self.prefs.base_url, self.prefs.model = "ANTHROPIC", "anthropic://api.anthropic.com/v1", "claude-opus-5-5"
        with patch.object(self.settings, "preferences", return_value=self.prefs), \
                patch.object(self.settings.credentials, "api_key", return_value="k-1") as lookup:
            found = self.settings.values()
        lookup.assert_called_with("anthropic://api.anthropic.com/v1")
        self.assertEqual((found["LOOPCUT_BASE_URL"], found["LOOPCUT_MODEL"], found["LOOPCUT_API_KEY"]),
                         ("anthropic://api.anthropic.com/v1", "claude-opus-5-5", "k-1"))
        with patch.object(self.settings, "preferences", return_value=self.prefs):
            self.assertEqual([option[0] for option in self.settings.model_options()][:2], ["claude-opus-5-5", "claude-sonnet-5-5"])

    # With a ChatGPT plan, the panel's model menu lists the plan's own models (owner, 2026-10-26).
    PLAN_ROWS = [{"slug": "gpt-6.1-sol", "display_name": "GPT-6.1 Sol", "visibility": "list"},
                 {"slug": "gpt-6.1-terra", "display_name": "GPT-6.1 Terra", "visibility": "list"},
                 {"slug": "gpt-6-luna", "display_name": "GPT-6 Luna", "visibility": "list"},
                 {"slug": "internal", "display_name": "x", "visibility": "hide"}]

    def plan_menu(self, provider="CHATGPT", rows=None, error=None):
        """The panel's model menu on `provider` with a signed-in plan whose /models replies `rows` (or raises
        `error`); the worker runs inline. Returns the list of /models requests, for counting."""
        class Inline:
            def __init__(self, target, args=(), **kwargs):
                self.target, self.args = target, args

            def start(self):
                self.target(*self.args)

        requests = []

        def get_json(url, token=""):
            requests.append(url)
            if error is not None:
                raise error
            return {"models": rows if rows is not None else self.PLAN_ROWS}
        self.prefs.provider = provider
        auth = self.settings.chatgpt_auth
        for context in (patch.object(self.settings, "preferences", return_value=self.prefs),
                        patch.object(self.settings, "threading", types.SimpleNamespace(Thread=Inline)),
                        patch.object(auth, "plan_signed_in", return_value=True),
                        patch.object(auth, "access_token", return_value="fake-access-token"),
                        patch.object(auth, "_get_json", side_effect=get_json),
                        patch.object(importlib.import_module(NAME + ".chatgpt_ready"), "account_key",
                                     return_value=("oaiapp_fake", "https://auth.example.test", "user-fake"))):
            context.start()
            self.addCleanup(context.stop)
        return requests

    def reset_plan_models(self):
        cache = patch.dict(self.settings._plan_models, rows=[], account=(), at=0.0, fetching=False, failed=False)
        cache.start()
        self.addCleanup(cache.stop)

    def test_chatgpt_menu_fetches_the_plans_models_and_lists_them_by_display_name(self):
        self.reset_plan_models()
        self.plan_menu()
        self.prefs.model = "gpt-6.1-sol"
        self.assertEqual(self.settings.model_options(), [("gpt-6.1-sol", "gpt-6.1-sol", "", True)])  # Before.
        self.settings.refresh_plan_models()
        self.assertEqual(self.settings.model_options(),
                         [("gpt-6.1-sol", "GPT-6.1 Sol", "", True), ("gpt-6.1-terra", "GPT-6.1 Terra", "", False),
                          ("gpt-6-luna", "GPT-6 Luna", "", False)])
        self.assertEqual(self.settings.model_menu_note(), "")
        self.prefs.model = "gpt-6.1-terra"
        self.assertEqual([option[3] for option in self.settings.model_options()], [False, True, False])

    def test_chatgpt_plan_models_are_cached_ten_minutes(self):
        self.reset_plan_models()
        requests = self.plan_menu()
        clock = [1000.0]
        with patch.object(self.settings.time, "monotonic", side_effect=lambda: clock[0]):
            self.settings.refresh_plan_models()
            clock[0] += 9 * 60
            self.settings.refresh_plan_models()  # A second open within ten minutes: no request.
            self.assertEqual(len(requests), 1)
            clock[0] += 2 * 60
            self.settings.refresh_plan_models()
            self.assertEqual(len(requests), 2)

    def test_a_failed_plan_fetch_keeps_the_model_and_list_with_one_muted_line(self):
        self.reset_plan_models()
        self.prefs.model = "gpt-6.1-terra"
        self.plan_menu(error=self.settings.chatgpt_auth.ChatGPTAuthError("offline", "network"))
        with redirect_stdout(io.StringIO()):
            self.settings.refresh_plan_models()  # Nothing cached yet: the current model alone.
        self.assertEqual(self.settings.model_options(), [("gpt-6.1-terra", "gpt-6.1-terra", "", True)])
        self.assertEqual(self.settings.model_menu_note(), "Couldn't load your plan's models")
        self.reset_plan_models()
        self.plan_menu()
        self.settings.refresh_plan_models()
        listed = self.settings.model_options()
        self.settings._plan_models["at"] -= self.settings.PLAN_MODELS_TTL + 1
        self.plan_menu(error=self.settings.chatgpt_auth.ChatGPTAuthError("offline", "network"))
        with redirect_stdout(io.StringIO()) as printed:
            self.settings.refresh_plan_models()  # A stale list and a failed refresh: the old list stays.
        self.assertNotIn("fake-access-token", printed.getvalue())
        self.assertEqual(self.settings.model_options(), listed)
        self.assertEqual((self.prefs.model, self.settings.model_menu_note()),
                         ("gpt-6.1-terra", "Couldn't load your plan's models"))

    def test_no_plan_model_fetch_on_other_providers(self):
        self.reset_plan_models()
        for provider in ("LOOPCUT", "OPENAI", "ANTHROPIC", "CUSTOM"):
            with self.subTest(provider=provider):
                requests = self.plan_menu(provider)
                self.settings.refresh_plan_models()
                self.assertEqual(requests, [])
                self.assertFalse(self.settings._plan_models["fetching"])
                self.assertEqual(self.settings.model_menu_note(), "")

    # Row keys only, shaped as the plan's /models (supported_reasoning_levels): Sol lists no levels; Terra lists
    # ultra too, which the Responses API refuses, and a default the menu never adopts.
    EFFORT_ROWS = [{"slug": "gpt-6.1-sol", "display_name": "GPT-6.1 Sol"},
                   {"slug": "gpt-6.1-terra", "display_name": "GPT-6.1 Terra", "default_reasoning_level": "low",
                    "supported_reasoning_levels": [{"effort": e, "description": f"Terra {e}"} for e in
                                                   ("low", "medium", "high", "xhigh", "max", "ultra")]},
                   {"slug": "two-low-high", "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}]},
                   {"slug": "two-low-medium", "supported_reasoning_levels": [{"effort": "minimal"}, {"effort": "low"},
                                                                             {"effort": "medium"}]},
                   {"slug": "to-xhigh", "supported_reasoning_levels": [{"effort": e} for e in
                                                                       ("low", "medium", "high", "xhigh")]}]
    REAL_ROWS = Path(__file__).resolve().parent / "fixtures" / "chatgpt-models-2026-10-26.json"

    def turn_body(self):
        """The next turn's config and Responses request body, from the preferences as they are."""
        config = self.settings.config
        unset = {key: "" for key in ("LOOPCUT_BASE_URL", "LOOPCUT_MODEL", "LOOPCUT_API_KEY", "LOOPCUT_REASONING_EFFORT")}
        with patch.object(self.settings.chatgpt_auth, "status", return_value={"signed_in": True, "plan_usage": True}), \
                patch.object(config, "_find_env_file", return_value=None), patch.dict(os.environ, unset):
            cfg = config.load()
        responses = importlib.import_module(NAME + ".chatgpt_responses")
        body = responses.request_body(model=cfg.model, messages=[{"role": "user", "content": "A chair"}], tools=[],
                                      reasoning_effort=cfg.reasoning_effort)
        return cfg, body

    def test_reasoning_rows_offer_the_accepted_levels_the_chosen_model_lists_with_their_descriptions(self):
        self.reset_plan_models()
        self.plan_menu(rows=self.EFFORT_ROWS)
        self.settings.refresh_plan_models()
        self.prefs.model, self.prefs.chatgpt_effort = "gpt-6.1-sol", "high"
        self.assertEqual(self.settings.effort_options(),
                         [("low", "Low", False, "Fastest"), ("medium", "Medium", False, ""),
                          ("high", "High", True, ""), ("xhigh", "Extra High", False, "Slower; more careful work")])
        self.prefs.model = "gpt-6.1-terra"
        self.assertEqual([(effort, tip) for effort, _, _, tip in self.settings.effort_options()],
                         [(e, f"Terra {e}") for e in ("low", "medium", "high", "xhigh", "max")])
        self.prefs.model = "two-low-medium"  # minimal is not offered.
        self.assertEqual([option[0] for option in self.settings.effort_options()], ["low", "medium"])
        self.prefs.model = "not-listed"
        self.assertEqual([option[0] for option in self.settings.effort_options()], ["low", "medium", "high", "xhigh"])
        self.settings.choose_effort("max")
        self.assertEqual(self.prefs.chatgpt_effort, "max")
        for refused in ("ultra", "minimal", "none"):
            self.settings.choose_effort(refused)
            self.assertEqual(self.prefs.chatgpt_effort, "max")
        for provider in ("LOOPCUT", "OPENAI"):
            self.prefs.provider = provider
            self.assertEqual(self.settings.effort_options(), [])
            self.settings.choose_effort("low")
            self.assertEqual(self.prefs.chatgpt_effort, "max")

    def test_picking_a_model_snaps_only_an_effort_it_lacks_to_the_nearest_never_to_the_servers_default(self):
        self.reset_plan_models()
        self.plan_menu(rows=self.EFFORT_ROWS)
        self.settings.refresh_plan_models()
        for model, before, after in (("gpt-6.1-terra", "xhigh", "xhigh"),   # Taken: kept, not the row's "low".
                                     ("gpt-6.1-terra", "high", "high"),
                                     ("two-low-medium", "xhigh", "medium"),  # The nearest.
                                     ("two-low-high", "medium", "high"),     # A tie: the higher.
                                     ("to-xhigh", "max", "xhigh"),
                                     ("gpt-6.1-sol", "max", "xhigh"),        # No levels listed: low to xhigh.
                                     ("gpt-6.1-terra", "ultra", "max"),      # A level never sent: max if taken,
                                     ("to-xhigh", "ultra", "xhigh")):        # else the highest taken.
            with self.subTest(model=model, effort=before):
                self.prefs.model, self.prefs.chatgpt_effort = "gpt-6.1-sol", before
                self.settings.choose_model(model)
                self.assertEqual((self.prefs.model, self.prefs.chatgpt_effort), (model, after))

    def test_the_owners_plan_rows_list_eight_models_and_their_accepted_levels(self):
        self.reset_plan_models()
        rows = json.loads(self.REAL_ROWS.read_text(encoding="utf-8"))["models"]
        self.plan_menu(rows=rows)
        self.assertEqual(self.settings.LoopcutPreferences().chatgpt_effort, "high")  # Loopcut's default.
        self.prefs.model, self.prefs.chatgpt_effort = "gpt-6.1-sol", "high"
        self.settings.refresh_plan_models()
        self.assertEqual([option[0] for option in self.settings.model_options()],
                         ["gpt-6.1-sol", "gpt-6-astra", "gpt-6.1-terra", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol",
                          "gpt-5.6-terra", "gpt-5.6-luna"])  # Not gpt-reserve or codex-auto-review.
        five = ["low", "medium", "high", "xhigh", "max"]
        for model in ("gpt-6.1-terra", "gpt-6-luna", "gpt-5.6-luna", "gpt-6-astra"):
            self.assertEqual(self.settings.plan_efforts(model), five, model)
        self.settings.choose_model("gpt-6.1-terra")  # Its row's default is low: Loopcut keeps high.
        self.assertEqual(self.prefs.chatgpt_effort, "high")
        self.prefs.chatgpt_effort = "ultra"  # Stored somehow: moving to a Luna lands on max.
        self.settings.choose_model("gpt-6-luna")
        self.assertEqual((self.prefs.model, self.prefs.chatgpt_effort), ("gpt-6-luna", "max"))

    def test_ultra_is_never_offered_or_sent_though_the_plans_rows_list_it(self):
        self.reset_plan_models()
        rows = json.loads(self.REAL_ROWS.read_text(encoding="utf-8"))["models"]
        self.assertIn("ultra", [level["effort"] for level in rows[0]["supported_reasoning_levels"]])
        self.plan_menu(rows=rows)
        self.settings.refresh_plan_models()
        self.assertNotIn("ultra", [effort for effort, *_ in self.settings.CHATGPT_EFFORTS])
        self.prefs.model, self.prefs.chatgpt_effort = "gpt-6.1-terra", "high"
        for slug, *_ in self.settings.model_options():
            self.prefs.model = slug
            self.assertNotIn("ultra", [option[0] for option in self.settings.effort_options()], slug)
        self.prefs.model = "gpt-6.1-terra"
        self.settings.choose_effort("ultra")
        self.assertEqual(self.prefs.chatgpt_effort, "high")
        self.settings.choose_effort("max")  # Max goes through unchanged.
        self.assertEqual(self.turn_body()[1]["reasoning"]["effort"], "max")
        self.prefs.chatgpt_effort = "ultra"  # Stored somehow: the turn still sends an accepted level.
        cfg, body = self.turn_body()
        self.assertEqual((cfg.reasoning_effort, body["reasoning"]["effort"]), ("max", "max"))

    def test_the_next_turn_sends_the_model_and_effort_picked_in_the_menu(self):
        self.reset_plan_models()
        self.plan_menu()
        self.settings.refresh_plan_models()
        self.prefs.model, self.prefs.chatgpt_effort = "gpt-6.1-sol", "high"
        self.settings.choose_model(self.settings.model_options()[1][0])  # GPT-6.1 Terra.
        self.settings.choose_effort(self.settings.effort_options()[3][0])  # Extra High.
        cfg, body = self.turn_body()
        self.assertEqual((cfg.base_url, cfg.model, cfg.reasoning_effort),
                         (self.settings.config.CHATGPT_PLAN_BASE_URL, "gpt-6.1-terra", "xhigh"))
        self.assertEqual((body["model"], body["reasoning"]["effort"]), ("gpt-6.1-terra", "xhigh"))

    # "Use this key instead of credits", per tool-vendor key (owner, 2026-10-16).
    def draw_vendor_keys(self, keys):
        layout = Layout()
        with patch.object(self.settings.credentials, "vendor_key", side_effect=lambda name: "set" if name in keys else ""), \
                patch.object(self.cloud, "_runtime", None):
            self.settings.draw_vendor_keys(layout, self.prefs)
        return [entry[1] for entry in layout.calls if entry[0] == "prop"]

    def test_key_first_checkbox_shows_only_once_its_key_is_set(self):
        boxes = {row[4] for row in self.settings.VENDOR_ROWS}
        self.assertFalse(boxes & set(self.draw_vendor_keys(())))
        drawn = self.draw_vendor_keys(("FAL_KEY",))
        self.assertEqual(boxes & set(drawn), {"fal_key_first"})
        self.assertEqual(drawn[drawn.index("fal_vendor_key") + 1], "fal_key_first", "right under its key")
        self.assertEqual(boxes & set(self.draw_vendor_keys({row[0] for row in self.settings.VENDOR_ROWS})), boxes)
        self.assertNotIn("byok_tools", drawn, "the old per-tool list is folded into the checkboxes")

    def test_key_first_says_who_pays_right_under_it(self):
        """F0286: the box comes ticked, with no line saying the key's own account is charged."""
        def drawn():
            layout = Layout()
            with patch.object(self.settings.credentials, "vendor_key", side_effect=lambda name: "set" if name == "OPENAI_API_KEY" else ""), \
                    patch.object(self.cloud, "_runtime", None):
                self.settings.draw_vendor_keys(layout, self.prefs)
            entries = [entry[:2] for entry in layout.calls]
            return entries[entries.index(("prop", "openai_key_first")) + 1]
        self.assertEqual(drawn(), ("label", "Charges go to your OpenAI account, not your Loopcut credits."))
        self.prefs.openai_key_first = False
        self.assertEqual(drawn(), ("label", "Loopcut credits first, then this key."))

    def test_key_first_is_on_by_default_short_and_only_for_tool_vendors(self):
        self.assertEqual([row[0] for row in self.settings.VENDOR_ROWS], list(self.settings.credentials.VENDOR_KEY_NAMES))
        for row in self.settings.VENDOR_ROWS:
            field = type(self.prefs).__annotations__[row[4]]
            self.assertIs(field.options["default"], True)
            self.assertEqual(field.options["name"], "Use this key instead of credits")
            self.assertIs(getattr(self.prefs, row[4]), True)
        self.assertNotIn("byok_tools", type(self.prefs).__annotations__)
        self.assertFalse([name for name in type(self.prefs).__annotations__ if "anthropic" in name and "first" in name])

    def test_key_first_reaches_the_tool_routes(self):
        self.prefs.provider = "OPENAI"
        with patch.object(self.settings, "preferences", return_value=self.prefs), \
                patch.object(self.settings.credentials, "api_key", return_value=""), \
                patch.object(self.settings.config, "_find_env_file", return_value=None), \
                patch.dict(os.environ):
            for name in self.settings.credentials.VENDOR_KEY_NAMES:
                os.environ.pop(self.settings.config.OWN_KEY_FIRST + name, None)  # The environment would win.
            keys = {"OPENAI_API_KEY": True}
            self.assertEqual(self.settings.values()["LOOPCUT_OWN_KEY_FIRST_OPENAI_API_KEY"], "true")
            self.assertEqual(self.settings.config.service_backends(keys), {"image_generate": "byok", "image_edit": "byok"})
            self.prefs.openai_key_first = False
            self.assertEqual(self.settings.values()["LOOPCUT_OWN_KEY_FIRST_OPENAI_API_KEY"], "false")
            self.assertEqual(self.settings.config.service_backends(keys), {})
        self.settings._key_first_changed(self.prefs, None)
        self.cloud.sync_settings.assert_called_once_with()

    def test_privacy_and_support_box_holds_both_switches_and_report_a_problem(self):
        """Proposal 015: the conversation-log switch (on by default) and Report a Problem live together in one box
        on the preferences page, the only place the button is drawn."""
        def box_of(entry):
            layout = entry[2]
            while layout is not None and layout.kind != "box":
                layout = layout.parent
            return layout
        self.assertTrue(self.settings.LoopcutPreferences.share_conversations.options["default"])
        self.assertIn("never used for training", self.settings.LoopcutPreferences.share_conversations.options["description"])
        for provider in ("LOOPCUT", "CHATGPT", "OPENAI"):
            with self.subTest(provider=provider):
                calls = self.draw_preferences(provider, False)
                entries = [next(c for c in calls if c[:2] == wanted) for wanted in
                           (("prop", "share_conversations"), ("prop", "share_usage_stats"),
                            ("operator", "loopcut.report_problem"))]
                box = box_of(entries[0])
                self.assertIsNotNone(box)
                self.assertTrue(all(box_of(entry) is box for entry in entries))
                self.assertEqual(next(c[1] for c in calls if c[0] == "label" and c[2] is box), "Privacy and Support")
                labels = " ".join(c[1] for c in calls if c[0] == "label" and box_of(c) is box)
                self.assertIn("90 days", labels)
                self.assertIn("never uses them for training", labels)

    def test_the_last_report_shows_in_the_privacy_box(self):
        session_logs = importlib.import_module(NAME + ".session_logs")
        self.addCleanup(session_logs._report.update, status="", code="", message="")
        session_logs._report.update(status="failed", code="", message="Reports aren't available right now. "
                                                                      "Please email hello@loopcut.org instead.")
        calls = self.draw_preferences("LOOPCUT", False)
        self.assertIn("Reports aren't available right now. Please email hello@loopcut.org instead.",
                      [c[1] for c in calls if c[0] == "label"])

    # --- The own-key check (own_key_ui.py) ----------------------------------------------------------------------

    def own_key_ui(self, timers=None, background=False):
        """own_key_ui with its state reset, this test's preferences and key, and Blender's timers recorded."""
        ui = importlib.import_module(NAME + ".own_key_ui")
        ui._result.update(status="idle", code="", text="", settings=None)
        ui._looked.clear()
        ui._again = False
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        for context in (patch.object(ui.bpy, "app", types.SimpleNamespace(background=background, timers=timers)),
                        patch.object(self.settings, "preferences", return_value=self.prefs),
                        patch.object(ui.credentials, "api_key", return_value=OWN_KEY),
                        patch.object(ui.credentials, "folder", return_value=self.folder),
                        patch.object(ui.mainthread, "run_on_main", side_effect=lambda fn: fn()),
                        patch.object(ui.mainthread, "request_redraw")):
            context.start()
            self.addCleanup(context.stop)
        self.prefs.provider, self.prefs.base_url, self.prefs.model = "OPENAI", "https://api.openai.com/v1", "gpt-x"
        return ui

    def test_saving_a_key_or_changing_the_base_url_model_or_provider_asks_for_a_check(self):
        ui = importlib.import_module(NAME + ".own_key_ui")
        fields = type(self.prefs).__annotations__
        self.assertIs(fields["model"].options["update"], self.settings._connection_changed)
        self.assertIs(fields["base_url"].options["update"], self.settings._connection_changed)
        with patch.object(ui, "request") as request, patch.object(self.settings.credentials, "store") as store:
            self.prefs.provider, self.prefs.base_url = "OPENAI", "https://api.openai.com/v1"
            self.prefs.api_key = OWN_KEY
            store.assert_called_once_with("https://api.openai.com/v1", OWN_KEY)
            self.settings._connection_changed(self.prefs, None)
            self.settings._provider_changed(self.prefs, None)
        self.assertEqual(request.call_count, 3)

    def test_the_check_waits_for_changes_to_settle_runs_off_the_main_thread_and_never_for_loopcut_or_chatgpt(self):
        timers = Timers()
        ui = self.own_key_ui(timers)
        ran = []

        def check(about, base_url, key, model, folder, again=False, cancelled=None):
            ran.append((threading.current_thread() is threading.main_thread(), about["name"], base_url, key, model,
                        folder, again))
            return {"code": "ok", "text": f"Works with {model}."}
        with patch.object(ui.own_key, "check", side_effect=check):
            for provider in ("LOOPCUT", "CHATGPT"):
                self.prefs.provider = provider
                ui.request()
                self.assertEqual((timers.registered, ui.status()["status"]), ({}, "idle"))
            self.prefs.provider = "OPENAI"
            with patch.object(ui.credentials, "api_key", return_value=""):
                ui.request()  # No key saved yet: the key field says so; saving one asks again.
                self.assertEqual((timers.registered, ui.status()["status"]), ({}, "idle"))
            for _ in range(5):  # A burst of changes: one check, after the last.
                ui.request()
            self.assertEqual(timers.registered, {ui._start: ui.DEBOUNCE})
            self.assertEqual((timers.registrations, timers.unregistrations), (5, 4))
            self.assertEqual(ui.status()["status"], "checking")
            timers.fire()
            self.assertEqual(ran, [(False, "OpenAI", "https://api.openai.com/v1", OWN_KEY, "gpt-x", self.folder, False)])
            self.assertEqual(ui.status(), {"status": "done", "code": "ok", "text": "Works with gpt-x."})
            self.assertEqual(ui.panel_line(), "")  # A pass stays out of the chat panel.
            ui.LOOPCUT_OT_own_key_check().execute(None)  # Check again: soon, and it may probe again (own_key.RECHECK).
            self.assertEqual(timers.registered, {ui._start: ui.AGAIN_DELAY})
            timers.fire()
            self.assertEqual(ran[-1][-1], True)
            ui.request()
            self.prefs.provider = "LOOPCUT"  # Switched away before the wait ended: no check.
            timers.fire()
            self.assertEqual(len(ran), 2)

    def test_a_failure_shows_under_the_fields_with_check_again_and_above_the_composer(self):
        ui = self.own_key_ui(Timers())
        refused = "This key was refused by OpenAI. Check it or make a new one."
        with patch.object(ui.own_key, "check", return_value={"code": "refused", "text": refused}):
            ui.request()
            ui.bpy.app.timers.fire()
        self.assertEqual(ui.panel_line(), refused)
        calls = self.draw_preferences("OPENAI", False)
        labels = [entry[1] for entry in calls if entry[0] == "label"]
        self.assertIn(refused, labels)
        self.assertIn("loopcut.own_key_check", [entry[1] for entry in calls if entry[0] == "operator"])
        self.prefs.model = "gpt-y"  # Its answer was about gpt-x.
        self.assertEqual((ui.panel_line(), ui.status()["status"]), ("", "idle"))
        self.prefs.model = "gpt-x"
        ui._result.update(status="done", code="rate_limited", text="OpenAI is limiting this key right now. Try "
                          "again in a minute.", settings=ui._settings(self.prefs))
        self.assertEqual(ui.panel_line(), "")  # Passing, not a setup mistake: Preferences only.

    def test_after_a_relaunch_the_saved_probe_result_shows_with_no_request(self):
        ui = self.own_key_ui(Timers())
        ui.own_key.remember(self.folder, OWN_KEY, "https://api.openai.com/v1", "gpt-x", "no_credit", "")
        with patch.object(ui.own_key, "check", side_effect=AssertionError("no check on launch")):
            self.assertEqual(ui.panel_line(), "This OpenAI account has no credit left.")
        self.assertEqual(ui.bpy.app.timers.registered, {})
        self.assertNotIn(OWN_KEY, (self.folder / ui.own_key.CACHE_FILE).read_text(encoding="utf-8"))

    def draw_preferences(self, provider, stored):
        self.prefs.provider = provider
        self.prefs.orchestrate = stored
        self.prefs.layout = Layout()
        # These panels have their own tests; stub their drawing to exercise the preferences page.
        fake_host = types.SimpleNamespace(NATIVE=False)
        fake_chatgpt_ui = types.SimpleNamespace(draw=Mock())
        with patch.dict(sys.modules, {NAME + ".ui.host": fake_host, NAME + ".chatgpt_ui": fake_chatgpt_ui}), \
                patch.object(self.settings.account, "draw"), patch.object(self.cloud, "_runtime", None):
            self.prefs.draw(None)
        return self.prefs.layout.calls


if __name__ == "__main__":
    unittest.main()
