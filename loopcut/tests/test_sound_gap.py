"""The sound-gap finish check (sound_gap.py; cloud-tools-implicit-use section 5): Loopcut's advisory note on an
animation for a trailer, a cutscene or a video that has no sound yet. Once per chat, silent after a decline, never a
gate. No Blender: the scene is a stand-in.

    python3 -m unittest discover -s loopcut/tests
"""

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

fake_bpy = types.ModuleType("bpy")
fake_bpy.app = types.SimpleNamespace(driver_namespace={})
sys.modules.setdefault("bpy", fake_bpy)

from loopcut import agent, llm, router, sound_gap, state  # noqa: E402

SCENE = "<scene_context>\nfile: forest.blend | frames: 1-250\n</scene_context>"


def thing(name, animated=False):
    action = types.SimpleNamespace(action=object(), nla_tracks=[], drivers=[]) if animated else None
    return types.SimpleNamespace(name=name, type="MESH", animation_data=action, parent=None, modifiers=[],
                                 constraints=[], data=None, rigid_body=None)


def scene(start=1, end=250, objects=(), strips=None):
    editor = None if strips is None else types.SimpleNamespace(strips_all=[types.SimpleNamespace(type=t)
                                                                            for t in strips])
    return types.SimpleNamespace(frame_start=start, frame_end=end, objects=list(objects), sequence_editor=editor)


def chat(*texts, purpose="horror game trailer", calls_for=("sound", "render")) -> tuple[dict, agent.Turn]:
    """A chat of these user texts, each but the last answered "Done."; the last turn routed with this purpose."""
    session = state.new_session()
    for number, text in enumerate(texts, 1):
        if number > 1:
            session["messages"].append({"role": "assistant", "content": "Done."})
        session["messages"].append({"role": "user", "content": f"{SCENE}\n\n{text}"})
    turn = agent.Turn()
    turn.outcome = "running"
    turn.routed = router.Decision(3, purpose=purpose or "", calls_for=list(calls_for))
    return session, turn


