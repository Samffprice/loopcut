"""image_budget.py with Blender's own imbuf: what tests/test_image_budget.py fakes.

    Blender -b --factory-startup --python loopcut/harness/image_budget_check.py

Light (a few seconds, one Blender, no model or network), but Blender: run it under the heavy lock.
The images are made here: a photographic one (smooth light and fine texture, the way renders and
captures look), a flat one (UI, text) and a see-through one.

1. A 1920x1080 photographic PNG stored as a tool image is a JPEG, 1568 px on its long side, a fraction of
   the PNG's bytes, also when stored from another thread (the agent loop's); its card's path loads with
   bpy.data.images.load at that size (ui/draw.py's preview) and leaves no datablock behind.
2. The flat image stays the PNG it was; the see-through one stays PNG; storing a stored image again gives
   the same reference. A 640x360 capture's stored size is printed.
3. A photo the user attaches is stored as JPEG at attachments.MAX_SIDE and SEND_SIDE.
4. Eight stored looks and four renders the user attached in one request: fit() brings their base64
   under REQUEST_BYTES, the newest as it was, every image sent a readable JPEG; Sized.tighten() says
   whether a retry would send less, and then halves what is sent.
Prints IMAGE BUDGET CHECK OK at the end; exits 1 on the first failure.
"""

import base64
import os
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

import bpy
import imbuf
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import checkout  # noqa: E402
checkout.use()
os.environ["LOOPCUT_DATA_DIR"] = tempfile.mkdtemp(prefix="loopcut-harness-")

from loopcut import attachments, conversations, image_budget, state  # noqa: E402


def step(text: str) -> None:
    print(f"ok  {text}", flush=True)


def write(pixels, path: Path, kind: str = "PNG") -> Path:
    """RGBA uint8 pixels, rows bottom-up as imbuf keeps them; PNG at Blender's default 15% compression."""
    height, width = pixels.shape[:2]
    image = imbuf.new((width, height))
    try:
        with image.with_buffer(write=True) as buffer:
            np.asarray(buffer)[:] = pixels
        image.file_type, image.compress, image.quality = kind, 15, 90
        imbuf.write(image, filepath=str(path))
    finally:
        image.free()
    return path


