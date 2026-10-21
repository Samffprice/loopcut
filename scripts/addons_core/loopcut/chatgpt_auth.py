"""Sign in with ChatGPT (SIWC) for the "ChatGPT plan" provider: the user's ChatGPT Plus or Pro plan
pays for Loopcut's model calls (chatgpt_responses.py), with no API key.

This is OpenAI's documented flow for open-source, locally hosted apps
(developers.openai.com/siwc/token-sharing-open-source), which needs no approval:

- First sign-in registers a client: client_id=dynamic_agent_client, our agent_name_hint and this
  computer's ext_agent_host_id; the callback returns the issued client_id (oaiapp_...), which every
  later sign-in, refresh and revocation uses. Each issued client is bound to one ChatGPT user and
  workspace; that is also where the user sets Loopcut's weekly usage limit.
- OAuth code flow with PKCE (S256), a fresh state and nonce per attempt, and a one-shot loopback
  listener on http://127.0.0.1:<port>/auth/callback (never "localhost"; only the port may vary).
- The ID token is checked (RS256 against OpenAI's JWKS, issuer, audience, expiry, nonce) and the
  granted scopes must include chatgpt.tokens.use.direct before any inference.
- Tokens live in one owner-only (0600) file written atomically, under ~/.config/loopcut/chatgpt
  (%APPDATA%\\loopcut\\chatgpt on Windows; LOOPCUT_CHATGPT_DIR overrides), outside Blender's own
  folders so the command-line sign-in (harness/chatgpt_signin.py) and every Blender on this
  computer share one sign-in and one host ID. Never in a .blend, the preferences, logs or env files.
- Refreshes are serialized across processes with a file lock (the refresh token rotates).
- Sign-out revokes the refresh token, then clears the tokens but keeps the registration (client id,
  account identity) and the host ID for a later sign-in, as the docs ask.

Only this official flow: Loopcut never reads Codex's or ChatGPT's own stored credentials and never
takes pasted ones. Stdlib only and free of bpy, so the command line can import it.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable

# The pseudo base URL that selects this provider (config.py, llm.stream_chat). Not a real endpoint:
# requests go to RESOURCE /responses with the signed-in account's token.
CHATGPT_PLAN_BASE_URL = "chatgpt-plan://api.openai.com/v1"
# What config.Config.api_key holds on this provider. Not a secret: the real token is read per request.
PLACEHOLDER_KEY = "chatgpt-plan"

AGENT_NAME = "Loopcut"
DYNAMIC_CLIENT = "dynamic_agent_client"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
RESOURCE = "https://api.openai.com/v1"
ISSUER = "https://auth.openai.com"
CALLBACK_PATH = "/auth/callback"
PREFERRED_PORT = 1455          # The docs' example; any free port works on a later sign-in.
USAGE_URL = "https://chatgpt.com/settings/usage"
REFRESH_MARGIN = 300.0         # Refresh an access token this many seconds before it expires.
CLOCK_SKEW = 120.0             # Allowed when checking an ID token's exp and iat.
SIGN_IN_TIMEOUT = 600.0
_HTTP_TIMEOUT = 20.0
_JWKS_TTL = 3600.0
# Refresh errors after which the refresh token is useless: clear the tokens, sign in again (docs:
# errors-and-recovery#refresh-errors).
_DEAD_REFRESH = {"invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired",
                 "refresh_token_invalidated", "refresh_token_reused"}
# Sign-in errors said in plain words: onboarding shows 70 characters of a failed sign-in. In 0.2.5, invalid_grant
# came at the end of a full browser sign-in, 39 of 41 installs on Windows, and a retry almost never worked
# (app_events, 2026-10-21). OpenAI doesn't say why it refuses an account (plan, workspace or country: a Plus user
# was refused too, GitHub issue #2), so the message doesn't guess.
SIGN_IN_MESSAGES = {
    "invalid_grant": "ChatGPT didn't let this account share its plan with Loopcut.",
    "unsupported_country_region_territory": "ChatGPT plan use isn't available in your country.",
}


def is_chatgpt_plan(base_url: str) -> bool:
    """Whether a configured base URL selects the ChatGPT plan provider."""
    return (base_url or "").strip().rstrip("/") in (CHATGPT_PLAN_BASE_URL, "chatgpt-plan") \
        or (base_url or "").startswith("chatgpt-plan:")


class ChatGPTAuthError(RuntimeError):
    """code: the OAuth or API error code when there is one (invalid_grant, access_denied, ...).
    stage: "token" when the token endpoint refused a sign-in's authorization code, else empty.
    Messages never carry a token."""

    def __init__(self, message: str, code: str = "", status: int | None = None, stage: str = ""):
        super().__init__(message)
        self.code = code
        self.status = status
        self.stage = stage


class NotSignedIn(ChatGPTAuthError):
    pass


# --- Endpoints ------------------------------------------------------------------------------------

def _loopback_override(name: str) -> str:
    """A test or fake server's address from the environment. Only 127.0.0.1 is accepted, so a
    stray variable can never send a ChatGPT token anywhere but OpenAI or this computer."""
    value = os.environ.get(name, "").strip().rstrip("/")
    if value and not value.startswith("http://127.0.0.1:"):
        raise ChatGPTAuthError(f"{name} may only point at http://127.0.0.1 (a local test server)")
    return value


def auth_base() -> str:
    return _loopback_override("LOOPCUT_CHATGPT_AUTH_URL") or ISSUER


def api_base() -> str:
    return _loopback_override("LOOPCUT_CHATGPT_API_URL") or RESOURCE


def issuer() -> str:
    return auth_base()


def authorize_endpoint() -> str:
    return auth_base() + "/api/accounts/authorize"


def token_endpoint() -> str:
    return auth_base() + "/api/accounts/oauth/token"


_discovery: dict = {}


def _discovered(key: str, default: str) -> str:
    """A value from the issuer's openid-configuration (the docs name revocation_endpoint there),
    falling back to the known path when it cannot be read."""
    if not _discovery:
        try:
            payload = _get_json(auth_base() + "/.well-known/openid-configuration")
            if isinstance(payload, dict):
                _discovery.update(payload)
        except ChatGPTAuthError:
            pass
    value = str(_discovery.get(key) or "")
    base = auth_base()
    return value if value.startswith(base + "/") else default


def revocation_endpoint() -> str:
    return _discovered("revocation_endpoint", auth_base() + "/api/accounts/oauth/revoke")


def jwks_endpoint() -> str:
    return _discovered("jwks_uri", auth_base() + "/.well-known/jwks.json")


# --- Storage --------------------------------------------------------------------------------------

def folder() -> Path:
    override = os.environ.get("LOOPCUT_CHATGPT_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / "loopcut" / "chatgpt"


ACCOUNTS_FILE = "accounts.json"
HOST_FILE = "host.json"
LOCK_FILE = "accounts.lock"
_thread_lock = threading.RLock()


def _ensure_folder() -> Path:
    path = folder()
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _write_private(path: Path, data: dict) -> None:
    """Atomically, created owner-only (0600) from the start, so a token is never on disk readable
    by anyone else, even for a moment."""
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class _FileLock:
    """Serializes credential changes across processes (the CLI, every Blender, an eval's Blender):
    the refresh token rotates, so two refreshes racing would sign one of them out."""

    def __enter__(self):
        _thread_lock.acquire()
        self._fd = None
        try:
            path = _ensure_folder() / LOCK_FILE
            self._fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            if sys.platform == "win32":
                import msvcrt
                while True:
                    try:
                        msvcrt.locking(self._fd, msvcrt.LK_LOCK, 1)
                        break
                    except OSError:
                        time.sleep(0.1)
            else:
                import fcntl
                fcntl.flock(self._fd, fcntl.LOCK_EX)
        except BaseException:
            self._release()
            raise
        return self

    def _release(self):
        if self._fd is not None:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    os.lseek(self._fd, 0, 0)
                    msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(self._fd)
            self._fd = None
        _thread_lock.release()

    def __exit__(self, *exc):
        self._release()
        return False


def _load_accounts() -> dict:
    data = _read_json(folder() / ACCOUNTS_FILE)
    accounts = data.get("accounts") if isinstance(data.get("accounts"), dict) else {}
    return {"version": 1, "active": data.get("active") if data.get("active") in accounts else None,
            "accounts": {k: v for k, v in accounts.items() if isinstance(v, dict)}}


def _save_accounts(data: dict) -> None:
    _write_private(_ensure_folder() / ACCOUNTS_FILE, data)


def host_id() -> str:
    """This computer's ext_agent_host_id: chosen once, before the first sign-in, and reused for
    every later one. An opaque urn:uuid (the docs accept it alongside JWK thumbprints), not an
    identity or a credential."""
    with _FileLock():
        path = folder() / HOST_FILE
        data = _read_json(path)
        value = str(data.get("ext_agent_host_id") or "")
        if value.startswith("urn:uuid:"):
            return value
        value = f"urn:uuid:{uuid.uuid4()}"
        _write_private(path, {**data, "ext_agent_host_id": value, "created": _now_iso()})
        return value


def _pending_client() -> str:
    """The client id a registration was issued before its code exchange failed with invalid_grant; empty if none."""
    return str(_read_json(folder() / HOST_FILE).get("pending_client_id") or "")


def _set_pending_client(client_id: str) -> None:
    """Keeps (or, given "", forgets) that issued client id for the next sign-in on this computer."""
    if client_id == _pending_client():
        return
    with _FileLock():
        path = folder() / HOST_FILE
        data = {k: v for k, v in _read_json(path).items() if k != "pending_client_id"}
        _write_private(path, {**data, "pending_client_id": client_id} if client_id else data)


def _host_flag(name: str) -> bool:
    return bool(_read_json(folder() / HOST_FILE).get(name))


def _set_host_flag(name: str) -> None:
    with _FileLock():
        path = folder() / HOST_FILE
        _write_private(path, {**_read_json(path), name: True})


def welcomed() -> bool:
    """Whether the "You're using your ChatGPT plan" note was shown (the docs: only on the first sign-in)."""
    return _host_flag("welcomed")


def mark_welcomed() -> None:
    _set_host_flag("welcomed")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def active_record() -> dict | None:
    data = _load_accounts()
    return data["accounts"].get(data["active"]) if data["active"] else None


def registrations() -> list[dict]:
    """Every saved registration, signed in or not, without tokens: for an account picker."""
    data = _load_accounts()
    return [{"client_id": cid, "label": r.get("label") or "", "email": r.get("email") or "",
             "plan_type": r.get("plan_type") or "", "signed_in": bool(r.get("refresh_token") or r.get("access_token")),
             "active": cid == data["active"]} for cid, r in data["accounts"].items()]


def status() -> dict:
    """The active account, without any token: what the preferences page and the CLI show."""
    record = active_record()
    if not record:
        return {"signed_in": False, "registered": False}
    signed_in = bool(record.get("refresh_token") or record.get("access_token"))
    return {"signed_in": signed_in, "registered": True, "email": record.get("email") or "",
            "plan_type": record.get("plan_type") or "", "plan_usage": PLAN_SCOPE in (record.get("scopes") or []),
            "expires_at": record.get("expires_at"), "saved_at": record.get("saved_at") or "",
            "label": record.get("label") or ""}


def signed_in() -> bool:
    return bool(status().get("signed_in"))


def plan_signed_in() -> bool:
    """Signed in with the plan-usage permission, so the ChatGPT plan can be Loopcut's model. While it is,
    nothing automatic moves the provider off it (account._store_key, onboarding's own-key screen): only the
    user's own pick of another provider does."""
    info = status()
    return bool(info.get("signed_in") and info.get("plan_usage"))


# --- HTTP -----------------------------------------------------------------------------------------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _oauth_error(body: bytes, status: int) -> ChatGPTAuthError:
    try:
        payload = json.loads(body)
    except ValueError:
        payload = {}
    code, message = "", ""
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):  # An API-style error object.
            code, message = str(error.get("code") or error.get("type") or ""), str(error.get("message") or "")
        elif error:
            code, message = str(error), str(payload.get("error_description") or "")
        elif payload.get("detail"):
            message = str(payload["detail"])
    return ChatGPTAuthError(f"HTTP {status}" + (f" {code}" if code else "") + (f": {message[:300]}" if message else ""),
                            code, status)


def _post_form(url: str, fields: dict) -> dict:
    request = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode("ascii"), method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded",
                                              "Accept": "application/json", "User-Agent": AGENT_NAME})
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as response:
            body = response.read(2_000_000)
    except urllib.error.HTTPError as ex:
        with ex:
            raise _oauth_error(ex.read(20_000), ex.code) from None
    except (urllib.error.URLError, TimeoutError, OSError) as ex:
        raise ChatGPTAuthError(f"Could not reach {urllib.parse.urlparse(url).netloc}: {getattr(ex, 'reason', ex)}",
                               "network") from None
    if not body.strip():
        return {}
    try:
        payload = json.loads(body)
    except ValueError:
        raise ChatGPTAuthError("The sign-in server sent a reply that is not JSON") from None
    return payload if isinstance(payload, dict) else {}


