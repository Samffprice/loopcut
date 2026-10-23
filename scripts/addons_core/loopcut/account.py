"""Signing in to Loopcut from the app, the way Cursor does it: a browser tab opens on the Loopcut
site, the person approves a short code there, and the app receives a key for the Loopcut gateway.

The gateway speaks the same chat-completions protocol as every other provider, so once the key
is stored under the Loopcut base URL (credentials.py) nothing else in the add-on changes. Model
names are "fast" and "pro"; the server decides what runs behind each.

Network calls happen on a worker thread. Anything that touches bpy goes through mainthread.
"""

import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.client import HTTPException
from urllib.parse import urlsplit

import bpy
import bpy.utils.previews

from . import allowance, config, credentials, mainthread, state

SERVICE_URL = config.SERVICE_URL
BASE_URL = f"{SERVICE_URL}/v1"
WATCH_EVERY = 4.0          # Seconds between looks at /v1/me while the user upgrades in the browser.
WATCH_FOR = 15 * 60.0      # How long to keep looking.
USAGE_REFRESH_DELAYS = (30.0, 120.0, 600.0)  # Then wait for an explicit refresh or fresh turn usage.
_watch_generation = 0
_credential_generation = 0  # Account refreshes survive model/provider changes, but never a different sign-in.
MODELS = (("fast", "Fast", "Quick answers"), ("pro", "Pro", "Thinks longer on hard scenes"))  # Until /v1/models answers.
# Tier ids earlier clients saved, as the gateway maps them (LEGACY_MODEL_ALIASES in its chat route): 0.2.4 offered
# "mimo", which is Pro now. A preference still holding one is labelled as the tier it reaches.
LEGACY_MODELS = {"mimo": "pro"}

_state = {
    "status": "idle",   # idle | starting | waiting | error
    "user_code": "",
    "url": "",
    "error": "",
    "account": None,    # /v1/me payload once known
    "models": None,     # /v1/models rows once known: what the picker lists, with plan and usage notes
    "default_model": "fast",
    "fetched_at": 0.0,
    "usage_refresh_at": None,
    "usage_refresh_attempts": 0,
    "usage_refresh_pending": False,
}
_cancel = threading.Event()
_TIMEOUT = 15.0
on_signed_in: list = []  # Called on the main thread once a key is stored (onboarding advances).
_previews = None
_MARK_SIZE = 64


class AccountError(RuntimeError):
    def __init__(self, message: str, code: str = "sign_in_failed"):
        super().__init__(message)
        self.code = code  # A machine code for the setup_step usage event (telemetry.py), never shown.


def signed_in() -> bool:
    return bool(credentials.api_key(BASE_URL))


def status() -> dict:
    return dict(_state)


def _redraw() -> None:
    mainthread.request_redraw()


def _client_header() -> dict:
    """The contract's x-loopcut-client (add-on, Blender, OS and processor), so the server can record what a
    sign-in linked (device poll) and what a token is used from. Never a reason for a request to fail."""
    try:
        from . import cloud_http
        return {"x-loopcut-client": cloud_http.client_value()}
    except Exception:
        return {}


def _post(path: str, payload: dict, token: str = "") -> dict:
    headers = {"Content-Type": "application/json", "Accept": "application/json", **_client_header()}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"{SERVICE_URL}{path}", data=json.dumps(payload).encode("utf-8"),
                                     headers=headers, method="POST")
    return _send(request)


def _get(path: str, token: str) -> dict:
    request = urllib.request.Request(f"{SERVICE_URL}{path}",
                                     headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                                              **_client_header()})
    return _send(request)


def _send(request) -> dict:
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            payload = json.loads(response.read(1_000_000))
    except urllib.error.HTTPError as ex:
        with ex:
            body = ex.read(10_000)
        try:
            message = json.loads(body)["error"]["message"]
        except (ValueError, KeyError, TypeError):
            message = f"HTTP {ex.code}"
        raise AccountError(message, f"http_{ex.code}") from ex
    except (OSError, HTTPException, ValueError) as ex:
        raise AccountError("Could not reach Loopcut. Check your connection", "unreachable") from ex
    if not isinstance(payload, dict):
        raise AccountError("Unexpected reply from the Loopcut service", "bad_reply")
    return payload


