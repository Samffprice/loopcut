"""First run of the Loopcut build, drawn in the splash screen: bring your Blender setup along,
optionally create a Loopcut account, connect a model, see what is sent where.

The fork's splash menus (WM_MT_splash_quick_setup, WM_MT_splash in bl_operators/wm.py) call
draw_splash() and draw themselves when it returns False. Which of the two menus is on screen is
Blender's decision: the quick setup until preferences are saved, the normal splash after.

Steps: "import" (only if stock Blender settings were found), "setup" (Blender's own quick setup,
for a fresh start), "account" (optional), "connect" (model), "privacy", "done". State lasts for the
session; someone who quits half way gets the normal splash next time and the panel itself asks for a key.
"""

import shutil
import sys
from pathlib import Path

import bpy

from . import account, blender_import, chatgpt_auth, config, settings

# left_chatgpt: "Use your own API key" moved the provider off a ChatGPT plan; "Back to model choices" puts it back.
_state = {"step": None, "imported": None, "failed": [], "error": "", "own_key": False, "left_chatgpt": False}
_popups = set()  # Splash regions to repaint while a sign-in is in flight; see _refresh_popups.
_REFRESH_SECONDS = 0.5


def _source():
    return blender_import.find_source(blender_import.stock_root(), bpy.app.version[:2])


def _connected() -> bool:
    prefs = settings.preferences()
    if prefs is not None and prefs.provider == "CHATGPT" and not _on_chatgpt_plan():
        return False
    try:
        config.load()
    except config.ConfigError:
        return False
    return True


def hints_dismissed() -> bool:
    """Keep the first-message hints dismissed across files, chats and app launches."""
    from . import state
    prefs = settings.preferences()
    return bool(state.ui.get("hints_dismissed") or getattr(prefs, "first_message_sent", False))


def dismiss_hints() -> None:
    from . import state
    if hints_dismissed():
        return
    state.ui["hints_dismissed"] = True
    prefs = settings.preferences()
    if prefs is not None:
        prefs.first_message_sent = True
        try:
            bpy.ops.wm.save_userpref()
        except RuntimeError as ex:
            print(f"Loopcut: could not save first-message preference: {ex}")


def _after_setup() -> str:
    return "done" if settings.preferences() is None else "account"


def draw_splash(layout, context, first_run: bool) -> bool:
    """True when a step was drawn in place of the menu that called."""
    step = _state["step"]
    if first_run:
        if step is None:
            # A previous Loopcut version's settings win: Blender's quick setup offers those itself.
            can_copy_prev = bpy.types.PREFERENCES_OT_copy_prev.poll(context)
            step = _state["step"] = "import" if not can_copy_prev and _source() else "setup"
        if step != "import":
            return False
        _draw_import(layout)
        return True
    if step is None:
        return False  # Preferences existed at startup: not a first run.
    if step in ("import", "setup"):  # Preferences were just saved or imported.
        step = _state["step"] = _after_setup()
    if step == "account":
        _draw_account(layout, context)
    elif step == "connect":
        _draw_connect(layout, context)
    elif step == "privacy":
        _draw_privacy(layout)
    else:
        _draw_done(layout)
        return False  # The normal splash follows: new file, recent files.
    return True


def _content(layout, title: str):
    layout.label(text=title)
    split = layout.split(factor=0.20)  # Same margins as Blender's quick setup.
    split.label()
    column = split.split(factor=0.73).column()
    column.emboss = "NORMAL"
    return column


def _notes(column, *paragraphs: str) -> None:
    sub = column.column(align=True)
    sub.scale_y = 0.75
    sub.active = False
    for paragraph in paragraphs:
        for line in settings.wrap(paragraph, 44):  # What fits the content column; labels do not wrap.
            sub.label(text=line)


def _step_button(column, text: str, step: str, save: bool = False):
    props = column.operator("loopcut.onboarding_step", text=text)
    props.step, props.save = step, save


def _draw_import(layout) -> None:
    version, source = _source() or ((0, 0), None)
    column = _content(layout, "Welcome to Loopcut")
    if source is None:  # Removed since the step was chosen.
        _step_button(column, "Continue", "setup")
        return
    column.operator("loopcut.import_blender_settings", text="Import Blender {:d}.{:d} Settings".format(*version))
    count = blender_import.addon_count(source)
    _notes(column,
           "Preferences, keymap, themes, startup file" + (f" and {count} add-ons." if count else "."),
           "Copied. Blender itself is left unchanged.")
    if _state["error"]:
        column.label(text=_state["error"], icon="ERROR")
    column.separator()
    _step_button(column, "Start Fresh", "setup")
    layout.separator(factor=2.0)


