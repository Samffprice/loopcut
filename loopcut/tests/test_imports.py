"""The modules the eval harness imports outside Blender (harness/evals/run.py reaches orchestrator.py
through config and pricing) must load without bpy: a module-level `from .tools import ...` broke
run.py once (glb.py), while every unit test passed because the suite stubs bpy."""
import subprocess
import sys
import unittest
from pathlib import Path

ADDONS = Path(__file__).resolve().parents[2] / "scripts" / "addons_core"


class ImportsWithoutBpy(unittest.TestCase):
    def test_orchestrator_and_friends_import_in_a_plain_python(self):
        code = ("import sys; sys.path.insert(0, %r)\n"
                "import loopcut.orchestrator, loopcut.config, loopcut.pricing, loopcut.glb\n"
                "assert 'bpy' not in sys.modules" % str(ADDONS))
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
