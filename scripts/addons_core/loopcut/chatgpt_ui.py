"""The "ChatGPT plan" provider in Blender: Sign in with ChatGPT, the signed-in account, Manage usage
and Sign out on Loopcut's preferences page (settings.py) and on onboarding's model connection screen.
The flow itself is chatgpt_auth.py; it runs on a worker thread, and whatever touches bpy goes through
mainthread. A sign-in makes the ChatGPT plan Loopcut's provider (_finished).

Wording: one button, "Sign in with ChatGPT", everywhere (the owner's choice over SIWC's "Continue with
ChatGPT"); the page says when requests use the ChatGPT plan with a "Manage usage" link to ChatGPT's usage
settings, and the first sign-in shows "You're using your ChatGPT plan" once.
"""

import threading
import time

import bpy

from . import chatgpt_auth, chatgpt_ready, mainthread

_state = {"status": "idle", "error": "", "url": ""}   # status: idle | waiting | error
_cancel = threading.Event()


def _redraw() -> None:
    mainthread.request_redraw()


def _finished(result: dict | None, error: str) -> None:
    """Main thread, after a sign-in attempt: signed in, the ChatGPT plan becomes the provider and onboarding
    stays on model setup for the readiness result."""
    from . import onboarding, settings
    if _cancel.is_set():
        result, error = None, ""
    _state.update(status="error" if error else "idle", error=error, url="")
    prefs = settings.preferences()
    if result and result.get("signed_in") and prefs is not None and prefs.provider != "CHATGPT":
        prefs.provider = "CHATGPT"  # settings._provider_changed sets its URL and model.
    settings.config_changed()
    if result and result.get("signed_in"):
        request_readiness(force=True)
        try:
            bpy.ops.wm.save_userpref()
        except RuntimeError as ex:
            print(f"Loopcut: could not save preferences after ChatGPT sign-in: {ex}")
    if result and result.get("signed_in") and not chatgpt_auth.welcomed():
        try:
            bpy.ops.loopcut.chatgpt_welcome("INVOKE_DEFAULT")
        except RuntimeError as ex:  # No window to show it in (background); the note is decoration.
            print(f"Loopcut: could not show the ChatGPT plan note: {ex}")
        chatgpt_auth.mark_welcomed()
    _redraw()


def _run_sign_in(new_account: bool) -> None:
    from . import account, telemetry
    result, error, code = None, "", ""
    try:
        result = chatgpt_auth.sign_in(account.open_browser, new_account=new_account, cancelled=_cancel.is_set,
                                      on_url=lambda url: (_state.update(status="waiting", url=url), _redraw()))
    except chatgpt_auth.ChatGPTAuthError as ex:
        error = "" if ex.code == "cancelled" else str(ex)
        code = ex.code or (f"http_{ex.status}" if ex.status else "sign_in_failed")
        if ex.stage == "token":  # Refused at the code exchange, not on the way back from the browser.
            code = "token_" + code
    except Exception:  # The worker thread must always report back.
        import traceback
        traceback.print_exc()
        error, code = "ChatGPT sign-in could not finish.", "internal"
    if code != "cancelled" and not _cancel.is_set():  # Whether ChatGPT sign-in worked: a setup_step usage event.
        telemetry.setup_step("chatgpt", "chatgpt", bool(result and result.get("signed_in")) and not error,
                             None if result and not error else code or "not_signed_in")
    mainthread.run_on_main(lambda: _finished(result, error))


_ready = {"status": "idle", "code": "", "model": "", "account": (), "selected": "", "note": ""}
_checked = set()
_ready_cancel = threading.Event()
_force_ready = False


def request_readiness(force: bool = False) -> None:
    """Main thread: debounce model edits; all network work stays on the worker."""
    global _force_ready
    if force:
        _force_ready = True
        _checked.clear()
    if not bpy.app.background and not bpy.app.timers.is_registered(_start_readiness):
        bpy.app.timers.register(_start_readiness, first_interval=0.5)


