"""Keeping the images a request carries small, for every provider.

Every request re-sends each image it keeps (context.kept_messages; in keep mode every look of the turn)
as base64, so image bytes, not tokens, are what breaks uploads. On 2026-10-17 a ChatGPT conversation whose
tool images were stored as PNGs (0.25-1.2 MB each) failed every try with BrokenPipeError at 6-7 MB a
request; with all of them converted to JPEG it still failed: a pose-matching session that compares every
round kept 42 images, 6.8 MB of base64. api.openai.com had cut 2.5-5 MB uploads about one time in four the
day before. Compact storage alone cannot bound a request, so there are three layers:

1. compact(), as an image is stored (conversations.store_image): at most STORE_SIDE on its long side,
   JPEG at STORE_QUALITY, but PNG when it has transparency or PNG is smaller (flat UI, text). A JPEG
   that fits already is kept byte for byte, so storing a stored image again gives the same file.
2. fit(), as each provider's adapter builds a request (Sized), when its images pass REQUEST_BYTES of
   base64, whatever their number: the newest KEEP_SHARP tool images and the user's own (attachments,
   a clip's sheets) are sent as stored; every older tool image as a THUMBNAIL; past that the oldest are
   left out, OMIT_BATCH at a time, each with a note of what it showed. If that is still too much, the
   rest are sent smaller step by step (SHRINK_STEPS) and then left out, the newest image never. Which
   images are thumbnails or left out follows from their age alone, so the next request, one look longer,
   starts the same and the provider's cache still holds. The stored images stay as they are.
3. Sized.tighten(), when the connection dropped while a request went up: the retry gets half the
   budget, down to MIN_BYTES, rather than the same bytes again.

Encoding is Blender's imbuf, in memory and temporary files: any thread, never bpy.data. Without imbuf
(the Blender-free tests) every image passes unchanged. loopcut/harness/image_budget_check.py runs the
real encoder.
"""

import array
import base64
import hashlib
import os
import tempfile
import threading
from pathlib import Path

from . import context

# Anthropic scales a longer edge down to 1568 px (vision docs); OpenAI's high detail scales the short side
# to 768 px, which 1568 covers up to 2:1. Captures are 640 px, looks and crops 1024; renders and the user's
# 2048 px attached copies (attachments.MAX_SIDE, stored at their own side) are the larger ones.
STORE_SIDE = 1568
STORE_QUALITY = 85     # As the clip sheets (videoref.SHEET_QUALITY): labels stay sharp.
PNG_COMPRESS = 15      # Blender's default for PNG output: fast, and a flat image still comes out small.
# The image base64 in one request. Uploads were cut from 2.5 MB up (2026-10-16) and every time at 6-7 MB
# (2026-10-17); 2 MB of images leaves a long request's text room under the lower figure. Stored compact,
# a request's images are usually well under it: a clip's six sheets (110-180 KB each) are about 1.2 MB.
REQUEST_BYTES = 2_000_000
MIN_BYTES = 500_000    # The least a retry after dropped uploads gets.
KEEP_SHARP = 3         # Tool images sent as stored, the newest: a step's looks (as context.KEEP_IMAGES).
# Older tool images: enough to recall what a look showed. A 512 px JPEG at 60 is ~20-40 KB of base64, so
# the 2 MB holds dozens beside the sharp ones (three renders ~0.4 MB each, six clip sheets ~1.2 MB).
THUMBNAIL = (512, 60)
# Older images are left out this many at a time: each removal changes the start of the request, which the
# provider re-bills from there on, so it happens once every OMIT_BATCH looks rather than on every one.
OMIT_BATCH = 8
# (long side px, JPEG quality) for the sharp images when the rest is not enough, each step about half the
# bytes of the one before. The first costs nothing a model sees: neither provider uses more than 1568 px.
SHRINK_STEPS = ((1568, 80), (1024, 75), (768, 70), (512, 60))
SHRUNK_NOTE = "(This image was sent smaller, at most {side} px on its long side, to keep the request small.)"
LEFT_OUT = "[earlier image omitted to keep the request small: {what}]"
CAPTURE_PREFIXES = (context.CAPTURE_LABEL, context.CAPTURE_TEXT, context.FILE_PREVIEW_LABEL)
_JPEG = b"\xff\xd8\xff"
_CACHE_BYTES = 16_000_000  # Shrunk images kept for the next request, which sends the same ones again.
_cache: dict[tuple, str] = {}
_lock = threading.Lock()


