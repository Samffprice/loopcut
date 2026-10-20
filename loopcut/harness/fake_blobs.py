"""An in-process blob service for the bundle unit tests and Blender check. No sockets."""

import hashlib
import io
import json
from urllib.parse import urlsplit


class Reply(io.BytesIO):
    def __init__(self, payload=None, status=200, headers=None):
        super().__init__(json.dumps(payload).encode("utf-8") if payload is not None else b"")
        self.status = status
        self.headers = headers or {}


class FakeBlobs:
    """part_size set: the multipart routes (contract 0.6) work with parts that size; None: they answer 404."""

    def __init__(self, part_size=None):
        self.key = "lc_" + "fake-for-tests"
        self.stored = {}
        self.pending = {}
        self.calls = []
        self.puts = []
        self.part_size = part_size
        self.uploads = {}  # upload_id -> {sha256, size, mime, count, parts: {n: bytes}}
        self.part_puts = []  # (upload_id, part number) per accepted part PUT
        self.aborted = []
        self.started = 0

    def client(self):
        from loopcut.cloud_http import Client
        return Client(base_url="https://service.test", api_key=self.key, transport=self)

    def __call__(self, request, *, timeout):
        method = request.get_method()
        headers = {key.lower(): value for key, value in request.header_items()}
        assert headers["x-loopcut-contract"] == "0.7"
        assert headers["x-loopcut-client"].startswith("loopcut-addon/")
        assert " blender/" in headers["x-loopcut-client"]
        assert timeout > 0
        self.calls.append({"method": method, "url": request.full_url, "headers": headers})
        endpoint = urlsplit(request.full_url).path
        if method == "PUT":
            assert urlsplit(request.full_url).netloc == "storage.test"
            assert "authorization" not in headers
            assert hasattr(request.data, "read"), "PUT must stream an open file"
            chunks = []
            for chunk in iter(lambda: request.data.read(8192), b""):
                chunks.append(chunk)
            body = b"".join(chunks)
            if endpoint.startswith("/part/"):
                _, _, upload_id, number = endpoint.split("/")
                upload, number = self.uploads[upload_id], int(number)
                size = self.part_size if number < upload["count"] else upload["size"] - (upload["count"] - 1) * self.part_size
                assert len(body) == size == int(headers["content-length"]), "a part is exactly its signed length"
                assert headers["content-type"] == upload["mime"]
                upload["parts"][number] = body
                self.part_puts.append((upload_id, number))
                return Reply(status=200, headers={"ETag": f'"{hashlib.md5(body).hexdigest()}"'})
            digest = endpoint.removeprefix("/")
            expected = self.pending[digest]
            assert hashlib.sha256(body).hexdigest() == digest
            assert len(body) == expected["size"] == int(headers["content-length"])
            assert headers["content-type"] == expected["mime"]
            self.stored[digest] = body
            self.puts.append(digest)
            return Reply(status=200)
        assert method == "POST"
        assert headers["authorization"] == "Bearer " + self.key
        assert headers["content-type"] == "application/json"
        payload = json.loads(request.data)
        self.calls[-1]["payload"] = payload
        if endpoint == "/v1/blobs/check":
            return Reply({"missing": [digest for digest in payload["sha256"] if digest not in self.stored]})
        if endpoint.startswith("/v1/blobs/multipart/"):
            return self.multipart(endpoint.removeprefix("/v1/blobs/multipart/"), payload)
        if endpoint == "/v1/blobs/upload-urls":
            self.pending.update({item["sha256"]: item for item in payload["items"]})
            return Reply({"items": [{"sha256": item["sha256"],
                                     "put_url": "https://storage.test/" + item["sha256"],
                                     "expires_at": "2099-01-01T00:00:00Z"} for item in payload["items"]]})
        raise AssertionError("Unexpected fake endpoint: " + endpoint)

    def part_urls(self, upload_id, numbers):
        return [{"part_number": n, "put_url": f"https://storage.test/part/{upload_id}/{n}"} for n in numbers]

    def multipart(self, action, payload):
        if self.part_size is None:
            return Reply({"error": {"message": "Multipart uploads are not available.", "type": "not_found",
                                    "code": "not_found"}}, status=404)
        digest = payload["sha256"]
        if action == "start":
            if digest in self.stored:
                return Reply({"sha256": digest, "stored": True})
            self.started += 1
            upload_id = f"upload-{self.started}"
            count = max(1, -(-payload["size"] // self.part_size))
            self.uploads[upload_id] = {**payload, "count": count, "parts": {}}
            return Reply({"sha256": digest, "stored": False, "upload_id": upload_id, "part_size": self.part_size,
                          "parts": self.part_urls(upload_id, range(1, count + 1)), "expires_at": "2099-01-01T00:00:00Z"})
        upload = self.uploads.get(payload["upload_id"])
        if action == "abort":
            self.aborted.append(payload["upload_id"])
            self.uploads.pop(payload["upload_id"], None)
            return Reply({"sha256": digest})
        if upload is None:
            return Reply({"error": {"message": "Unknown upload.", "type": "not_found", "code": "not_found"}}, status=404)
        if action == "urls":
            return Reply({"sha256": digest, "upload_id": payload["upload_id"],
                          "parts": self.part_urls(payload["upload_id"], payload["part_numbers"]),
                          "expires_at": "2099-01-01T00:00:00Z"})
        assert action == "complete"
        listed = payload["parts"]
        assert [part["part_number"] for part in listed] == list(range(1, upload["count"] + 1))
        for part in listed:
            assert part["etag"] == f'"{hashlib.md5(upload["parts"][part["part_number"]]).hexdigest()}"'
        body = b"".join(upload["parts"][n] for n in range(1, upload["count"] + 1))
        assert hashlib.sha256(body).hexdigest() == digest
        self.stored[digest] = body
        del self.uploads[payload["upload_id"]]
        return Reply({"sha256": digest})
