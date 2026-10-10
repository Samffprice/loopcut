# SPDX-FileCopyrightText: 2026 Oxicid
# SPDX-License-Identifier: GPL-3.0-or-later

import bpy
import rna_keymap_ui  # type: ignore[import-untyped]

from . import utils
from .utils import ENUM
from bpy.props import *

UV_LAYERS_ENABLE = True


def prefs() -> 'UNIV_AddonPreferences':
    return bpy.context.preferences.addons[__package__].preferences


def univ_settings() -> 'UNIV_AddonPreferences':
    return bpy.context.preferences.addons[__package__].preferences


def force_debug():
    return prefs().debug == 'FORCE'


def debug():
    return prefs().debug == 'ENABLED'


def stable():
    return prefs().mode == 'STABLE'


def experimental():
    return prefs().mode == 'EXPERIMENTAL'


def _update_size_x(_self, _context):
    if univ_settings().lock_size and univ_settings().size_y != univ_settings().size_x:
        univ_settings().size_y = univ_settings().size_x


def _update_size_y(_self, _context):
    if univ_settings().lock_size and univ_settings().size_x != univ_settings().size_y:
        univ_settings().size_x = univ_settings().size_y


def _update_lock_size(_self, _context):
    if univ_settings().lock_size and univ_settings().size_y != univ_settings().size_x:
        univ_settings().size_y = univ_settings().size_x


def update_panel(_self, _context):
    from .ui import UNIV_PT_General_VIEW_3D as Panel
    from .ui import UNIV_PT_TD_PresetsManager_VIEW3D as PresetPanel

    if Panel.bl_category != prefs().panel_3d_view_category:
        try:
            for panel in (Panel, PresetPanel):
                if "bl_rna" in panel.__dict__:
                    bpy.utils.unregister_class(panel)

                panel.bl_category = prefs().panel_3d_view_category
                bpy.utils.register_class(panel)
        except:  # noqa
            print(f"UniV: Updating Panel View 3D category has failed:\n")
            import traceback
            traceback.print_exc()




def _update_uv_layers_show(_self, _context):
    from .operators.misc import UNIV_OT_UV_Layers_Manager
    if _self.uv_layers_show:
        if not any(handler is UNIV_OT_UV_Layers_Manager.univ_uv_layers_update for handler in bpy.app.handlers.depsgraph_update_post):
            bpy.app.handlers.depsgraph_update_post.append(UNIV_OT_UV_Layers_Manager.univ_uv_layers_update)
        from . import ui
        ui.REDRAW_UV_LAYERS = True
    else:
        for handler in reversed(bpy.app.handlers.depsgraph_update_post):
            if handler is UNIV_OT_UV_Layers_Manager.univ_uv_layers_update:
                bpy.app.handlers.depsgraph_update_post.remove(handler)


def _update_uv_layers_name(_self, context):
    if UV_LAYERS_ENABLE:
        settings = univ_settings()
        idx = settings.uv_layers_active_idx
        uv_name = settings.uv_layers_presets[idx].name
        for obj in context.selected_objects:
            if obj.type == 'MESH':
                uvs = obj.data.uv_layers
                if len(obj.data.uv_layers) >= idx+1:
                    if uvs[idx].name != uv_name:
                        uvs[idx].name = uv_name
        from .operators.misc import UNIV_OT_UV_Layers_Manager
        UNIV_OT_UV_Layers_Manager.update_uv_layers_props()


def _update_uv_layers_active_idx(self, context):
    if UV_LAYERS_ENABLE:
        is_edit = bpy.context.mode == 'EDIT_MESH'
        if is_edit:
            objects = context.objects_in_mode_unique_data
        else:
            objects = (obj_ for obj_ in context.selected_objects if obj_.type == 'MESH')

        idx = self.uv_layers_active_idx
        for obj in objects:
            uvs = obj.data.uv_layers
            if len(obj.data.uv_layers) >= idx+1:
                if not uvs[idx].active:
                    uvs[idx].active = True
        if prefs().enable_uv_layers_sync_borders_seam and is_edit:
            area = bpy.context.area
            if area:
                if area.type == 'VIEW_3D':
                    bpy.ops.mesh.univ_seam_border(selected=False, mtl=False, by_sharps=False)  # noqa
                else:
                    bpy.ops.uv.univ_seam_border(selected=False, mtl=False, by_sharps=False)  # noqa

        from .operators.misc import UNIV_OT_UV_Layers_Manager
        UNIV_OT_UV_Layers_Manager.update_uv_layers_props()

