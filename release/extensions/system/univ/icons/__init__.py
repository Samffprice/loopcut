# SPDX-FileCopyrightText: 2026 Oxicid
# SPDX-License-Identifier: GPL-3.0-or-later

# The icons were created by Vitaly Zhdanov , for which he is very thankful!
# His work gave the project a finished and professional look.
# Excellent detailing and stylish design made the interface more convenient and pleasant.
# Thank you for the work done!

import bpy
import traceback
from pathlib import Path


class icons:
    """
    Interface for accessing icons.

    NOTE: Icon names must match the names of the corresponding PNG files.
    NOTE: For properties, methods, and other attributes that don’t contain an icon value, names must start with an underscore.
    """
    _icons_ = None
    adjust = 0
    area = 0
    arrow = 0
    arrow_bottom = 0
    arrow_bottom_left = 0
    arrow_bottom_right = 0
    arrow_left = 0
    arrow_right = 0
    arrow_top = 0
    arrow_top_left = 0
    arrow_top_right = 0
    align_border_verts = 0
    border = 0
    border_by_angle = 0
    border_seam = 0
    box = 0
    center = 0
    checker = 0
    coverage = 0
    crop = 0
    cursor = 0
    cut = 0
    distribute = 0
    edge_grow = 0
    fill = 0
    flat = 0
    flatten = 0
    flip = 0
    flipped = 0
    gravity = 0
    grow = 0
    home = 0
    horizontal_a = 0
    horizontal_c = 0
    large = 0
    linked = 0
    loop_select = 0
    medium = 0
    non_splitted = 0
    normal = 0
    normalize = 0
    orient = 0
    over = 0
    overlap = 0
    pack = 0
    pack_others = 0
    pin = 0
    quadrify = 0
    random = 0
    rectify = 0
    relax = 0
    remove = 0
    reset = 0
    rotate = 0
    select_stacked = 0
    settings_a = 0
    settings_b = 0
    shift = 0
    small = 0
    smart = 0
    sort = 0
    square = 0
    stack = 0
    stitch = 0
    straight = 0
    symmetrize = 0
    td_get = 0
    td_set = 0
    transfer = 0
    unwrap = 0
    vertical_a = 0
    vertical_b = 0
    view = 0
    weld = 0
    x = 0
    y = 0
    zero = 0

    @classmethod
    def register_icons_(cls):

        from .. import utils
        from ..preferences import prefs
        from bpy.utils import previews
        if cls._icons_:
            cls.unregister_icons_()
        cls._icons_ = previews.new()

        is_mono = prefs().color_mode == 'MONO'
        addon_icons = Path(__file__).parent / ("png_mono" if is_mono else "png")
        extension_icons = None

        extension_path = utils.extension_path_user(utils.univ_root_path, path=f"icons/{addon_icons.name}", create=False)
        if extension_path:
            extension_icons = Path(extension_path)

        for attr in dir(cls):
            if attr.endswith("_"):
                continue

            if not isinstance(getattr(cls, attr), int):
                print(f"UniV: Attribute '{attr}' is not an icon id")
                continue

            candidates = []
            if extension_icons:
                candidates.append(extension_icons / f"{attr}.png")
            candidates.append(addon_icons / f"{attr}.png")

            for full_path in candidates:
                if full_path.exists():
                    break
            else:
                print(f"UniV: Icon '{attr}' not found")
                continue

            icon = cls._icons_.load(attr, str(full_path), "IMAGE")
            # Force Blender to load the icon immediately (workaround for Blender bug)
            _ = icon.icon_pixels[0]
            setattr(cls, attr, icon.icon_id)


        # Register category icons
        cls.update_general_panels_icon_()

    @staticmethod
    def register_ws_icons_():
        from .. import ui
        from .. import utils
        from ..preferences import prefs

        # Default icon path inside the add-on
        is_mono = prefs().color_mode == 'MONO'
        expected_icon = "univ_mono" if is_mono else "univ"
        expected_path = Path(__file__).with_name(expected_icon)

        # Use the extension icon if it exists
        extension_icons = utils.extension_path_user(utils.univ_root_path, path="icons", create=False)

        if extension_icons:
            extension_icon = Path(extension_icons) / f"{expected_icon}.dat"
            if extension_icon.exists():
                expected_path = extension_icon.with_suffix("")

        # Skip updating if every workspace already uses the expected icon
        panels = (ui.UNIV_WT_edit_VIEW3D, ui.UNIV_WT_object_VIEW3D)
        if any(Path(panel.bl_icon) != expected_path for panel in panels):
            from .. import keymaps
            keymaps.remove_keymaps_ws()

            for panel in panels:
                try:
                    bpy.utils.unregister_tool(panel)
                    panel.bl_icon = str(expected_path)
                    bpy.utils.register_tool(panel)
                except Exception:  # noqa
                    print(f"UniV: Failed to update workspace icon for {panel.__name__}")
                    traceback.print_exc()
            keymaps.add_keymaps_ws()

    @classmethod
    def reset_icon_value_(cls):
        for attr in dir(cls):
            if not attr.endswith('_'):
                setattr(cls, attr, 0)

    @classmethod
    def unregister_icons_(cls):
        from bpy.utils import previews
        try:
            previews.remove(cls._icons_)
        except KeyError:
            from ..preferences import debug
            if debug():
                print("UniV: Can't unregister icons.")
                traceback.print_exc()

        cls.reset_icon_value_()

    @classmethod
    def update_general_panels_icon_(cls):
        if bpy.app.version >= (5, 2, 0):
            # Set icons to general panel.
            try:
                from .. import classes
                # For the property to apply to the panel, it must be applied to all panels in use.
                all_panels = [c for c in classes if issubclass(c, bpy.types.Panel)]
                if not len(all_panels):
                    print("UniV: Panels: N-Panel not found to set icon in compact mode.")
                else:
                    for panel in all_panels:
                        if "bl_rna" in panel.__dict__:
                            bpy.utils.unregister_class(panel)
                        panel.bl_icon_value = cls.unwrap
                        bpy.utils.register_class(panel)
            except:  # noqa
                print("UniV: Can't set icons to compact panel.")
                traceback.print_exc()