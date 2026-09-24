"""context.py: step records, folding, compaction and what gets sent. No Blender, no network."""

import copy
import json
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))
sys.modules.setdefault("bpy", types.ModuleType("bpy"))

from loopcut import context as cx  # noqa: E402
from loopcut import conversations as cv  # noqa: E402

CHANGES = "\n\nScene changes (+ added, - removed, ~ changed; bounds are world space):\n"


def call(call_id: str, code: str = "bpy.ops.mesh.primitive_cube_add()", name: str = "run_python",
         summary: str = "Add a cube") -> dict:
    arguments = {"code": code, "summary": summary} if name == "run_python" else {"object": "Cube"}
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def step(number: int, n: int, chars: int, prose: str | None = None) -> list[dict]:
    call_id = f"call_{number}_{n}"
    return [{"role": "assistant", "content": prose if prose is not None else f"Step {number}.{n}: a cube, because the user asked.",
             "tool_calls": [call(call_id, code="x = 1\n" * 40)]},
            {"role": "tool", "tool_call_id": call_id,
             "content": "OK (no output)" if not chars else f"printed {number}.{n}\n" + "x" * chars
             + CHANGES + f"+ Cube.{number}{n} (mesh) at [0, 0, 0]..[1, 1, 1]"}]


def turn(number: int, steps: int, chars: int) -> list[dict]:
    """One user turn: a message, then `steps` run_python steps each printing `chars` characters."""
    messages = [{"role": "user", "content": f"<scene_context>\nfile: a.blend\n</scene_context>\n\nturn {number}"}]
    for n in range(steps):
        messages += step(number, n, chars)
    messages.append({"role": "assistant", "content": f"Done with turn {number}."})
    return messages


class Config:
    base_url, api_key, model, context_budget = "http://127.0.0.1:1", "k", "m", 40_000


def records(sent: list) -> list[str]:
    return [m["content"] for m in sent if m["role"] == "assistant" and (m.get("content") or "").startswith(cx.RECORD_HEAD)]


class RecordTest(unittest.TestCase):
    def test_a_record_keeps_what_the_model_said_and_what_changed_not_the_code(self):
        messages = [{"role": "user", "content": "go"}] + step(1, 0, 50, prose="The liquid renders black: refraction "
                                                                               "only sees the dim backdrop. Adding a card.")
        messages[1][cx.FOLDED] = True
        (text,) = records(cx.view(messages))
        self.assertIn("The liquid renders black: refraction only sees the dim backdrop. Adding a card.", text)
        self.assertIn('- run_python "Add a cube": ok, printed:\n    printed 1.0', text)
        self.assertIn("    + Cube.10 (mesh) at [0, 0, 0]..[1, 1, 1]", text)
        self.assertNotIn("x = 1", text, "code is not kept")

    def test_a_failure_is_remembered_by_its_exception_and_what_it_printed_first(self):
        messages = [{"role": "user", "content": "go"}] + step(1, 0, 0)
        messages[2]["content"] = ("halfway\nTraceback (most recent call last):\n  File x\nAttributeError: no such thing"
                                  "\n\nScene changes: none. (If you expected a change, the code did not do what you think.)")
        messages[1][cx.FOLDED] = True
        (text,) = records(cx.view(messages))
        self.assertIn('- run_python "Add a cube": failed: AttributeError: no such thing\n    halfway\n    no scene changes', text)

    def test_a_step_that_never_ran_says_so(self):
        messages = [{"role": "user", "content": "go"}] + step(1, 0, 0)
        messages[2]["content"] = "The user rejected this action. Ask what they want instead."
        messages[1][cx.FOLDED] = True
        self.assertIn('run_python "Add a cube": not run: The user rejected this action.', records(cx.view(messages))[0])

    def test_long_output_and_long_change_lists_are_capped(self):
        messages = [{"role": "user", "content": "go"}] + step(1, 0, 5000)
        messages[2]["content"] += "".join(f"\n+ Extra.{i} (mesh)" for i in range(30))
        messages[1][cx.FOLDED] = True
        (text,) = records(cx.view(messages))
        self.assertIn(f"[+{len('printed 1.0') + 1 + 5000 - cx.RECORD_PRINTED} chars]", text)
        self.assertIn(f"... and {31 - cx.RECORD_CHANGES} more: Extra.{cx.RECORD_CHANGES - 1}, Extra.{cx.RECORD_CHANGES}", text)
        self.assertLess(len(text), 1500)

    def test_other_tools_keep_their_first_lines(self):
        messages = [{"role": "user", "content": "go"},
                    {"role": "assistant", "content": "Reading the glass.", "tool_calls": [call("c1", name="get_object_info")]},
                    {"role": "tool", "tool_call_id": "c1", "content": "Bottle_Body: mesh\n" + "node detail " * 100}]
        messages[1][cx.FOLDED] = True
        (text,) = records(cx.view(messages))
        self.assertIn('- get_object_info({"object":"Cube"}): Bottle_Body: mesh\n    node detail', text)
        self.assertLess(len(text), 700)

    def test_a_dropped_capture_is_remembered_with_the_step_that_took_it(self):
        described = {"role": "user", cx.CAPTURE: True, "content": [
            {"type": "text", "text": cx.CAPTURE_LABEL + "front view of Cube, Sphere (after the step above).\nMore."},
            {"type": "image_url", "image_url": {"url": "img:a"}}]}
        messages = turn(1, 1, 10)[:-1] + [described] + step(1, 1, 10) + [capture("img:b")]
        messages[1][cx.FOLDED] = True
        (text,) = records(cx.view(messages))
        self.assertTrue(text.endswith("- looked: front view of Cube, Sphere (after the step above)."))


