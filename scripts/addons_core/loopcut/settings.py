"""Loopcut's page in Blender's preferences: provider, key, model and how much it may do unasked.

config.py reads these through values(); the API key itself is kept by credentials.py, not in
Blender's preferences file.
"""

import json
import os
import sys
import urllib.error
import urllib.request

import bpy

from . import account, chatgpt_auth, cloud_account, config, credentials

PACKAGE = __package__

# (id, label, base URL, default model, note shown under the key field)
PROVIDERS = (
    ("LOOPCUT", "Loopcut", account.BASE_URL, "fast",
     "Sign in with your Loopcut account. The Loopcut service picks the model behind Fast and Pro and meters "
     "usage per 5-hour session and per week."),
    ("META", "Meta", "https://api.meta.ai/v1", "muse-spark-1.3-contributor",
     "On Meta's contributor tier your prompts, scene descriptions and viewport captures may be "
     "used for training. Pick another tier or provider for confidential work."),
    ("OPENAI", "OpenAI", "https://api.openai.com/v1", "",
     "Chats are billed to this key's OpenAI account, not your ChatGPT plan or Loopcut credits."),
    ("OPENROUTER", "OpenRouter", "https://openrouter.ai/api/v1", "", ""),
    ("OLLAMA", "Ollama (this computer)", "http://127.0.0.1:11434/v1", "",
     "Model requests stay on this computer. The model needs tool calling and image input."),
    ("CUSTOM", "Custom", "", "", "Any endpoint that speaks the OpenAI chat completions API with tools."),
    # Last, so the stored enum values of the rows above stay what they were.
    ("CHATGPT", "ChatGPT plan", chatgpt_auth.CHATGPT_PLAN_BASE_URL, config.CHATGPT_PLAN_MODEL,
     "Sign in with ChatGPT: requests go from this computer to OpenAI and use your ChatGPT Plus or Pro plan's "
     "usage. Loopcut never sees your ChatGPT tokens; they stay in a private file on this computer. One agent "
     "does the whole job in a working folder of its own, as a coding agent would."),
    # Anthropic's Messages API, not chat completions (anthropic_messages.py). Data use: Anthropic's Commercial Terms
    # (anthropic.com/legal/commercial-terms, effective 2025-06-17: no training on customer content) and its API
    # retention article (privacy.claude.com, 2026-07-01: inputs and outputs deleted within 30 days), so class A
    # as cloud_account.DATA_LABELS has it, like OpenAI's API.
    ("ANTHROPIC", "Anthropic (Claude)", config.ANTHROPIC_BASE_URL, config.ANTHROPIC_MODEL,
     "Private (A): Anthropic does not train on API inputs or outputs and deletes them within 30 days. Requests "
     "go from this computer to Anthropic with your key; the context repeated on every step is cached, which "
     "makes it much cheaper."),
)
_BY_ID = {p[0]: p for p in PROVIDERS}
_models: list[str] = []  # Filled by "Fetch models"; not saved.
# (key name, label, key field, note, "Use this key instead of credits" checkbox). Tool vendors only: the
# model's own key (Anthropic, OpenAI chat) is the provider's API key above, which credits never replace.
VENDOR_ROWS = (
    ("OPENAI_API_KEY", "OpenAI", "openai_vendor_key", "Image generation and editing · Private (A)", "openai_key_first"),
    ("FAL_KEY", "fal", "fal_vendor_key", "Image to 3D · Private (A); retopology · Longer retention (B)", "fal_key_first"),
    ("GEMINI_API_KEY", "Google Gemini", "gemini_vendor_key",
     "Voice lines · Paid API only, kept 55 days (B); free tier may train (D)", "gemini_key_first"),
    ("ELEVENLABS_API_KEY", "ElevenLabs", "elevenlabs_vendor_key",
     "Sound effects · May train (C); disable training and SFX sharing in your vendor account", "elevenlabs_key_first"),
)


def _vendor_callbacks(name):
    def get(self):
        return credentials.vendor_key(name)

    def set(self, value):
        try:
            credentials.set_vendor_key(name, value)
        except OSError as ex:
            print(f"Loopcut: could not save the vendor key: {ex}")
    return get, set


_get_openai_key, _set_openai_key = _vendor_callbacks("OPENAI_API_KEY")
_get_fal_key, _set_fal_key = _vendor_callbacks("FAL_KEY")
_get_gemini_key, _set_gemini_key = _vendor_callbacks("GEMINI_API_KEY")
_get_elevenlabs_key, _set_elevenlabs_key = _vendor_callbacks("ELEVENLABS_API_KEY")


def _key_first_changed(self, context) -> None:
    from . import cloud
    cloud.sync_settings()  # config.service_backends: the tools' routes change with the checkbox.
    _changed()


SPENDING_NOTE = ("Paid steps at or under \"Ask above\" run on their own; bigger ones ask first. Once a chat has "
                 "spent its budget without asking, the next paid step asks before going on. Own keys ask once per "
                 "chat for each vendor. Workers spend from the same chat budget.")