def _start_readiness():
    global _ready_cancel, _force_ready
    from . import settings
    prefs = settings.preferences()
    if prefs is None or prefs.provider != "CHATGPT" or not chatgpt_auth.plan_signed_in():
        _ready_cancel.set()
        _checked.clear()
        return None
    account, selected = chatgpt_ready.account_key(), prefs.model
    key = (account, selected)
    if key in _checked and not _ready_cancel.is_set():
        return None
    _checked.clear()
    _checked.add(key)
    _ready_cancel.set()
    cancel = threading.Event()
    _ready_cancel = cancel
    force, _force_ready = _force_ready, False
    # Not gated on bpy.app.online_access: it is off on most installs and Loopcut's chat never reads it, so the
    # gate only skipped the check (and its model fallback) for people who then chatted fine. A real network
    # failure still ends the check as "offline".
    actual = chatgpt_ready.effective_model(account, selected)
    if not force and chatgpt_ready.verified(account, actual):
        note = _ready["note"] if _ready["account"] == account and _ready["model"] == actual else ""
        _ready.update(status="done", code="ready", model=actual, account=account, selected=selected, note=note)
        if actual != selected:
            model_used(account, selected, actual)
        return None
    _ready.update(status="checking", code="", model=selected, account=account, selected=selected, note="",
                  started=time.monotonic())
    _redraw()

    def run():
        try:
            result = chatgpt_ready.check(selected, cancel.is_set, fresh=force)
        except Exception:  # Always retire the UI's in-flight state, even for an unexpected worker failure.
            import traceback
            traceback.print_exc()
            result = {"code": "check_failed", "model": selected, "account": account}
        mainthread.run_on_main(lambda: _readiness_finished(result, selected, cancel))
    threading.Thread(target=run, name="loopcut-chatgpt-ready", daemon=True).start()
    return None


def _readiness_finished(result: dict, selected: str, cancel) -> None:
    from . import settings, telemetry
    prefs = settings.preferences()
    if cancel.is_set() or result["account"] != chatgpt_ready.account_key() or prefs is None \
            or prefs.provider != "CHATGPT" or prefs.model != selected:
        if cancel is _ready_cancel:
            _checked.discard((result["account"], selected))
        return
    code = result["code"]
    _ready.update(result, status="done", selected=selected)
    telemetry.setup_step("chatgpt", "chatgpt", code == "ready", "readiness_" + code)
    if code == "ready" and result["model"] != selected:
        model_used(result["account"], selected, result["model"])
    _redraw()


def readiness() -> dict:
    from . import settings
    prefs = settings.preferences()
    if prefs is None or prefs.provider != "CHATGPT" or _ready["account"] != chatgpt_ready.account_key() \
            or prefs.model not in (_ready["selected"], _ready["model"]):
        return {"status": "idle", "code": "", "note": ""}
    return dict(_ready)


def draw_readiness(column) -> None:
    ready = readiness()
    if ready["status"] == "checking":
        column.label(text="Checking ChatGPT…", icon="TIME")
    elif ready["code"]:
        from . import settings
        for line in settings.wrap(ready.get("note") or chatgpt_ready.message(ready["code"]), 70):
            column.label(text=line, icon="INFO" if ready["code"] == "ready" else "ERROR")
        if ready["code"] != "ready":
            column.operator("loopcut.chatgpt_check", text="Check again")
            if ready["code"] not in ("offline", "network", "unsupported_model"):
                column.operator("loopcut.chatgpt_sign_in", text="Sign in again")


# Only an answer that this plan can't work here holds setup back. A slow check or a passing error (5xx,
# timeouts) lets the user carry on; their first turn retries and says what went wrong.
REFUSALS = frozenset(("invalid_grant", "subscription_sharing_user_not_eligible", "no_plan_scope",
                      "unsupported_country_region_territory", "country_not_supported", "chatgpt_not_signed_in",
                      "subscription_sharing_invalid_user", "invalid_refresh_token", "unsupported_model"))
CHECK_WAIT = 20.0


def can_continue() -> bool:
    ready = readiness()
    if ready["status"] == "checking":
        return time.monotonic() - ready.get("started", 0.0) >= CHECK_WAIT
    return ready["code"] not in REFUSALS


