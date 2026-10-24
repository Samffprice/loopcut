"""The worker copy must be byte-identical and import without Blender or the add-on."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'scripts/addons_core/loopcut/rig_plan.py'
SCRIPT = ROOT / 'loopcut/scripts/sync_rig_plan.py'


class VendorSync(unittest.TestCase):
    def test_sync_check_drift_and_standalone_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'workers/autorig'
            target.mkdir(parents=True)
            command = [sys.executable, str(SCRIPT), tmp]
            self.assertNotEqual(subprocess.run(command + ['--check'], capture_output=True).returncode, 0)
            subprocess.run(command, check=True, capture_output=True)
            self.assertEqual((target / SOURCE.name).read_bytes(), SOURCE.read_bytes())
            subprocess.run(command + ['--check'], check=True, capture_output=True)
            code = ('import rig_plan, sys; '
                    'assert "bpy" not in sys.modules and "numpy" not in sys.modules and "loopcut" not in sys.modules; '
                    'assert rig_plan.plan_json({"schema_version":1,"joints":[],"vertices":[]})'
                    '["anatomy_reason"] == "too_few_joints"')
            subprocess.run([sys.executable, '-S', '-c', code], cwd=target, check=True, capture_output=True)
            (target / SOURCE.name).write_text('# stale worker policy\n')
            self.assertNotEqual(subprocess.run(command + ['--check'], capture_output=True).returncode, 0)
            self.assertEqual((target / SOURCE.name).read_text(), '# stale worker policy\n')

    def test_missing_checkout_and_symlink_are_not_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = [sys.executable, str(SCRIPT), tmp]
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            directory = Path(tmp) / 'workers/autorig'
            directory.mkdir(parents=True)
            protected = Path(tmp) / 'protected.py'
            protected.write_text('preserved')
            (directory / SOURCE.name).symlink_to(protected)
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertEqual(protected.read_text(), 'preserved')

    @unittest.skipUnless(os.environ.get('LOOPCUT_AUTORIG_WORKER_DIR'), 'Optional real worker vendor comparison')
    def test_real_worker_vendor_matches(self):
        target = Path(os.environ['LOOPCUT_AUTORIG_WORKER_DIR']) / 'workers/autorig/rig_plan.py'
        self.assertEqual(target.read_bytes(), SOURCE.read_bytes())


if __name__ == '__main__':
    unittest.main()
