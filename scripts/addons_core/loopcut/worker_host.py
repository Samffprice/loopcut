"""The parent side of a worker: its background Blender, the calls into it, and where each tool runs.

A worker's model calls stay in the user's Blender, where the keys are; its scene tools run in a
child Blender opened on the wave's spawn copy (07_m4_design §2.7). Child owns the process: it
starts it with an authenticated loopback connection, reads replies on a thread, and turns a timeout, EOF or a
garbled reply into ChildCrashed after killing it. WorkerRuntime is what the worker loop calls
instead of tools.execute: network tools and report_back in the parent, scene tools in the child,
import_asset split between them (fetch here, load there).

A child dies with its parent: EOF on its connection, or a watchdog seeing its parent exit. One
stuck in a long native call can miss both, so reap_orphans kills what an earlier Blender left
behind, and shutdown_all (atexit, and the add-on's unregister) kills this one's.

A child that dies under a call leaves a crash kit, <worker>/crash-<n>/ (Child._keep): Blender's own
crash report, which it writes to the child's TMPDIR and so would go with it, the last .blend the
child had, the tail of worker.log and crash.json. The job's cleanup (waves.reap_wave) and an eval's
copy of the job keep it.
"""

import atexit
import json
import os
import queue
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from . import worker_ipc

CHILD_SCRIPT = Path(__file__).with_name("worker_child.py")
# Where each worker tool runs. import_asset is both: fetch in the parent, load in the child.
CHILD_TOOLS = {"run_python", "get_scene_info", "get_object_info", "inspect_api", "check_contact", "frame_camera",
               "capture_viewport", "check_brief", "inspect_uv", "uv_unwrap", "inspect_texture", "bake_paint_masks",
               "stylize_material"}
PARENT_TOOLS = {"search_assets", "look_at_reference", "report_back"}
KILL_GRACE = 2.0     # Terminate, then force-kill after this many seconds (POSIX).
POLL = 0.1           # How often a waiting call looks at is_cancelled.
RECORD = "child.json"
CRASH_NOTE = ("Your Blender crashed during {tool}; the scene is back at the start of your brief, and nothing you "
              "built is kept.")
UNAVAILABLE_NOTE = ("New workers are unavailable on this machine because startup failed. "
                    "Starting workers is disabled for this session.")
CRASH_KIT = "crash-{n}"   # In the worker's folder, one per crash: what Child._keep saves.
CRASH_BLEND_MB = 200      # The last .blend is copied into the kit up to this size; a bigger one is only named.
LOG_TAIL = 80             # Lines of worker.log kept beside the crash report.

_live = set()        # Children started and not yet reaped: what shutdown_all kills.
_live_lock = threading.Lock()