def draw_spending(layout, prefs) -> None:
    """Credits (C47): when a paid step asks first. One setting a row, so it reads in a narrow window."""
    box = layout.box()
    box.label(text="Spending", icon="FUND")
    column = box.column()
    column.prop(prefs, "ask_above_credits")
    column.prop(prefs, "chat_budget_credits")
    flags = box.column()
    flags.use_property_split = False
    flags.prop(prefs, "ask_every_paid_call")
    info = box.column(align=True)
    info.scale_y = 0.85
    for n, line in enumerate(wrap(SPENDING_NOTE, 70)):
        info.label(text=line, icon="INFO" if n == 0 else "BLANK1")
    from . import cloud
    runtime = cloud._runtime
    today = ""
    if runtime is not None:
        with runtime.lock:
            today = cloud_account.today_label(runtime.manifest, runtime.signed_in)
    row = box.row(align=True)
    if today:
        row.label(text=today)
    row.operator("wm.url_open", text="Set a daily limit", icon="URL").url = config.credits_url("daily_limit")


def draw_vendor_keys(layout, prefs):
    """PASSWORD/SKIP_SAVE fields use the same credentials callbacks as the endpoint key. A key's
    "Use this key instead of credits" box shows only once that key is set (here or in the environment)."""
    box = layout.box()
    box.label(text="Your own keys", icon="KEYINGSET")
    keys_set = credentials.vendor_keys_set()
    for key, label, prop, note, first in VENDOR_ROWS:
        row = box.row(align=True)
        row.prop(prefs, prop, text=label)
        row.operator("loopcut.clear_vendor_key", text="Clear").key_name = key
        if keys_set.get(key):
            box.prop(prefs, first)
            # Who pays, right under the choice (F0286): the box is on by default, so say it plainly.
            billed = box.column(align=True)
            billed.scale_y = 0.85
            billed.label(text=f"Charges go to your {label} account, not your Loopcut credits." if getattr(prefs, first, True)
                         else "Loopcut credits first, then this key.")
        info = box.column(align=True)
        info.scale_y = 0.85
        info.label(text=note, icon="INFO")
        if os.environ.get(key, "").strip():
            info.label(text="Environment key takes precedence; Clear removes only the saved key.", icon="INFO")
    flags = box.column()
    flags.use_property_split = False
    from . import cloud
    flags.label(text="Account defaults" if cloud._runtime and cloud._runtime.signed_in else "Local defaults")
    flags.prop(prefs, "no_generation")
    flags.prop(prefs, "private_mode")
    flags.prop(prefs, "project_no_generation")
    flags.label(text="Project Off cannot lift account No generation.")
    status = cloud_account.panel_data(cloud._runtime)["message"]
    if status:
        flags.label(text=status, icon="INFO")
    for tool in cloud_account.tool_rows(cloud._runtime):
        box.label(text=tool["title"] + " · " + tool["label"])
        if tool["reason"]:
            box.label(text=tool["reason"], icon="INFO")


def _account_setting(name):
    local_name = "local_" + name

    def get(self):
        from . import cloud
        return cloud_account.setting_value(cloud._runtime, name, getattr(self, local_name))

    def set(self, value):
        from . import cloud
        runtime = cloud._snapshot()
        if runtime.signed_in:
            cloud_account.change_setting(runtime, name, value)
        else:
            setattr(self, local_name, value)
            cloud.sync_settings()
        _changed()
    return get, set


_get_no_generation, _set_no_generation = _account_setting("no_generation")
_get_private_mode, _set_private_mode = _account_setting("private_mode")


def _get_project_no_generation(self):
    return cloud_account.OVERRIDES.index(cloud_account.project_override(getattr(bpy.context, "scene", None)))


def _set_project_no_generation(self, value):
    cloud_account.set_project_override(bpy.context.scene, cloud_account.OVERRIDES[value])
    from . import cloud
    cloud.sync_settings()
    _changed()


def _changed(self=None, context=None) -> None:
    from .ui import host
    host.config_changed()
    from . import chatgpt_ui
    chatgpt_ui.request_readiness()


def config_changed() -> None:
    """For other modules that change the connection (account.py after a sign-in)."""
    _changed()


def _provider_changed(self, context) -> None:
    if self.provider != "LOOPCUT":
        from . import state
        state.clear_allowance_notice(invalidate=True)
    _, _, base_url, model, _ = _BY_ID[self.provider]
    if self.provider == "LOOPCUT":
        self.base_url, self.model = base_url, self.tier
    elif self.provider != "CUSTOM":
        self.base_url, self.model = base_url, model
    _models.clear()
    _changed()


def listed_models(provider: str = "") -> list[str]:
    """The model menu: what "Fetch models" found, else Anthropic's current models on that provider."""
    if _models:
        return list(_models)
    if not provider:
        prefs = preferences()
        provider = prefs.provider if prefs is not None else ""
    if provider == "ANTHROPIC":
        from . import anthropic_messages
        return list(anthropic_messages.MODELS)
    return []


def draw_key(layout, prefs) -> None:
    """The API key field; on Anthropic, with a button to the console page that hands keys out."""
    if prefs.provider == "ANTHROPIC":
        row = layout.row(align=True)
        row.prop(prefs, "api_key")
        row.operator("loopcut.anthropic_get_key", text="", icon="URL")
    else:
        layout.prop(prefs, "api_key")


def _get_key(self) -> str:
    return credentials.api_key(self.base_url)


def _tier_changed(self, context) -> None:
    if self.provider == "LOOPCUT":
        self.model = self.tier
    _changed()


def _get_polypizza_key(self) -> str:
    from . import polypizza
    return credentials.api_key(polypizza.KEY_ID)


def _set_polypizza_key(self, value: str) -> None:
    from . import polypizza
    try:
        credentials.store(polypizza.KEY_ID, value.strip())
    except OSError as ex:
        print(f"Loopcut: could not save the Poly Pizza key: {ex}")