def _draw_connect(layout, context) -> None:
    prefs = settings.preferences()
    if not _state["own_key"]:
        _draw_sign_in(layout, context)
        return
    column = _content(layout, "Use Your Own Key")
    column.use_property_split = True
    column.use_property_decorate = False
    column.prop(prefs, "provider")
    if prefs.provider == "LOOPCUT":
        account.draw(column.column(align=True))
    elif prefs.provider == "CHATGPT":
        from . import chatgpt_ui
        chatgpt_ui.draw(column.column(align=True), prefs)
    else:
        if prefs.provider == "CUSTOM":
            column.prop(prefs, "base_url")
        local = prefs.base_url.startswith("http://127.0.0.1")
        if not local:
            settings.draw_key(column, prefs)
        row = column.row(align=True)
        row.prop(prefs, "model")
        row.operator("loopcut.fetch_models", text="", icon="FILE_REFRESH")
        if settings.has_models():
            row.menu("LOOPCUT_MT_models", text="", icon="DOWNARROW_HLT")
        _notes(column, "Choose your model provider, enter its API key, then type a model name or use the refresh button. "
               "The 'Your own keys' fields in preferences are for optional cloud tools, not the chat model.")
        if not _connected():
            try:
                config.load()
            except config.ConfigError as ex:
                _notes(column, str(ex), "Correct the connection fields above to enable Continue.")
    column.separator(factor=2)
    sub = column.column()
    sub.enabled = _connected()
    _step_button(sub, "Continue", "privacy", save=True)
    _link(column, "Back to model choices", "loopcut.onboarding_own_key").enable = False
    layout.separator(factor=2.0)


def _centered(layout):
    row = layout.row()
    row.alignment = "CENTER"
    return row


def _link(layout, text: str, operator: str):
    """A quiet, text-only button: the secondary choices under the big one."""
    row = _centered(layout)
    row.emboss = "NONE"
    row.active = False
    return row.operator(operator, text=text)


def _region_exists(region) -> bool:
    """Popup regions are freed when the popup closes and bpy does not invalidate the handle; the
    context override is the one safe probe (the same workaround Blender's extension notifier uses)."""
    try:
        with bpy.context.temp_override(region=region):
            return True
    except TypeError:
        return False


def _refresh_popups():
    """Timer: a popup only repaints on input, so while the sign-in thread works, repaint it here."""
    for region in list(_popups):
        if not _region_exists(region):
            _popups.discard(region)
            continue
        region.tag_redraw()
        region.tag_refresh_ui()
    from . import chatgpt_ui
    busy = (account.status()["status"] in ("starting", "waiting")
            or chatgpt_ui.status()["status"] == "waiting")
    return _REFRESH_SECONDS if busy and _popups else None


def _watch_popup(context) -> None:
    region = getattr(context, "region_popup", None)
    if region is not None:
        _popups.add(region)
    if not bpy.app.timers.is_registered(_refresh_popups):
        bpy.app.timers.register(_refresh_popups, first_interval=_REFRESH_SECONDS, persistent=True)


def _on_chatgpt_plan() -> bool:
    prefs = settings.preferences()
    info = chatgpt_auth.status()
    return prefs is not None and info.get("signed_in") and info.get("plan_usage")


ACCOUNT_LEAD = ("Step 1 of 2. The best setup is both: a Loopcut account for tools, your ChatGPT plan for the "
                "conversation. Either one is optional.")
ACCOUNT_UNLOCKS = ("Search free asset libraries from the chat", "Trial credits for cloud tools: images, 3D, sound",
                   "Loopcut's Fast model, free, if you don't use ChatGPT")
ACCOUNT_UNLOCKS_NO_SALES = (ACCOUNT_UNLOCKS[0], "Cloud tools as they open: images, 3D, sound", ACCOUNT_UNLOCKS[2])
ACCOUNT_FAILED = "Couldn't reach Loopcut. Try again, or skip and sign in later from the Loopcut panel."
CONNECT_LEAD = "Your ChatGPT Plus or Pro plan runs the agent, with no charge from Loopcut."


def _trial_line() -> str:
    """"100 trial credits ($1)" once the manifest reports the grant and the server sells; else ""."""
    from . import cloud, nudges
    runtime = cloud._runtime
    if runtime is None or not config.billing_open():
        return ""
    with runtime.lock:
        trial = ((runtime.manifest.get("account") or {}).get("credits") or {}).get("trial")
    amount = trial.get("amount") if isinstance(trial, dict) else None
    return f"{amount:g} trial credits ({nudges.dollars(amount)})" if type(amount) in (int, float) and amount > 0 else ""


