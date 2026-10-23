"""The pure half of the updater: versions, the manifest, where an install lives, the helper and
what the banner says. No Blender."""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path, PureWindowsPath
from unittest.mock import Mock, patch
from contextlib import contextmanager

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from loopcut import update  # noqa: E402
import windows_update_smoke as windows_smoke  # noqa: E402

SHA = "a" * 64


def windows_args(command: str) -> list[str]:
    """MS C-runtime argv rules for arguments (not argv[0]), also used by Python on Windows.

    https://learn.microsoft.com/en-us/cpp/c-language/parsing-c-command-line-arguments
    Backslashes escape quotes for a native process, NOT for cmd.exe. No shell expansion is simulated.
    """
    args, index = [], 0
    while index < len(command):
        if command[index] in " \t":
            index += 1
            continue
        value, quoted = "", False
        while index < len(command) and (quoted or command[index] not in " \t"):
            slashes = 0
            while index < len(command) and command[index] == "\\":
                slashes += 1
                index += 1
            if index < len(command) and command[index] == '"':
                value += "\\" * (slashes // 2)
                if slashes % 2:
                    value += '"'
                elif quoted and index + 1 < len(command) and command[index + 1] == '"':
                    value += '"'
                    index += 1
                else:
                    quoted = not quoted
            else:
                value += "\\" * slashes
                if index == len(command) or (not quoted and command[index] in " \t"):
                    break
                value += command[index]
            index += 1
        args.append(value)
    return args


def helper_job(folder):
    return {"pid": 2_000_000_000, "installer": str(folder / "Loopcut installer.msi"),
            "target": str(folder / "Loopcut.app"), "log": str(folder / "installer log.txt"),
            "restart": str(folder / "restart"), "result": str(folder / "result.json"),
            "version": "0.1.4", "from_version": "0.1.3", "sha256": hashlib.sha256(b"installer").hexdigest(),
            "size": 9, "ack": str(folder / "waiting")}


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
            "checked_at": 0.0, "armed": False, "critical": False, "pickup": False, "legacy_fallback": False}
    base.update(overrides)
    return base


@contextmanager
def windows_platform():
    with patch.object(update.sys, "platform", "win32"), \
            patch.object(update.subprocess, "DETACHED_PROCESS", 8, create=True), \
            patch.object(update.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, create=True), \
            patch.object(update.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True):
        yield


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
    def test_critical_metadata_is_optional_and_validated(self):
        normal = update.parse_manifest(manifest(), "darwin-arm64")
        self.assertIsNone(normal["min_version"])
        self.assertEqual(normal["headline"], "")
        critical = update.parse_manifest(manifest(min_version="0.1.3", headline="Fixes the helper crash"), "darwin-arm64")
        self.assertEqual(critical["min_version"], (0, 1, 3))
        for fields in ({"min_version": "0.1.5"}, {"min_version": None}, {"headline": []},
                       {"headline": "x" * 121}, {"headline": "line\nbreak"},
                       {"notes_url": "http://127.0.0.1.evil.example/update"}):
            with self.subTest(fields), self.assertRaises(update.UpdateError):
                update.parse_manifest(manifest(**fields), "darwin-arm64")

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
        self.assertEqual(update.install_target(exe, "win32", program_files="C:/Program Files"), str(PureWindowsPath(exe)))
        self.assertEqual(update.install_target("C:/Users/me/Desktop/loopcut/blender.exe", "win32",
                                               program_files="C:/Program Files"), "")
        with patch.dict(os.environ, {"ProgramFiles": ""}):
            self.assertEqual(update.install_target(exe, "win32", program_files=""), "")

    def test_windows_backslashes_case_and_launcher_on_any_host(self):
        exe = r"c:\PROGRAM FILES\Loopcut\Loopcut 5.2\blender.exe"
        with patch.object(update.Path, "is_file", return_value=True):
            self.assertEqual(update.install_target(exe, "win32", program_files=r"C:\Program Files"),
                             r"c:\PROGRAM FILES\Loopcut\Loopcut 5.2\blender-launcher.exe")
        for outside in (r"C:\Program Files\Loopcut-other\blender.exe", r"C:\Program Files2\Loopcut\blender.exe",
                        r"D:\Program Files\Loopcut\blender.exe"):
            self.assertEqual(update.install_target(outside, "win32", program_files=r"C:\Program Files"), "")

    def test_other_platforms_never(self):
        self.assertEqual(update.install_target("/opt/loopcut/blender", "linux"), "")


class HelperTest(unittest.TestCase):
    def test_windows_environment_rebuilds_module_paths_and_preserves_other_values(self):
        parent = {"PSModulePath": "pwsh7-modules", "PsModulePath": "also-inherited",
                  "Path": "native-executable-search-path", "USERPROFILE": "Sam's 50% & Zoë",
                  "UPDATE_TEST_OUTCOME": "3010"}
        child = update.helper_environment(parent)
        self.assertEqual(child, {key: value for key, value in parent.items() if key not in ("PSModulePath", "PsModulePath")})
        self.assertEqual(parent["PSModulePath"], "pwsh7-modules")

    def test_scripts_take_their_paths_as_arguments(self):
        name, script = update.helper_script("darwin")
        self.assertEqual(name, "apply.sh")
        self.assertTrue(script.startswith("#!/bin/sh"))
        self.assertIn("hdiutil attach", script)
        self.assertIn("ditto", script)
        name, script = update.helper_script("win32")
        self.assertEqual(name, "apply.ps1")
        self.assertIn("-Verb RunAs -Wait -PassThru", script)
        with self.assertRaises(update.UpdateError):
            update.helper_script("linux")

    def test_commands_preserve_paths_as_data(self):
        odd = "Sam's 50% & Zoë"
        job = helper_job(Path("/t") / odd)
        command = update.helper_command("/t/apply.sh", job, "/t/job.json", "darwin")
        self.assertEqual(command[0:2], ["/bin/sh", "/t/apply.sh"])
        self.assertEqual(command[3], job["installer"])
        self.assertEqual(command[4], job["target"])
        script = f"C:/Users/{odd}/apply.ps1"
        record = f"C:/Users/{odd}/job.json"
        command = update.helper_command(script, job, record, "win32")
        self.assertNotIn("cmd.exe", command)
        self.assertNotIn("-EncodedCommand", command)
        self.assertEqual(command[-4:], ["-File", script, "-JobFile", record])
        self.assertEqual(windows_args(subprocess.list2cmdline(command)), command)
        # Include Windows backslashes and literal shell-looking text; no shell should see these.
        for directory in (r"C:\Users\Sam's 50% & Zoë", r"C:\space dir\%TEMP%\$env:HOME", "C:\\trailing space\\"):
            command = update.helper_command(directory + r"\apply.ps1", job, directory + r"\job.json", "win32")
            self.assertEqual(windows_args(subprocess.list2cmdline(command)), command)

    def test_legacy_command_is_the_unchanged_029_launch_form(self):
        self.assertEqual(update.legacy_helper_command("C:/temp/apply.cmd", 42, "C:/temp/x.msi", "C:/P/blender.exe", "C:/temp/log"),
                         ["cmd.exe", "/c", "C:/temp/apply.cmd", "42", "C:/temp/x.msi", "C:/P/blender.exe", "C:/temp/log"])
        self.assertIn('msiexec /i "%msi%" /passive /norestart /l*v "%log%"', update.HELPER_CMD)

    def test_no_helper_asks_powershell_for_a_timeout_above_its_maximum(self):
        # Wait-Process -Timeout takes at most 32767; 0.2.4-0.2.10's legacy helper passed 86400, binding failed
        # and nothing waited for Blender to close (found on a real Windows runner).
        for text in (update.HELPER_PS, update.HELPER_CMD):
            for value in re.findall(r"-Timeout(?:Sec)?\s+(\d+)", text):
                self.assertLessEqual(int(value), 32767, text)
        self.assertNotIn("Wait-Process", update.HELPER_CMD)
        self.assertIn("Get-Process -Id %pid%", update.HELPER_CMD)