def open_browser(url: str) -> None:
    """Python's webbrowser module (which wm.url_open also uses) drives macOS through an
    AppleScript "open location", and that has opened an empty tab in the default browser.
    The OS launchers take the URL as an argument and get it right."""
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["/usr/bin/open", url])
        elif sys.platform == "win32":
            os.startfile(url)  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", url])
    except OSError as ex:
        print(f"Loopcut: could not open the browser: {ex}")
        import webbrowser
        webbrowser.open(url)


def _setup_event(ok: bool, code: str = "") -> None:
    """The Loopcut sign-in's outcome as a setup_step usage event (telemetry.py); any thread, never raises."""
    try:
        from . import telemetry
        telemetry.setup_step("loopcut_account", "loopcut", ok, code or None)
    except Exception:
        pass


def _platform() -> dict:
    """os and arch for the device poll (the usage events' values): the server records them with the link."""
    from . import telemetry
    return {"os": telemetry.os_name(), "arch": telemetry.arch_name()}


def _device_name() -> str:
    import platform
    return f"Blender on {platform.node() or platform.system()}"[:80]


def _device_flow(reply: dict, fresh: bool = False) -> dict:
    """Validate fresh and saved replies alike; a saved secret must only go back to this service.
    A fresh code lasts 15 minutes by this computer's clock (the server's): its absolute expires_at
    would reject every code on a computer whose clock is off. A saved code keeps that local deadline."""
    device_code, user_code = reply.get("device_code"), reply.get("user_code")
    url = reply.get("verification_url")
    try:
        expiry = time.time() + 15 * 60 if fresh else float(reply["expires_local"])
        interval = float(reply.get("interval", 3))
        address, service = urlsplit(url), urlsplit(SERVICE_URL)
    except (KeyError, TypeError, ValueError, AttributeError):
        raise AccountError("The sign-in reply was incomplete", "bad_reply") from None
    if not (isinstance(device_code, str) and re.fullmatch(r"[A-Za-z0-9_-]{32,128}", device_code)
            and isinstance(user_code, str) and re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{4}", user_code)
            and isinstance(url, str) and address.scheme == "https" and address.netloc == service.netloc
            and address.path == "/link" and math.isfinite(interval) and 1 <= interval <= 30
            and time.time() < expiry <= time.time() + 15 * 60 + 30):
        raise AccountError("The sign-in code expired or was incomplete; try again", "bad_reply")
    return {"device_code": device_code, "user_code": user_code, "verification_url": url,
            "expires_local": expiry, "interval": interval}


def _clear_pending_device() -> None:
    try:
        credentials.store_pending_device(SERVICE_URL, None)
    except OSError:
        # A disk failure must not trap Cancel or hide a key that was successfully stored.
        print("Loopcut: could not clear the saved sign-in code")


def _sign_in_cancelled(cancel) -> bool:
    if cancel is not _cancel:
        return True
    if cancel.is_set():
        _state.update(status="idle", user_code="", url="", error="")
        return True
    return False


