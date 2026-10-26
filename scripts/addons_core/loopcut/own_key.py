"""An own key, checked once when it changes, and what's wrong with it in one plain sentence (0.2.20).

Real own-key turns (the user's own provider: OpenAI, OpenRouter, Ollama, Custom, Anthropic …) errored about 60% in a
day (2026-10-26), almost all setup mistakes the user could fix in a minute: a refused key, a model the key doesn't
have, an empty balance, a local server that isn't running, a model without tool calling. They only showed mid-chat,
as raw errors. own_key_ui.py runs check() on a worker thread after the key, the base URL or the model changed;
agent.py words a turn's failure with chat_text(), so Preferences, the panel and the chat say the same sentence.

The messages name only the user's own provider (its label in Preferences, or the host for Custom), never the key:
nothing here prints, logs, returns or saves it. The saved probe results (CACHE_FILE) are keyed by a salted hash.
No bpy.
"""

import hashlib
import json
import os
import re
import secrets
import socket
import ssl
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from . import config, llm

HOSTED = ("LOOPCUT", "CHATGPT")  # Loopcut's account and the ChatGPT plan: no own key to check.

OK = "ok"                      # The model answered the probe.
KEY_OK = "key_ok"              # The models were listed; no model picked yet, so no probe.
REFUSED = "refused"            # 401 / 403 / invalid_api_key / authentication_error.
NO_MODEL = "no_model"          # Not among the listed models, or a 404 model_not_found.
NO_CREDIT = "no_credit"        # 402, insufficient_quota (OpenAI sends it as a 429), "credit balance is too low".
NO_TOOLS = "no_tools"          # The model (or a local server's setup) refuses the one trivial tool.
NOT_RUNNING = "not_running"    # A refused connection on 127.0.0.1.
UNREACHABLE = "unreachable"    # DNS, TLS, a refused connection elsewhere.
RATE_LIMITED = "rate_limited"  # 429: not a setup mistake.
SLOW = "slow"                  # No answer in time (a local model still loading, a busy provider).
OTHER = "other"                # "<Provider> answered: <short reason>".

WORKS = frozenset({OK, KEY_OK})
PASSING = frozenset({RATE_LIMITED, SLOW})  # Phrased as passing; never shown above the chat's composer.
# What a turn's failure is reworded as (chat_text): the setup mistakes, and a rate limit, in the same words.
CHAT = frozenset({REFUSED, NO_MODEL, NO_CREDIT, NO_TOOLS, NOT_RUNNING, RATE_LIMITED})
# Check again probes a combination it already probed only when the saved result is one the user may have fixed
# outside Loopcut (topping up, waiting out a limit, a provider's or a network's hiccup). A pass, a refused key, a
# missing model and a model without tools belong to the key, base URL and model themselves: they stay saved until
# one of the three changes, which makes a new combination (and a new probe).
RECHECK = frozenset({NO_CREDIT, RATE_LIMITED, SLOW, UNREACHABLE, OTHER})

LIST_TIMEOUT = 10.0
PROBE_TIMEOUT = 30.0
LOCAL_PROBE_TIMEOUT = 120.0  # Ollama loads the model into memory on its first request.
DETAIL_CHARS = 140
CACHE_FILE = "own_key_checks.json"
CACHE_ENTRIES = 64
_CACHE_LOCK = threading.Lock()

# The probe's one tool: a model (or a local server) that can't call tools refuses the request, as the chat would.
PING_TOOL = {"name": "ping", "description": "Say the check is done.",
             "parameters": {"type": "object", "properties": {}}}
PING_MESSAGES = [{"role": "user", "content": "Reply OK."}]


# --- Words -------------------------------------------------------------------------------------------------------

def endpoint(base_url: str) -> str:
    """host[:port] of a base URL (anthropic:// too), without any user or password in it."""
    try:
        parsed = urlparse((base_url or "").strip())
        host, port = parsed.hostname or "", parsed.port
    except ValueError:
        return ""
    return f"{host}:{port}" if host and port else host


def who(provider: str, label: str, base_url: str, key: str = "") -> dict:
    """What the sentences need: the provider's name as the user knows it (its Preferences label without the
    parenthesis, "Ollama (this computer)" is Ollama; the host for Custom), the endpoint's host:port, whether it is on
    this computer, and whether the user gave a key. Never the key itself."""
    host = endpoint(base_url)
    custom = provider == "CUSTOM"
    name = host if custom else (label or "").split(" (")[0].strip()
    return {"name": name or "your provider", "host": host, "custom": custom, "keyed": bool(key),
            "local": (base_url or "").startswith("http://127.0.0.1")}


