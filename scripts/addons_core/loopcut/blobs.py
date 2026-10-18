"""Content-addressed file uploads: ask first, stream only absent bytes to presigned URLs.

The service key belongs only on service requests. Storage PUTs carry contract/version headers
and the file's MIME and length, never Authorization. No file is read into memory in full.
"""

import contextlib
import hashlib
import mimetypes
import re
import stat
import tempfile
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from . import cloud_http

CHUNK_SIZE = 1024 * 1024
BATCH_SIZE = 100
MAX_BLOB_SIZE = 200 * 1024 * 1024  # Contract upload-urls item limit.
PUT_ATTEMPTS = 5
RETRY_DELAYS = (1, 2, 4, 8)  # Seconds before tries 2-5: large PUTs drop or corrupt mid-body now and then.
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
URL_MARGIN = 60  # Seconds: a presigned URL this close to its expires_at is replaced before a try.
MULTIPART = "/v1/blobs/multipart/"  # start, urls, complete, abort (contract 0.6, SPEC 3.3).
SERVICE_ATTEMPTS = 3  # Multipart service calls; parts themselves get PUT_ATTEMPTS each.
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def sha256(path, *, max_bytes=None):
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            size += len(chunk)
            if max_bytes is not None and size > max_bytes:
                raise ValueError("Blob exceeds the upload size limit")
            digest.update(chunk)
    return digest.hexdigest()


def _hash(value):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ValueError("Expected a lowercase SHA-256 hash")
    return value


def _batches(items, size):
    if not isinstance(size, int) or isinstance(size, bool) or size < 1:
        raise ValueError("Batch size must be positive")
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _invalid():
    return cloud_http.CloudError("Invalid blob response from Loopcut", "invalid_response")


def check(hashes, *, client=None, batch_size=BATCH_SIZE):
    client = client or cloud_http.Client()
    hashes = list(dict.fromkeys(_hash(value) for value in hashes))
    missing = []
    for batch in _batches(hashes, batch_size):
        reply = client.request("POST", "/v1/blobs/check", {"sha256": batch})
        absent = reply.get("missing") if isinstance(reply, dict) else None
        if (not isinstance(absent, list) or any(not isinstance(h, str) or h not in batch for h in absent)
                or len(set(absent)) != len(absent)):
            raise _invalid()
        missing.extend(absent)
    return missing


def upload_urls(items, *, client=None, batch_size=BATCH_SIZE):
    client = client or cloud_http.Client()
    validated = []
    for item in items:
        size, mime = item["size"], item["mime"]
        if (not isinstance(size, int) or isinstance(size, bool) or not 0 <= size <= MAX_BLOB_SIZE
                or not isinstance(mime, str) or not mime or any(ord(c) < 32 for c in mime)):
            raise ValueError("Invalid blob size or MIME type")
        validated.append({"sha256": _hash(item["sha256"]), "size": size, "mime": mime})
    hashes = [item["sha256"] for item in validated]
    if len(hashes) != len(set(hashes)):
        raise ValueError("Duplicate blob hash")
    urls = []
    for batch in _batches(validated, batch_size):
        reply = client.request("POST", "/v1/blobs/upload-urls", {"items": batch})
        rows = reply.get("items") if isinstance(reply, dict) else None
        wanted = {item["sha256"] for item in batch}
        if not isinstance(rows, list):
            raise _invalid()
        found = set()
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get("sha256"), str)
                    or row["sha256"] not in wanted or row["sha256"] in found
                    or not isinstance(row.get("expires_at"), str)):
                raise _invalid()
            try:
                cloud_http.validate_url(row.get("put_url"))
            except ValueError:
                raise _invalid() from None
            found.add(row["sha256"])
        # Ready hashes are omitted (SPEC 3.3): storage already has them, so they need no PUT.
        urls.extend(rows)
    return urls


def _upload_size(path):
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BLOB_SIZE:
        raise ValueError("Blob must be a regular file within the upload size limit")
    return info.st_size


def _deadline(expires_at):
    try:
        return datetime.fromisoformat(expires_at.replace("Z", "+00:00")).timestamp()
    except (AttributeError, TypeError, ValueError):
        return None


def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise cloud_http.CloudError("Cloud call canceled before submission", "canceled")


class _Body:
    """`length` bytes of the frozen copy from its current position, as a PUT body that stops
    mid-stream once the call is canceled. A whole-file PUT sends all of it; a part, its window."""

    def __init__(self, handle, cancel, length):
        self.handle, self.cancel, self.left = handle, cancel, length

    def read(self, size=-1):
        _check_cancel(self.cancel)
        if self.left <= 0:
            return b""
        data = self.handle.read(self.left if size is None or size < 0 else min(size, self.left))
        self.left -= len(data)
        return data