@patch.object(update.config, "BETA", False)
class BannerTest(unittest.TestCase):
    def test_legacy_fallback_keeps_the_029_restart_banner(self):
        fallback = state(status="ready", legacy_fallback=True)
        banner = update.banner_for(fallback, True)
        self.assertEqual(banner["text"], "Loopcut 0.1.4 is ready.")
        self.assertEqual(banner["button"], ("Restart to update", "update_restart"))
        self.assertEqual(update.status_line(fallback, (0, 1, 3)), "Loopcut 0.1.4 is downloaded. Restart to update.")

    def test_critical_updates_override_dismissal_but_always_allow_not_now(self):
        critical = state(critical=True, dismissed=True, status="ready")
        critical["release"]["headline"] = "Fixes the helper crash"
        banner = update.banner_for(critical, True)
        self.assertIn("Fixes the helper crash", banner["text"])
        self.assertFalse(banner["dismiss"])
        critical["pickup"] = True
        self.assertEqual(update.banner_for(critical, True)["button"], ("Not now", "update_defer"))
        self.assertFalse(update.banner_for(critical, False)["dismiss"])

    def test_non_installable_location_has_a_reason(self):
        self.assertIn("update", update.banner_for(state(), False)["text"].lower())

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
                         {"text": "Loopcut 0.1.4 installs when you close Blender.", "button": ("Restart now", "update_restart"),
                          "dismiss": True, "progress": None})
        self.assertEqual(update.banner_for(state(status="installing"), True)["text"],
                         "Restarting to install Loopcut 0.1.4…")
        failed = update.banner_for(state(status="error", error="HTTP 404 downloading x"), True)
        self.assertEqual(failed["text"], "The update couldn't finish. You can keep working.")
        self.assertEqual(failed["button"], ("Try again", "update_download"))
        self.assertEqual(update.banner_for(state(status="error", error="x"), False)["button"], ("Download", "update_page"))

    def test_status_line(self):
        self.assertEqual(update.status_line(state(status="idle", release=None), (0, 1, 3)), "Loopcut 0.1.3")
        self.assertEqual(update.status_line(state(status="idle", release=None, error="HTTP 500"), (0, 1, 3)),
                         "Could not check for updates: HTTP 500")
        self.assertEqual(update.status_line(state(status="current", release=None), (0, 1, 3)), "Loopcut 0.1.3 is up to date.")
        self.assertEqual(update.status_line(state(status="ready"), (0, 1, 3)), "Loopcut 0.1.4 installs when you close Blender.")
        self.assertEqual(update.status_line(state(status="downloading", progress=0.5), (0, 1, 3)), "Downloading Loopcut 0.1.4… 50%")


class StartupPolicyTest(unittest.TestCase):
    def setUp(self):
        self.pending = {"manifest": manifest(), "auto_restart_attempted": False}

    def eligible(self, **changes):
        return update.startup_eligible(self.pending, (0, 1, 3),
            **dict({"dirty": False, "filepath": "", "argv": ["blender"], "busy": False}, **changes))

    def test_clean_newer_pending_update_only(self):
        self.assertTrue(self.eligible())
        for changes in ({"dirty": True}, {"filepath": "working.blend"}, {"busy": True},
                        {"argv": ["blender", "C:/My Work/FILE.BLEND"]},
                        {"argv": ["blender", "--python", "script.py"]}, {"argv": ["blender", "-b"]}):
            with self.subTest(changes):
                self.assertFalse(self.eligible(**changes))
        self.pending["manifest"]["version"] = "0.1.3"
        self.assertFalse(self.eligible())

    def test_failed_restart_never_repeats_even_for_critical_update(self):
        self.pending["auto_restart_attempted"] = True
        self.pending["manifest"]["min_version"] = "0.1.4"
        self.assertFalse(self.eligible())

    def test_retry_at_most_once_per_version_per_day(self):
        result = {"version": "0.1.4", "attempted_at": 100}
        self.assertFalse(update.retry_due(result, "0.1.4", 101))
        self.assertFalse(update.retry_due(result, "0.1.4", 99))  # Clock moved back: fail closed.
        self.assertTrue(update.retry_due(result, "0.1.4", 100 + update.RETRY_AFTER))
        self.assertTrue(update.retry_due(result, "0.1.5", 101))


class CacheTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="update-test-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.events = Mock()
        self.patches = [patch.object(update, "_cache", self.folder), patch.object(update, "_pending", {}),
                        patch.object(update, "_job", None), patch.object(update, "_helper_process", None), patch.object(update, "_target", "/Applications/Loopcut.app"),
                        patch.object(update, "_state", state()), patch.object(update, "_event", self.events),
                        patch.object(update, "_set", lambda **kw: update._state.update(kw)),
                        patch.object(update, "current_version", return_value=(0, 1, 3)),
                        patch.object(update, "platform_key", return_value="darwin-arm64")]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        payload = manifest()
        asset = payload["assets"]["darwin-arm64"]
        asset.update(size=9, sha256=hashlib.sha256(b"installer").hexdigest())
        self.release = update.parse_manifest(payload, "darwin-arm64")
        update._state["release"] = self.release
        self.installer = self.folder / asset["name"]
        self.installer.write_bytes(b"installer")

    def pending(self):
        update._save_pending(self.release)
        return json.loads((self.folder / "pending.json").read_text())

    def test_round_trip_and_guard_survive_new_session(self):
        self.pending()
        update._pending["auto_restart_attempted"] = True
        update._save_pending(self.release)
        update._pending = {}
        with patch.object(update, "_arm", return_value=False):
            update._load_pending()
        self.assertTrue(update._pending["auto_restart_attempted"])
        self.assertEqual(update._state["path"], str(self.installer))
        update._save_pending(self.release)
        self.assertTrue(json.loads((self.folder / "pending.json").read_text())["auto_restart_attempted"])

    def test_tampered_or_missing_download_is_not_used(self):
        for content in (b"tampered!", None):
            with self.subTest(content):
                self.pending()
                if content is None:
                    self.installer.unlink(missing_ok=True)
                else:
                    self.installer.write_bytes(content)
                with patch.object(update, "_arm") as arm:
                    update._load_pending()
                arm.assert_not_called()
                self.assertFalse((self.folder / "pending.json").exists())

    def test_stale_record_and_installer_are_removed(self):
        self.pending()
        with patch.object(update, "current_version", return_value=(0, 1, 4)), patch.object(update, "_arm") as arm:
            update._load_pending()
        arm.assert_not_called()
        self.assertFalse(self.installer.exists())
        self.assertFalse((self.folder / "pending.json").exists())

    def test_legacy_success_is_confirmed_by_running_version_without_a_result_file(self):
        self.pending()
        update._pending.update(legacy_fallback=True, auto_restart_attempted=True)
        update._save_pending(self.release)
        with patch.object(update, "current_version", return_value=(0, 1, 4)):
            update._load_pending()
            update._load_pending()
        self.events.assert_called_once_with("applied", "0.1.3", "0.1.4")

    def test_fallback_is_scoped_to_one_release_version(self):
        self.pending()
        update._pending["legacy_fallback"] = True
        update._save_pending(self.release)
        newer = {**self.release, "label": "0.1.5", "version": (0, 1, 5),
                 "asset": {**self.release["asset"], "name": "loopcut-v0.1.5-arm64.dmg"}}
        update._save_pending(newer)
        self.assertFalse(update._pending.get("legacy_fallback", False))

    def test_update_result_and_updated_from_only_once(self):
        self.pending()
        update._write_json(self.folder / "version.json", {"version": "0.1.3"})
        update._write_json(self.folder / "result-0.1.4.json",
                          {"version": "0.1.4", "status": "applied", "attempted_at": 100})
        with patch.object(update, "current_version", return_value=(0, 1, 4)):
            update._load_pending()
            update._load_pending()
        self.events.assert_any_call("updated_from", "0.1.3", "0.1.4")
        self.events.assert_any_call("applied", "0.1.3", "0.1.4")
        self.assertEqual(self.events.call_count, 2)

    def test_old_binary_confirms_failed_install_even_when_helper_said_applied(self):
        self.pending()
        update._write_json(self.folder / "result-0.1.4.json",
                          {"version": "0.1.4", "status": "applied", "attempted_at": 100})
        with patch.object(update, "_arm", return_value=False):
            update._load_pending()
            update._load_pending()
        self.events.assert_called_once_with("failed", "0.1.3", "0.1.4")

    def test_verified_download_is_persisted_then_armed_without_click(self):
        with patch.object(update, "_download", return_value=self.installer), patch.object(update, "_arm") as arm:
            update._download_locked()
        arm.assert_called_once_with()
        self.assertTrue((self.folder / "pending.json").is_file())
        self.events.assert_any_call("downloaded")

    def test_cached_installer_is_rechecked_and_bad_cache_is_replaced(self):
        with patch.object(update, "_stream") as stream:
            self.assertEqual(update._download(self.release["asset"]), self.installer)
        stream.assert_not_called()
        self.installer.write_bytes(b"tampered!")
        def stream(asset, path):
            path.write_bytes(b"installer")
        with patch.object(update, "_stream", side_effect=stream) as download:
            self.assertEqual(update._download(self.release["asset"]), self.installer)
        download.assert_called_once()
        self.assertEqual(self.installer.read_bytes(), b"installer")

    def test_failed_download_does_not_leave_a_pending_record_or_partial(self):
        self.installer.unlink()
        def fail(asset, path):
            path.write_bytes(b"partial")
            raise update.UpdateError("checksum")
        with patch.object(update, "_stream", side_effect=fail):
            update._download_locked()
        self.assertFalse((self.folder / "pending.json").exists())
        self.assertFalse(list(self.folder.glob("*.part")))
        self.events.assert_called_once_with("failed")

    def test_arm_requires_helper_acknowledgement(self):
        update._state["path"] = str(self.installer)
        def start(*args, **kwargs):
            job_path = self.folder / f"job-0.1.4-{os.getpid()}" / "job.json"
            job = json.loads(job_path.read_text())
            Path(job["ack"]).touch()
            self.assertNotIn("shell", kwargs)
            return Mock(poll=lambda: None)
        with patch.object(update.sys, "platform", "darwin"), patch.object(update.subprocess, "Popen", side_effect=start):
            self.assertTrue(update._arm())
        self.assertTrue(update._state["armed"])
        self.assertFalse(Path(update._job["restart"]).exists())
        self.events.assert_called_once_with("armed")

    def test_dead_helper_is_never_reported_as_armed(self):
        update._state["path"] = str(self.installer)
        for platform, status in (("darwin", "error"), ("win32", "ready")):
            with self.subTest(platform=platform), windows_platform(), patch.object(update.sys, "platform", platform), \
                    patch.object(update.subprocess, "Popen", return_value=Mock(poll=lambda: 1)):
                self.assertFalse(update._arm())
            self.assertFalse(update._state["armed"])
            self.assertEqual(update._state["status"], status)
            self.assertEqual(update._state["legacy_fallback"], platform == "win32")

    def test_windows_no_ack_times_out_into_durable_fallback(self):
        update._state["path"] = str(self.installer)
        process = Mock(poll=lambda: None)
        with windows_platform(), patch.object(update.subprocess, "Popen", return_value=process) as start, \
                patch.object(update.time, "monotonic", side_effect=[0, update.HELPER_ACK_TIMEOUT + 1]):
            self.assertFalse(update._arm())
        process.terminate.assert_called_once()
        self.assertEqual(start.call_args.args[0][-4], "-File")
        self.assertTrue(update._state["legacy_fallback"])
        self.assertFalse(update._state["armed"])
        self.assertEqual(update._state["status"], "ready")
        self.assertEqual(update.banner_for(update._state, True)["button"], ("Restart to update", "update_restart"))
        self.assertTrue(json.loads((self.folder / "pending.json").read_text())["legacy_fallback"])
        self.assertTrue((self.folder / f"job-0.1.4-{os.getpid()}" / "job.json.cancel").exists())
        self.events.assert_called_once_with("failed", reason="helper_noack")

    def test_cold_windows_helper_can_ack_after_the_old_five_second_limit(self):
        update._state["path"] = str(self.installer)
        clock, record = [0.0], {}
        def start(command, **kwargs):
            record.update(json.loads(Path(command[-1]).read_text()))
            clock[0] += 4  # Process creation itself is part of the acknowledgement budget.
            return Mock(poll=lambda: None)
        def advance(seconds):
            clock[0] += 1  # Simulate scheduler/module-loading delays without waiting in the test.
            if clock[0] >= 10:
                Path(record["ack"]).touch()
        with windows_platform(), patch.object(update.subprocess, "Popen", side_effect=start), \
                patch.object(update.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(update.time, "sleep", side_effect=advance):
            self.assertTrue(update._arm())
        self.assertGreater(clock[0], 5)
        self.assertTrue(update._state["armed"])
        self.assertFalse(update._state["legacy_fallback"])
        self.events.assert_called_once_with("armed")

    def test_windows_ack_deadline_includes_time_spent_starting_the_process(self):
        update._state["path"] = str(self.installer)
        clock = [0.0]
        process = Mock(poll=lambda: None)
        def start(command, **kwargs):
            clock[0] += update.HELPER_ACK_TIMEOUT - 10
            return process
        def advance(seconds):
            clock[0] += 5
        with windows_platform(), patch.object(update.subprocess, "Popen", side_effect=start), \
                patch.object(update.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(update.time, "sleep", side_effect=advance):
            self.assertFalse(update._arm())
        self.assertEqual(clock[0], update.HELPER_ACK_TIMEOUT)
        process.terminate.assert_called_once()
        self.assertTrue(update._state["legacy_fallback"])
        self.events.assert_called_once_with("failed", reason="helper_noack")

    def test_windows_blocked_or_early_dead_powershell_keeps_fallback(self):
        update._state["path"] = str(self.installer)
        for outcome in (OSError("blocked"), Mock(poll=lambda: 1)):
            with self.subTest(outcome=type(outcome).__name__):
                update._pending = {}
                update._state["legacy_fallback"] = False
                kwargs = {"side_effect": outcome} if isinstance(outcome, OSError) else {"return_value": outcome}
                with windows_platform(), patch.object(update.subprocess, "Popen", **kwargs):
                    self.assertFalse(update._arm())
                self.assertTrue(update._state["legacy_fallback"])
                self.assertFalse(update._state["armed"])

    def test_windows_ack_keeps_install_on_quit_and_file_launch(self):
        update._state["path"] = str(self.installer)
        odd = self.folder / "Sam's 50% & Zoë"
        odd.mkdir()
        def acknowledge(command, **kwargs):
            job_bytes = Path(command[-1]).read_bytes()
            self.assertTrue(job_bytes.isascii())
            record = json.loads(job_bytes.decode("utf-8"))
            self.assertEqual(Path(record["ack"]).parent.parent, odd)
            script_bytes = Path(command[-3]).read_bytes()
            self.assertTrue(script_bytes.startswith(b'\xef\xbb\xbf'))
            self.assertNotIn(b'\n', script_bytes.replace(b'\r\n', b''))
            self.assertEqual(script_bytes.decode("utf-8-sig").replace("\r\n", "\n"), update.HELPER_PS)
            Path(record["ack"]).touch()
            self.assertNotIn("shell", kwargs)
            self.assertFalse(any(key.upper() == "PSMODULEPATH" for key in kwargs["env"]))
            self.assertEqual(command[:8], ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
                                          "-ExecutionPolicy", "Bypass", "-File", str(Path(command[-1]).with_name("apply.ps1"))])
            return Mock(poll=lambda: None)
        with windows_platform(), patch.object(update, "_cache", odd), \
                patch.dict(os.environ, {"PSModulePath": "pwsh7-modules"}), \
                patch.object(update.subprocess, "Popen", side_effect=acknowledge):
            self.assertTrue(update._arm())
        self.assertTrue(update._state["armed"])
        self.assertFalse(update._state["legacy_fallback"])
        self.assertIn("installs when you close", update.banner_for(update._state, True)["text"])

    def test_fallback_survives_startup_without_retrying_powershell_or_auto_restarting(self):
        self.pending()
        update._pending["legacy_fallback"] = True
        update._save_pending(self.release)
        update._pending = {}
        with windows_platform(), patch.object(update.subprocess, "Popen") as start:
            update._load_pending()
        start.assert_not_called()
        self.assertTrue(update._state["legacy_fallback"])
        self.assertFalse(update.startup_eligible(update._pending, (0, 1, 3), dirty=False, filepath="", argv=["blender"]))
        fake = types.SimpleNamespace()
        with windows_platform(), patch.dict(sys.modules, bpy=fake), patch.object(update.threading, "Thread") as thread:
            update._request_restart(automatic=True)
        thread.assert_not_called()

    def test_explicit_fallback_restart_launches_the_legacy_command_only_after_click(self):
        import loopcut
        self.pending()
        update._state.update(path=str(self.installer), legacy_fallback=True)
        queue = Mock()
        with windows_platform(), patch.dict(os.environ, {"PSModulePath": "pwsh7-modules"}), \
                patch.object(update.subprocess, "Popen", return_value=Mock(poll=lambda: None)) as start, \
                patch.object(loopcut, "mainthread", types.SimpleNamespace(run_on_main=queue), create=True):
            update._restart_legacy()
        self.assertEqual(start.call_args.args[0], ["cmd.exe", "/c", str(self.folder / "apply.cmd"), str(os.getpid()),
                                                  str(self.installer), update._target, str(self.folder / "update.log")])
        self.assertNotIn("shell", start.call_args.kwargs)
        self.assertFalse(any(key.upper() == "PSMODULEPATH" for key in start.call_args.kwargs["env"]))
        self.assertEqual((self.folder / "apply.cmd").read_text(), update.HELPER_CMD)
        self.assertTrue(update._state["armed"])
        self.assertTrue(update._pending["auto_restart_attempted"])
        queue.assert_called_once_with(update._quit_legacy)

    def test_av_killing_an_acknowledged_helper_switches_to_fallback(self):
        self.pending()
        update._state.update(path=str(self.installer), armed=True)
        update._helper_process = Mock(poll=lambda: 1)
        def thread(**kwargs):
            return types.SimpleNamespace(start=lambda: kwargs["target"]())
        with windows_platform(), patch.object(update.threading, "Thread", side_effect=thread):
            update._watch_helper()
        self.assertTrue(update._state["legacy_fallback"])
        self.assertFalse(update._state["armed"])
        self.events.assert_called_once_with("failed", reason="helper_noack")

    def test_cooldown_keeps_working_without_launching_helper(self):
        update._state["path"] = str(self.installer)
        update._write_json(self.folder / "result-0.1.4.json",
                          {"version": "0.1.4", "status": "failed", "attempted_at": update.time.time()})
        with patch.object(update.subprocess, "Popen") as process:
            self.assertFalse(update._arm())
        process.assert_not_called()
        self.assertEqual(update._state["status"], "waiting")

    def test_live_installer_is_not_reported_as_a_failed_install(self):
        self.pending()
        update._write_json(self.folder / "result-0.1.4.json",
                          {"version": "0.1.4", "status": "installing", "attempted_at": update.time.time(),
                           "helper_pid": os.getpid()})
        with patch.object(update.subprocess, "Popen") as process:
            update._load_pending()
        process.assert_not_called()
        self.events.assert_not_called()
        self.assertEqual(update._state["status"], "applying")

    def test_restart_guard_is_durable_before_quit_and_late_edits_cancel_exit(self):
        self.pending()
        job = helper_job(self.folder)
        update._job = job
        update._state["armed"] = True
        callbacks = []
        fake = types.SimpleNamespace(data=types.SimpleNamespace(is_dirty=False, filepath=""),
            app=types.SimpleNamespace(timers=types.SimpleNamespace(register=lambda fn, **kw: callbacks.append(fn))),
            ops=types.SimpleNamespace(wm=types.SimpleNamespace(quit_blender=Mock())))
        with patch.dict(sys.modules, bpy=fake), patch.object(update, "_safe_startup", return_value=True):
            update._request_restart(automatic=True)
            self.assertTrue(json.loads((self.folder / "pending.json").read_text())["auto_restart_attempted"])
            self.assertTrue(Path(job["restart"]).exists())
            fake.data.is_dirty = True
            callbacks[0]()
        fake.ops.wm.quit_blender.assert_not_called()
        self.assertFalse(Path(job["restart"]).exists())
        self.assertEqual(update._state["status"], "ready")

    def test_not_now_can_cancel_between_countdown_and_scheduled_exit(self):
        self.pending()
        update._job = helper_job(self.folder)
        update._state["armed"] = True
        callbacks = []
        fake = types.SimpleNamespace(data=types.SimpleNamespace(is_dirty=False, filepath=""),
            app=types.SimpleNamespace(timers=types.SimpleNamespace(register=lambda fn, **kw: callbacks.append(fn))),
            ops=types.SimpleNamespace(wm=types.SimpleNamespace(quit_blender=Mock())))
        with patch.dict(sys.modules, bpy=fake), patch.object(update, "_safe_startup", return_value=True):
            update._request_restart(automatic=True)
            update.defer_startup()
            callbacks[0]()
        fake.ops.wm.quit_blender.assert_not_called()
        self.assertFalse(Path(update._job["restart"]).exists())

    def test_manifest_offer_sets_critical_and_downloads_automatically(self):
        update._state = state(release=None, status="idle")
        self.release.update(min_version=(0, 1, 4), headline="Fixes the helper crash")
        with patch.object(update, "_fetch_manifest", return_value=self.release), patch.object(update, "installable", return_value=True), \
                patch.object(update, "_download_locked") as download:
            update._run_check(True)
        self.assertTrue(update._state["critical"])
        self.events.assert_called_once_with("offered")
        download.assert_called_once()

    def test_network_check_failure_does_not_forget_an_armed_update(self):
        update._state.update(status="ready", armed=True, path=str(self.installer))
        with patch.object(update, "_fetch_manifest", side_effect=update.UpdateError("offline")):
            update._run_check(True)
        self.assertTrue(update._state["armed"])
        self.assertEqual(update._state["status"], "ready")

    def test_not_now_and_popup_cancel_stop_countdown(self):
        update._state["pickup"] = True
        update.defer_startup()
        with patch.object(update, "_request_restart") as restart:
            self.assertIsNone(update._pickup_tick())
        restart.assert_not_called()
        fake = types.SimpleNamespace(types=types.SimpleNamespace(Operator=object))
        with patch.dict(sys.modules, bpy=fake):
            notice = update._operators()[-1]()
            update._state["pickup"] = True
            notice.cancel(None)
            self.assertFalse(update._state["pickup"])
            update._state["pickup"] = True
            notice.execute(None)
            self.assertFalse(update._state["pickup"])

    def test_unsaved_work_uses_confirmation_before_relaunch_is_requested(self):
        fake = types.SimpleNamespace(types=types.SimpleNamespace(Operator=object),
            data=types.SimpleNamespace(is_dirty=True))
        context = types.SimpleNamespace(window_manager=types.SimpleNamespace(invoke_props_dialog=Mock(return_value={"RUNNING_MODAL"})))
        with patch.dict(sys.modules, bpy=fake), patch.object(update, "_request_restart") as restart:
            operator = update._operators()[1]()
            self.assertEqual(operator.invoke(context, None), {"RUNNING_MODAL"})
        restart.assert_not_called()


class TelemetryTest(unittest.TestCase):
    def test_fallback_reason_is_accepted_by_existing_telemetry(self):
        from loopcut import telemetry
        code = update.event_code("failed", "0.2.10", "0.2.11", "win32", True, "helper_noack")
        self.assertEqual(code, "update_failed_0_2_10_to_0_2_11_windows_1_helper_noack")
        self.assertLessEqual(len(code), 64)
        props = telemetry.setup_step_props("done", None, False, code)
        self.assertEqual(props["error_code"], code)

    def test_update_payloads_validate_against_live_checkout_contract(self):
        from platform_contract import require
        from test_telemetry import validate
        from loopcut import telemetry
        path = require("schemas", "usage-event.schema.json")
        schema = json.loads(path.read_text())
        cases = [(name, "") for name in ("offered", "downloaded", "armed", "applied", "failed", "updated_from")]
        for name, reason in cases + [("failed", "helper_noack")]:
            code = update.event_code(name, "0.2.10", "0.2.11", "win32", True, reason)
            body = telemetry.envelope("setup_step", telemetry.setup_step_props("done", None, name != "failed", code),
                                      "12345678-1234-4234-8234-123456789abc", "0.2.10")
            validate(body, schema, schema, path.parent)

    def test_update_codes_fit_existing_server_props(self):
        from loopcut import telemetry
        for name in ("offered", "downloaded", "armed", "applied", "failed", "updated_from"):
            for platform in ("win32", "darwin", "linux"):
                for installable in (False, True):
                    code = update.event_code(name, "0.2.10", "0.2.11", platform, installable)
                    self.assertLessEqual(len(code), 64)
                    self.assertRegex(code, r"^[a-z0-9_]+$")
                    props = telemetry.setup_step_props("done", None, name != "failed", code)
                    envelope = telemetry.envelope("setup_step", props, "12345678-1234-4234-8234-123456789abc", "0.2.10")
                    self.assertEqual(envelope["props"]["error_code"], code)

    def test_update_events_use_normal_opt_out_gated_sender(self):
        from loopcut import telemetry
        with patch.object(telemetry, "enabled", return_value=False), patch.object(telemetry, "_ensure_sender") as sender:
            telemetry.setup_step("done", None, True, update.event_code("armed", "0.2.10", "0.2.11", "win32", True))
        sender.assert_not_called()


class ReleaseScriptTest(unittest.TestCase):
    @unittest.skipIf(sys.platform == "win32", "POSIX release tooling")
    def test_dry_run_metadata_defaults_validation_and_old_client_compatibility(self):
        root = Path(__file__).resolve().parents[2]
        version = update.label(update.current_version())
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / f"loopcut-v{version}-arm64.dmg"
            file.write_bytes(b"installer")
            command = ["/bin/sh", str(root / "loopcut/scripts/publish_release.sh"), "v" + version]
            defaults = subprocess.run([*command, "--dry-run", str(file)], capture_output=True, text=True, check=True)
            self.assertNotIn("min_version", json.loads(defaults.stdout))
            self.assertNotIn("headline", json.loads(defaults.stdout))
            headline = "Fixes Sam's helper & 50% crash — safely"
            result = subprocess.run([*command, "--draft", "--partial", "--min-version", "0.2.5", "--headline", headline,
                                     "--dry-run", str(file)], capture_output=True, text=True, check=True)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["min_version"], "0.2.5")
            self.assertEqual(payload["headline"], headline)
            self.assertEqual(update.parse_manifest(payload, "darwin-arm64")["version"], update.current_version())
            for tag in ("v0.2.5", "v0.2.6", "v0.2.7", "v0.2.8", "v0.2.9"):
                source = subprocess.run(["git", "show", f"{tag}:scripts/addons_core/loopcut/update.py"],
                                        cwd=root, capture_output=True, text=True)
                if source.returncode:
                    continue  # A shallow CI checkout has no release tags; local check covers all five.
                legacy = {"__package__": "loopcut", "__name__": "loopcut.legacy_update"}
                exec(compile(source.stdout, tag, "exec"), legacy)
                self.assertEqual(legacy["parse_manifest"](payload, "darwin-arm64")["version"], update.current_version())
            for flags in (["--min-version", "9.0.0"], ["--min-version", "invalid"],
                          ["--headline", "line\nbreak"], ["--headline", "x" * 121], ["--headline"], ["--unknown"]):
                result = subprocess.run([*command, "--dry-run", *flags, str(file)], capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0, flags)


