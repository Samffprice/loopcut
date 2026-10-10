"""Bounded urllib transport with injectable HTTP for offline tests and safe local errors.

Vendor response bodies can contain keys or user input. Error envelopes never echo those
bodies. Authenticated redirects are refused, and output downloads carry no vendor credentials.
"""

import ipaddress
import http.client
import json
import math
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

TIMEOUT = 30.0
MAX_BYTES = 160 * 1024 * 1024
JSON_BYTES = 16 * 1024 * 1024


class ByokError(RuntimeError):
    def __init__(self, code, message, **extra):
        super().__init__(message)
        self.error = {"code": code, "type": "byok_error", "message": message, **extra}


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict
    body: bytes


def check_url(url: str, *, host=None) -> str:
    try:
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.fragment or parsed.port not in (None, 443)):
            raise ValueError
        if host and parsed.hostname != host:
            raise ValueError
        if parsed.hostname in ("localhost", "localhost.localdomain") or parsed.hostname.endswith(".local"):
            raise ValueError
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ByokError("vendor_failed", "Vendor returned an unsafe output or queue URL") from None
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def urllib_transport(method, url, headers, body, timeout) -> Response:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as ex:
        with ex:
            return Response(ex.code, dict(ex.headers or {}), ex.read(JSON_BYTES + 1))
    with response:
        return Response(response.status, dict(response.headers), response.read(MAX_BYTES + 1))


def _retry_after(value):
    try:
        seconds = float(value)
        return max(0, math.ceil(seconds)) if math.isfinite(seconds) else None
    except (ValueError, TypeError):
        try:
            return max(0, math.ceil((parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()))
        except (ValueError, TypeError, OverflowError):
            return None


def _invalid_key(body):
    try:
        data = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        return False
    if not isinstance(data, dict):
        return False
    error = data.get("error", data.get("detail"))
    if not isinstance(error, dict):
        return False
    if error.get("code") in (401, "invalid_api_key") or error.get("status") in ("UNAUTHENTICATED", "invalid_api_key"):
        return True
    details = error.get("details", [])
    return isinstance(details, list) and any(isinstance(d, dict) and d.get("reason") == "API_KEY_INVALID" for d in details)


class Client:
    def __init__(self, transport=None, *, timeout=TIMEOUT):
        self.transport = transport or urllib_transport
        self.timeout = timeout

    def request(self, method, url, *, headers=None, body=None, max_bytes=MAX_BYTES) -> Response:
        check_url(url)
        try:
            response = self.transport(method, url, headers or {}, body, self.timeout)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, http.client.HTTPException):
            raise ByokError("vendor_failed", "Vendor connection failed or timed out") from None
        if response.status in (401, 403) or (response.status == 400 and _invalid_key(response.body)):
            raise ByokError("byok_key_invalid", "Vendor rejected your key; replace it in Your own keys")
        if response.status == 429:
            retry = _retry_after(next((v for k, v in response.headers.items() if k.lower() == "retry-after"), None))
            raise ByokError("rate_limited", "Vendor rate limit reached", **({"retry_after": retry} if retry is not None else {}))
        if 400 <= response.status < 500:
            raise ByokError("invalid_input", "Vendor rejected the tool input")
        if not 200 <= response.status < 300:
            raise ByokError("vendor_failed", "Vendor request failed")
        if len(response.body) > max_bytes:
            raise ByokError("vendor_failed", "Vendor response exceeds the local size limit")
        return response

    def json(self, method, url, *, headers=None, payload=None) -> dict:
        headers = {"Accept": "application/json", **(headers or {})}
        body = None
        if payload is not None:
            body = json.dumps(payload, allow_nan=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        response = self.request(method, url, headers=headers, body=body, max_bytes=JSON_BYTES)
        try:
            data = json.loads(response.body)
        except (ValueError, UnicodeError, RecursionError):
            raise ByokError("vendor_failed", "Vendor returned invalid JSON") from None
        if not isinstance(data, dict):
            raise ByokError("vendor_failed", "Vendor returned an unexpected response")
        return data

    def download(self, url) -> bytes:
        data = self.request("GET", url).body
        if not data:
            raise ByokError("vendor_failed", "Vendor returned an empty output")
        return data