def _get_json(url: str, token: str = "") -> dict:
    headers = {"Accept": "application/json", "User-Agent": AGENT_NAME}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=_HTTP_TIMEOUT) as response:
            payload = json.loads(response.read(4_000_000))
    except urllib.error.HTTPError as ex:
        with ex:
            raise _oauth_error(ex.read(20_000), ex.code) from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as ex:
        raise ChatGPTAuthError(f"Could not read {urllib.parse.urlparse(url).netloc}: {getattr(ex, 'reason', ex)}",
                               "network") from None
    return payload if isinstance(payload, dict) else {}


# --- ID token -------------------------------------------------------------------------------------

_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")
_jwks_cache: dict = {"at": 0.0, "keys": []}


def _jwks(force: bool = False) -> list[dict]:
    if force or not _jwks_cache["keys"] or time.monotonic() - _jwks_cache["at"] > _JWKS_TTL:
        keys = _get_json(jwks_endpoint()).get("keys")
        _jwks_cache.update(at=time.monotonic(), keys=[k for k in keys or [] if isinstance(k, dict)])
    return _jwks_cache["keys"]


def _rs256_valid(signing_input: bytes, signature: bytes, jwk: dict) -> bool:
    """RSASSA-PKCS1-v1_5 with SHA-256, in plain Python: Blender's Python has no crypto library, and
    verifying is one modular exponentiation and a byte comparison."""
    try:
        n = int.from_bytes(_b64url_decode(jwk["n"]), "big")
        e = int.from_bytes(_b64url_decode(jwk["e"]), "big")
    except (KeyError, ValueError, TypeError):
        return False
    size = (n.bit_length() + 7) // 8
    if len(signature) != size or n < 2 ** 2047:
        return False
    decoded = pow(int.from_bytes(signature, "big"), e, n).to_bytes(size, "big")
    payload = _SHA256_DIGEST_INFO + hashlib.sha256(signing_input).digest()
    expected = b"\x00\x01" + b"\xff" * (size - len(payload) - 3) + b"\x00" + payload
    return hmac.compare_digest(decoded, expected)