@unittest.skipUnless(sys.platform == "darwin", "macOS shell and atomic rename")
class MacHelperTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="Sam's 50% & Zoë-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.job = helper_job(self.folder)
        Path(self.job["installer"]).write_bytes(b"installer")
        target = Path(self.job["target"])
        target.mkdir()
        (target / "version").write_text("old")
        self.source = self.folder / "source.app"
        (self.source / "Contents/MacOS").mkdir(parents=True)
        (self.source / "version").write_text("new")
        binary = self.source / "Contents/MacOS/Loopcut"
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o700)
        self.bin = self.folder / "bin"
        self.bin.mkdir()
        stubs = {
            "hdiutil": '''#!/bin/sh
[ "$UPDATE_FAIL" != "$1" ] || exit 1
if [ "$1" = attach ]; then
  while [ "$#" -gt 0 ]; do
    if [ "$1" = -mountpoint ]; then shift; mnt="$1"; fi
    shift
  done
  /bin/cp -R "$UPDATE_SOURCE" "$mnt/Loopcut.app"
else
  # The real detach removes the mounted contents without changing the source.
  /bin/rm -rf "$2/Loopcut.app"
fi
''',
            "ditto": '''#!/bin/sh
[ "$UPDATE_FAIL" != copy ] || exit 1
/bin/cp -R "$1" "$2"
''',
            "open": '''#!/bin/sh
printf '%s\\n' "$@" >> "$UPDATE_OPEN_LOG"
''',
        }
        for name, contents in stubs.items():
            path = self.bin / name
            path.write_text(contents)
            path.chmod(0o700)
        self.env = {**os.environ, "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                    "TMPDIR": str(self.folder), "UPDATE_SOURCE": str(self.source), "UPDATE_FAIL": "",
                    "UPDATE_OPEN_LOG": str(self.folder / "opened")}
        self.script = self.folder / "apply.sh"
        self.script.write_text(update.HELPER_SH)

    def run_helper(self):
        return subprocess.run(update.helper_command(str(self.script), self.job, "", "darwin"),
                              env=self.env, capture_output=True, text=True, timeout=15)

    def test_plain_quit_installs_without_relaunch_and_daily_retry_skips(self):
        result = self.run_helper()
        self.assertEqual(result.returncode, 0, Path(self.job["log"]).read_text())
        self.assertEqual((Path(self.job["target"]) / "version").read_text(), "new")
        self.assertFalse((self.folder / "opened").exists())
        self.assertEqual(json.loads(Path(self.job["result"]).read_text())["status"], "applied")
        Path(self.job["restart"]).touch()
        self.assertEqual(self.run_helper().returncode, 0)
        self.assertFalse((self.folder / "opened").exists())

    def test_explicit_restart_relaunches_after_success(self):
        Path(self.job["restart"]).touch()
        self.assertEqual(self.run_helper().returncode, 0)
        self.assertIn(self.job["target"], (self.folder / "opened").read_text())

    def test_checksum_and_copy_failures_preserve_original(self):
        for failure in ("checksum", "copy", "attach"):
            with self.subTest(failure):
                Path(self.job["result"]).unlink(missing_ok=True)
                self.env["UPDATE_FAIL"] = failure
                self.job["sha256"] = "a" * 64 if failure == "checksum" else hashlib.sha256(b"installer").hexdigest()
                result = self.run_helper()
                self.assertEqual(result.returncode, 1)
                self.assertEqual((Path(self.job["target"]) / "version").read_text(), "old")
                self.assertEqual(json.loads(Path(self.job["result"]).read_text())["status"], "failed")
                self.assertFalse((self.folder / "opened").exists())


