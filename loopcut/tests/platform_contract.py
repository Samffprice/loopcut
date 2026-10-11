"""Where tests find the platform contract: its mock server, schemas and fixtures.

The contract belongs to the private platform repository, so the open repository and its CI do
not have it, and nothing from it is copied here. LOOPCUT_CONTRACT_DIR names its contract/ folder
(a missing or empty value hides it). Otherwise a loopcut-web-platform checkout beside this
repository, or beside any folder above it, is used. A test that needs it calls require(), which
skips the test with REASON when it is not there.
"""

import os
import unittest
from pathlib import Path

ENV = "LOOPCUT_CONTRACT_DIR"
REASON = "needs the private platform contract; set LOOPCUT_CONTRACT_DIR to its contract folder"


def contract_dir():
    """The contract folder, or None when this checkout cannot see it."""
    if ENV in os.environ:
        path = Path(os.environ[ENV]) if os.environ[ENV] else None
    else:
        repo = Path(__file__).resolve().parents[2]
        path = next((folder / "loopcut-web-platform" / "contract" for folder in repo.parents
                     if (folder / "loopcut-web-platform" / "contract").is_dir()), None)
    return path if path is not None and path.is_dir() else None


def require(*parts):
    """The contract folder joined with parts. Skips the calling test when it is missing."""
    root = contract_dir()
    path = root.joinpath(*parts) if root else None
    if path is None or not path.exists():
        raise unittest.SkipTest(REASON + (f" ({'/'.join(parts)})" if parts else ""))
    return path