def _get_blenderkit_key(self) -> str:
    from . import blenderkit
    return credentials.api_key(blenderkit.KEY_ID)


def _set_blenderkit_key(self, value: str) -> None:
    from . import blenderkit
    try:
        credentials.store(blenderkit.KEY_ID, value.strip())
    except OSError as ex:
        print(f"Loopcut: could not save the BlenderKit key: {ex}")


def _set_key(self, value: str) -> None:
    if not self.base_url:
        return  # No endpoint to file it under; the page says to fill that in first.
    try:
        credentials.store(self.base_url, value.strip())
    except OSError as ex:
        print(f"Loopcut: could not save the API key: {ex}")
    _changed()


class LoopcutPreferences(bpy.types.AddonPreferences):
    bl_idname = PACKAGE

    first_message_sent: bpy.props.BoolProperty(default=False, options={"HIDDEN"})
    # shaders.default_to_subprocess ran: "subprocess" (it switched Thread to Subprocess) or "kept"
    # (the user had chosen Subprocess). Set once, so a later switch back to Thread stays.
    shader_compilation_once: bpy.props.StringProperty(default="", options={"HIDDEN"})

    provider: bpy.props.EnumProperty(
        name="Provider", items=[(p[0], p[1], p[2] or "Your own endpoint") for p in PROVIDERS],
        default="LOOPCUT", update=_provider_changed)
    tier: bpy.props.StringProperty(
        name="Model", default="fast", update=_tier_changed,
        description="Which Loopcut model answers; the menu says how much usage each takes against the default")
    base_url: bpy.props.StringProperty(
        name="Base URL", default=PROVIDERS[0][2], update=_changed,
        description="OpenAI-compatible endpoint. Must be https, or http on 127.0.0.1")
    api_key: bpy.props.StringProperty(
        name="API Key", subtype="PASSWORD", options={"SKIP_SAVE"}, get=_get_key, set=_set_key,
        description="Stored in Loopcut's own file in your Blender config folder, not in preferences")
    model: bpy.props.StringProperty(name="Model", default=PROVIDERS[0][3], update=_changed,
                                   description="Type your provider's model name, or use refresh and pick from the menu")
    reasoning_effort: bpy.props.EnumProperty(
        name="Reasoning", items=[("low", "Low", "Fastest"), ("medium", "Medium", ""), ("high", "High", "Slowest")],
        default="medium", update=_changed)
    # The ChatGPT plan's own (config.CHATGPT_PLAN_EFFORTS): high by default, as in the evals, and xhigh too.
    chatgpt_effort: bpy.props.EnumProperty(
        name="Reasoning", items=[("low", "Low", "Fastest"), ("medium", "Medium", ""), ("high", "High", ""),
                                 ("xhigh", "Extra High", "Slowest; the most careful work")],
        default=config.CHATGPT_PLAN_EFFORT, update=_changed)
    auto_run: bpy.props.BoolProperty(
        name="Run without asking", default=False, update=_changed,
        description="Run code and file changes without the Run / Reject step. Renders and bakes still ask, and "
                    "paid steps follow Spending. Every turn still gets a checkpoint you can restore")
    # Spending (spending.py, C47): when a paid step asks first.
    ask_above_credits: bpy.props.IntProperty(
        name="Ask above", default=25, min=0, max=100_000, update=_changed,
        description="Credits a single paid step may cost before it asks you first (the service may ask lower). "
                    "0 asks for every paid step")
    chat_budget_credits: bpy.props.IntProperty(
        name="Chat budget", default=150, min=1, max=100_000, update=_changed,
        description="Credits one chat may spend without asking, workers included. Then the next paid step asks "
                    "whether to allow this much more")
    ask_every_paid_call: bpy.props.BoolProperty(
        name="Ask before every paid call", default=False, update=_changed,
        description="Every step that costs credits, or uses your own vendor key, asks first")
    auto_look: bpy.props.BoolProperty(
        name="Look after every change", default=True, update=_changed,
        description="After each step that changes the scene, the agent gets a picture of the result. "
                    "One image per change; off, it only sees what it asks to see")
    orchestrate: bpy.props.BoolProperty(
        name="Use workers for big jobs", default=True, update=_changed,
        description="For a request with several parts, Loopcut briefs workers that build each part in a "
                    "background copy of your scene, then merges their results in one undo step. Off, one "
                    "agent does everything itself")
    context_budget: bpy.props.IntProperty(
        name="Context budget", default=config.DEFAULT_CONTEXT_BUDGET,
        min=config.MIN_CONTEXT_BUDGET, max=config.MAX_CONTEXT_BUDGET, step=1000, update=_changed,
        description="Tokens each request may carry, prompt included. Earlier turns are always sent as "
                    "short step records; over this, the current turn's older steps are too, and then the "
                    "earlier conversation is summarized. Lower is cheaper; higher remembers more")
    run_timeout: bpy.props.IntProperty(
        name="Stop code after (s)", default=60, min=1, max=3600, update=_changed,
        description="A step that runs longer is stopped, so an endless loop cannot freeze Blender")
    checkpoint_budget_mb: bpy.props.IntProperty(
        name="Checkpoints (MB)", default=2048, min=1, max=1_000_000, update=_changed,
        description="Checkpoint storage per conversation. The oldest checkpoints expire first")
    dock_on_startup: bpy.props.BoolProperty(
        name="Open the panel at startup", default=True,
        description="Dock Loopcut at the right of the 3D viewport when Blender starts without it")
    blenderkit_key: bpy.props.StringProperty(
        name="BlenderKit API key", subtype="PASSWORD", options={"SKIP_SAVE"},
        get=_get_blenderkit_key, set=_set_blenderkit_key,
        description="Only for Full or Business plan assets; free BlenderKit assets need no key. Read from the "
                    "BlenderKit add-on when it is installed. Stored in Loopcut's credentials file")
    polypizza_key: bpy.props.StringProperty(
        name="Poly Pizza API key", subtype="PASSWORD", options={"SKIP_SAVE"},
        get=_get_polypizza_key, set=_set_polypizza_key,
        description="Free key from poly.pizza (account > API) for low-poly models. Stored in Loopcut's "
                    "credentials file. Poly Haven and your Blender asset libraries need no key")
    share_conversations: bpy.props.BoolProperty(
        name="Share conversations to improve Loopcut", default=True,
        description="After each turn, send Loopcut a text log of the conversation (your messages, the replies, the "
                    "tool calls and their results) and the add-on's console lines, with a random install id. Kept "
                    "90 days to fix problems and improve Loopcut, never used for training. Never images, .blend files "
                    "or keys. Details: loopcut.org/privacy")
    share_usage_stats: bpy.props.BoolProperty(
        name="Share anonymous usage stats", default=True,
        description="Send Loopcut short events about launches, the setup steps and how each turn ended: versions, "
                    "OS, the model's name, timings and error codes, with a random install id. Never prompts, "
                    "replies, file or object names, or anything from the scene. Details: loopcut.org/privacy")
    auto_update: bpy.props.BoolProperty(
        name="Download updates automatically", default=True,
        description="Look for a newer Loopcut now and then and download it in the background. "
                    "Updates install when you close Blender. A pending update can restart a fresh launch; "
                    "choose Not now to keep working")
    openai_vendor_key: bpy.props.StringProperty(
        name="OpenAI", subtype="PASSWORD", options={"SKIP_SAVE"}, get=_get_openai_key, set=_set_openai_key,
        description="Set or replace your Images API key; environment OPENAI_API_KEY takes precedence")
    fal_vendor_key: bpy.props.StringProperty(
        name="fal", subtype="PASSWORD", options={"SKIP_SAVE"}, get=_get_fal_key, set=_set_fal_key,
        description="Set or replace your fal key; environment FAL_KEY takes precedence")
    gemini_vendor_key: bpy.props.StringProperty(
        name="Google Gemini", subtype="PASSWORD", options={"SKIP_SAVE"}, get=_get_gemini_key, set=_set_gemini_key,
        description="Paid Gemini API key for voice lines; environment GEMINI_API_KEY takes precedence")
    elevenlabs_vendor_key: bpy.props.StringProperty(
        name="ElevenLabs", subtype="PASSWORD", options={"SKIP_SAVE"}, get=_get_elevenlabs_key, set=_set_elevenlabs_key,
        description="Set or replace your SFX key; environment ELEVENLABS_API_KEY takes precedence")
    # "Use this key instead of credits", per vendor key (config.service_backends). On by default.
    openai_key_first: bpy.props.BoolProperty(
        name="Use this key instead of credits", default=True, update=_key_first_changed,
        description="On: OpenAI tools use this key even with Loopcut credits. Off: credits first, then this key")
    fal_key_first: bpy.props.BoolProperty(
        name="Use this key instead of credits", default=True, update=_key_first_changed,
        description="On: fal tools use this key even with Loopcut credits. Off: credits first, then this key")
    gemini_key_first: bpy.props.BoolProperty(
        name="Use this key instead of credits", default=True, update=_key_first_changed,
        description="On: Gemini tools use this key even with Loopcut credits. Off: credits first, then this key")
    elevenlabs_key_first: bpy.props.BoolProperty(
        name="Use this key instead of credits", default=True, update=_key_first_changed,
        description="On: ElevenLabs tools use this key even with Loopcut credits. Off: credits first, then this key")
    # AddonPreferences has no IDProperty mapping; saved RNA fields back local callbacks.
    local_no_generation: bpy.props.BoolProperty(default=cloud_account.DEFAULTS["no_generation"], options={"HIDDEN"})
    local_private_mode: bpy.props.BoolProperty(default=cloud_account.DEFAULTS["private_mode"], options={"HIDDEN"})
    no_generation: bpy.props.BoolProperty(
        name="No generation", default=False, get=_get_no_generation, set=_set_no_generation,
        description="Hide and refuse generative service tools. Retopology remains available")
    private_mode: bpy.props.BoolProperty(
        name="Private mode", default=True, get=_get_private_mode, set=_set_private_mode,
        description="Hide and refuse service vendors whose data class is C or D")
    project_no_generation: bpy.props.EnumProperty(
        name="Project no generation", get=_get_project_no_generation, set=_set_project_no_generation,
        items=[("inherit", "Inherit", "Use the account or local default", 0),
               ("on", "On", "Refuse generation in this scene", 1),
               ("off", "Off", "Account restrictions still apply", 2)],
        description="Saved in this scene's .blend file")

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        layout.label(text=f"Loopcut {config.version_label()}")

        box = layout.box()
        box.label(text="Connection", icon="WORLD")
        column = box.column()
        column.prop(self, "provider")
        if self.provider == "LOOPCUT":
            account.draw(column.column(align=True))
            column.separator()
            row = column.row(align=True)
            row.label(text="Model")
            row.menu("LOOPCUT_MT_tiers", text=account.model_label(self.tier), icon="DOWNARROW_HLT")
        elif self.provider == "CHATGPT":
            from . import chatgpt_ui
            chatgpt_ui.draw(column.column(align=True), self)
        else:
            if self.provider == "CUSTOM":
                column.prop(self, "base_url")
            if not self.base_url.startswith("http://127.0.0.1"):
                draw_key(column, self)
            row = column.row(align=True)
            row.prop(self, "model")
            row.operator("loopcut.fetch_models", text="", icon="FILE_REFRESH")
            if listed_models(self.provider):
                row.menu("LOOPCUT_MT_models", text="", icon="DOWNARROW_HLT")
        note = provider_note(self.provider)
        if note:
            lines = wrap(note, 90)
            info = box.column(align=True)
            info.scale_y = 0.85
            for n, line in enumerate(lines):
                info.label(text=line, icon="INFO" if n == 0 else "BLANK1")

        draw_connections(layout, self)

        draw_vendor_keys(layout, self)

        draw_spending(layout, self)

        split = layout.split(factor=0.5)
        agent = split.box()
        agent.label(text="Agent", icon="PLAY")
        chatgpt = self.provider == "CHATGPT"
        agent.column().prop(self, "chatgpt_effort" if chatgpt else "reasoning_effort")
        flags = agent.column()
        flags.use_property_split = False  # A checkbox reads best with its whole label beside it.
        flags.prop(self, "auto_run")
        flags.prop(self, "auto_look")
        if not chatgpt:  # On the ChatGPT plan one agent does the whole job (config.load_roles).
            row = flags.row()
            row.enabled = self.provider == "LOOPCUT"
            row.prop(self, "orchestrate")
            if self.provider != "LOOPCUT":
                flags.label(text="Workers and routing run on Loopcut plans" if config.billing_open() else
                            "Workers and routing use the Loopcut service")
        flags.prop(self, "dock_on_startup")
        limits = split.box()
        limits.label(text="Limits", icon="TIME")
        column = limits.column()
        column.prop(self, "context_budget")
        column.prop(self, "run_timeout")
        column.prop(self, "checkpoint_budget_mb")

        box = layout.box()
        box.label(text="Asset Libraries", icon="ASSET_MANAGER")
        column = box.column()
        row = column.row(align=True)
        row.prop(self, "blenderkit_key")
        if config.billing_open():
            row.operator("loopcut.blenderkit_get_key", text="", icon="URL")
        row = column.row(align=True)
        row.prop(self, "polypizza_key")
        row.operator("loopcut.polypizza_get_key", text="", icon="URL")
        info = column.column(align=True)
        info.scale_y = 0.85
        info.label(text="BlenderKit's free assets, Poly Haven and the asset libraries in Preferences > File Paths", icon="INFO")
        info.label(text=("need no key. A BlenderKit plan key adds its paid assets. Poly Pizza's key is free; its"
                         if config.billing_open() else "need no key. Poly Pizza's key is free; its"), icon="BLANK1")
        info.label(text="terms ask for attribution and allow no commercial use by anyone earning over $50,000 a year.", icon="BLANK1")

        from .ui import host
        if host.NATIVE:  # As an extension in stock Blender, updates come through Blender's own extension system.
            from . import update
            box = layout.box()
            box.label(text="Updates", icon="FILE_REFRESH")
            column = box.column()
            column.use_property_split = False
            column.prop(self, "auto_update")
            row = column.row(align=True)
            row.label(text=update.preferences_line())
            update_state = update.state()
            status = update_state["status"]
            if sys.platform == "win32" and status == "ready" and not update_state["legacy_fallback"]:
                column.label(text="Windows will ask to allow the update after Blender closes.")
            if status == "ready":
                row.operator("loopcut.restart_to_update")
            elif status not in ("checking", "downloading", "preparing", "installing", "applying"):
                row.operator("loopcut.check_for_updates", text="Check Now")

        draw_privacy(layout, self)


