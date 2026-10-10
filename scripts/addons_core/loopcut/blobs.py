"""Content-addressed file uploads: ask first, stream only absent bytes to presigned URLs.

The service key belongs only on service requests. Storage PUTs carry contract/version headers
and the file's MIME and length, never Authorization. No file is read into memory in full.
"""

import hashlib
import mimetypes
import re
import stat
import tempfile
import urllib.request
from pathlib import Path

from . import cloud_http

CHUNK_SIZE = 1024 * 1024
BATCH_SIZE = 100
MAX_BLOB_SIZE = 200 * 1024 * 1024  # Contract upload-urls item limit.
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
        if found != wanted:
            raise _invalid()
        urls.extend(rows)
    return urls


def _upload_size(path):
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BLOB_SIZE:
        raise ValueError("Blob must be a regular file within the upload size limit")
    return info.st_size


def put(url, path, mime, *, client=None, expected_hash=None, expected_size=None):
    client = client or cloud_http.Client()
    cloud_http.validate_url(url)
    if not isinstance(mime, str) or not mime or any(ord(c) < 32 for c in mime):
        raise ValueError("Invalid MIME type")
    path = Path(path)
    _upload_size(path)
    # Freeze bytes before opening HTTP: the source can be edited while a PUT is streaming.
    with tempfile.TemporaryFile() as handle:
        digest, size = hashlib.sha256(), 0
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(CHUNK_SIZE), b""):
                size += len(chunk)
                if size > MAX_BLOB_SIZE:
                    raise ValueError("Blob exceeds the upload size limit")
                digest.update(chunk)
                handle.write(chunk)
        if (expected_hash is not None and digest.hexdigest() != expected_hash
                or expected_size is not None and size != expected_size):
            raise ValueError("File changed before upload")
        handle.seek(0)
        headers = {**client.version_headers(), "Content-Type": mime, "Content-Length": str(size)}
        request = urllib.request.Request(url, data=handle, headers=headers, method="PUT")
        _, status, _ = cloud_http.send(request, client.transport, client.timeout)
        if not 200 <= status < 300:
            raise cloud_http.CloudError("Storage did not accept the blob", "http_error", status)


def upload_missing(files, *, client=None, batch_size=BATCH_SIZE):
    """Files are descriptors {path, sha256?, size?, mime?}; return the hashes PUT this time.

    Supplied digests and lengths are verified locally before HTTP so stale manifests cannot
    publish different bytes under an old hash. Duplicate content is uploaded once.
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
    items = [unique[digest] for digest in missing]
    for batch in _batches(items, batch_size):
        for row in upload_urls(batch, client=client, batch_size=batch_size):
            item = unique[row["sha256"]]
            put(row["put_url"], item["path"], item["mime"], client=client,
                expected_hash=item["sha256"], expected_size=item["size"])
    return missing