def decode_jwt(token: str) -> tuple[dict, dict, bytes, bytes]:
    """(header, claims, signing input, signature) without checking anything."""
    try:
        head, body, signature = token.split(".")
        header = json.loads(_b64url_decode(head))
        claims = json.loads(_b64url_decode(body))
        return header, claims, f"{head}.{body}".encode("ascii"), _b64url_decode(signature)
    except (ValueError, TypeError, UnicodeEncodeError):
        raise ChatGPTAuthError("The ID token is not a well-formed JWT") from None


def validate_id_token(id_token: str, client_id: str, nonce: str | None, now: float | None = None) -> dict:
    """The verified claims: RS256 signature against the issuer's JWKS, iss, aud (the issued client
    id), exp/iat with a small skew, and the nonce of this attempt (None on a refresh, which has none)."""
    header, claims, signing_input, signature = decode_jwt(id_token)
    if header.get("alg") != "RS256":
        raise ChatGPTAuthError(f"Unexpected ID token algorithm {header.get('alg')!r}")
    kid = header.get("kid")
    keys = [k for k in _jwks() if k.get("kid") == kid] or [k for k in _jwks(force=True) if k.get("kid") == kid]
    if not keys or not any(_rs256_valid(signing_input, signature, k) for k in keys):
        raise ChatGPTAuthError("The ID token's signature did not verify against OpenAI's published keys")
    now = time.time() if now is None else now
    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    if claims.get("iss") != issuer():
        raise ChatGPTAuthError("The ID token comes from an unexpected issuer")
    if client_id not in audiences:
        raise ChatGPTAuthError("The ID token was issued to another client")
    if not isinstance(claims.get("exp"), (int, float)) or claims["exp"] < now - CLOCK_SKEW:
        raise ChatGPTAuthError("The ID token has expired")
    if isinstance(claims.get("iat"), (int, float)) and claims["iat"] > now + CLOCK_SKEW:
        raise ChatGPTAuthError("The ID token was issued in the future; check this computer's clock")
    if nonce is not None and not hmac.compare_digest(str(claims.get("nonce") or ""), nonce):
        raise ChatGPTAuthError("The ID token's nonce does not match this sign-in")
    if not claims.get("sub"):
        raise ChatGPTAuthError("The ID token names no subject")
    return claims


