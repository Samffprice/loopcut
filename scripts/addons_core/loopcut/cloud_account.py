"""Account policy and cost consent shared by preferences, cards and cloud execution.

Manifest values are account defaults; local preferences only apply when signed out. A
project can add a generation restriction, but cannot lift the server's account restriction.
Pending request hashes, retry keys and quote amounts survive restarts so a lost reply cannot
turn a retry into a second charge. Calls forward only the approved quote ID to managed jobs.
"""

import math
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


def complete(runtime, arguments):
    """Release retry metadata only after a definitive outcome, never after a lost reply."""
    with runtime.lock:
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
    credits = (manifest.get("account") or {}).get("credits") if signed_in else None
    if not isinstance(credits, dict) or not _amount(credits.get("balance")):
        return ""
    parts = [f"{credits['balance']:,g} credits"]
    if _amount(credits.get("monthly")):
        parts.append(f"{credits['monthly']:,g}/month")
    if isinstance(credits.get("resets_at"), str):
        try:
            parts.append("resets " + datetime.fromisoformat(credits["resets_at"].replace("Z", "+00:00")).date().isoformat())
        except ValueError:
            pass  # Optional display metadata does not invalidate a usable balance.
    return " · ".join(parts)


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
    if config.BETA and reason in ("sign_in", "credits", "plan"):
        return "Coming soon."  # The beta sells nothing: no credits or plans to point at.
    return {"no_generation": "No generation is on for this account or project.",
            "sign_in": "Sign in to use credits.", "credits": "Not enough credits.",
            "plan": "Requires a different plan.", "region": "Unavailable in your region.",
            "disabled": "Tool unavailable."}.get(reason, "")


def approval_details(descriptor, quote=None):
    """Pure card text and decision; the threshold is strictly greater-than."""
    if descriptor.get("backend") == "byok":
        provider = (descriptor.get("byok") or {}).get("provider", "vendor")
        vendor = {"openai": "OpenAI", "gemini": "Google Gemini", "elevenlabs": "ElevenLabs"}.get(provider, provider)
        return {"required": True, "text": f"Uses your own {vendor} key", "credits": None}
    cost = descriptor.get("cost") or {}
    if cost.get("quote_required"):
        if quote is None:
            return {"required": True, "text": "Getting a credit quote…", "credits": None}
        if (not isinstance(quote, dict) or not isinstance(quote.get("quote_id"), str) or not quote["quote_id"]
                or not _amount(quote.get("credits")) or not isinstance(quote.get("expires_at"), str)):
            raise cloud_http.CloudError("Invalid credit quote from Loopcut", "invalid_response")
        return {"required": True, "text": f"{quote['credits']:g} credits (quoted)", "credits": quote["credits"]}
    estimate = cost.get("credits_estimate", 0)
    threshold = (descriptor.get("approval") or {}).get("confirm_over_credits", 50)
    if not _amount(estimate) or not _amount(threshold):
        raise cloud_http.CloudError("Invalid tool credit estimate", "invalid_response")
    return {"required": estimate > threshold, "text": f"{estimate:g} credits estimated", "credits": estimate}


@contextmanager
def approval_context(callback):
    """Bind consent to the agent worker's current card, never a global last quote."""
    previous = getattr(_approval, "callback", None)
    _approval.callback = callback
    try:
        yield
    finally:
        _approval.callback = previous


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
        module.run(descriptor, clean, files, project=project, cancel=cancel, client=client)
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
    if descriptor.get("backend") == "managed" and (descriptor.get("cost") or {}).get("quote_required"):
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
            pending = {'idempotency_key': arguments.get('idempotency_key') or uuid.uuid4().hex,
                       'quote': arguments.quote}
            rows[arguments.fingerprint] = pending
            _save_pending(runtime, rows)
        elif pending['quote'] != arguments.quote:
            raise cloud_http.CloudError('Another submission is pending; retry this call.', 'submission_pending')
        arguments['idempotency_key'] = pending['idempotency_key']
        arguments.receipt = dict(arguments)
    quote = arguments.quote
    try:
        result = module.run(descriptor, arguments, files, project=project, on_progress=on_progress, cancel=cancel, client=client)
    except cloud_http.CloudError as ex:
        # 4xx rejections and terminal jobs are definitive. Transport/5xx errors are ambiguous.
        definitive = ('job' not in ex.extra and ex.status is not None and 400 <= ex.status < 500) or (
            ex.extra.get('job', {}).get('status') in {'failed', 'canceled'})
        if definitive:
            complete(runtime, arguments)
        if ex.code != "quote_expired" or ex.status != 409 or quote is None:
            raise
    else:
        if result.get('status') in {'succeeded', 'failed', 'canceled'}:
            complete(runtime, arguments)
        return result
    replacement = _quote(module, descriptor, arguments, files, project, cancel, runtime, client)
    if replacement["credits"] > quote["credits"]:
        raise cloud_http.CloudError(f"The new quote is {replacement['credits']:g} credits; approve a new call to continue.",
                                    "quote_changed", extra=replacement)
    retry = _Authorized({k: v for k, v in arguments.items() if k != "idempotency_key"})
    retry["quote_id"] = replacement["quote_id"]
    retry.runtime, retry.fingerprint, retry.quote = runtime, arguments.fingerprint, replacement
    retry.receipt = dict(retry)
    _check_cancel(cancel)
    # Persist the replacement too, but never refresh a second expired quote in this call.
    with runtime.lock:
        rows = _pending(runtime)
        retry['idempotency_key'] = uuid.uuid4().hex
        arguments.update(retry)
        rows[retry.fingerprint] = {'idempotency_key': retry['idempotency_key'], 'quote': replacement}
        _save_pending(runtime, rows)
    try:
        result = module.run(descriptor, retry, files, project=project, on_progress=on_progress, cancel=cancel, client=client)
    except cloud_http.CloudError as ex:
        if ('job' not in ex.extra and ex.status is not None and 400 <= ex.status < 500) or (
                ex.extra.get('job', {}).get('status') in {'failed', 'canceled'}):
            complete(runtime, retry)
        raise
    if result.get('status') in {'succeeded', 'failed', 'canceled'}:
        complete(runtime, retry)
    return result


def error_details(error):
    """Short model result, readable card, and a validated link kept outside model text."""
    extra, code = error.extra, error.code
    if code == "insufficient_credits":
        balance = extra.get("balance") if _amount(extra.get("balance")) else "?"
        needed = extra.get("needed") if _amount(extra.get("needed")) else "?"
        url = extra.get("upgrade", "") if not config.BETA else ""
        try:
            cloud_http.validate_url(url)
        except (ValueError, TypeError):
            url = ""
        hint = "" if config.BETA else " Add credits or use BYOK."
        return {"text": f"insufficient_credits: balance {balance}, needed {needed}." + hint,
                "message": f"Not enough credits: balance {balance}, needed {needed}.", "upgrade": url}
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


def panel_data(runtime):
    if runtime is None:
        return {"credits": "", "message": ""}
    with runtime.lock:
        return {"credits": "" if config.BETA else credits_label(runtime.manifest, runtime.signed_in),
                "message": "Saving account settings…" if getattr(runtime, "account_saving", False)
                else getattr(runtime, "account_message", "")}


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
