"""Real MSI/helper check for the disposable, elevated Windows CI runner. Never starts Blender.

Downloads the public 0.2.9 MSI, verifies update.json's SHA-256, installs it, and repairs that
installation through each production helper. Legacy cmd always relaunches; its target here is
an inert batch probe that writes a marker and exits. No installer/process-launch calls are mocked.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))
from loopcut import update

RELEASE = "https://github.com/Samffprice/loopcut/releases/download/v0.2.9/"
MSI_NAME = "loopcut-v0.2.9-windows64.msi"
SUCCESS = (0, 3010)


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=True), encoding="utf-8")


def fetch_installer(folder):
    with urllib.request.urlopen(RELEASE + "update.json", timeout=30) as response:
        payload = json.loads(response.read(100_000))
    release = update.parse_manifest(payload, "windows-x64")
    asset = release["asset"]
    if release["version"] != (0, 2, 9) or asset["name"] != MSI_NAME or asset["url"] != RELEASE + MSI_NAME:
        raise AssertionError("Unexpected public release manifest")
    write_json(folder / "public-update.json", payload)
    installer = folder / MSI_NAME
    digest, received, deadline = hashlib.sha256(), 0, time.monotonic() + 240
    with urllib.request.urlopen(asset["url"], timeout=30) as response, installer.open("wb") as output:
        while chunk := response.read(1 << 20):
            received += len(chunk)
            if received > asset["size"] or time.monotonic() > deadline:
                raise AssertionError("Installer download exceeded its size/time bound")
            digest.update(chunk)
            output.write(chunk)
    if received != asset["size"] or digest.hexdigest() != asset["sha256"]:
        raise AssertionError("Public MSI does not match update.json")
    return installer, asset


def installed_products():
    import winreg
    products = []
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                            0, winreg.KEY_READ | view) as root:
            for index in range(winreg.QueryInfoKey(root)[0]):
                name = winreg.EnumKey(root, index)
                with winreg.OpenKey(root, name) as entry:
                    try:
                        display = winreg.QueryValueEx(entry, "DisplayName")[0]
                    except FileNotFoundError:
                        continue
                    if not display.startswith("Loopcut"):
                        continue
                    version = winreg.QueryValueEx(entry, "DisplayVersion")[0]
                    products.append({"product_code": name, "name": display, "version": version})
    if not products:
        raise AssertionError("Loopcut is absent from the installed-products registry")
    if not list((Path(os.environ["ProgramFiles"]) / "Loopcut").glob("*/blender.exe")):
        raise AssertionError("The installed Loopcut executable is missing")
    return sorted(products, key=lambda row: row["product_code"])


def run_installer(installer, log):
    result = subprocess.run(["msiexec.exe", "/i", str(installer), "/passive", "/norestart", "/l*v", str(log)], timeout=300)
    if result.returncode not in SUCCESS:
        raise AssertionError(f"Initial msiexec exited {result.returncode}; see {log}")
    return result.returncode


def stop_owned(process):
    if process is not None and process.poll() is None:
        process.kill()  # Only a Popen process created in this test, by its exact handle/PID.
        process.wait(timeout=5)


def helper_diagnostics(folder, process=None, extra=()):
    """Bounded, encoding-aware evidence, also used by the Windows unit tests on failure."""
    lines = [f"Helper folder: {ascii(str(folder))}"]
    if process is not None:
        lines.append(f"Helper PID: {process.pid}; exit code: {process.poll()}")
    script = folder / "apply.ps1"
    if script.exists():
        raw = script.read_bytes()
        bom, crlf = raw.startswith(b'\xef\xbb\xbf'), b'\r\n' in raw
        lines.append(f"PS1 UTF-8 BOM: {bom}; CRLF: {crlf}")
    for path in (folder / "helper.log", folder / "result.json", *extra):
        lines.append(f"--- {ascii(str(path))} ---")
        try:
            with path.open("rb") as source:
                raw = source.read(65_536)
            encoding = ("utf-16" if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else
                        "utf-16-le" if b'\x00' in raw[:200] else "utf-8-sig")
            lines.append(raw.decode(encoding, errors="replace"))
            if path.stat().st_size > len(raw):
                lines.append("[truncated after 65536 bytes]")
        except OSError as error:
            lines.append(f"[unavailable: {error}]")
    return "\n".join(lines)


def wait_for(path, process=None, timeout=10, started_at=None):
    started_at = time.monotonic() if started_at is None else started_at
    deadline = started_at + timeout
    while not path.exists():
        now = time.monotonic()
        if (process is not None and process.poll() is not None) or now >= deadline:
            raise AssertionError(f"Helper failed to produce {path.name} after {now - started_at:.1f}s (limit {timeout:g}s)")
        time.sleep(0.1)


def make_job(folder, installer, asset, pid):
    return {"pid": pid, "installer": str(installer), "target": str(folder / "relaunch.cmd"),
            "log": str(folder / "msi.log"), "restart": str(folder / "restart"),
            "result": str(folder / "result.json"), "version": "0.2.9", "from_version": "0.2.9",
            "sha256": asset["sha256"], "size": asset["size"], "ack": str(folder / "waiting")}


def assert_no_blender():
    # A marker proves the intended launch target wasn't invoked; this also catches accidental
    # fallback to the installed executable when looking up Program Files after MSI repair.
    result = subprocess.run(["tasklist.exe", "/FI", "IMAGENAME eq blender.exe", "/FO", "CSV", "/NH"],
                            capture_output=True, text=True, check=True, timeout=10)
    if '"blender.exe"' in result.stdout.lower():
        raise AssertionError("A helper started Blender")


def exercise_powershell(folder, installer, asset):
    folder.mkdir()
    script = folder / "apply.ps1"
    script.write_text(update.HELPER_PS, encoding="utf-8-sig", newline="\r\n")
    # This target would leave proof if the restart flag were ignored. Never set that flag.
    marker = folder.parent / "powershell-relaunched.txt"
    probe = folder.parent / "powershell-relaunch.cmd"
    probe.write_text('@echo off\r\necho unexpected>"' + str(marker) + '"\r\nexit\r\n', encoding="ascii")
    # Keep the waited-for PID alive throughout even a cold 30-second helper start. EOF below
    # exits it normally after the acknowledgement/wait assertions, without a timing race.
    dummy = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read(1)"], stdin=subprocess.PIPE)
    helper = None
    try:
        job = make_job(folder, installer, asset, dummy.pid)
        job["target"] = str(probe)
        job_path = folder / "job.json"
        write_json(job_path, job)
        with (folder / "helper.log").open("wb") as output:
            command = update.helper_command(str(script), job, str(job_path), "win32")
            environment = update.helper_environment()
            output.write((time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) +
                          f" starting helper; acknowledgement timeout={update.HELPER_ACK_TIMEOUT:g}s\n").encode("utf-8"))
            output.flush()
            started_at = time.monotonic()
            helper = subprocess.Popen(command,
                                      stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                      env=environment)
            wait_for(Path(job["ack"]), helper, timeout=update.HELPER_ACK_TIMEOUT, started_at=started_at)
            time.sleep(1)
            if dummy.poll() is not None or Path(job["result"]).exists() or Path(job["log"]).exists():
                raise AssertionError("PowerShell helper did not wait for the live PID")
            dummy.stdin.close()
            dummy.wait(timeout=5)
            exited_at = int(time.time())
            code = helper.wait(timeout=300)
        if code != 0:
            raise AssertionError(f"PowerShell helper exited {code}")
        result = json.loads(Path(job["result"]).read_text(encoding="utf-8-sig"))
        if result["status"] != "applied" or result.get("exit_code") not in SUCCESS:
            raise AssertionError(f"PowerShell MSI result: {result}")
        if result["attempted_at"] < exited_at - 1:
            raise AssertionError("Installer result predates the waited-for PID exit")
        # Give any erroneous asynchronous launch time to leave its marker.
        time.sleep(1)
        if marker.exists() or Path(job["restart"]).exists():
            raise AssertionError("PowerShell helper relaunched despite restart being off")
        assert_no_blender()
        return {"ack": True, "waited_for_pid": True, "relaunch": False, "result": result,
                "products": installed_products()}
    except (AssertionError, OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        print(helper_diagnostics(folder, helper, extra=(folder / "msi.log",)), flush=True)
        raise
    finally:
        stop_owned(helper)
        stop_owned(dummy)
        dummy.stdin.close()


def exercise_legacy(folder, installer, out):
    # The compatibility path intentionally preserves cmd.exe's old launch form. Use a simple
    # ASCII path here; the new -File PowerShell path above exercises hostile-looking filenames.
    if any(character in str(folder) for character in ' &%\'"'):
        raise AssertionError("Legacy smoke folder must be a simple Windows path")
    folder.mkdir()
    msi = folder / MSI_NAME
    shutil.copyfile(installer, msi)
    script = folder / "apply.cmd"
    script.write_text(update.HELPER_CMD, encoding="utf-8", newline="\r\n")
    log, marker = folder / "msi.log", folder / "relaunched.txt"
    target = folder / "relaunch.cmd"
    target.write_text('@echo off\r\necho restarted>"' + str(marker) + '"\r\nexit\r\n', encoding="ascii")
    dummy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(15)"])
    helper = None
    try:
        with (out / "legacy-helper.log").open("wb") as output:
            helper = subprocess.Popen(update.legacy_helper_command(str(script), dummy.pid, str(msi), str(target), str(log)),
                                      stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                      env=update.helper_environment())
            time.sleep(3)
            if dummy.poll() is not None or helper.poll() is not None or log.exists() or marker.exists():
                raise AssertionError("Legacy helper did not wait for the live PID")
            dummy.wait(timeout=25)
            exited_at = time.time()
            code = helper.wait(timeout=300)
            wait_for(marker, timeout=10)
        if code != 0:
            raise AssertionError(f"Legacy cmd helper exited {code}")
        raw = log.read_bytes()
        encoding = "utf-16" if raw.startswith(b'\xff\xfe') else "utf-16-le" if b'\x00' in raw[:200] else "utf-8"
        text = raw.decode(encoding, errors="replace")
        codes = re.findall(r"MainEngineThread is returning (\d+)", text)
        if not codes or int(codes[-1]) not in SUCCESS or log.stat().st_mtime < exited_at - 1:
            raise AssertionError("Legacy MSI success/wait evidence is missing")
        assert_no_blender()
        record = {"helper_exit_code": code, "msi_exit_code": int(codes[-1]), "waited_for_pid": True,
                  "relaunch_probe": True, "products": installed_products()}
        write_json(out / "legacy-result.json", record)
        return record
    except (AssertionError, OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        print(helper_diagnostics(folder, helper, extra=(out / "legacy-helper.log", log)), flush=True)
        raise
    finally:
        stop_owned(helper)
        stop_owned(dummy)
        if log.exists():
            shutil.copyfile(log, out / "legacy-msi.log")
        # This exact directory was created above by this test on the disposable runner.
        shutil.rmtree(folder)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("This test installs an MSI and is only for a disposable Windows runner")
    args.out.mkdir(parents=True, exist_ok=False)
    installer, asset = fetch_installer(args.out)
    initial = run_installer(installer, args.out / "initial-msi.log")
    products = installed_products()
    new = exercise_powershell(args.out / "Sam's 50% & Zoë", installer, asset)
    legacy = exercise_legacy(Path(os.environ["SystemRoot"]) / "Temp" / f"loopcut-ci-{os.getpid()}", installer, args.out)
    if new["products"] != products or legacy["products"] != products:
        raise AssertionError("Repair changed the registered product identity")
    write_json(args.out / "summary.json", {"initial_msi_exit_code": initial, "powershell": new, "legacy": legacy})
    print("PASS: verified public MSI, both PID waits, real MSI repair, no Blender launch, product remains installed")


if __name__ == "__main__":
    main()
