"""The own-key check (own_key.py, 0.2.20): each provider answer as one plain sentence, the paid probe once per key,
base URL and model, and the same sentence for a turn that fails the same way. Local mock servers only: no real key
and no real provider."""

import json
import os
import socket
import sys
import tempfile
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

# agent.py only needs bpy for the session store; give it a stand-in (as test_agent_loop does).
fake_bpy = types.ModuleType("bpy")
fake_bpy.app = types.SimpleNamespace(driver_namespace={})
sys.modules.setdefault("bpy", fake_bpy)

from loopcut import agent, config, llm, own_key, state  # noqa: E402

KEY = "sk-" + "test-own-key-0123456789abcdef"
MASKED = "sk-test-" + "*" * 20 + "cdef"


class Provider(BaseHTTPRequestHandler):
    """Answers each (method, path) from the server's routes: (status, JSON or bytes). Records what arrived."""

    def do_GET(self):
        self._answer()

    def do_POST(self):
        self._answer()

    def _answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.seen.append((self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, body))
        route = self.server.routes.get((self.command, self.path.split("?")[0]),
                                       (404, {"error": {"message": "Not Found"}}))
        status, payload = route() if callable(route) else route
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def models(*names):
    return 200, {"object": "list", "data": [{"id": n, "object": "model"} for n in names]}


def error(status, message, code="", kind="invalid_request_error"):
    return status, {"error": {"message": message, "type": kind, "code": code or None}}


ANSWER = (200, {"choices": [{"index": 0, "message": {"role": "assistant", "content": "O"}, "finish_reason": "length"}]})


def closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class CheckTest(unittest.TestCase):
    def serve(self, routes):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        server.routes, server.seen = dict(routes), []
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}/v1", server

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def probes(self, server):
        return [s for s in server.seen if s[0] == "POST"]

    def check(self, routes, model="gpt-x", provider=("OPENAI", "OpenAI", "https://api.openai.com/v1"), key=KEY,
              **kwargs):
        url, server = self.serve(routes)
        about = own_key.who(*provider, key)
        return own_key.check(about, url, key, model, self.folder, **kwargs), server, about

    def test_each_answer_is_one_plain_sentence(self):
        cases = [
            ("401", {("GET", "/v1/models"): error(401, f"Incorrect API key provided: {MASKED}.", "invalid_api_key")},
             own_key.REFUSED, "This key was refused by OpenAI. Check it or make a new one.", False),
            ("403", {("GET", "/v1/models"): error(403, "Forbidden", kind="permission_error")},
             own_key.REFUSED, "This key was refused by OpenAI. Check it or make a new one.", False),
            ("not listed", {("GET", "/v1/models"): models("gpt-a", "gpt-b")},
             own_key.NO_MODEL, 'No model called "gpt-x" on this key. Pick one from the list.', False),
            ("402", {("GET", "/v1/models"): models("gpt-x"),
                     ("POST", "/v1/chat/completions"): error(402, "Insufficient credits. Add more using …")},
             own_key.NO_CREDIT, "This OpenAI account has no credit left.", True),
            ("insufficient_quota", {("GET", "/v1/models"): models("gpt-x"), ("POST", "/v1/chat/completions"): error(
                429, "You exceeded your current quota, please check your plan.", "insufficient_quota")},
             own_key.NO_CREDIT, "This OpenAI account has no credit left.", True),
            ("404 model", {("POST", "/v1/chat/completions"): error(
                404, "The model `gpt-x` does not exist or you do not have access to it.", "model_not_found")},
             own_key.NO_MODEL, 'No model called "gpt-x" on this key. Pick one from the list.', True),
            ("no tools", {("GET", "/v1/models"): models("gpt-x"), ("POST", "/v1/chat/completions"): error(
                400, "registry.ollama.ai/library/gpt-x:latest does not support tools")},
             own_key.NO_TOOLS, "This model can't use tools, which Loopcut needs. Pick another one.", True),
            ("429", {("GET", "/v1/models"): models("gpt-x"), ("POST", "/v1/chat/completions"): error(
                429, "Rate limit reached for requests", "rate_limit_exceeded")},
             own_key.RATE_LIMITED, "OpenAI is limiting this key right now. Try again in a minute.", True),
            ("success", {("GET", "/v1/models"): models("gpt-x"), ("POST", "/v1/chat/completions"): ANSWER},
             own_key.OK, "Works with gpt-x.", True),
            ("other", {("GET", "/v1/models"): models("gpt-x"), ("POST", "/v1/chat/completions"): error(
                400, "Unsupported parameter: 'max_tokens' is not supported with this model.")},
             own_key.OTHER, "OpenAI answered: Unsupported parameter: 'max_tokens' is not supported with this model.",
             True),
        ]
        for name, routes, code, text, probed in cases:
            with self.subTest(name):
                result, server, _ = self.check(routes)
                self.assertEqual((result["code"], result["text"]), (code, text))
                self.assertEqual(len(self.probes(server)), int(probed))  # Step 2 is skipped when step 1 fails.

    def test_a_local_server_that_is_not_running_is_named_with_its_address(self):
        port = closed_port()
        for provider, text in ((("OLLAMA", "Ollama (this computer)"), f"Ollama isn't running at 127.0.0.1:{port}."),
                               (("CUSTOM", "Custom"), f"Nothing is running at 127.0.0.1:{port}.")):
            with self.subTest(provider[0]):
                url = f"http://127.0.0.1:{port}/v1"
                about = own_key.who(*provider, url)
                result = own_key.check(about, url, "", "llama3.2", self.folder)
                self.assertEqual((result["code"], result["text"]), (own_key.NOT_RUNNING, text))

    def test_ollama_takes_a_name_without_its_latest_tag_and_says_when_a_model_is_missing(self):
        url, server = self.serve({("GET", "/v1/models"): models("llama3.2:latest"),
                                  ("POST", "/v1/chat/completions"): ANSWER})
        about = own_key.who("OLLAMA", "Ollama (this computer)", url)
        self.assertEqual(own_key.check(about, url, "", "llama3.2", self.folder)["code"], own_key.OK)
        missing = own_key.check(about, url, "", "qwen3", self.folder)
        self.assertEqual(missing["text"], 'Ollama has no model called "qwen3". Pick one from the list.')

    def test_the_probe_is_one_token_with_one_trivial_tool(self):
        result, server, _ = self.check({("GET", "/v1/models"): models("gpt-x"),
                                        ("POST", "/v1/chat/completions"): ANSWER})
        self.assertEqual(result["code"], own_key.OK)
        (_, path, headers, body), = self.probes(server)
        sent = json.loads(body)
        self.assertEqual((path, sent["model"], sent["max_tokens"]), ("/v1/chat/completions", "gpt-x", 1))
        self.assertEqual([t["function"]["name"] for t in sent["tools"]], ["ping"])
        self.assertEqual(headers["authorization"], f"Bearer {KEY}")  # Only to the user's own endpoint.
        # Both requests say Loopcut, as the chat's own do (Kimi Code checks the client).
        self.assertEqual([s[2]["user-agent"] for s in server.seen], ["Loopcut (Blender add-on)"] * 2)

    def test_a_chat_request_says_loopcut_in_the_user_agent(self):
        url, server = self.serve({("POST", "/v1/chat/completions"): error(400, "Bad request")})
        with self.assertRaises(llm.LLMError):
            llm.stream_chat(base_url=url, api_key=KEY, model="kimi-for-coding",
                            messages=[{"role": "user", "content": "Hi"}], tools=[], reasoning_effort="low",
                            on_text=lambda text: None, is_cancelled=lambda: False, retry_delays=())
        (_, _, headers, _), = server.seen
        self.assertEqual(headers["user-agent"], llm.USER_AGENT)
        self.assertEqual(llm.USER_AGENT, "Loopcut (Blender add-on)")

    def test_a_provider_that_wants_more_tokens_is_asked_once_more_with_16(self):
        answers = iter([error(400, "max_tokens must be at least 16"), ANSWER])
        result, server, _ = self.check({("GET", "/v1/models"): models("gpt-x"),
                                        ("POST", "/v1/chat/completions"): lambda: next(answers)})
        self.assertEqual(result["code"], own_key.OK)
        self.assertEqual([json.loads(s[3])["max_tokens"] for s in self.probes(server)], [1, 16])

    def test_custom_is_named_by_its_host_and_no_message_names_another_provider_or_holds_the_key(self):
        about = own_key.who("CUSTOM", "Custom", "https://llm.example.test:8443/v1", KEY)
        self.assertEqual(own_key.message(own_key.REFUSED, about),
                         "This key was refused by llm.example.test:8443. Check it or make a new one.")
        # Loopcut's own vendors, and the other providers in Preferences: only the user's own provider is named.
        others = ("fal", "RunPod", "Z.ai", "Xiaomi", "MiMo", "GLM", "ElevenLabs", "Gemini", "Vercel", "OpenRouter",
                  "Ollama", "Meta")
        for provider in (("OPENAI", "OpenAI", "https://api.openai.com/v1"), ("CUSTOM", "Custom", "https://x.test/v1"),
                         ("ANTHROPIC", "Anthropic (Claude)", config.ANTHROPIC_BASE_URL)):
            about = own_key.who(*provider, KEY)
            for code in (own_key.OK, own_key.KEY_OK, own_key.REFUSED, own_key.NO_MODEL, own_key.NO_CREDIT,
                         own_key.NO_TOOLS, own_key.RATE_LIMITED, own_key.SLOW, own_key.UNREACHABLE, own_key.OTHER):
                text = own_key.message(code, about, "m", own_key.scrub(f"Bad key {KEY} ({MASKED})", KEY))
                with self.subTest(provider=provider[0], code=code):
                    self.assertNotIn(KEY, text)
                    self.assertNotIn(KEY[:8], text)
                    self.assertFalse([v for v in others if v in text])
                    if code in (own_key.REFUSED, own_key.NO_CREDIT, own_key.RATE_LIMITED, own_key.SLOW,
                                own_key.UNREACHABLE, own_key.OTHER):
                        self.assertIn(about["name"], text)
        self.assertEqual(own_key.who("ANTHROPIC", "Anthropic (Claude)", config.ANTHROPIC_BASE_URL)["name"], "Anthropic")

    def test_a_reason_that_echoes_the_key_is_shown_without_it(self):
        result, _, _ = self.check({("GET", "/v1/models"): models("gpt-x"), ("POST", "/v1/chat/completions"): error(
            400, f"Bad request for key {KEY}; masked {MASKED}")})
        self.assertEqual(result["code"], own_key.OTHER)
        self.assertNotIn(KEY, result["text"])
        self.assertNotIn(KEY[:8], result["text"])
        self.assertTrue(result["text"].startswith("OpenAI answered: Bad request for key …"))

    def test_anthropic_lists_with_its_own_api_and_probes_the_messages_api(self):
        url, server = self.serve({("GET", "/models"): models("claude-opus-5-5"),
                                  ("POST", "/messages"): error(400, "Your credit balance is too low to access the "
                                                                    "Anthropic API.")})
        about = own_key.who("ANTHROPIC", "Anthropic (Claude)", config.ANTHROPIC_BASE_URL, KEY)
        with mock.patch.dict(os.environ, {"LOOPCUT_ANTHROPIC_API_URL": url.removesuffix("/v1")}):
            result = own_key.check(about, config.ANTHROPIC_BASE_URL, KEY, "claude-opus-5-5", self.folder)
        self.assertEqual(result["text"], "This Anthropic account has no credit left.")
        (_, _, headers, body), = self.probes(server)
        self.assertEqual((headers["x-api-key"], json.loads(body)["tools"][0]["input_schema"]["type"]), (KEY, "object"))
        server.routes[("GET", "/models")] = error(401, "invalid x-api-key", kind="authentication_error")
        with mock.patch.dict(os.environ, {"LOOPCUT_ANTHROPIC_API_URL": url.removesuffix("/v1")}):
            refused = own_key.check(about, config.ANTHROPIC_BASE_URL, KEY, "claude-opus-5-5", self.folder)
        self.assertEqual(refused["text"], "This key was refused by Anthropic. Check it or make a new one.")

    def test_google_gemini_refusals_read_as_its_other_providers_do(self):
        gemini = ("GEMINI", "Google Gemini (API key)", "https://generativelanguage.googleapis.com/v1beta/openai")
        invalid = (400, [{"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.",
                                    "status": "INVALID_ARGUMENT", "details": [{"reason": "API_KEY_INVALID"}]}}])
        result, server, about = self.check({("GET", "/v1/models"): invalid}, model="gemini-2.5-flash", provider=gemini)
        self.assertEqual((result["text"], self.probes(server)),
                         ("This key was refused by Google Gemini. Check it or make a new one.", []))
        # It lists "models/…" and takes the bare name; its rate limit says "exceeded your current quota" too.
        limited = (429, {"error": {"code": 429, "message": "You exceeded your current quota, please check your plan "
                                                           "and billing details.", "status": "RESOURCE_EXHAUSTED"}})
        result, server, _ = self.check({("GET", "/v1/models"): models("models/gemini-2.5-flash"),
                                        ("POST", "/v1/chat/completions"): limited}, model="gemini-2.5-flash",
                                       provider=gemini)
        self.assertEqual((result["code"], len(self.probes(server))), (own_key.RATE_LIMITED, 1))
        refusal = llm.LLMError(f"HTTP 400: {json.dumps(invalid[1])}", 400)  # llm.py reads no error object from a list.
        self.assertEqual(own_key.chat_text(refusal, about, "gemini-2.5-flash"),
                         "This key was refused by Google Gemini. Check it or make a new one.")

    # --- The paid probe: once per key, base URL and model --------------------------------------------------------

    def test_the_same_combination_twice_probes_once_and_a_change_probes_again(self):
        url, server = self.serve({("GET", "/v1/models"): models("gpt-x", "gpt-y"),
                                  ("POST", "/v1/chat/completions"): ANSWER})
        about = own_key.who("OPENAI", "OpenAI", "https://api.openai.com/v1", KEY)
        for model, key in (("gpt-x", KEY), ("gpt-x", KEY), ("gpt-y", KEY), ("gpt-x", KEY + "2"), ("gpt-y", KEY)):
            self.assertEqual(own_key.check(about, url, key, model, self.folder)["code"], own_key.OK)
        sent = [(json.loads(s[3])["model"], s[2]["authorization"]) for s in self.probes(server)]
        self.assertEqual(sent, [("gpt-x", f"Bearer {KEY}"), ("gpt-y", f"Bearer {KEY}"), ("gpt-x", f"Bearer {KEY}2")])
        self.assertEqual(len([s for s in server.seen if s[0] == "GET"]), 5)  # The free list runs every time.

    def test_the_saved_results_hold_no_key_text(self):
        result, _, _ = self.check({("GET", "/v1/models"): models("gpt-x"), ("POST", "/v1/chat/completions"): error(
            400, f"Something about {KEY}")})
        self.assertEqual(result["code"], own_key.OTHER)
        text = (self.folder / own_key.CACHE_FILE).read_text(encoding="utf-8")
        self.assertNotIn(KEY, text)
        self.assertNotIn(KEY[:8], text)
        self.assertNotIn(KEY[-8:], text)
        saved = json.loads(text)
        self.assertEqual(len(saved["checks"]), 1)
        self.assertRegex(next(iter(saved["checks"])), r"^[0-9a-f]{64}$")

    def test_check_again_lists_again_and_probes_again_only_after_a_result_the_user_may_have_fixed(self):
        answers = iter([error(402, "Insufficient credits"), ANSWER])
        url, server = self.serve({("GET", "/v1/models"): models("gpt-x"),
                                  ("POST", "/v1/chat/completions"): lambda: next(answers)})
        about = own_key.who("OPENROUTER", "OpenRouter", "https://openrouter.ai/api/v1", KEY)
        self.assertEqual(own_key.check(about, url, KEY, "gpt-x", self.folder)["text"],
                         "This OpenRouter account has no credit left.")
        self.assertEqual(own_key.check(about, url, KEY, "gpt-x", self.folder)["code"], own_key.NO_CREDIT)  # Saved.
        self.assertEqual(own_key.check(about, url, KEY, "gpt-x", self.folder, again=True)["code"], own_key.OK)
        self.assertEqual(own_key.check(about, url, KEY, "gpt-x", self.folder, again=True)["code"], own_key.OK)
        self.assertEqual(len(self.probes(server)), 2)  # A pass stays saved: Check again reran only /models.
        self.assertEqual(len([s for s in server.seen if s[0] == "GET"]), 4)

    # --- Mid-chat: the same sentence --------------------------------------------------------------------------------

    def test_a_turn_that_fails_the_same_way_gets_the_same_sentence(self):
        about = own_key.who("OPENAI", "OpenAI", "https://api.openai.com/v1", KEY)
        checked, _, _ = self.check({("GET", "/v1/models"): error(401, "Incorrect API key", "invalid_api_key")})
        refusal = llm.LLMError(f"HTTP 401: Incorrect API key provided: {MASKED}", 401, "invalid_api_key",
                               {"message": f"Incorrect API key provided: {MASKED}", "code": "invalid_api_key"})
        self.assertEqual(own_key.chat_text(refusal, about, "gpt-x"), checked["text"])
        for ex, text in (
                (llm.LLMError("HTTP 402: …", 402, "", {"message": "Insufficient credits"}),
                 "This OpenAI account has no credit left."),
                (llm.LLMError('HTTP 400: {"error": "gpt-x does not support tools"}', 400),
                 "This model can't use tools, which Loopcut needs. Pick another one."),
                (llm.LLMError("HTTP 404: …", 404, "model_not_found", {"message": "The model `gpt-x` does not exist"}),
                 'No model called "gpt-x" on this key. Pick one from the list.'),
                (llm.LLMError("HTTP 500: oops", 500, "server_error", {"message": "oops"}), "")):
            with self.subTest(text=text):
                self.assertEqual(own_key.chat_text(ex, about, "gpt-x"), text)
        self.assertEqual(own_key.chat_text(refusal, {}, "gpt-x"), "")  # Loopcut's account and the ChatGPT plan.
        port = closed_port()
        ollama = own_key.who("OLLAMA", "Ollama (this computer)", f"http://127.0.0.1:{port}/v1")
        with self.assertRaises(llm.LLMError) as dropped:
            llm._open(lambda: __import__("urllib.request").request.Request(f"http://127.0.0.1:{port}/v1/x"),
                      f"http://127.0.0.1:{port}/v1", 2, lambda: False, retry_delays=())
        self.assertEqual(own_key.chat_text(dropped.exception, ollama, "llama3.2"),
                         f"Ollama isn't running at 127.0.0.1:{port}.")

    def test_a_turn_refused_mid_chat_shows_the_checks_sentence(self):
        def run(own):
            session = state.new_session()
            session["items"].append(state.item_user("Build a chest"))
            session["messages"].append({"role": "user", "content": "Build a chest"})
            session["busy"] = True
            turn = agent.Turn()
            turn.config = config.Config(KEY, "https://api.openai.com/v1", "gpt-x", "low", False, False, 0, 24000)
            turn.own_key = own
            spec = agent.LoopSpec(role="single", system=lambda s: "", schemas=lambda: [], save=mock.Mock(),
                                  asks=False, checkpoints=False)
            refusal = llm.LLMError("HTTP 401: Incorrect API key provided", 401, "invalid_api_key",
                                   {"message": "Incorrect API key provided"})
            with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"LOOPCUT_DATA_DIR": tmp}), \
                    mock.patch.object(agent, "_request", side_effect=refusal), \
                    mock.patch.object(agent, "_changes_item", return_value=None):
                agent.run_loop(session, turn, spec)
            return [i["text"] for i in session["items"] if i["kind"] == "error"]
        about = own_key.who("OPENAI", "OpenAI", "https://api.openai.com/v1", KEY)
        self.assertEqual(run(about), ["This key was refused by OpenAI. Check it or make a new one."])
        self.assertEqual(run({}), ["HTTP 401: Incorrect API key provided"])  # Not an own key: as before.


if __name__ == "__main__":
    unittest.main()