units = (
    ('cm', 'Centimeter', ''),
    ('m', 'Meter', ''),
    ('km', 'Kilometer', ''),
    ('in', 'Inch', ''),
    ('ft', 'Foot', ''),
    ('yd', 'Yard', ''),
    ('mi', 'Mile', ''),
)


def _set_transform_texel_unit(self, new_val, curr_val, _is_set):
    if new_val == curr_val:
        return new_val

    old_unit = units[curr_val][0]
    new_unit = units[new_val][0]

    self.texel = utils.unit_conversion(
        self.texel,
        new_unit,
        old_unit,
    )
    return new_val

def _set_transform_preset_texel_unit(self, new_val, curr_val, _is_set):
    if new_val == curr_val:
        return new_val

    old_unit = units[curr_val][0]
    new_unit = units[new_val][0]

    self.texel = utils.unit_conversion(
        self.texel,
        new_unit,
        old_unit,
    )
    return new_val

def _update_color_mode(_self, _context):
    from .icons import icons
    icons.register_icons_()
    icons.register_ws_icons_()

checker_generated_types = [
    ('UV_GRID', 'Grid', ''),
    ('COLOR_GRID', 'Color Grid', ''),
]




_udim_source = [
    ('CLOSEST_UDIM', 'Closest UDIM', "Pack islands to closest UDIM"),
    ('ACTIVE_UDIM', 'Active UDIM', "Pack islands to active UDIM image tile or UDIM grid tile where 2D cursor is located")
]

copy_to_layers_uv_channels_items_from = [
    ('0', 'Active', ''),
    ('1', '1', ''),
    ('2', '2', ''),
    ('3', '3', ''),
    ('4', '4', ''),
    ('5', '5', ''),
    ('6', '6', ''),
    ('7', '7', ''),
    ('8', '8', ''),
]

copy_to_layers_uv_channels_items_to = copy_to_layers_uv_channels_items_from.copy()
copy_to_layers_uv_channels_items_to[0] = ('0', 'Other', '')

_is_360_pack = bpy.app.version >= (3, 6, 0)
if _is_360_pack:
    _udim_source.append(('ORIGINAL_AABB', 'Original BBox', "Pack to starting bounding box of islands"))


# noinspection PyTypeHints
class UNIV_TexelPreset(bpy.types.PropertyGroup):
    if bpy.app.version >= (5, 0, 0):
        unit: EnumProperty(name='Unit', default='m', items=units, set_transform=_set_transform_preset_texel_unit)
    else:
        unit: EnumProperty(name='Unit', default='m', items=units)

    texel: FloatProperty(name='Texel', default=512, min=0.01, max=850_000)
    size_x: EnumProperty(name='Size X', default='2048', items=utils.resolutions)
    size_y: EnumProperty(name='Size Y', default='2048', items=utils.resolutions)

# noinspection PyTypeHints
class UNIV_UV_Layers(bpy.types.PropertyGroup):
    name: StringProperty(name='UVMap', update=_update_uv_layers_name)
    flag: IntProperty(name='Flag', default=0, min=0, max=3)