def _plan_type(claims: dict) -> str:
    """The ChatGPT plan named in the ID token, when OpenAI puts one there (best effort; display only)."""
    for value in claims.values():
        if isinstance(value, dict) and value.get("chatgpt_plan_type"):
            return str(value["chatgpt_plan_type"])
    return str(claims.get("chatgpt_plan_type") or "")


# --- Authorization --------------------------------------------------------------------------------

def pkce_pair() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(48))
    return verifier, _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def authorize_url(*, client_id: str, redirect_uri: str, state: str, nonce: str, code_challenge: str,
                  ext_agent_host_id: str, id_token_hint: str = "", login_hint: str = "") -> str:
    """The browser URL. agent_name_hint goes only with a first registration; the hints only with
    a returning one. Never log a URL built with id_token_hint."""
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "scope": SCOPES,
              "resource": RESOURCE, "state": state, "nonce": nonce, "code_challenge": code_challenge,
              "code_challenge_method": "S256", "ext_agent_host_id": ext_agent_host_id}
    if client_id == DYNAMIC_CLIENT:
        params["agent_name_hint"] = AGENT_NAME
    else:
        if id_token_hint:
            params["id_token_hint"] = id_token_hint
        if login_hint:
            params["login_hint"] = login_hint
    return authorize_endpoint() + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)