PRIVACY_NOTE = ("Conversation logs are text only: no images, .blend files or keys. Loopcut keeps them 90 days to fix "
                "problems and improve the app, and never uses them for training.")


def draw_privacy(layout, prefs) -> None:
    """Privacy and Support on the preferences page: the two sharing switches (session_logs.py, telemetry.py; the
    privacy policy points here) and Report a Problem, which lives here and never in the chat panel."""
    from . import session_logs
    box = layout.box()
    box.label(text="Privacy and Support", icon="LOCKED")
    column = box.column()
    column.use_property_split = False
    column.prop(prefs, "share_conversations")
    column.prop(prefs, "share_usage_stats")
    notes = column.column(align=True)
    notes.scale_y = 0.75
    notes.active = False
    for line in wrap(PRIVACY_NOTE, 90):
        notes.label(text=line)
    row = column.row()
    row.operator("loopcut.report_problem", text="Report a Problem…", icon="HELP")
    line = session_logs.report_line()
    if line:
        icon = "ERROR" if session_logs.report_state()["status"] == "failed" else "INFO"
        for index, part in enumerate(wrap(line, 90)):
            column.label(text=part, icon=icon if index == 0 else "BLANK1")


def wrap(text: str, width: int) -> list[str]:
    if not text:
        return []
    lines, line = [], ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    return lines + [line]