def message(code: str, about: dict, model: str = "", detail: str = "") -> str:
    """One short sentence for a check's (or a turn's) result."""
    name, model = about["name"], (model or "").strip()
    keyed = about["keyed"] and not about["local"]
    if code == OK:
        return f"Works with {model}."
    if code == KEY_OK:  # Not "accepted": OpenRouter lists its models to any key, so only the probe can say so.
        return "Pick a model to finish checking this key." if keyed else f"{name} is running. Pick a model."
    if code == REFUSED:
        return f"This key was refused by {name}. Check it or make a new one."
    if code == NO_MODEL:
        return (f'No model called "{model}" on this key. Pick one from the list.' if keyed
                else f'{name} has no model called "{model}". Pick one from the list.')
    if code == NO_CREDIT:
        return f"This {name} account has no credit left."
    if code == NO_TOOLS:
        return "This model can't use tools, which Loopcut needs. Pick another one."
    if code == NOT_RUNNING:
        return f"Nothing is running at {about['host']}." if about["custom"] else \
            f"{name} isn't running at {about['host']}."
    if code == RATE_LIMITED:
        return f"{name} is limiting this key right now. Try again in a minute." if keyed else \
            f"{name} is busy right now. Try again in a minute."
    if code == SLOW:
        return f"{name} didn't answer in time. Try again in a minute."
    if code == UNREACHABLE:
        return f"Couldn't reach {name}. Check the base URL and your connection." if about["custom"] else \
            f"Couldn't reach {name}. Check your connection."
    return f"{name} answered: {detail}" if detail else f"{name} answered with an error."


_MASKED = re.compile(r"\S*\*{3,}\S*")  # OpenAI's 401 echoes the key masked: "sk-abc*********xyz".


def scrub(text: str, key: str = "") -> str:
    """A provider's reason, trimmed to a line, without the key or any masked or leading piece of it."""
    text = " ".join(str(text or "").split())
    if key:
        text = text.replace(key, "…")
        if len(key) >= 12:
            text = re.sub(re.escape(key[:8]) + r"\S*", "…", text)
    text = _MASKED.sub("…", text)
    if len(text) > DETAIL_CHARS:
        text = text[:DETAIL_CHARS - 1].rstrip() + "…"
    return text


# --- What an error means -----------------------------------------------------------------------------------------

# Not "You exceeded your current quota": Google Gemini says so for a rate limit too (OpenAI's no-credit 429 says
# it with the code insufficient_quota, which counts).
_CREDIT_WORDS = ("credit balance is too low", "insufficient credits", "insufficient_quota", "insufficient balance",
                 "out of credits", "no credits", "insufficient funds")
# Google Gemini refuses a key with a 400 INVALID_ARGUMENT: "API key not valid", reason API_KEY_INVALID.
_KEY_WORDS = ("api key not valid", "api_key_invalid", "invalid api key", "incorrect api key")
_TOOL_WORDS = ("support", "enable-auto-tool-choice", "tool-call-parser", "not enabled", "not available")
# A 403 that isn't about the key: a provider's moderation (OpenRouter's "flagged") or a country it doesn't serve.
_NOT_THE_KEY = ("flagged", "moderation", "country", "region", "territory")
_MISSING_WORDS = ("not found", "does not exist", "doesn't exist", "no such", "unknown model", "invalid model",
                  "not a valid model", "try pulling it")


def classify(status: int | None, kind: str = "", text: str = "") -> str:
    """A provider's refusal as one of the codes above: its HTTP status, its error code or type, and its message."""
    kind, low = str(kind or "").lower(), str(text or "").lower()
    if status == 402 or kind in ("insufficient_quota", "billing_error", "insufficient_credits", "payment_required") \
            or any(w in low for w in _CREDIT_WORDS):
        return NO_CREDIT
    if status == 401 or kind in ("invalid_api_key", "authentication_error") or any(w in low for w in _KEY_WORDS) or \
            (status == 403 or kind == "permission_error") and not any(w in low or w in kind for w in _NOT_THE_KEY):
        return REFUSED
    # Not a schema detail of one of Loopcut's tools ("Invalid 'tools[3].function.parameters' … is not supported").
    if "tools[" not in low and (("tool" in low and any(w in low for w in _TOOL_WORDS))
                                or ("function calling" in low and "not" in low)):
        return NO_TOOLS
    if kind in ("model_not_found", "not_found_error") or \
            (status in (400, 404) and "model" in low and any(w in low for w in _MISSING_WORDS)):
        return NO_MODEL
    if status == 429 or kind in ("rate_limit_exceeded", "rate_limit_error"):
        return RATE_LIMITED
    return OTHER


