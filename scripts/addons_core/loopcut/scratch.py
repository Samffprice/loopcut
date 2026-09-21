"""Private transient images for this Blender process, never shared with another eval or window."""

import tempfile
from pathlib import Path

_directory = tempfile.TemporaryDirectory(prefix="loopcut-images-")


def folder() -> Path:
    return Path(_directory.name)
