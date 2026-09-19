"""`import loopcut` must mean this checkout in every harness script. In the Loopcut build of
Blender a bundled copy is already enabled at startup, so that one is switched off first."""

import sys
from pathlib import Path

EXTENSION = Path(__file__).resolve().parent.parent / "extension"


def use() -> None:
    loaded = [name for name in sys.modules if name == "loopcut" or name.startswith("loopcut.")]
    if loaded:
        import addon_utils
        addon_utils.disable("loopcut")
        for name in loaded:
            sys.modules.pop(name, None)
    sys.path.insert(0, str(EXTENSION))
