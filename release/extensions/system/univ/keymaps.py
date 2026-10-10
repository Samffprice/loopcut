# SPDX-FileCopyrightText: 2026 Oxicid
# SPDX-License-Identifier: GPL-3.0-or-later

import bpy

keys = []
keys_ws = []
keys_areas = ['UV Editor', 'Window', 'Object Mode', 'Mesh']  # TODO: Rename to spaces
keys_areas_workspace = ['3D View Tool: Object, UniV', '3D View Tool: Edit Mesh, UniV']
other_conflict_areas = ['Frames']  # NOTE: not actual after delete keymaps for align?


def add_mesh_keymaps(km):
    # Grow
    kmi = km.keymap_items.new('mesh.univ_select_grow', 'WHEELUPMOUSE', 'PRESS', ctrl=True)
    kmi.properties.grow = True
    keys.append((km, kmi))

    kmi = km.keymap_items.new('mesh.univ_select_grow', 'WHEELDOWNMOUSE', 'PRESS', ctrl=True)
    kmi.properties.grow = False
    keys.append((km, kmi))

    # Edge grow
    kmi = km.keymap_items.new('mesh.univ_select_edge_grow', 'WHEELUPMOUSE', 'PRESS', ctrl=True, alt=True)
    kmi.properties.grow = True
    keys.append((km, kmi))

    kmi = km.keymap_items.new('mesh.univ_select_edge_grow', 'WHEELDOWNMOUSE', 'PRESS', ctrl=True, alt=True)
    kmi.properties.grow = False
    keys.append((km, kmi))