class SoundGapTest(unittest.TestCase):
    def setUp(self):
        live = scene(objects=[thing("Camera", animated=True), thing("Tree")])
        for patch in (mock.patch.object(sound_gap, "_sound_tool", lambda: True),  # sfx callable or unlockable here.
                      mock.patch.object(sys.modules["bpy"], "context", types.SimpleNamespace(scene=live), create=True)):
            patch.start()
            self.addCleanup(patch.stop)

    def check(self, session, turn, text="Done: the forest and a slow push-in.", role="orchestrator"):
        completion = llm.Completion(text=text)
        return sound_gap.finish_check(session, turn, completion, role, lambda fn: fn())

    def test_fires_for_a_trailer_that_animates_over_frames_with_no_sound(self):
        session, turn = chat("Spooky forest for a horror game trailer")
        self.assertEqual(self.check(session, turn), sound_gap.LINE)
        self.assertEqual(sound_gap.LINE, "[Loopcut] This animation has no sound yet; an ambience track would finish it.")
        self.assertNotRegex(sound_gap.LINE, r"\d", "no price: the gateway's credits line says the prices")
        for purpose, calls_for in (("an animated short", ()), ("intro cutscene", ()), ("a youtube video", ()),
                                   ("my portfolio", ("sound",))):
            with self.subTest(purpose=purpose):
                session, turn = chat("x", purpose=purpose, calls_for=calls_for)
                self.assertEqual(self.check(session, turn), sound_gap.LINE)

    def test_not_for_a_typewriter_or_a_still(self):
        session, turn = chat("Model a typewriter", purpose=None, calls_for=())
        self.assertIsNone(self.check(session, turn), "no purpose")
        session, turn = chat("A typewriter for my desk", purpose="a typewriter for my desk", calls_for=())
        self.assertIsNone(self.check(session, turn))
        for still in (scene(1, 1, [thing("Camera", animated=True)]), scene(objects=[thing("Tree")]),
                      scene(objects=[thing("Camera", animated=True)], strips=["SOUND"])):
            with self.subTest(still=still), \
                    mock.patch.object(sys.modules["bpy"], "context", types.SimpleNamespace(scene=still), create=True):
                session, turn = chat("Spooky forest for a horror game trailer")
                self.assertIsNone(self.check(session, turn), "one frame, nothing animates, or a sound strip")
        with mock.patch.object(sound_gap, "_sound_tool", lambda: False):
            self.assertIsNone(self.check(*chat("trailer")), "no sound tool here: an own key without cloud tools")

    def test_once_per_chat_and_its_later_turns_keep_the_purpose(self):
        session, turn = chat("Spooky forest for a horror game trailer")
        line = self.check(session, turn)
        session["messages"] += [{"role": "user", "loopcut_step_limit": True, "content": line},
                                {"role": "assistant", "content": "It has no sound yet; want an ambience track?"}]
        self.assertTrue(sound_gap.said(session))
        self.assertFalse(sound_gap.declined(session), "the gap itself is no decline, nor the model's own question")
        self.assertIsNone(self.check(session, turn), "said once in this chat")
        session, turn = chat("Spooky forest for a horror game trailer", "add a camera move", purpose=None, calls_for=())
        self.assertIsNone(self.check(session, turn), "no purpose anywhere in the chat")
        routed = f"{SCENE}\n\nSpooky forest\n\n<routing>level 3 — x\n{router.purpose_line(router.Decision(3, purpose='horror game trailer', calls_for=['render']))}</routing>"
        session["messages"][0]["content"] = routed
        self.assertEqual(self.check(session, turn), sound_gap.LINE, "the chat's last purpose line")

    def test_silent_after_a_decline(self):
        declines = {
            "in the request": (["Horror trailer, no sound please"], None),
            "own music": (["Horror trailer; I'll add my own music"], None),
            "a no to the question": (["Horror trailer", "no thanks"], "Done. Want me to add an ambience sound?"),
            "the model says so": (["Horror trailer", "make it darker"], "Done; you didn't want any sound, so none."),
        }
        for what, (texts, reply) in declines.items():
            with self.subTest(what):
                session, turn = chat(*texts)
                if reply:
                    session["messages"][1]["content"] = reply
                self.assertTrue(sound_gap.declined(session))
                self.assertIsNone(self.check(session, turn))
        session, turn = chat("Horror trailer", "make it darker")
        session["items"].append({**state.item_tool("loopcut_cloud__sfx", "Night ambience", ""), "status": "rejected"})
        self.assertTrue(sound_gap.declined(session))
        self.assertIsNone(self.check(session, turn), "a skipped sound card")
        session, turn = chat("Horror trailer", "yes, darker")
        session["messages"][1]["content"] = "Done. Want it darker?"  # A no to another question is no decline.
        self.assertEqual(self.check(session, turn), sound_gap.LINE)
        session, turn = chat("Horror trailer with no people, just fog and sound")  # The "no" is another clause's.
        self.assertFalse(sound_gap.declined(session))

    def test_never_for_a_worker_a_question_a_stop_or_a_declined_call(self):
        session, turn = chat("Horror trailer")
        self.assertIsNone(self.check(session, turn, role="worker"))
        self.assertIsNone(self.check(session, turn, text="Done. Should the fog be thicker?"), "the user is to answer")
        turn.declined = True
        self.assertIsNone(self.check(session, turn))
        session, turn = chat("Horror trailer")
        turn.outcome = "step_limit"
        self.assertIsNone(self.check(session, turn))
        session, turn = chat("Horror trailer")
        turn.cancel.set()
        self.assertIsNone(self.check(session, turn))
        session, turn = chat("Horror trailer")
        self.assertIsNone(sound_gap.finish_check(session, turn, llm.Completion(text="Done."), "orchestrator",
                                                 lambda fn: 1 / 0), "a check that cannot run says nothing")

    def test_the_final_reply_sends_it_once_as_its_own_message(self):
        """agent._final_reply: after the finish gate and the rig check, before the contact block."""
        session, turn = chat("Spooky forest for a horror game trailer")
        turn.routed.level = 1  # Not router.nudge's level-3 turn that delegated nothing.
        session["messages"].append({"role": "assistant", "content": "Done."})
        with mock.patch.object(agent, "_on_main", lambda fn, *a, **k: fn()):
            self.assertEqual(agent._final_reply(session, turn, llm.Completion(text="Done."), agent.ORCH_SPEC),
                             sound_gap.LINE)
            session["messages"].append({"role": "user", "loopcut_step_limit": True, "content": sound_gap.LINE})
            self.assertIsNone(agent._final_reply(session, turn, llm.Completion(text="Added it."), agent.ORCH_SPEC))


if __name__ == "__main__":
    unittest.main()
