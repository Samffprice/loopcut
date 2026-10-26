"""Where tests find the platform contract: its mock server, schemas and fixtures.

The contract belongs to the private platform repository, so the open repository and its CI do
not have it, and nothing from it is copied here. LOOPCUT_CONTRACT_DIR names its contract/ folder
(a missing or empty value hides it). Otherwise a loopcut-web-platform checkout beside this
repository, or beside any folder above it, is used. A test that needs it calls require(), which
skips the test with REASON when it is not there. An explicitly configured path,
missing fixture in an available checkout, or LOOPCUT_REQUIRE_CONTRACT=1 instead
fails: private CI must not quietly turn a missing contract into a passing skip.
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
    """Find a fixture; only unconfigured public checkouts may skip this test."""
    root = contract_dir()
    path = root.joinpath(*parts) if root else None
    if path is None or not path.exists():
        if root or os.environ.get(ENV) or os.environ.get('LOOPCUT_REQUIRE_CONTRACT') == '1':
            raise FileNotFoundError(REASON + (f" ({'/'.join(parts)})" if parts else ""))
        raise unittest.SkipTest(REASON + (f" ({'/'.join(parts)})" if parts else ""))
    return path


def live_tool(name, **changes):
    """A tool's descriptor as the contract records it (fixtures/tool-descriptor), live and available as the mock
    serves a --live-tools tool to a funded account: its cost is the recorded catalog's."""
    import json
    descriptor = json.loads(require("fixtures", "tool-descriptor", f"{name}.json").read_text())
    descriptor.update(status="live", available=True, unavailable_reason=None, **changes)
    return descriptor
