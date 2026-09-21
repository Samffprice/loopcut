"""Durable render records. Pure filesystem/process code; no Blender or chat lifetime.

spec.json is immutable. The supervisor alone writes status.json, the renderer alone writes
progress.json. A per-attempt lease prevents two renderers from writing the same output.
Cancellation is a file, never a signal sent to an unverified PID from a previous launch.
"""

import hashlib
import json
import os
import re
import subprocess
import struct
import time
import uuid
import zlib
from pathlib import Path

ACTIVE = {"queued", "running", "cancelling"}
TERMINAL = {"complete", "cancelled", "failed", "interrupted"}
_ID = re.compile(r"[0-9a-f]{32}\Z")
HEARTBEAT_TIMEOUT = 30
_children = []


class JobError(Exception):
    pass


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, indent=1, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise JobError(f"Invalid job record: {path.name}")
    return value


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def verify_png(path: Path, width: int, height: int) -> None:
    """Validate our 8-bit RGBA PNG stream, including CRCs and complete compressed scanlines.

    Blender's image loader tolerates some truncated files; a header/dimension check is insufficient
    for deciding to skip an expensive frame on resume. Decompression is bounded to one row at a time.
    """
    decoder, decoded, ended = zlib.decompressobj(), 0, False
    row_bytes = width * 4 + 1
    with path.open("rb") as handle:
        if handle.read(8) != b"\x89PNG\r\n\x1a\n":
            raise JobError("Invalid PNG signature.")
        first = True
        while True:
            header = handle.read(8)
            if len(header) != 8:
                raise JobError("Truncated PNG.")
            length, kind = struct.unpack(">I4s", header)
            if length > 1024 * 1024:
                raise JobError("Invalid PNG chunk length.")
            data, crc = handle.read(length), handle.read(4)
            if len(data) != length or len(crc) != 4 or zlib.crc32(kind + data) != struct.unpack(">I", crc)[0]:
                raise JobError("PNG checksum mismatch.")
            if first:
                if kind != b"IHDR" or len(data) != 13 or struct.unpack(">IIBBBBB", data) != (width, height, 8, 6, 0, 0, 0):
                    raise JobError("Unexpected PNG dimensions or encoding.")
                first = False
            if kind == b"IDAT":
                while data:
                    try:
                        decoded += len(decoder.decompress(data, row_bytes))
                    except zlib.error as ex:
                        raise JobError("Invalid PNG pixel stream.") from ex
                    data = decoder.unconsumed_tail
                    if decoded > row_bytes * height:
                        raise JobError("PNG expands past expected dimensions.")
            if kind == b"IEND":
                ended = length == 0 and not handle.read(1)
                break
        if not ended or not decoder.eof or decoder.unused_data or decoded != row_bytes * height:
            raise JobError("Incomplete PNG pixels.")


def reap() -> None:
    _children[:] = [child for child in _children if child.poll() is None]