class FoldTest(unittest.TestCase):
    def test_earlier_turns_are_records_and_the_current_turn_is_whole(self):
        session = {"messages": turn(1, 3, 100) + turn(2, 3, 100)}
        sent, _ = cx.prepare(session, Config, lambda: False)
        self.assertIs(sent[0], session["messages"][0], "the user's message is never folded")
        self.assertEqual(len(records(sent)), 1, "turn 1's steps are one record message")
        self.assertIs(sent[2], session["messages"][7], "turn 1's closing reply stays")
        self.assertEqual(sent[3:], session["messages"][8:], "turn 2, still going, is sent as itself")

    def test_nothing_stored_is_edited_only_marked(self):
        session = {"messages": turn(1, 3, 100) + turn(2, 3, 100)}
        before = copy.deepcopy(session["messages"])
        cx.prepare(session, Config, lambda: False)
        stripped = [{k: v for k, v in m.items() if k != cx.FOLDED} for m in session["messages"]]
        self.assertEqual(stripped, before)
        self.assertEqual(sum(bool(m.get(cx.FOLDED)) for m in session["messages"]), 3)

    def test_a_note_sent_mid_turn_does_not_start_a_new_turn(self):
        messages = turn(1, 3, 100)[:-1] + [{"role": "user", cx.STEER: True, "content": "<steer>bluer</steer>"}]
        session = {"messages": messages}
        sent, _ = cx.prepare(session, Config, lambda: False)
        self.assertEqual(sent, messages, "the steps before the note are still this turn's")

    def test_mid_turn_the_oldest_steps_fold_only_past_the_threshold(self):
        messages = turn(1, 8, 1000)[:-1]
        session = {"messages": messages}
        sent, _ = cx.prepare(session, Config, lambda: False)
        self.assertEqual(records(sent), [], "under FOLD_AT nothing folds: the cache holds")
        messages += [m for n in range(8, 70) for m in step(1, n, 1000)]
        before = cx.estimate_tokens(cx.view(messages))
        self.assertGreater(before, Config.context_budget * cx.FOLD_AT)
        sent, _ = cx.prepare(session, Config, lambda: False)
        self.assertLessEqual(cx.estimate_tokens(sent), Config.context_budget * cx.FOLD_TO)
        live = cx.steps(messages)[-cx.LIVE_STEPS:]
        self.assertFalse(any(messages[i].get(cx.FOLDED) for i in live), "the newest steps stay whole")
        folded = [i for i in cx.steps(messages) if messages[i].get(cx.FOLDED)]
        self.assertEqual(folded, cx.steps(messages)[:len(folded)], "oldest first")
        again, _ = cx.prepare(session, Config, lambda: False)
        self.assertEqual(again, sent, "stable: the next request has the same prefix")

    def test_a_step_may_send_several_captures_and_the_newest_stays_until_replaced(self):
        messages = turn(1, 2, 100)[:-1]
        messages[3:3] = [capture("img:a")]                    # After step 1.
        messages += [capture("img:b"), capture("img:c")]      # After step 2: two angles, not yet seen.
        self.assertEqual(cx.kept_images(messages), {"img:b", "img:c"})
        sent = cx.view(messages)
        self.assertTrue(all("img:a" not in json.dumps(m) for m in sent), "replaced: dropped")
        self.assertIn("- looked at the viewport", json.dumps(sent))
        self.assertEqual(sum("img:b" in json.dumps(m) or "img:c" in json.dumps(m) for m in sent), 2)
        messages += step(1, 2, 100)                           # Step 3 acted on them.
        self.assertEqual(cx.kept_images(messages), {"img:c"}, "the newest look stays: the model is never blind")
        messages.append({"role": "user", "content": "next turn"})
        self.assertEqual(cx.kept_images(messages), {"img:c"}, "across turns too, until a newer one")
        messages.append(capture("img:d"))
        self.assertEqual(cx.kept_images(messages), {"img:d"})
        self.assertTrue(all("img:c" not in json.dumps(m) for m in cx.view(messages)))

    def test_attached_references_stay_pinned_across_turns_until_unpinned(self):
        messages = [{"role": "user", cx.ATTACHED: True, "content": [
            {"type": "text", "text": "like this"}, {"type": "image_url", "image_url": {"url": "img:ref"}}]}]
        messages += turn(1, 1, 10)[1:] + turn(2, 1, 10)
        self.assertEqual(cx.kept_images(messages), {"img:ref"}, "a reference outlives its turn")
        self.assertIs(cx.view(messages)[0], messages[0])
        self.assertEqual(cx.kept_images(messages, unpinned=frozenset({"img:ref"})), set())
        session = {"references": [{"ref": "img:ref", "name": "photo.png", "pinned": False}]}
        self.assertEqual(cx.unpinned_refs(session), frozenset({"img:ref"}))
        self.assertEqual(cx.unpinned_refs({}), frozenset())


