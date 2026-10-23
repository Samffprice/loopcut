"""Pending sign-in secrets use the existing atomic, private credentials store; no Blender or network."""
import importlib.util
import json
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[2] / "scripts/addons_core/loopcut/credentials.py"
spec = importlib.util.spec_from_file_location("_device_credentials", SOURCE)
credentials = importlib.util.module_from_spec(spec)
spec.loader.exec_module(credentials)


class PendingDeviceTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="device code ü ")
        self.addCleanup(folder.cleanup)
        self.target = Path(folder.name)
        override = patch.dict(os.environ, {"LOOPCUT_CONFIG_DIR": folder.name})
        override.start()
        self.addCleanup(override.stop)
        self.service = "https://loopcut.test"
        self.record = {"device_code": "fixture-secret", "user_code": "TEST-CODE", "expires_at": "future"}

    def test_pending_code_survives_reload_without_changing_existing_keys(self):
        credentials.store(self.service + "/v1", "existing-key")
        credentials.set_vendor_key("FAL_KEY", "existing-vendor")
        credentials.store_pending_device(self.service, self.record)
        self.assertEqual(credentials.pending_device(self.service), {**self.record, "service_url": self.service})
        self.assertEqual(credentials.api_key(self.service + "/v1"), "existing-key")
        self.assertEqual(credentials.vendor_key("FAL_KEY"), "existing-vendor")
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE((self.target / credentials.FILE_NAME).stat().st_mode), 0o600)
        credentials.store_pending_device(self.service, None)
        self.assertIsNone(credentials.pending_device(self.service))
        self.assertEqual(credentials.api_key(self.service + "/v1"), "existing-key")

    def test_code_is_scoped_to_its_service(self):
        credentials.store_pending_device(self.service, self.record)
        self.assertIsNone(credentials.pending_device("https://other.test"))
        credentials.store_pending_device("https://other.test", None)
        self.assertIsNotNone(credentials.pending_device(self.service))

    def test_cancelled_start_cannot_save_a_late_code(self):
        cancelled = threading.Event()
        cancelled.set()
        self.assertFalse(credentials.store_pending_device(self.service, self.record, cancelled=cancelled))
        self.assertIsNone(credentials.pending_device(self.service))

    def test_damaged_credentials_are_never_overwritten_to_save_a_pending_code(self):
        path = self.target / credentials.FILE_NAME
        path.write_text('{"api_keys": "damaged"}', encoding="utf-8")
        with self.assertRaises(OSError):
            credentials.store_pending_device(self.service, self.record)
        self.assertEqual(json.loads(path.read_text()), {"api_keys": "damaged"})


if __name__ == "__main__":
    unittest.main()
