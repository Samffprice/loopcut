"""Loopcut's page in Blender's preferences: provider, key, model and how much it may do unasked.

config.py reads these through values(); the API key itself is kept by credentials.py, not in
Blender's preferences file.
"""

import json
import urllib.error
import urllib.request

import bpy

from . import account, config, credentials

PACKAGE = __package__

# (id, label, base URL, default model, note shown under the key field)
PROVIDERS = (
    ("LOOPCUT", "Loopcut", account.BASE_URL, "fast",
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
)
_BY_ID = {p[0]: p for p in PROVIDERS}
_models: list[str] = []  # Filled by "Fetch models"; not saved.


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

    provider: bpy.props.EnumProperty(
        name="Provider", items=[(p[0], p[1], p[2] or "Your own endpoint") for p in PROVIDERS],
        default="LOOPCUT", update=_provider_changed)
    tier: bpy.props.EnumProperty(
        name="Model", items=account.MODELS, default="fast", update=_tier_changed,
        description="Fast answers quickly; Pro thinks longer and uses more of your allowance")
    base_url: bpy.props.StringProperty(
        name="Base URL", default=PROVIDERS[0][2], update=_changed,
        description="OpenAI-compatible endpoint. Must be https, or http on 127.0.0.1")
    api_key: bpy.props.StringProperty(
        name="API Key", subtype="PASSWORD", options={"SKIP_SAVE"}, get=_get_key, set=_set_key,
        description="Stored in Loopcut's own file in your Blender config folder, not in preferences")
    model: bpy.props.StringProperty(name="Model", default=PROVIDERS[0][3], update=_changed)
    reasoning_effort: bpy.props.EnumProperty(
        name="Reasoning", items=[("low", "Low", "Fastest"), ("medium", "Medium", ""), ("high", "High", "Slowest")],
        default="medium", update=_changed)
    auto_run: bpy.props.BoolProperty(
        name="Run without asking", default=False, update=_changed,
        description="Run code and file changes without the Run / Reject step. Renders and bakes still ask. "
                    "Every turn still gets a checkpoint you can restore")
    auto_look: bpy.props.BoolProperty(
        name="Look after every change", default=True, update=_changed,
        description="After each step that changes the scene, the agent gets a picture of the result. "
                    "One image per change; off, it only sees what it asks to see")
    max_steps: bpy.props.IntProperty(name="Steps per message", default=25, min=1, max=200, update=_changed)
    context_budget: bpy.props.IntProperty(
        name="Context budget", default=config.DEFAULT_CONTEXT_BUDGET,
        min=config.MIN_CONTEXT_BUDGET, max=config.MAX_CONTEXT_BUDGET, step=1000, update=_changed,
        description="Tokens each request may carry of the conversation. Over this, old tool results "
                    "are shortened and then the earlier conversation is summarized. Lower is cheaper; "
                    "higher remembers more")
    run_timeout: bpy.props.IntProperty(
        name="Stop code after (s)", default=60, min=1, max=3600, update=_changed,
        description="A step that runs longer is stopped, so an endless loop cannot freeze Blender")
    checkpoint_budget_mb: bpy.props.IntProperty(
        name="Checkpoints (MB)", default=2048, min=1, max=1_000_000, update=_changed,
        description="Checkpoint storage per conversation. The oldest checkpoints expire first")
    dock_on_startup: bpy.props.BoolProperty(
        name="Open the panel at startup", default=True,
        description="Dock Loopcut at the right of the 3D viewport when Blender starts without it")
    auto_update: bpy.props.BoolProperty(
        name="Download updates automatically", default=True,
        description="Look for a newer Loopcut now and then and download it in the background. "
                    "Installing waits until you choose Restart to update")

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        box = layout.box()
        box.label(text="Connection", icon="WORLD")
        column = box.column()
        column.prop(self, "provider")
        if self.provider == "LOOPCUT":
            account.draw(column.column(align=True))
            column.separator()
            column.row(align=True).prop(self, "tier", expand=True)
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

        split = layout.split(factor=0.5)
        agent = split.box()
        agent.label(text="Agent", icon="PLAY")
        agent.column().prop(self, "reasoning_effort")
        flags = agent.column()
        flags.use_property_split = False  # A checkbox reads best with its whole label beside it.
        flags.prop(self, "auto_run")
        flags.prop(self, "auto_look")
        flags.prop(self, "dock_on_startup")
        limits = split.box()
        limits.label(text="Limits", icon="TIME")
        column = limits.column()
        column.prop(self, "max_steps")
        column.prop(self, "context_budget")
        column.prop(self, "run_timeout")
        column.prop(self, "checkpoint_budget_mb")

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
        if prefs is None or not prefs.base_url:
            self.report({"ERROR"}, "Set the base URL first")
            return {"CANCELLED"}
        request = urllib.request.Request(
            f"{prefs.base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {credentials.api_key(prefs.base_url)}", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read(4_000_000))
        except urllib.error.HTTPError as ex:
            self.report({"ERROR"}, f"The provider answered HTTP {ex.code}; check the key")
            return {"CANCELLED"}
        except (urllib.error.URLError, ValueError, TimeoutError) as ex:
            self.report({"ERROR"}, f"Could not list models: {ex}")
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
        return [(id, label, note, prefs.tier == id) for id, label, note in account.MODELS]
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
        if model_id in {id for id, _, _ in account.MODELS}:
            prefs.tier = model_id  # _tier_changed copies it to the model and reloads the config.
    else:
        prefs.model = model_id


def set_tier(tier: str) -> None:
    """Pick Fast or Pro on the Loopcut provider, as the model menu would."""
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
        "LOOPCUT_REASONING_EFFORT": prefs.reasoning_effort,
        "LOOPCUT_AUTO_RUN": "true" if prefs.auto_run else "false",
        "LOOPCUT_AUTO_LOOK": "true" if prefs.auto_look else "false",
        "LOOPCUT_MAX_STEPS": str(prefs.max_steps),
        "LOOPCUT_CONTEXT_BUDGET": str(prefs.context_budget),
        "LOOPCUT_RUN_TIMEOUT": str(prefs.run_timeout),
        "LOOPCUT_CHECKPOINT_BUDGET_MB": str(prefs.checkpoint_budget_mb),
    }
    base_url = prefs.base_url.rstrip("/")
    key = credentials.api_key(base_url) if base_url else ""
    local = base_url.startswith("http://127.0.0.1")
    if base_url and (key or local):
        found.update({"LOOPCUT_BASE_URL": base_url, "LOOPCUT_MODEL": prefs.model,
                      "LOOPCUT_API_KEY": key or "local"})
    return found


_CLASSES = (LoopcutPreferences, LOOPCUT_OT_fetch_models, LOOPCUT_OT_pick_model, LOOPCUT_MT_models)


def register() -> None:
    for cls in _CLASSES:
        bpy.utils.register_class(cls)


def unregister() -> None:
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
