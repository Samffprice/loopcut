"""Smoke-lane CLI behavior with real loopback HTTP and an isolated fake Blender process."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HARNESS = Path(__file__).resolve().parents[1] / 'harness'
sys.path.insert(0, str(HARNESS))
from beta_servers_check import platform_mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import platform_contract


class BetaServersCheckTest(unittest.TestCase):
    def run_check(self, *args, env=None):
        return subprocess.run([sys.executable, '-B', str(HARNESS / 'beta_servers_check.py'), *args],
                              env=env, capture_output=True, text=True, timeout=45)

    def test_production_html_404_lifecycle_panel_and_turns(self):
        result = self.run_check('--prod-stub')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('PASS NO production-stub request carried x-loopcut-prompt', result.stdout)
        self.assertIn('PASS pro completed a hosted turn', result.stdout)

    def test_existing_server_mode_uses_key_env_and_contract_routes(self):
        mock = platform_contract.require('mock', 'server.py')
        with tempfile.TemporaryDirectory() as folder, platform_mock(mock, Path(folder)) as url:
            result = self.run_check('--base-url', url + '/v1', '--key-env', 'BETA_SMOKE_TEST_KEY',
                                    env={**os.environ, 'BETA_SMOKE_TEST_KEY': 'lc_demo'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('PASS pro selected orchestrator', result.stdout)
        self.assertIn('PASS signed out: the vote click sent the right request for this server', result.stdout)
        self.assertIn('PASS signed in: the vote click sent the right request for this server', result.stdout)
        self.assertIn('PASS worker request carries its hosted prompt id', result.stdout)

    def test_missing_key_is_a_nonzero_failure_before_network(self):
        env = {k: v for k, v in os.environ.items() if k != 'BETA_SMOKE_TEST_KEY'}
        result = self.run_check('--base-url', 'http://127.0.0.1:1', '--key-env', 'BETA_SMOKE_TEST_KEY', env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--base-url requires --key-env', result.stderr)


if __name__ == '__main__':
    unittest.main()
