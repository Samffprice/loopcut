"""Account-specific model acceptance and a small, tool-free readiness request. No bpy or user content."""

import http.client
import re
import time

from . import chatgpt_auth, llm

_MODEL = re.compile(r"[a-zA-Z0-9._-]{1,100}\Z")


def account_key() -> tuple:
    record = chatgpt_auth.active_record() or {}
    return tuple(record.get(k, "") for k in ("client_id", "issuer", "subject"))


def _saved(account: tuple) -> dict:
    if not account[0] or account_key() != account:
        return {}
    saved = (chatgpt_auth.active_record() or {}).get("model_checks")
    return saved if isinstance(saved, dict) else {}


def verified(account: tuple, model: str) -> bool:
    return _saved(account).get(model) is True


def effective_model(account: tuple, model: str) -> str:
    record = chatgpt_auth.active_record() or {}
    aliases = record.get("model_fallbacks", {}) if account_key() == account else {}
    actual = aliases.get(model) if isinstance(aliases, dict) else None
    return actual if isinstance(actual, str) and verified(account, actual) else model


def remember(account: tuple, model: str, ok: bool, requested: str = "") -> None:
    if not _MODEL.fullmatch(model) or not account[0]:
        return
    try:
        with chatgpt_auth._FileLock():
            data = chatgpt_auth._load_accounts()
            record = data["accounts"].get(account[0])
            if data["active"] != account[0] or not record or \
                    tuple(record.get(k, "") for k in ("client_id", "issuer", "subject")) != account:
                return
            checks = record.get("model_checks")
            checks = dict(checks) if isinstance(checks, dict) else {}
            aliases = record.get("model_fallbacks")
            aliases = dict(aliases) if isinstance(aliases, dict) else {}
            if checks.get(model) is ok and (not requested or
                    (requested == model and requested not in aliases) or aliases.get(requested) == model):
                return
            checks[model] = bool(ok)
            record["model_checks"] = dict(list(checks.items())[-32:])
            if ok and requested == model:
                aliases.pop(model, None)
            if ok and requested and requested != model and _MODEL.fullmatch(requested):
                aliases[requested] = model
            if not ok:
                aliases = {k: v for k, v in aliases.items() if v != model}
            record["model_fallbacks"] = dict(list(aliases.items())[-32:])
            chatgpt_auth._save_accounts(data)
    except OSError as ex:
        print(f"Loopcut: could not save ChatGPT model readiness: {type(ex).__name__}")


def unsupported(error) -> bool:
    message = str(error).lower()
    return (getattr(error, "code", "") in ("unsupported_model", "model_not_supported") or
            (getattr(error, "status", None) == 400 and
             ("model" in message and ("model is not supported" in message or
                                      "model' is not supported" in message or
                                      "model is unsupported" in message or
                                      ("not supported" in message and "chatgpt account" in message)))))


def fallback(account: tuple, refused: str) -> str | None:
    # Accepted models first, then the account catalog in OpenAI's preference order. At most three candidates;
    # the caller makes ONE alternative inference request, never walks the list on failure.
    checks = _saved(account)
    candidates = [m for m, ok in checks.items() if ok is True and m != refused]
    if not candidates:
        try:
            candidates = [row["slug"] for row in chatgpt_auth.list_models()]
        except chatgpt_auth.ChatGPTAuthError:
            return None
    if account_key() != account:
        return None
    ordered = [m for m in candidates if isinstance(m, str) and _MODEL.fullmatch(m)
               and m != refused and checks.get(m) is not False][:3]
    return ordered[0] if ordered else None


def notice(requested: str, actual: str) -> str:
    return f"Your ChatGPT plan doesn't include {requested}, so Loopcut used {actual}."


def model_used(account: tuple, requested: str, actual: str) -> None:
    try:
        import bpy
    except ImportError:  # CLI and tests have no bpy.
        return
    if getattr(bpy.app, "background", True):
        return
    from . import chatgpt_ui
    chatgpt_ui.model_used(account, requested, actual)


def message(code: str) -> str:
    if code in ("invalid_grant", "subscription_sharing_user_not_eligible", "no_plan_scope"):
        return "ChatGPT didn't let this account share its plan with Loopcut. Use another account or Loopcut Fast."
    if code in ("unsupported_country_region_territory", "country_not_supported"):
        return "OpenAI doesn't allow ChatGPT plan use in your country or region. Use Loopcut Fast."
    if code in ("chatgpt_not_signed_in", "subscription_sharing_invalid_user", "invalid_refresh_token"):
        return "Your ChatGPT sign-in expired. Sign in with ChatGPT again."
    if code == "unsupported_model":
        return "Your ChatGPT plan doesn't include this model. Choose another model or use Loopcut Fast."
    if code in ("offline", "network"):
        return "Couldn't reach ChatGPT. Readiness check skipped; try again when you're online."
    if code == "ready":
        return "ChatGPT is ready."
    if code == "subscription_sharing_usage_limit_exceeded":
        return "Your ChatGPT plan's limit for Loopcut is reached. Manage usage or use Loopcut Fast."
    return "Couldn't check ChatGPT just now. Try again or use Loopcut Fast."


def check(model: str, cancelled=lambda: False, fresh: bool = False) -> dict:
    """Worker only: no scene, tools, history or transcript. One short response plus at most one fallback.

    SIWC refuses max_output_tokens; request a one-word answer and close the stream after 15 seconds.
    Network failures skip the check without model retries or a backoff loop.
    """
    from . import chatgpt_responses as responses
    account = account_key()
    actual = model if fresh else effective_model(account, model)

    def probe(selected):
        if cancelled() or account_key() != account:
            raise llm.Cancelled()
        body = {"model": selected, "input": [{"role": "user", "content": "Reply OK."}],
                "store": False, "stream": True}
        transcript = responses._Transcript()
        transcript.folder = None
        response = responses._open(lambda: body, cancelled, transcript, probe=True)
        result = responses._read(response, lambda _: None, cancelled, time.monotonic() + 15,
                                 transcript, {"model": selected})
        if result.timed_out or result.finish_reason != "stop":
            raise llm.LLMError("Readiness check timed out", code="check_incomplete")
        return result

    try:
        try:
            probe(actual)
        except llm.LLMError as ex:
            if not unsupported(ex):
                raise
            remember(account, actual, False)
            alternative = fallback(account, actual) if actual == model else None
            if not alternative:
                raise llm.LLMError(message("unsupported_model"), code="unsupported_model") from ex
            actual = alternative
            probe(actual)
        if cancelled() or account_key() != account:
            return {"code": "cancelled", "model": model, "account": account}
        remember(account, actual, True, requested=model)
        return {"code": "ready", "model": actual, "account": account,
                "note": notice(model, actual) if actual != model else ""}
    except llm.Cancelled:
        code = "cancelled"
    except (chatgpt_auth.ChatGPTAuthError, llm.LLMError) as ex:
        code = "unsupported_model" if unsupported(ex) else getattr(ex, "code", "") or "check_failed"
    except (OSError, http.client.HTTPException):
        code = "offline"
    except responses._Interrupted:
        code = "offline"
    return {"code": code, "model": model, "account": account}