def _mime(mime):
    if not isinstance(mime, str) or not mime or any(ord(c) < 32 for c in mime):
        raise ValueError("Invalid MIME type")
    return mime


@contextlib.contextmanager
def _frozen(path, expected_hash, expected_size):
    """A private copy of the file's bytes, checked against the hash and size the server was told.

    Freeze bytes before opening HTTP: the source can be edited while a PUT is streaming.
    """
    with tempfile.TemporaryFile() as handle:
        digest, size = hashlib.sha256(), 0
        with Path(path).open("rb") as source:
            for chunk in iter(lambda: source.read(CHUNK_SIZE), b""):
                size += len(chunk)
                if size > MAX_BLOB_SIZE:
                    raise ValueError("Blob exceeds the upload size limit")
                digest.update(chunk)
                handle.write(chunk)
        if (expected_hash is not None and digest.hexdigest() != expected_hash
                or expected_size is not None and size != expected_size):
            raise ValueError("File changed before upload")
        yield handle, size


def _wait(cancel, seconds):
    if cancel is not None:
        cancel.wait(seconds)
    else:
        time.sleep(seconds)


def _send(client, url, handle, offset, length, headers, *, amount, part="", noun="Upload", expires_at=None,
          refresh=None, cancel=None, progress=None):
    """PUT `length` bytes of the frozen copy from `offset`: the one retry loop for whole files and parts.

    Retries dropped connections, 408, 429 and 5xx up to PUT_ATTEMPTS; other refusals are final. A repeat
    is safe: the URL names the content hash (and a part's number), so it can only store the same bytes.
    Before a try on a URL near expires_at, refresh() returns a fresh (put_url, expires_at), or None when
    storage already has the blob. Returns the response headers, or None after such a refresh.
    `amount` ("82.0 MB") and `part` (" (part 4/16)") name the upload in messages; `noun` in progress.
    """
    deadline = _deadline(expires_at)
    for attempt in range(1, PUT_ATTEMPTS + 1):
        _check_cancel(cancel)
        if refresh is not None and deadline is not None and time.time() > deadline - URL_MARGIN:
            fresh = refresh()
            if fresh is None:
                return None
            url, deadline = fresh[0], _deadline(fresh[1])
            cloud_http.validate_url(url)
        handle.seek(offset)
        request = urllib.request.Request(url, data=_Body(handle, cancel, length), headers=headers, method="PUT")
        try:
            _, status, response_headers = cloud_http.send(request, client.transport, client.timeout)
        except cloud_http.CloudError as ex:
            if ex.code != "connection_failed" and ex.status not in RETRY_STATUSES:
                if ex.code == "http_error" and ex.status is not None:
                    raise cloud_http.CloudError(f"Cloud storage refused the {amount} upload{part} (HTTP {ex.status})",
                                                "http_error", ex.status) from None
                raise
            error, cause = ex, ex.detail or f"HTTP {ex.status}"
        else:
            if not 200 <= status < 300:
                raise cloud_http.CloudError("Storage did not accept the blob", "http_error", status)
            return response_headers
        if attempt < PUT_ATTEMPTS:
            print(f"Loopcut: {amount} upload{part} interrupted ({cause}); retrying ({attempt + 1}/{PUT_ATTEMPTS})")
            if progress is not None:
                progress(f"{noun} interrupted, retrying ({attempt + 1}/{PUT_ATTEMPTS})…")
            _wait(cancel, RETRY_DELAYS[min(attempt, len(RETRY_DELAYS)) - 1])
    if error.code == "connection_failed":
        message = (f"Upload to cloud storage was interrupted: {amount}{part}, {PUT_ATTEMPTS} tries, last {cause}. "
                   "Check the connection and try again.")
    else:
        message = (f"Cloud storage did not accept the {amount} upload{part}: {cause} after {PUT_ATTEMPTS} tries. "
                   "Try again later.")
    print(f"Loopcut: {message}")
    raise cloud_http.CloudError(message, error.code, error.status, detail=error.detail)


def _amount(size):
    return f"{size / 1_000_000:,.1f} MB"


def put(url, path, mime, *, client=None, expected_hash=None, expected_size=None, expires_at=None,
        refresh=None, cancel=None, progress=None):
    """PUT a frozen copy, retrying dropped connections, 408, 429 and 5xx; other refusals are final.

    A repeat is safe: the URL names the content hash, so it can only store the same bytes (a 25 MB
    scene bundle's PUT broke at 5-17 MB with TLS "bad record mac", live test 2026-10-17).
    Before a try on a URL near expires_at, refresh() returns a fresh (put_url, expires_at), or
    None when storage already has the blob. cancel.is_set() stops between tries and mid-body.
    """
    client = client or cloud_http.Client()
    cloud_http.validate_url(url)
    _mime(mime)
    path = Path(path)
    _upload_size(path)
    with _frozen(path, expected_hash, expected_size) as (handle, size):
        headers = {**client.version_headers(), "Content-Type": mime, "Content-Length": str(size)}
        _send(client, url, handle, 0, size, headers, amount=_amount(size), expires_at=expires_at,
              refresh=refresh, cancel=cancel, progress=progress)


