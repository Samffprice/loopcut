"""The ChatGPT plan provider without the network: chatgpt_auth.py's Sign in with ChatGPT flow against
a fake OAuth server (registration, PKCE, loopback callback, ID token checks, refresh, revocation,
storage), and chatgpt_responses.py against a fake Responses API (input mapping, namespaced tools,
streaming into llm.Completion, usage, replayed reasoning, errors and retries). No Blender needed.

    python3 -m unittest discover -s loopcut/tests
"""

import base64
import hashlib
import json
import os
import random
import stat
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

from loopcut import chatgpt_auth, chatgpt_ready, chatgpt_responses, config, llm  # noqa: E402


# --- A test RSA key (2048 bits, deterministic) and JWT signing -----------------------------------

def _probable_prime(n: int, rng: random.Random) -> bool:
    for p in (3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47):
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d, r = d // 2, r + 1
    for _ in range(16):
        x = pow(rng.randrange(2, n - 2), d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(rng: random.Random) -> int:
    while True:
        n = rng.getrandbits(1024) | (3 << 1022) | 1
        if _probable_prime(n, rng):
            return n


def _make_key():
    rng = random.Random(20261001)
    while True:
        p, q = _prime(rng), _prime(rng)
        phi = (p - 1) * (q - 1)
        try:
            d = pow(65537, -1, phi)
        except ValueError:
            continue
        return p * q, 65537, d


N, E, D = _make_key()


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _int_b64(value: int) -> str:
    return b64url(value.to_bytes((value.bit_length() + 7) // 8, "big"))


JWKS = {"keys": [{"kty": "RSA", "kid": "test-key", "alg": "RS256", "use": "sig", "n": _int_b64(N), "e": _int_b64(E)}]}


def sign_jwt(claims: dict, kid: str = "test-key", key=(N, D)) -> str:
    head = b64url(json.dumps({"alg": "RS256", "kid": kid, "typ": "JWT"}).encode())
    body = b64url(json.dumps(claims).encode())
    signing_input = f"{head}.{body}".encode()
    size = (key[0].bit_length() + 7) // 8
    payload = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(signing_input).digest()
    em = b"\x00\x01" + b"\xff" * (size - len(payload) - 3) + b"\x00" + payload
    signature = pow(int.from_bytes(em, "big"), key[1], key[0]).to_bytes(size, "big")
    return f"{head}.{body}.{b64url(signature)}"


# --- Fake servers ---------------------------------------------------------------------------------

class Server:
    def __init__(self, handler):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class FakeAuth(BaseHTTPRequestHandler):
    """auth.openai.com as the SIWC docs describe it."""
    config: dict = {}
    seen: dict = {}

    def log_message(self, *args):
        pass

    def _json(self, status: int, payload: dict | None):
        body = json.dumps(payload).encode() if payload is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/.well-known/jwks.json":
            return self._json(200, JWKS)
        if parsed.path == "/.well-known/openid-configuration":
            base = FakeAuth.config["base"]
            return self._json(200, {"issuer": base, "jwks_uri": base + "/.well-known/jwks.json",
                                    "revocation_endpoint": base + "/api/accounts/oauth/revoke"})
        if parsed.path == "/api/accounts/authorize":
            params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
            FakeAuth.seen.setdefault("authorize", []).append(params)
            cfg = FakeAuth.config
            back = {"state": params["state"]}
            if cfg.get("deny") or cfg.get("callback_error"):
                back["error"] = cfg.get("callback_error") or "access_denied"
            else:
                code = f"code-{len(FakeAuth.seen['authorize'])}"
                cfg.setdefault("codes", {})[code] = params
                back["code"] = code
                if params["client_id"] == chatgpt_auth.DYNAMIC_CLIENT:
                    back["client_id"] = cfg.get("issue", "oaiapp_test1")
                elif cfg.get("callback_client"):
                    back["client_id"] = cfg["callback_client"]
            if cfg.get("stray_first"):  # A request with a wrong state reaches the listener first.
                cfg["stray_first"] = False
                try:
                    urllib.request.urlopen(params["redirect_uri"] + "?code=evil&state=wrong", timeout=5).read()
                except urllib.error.HTTPError as ex:
                    ex.close()
            self.send_response(302)
            self.send_header("Location", params["redirect_uri"] + "?" + urllib.parse.urlencode(back))
            self.end_headers()
            return
        self._json(404, {"error": "not_found"})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        form = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(length).decode()).items()}
        path = urllib.parse.urlparse(self.path).path
        FakeAuth.seen.setdefault(path, []).append(form)
        cfg = FakeAuth.config
        if path == "/api/accounts/oauth/revoke":
            return self._json(200, None)
        if path != "/api/accounts/oauth/token":
            return self._json(404, {"error": "not_found"})
        if form.get("grant_type") == "authorization_code":
            started = cfg.get("codes", {}).get(form.get("code"))
            if not started:
                return self._json(400, {"error": "invalid_grant", "error_description": "unknown code"})
            if cfg.get("exchange_errors"):  # Refused codes, one per exchange, in order.
                return self._json(400, {"error": cfg["exchange_errors"].pop(0), "error_description": "refused"})
            challenge = b64url(hashlib.sha256(form["code_verifier"].encode()).digest())
            issued = cfg.get("issue", "oaiapp_test1") if started["client_id"] == chatgpt_auth.DYNAMIC_CLIENT else started["client_id"]
            if (challenge != started["code_challenge"] or form["redirect_uri"] != started["redirect_uri"]
                    or form["client_id"] != issued or form.get("resource") != chatgpt_auth.RESOURCE):
                return self._json(400, {"error": "invalid_grant", "error_description": "pkce/redirect/client mismatch"})
            return self._json(200, self._tokens(issued, started["nonce"], form))
        if form.get("grant_type") == "refresh_token":
            if cfg.get("refresh_error"):
                return self._json(400, {"error": cfg["refresh_error"]})
            if form.get("refresh_token") != cfg.get("refresh_token"):
                return self._json(400, {"error": "invalid_grant"})
            return self._json(200, self._tokens(form["client_id"], None, form))
        return self._json(400, {"error": "unsupported_grant_type"})

    def _tokens(self, client_id: str, nonce: str | None, form: dict) -> dict:
        cfg = FakeAuth.config
        cfg["serial"] = cfg.get("serial", 0) + 1
        now = int(time.time())
        claims = {"iss": cfg["base"], "aud": client_id, "sub": cfg.get("sub", "user-1"), "email": "someone@example.com",
                  "iat": now, "exp": now + 3600, "https://api.openai.com/auth": {"chatgpt_plan_type": "plus"}}
        if nonce:
            claims["nonce"] = nonce
        cfg["refresh_token"] = f"rt-{cfg['serial']}"
        return {"access_token": f"at-{cfg['serial']}", "refresh_token": cfg["refresh_token"],
                "id_token": sign_jwt(claims), "token_type": "Bearer", "expires_in": cfg.get("expires_in", 3600),
                "scope": cfg.get("scope", chatgpt_auth.SCOPES)}


def sse(*events: dict) -> bytes:
    return b"".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events)


