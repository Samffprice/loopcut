"""Exercise preference callbacks with saved RNA fields and no IDProperty mapping.

A private package keeps the suite's other bpy fakes intact. Registration installs
descriptors so reading UI properties calls their getters, just as Blender does.
"""

import importlib
import importlib.util
import io
import os
import sys
import threading
import types
import unittest
from contextlib import redirect_stderr
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
        for provider in ("META", "OPENAI", "OPENROUTER", "OLLAMA", "CUSTOM", "ANTHROPIC"):
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

    def test_anthropic_is_the_last_provider_with_opus_by_default_and_a_class_a_note(self):
        self.assertEqual([p[0] for p in self.settings.PROVIDERS],
                         ["LOOPCUT", "META", "OPENAI", "OPENROUTER", "OLLAMA", "CUSTOM", "CHATGPT", "ANTHROPIC"],
                         "appended, so the stored enum values of the others stay what they were")
        _, label, base_url, model, note = self.settings.PROVIDERS[-1]
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