_PAGE = ("<!doctype html><meta charset=utf-8><title>Loopcut</title><body style=\"font:16px -apple-system,"
         "system-ui,sans-serif;max-width:32em;margin:15vh auto;padding:0 1em\"><h2>{title}</h2><p>{text}</p>")


class _CallbackServer:
    """One-shot loopback listener for the authorization redirect. Prefers port 1455, else any free
    port; the path is always /auth/callback. Requests to other paths get a 404 and are ignored."""

    def __init__(self, port: int = PREFERRED_PORT):
        owner = self
        self.result: dict | None = None
        self.expected_state = ""

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != CALLBACK_PATH:
                    self.send_error(404)
                    return
                query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
                if not hmac.compare_digest(query.get("state", ""), owner.expected_state):
                    self._page(400, "Sign-in not recognised", "This page did not come from the sign-in Loopcut "
                               "started. Go back to Loopcut and try again.")
                    return
                if query.get("error"):
                    self._page(200, "Sign-in stopped", "Loopcut was not given access. You can close this tab.")
                else:
                    self._page(200, "Signed in to Loopcut", "You can close this tab and go back to Blender.")
                owner.result = query

            def _page(self, status: int, title: str, text: str):
                body = _PAGE.format(title=title, text=text).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):  # The request line carries the code: never print it.
                pass

        try:
            self.server = HTTPServer(("127.0.0.1", port), Handler)
        except OSError:
            self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.server.timeout = 0.25
        self.redirect_uri = f"http://127.0.0.1:{self.server.server_address[1]}{CALLBACK_PATH}"

    def wait(self, deadline: float, cancelled: Callable[[], bool]) -> dict:
        while self.result is None:
            if cancelled():
                raise ChatGPTAuthError("Sign-in cancelled", "cancelled")
            if time.monotonic() > deadline:
                raise ChatGPTAuthError("Sign-in timed out: the browser never came back to Loopcut", "timeout")
            self.server.handle_request()
        return self.result

    def close(self):
        self.server.server_close()


