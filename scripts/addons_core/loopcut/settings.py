"""Loopcut's page in Blender's preferences: provider, key, model and how much it may do unasked.

config.py reads these through values(); the API key itself is kept by credentials.py, not in
Blender's preferences file.
"""

import json
import os
import urllib.error
import urllib.request

import bpy

from . import account, chatgpt_auth, cloud_account, config, credentials

PACKAGE = __package__

# (id, label, base URL, default model, note shown under the key field)
PROVIDERS = (
    ("LOOPCUT", "Loopcut", account.BASE_URL, "fast",
     "Sign in with your Loopcut account. The Loopcut service picks the model behind Fast and Pro and meters "
     "usage per 5-hour session and per week." if config.BETA else
     "Sign in with your Loopcut account. Fast and Pro are included in your plan; the Loopcut "
     "service picks the model behind each and meters usage per 5-hour session and per week."),
    ("META", "Meta", "https://api.meta.ai/v1", "muse-spark-1.3-contributor",
     "On Meta's contributor tier your prompts, scene descriptions and viewport captures may be "
     "used for training. Pick another tier or provider for confidential work."),
    ("OPENAI", "OpenAI", "https://api.openai.com/v1", "", ""),
    ("OPENROUTER", "OpenRouter", "https://openrouter.ai/api/v1", "", ""),
    ("OLLAMA", "Ollama (this computer)", "http://127.0.0.1:11434/v1", "",
     "Nothing leaves this computer. The model needs tool calling and image input."),
    ("CUSTOM", "Custom", "", "", "Any endpoint that speaks the OpenAI chat completions API with tools."),
    # Last, so the stored enum values of the rows above stay what they were.
    ("CHATGPT", "ChatGPT plan", chatgpt_auth.CHATGPT_PLAN_BASE_URL, config.CHATGPT_PLAN_MODEL,
     "Sign in with ChatGPT: requests go from this computer to OpenAI and use your ChatGPT Plus or Pro plan's "
     "usage. Loopcut never sees your ChatGPT tokens; they stay in a private file on this computer. One agent "
     "does the whole job in a working folder of its own, as a coding agent would."),
)
_BY_ID = {p[0]: p for p in PROVIDERS}
_models: list[str] = []  # Filled by "Fetch models"; not saved.
VENDOR_ROWS = (
    ("OPENAI_API_KEY", "OpenAI", "openai_vendor_key", "Image generation and editing · Private (A)"),
    ("FAL_KEY", "fal", "fal_vendor_key", "Image to 3D · Private (A); retopology · Longer retention (B)"),
    ("GEMINI_API_KEY", "Google Gemini", "gemini_vendor_key", "Voice lines · Paid API only, kept 55 days (B); free tier may train (D)"),
    ("ELEVENLABS_API_KEY", "ElevenLabs", "elevenlabs_vendor_key", "Sound effects · May train (C); disable training and SFX sharing in your vendor account"),
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


def draw_vendor_keys(layout, prefs):
    """PASSWORD/SKIP_SAVE fields use the same credentials callbacks as the endpoint key."""
    box = layout.box()
    box.label(text="Your own keys", icon="KEYINGSET")
    for key, label, prop, note in VENDOR_ROWS:
        row = box.row(align=True)
        row.prop(prefs, prop, text=label)
        row.operator("loopcut.clear_vendor_key", text="Clear").key_name = key
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
    flags.prop(prefs, "byok_tools")
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


def config_changed() -> None:
    """For other modules that change the connection (account.py after a sign-in)."""
    _changed()


def _provider_changed(self, context) -> None:
    _, _, base_url, model, _ = _BY_ID[self.provider]
    if self.provider == "LOOPCUT":
        self.base_url, self.model = base_url, self.tier
    elif self.provider != "CUSTOM":
        self.base_url, self.model = base_url, model
    _models.clear()
    _changed()


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
    coming_soon_expanded: bpy.props.BoolProperty(default=True, options={"HIDDEN"})

    provider: bpy.props.EnumProperty(
        name="Provider", items=[(p[0], p[1], p[2] or "Your own endpoint") for p in PROVIDERS],
        default="LOOPCUT", update=_provider_changed)
    tier: bpy.props.StringProperty(
        name="Model", default="fast", update=_tier_changed,
        description="Which Loopcut model answers; the menu says how much usage each takes against the default"
        if config.BETA else
        "Which of the models your Loopcut plan offers answers; the menu says what each costs against the default")
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
        description="Run code and file changes without the Run / Reject step. Renders and bakes still ask. "
                    "Every turn still gets a checkpoint you can restore")
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
    auto_update: bpy.props.BoolProperty(
        name="Download updates automatically", default=True,
        description="Look for a newer Loopcut now and then and download it in the background. "
                    "Installing waits until you choose Restart to update")
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
    # AddonPreferences has no IDProperty mapping; saved RNA fields back local callbacks.
    local_no_generation: bpy.props.BoolProperty(default=cloud_account.DEFAULTS["no_generation"], options={"HIDDEN"})
    local_private_mode: bpy.props.BoolProperty(default=cloud_account.DEFAULTS["private_mode"], options={"HIDDEN"})
    no_generation: bpy.props.BoolProperty(
        name="No generation", default=False, get=_get_no_generation, set=_set_no_generation,
        description="Hide and refuse generative service tools. Retopology remains available")
    byok_tools: bpy.props.StringProperty(
        name="Use own keys for tools", default="",
        description="Comma-separated tool names, such as image_generate,voice_line"
        + ("" if config.BETA else ". Otherwise funded accounts use credits"))
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
                column.prop(self, "api_key")
            row = column.row(align=True)
            row.prop(self, "model")
            row.operator("loopcut.fetch_models", text="", icon="FILE_REFRESH")
            if _models:
                row.menu("LOOPCUT_MT_models", text="", icon="DOWNARROW_HLT")
        note = _BY_ID[self.provider][4]
        if note:
            lines = wrap(note, 90)
            info = box.column(align=True)
            info.scale_y = 0.85
            for n, line in enumerate(lines):
                info.label(text=line, icon="INFO" if n == 0 else "BLANK1")

        if self.provider != "LOOPCUT" and not account.signed_in():
            box = layout.box()
            box.label(text="Loopcut account")
            account.draw(box.column(align=True))

        draw_vendor_keys(layout, self)

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
                flags.label(text="Workers and routing use the Loopcut service" if config.BETA else
                            "Workers and routing run on Loopcut plans")
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
        if not config.BETA:
            row.operator("loopcut.blenderkit_get_key", text="", icon="URL")
        row = column.row(align=True)
        row.prop(self, "polypizza_key")
        row.operator("loopcut.polypizza_get_key", text="", icon="URL")
        info = column.column(align=True)
        info.scale_y = 0.85
        info.label(text="BlenderKit's free assets, Poly Haven and the asset libraries in Preferences > File Paths", icon="INFO")
        info.label(text=("need no key. Poly Pizza's key is free; its" if config.BETA else
                         "need no key. A BlenderKit plan key adds its paid assets. Poly Pizza's key is free; its"), icon="BLANK1")
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
            status = update.state()["status"]
            if status == "ready":
                row.operator("loopcut.restart_to_update")
            elif status not in ("checking", "downloading", "installing"):
                row.operator("loopcut.check_for_updates", text="Check Now")


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
        for name in _models:
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


