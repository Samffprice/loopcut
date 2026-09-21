"""Renders a layout display list with Blender's gpu and blf modules."""

from pathlib import Path

import blf
import bpy
import gpu
from gpu_extras.batch import batch_for_shader

_VERTEX = """
void main()
{
  /* One pixel of slack around the rect so the antialiased edge is not cut off. */
  vec2 p = rect.xy - vec2(1.0) + pos * (rect.zw + vec2(2.0));
  local = p - (rect.xy + rect.zw * 0.5);
  gl_Position = ModelViewProjectionMatrix * vec4(p, 0.0, 1.0);
}
"""

_FRAGMENT = """
void main()
{
  vec2 half_size = rect.zw * 0.5;
  if (shape.z > 0.5) {
    /* Ring: a track in border_color, filled clockwise from the top for shape.w of the way in color. */
    float radius = min(half_size.x, half_size.y);
    float band = abs(length(local) - (radius - shape.y * 0.5)) - shape.y * 0.5;
    float coverage = 1.0 - smoothstep(-0.5, 0.5, band);
    float angle = atan(local.x, local.y);
    if (angle < 0.0) angle += 6.2831853;
    float fill = shape.w >= 1.0 ? 1.0 : smoothstep(-0.5, 0.5, (shape.w * 6.2831853 - angle) * radius);
    vec4 ring = mix(border_color, color, fill);
    fragColor = vec4(ring.rgb, ring.a * coverage);
    return;
  }
  float r = min(shape.x, min(half_size.x, half_size.y));
  vec2 q = abs(local) - half_size + vec2(r);
  float dist = length(max(q, vec2(0.0))) + min(max(q.x, q.y), 0.0) - r;
  float coverage = 1.0 - smoothstep(-0.5, 0.5, dist);
  float inside = 1.0 - smoothstep(-0.5, 0.5, dist + shape.y);
  vec4 col = mix(border_color, color, shape.y > 0.0 ? inside : 1.0);
  fragColor = vec4(col.rgb, col.a * coverage);
}
"""

_cache: dict = {}
_measure_cache: dict = {}
_MEASURE_CACHE_LIMIT = 20000


def _rect_shader():
    if "shader" not in _cache:
        info = gpu.types.GPUShaderCreateInfo()
        info.push_constant("MAT4", "ModelViewProjectionMatrix")
        info.push_constant("VEC4", "rect")
        info.push_constant("VEC4", "color")
        info.push_constant("VEC4", "border_color")
        info.push_constant("VEC4", "shape")  # x: corner radius, y: border or ring width, z: 1 for a ring, w: ring share
        info.vertex_in(0, "VEC2", "pos")
        interface = gpu.types.GPUStageInterfaceInfo("loopcut_rect_iface")
        interface.smooth("VEC2", "local")
        info.vertex_out(interface)
        info.fragment_out(0, "VEC4", "fragColor")
        info.vertex_source(_VERTEX)
        info.fragment_source(_FRAGMENT)
        shader = gpu.shader.create_from_info(info)
        _cache["shader"] = shader
        _cache["batch"] = batch_for_shader(
            shader, "TRIS", {"pos": ((0, 0), (1, 0), (1, 1), (0, 1))}, indices=((0, 1, 2), (0, 2, 3)))
    return _cache["shader"], _cache["batch"]


def font_id(font: str) -> int:
    if font == "ui":
        return 0
    if "mono" not in _cache:
        path = Path(bpy.utils.system_resource("DATAFILES")) / "fonts" / "DejaVuSansMono.woff2"
        loaded = blf.load(str(path)) if path.is_file() else -1
        if loaded == -1:
            print(f"Loopcut: monospace font not found at {path}; code will use the UI font")
            loaded = 0
        _cache["mono"] = loaded
    return _cache["mono"]


def measure(font: str, size: int, text: str) -> float:
    key = (font, size, text)
    width = _measure_cache.get(key)
    if width is None:
        if len(_measure_cache) > _MEASURE_CACHE_LIMIT:
            _measure_cache.clear()
        fid = font_id(font)
        blf.size(fid, size)
        width = _measure_cache[key] = blf.dimensions(fid, text)[0]
    return width


def _mark_texture(size: int):
    """The Loopcut mark at exactly this many pixels, antialiased by brand.square rather than by the GPU."""
    key = ("mark", size)
    if key not in _cache:
        from . import brand
        pixels = brand.square(size, margin=0.0)[::-1]  # Textures start at the bottom row.
        pixels[..., :3] *= pixels[..., 3:]  # Premultiplied, which is what the image shader blends.
        buffer = gpu.types.Buffer("FLOAT", size * size * 4, pixels.astype("float32").ravel())
        _cache[key] = gpu.types.GPUTexture((size, size), format="RGBA8", data=buffer)
    return _cache[key]


def _draw_mark(prim: dict, height: int) -> None:
    from gpu_extras.presets import draw_texture_2d
    gpu.state.blend_set("ALPHA_PREMULT")
    draw_texture_2d(_mark_texture(prim["w"]), (prim["x"], height - prim["y"] - prim["h"]), prim["w"], prim["h"])


def render(display: dict) -> None:
    height = display["height"]
    shader, batch = _rect_shader()
    matrix = gpu.matrix.get_projection_matrix() @ gpu.matrix.get_model_view_matrix()
    for prim in display["prims"]:
        if prim["t"] == "rect":
            # Per rect, not once up front: blf.draw leaves blending switched off behind it.
            gpu.state.blend_set("ALPHA")
            shader.bind()
            shader.uniform_float("ModelViewProjectionMatrix", matrix)
            shader.uniform_float("rect", (prim["x"], height - prim["y"] - prim["h"], prim["w"], prim["h"]))
            shader.uniform_float("color", prim["color"])
            shader.uniform_float("border_color", prim["border_color"])
            shader.uniform_float("shape", (prim["radius"], prim["border"], 0.0, 0.0))
            batch.draw(shader)
        elif prim["t"] == "ring":
            gpu.state.blend_set("ALPHA")
            shader.bind()
            shader.uniform_float("ModelViewProjectionMatrix", matrix)
            shader.uniform_float("rect", (prim["x"], height - prim["y"] - prim["h"], prim["w"], prim["h"]))
            shader.uniform_float("color", prim["color"])
            shader.uniform_float("border_color", prim["track"])
            shader.uniform_float("shape", (0.0, prim["thickness"], 1.0, prim["share"]))
            batch.draw(shader)
        elif prim["t"] == "mark":
            _draw_mark(prim, height)
        else:
            fid, size = font_id(prim["font"]), prim["size"]
            blf.size(fid, size)
            blf.color(fid, *prim["color"])
            # Baseline: centre the font's cap height inside the line box.
            baseline = height - prim["y"] - prim["h"] / 2 - size * 0.36
            blf.position(fid, prim["x"], round(baseline), 0)
            blf.draw(fid, prim["text"])
    gpu.state.blend_set("NONE")
