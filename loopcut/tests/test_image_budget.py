"""image_budget.py's rules without Blender: images stored compact, a request's images under its budget
(oldest tool images shrunk first, the newest kept sharpest, nothing left out without a note), a smaller
budget after a dropped upload. imbuf is tests/fake_imbuf.py here; the real encoder, and a card loading
its stored JPEG, are loopcut/harness/image_budget_check.py.

    python3 -m unittest discover -s loopcut/tests
"""

import base64
import copy
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.modules.setdefault("bpy", types.ModuleType("bpy"))

import fake_imbuf  # noqa: E402
from loopcut import conversations as cv  # noqa: E402
from loopcut import image_budget as ib  # noqa: E402
from loopcut import state  # noqa: E402


def data_url(data: bytes) -> str:
    return f"data:{cv.media_type(data)};base64,{base64.b64encode(data).decode()}"


def capture(data: bytes, what: str = "the desk") -> dict:
    return {"role": "user", "content": [{"type": "text", "text": f"{cv.context.CAPTURE_LABEL}{what}."},
                                        {"type": "image_url", "image_url": {"url": data_url(data)}}]}


def attached(data: bytes) -> dict:
    return {"role": "user", "content": [{"type": "text", "text": "make it look like this"},
                                        {"type": "image_url", "image_url": {"url": data_url(data)}}]}


def urls(messages: list) -> list[str]:
    return [p["image_url"]["url"] for m in messages if isinstance(m.get("content"), list)
            for p in m["content"] if p.get("type") == "image_url"]


def texts(messages: list) -> list[str]:
    return [p["text"] for m in messages if isinstance(m.get("content"), list)
            for p in m["content"] if p.get("type") == "text"]


PHOTO = fake_imbuf.make(1568, 882, kind="JPEG", quality=85)  # A stored render: ~290 KB, ~390 KB as base64.