class SignIn:
    """One sign-in attempt. start() prepares everything and returns the URL to open; wait() blocks
    (call it from a worker thread in Blender) until the browser comes back, then exchanges the code,
    validates the ID token and the plan scope, and saves the account as the active one.

    new_account: register another ChatGPT account (dynamic registration) instead of signing in
    again to the saved one with its issued client id."""

    def __init__(self, new_account: bool = False, port: int = PREFERRED_PORT, timeout: float = SIGN_IN_TIMEOUT):
        self.new_account, self.port, self.timeout = new_account, port, timeout
        self._server: _CallbackServer | None = None
        self.url = ""
        self.display_url = ""   # The same without id_token_hint: the one safe to show or copy.

    def start(self) -> str:
        self.host = host_id()
        saved = None if self.new_account else active_record()
        if saved is None and not self.new_account:
            saved = next((r for r in _load_accounts()["accounts"].values()), None)
        self.saved = saved
        # Docs (sign-in): after invalid_grant, a fresh authorization keeps the client id that registration issued.
        self.pending = "" if saved or self.new_account else _pending_client()
        self.client_id = saved["client_id"] if saved and saved.get("client_id") else self.pending or DYNAMIC_CLIENT
        self.state, self.nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.verifier, challenge = pkce_pair()
        self._server = _CallbackServer(self.port)
        self._server.expected_state = self.state
        common = dict(client_id=self.client_id, redirect_uri=self._server.redirect_uri, state=self.state,
                      nonce=self.nonce, code_challenge=challenge, ext_agent_host_id=self.host,
                      login_hint=(saved or {}).get("email") or "")
        self.url = authorize_url(**common, id_token_hint=(saved or {}).get("id_token") or "")
        self.display_url = authorize_url(**common)
        return self.url

    @property
    def redirect_uri(self) -> str:
        return self._server.redirect_uri if self._server else ""

    def wait(self, cancelled: Callable[[], bool] = lambda: False) -> dict:
        if self._server is None:
            raise ChatGPTAuthError("SignIn.wait() before start()")
        try:
            return self._finish(cancelled)
        except ChatGPTAuthError as ex:
            if self.pending and ex.code not in ("cancelled", "timeout"):
                _set_pending_client("")  # One more try on the kept client id; after that, register afresh.
            raise

    def _finish(self, cancelled: Callable[[], bool]) -> dict:
        try:
            query = self._server.wait(time.monotonic() + self.timeout, cancelled)
        finally:
            self._server.close()
        if query.get("error"):
            code = query["error"]
            if code == "access_denied":
                raise ChatGPTAuthError("ChatGPT sign-in was declined, so Loopcut can't use your ChatGPT plan. "
                                       "Sign in again to allow it, or pick another provider.", code)
            raise ChatGPTAuthError(SIGN_IN_MESSAGES.get(code)
                                   or f"ChatGPT sign-in failed: {code} {query.get('error_description', '')}".strip(), code)
        returned_client = query.get("client_id") or ""
        if self.client_id == DYNAMIC_CLIENT:
            if not returned_client or returned_client == DYNAMIC_CLIENT:
                raise ChatGPTAuthError("Registration did not finish: the callback named no issued client id", "registration")
            client_id = returned_client
        else:
            if returned_client and returned_client != self.client_id:
                raise ChatGPTAuthError("The sign-in came back for a different client; nothing was changed", "client_mismatch")
            client_id = self.client_id
        if not query.get("code"):
            raise ChatGPTAuthError("The sign-in came back without an authorization code")
        try:
            tokens = _post_form(token_endpoint(), {
                "grant_type": "authorization_code", "client_id": client_id, "code": query["code"],
                "code_verifier": self.verifier, "redirect_uri": self._server.redirect_uri, "resource": RESOURCE})
        except ChatGPTAuthError as ex:
            if ex.code == "invalid_grant" and self.client_id == DYNAMIC_CLIENT:
                try:
                    _set_pending_client(client_id)
                except OSError as kept:  # The sign-in's own error is the one to show.
                    print(f"Loopcut: could not keep the issued ChatGPT client id: {type(kept).__name__}")
            raise ChatGPTAuthError(SIGN_IN_MESSAGES.get(ex.code) or str(ex), ex.code, ex.status, "token") from None
        _set_pending_client("")  # A code went through: any kept id is spent. Writes only when there is one.
        return _save_sign_in(tokens, client_id, self.nonce, self.host, self.saved)


