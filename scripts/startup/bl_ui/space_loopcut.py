# SPDX-FileCopyrightText: 2026 Loopcut Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

from bpy.types import Header


class LOOPCUT_HT_header(Header):
    bl_space_type = 'LOOPCUT'

    def draw(self, _context):
        # The panel itself is drawn by the `loopcut` add-on. This header is hidden by default and
        # only exists so the editor type can be changed.
        self.layout.template_header()


classes = (
    LOOPCUT_HT_header,
)

if __name__ == "__main__":  # only for live edit.
    from bpy.utils import register_class
    for cls in classes:
        register_class(cls)