def _draw_account(layout, context) -> None:
    """Accounts are encouraged independently of the model connection and never gate it."""
    column = _content(layout, "Create your free Loopcut account")
    status = account.status()
    if account.signed_in():
        column.label(text=f"Signed in as {account.account_line()}", icon="CHECKMARK")
        trial = _trial_line()
        if trial:
            column.label(text=trial, icon="BLANK1")
        _step_button(column, "Continue", "connect")
    else:
        if status["status"] in ("starting", "waiting"):
            _watch_popup(context)
        if status["status"] == "waiting":
            column.label(text=f"Approve code {status['user_code']} in your browser", icon="URL")
            row = column.row(align=True)
            row.operator("loopcut.open_sign_in_page", text="Open the page again")
            row.operator("loopcut.copy_sign_in_link", text="Copy link")
            _link(column, "Cancel", "loopcut.cancel_sign_in")
        elif status["status"] == "starting":
            column.label(text="Contacting Loopcut…", icon="TIME")
            _link(column, "Cancel", "loopcut.cancel_sign_in")
        else:
            _notes(column, ACCOUNT_LEAD)
            unlocks = column.column(align=True)
            unlocks.scale_y = 0.8
            for line in ACCOUNT_UNLOCKS if config.billing_open() else ACCOUNT_UNLOCKS_NO_SALES:
                unlocks.label(text=line, icon="CHECKMARK")
            column.separator()
            primary = column.column()
            primary.scale_y = 1.7
            primary.operator("loopcut.sign_in", text="Create free account", icon_value=account.mark_icon())
            if status["status"] == "error":
                _notes(column, ACCOUNT_FAILED)  # Never the raw HTTP error; the log has it.
                _step_button(column, "Continue", "connect")
        props = _link(column, "Skip for now", "loopcut.onboarding_step")
        props.step, props.save = "connect", False
    layout.separator(factor=2.0)


def _draw_sign_in(layout, context) -> None:
    """Model choices stay available whether the optional Loopcut account was skipped or failed."""
    from . import chatgpt_ui
    chatgpt = chatgpt_ui.status()
    column = _content(layout, "Connect your ChatGPT plan")
    if chatgpt["status"] == "waiting":
        _watch_popup(context)
        column.label(text="Finish signing in with ChatGPT in your browser", icon="URL")
        row = column.row(align=True)
        if chatgpt["url"]:
            row.operator("loopcut.chatgpt_copy_link", text="Copy link")
        row.operator("loopcut.chatgpt_cancel", text="Cancel")
        _notes(column, "Browser did not open? Copy the link and paste it into your browser.")
    elif _on_chatgpt_plan():
        column.label(text=f"Signed in with ChatGPT: {chatgpt_ui.account_line()}", icon="CHECKMARK")
        props = column.operator("loopcut.onboarding_step", text="Continue")
        props.step, props.save, props.provider = "privacy", True, "CHATGPT"
    else:
        if account.signed_in():
            column.label(text="Loopcut account connected", icon="CHECKMARK")
        _notes(column, "Step 2 of 2. " + CONNECT_LEAD)
        primary = column.column()
        primary.scale_y = 1.7
        primary.operator("loopcut.chatgpt_sign_in", text="Sign in with ChatGPT", icon="USER")
        _notes(column, "Recommended · Plus or Pro · your plan's usage limits apply")
    if chatgpt["status"] == "error" and chatgpt["error"]:
        _notes(column, chatgpt["error"][:160], "Try signing in again, or use your own API key.")
        _link(column, "Use another ChatGPT account", "loopcut.chatgpt_sign_in").new_account = True
    _link(column, "Use your own API key", "loopcut.onboarding_own_key").enable = True
    if account.signed_in():
        column.separator()
        props = column.operator("loopcut.onboarding_step", text="Use Loopcut's models")
        props.step, props.save, props.provider = "privacy", True, "LOOPCUT"
        _notes(column, "No ChatGPT plan? Fast is free; the Pro model comes with the Pro plan."
               if config.billing_open() else "No ChatGPT plan? Fast and Pro")
    else:
        props = _link(column, "No ChatGPT plan? Create a free Loopcut account", "loopcut.onboarding_step")
        props.step, props.save = "account", False
    column.separator()
    props = _link(column, "Skip for now", "loopcut.onboarding_step")
    props.step, props.save = "privacy", False
    layout.separator(factor=2.0)


def _on_signed_in() -> None:
    """A Loopcut account opens model choices; a connected ChatGPT plan opens privacy."""
    if _state["step"] == "account":
        _state["step"] = "connect"
    elif _state["step"] == "connect" and getattr(settings.preferences(), "provider", None) == "CHATGPT":
        _state["step"] = "privacy"


