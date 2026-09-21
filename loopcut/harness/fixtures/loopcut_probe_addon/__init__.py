"""A stand-in for a user-installed add-on, enabled by the harness from its own path."""

import bpy

bl_info = {
    "name": "Loopcut Probe",
    "author": "Loopcut",
    "version": (1, 0),
    "blender": (4, 2, 0),
    "description": "A tiny add-on the harness enables to check that the model is told about it",
    "category": "Development",
}


class PROBE_OT_say_hi(bpy.types.Operator):
    bl_idname = "probe.say_hi"
    bl_label = "Say hi"
    bl_description = "Print a greeting"
    times: bpy.props.IntProperty(name="Times", default=1, min=1, description="How many greetings")

    def execute(self, context):
        print("hi " * self.times)
        return {"FINISHED"}


class PROBE_OT_say_bye(bpy.types.Operator):
    bl_idname = "probe.say_bye"
    bl_label = "Say bye"

    def execute(self, context):
        return {"FINISHED"}


class PROBE_OT_wave(bpy.types.Operator):
    bl_idname = "wm.probe_wave"
    bl_label = "Wave"

    def execute(self, context):
        return {"FINISHED"}


CLASSES = (PROBE_OT_say_hi, PROBE_OT_say_bye, PROBE_OT_wave)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