def _error_of(body: bytes) -> tuple[str, str]:
    """(code or type, message) of an API error body; ("", the text) when it is not one."""
    try:
        payload = json.loads(body)
    except ValueError:
        return "", body.decode("utf-8", "replace")
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        payload = payload[0]  # Google Gemini: [{"error": {...}}]
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        kind = next((error[k] for k in ("code", "type", "status") if isinstance(error.get(k), str)), "")
        details = error.get("details") if isinstance(error.get("details"), list) else []
        reasons = " ".join(str(d.get("reason")) for d in details if isinstance(d, dict) and d.get("reason"))
        return str(kind), " ".join(p for p in (str(error.get("message") or ""), reasons) if p)
    if isinstance(error, str):  # Ollama: {"error": "model \"x\" not found, try pulling it first"}
        return "", error
    return "", json.dumps(payload)[:500]


def _refused_connection(ex) -> bool:
    seen = 0
    while ex is not None and seen < 6:
        if isinstance(ex, ConnectionRefusedError) or isinstance(getattr(ex, "reason", None), ConnectionRefusedError):
            return True
        ex, seen = ex.__cause__ or ex.__context__, seen + 1
    return False


def _timed_out(ex) -> bool:
    reason = getattr(ex, "reason", ex)
    return isinstance(ex, (TimeoutError, socket.timeout)) or isinstance(reason, (TimeoutError, socket.timeout))


def failure(ex, about: dict, key: str = "") -> tuple[str, str]:
    """(code, short reason) for what a request raised."""
    if isinstance(ex, urllib.error.HTTPError):
        with ex:
            try:
                body = ex.read(200_000)
            except OSError:
                body = b""
        kind, text = _error_of(body)
        return classify(ex.code, kind, text), scrub(text or f"HTTP {ex.code}", key)
    if _refused_connection(ex):
        return (NOT_RUNNING if about["local"] else UNREACHABLE), ""
    if _timed_out(ex):
        return SLOW, ""
    if isinstance(ex, (urllib.error.URLError, ssl.SSLError, OSError)):
        return UNREACHABLE, scrub(str(getattr(ex, "reason", ex)), key)
    return OTHER, scrub(str(ex), key)


def chat_text(ex, about: dict | None, model: str = "") -> str:
    """The sentence a turn's failure shows instead of the raw error, when it is one of the setup mistakes the
    check names (CHAT); "" otherwise, and on Loopcut's own account or the ChatGPT plan (about is empty there)."""
    if not about:
        return ""
    status = getattr(ex, "status", None)
    if isinstance(ex, llm.LLMError) and status is None and ex.code == llm.CONNECTION_LOST:
        code = NOT_RUNNING if about["local"] and _refused_connection(ex) else ""
    elif isinstance(ex, llm.LLMError) and (status is not None or ex.code):
        details = ex.details if isinstance(ex.details, dict) else {}
        code = classify(status, ex.code, str(details.get("message") or ex))
    else:
        code = ""
    return message(code, about, model) if code in CHAT else ""


def for_turn(cfg) -> dict:
    """Main thread, as a turn starts (agent._launch): who() for the own key it runs on, which chat_text words a
    failure with; {} on Loopcut's account, the ChatGPT plan or an endpoint from the environment. Never raises."""
    try:
        from . import settings
        prefs = settings.preferences()
        base_url = str(getattr(cfg, "base_url", "") or "").rstrip("/")
        if prefs is None or prefs.provider in HOSTED or not base_url or config.on_service(base_url) \
                or prefs.base_url.strip().rstrip("/") != base_url:
            return {}
        local = base_url.startswith("http://127.0.0.1")
        return who(prefs.provider, settings.provider_label(prefs.provider), base_url,
                   "" if local else str(getattr(cfg, "api_key", "") or ""))
    except Exception:
        return {}


# --- The two requests --------------------------------------------------------------------------------------------