def provider_note(provider: str) -> str:
    return _BY_ID[provider][4]


def has_models() -> bool:
    return bool(_models)


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
    names = list(_models)
    if prefs.model and prefs.model not in names:
        names.insert(0, prefs.model)
    return [(name, name, "", name == prefs.model) for name in names]


def choose_model(model_id: str) -> None:
    """The panel's model button picked one; see model_options."""
    prefs = preferences()
    if prefs is None or not model_id:
        return
    if prefs.provider == "LOOPCUT":
        if model_id in {m["id"] for m in account.models()}:
            prefs.tier = model_id  # _tier_changed copies it to the model and reloads the config.
    else:
        prefs.model = model_id


def model_label(model_id: str) -> str:
    """What the panel's model button shows for the configured model."""
    prefs = preferences()
    if prefs is not None and prefs.provider == "LOOPCUT":
        return account.model_label(model_id)
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
        "LOOPCUT_BYOK_TOOLS": getattr(prefs, "byok_tools", ""),
    }
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
        return not config.BETA

    def execute(self, context):
        if config.BETA:
            return {"CANCELLED"}
        from . import blenderkit
        account.open_browser(blenderkit.PLANS)
        return {"FINISHED"}


class LOOPCUT_OT_clear_vendor_key(bpy.types.Operator):
    """Remove a saved vendor key without changing Loopcut's account or endpoint keys"""
    bl_idname = "loopcut.clear_vendor_key"
    bl_label = "Clear Vendor Key"
    bl_options = {"INTERNAL"}

    key_name: bpy.props.EnumProperty(items=[(key, label, "") for key, label, _, _ in VENDOR_ROWS])

    def execute(self, context):
        try:
            credentials.clear_vendor_key(self.key_name)
        except (OSError, ValueError) as ex:
            self.report({"ERROR"}, f"Could not clear the saved vendor key: {ex}")
            return {"CANCELLED"}
        if os.environ.get(self.key_name, "").strip():
            self.report({"INFO"}, "Saved key cleared; the environment key still takes precedence")
        return {"FINISHED"}


_CLASSES = (LoopcutPreferences, LOOPCUT_OT_fetch_models, LOOPCUT_OT_pick_model, LOOPCUT_MT_models,
            LOOPCUT_OT_pick_tier, LOOPCUT_MT_tiers,
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