def _run_sign_in(resume_only: bool = False, cancel=None) -> None:
    cancel = cancel if cancel is not None else _cancel
    try:
        if _sign_in_cancelled(cancel):
            return
        saved = credentials.pending_device(SERVICE_URL)
        try:
            started = _device_flow(saved) if saved else None
        except AccountError:
            _clear_pending_device()
            started = None
        if not started:
            if resume_only:
                _state.update(status="idle")
                return
            started = _device_flow(_post("/api/device/start", {"device_name": _device_name()}), fresh=True)
        device_code, user_code, url = started["device_code"], started["user_code"], started["verification_url"]
        interval = started["interval"]
        expiry = started["expires_local"]
        if _sign_in_cancelled(cancel):
            return
        if credentials.store_pending_device(SERVICE_URL, started, cancelled=cancel) is False:
            _state.update(status="idle", user_code="", url="")
            return
        _state.update(status="waiting", user_code=user_code, url=url, error="")
        _redraw()
        if _sign_in_cancelled(cancel):
            return
        if not resume_only:
            open_browser(url)
        deadline = time.monotonic() + max(0, expiry - time.time())
        retry_interval = interval
        while time.time() < expiry and time.monotonic() < deadline:
            if cancel.wait(min(retry_interval, max(0, expiry - time.time()))):
                _sign_in_cancelled(cancel)
                return
            if time.time() >= expiry:
                break
            try:
                polled = _post("/api/device/poll", {"device_code": device_code, **_platform()})
            except AccountError as ex:
                if _sign_in_cancelled(cancel):
                    return
                if ex.code == "unreachable" or ex.code == "http_429" or ex.code.startswith("http_5"):
                    retry_interval = min(30, retry_interval * 2)
                    _state.update(error="Connection interrupted. Retrying while this code is valid.")
                    _redraw()
                    continue
                raise
            retry_interval = interval
            if _sign_in_cancelled(cancel):
                return
            _state.update(error="")
            outcome = polled.get("status")
            if outcome == "pending":
                continue
            if outcome == "approved" and polled.get("api_key"):
                key = str(polled["api_key"])
                generation = mainthread.run_on_main(lambda: None if _sign_in_cancelled(cancel) else _store_key(key)).result()
                if _sign_in_cancelled(cancel):
                    return
                _clear_pending_device()
                _state.update(status="idle", user_code="", url="", error="")
                _setup_event(True)
                _refresh_account(key, generation=generation)
                return
            raise AccountError("Sign-in was denied" if outcome == "denied" else "The sign-in code expired; try again",
                               "denied" if outcome == "denied" else "expired")
        raise AccountError("The sign-in code expired; try again", "expired")
    except AccountError as ex:
        if _sign_in_cancelled(cancel):
            return
        _clear_pending_device()
        print(f"Loopcut: sign-in failed: {ex}")
        _setup_event(False, ex.code)
        _state.update(status="error", error=str(ex) + ". Try Sign in to Loopcut again, or use another route.", user_code="", url="")
    except Exception as ex:  # Worker thread: show it rather than lose it.
        if _sign_in_cancelled(cancel):
            return
        import traceback
        traceback.print_exc()
        _setup_event(False, "internal")
        _state.update(status="error", error="Sign-in could not finish. Try Sign in to Loopcut again, or use another route.",
                      user_code="", url="")
    finally:
        _redraw()


def _own_route_works(prefs) -> bool:
    """A provider of the user's own (their key, or a local model) that loads: kept over a new sign-in."""
    if prefs.provider in ("LOOPCUT", "CHATGPT"):
        return False
    try:
        config.load()
    except config.ConfigError:
        return False
    return True


def _store_key(key: str) -> int:
    """Main thread: the credentials folder comes from bpy, and the preferences page changes."""
    from . import chatgpt_auth, settings
    global _credential_generation
    _credential_generation += 1
    _state.update(usage_refresh_at=None, usage_refresh_attempts=0, usage_refresh_pending=False)
    state.clear_allowance_notice(invalidate=True)
    state.ui.pop("warned_session_allowance", None)
    state.ui.pop("warned_week_allowance", None)
    generation = _credential_generation
    credentials.store(BASE_URL, key)
    prefs = settings.preferences()
    # A signed-in ChatGPT plan stays the model, and so does a working key of the user's own (an install from
    # before the setup signing in now): the account adds asset search and credits. Only the user's own pick of
    # Loopcut Fast/Pro (the provider menu, onboarding's "Use Loopcut's models") moves it.
    if prefs is not None and prefs.provider != "LOOPCUT" and not chatgpt_auth.plan_signed_in() \
            and not _own_route_works(prefs):
        prefs.provider = "LOOPCUT"
    settings.config_changed()
    try:
        bpy.ops.wm.save_userpref()
    except RuntimeError as ex:
        print(f"Loopcut: could not save preferences after sign-in: {ex}")
    for callback in list(on_signed_in):
        try:
            callback()
        except Exception:  # One listener must not stop the sign-in from completing.
            import traceback
            traceback.print_exc()
    return generation


def mark_icon() -> int:
    """icon_value of the Loopcut mark for layout buttons and labels; 0 before register()."""
    return _previews["mark"].icon_id if _previews and "mark" in _previews else 0


def _load_mark() -> None:
    global _previews
    from .ui import brand
    _previews = bpy.utils.previews.new()
    preview = _previews.new("mark")
    pixels = brand.square(_MARK_SIZE, margin=0.0)[::-1]  # Previews start at the bottom row.
    preview.icon_size = (_MARK_SIZE, _MARK_SIZE)
    preview.icon_pixels_float = pixels.astype("float32").ravel().tolist()