# ------------------------------------------------------------------ multipart (contract 0.6, SPEC 3.3)

class _MultipartOff(Exception):
    """The server has multipart switched off (404 not_found): send this file with one PUT."""


def multipart_capability(value):
    """The manifest's capabilities.blob_multipart if it is well formed, else None (single PUTs only)."""
    size = value.get("min_size") if isinstance(value, dict) else None
    return {"min_size": size} if isinstance(size, int) and not isinstance(size, bool) and size >= 1 else None


def _service(client, action, payload, cancel=None):
    """POST a multipart call. Each is safe to repeat (start replaces, complete is idempotent), so a
    dropped connection, 429 (after its retry_after) or 502-504 is tried again, up to SERVICE_ATTEMPTS."""
    for attempt in range(1, SERVICE_ATTEMPTS + 1):
        _check_cancel(cancel)
        try:
            return client.request("POST", MULTIPART + action, payload)
        except cloud_http.CloudError as ex:
            if ex.code != "connection_failed" and ex.status not in {429, 502, 503, 504} or attempt == SERVICE_ATTEMPTS:
                raise
            after = ex.extra.get("retry_after")
            delay = after if isinstance(after, int) and not isinstance(after, bool) and 0 <= after <= 60 else \
                RETRY_DELAYS[min(attempt, len(RETRY_DELAYS)) - 1]
            print(f"Loopcut: multipart {action} failed ({ex.detail or ex.code}); "
                  f"retrying ({attempt + 1}/{SERVICE_ATTEMPTS})")
            _wait(cancel, delay)


def _parts(reply):
    """Validate a start or urls reply's parts: {part_number: put_url}."""
    rows = reply.get("parts")
    if not isinstance(rows, list) or not rows or not isinstance(reply.get("expires_at"), str):
        raise _invalid()
    urls = {}
    for row in rows:
        number = row.get("part_number") if isinstance(row, dict) else None
        if not isinstance(number, int) or isinstance(number, bool) or number < 1 or number in urls:
            raise _invalid()
        try:
            cloud_http.validate_url(row.get("put_url"))
        except ValueError:
            raise _invalid() from None
        urls[number] = row["put_url"]
    return urls


