"""The service contract in one place, independent of Blender's UI and worker threads.

Transports have urlopen's signature: transport(Request, timeout=seconds) returns a context
manager with status, headers and read(). Tests inject one; production uses urllib with
redirects disabled so a service redirect cannot forward the account key to another host.
"""

import errno
import gzip
import json
import http.client
import re
import socket
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass

from . import bl_info, config, credentials

CONTRACT = "0.7"
MAX_RESPONSE = 4 * 1024 * 1024


class CloudError(RuntimeError):
    """A contract error, retaining fields callers need for credits, retry and upgrades."""

    def __init__(self, message, code, status=None, extra=None, error_type=None, detail=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.extra = dict(extra or {})
        self.error_type = error_type
        self.detail = detail  # A transport failure's describe() text; never a URL, header or key.


@dataclass(frozen=True)
class Response:
    data: dict | None
    status: int
    headers: dict


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def urllib_transport(request, *, timeout):
    return urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout)


def validate_url(url):
    """TLS for credentials and blobs; plaintext is only for loopback development."""
    if not isinstance(url, str) or any(ord(c) <= 32 or ord(c) == 127 for c in url):
        raise ValueError("Invalid HTTP URL")
    parts = urllib.parse.urlsplit(url)
    if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.fragment or parts.port == 0
            or parts.scheme == "http" and parts.hostname not in {"localhost", "127.0.0.1", "::1"}):
        raise ValueError("Invalid HTTP URL")
    return parts


def client_value() -> str:
    """The x-loopcut-client value: `loopcut-addon/<version> blender/<version> os/<os> arch/<arch>`. The first two
    are SPEC §3's; os and arch (the usage events' values) follow as extra space-separated tokens, which every
    server parser of the header ignores, so the device link and API requests say which platform they run on.
    bpy.app.version is a constant, so any thread may call this."""
    from . import telemetry
    try:
        import bpy
    except ImportError:  # Plain Python (tests, tools): no Blender to name.
        bpy = None
    version = getattr(getattr(bpy, "app", None), "version", ())
    blender = ".".join(map(str, version)) if version else "unknown"
    addon = ".".join(map(str, bl_info["version"]))
    return f"loopcut-addon/{addon} blender/{blender} os/{telemetry.os_name()} arch/{telemetry.arch_name()}"


def contract_headers():
    if threading.current_thread() is not threading.main_thread():
        from . import mainthread
        return mainthread.run_on_main(contract_headers).result(timeout=30)
    return {"x-loopcut-contract": CONTRACT, "x-loopcut-client": client_value()}


def json_body(payload) -> bytes:
    """A request body as Client sends it; callers that must fit a size limit measure this."""
    return json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")


def gzip_body(data: bytes) -> bytes:
    """The compressed form a request with compress=True sends (Content-Encoding: gzip). mtime=0 keeps the same
    body byte-identical, so a measured size is the size sent."""
    return gzip.compress(data, compresslevel=6, mtime=0)


def describe(error):
    """A transport failure as class plus errno or SSL reason, e.g. BrokenPipeError [Errno 32 EPIPE].

    Never the exception's text: socket and HTTP messages can carry keys or signed URLs.
    """
    if isinstance(error, urllib.error.URLError) and isinstance(error.reason, BaseException):
        error = error.reason
    name = type(error).__name__
    if isinstance(error, ssl.SSLError):
        reason = getattr(error, "reason", None)
        if not isinstance(reason, str):
            found = re.search(r"\[SSL: ([A-Z0-9_]{1,64})\]", str(error))
            reason = found and found[1]
        return f"{name} {reason}" if isinstance(reason, str) and re.fullmatch(r"[A-Z0-9_]{1,64}", reason) else name
    if isinstance(error, TimeoutError):
        return f"{name} (timed out)"
    if isinstance(error, socket.gaierror):
        return f"{name} (name lookup failed)"
    if isinstance(error, OSError) and isinstance(error.errno, int):
        symbol = errno.errorcode.get(error.errno)
        return f"{name} [Errno {error.errno}{' ' + symbol if symbol else ''}]"
    return name


def _unreachable(error, request):
    cause = describe(error)
    print(f"Loopcut: {request.get_method()} request failed in transport: {cause}")
    return CloudError(f"Could not reach the Loopcut service ({cause})", "connection_failed", detail=cause)


def _read(response):
    body = response.read(MAX_RESPONSE + 1)
    if len(body) > MAX_RESPONSE:
        raise CloudError("Loopcut response is too large", "invalid_response", response.status)
    return body


def _redact(value, secrets):
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[redacted]")
    elif isinstance(value, dict):
        value = {_redact(k, secrets): _redact(v, secrets) for k, v in value.items()}
    elif isinstance(value, list):
        value = [_redact(v, secrets) for v in value]
    return value


def _error(status, body, request):
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        payload = None
    # Error envelopes are untrusted, even when they echo the Authorization header.
    secrets = [value.removeprefix("Bearer ") for name, value in request.header_items()
               if name.lower() == "authorization" and value]
    query = urllib.parse.urlsplit(request.full_url).query
    if query:
        secrets.extend([request.full_url, query, *[v for _, v in urllib.parse.parse_qsl(query) if v]])
    try:
        payload = _redact(payload, sorted(set(secrets), key=len, reverse=True))
    except RecursionError:
        return CloudError(f"HTTP {status}", "http_error", status)
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return CloudError(f"HTTP {status}", "http_error", status)
    message = error.get("message")
    code = error.get("code")
    extra = {k: v for k, v in error.items() if k not in {"message", "code", "type"}}
    return CloudError(message if isinstance(message, str) else f"HTTP {status}",
                      code if isinstance(code, str) else "http_error", status, extra, error.get("type"))


