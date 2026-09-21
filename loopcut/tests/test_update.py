"""The pure half of the updater: versions, the manifest, where an install lives, the helper and
what the banner says. No Blender."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

from loopcut import update  # noqa: E402

SHA = "a" * 64


def manifest(**overrides):
    payload = {"version": "0.1.4", "tag": "v0.1.4", "notes_url": "https://github.com/Samffprice/loopcut/releases/tag/v0.1.4",
               "assets": {"darwin-arm64": {"name": "loopcut-v0.1.4-arm64.dmg", "size": 300_000_000, "sha256": SHA,
                                           "url": "https://github.com/Samffprice/loopcut/releases/download/v0.1.4/loopcut-v0.1.4-arm64.dmg"},
                          "windows-x64": {"name": "loopcut-v0.1.4-windows64.msi", "size": 320_000_000, "sha256": SHA,
                                          "url": "https://github.com/Samffprice/loopcut/releases/download/v0.1.4/loopcut-v0.1.4-windows64.msi"}}}
    payload.update(overrides)
    return payload


def state(**overrides):
    release = {"version": (0, 1, 4), "label": "0.1.4", "notes_url": "https://example.com/notes",
               "asset": manifest()["assets"]["darwin-arm64"]}
    base = {"status": "available", "release": release, "progress": 0.0, "path": "", "error": "", "dismissed": False,
            "checked_at": 0.0}
    base.update(overrides)
    return base


class VersionTest(unittest.TestCase):
    def test_parses_with_or_without_the_v(self):
        self.assertEqual(update.parse_version("0.1.4"), (0, 1, 4))
        self.assertEqual(update.parse_version("v10.2.0"), (10, 2, 0))
        self.assertEqual(update.label((0, 1, 4)), "0.1.4")

    def test_rejects_anything_else(self):
        for bad in ("0.1", "0.1.4-beta", "", None, "1.2.3.4", "v"):
            with self.assertRaises(update.UpdateError):
                update.parse_version(bad)

    def test_platform_keys(self):
        self.assertEqual(update.platform_key("darwin", "arm64"), "darwin-arm64")
        self.assertEqual(update.platform_key("darwin", "x86_64"), "darwin-x86_64")
        self.assertEqual(update.platform_key("win32", "AMD64"), "windows-x64")
        self.assertEqual(update.platform_key("linux", "x86_64"), "linux-x86_64")


class ManifestTest(unittest.TestCase):
    def test_the_installer_for_this_platform(self):
        release = update.parse_manifest(manifest(), "windows-x64")
        self.assertEqual(release["version"], (0, 1, 4))
        self.assertEqual(release["label"], "0.1.4")
        self.assertEqual(release["asset"]["name"], "loopcut-v0.1.4-windows64.msi")
        self.assertEqual(release["asset"]["size"], 320_000_000)
        self.assertEqual(release["asset"]["sha256"], SHA)

    def test_no_installer_for_this_platform_is_none(self):
        self.assertIsNone(update.parse_manifest(manifest(), "darwin-x86_64")["asset"])
        self.assertIsNone(update.parse_manifest(manifest(), "linux-x86_64")["asset"])

    def test_bad_manifests_are_refused(self):
        good = manifest()["assets"]["darwin-arm64"]
        cases = [
            ("not an object", [], "darwin-arm64"),
            ("bad version", manifest(version="0.1"), "darwin-arm64"),
            ("http notes", manifest(notes_url="http://example.com"), "darwin-arm64"),
            ("assets not an object", manifest(assets=[]), "darwin-arm64"),
            ("http installer", manifest(assets={"darwin-arm64": {**good, "url": "http://x/y.dmg"}}), "darwin-arm64"),
            ("wrong suffix", manifest(assets={"darwin-arm64": {**good, "name": "loopcut.zip"}}), "darwin-arm64"),
            ("path in name", manifest(assets={"darwin-arm64": {**good, "name": "../x.dmg"}}), "darwin-arm64"),
            ("short sha", manifest(assets={"darwin-arm64": {**good, "sha256": "abc"}}), "darwin-arm64"),
            ("size as bool", manifest(assets={"darwin-arm64": {**good, "size": True}}), "darwin-arm64"),
            ("size too big", manifest(assets={"darwin-arm64": {**good, "size": 10**12}}), "darwin-arm64"),
            ("asset not an object", manifest(assets={"darwin-arm64": "x"}), "darwin-arm64"),
        ]
        for name, payload, platform in cases:
            with self.subTest(name), self.assertRaises(update.UpdateError):
                update.parse_manifest(payload, platform)


class InstallTargetTest(unittest.TestCase):
    def test_mac_app_in_applications(self):
        self.assertEqual(update.install_target("/Applications/Loopcut.app/Contents/MacOS/Loopcut", "darwin", "/Users/me"),
                         "/Applications/Loopcut.app")
        self.assertEqual(update.install_target("/Users/me/Applications/Loopcut.app/Contents/MacOS/Loopcut", "darwin",
                                               "/Users/me"), "/Users/me/Applications/Loopcut.app")

    def test_mac_elsewhere_is_not_updated_in_place(self):
        for path in ("/Volumes/Loopcut/Loopcut.app/Contents/MacOS/Loopcut",  # Run from the disk image.
                     "/Users/me/loopcut/build/release/bin/Loopcut.app/Contents/MacOS/Loopcut",
                     "/Applications/Tools/Loopcut.app/Contents/MacOS/Loopcut",
                     "/usr/local/bin/blender"):
            self.assertEqual(update.install_target(path, "darwin", "/Users/me"), "", path)

    def test_windows_under_program_files(self):
        exe = "C:/Program Files/Loopcut/Loopcut 5.2/blender.exe"
        self.assertEqual(update.install_target(exe, "win32", program_files="C:/Program Files"), exe)
        self.assertEqual(update.install_target("C:/Users/me/Desktop/loopcut/blender.exe", "win32",
                                               program_files="C:/Program Files"), "")
        self.assertEqual(update.install_target(exe, "win32", program_files=""), "")

    def test_other_platforms_never(self):
        self.assertEqual(update.install_target("/opt/loopcut/blender", "linux"), "")


class HelperTest(unittest.TestCase):
    def test_scripts_take_their_paths_as_arguments(self):
        name, script = update.helper_script("darwin")
        self.assertEqual(name, "apply.sh")
        self.assertTrue(script.startswith("#!/bin/sh"))
        self.assertIn("hdiutil attach", script)
        self.assertIn("ditto", script)
        name, script = update.helper_script("win32")
        self.assertEqual(name, "apply.cmd")
        self.assertIn("msiexec /i", script)
        with self.assertRaises(update.UpdateError):
            update.helper_script("linux")

    def test_commands(self):
        self.assertEqual(update.helper_command("/t/apply.sh", 42, "/t/x.dmg", "/Applications/Loopcut.app", "/t/log", "darwin"),
                         ["/bin/sh", "/t/apply.sh", "42", "/t/x.dmg", "/Applications/Loopcut.app", "/t/log"])
        self.assertEqual(update.helper_command("C:/t/apply.cmd", 42, "C:/t/x.msi", "C:/P/blender.exe", "C:/t/log", "win32"),
                         ["cmd.exe", "/c", "C:/t/apply.cmd", "42", "C:/t/x.msi", "C:/P/blender.exe", "C:/t/log"])


class BannerTest(unittest.TestCase):
    def test_nothing_to_say(self):
        self.assertIsNone(update.banner_for(state(status="idle", release=None), True))
        self.assertIsNone(update.banner_for(state(status="current", release=None), True))
        self.assertIsNone(update.banner_for(state(status="checking"), True))
        self.assertIsNone(update.banner_for(state(dismissed=True), True))

    def test_available_offers_update_or_the_page(self):
        self.assertEqual(update.banner_for(state(), True),
                         {"text": "Loopcut 0.1.4 is available.", "button": ("Update", "update_download"), "dismiss": True,
                          "progress": None})
        self.assertEqual(update.banner_for(state(), False)["button"], ("Download", "update_page"))
        no_asset = state()
        no_asset["release"] = {**no_asset["release"], "asset": None}
        self.assertEqual(update.banner_for(no_asset, True)["button"], ("Download", "update_page"))

    def test_progress_ready_installing_error(self):
        self.assertEqual(update.banner_for(state(status="downloading", progress=0.437), True),
                         {"text": "Downloading Loopcut 0.1.4… 43%", "button": None, "dismiss": False, "progress": 0.437})
        self.assertEqual(update.banner_for(state(status="ready", path="/t/x.dmg"), True),
                         {"text": "Loopcut 0.1.4 is ready.", "button": ("Restart to update", "update_restart"),
                          "dismiss": True, "progress": None})
        self.assertEqual(update.banner_for(state(status="installing"), True)["text"],
                         "Loopcut 0.1.4 installs when Blender closes.")
        failed = update.banner_for(state(status="error", error="HTTP 404 downloading x"), True)
        self.assertEqual(failed["text"], "Update failed: HTTP 404 downloading x")
        self.assertEqual(failed["button"], ("Try again", "update_download"))
        self.assertEqual(update.banner_for(state(status="error", error="x"), False)["button"], ("Download", "update_page"))

    def test_status_line(self):
        self.assertEqual(update.status_line(state(status="idle", release=None), (0, 1, 3)), "Loopcut 0.1.3")
        self.assertEqual(update.status_line(state(status="idle", release=None, error="HTTP 500"), (0, 1, 3)),
                         "Could not check for updates: HTTP 500")
        self.assertEqual(update.status_line(state(status="current", release=None), (0, 1, 3)), "Loopcut 0.1.3 is up to date.")
        self.assertEqual(update.status_line(state(status="ready"), (0, 1, 3)), "Loopcut 0.1.4 is downloaded. Restart to update.")
        self.assertEqual(update.status_line(state(status="downloading", progress=0.5), (0, 1, 3)), "Downloading Loopcut 0.1.4… 50%")


if __name__ == "__main__":
    unittest.main()