def photo(width: int, height: int, seed: int = 1):
    """Light falling across a surface with a fine texture: incompressible enough to be a render."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:height, 0:width].astype(np.float32)
    light = 0.5 + 0.4 * np.sin(x / width * 3.1 + seed) * np.cos(y / height * 2.3)
    grain = rng.normal(0, 0.02, (height, width)).astype(np.float32)
    rgb = np.stack([light + grain, light * 0.8 + grain, light * 0.6 + grain], axis=-1)
    pixels = np.empty((height, width, 4), np.uint8)
    pixels[..., :3] = np.clip(rgb * 255, 0, 255)
    pixels[..., 3] = 255
    return pixels


def size_of(data: bytes) -> tuple[int, int]:
    image = imbuf.load_from_buffer(data)
    try:
        return tuple(image.size)
    finally:
        image.free()


def main() -> None:
    started = time.monotonic()
    work = Path(tempfile.mkdtemp(prefix="loopcut-image-budget-"))
    session = state.new_session()

    source = write(photo(1920, 1080), work / "render.png")
    card = {}
    began = time.monotonic()
    ref = conversations.store_image(session, source, card)
    took = time.monotonic() - began
    stored = conversations.image_path(session["id"], ref).read_bytes()
    assert ref.endswith(".png") and conversations.media_type(stored) == "image/jpeg", (ref, stored[:4])
    assert size_of(stored) == (image_budget.STORE_SIDE, 882), size_of(stored)
    assert len(stored) < source.stat().st_size / 4, (len(stored), source.stat().st_size)
    step(f"1920x1080 PNG of {source.stat().st_size // 1000} KB stored as a 1568x882 JPEG of {len(stored) // 1000} KB "
         f"in {took * 1000:.0f} ms")

    before = set(bpy.data.images.keys())
    preview = bpy.data.images.load(card["image_path"], check_existing=False)
    try:
        assert tuple(preview.size) == (image_budget.STORE_SIDE, 882), tuple(preview.size)
        if not bpy.app.background:
            import gpu
            gpu.texture.from_image(preview)
    finally:
        bpy.data.images.remove(preview)
    assert set(bpy.data.images.keys()) == before
    step(f"the card's {Path(card['image_path']).name[:12]}….png (JPEG inside) loads at {image_budget.STORE_SIDE}x882")

    outcome = {}
    other = write(photo(1920, 1080, seed=2), work / "other.png")
    thread = threading.Thread(target=lambda: outcome.update(ref=conversations.store_image(session, other)))
    thread.start()
    thread.join(60)
    threaded = conversations.image_path(session["id"], outcome["ref"]).read_bytes()
    assert conversations.media_type(threaded) == "image/jpeg"
    step("stored from another thread the same way")

    capture = write(photo(640, 360, seed=3), work / "capture.png")
    small = conversations.image_path(session["id"], conversations.store_image(session, capture)).read_bytes()
    assert conversations.media_type(small) == "image/jpeg" and size_of(small) == (640, 360)
    step(f"a 640x360 capture: PNG {capture.stat().st_size // 1000} KB, stored {len(small) // 1000} KB")

    flat_pixels = np.full((600, 800, 4), (238, 238, 238, 255), np.uint8)
    flat_pixels[100:140, 50:700] = (30, 30, 30, 255)
    flat_pixels[200:400, 300:500] = (60, 120, 200, 255)
    flat = write(flat_pixels, work / "panel.png")
    kept = conversations.image_path(session["id"], conversations.store_image(session, flat)).read_bytes()
    assert kept == flat.read_bytes(), "a flat image is smaller as its PNG"
    clear = photo(800, 600, seed=4)
    clear[:100, :, 3] = 0
    see_through = write(clear, work / "cutout.png")
    alpha = conversations.image_path(session["id"], conversations.store_image(session, see_through)).read_bytes()
    assert conversations.media_type(alpha) == "image/png"
    again = conversations.store_image(session, conversations.image_path(session["id"], ref))
    assert again == ref, "storing a stored image again"
    step(f"flat stays PNG ({len(kept) // 1000} KB), see-through stays PNG, a stored image stored again is the same file")

    attached = write(photo(2400, 1600, seed=5), work / "holiday.jpg", "JPEG")
    assert attachments.add(session, [str(attached)]) == []
    entry = session["attachments"][-1]
    for key, side in (("full", attachments.MAX_SIDE), ("ref", attachments.SEND_SIDE)):
        data = conversations.image_path(session["id"], entry[key]).read_bytes()
        assert conversations.media_type(data) == "image/jpeg" and max(size_of(data)) == side, (key, size_of(data))
    step("an attached photo is stored as JPEG at 2048 and 768 px")

    refs = [conversations.store_image(session, write(photo(1568, 882, seed=10 + n), work / f"look{n}.png"))
            for n in range(8)]
    inline = [conversations.image_path(session["id"], r).read_bytes() for r in refs]  # As wire_messages sends them.
    url_of = lambda d: f"data:{conversations.media_type(d)};base64,{base64.b64encode(d).decode()}"
    # Four the user attached (they stay as stored), then the eight looks: still heavy once the older looks are thumbnails.
    messages = [{"role": "user", "content": [{"type": "text", "text": "like these"},
                                             *({"type": "image_url", "image_url": {"url": url_of(d)}} for d in inline[:4])]},
                *({"role": "user", "content": [{"type": "text", "text": f"Viewport capture: look {n}."},
                                               {"type": "image_url", "image_url": {"url": url_of(d)}}]}
                  for n, d in enumerate(inline))]
    urls = lambda ms: [p["image_url"]["url"] for m in ms for p in m["content"] if p.get("type") == "image_url"]
    total = sum(map(len, urls(messages)))
    sized = image_budget.Sized(messages)
    fitted = urls(sized.messages())
    sent = sum(map(len, fitted))
    print(f"    {len(urls(messages))} images, {total // 1000} KB of base64; fitted {len(fitted)}, {sent // 1000} KB",
          flush=True)
    assert total > image_budget.REQUEST_BYTES, total
    assert sent <= image_budget.REQUEST_BYTES and fitted[-1] == urls(messages)[-1]
    for url in fitted:
        data = base64.b64decode(url.partition(",")[2])
        assert conversations.media_type(data) == "image/jpeg" and max(size_of(data)) <= image_budget.STORE_SIDE
    assert sized.tighten() == (sent > image_budget.REQUEST_BYTES // 2), "tighten says whether a retry sends less"
    tighter = sum(map(len, urls(sized.messages())))
    assert tighter <= image_budget.REQUEST_BYTES // 2 and tighter < sent, (tighter, sent)
    step(f"twelve stored renders, {total // 1000} KB of base64: fitted to {sent // 1000} KB, "
         f"after a dropped upload {tighter // 1000} KB")
    print(f"IMAGE BUDGET CHECK OK in {time.monotonic() - started:.1f} s", flush=True)


try:
    main()
except Exception:
    traceback.print_exc()
    sys.stderr.flush()
    os._exit(1)