def _draw_privacy(layout) -> None:
    prefs = settings.preferences()
    provider = "OpenAI, on your ChatGPT plan" if prefs.provider == "CHATGPT" else settings.provider_label(prefs.provider)
    column = _content(layout, "Privacy and Control")
    if prefs.provider == "CHATGPT" and account.signed_in():  # Both connected: say what goes where.
        _notes(column,
               "Chat: your messages, a scene description and viewport captures go to OpenAI on your ChatGPT "
               "plan. Loopcut's server never sees the conversation.",
               "Cloud tools: only what a tool needs (a picture, a mesh, a line of text) goes to Loopcut, and "
               "each one shows its credit price. Your .blend files are never uploaded.")
    else:
        _notes(column,
               f"Your messages, a description of the scene and viewport captures go to {provider}. "
               "Your .blend files are never uploaded.",
               "Loopcut passes them to the model provider behind Fast and Pro and keeps only token counts "
               "for metering, never the content." if prefs.provider == "LOOPCUT"
               else settings.provider_note(prefs.provider))
    column.separator()
    column.prop(prefs, "auto_run")
    _notes(column, "Off: Loopcut asks before it runs code. Either way, every turn gets a checkpoint you can restore.")
    column.separator(factor=2)
    _step_button(column, "Continue", "done", save=True)
    _step_button(column, "Back", "connect")
    layout.separator(factor=2.0)


def _draw_done(layout) -> None:
    column = layout.column(align=True)
    if _state["imported"]:
        column.label(text="Imported your Blender {:d}.{:d} settings.".format(*_state["imported"]), icon="CHECKMARK")
    failed = _state["failed"]
    if failed:
        shown = ", ".join(failed[:4]) + (f" and {len(failed) - 4} more" if len(failed) > 4 else "")
        column.label(text=f"Did not load in this version: {shown}", icon="ERROR")
    shortcut = "Cmd+L" if sys.platform == "darwin" else "Ctrl+Alt+L"
    ready = (f"Loopcut is ready on {settings.route_label()}" if _connected()
             else "Sign in or use your own model key in the Loopcut panel to start")
    column.label(text=f"{ready}. {shortcut} jumps to the chat.", icon="NONE" if _state["imported"] else "CHECKMARK")
    nudge = done_nudge(_connected() and _on_chatgpt_plan(), account.signed_in(), _connected())
    if nudge:
        column.label(text=nudge, icon="INFO")
    layout.separator()
    layout.separator(type="LINE")
    layout.separator()


def done_nudge(chatgpt: bool, loopcut: bool, connected: bool) -> str:
    """The Done screen's one line about the half that is missing (spec §3); "" when both are connected."""
    if chatgpt and loopcut:
        return "ChatGPT runs the conversation; your Loopcut account runs assets and cloud tools."
    if chatgpt:
        return "Add a free Loopcut account any time for asset search and cloud tools: the panel's settings menu."
    if loopcut and connected:
        return "Have ChatGPT Plus or Pro? Sign in with ChatGPT in Preferences and the agent runs on your plan."
    return ""


def _failed_addons(context) -> list[str]:
    """Enabled in the imported preferences, but not running here (written for another version,
    or the module is missing)."""
    failed = []
    for name in context.preferences.addons.keys():
        module = sys.modules.get(name)
        if module is None or not getattr(module, "__addon_enabled__", False):
            failed.append(name.rpartition(".")[2])
    return sorted(failed)


def _disable_other_copies(context) -> None:
    """Someone who ran Loopcut as an extension in stock Blender imports it enabled; two copies
    would register the same operators. The bundled one stays."""
    import addon_utils
    others = [name for name in context.preferences.addons.keys()
              if name != settings.PACKAGE and name.rpartition(".")[2] == settings.PACKAGE.rpartition(".")[2]]
    for name in others:
        addon_utils.disable(name, default_set=True)
    if others:  # The copy that registered last owned the operators; put the bundled one back.
        addon_utils.disable(settings.PACKAGE)
        addon_utils.enable(settings.PACKAGE)


