"""Detached budget/cancellation supervisor. Invoked by jobs.launch, never registered as an addon."""

import os
import signal
import subprocess
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import jobs


def supervise(path: Path) -> int:
    spec = jobs.spec_at(path)
    state = jobs.read_json(path / "status.json")
    started = time.monotonic()
    child = None

    def finish(status, error=""):
        jobs.atomic_json(path / "status.json", {**state, "state": status, "error": error,
                         "updated": time.time(), "elapsed_seconds": round(time.monotonic() - started, 2)})

    try:
        finish("running")
        command = [spec["binary"], "--background", "--factory-startup", "--disable-autoexec",
                   "--python-exit-code", "1", "--python", str(Path(__file__).with_name("job_worker.py")),
                   "--", str(path)]
        with (path / "worker.log").open("ab") as log:
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                     start_new_session=True)
        jobs.atomic_json(path / "lease" / "child.json", {"pid": child.pid})
        while True:
            (path / "lease" / "heartbeat").touch()
            cancelled = (path / "cancel").exists()
            exhausted = time.monotonic() - started >= spec["budget_seconds"]
            if cancelled or exhausted:
                if child.poll() is None:
                    try:
                        if os.name == "posix":
                            os.killpg(child.pid, signal.SIGTERM)
                        else:
                            child.terminate()
                    except ProcessLookupError:
                        pass  # Exited between poll and terminate; reap it below.
                    try:
                        child.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
                finish("cancelled" if cancelled else "failed",
                       "Cancelled by user." if cancelled else "Time budget exhausted; verified frames can be resumed.")
                return 0
            code = child.poll()
            if code is not None:
                progress = jobs.read_json(path / "progress.json") if (path / "progress.json").exists() else {}
                complete = code == 0 and progress.get("verified_complete") is True
                finish("complete" if complete else "failed",
                       "" if complete else progress.get("error", f"Renderer exited with code {code}; see worker.log."))
                return 0 if complete else 1
            time.sleep(0.2)
    except Exception:
        traceback.print_exc()
        if child and child.poll() is None:
            child.kill()
            child.wait()
        finish("failed", "Job supervisor failed; see supervisor.log.")
        return 1
    finally:
        lease = path / "lease"
        for record in lease.iterdir():
            record.unlink()
        lease.rmdir()


if __name__ == "__main__":
    code = supervise(Path(sys.argv[sys.argv.index("--") + 1]))
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)  # No UI shutdown/handlers; all files are flushed before exiting.