# noinspection PyTypeHints
class UNIV_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    # Settings
    # ================================================================================

    # Global Settings
    size_x: EnumProperty(name='X', default='2048', items=utils.resolutions, update=_update_size_x)
    size_y: EnumProperty(name='Y', default='2048', items=utils.resolutions, update=_update_size_y)
    lock_size: BoolProperty(name='Lock Size', default=True, update=_update_lock_size)

    invert_toggle_logic: BoolProperty(name='Invert Toggle Logic', default=False,
        description="When the selected elements contain both marked/unmarked or pinned/unpinned elements, "
                    "enabling this option will set the boolean value to False.")

    # Checker Texture
    checker_toggle: EnumProperty(name='Toggle', default='TOGGLE', items=ENUM('TOGGLE', 'OVERWRITE'),
                                           description='Off/On checker modifier')
    checker_generated_type: EnumProperty(name='Texture Type', default='UV_GRID', items=checker_generated_types)


    # Texel Settings
    use_texel: BoolProperty(name='Use Texel', default=False, description='Set Texel from global values in operators')
    if bpy.app.version >= (5, 0, 0):
        texel_unit: EnumProperty(name='Unit', default='m', items=units, set_transform=_set_transform_texel_unit)
    else:
        texel_unit: EnumProperty(name='Unit', default='m', items=units)
    texel: FloatProperty(name="Texel Density", default=512, min=0.01, max=850_000, precision=1,
                                 description="The number of texture pixels (texels) per unit surface area in 3D space.")
    active_td_index: IntProperty(min=0, max=8, options={'SKIP_SAVE'})
    texels_presets: CollectionProperty(name="TD Presets", type=UNIV_TexelPreset)

    texture_physical_size: FloatVectorProperty(name='TD from Physical Size', default=(2.5, 0.0), min=0.0,
                                                    soft_max=6, size=2, subtype='TRANSLATION')


    # Colors 2D
    overlay_2d_uv_edge_h_constraints_color: FloatVectorProperty(name="H-Constr", default=(0.85, 1.0, 0.0, 0.15),
        min=0.0, max=1.0, size=4, subtype='COLOR'
    )
    overlay_2d_uv_edge_v_constraints_color: FloatVectorProperty(name="V-Constr", default=(0.1, 0.1, 0.8, 0.35),
        min=0.0, max=1.0, size=4, subtype='COLOR'
    )

    overlay_2d_uv_edge_seam_color: FloatVectorProperty(name="Edge Seam", default=(0.8, 0.0, 0.0, 0.25),
        min=0.0, max=1.0, size=4, subtype='COLOR'
    )

    # Colors 3D

    # UV Layer
    uv_layers_show: BoolProperty(name='Show UV Layers in Panel', default=True, update=_update_uv_layers_show)

    uv_layers_size: IntProperty(name='Size', min=0, max=8, default=0, options={'SKIP_SAVE'})
    uv_layers_active_idx: IntProperty(name='Active UV index', min=0, max=7, default=0,
                                      update=_update_uv_layers_active_idx, options={'SKIP_SAVE'})
    uv_layers_active_render_idx: IntProperty(name='Active uv render index',
                                             min=-1, max=7, default=-1, options={'SKIP_SAVE'})
    uv_layers_presets: CollectionProperty(name="UV Layers", type=UNIV_UV_Layers, options={'SKIP_SAVE'})

    copy_to_layers_from: EnumProperty(name='From', default='0', items=copy_to_layers_uv_channels_items_from)
    copy_to_layers_to: EnumProperty(name='To', default='0', items=copy_to_layers_uv_channels_items_to)

    # Pack Settings
    use_uvpm: BoolProperty(name='Use UVPackmaster', default=False)
    shape_method: EnumProperty(name='Shape Method', default='CONCAVE',
                               items=(('CONCAVE', 'Exact', 'Uses exact geometry'),
                                      ('AABB', 'Fast', 'Uses bounding boxes'))
                               )
    scale: BoolProperty(name='Scale', default=True, description="Scale islands to fill unit square")
    rotate: BoolProperty(name='Rotate', default=True, description="Rotate islands to improve layout")
    rotate_method: EnumProperty(name='Rotation Method', default='CARDINAL',
                                items=(
                                    ('ANY', 'Any', "Any angle is allowed for rotation"),
                                    ('AXIS_ALIGNED', 'Orient', "Rotated to a minimal rectangle, either vertical or horizontal"),
                                    ('CARDINAL', 'Step 90', "Only 90 degree rotations are allowed")

                                ))

    pin: BoolProperty(name='Lock Pinned Islands', default=False,
                      description="Constrain islands containing any pinned UV's")
    pin_method: EnumProperty(name='Lock Method', default='LOCKED',
                             items=(
                                 ('LOCKED', 'All', "Pinned islands are locked in place"),
                                 ('ROTATION_SCALE', 'Rotation and Scale', "Pinned islands will translate only"),
                                 ('ROTATION', 'Rotation', "Pinned islands won't rotate"),
                                 ('SCALE', 'Scale', "Pinned islands won't rescale")))

    merge_overlap: BoolProperty(name='Lock Overlaps', default=False)
    udim_source: EnumProperty(name='Pack to', default='CLOSEST_UDIM', items=_udim_source)

    padding: IntProperty(name='Padding', default=8, min=0, soft_min=2, soft_max=32, max=64, step=2,
                         subtype='PIXEL', description="Space between islands in pixels.\n\n"
                                                      "Formula for converting the current Padding implementation to Margin:\n"
                                                      "Margin = Padding / 2 / Texture Size\n\n"
                                                      "Optimal value for UV padding:\n"
                                                      "256 = 1  px\n"
                                                      "512 = 2-3 px\n"
                                                      "1024 = 4-5 px\n"
                                                      "2048 = 8-10 px\n"
                                                      "4096 = 16-20 px\n"
                                                      "8192 = 32-40 px\t")

    align_mode: EnumProperty(name="Align Mode", default='ALIGN', items=(
        ('ALIGN', 'Align', 'Align', 'EMPTY_SINGLE_ARROW', 0),
        ('MOVE_ANGLE_COLLECT', 'Move | Align by Angle | Collect', 'Move in Island Mode. '
                                                    'Collect in Island mode when press Center. '
                                                    'HV applies align by edge angle in Island mode', 'ARROW_LEFTRIGHT', 1),
        ('ALIGN_TO_CURSOR', 'Align to cursor', 'Align to cursor', 'PIVOT_CURSOR', 2),
        ('ALIGN_TO_CURSOR_UNION', 'Align to cursor union', 'Align to cursor union', 'EVENT_U', 3),
        ('INDIVIDUAL', 'Individual', 'Individual Align', 'PIVOT_INDIVIDUAL', 4)
    ))

    align_island_mode: EnumProperty(name="Island Mode", default='FOLLOW', items=(
        ('FOLLOW', 'Follow', '', 'EVENT_F', 0),
        ('ISLAND', 'Island', '', 'UV_ISLANDSEL', 1),
        ('VERTEX', 'Vertex', '', 'VERTEXSEL', 2)
    ))

    batch_inspect_flags: IntProperty(name="Batch Inspect Tags", min=0,
                                     default=__import__(__package__.replace('preferences', '') + '.operators.inspect',
                                                        fromlist=['inspect']).Inspect.default_value_for_settings()
                                     )

    # ================================================================================

    tab: EnumProperty(
        items=(
            ('GENERAL', 'General', ''),
            ('UI', 'UI', ''),
            ('INFO', 'Info', ''),
        ),
        default='GENERAL')
    # default='INFO')  # noqa

    debug: EnumProperty(name='Debug',
                        items=(
                            ('DISABLED', 'Disabled', ''),
                            ('ENABLED', 'Enabled', ''),
                            ('FORCE', 'Force', ''),
                        ),
                        default='DISABLED')

    mode: EnumProperty(name='Mode',
                       items=(
                           ('STABLE', 'Stable', ''),
                           ('EXTENDED', 'Extended', ''),
                           ('EXPERIMENTAL', 'Experimental', ''),
                       ),
                       default='EXTENDED')



    use_csa_mods: bpy.props.BoolProperty(default=True,
                                         name="Use Modifier Keys",
                                         description="Enable behavior changes based on Ctrl, Shift, and Alt keys when invoking the operator"
                                         )

    snap_points_default: EnumProperty(name='Default Snap Points',
                                      items=(
                                          ('ALL', 'All', ''),
                                          ('FOLLOW_MODE', 'Follow Mode', 'Follow the selection mode, VERTEX mode remains always')
                                      ),
                                      default='FOLLOW_MODE',
                                      description='Default Snap Points for QuickSnap')

    # ----------
    color_mode: EnumProperty(name='Color Mode',
                             items=(('COLOR', 'Color', ''), ('MONO', 'Monochrome', '')),
                             default='COLOR',
                             update=_update_color_mode)

    icon_scale: FloatProperty(name='Icon Scale', default=1.0, min=1.0, soft_max=1.25, max=2.0)

    icon_size: EnumProperty(name='Icon Size',
                            items=(('32', '32', ''), ('64', '64', ''), ('128', '128', ''), ('256', '256', '')),
                            default='32')
    icon_antialiasing: EnumProperty(name='Anti-Aliasing',
                                    items=(('1', 'x1', ''), ('2', 'x2', ''), ('4', 'x4', ''), ('8', 'x8', '')),
                                    default='4')

    # NOTE: The prefixes "icon + [mono | colored | common]" must be used to specify whether,
    #  a default value should be included when generating icons.
    # Mono
    icon_mono_green: FloatVectorProperty(name='Green',
                                         # 8bc6a1
                                         subtype="COLOR", size=4, min=0.0, max=1.0, default=(0.258181, 0.564712, 0.3564003, 1))

    icon_mono_gray: FloatVectorProperty(name='Grey Color',
                                        # c7c7c7
                                        subtype="COLOR", size=4, min=0.0, max=1.0, default=(0.5711, 0.5711, 0.5711, 1))

    # Colored
    icon_colored_violet: FloatVectorProperty(name='Violet',
                                             # 7d87ff
                                             subtype="COLOR", size=4, min=0.0, max=1.0, default=(0.20505, 0.24228, 1, 1))

    icon_colored_cian: FloatVectorProperty(name='Cian',
                                           # 62cdf9
                                           subtype="COLOR", size=4, min=0.0, max=1.0, default=(0.1221, 0.6105, 0.9473, 1))

    icon_colored_purple: FloatVectorProperty(name='Purple',
                                             # dc87ff
                                             subtype="COLOR", size=4, min=0.0, max=1.0, default=(0.71569, 0.24228, 1, 1))

    icon_colored_pink: FloatVectorProperty(name='Pink',
                                           # ff87a9
                                           subtype="COLOR", size=4, min=0.0, max=1.0, default=(1, 0.24228, 0.396755, 1))
    # Common
    icon_common_white: FloatVectorProperty(name='White Color',
                                          subtype="COLOR", size=4, min=0.0, max=1.0, default=(1, 1, 1, 1))  # ffffff

    icon_common_select_arrow: FloatVectorProperty(name='Select Arrow Color',
                                                 # ececec
                                                 subtype="COLOR", size=4, min=0.0, max=1.0, default=(0.83879, 0.83879, 0.83879, 1))

    # ----------

    keymap_conflict_filter: BoolProperty(name='Show Only Error', default=False)

    keymap_name_filter: StringProperty(name="Search by Name", default='', options={'TEXTEDIT_UPDATE'})
    keymap_key_filter: StringProperty(name="Search by Key-Binding", default='', options={'TEXTEDIT_UPDATE'})

    split_toggle_uv_by_cursor: BoolProperty(name='Split ToggleUV by Mouse Cursor', default=False)
    show_split_toggle_uv_button: BoolProperty(name='Show Split ToggleUV Button', default=False)
    show_view_3d_panel: BoolProperty(name='Show View 3D Panel', default=True)
    panel_3d_view_category: StringProperty(name="Panel 3D View Category", description="Enter a name for the panel category",
                                           default='UniV', update=update_panel)
    # enable_uv_name_controller: BoolProperty(name='Enable UV name controller', default=False)
    enable_uv_layers_sync_borders_seam: BoolProperty(name='Enable sync Border Seam', default=True)

    max_pick_distance: IntProperty(name='Max Pick Distance', default=75, min=15, soft_max=100, subtype='PIXEL',
                                   description='Pick Distance for Pick Select, Quick Snap operators'
                                   )

    @property
    def glob_size(self) -> tuple[int, int]:
        return int(prefs().size_x), int(prefs().size_y)

    @property
    def texel_density(self):
        return utils.unit_conversion(self.texel, 'm', self.texel_unit)

    @texel_density.setter
    def texel_density(self, td):
        self.texel = utils.unit_conversion(td, self.texel_unit, 'm')

    def draw(self, context):
        layout = self.layout
        row = layout.row()
        row.prop(self, "tab", expand=True)

        if self.tab == 'GENERAL':
            layout.prop(self, 'debug')
            layout.prop(self, 'mode')


            # layout.separator(factor=0.5)
            col = layout.column(align=True)
            col.prop(self, 'use_texel', text='Use Texel in operators')
            col.prop(self, 'use_csa_mods')
            col.prop(self, 'split_toggle_uv_by_cursor')
            # layout.separator(factor=0.5)
            layout.prop(self, 'show_split_toggle_uv_button')

            layout.label(text='QuickSnap:')
            layout.prop(self, 'snap_points_default')
            layout.separator()

            layout.prop(self, 'max_pick_distance')

        elif self.tab == 'UI':
            box = layout.box()
            box.prop(self, 'color_mode')

            split = layout.split(factor=0.5)
            split.prop(self, 'show_view_3d_panel')
            row = split.row(heading='3D Panel Category:')
            row.prop(self, 'panel_3d_view_category', text='')

            layout.prop(self, 'show_split_toggle_uv_button')

            from . import ui
            ui.UNIV_PT_GlobalSettings.draw_ui_settings(layout)

        # elif self.tab == 'INFO':
        else:
            enable = True
            if hasattr(bpy.app, 'online_access'):
                enable = bpy.app.online_access
            row = layout.row(align=True)
            row.enabled = enable
            row.operator("wm.url_open", text="GitHub").url = r"https://github.com/Oxicid/UniV"


            import textwrap
            light_docs_info = textwrap.dedent("""
            Many operators support the Pick system. 
            This means that when an operation is triggered via a keymap with no mesh elements selected, it is applied to the nearest vert & edge & island. This greatly improves convenience, letting you avoid the effects Sync (select) mode has on shared edges and vertices.
            
            There is much more to UniV operators than you might think at first glance. Many operators are context-dependent, for example, on Sync state, selection mode (Verts, Edge, Face and Islands), as well as on pressed Ctrl, Shift, Alt (CSA) keys and combinations thereof.
            
            That is, before pressing the LMB button press CSA, then other modes of the operator are called. And these modifications are subject to a certain logic, which in most cases works:
            
            Ctrl - To Cursor for transform or Deselect for select
            Alt - Alternative operation that is fundamentally different from the default.
            Shift - Individual, Inplace for transform or Extend for select
            
            But you don't have to use the CSA keys, because a panel appears in the lower left corner where you can change the properties
            
            
            Also, the addon doesn't impose its hotkeys on you, but you can easily enable them in Edit->Preferences->Extensions->UniV->Keymaps. But some operators due to their specificity can be called only through keymaps (QuickSnap, SplitUVToggle, SyncUVToggle).
            """)

            width = 80
            for r in context.area.regions:
                if r.type == "WINDOW":
                    width = r.width // 6
                    break
            width = width * (1 / context.preferences.view.ui_scale) - 2

            box = layout.box()
            for paragraph in light_docs_info.strip().splitlines():
                if paragraph.strip():
                    for line in textwrap.fill(paragraph, width=width).splitlines():
                        box.label(text=line)
                else:
                    box.separator()





class UNIV_OT_ShowAddonPreferences(bpy.types.Operator):
    bl_idname = 'wm.univ_show_addon_preferences'
    bl_label = 'Addon Preferences'

    def execute(self, context):
        bpy.ops.screen.userpref_show()
        context.preferences.active_section = 'ADDONS'
        context.window_manager.addon_search = 'UniV'
        return {'FINISHED'}
