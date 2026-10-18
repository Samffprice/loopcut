"""A stand-in for Blender's imbuf module, for image_budget's rules without Blender.

An "image" is magic bytes (PNG's or JPEG's) and a JSON line: size, whether it has see-through pixels,
whether it is flat (UI, text: small as PNG), and the JPEG quality it was written at; then padding to the
size such a file would have. A photo is ~1.5 bytes a pixel as PNG, quality/400 as JPEG; a flat image is
1/50 as PNG. The real encoder is loopcut/harness/image_budget_check.py's.

    with mock.patch.dict(sys.modules, {"imbuf": fake_imbuf}): ...
"""

import json
from pathlib import Path

PNG, JPEG = b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff"
written: list[str] = []  # File types written, in order.


def make(width: int, height: int, *, kind: str = "PNG", alpha: bool = False, flat: bool = False,
         quality: int | None = None) -> bytes:
    header = json.dumps({"size": [width, height], "alpha": alpha, "flat": flat, "quality": quality}).encode()
    if kind == "JPEG":
        length = width * height * (quality or 85) // 400
    else:
        length = width * height // 50 if flat else width * height * 3 // 2
    head = (JPEG if kind == "JPEG" else PNG) + header + b"\n"
    return head + b"\0" * max(0, length - len(head))


def info(data: bytes) -> dict:
    """What make() said, and the type."""
    for magic, kind in ((PNG, "PNG"), (JPEG, "JPEG")):
        if data.startswith(magic):
            return {**json.loads(data[len(magic):data.index(b"\n", len(magic))]), "type": kind}
    raise ValueError("not a fake image")


class _Buffer:
    def __init__(self, alpha: bool):
        self.shape, self.format = (1, 2, 4), "B"
        self._bytes = bytes([90, 90, 90, 255, 90, 90, 90, 0 if alpha else 255])

    def tobytes(self) -> bytes:
        return self._bytes

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class ImBuf:
    def __init__(self, meta: dict):
        self.size, self.meta = tuple(meta["size"]), meta
        self.planes, self.file_type, self.quality, self.compress, self.freed = 32, meta["type"], 90, 15, False

    def resize(self, size, method="FAST"):
        assert method == "BILINEAR", "a box filter when shrinking"
        self.size = tuple(size)

    def with_buffer(self, write=False):
        return _Buffer(self.meta["alpha"])

    def free(self):
        self.freed = True


def load_from_buffer(buffer) -> ImBuf:
    return ImBuf(info(bytes(buffer)))


def write(image: ImBuf, filepath: str) -> None:
    written.append(image.file_type)
    data = make(*image.size, kind=image.file_type, alpha=image.meta["alpha"], flat=image.meta["flat"],
                quality=image.quality if image.file_type == "JPEG" else None)
    Path(filepath).write_bytes(data)