# ------------------------------------------------------------------ encoding

def _imbuf():
    try:
        import imbuf
    except ImportError:
        return None
    return imbuf


def _transparent(image) -> bool:
    """Whether any pixel is see-through, which JPEG would lose. Unsure counts as yes."""
    if image.planes not in (16, 32):  # Grey or RGB with no alpha channel.
        return False
    try:
        with image.with_buffer() as buffer:
            channels, raw = buffer.shape[-1], buffer.tobytes()
            if buffer.format == "B":
                alpha = raw[channels - 1::channels]
                return alpha.count(255) != len(alpha)
            return min(array.array(buffer.format, raw)[channels - 1::channels], default=1.0) < 1.0
    except Exception:
        return True


def _write(imbuf, image, kind: str, quality: int) -> bytes:
    image.file_type = kind
    if kind == "JPEG":
        image.quality = quality
    else:
        image.compress = PNG_COMPRESS
    handle, path = tempfile.mkstemp(suffix=".jpg" if kind == "JPEG" else ".png", prefix="loopcut-image-")
    os.close(handle)
    try:
        imbuf.write(image, filepath=path)  # imbuf.write_to_buffer writes no JPEG.
        return Path(path).read_bytes()
    finally:
        os.unlink(path)


def _smallest(data: bytes, side: int, quality: int, keep_jpeg: bool) -> bytes:
    """data at most `side` px on its long side: JPEG at quality, or PNG for transparency or when smaller; data
    itself when it fits and nothing re-encoded is smaller, or when imbuf cannot read it. keep_jpeg: a JPEG
    that fits is kept as it is."""
    imbuf = _imbuf()
    if imbuf is None:
        return data
    try:
        image = imbuf.load_from_buffer(data)
    except Exception:
        return data
    try:
        width, height = image.size
        fits = max(width, height) <= side
        if fits and keep_jpeg and data.startswith(_JPEG):
            return data
        if not fits:
            scale = side / max(width, height)
            image.resize((max(1, round(width * scale)), max(1, round(height * scale))), method="BILINEAR")
        found = [data] if fits else []
        if _transparent(image):
            found.append(_write(imbuf, image, "PNG", 0))
        else:
            found.append(_write(imbuf, image, "JPEG", quality))
            if not fits:
                found.append(_write(imbuf, image, "PNG", 0))
        return min(found, key=len)  # The first of equals: data, when it is one.
    except Exception as ex:  # A bigger image is never a reason to lose it.
        print(f"Loopcut: could not make an image smaller: {ex!r}")
        return data
    finally:
        image.free()


def compact(data: bytes, side: int = STORE_SIDE) -> bytes:
    """An image as the conversation stores it (see the module's notes)."""
    return _smallest(data, side, STORE_QUALITY, keep_jpeg=True)


def compact_file(path: Path) -> Path:
    """path's image as store_image will keep it, written beside it; path itself when that is the same. A new
    look is compared with the previous one as stored, or JPEG's noise would count as a change."""
    data = path.read_bytes()
    small = compact(data)
    if small == data:
        return path
    out = path.with_name(f"{path.stem}-stored{path.suffix}")
    out.write_bytes(small)
    return out


# ------------------------------------------------------------------ requests

def _inline(part) -> bool:
    if not (isinstance(part, dict) and part.get("type") == "image_url" and isinstance(part.get("image_url"), dict)):
        return False
    url = part["image_url"].get("url")
    return isinstance(url, str) and url.startswith("data:") and ";base64," in url[:64]


def _inline_urls(messages: list) -> dict:
    """{(message index, part index): data: URL} of every image sent inline, in request order."""
    return {(i, j): part["image_url"]["url"] for i, message in enumerate(messages)
            if isinstance(message.get("content"), list) for j, part in enumerate(message["content"]) if _inline(part)}


def _is_capture(message: dict) -> bool:
    """A tool's image (agent._image_message), as opposed to one the user attached."""
    first = (message.get("content") or [{}])[0]
    return (message.get("role") == "user" and isinstance(first, dict) and first.get("type") == "text"
            and str(first.get("text", "")).startswith(CAPTURE_PREFIXES))


def _left_out(message: dict) -> str:
    """The note in place of an image: what its message says it showed."""
    first = str(message["content"][0].get("text", "")) if _is_capture(message) else ""
    what = next((first[len(p):] for p in CAPTURE_PREFIXES if first.startswith(p)), "") or "an image the user attached"
    what = what.strip().rstrip(".")
    return LEFT_OUT.format(what=what if len(what) <= 120 else what[:119] + "…")


