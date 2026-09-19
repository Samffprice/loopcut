"""First run of the Loopcut build, drawn in the splash screen: bring your Blender setup along,
connect a model, see what is sent where.

The fork's splash menus (WM_MT_splash_quick_setup, WM_MT_splash in bl_operators/wm.py) call
draw_splash() and draw themselves when it returns False. Which of the two menus is on screen is
Blender's decision: the quick setup until preferences are saved, the normal splash after.

Steps: "import" (only if stock Blender settings were found), "setup" (Blender's own quick setup,
for a fresh start), "connect", "privacy", "done". State lasts for the session; someone who quits
half way gets the normal splash next time and the panel itself asks for a key.
"""

import shutil
import sys
from pathlib import Path

import bpy

from . import blender_import, config, settings

_state = {"step": None, "imported": None, "failed": [], "error": ""}


def _source():
    return blender_import.find_source(blender_import.stock_root(), bpy.app.version[:2])


def _connected() -> bool:
    try:
        config.load()
    except config.ConfigError:
        return False
    return True


def _after_setup() -> str:
    return "done" if _connected() or settings.preferences() is None else "connect"


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
    if step == "connect":
        _draw_connect(layout)
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


def _draw_connect(layout) -> None:
    prefs = settings.preferences()
    column = _content(layout, "Connect a Model")
    column.use_property_split = True
    column.use_property_decorate = False
    column.prop(prefs, "provider")
    if prefs.provider == "CUSTOM":
        column.prop(prefs, "base_url")
    local = prefs.base_url.startswith("http://127.0.0.1")
    if not local:
        column.prop(prefs, "api_key")
    row = column.row(align=True)
    row.prop(prefs, "model")
    row.operator("loopcut.fetch_models", text="", icon="FILE_REFRESH")
    if settings.has_models():
        row.menu("LOOPCUT_MT_models", text="", icon="DOWNARROW_HLT")
    column.separator(factor=2)
    sub = column.column()
    sub.enabled = _connected()
    _step_button(sub, "Continue", "privacy", save=True)
    _step_button(column, "Skip for Now", "done", save=True)
    layout.separator(factor=2.0)


def _draw_privacy(layout) -> None:
    prefs = settings.preferences()
    provider = settings.provider_label(prefs.provider)
    column = _content(layout, "Privacy and Control")
    _notes(column,
           f"Your messages, a description of the scene and viewport captures go to {provider}. "
           "Your .blend files are never uploaded.",
           settings.provider_note(prefs.provider))
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
    ready = "Loopcut is ready" if _connected() else "Add a key in Preferences > Add-ons > Loopcut to start"
    column.label(text=f"{ready}. {shortcut} jumps to the chat.", icon="NONE" if _state["imported"] else "CHECKMARK")
    layout.separator()
    layout.separator(type="LINE")
    layout.separator()


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
        _state["error"] = "Could not copy the settings; details are in the console."
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
    if not host.find_panel(window):  # Their startup file has no Loopcut panel.
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


class LOOPCUT_OT_onboarding_step(bpy.types.Operator):
    bl_idname = "loopcut.onboarding_step"
    bl_label = "Continue"
    bl_options = {"INTERNAL"}

    step: bpy.props.EnumProperty(items=[(s, s, "") for s in ("setup", "connect", "privacy", "done")])
    save: bpy.props.BoolProperty()

    def execute(self, context):
        _state["step"] = self.step
        if self.save:
            bpy.ops.wm.save_userpref()
        if context.region_popup is not None:  # The splash stays open and only redraws when told to.
            context.region_popup.tag_refresh_ui()
        return {"FINISHED"}


_CLASSES = (LOOPCUT_OT_import_blender_settings, LOOPCUT_OT_onboarding_step)


def register() -> None:
    for cls in _CLASSES:
        bpy.utils.register_class(cls)


def unregister() -> None:
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