class LOOPCUT_OT_fetch_models(bpy.types.Operator):
    """Ask the provider which models this key can use"""
    bl_idname = "loopcut.fetch_models"
    bl_label = "Fetch Models"

    def execute(self, context):
        prefs = preferences()
        if prefs is not None and prefs.provider == "CHATGPT":
            try:
                found = [m["slug"] for m in chatgpt_auth.list_models()]
            except chatgpt_auth.ChatGPTAuthError as ex:
                self.report({"ERROR"}, f"Could not list ChatGPT models: {ex}. Sign in again, or use the default model.")
                return {"CANCELLED"}
            if not found:
                self.report({"WARNING"}, "ChatGPT listed no models for this account; type the model name instead")
                return {"CANCELLED"}
            _models[:] = found[:200]
            self.report({"INFO"}, f"{len(found)} models; pick one from the menu next to the field")
            return {"FINISHED"}
        if prefs is None or not prefs.base_url:
            self.report({"ERROR"}, "Set the base URL first")
            return {"CANCELLED"}
        if config.anthropic(prefs.base_url):
            return self._anthropic(prefs)
        try:
            config._check_base_url(prefs.base_url, "Base URL")
            request = urllib.request.Request(
                f"{prefs.base_url.rstrip('/')}/models",
                headers={"Authorization": f"Bearer {credentials.api_key(prefs.base_url)}", "Accept": "application/json"})
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read(4_000_000))
        except urllib.error.HTTPError as ex:
            self.report({"ERROR"}, f"The provider answered HTTP {ex.code}; check the key")
            return {"CANCELLED"}
        except (config.ConfigError, urllib.error.URLError, ValueError, TimeoutError) as ex:
            self.report({"ERROR"}, f"Could not list models: {ex}. Check the URL and key, then retry or type a model name.")
            return {"CANCELLED"}
        rows = payload.get("data") if isinstance(payload, dict) else None
        found = sorted({str(r["id"]) for r in rows or [] if isinstance(r, dict) and r.get("id")})
        if not found:
            self.report({"WARNING"}, "The provider listed no models; type the model name instead")
            return {"CANCELLED"}
        _models[:] = found[:200]
        self.report({"INFO"}, f"{len(found)} models; pick one from the menu next to the field")
        return {"FINISHED"}

    def _anthropic(self, prefs):
        from . import anthropic_messages
        try:
            found = anthropic_messages.list_models(prefs.base_url, credentials.api_key(prefs.base_url))
        except urllib.error.HTTPError as ex:
            self.report({"ERROR"}, f"Anthropic answered HTTP {ex.code}; check the key")
            return {"CANCELLED"}
        except (urllib.error.URLError, ValueError, TimeoutError) as ex:
            self.report({"ERROR"}, f"Could not list Anthropic's models: {ex}. Retry, or pick one from the menu.")
            return {"CANCELLED"}
        if not found:
            self.report({"WARNING"}, "Anthropic listed no models for this key; pick one from the menu instead")
            return {"CANCELLED"}
        _models[:] = found[:200]  # Newest first, as Anthropic lists them.
        self.report({"INFO"}, f"{len(found)} models; pick one from the menu next to the field")
        return {"FINISHED"}


