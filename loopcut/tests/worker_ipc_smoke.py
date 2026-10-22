"""Plain-Python worker IPC smoke test; no Blender or third-party dependencies.

Run on Windows (Python 3.10+): py loopcut/tests/worker_ipc_smoke.py
Run on macOS/Linux: python3 loopcut/tests/worker_ipc_smoke.py
Exercises Child.start, authentication, init, Unicode/large replies, EOF and process cleanup.
Only the Blender command is substituted with this script's plain Python child.
"""

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))
from loopcut import worker_host, worker_ipc  # noqa: E402


def serve():
    args = worker_ipc.parse_child_args(sys.argv)
    with sys.stdin.buffer as credentials:
        token = worker_ipc.read_token(credentials)
    channel = worker_ipc.connect(args["port"], token)
    try:
        print("Child stdout belongs in worker.log", flush=True)
        while (request := channel.receive()) is not None:
            channel.send({"id": request["id"], "ok": True, "echo": request.get("text"), "pid": os.getpid()})
            if request["op"] == "exit":
                return
    finally:
        channel.close()


def smoke():
    original = worker_ipc.child_command

    def command(*args):
        blender_args = original(*args)
        return [sys.executable, str(Path(__file__).resolve()), *blender_args[blender_args.index("--"):]]

    with tempfile.TemporaryDirectory(prefix="loopcut IPC ü ") as directory:
        folder = Path(directory)
        child = worker_host.Child(folder, "smoke", folder / "w1" / "spawn.blend", "modeling",
                                  binary=sys.executable, run_timeout=5)
        try:
            with mock.patch.object(worker_ipc, "child_command", side_effect=command):
                child.start()
            assert child.call("init")["pid"] == child.pid
            payload = "Unicode ü ✓\n" + "x" * (1024 * 1024)
            assert child.call("tool", {"text": payload})["echo"] == payload
            child.exit()
            assert child.process.returncode == 0
            assert "Child stdout belongs in worker.log" in (child.folder / "worker.log").read_text(encoding="utf-8")
        finally:
            child.kill()
    print(f"PASS: worker IPC round trip, log isolation and cleanup ({sys.platform})")


if __name__ == "__main__":
    serve() if "--" in sys.argv else smoke()