def model_used(account: tuple, requested: str, actual: str) -> None:
    def apply():
        from . import settings, state
        prefs = settings.preferences()
        if account != chatgpt_ready.account_key() or prefs is None or prefs.provider != "CHATGPT" \
                or prefs.model != requested:
            return
        prefs.model = actual
        _ready.update(status="done", code="ready", model=actual, account=account, selected=requested,
                      note=chatgpt_ready.notice(requested, actual))
        state.session()["items"].append(state.item_notice(_ready["note"]))
        try:
            bpy.ops.wm.save_userpref()
        except RuntimeError as ex:
            print(f"Loopcut: could not save the ChatGPT model: {ex}")
        _redraw()
    mainthread.run_on_main(apply)


class LOOPCUT_OT_chatgpt_check(bpy.types.Operator):
    bl_idname = "loopcut.chatgpt_check"
    bl_label = "Check ChatGPT"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        from . import settings
        prefs = settings.preferences()
        if prefs is not None and prefs.provider != "CHATGPT":
            prefs.provider = "CHATGPT"
        request_readiness(force=True)
        return {"FINISHED"}


def sign_in(new_account: bool = False) -> bool:
    if _state["status"] == "waiting":
        return False
    _cancel.clear()
    _state.update(status="waiting", error="", url="")
    threading.Thread(target=_run_sign_in, args=(new_account,), name="loopcut-chatgpt-sign-in", daemon=True).start()
    return True


def cancel_sign_in() -> None:
    _cancel.set()


def _run_sign_out() -> None:
    try:
        confirmed = chatgpt_auth.sign_out()
        error = "" if confirmed else ("Signed out on this computer; ChatGPT did not confirm the revocation. "
                                      "You can disconnect Loopcut in ChatGPT's settings.")
    except chatgpt_auth.ChatGPTAuthError as ex:
        error = str(ex)
    mainthread.run_on_main(lambda: _finished(None, error))


def status() -> dict:
    """The sign-in in flight: {status: idle | waiting | error, error, url}."""
    return dict(_state)


def account_line() -> str:
    info = chatgpt_auth.status()
    if not info.get("signed_in"):
        return "Not signed in"
    plan = str(info.get("plan_type") or "").capitalize()
    who = info.get("email") or "Signed in"
    return f"{who}  ·  ChatGPT {plan}" if plan else who


class LOOPCUT_OT_chatgpt_sign_in(bpy.types.Operator):
    """Sign in with your ChatGPT account so Loopcut's requests use your ChatGPT plan"""
    bl_idname = "loopcut.chatgpt_sign_in"
    bl_label = "Sign in with ChatGPT"
    bl_options = {"INTERNAL"}

    new_account: bpy.props.BoolProperty(default=False, options={"SKIP_SAVE"})

    def execute(self, context):
        return {"FINISHED"} if sign_in(self.new_account) else {"CANCELLED"}


class LOOPCUT_OT_chatgpt_cancel(bpy.types.Operator):
    bl_idname = "loopcut.chatgpt_cancel"
    bl_label = "Cancel"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        cancel_sign_in()
        return {"FINISHED"}


class LOOPCUT_OT_chatgpt_copy_link(bpy.types.Operator):
    """Copy the sign-in link, for when the browser did not open it"""
    bl_idname = "loopcut.chatgpt_copy_link"
    bl_label = "Copy Link"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        if not _state["url"]:
            return {"CANCELLED"}
        context.window_manager.clipboard = _state["url"]
        self.report({"INFO"}, "Sign-in link copied")
        return {"FINISHED"}


class LOOPCUT_OT_chatgpt_sign_out(bpy.types.Operator):
    """Revoke Loopcut's ChatGPT session and forget its tokens on this computer"""
    bl_idname = "loopcut.chatgpt_sign_out"
    bl_label = "Sign out"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        threading.Thread(target=_run_sign_out, name="loopcut-chatgpt-sign-out", daemon=True).start()
        return {"FINISHED"}


class LOOPCUT_OT_chatgpt_manage_usage(bpy.types.Operator):
    """Open ChatGPT's usage settings: your plan's usage and Loopcut's weekly limit"""
    bl_idname = "loopcut.chatgpt_manage_usage"
    bl_label = "Manage usage"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        from . import account
        account.open_browser(chatgpt_auth.USAGE_URL)
        return {"FINISHED"}