def add_keymaps():
    global keys

    kc = bpy.context.window_manager.keyconfigs.addon
    if not kc:
        return  # Can be None in background mode.


    # Object Mode
    km = kc.keymaps.new(name='Object Mode')
    kmi = km.keymap_items.new('object.univ_join', 'J', 'PRESS', ctrl=True)
    keys.append((km, kmi))

    # Pie Menu
    kmi = km.keymap_items.new("wm.call_menu_pie", 'ACCENT_GRAVE', 'PRESS')
    kmi.properties.name = "VIEW3D_MT_PIE_univ_obj"
    keys.append((km, kmi))

    # Mesh
    km = kc.keymaps.new(name='Mesh')

    # Pie Menu
    kmi = km.keymap_items.new("wm.call_menu_pie", 'ACCENT_GRAVE', 'PRESS')
    kmi.properties.name = "VIEW3D_MT_PIE_univ_edit"
    keys.append((km, kmi))

    kmi = km.keymap_items.new('mesh.univ_select_linked_pick', 'WHEELUPMOUSE', 'PRESS', shift=True)
    keys.append((km, kmi))

    kmi = km.keymap_items.new('mesh.univ_deselect_linked_pick', 'WHEELDOWNMOUSE', 'PRESS', shift=True)
    keys.append((km, kmi))

    kmi = km.keymap_items.new('mesh.univ_select_linked', 'WHEELUPMOUSE', 'PRESS', ctrl=True, shift=True)
    kmi.properties.select = True
    keys.append((km, kmi))

    kmi = km.keymap_items.new('mesh.univ_select_linked', 'WHEELDOWNMOUSE', 'PRESS', ctrl=True, shift=True)
    kmi.properties.select = False
    keys.append((km, kmi))

    add_mesh_keymaps(km)

    # Window
    km = kc.keymaps.new(name='Window')

    kmi = km.keymap_items.new('wm.univ_split_uv_toggle', 'T', 'PRESS', shift=True)
    kmi.properties.mode = 'SPLIT'
    keys.append((km, kmi))

    kmi = km.keymap_items.new('wm.univ_toggle_panels_by_cursor', 'T', 'PRESS', alt=True)
    keys.append((km, kmi))

    # UV Editor
    km = kc.keymaps.new(name='UV Editor')

    # Pie Menus
    kmi = km.keymap_items.new("wm.call_menu_pie", 'F1', 'PRESS')
    kmi.properties.name = "IMAGE_MT_PIE_univ_inspect"
    keys.append((km, kmi))

    kmi = km.keymap_items.new("wm.call_menu_pie", 'ACCENT_GRAVE', 'PRESS')
    kmi.properties.name = "IMAGE_MT_PIE_univ_edit"
    keys.append((km, kmi))

    kmi = km.keymap_items.new("wm.call_menu_pie", 'X', 'PRESS')
    kmi.properties.name = "IMAGE_MT_PIE_univ_align"
    keys.append((km, kmi))

    kmi = km.keymap_items.new("wm.call_menu_pie", 'D', 'PRESS')
    kmi.properties.name = "IMAGE_MT_PIE_univ_misc"
    keys.append((km, kmi))

    kmi = km.keymap_items.new("wm.call_menu_pie", 'Q', 'PRESS')
    kmi.properties.name = "IMAGE_MT_PIE_univ_favorites_edit"
    keys.append((km, kmi))

    kmi = km.keymap_items.new("wm.call_menu_pie", 'T', 'PRESS')
    kmi.properties.name = "IMAGE_MT_PIE_univ_transform"
    keys.append((km, kmi))

    # Select
    kmi = km.keymap_items.new('uv.univ_select_linked', 'WHEELUPMOUSE', 'PRESS', ctrl=True, shift=True)
    kmi.properties.deselect = False
    keys.append((km, kmi))

    kmi = km.keymap_items.new('uv.univ_select_linked', 'WHEELDOWNMOUSE', 'PRESS', ctrl=True, shift=True)
    kmi.properties.deselect = True
    keys.append((km, kmi))

    kmi = km.keymap_items.new('uv.univ_select_pick', 'WHEELUPMOUSE', 'PRESS', shift=True)
    kmi.properties.select = True
    keys.append((km, kmi))

    kmi = km.keymap_items.new('uv.univ_select_pick', 'WHEELDOWNMOUSE', 'PRESS', shift=True)
    kmi.properties.select = False
    keys.append((km, kmi))

    kmi = km.keymap_items.new('uv.univ_select_grow', 'WHEELUPMOUSE', 'PRESS', ctrl=True)
    kmi.properties.grow = True
    keys.append((km, kmi))

    kmi = km.keymap_items.new('uv.univ_select_grow', 'WHEELDOWNMOUSE', 'PRESS', ctrl=True)
    kmi.properties.grow = False
    keys.append((km, kmi))

    # Edge Grow (Conflict)
    kmi = km.keymap_items.new('uv.univ_select_edge_grow', 'WHEELUPMOUSE', 'PRESS', ctrl=True, alt=True)
    kmi.properties.grow = True
    keys.append((km, kmi))

    kmi = km.keymap_items.new('uv.univ_select_edge_grow', 'WHEELDOWNMOUSE', 'PRESS', ctrl=True, alt=True)
    kmi.properties.grow = False
    keys.append((km, kmi))



    # Flip
    kmi = km.keymap_items.new('uv.univ_flip', 'F', 'PRESS')
    keys.append((km, kmi))

    kmi = km.keymap_items.new('uv.univ_select_mode', 'ONE', 'PRESS')
    kmi.properties.type = 'VERTEX'
    keys.append((km, kmi))

    kmi = km.keymap_items.new('uv.univ_select_mode', 'TWO', 'PRESS')
    kmi.properties.type = 'EDGE'
    keys.append((km, kmi))

    kmi = km.keymap_items.new('uv.univ_select_mode', 'THREE', 'PRESS')
    kmi.properties.type = 'FACE'
    keys.append((km, kmi))

    kmi = km.keymap_items.new('uv.univ_select_mode', 'FOUR', 'PRESS')
    kmi.properties.type = 'ISLAND'
    keys.append((km, kmi))

    # Rotate
    # Default. CW.
    kmi = km.keymap_items.new('uv.univ_rotate', 'FIVE', 'PRESS')
    kmi.properties.rot_dir = 'CW'
    kmi.properties.mode = 'DEFAULT'
    keys.append((km, kmi))

    # Default. CCW.
    kmi = km.keymap_items.new('uv.univ_rotate', 'FIVE', 'PRESS', alt=True)
    kmi.properties.rot_dir = 'CCW'
    kmi.properties.mode = 'DEFAULT'
    keys.append((km, kmi))

    # Default. CW. Individual.
    kmi = km.keymap_items.new('uv.univ_rotate', 'FIVE', 'PRESS', shift=True)
    kmi.properties.rot_dir = 'CW'
    kmi.properties.mode = 'INDIVIDUAL'
    keys.append((km, kmi))

    # Default. CCW. Individual.
    kmi = km.keymap_items.new('uv.univ_rotate', 'FIVE', 'PRESS', shift=True, alt=True)
    kmi.properties.rot_dir = 'CCW'
    kmi.properties.mode = 'INDIVIDUAL'
    keys.append((km, kmi))

    kmi = km.keymap_items.new("wm.call_menu_pie", 'A', 'PRESS', shift=True)
    kmi.properties.name = "IMAGE_MT_PIE_univ_texel"
    keys.append((km, kmi))

    kmi = km.keymap_items.new('uv.univ_home', 'G', 'PRESS', alt=True)
    keys.append((km, kmi))

    # Relax
    kmi = km.keymap_items.new('uv.univ_relax', 'R', 'PRESS', alt=True)
    keys.append((km, kmi))

    # Unwrap
    kmi = km.keymap_items.new('uv.univ_unwrap', 'U', 'PRESS')

    keys.append((km, kmi))

    # Pack
    kmi = km.keymap_items.new('uv.univ_pack', 'P', 'PRESS')
    keys.append((km, kmi))

    # Quadrify
    kmi = km.keymap_items.new('uv.univ_quadrify', 'E', 'PRESS')
    keys.append((km, kmi))

    # Straight
    kmi = km.keymap_items.new('uv.univ_straight', 'E', 'PRESS', shift=True)
    keys.append((km, kmi))

    # Weld
    kmi = km.keymap_items.new('uv.univ_weld', 'W', 'PRESS')
    kmi.properties.use_by_distance = False
    keys.append((km, kmi))

    # Stitch
    kmi = km.keymap_items.new('uv.univ_stitch', 'W', 'PRESS', shift=True)
    keys.append((km, kmi))

    # Quick Snap
    kmi = km.keymap_items.new('uv.univ_quick_snap', 'V', 'PRESS')
    kmi.properties.quick_start = True
    keys.append((km, kmi))

    kmi = km.keymap_items.new('uv.univ_quick_snap', 'V', 'PRESS', alt=True)
    kmi.properties.quick_start = False
    keys.append((km, kmi))

    # Drag


    # Cut
    kmi = km.keymap_items.new('uv.univ_cut', 'C', 'PRESS')
    kmi.properties.addition = False
    keys.append((km, kmi))

    kmi = km.keymap_items.new('uv.univ_cut', 'C', 'PRESS', shift=True)
    kmi.properties.addition = True
    keys.append((km, kmi))

    # Stack
    kmi = km.keymap_items.new('uv.univ_stack', 'S', 'PRESS', alt=True)
    keys.append((km, kmi))

    kmi = km.keymap_items.new('uv.univ_symmetrize', 'X', 'PRESS', alt=True)
    keys.append((km, kmi))

    # Orient
    kmi = km.keymap_items.new('uv.univ_orient', 'O', 'PRESS')
    kmi.properties.edge_dir = 'BOTH'
    keys.append((km, kmi))

    # Stretch Toggle
    kmi = km.keymap_items.new('uv.univ_stretch_uv_toggle', 'Z', 'DOUBLE_CLICK')
    kmi.properties.swap = True
    keys.append((km, kmi))

    kmi = km.keymap_items.new('uv.univ_stretch_uv_toggle', 'Z', 'CLICK')
    kmi.properties.swap = False
    keys.append((km, kmi))

    kmi = km.keymap_items.new('uv.univ_show_modified_uv_edges_toggle', 'Z', 'PRESS', alt=True)
    keys.append((km, kmi))

    # Hide
    kmi = km.keymap_items.new('uv.univ_hide', 'H', 'PRESS')
    kmi.properties.unselected = False
    keys.append((km, kmi))

    kmi = km.keymap_items.new('uv.univ_hide', 'H', 'PRESS', shift=True)
    kmi.properties.unselected = True
    keys.append((km, kmi))

    # Set Cursor 2D
    kmi = km.keymap_items.new('uv.univ_set_cursor_2d', 'MIDDLEMOUSE', 'PRESS', ctrl=True, shift=True)
    keys.append((km, kmi))

    # Focus
    kmi = km.keymap_items.new('uv.univ_focus', 'NUMPAD_PERIOD', 'PRESS')
    keys.append((km, kmi))

    for _, kmi in keys:
        kmi.active = False