def _shrunk(url: str, side: int, quality: int) -> str:
    """The data: URL at most side px at quality, made once."""
    key = (hashlib.sha1(url.encode("ascii", "replace")).digest(), side, quality)
    with _lock:
        if key in _cache:
            return _cache[key]
    try:
        data = base64.b64decode(url.partition(",")[2])
    except ValueError:
        return url
    small = _smallest(data, side, quality, keep_jpeg=False)
    if small is not data:
        from . import conversations
        url = f"data:{conversations.media_type(small)};base64,{base64.b64encode(small).decode('ascii')}"
    with _lock:
        _cache[key] = url
        while len(_cache) > 1 and sum(map(len, _cache.values())) > _CACHE_BYTES:
            _cache.pop(next(iter(_cache)))
    return url


def fit(messages: list, budget: int = REQUEST_BYTES) -> list:
    """messages with their inline images' base64 at most budget, as far as thumbnails and then leaving out
    images gets (the newest is never left out; see the module's notes). The same list when it fits already;
    never changed in place."""
    sent = _inline_urls(messages)
    before = total = sum(map(len, sent.values()))
    if total <= budget:
        return messages
    notes = {}

    def change(key, url, note):
        nonlocal total
        total += len(url or "") - len(sent[key])
        sent[key], notes[key] = url, note

    tools = [key for key in sent if _is_capture(messages[key[0]])]
    older = tools[:-KEEP_SHARP]
    for key in older:
        small = _shrunk(sent[key], *THUMBNAIL)
        if len(small) < len(sent[key]):
            change(key, small, SHRUNK_NOTE.format(side=THUMBNAIL[0]))
    if total > budget and older:
        over, count = total - budget, 0
        while count < len(older) and over > 0:
            over -= len(sent[older[count]])
            count += 1
        for key in older[:min(len(older), -(-count // OMIT_BATCH) * OMIT_BATCH)]:
            change(key, None, _left_out(messages[key[0]]))
    # Still too much: the sharp tool images oldest first, then the user's, the newest image last.
    rest = [key for key in list(sent)[:-1] if key not in notes]
    order = sorted(rest, key=lambda key: not _is_capture(messages[key[0]])) + list(sent)[-1:]
    for key in order:
        original = sent[key]
        for side, quality in SHRINK_STEPS:
            if total <= budget:
                break
            smaller = _shrunk(original, side, quality)
            if len(smaller) < len(sent[key]):
                change(key, smaller, SHRUNK_NOTE.format(side=side))
    for key in order[:-1]:
        if total <= budget:
            break
        change(key, None, _left_out(messages[key[0]]))
    if not notes:
        return messages
    print(f"Loopcut: the request's images were {before // 1000} KB, over its {budget // 1000} KB; "
          f"{sum(v is not None for k, v in sent.items() if k in notes)} sent smaller, "
          f"{sum(v is None for v in sent.values())} left out: {total // 1000} KB")
    out = list(messages)
    for i in sorted({i for i, _ in notes}):
        parts = []
        for j, part in enumerate(messages[i]["content"]):
            if (i, j) not in notes:
                parts.append(part)
            elif sent[i, j] is None:
                parts.append({"type": "text", "text": notes[i, j]})
            else:
                parts += [{**part, "image_url": {**part["image_url"], "url": sent[i, j]}},
                          {"type": "text", "text": notes[i, j]}]
        out[i] = {**messages[i], "content": parts}
    return out


class Sized:
    """One request's messages fitted to its budget: REQUEST_BYTES, halved by each tighten() down to MIN_BYTES,
    so a retry after a dropped upload sends less than the try that was cut off."""

    def __init__(self, messages: list):
        self.source, self.budget, self._fitted = messages, REQUEST_BYTES, None

    def messages(self) -> list:
        if self._fitted is None:
            self._fitted = fit(self.source, self.budget)
        return self._fitted

    def tighten(self) -> bool:
        """After the connection dropped as the request went up, or a 413. False when the next try would send
        the same: the budget is the least already, or the images are under the smaller one."""
        smaller = max(MIN_BYTES, self.budget // 2)
        if self.budget <= MIN_BYTES or sum(map(len, _inline_urls(self.messages()).values())) <= smaller:
            return False
        self.budget, self._fitted = smaller, None
        return True
