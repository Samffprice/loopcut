"""Images the user attaches to a message: dropped on the panel or picked with the file browser.

Every file is decoded by Blender and re-saved as a PNG no larger than MAX_SIDE, so what reaches
the conversation folder (and the model) is a real image of bounded size whatever was dropped.
Main thread only: it goes through bpy.data.
"""

import os
import tempfile
from pathlib import Path

import bpy

from . import conversations

EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".tga")
MAX_PER_MESSAGE = 6
MAX_SIDE = 1568            # The stored copy; look_at_reference reads details from it.
SEND_SIDE = 768            # The copy in every request: a pinned reference must stay cheap.
MAX_FILE_BYTES = 100_000_000


class AttachmentError(Exception):
    pass


def _to_png(source: Path, target: Path, max_side: int = MAX_SIDE) -> tuple[int, int]:
    """Returns the source image's size."""
    try:
        image = bpy.data.images.load(str(source), check_existing=False)
    except RuntimeError as ex:
        raise AttachmentError(f"{source.name} is not an image Blender can read") from ex
    try:
        width, height = image.size
        if width == 0 or height == 0:
            raise AttachmentError(f"{source.name} is not an image Blender can read")
        longest = max(width, height)
        if longest > max_side:
            image.scale(max(1, round(width * max_side / longest)), max(1, round(height * max_side / longest)))
        image.filepath_raw = str(target)
        image.file_format = "PNG"
        image.save()
        return width, height
    except RuntimeError as ex:
        raise AttachmentError(f"Could not convert {source.name}: {ex}") from ex
    finally:
        bpy.data.images.remove(image)  # The user's file must not gain an image datablock.


def add(session: dict, paths: list[str]) -> list[str]:
    """Attach what can be attached; returns one line per file that could not be."""
    problems = []
    for raw in paths:
        source = Path(raw)
        if len(session["attachments"]) >= MAX_PER_MESSAGE:
            problems.append(f"Only {MAX_PER_MESSAGE} images fit in one message; the rest were left out.")
            break
        if source.suffix.lower() not in EXTENSIONS:
            problems.append(f"{source.name} is not an image (png, jpg, webp, bmp, tif, tga).")
            continue
        try:
            if not source.is_file() or source.stat().st_size > MAX_FILE_BYTES:
                problems.append(f"{source.name} is missing or larger than {MAX_FILE_BYTES // 1_000_000} MB.")
                continue
            handle, temporary = tempfile.mkstemp(suffix=".png", prefix="loopcut-attach-")
            os.close(handle)
            try:
                stored = []
                for side in (MAX_SIDE, SEND_SIDE):
                    _to_png(source, Path(temporary), side)
                    stored.append(conversations.store_image(session, Path(temporary)))
            finally:
                os.unlink(temporary)
        except (AttachmentError, OSError) as ex:
            problems.append(str(ex))
            continue
        full, small = stored
        if all(a["ref"] != small for a in session["attachments"]):
            session["attachments"].append({"ref": small, "full": full, "name": source.name})
    return problems
