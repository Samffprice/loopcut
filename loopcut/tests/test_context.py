"""context.py: elision, compaction and what gets sent. No Blender, no network."""

import json
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))
sys.modules.setdefault("bpy", types.ModuleType("bpy"))

from loopcut import context as cx  # noqa: E402
from loopcut import conversations as cv  # noqa: E402


def call(call_id: str, code: str = "bpy.ops.mesh.primitive_cube_add()") -> dict:
    return {"id": call_id, "type": "function",
            "function": {"name": "run_python", "arguments": json.dumps({"code": code, "summary": "Add a cube"})}}


def turn(number: int, results: int, chars: int) -> list[dict]:
    """One user turn: a message, then `results` tool calls each answered with `chars` characters."""
    messages = [{"role": "user", "content": f"<scene_context>\nfile: a.blend\n</scene_context>\n\nturn {number}"}]
    for n in range(results):
        call_id = f"call_{number}_{n}"
        messages.append({"role": "assistant", "content": None, "tool_calls": [call(call_id)]})
        messages.append({"role": "tool", "tool_call_id": call_id,
                         "content": f"OK result {number}.{n}\n" + "x" * chars})
    messages.append({"role": "assistant", "content": f"Done with turn {number}."})
    return messages


class Config:
    base_url, api_key, model, context_budget = "http://127.0.0.1:1", "k", "m", 10_000


class ElisionTest(unittest.TestCase):
    def test_recent_steps_are_left_whole(self):
        messages = turn(1, 2, 100)
        self.assertEqual(cx.elide(messages), 0)
        self.assertEqual(json.dumps(messages), json.dumps(turn(1, 2, 100)))

    def test_older_results_and_code_are_cut_oldest_first(self):
        messages = turn(1, 12, 5000)
        edits = cx.elide(messages)
        results = [m for m in messages if m["role"] == "tool"]
        calls = [m["tool_calls"][0]["function"] for m in messages if m.get("tool_calls")]
        stubbed = [cx.ELIDED in m for m in results]
        self.assertEqual(stubbed, [True] * (12 - cx.PROTECTED_RESULTS) + [False] * cx.PROTECTED_RESULTS)
        cut = ["lines elided" in f["arguments"] for f in calls]
        self.assertEqual(cut, [True] * (12 - cx.PROTECTED_CODE) + [False] * cx.PROTECTED_CODE)
        self.assertEqual(edits, (12 - cx.PROTECTED_RESULTS) + (12 - cx.PROTECTED_CODE))
        first = results[0]
        self.assertEqual(first[cx.ELIDED], 5000 + len("OK result 1.0\n"))
        self.assertTrue(first["content"].startswith("OK result 1.0\n[+"), first["content"])
        self.assertIn("call the tool again", first["content"])
        self.assertEqual(json.loads(calls[0]["arguments"]), {"code": "[1 lines elided]", "summary": "Add a cube"})
        self.assertIn("primitive_cube_add", calls[-1]["arguments"])
        before, after = cx.estimate_tokens(turn(1, 12, 5000)), cx.estimate_tokens(messages)
        self.assertLess(after - cx.FIXED_TOKENS, (before - cx.FIXED_TOKENS) / 2)

    def test_elision_is_stable_and_never_undone(self):
        messages = turn(1, 12, 5000)
        cx.elide(messages)
        snapshot = json.dumps(messages)
        self.assertEqual(cx.elide(messages), 0)
        self.assertEqual(json.dumps(messages), snapshot, "a second pass changes nothing")

    def test_a_failed_step_keeps_its_code_until_the_retry_is_past(self):
        messages = turn(1, 2, 100)  # Step 1 failed, step 2 is the retry: both still whole.
        messages[2]["content"] = "Traceback...\nAttributeError: no"
        cx.elide(messages)
        self.assertIn("primitive_cube_add", messages[1]["tool_calls"][0]["function"]["arguments"])
        messages[-1:-1] = turn(1, 1, 100)[1:3]  # A third step: the failed one is now history.
        cx.elide(messages)
        self.assertIn("lines elided", messages[1]["tool_calls"][0]["function"]["arguments"])
        self.assertTrue(messages[2]["content"].startswith("Traceback..."), "the stub keeps the first line")

    def test_only_run_python_code_is_cut(self):
        messages = turn(1, 8, 4000)
        messages[1]["tool_calls"][0]["function"]["name"] = "get_scene_info"
        messages[1]["tool_calls"][0]["function"]["arguments"] = '{"name_contains": "Cube"}'
        cx.elide(messages)
        self.assertEqual(messages[1]["tool_calls"][0]["function"]["arguments"], '{"name_contains": "Cube"}')