def capture(ref: str) -> dict:
    return {"role": "user", cx.CAPTURE: True, "content": [
        {"type": "text", "text": cx.CAPTURE_TEXT}, {"type": "image_url", "image_url": {"url": ref}}]}


class CompactionTest(unittest.TestCase):
    def test_turn_starts_are_recognized_and_captures_and_notes_are_not(self):
        self.assertTrue(cx.is_turn_start({"role": "user", "content": "hi"}))
        self.assertTrue(cx.is_turn_start({"role": "user", "content": [{"type": "text", "text": "like this"},
                                                                       {"type": "image_url", "image_url": {"url": "x"}}]}))
        self.assertFalse(cx.is_turn_start(capture("x")))
        self.assertFalse(cx.is_turn_start({"role": "user", "content": [  # Stored before the marker existed.
            {"type": "text", "text": cx.CAPTURE_TEXT}, {"type": "image_url", "image_url": {"url": "x"}}]}))
        self.assertFalse(cx.is_turn_start({"role": "user", cx.STEER: True, "content": "<steer>x</steer>"}))
        self.assertFalse(cx.is_turn_start({"role": "assistant", "content": "hi"}))

    def test_cut_prefers_the_start_of_a_turn_that_fits(self):
        messages = turn(1, 3, 20_000) + turn(2, 3, 20_000) + turn(3, 1, 100)
        cut = cx.compaction_cut(messages, budget=40_000)
        self.assertTrue(messages[cut]["content"].endswith("turn 3"))
        self.assertIsNone(cx.compaction_cut(turn(1, 1, 100)[:1], budget=40_000), "nothing before the only message")

    def test_cut_falls_back_inside_a_turn_that_is_too_big_on_its_own(self):
        messages = turn(1, 1, 100) + turn(2, 12, 10_000)
        cut = cx.compaction_cut(messages, budget=40_000)
        self.assertIsNotNone(cut)
        self.assertGreater(cut, len(turn(1, 1, 100)) + 1, "inside turn 2")
        self.assertEqual(messages[cut]["role"], "assistant", "a request may start with an assistant message")
        self.assertNotEqual(messages[cut - 1]["role"], "assistant", "never between a call and its result")

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
        messages = turn(1, 1, 10) + [capture("data:...")]
        text = cx.transcript(messages)
        self.assertIn("USER: <scene_context>", text)
        self.assertIn("ASSISTANT CALLED run_python: ", text)
        self.assertIn("TOOL RESULT: printed 1.0", text)
        self.assertIn("[image]", text)
        self.assertNotIn("data:...", text)

    def test_records_are_summarized_only_when_they_alone_exceed_the_budget(self):
        pasted = [{"role": "user", "content": f"turn {n}: " + "p" * 60_000} for n in range(3)]
        messages = []
        for n, message in enumerate(pasted):
            messages += [message] + turn(n, 2, 100)[1:]
        messages += turn(9, 1, 100)
        session = {"messages": messages, "token_ratio": 0.0}
        asked = []

        def fake_summarize(cfg, messages, is_cancelled):
            asked.append(messages)
            return "Three pasted documents; six cubes."

        sent, compacted = cx.prepare(session, Config, lambda: False, summarize=fake_summarize)
        self.assertEqual(len(asked), 1)
        cut = len(messages) - len(turn(9, 1, 100))
        self.assertIs(asked[0][0], session["messages"][0], "summarized from the start...")
        self.assertTrue(any(m["content"].startswith(cx.RECORD_HEAD) for m in asked[0]), "...as records, like a request")
        self.assertEqual(session["messages"][cut][cx.SUMMARY], "Three pasted documents; six cubes.")
        self.assertEqual(compacted, cut)
        self.assertIn("six cubes", sent[0]["content"])
        # Still valid after a save and load: the markers are simple values.
        self.assertTrue(all(isinstance(v, (str, int, bool)) for m in session["messages"]
                            for k, v in m.items() if k.startswith(cv.PRIVATE_PREFIX)))
        again, compacted = cx.prepare(session, Config, lambda: False, summarize=fake_summarize)
        self.assertEqual((again, compacted, len(asked)), (sent, 0, 1), "nothing more to do next step")

    def test_a_failed_summary_sends_the_conversation_as_is(self):
        from loopcut import llm
        messages = [{"role": "user", "content": "p" * 200_000}] + turn(2, 1, 100)
        tried = []

        def failing(cfg, messages, is_cancelled):
            tried.append(messages)
            raise llm.LLMError("HTTP 500: no")

        sent, compacted = cx.prepare({"messages": messages}, Config, lambda: False, summarize=failing)
        self.assertEqual((len(tried), compacted), (1, 0))
        self.assertIs(sent[0], messages[0], "no summary: the conversation starts at the start")
        self.assertTrue(all(cx.SUMMARY not in m for m in messages))


