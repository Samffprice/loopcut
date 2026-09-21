"""Exercise the Poly Pizza tools against the live API inside Blender. Needs a key:
  LOOPCUT_POLYPIZZA_KEY=... Blender -b --factory-startup --python harness/polypizza_check.py
Without one it only checks the no-key path and exits 0 with SKIPPED."""
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
import checkout  # noqa: E402
checkout.use()
os.environ.setdefault("LOOPCUT_DATA_DIR", tempfile.mkdtemp(prefix="loopcut-harness-"))
os.environ.setdefault("LOOPCUT_CONFIG_DIR", tempfile.mkdtemp(prefix="loopcut-harness-config-"))

from loopcut import polypizza, tools  # noqa: E402


def check():
    code = 1
    try:
        if not polypizza.api_key():
            missing = tools.execute("search_polypizza", json.dumps({"query": "tree"}))
            assert not missing.ok and "poly.pizza" in missing.text, missing.text
            print("polypizza_check: SKIPPED (set LOOPCUT_POLYPIZZA_KEY to run the live part)")
            code = 0
            return
        found = tools.execute("search_polypizza", json.dumps({"query": "tree", "licence": "CC0", "limit": 4}))
        assert found.ok and "1. " in found.text and found.image_path, found.text
        print(found.text)
        model_id = found.text.splitlines()[1].split(" ", 1)[1].split(":")[0]
        got = tools.execute("import_polypizza", json.dumps({"model_id": model_id, "target_size": 2.0}))
        assert got.ok and "Scaled by" in got.text and "CC0" in got.text, got.text
        roots = [o for o in bpy.data.objects if o.get("polypizza_id") == model_id]
        assert roots, "roots tagged"
        print(got.text[:300])
        again = tools.execute("import_polypizza", json.dumps({"model_id": model_id}))
        assert again.ok, again.text
        bad = tools.execute("import_polypizza", json.dumps({"model_id": "no_such_model_xyz"}))
        assert not bad.ok and "no model" in bad.text, bad.text
        print("polypizza_check: OK")
        code = 0
    except Exception:
        traceback.print_exc()
    finally:
        sys.exit(code)


check()