class LOOPCUT_OT_anthropic_get_key(bpy.types.Operator):
    """Open Anthropic's console in the browser: create a key under API keys, paste it here"""
    bl_idname = "loopcut.anthropic_get_key"
    bl_label = "Get an Anthropic API key"

    def execute(self, context):
        from . import anthropic_messages
        account.open_browser(anthropic_messages.KEYS_URL)
        self.report({"INFO"}, "Anthropic's API keys page is open in your browser")
        return {"FINISHED"}


BOTH_NOTE = ("Best setup: both. Your ChatGPT plan runs the conversation; your Loopcut account adds asset search "
             "and cloud tools. Loopcut credits never pay for ChatGPT.")


def draw_connections(layout, prefs) -> None:
    """Both connections, whichever is the model (M9). Connection above draws the chosen provider in full; this
    box shows the other one, so signing in to one never hides the other."""
    box = layout.box()
    box.label(text="Your connections", icon="LINKED")
    info = box.column(align=True)
    info.scale_y = 0.85
    for n, line in enumerate(wrap(BOTH_NOTE, 90)):
        info.label(text=line, icon="INFO" if n == 0 else "BLANK1")
    if prefs.provider != "CHATGPT":
        column = box.column(align=True)
        column.label(text="ChatGPT plan", icon="USER")
        info = chatgpt_auth.status()
        if info.get("signed_in") and info.get("plan_usage"):
            row = column.row(align=True)
            row.label(text=f"{info.get('email') or 'Signed in'} · not the model now", icon="CHECKMARK")
            row.operator("loopcut.use_provider", text="Use ChatGPT plan").provider = "CHATGPT"
        else:
            column.label(text="ChatGPT Plus or Pro runs the agent, with no charge from Loopcut.")
            column.operator("loopcut.chatgpt_sign_in", text="Sign in with ChatGPT")
    if prefs.provider != "LOOPCUT":
        column = box.column(align=True)
        column.label(text="Loopcut account", icon_value=account.mark_icon())
        account.draw(column)


class LOOPCUT_OT_use_provider(bpy.types.Operator):
    """Make this connection the model"""
    bl_idname = "loopcut.use_provider"
    bl_label = "Use"
    bl_options = {"INTERNAL"}

    provider: bpy.props.StringProperty()

    def execute(self, context):
        prefs = preferences()
        if prefs is not None and self.provider in _BY_ID:
            prefs.provider = self.provider
        return {"FINISHED"}