class Fake(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(sys.modules, {"imbuf": fake_imbuf})
        patcher.start()
        self.addCleanup(patcher.stop)
        fake_imbuf.written.clear()
        ib._cache.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"LOOPCUT_DATA_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)


class StoreTest(Fake):
    def store(self, data: bytes, **kwargs) -> tuple[str, bytes, dict]:
        session, card = state.new_session(), {}
        source = Path(self.tmp.name) / "capture.png"
        source.write_bytes(data)
        ref = cv.store_image(session, source, card, **kwargs)
        stored = cv.image_path(session["id"], ref).read_bytes()
        return ref, stored, card

    def test_a_large_png_capture_is_stored_as_a_small_jpeg_its_card_still_shows(self):
        # 2026-10-17: tool images stored as PNGs of 0.25-1.2 MB; six in one request and ChatGPT cut the upload.
        source = fake_imbuf.make(1920, 1080)
        ref, stored, card = self.store(source)
        self.assertTrue(ref.endswith(".png"), "stored names keep their form")
        self.assertEqual(cv.media_type(stored), "image/jpeg")
        self.assertEqual(fake_imbuf.info(stored)["size"], [ib.STORE_SIDE, 882])
        self.assertEqual(fake_imbuf.info(stored)["quality"], ib.STORE_QUALITY)
        self.assertLess(len(stored), len(source) / 8)
        # The card shows the stored file: Blender reads its JPEG whatever the name, and a reload keeps the card.
        self.assertEqual(Path(card["image_path"]).read_bytes(), stored)
        self.assertTrue(cv._own_image_path(card["image_path"]))

    def test_see_through_and_flat_images_stay_png(self):
        _, stored, _ = self.store(fake_imbuf.make(800, 600, alpha=True))
        self.assertEqual(fake_imbuf.info(stored)["type"], "PNG", "JPEG would lose the transparency")
        flat = fake_imbuf.make(800, 600, flat=True)
        self.assertEqual(self.store(flat)[1], flat, "a UI capture is smaller as the PNG it was")

    def test_storing_a_stored_image_again_gives_the_same_file(self):
        # waves.attach_references stores the user's stored copy into a worker's conversation, by its ref.
        first, stored, _ = self.store(fake_imbuf.make(1920, 1080))
        written = len(fake_imbuf.written)
        self.assertEqual(self.store(stored)[0], first)
        self.assertEqual(len(fake_imbuf.written), written, "a JPEG that fits is not encoded again")

    def test_an_attached_copy_keeps_its_own_side(self):
        _, stored, _ = self.store(fake_imbuf.make(2048, 1536), side=2048)
        self.assertEqual(fake_imbuf.info(stored)["size"], [2048, 1536])
        self.assertEqual(fake_imbuf.info(stored)["type"], "JPEG")

    def test_without_imbuf_or_on_bytes_it_cannot_read_the_image_is_stored_as_it_was(self):
        self.assertEqual(self.store(b"made up")[1], b"made up")
        with mock.patch.dict(sys.modules, {"imbuf": None}):  # import imbuf raises ImportError.
            source = fake_imbuf.make(1920, 1080)
            self.assertEqual(self.store(source)[1], source)

    def test_a_new_look_is_compared_with_the_last_one_as_stored(self):
        shot = Path(self.tmp.name) / "capture.png"
        shot.write_bytes(fake_imbuf.make(640, 360))
        compared = ib.compact_file(shot)
        self.assertNotEqual(compared, shot)
        self.assertEqual(cv.media_type(compared.read_bytes()), "image/jpeg")
        self.assertEqual(ib.compact_file(compared), compared, "already as stored")


def omitted(messages: list) -> list[str]:
    return [t for t in texts(messages) if t.startswith("[earlier image omitted")]


def shrunk(messages: list) -> list[str]:
    return [t for t in texts(messages) if t.startswith("(This image was sent smaller")]


def side_of(url: str) -> int:
    return max(fake_imbuf.info(base64.b64decode(url.partition(",")[2]))["size"])


class FitTest(Fake):
    def conversation(self, looks: int = 5) -> list:
        return [{"role": "system", "content": "You are Loopcut."}, attached(PHOTO),
                *(capture(PHOTO, f"three_quarter view of the chair, look {n}") for n in range(looks))]

    def test_a_request_within_its_budget_is_sent_as_it_is(self):
        messages = self.conversation()
        self.assertIs(ib.fit(messages, budget=10_000_000), messages)
        self.assertEqual(fake_imbuf.written, [])

    def test_over_budget_older_looks_are_thumbnails_the_newest_and_the_users_stay_sharp(self):
        messages = self.conversation()
        before = copy.deepcopy(messages)
        self.assertGreater(sum(map(len, urls(messages))), ib.REQUEST_BYTES)
        fitted = ib.fit(messages)
        self.assertEqual(messages, before, "the conversation itself is never changed")
        sent = urls(fitted)
        self.assertLessEqual(sum(map(len, sent)), ib.REQUEST_BYTES)
        self.assertEqual(sent[0], urls(messages)[0], "the user's image as stored")
        self.assertEqual(sent[-ib.KEEP_SHARP:], urls(messages)[-ib.KEEP_SHARP:], "the newest looks as stored")
        self.assertEqual([side_of(u) for u in sent[1:-ib.KEEP_SHARP]], [ib.THUMBNAIL[0]] * 2)
        self.assertEqual(len(shrunk(fitted)), 2)
        self.assertEqual(omitted(fitted), [])

    def test_a_long_session_of_looks_is_bounded_and_says_what_each_left_out_image_showed(self):
        # 2026-10-17, live: 42 images kept in one pose-matching chat, 6.8 MB of base64 on every request.
        messages = self.conversation(looks=42)
        self.assertGreater(sum(map(len, urls(messages))), 15_000_000)
        fitted = ib.fit(messages)
        sent = urls(fitted)
        self.assertLessEqual(sum(map(len, sent)), ib.REQUEST_BYTES)
        self.assertEqual(sent[-ib.KEEP_SHARP:], urls(messages)[-ib.KEEP_SHARP:])
        self.assertEqual(sent[0], urls(messages)[0])
        gone = omitted(fitted)
        self.assertEqual(len(gone) % ib.OMIT_BATCH, 0, "left out a batch at a time")
        self.assertEqual(gone[0], "[earlier image omitted to keep the request small: "
                                  "three_quarter view of the chair, look 0]")
        kept = [int(t.rsplit(" ", 1)[1].rstrip(".")) for t in texts(fitted) if t.startswith(cv.context.CAPTURE_LABEL)]
        self.assertEqual(len(kept), 42, "every look's own line is still sent")
        thumbnails = [side_of(u) for u in sent[1:-ib.KEEP_SHARP]]
        self.assertEqual(len(gone) + len(thumbnails) + ib.KEEP_SHARP, 42)
        self.assertTrue(thumbnails and set(thumbnails) == {ib.THUMBNAIL[0]})

    def test_one_look_more_leaves_the_start_of_the_request_as_it_was(self):
        # The provider caches the start of a request: each removal re-bills everything after it.
        groups = {}
        for looks in range(30, 30 + ib.OMIT_BATCH):
            fitted = ib.fit(self.conversation(looks=looks))
            groups.setdefault(len(omitted(fitted)), []).append(urls(fitted)[:6])
        self.assertLessEqual(len(groups), 2, "one removal in OMIT_BATCH more looks")
        for starts in groups.values():
            self.assertTrue(all(start == starts[0] for start in starts), "between removals the start is the same")

    def test_past_everything_else_images_are_left_out_with_a_note_never_the_newest(self):
        messages = self.conversation()
        fitted = ib.fit(messages, budget=40_000)
        self.assertEqual(len(urls(fitted)), 1)
        self.assertEqual(len(omitted(fitted)), 5)
        self.assertIn("[earlier image omitted to keep the request small: an image the user attached]", omitted(fitted))
        self.assertEqual(side_of(urls(fitted)[0]), ib.SHRINK_STEPS[-1][0], "the newest is shrunk, not left out")
        self.assertIn(ib.SHRUNK_NOTE.format(side=ib.SHRINK_STEPS[-1][0]), texts(fitted)[-1])
        self.assertTrue(texts(fitted)[-2].startswith(cv.context.CAPTURE_LABEL), "what it showed is still said")

    def test_a_shrunk_image_is_made_once_for_the_requests_that_follow(self):
        messages = self.conversation(looks=20)
        ib.fit(messages)
        written = len(fake_imbuf.written)
        ib.fit(copy.deepcopy(messages))
        self.assertEqual(len(fake_imbuf.written), written)

    def test_each_dropped_upload_halves_the_budget_down_to_the_least(self):
        sized = ib.Sized(self.conversation())
        sizes = [sum(map(len, urls(sized.messages())))]
        while sized.tighten():
            sizes.append(sum(map(len, urls(sized.messages()))))
        self.assertEqual(sized.budget, ib.MIN_BYTES)
        self.assertEqual(len(sizes), 3, "2 MB, 1 MB, 0.5 MB")
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        self.assertLessEqual(sizes[-1], ib.MIN_BYTES)
        self.assertEqual(urls(sized.messages())[-1], urls(sized.source)[-1], "the newest still as stored at 0.5 MB")


if __name__ == "__main__":
    unittest.main()