def _save_sign_in(tokens: dict, client_id: str, nonce: str, host: str, saved: dict | None) -> dict:
    for key in ("access_token", "id_token"):
        if not tokens.get(key):
            raise ChatGPTAuthError(f"The token reply had no {key}")
    claims = validate_id_token(tokens["id_token"], client_id, nonce)
    scopes = sorted(str(tokens.get("scope") or "").split())
    if saved and saved.get("client_id") == client_id and saved.get("subject") and saved["subject"] != claims["sub"]:
        raise ChatGPTAuthError("You signed in with a different ChatGPT account than this registration's; "
                               "nothing was changed. Use 'another account' to add it.", "account_mismatch")
    now = time.time()
    record = {
        "email": str(claims.get("email") or ""), "issuer": claims["iss"], "subject": claims["sub"],
        "client_id": client_id, "ext_agent_host_id": host, "id_token": tokens["id_token"],
        "access_token": tokens["access_token"], "refresh_token": tokens.get("refresh_token") or "",
        "token_type": tokens.get("token_type") or "Bearer", "expires_in": int(tokens.get("expires_in") or 3600),
        "scopes": scopes, "saved_at": _now_iso(),
        # Loopcut's own fields:
        "expires_at": now + int(tokens.get("expires_in") or 3600),
        "earliest_refresh_at": _epoch(tokens.get("earliest_refresh_at")),
        "plan_type": _plan_type(claims), "label": str(claims.get("email") or claims["sub"]),
    }
    with _FileLock():
        data = _load_accounts()
        data["accounts"][client_id] = {**data["accounts"].get(client_id, {}), **record}
        data["active"] = client_id
        _save_accounts(data)
    if PLAN_SCOPE not in scopes:
        raise ChatGPTAuthError("Signed in, but ChatGPT plan use was not allowed, so Loopcut can't use your plan "
                               "for model calls. Sign in again and allow it, or pick another provider.", "no_plan_scope")
    return status()