class _Upload:
    """One file's multipart upload: its id, part layout and current part URLs."""

    def __init__(self, client, reply, digest, size, cancel):
        self.client, self.digest, self.size, self.cancel = client, digest, size, cancel
        upload_id, part_size = reply.get("upload_id"), reply.get("part_size")
        if (not isinstance(upload_id, str) or not 0 < len(upload_id) <= 1024
                or any(not 33 <= ord(c) <= 126 for c in upload_id)
                or not isinstance(part_size, int) or isinstance(part_size, bool) or part_size < 1):
            raise _invalid()
        self.upload_id, self.part_size = upload_id, part_size
        self.count = max(1, -(-size // part_size))
        self.urls, self.expires_at = _parts(reply), reply["expires_at"]
        if sorted(self.urls) != list(range(1, self.count + 1)):
            raise _invalid()

    def refresh(self, number):
        """Fresh URLs for this part and every later one, in one call; returns (put_url, expires_at)."""
        reply = _service(self.client, "urls", {"sha256": self.digest, "upload_id": self.upload_id,
                                               "part_numbers": list(range(number, self.count + 1))}, self.cancel)
        if (not isinstance(reply, dict) or reply.get("sha256") != self.digest
                or reply.get("upload_id") != self.upload_id):
            raise _invalid()
        urls = _parts(reply)
        if number not in urls or any(n < number or n > self.count for n in urls):
            raise _invalid()
        self.urls.update(urls)
        self.expires_at = reply["expires_at"]
        return self.urls[number], self.expires_at


def _abort(client, digest, upload_id):
    """One try, best effort (often after a cancel): storage also discards unfinished uploads after a day."""
    try:
        client.request("POST", MULTIPART + "abort", {"sha256": digest, "upload_id": upload_id})
    except (cloud_http.CloudError, ValueError, OSError):
        pass


def put_multipart(path, digest, size, mime, *, client=None, cancel=None, progress=None):
    """Upload one file in the server's parts, each PUT through _send's retries; True if bytes went up.

    False when storage already has the hash. Raises _MultipartOff when the server answers the start
    with 404 (multipart switched off), so the caller sends one PUT instead. Any failure or cancel
    after the start aborts the upload.
    """
    client = client or cloud_http.Client()
    _mime(mime)
    path = Path(path)
    with _frozen(path, digest, size) as (handle, size):
        try:
            reply = _service(client, "start", {"sha256": digest, "size": size, "mime": mime}, cancel)
        except cloud_http.CloudError as ex:
            if ex.status == 404 and ex.code == "not_found":
                raise _MultipartOff() from None
            raise
        if not isinstance(reply, dict) or reply.get("sha256") != digest or type(reply.get("stored")) is not bool:
            raise _invalid()
        if reply["stored"]:
            return False
        try:
            upload = _Upload(client, reply, digest, size, cancel)
        except cloud_http.CloudError:
            # A reply we can't use may still have opened an upload: end it.
            if isinstance(reply.get("upload_id"), str) and 0 < len(reply["upload_id"]) <= 1024:
                _abort(client, digest, reply["upload_id"])
            raise
        amount, done = _amount(size), False
        try:
            etags = []
            for number in range(1, upload.count + 1):
                offset = (number - 1) * upload.part_size
                length = min(upload.part_size, size - offset)
                label = f"part {number}/{upload.count}"
                if progress is not None:
                    progress(f"Uploading {path.name}: {label}…")
                headers = {**client.version_headers(), "Content-Type": mime, "Content-Length": str(length)}
                answer = _send(client, upload.urls[number], handle, offset, length, headers, amount=amount,
                               part=f" ({label})", noun=label.capitalize(), expires_at=upload.expires_at,
                               refresh=lambda number=number: upload.refresh(number), cancel=cancel, progress=progress)
                etag = next((v for k, v in (answer or {}).items() if k.lower() == "etag"), None)
                if not isinstance(etag, str) or not 0 < len(etag) <= 256 or any(not 33 <= ord(c) <= 126 for c in etag):
                    raise cloud_http.CloudError(f"Cloud storage did not confirm {label} of the {amount} upload",
                                                "invalid_response")
                etags.append({"part_number": number, "etag": etag})
            reply = _service(client, "complete", {"sha256": digest, "upload_id": upload.upload_id, "parts": etags},
                             cancel)
            if not isinstance(reply, dict) or reply.get("sha256") != digest:
                raise _invalid()
            done = True
        finally:
            if not done:
                _abort(client, digest, upload.upload_id)
    return True


def _fresh_url(item, client):
    """A new (put_url, expires_at) for one blob, or None once storage has it."""
    rows = upload_urls([item], client=client)
    return (rows[0]["put_url"], rows[0]["expires_at"]) if rows else None


def upload_missing(files, *, client=None, batch_size=BATCH_SIZE, cancel=None, progress=None, multipart=None):
    """Files are descriptors {path, sha256?, size?, mime?}; return the hashes PUT this time.

    Supplied digests and lengths are verified locally before HTTP so stale manifests cannot
    publish different bytes under an old hash. Duplicate content is uploaded once.
    cancel and progress reach put(): a canceled call stops, a retry says so.
    multipart is the manifest's capabilities.blob_multipart: files of its min_size and up go in
    parts (put_multipart), falling back to one PUT if the server has switched it off.
    """
    client = client or cloud_http.Client()
    unique = {}
    for item in files:
        path = Path(item["path"])
        size = _upload_size(path)
        digest = sha256(path, max_bytes=MAX_BLOB_SIZE)
        if "sha256" in item and _hash(item["sha256"]) != digest:
            raise ValueError(f"File changed since hashing: {path.name}")
        if "size" in item and item["size"] != size:
            raise ValueError(f"File changed since sizing: {path.name}")
        mime = item.get("mime") or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        unique.setdefault(digest, {"sha256": digest, "size": size, "mime": mime, "path": path})
    missing = check(unique, client=client, batch_size=batch_size)
    capability = multipart_capability(multipart)
    parts = {digest for digest in missing if capability and unique[digest]["size"] >= capability["min_size"]}
    large = [unique[digest] for digest in missing if digest in parts]
    items = [unique[digest] for digest in missing if digest not in parts]
    uploaded = []
    for item in large:
        try:
            if put_multipart(item["path"], item["sha256"], item["size"], item["mime"], client=client,
                             cancel=cancel, progress=progress):
                uploaded.append(item["sha256"])
        except _MultipartOff:
            items.append(item)  # Switched off since the manifest was read: one PUT, as before.
    for batch in _batches(items, batch_size):
        for row in upload_urls(batch, client=client, batch_size=batch_size):
            item = unique[row["sha256"]]
            # A batch's URLs share one expiry; later files in a long batch may need fresh ones.
            put(row["put_url"], item["path"], item["mime"], client=client, expected_hash=item["sha256"],
                expected_size=item["size"], expires_at=row["expires_at"], cancel=cancel, progress=progress,
                refresh=lambda item=item: _fresh_url(item, client))
            uploaded.append(row["sha256"])
    return uploaded