def integer(value, name: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise JobError(f"{name} must be an integer from {low} to {high}.")
    return value


def validate_spec(spec: dict) -> None:
    if spec.get("version") != 1 or not _ID.fullmatch(str(spec.get("id", ""))):
        raise JobError("Unsupported job record.")
    frames = spec.get("frames")
    if not isinstance(frames, list) or not 1 <= len(frames) <= 10000:
        raise JobError("Request 1 to 10000 frames.")
    for frame in frames:
        integer(frame, "frame", -1048574, 1048574)
    if len(set(frames)) != len(frames) or frames != sorted(frames):
        raise JobError("Frames must be unique and increasing.")
    for field, low, high in (("width", 4, 8192), ("height", 4, 8192), ("samples", 1, 4096),
                             ("budget_seconds", 1, 86400), ("fps", 1, 240)):
        integer(spec.get(field), field, low, high)
    base = spec.get("fps_base")
    if type(base) not in (int, float) or not 0.1 <= base <= 10:
        raise JobError("fps_base must be between 0.1 and 10.")
    if spec.get("format") not in {"PNG", "MP4"}:
        raise JobError("format must be PNG or MP4 (retains the PNG sequence).")
    if spec["format"] == "MP4" and (spec["width"] % 2 or spec["height"] % 2):
        raise JobError("MP4 requires even width and height.")
    if spec.get("engine") not in {"BLENDER_EEVEE", "CYCLES", "BLENDER_WORKBENCH"}:
        raise JobError("Durable jobs support EEVEE, Cycles and Workbench.")
    for field in ("camera", "scene", "output", "binary", "source", "session_id"):
        if not isinstance(spec.get(field), str):
            raise JobError(f"Invalid {field} in job record.")
    if not Path(spec["output"]).is_absolute() or not Path(spec["binary"]).is_absolute():
        raise JobError("Job paths must be absolute.")
    if spec.get("existing_files") != "verify_owned_frames":
        raise JobError("Unsupported existing-file policy.")


def folder(root: Path, job_id: str) -> Path:
    if not isinstance(job_id, str) or not _ID.fullmatch(job_id):
        raise JobError("Invalid job id.")
    return root / "jobs" / job_id


def spec_at(path: Path) -> dict:
    spec = read_json(path / "spec.json")
    validate_spec(spec)
    if spec["id"] != path.name:
        raise JobError("Job id does not match its folder.")
    return spec


def status(path: Path) -> dict:
    spec = spec_at(path)
    current = read_json(path / "status.json")
    if current.get("state") not in ACTIVE | TERMINAL:
        raise JobError("Invalid job status.")
    if current["state"] in ACTIVE:
        heartbeat = path / "lease" / "heartbeat"
        stamp = heartbeat.stat().st_mtime if heartbeat.exists() else current["updated"]
        if time.time() - stamp > HEARTBEAT_TIMEOUT:
            # Report loss, but never release an uncertain lease or start a second writer.
            current = {**current, "state": "interrupted", "error": "Worker heartbeat was lost. "
                       "Resume is allowed once its supervisor has exited."}
        elif (path / "cancel").exists():
            current = {**current, "state": "cancelling"}
    progress = read_json(path / "progress.json") if (path / "progress.json").exists() else {}
    return {**spec, **current, "progress": progress}


def list_jobs(root: Path) -> list[dict]:
    parent = root / "jobs"
    if not parent.exists():
        return []
    rows = []
    for path in parent.iterdir():
        if path.is_dir() and _ID.fullmatch(path.name) and (path / "spec.json").exists():
            try:
                rows.append(status(path))
            except (OSError, ValueError, JobError) as ex:
                print(f"Loopcut: unreadable job {path.name}: {ex}")
    return sorted(rows, key=lambda row: row.get("created", 0), reverse=True)


def cancel(path: Path) -> None:
    if status(path)["state"] in ACTIVE:
        (path / "cancel").touch()


def _alive(pid: int) -> bool:
    if pid <= 0:
        raise JobError("Invalid worker process id.")
    if os.name == "nt":
        # os.kill(pid, 0) is not a liveness probe on Windows: it can terminate the process.
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            if ctypes.get_last_error() == 87:  # ERROR_INVALID_PARAMETER: no such process.
                return False
            return True  # Access denied or unknown: do not permit another writer.
        try:
            code = wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def launch(path: Path) -> dict:
    spec = spec_at(path)
    lease = path / "lease"
    try:
        lease.mkdir()
    except FileExistsError:
        owner = lease / "owner.json"
        heartbeat = lease / "heartbeat"
        stamp = heartbeat.stat().st_mtime if heartbeat.exists() else lease.stat().st_mtime
        pid = read_json(owner).get("pid") if owner.exists() else None
        if time.time() - stamp < HEARTBEAT_TIMEOUT or (type(pid) is int and _alive(pid)):
            raise JobError("This job already has a worker. Cancel it before resuming.")
        # The supervisor is gone. Its child may still be rendering: never reuse its files.
        child = lease / "child.json"
        child_pid = read_json(child).get("pid") if child.exists() else None
        if type(child_pid) is int and _alive(child_pid):
            raise JobError("The previous renderer is still alive. Wait for it to exit before resuming.")
        for record in lease.iterdir():
            record.unlink()
        lease.rmdir()
        return launch(path)
    try:
        (lease / "heartbeat").touch()
        (path / "cancel").unlink(missing_ok=True)
        old = read_json(path / "status.json") if (path / "status.json").exists() else {}
        atomic_json(path / "status.json", {"state": "queued", "updated": time.time(),
                    "attempt": old.get("attempt", 0) + 1, "error": ""})
        command = [spec["binary"], "--background", "--factory-startup", "--disable-autoexec",
                   "--python-exit-code", "1", "--python", str(Path(__file__).with_name("job_supervisor.py")),
                   "--", str(path)]
        env = {k: v for k, v in os.environ.items() if not k.startswith("LOOPCUT_")}
        with (path / "supervisor.log").open("ab") as log:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                       start_new_session=True, env=env)
        _children.append(process)
        atomic_json(lease / "owner.json", {"pid": process.pid})
    except (OSError, ValueError):
        for record in lease.iterdir():
            record.unlink()
        lease.rmdir()
        atomic_json(path / "status.json", {"state": "failed", "updated": time.time(),
                                           "error": "Could not start render worker."})
        raise
    return status(path)
