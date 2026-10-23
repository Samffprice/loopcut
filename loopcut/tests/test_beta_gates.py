"""Beta UI and support lifecycle with an isolated bpy stand-in; no browser or Blender."""

import ast
import importlib
import importlib.util
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fake_preferences import AddonPreferences
from test_onboarding import Splash
from test_settings import Layout

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / 'scripts/addons_core/loopcut'
NAME = '_gate_loopcut'


class BetaGatesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fake = types.ModuleType('bpy')
        fake.types = types.SimpleNamespace(**{name: type(name, (), {}) for name in
            ('Operator', 'AddonPreferences', 'Menu', 'FileHandler', 'SpaceTextEditor', 'OperatorFileListElement')})
        fake.types.AddonPreferences = AddonPreferences
        fake.props, fake.ops = Mock(), Mock()
        fake.app = types.SimpleNamespace(version=(5, 2, 0), timers=Mock())
        fake.context = types.SimpleNamespace()
        fake.utils = types.ModuleType('bpy.utils')
        fake.utils.previews = types.ModuleType('bpy.utils.previews')
        fake.utils.register_class, fake.utils.unregister_class = Mock(), Mock()
        cls.fake = fake
        cls.modules = patch.dict(sys.modules, {'bpy': fake, 'bpy.utils': fake.utils,
            'bpy.utils.previews': fake.utils.previews, NAME + '.ui.draw': types.ModuleType(NAME + '.ui.draw')})
        cls.modules.start()
        cls.addClassCleanup(cls.modules.stop)  # Runs even when the imports below fail.
        spec = importlib.util.spec_from_file_location(NAME, PACKAGE / '__init__.py',
                                                     submodule_search_locations=[str(PACKAGE)])
        cls.package = importlib.util.module_from_spec(spec)
        sys.modules[NAME] = cls.package
        spec.loader.exec_module(cls.package)
        for name in ('config', 'state', 'agent', 'account', 'cloud_account', 'cloud_http',
                     'settings', 'update', 'support', 'ui.host', 'ui.layout'):
            setattr(cls, name.replace('ui.', ''), importlib.import_module(NAME + '.' + name))

    def setUp(self):
        # BETA is the version label now; purchase surfaces follow the server's flags.billing (config.billing_open).
        for context in (patch.object(self.config, 'BETA', True), patch.object(self.config, 'billing_open', return_value=False)):
            context.start()
            self.addCleanup(context.stop)

    def display(self, *items, **ui):
        session = self.state.new_session()
        session['items'] = list(items)
        return self.layout.build(session, 420, 1000, 1, lambda font, size, text: len(text) * size * .6,
                                 'fast', None, ui)

    def assert_no_purchase(self, value):
        serialized = json.dumps(value).lower()
        for token in ('/pricing', '/checkout', 'see plans', 'upgrade', 'add credits', 'higher plan'):
            # Empty compatibility keys ("upgrade": null/"") are not UI surfaces.
            cleaned = serialized.replace('"upgrade": null', '').replace('"upgrade": ""', '')
            self.assertNotIn(token, cleaned)

    def test_low_allowance_notices_have_facts_and_no_purchase(self):
        for day, (plan, billing) in enumerate((('free', False), ('pro', False), ('free', True)), 21):
            with self.subTest(plan=plan, billing=billing), patch.object(self.config, 'billing_open', return_value=billing):
                session = self.state.reset()
                items = session['items']
                reset = f'2099-10-{day}T18:00:00Z'  # Each case exercises a distinct server window.
                self.agent._warn_low_allowance(session, items, {'plan': plan, 'week': {'used': .95, 'resets_at': reset}})
                self.agent._warn_low_allowance(session, items, {'plan': plan, 'week': {'used': .96, 'resets_at': reset}})
                self.assertEqual(len(items), 1)
                self.assertIn("95% of this week's chat allowance", items[0]['text'])
                self.assertEqual(items[0]['url'], '')
                self.assertEqual(items[0]['action_label'], '')
                self.assert_no_purchase(items)
                self.assert_no_purchase(self.display(*items))

    def test_limit_state_and_saved_cards_suppress_offers_and_keep_reset(self):
        offer = {'url': self.config.pricing_url(), 'label': 'Upgrade', 'note': 'Purchase Pro now'}
        with patch.object(self.config, 'billing_open', return_value=True):
            old = self.state.item_limit('Upgrade to continue', 'week_limit', 'free', '2026-10-12', offer)
            self.assertEqual(old['upgrade'], offer)
            self.assertIn(('limit_upgrade', 0), [h['action'] for h in self.display(old)['hits']])
        current = self.state.item_limit(old['text'], old['reason'], old['plan'], old['resets_at'], offer)
        self.assertIn('2026-10-12', current['text'])
        self.assert_no_purchase(current)
        for status in ('', 'waiting', 'done'):
            drawn = self.display({**old, 'status': status})
            self.assert_no_purchase(drawn)
            self.assertIn('2026-10-12', json.dumps(drawn))

    def test_credit_error_state_and_saved_tool_card_have_no_purchase(self):
        error = self.cloud_http.CloudError('Refused', 'insufficient_credits', 402,
            {'balance': 1, 'needed': 20, 'upgrade': self.config.pricing_url()})
        detail = self.cloud_account.error_details(error)
        self.assertIn('needs 20 ($0.20), you have 1', detail['message'])
        self.assert_no_purchase(detail)
        card = self.state.item_tool('tool', 'Tool', '')
        card.update(status='failed', expanded=True, cloud_error={'upgrade': self.config.pricing_url()})
        self.assert_no_purchase(self.display(card))
        with patch.object(self.config, 'billing_open', return_value=True):
            self.assertEqual(self.cloud_account.error_details(error)['upgrade'], self.config.pricing_url())
            self.assertIn(('cloud_upgrade', 0), [h['action'] for h in self.display(card)['hits']])

    def test_saved_notice_and_stale_actions_cannot_open_purchase(self):
        notice = self.state.item_notice("About 5% is left. Upgrading now keeps going.", 'See plans',
                                       url=self.config.pricing_url('low_allowance'))
        self.assert_no_purchase(self.display(notice))
        session = self.state.new_session()
        session['items'] = [notice]
        with patch.object(self.account, 'open_browser') as browser:
            for kind in ('open_url', 'limit_upgrade', 'cloud_upgrade'):
                self.host._do_action(session, (kind, 0))
            self.host._start_upgrade(session, {})
            browser.assert_not_called()

    def test_model_notes_hide_plan_requirement_in_both_pickers(self):
        rows = [{'id': 'pro', 'available': False, 'usage_multiplier': 2,
                 'unlock_plan': {'name': 'Max'}}]
        with patch.dict(self.account._state, models=rows):
            self.assertEqual(self.account.models()[0]['note'], '2x usage  ·  Unavailable')
            with patch.object(self.config, 'billing_open', return_value=True):
                self.assertEqual(self.account.models()[0]['note'], '2x usage  ·  Max plan')

    def test_preferences_gate_purchase_and_show_beta_version(self):
        def operator(layout, name, **kwargs):
            layout.calls.append(('operator', name))
            return types.SimpleNamespace()

        for beta in (True, False):
            layout = Layout()
            prefs = types.SimpleNamespace(layout=layout, provider='CUSTOM', base_url='https://example.test')
            with patch.object(self.config, 'BETA', beta), patch.object(self.config, 'billing_open', return_value=not beta), patch.object(Layout, 'operator', operator), \
                    patch.object(self.account, 'signed_in', return_value=False), \
                    patch.object(self.settings.credentials, 'vendor_keys_set', return_value={}):
                self.settings.LoopcutPreferences.draw(prefs, None)
                self.assertEqual(self.settings.LOOPCUT_OT_blenderkit_get_key.poll(None), not beta)
            names = [entry[1] for entry in layout.calls if entry[0] == 'operator']
            self.assertEqual('loopcut.blenderkit_get_key' in names, not beta)
            texts = [entry[1] for entry in layout.calls if entry[0] == 'label']
            self.assertIn('Loopcut 0.2.13' + (' beta' if beta else ''), texts)
            if beta:
                self.assert_no_purchase(texts)
                self.assertNotIn('Workers and routing run on Loopcut plans', texts)

    def test_vote_tips_are_neutral_without_credits_prices_or_data_classes(self):
        rows = [{'name': 'image_generate', 'title': 'Image generation', 'summary': 'Make an image from a prompt.',
                 'state': 'ready', 'category': 'Images', 'voted': False, 'level': 0.6, 'badge': 'popular'},
                {'name': 'render', 'title': 'Cloud rendering', 'summary': 'Render on our machines.', 'state': 'ready',
                 'category': 'Render', 'voted': True, 'level': 1.0, 'badge': 'most_wanted'}]
        for beta in (True, False):  # None of these run yet: no route, price or data class to explain either way.
            with self.subTest(beta=beta), patch.object(self.config, 'BETA', beta), patch.object(self.config, 'billing_open', return_value=not beta):
                for view in ('chat', 'vote'):
                    display = self.display(coming_soon=rows, view=view, service_signed_in=False)
                    self.assert_no_purchase(display)
                    shown = ' '.join([p['text'] for p in display['prims'] if p['t'] == 'text'] +
                                     [h['tip'] for h in display['hits']]).lower()
                    for word in ('credit', 'price', '$', 'data class', 'private (', 'may train', 'retention'):
                        self.assertNotIn(word, shown)
                tips = {h['id']: h['tip'] for h in display['hits'] if h['action'][0] == 'vote'}
                self.assertEqual(tips, {'vote.image_generate': 'Make an image from a prompt.\nClick to vote',
                                        'vote.render': 'Render on our machines.\nClick to take your vote back'})

    def test_no_credits_or_billing_in_preferences_or_the_panel(self):
        for reason in ('sign_in', 'credits', 'plan'):
            self.assertEqual(self.cloud_account.refusal({'unavailable_reason': reason}, {}), 'Coming soon.')
        import threading
        runtime = types.SimpleNamespace(lock=threading.Lock(), signed_in=True, account_saving=False, account_message='',
                                        manifest={'account': {'credits': {'balance': 1840, 'resets_at': None}}})
        self.assertEqual(self.cloud_account.panel_data(runtime)['credits'], '')
        with patch.object(self.config, 'billing_open', return_value=True):
            self.assertIn('1,840 credits', self.cloud_account.panel_data(runtime)['credits'])
            self.assertEqual(self.cloud_account.refusal({'unavailable_reason': 'sign_in'}, {}), 'Sign in to use credits.')

        def draw(beta):
            texts = []

            def operator(layout, name, text='', **kwargs):
                texts.append(text)
                return types.SimpleNamespace()

            info = {'account_id': '0f8b6a52-2c1d-4e8f-9a3b-5d7e1c2b4a69', 'email': 'tester@example.test', 'plan': 'pro',
                    'session': {'used': .2}, 'week': {'used': .4}}
            layout = Layout()
            with patch.object(self.config, 'BETA', beta), patch.object(self.config, 'billing_open', return_value=not beta), patch.object(Layout, 'operator', operator), \
                    patch.object(self.account, 'signed_in', return_value=True), \
                    patch.dict(self.account._state, account=info, status='idle'):
                self.account.draw(layout)
            return texts + [entry[1] for entry in layout.calls if entry[0] == 'label']
        beta, before = draw(True), draw(False)
        self.assertIn('Account', beta)
        self.assertNotIn('Account and Billing', beta)
        self.assertIn('tester@example.test', beta, 'no plan beside the email in the beta')
        self.assert_no_purchase(beta)
        self.assertFalse([t for t in beta if 'credit' in t.lower() or 'billing' in t.lower() or 'plan' in t.lower()], beta)
        self.assertIn('See plans', before)
        self.assertIn('Credits', before)
        self.assertIn('tester@example.test  ·  Pro plan', before)

    def test_version_display_and_machine_versions_are_separate(self):
        self.assertEqual(self.config.version_label(), '0.2.13 beta')
        self.assertIn('0.2.13 beta', [p.get('text') for p in self.display()['prims']])
        self.assertEqual(self.update.current_version(), (0, 2, 13))
        self.assertTrue(self.cloud_http.contract_headers()['x-loopcut-client'].startswith('loopcut-addon/0.2.13 blender/5.2.0 os/'))
        release = {'version': (0, 2, 14), 'label': '0.2.14', 'asset': None}
        status = {'release': release, 'status': 'available', 'dismissed': False, 'error': '', 'progress': 0}
        self.assertIn('0.2.14 beta', self.update.banner_for(status, False)['text'])
        self.assertIn('0.2.14 beta', self.update.status_line(status, (0, 2, 13)))
        for state in ('idle', 'current'):
            self.assertIn('0.2.13 beta', self.update.status_line({**status, 'status': state, 'release': None}, (0, 2, 13)))
        with patch.object(self.config, 'BETA', False):  # The label alone; sales follow billing_open.
            self.assertEqual(self.config.version_label(), '0.2.13')
            self.assertNotIn('beta', self.update.banner_for(status, False)['text'])

    def test_config_environment_switches(self):
        for value, expected in (('on', True), ('off', False)):
            with patch.dict(os.environ, {'LOOPCUT_BETA': value, 'LOOPCUT_BUG_REPORT_URL': 'https://example.test/bugs'}):
                spec = importlib.util.spec_from_file_location(NAME + '.test_config', PACKAGE / 'config.py')
                module = importlib.util.module_from_spec(spec)
                with patch.dict(sys.modules, {spec.name: module}):
                    spec.loader.exec_module(module)
                self.assertEqual(module.BETA, expected)
                self.assertEqual(module.BUG_REPORT_URL, 'https://example.test/bugs')

    def test_support_registration_callbacks_and_help_fallback(self):
        registered = {}
        with patch.object(self.fake.types.Operator, 'bl_rna_get_subclass_py',
                          side_effect=lambda name: registered.get(name), create=True), \
                patch.object(self.fake.utils, 'register_class', side_effect=lambda cls: registered.update({cls.__name__: cls})), \
                patch.object(self.fake.utils, 'unregister_class', side_effect=lambda cls: registered.pop(cls.__name__)), \
                patch.object(self.fake.types, 'SpaceLoopcut', object, create=True):
            menus = {name: Mock() for name in self.support._MENUS}
            with patch.dict(vars(self.fake.types), menus):
                self.support.register()
                for menu in menus.values():
                    menu.append.assert_called_once_with(self.support._draw_info)
                self.draw_help(expect_loopcut=True)
                splash = Splash()
                self.support._draw_info(types.SimpleNamespace(layout=splash), None)
                self.assertIn(('label', 'Loopcut 0.2.13 beta'), splash.records)
                self.assertEqual(splash.operators()[0][1], 'loopcut.report_bug')
                with patch.object(self.config, 'BUG_REPORT_URL', 'https://example.test/form'):
                    self.support.LOOPCUT_OT_report_bug().execute(None)
                    self.fake.ops.wm.url_open.assert_called_with(url='https://example.test/form')
                self.support.unregister()
                for menu in menus.values():
                    menu.remove.assert_called_once_with(self.support._draw_info)
                self.draw_help(expect_loopcut=False)

    def draw_help(self, *, expect_loopcut):
        source = ROOT / 'scripts/startup/bl_ui/space_topbar.py'
        tree = ast.parse(source.read_text())
        menu = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'TOPBAR_MT_help')
        namespace = {'bpy': self.fake, 'Menu': object}
        exec(compile(ast.Module(body=[menu], type_ignores=[]), str(source), 'exec'), namespace)
        instance = namespace['TOPBAR_MT_help']()
        instance.layout = Splash()
        instance.draw(types.SimpleNamespace(preferences=types.SimpleNamespace(view=types.SimpleNamespace(show_developer_ui=False))))
        operators = instance.layout.operators()
        self.assertEqual(any(row[1] == 'loopcut.report_bug' for row in operators), expect_loopcut)
        bugs = [row for row in operators if row[1] == 'wm.url_open_preset' and getattr(row[3], 'type', '') == 'BUG']
        self.assertEqual(len(bugs), 1)
        self.assertEqual(bugs[0][2], 'Report a Blender Bug' if expect_loopcut else 'Report a Bug')


if __name__ == '__main__':
    unittest.main()
