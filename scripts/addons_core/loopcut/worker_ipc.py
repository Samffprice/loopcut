"""The protocol between the user's Blender and a worker's background Blender, with no Blender in it.

A worker's model calls stay in the parent; its tools run in a child Blender started on the wave's
spawn copy (07_m4_design §2.7). The two talk over a pair of inherited anonymous pipes: UTF-8 JSON,
one object per line, one request in flight, images passed as file paths. The child's stdout and
stderr go to worker.log, so Blender's own prints never touch the protocol. json.dumps escapes a
newline inside a string, so a line is always one whole message whatever the text holds.

Checked on day 1 (Blender 5.2.2, macOS): Blender keeps fds passed with pass_fds open and prints
nothing into them, so the multiprocessing.connection fallback the design kept in reserve is not
needed.
"""

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

OPS = ("init", "tool", "check", "finish", "exit")
# Parent-side timeouts in seconds. A tool gets the run_python limit plus TOOL_GRACE; looks, imports
# and checks have their own. A timeout or EOF kills the child: the crash path (07 §2.4).
TIMEOUTS = {"init": 60.0, "capture": 90.0, "import": 120.0, "check": 60.0, "finish": 120.0, "exit": 5.0, "bake": 180.0}
TOOL_GRACE = 30.0
CAPTURE_TOOLS = {"capture_viewport"}
CHECK_TOOLS = {"check_brief"}
IMPORT_TOOLS = {"import_asset"}
BAKE_TOOLS = {"bake_paint_masks"}  # 9.7 s on the calibration chest; ray tracing a heavy scene is slower.
# The only variables a child inherits. Everything else, keys above all, stays in the parent: the
# child's model-written code could print any of it.
KEEP_ENV = ("PATH", "HOME", "LANG")
SECRET_NAME = re.compile(r"(^|_)(KEY|TOKEN|SECRET|PASSWORD)S?$", re.IGNORECASE)
WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")  # A folder name, from the orchestrator's brief.
ENV_FILE = "child.env"


class ProtocolError(Exception):
    """A line that is not one JSON object, or a reply that does not answer the request."""


# ------------------------------------------------------------------ framing

def encode(message: dict) -> bytes:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


def decode(line: bytes) -> dict:
    try:
        message = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as ex:
        raise ProtocolError(f"not a JSON line: {ex}") from None
    if not isinstance(message, dict):
        raise ProtocolError(f"expected a JSON object, got {type(message).__name__}")
    return message


class Channel:
    """One side of the pipe pair: binary file objects, one message per line."""

    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer

    @classmethod
    def from_fds(cls, fd_in: int, fd_out: int) -> "Channel":
        return cls(os.fdopen(fd_in, "rb"), os.fdopen(fd_out, "wb"))

    def send(self, message: dict) -> None:
        self.writer.write(encode(message))
        self.writer.flush()

    def receive(self) -> dict | None:
        """The next message, or None at EOF: the other side is gone."""
        line = self.reader.readline()
        if not line:
            return None
        return decode(line)

    def close(self) -> None:
        for stream in (self.writer, self.reader):
            try:
                stream.close()
            except OSError:
                pass


# ------------------------------------------------------------------ messages

@dataclass
class Request:
    id: int
    op: str
    payload: dict = field(default_factory=dict)

    def to_message(self) -> dict:
        return {**self.payload, "id": self.id, "op": self.op}

    @classmethod
    def from_message(cls, message: dict) -> "Request":
        op, number = message.get("op"), message.get("id")
        if op not in OPS:
            raise ProtocolError(f"unknown op {op!r}")
        if type(number) is not int:
            raise ProtocolError("a request needs an integer id")
        return cls(number, op, {k: v for k, v in message.items() if k not in ("id", "op")})