class LOOPCUT_OT_pick_model(bpy.types.Operator):
    bl_idname = "loopcut.pick_model"
    bl_label = "Use Model"
    bl_options = {"INTERNAL"}

    model: bpy.props.StringProperty()

    def execute(self, context):
        prefs = preferences()
        if prefs is not None:
            prefs.model = self.model
        return {"FINISHED"}


class LOOPCUT_MT_models(bpy.types.Menu):
    bl_label = "Models"

    def draw(self, context):
        for name in listed_models():
            self.layout.operator("loopcut.pick_model", text=name).model = name


class LOOPCUT_OT_pick_tier(bpy.types.Operator):
    bl_idname = "loopcut.pick_tier"
    bl_label = "Use Model"
    bl_options = {"INTERNAL"}

    tier: bpy.props.StringProperty()

    def execute(self, context):
        set_tier(self.tier)
        return {"FINISHED"}


class LOOPCUT_MT_tiers(bpy.types.Menu):
    """The models the Loopcut service offers, as /v1/models lists them."""
    bl_label = "Model"

    def draw(self, context):
        for m in account.models():
            about = "  ·  ".join(p for p in (m["blurb"], m["note"]) if p)
            text = m["label"] + (f"   —   {about}" if about else "")
            self.layout.operator("loopcut.pick_tier", text=text).tier = m["id"]


def provider_label(provider: str) -> str:
    return _BY_ID[provider][1]


LOOPCUT_PLAN_NOTE = ("Sign in with your Loopcut account. Fast is free with a daily allowance; the Pro model comes "
                     "with the Pro plan. The Loopcut service picks the model behind each and meters usage per "
                     "5-hour session and per week.")


def provider_note(provider: str) -> str:
    if provider == "LOOPCUT" and config.billing_open():
        return LOOPCUT_PLAN_NOTE  # Plan words only while the server sells plans.
    return _BY_ID[provider][4]


def has_models() -> bool:
    return bool(listed_models())


def preferences():
    """None when Loopcut runs from a dev checkout without being enabled as an add-on."""
    addon = bpy.context.preferences.addons.get(PACKAGE)
    return addon.preferences if addon else None


def model_options() -> list[tuple[str, str, str, bool]]:
    """(id, label, note, current) for the panel's model button. On the Loopcut provider the
    choice is the tier; elsewhere it is among the models "Fetch models" found, plus the current one."""
    prefs = preferences()
    if prefs is None:
        try:
            current = config.load().model
        except config.ConfigError:
            return []
        return [(current, current, "from .env", True)]
    if prefs.provider == "LOOPCUT":
        return [(m["id"], m["label"], m["note"], prefs.tier == m["id"]) for m in account.models()]
    names = listed_models(prefs.provider)
    if prefs.model and prefs.model not in names:
        names.insert(0, prefs.model)
    return [(name, name, "", name == prefs.model) for name in names]


def choose_model(model_id: str) -> None:
    """The panel's model button picked one; see model_options."""
    prefs = preferences()
    if prefs is None or not model_id:
        return
    if prefs.provider == "LOOPCUT":
        set_tier(model_id)
    else:
        prefs.model = model_id


def model_label(model_id: str) -> str:
    """What the panel's model button shows for the configured model."""
    prefs = preferences()
    if prefs is not None and prefs.provider == "LOOPCUT":  # "Loopcut Fast", as the site and the setup say.
        label = account.model_label(model_id)
        return label if label.startswith("Loopcut") else f"Loopcut {label}"
    if on_chatgpt_plan():  # The route shows too: signed in with ChatGPT.
        return f"ChatGPT · {model_id}"
    return model_id


def on_chatgpt_plan() -> bool:
    """Whether Loopcut works on the user's ChatGPT plan: the provider chosen here, or, with no preferences (a dev
    checkout), the environment's base URL. Cheap enough to ask on every redraw."""
    prefs = preferences()
    if prefs is not None:
        return prefs.provider == "CHATGPT"
    return config.chatgpt_plan(os.environ.get("LOOPCUT_BASE_URL", ""))


def route_label() -> str:
    """How Loopcut reaches its model, in the user's words (onboarding)."""
    prefs = preferences()
    if on_chatgpt_plan():
        return "your ChatGPT plan"
    if prefs is not None and prefs.provider == "LOOPCUT":
        return "your Loopcut account"
    return provider_label(prefs.provider) if prefs is not None else "your own provider"


def set_tier(tier: str) -> None:
    """Pick a model of the Loopcut service by alias, as the model menu would."""
    prefs = preferences()
    if prefs is not None and prefs.provider == "LOOPCUT":
        model = next((m for m in account.models() if m["id"] == tier), None)
        if model is None:
            return
        if not model["available"]:
            from . import state
            unlock = model.get("unlock_plan") or {}
            plan = str(unlock.get("name") or unlock.get("id") or "Pro")
            text = f"{model['label']} needs the {plan} plan."
            billing = config.billing_open()
            url = config.pricing_url("model_not_in_plan", str(unlock.get("id") or "pro")) if billing else ""
            session = state.session()
            notice = state.item_notice(text, f"Upgrade to {plan}" if billing else "", url=url)
            if billing:
                notice["plan_upgrade"] = (state.ui.get("account") or {}).get("plan", "free")
            session["items"].append(notice)
            session["scroll"] = 0.0
            _changed()
            return
        prefs.tier = tier  # _tier_changed copies it to the model and reloads the config.