class ChildCrashed(Exception):
    """The child is gone or stopped answering. It has been killed; the wave's crash path takes over.
    kept: its crash kit's folder (Child._keep), or "" when none was kept."""

    def __init__(self, reason: str, detail: str = "", kept: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason, self.detail, self.kept = reason, detail, kept


class WorkerUnavailable(Exception):
    """An explicitly classified spawn/transport failure before scene or add-on initialization."""


def _binary() -> str:
    import bpy
    return bpy.app.binary_path


class Child:
    """One worker's background Blender. call() is used from one thread at a time: one request in flight."""

    def __init__(self, job_dir, worker_id: str, spawn_path, role: str, *, binary: str | None = None,
                 script: Path | None = None, run_timeout: float | None = None):
        if not worker_ipc.valid_worker_id(worker_id):
            raise ValueError(f"worker id {worker_id!r} must be a portable folder name using letters, digits, _ or -")
        self.job_dir, self.worker_id, self.role = Path(job_dir), worker_id, role
        self.spawn_path = Path(spawn_path)
        self.folder = worker_ipc.worker_dir(self.spawn_path, worker_id)
        self.binary = binary or _binary()
        self.script = Path(script or CHILD_SCRIPT)
        if run_timeout is None:
            from . import config
            run_timeout = config.run_timeout()
        self.run_timeout = float(run_timeout)
        self.process: subprocess.Popen | None = None
        self.channel: worker_ipc.Channel | None = None
        self.replies: queue.Queue = queue.Queue()
        self.next_id = 1
        self.init_payload: dict | None = None
        self.ready: dict | None = None     # The init reply.
        self.startup_deadline: float | None = None  # One budget for spawn, connection and init.
        self.peak_rss_mb = 0.0             # From the child's own getrusage, reported with every reply.
        self.t = {}                        # time.time() of spawn, ready, end.
        self.dead = ""                     # Why the child is gone, once it is.
        self.request = ("", "")            # The op and tool of the call in flight, for a crash kit.
        self.stopping = ""                 # Why we kill it, set before the signal: the EOF that follows is no crash.

    # ------------------------------------------------------------------ process

    def start(self, is_cancelled=None) -> "Child":
        def check_cancelled():
            if is_cancelled is not None and is_cancelled():
                from . import llm
                raise llm.Cancelled()

        listener = None
        try:
            check_cancelled()
            self.startup_deadline = time.monotonic() + worker_ipc.TIMEOUTS["init"]
            self.folder.mkdir(parents=True, exist_ok=True)
            (self.folder / "tmp").mkdir(exist_ok=True)
            worker_ipc.write_env_file(self.job_dir, self.run_timeout)
            try:
                listener = worker_ipc.Listener()
            except OSError as ex:
                raise WorkerUnavailable(f"could not listen for worker: {ex}") from ex
            command = worker_ipc.child_command(self.binary, self.spawn_path, self.script, self.job_dir,
                                               self.worker_id, listener.port)
            with (self.folder / "worker.log").open("ab") as log:
                environment = worker_ipc.child_env(self.job_dir, self.folder)
                try:
                    self.process = subprocess.Popen(
                        command, close_fds=True, stdin=subprocess.PIPE, stdout=log,
                        stderr=subprocess.STDOUT, env=environment)
                except (OSError, ValueError) as ex:
                    raise WorkerUnavailable(f"could not spawn worker: {ex}") from ex
            self.t["spawn"] = time.time()
            with _live_lock:
                _live.add(self)  # Shutdown/cancellation must also reap a child still connecting.
            created = None
            if os.name == "nt":
                from .worker_process import windows_creation
                created = windows_creation(self.process.pid)
            _atomic_json(self.folder / RECORD, {"pid": self.process.pid, "owner": os.getpid(), "job": str(self.job_dir),
                                                "worker": self.worker_id, "started": self.t["spawn"],
                                                "windows_created": created})
            try:
                # 65 bytes fit in an empty pipe on every platform; close immediately to deliver EOF.
                with self.process.stdin as credentials:
                    credentials.write((listener.token + "\n").encode("ascii"))
                self.channel = listener.accept(self.process, max(0.0, self.startup_deadline - time.monotonic()),
                                               check_cancelled)
            except (OSError, worker_ipc.ProtocolError) as ex:
                raise WorkerUnavailable(f"could not connect worker: {ex}") from ex
            threading.Thread(target=self._read, name=f"loopcut-child-{self.worker_id}", daemon=True).start()
        except BaseException:
            self.kill(reason="startup failed")
            raise
        finally:
            if self.process is not None and self.process.stdin is not None:
                self.process.stdin.close()
            if listener is not None:
                listener.close()
        return self

    def _read(self) -> None:
        channel = self.channel
        while True:
            try:
                message = channel.receive()
            except worker_ipc.ProtocolError as ex:
                self.replies.put(ex)
                break
            except (OSError, ValueError):  # The connection broke: the same as EOF.
                message = None
            if message is None:
                self.replies.put(None)
                break
            self.replies.put(message)
        try:
            channel.reader.close()  # EOF or a protocol error: release the read stream.
        except OSError:
            pass

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process else None

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None and not self.dead

    def rss_mb(self) -> float | None:
        """Resident memory now, from ps; None when unsupported or the child is gone."""
        if os.name == "nt" or not self.alive():
            return None
        try:
            out = subprocess.run(["ps", "-o", "rss=", "-p", str(self.pid)], capture_output=True, text=True, timeout=5)
            return round(int(out.stdout.strip()) / 1024, 1)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None

    def kill(self, grace: float = KILL_GRACE, reason: str = "killed") -> None:
        kill_all([self], grace, reason)

    def _gone(self, reason: str) -> None:
        self.dead = self.dead or reason
        self.t.setdefault("end", time.time())
        if self.channel:
            self.channel.close()
        with _live_lock:
            _live.discard(self)

    # ------------------------------------------------------------------ calls

    def call(self, op: str, payload: dict | None = None, timeout: float | None = None, is_cancelled=None) -> dict:
        """Send one request and wait for its reply: the reply's fields, with ok and maybe error.
        Raises ChildCrashed (the child is killed first) on a timeout, EOF or a reply out of step,
        and llm.Cancelled (the child is killed too: it is mid-call) when is_cancelled() turns true."""
        if self.dead or self.process is None:
            raise ChildCrashed("gone", self.dead or "not started")
        payload = dict(payload or {})
        if op == "init":
            payload.setdefault("role", self.role)
            payload.setdefault("run_timeout", self.run_timeout)
            self.init_payload = payload
        if timeout is None:
            timeout = worker_ipc.timeout_for(op, payload.get("name", ""), payload.get("arguments"), self.run_timeout)
        if op == "init" and self.startup_deadline is not None:
            timeout = min(timeout, max(0.0, self.startup_deadline - time.monotonic()))
        self.request = (op, str(payload.get("name") or ""))
        number, self.next_id = self.next_id, self.next_id + 1
        try:
            self.channel.send(worker_ipc.Request(number, op, payload).to_message())
        except (OSError, ValueError) as ex:
            self._crash("exited", f"could not send {op}: {ex}")
        deadline = time.monotonic() + timeout
        while True:
            if is_cancelled is not None and is_cancelled():
                from . import llm
                self.kill(reason="cancelled")
                raise llm.Cancelled()
            left = deadline - time.monotonic()
            if left <= 0:
                self._crash("timeout", f"no reply to {op} in {timeout:g} s")
            try:
                message = self.replies.get(timeout=min(POLL, left))
            except queue.Empty:
                continue
            if message is None:
                self._crash("exited", self._exit_detail())
            if isinstance(message, Exception):
                self._crash("protocol", str(message))
            try:
                reply = worker_ipc.Reply.from_message(message)
            except worker_ipc.ProtocolError as ex:
                self._crash("protocol", str(ex))
            if reply.id != number:
                self._crash("protocol", f"reply {reply.id} to request {number}")
            peak = reply.data.get("peak_rss_mb")
            if isinstance(peak, (int, float)):
                self.peak_rss_mb = max(self.peak_rss_mb, float(peak))
            if op == "init" and reply.ok:
                self.ready = reply.data
                self.t["ready"] = time.time()
            elif op == "init":
                self._crash("init", reply.error)
            return {**reply.data, "ok": reply.ok, **({} if reply.ok else {"error": reply.error})}

    def _exit_detail(self) -> str:
        try:
            code = self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            return "closed its connection"
        return f"exited with code {code}" + ("; see worker.log" if code else "")

    def _crash(self, reason: str, detail: str):
        ours = self.request[0] == "exit" or self.stopping  # Asked to leave, or killed (Stop, Cancel): no crash kit.
        self.kill(reason=reason)
        kept = "" if ours else self._keep(reason, detail)
        raise ChildCrashed(reason, detail, kept)

    def _keep(self, reason: str, detail: str) -> str:
        """The crash kit of a child that just died: <worker>/crash-<n>/ with Blender's crash report
        (*.crash.txt, moved out of the child's TMPDIR, which the job's cleanup deletes; a child we
        killed after a timeout writes none), the last .blend it had (_last_blend), worker.log's last
        LOG_TAIL lines and crash.json: what it was doing and what the kit holds. Its folder, or ""."""
        op, tool = self.request
        try:
            number = 1 + sum(1 for path in self.folder.glob(CRASH_KIT.format(n="*")) if path.is_dir())
            kit = self.folder / CRASH_KIT.format(n=number)
            kit.mkdir(parents=True)
            reports = sorted((self.folder / "tmp").glob("*.crash.txt"))
            for report in reports:  # Moved: the next child in this folder writes its own.
                shutil.move(str(report), str(kit / report.name))
            blend, blend_note = self._last_blend(kit)
            (kit / "worker.log.tail.txt").write_text(_tail(self.folder / "worker.log", LOG_TAIL), encoding="utf-8")
            code = self.process.poll() if self.process is not None else None
            _atomic_json(kit / "crash.json", {
                "worker": self.worker_id, "op": op, "tool": tool, "reason": reason, "detail": detail, "exit_code": code,
                "pid": self.pid, "time": time.time(), "report": [r.name for r in reports],
                **({} if reports else {"report_note": "Blender wrote no crash report"
                                       + (" (the child was killed, it did not crash)" if reason != "exited" else "")}),
                "blend": blend, "blend_note": blend_note, "log_tail": "worker.log.tail.txt"})
            return str(kit)
        except OSError as ex:
            print(f"Loopcut: could not keep the crash of worker {self.worker_id}: {ex}")
            return ""

    def _last_blend(self, kit: Path) -> tuple:
        """(file name in the kit or None, what it is): the per-call copy worker_child saved before its
        last scene-changing call (undo "save"), else the spawn copy it opened, the start of its brief,
        on which its conversation.json replays; not copied over CRASH_BLEND_MB."""
        percall = self.folder / "percall.blend"
        if percall.is_file() and percall.stat().st_mtime >= self.t.get("spawn", 0):
            source, what = percall, "the scene before its last run_python, import_asset or frame_camera call"
        else:
            source, what = self.spawn_path, ("the scene its Blender opened, the start of its brief (no per-call "
                                              "copy); replay conversation.json's calls on it")
        if not source.is_file():
            return None, f"no .blend: {source} is gone"
        size = source.stat().st_size
        if size > CRASH_BLEND_MB * 2 ** 20:
            return None, f"not copied: {source} ({what}) is {size / 2 ** 20:.0f} MB, over {CRASH_BLEND_MB} MB"
        shutil.copy2(source, kit / source.name)
        return source.name, what

    def exit(self, timeout: float = 5.0) -> None:
        """Ask the child to leave; kill it if it does not."""
        if self.alive():
            try:
                self.call("exit", timeout=timeout)
            except ChildCrashed:
                pass
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                pass
        self.kill(reason="exited")


def kill_all(children, grace: float = KILL_GRACE, reason: str = "killed") -> None:
    """Terminate every live child at once, then force-kill whatever is left after `grace` seconds: a
    wave's children go down together, not one grace period after another."""
    live = [c for c in children if c.process is not None and c.process.poll() is None]
    for child in live:
        child.stopping = child.stopping or reason
        try:
            child.process.terminate()  # SIGTERM on POSIX, TerminateProcess on Windows.
        except OSError:
            pass
    deadline = time.monotonic() + grace
    for child in live:
        try:
            child.process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                child.process.kill()
            except OSError:
                pass
            child.process.wait()
    for child in children:
        if child.process is not None:
            child._gone(reason)


def shutdown_all() -> None:
    """Kill every child this Blender started: at exit, and when the add-on is disabled."""
    with _live_lock:
        children = list(_live)
    kill_all(children, reason="shutdown")


atexit.register(shutdown_all)


# ------------------------------------------------------------------ orphans

def _alive(pid) -> bool:
    if type(pid) is not int or pid <= 0:
        return False
    from . import jobs
    return jobs._alive(pid)  # Uses a process handle on Windows; os.kill(pid, 0) can terminate it there.


def _command_of(pid: int) -> str:
    if os.name == "nt":
        return ""
    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip()


def reap_orphans(root) -> list[int]:
    """Kill worker Blenders an earlier Blender left running: a <job>/w<n>/<worker>/child.json under
    `root` whose owner is dead while its child still runs worker_child.py for that job (POSIX), or
    still has the recorded process creation time (Windows). A child of
    a Blender that is still alive (another window, another eval) is never touched. Returns the pids
    killed."""
    killed = []
    for record in Path(root).glob(f"*/w*/*/{RECORD}"):
        try:
            data = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        pid, owner = data.get("pid"), data.get("owner")
        if owner == os.getpid() or _alive(owner):
            continue
        if os.name == "nt":
            from .worker_process import windows_kill
            if windows_kill(pid, data.get("windows_created")):
                killed.append(pid)
        elif _alive(pid):
            command = _command_of(pid)
            if CHILD_SCRIPT.name not in command or f"--job {data.get('job')}" not in command:
                continue
            try:
                os.kill(pid, signal.SIGKILL)
                killed.append(pid)
            except OSError:
                continue
        try:
            record.unlink()
        except OSError:
            pass
    return killed


def _tail(path: Path, lines: int) -> str:
    """The last `lines` lines of a text file ("" when it is not there), from its last 256 KB."""
    try:
        with open(path, "rb") as file:
            file.seek(max(0, file.seek(0, os.SEEK_END) - 256 * 1024))
            data = file.read()
    except OSError:
        return ""
    return "\n".join(data.decode("utf-8", "replace").splitlines()[-lines:]) + "\n"


def _atomic_json(path: Path, data: dict) -> None:
    partial = path.with_name(path.name + ".partial")
    partial.write_text(json.dumps(data), encoding="utf-8")
    os.replace(partial, path)


# ------------------------------------------------------------------ the worker's tools

class WorkerRuntime:
    """run_tool for a worker loop. parent_tools maps a tool the parent runs to a callable:
    search_assets, look_at_reference and report_back take the arguments JSON and return a
    ToolResult (run them on the main thread as tools.execute would); "fetch_asset", if given,
    takes import_asset's arguments as a dict and returns assets.fetch_asset's record (default:
    assets.fetch_asset on the calling thread). Results carry .changed, .rolled_back and .advisory
    from the child; a crash is a failed result with .crashed set, and restart() starts over from
    the pristine spawn copy."""

    def __init__(self, child: Child, parent_tools: dict):
        self.child, self.parent_tools = child, dict(parent_tools)
        self.step = 0           # The worker loop's step, sent with each call; the loop may set it.
        self.calls = 0
        self.rollbacks = 0
        self.crashed = ""       # Why the child died, until restart().
        self.restarts = 0

    def run_tool(self, name: str, arguments_json: str, is_cancelled=None):
        from .tools import ToolResult
        try:
            arguments = json.loads(arguments_json) if arguments_json and arguments_json.strip() else {}
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be a JSON object")
        except ValueError as ex:
            return _marked(ToolResult(f"Invalid tool arguments: {ex}", ok=False))
        self.calls += 1
        if name in PARENT_TOOLS:
            run = self.parent_tools.get(name)
            if run is None:
                return _marked(ToolResult(f"{name} is not available to this worker.", ok=False))
            return _marked(run(arguments_json))
        if name == "import_asset":
            return self._import(arguments, is_cancelled)
        if name in CHILD_TOOLS:
            return self._in_child(name, arguments, {}, is_cancelled)
        available = sorted(CHILD_TOOLS | {"import_asset"} | (PARENT_TOOLS & set(self.parent_tools)))
        return _marked(ToolResult(f"Unknown tool {name!r}. Available: {', '.join(available)}", ok=False))

    def _import(self, arguments: dict, is_cancelled):
        from .tools import ToolError, ToolResult
        fetch = self.parent_tools.get("fetch_asset")
        try:
            if fetch is None:
                from . import assets
                fetched = assets.fetch_asset(**arguments)
            else:
                fetched = fetch(arguments)
        except ToolError as ex:
            return _marked(ToolResult(str(ex), ok=False))
        except TypeError as ex:
            return _marked(ToolResult(f"Bad arguments for import_asset: {ex}", ok=False))
        if is_cancelled is not None and is_cancelled():
            from . import llm
            raise llm.Cancelled()
        return self._in_child("import_asset", arguments, {"fetched": fetched}, is_cancelled)

    def _in_child(self, name: str, arguments: dict, extra: dict, is_cancelled):
        from .tools import ToolResult
        if self.crashed:
            return _marked(ToolResult(f"Your Blender is not running ({self.crashed}).", ok=False), crashed=self.crashed)
        request = {"name": name, "arguments": arguments, "step": self.step or self.calls, **extra}
        try:
            reply = self.child.call("tool", request, is_cancelled=is_cancelled)
        except ChildCrashed as ex:
            self.crashed = ex.reason
            text = f"Your Blender stopped during {name} ({ex})."
            return _marked(ToolResult(text, ok=False), crashed=ex.reason, kept=ex.kept)
        if not reply["ok"]:
            return _marked(ToolResult(f"{name} failed in the worker's Blender: {reply.get('error')}", ok=False))
        image = reply.get("image")
        result = ToolResult(reply.get("text", ""), image_path=Path(image) if image else None,
                            ok=bool(reply.get("tool_ok", True)))
        result.ms = reply.get("ms")
        if reply.get("rolled_back"):
            self.rollbacks += 1
        return _marked(result, changed=bool(reply.get("changed")), rolled_back=reply.get("rolled_back"),
                       advisory=reply.get("advisory"))

    def restart(self, is_cancelled=None) -> dict:
        """A new child on the same pristine spawn copy, initialised as the first was: the crash
        retry. Everything the dead child built is gone; the caller tells the worker so (CRASH_NOTE)."""
        old = self.child
        old.kill(reason=old.dead or "restarted")
        child = Child(old.job_dir, old.worker_id, old.spawn_path, old.role, binary=old.binary, script=old.script,
                      run_timeout=old.run_timeout).start(is_cancelled=is_cancelled)
        reply = child.call("init", old.init_payload or {}, is_cancelled=is_cancelled)
        child.peak_rss_mb = max(child.peak_rss_mb, old.peak_rss_mb)
        self.child, self.crashed = child, ""
        self.restarts += 1
        return reply


def _marked(result, changed: bool = False, rolled_back=None, advisory=None, crashed: str = "", kept: str = ""):
    """The attributes the worker loop reads off every result, whatever ran it. kept: a crashed call's
    crash kit (Child._keep)."""
    result.changed, result.rolled_back, result.advisory, result.crashed = changed, rolled_back, advisory, crashed
    result.crash_kept = kept
    return result