def reply(text: str = "", calls=(), usage=None, reasoning: str = "enc-1", status: str = "completed") -> bytes:
    """A Responses stream: optional reasoning item, a message, function calls, then the terminal event."""
    events, output = [], []
    index = 0
    if reasoning:
        item = {"type": "reasoning", "id": f"rs_{index}", "summary": [{"type": "summary_text", "text": "thought"}],
                "encrypted_content": reasoning}
        events += [{"type": "response.output_item.added", "output_index": index, "item": {"type": "reasoning"}},
                   {"type": "response.reasoning_summary_text.done", "output_index": index, "text": "thought"},
                   {"type": "response.output_item.done", "output_index": index, "item": item}]
        output.append(item)
        index += 1
    if text:
        item = {"type": "message", "id": "msg_1", "role": "assistant", "phase": "commentary" if calls else "final_answer",
                "content": [{"type": "output_text", "text": text, "annotations": []}], "status": "completed"}
        events.append({"type": "response.output_item.added", "output_index": index,
                       "item": {"type": "message", "role": "assistant", "content": []}})
        for piece in (text[: len(text) // 2], text[len(text) // 2:]):
            events.append({"type": "response.output_text.delta", "output_index": index, "content_index": 0, "delta": piece})
        events.append({"type": "response.output_item.done", "output_index": index, "item": item})
        output.append(item)
        index += 1
    for call_id, name, arguments, namespace in calls:
        item = {"type": "function_call", "id": f"fc_{call_id}", "call_id": call_id, "name": name,
                "arguments": arguments, "status": "completed", **({"namespace": namespace} if namespace else {})}
        events += [{"type": "response.output_item.added", "output_index": index, "item": {**item, "arguments": ""}},
                   {"type": "response.function_call_arguments.delta", "output_index": index, "delta": arguments},
                   {"type": "response.output_item.done", "output_index": index, "item": item}]
        output.append(item)
        index += 1
    terminal = {"type": f"response.{status}", "response": {
        "id": "resp_1", "status": status, "output": output,
        "usage": usage or {"input_tokens": 1200, "input_tokens_details": {"cached_tokens": 1000},
                           "output_tokens": 300, "output_tokens_details": {"reasoning_tokens": 250}}}}
    if status == "incomplete":
        terminal["response"]["incomplete_details"] = {"reason": "max_output_tokens"}
    return sse(*events, terminal)


class FakeResponses(BaseHTTPRequestHandler):
    """api.openai.com/v1 for the ChatGPT plan: a queue of scripted replies; every request recorded."""
    script: list = []
    requests: list = []

    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        FakeResponses.requests.append({"path": self.path, "headers": dict(self.headers), "body": None})
        body = json.dumps({"models": [{"slug": "gpt-6.1-sol", "display_name": "GPT-6.1 Sol", "visibility": "list"},
                                      {"slug": "hidden", "display_name": "x", "visibility": "hide"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length))
        FakeResponses.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
        status, payload = FakeResponses.script.pop(0) if FakeResponses.script else (200, reply("done"))
        stall = 0.0
        if isinstance(payload, tuple):  # (first bytes, seconds to stall after them): a model thinking silently.
            payload, stall = payload
        self.send_response(status)
        self.send_header("Content-Type", "text/event-stream" if status == 200 else "application/json")
        self.send_header("x-request-id", f"req_{len(FakeResponses.requests)}")
        self.send_header("x-codex-turn-state", f"state-{len(FakeResponses.requests)}")
        self.send_header("Set-Cookie", "secret=1")
        if status != 200:
            self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        if stall:
            self.wfile.flush()
            time.sleep(stall)


def error_body(code: str, message: str = "nope", param: str | None = None) -> bytes:
    return json.dumps({"error": {"code": code, "message": message, "param": param, "type": "invalid_request_error"}}).encode()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.auth = Server(FakeAuth)
        self.api = Server(FakeResponses)
        FakeAuth.config, FakeAuth.seen = {"base": self.auth.url}, {}
        FakeResponses.script, FakeResponses.requests = [], []
        self.env = mock.patch.dict(os.environ, {"LOOPCUT_CHATGPT_DIR": self.tmp.name,
                                                "LOOPCUT_CHATGPT_AUTH_URL": self.auth.url,
                                                "LOOPCUT_CHATGPT_API_URL": self.api.url})
        self.env.start()
        os.environ.pop("LOOPCUT_CHATGPT_LOG_DIR", None)
        chatgpt_auth._discovery.clear()
        chatgpt_auth._jwks_cache.update(at=0.0, keys=[])
        chatgpt_responses._disabled.clear()
        chatgpt_responses.replay.clear()
        chatgpt_responses._turn_states.clear()
        self.no_wait = mock.patch.object(llm, "_wait", lambda seconds, cancelled: None)
        self.no_wait.start()

    def tearDown(self):
        self.no_wait.stop()
        self.env.stop()
        self.auth.close()
        self.api.close()
        self.tmp.cleanup()

    def browser(self, url: str) -> None:
        """The user's browser: follows the authorize redirect back to the loopback listener."""
        threading.Thread(target=lambda: urllib.request.urlopen(url, timeout=10).read(), daemon=True).start()

    def sign_in(self, **kwargs) -> dict:
        attempt = chatgpt_auth.SignIn(port=0, timeout=10, **kwargs)
        self.browser(attempt.start())
        return attempt.wait()


# --- Sign in with ChatGPT --------------------------------------------------------------------------

class SignInTest(Base):
    def test_browser_failure_releases_the_callback_port_for_retry(self):
        attempts = []
        original = chatgpt_auth.SignIn

        def attempt(**kwargs):
            result = original(port=0, **kwargs)
            attempts.append(result)
            return result

        with mock.patch.object(chatgpt_auth, "SignIn", side_effect=attempt):
            with self.assertRaises(OSError):
                chatgpt_auth.sign_in(lambda url: (_ for _ in ()).throw(OSError("No browser")))
        self.assertEqual(attempts[0]._server.server.fileno(), -1)
        self.assertTrue(self.sign_in()["signed_in"])

    def test_constants_agree_with_config(self):
        self.assertEqual(config.CHATGPT_PLAN_BASE_URL, chatgpt_auth.CHATGPT_PLAN_BASE_URL)
        self.assertEqual(config.CHATGPT_PLAN_KEY, chatgpt_auth.PLACEHOLDER_KEY)
        self.assertTrue(chatgpt_auth.is_chatgpt_plan(config.CHATGPT_PLAN_BASE_URL))
        self.assertFalse(chatgpt_auth.is_chatgpt_plan("https://api.openai.com/v1"))

    def test_pkce_challenge_is_s256_without_padding(self):
        verifier, challenge = chatgpt_auth.pkce_pair()
        self.assertGreaterEqual(len(verifier), 43)
        self.assertEqual(challenge, b64url(hashlib.sha256(verifier.encode()).digest()))
        self.assertNotIn("=", challenge)

    def test_first_registration_request(self):
        self.sign_in()
        params = FakeAuth.seen["authorize"][0]
        self.assertEqual(params["client_id"], "dynamic_agent_client")
        self.assertEqual(params["agent_name_hint"], "Loopcut")
        self.assertTrue(params["ext_agent_host_id"].startswith("urn:uuid:"))
        self.assertEqual(params["scope"], "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct")
        self.assertEqual(params["resource"], "https://api.openai.com/v1")
        self.assertEqual(params["code_challenge_method"], "S256")
        self.assertEqual(params["response_type"], "code")
        self.assertRegex(params["redirect_uri"], r"^http://127\.0\.0\.1:\d+/auth/callback$")
        self.assertNotIn("id_token_hint", params)
        form = FakeAuth.seen["/api/accounts/oauth/token"][0]
        self.assertEqual(form["client_id"], "oaiapp_test1")  # The issued id, never dynamic_agent_client.
        self.assertNotIn("client_secret", form)

    def test_sign_in_stores_a_private_record(self):
        info = self.sign_in()
        self.assertTrue(info["signed_in"])
        self.assertTrue(info["plan_usage"])
        self.assertEqual(info["plan_type"], "plus")
        path = Path(self.tmp.name) / "accounts.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((Path(self.tmp.name) / "host.json").stat().st_mode), 0o600)
        data = json.loads(path.read_text())
        record = data["accounts"]["oaiapp_test1"]
        self.assertEqual(data["active"], "oaiapp_test1")
        self.assertEqual(record["subject"], "user-1")
        self.assertEqual(record["access_token"], "at-1")
        self.assertEqual(record["refresh_token"], "rt-1")
        self.assertIn("chatgpt.tokens.use.direct", record["scopes"])
        self.assertEqual(record["ext_agent_host_id"], chatgpt_auth.host_id())
        self.assertNotIn("at-1", json.dumps(chatgpt_auth.status()))

    def test_host_id_is_stable(self):
        first = chatgpt_auth.host_id()
        self.assertEqual(first, chatgpt_auth.host_id())
        self.sign_in()
        self.sign_in()
        hosts = {p["ext_agent_host_id"] for p in FakeAuth.seen["authorize"]}
        self.assertEqual(hosts, {first})

    def test_returning_sign_in_reuses_the_issued_client(self):
        self.sign_in()
        self.sign_in()
        second = FakeAuth.seen["authorize"][1]
        self.assertEqual(second["client_id"], "oaiapp_test1")
        self.assertNotIn("agent_name_hint", second)
        self.assertIn("id_token_hint", second)
        self.assertEqual(second["login_hint"], "someone@example.com")

    def test_display_url_has_no_id_token_hint(self):
        self.sign_in()
        attempt = chatgpt_auth.SignIn(port=0, timeout=1)
        attempt.start()
        self.assertIn("id_token_hint", attempt.url)
        self.assertNotIn("id_token_hint", attempt.display_url)
        attempt._server.close()

    def test_callback_with_a_different_client_is_rejected(self):
        self.sign_in()
        FakeAuth.config["callback_client"] = "oaiapp_other"
        with self.assertRaises(chatgpt_auth.ChatGPTAuthError) as caught:
            self.sign_in()
        self.assertEqual(caught.exception.code, "client_mismatch")

    def test_another_account_on_a_saved_registration_is_rejected(self):
        self.sign_in()
        FakeAuth.config["sub"] = "user-2"
        with self.assertRaises(chatgpt_auth.ChatGPTAuthError) as caught:
            self.sign_in()
        self.assertEqual(caught.exception.code, "account_mismatch")
        self.assertEqual(chatgpt_auth.active_record()["subject"], "user-1")

    def test_new_account_registers_again(self):
        self.sign_in()
        FakeAuth.config.update(issue="oaiapp_test2", sub="user-2")
        self.sign_in(new_account=True)
        self.assertEqual(FakeAuth.seen["authorize"][1]["client_id"], "dynamic_agent_client")
        self.assertEqual(chatgpt_auth.active_record()["client_id"], "oaiapp_test2")
        self.assertEqual(len(chatgpt_auth.registrations()), 2)

    def test_declined_consent(self):
        FakeAuth.config["deny"] = True
        with self.assertRaises(chatgpt_auth.ChatGPTAuthError) as caught:
            self.sign_in()
        self.assertEqual(caught.exception.code, "access_denied")
        self.assertNotIn("/api/accounts/oauth/token", FakeAuth.seen)

    def refused(self, **kwargs) -> chatgpt_auth.ChatGPTAuthError:
        with self.assertRaises(chatgpt_auth.ChatGPTAuthError) as caught:
            self.sign_in(**kwargs)
        return caught.exception

    def test_a_refused_code_says_why_and_the_retry_keeps_the_issued_client(self):
        FakeAuth.config["exchange_errors"] = ["invalid_grant"]
        error = self.refused()
        self.assertEqual((error.code, error.stage, error.status), ("invalid_grant", "token", 400))
        self.assertEqual(str(error), chatgpt_auth.SIGN_IN_MESSAGES["invalid_grant"])
        self.assertIsNone(chatgpt_auth.active_record())
        self.assertTrue(self.sign_in()["signed_in"])
        retry = FakeAuth.seen["authorize"][1]
        self.assertEqual(retry["client_id"], "oaiapp_test1")  # Docs: a fresh authorization, same issued id.
        self.assertNotIn("agent_name_hint", retry)
        self.assertNotIn("login_hint", retry)
        self.assertEqual(chatgpt_auth.active_record()["client_id"], "oaiapp_test1")
        self.assertEqual(chatgpt_auth._pending_client(), "")

    def test_a_second_refusal_on_the_kept_client_registers_afresh(self):
        FakeAuth.config["exchange_errors"] = ["invalid_grant", "invalid_grant"]
        self.refused()
        self.assertEqual(self.refused().stage, "token")
        self.assertEqual(chatgpt_auth._pending_client(), "")
        self.assertTrue(self.sign_in()["signed_in"])
        self.assertEqual([p["client_id"] for p in FakeAuth.seen["authorize"]],
                         ["dynamic_agent_client", "oaiapp_test1", "dynamic_agent_client"])

    def test_a_timeout_keeps_the_client_and_another_account_registers_afresh(self):
        FakeAuth.config["exchange_errors"] = ["invalid_grant"]
        self.refused()
        attempt = chatgpt_auth.SignIn(port=0, timeout=0.3)
        attempt.start()
        with self.assertRaises(chatgpt_auth.ChatGPTAuthError):
            attempt.wait()
        self.assertEqual(chatgpt_auth._pending_client(), "oaiapp_test1")
        FakeAuth.config["issue"] = "oaiapp_test2"
        self.sign_in(new_account=True)
        self.assertEqual(FakeAuth.seen["authorize"][-1]["client_id"], "dynamic_agent_client")
        self.assertEqual(chatgpt_auth.active_record()["client_id"], "oaiapp_test2")
        self.assertEqual(chatgpt_auth._pending_client(), "")

    def test_callback_errors_have_no_stage_and_a_blocked_country_says_so(self):
        FakeAuth.config["callback_error"] = "unsupported_country_region_territory"
        error = self.refused()
        self.assertEqual((error.code, error.stage), ("unsupported_country_region_territory", ""))
        self.assertIn("isn't available in your country", str(error))
        self.assertNotIn("/api/accounts/oauth/token", FakeAuth.seen)
        self.assertEqual(chatgpt_auth._pending_client(), "")

    def test_sign_in_messages_fit_a_setup_row(self):
        for code, message in chatgpt_auth.SIGN_IN_MESSAGES.items():
            self.assertLessEqual(len(message), 70, code)  # onboarding._sentence's limit.
            self.assertTrue(message.endswith("."), code)

    def test_a_stray_callback_with_another_state_is_ignored(self):
        FakeAuth.config["stray_first"] = True
        self.assertTrue(self.sign_in()["signed_in"])
        self.assertEqual([f["code"] for f in FakeAuth.seen["/api/accounts/oauth/token"]], ["code-1"])

    def test_without_the_plan_scope_no_inference(self):
        FakeAuth.config["scope"] = "openid profile email offline_access"
        with self.assertRaises(chatgpt_auth.ChatGPTAuthError) as caught:
            self.sign_in()
        self.assertEqual(caught.exception.code, "no_plan_scope")
        self.assertFalse(chatgpt_auth.status()["plan_usage"])
        with self.assertRaises(chatgpt_auth.NotSignedIn):
            chatgpt_auth.access_token()

    def test_sign_in_times_out(self):
        attempt = chatgpt_auth.SignIn(port=0, timeout=0.3)
        attempt.start()
        with self.assertRaises(chatgpt_auth.ChatGPTAuthError) as caught:
            attempt.wait()
        self.assertEqual(caught.exception.code, "timeout")

    def test_overrides_only_reach_this_computer(self):
        with mock.patch.dict(os.environ, {"LOOPCUT_CHATGPT_API_URL": "https://evil.example/v1"}):
            with self.assertRaises(chatgpt_auth.ChatGPTAuthError):
                chatgpt_auth.api_base()


class IdTokenTest(Base):
    def claims(self, **changes):
        now = int(time.time())
        base = {"iss": self.auth.url, "aud": "oaiapp_x", "sub": "s", "nonce": "n1", "iat": now, "exp": now + 600}
        base.update(changes)
        return base

    def test_valid(self):
        self.assertEqual(chatgpt_auth.validate_id_token(sign_jwt(self.claims()), "oaiapp_x", "n1")["sub"], "s")

    def test_rejections(self):
        cases = {
            "wrong audience": (self.claims(aud="oaiapp_y"), "n1"),
            "wrong nonce": (self.claims(), "n2"),
            "expired": (self.claims(exp=int(time.time()) - 3600), "n1"),
            "other issuer": (self.claims(iss="https://elsewhere"), "n1"),
        }
        for name, (claims, nonce) in cases.items():
            with self.subTest(name), self.assertRaises(chatgpt_auth.ChatGPTAuthError):
                chatgpt_auth.validate_id_token(sign_jwt(claims), "oaiapp_x", nonce)

    def test_tampered_signature(self):
        head, body, signature = sign_jwt(self.claims()).split(".")
        forged = b64url(json.dumps(self.claims(sub="admin")).encode())
        with self.assertRaises(chatgpt_auth.ChatGPTAuthError):
            chatgpt_auth.validate_id_token(f"{head}.{forged}.{signature}", "oaiapp_x", "n1")

    def test_unknown_key(self):
        with self.assertRaises(chatgpt_auth.ChatGPTAuthError):
            chatgpt_auth.validate_id_token(sign_jwt(self.claims(), kid="other"), "oaiapp_x", "n1")


class TokenTest(Base):
    def expire(self):
        path = Path(self.tmp.name) / "accounts.json"
        data = json.loads(path.read_text())
        data["accounts"][data["active"]]["expires_at"] = time.time() + 10
        path.write_text(json.dumps(data))

    def test_fresh_token_is_used_as_is(self):
        self.sign_in()
        self.assertEqual(chatgpt_auth.access_token(), "at-1")
        self.assertNotIn("/api/accounts/oauth/token", {k for k in FakeAuth.seen if len(FakeAuth.seen[k]) > 1})

    def test_refresh_near_expiry_rotates(self):
        self.sign_in()
        self.expire()
        self.assertEqual(chatgpt_auth.access_token(), "at-2")
        form = FakeAuth.seen["/api/accounts/oauth/token"][-1]
        self.assertEqual(form, {"grant_type": "refresh_token", "client_id": "oaiapp_test1", "refresh_token": "rt-1",
                                "resource": "https://api.openai.com/v1"})
        record = chatgpt_auth.active_record()
        self.assertEqual(record["refresh_token"], "rt-2")
        self.assertGreater(record["expires_at"], time.time() + 3000)

    def test_a_refresh_another_process_made_is_reused(self):
        self.sign_in()
        self.expire()
        chatgpt_auth.refresh()
        count = len(FakeAuth.seen["/api/accounts/oauth/token"])
        chatgpt_auth.refresh()  # Fresh again: re-read under the lock, no second POST.
        self.assertEqual(len(FakeAuth.seen["/api/accounts/oauth/token"]), count)

    def test_dead_refresh_token_signs_out(self):
        self.sign_in()
        self.expire()
        FakeAuth.config["refresh_error"] = "refresh_token_reused"
        with self.assertRaises(chatgpt_auth.NotSignedIn):
            chatgpt_auth.access_token()
        record = chatgpt_auth.active_record()
        self.assertNotIn("refresh_token", record)
        self.assertEqual(record["client_id"], "oaiapp_test1")

    def test_network_failure_keeps_the_tokens(self):
        self.sign_in()
        self.expire()
        with mock.patch.dict(os.environ, {"LOOPCUT_CHATGPT_AUTH_URL": "http://127.0.0.1:9"}):
            with self.assertRaises(chatgpt_auth.ChatGPTAuthError):
                chatgpt_auth.refresh()
        self.assertEqual(chatgpt_auth.active_record()["refresh_token"], "rt-1")

    def test_sign_out_revokes_then_clears(self):
        self.sign_in()
        host = chatgpt_auth.host_id()
        self.assertTrue(chatgpt_auth.sign_out())
        self.assertEqual(FakeAuth.seen["/api/accounts/oauth/revoke"][0],
                         {"token": "rt-1", "token_type_hint": "refresh_token", "client_id": "oaiapp_test1"})
        record = chatgpt_auth.active_record()
        for key in ("access_token", "refresh_token", "id_token"):
            self.assertNotIn(key, record)
        self.assertEqual(record["client_id"], "oaiapp_test1")
        self.assertEqual(chatgpt_auth.host_id(), host)
        self.assertFalse(chatgpt_auth.signed_in())
        self.sign_in()  # Again with the kept registration, without an ID token hint.
        params = FakeAuth.seen["authorize"][-1]
        self.assertEqual(params["client_id"], "oaiapp_test1")
        self.assertNotIn("id_token_hint", params)

    def test_models(self):
        self.sign_in()
        self.assertEqual(chatgpt_auth.list_models(), [{"slug": "gpt-6.1-sol", "display_name": "GPT-6.1 Sol"}])
        self.assertEqual(FakeResponses.requests[0]["headers"]["Authorization"], "Bearer at-1")


# --- Responses adapter -----------------------------------------------------------------------------

TOOLS = [{"type": "function", "function": {"name": "run_python", "description": "Run code.",
                                           "parameters": {"type": "object", "properties": {"code": {"type": "string"}}}}},
         {"type": "function", "function": {"name": "capture_viewport", "description": "Look.",
                                           "parameters": {"type": "object", "properties": {}}}}]
IMAGE = "data:image/png;base64," + base64.b64encode(b"\x89PNG fake image").decode()


def conversation():
    return [
        {"role": "system", "content": "You are Loopcut."},
        {"role": "user", "content": [{"type": "text", "text": "Make it red"}, {"type": "image_url", "image_url": {"url": IMAGE}}]},
        {"role": "assistant", "content": "Looking first.", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "capture_viewport", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "Captured."},
        {"role": "user", "content": [{"type": "text", "text": "Viewport capture: the scene (after the step above)."},
                                     {"type": "image_url", "image_url": {"url": IMAGE}}]},
        {"role": "system", "content": "A late note."},
    ]


class MappingTest(unittest.TestCase):
    def setUp(self):
        chatgpt_responses.replay.clear()

    def test_image_detail_defaults_to_high_and_explicit_detail_survives(self):
        for kind in ("image_url", "input_image"):
            for detail in (None, "low", "high", "auto"):
                with self.subTest(kind=kind, detail=detail):
                    part = {"type": kind, "image_url": {"url": IMAGE} if kind == "image_url" else IMAGE}
                    if detail:
                        (part["image_url"] if kind == "image_url" else part)["detail"] = detail
                    original = json.loads(json.dumps(part))
                    _, items = chatgpt_responses.to_input([{"role": "user", "content": [part]}])
                    self.assertEqual(items[0]["content"][0]["detail"], detail or "high")
                    self.assertEqual(part, original)

    def test_input_items(self):
        instructions, items = chatgpt_responses.to_input(conversation())
        self.assertEqual(instructions, "You are Loopcut.")
        self.assertEqual(items[0], {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "Make it red"}, {"type": "input_image", "image_url": IMAGE, "detail": "high"}]})
        self.assertEqual(items[1], {"type": "message", "role": "assistant",
                                    "content": [{"type": "output_text", "text": "Looking first."}]})
        self.assertEqual(items[2], {"type": "function_call", "call_id": "call_1", "name": "capture_viewport",
                                    "namespace": "loopcut", "arguments": "{}"})
        # The capture rides in the tool's own output, as an image.
        self.assertEqual(items[3], {"type": "function_call_output", "call_id": "call_1", "output": [
            {"type": "input_text", "text": "Captured."},
            {"type": "input_text", "text": "Viewport capture: the scene (after the step above)."},
            {"type": "input_image", "image_url": IMAGE, "detail": "high"}]})
        self.assertEqual(items[4]["role"], "developer")
        self.assertNotIn("system", json.dumps([i.get("role") for i in items]))

    def test_file_preview_is_routed_into_tool_output(self):
        messages = conversation()
        messages[2]["tool_calls"][0]["function"] = {"name": "view_image", "arguments": '{"path":"preview.png"}'}
        label = "File preview: preview.png. This is an image file; viewing it does not change the open scene."
        messages[4]["content"][0]["text"] = label
        _, items = chatgpt_responses.to_input(messages)
        self.assertEqual(items[3], {"type": "function_call_output", "call_id": "call_1", "output": [
            {"type": "input_text", "text": "Captured."},
            {"type": "input_text", "text": label},
            {"type": "input_image", "image_url": IMAGE, "detail": "high"}]})
        self.assertEqual(items[4]["role"], "developer")
        _, fallback = chatgpt_responses.to_input(messages, tool_images=False)
        self.assertEqual(fallback[4]["role"], "user")
        self.assertEqual(fallback[4]["content"][0]["text"], label)

    def test_without_tool_images_the_capture_stays_a_user_message(self):
        _, items = chatgpt_responses.to_input(conversation(), tool_images=False)
        self.assertEqual(items[3], {"type": "function_call_output", "call_id": "call_1", "output": "Captured."})
        self.assertEqual(items[4]["role"], "user")

    def test_namespaced_tools(self):
        (namespace,) = chatgpt_responses.to_tools(TOOLS)
        self.assertEqual(namespace["type"], "namespace")
        self.assertEqual(namespace["name"], "loopcut")
        self.assertTrue(namespace["description"])
        self.assertEqual([t["name"] for t in namespace["tools"]], ["run_python", "capture_viewport"])
        self.assertTrue(all(t["type"] == "function" and t["strict"] is False for t in namespace["tools"]))
        self.assertEqual(chatgpt_responses.to_tools([]), [])

    def test_bare_names(self):
        for name, namespace in (("run_python", "loopcut"), ("loopcut.run_python", None),
                                ("loopcut__run_python", None), ("run_python", None)):
            self.assertEqual(chatgpt_responses.bare_name(name, namespace), "run_python")

    def test_body_follows_the_preview_rules(self):
        body = chatgpt_responses.request_body(model="gpt-6.1-sol", messages=conversation(), tools=TOOLS,
                                              reasoning_effort="xhigh", cache_key="loopcut-c-single", disabled=set())
        self.assertIs(body["store"], False)
        self.assertIs(body["stream"], True)
        self.assertEqual(body["reasoning"], {"effort": "xhigh", "summary": "auto"})
        self.assertEqual(body["include"], ["reasoning.encrypted_content"])
        self.assertEqual(body["prompt_cache_key"], "loopcut-c-single")
        for refused in ("temperature", "top_p", "max_output_tokens", "previous_response_id", "metadata", "user",
                        "prompt", "truncation", "messages", "max_tokens"):
            self.assertNotIn(refused, body)
        self.assertEqual(body["instructions"], "You are Loopcut.")


class StreamTest(Base):
    def setUp(self):
        super().setUp()
        self.sign_in()

    def stream(self, messages=None, **kwargs):
        seen = []
        completion = chatgpt_responses.stream(
            model="gpt-6.1-sol", messages=messages or conversation(), tools=TOOLS, reasoning_effort="high",
            on_text=seen.append, is_cancelled=lambda: False,
            headers=llm.correlation_headers("c0ffee", "single"), **kwargs)
        return completion, "".join(seen)

    def test_text_tool_calls_and_usage(self):
        FakeResponses.script = [(200, reply("Making it red.", calls=[("call_9", "run_python", '{"code": "x"}', "loopcut")]))]
        completion, streamed = self.stream()
        self.assertEqual(streamed, "Making it red.")
        self.assertEqual(completion.text, "Making it red.")
        self.assertEqual([(c.id, c.name, c.arguments) for c in completion.tool_calls],
                         [("call_9", "run_python", '{"code": "x"}')])
        self.assertEqual(completion.finish_reason, "tool_calls")
        self.assertEqual(completion.usage, {"input": 1200, "cached": 1000, "output": 300, "reasoning": 250})
        request = FakeResponses.requests[-1]
        self.assertEqual(request["path"], "/responses")
        self.assertEqual(request["headers"]["Authorization"], "Bearer at-1")
        self.assertEqual(request["body"]["prompt_cache_key"], "loopcut-c0ffee-single")
        headers = {k.lower(): v for k, v in request["headers"].items()}
        self.assertEqual(headers["session_id"], chatgpt_responses.session_id("loopcut-c0ffee-single"))
        self.assertRegex(headers["session_id"], r"^[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-")
        self.assertNotIn("x-loopcut-conversation", {k.lower() for k in request["headers"]})

    def test_text_only_finishes_with_stop(self):
        FakeResponses.script = [(200, reply("All done."))]
        completion, _ = self.stream()
        self.assertEqual((completion.text, completion.finish_reason, completion.tool_calls), ("All done.", "stop", []))

    def test_stream_chat_keeps_local_correlation_but_sends_no_loopcut_headers(self):
        FakeResponses.script = [(200, reply("Done."))]
        completion = llm.stream_chat(
            base_url=config.CHATGPT_PLAN_BASE_URL, api_key="unused", model="gpt-6.1-sol",
            messages=conversation(), tools=TOOLS, reasoning_effort="high", on_text=lambda _: None,
            is_cancelled=lambda: False, headers=llm.correlation_headers("conv-local", "single"), prompt_id="single")
        self.assertEqual(completion.text, "Done.")
        request = FakeResponses.requests[-1]
        self.assertEqual(request["body"]["prompt_cache_key"], "loopcut-conv-local-single")
        self.assertFalse([name for name in request["headers"] if name.lower().startswith("x-loopcut-")])

    def test_reasoning_and_phase_are_replayed_next_step(self):
        FakeResponses.script = [(200, reply("Step one.", calls=[("call_5", "run_python", "{}", "loopcut")], reasoning="ENC-A")),
                                (200, reply("Finished.", reasoning="ENC-B"))]
        messages = conversation()
        completion, _ = self.stream(messages)
        messages.append(completion.as_message())
        messages.append({"role": "tool", "tool_call_id": "call_5", "content": "ok"})
        self.stream(messages)
        items = FakeResponses.requests[-1]["body"]["input"]
        index = next(n for n, i in enumerate(items) if i.get("type") == "reasoning")
        self.assertEqual(items[index]["encrypted_content"], "ENC-A")
        self.assertEqual(items[index + 1]["phase"], "commentary")
        self.assertEqual(items[index + 2]["call_id"], "call_5")
        self.assertEqual(items[index + 3], {"type": "function_call_output", "call_id": "call_5", "output": "ok"})

    def test_turn_state_is_sent_back_within_a_turn(self):
        FakeResponses.script = [(200, reply("a", calls=[("call_t", "run_python", "{}", "loopcut")])), (200, reply("b")),
                                (200, reply("c")), (200, reply("d"))]
        messages = conversation()
        completion, _ = self.stream(messages)
        messages += [completion.as_message(), {"role": "tool", "tool_call_id": "call_t", "content": "ok"}]
        completion, _ = self.stream(messages)
        messages += [completion.as_message(), {"role": "user", "content": "Now make it blue."}]
        self.stream(messages)
        self.stream(messages + [{"role": "assistant", "content": "c"}, {"role": "user", "content": [
            {"type": "text", "text": "Viewport capture: blue (after the step above)."}]}])
        sent = [{k.lower(): v for k, v in r["headers"].items()}.get("x-codex-turn-state") for r in FakeResponses.requests
                if r["body"] is not None][-4:]
        self.assertEqual(sent, [None, "state-1", None, "state-3"])  # The first state of each turn; a capture is no turn.

    def test_usage_limit_mid_stream_is_not_retried(self):
        failed = sse({"type": "response.output_item.added", "output_index": 0, "item": {"type": "reasoning"}},
                     {"type": "response.failed", "response": {"status": "failed", "error": {
                         "code": "subscription_sharing_usage_limit_exceeded", "message": "limit"}}})
        FakeResponses.script = [(200, failed)]
        with self.assertRaises(llm.LLMError) as caught:
            self.stream()
        self.assertEqual(caught.exception.code, "subscription_sharing_usage_limit_exceeded")
        self.assertIn("chatgpt.com/settings/usage", str(caught.exception))
        self.assertEqual(len([r for r in FakeResponses.requests if r["body"]]), 1)

    def test_usage_limit_429_is_not_retried(self):
        FakeResponses.script = [(429, error_body("subscription_sharing_usage_limit_exceeded"))]
        with self.assertRaises(llm.LLMError) as caught:
            self.stream()
        self.assertEqual((caught.exception.status, caught.exception.code), (429, "subscription_sharing_usage_limit_exceeded"))
        self.assertEqual(len(FakeResponses.requests), 1)

    def test_busy_server_is_retried(self):
        FakeResponses.script = [(503, json.dumps({"detail": "busy"}).encode()), (200, reply("ok"))]
        completion, _ = self.stream()
        self.assertEqual(completion.text, "ok")
        self.assertEqual(len(FakeResponses.requests), 2)

    def test_not_eligible_says_why(self):
        FakeResponses.script = [(403, error_body("subscription_sharing_user_not_eligible"))]
        with self.assertRaises(llm.LLMError) as caught:
            self.stream()
        self.assertIn("plan, workspace and country", str(caught.exception))  # Not "Plus can": issue #2 was on Plus.
        self.assertEqual(len(FakeResponses.requests), 1)

    def test_401_refreshes_once_then_retries(self):
        FakeResponses.script = [(401, json.dumps({"detail": "expired"}).encode()), (200, reply("ok"))]
        completion, _ = self.stream()
        self.assertEqual(completion.text, "ok")
        self.assertEqual(FakeResponses.requests[-1]["headers"]["Authorization"], "Bearer at-2")

    def test_a_refused_optional_field_is_dropped(self):
        FakeResponses.script = [(400, error_body("subscription_sharing_unsupported_capability", "unsupported",
                                                 "prompt_cache_key")), (200, reply("ok"))]
        completion, _ = self.stream()
        self.assertEqual(completion.text, "ok")
        self.assertIn("prompt_cache_key", FakeResponses.requests[0]["body"])
        self.assertNotIn("prompt_cache_key", FakeResponses.requests[1]["body"])

    def test_refused_tool_images_fall_back_to_user_messages(self):
        FakeResponses.script = [(400, error_body("subscription_sharing_unsupported_capability", "bad",
                                                 "input[3].output")), (200, reply("ok"))]
        self.stream()
        retried = FakeResponses.requests[1]["body"]["input"]
        self.assertEqual(retried[3]["output"], "Captured.")
        self.assertEqual(retried[4]["role"], "user")

    def test_other_400_is_shown_not_retried(self):
        FakeResponses.script = [(400, error_body("invalid_value", "bad model", "model"))]
        with self.assertRaises(llm.LLMError):
            self.stream()
        self.assertEqual(len(FakeResponses.requests), 1)

    def test_stream_without_completed_is_retried(self):
        broken = sse({"type": "response.created", "response": {"status": "in_progress"}})
        FakeResponses.script = [(200, broken), (200, reply("ok"))]
        completion, _ = self.stream()
        self.assertEqual(completion.text, "ok")

    def test_empty_stream_exhaustion_keeps_an_exception_cause(self):
        FakeResponses.script = [(200, b"")] * (len(llm.RETRY_DELAYS) + 1)
        with self.assertRaises(llm.LLMError) as caught:
            self.stream()
        self.assertEqual(caught.exception.code, llm.CONNECTION_LOST)
        self.assertIsInstance(caught.exception.__cause__, EOFError)
        self.assertIn("before response.completed", str(caught.exception))

    def test_partial_stream_raises_connection_error_instead_of_type_error(self):
        partial = sse({"type": "response.output_text.delta", "output_index": 0, "delta": "hello"})
        for retry in (False, True):
            with self.subTest(retry=retry):
                FakeResponses.requests = []
                FakeResponses.script = [(200, partial)] * 2
                restarted = []
                with self.assertRaises(llm.LLMError) as caught:
                    self.stream(**({"on_retry": lambda: restarted.append(True)} if retry else {}))
                self.assertEqual(caught.exception.code, llm.CONNECTION_LOST)
                self.assertIsInstance(caught.exception.__cause__, EOFError)
                self.assertIn("while it was replying", str(caught.exception))
                self.assertEqual(len(FakeResponses.requests), 2 if retry else 1)
                self.assertEqual(len(restarted), int(retry))

    def test_disconnect_preserves_the_original_exception(self):
        disconnected = ConnectionResetError("connection reset")
        def interrupted(response, *args):
            response.close()
            self.interrupted(disconnected)
        with mock.patch.object(chatgpt_responses, "_read", side_effect=interrupted):
            with self.assertRaises(llm.LLMError) as caught:
                self.stream()
        self.assertIs(caught.exception.__cause__, disconnected)

    @staticmethod
    def interrupted(cause):
        raise chatgpt_responses._Interrupted(False) from cause

    def test_unsupported_default_retries_once_and_remembers_the_account(self):
        refusal = error_body("invalid_request_error", "The 'gpt-6.1-sol' model is not supported when using Codex "
                             "with a ChatGPT account.")
        FakeResponses.script = [(400, refusal), (200, reply("done")), (200, reply("again"))]
        with mock.patch.object(chatgpt_auth, "list_models", return_value=[{"slug": "gpt-6-sol"}]) as catalog, \
                mock.patch.object(chatgpt_ready, "model_used") as used:
            self.assertEqual(self.stream()[0].text, "done")
            self.assertEqual(self.stream()[0].text, "again")
        self.assertEqual([r["body"]["model"] for r in FakeResponses.requests],
                         ["gpt-6.1-sol", "gpt-6-sol", "gpt-6-sol"])
        catalog.assert_called_once()
        used.assert_called_with(chatgpt_ready.account_key(), "gpt-6.1-sol", "gpt-6-sol")
        self.assertTrue(chatgpt_ready.verified(chatgpt_ready.account_key(), "gpt-6-sol"))
        self.assertFalse(chatgpt_ready.verified(("different", "issuer", "subject"), "gpt-6-sol"))

    def test_unsupported_fallback_never_walks_the_catalog(self):
        FakeResponses.script = [(400, error_body("unsupported_model"))] * 3
        with mock.patch.object(chatgpt_auth, "list_models", return_value=[{"slug": "gpt-6-sol"},
                                                                        {"slug": "gpt-5.4"}]), \
                self.assertRaises(llm.LLMError):
            self.stream()
        self.assertEqual(len(FakeResponses.requests), 2)
        self.assertFalse(chatgpt_ready.verified(chatgpt_ready.account_key(), "gpt-6-sol"))

    def test_a_known_accepted_model_is_used_without_listing(self):
        chatgpt_ready.remember(chatgpt_ready.account_key(), "gpt-6-sol", True)
        FakeResponses.script = [(400, error_body("unsupported_model")), (200, reply("done"))]
        with mock.patch.object(chatgpt_auth, "list_models") as catalog:
            self.stream()
        catalog.assert_not_called()
        self.assertEqual(FakeResponses.requests[-1]["body"]["model"], "gpt-6-sol")

    def test_readiness_is_small_and_tool_free(self):
        FakeResponses.script = [(200, reply("OK"))]
        result = chatgpt_ready.check("gpt-6.1-sol")
        self.assertEqual(result["code"], "ready")
        self.assertEqual(FakeResponses.requests[0]["body"], {
            "model": "gpt-6.1-sol", "input": [{"role": "user", "content": "Reply OK."}],
            "store": False, "stream": True})

    def test_readiness_falls_back_once_and_remembers_it(self):
        FakeResponses.script = [(400, error_body("unsupported_model")), (200, reply("OK"))]
        with mock.patch.object(chatgpt_auth, "list_models", return_value=[{"slug": "gpt-6-sol"}]):
            result = chatgpt_ready.check("gpt-6.1-sol")
        self.assertEqual((result["code"], result["model"]), ("ready", "gpt-6-sol"))
        self.assertIn("so Loopcut used gpt-6-sol", result["note"])

    def test_an_explicit_recheck_can_restore_a_newly_available_model(self):
        account = chatgpt_ready.account_key()
        chatgpt_ready.remember(account, "gpt-6-sol", True, requested="gpt-6.1-sol")
        FakeResponses.script = [(200, reply("OK"))]
        result = chatgpt_ready.check("gpt-6.1-sol", fresh=True)
        self.assertEqual((result["code"], result["model"]), ("ready", "gpt-6.1-sol"))
        self.assertEqual(chatgpt_ready.effective_model(account, "gpt-6.1-sol"), "gpt-6.1-sol")

    def test_readiness_specific_refusals_do_not_retry(self):
        for code in ("invalid_grant", "subscription_sharing_user_not_eligible",
                     "unsupported_country_region_territory"):
            with self.subTest(code=code):
                FakeResponses.requests = []
                FakeResponses.script = [(403, error_body(code))]
                result = chatgpt_ready.check("gpt-6.1-sol")
                self.assertEqual(result["code"], code)
                self.assertEqual(len(FakeResponses.requests), 1)
                self.assertIn("Loopcut Fast", chatgpt_ready.message(code))

    def test_readiness_offline_and_incomplete_are_never_cached_as_ready(self):
        with mock.patch.object(chatgpt_responses, "_post", side_effect=urllib.error.URLError("offline")) as post, \
                mock.patch.object(llm, "_wait") as wait:
            self.assertEqual(chatgpt_ready.check("gpt-6.1-sol")["code"], "offline")
            post.assert_called_once()
            wait.assert_not_called()
        FakeResponses.script = [(200, reply("partial", status="incomplete"))]
        self.assertEqual(chatgpt_ready.check("gpt-6.1-sol")["code"], "check_incomplete")
        self.assertFalse(chatgpt_ready.verified(chatgpt_ready.account_key(), "gpt-6.1-sol"))

    def test_readiness_does_not_mark_an_account_that_changed(self):
        account = chatgpt_ready.account_key()
        FakeResponses.script = [(200, reply("OK"))]
        with mock.patch.object(chatgpt_ready, "account_key", side_effect=[account, account, account, ("new", "", "")]):
            result = chatgpt_ready.check("gpt-6.1-sol")
        self.assertEqual(result["code"], "cancelled")
        self.assertFalse(chatgpt_ready.verified(account, "gpt-6.1-sol"))

    def test_incomplete_is_a_capped_reply(self):
        FakeResponses.script = [(200, reply("partial", status="incomplete"))]
        completion, _ = self.stream()
        self.assertEqual(completion.finish_reason, "length")

    def test_error_event(self):
        FakeResponses.script = [(200, sse({"type": "error", "code": "invalid_prompt", "message": "boom"}))]
        with self.assertRaises(llm.LLMError) as caught:
            self.stream()
        self.assertEqual(caught.exception.code, "invalid_prompt")
        self.assertEqual(len(FakeResponses.requests), 1)

    def test_transient_failure_in_the_stream_is_retried(self):
        failed = sse({"type": "response.failed", "response": {"status": "failed", "error": {
            "code": "subscription_sharing_usage_unavailable", "message": "try later"}}})
        FakeResponses.script = [(200, failed), (200, reply("ok"))]
        completion, _ = self.stream()
        self.assertEqual(completion.text, "ok")

    def _drops(self, *failures):
        """chatgpt_responses._post raising each failure in turn, then the fake server as usual."""
        real, calls = chatgpt_responses._post, []

        def post(*args, **kwargs):
            calls.append(1)
            if len(calls) <= len(failures):
                raise failures[len(calls) - 1]
            return real(*args, **kwargs)
        patcher = mock.patch.object(chatgpt_responses, "_post", post)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def test_a_tls_alert_in_place_of_the_status_line_is_retried(self):
        """Persona round 1 (2026-10-16): api.openai.com cut large uploads with a bad_record_mac alert, read by
        getresponse() as ssl.SSLError (urlopen does not wrap it), and the turn ended as 'Internal error'."""
        import ssl
        FakeResponses.script = [(200, reply("ok"))]
        calls = self._drops(ssl.SSLError(1, "[SSL: SSLV3_ALERT_BAD_RECORD_MAC] ssl/tls alert bad record mac"))
        completion, _ = self.stream()
        self.assertEqual((completion.text, len(calls)), ("ok", 2))

    def test_broken_pipe_reset_and_a_cut_off_status_line_are_retried(self):
        import http.client
        FakeResponses.script = [(200, reply("ok"))]
        calls = self._drops(urllib.error.URLError(BrokenPipeError(32, "Broken pipe")),
                            ConnectionResetError(54, "Connection reset by peer"), http.client.IncompleteRead(b""))
        completion, _ = self.stream()
        self.assertEqual((completion.text, len(calls)), ("ok", 4))

    def test_a_broken_pipe_is_retried_with_smaller_images(self):
        # 2026-10-17: every try re-sent the same 6-7 MB of images, and every try was cut off with a broken pipe.
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import fake_imbuf
        from loopcut import image_budget
        photo = base64.b64encode(fake_imbuf.make(1568, 882, kind="JPEG", quality=85)).decode()
        looks = [{"role": "user", "content": [{"type": "text", "text": f"Viewport capture: look {n}."},
                                              {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{photo}"}}]}
                 for n in range(6)]
        FakeResponses.script = [(200, reply("ok"))]
        sizes, images, real = [], [], chatgpt_responses._post

        def post(body, *args, **kwargs):
            sizes.append(len(json.dumps(body)))
            images.append((json.dumps(body).count('"input_image"'), json.dumps(body).count("omitted to keep the request small")))
            if len(sizes) == 1:
                raise urllib.error.URLError(BrokenPipeError(32, "Broken pipe"))
            return real(body, *args, **kwargs)
        with mock.patch.dict(sys.modules, {"imbuf": fake_imbuf}), mock.patch.object(chatgpt_responses, "_post", post):
            completion, _ = self.stream([*conversation(), *looks])
        self.assertEqual((completion.text, len(sizes)), ("ok", 2))
        self.assertLess(sizes[0], image_budget.REQUEST_BYTES + 50_000, "the first try is fitted already")
        self.assertLess(sizes[1], min(sizes[0], image_budget.REQUEST_BYTES // 2 + 50_000), "the retry: half the budget")
        self.assertEqual(sum(images[1]), sum(images[0]), "every look sent, smaller, or named in a note")

    def test_a_connection_that_keeps_dropping_ends_in_plain_words_with_try_again(self):
        import ssl
        calls = self._drops(*[ssl.SSLError(1, "[SSL: SSLV3_ALERT_BAD_RECORD_MAC] bad record mac")] * 9)
        with self.assertRaises(llm.LLMError) as caught:
            self.stream()
        self.assertEqual(len(calls), len(llm.RETRY_DELAYS) + 1, "the existing budget: one try and three retries")
        self.assertEqual(caught.exception.code, llm.CONNECTION_LOST)
        text = str(caught.exception)
        self.assertTrue(text.startswith("The connection to ChatGPT dropped"), text)
        self.assertIn("Try again", text)
        self.assertNotIn("Internal error", text)

    def test_a_refused_certificate_is_not_retried(self):
        import ssl
        calls = self._drops(urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate verify failed")))
        with self.assertRaises(llm.LLMError) as caught:
            self.stream()
        self.assertEqual(len(calls), 1)
        self.assertNotEqual(caught.exception.code, llm.CONNECTION_LOST)

    def test_an_error_answer_whose_body_breaks_off_still_counts_by_its_status(self):
        import io
        import ssl

        class Broken(io.RawIOBase):
            def readable(self):
                return True

            def readinto(self, buffer):
                raise ssl.SSLError(1, "[SSL: SSLV3_ALERT_BAD_RECORD_MAC] bad record mac")
        busy = urllib.error.HTTPError("https://api.openai.com/v1/responses", 503, "busy", {}, io.BufferedReader(Broken()))
        FakeResponses.script = [(200, reply("ok"))]
        calls = self._drops(busy)
        completion, _ = self.stream()
        self.assertEqual((completion.text, len(calls)), ("ok", 2))

    def test_replay_falls_back_without_ids_then_without_replay(self):
        FakeResponses.script = [(200, reply("one", calls=[("call_r", "run_python", "{}", "loopcut")], reasoning="ENC-R"))]
        messages = conversation()
        completion, _ = self.stream(messages)
        messages += [completion.as_message(), {"role": "tool", "tool_call_id": "call_r", "content": "ok"}]
        missing = error_body("invalid_request_error", "Item with id 'rs_0' not found. Items are not persisted when "
                             "`store` is set to false.", "input")
        FakeResponses.script = [(400, missing), (400, missing), (200, reply("two"))]
        completion, _ = self.stream(messages)
        self.assertEqual(completion.text, "two")
        second, third = (FakeResponses.requests[n]["body"]["input"] for n in (2, 3))
        reasoning = [i for i in second if i.get("type") == "reasoning"]
        self.assertEqual(reasoning[0]["encrypted_content"], "ENC-R")
        self.assertNotIn("id", reasoning[0])
        self.assertEqual([i for i in third if i.get("type") == "reasoning"], [])

    def test_cancel_interrupts_a_silent_stream(self):
        FakeResponses.script = [(200, (sse({"type": "response.created", "response": {}}), 3.0))]
        started = time.monotonic()
        with self.assertRaises(llm.Cancelled):
            chatgpt_responses.stream(model="m", messages=conversation(), tools=TOOLS, reasoning_effort="low",
                                     on_text=lambda _: None, is_cancelled=lambda: time.monotonic() - started > 0.3)
        self.assertLess(time.monotonic() - started, 2.0)

    def test_wall_clock_limit_ends_like_a_capped_reply(self):
        FakeResponses.script = [(200, (sse({"type": "response.created", "response": {}}), 3.0))]
        started = time.monotonic()
        completion, _ = self.stream(call_max_seconds=0.4)
        self.assertTrue(completion.timed_out)
        self.assertEqual(completion.finish_reason, "length")
        self.assertLess(time.monotonic() - started, 2.0)

    def test_not_signed_in(self):
        chatgpt_auth.sign_out()
        with self.assertRaises(llm.LLMError) as caught:
            self.stream()
        self.assertEqual(caught.exception.code, "not_signed_in")
        self.assertEqual([r for r in FakeResponses.requests if r["body"]], [])

    def test_stream_chat_dispatches_on_the_plan_base_url(self):
        FakeResponses.script = [(200, reply("via llm"))]
        completion = llm.stream_chat(
            base_url=config.CHATGPT_PLAN_BASE_URL, api_key=config.CHATGPT_PLAN_KEY, model="gpt-6.1-sol",
            messages=conversation(), tools=TOOLS, reasoning_effort="low", on_text=lambda _: None,
            is_cancelled=lambda: False, max_tokens=32000, headers=llm.correlation_headers("abc", "single"))
        self.assertEqual(completion.text, "via llm")
        body = FakeResponses.requests[-1]["body"]
        self.assertNotIn("max_output_tokens", body)
        self.assertEqual(FakeResponses.requests[-1]["headers"]["Authorization"], "Bearer at-1")

    def test_transcript_keeps_requests_and_replies_without_secrets(self):
        log = Path(self.tmp.name) / "log"
        FakeResponses.script = [(200, reply("one", calls=[("call_1x", "run_python", "{}", "loopcut")], reasoning="SECRETLY-LONG")),
                                (200, reply("two"))]
        with mock.patch.dict(os.environ, {"LOOPCUT_CHATGPT_LOG_DIR": str(log)}):
            messages = conversation()
            completion, _ = self.stream(messages)
            messages += [completion.as_message(), {"role": "tool", "tool_call_id": "call_1x", "content": "ok"}]
            self.stream(messages)
        files = sorted(p.name for p in log.iterdir())
        self.assertEqual(len([f for f in files if f.endswith(".request.json")]), 2)
        self.assertEqual(len([f for f in files if f.endswith(".response.json")]), 2)
        self.assertIn("images", files)
        text = "".join(p.read_text() for p in log.glob("*.json*"))
        for secret in ("at-1", "rt-1", "Bearer", "SECRETLY-LONG", "secret=1", "base64,"):
            self.assertNotIn(secret, text)
        second = json.loads(sorted(log.glob("*.request.json"))[1].read_text())
        self.assertGreater(second["input_extends_previous"], 0)
        lines = (log / "requests.jsonl").read_text().splitlines()
        self.assertEqual(json.loads(lines[0])["usage"]["cached"], 1000)


class ConfigTest(unittest.TestCase):
    def test_plan_base_url_needs_no_key(self):
        with mock.patch.dict(os.environ, {"LOOPCUT_BASE_URL": "chatgpt-plan", "LOOPCUT_API_KEY": "",
                                          "LOOPCUT_MODEL": "", "LOOPCUT_ENV_FILE": ""}), \
                mock.patch.object(config, "_preference_values", lambda: {}), \
                mock.patch.object(config, "_find_env_file", lambda: None):
            cfg = config.load()
        self.assertEqual((cfg.base_url, cfg.api_key, cfg.model),
                         (config.CHATGPT_PLAN_BASE_URL, "chatgpt-plan", "gpt-6.1-sol"))


if __name__ == "__main__":
    unittest.main()