def values() -> dict[str, str]:
    """Preferences as the LOOPCUT_* settings config.py understands. The connection is only
    reported once it is usable (a key is stored, or the endpoint is local); until then a dev
    checkout's .env keeps working untouched."""
    prefs = preferences()
    if prefs is None:
        return {}
    found = {
        "LOOPCUT_REASONING_EFFORT": prefs.chatgpt_effort if prefs.provider == "CHATGPT" else prefs.reasoning_effort,
        "LOOPCUT_AUTO_RUN": "true" if prefs.auto_run else "false",
        "LOOPCUT_AUTO_LOOK": "true" if prefs.auto_look else "false",
        "LOOPCUT_ORCHESTRATE": "true" if prefs.orchestrate else "false",
        "LOOPCUT_CONTEXT_BUDGET": str(prefs.context_budget),
        "LOOPCUT_RUN_TIMEOUT": str(prefs.run_timeout),
        "LOOPCUT_CHECKPOINT_BUDGET_MB": str(prefs.checkpoint_budget_mb),
        "LOOPCUT_NO_GENERATION": "true" if prefs.no_generation else "false",
        "LOOPCUT_PRIVATE_MODE": "true" if prefs.private_mode else "false",
        "LOOPCUT_ASK_ABOVE_CREDITS": str(getattr(prefs, "ask_above_credits", 25)),
        "LOOPCUT_CHAT_BUDGET_CREDITS": str(getattr(prefs, "chat_budget_credits", 150)),
        "LOOPCUT_ASK_EVERY_PAID_CALL": "true" if getattr(prefs, "ask_every_paid_call", False) else "false",
    }
    for key, _, _, _, first in VENDOR_ROWS:
        found[config.OWN_KEY_FIRST + key] = "true" if getattr(prefs, first, True) else "false"
    if callable(getattr(prefs, "is_property_set", None)) and not prefs.is_property_set("local_no_generation"):
        # Until the user sets a project/local override, the manifest's account default applies.
        found.pop("LOOPCUT_NO_GENERATION")
    if prefs.provider == "CHATGPT":  # No key: the signed-in account's token is read per request.
        info = chatgpt_auth.status()
        if info.get("signed_in") and info.get("plan_usage"):
            found.update({"LOOPCUT_BASE_URL": chatgpt_auth.CHATGPT_PLAN_BASE_URL,
                          "LOOPCUT_MODEL": prefs.model or config.CHATGPT_PLAN_MODEL,
                          "LOOPCUT_API_KEY": chatgpt_auth.PLACEHOLDER_KEY})
        return found
    base_url = prefs.base_url.rstrip("/")
    key = credentials.api_key(base_url) if base_url else ""
    local = base_url.startswith("http://127.0.0.1")
    if base_url and (key or local):
        found.update({"LOOPCUT_BASE_URL": base_url, "LOOPCUT_MODEL": prefs.model,
                      "LOOPCUT_API_KEY": key or "local"})
    return found


class LOOPCUT_OT_polypizza_get_key(bpy.types.Operator):
    """Open Poly Pizza's API page in the browser: sign in, copy the key, paste it here"""
    bl_idname = "loopcut.polypizza_get_key"
    bl_label = "Get a Poly Pizza key"

    def execute(self, context):
        account.open_browser("https://poly.pizza/settings/api")
        self.report({"INFO"}, "Poly Pizza's API page is open in your browser; the terms are at poly.pizza/docs/api/v1.1")
        return {"FINISHED"}


class LOOPCUT_OT_blenderkit_get_key(bpy.types.Operator):
    """Open BlenderKit's plans page in the browser; a plan's key is under your profile > API"""
    bl_idname = "loopcut.blenderkit_get_key"
    bl_label = "BlenderKit plans"

    @classmethod
    def poll(cls, context):
        return config.billing_open()

    def execute(self, context):
        if not config.billing_open():
            return {"CANCELLED"}
        from . import blenderkit
        account.open_browser(blenderkit.PLANS)
        return {"FINISHED"}


class LOOPCUT_OT_clear_vendor_key(bpy.types.Operator):
    """Remove a saved vendor key without changing Loopcut's account or endpoint keys"""
    bl_idname = "loopcut.clear_vendor_key"
    bl_label = "Clear Vendor Key"
    bl_options = {"INTERNAL"}

    key_name: bpy.props.EnumProperty(items=[(key, label, "") for key, label, *_ in VENDOR_ROWS])

    def execute(self, context):
        try:
            credentials.clear_vendor_key(self.key_name)
        except (OSError, ValueError) as ex:
            self.report({"ERROR"}, f"Could not clear the saved vendor key: {ex}")
            return {"CANCELLED"}
        if os.environ.get(self.key_name, "").strip():
            self.report({"INFO"}, "Saved key cleared; the environment key still takes precedence")
        return {"FINISHED"}


_CLASSES = (LoopcutPreferences, LOOPCUT_OT_fetch_models, LOOPCUT_OT_anthropic_get_key, LOOPCUT_OT_use_provider,
            LOOPCUT_OT_pick_model,
            LOOPCUT_MT_models, LOOPCUT_OT_pick_tier, LOOPCUT_MT_tiers,
            LOOPCUT_OT_polypizza_get_key, LOOPCUT_OT_blenderkit_get_key, LOOPCUT_OT_clear_vendor_key)


def register() -> None:
    from . import chatgpt_ui
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    chatgpt_ui.register()


def unregister() -> None:
    from . import chatgpt_ui
    chatgpt_ui.unregister()
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