class CompactionTest(unittest.TestCase):
    def test_turn_starts_are_recognized_and_captures_are_not(self):
        self.assertTrue(cx.is_turn_start({"role": "user", "content": "hi"}))
        self.assertTrue(cx.is_turn_start({"role": "user", "content": [{"type": "text", "text": "like this"},
                                                                       {"type": "image_url", "image_url": {"url": "x"}}]}))
        self.assertFalse(cx.is_turn_start({"role": "user", cx.CAPTURE: True, "content": [
            {"type": "text", "text": cx.CAPTURE_TEXT}, {"type": "image_url", "image_url": {"url": "x"}}]}))
        self.assertFalse(cx.is_turn_start({"role": "user", "content": [  # Stored before the marker existed.
            {"type": "text", "text": cx.CAPTURE_TEXT}, {"type": "image_url", "image_url": {"url": "x"}}]}))
        self.assertFalse(cx.is_turn_start({"role": "assistant", "content": "hi"}))

    def test_cut_prefers_the_start_of_a_turn_that_fits(self):
        messages = turn(1, 3, 3000) + turn(2, 3, 3000) + turn(3, 1, 100)
        cut = cx.compaction_cut(messages, budget=10_000)
        self.assertEqual(messages[cut]["content"].endswith("turn 3"), True)
        self.assertIsNone(cx.compaction_cut(turn(1, 1, 100)[:1], budget=10_000), "nothing before the only message")

    def test_cut_falls_back_inside_a_turn_that_is_too_big_on_its_own(self):
        messages = turn(1, 1, 100) + turn(2, 12, 3000)
        cut = cx.compaction_cut(messages, budget=10_000)
        self.assertIsNotNone(cut)
        self.assertGreater(cut, len(turn(1, 1, 100)) + 1, "inside turn 2")
        self.assertEqual(messages[cut]["role"], "assistant", "a request may start with an assistant message")
        self.assertNotEqual(messages[cut - 1]["role"], "assistant", "never between a call and its result")
        self.assertLessEqual(cx.estimate_tokens(messages[cut:]) + cx.SUMMARY_TOKENS, 10_000 * cx.COMPACT_TO)

    def test_a_summary_replaces_what_is_before_it_and_a_later_one_supersedes(self):
        messages = turn(1, 1, 10) + turn(2, 1, 10) + turn(3, 1, 10)
        second, third = len(turn(1, 1, 10)), 2 * len(turn(1, 1, 10))
        messages[second][cx.SUMMARY] = "Turn 1: added Cube."
        sent = cx.view(messages)
        self.assertEqual(sent[0]["role"], "user")
        self.assertIn("<conversation_summary>\nTurn 1: added Cube.\n</conversation_summary>", sent[0]["content"])
        self.assertEqual(sent[1:], messages[second:])
        messages[third][cx.SUMMARY] = "Turns 1-2: added Cube and Sphere."
        self.assertEqual(cx.view(messages)[1:], messages[third:])
        self.assertIn("Sphere", cx.view(messages)[0]["content"])
        # A restore that cuts the conversation back to turn 3's start takes the newer summary with
        # it, and the older one is exactly right for what is left.
        del messages[third:]
        self.assertEqual(cx.window(messages), (second, "Turn 1: added Cube."))
        self.assertEqual(cx.view(messages, upto=second + 1), [sent[0], messages[second]], "what a summary call sees")

    def test_transcript_is_plain_text_without_images(self):
        messages = turn(1, 1, 10)
        messages.append({"role": "user", cx.CAPTURE: True, "content": [
            {"type": "text", "text": cx.CAPTURE_TEXT}, {"type": "image_url", "image_url": {"url": "data:..."}}]})
        text = cx.transcript(messages)
        self.assertIn("USER: <scene_context>", text)
        self.assertIn("ASSISTANT CALLED run_python: ", text)
        self.assertIn("TOOL RESULT: OK result 1.0", text)
        self.assertIn("[image]", text)
        self.assertNotIn("data:...", text)

    def test_prepare_elides_first_then_summarizes_and_marks_the_cut(self):
        session = {"messages": turn(1, 8, 9000) + turn(2, 8, 9000) + turn(3, 1, 100), "token_ratio": 0.0}
        asked = []

        def fake_summarize(cfg, messages, is_cancelled):
            asked.append(messages)
            return "Turns 1 and 2: sixteen cubes."

        sent, compacted = cx.prepare(session, Config, lambda: False, summarize=fake_summarize)
        self.assertEqual(len(asked), 1)
        cut = len(turn(1, 8, 9000)) + len(turn(2, 8, 9000))
        self.assertIs(asked[0][0], session["messages"][0], "summarized from the start...")
        self.assertIs(asked[0][-1], session["messages"][cut - 1], "...to just before the cut")
        self.assertTrue(any(m["content"].startswith(cx.LEDGER_HEAD) for m in asked[0]), "folded, like a request")
        self.assertEqual(session["messages"][cut][cx.SUMMARY], "Turns 1 and 2: sixteen cubes.")
        self.assertEqual(compacted, cut)
        self.assertEqual(len(sent), 1 + len(turn(3, 1, 100)))
        self.assertIn("sixteen cubes", sent[0]["content"])
        self.assertTrue(any(cx.ELIDED in m for m in session["messages"]), "elision was tried first")
        # Still valid after a save and load: the markers are simple values.
        self.assertTrue(all(isinstance(v, (str, int, bool)) for m in session["messages"]
                            for k, v in m.items() if k.startswith(cv.PRIVATE_PREFIX)))
        again, compacted = cx.prepare(session, Config, lambda: False, summarize=fake_summarize)
        self.assertEqual((again, compacted, len(asked)), (sent, 0, 1), "nothing more to do next step")

    def test_a_failed_summary_sends_the_conversation_as_is(self):
        from loopcut import llm
        session = {"messages": turn(1, 8, 9000) + turn(2, 8, 9000) + turn(3, 1, 100)}
        tried = []

        def failing(cfg, messages, is_cancelled):
            tried.append(messages)
            raise llm.LLMError("HTTP 500: no")

        sent, compacted = cx.prepare(session, Config, lambda: False, summarize=failing)
        self.assertEqual((len(tried), compacted), (1, 0))
        self.assertIs(sent[0], session["messages"][0], "no summary: the conversation starts at the start")
        self.assertTrue(all(cx.SUMMARY not in m for m in session["messages"]))

    def test_calibration_scales_the_estimate_within_bounds(self):
        session = {}
        cx.calibrate(session, estimated=1000, reported=1300)
        self.assertAlmostEqual(session["token_ratio"], 1.3)
        cx.calibrate(session, estimated=1000, reported=10)
        self.assertEqual(session["token_ratio"], cx.MIN_RATIO)
        cx.calibrate(session, estimated=0, reported=10)
        self.assertEqual(session["token_ratio"], cx.MIN_RATIO, "no estimate, no change")