def add_keymaps_ws():
    global keys_ws
    kc = bpy.context.window_manager.keyconfigs.addon
    if not kc:
        return  # Can be None in background mode.


    # Workspace keymaps
    def workspace_duplicates(km_ws):
        kmi_ws = km_ws.keymap_items.new("mesh.univ_gravity", 'O', 'PRESS')
        keys_ws.append((km_ws, kmi_ws))

        kmi_ws = km_ws.keymap_items.new("wm.call_menu_pie", 'A', 'PRESS', shift=True)
        kmi_ws.properties.name = "VIEW3D_MT_PIE_univ_texel"
        keys_ws.append((km_ws, kmi_ws))

        kmi_ws = km_ws.keymap_items.new("wm.call_menu_pie", 'Q', 'PRESS', shift=True)
        kmi_ws.properties.name = "VIEW3D_MT_PIE_univ_projection"
        keys_ws.append((km_ws, kmi_ws))

    # Edit Mode
    km = kc.keymaps.new(name='3D View Tool: Edit Mesh, UniV', space_type='VIEW_3D', tool=True)

    ## Rotate
    kmi = km.keymap_items.new('mesh.univ_rotate', 'FIVE', 'PRESS')
    kmi.properties.rot_dir = 'CW'
    kmi.properties.mode = 'DEFAULT'
    keys_ws.append((km, kmi))

    # Default. CW. Individual.
    kmi = km.keymap_items.new('mesh.univ_rotate', 'FIVE', 'PRESS', shift=True)
    kmi.properties.rot_dir = 'CW'
    kmi.properties.mode = 'INDIVIDUAL'
    keys_ws.append((km, kmi))

    # kmi = km.keymap_items.new('uv.univ_flip', 'F', 'PRESS')
    # keys_ws.append((km, kmi))

    kmi = km.keymap_items.new("wm.call_menu_pie", 'D', 'PRESS')
    kmi.properties.name = "VIEW3D_MT_PIE_univ_misc"
    keys_ws.append((km, kmi))

    kmi = km.keymap_items.new("wm.call_menu_pie", 'Q', 'PRESS')
    kmi.properties.name = "VIEW3D_MT_PIE_univ_favorites_edit"
    keys_ws.append((km, kmi))

    kmi = km.keymap_items.new("view3d.select_box", 'LEFTMOUSE', 'CLICK_DRAG')
    keys_ws.append((km, kmi))

    kmi = km.keymap_items.new("view3d.select_box", 'LEFTMOUSE', 'CLICK_DRAG', shift=True)
    kmi.properties.mode = 'ADD'
    keys_ws.append((km, kmi))

    kmi = km.keymap_items.new("view3d.select_box", 'LEFTMOUSE', 'CLICK_DRAG', ctrl=True)
    kmi.properties.mode = 'SUB'
    keys_ws.append((km, kmi))

    kmi = km.keymap_items.new("mesh.univ_cut", 'C', 'PRESS')
    kmi.properties.addition = False
    keys_ws.append((km, kmi))

    kmi = km.keymap_items.new("mesh.univ_cut", 'C', 'PRESS', shift=True)
    kmi.properties.addition = True
    keys_ws.append((km, kmi))

    kmi = km.keymap_items.new("mesh.univ_weld", 'W', 'PRESS')
    kmi.properties.use_by_distance = False
    keys_ws.append((km, kmi))

    kmi = km.keymap_items.new("mesh.univ_stitch", 'W', 'PRESS', shift=True)
    keys_ws.append((km, kmi))

    kmi = km.keymap_items.new("mesh.univ_relax", 'R', 'PRESS', alt=True)
    keys_ws.append((km, kmi))

    kmi = km.keymap_items.new("mesh.univ_unwrap", 'U', 'PRESS')

    keys_ws.append((km, kmi))

    kmi = km.keymap_items.new("mesh.univ_stack", 'S', 'PRESS', alt=True)
    keys_ws.append((km, kmi))

    kmi = km.keymap_items.new("mesh.univ_seam_border", 'B', 'PRESS', alt=True)
    keys_ws.append((km, kmi))

    kmi = km.keymap_items.new("mesh.univ_angle", 'A', 'PRESS', ctrl=True)
    keys_ws.append((km, kmi))



    workspace_duplicates(km)

    # Object Mode
    km = kc.keymaps.new(name='3D View Tool: Object, UniV', space_type='VIEW_3D', tool=True)

    kmi = km.keymap_items.new("view3d.select_box", 'LEFTMOUSE', 'CLICK_DRAG')
    keys_ws.append((km, kmi))

    kmi = km.keymap_items.new("view3d.select_box", 'LEFTMOUSE', 'CLICK_DRAG', shift=True)
    kmi.properties.mode = 'ADD'
    keys_ws.append((km, kmi))

    kmi = km.keymap_items.new("view3d.select_box", 'LEFTMOUSE', 'CLICK_DRAG', ctrl=True)
    kmi.properties.mode = 'SUB'
    keys_ws.append((km, kmi))

    workspace_duplicates(km)


def remove_keymaps():
    global keys
    import traceback
    from .preferences import debug

    for km, kmi in keys:
        try:
            km.keymap_items.remove(kmi)
        except RuntimeError:
            if debug():
                traceback.print_exc()
    keys.clear()


def remove_keymaps_ws():
    global keys_ws
    import traceback
    from .preferences import debug

    for km, kmi in keys_ws:
        try:
            km.keymap_items.remove(kmi)
        except RuntimeError:
            if debug():
                traceback.print_exc()
    keys_ws.clear()
