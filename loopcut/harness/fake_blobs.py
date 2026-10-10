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
    def __init__(self):
        self.key = "lc_" + "fake-for-tests"
        self.stored = {}
        self.pending = {}
        self.calls = []
        self.puts = []

    def client(self):
        from loopcut.cloud_http import Client
        return Client(base_url="https://service.test", api_key=self.key, transport=self)

    def __call__(self, request, *, timeout):
        method = request.get_method()
        headers = {key.lower(): value for key, value in request.header_items()}
        assert headers["x-loopcut-contract"] == "0.5"
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
        if endpoint == "/v1/blobs/upload-urls":
            self.pending.update({item["sha256"]: item for item in payload["items"]})
            return Reply({"items": [{"sha256": item["sha256"],
                                     "put_url": "https://storage.test/" + item["sha256"],
                                     "expires_at": "2099-01-01T00:00:00Z"} for item in payload["items"]]})
        raise AssertionError("Unexpected fake endpoint: " + endpoint)
