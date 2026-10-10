"""Anonymous interest crosses only the events boundary; disk and HTTP are isolated."""

import json
import sys
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import test_cloud as fixtures
from loopcut import cloud, credentials, config


class InterestTest(fixtures.RuntimeFixture):
    def setUp(self):
        super().setUp()
        sys.modules['bpy'].app.background = False
        self.config_dir = Path(self.directory.name) / 'config'
        self.folder = patch.object(credentials, 'folder', return_value=self.config_dir)
        self.folder.start()
        self.addCleanup(self.folder.stop)

    def runtime_with_tool(self, *replies, signed_in=False):
        runtime = self.runtime(fixtures.Reply(fixtures.manifest(
            fixtures.tool(status='coming_soon', available=False), account=None)), *replies, signed_in=signed_in)
        self.assertTrue(runtime.refresh())
        return runtime

    def test_id_created_once_and_reused_across_concurrent_callers(self):
        value = credentials.interest_install_id()
        self.assertEqual(uuid.UUID(value).version, 4)
        path = self.config_dir / 'install.json'
        stamp = path.stat().st_mtime_ns
        results = []
        threads = [threading.Thread(target=lambda: results.append(credentials.interest_install_id())) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(results, [value] * 4)
        self.assertEqual(json.loads(path.read_text()), {'install_id': value})
        self.assertEqual(path.stat().st_mtime_ns, stamp)
        self.assertEqual(list(self.config_dir.glob('*.tmp')), [])
        self.assertFalse((self.config_dir / 'credentials.json').exists())

    def test_corrupt_id_is_replaced(self):
        self.config_dir.mkdir()
        path = self.config_dir / 'install.json'
        for content in ('{', '[]', '{}', '{"install_id": 3}', '{"install_id": "invalid"}',
                        '[0,' * 1100, 'x' * 4097,
                        '{"install_id": "12345678-1234-1234-9234-123456789abc"}'):
            with self.subTest(content=content):
                path.write_text(content)
                value = credentials.interest_install_id()
                self.assertEqual(uuid.UUID(value).version, 4)
                self.assertEqual(credentials.interest_install_id(), value)

    def test_unreadable_id_is_replaced(self):
        previous = credentials.interest_install_id()
        with patch.object(Path, 'open', side_effect=PermissionError):
            replacement = credentials.interest_install_id()
        self.assertNotEqual(previous, replacement)
        self.assertEqual(credentials.interest_install_id(), replacement)

    def test_identity_and_credentials_remain_separate(self):
        credentials.store('https://service.test/v1', 'lc_example')
        value = credentials.interest_install_id()
        credentials.set_vendor_key('FAL_KEY', 'vendor-example')
        self.assertEqual(credentials.interest_install_id(), value)
        self.assertEqual(credentials.api_key('https://service.test/v1'), 'lc_example')
        self.assertNotIn(value, (self.config_dir / 'credentials.json').read_text())
        self.assertEqual(json.loads((self.config_dir / 'install.json').read_text()), {'install_id': value})

    def test_background_never_touches_config_or_sends_anonymous_interest(self):
        runtime = self.runtime_with_tool()
        with patch.object(sys.modules['bpy'].app, 'background', True), patch.object(credentials, 'folder') as folder:
            self.assertIsNone(credentials.interest_install_id())
            self.assertFalse(runtime.interest('image_generate'))
            folder.assert_not_called()
        self.assertEqual(runtime.interests['image_generate'], 'retry')
        self.assertFalse(self.config_dir.exists())
        self.assertEqual(len(self.transport.calls), 1)

    def test_signed_out_manifest_and_event_have_no_authorization(self):
        runtime = self.runtime_with_tool(fixtures.Reply(status=204))
        self.assertEqual(runtime.interests, {})
        # Even an accidentally retained key must not accompany a signed-out event.
        runtime.client.api_key = 'lc_unused'
        self.assertTrue(runtime.interest('image_generate'))
        event = self.transport.calls[-1]
        self.assertEqual(runtime.interests, {'image_generate': 'thanks'})
        self.assertEqual(runtime.coming_soon()[0]['state'], 'ready', 'rows show votes, not interest states')
        value = credentials.interest_install_id()
        self.assertEqual(event['body'], {'type': 'interest', 'tool': 'image_generate', 'app_version': config.app_version(),
                                        'source': 'coming_soon_button', 'install_id': value})
        self.assertTrue(all('authorization' not in call['headers'] for call in self.transport.calls))
        self.assertFalse(runtime.interest('image_generate'))
        self.assertFalse(runtime.interest('not_a_tool'))
        self.assertEqual(len(self.transport.calls), 2)
        self.assertNotIn(value, (runtime.cache_dir / 'manifest.json').read_text())
        restarted = cloud.Runtime(runtime.client, runtime.cache_dir, signed_in=False)
        self.assertEqual(restarted.interests, {'image_generate': 'thanks'})
        self.assertFalse(restarted.interest('image_generate'))
        self.assertEqual(len(self.transport.calls), 2)

    def test_signed_in_click_keeps_the_bearer_and_adds_install_id_and_app_version(self):
        # Contract 0.6: the bearer identifies the user; install_id and app_version ride along on both paths.
        runtime = self.runtime_with_tool(fixtures.Reply(status=204), signed_in=True)
        self.assertTrue(runtime.interest('image_generate'))
        self.assertEqual(runtime.interests, {'image_generate': 'thanks'})
        event = self.transport.calls[-1]
        self.assertEqual(event['headers']['authorization'], 'Bearer lc_demo')
        self.assertEqual(event['body'], {'type': 'interest', 'tool': 'image_generate', 'source': 'coming_soon_button',
                                         'app_version': config.app_version(),
                                         'install_id': credentials.interest_install_id()})

    def test_a_contract_05_server_gets_one_resend_without_the_06_fields(self):
        for status in (400, 422):
            for signed_in in (False, True):
                with self.subTest(status=status, signed_in=signed_in):
                    runtime = self.runtime_with_tool(fixtures.Reply(status=status), fixtures.Reply(status=204),
                                                     signed_in=signed_in)
                    self.assertTrue(runtime.interest('image_generate'))
                    first, second = self.transport.calls[-2:]
                    self.assertIn('app_version', first['body'])
                    expected = {'type': 'interest', 'tool': 'image_generate', 'source': 'coming_soon_button'}
                    if not signed_in:
                        expected['install_id'] = first['body']['install_id']
                    self.assertEqual(second['body'], expected)
                    self.assertEqual(runtime.interests['image_generate'], 'thanks')
                    (runtime.cache_dir / 'manifest.json').unlink()
        runtime = self.runtime_with_tool(fixtures.Reply(status=400), fixtures.Reply(status=400))
        self.assertFalse(runtime.interest('image_generate'))  # Both shapes refused: the quiet line, no loop.
        self.assertEqual(runtime.interests['image_generate'], 'notify')
        self.assertEqual(len([c for c in self.transport.calls if c['url'].endswith('/v1/events')]), 2)

    def test_old_servers_leave_actionable_state_without_automatic_retry(self):
        for status in (401, 404, 429):
            with self.subTest(status=status):
                runtime = self.runtime_with_tool(fixtures.Reply(status=status), fixtures.Reply(status=204))
                with patch.object(runtime, 'refresh_async') as refresh, patch.object(runtime, 'handle_error') as error:
                    self.assertFalse(runtime.interest('image_generate'))
                    refresh.assert_not_called()
                    error.assert_not_called()
                self.assertEqual(runtime.interests['image_generate'], 'notify', 'a later click may send again')
                self.assertEqual(runtime.message, '')
                self.assertEqual(len(self.transport.calls), 2)
                self.assertTrue(runtime.interest('image_generate'))  # An explicit second click only.
                self.assertEqual(self.transport.calls[-1]['body'], self.transport.calls[-2]['body'])
                # A successful cache from this case must not suppress the next case's click.
                (runtime.cache_dir / 'manifest.json').unlink()

    def test_in_flight_click_is_not_sent_twice(self):
        runtime = self.runtime_with_tool(fixtures.Reply(status=204))
        request = runtime.client.request

        def during_request(*args, **kwargs):
            self.assertEqual(runtime.interests['image_generate'], 'sending')
            self.assertFalse(runtime.interest('image_generate'))
            return request(*args, **kwargs)

        with patch.object(runtime.client, 'request', side_effect=during_request):
            self.assertTrue(runtime.interest('image_generate'))
        self.assertEqual(len(self.transport.calls), 2)

    def test_unwritable_config_leaves_retry_without_request(self):
        runtime = self.runtime_with_tool()
        with patch.object(credentials, '_write', side_effect=PermissionError):
            self.assertFalse(runtime.interest('image_generate'))
        self.assertEqual(runtime.interests['image_generate'], 'retry')
        self.assertEqual(len(self.transport.calls), 1)

    def test_signed_out_panel_refresh_is_throttled(self):
        runtime = self.runtime_with_tool()
        runtime.refreshed_at = 0
        with patch.object(cloud, '_snapshot', return_value=runtime), patch.object(runtime, 'refresh_async') as refresh:
            cloud.panel_seen()
            cloud.panel_seen()
            refresh.assert_called_once()
        self.assertEqual(cloud.coming_soon()[0]['name'], 'image_generate')


if __name__ == '__main__':
    unittest.main()