def list_models(base_url: str, api_key: str, timeout: float = LIST_TIMEOUT) -> list[str]:
    """GET {base_url}/models on an OpenAI-compatible endpoint: the model ids, sorted. Raises urllib's errors.
    The Preferences refresh button (settings.LOOPCUT_OT_fetch_models) lists with this too. Like the chat's own
    requests it says Loopcut in the User-Agent (llm.USER_AGENT: Kimi Code checks the client)."""
    request = urllib.request.Request(f"{base_url.rstrip('/')}/models",
                                     headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json",
                                              "User-Agent": llm.USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read(4_000_000))
    rows = payload.get("data") if isinstance(payload, dict) else None
    return sorted({str(r["id"]) for r in rows or [] if isinstance(r, dict) and r.get("id")})


def listed(model: str, found: list[str]) -> bool:
    """Ollama takes "llama3.2" for the listed "llama3.2:latest"; OpenRouter takes a listed id with a routing
    suffix ("…:nitro", "…:floor"); Google Gemini's OpenAI-compatible endpoint lists "models/gemini-…" and takes
    the name without "models/"."""
    return model in found or f"{model}:latest" in found or f"models/{model}" in found \
        or (":" in model and model.rsplit(":", 1)[0] in found)


class _Refused(Exception):
    """A probe's refusal: its HTTP status (None for an error inside a 200) and its body, read once."""

    def __init__(self, status: int | None, body: bytes):
        super().__init__(f"HTTP {status}")
        self.status, self.body = status, body


