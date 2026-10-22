"""The protocol between the user's Blender and a worker's background Blender, with no Blender in it.

A worker's model calls stay in the parent; its tools run in a child Blender started on the wave's
spawn copy (07_m4_design §2.7). The two talk over an authenticated loopback socket: UTF-8 JSON,
one object per line, one request in flight, images passed as file paths. The child's stdout and
stderr go to worker.log, so Blender's own prints never touch the protocol. json.dumps escapes a
newline inside a string, so a line is always one whole message whatever the text holds.

Only redirected standard streams are inherited. A fresh random token, delivered over stdin,
admits one child, after which the listener closes. The transport works on Windows, macOS and Linux.
"""

import json
import os
import re
import secrets
import socket
import time
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
KEEP_ENV = ("PATH", "HOME", "LANG", "SYSTEMROOT", "WINDIR", "USERPROFILE", "APPDATA", "LOCALAPPDATA")
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
    """Binary streams over a socket, one message per line."""

    def __init__(self, reader, writer, connection=None):
        self.reader, self.writer = reader, writer
        self.connection = connection
        self.closing = False

    @classmethod
    def from_socket(cls, connection) -> "Channel":
        connection.settimeout(None)  # Buffered readline must not time out part way through a message.
        connection.set_inheritable(False)
        return cls(connection.makefile("rb"), connection.makefile("wb"), connection)

    def send(self, message: dict) -> None:
        self.writer.write(encode(message))
        self.writer.flush()

    def receive(self) -> dict | None:
        """The next message, or None at EOF: the other side is gone."""
        try:
            line = self.reader.readline()
        except (ConnectionResetError, ConnectionAbortedError):
            return None  # Windows reports a peer that died as a reset, not as EOF (CI, 2026-10-22).
        except OSError:
            if self.closing:
                return None  # Windows fails a read blocked across our own shutdown (WinError 10060) instead of EOF.
            raise
        if not line:
            return None
        return decode(line)

    def close(self) -> None:
        # Wake a blocked reader before closing its buffered stream (which otherwise waits for it).
        self.closing = True
        if self.connection is not None:
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for stream in (self.writer, self.reader):
            try:
                stream.close()
            except OSError:
                pass
        if self.connection is not None:
            self.connection.close()


def _receive_exact(connection, length: int) -> bytes:
    data = b""
    timeout = connection.gettimeout()
    deadline = time.monotonic() + timeout if timeout is not None else None
    while len(data) < length:
        if deadline is not None:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("worker authentication timed out")
            connection.settimeout(left)
        part = connection.recv(length - len(data))
        if not part:
            raise ProtocolError("connection closed during authentication")
        data += part
    return data


class Listener:
    """A single-use, loopback-only rendezvous. Unauthenticated clients never reach the JSON parser."""

    def __init__(self):
        self.token = secrets.token_hex(32)
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.socket.bind(("127.0.0.1", 0))
            self.socket.listen(4)
            self.port = self.socket.getsockname()[1]
        except BaseException:
            self.close()
            raise

    def accept(self, process, timeout: float, check_cancelled=None) -> Channel:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if check_cancelled is not None:
                check_cancelled()
            if process.poll() is not None:
                raise ProtocolError(f"child exited before connecting (code {process.returncode})")
            self.socket.settimeout(min(0.1, max(0.001, deadline - time.monotonic())))
            try:
                connection, _ = self.socket.accept()
            except socket.timeout:
                continue
            try:
                connection.settimeout(min(1.0, max(0.001, deadline - time.monotonic())))
                token = _receive_exact(connection, len(self.token))
                if not secrets.compare_digest(token, self.token.encode("ascii")):
                    connection.close()
                    continue
                connection.sendall(b"OK")
                channel = Channel.from_socket(connection)
            except (OSError, ProtocolError):
                connection.close()
                continue
            self.close()  # The token cannot admit another connection.
            return channel
        raise TimeoutError("worker connection timed out")

    def close(self) -> None:
        self.socket.close()


def connect(port: int, token: str, timeout: float = TIMEOUTS["init"]) -> Channel:
    connection = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        connection.sendall(token.encode("ascii"))
        if _receive_exact(connection, 2) != b"OK":
            raise ProtocolError("worker authentication failed")
        return Channel.from_socket(connection)
    except BaseException:
        connection.close()
        raise


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
    # Windows device names are reserved even with an extension, including directory names.
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    return isinstance(worker, str) and bool(WORKER_ID.fullmatch(worker)) and worker.upper() not in reserved


def worker_dir(spawn_path: Path, worker: str) -> Path:
    """<job>/w<n>/<worker>: next to the wave's spawn copy. Captures, the transcript, the result."""
    return Path(spawn_path).parent / worker


def child_command(binary, spawn_path, script, job, worker: str, port: int) -> list[str]:
    # Open the scene only after authentication: a broken scene is not a machine-wide IPC failure.
    return [str(binary), "-b", "--factory-startup", "--disable-autoexec",
            "--python-exit-code", "1", "--python", str(script),
            "--", "--job", str(job), "--worker", worker, "--spawn", str(spawn_path), "--port", str(port),
            "--parent", str(os.getpid())]


def parse_child_args(argv: list[str]) -> dict:
    """The child's side of child_command: paths, port and the original parent PID. No secrets."""
    if "--" not in argv:
        raise ProtocolError("no arguments after --")
    rest, found = argv[argv.index("--") + 1:], {}
    if len(rest) % 2:
        raise ProtocolError("missing argument value")
    for flag, value in zip(rest[::2], rest[1::2]):
        found[flag.lstrip("-")] = value
    missing = [k for k in ("job", "worker", "spawn", "port", "parent") if k not in found]
    if missing:
        raise ProtocolError(f"missing {', '.join(missing)}")
    try:
        port, parent = int(found["port"]), int(found["parent"])
        if not 0 < port < 65536 or parent <= 0:
            raise ValueError()
        if not valid_worker_id(found["worker"]):
            raise ValueError()
    except ValueError:
        raise ProtocolError("invalid worker connection arguments") from None
    return {**found, "port": port, "parent": parent}


def read_token(stream) -> str:
    """Read the fixed-size bootstrap secret from redirected binary stdin, never argv or env."""
    token = stream.readline(66)
    if not re.fullmatch(rb"[0-9a-f]{64}\n", token):
        raise ProtocolError("invalid worker authentication input")
    return token[:-1].decode("ascii")


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
    """The whole environment of a child: OS paths/locale from the parent, its own temp directory,
    LOOPCUT_WORKER=1 and LOOPCUT_ENV_FILE. Nothing named like a key, token or secret."""
    environ = os.environ if environ is None else environ
    env = {name.upper(): value for name, value in environ.items() if name.upper() in KEEP_ENV and value}
    temporary = str(Path(worker_folder) / "tmp")
    env.update(TMPDIR=temporary, TMP=temporary, TEMP=temporary, LOOPCUT_WORKER="1",
               LOOPCUT_ENV_FILE=str(env_file(job_dir)))
    return {name: value for name, value in env.items() if not is_secret(name)}
