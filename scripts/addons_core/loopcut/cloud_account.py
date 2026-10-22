"""Account policy and cost consent shared by preferences, cards and cloud execution.

Manifest values are account defaults; local preferences only apply when signed out. A
project can add a generation restriction, but cannot lift the server's account restriction.
Pending request hashes, retry keys and quote amounts survive restarts so a lost reply cannot
turn a retry into a second charge; spent keys do too, so a retry after a failure is a new job.
Calls forward only the approved quote ID to managed jobs.
"""

import math
import re
import hashlib
import hmac
import json
import threading
import uuid
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from . import cloud_http, config
from .nudges import dollars

PROJECT_PROPERTY = "loopcut_no_generation"
OVERRIDES = ("inherit", "on", "off")
DEFAULTS = {"no_generation": False, "private_mode": True}
_approval = threading.local()
MAX_PENDING = 32


class _Authorized(dict):
    """Internal receipt: model-supplied quote IDs are never proof of consent."""


def _fingerprint(descriptor, arguments, files, project):
    from . import blobs
    fields = {row['field'] for row in descriptor.get('file_inputs', [])}
    try:
        hashes = {k: blobs.sha256(Path(path)) for k, path in files.items()}
    except OSError as ex:
        raise cloud_http.CloudError('A cloud input changed or could not be read.', 'invalid_input', 422,
                                    {'field': 'files'}) from ex
    request = {'tool': descriptor['name'], 'version': descriptor['version'], 'project': project,
               'input': {k: v for k, v in arguments.items() if k not in fields | {'quote_id', 'idempotency_key'}},
               'files': hashes}
    return hashlib.sha256(json.dumps(request, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _pending(runtime):
    """Read only bounded retry metadata; never persist prompts, filenames or credentials."""
    try:
        with (runtime.cache_dir / 'pending.json').open('rb') as handle:
            raw = handle.read(65537)
        if len(raw) > 65536:
            raise ValueError('Pending submission cache is too large')
        rows = json.loads(raw)
        if not isinstance(rows, dict) or len(rows) > MAX_PENDING:
            raise ValueError('Invalid pending submissions')
        for key, row in rows.items():
            if (len(key) != 64 or any(c not in '0123456789abcdef' for c in key)
                    or not isinstance(row, dict) or set(row) != {'idempotency_key', 'quote'}
                    or not isinstance(row['idempotency_key'], str) or not 1 <= len(row['idempotency_key']) <= 200):
                raise ValueError('Invalid pending submission')
            if row['quote'] is not None:
                approval_details({'cost': {'quote_required': True}}, row['quote'])
        return rows
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, cloud_http.CloudError) as ex:
        raise cloud_http.CloudError('Could not recover pending submissions; do not submit again until resolved.',
                                    'submission_recovery_failed') from ex


def _save_pending(runtime, rows):
    from .cloud import _write_json
    try:
        _write_json(runtime.cache_dir / 'pending.json', rows)
    except OSError as ex:
        raise cloud_http.CloudError('Could not save the submission retry key; no new job was submitted.',
                                    'submission_recovery_failed') from ex


MAX_KEYS = 128


def _digest(key):
    return hashlib.sha256(key.encode()).hexdigest()


def _keys(runtime):
    """Hashes of sent retry keys and the request each was sent for; "" once it is spent.

    A key names one job for good (SPEC §3.4): the same request replays that job, any other is refused
    as "already used for a different request". So a key is reused only for its own request, and only
    while its job can still succeed. A lost or damaged file loses only this check, never a charge."""
    try:
        with (runtime.cache_dir / 'keys.json').open('rb') as handle:
            raw = handle.read(32769)
        rows = json.loads(raw) if len(raw) <= 32768 else None
        if (not isinstance(rows, dict) or len(rows) > MAX_KEYS
                or any(not re.fullmatch('[0-9a-f]{64}', k) or not isinstance(v, str)
                       or not re.fullmatch('(?:[0-9a-f]{64})?', v) for k, v in rows.items())):
            return {}
        return rows
    except (OSError, ValueError):
        return {}


def _note_key(runtime, key, fingerprint):
    """Under runtime.lock: `key` was sent for this request ("" once spent: never send it again)."""
    from .cloud import _write_json
    rows = _keys(runtime)
    rows.pop(_digest(key), None)
    rows[_digest(key)] = fingerprint
    while len(rows) > MAX_KEYS:
        del rows[next(iter(rows))]
    try:
        _write_json(runtime.cache_dir / 'keys.json', rows)
    except OSError:
        pass  # Only the check is lost: at worst a retry is refused or replays the old job.


def _key_spent(error):
    """The key can only replay a failed or canceled job, or the service refused it as another request's."""
    return error.extra.get('job', {}).get('status') in {'failed', 'canceled'} or (
        error.code == 'invalid_input' and error.extra.get('field') == 'idempotency_key')


def complete(runtime, arguments, spent=False):
    """Release retry metadata only after a definitive outcome, never after a lost reply. A spent key is never
    sent again, so retrying a failed or canceled job submits a new one."""
    with runtime.lock:
        if spent and arguments.get('idempotency_key'):
            _note_key(runtime, arguments['idempotency_key'], '')
        rows = _pending(runtime)
        if rows.get(arguments.fingerprint, {}).get('idempotency_key') == arguments.get('idempotency_key'):
            del rows[arguments.fingerprint]
            _save_pending(runtime, rows)


def effective_no_generation(account_default, override="inherit"):
    """Account enforcement wins even over an explicit project Off (§3.6)."""
    if type(account_default) is not bool or override not in OVERRIDES:
        raise ValueError("Invalid no-generation policy")
    return account_default or override == "on"


def policy_for(manifest, *, signed_in, local=None, override="inherit"):
    defaults = ((manifest.get("account") or {}).get("settings") or {}) if signed_in else (local or {})
    result = {key: defaults.get(key, value) for key, value in DEFAULTS.items()}
    result["no_generation"] = effective_no_generation(result["no_generation"], override)
    if not signed_in and override == "off":
        result["no_generation"] = False
    return result


def project_override(scene):
    value = scene.get(PROJECT_PROPERTY, "inherit") if scene is not None else "inherit"
    # A malformed custom property fails closed until the user chooses a valid value.
    return value if value in OVERRIDES else "on"


def set_project_override(scene, value):
    if value not in OVERRIDES:
        raise ValueError("Project no-generation must be inherit, on or off")
    scene[PROJECT_PROPERTY] = value


def current_policy(local):
    """Main thread snapshot; import Blender only at the scene boundary."""
    import bpy
    from . import cloud
    runtime = cloud._runtime
    scene = getattr(getattr(bpy, "context", None), "scene", None)
    return policy_for(runtime.manifest if runtime else {}, signed_in=bool(runtime and runtime.signed_in),
                      local=local, override=project_override(scene))


def credits_label(manifest, signed_in):
    """The panel's credits line, short enough for a narrow panel: "94 credits ($0.94) · renews 2026-11-01"."""
    credits = (manifest.get("account") or {}).get("credits") if signed_in else None
    if not isinstance(credits, dict) or not _amount(credits.get("balance")):
        return ""
    parts = [f"{credits['balance']:,g} credits ({dollars(credits['balance'])})"]
    if _amount(credits.get("monthly")) and credits["monthly"] and isinstance(credits.get("resets_at"), str):
        try:
            parts.append("renews " + datetime.fromisoformat(credits["resets_at"].replace("Z", "+00:00")).date().isoformat())
        except ValueError:
            pass  # Optional display metadata does not invalidate a usable balance.
    return " · ".join(parts)


def receipt(job):
    """What a finished managed job used, for its card (M4): "Used 6 credits ($0.06)"; "" when nothing was charged."""
    charged = ((job or {}).get("credits") or {}).get("charged")
    if not _amount(charged) or not charged:
        return ""
    return f"Used {charged:,g} credits ({dollars(charged)})"


def _amount(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


DATA_LABELS = {"A": "Private (A)", "B": "Longer retention (B)", "C": "May train (C)", "D": "May train (D)"}


def data_class(descriptor):
    """The data class to show for a tool: contract 0.6's top-level data_class, the class of the route Loopcut
    runs (render, lip sync and asset search have no own-key route, so no byok.data_class), else the own-key
    route's byok.data_class. Display only: own-key routing and Private mode's own-key checks read
    byok.data_class themselves (cloud.merge_tools, byok.catalog, byok.runner), so the managed class never
    decides an own-key call."""
    value = descriptor.get("data_class")
    return value if value in DATA_LABELS else (descriptor.get("byok") or {}).get("data_class")


def data_label(descriptor):
    return DATA_LABELS.get(data_class(descriptor), "Data class unknown")


def refusal(descriptor, policy):
    reason = descriptor.get("unavailable_reason")
    if policy.get("no_generation") and descriptor.get("kind") == "gen":
        return "No generation is on for this account or project."
    if reason == "private_mode":  # The service refused its own route, by the class it reports.
        return "Private mode blocks this vendor: " + data_label(descriptor) + "."
    own_key = (descriptor.get("byok") or {}).get("data_class")
    if policy.get("private_mode") and own_key in ("C", "D"):
        return "Private mode blocks this vendor: " + DATA_LABELS[own_key] + "."
    if not config.billing_open() and reason in ("sign_in", "credits", "plan"):
        return "Coming soon."  # Billing closed: no credits or plans to point at.
    return {"no_generation": "No generation is on for this account or project.",
            "sign_in": "Sign in to use credits.", "credits": "Not enough credits.",
            "plan": "Requires a different plan.", "region": "Unavailable in your region.",
            "disabled": "Tool unavailable."}.get(reason, "")


def quoted(descriptor):
    """Whether a managed call is priced by a server quote before its card shows: every paid tool, not only those
    marked quote_required. credits_estimate prices a tool's default arguments (5 s of motion, a final model); the
    quote prices this call's (3 s, a draft), and the job is charged what it says, so card and charge agree."""
    cost = descriptor.get("cost") or {}
    return bool(cost.get("quote_required")) or (_amount(cost.get("credits_estimate")) and cost["credits_estimate"] > 0)


def approval_details(descriptor, quote=None):
    """Pure card text and the server's decision; the threshold is strictly greater-than. required says whether
    the server's own threshold asks; spending.decide adds the user's "Ask above", the chat budget and the burst
    guard (C47). A quoted call is decided on its quote, like an estimate; without one (a quick check before the
    call, cloud.needs_approval) a tool not marked quote_required is decided on its estimate."""
    if descriptor.get("backend") == "byok":
        provider = (descriptor.get("byok") or {}).get("provider", "vendor")
        vendor = {"openai": "OpenAI", "gemini": "Google Gemini", "elevenlabs": "ElevenLabs"}.get(provider, provider)
        return {"required": True, "text": f"Uses your own {vendor} key", "credits": None, "vendor": vendor}
    cost = descriptor.get("cost") or {}
    threshold = (descriptor.get("approval") or {}).get("confirm_over_credits", 50)
    if not _amount(threshold):
        raise cloud_http.CloudError("Invalid tool credit estimate", "invalid_response")
    if cost.get("quote_required") or quote is not None:
        if quote is None:
            return {"required": True, "text": "Getting a credit quote…", "credits": None, "threshold": threshold}
        if (not isinstance(quote, dict) or not isinstance(quote.get("quote_id"), str) or not quote["quote_id"]
                or not _amount(quote.get("credits")) or not isinstance(quote.get("expires_at"), str)):
            raise cloud_http.CloudError("Invalid credit quote from Loopcut", "invalid_response")
        return {"required": quote["credits"] > threshold, "text": f"{quote['credits']:g} credits "
                f"({dollars(quote['credits'])}), quoted", "credits": quote["credits"], "threshold": threshold}
    estimate = cost.get("credits_estimate", 0)
    if not _amount(estimate):
        raise cloud_http.CloudError("Invalid tool credit estimate", "invalid_response")
    # The server's threshold decides; 0 means every priced call asks first (what it sends to add-ons older than
    # 0.2.4, which hide the balance). Free calls (0 credits) never ask.
    text = f"About {estimate:g} credits ({dollars(estimate)})" if estimate else "Free"
    return {"required": estimate > threshold, "text": text, "credits": estimate, "threshold": threshold}


def balance(manifest):
    """The account's credit balance from the manifest, or None."""
    credits = ((manifest or {}).get("account") or {}).get("credits")
    value = credits.get("balance") if isinstance(credits, dict) else None
    return value if _amount(value) else None


@contextmanager
def approval_context(callback):
    """Bind consent to the agent worker's current card, never a global last quote."""
    previous = getattr(_approval, "callback", None)
    _approval.callback = callback
    try:
        yield
    finally:
        _approval.callback = previous


def charge_reporter():
    """What a call was charged, for the card and chat that started it, even from a detached job (C47):
    report(charged, text) with charged None when it is not known."""
    callback = getattr(_approval, "callback", None)

    def report(charged, text=""):
        if getattr(callback, "settles", False) is True:  # agent._Consent; a plain consent function is never sent charges.
            callback({"charged": charged, "text": text})
    return report


def confirm(details):
    callback = getattr(_approval, "callback", None)
    if callback is not None:
        accepted = callback(details)
    else:
        accepted = not details["required"]
    if not accepted:
        raise cloud_http.CloudError("Cloud call was not approved; no job was submitted.", "approval_required")


def _check_cancel(cancel):
    if cancel.is_set():
        raise cloud_http.CloudError("Cloud call canceled before submission", "canceled")


def _quote(module, descriptor, arguments, files, project, cancel, runtime, client):
    _check_cancel(cancel)
    clean = {k: v for k, v in arguments.items() if k not in ("quote_id", "idempotency_key")}
    try:
        # Quotes pass the same server limits as jobs (C47); an approved refusal's token also admits the job.
        _confirmed(lambda token: module.run(descriptor, clean, files, project=project, cancel=cancel, client=client,
                                            quote=True, **({"confirm_token": token} if token else {})),
                   descriptor, arguments, cancel)
    except cloud_http.CloudError as ex:
        if ex.code != "quote_required":
            raise
        quote = {k: ex.extra.get(k) for k in ("quote_id", "credits", "expires_at")}
        approval_details(descriptor, quote)
        runtime.handle_error(ex)
        return quote
    raise cloud_http.CloudError("The service did not provide a credit quote", "invalid_response")


def authorize(module, descriptor, arguments, files, *, project, cancel, runtime, client):
    """Worker: resolve a runtime quote before consent, then forward its ID."""
    _check_cancel(cancel)
    arguments = _Authorized(arguments)
    key = arguments.get('idempotency_key')
    if key is not None and (not isinstance(key, str) or not 1 <= len(key) <= 200):
        raise cloud_http.CloudError('Invalid idempotency key', 'invalid_input', 422, {'field': 'idempotency_key'})
    arguments.runtime = runtime
    arguments.fingerprint = _fingerprint(descriptor, arguments, files, project)
    with runtime.lock:
        pending = _pending(runtime).get(arguments.fingerprint) if descriptor.get('backend') == 'managed' else None
    if pending:
        # The original quote and key must survive both a lost response and a process restart.
        arguments['idempotency_key'] = pending['idempotency_key']
        if pending['quote']:
            arguments['quote_id'] = pending['quote']['quote_id']
    quote = None
    if descriptor.get("backend") == "managed" and quoted(descriptor):
        with runtime.lock:
            quote_id = arguments.get('quote_id')
            if runtime.quote_requests.get(quote_id) == arguments.fingerprint:
                quote = runtime.quotes.get(quote_id)
            if pending:
                quote = pending['quote']
        if quote is None:
            quote = _quote(module, descriptor, arguments, files, project, cancel, runtime, client)
        arguments["quote_id"] = quote["quote_id"]
        with runtime.lock:
            runtime.quotes[quote['quote_id']] = quote
            runtime.quote_requests[quote['quote_id']] = arguments.fingerprint
    details = approval_details(descriptor, quote)
    if isinstance(getattr(runtime, "manifest", None), dict):
        with runtime.lock:
            details["balance"] = balance(runtime.manifest)  # The card's "Balance 1,840 → 1,760".
    _check_cancel(cancel)
    confirm(details)
    _check_cancel(cancel)
    arguments.quote = quote
    arguments.receipt = dict(arguments)
    return arguments


def recheck(runtime, descriptor, snapshot):
    """Main thread: settings may change while the cost card waits for consent."""
    from . import cloud
    cloud.sync_settings()
    if cloud._runtime is not runtime:
        raise cloud_http.CloudError("The cloud account changed; start a new call.", "account_changed")
    policy = {**runtime.policy, "no_generation": runtime.no_generation()}
    if policy["no_generation"] and descriptor.get("kind") == "gen":
        settings = (runtime.manifest.get("account") or {}).get("settings") or {}
        scope = "account" if settings.get("no_generation") else "project"
        raise cloud_http.CloudError("Generation disabled", "generation_disabled", 403, {"scope": scope})
    data_class = (descriptor.get("byok") or {}).get("data_class")
    private = runtime.private_mode()
    if private and data_class in ("C", "D"):
        raise cloud_http.CloudError("Private mode refuses this vendor", "private_mode", 403, {"data_class": data_class})
    current = next((d for d in runtime.descriptors() if d['name'] == descriptor['name']), None)
    if current != descriptor:
        raise cloud_http.CloudError('Tool availability or price changed; start a new call.', 'tool_changed')
    if descriptor.get('backend') == 'byok':
        from . import credentials
        key = credentials.vendor_key(descriptor['byok']['key_name'])
        if not hmac.compare_digest((key or '').encode(), (snapshot.vendor_key or '').encode()):
            raise cloud_http.CloudError('The vendor key changed; approve a new call.', 'account_changed')
    snapshot.policy = {**policy, "private_mode": private}


SERVER_LIMITS = ("daily_limit_reached", "rate_limited")  # Refusals the user may approve past (spending.py).
CONFIRM_HEADER = "x-loopcut-confirm"


def _confirm_token(error):
    token = error.extra.get("confirm_token")
    return token if isinstance(token, str) and 1 <= len(token) <= 512 and token.isprintable() else None


_URL = re.compile(r"https?://[^\s<>\"']+")


def _readable(message):
    """A refusal's message for a card: one line, without the link the card's button carries."""
    text = re.sub(r"\s*(?:Change it|Manage it|See|Visit)?\s*(?:at|on)?\s*" + _URL.pattern + r"\.?", "", message or "")
    return " ".join(text.split())[:240]


def limit_details(error, descriptor, arguments):
    """The approval card for a server refusal: what was hit, this call's cost, and the account link."""
    quote = getattr(arguments, "quote", None)
    details = approval_details(descriptor, quote)
    message = error.extra.get("message") if isinstance(error.extra.get("message"), str) else str(error)
    details.update(limit=error.code, required=True, message=_readable(message),
                   url=_safe_link(error.extra, message) if error.code == "daily_limit_reached" else "")
    runtime = getattr(arguments, "runtime", None)
    if isinstance(getattr(runtime, "manifest", None), dict):
        with runtime.lock:
            details["balance"] = balance(runtime.manifest)
    return details


def _run_confirmed(module, descriptor, arguments, files, *, project, on_progress, cancel, client):
    return _confirmed(lambda token: module.run(descriptor, arguments, files, project=project, on_progress=on_progress,
                                               cancel=cancel, client=client,
                                               **({"confirm_token": token} if token else {})),
                      descriptor, arguments, cancel)


def _confirmed(run, descriptor, arguments, cancel):
    """The owner (2026-10-16): a limit pauses on an approval card, never ends the turn. The server refuses over the
    user's daily limit (402) or its burst guard (429) with a single-use confirm_token, bound to this tool and input
    and spent only when a job is admitted. The card asks once per limit; Approve sends the same request (same
    idempotency key) again with it in the x-loopcut-confirm header, and the call keeps it for its job after a quote.
    "Skip this" declines only this call: the refusal created no job, so its retry key is released."""
    token = getattr(arguments, "confirm_token", None)
    asked = set()
    while True:
        try:
            return run(token)
        except cloud_http.CloudError as ex:
            fresh = _confirm_token(ex)
            callback = getattr(_approval, "callback", None)
            if (ex.code not in SERVER_LIMITS or fresh is None or callback is None or "job" in ex.extra
                    or ex.code in asked):
                raise
            asked.add(ex.code)
            try:
                accepted = callback(limit_details(ex, descriptor, arguments))
            except cloud_http.CloudError as declined:
                declined.status = declined.status or ex.status  # Definitive: no job was created.
                raise
            if not accepted:
                raise cloud_http.CloudError("Cloud call was not approved; no job was submitted.", "approval_required",
                                            ex.status) from ex
            _check_cancel(cancel)
            token = fresh
            if isinstance(arguments, _Authorized):
                arguments.confirm_token = fresh


def submit(module, descriptor, arguments, files, *, project, on_progress, cancel, runtime, client):
    """An expired quote is refreshed once, within the price the user approved.

    This handoff finishes before cloud_jobs detaches a long job. A higher replacement
    price needs a new card on a subsequent call, never an unapproved retry.
    """
    _check_cancel(cancel)
    if (not isinstance(arguments, _Authorized) or arguments.runtime is not runtime
            or dict(arguments) != arguments.receipt
            or arguments.fingerprint != _fingerprint(descriptor, arguments, files, project)):
        raise cloud_http.CloudError('Cloud call needs approval for these inputs.', 'approval_required')
    with runtime.lock:
        rows = _pending(runtime)
        pending = rows.get(arguments.fingerprint)
        if pending is None:
            if len(rows) >= MAX_PENDING:
                raise cloud_http.CloudError('Resolve pending cloud submissions before starting more jobs.', 'pending_limit')
            key = arguments.get('idempotency_key')
            # A key sent for other inputs, or spent on a failed or canceled job, would be refused or only
            # replay that job (SPEC §3.4): this attempt gets a new one.
            if key is None or _keys(runtime).get(_digest(key), arguments.fingerprint) != arguments.fingerprint:
                key = uuid.uuid4().hex
            pending = {'idempotency_key': key, 'quote': arguments.quote}
            rows[arguments.fingerprint] = pending
            _save_pending(runtime, rows)
        elif pending['quote'] != arguments.quote:
            raise cloud_http.CloudError('Another submission is pending; retry this call.', 'submission_pending')
        arguments['idempotency_key'] = pending['idempotency_key']
        _note_key(runtime, pending['idempotency_key'], arguments.fingerprint)
        arguments.receipt = dict(arguments)
    quote = arguments.quote
    try:
        result = _run_confirmed(module, descriptor, arguments, files, project=project, on_progress=on_progress,
                                cancel=cancel, client=client)
    except cloud_http.CloudError as ex:
        # 4xx rejections and terminal jobs are definitive. Transport/5xx errors are ambiguous.
        definitive = ('job' not in ex.extra and ex.status is not None and 400 <= ex.status < 500) or (
            ex.extra.get('job', {}).get('status') in {'failed', 'canceled'})
        if definitive:
            complete(runtime, arguments, _key_spent(ex))
        if ex.code != "quote_expired" or ex.status != 409 or quote is None:
            raise
    else:
        if result.get('status') in {'succeeded', 'failed', 'canceled'}:
            complete(runtime, arguments, result['status'] != 'succeeded')
        return result
    replacement = _quote(module, descriptor, arguments, files, project, cancel, runtime, client)
    if replacement["credits"] > quote["credits"]:
        raise cloud_http.CloudError(f"The new quote is {replacement['credits']:g} credits; approve a new call to continue.",
                                    "quote_changed", extra=replacement)
    retry = _Authorized({k: v for k, v in arguments.items() if k != "idempotency_key"})
    retry["quote_id"] = replacement["quote_id"]
    retry.runtime, retry.fingerprint, retry.quote = runtime, arguments.fingerprint, replacement
    retry.confirm_token = getattr(arguments, "confirm_token", None)  # Not spent: no job was admitted.
    retry.receipt = dict(retry)
    _check_cancel(cancel)
    # Persist the replacement too, but never refresh a second expired quote in this call.
    with runtime.lock:
        rows = _pending(runtime)
        retry['idempotency_key'] = uuid.uuid4().hex
        arguments.update(retry)
        rows[retry.fingerprint] = {'idempotency_key': retry['idempotency_key'], 'quote': replacement}
        _save_pending(runtime, rows)
        _note_key(runtime, retry['idempotency_key'], retry.fingerprint)
    try:
        result = _run_confirmed(module, descriptor, retry, files, project=project, on_progress=on_progress,
                                cancel=cancel, client=client)
    except cloud_http.CloudError as ex:
        if ('job' not in ex.extra and ex.status is not None and 400 <= ex.status < 500) or (
                ex.extra.get('job', {}).get('status') in {'failed', 'canceled'}):
            complete(runtime, retry, _key_spent(ex))
        raise
    if result.get('status') in {'succeeded', 'failed', 'canceled'}:
        complete(runtime, retry, result['status'] != 'succeeded')
    return result


def _safe_link(extra, message=""):
    """A server-sent account link (the daily limit's), validated: a link field, else one in the message, else the
    account page."""
    found = _URL.search(message) if isinstance(message, str) else None
    for url in [extra.get(key) for key in ("url", "link", "account_url", "manage_url", "upgrade")] + (
            [found.group(0).rstrip(".,)")] if found else []):
        try:
            cloud_http.validate_url(url)
            return url
        except (ValueError, TypeError):
            continue
    return config.credits_url("daily_limit")


def error_details(error):
    """Short model result, readable card, and a validated link kept outside model text."""
    extra, code = error.extra, error.code
    if code == "insufficient_credits":
        balance = extra.get("balance") if _amount(extra.get("balance")) else "?"
        needed = extra.get("needed") if _amount(extra.get("needed")) else "?"
        url = extra.get("upgrade", "") if config.billing_open() else ""
        try:
            cloud_http.validate_url(url)
        except (ValueError, TypeError):
            url = ""
        hint = " Add credits or use your own vendor key." if config.billing_open() else ""
        price = f"{needed:g} ({dollars(needed)})" if needed != "?" else "more"
        return {"text": f"insufficient_credits: balance {balance}, needed {needed}." + hint,
                "message": f"Not enough credits: this needs {price}, you have {balance}.", "upgrade": url}
    if code == "daily_limit_reached":
        # Server C47 (HTTP 402): the user's optional daily limit. With a confirm_token the card asked first
        # (_confirmed); this is the refusal without one, or after its one retry.
        message = _readable(str(error))
        if not message:
            limit, spent = extra.get("daily_limit"), extra.get("spent_today")
            used = f" ({spent:,g} of {limit:,g} credits today)" if _amount(spent) and _amount(limit) else ""
            message = "Today's credit limit is reached" + used + "."
        return {"text": "daily_limit_reached: the user's daily credit limit is reached. Do not retry paid tools "
                        "today unless the user raises it; go on without them.",
                "message": message, "upgrade": "", "link": _safe_link(extra, str(error)),
                "link_label": "Change daily limit ↗"}
    if code == "rate_limited" and (error.status == 429 or "confirm_token" in extra) and "job" not in extra:
        # The service's own burst guard on paid jobs (about 30 in 10 minutes), refused at submission. A vendor's
        # 429 on an own-key call arrives as a failed job instead, and keeps the plain retry text.
        wait = extra.get("retry_after")
        after = f"retry after {wait:g} seconds" if _amount(wait) and wait else "wait a few minutes"
        return {"text": f"rate_limited: too many paid jobs in a short time; {after}. Do not retry in a loop: go on "
                        "without paid tools, or tell the user.",
                "message": "Too many paid jobs in a short time. Loopcut paused new ones; "
                           + after.replace("retry after", "try again in") + ".",
                "upgrade": "", "stop": True}
    if code == "generation_disabled":
        scope = extra.get("scope")
        scope = scope if scope in ("account", "project") else "account or project"
        text = f"generation_disabled: generation is disabled for this {scope}. Use an assist tool."
    elif code == "private_mode":
        value = extra.get("data_class")
        label = value if value in ("C", "D") else "C or D"
        text = f"private_mode: Private mode refuses this vendor (data class {label}). Choose a private tool."
    else:
        return None
    return {"text": text, "message": text, "upgrade": ""}


def report_error(error, callback=None):
    details = error_details(error)
    callback = callback or getattr(_approval, "callback", None)
    if details and callback:
        callback({"error": details})
    return details["text"] if details else None


def error_reporter():
    """Detached jobs can still annotate their original card, without reopening consent."""
    callback = getattr(_approval, "callback", None)
    return lambda error: report_error(error, callback)


def patch_settings(runtime, changes):
    """Worker-only HTTP; serialize with manifest refresh so stale reads cannot win."""
    if not changes or set(changes) - DEFAULTS.keys() or any(type(v) is not bool for v in changes.values()):
        raise ValueError("Settings require boolean no_generation/private_mode values")
    if not runtime.signed_in:
        raise cloud_http.CloudError("Sign in to change account settings", "sign_in_required")
    with runtime.refresh_lock:
        result = runtime.client.request("PATCH", "/v1/settings", changes)
        if not isinstance(result, dict) or any(type(result.get(k)) is not bool for k in DEFAULTS):
            raise cloud_http.CloudError("Invalid account settings from Loopcut", "invalid_response")
        with runtime.lock:
            account = runtime.manifest.get("account") or {}
            account["settings"] = {k: result[k] for k in DEFAULTS}
            runtime.manifest["account"] = account
            runtime.etag, runtime.refreshed_at = "", 0
            runtime._save()
    runtime.refresh()
    return result


def setting_value(runtime, name, local):
    if runtime is None or not runtime.signed_in:
        return local
    with runtime.lock:
        return getattr(runtime, "account_pending", {}).get(name,
            ((runtime.manifest.get("account") or {}).get("settings") or {}).get(name, DEFAULTS[name]))


def change_setting(runtime, name, value):
    """One writer per account; rapid preference edits are coalesced in order."""
    if name not in DEFAULTS or type(value) is not bool:
        raise ValueError("Invalid account setting")
    with runtime.lock:
        if not hasattr(runtime, "account_pending"):
            runtime.account_pending = {}
        runtime.account_pending[name] = value
        if getattr(runtime, "account_saving", False):
            return
        runtime.account_saving = True

    def save():
        from . import mainthread
        while True:
            with runtime.lock:
                changes = dict(runtime.account_pending)
                if not changes:
                    runtime.account_saving = False
                    mainthread.request_redraw()
                    return
            try:
                patch_settings(runtime, changes)
                runtime.account_message = ""
            except cloud_http.CloudError as ex:
                runtime.account_message = "Settings not saved: " + str(ex)[:200]
            finally:
                with runtime.lock:
                    for key, value in changes.items():
                        if runtime.account_pending.get(key) == value:
                            runtime.account_pending.pop(key)
                mainthread.request_redraw()

    threading.Thread(target=save, name="loopcut-account-settings", daemon=True).start()


def today_label(manifest, signed_in):
    """Server C47's account.spend: "Today: 120 credits", and "of 1,000" only when the user set a daily limit.
    "" when the fields are absent (older servers) or malformed."""
    spend = ((manifest or {}).get("account") or {}).get("spend") if signed_in else None
    if not isinstance(spend, dict) or not _amount(spend.get("spent_today")):
        return ""
    limit = spend.get("daily_limit")
    if _amount(limit) and limit:
        return f"Today: {spend['spent_today']:,g} of {limit:,g} credits"
    return f"Today: {spend['spent_today']:,g} credits"


def panel_data(runtime):
    if runtime is None:
        return {"credits": "", "today": "", "message": ""}
    with runtime.lock:
        billing = config.billing_open()
        data = {"credits": credits_label(runtime.manifest, runtime.signed_in) if billing else "",
                "today": today_label(runtime.manifest, runtime.signed_in) if billing else "",
                "message": "Saving account settings…" if getattr(runtime, "account_saving", False)
                else getattr(runtime, "account_message", "")}
        credit_balance = balance(runtime.manifest)
        if credit_balance is not None:
            data["credit_balance"] = credit_balance if runtime.signed_in else None
        return data


def tool_rows(runtime):
    """All known tools remain explainable in preferences, including refused routes."""
    from .byok import catalog
    policy = {**DEFAULTS, **(runtime.policy if runtime else {})}
    if runtime:
        policy.update(no_generation=runtime.no_generation(), private_mode=runtime.private_mode())
    rows = {d["name"]: d for d in catalog.descriptors(no_generation=policy["no_generation"],
                private_mode=policy["private_mode"], keys_set=runtime.keys_set if runtime else {})}
    if runtime:
        with runtime.lock:
            from .cloud import _descriptor
            rows.update({d["name"]: deepcopy(d) for d in runtime.manifest["tools"] if _descriptor(d)})
        rows.update({d["name"]: d for d in runtime.descriptors()})
    return [{"title": d["title"], "label": data_label(d), "reason": refusal(d, policy)} for d in rows.values()]