def send(request, transport_fn, timeout):
    """Raw HTTP also serves presigned PUTs; error bodies never include request secrets."""
    try:
        try:
            response = transport_fn(request, timeout=timeout)
        except urllib.error.HTTPError as ex:
            response = ex
        with response:
            body = _read(response)
            status, headers = response.status, dict(response.headers)
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as ex:
        # Transport messages and HTTP reason strings can contain keys or signed URLs: describe() only.
        raise _unreachable(ex, request) from None
    if not (200 <= status < 300 or status == 304):
        raise _error(status, body, request) from None
    return body, status, headers


@contextmanager
def open_response(request, transport_fn, timeout):
    """Keep a successful response open for SSE or large signed downloads."""
    try:
        with transport_fn(request, timeout=timeout) as response:
            if not 200 <= response.status < 300:
                raise _error(response.status, _read(response), request) from None
            yield response
    except urllib.error.HTTPError as ex:
        with ex:
            body = _read(ex)
        raise _error(ex.code, body, request) from None
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as ex:
        # As in send(): transport messages and reasons can carry keys or signed URLs.
        raise _unreachable(ex, request) from None


class Client:
    def __init__(self, *, base_url=None, api_key=None, transport=None, timeout=30, client_headers=None):
        self.base_url = (base_url if base_url is not None else config.SERVICE_URL).rstrip("/")
        parts = validate_url(self.base_url)
        if parts.query:
            raise ValueError("Service base URL cannot contain a query")
        self.api_key = api_key
        self.transport = transport or urllib_transport
        self.timeout = timeout
        # A main-thread snapshot lets job workers avoid reading Blender or its preferences.
        if client_headers is not None and set(k.lower() for k in client_headers) - {"x-loopcut-contract", "x-loopcut-client"}:
            raise ValueError("Client header snapshots may contain only contract/version headers")
        self.client_headers = dict(client_headers) if client_headers is not None else None

    def version_headers(self):
        return dict(self.client_headers) if self.client_headers is not None else contract_headers()

    def _request(self, method, path, payload=None, *, headers=None, auth=True, accept="application/json",
                 compress=False):
        parts = urllib.parse.urlsplit(path)
        if (not path.startswith("/") or path.startswith("//") or parts.scheme or parts.netloc
                or parts.fragment or "\\" in path or any(ord(c) < 32 for c in path)):
            raise ValueError("Service requests need a relative endpoint path")
        supplied = dict(headers or {})
        reserved = {"authorization", "x-loopcut-contract", "x-loopcut-client", "content-type", "content-encoding"}
        if reserved.intersection(k.lower() for k in supplied):
            raise ValueError("Contract headers cannot be overridden")
        supplied.update(self.version_headers())
        supplied["Accept"] = accept
        if auth:
            key = self.api_key if self.api_key is not None else credentials.api_key(f"{self.base_url}/v1")
            if (not isinstance(key, str) or not key.startswith("lc_") or len(key) <= 3
                    or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in key)):
                raise CloudError("Sign in to Loopcut first", "sign_in_required")
            supplied["Authorization"] = f"Bearer {key}"
        body = None
        if payload is not None:
            body = json_body(payload)
            supplied["Content-Type"] = "application/json"
            if compress:  # Conversation logs and problem reports (SPEC §3.12): the wire limit is on gzip bytes.
                body = gzip_body(body)
                supplied["Content-Encoding"] = "gzip"
        return urllib.request.Request(self.base_url + path, data=body, headers=supplied, method=method)

    @contextmanager
    def stream(self, path, *, headers=None, timeout=None):
        """SSE uses the same endpoint, auth, error and redirect rules as JSON requests."""
        request = self._request("GET", path, headers=headers, accept="text/event-stream")
        with open_response(request, self.transport, self.timeout if timeout is None else timeout) as response:
            yield response

    @contextmanager
    def download(self, url):
        """Presigned storage GETs never carry the Loopcut account key."""
        validate_url(url)
        request = urllib.request.Request(url, headers=self.version_headers(), method="GET")
        with open_response(request, self.transport, self.timeout) as response:
            yield response

    def request_response(self, method, path, payload=None, *, headers=None, auth=True, compress=False):
        request = self._request(method, path, payload, headers=headers, auth=auth, compress=compress)
        body, status, response_headers = send(request, self.transport, self.timeout)
        if status in {204, 304}:
            return Response(None, status, response_headers)
        try:
            data = json.loads(body)
        except (ValueError, UnicodeError, RecursionError):
            raise CloudError("Invalid JSON from Loopcut", "invalid_response", status) from None
        if not isinstance(data, dict):
            raise CloudError("Expected an object from Loopcut", "invalid_response", status)
        return Response(data, status, response_headers)

    def request(self, method, path, payload=None, **options):
        return self.request_response(method, path, payload, **options).data
