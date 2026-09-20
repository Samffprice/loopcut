"""Attaching images in a real Blender: conversion to a bounded PNG, refusals, a clean bpy.data,
and the message a send would put on the wire.

    Blender -b --factory-startup --python harness/attachments_check.py
"""

import os
import sys
import tempfile
import traceback
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
import checkout  # noqa: E402
checkout.use()
os.environ["LOOPCUT_DATA_DIR"] = tempfile.mkdtemp(prefix="loopcut-harness-")

from loopcut import attachments, conversations, state  # noqa: E402


def main() -> None:
    work = Path(tempfile.mkdtemp(prefix="loopcut-attach-"))
    big = bpy.data.images.new("big", 4000, 1000)
    big.filepath_raw, big.file_format = str(work / "big photo.jpg"), "JPEG"
    big.save()
    bpy.data.images.remove(big)
    (work / "notes.txt").write_text("not an image")
    (work / "fake.png").write_text("not an image either")
    before = set(bpy.data.images.keys())

    session = state.new_session()
    problems = attachments.add(session, [str(work / "big photo.jpg"), str(work / "notes.txt"),
                                         str(work / "fake.png"), str(work / "missing.png")])
    assert len(problems) == 3, problems
    assert [a["name"] for a in session["attachments"]] == ["big photo.jpg"], session["attachments"]
    assert set(bpy.data.images.keys()) == before, "attaching left an image datablock behind"

    for key, side in (("full", attachments.MAX_SIDE), ("ref", attachments.SEND_SIDE)):
        stored = conversations.image_path(session["id"], session["attachments"][0][key])
        check = bpy.data.images.load(str(stored))
        assert tuple(check.size) == (side, side // 4), (key, tuple(check.size))
        assert check.file_format == "PNG", check.file_format
        bpy.data.images.remove(check)

    attachments.add(session, [str(work / "big photo.jpg")])
    assert len(session["attachments"]) == 1, "the same image attached twice"

    wired = conversations.wire_messages(session["id"], [{"role": "user", conversations.ATTACHED: True, "content": [
        {"type": "text", "text": "like this"},
        {"type": "image_url", "image_url": {"url": session["attachments"][0]["ref"]}}]}])
    assert wired[0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert conversations.ATTACHED not in wired[0]
    print("ATTACHMENTS CHECK OK")


try:
    main()
except Exception:
    traceback.print_exc()
    sys.stderr.flush()
    os._exit(1)