class BudgetTest(unittest.TestCase):
    def test_the_estimate_counts_the_prompt_and_schemas_the_caller_measured(self):
        fixed = cx.fixed_tokens("p" * 340, [{"name": "t"}])
        self.assertEqual(fixed, round((340 + len(json.dumps([{"name": "t"}]))) / cx.CHARS_PER_TOKEN))
        messages = [{"role": "user", "content": "x" * 340}]
        self.assertEqual(cx.estimate_tokens(messages, fixed) - cx.estimate_tokens(messages, 0), fixed)

    def test_calibration_scales_the_estimate_within_bounds(self):
        session = {}
        cx.calibrate(session, estimated=1000, reported=1300)
        self.assertAlmostEqual(session["token_ratio"], 1.3)
        cx.calibrate(session, estimated=1000, reported=10)
        self.assertEqual(session["token_ratio"], cx.MIN_RATIO)
        cx.calibrate(session, estimated=0, reported=10)
        self.assertEqual(session["token_ratio"], cx.MIN_RATIO, "no estimate, no change")


class WireTest(unittest.TestCase):
    def test_private_markers_are_never_sent(self):
        messages = turn(1, 1, 10)
        messages[0][cx.SUMMARY] = "nothing yet"
        messages[1][cx.FOLDED] = True
        wired = cv.wire_messages("0" * 32, messages)
        self.assertFalse(any(k.startswith(cv.PRIVATE_PREFIX) for m in wired for k in m))
        self.assertEqual([m["role"] for m in wired], [m["role"] for m in messages])


if __name__ == "__main__":
    unittest.main()
