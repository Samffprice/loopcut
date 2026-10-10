"""Loopcut support and identity without copying Blender's splash/About menus."""

import bpy

from . import config


class LOOPCUT_OT_report_bug(bpy.types.Operator):
    """Open Loopcut's beta bug-report destination"""
    bl_idname = "loopcut.report_bug"
    bl_label = "Report a Loopcut Bug"

    def execute(self, context):
        bpy.ops.wm.url_open(url=config.bug_report_url())
        return {"FINISHED"}


def _draw_info(self, context):
    self.layout.label(text=f"Loopcut {config.version_label()}")
    self.layout.operator("loopcut.report_bug", text="Report a Bug", icon="URL")


# Not the first-run quick setup: onboarding.py owns those steps.
_MENUS = ("WM_MT_splash", "WM_MT_splash_about")


def register():
    bpy.utils.register_class(LOOPCUT_OT_report_bug)
    if hasattr(bpy.types, "SpaceLoopcut"):
        for name in _MENUS:
            getattr(bpy.types, name).append(_draw_info)


def unregister():
    if hasattr(bpy.types, "SpaceLoopcut"):
        for name in _MENUS:
            getattr(bpy.types, name).remove(_draw_info)
    bpy.utils.unregister_class(LOOPCUT_OT_report_bug)