def _import_now() -> None:
    """From a timer, not the operator: rereading preferences re-registers add-ons, and an
    operator must not outlive its own class."""
    found = _source()
    window = bpy.context.window_manager.windows[0]
    if found is None:
        _state.update(step="setup", error="")
        return
    version, source = found
    try:
        blender_import.copy_settings(source, Path(bpy.utils.resource_path("USER")))
    except (OSError, shutil.Error) as ex:
        print(f"Loopcut: could not copy {source}: {ex}")
        _state["error"] = "Could not copy the settings. Try importing again, or choose Start Fresh."
        return
    _state.update(step="setup", imported=version, error="")  # draw_splash picks what follows.
    with bpy.context.temp_override(window=window):
        # The same follow-up as Blender's own "copy previous settings".
        bpy.ops.wm.read_userpref()
        bpy.ops.wm.read_history()
        bpy.ops.wm.operator_presets_cleanup()
        _disable_other_copies(bpy.context)
        _state["failed"] = _failed_addons(bpy.context)
        if bpy.data.is_saved is bpy.data.is_dirty is False:
            bpy.ops.wm.read_homefile()  # Their startup file. Closes the splash.
    bpy.app.timers.register(_reopen, first_interval=0.1)


def _reopen() -> None:
    from .ui import host
    window = bpy.context.window_manager.windows[0]
    if not host.has_panel(window):  # Their startup file has no Loopcut panel.
        try:
            host.dock(window)
        except RuntimeError as ex:
            print(f"Loopcut: {ex}")
    with bpy.context.temp_override(window=window):
        bpy.ops.wm.splash("INVOKE_DEFAULT")


class LOOPCUT_OT_import_blender_settings(bpy.types.Operator):
    """Copy preferences, add-ons, keymap, themes and the startup file from Blender. Blender's own settings are not changed"""
    bl_idname = "loopcut.import_blender_settings"
    bl_label = "Import Blender Settings"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context):
        # Only into a folder without preferences: never over someone's Loopcut settings.
        return not (Path(bpy.utils.resource_path("USER")) / "config" / "userpref.blend").is_file()

    def execute(self, context):
        bpy.app.timers.register(_import_now, first_interval=0.0)
        return {"FINISHED"}


class LOOPCUT_OT_onboarding_own_key(bpy.types.Operator):
    """Connect a provider of your own instead of a Loopcut account"""
    bl_idname = "loopcut.onboarding_own_key"
    bl_label = "Use Your Own Key"
    bl_options = {"INTERNAL"}

    enable: bpy.props.BoolProperty(default=True)

    def execute(self, context):
        if self.enable:
            from . import chatgpt_ui
            account.cancel_sign_in()
            chatgpt_ui.cancel_sign_in()
        _state["own_key"] = self.enable
        prefs = settings.preferences()
        if prefs is not None:
            if self.enable and prefs.provider in {"LOOPCUT", "CHATGPT"}:
                _state["left_chatgpt"] = prefs.provider == "CHATGPT"
                prefs.provider = "OPENAI"
            elif (not self.enable and _state["left_chatgpt"] and prefs.provider == "OPENAI"
                  and chatgpt_auth.plan_signed_in()):
                # Back without picking a provider: looking at the key form must not cost the ChatGPT plan.
                prefs.provider = "CHATGPT"
        if not self.enable:
            _state["left_chatgpt"] = False
        if context.region_popup is not None:
            context.region_popup.tag_refresh_ui()
        return {"FINISHED"}


class LOOPCUT_OT_onboarding_step(bpy.types.Operator):
    bl_idname = "loopcut.onboarding_step"
    bl_label = "Continue"
    bl_options = {"INTERNAL"}

    step: bpy.props.EnumProperty(items=[(s, s, "") for s in ("setup", "account", "connect", "privacy", "done")])
    save: bpy.props.BoolProperty()
    provider: bpy.props.EnumProperty(items=[(s, s, "") for s in ("CURRENT", "LOOPCUT", "CHATGPT")],
                                     default="CURRENT", options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, context):
        if _state["step"] == "account":
            account.cancel_sign_in()  # Skipping must not let a pending account change the model later.
        if _state["step"] == "connect":
            from . import chatgpt_ui
            chatgpt_ui.cancel_sign_in()
        prefs = settings.preferences()
        if self.provider != "CURRENT" and prefs is not None:
            prefs.provider = self.provider
        _state["step"] = self.step
        if self.save:
            bpy.ops.wm.save_userpref()
        if context.region_popup is not None:  # The splash stays open and only redraws when told to.
            context.region_popup.tag_refresh_ui()
        return {"FINISHED"}


_CLASSES = (LOOPCUT_OT_import_blender_settings, LOOPCUT_OT_onboarding_own_key, LOOPCUT_OT_onboarding_step)


def register() -> None:
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    account.on_signed_in.append(_on_signed_in)


def unregister() -> None:
    if bpy.app.timers.is_registered(_refresh_popups):
        bpy.app.timers.unregister(_refresh_popups)
    _popups.clear()
    if _on_signed_in in account.on_signed_in:
        account.on_signed_in.remove(_on_signed_in)
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