def _epoch(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return float(value)
        except ValueError:
            try:
                return time.mktime(time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
            except ValueError:
                return None
    return None


def sign_in(open_url: Callable[[str], None], new_account: bool = False, cancelled: Callable[[], bool] = lambda: False,
            timeout: float = SIGN_IN_TIMEOUT, on_url: Callable[[str], None] | None = None) -> dict:
    """The whole flow in one call (blocking). on_url gets the display URL (no id_token_hint)."""
    attempt = SignIn(new_account=new_account, timeout=timeout)
    try:
        url = attempt.start()
        if on_url:
            on_url(attempt.display_url)
        open_url(url)
        return attempt.wait(cancelled)
    finally:
        # A browser/URL callback failure must not leave the loopback port occupied.
        if attempt._server is not None:
            attempt._server.close()


# --- Using the tokens -----------------------------------------------------------------------------

def _clear_tokens(record: dict) -> None:
    for key in ("access_token", "refresh_token", "id_token", "expires_at", "earliest_refresh_at"):
        record.pop(key, None)


def refresh(force: bool = False) -> dict:
    """Renew the active account's access token. Under the file lock, and re-read inside it: when
    another process just refreshed, its fresh token is used instead of spending the rotated one."""
    with _FileLock():
        data = _load_accounts()
        client_id = data["active"]
        record = data["accounts"].get(client_id) if client_id else None
        if not record or not record.get("refresh_token"):
            raise NotSignedIn("Not signed in to ChatGPT", "not_signed_in")
        now = time.time()
        expires_at = float(record.get("expires_at") or 0)
        fresh = expires_at - now > REFRESH_MARGIN
        too_early = (record.get("earliest_refresh_at") or 0) > now and expires_at > now
        if (fresh and not force) or (too_early and not force):
            return record
        try:
            tokens = _post_form(token_endpoint(), {"grant_type": "refresh_token", "client_id": client_id,
                                                   "refresh_token": record["refresh_token"], "resource": RESOURCE})
        except ChatGPTAuthError as ex:
            if ex.code in _DEAD_REFRESH:
                _clear_tokens(record)
                _save_accounts(data)
                raise NotSignedIn("Your ChatGPT sign-in has ended. Sign in with ChatGPT again.", ex.code) from None
            if ex.code == "invalid_client":
                raise ChatGPTAuthError("ChatGPT no longer recognises Loopcut's registration on this computer "
                                       "(invalid_client). Sign in again as another account.", ex.code) from None
            raise
        if not tokens.get("access_token"):
            raise ChatGPTAuthError("The refresh reply had no access token")
        if tokens.get("id_token"):
            try:
                claims = validate_id_token(tokens["id_token"], client_id, None)
                if claims["sub"] == record.get("subject"):
                    record["id_token"] = tokens["id_token"]
            except ChatGPTAuthError as ex:
                print(f"Loopcut: kept the previous ID token; the refreshed one did not validate: {ex}")
        record.update(access_token=tokens["access_token"],
                      refresh_token=tokens.get("refresh_token") or record["refresh_token"],
                      expires_in=int(tokens.get("expires_in") or 3600), expires_at=now + int(tokens.get("expires_in") or 3600),
                      earliest_refresh_at=_epoch(tokens.get("earliest_refresh_at")), saved_at=_now_iso())
        if tokens.get("scope"):
            record["scopes"] = sorted(str(tokens["scope"]).split())
        _save_accounts(data)
        return record


def access_token(force_refresh: bool = False) -> str:
    """A usable access token for the active account, refreshed when it is near expiry. Raises
    NotSignedIn when there is none, or when the account did not allow ChatGPT plan use."""
    record = active_record()
    if not record or not (record.get("access_token") or record.get("refresh_token")):
        raise NotSignedIn("Not signed in to ChatGPT. Sign in under Preferences > Add-ons > Loopcut "
                          "(provider: ChatGPT plan), or run harness/chatgpt_signin.py sign-in.", "not_signed_in")
    if PLAN_SCOPE not in (record.get("scopes") or []):
        raise NotSignedIn("This ChatGPT sign-in did not allow plan use. Sign in again and allow it.", "no_plan_scope")
    if force_refresh or float(record.get("expires_at") or 0) - time.time() <= REFRESH_MARGIN:
        record = refresh(force=force_refresh)
    return record["access_token"]


def sign_out() -> bool:
    """Revoke the renewable session, then clear the tokens. Keeps the registration and the host ID.
    Returns whether revocation was confirmed (False: cleared locally only; the user can disconnect
    Loopcut in ChatGPT's settings)."""
    with _FileLock():
        data = _load_accounts()
        client_id = data["active"]
        record = data["accounts"].get(client_id) if client_id else None
        if not record:
            return True
        confirmed = not record.get("refresh_token")
        for delay in (0.0, 1.0, 3.0):
            if confirmed:
                break
            time.sleep(delay)
            try:
                _post_form(revocation_endpoint(), {"token": record["refresh_token"],
                                                   "token_type_hint": "refresh_token", "client_id": client_id})
                confirmed = True
            except ChatGPTAuthError as ex:
                if ex.code != "network" and not (ex.status or 0) >= 500:
                    break
        _clear_tokens(record)
        _save_accounts(data)
        return confirmed


def list_models() -> list[dict]:
    """The signed-in account's model picker: [{slug, display_name}] in the server's order, only the
    ones meant for display."""
    payload = _get_json(api_base() + "/models", access_token())
    rows = payload.get("models") if isinstance(payload.get("models"), list) else payload.get("data") or []
    found = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        slug = str(row.get("slug") or row.get("id") or "")
        if slug and row.get("visibility", "list") == "list":
            found.append({"slug": slug, "display_name": str(row.get("display_name") or slug)})
    return found
