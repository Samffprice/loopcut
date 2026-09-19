"""Where things are, and `import loopcut` meaning this checkout in every harness script.

The repository is the Blender fork; the add-on is one of its core add-ons. Build folders, the
stock Blender used for fast iteration, harness output and .env sit next to the repository, the
way Blender's own build folders do, so none of them can end up in a commit.

In the Loopcut build a bundled copy of the add-on is already enabled at startup, so that one is
switched off first. The package is loaded by path rather than by putting scripts/addons_core on
sys.path, which would also put this checkout's copies of Blender's core add-ons in front of the
running Blender's own.
"""

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PACKAGE = REPO / "scripts" / "addons_core" / "loopcut"
WORKSPACE = REPO.parent
OUT = WORKSPACE / "out"


def use() -> None:
    loaded = [name for name in sys.modules if name == "loopcut" or name.startswith("loopcut.")]
    if loaded:
        import addon_utils
        addon_utils.disable("loopcut")
        for name in loaded:
            sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(
        "loopcut", PACKAGE / "__init__.py", submodule_search_locations=[str(PACKAGE)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["loopcut"] = module
    spec.loader.exec_module(module)