def capture(ref: str) -> dict:
    return {"role": "user", cx.CAPTURE: True, "content": [
        {"type": "text", "text": cx.CAPTURE_TEXT}, {"type": "image_url", "image_url": {"url": ref}}]}


class FoldTest(unittest.TestCase):
    def test_finished_steps_become_one_ledger_message_and_live_steps_stay(self):
        messages = turn(1, 8, 500)
        messages[2]["content"] = "Traceback (most recent call last):\n  File x\nAttributeError: no such thing\n\nScene changes: none."
        messages[4]["content"] = "OK (no output)\n\nScene changes (+ added):\n+ Sphere (mesh) at [0, 0, 0]..[1, 1, 1]"
        cx.elide(messages)
        sent = cx.view(messages)
        self.assertIs(sent[0], messages[0], "the user's message is never folded")
        ledger = sent[1]
        self.assertEqual(ledger["role"], "assistant")
        lines = ledger["content"].split("\n")
        self.assertEqual(lines[0], cx.LEDGER_HEAD)
        self.assertEqual(lines[1], "- Add a cube: AttributeError: no such thing", "a failure is remembered by its exception")
        self.assertEqual(lines[2], "- Add a cube: OK (no output) | + Sphere (mesh) at [0, 0, 0]..[1, 1, 1]")
        self.assertEqual(len(lines), 1 + (8 - cx.PROTECTED_RESULTS), "one line per finished step")
        live = sent[2:]
        self.assertEqual([m["role"] for m in live], ["assistant", "tool"] * cx.PROTECTED_RESULTS + ["assistant"])
        self.assertIs(live[-1], messages[-1])
        self.assertLess(cx.estimate_tokens(sent), cx.estimate_tokens(messages))

    def test_partly_finished_multi_call_steps_are_not_folded(self):
        messages = turn(1, 1, 100)
        messages[1]["tool_calls"].append(call("call_1_x"))
        messages.insert(3, {"role": "tool", "tool_call_id": "call_1_x", "content": "second"})
        messages[2][cx.ELIDED] = 100  # Only one of the two results is elided.
        sent = cx.view(messages)
        self.assertEqual(len(sent), len(messages))

    def test_a_capture_is_sent_once_then_folded_and_a_step_may_send_several(self):
        messages = turn(1, 2, 100)[:-1]
        messages[3:3] = [capture("img:a")]                    # After step 1.
        messages += [capture("img:b"), capture("img:c")]      # After step 2: two angles, not yet seen.
        self.assertEqual(cx.kept_images(messages), {"img:b", "img:c"})
        sent = cx.view(messages)
        self.assertTrue(all("img:a" not in json.dumps(m) for m in sent), "acted on: dropped")
        self.assertIn("- looked at the viewport", sent[3]["content"])
        self.assertEqual(sum("img:b" in json.dumps(m) or "img:c" in json.dumps(m) for m in sent), 2)
        messages += turn(1, 1, 100)[1:3]                      # Step 3 consumed them.
        self.assertEqual(cx.kept_images(messages), set())
        messages.append({"role": "user", "content": "next turn"})
        messages.append(capture("img:d"))
        self.assertEqual(cx.kept_images(messages), {"img:d"})

    def test_attached_images_ride_the_turn_they_were_attached_to(self):
        messages = [{"role": "user", cx.ATTACHED: True, "content": [
            {"type": "text", "text": "like this"}, {"type": "image_url", "image_url": {"url": "img:ref"}}]}]
        messages += turn(1, 1, 10)[1:]  # The steps of that turn.
        self.assertEqual(cx.kept_images(messages), {"img:ref"})
        self.assertIs(cx.view(messages)[0], messages[0])
        messages += turn(2, 1, 10)
        self.assertEqual(cx.kept_images(messages), set(), "the next turn no longer carries it")


class WireTest(unittest.TestCase):
    def test_private_markers_are_never_sent(self):
        messages = turn(1, 1, 10)
        messages[0][cx.SUMMARY] = "nothing yet"
        messages[2][cx.ELIDED] = 10
        wired = cv.wire_messages("0" * 32, messages)
        self.assertFalse(any(k.startswith(cv.PRIVATE_PREFIX) for m in wired for k in m))
        self.assertEqual([m["role"] for m in wired], [m["role"] for m in messages])


if __name__ == "__main__":
    unittest.main()
