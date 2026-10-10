# SPDX-FileCopyrightText: 2026 Oxicid
# SPDX-License-Identifier: GPL-3.0-or-later

import gpu
from gpu_extras.batch import batch_for_shader

from . import shaders
from . import mesh_extract
from .text import TextDraw
from ..utypes import UMesh
from ..preferences import prefs
from .lines import LinesDrawSimple, LinesDrawSimple3D, DotLinesDrawSimple


class DrawCallSeams2D:
    def __init__(self, batch: gpu.types.GPUBatch):
        self.shader = shaders.POLYLINE_UNIFORM_COLOR_2D
        self.color = prefs().overlay_2d_uv_edge_seam_color
        self.batch = batch
        # To avoid iterating over all mesh elements again,
        # UniV operators can control Update by adding extended draw elements on top of existing draw elements.
        # self.coords_extend = []
        # self.batch_extend = None

    def __call__(self):
        shaders.blend_set_alpha()
        shaders.depth_test_set_less()
        shaders.set_line_width(4)

        self.shader.bind()
        self.shader.uniform_float("color", self.color)
        shaders.set_line_width_vk(self.shader, 4.0)
        self.batch.draw(self.shader)
        # if self.batch_extend:
        #     self.batch_extend.draw(self.shader)

        shaders.set_line_width(1)
        shaders.depth_test_set_none()
        shaders.blend_set_none()

    @classmethod
    def init(cls, umesh: UMesh) -> 'DrawCallSeams2D | None':
        data = mesh_extract.extract_seams_umesh(umesh)
        if len(data):
            return cls(batch_for_shader(shaders.POLYLINE_UNIFORM_COLOR_2D, 'LINES', {"pos": data}))
        return None

    @staticmethod
    def get_color():
        return prefs().overlay_2d_uv_edge_seam_color

    @staticmethod
    def is_enable():
        return False

