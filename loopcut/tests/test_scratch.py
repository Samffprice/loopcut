"""Concurrent Blender processes must never reuse an image path."""

import os
import subprocess
import sys
import unittest
from pathlib import Path


class ScratchTest(unittest.TestCase):
    def test_each_process_owns_and_cleans_its_image_directory(self):
        package = Path(__file__).resolve().parents[2] / "scripts" / "addons_core"
        command = [sys.executable, "-c", "from loopcut.scratch import folder; print(folder())"]
        env = {**os.environ, "PYTHONPATH": str(package)}
        paths = [Path(subprocess.check_output(command, env=env, text=True).strip()) for _ in range(2)]
        self.assertNotEqual(*paths)
        self.assertTrue(all(not path.exists() for path in paths), "clean exit removes scratch images")


if __name__ == "__main__":
    unittest.main()