def _set_account(payload: dict | None) -> None:
    global _credential_generation
    notice = state.ui.get("allowance_notice")
    previous_plan = (_state["account"] or {}).get("plan") or ((notice or {}).get("item") or {}).get("plan")
    if payload is None:
        _credential_generation += 1
        _state["usage_refresh_pending"] = False
        state.clear_allowance_notice(invalidate=True)
        state.ui.pop("warned_session_allowance", None)
        state.ui.pop("warned_week_allowance", None)
    elif previous_plan and payload.get("plan") and payload["plan"] != previous_plan:
        state.clear_allowance_notice()
    elif notice and notice["item"].get("reason") in allowance.LIMIT_WINDOWS \
            and not allowance.refused_windows(payload, notice):
        state.clear_allowance_notice()
    _state["account"] = payload
    if not any(allowance.expired((payload or {}).get(name)) for name in ("session", "week")):
        _state.update(usage_refresh_at=None, usage_refresh_attempts=0)
    _state["fetched_at"] = time.monotonic()
    state.ui["account"] = payload  # The panel footer reads it without touching bpy.


def _refresh_account(key: str, on_done=None, generation: int | None = None) -> None:
    generation = _credential_generation if generation is None else generation
    payload, catalogue = None, None
    try:
        payload = _get("/v1/me", key)
        catalogue = _get("/v1/models", key)
    except AccountError as ex:
        _state["error"] = str(ex)

    def apply():
        if generation != _credential_generation:
            return
        if payload is not None:
            _set_account(payload)
        if catalogue is not None:
            _set_models(catalogue)
        if on_done:
            on_done()
        _redraw()
    mainthread.run_on_main(apply)


def _set_models(payload: dict) -> None:
    """The catalogue as the service lists it; see loopcut-web's /v1/models."""
    rows = [r for r in payload.get("data") or [] if isinstance(r, dict) and r.get("id")]
    if rows:
        _state["models"] = rows
        _state["default_model"] = str(payload.get("default") or next((r["id"] for r in rows if r.get("default")), "fast"))


def models() -> list[dict]:
    """(id, label, blurb, note, available, default) per model the service offers, in its order.
    The blurb says what the model is good at; the note, short enough for the panel's model menu,
    what a step costs against the default ("2.4x usage") and which plan unlocks it."""
    rows = _state["models"]
    if not rows:
        return [{"id": id, "label": label, "blurb": blurb, "note": "", "available": True, "default": id == "fast"}
                for id, label, blurb in MODELS]
    out = []
    for r in rows:
        parts = []
        multiplier = r.get("usage_multiplier")
        if isinstance(multiplier, (int, float)) and not r.get("default"):
            parts.append(f"{multiplier:g}x usage")
        unlock = r.get("unlock_plan") or {}
        if not r.get("available", True):
            parts.append(f"{unlock.get('name') or 'a higher'} plan" if config.billing_open() else "Unavailable")
        out.append({"id": str(r["id"]), "label": str(r.get("display_name") or r["id"]),
                    "blurb": str(r.get("blurb") or ""), "note": "  ·  ".join(parts),
                    "available": bool(r.get("available", True)),
                    "unlock_plan": dict(unlock),
                    "default": bool(r.get("default"))})
    return out


def default_model() -> str:
    return _state["default_model"]


def keeps_context(base_url: str, model_id: str) -> bool:
    """Whether the service lists this model with keep_context: its upstream's cache rewards only
    requests that extend the previous one whole, so context.py must not rewrite a turn."""
    return base_url.rstrip("/") == BASE_URL and any(
        r.get("id") == model_id and r.get("keep_context") is True for r in (_state["models"] or []))


def model_label(model_id: str) -> str:
    model_id = LEGACY_MODELS.get(model_id, model_id)
    return next((m["label"] for m in models() if m["id"] == model_id), model_id)


def update_usage(info: dict) -> None:
    """Fold what a gateway reply's headers said (plan, session and week usage) into the account,
    so the panel stays current without a request to /v1/me."""
    previous = (_state["account"] or {}).get("plan")
    _set_account({**(_state["account"] or {}), **info})
    if info.get("plan") and info["plan"] != previous:
        _state["usage_refresh_at"] = time.monotonic()
        refresh_account()  # Model availability is account-specific too.
    else:
        refresh_expired_usage()