@dataclass
class Reply:
    id: int
    ok: bool = True
    data: dict = field(default_factory=dict)
    error: str = ""

    def to_message(self) -> dict:
        message = {**self.data, "id": self.id, "ok": self.ok}
        if not self.ok:
            message["error"] = self.error or "unknown error"
        return message

    @classmethod
    def from_message(cls, message: dict) -> "Reply":
        number, ok = message.get("id"), message.get("ok")
        if type(number) is not int or type(ok) is not bool:
            raise ProtocolError("a reply needs an integer id and a boolean ok")
        data = {k: v for k, v in message.items() if k not in ("id", "ok", "error")}
        return cls(number, ok, data, str(message.get("error", "")))


def timeout_for(op: str, name: str = "", arguments: dict | None = None, run_timeout: float = 60.0) -> float:
    """Seconds the parent waits for the reply to one request."""
    if op != "tool":
        return TIMEOUTS[op]
    if name in CAPTURE_TOOLS:
        return TIMEOUTS["capture"]
    if name in IMPORT_TOOLS:
        return TIMEOUTS["import"]
    if name in CHECK_TOOLS:
        return TIMEOUTS["check"]
    if name in BAKE_TOOLS:
        return TIMEOUTS["bake"]
    seconds = run_timeout + TOOL_GRACE
    if name == "run_python" and (arguments or {}).get("capture"):
        seconds += TIMEOUTS["capture"]  # One call that is a step and a look.
    return seconds


# ------------------------------------------------------------------ starting a child

def valid_worker_id(worker: str) -> bool:
    return isinstance(worker, str) and bool(WORKER_ID.match(worker))


def worker_dir(spawn_path: Path, worker: str) -> Path:
    """<job>/w<n>/<worker>: next to the wave's spawn copy. Captures, the transcript, the result."""
    return Path(spawn_path).parent / worker


def child_command(binary, spawn_path, script, job, worker: str, fd_in: int, fd_out: int) -> list[str]:
    return [str(binary), "-b", "--factory-startup", "--disable-autoexec", str(spawn_path),
            "--python-exit-code", "1", "--python", str(script),
            "--", "--job", str(job), "--worker", worker, "--in", str(fd_in), "--out", str(fd_out)]


def parse_child_args(argv: list[str]) -> dict:
    """The child's side of child_command: {"job", "worker", "in", "out"} from what follows "--"."""
    if "--" not in argv:
        raise ProtocolError("no arguments after --")
    rest, found = argv[argv.index("--") + 1:], {}
    for flag, value in zip(rest[::2], rest[1::2]):
        found[flag.lstrip("-")] = value
    missing = [k for k in ("job", "worker", "in", "out") if k not in found]
    if missing:
        raise ProtocolError(f"missing {', '.join(missing)}")
    return {"job": found["job"], "worker": found["worker"], "in": int(found["in"]), "out": int(found["out"])}


def is_secret(name: str) -> bool:
    return bool(SECRET_NAME.search(name))


def env_file(job_dir) -> Path:
    return Path(job_dir) / ENV_FILE


def write_env_file(job_dir, run_timeout: float) -> Path:
    """The child's LOOPCUT_ENV_FILE. It holds only the run_python limit; its being set also stops
    config._find_env_file walking up from the package to a developer's .env with keys in it."""
    path = env_file(job_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"LOOPCUT_RUN_TIMEOUT={float(run_timeout):g}\n", encoding="utf-8")
    return path


def child_env(job_dir, worker_folder, environ=None) -> dict[str, str]:
    """The whole environment of a child: PATH, HOME and LANG from the parent, its own TMPDIR,
    LOOPCUT_WORKER=1 and LOOPCUT_ENV_FILE. Nothing named like a key, token or secret."""
    environ = os.environ if environ is None else environ
    env = {name: environ[name] for name in KEEP_ENV if environ.get(name)}
    env.update(TMPDIR=str(Path(worker_folder) / "tmp"), LOOPCUT_WORKER="1",
               LOOPCUT_ENV_FILE=str(env_file(job_dir)))
    return {name: value for name, value in env.items() if not is_secret(name)}
