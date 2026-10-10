"""Exercise preference callbacks with saved RNA fields and no IDProperty mapping.

A private package keeps the suite's other bpy fakes intact. Registration installs
descriptors so reading UI properties calls their getters, just as Blender does.
"""

import importlib
import importlib.util
import io
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

    def __init__(self, calls=None, parent=None):
        self.calls = calls if calls is not None else []
        self.parent = parent
        self.enabled = True

    def child(self, **kwargs):
        return Layout(self.calls, self)

    box = column = row = split = child

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
        fake.app = types.SimpleNamespace(driver_namespace={})
        fake.utils = types.ModuleType("bpy.utils")
        fake.utils.previews = types.ModuleType("bpy.utils.previews")
        fake.utils.register_class = register
        cls.modules = patch.dict(sys.modules, {"bpy": fake, "bpy.utils": fake.utils,
                                               "bpy.utils.previews": fake.utils.previews})
        cls.modules.start()
        spec = importlib.util.spec_from_file_location(NAME, PACKAGE / "__init__.py",
                                                     submodule_search_locations=[str(PACKAGE)])
        package = importlib.util.module_from_spec(spec)
        sys.modules[NAME] = package
        spec.loader.exec_module(package)
        for name in ("settings", "cloud", "cloud_account"):
            setattr(cls, name, importlib.import_module(NAME + "." + name))
        cls.settings.register()

    @classmethod
    def tearDownClass(cls):
        for name in list(sys.modules):
            if name == NAME or name.startswith(NAME + "."):
                del sys.modules[name]
        cls.modules.stop()

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

    def test_coming_soon_choice_is_saved_hidden_rna_with_expanded_default(self):
        field = type(self.prefs).__annotations__['coming_soon_expanded']
        self.assertEqual(field.options['options'], {'HIDDEN'})
        self.assertTrue(self.prefs.coming_soon_expanded)
        self.prefs.coming_soon_expanded = False
        self.assertTrue(self.prefs.is_property_set('coming_soon_expanded'))
        self.assertFalse(self.prefs.coming_soon_expanded)
        self.assertFalse([entry for entry in self.draw_preferences('LOOPCUT', True)
                          if entry[:2] == ('prop', 'coming_soon_expanded')])

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
        for provider in ("META", "OPENAI", "OPENROUTER", "OLLAMA", "CUSTOM"):
            for stored in (True, False):
                with self.subTest(provider=provider, stored=stored):
                    calls = self.draw_preferences(provider, stored)
                    checkbox = next(n for n, entry in enumerate(calls) if entry[:2] == ("prop", "orchestrate"))
                    row = calls[checkbox][2]
                    self.assertIs(row.enabled, False)
                    self.assertEqual(calls[checkbox + 1][:2], ("label", "Workers and routing use the Loopcut service" if self.settings.config.BETA else
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