def refresh_expired_usage() -> None:
    """Main thread: bounded retries shared by UI polls and late helper reports."""
    account = _state["account"] or {}
    if not any(allowance.expired(account.get(name)) for name in ("session", "week")):
        return
    attempts = _state["usage_refresh_attempts"]
    if _state["usage_refresh_pending"] or attempts > len(USAGE_REFRESH_DELAYS):
        return
    now = time.monotonic()
    previous = _state.get("usage_refresh_at")
    if attempts and previous is not None and now - previous < USAGE_REFRESH_DELAYS[attempts - 1]:
        return
    _state.update(usage_refresh_pending=True, usage_refresh_attempts=attempts + 1)

    def done():
        # Include the network wait and the queued main-thread update in the in-flight guard.
        _state.update(usage_refresh_pending=False, usage_refresh_at=time.monotonic())

    refresh_account(on_done=done)


def watch_plan(previous: str, on_change) -> None:
    """The user went to the plans page: look at /v1/me every few seconds until the plan is no
    longer `previous`, then call on_change(account) on the main thread. A newer watch replaces
    an older one; WATCH_FOR later the watch gives up quietly."""
    global _watch_generation
    _watch_generation += 1
    generation = _watch_generation
    account_generation = _credential_generation
    key = credentials.api_key(BASE_URL)
    if not key:
        return

    def watch() -> None:
        deadline = time.monotonic() + WATCH_FOR
        while time.monotonic() < deadline and generation == _watch_generation:
            time.sleep(WATCH_EVERY)
            try:
                payload = _get("/v1/me", key)
            except AccountError:
                continue
            if str(payload.get("plan") or "") not in ("", previous):
                try:
                    catalogue = _get("/v1/models", key)
                except AccountError:
                    continue  # Do not finish the watch with the old plan's model locks.
                def apply():
                    if generation != _watch_generation or account_generation != _credential_generation:
                        return
                    _set_account(payload)
                    _set_models(catalogue)
                    on_change(payload)
                    _redraw()
                mainthread.run_on_main(apply)
                return

    threading.Thread(target=watch, name="loopcut-plan-watch", daemon=True).start()


def stop_watching() -> None:
    global _watch_generation
    _watch_generation += 1


def sign_in() -> bool:
    global _cancel
    if _state["status"] in ("starting", "waiting"):
        return False
    _cancel = threading.Event()
    _state.update(status="starting", error="")
    threading.Thread(target=_run_sign_in, args=(False, _cancel), name="loopcut-sign-in", daemon=True).start()
    return True


def cancel_sign_in() -> None:
    _cancel.set()
    _clear_pending_device()


def sign_out() -> None:
    """Forgets the key on this computer. The account page on the site can revoke it as well."""
    from . import settings
    cancel_sign_in()
    credentials.store(BASE_URL, "")
    stop_watching()
    _set_account(None)
    _state.update(error="", status="idle", models=None, default_model="fast")
    settings.config_changed()
    from . import cloud
    cloud.refresh_async()


def refresh_account(on_done=None) -> None:
    key = credentials.api_key(BASE_URL)
    if key:
        generation = _credential_generation
        threading.Thread(target=_refresh_account, args=(key, on_done, generation), name="loopcut-account", daemon=True).start()
    elif on_done:
        on_done()


def account_line() -> str:
    """One line for the preferences page and the onboarding step."""
    account = _state["account"]
    if not signed_in():
        return "Not signed in"
    if not account:
        return "Signed in"
    who = account.get("email") or "Signed in"
    plan = str(account.get("plan") or "").capitalize() if config.billing_open() else ""  # No sales, no plan names.
    return f"{who}  ·  {plan} plan" if plan else who


def usage_lines() -> list[str]:
    account = _state["account"]
    if not account:
        return []
    lines = []
    refused = allowance.refused_windows(account, state.ui.get("allowance_notice"))
    for key, label in (("session", "Session"), ("week", "Week")):
        used = 1.0 if key in refused else allowance.used(account.get(key))
        if used is not None:
            lines.append(f"{label}: {round(used * 100):d}% used")
    return lines


class LOOPCUT_OT_sign_in(bpy.types.Operator):
    """Open loopcut in your browser to sign in or create an account"""
    bl_idname = "loopcut.sign_in"
    bl_label = "Sign in to Loopcut"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        if not sign_in():
            return {"CANCELLED"}
        return {"FINISHED"}


class LOOPCUT_OT_open_sign_in_page(bpy.types.Operator):
    """Open the sign-in page in your browser again"""
    bl_idname = "loopcut.open_sign_in_page"
    bl_label = "Open the Page Again"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        if not _state["url"]:
            return {"CANCELLED"}
        open_browser(_state["url"])
        return {"FINISHED"}