class WindowsDiagnosticsTest(unittest.TestCase):
    def test_smoke_ack_wait_uses_the_process_start_deadline(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(windows_smoke.time, "monotonic", side_effect=[31, 31, 62]), \
                patch.object(windows_smoke.time, "sleep") as sleep:
            with self.assertRaisesRegex(AssertionError, "after 31.0s.*limit 30s"):
                windows_smoke.wait_for(Path(temp) / "waiting", Mock(poll=lambda: None), timeout=30, started_at=0)
        sleep.assert_not_called()

    def test_smoke_prints_startup_output_when_the_helper_exits_without_ack(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / "Sam's 50% & Zoë"
            dummy, helper = Mock(pid=123, poll=lambda: None), Mock(pid=456, poll=lambda: 7)
            def start(command, **kwargs):
                if command[0] == sys.executable:
                    return dummy
                self.assertFalse(any(key.upper() == "PSMODULEPATH" for key in kwargs["env"]))
                kwargs["stdout"].write(b"cannot initialize PowerShell module\r\n")
                kwargs["stdout"].flush()
                return helper
            with patch.object(subprocess, "Popen", side_effect=start), patch("builtins.print") as output:
                with self.assertRaisesRegex(AssertionError, "failed to produce waiting"):
                    windows_smoke.exercise_powershell(folder, Path(temp) / "installer.msi", {"size": 9, "sha256": SHA})
            self.assertIn("cannot initialize PowerShell module", output.call_args.args[0])
            self.assertIn("exit code: 7", output.call_args.args[0])
            dummy.kill.assert_called_once()
            helper.kill.assert_not_called()

    def test_startup_errors_and_unicode_paths_survive_ci_output(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / "Sam's 50% & Zoë"
            folder.mkdir()
            (folder / "apply.ps1").write_text(update.HELPER_PS, encoding="utf-8-sig", newline="\r\n")
            (folder / "msi.log").write_text("installer failure: Zoë", encoding="utf-16")
            (folder / "helper.log").write_text("2026-10-23T01:46:40.6363554Z start PowerShell=5.1\n"
                                               "2026-10-23T01:46:41.6363554Z fatal error: bad JSON", encoding="utf-8-sig")
            details = windows_smoke.helper_diagnostics(folder, Mock(pid=123, poll=lambda: 7), extra=(folder / "msi.log",))
            self.assertIn("PS1 UTF-8 BOM: True; CRLF: True", details)
            self.assertIn("exit code: 7", details)
            self.assertIn("installer failure: Zoë", details)
            self.assertIn("2026-10-23T01:46:41.6363554Z fatal error:", details)
            self.assertIn("fatal error: bad JSON", details)
            self.assertIn("Zo\\xeb", details)  # Path identity remains readable on any console code page.


@unittest.skipUnless(sys.platform == "win32", "real Windows PowerShell quoting and helper control flow")
class WindowsHelperTest(unittest.TestCase):
    def test_ack_and_timestamped_logs_are_flushed_before_pid_wait_and_hash(self):
        with tempfile.TemporaryDirectory(prefix="Sam's 50% & Zoë-") as temp:
            folder = Path(temp)
            job = helper_job(folder)
            job["pid"] = os.getpid()  # This test stays alive; no installer exists or may run.
            record = folder / "job.json"
            record.write_text(json.dumps(job), encoding="utf-8")
            script, log = folder / "apply.ps1", folder / "helper.log"
            script.write_text(update.HELPER_PS, encoding="utf-8-sig", newline="\r\n")
            process = None
            try:
                with log.open("wb") as output:
                    started_at = update.time.monotonic()
                    process = subprocess.Popen(update.helper_command(str(script), job, str(record), "win32"),
                                               env=update.helper_environment(), stdin=subprocess.DEVNULL,
                                               stdout=output, stderr=subprocess.STDOUT, close_fds=True,
                                               creationflags=update.windows_helper_flags())
                    try:
                        windows_smoke.wait_for(Path(job["ack"]), process, timeout=update.HELPER_ACK_TIMEOUT, started_at=started_at)
                    except AssertionError as error:
                        self.fail(f"{error}\n{windows_smoke.helper_diagnostics(folder, process)}")
                    deadline = update.time.monotonic() + 5
                    while "acknowledged; waiting" not in log.read_text(encoding="utf-8-sig", errors="replace"):
                        if process.poll() is not None or update.time.monotonic() >= deadline:
                            self.fail(windows_smoke.helper_diagnostics(folder, process))
                        update.time.sleep(0.05)
                    details = windows_smoke.helper_diagnostics(folder, process)
                    self.assertIsNone(process.poll(), details)
                    for step in ("start PowerShell=5.1", "reading job file", "job file read; parsing JSON", "acknowledged; waiting"):
                        self.assertRegex(details, r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+Z " + step)
                    self.assertNotIn("hashing SHA-256", details)
                    self.assertFalse(Path(job["result"]).exists(), details)
                    self.assertFalse(Path(job["log"]).exists(), details)
            finally:
                windows_smoke.stop_owned(process)

    def test_invalid_job_logs_the_bootstrap_error_before_acknowledgement(self):
        with tempfile.TemporaryDirectory(prefix="Sam's 50% & Zoë-") as temp:
            folder = Path(temp)
            record = folder / "job.json"
            record.write_text("{invalid JSON", encoding="utf-8")
            script = folder / "apply.ps1"
            script.write_text(update.HELPER_PS, encoding="utf-8-sig", newline="\r\n")
            result = subprocess.run(update.helper_command(str(script), {}, str(record), "win32"),
                                    env=update.helper_environment(), capture_output=True,
                                    text=True, encoding="utf-8", errors="replace", timeout=update.HELPER_ACK_TIMEOUT + 10)
            details = result.stdout + result.stderr + windows_smoke.helper_diagnostics(folder)
            self.assertNotEqual(result.returncode, 0, details)
            self.assertIn("start PowerShell=5.1", details)
            self.assertIn("fatal error:", details)
            self.assertFalse((folder / "waiting").exists(), details)

    def test_cancelled_no_ack_job_cannot_install_if_it_starts_late(self):
        with tempfile.TemporaryDirectory(prefix="Sam's 50% & Zoë-") as temp:
            folder = Path(temp)
            job = helper_job(folder)
            record = folder / "job.json"
            record.write_text(json.dumps(job), encoding="utf-8")
            Path(str(record) + ".cancel").touch()
            script = folder / "apply.ps1"
            script.write_text(update.HELPER_PS, encoding="utf-8-sig", newline="\r\n")
            result = subprocess.run(update.helper_command(str(script), job, str(record), "win32"),
                                    env=update.helper_environment(), capture_output=True,
                                    text=True, encoding="utf-8", errors="replace", timeout=update.HELPER_ACK_TIMEOUT + 10)
            details = result.stdout + result.stderr + windows_smoke.helper_diagnostics(folder)
            self.assertEqual(result.returncode, 0, details)
            self.assertTrue(Path(job["ack"]).exists(), details)
            self.assertFalse(Path(job["log"]).exists())
            self.assertFalse(Path(job["result"]).exists())

    def test_argument_paths_msi_results_uac_cancel_checksum_and_relaunch(self):
        for outcome in ("0", "3010", "1603", "cancel", "checksum"):
            for restart in (False, True):
                with self.subTest(outcome=outcome, restart=restart), tempfile.TemporaryDirectory(prefix="Sam's 50% & Zoë-") as temp:
                    folder = Path(temp)
                    job = helper_job(folder)
                    job["target"] = str(folder / "blender.exe")
                    Path(job["target"]).write_bytes(b"old executable")
                    Path(job["installer"]).write_bytes(b"installer")
                    if outcome == "checksum":
                        job["sha256"] = "a" * 64
                    if restart:
                        Path(job["restart"]).touch()
                    script = folder / "apply.ps1"
                    script.write_text(update.HELPER_PS, encoding="utf-8-sig", newline="\r\n")
                    record = folder / "job.json"
                    record.write_text(json.dumps(job), encoding="utf-8")
                    calls = folder / "calls.jsonl"
                    wrapper = folder / "wrapper.ps1"
                    wrapper.write_text(r'''param([string]$JobFile)
function Get-Process { param($Id, $ErrorAction) }
function Start-Process {
    param($FilePath, $ArgumentList, $Verb, [switch]$Wait, [switch]$PassThru)
    @{file=$FilePath; arguments=$ArgumentList; verb=$Verb} | ConvertTo-Json -Compress |
        Add-Content -LiteralPath $env:UPDATE_TEST_CALLS -Encoding UTF8
    if ($Verb -eq 'RunAs') {
        if ($env:UPDATE_TEST_OUTCOME -eq 'cancel') { throw 'UAC cancelled' }
        return [pscustomobject]@{ExitCode=[int]$env:UPDATE_TEST_OUTCOME}
    }
}
. ''' + "'" + str(script).replace("'", "''") + "' -JobFile $JobFile\n", encoding="utf-8-sig")
                    # Reproduce a pwsh -> Python -> Windows PowerShell launch. The child must
                    # rebuild its own 5.1 module search path rather than inherit this poisoned one.
                    env = update.helper_environment({**os.environ, "PSModulePath": str(folder / "pwsh7-modules"),
                                                     "UPDATE_TEST_CALLS": str(calls), "UPDATE_TEST_OUTCOME": outcome})
                    result = subprocess.run(update.helper_command(str(wrapper), job, str(record), "win32"),
                                            env=env, capture_output=True, text=True,
                                            encoding="utf-8", errors="replace", timeout=update.HELPER_ACK_TIMEOUT + 10)
                    details = result.stdout + result.stderr + windows_smoke.helper_diagnostics(folder)
                    self.assertEqual(result.returncode, 0, details)
                    self.assertTrue(Path(job["result"]).exists(), details)
                    self.assertIn("start PowerShell=5.1", details)
                    data = json.loads(Path(job["result"]).read_text(encoding="utf-8-sig"))
                    self.assertEqual(data["status"], "applied" if outcome in ("0", "3010") else "failed", details)
                    if outcome not in ("0", "3010"):
                        self.assertIn("install error:", details)
                    if outcome in ("0", "3010", "1603"):
                        self.assertIn("exit_code", data, details)
                        self.assertEqual(data["exit_code"], int(outcome), details)
                    self.assertEqual(Path(job["target"]).read_bytes(), b"old executable")
                    actions = [json.loads(line) for line in calls.read_text(encoding="utf-8-sig").splitlines()] if calls.exists() else []
                    installs = [action for action in actions if action["verb"] == "RunAs"]
                    self.assertEqual(len(installs), 0 if outcome == "checksum" else 1, details)
                    if installs:
                        self.assertEqual(installs[0]["arguments"], f'/i "{job["installer"]}" /passive /norestart /l*v "{job["log"]}"')
                    self.assertEqual(len([action for action in actions if action["file"] == job["target"]]), int(restart))


if __name__ == "__main__":
    unittest.main()