def _post(url: str, body: dict, headers: dict, timeout: float) -> None:
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={**headers, "Content-Type": "application/json",
                                              "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read(1_000_000)
    except urllib.error.HTTPError as ex:
        with ex:
            try:
                answer = ex.read(200_000)
            except OSError:
                answer = b""
        raise _Refused(ex.code, answer) from None
    try:
        error = json.loads(payload).get("error")
    except (ValueError, AttributeError):
        return
    if error:  # A proxy's refusal inside a 200 (OpenRouter sends an upstream's failure this way).
        raise _Refused(error.get("code") if isinstance(error, dict) and isinstance(error.get("code"), int) else None,
                       payload)


def probe(base_url: str, key: str, model: str, timeout: float) -> None:
    """One chat request of at most one token, with one trivial tool, the way a turn sends its requests (chat
    completions, or Anthropic's Messages API for anthropic://). It costs the user about a token on their own
    account, which is why own_key_ui.py sends it once per key, base URL and model (see RECHECK). Raises _Refused
    for an answer that refused it, and urllib's errors for no answer."""
    if config.anthropic(base_url):
        from . import anthropic_messages
        tool = {"name": PING_TOOL["name"], "description": PING_TOOL["description"],
                "input_schema": PING_TOOL["parameters"]}
        _post(anthropic_messages.api_base(base_url) + "/messages",
              {"model": model, "max_tokens": 1, "messages": PING_MESSAGES, "tools": [tool]},
              {"x-api-key": key, "anthropic-version": anthropic_messages.API_VERSION}, timeout)
        return
    body = {"model": model, "messages": PING_MESSAGES, "max_tokens": 1, "stream": False,
            "tools": [{"type": "function", "function": PING_TOOL}]}
    url, headers = f"{base_url.rstrip('/')}/chat/completions", {"Authorization": f"Bearer {key}",
                                                                 "User-Agent": llm.USER_AGENT}  # As the chat's.
    try:
        _post(url, body, headers, timeout)
    except _Refused as ex:
        # A provider that wants a few tokens at least ("max_tokens must be at least 16") has passed the key, the
        # model and the tools by then: once more with 16, so a working setup never reads as broken.
        text = ex.body.decode("utf-8", "replace").lower()
        if ex.status != 400 or ("max_tokens" not in text and "max_completion_tokens" not in text) or \
                not any(w in text for w in ("at least", "minimum", "too small", "greater than")):
            raise
        _post(url, {**body, "max_tokens": 16}, headers, timeout)


# --- Saved probe results -----------------------------------------------------------------------------------------

def _cache_read(folder: Path) -> dict:
    try:
        data = json.loads((folder / CACHE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def combination(salt: str, key: str, base_url: str, model: str) -> str:
    """The saved results' name for a key, base URL and model: a salted SHA-256, never the key."""
    text = "\n".join((salt, key, base_url.rstrip("/"), model.strip()))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def saved(folder: Path | None, key: str, base_url: str, model: str) -> dict | None:
    """The saved probe result for this combination: {code, detail, at}, or None."""
    if folder is None:
        return None
    data = _cache_read(folder)
    salt, checks = data.get("salt"), data.get("checks")
    if not isinstance(salt, str) or not isinstance(checks, dict):
        return None
    found = checks.get(combination(salt, key, base_url, model))
    return dict(found) if isinstance(found, dict) and isinstance(found.get("code"), str) else None


def remember(folder: Path | None, key: str, base_url: str, model: str, code: str, detail: str) -> None:
    """Save a probe's result, so neither this session nor a relaunch probes the same combination again."""
    if folder is None:
        return
    try:
        with _CACHE_LOCK:
            folder.mkdir(parents=True, exist_ok=True)
            data = _cache_read(folder)
            salt = data.get("salt") if isinstance(data.get("salt"), str) and data.get("salt") else secrets.token_hex(16)
            checks = data.get("checks") if isinstance(data.get("checks"), dict) else {}
            name = combination(salt, key, base_url, model)
            checks.pop(name, None)
            checks[name] = {"code": code, "detail": scrub(detail, key), "at": int(time.time())}
            text = json.dumps({"salt": salt, "checks": dict(list(checks.items())[-CACHE_ENTRIES:])}, indent=1)
            handle, temporary = tempfile.mkstemp(prefix=CACHE_FILE, dir=folder)
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as out:
                    out.write(text)
                os.replace(temporary, folder / CACHE_FILE)
            except BaseException:
                os.unlink(temporary)
                raise
    except OSError as ex:
        print(f"Loopcut: could not save the key check: {type(ex).__name__}")


# --- The check ---------------------------------------------------------------------------------------------------

def check(about: dict, base_url: str, key: str, model: str, folder: Path | None, *, again: bool = False,
          cancelled=lambda: False) -> dict:
    """Worker only. {code, text, detail, probed}.

    Step 1, free: list the models with the key (anthropic_messages.list_models on Anthropic). A refused key, an
    empty balance, a server that isn't running or a model the list doesn't have ends the check here. Step 2, only
    when step 1 passed and a model is picked: probe(), unless this combination's result is saved (again: Check
    again, which probes once more only after a RECHECK result)."""
    base_url, model = base_url.rstrip("/"), model.strip()
    send = key or "local"  # A server on this computer takes any key (settings.values does the same).

    def result(code: str, detail: str = "", probed: bool = False) -> dict:
        return {"code": code, "text": message(code, about, model, detail), "detail": detail, "probed": probed}

    found = None
    try:
        if config.anthropic(base_url):
            from . import anthropic_messages
            found = anthropic_messages.list_models(base_url, send, timeout=LIST_TIMEOUT)
        else:
            found = list_models(base_url, send)
    except OSError as ex:  # urllib's HTTPError and URLError are OSErrors.
        code, detail = failure(ex, about, key)
        if code in (REFUSED, NO_CREDIT, NOT_RUNNING, UNREACHABLE, RATE_LIMITED, SLOW):
            return result(code, detail)
        # Otherwise no list here (a 404, a server without /models): the probe says whether the model works.
    except (ValueError, llm.LLMError):
        pass  # An answer that isn't a model list: the same.
    if cancelled():
        return {"code": "cancelled"}
    # Anthropic accepts aliases its list leaves out ("claude-opus-4-0"): only the probe's 404 says one is missing.
    if model and found and not listed(model, found) and not config.anthropic(base_url):
        return result(NO_MODEL)
    if not model:
        return result(KEY_OK)
    known = saved(folder, key, base_url, model)
    if known is not None and not (again and known["code"] in RECHECK):
        return {**result(known["code"], known.get("detail", "")), "saved": True}
    try:
        probe(base_url, send, model, LOCAL_PROBE_TIMEOUT if about["local"] else PROBE_TIMEOUT)
        code, detail = OK, ""
    except _Refused as ex:
        kind, text = _error_of(ex.body)
        code, detail = classify(ex.status, kind, text), scrub(text, key)
    except Exception as ex:  # Any failure is a result to show, never a crash on the worker.
        code, detail = failure(ex, about, key)
    if cancelled():
        return {"code": "cancelled"}
    remember(folder, key, base_url, model, code, detail)
    return result(code, detail, probed=True)