class LOOPCUT_OT_cancel_sign_in(bpy.types.Operator):
    bl_idname = "loopcut.cancel_sign_in"
    bl_label = "Cancel"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        cancel_sign_in()
        return {"FINISHED"}


class LOOPCUT_OT_sign_out(bpy.types.Operator):
    """Forget the Loopcut key stored on this computer"""
    bl_idname = "loopcut.sign_out"
    bl_label = "Sign out"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        sign_out()
        return {"FINISHED"}


class LOOPCUT_OT_copy_sign_in_link(bpy.types.Operator):
    """Copy the sign-in link, for when the browser did not open it"""
    bl_idname = "loopcut.copy_sign_in_link"
    bl_label = "Copy Link"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        if not _state["url"]:
            return {"CANCELLED"}
        context.window_manager.clipboard = _state["url"]
        self.report({"INFO"}, "Sign-in link copied")
        return {"FINISHED"}


class LOOPCUT_OT_refresh_account(bpy.types.Operator):
    """Fetch your plan and usage from Loopcut"""
    bl_idname = "loopcut.refresh_account"
    bl_label = "Refresh"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        refresh_account()
        return {"FINISHED"}


SIGNED_OUT_NOTE = "Free: asset search and cloud tools. Works alongside your ChatGPT plan."


def _credits_line() -> str:
    from . import cloud, cloud_account
    runtime = cloud._runtime
    if runtime is None:
        return ""
    with runtime.lock:
        return cloud_account.credits_label(runtime.manifest, runtime.signed_in)


def draw(column) -> None:
    """The sign-in block on the preferences page."""
    state = _state
    if state["status"] == "waiting":
        column.label(text=f"Approve code {state['user_code']} in your browser", icon="URL")
        row = column.row(align=True)
        row.operator("loopcut.open_sign_in_page", text="Open the page again")
        row.operator("loopcut.copy_sign_in_link", text="Copy Link")
        row.operator("loopcut.cancel_sign_in", text="Cancel")
        if state["error"]:
            column.label(text=state["error"], icon="INFO")
        return
    if state["status"] == "starting":
        column.label(text="Contacting Loopcut…")
        column.operator("loopcut.cancel_sign_in", text="Cancel")
        return
    billing = config.billing_open()
    if signed_in():
        row = column.row(align=True)
        row.label(text=account_line(), icon="CHECKMARK")
        row.operator("loopcut.refresh_account", text="", icon="FILE_REFRESH")
        credits = _credits_line() if billing else ""
        if credits:
            column.label(text=credits)
        for line in usage_lines():
            column.label(text=line)
        row = column.row(align=True)
        if billing:  # Buying happens on loopcut.org, never in Blender.
            row.operator("wm.url_open", text="Credits").url = config.credits_url("preferences")
            row.operator("wm.url_open", text="See plans").url = config.pricing_url("preferences")
        else:
            row.operator("wm.url_open", text="Account").url = f"{SERVICE_URL}/account"
        row.operator("loopcut.sign_out")
    else:
        column.label(text=SIGNED_OUT_NOTE)
        column.operator("loopcut.sign_in", icon="USER")
    if state["status"] == "error" and state["error"]:
        column.label(text=state["error"], icon="ERROR")


_CLASSES = (LOOPCUT_OT_sign_in, LOOPCUT_OT_open_sign_in_page, LOOPCUT_OT_copy_sign_in_link, LOOPCUT_OT_cancel_sign_in,
            LOOPCUT_OT_sign_out, LOOPCUT_OT_refresh_account)


def register() -> None:
    global _cancel
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    try:
        _load_mark()
    except Exception as ex:  # The mark is decoration; sign-in must work without it.
        print(f"Loopcut: could not load the mark icon: {ex}")
    if signed_in():
        refresh_account()
    elif not bpy.app.background:
        # File/network work stays on the worker; do not reopen a browser just for restarting Blender.
        _cancel = threading.Event()
        _state.update(status="starting", error="")
        threading.Thread(target=_run_sign_in, args=(True, _cancel), name="loopcut-sign-in", daemon=True).start()


def unregister() -> None:
    global _previews
    _cancel.set()  # Preserve the private pending code for the next interactive start.
    on_signed_in.clear()
    if _previews is not None:
        bpy.utils.previews.remove(_previews)
        _previews = None
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