class LOOPCUT_OT_chatgpt_welcome(bpy.types.Operator):
    """Shown once, after the first sign-in with ChatGPT plan use"""
    bl_idname = "loopcut.chatgpt_welcome"
    bl_label = "You're using your ChatGPT plan"
    bl_options = {"INTERNAL"}

    def invoke(self, context, event):
        try:
            return context.window_manager.invoke_props_dialog(self, width=420, confirm_text="Got it")
        except TypeError:  # Blender before 4.1 has no confirm_text.
            return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, context):
        column = self.layout.column(align=True)
        for line in ("Loopcut's model requests now use your ChatGPT plan.",
                     "You can review usage and set Loopcut's limit in ChatGPT settings."):
            column.label(text=line)
        column.separator()
        column.operator("loopcut.chatgpt_manage_usage", icon="URL")
        from . import account
        column.separator()
        if account.signed_in():  # Both connected: the account keeps adding what a chat plan can't.
            column.label(text="Your Loopcut account still adds asset search and cloud tools.")
        else:
            for line in ("Want images, 3D, audio or asset search too?", "Those run on a free Loopcut account."):
                column.label(text=line)
            column.operator("loopcut.sign_in", text="Create free Loopcut account", icon_value=account.mark_icon())

    def execute(self, context):
        return {"FINISHED"}


def draw(column, prefs=None) -> None:
    """The ChatGPT plan block: sign-in state, Manage usage, Sign out, and the model row."""
    if _state["status"] == "waiting":
        column.label(text="Finish signing in with ChatGPT in your browser", icon="URL")
        row = column.row(align=True)
        if _state["url"]:
            row.operator("loopcut.chatgpt_copy_link", text="Copy Link")
        row.operator("loopcut.chatgpt_cancel", text="Cancel")
        column.label(text="Browser did not open? Copy Link and paste it into your browser.")
        return
    info = chatgpt_auth.status()
    if info.get("signed_in"):
        column.label(text=account_line(), icon="CHECKMARK")
        if info.get("plan_usage"):
            row = column.row(align=True)
            row.label(text="Using ChatGPT plan")
            row.operator("loopcut.chatgpt_manage_usage", icon="URL")
        else:
            column.label(text="This sign-in did not allow ChatGPT plan use", icon="ERROR")
            column.operator("loopcut.chatgpt_sign_in", text="Sign in with ChatGPT")
        row = column.row(align=True)
        row.operator("loopcut.chatgpt_sign_in", text="Use another account").new_account = True
        row.operator("loopcut.chatgpt_sign_out")
    else:
        lines = ("Your ChatGPT Plus or Pro plan runs Loopcut's agent: eligible requests use the",
                 "usage included in your plan. Loopcut credits never pay for it.")
        for n, line in enumerate(lines):
            column.label(text=line, icon="INFO" if n == 0 else "BLANK1")
        column.operator("loopcut.chatgpt_sign_in", text="Sign in with ChatGPT", icon="USER")
    if _state["status"] == "error" and _state["error"]:
        column.label(text=_state["error"][:160], icon="ERROR")
        column.label(text="Try Sign in with ChatGPT again, or choose another provider.")
        if not info.get("signed_in"):
            column.operator("loopcut.chatgpt_sign_in", text="Use another ChatGPT account").new_account = True
    if prefs is not None and info.get("signed_in"):
        from . import settings
        draw_readiness(column)
        row = column.row(align=True)
        row.prop(prefs, "model")
        row.operator("loopcut.fetch_models", text="", icon="FILE_REFRESH")
        if settings.has_models():
            row.menu("LOOPCUT_MT_models", text="", icon="DOWNARROW_HLT")


_CLASSES = (LOOPCUT_OT_chatgpt_check, LOOPCUT_OT_chatgpt_sign_in, LOOPCUT_OT_chatgpt_cancel, LOOPCUT_OT_chatgpt_copy_link,
            LOOPCUT_OT_chatgpt_sign_out, LOOPCUT_OT_chatgpt_manage_usage, LOOPCUT_OT_chatgpt_welcome)


def register() -> None:
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    request_readiness()


def unregister() -> None:
    _cancel.set()
    _ready_cancel.set()
    _checked.clear()
    if bpy.app.timers.is_registered(_start_readiness):
        bpy.app.timers.unregister(_start_readiness)
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
