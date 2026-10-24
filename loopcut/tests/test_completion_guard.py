"""completion_guard.announces_step: a reply that says what it will do next and stops is continued; a reply
that asks, hands off or reports the work done never is. The loop side is in test_agent_loop.AnnouncedStepTest."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))
from loopcut import completion_guard  # noqa: E402

# Final replies seen 2026-10-23 on hosted Fast, each of which ended its turn with the job barely started.
REAL = (
    "I have successfully imported the Modern Wooden Floor TV Console. To start building out the VTuber room, I "
    "will next import the Gaming Desk with Shelves (pick #2) to provide a place for a monitor and setup.",
    "I have imported the drawing tablet and the penholder. These look like a good start for the desk area of the "
    "VTuber's setup. Since these items are small, I'll now search for larger environmental assets like \"room\" or "
    "\"study\" setups to build the actual room. I'll use the BlenderKit library again for variety.",
    "I apologize, it seems like I'm having persistent trouble ... Let's try a broader approach. ... I will try "
    "searching BlenderKit for a vintage desk.",
)


class AnnouncesStepTest(unittest.TestCase):
    def test_real_replies_that_stopped_after_announcing_a_step(self):
        for text in REAL:
            with self.subTest(text=text[:50]):
                self.assertTrue(completion_guard.announces_step(text))

    def test_other_announcements(self):
        for text in ("Next, I'll add a chair.", "Let me check the scene.", "Now let me search Poly Haven for a rug.",
                     "I'm going to place the lamp on the desk.", "I’ll go ahead and import the bookshelf.",
                     "**Next, I'll add warm lights.** This will give the room a cozy feel.",
                     "The desk should include drawers. I'll now import one.",
                     "Here's what I did:\n- Imported a desk\n- Imported a chair\n\nNext, I will add the lighting.",
                     "BlenderKit returned nothing useful. I'll try Poly Haven instead.",
                     "I am about to build the walls."):
            with self.subTest(text=text):
                self.assertTrue(completion_guard.announces_step(text))

    def test_verbs_fast_uses_for_scene_work(self):
        # A/B 2026-10-23: a Fast reply that ended its turn unnudged because "conduct" was not a step verb.
        self.assertTrue(completion_guard.announces_step(
            "I will conduct a new search in BlenderKit, looking for \"office\" or \"room\" models, as this will "
            "give me larger pieces to start building the space."))
        for text in ("I'll perform a boolean cut for the window.", "Let me measure the desk first."):
            with self.subTest(text=text):
                self.assertTrue(completion_guard.announces_step(text))

    def test_a_bare_do_is_not_a_step(self):
        # "do" is too broad to count as a tool step ("I'll do my best to match it."): a miss costs one "go on".
        self.assertFalse(completion_guard.announces_step("I'll do my best to match it."))

    def test_step_verbs_in_hand_offs_and_questions(self):
        for step in ("conduct a new search", "perform the boolean cut", "run the cleanup", "build the walls",
                     "make the desk", "model the chair", "set up the lighting", "start on the room",
                     "begin the layout", "apply the material", "move the lamp", "scale the rug", "rotate the chair",
                     "position the camera", "arrange the shelves", "furnish the room", "light the scene",
                     "render a preview", "capture the viewport", "inspect the mesh", "measure the desk",
                     "fix the gaps", "adjust the camera", "replace the sofa", "delete the cube", "remove the plane",
                     "retry the import", "do the walls"):
            for text in (f"I'll {step} once you confirm.", f"Should I {step}?", f"Say the word and I'll {step}.",
                         f"I can {step} next. Would you like that"):
                with self.subTest(text=text):
                    self.assertFalse(completion_guard.announces_step(text))

    def test_questions_and_choices_for_the_user(self):
        for text in ("What size should it be?", "Would you like me to add a chair? I'll import it next.",
                     "I imported the desk. Do you want a chair too", "Should I import the oak desk or the walnut one",
                     "I can add a lamp if you'd like.", "Pick one of these: oak or walnut. I'll import it then.",
                     "Which style do you prefer? Let me know and I'll import it.",
                     "I imported the desk. Shall I continue with the chair"):
            with self.subTest(text=text):
                self.assertFalse(completion_guard.announces_step(text))

    def test_finished_stopping_and_hand_off_replies(self):
        for text in ("Done.", "Done. The room is ready.", "Let me know if you want changes.",
                     "I imported the console and the desk. Let me know if you want changes.",
                     "The room is complete. Let me know if you'd like changes.", "That's all for now.",
                     "I'm stopping here because the search failed.", "All done! I'll leave the rest to you.",
                     "I imported the desk.", "Next steps: you could add lighting.", "Feel free to tweak the colors.",
                     "I'll stop here; BlenderKit keeps timing out."):
            with self.subTest(text=text):
                self.assertFalse(completion_guard.announces_step(text))

    def test_steps_that_wait_on_the_user_or_a_later_turn(self):
        for text in ("I'll create it after you choose a size.", "I'll create a desk once you confirm the style.",
                     "I'll add lights later.", "I'll add the rug in the next message.",
                     "I'll adjust the lighting if needed.", "I'll continue when you're ready."):
            with self.subTest(text=text):
                self.assertFalse(completion_guard.announces_step(text))

    def test_hand_offs_without_a_question_mark(self):
        # Review of f7094fe5bc6: each of these was nudged; after the desk list Loopcut picked for the user.
        for text in ("Say the word and I'll build it.", "Just pick one and I'll import it.",
                     "I'll import the one you pick.", "If this looks good, I'll move on to the lighting next.",
                     "Once approved, I'll start with the walls.", "Give me the go-ahead and I'll start building.",
                     "Sign in to BlenderKit in Preferences, then I'll import the desk.",
                     "Go ahead and select the mesh, then I'll apply the material.",
                     "I found three desks:\n1. Oak writing desk\n2. Walnut gaming desk\n3. Glass corner desk\n\n"
                     "Just pick one and I'll import it."):
            with self.subTest(text=text):
                self.assertFalse(completion_guard.announces_step(text))

    def test_not_an_announced_step(self):
        for text in ("", "Let me explain how this works.", "I would add a lamp next.", "I won't import that.",
                     "I can't create that.", "Plan: build the mesh, then add materials.",
                     "I will use this call:\nrun_python({\"code\": \"pass\"})",
                     "Let me import the desk with this script:\n```python\nimport bpy\n```",
                     "Here's the plan:\n1. I'll import a desk\n2. I'll add a chair",
                     # Announced earlier, then reported on: narration of steps taken, not a step to take.
                     "I'll now search for a desk. The search returned five results. I imported the oak one. "
                     "It sits against the wall.", "Use run_python to create the mesh."):
            with self.subTest(text=text):
                self.assertFalse(completion_guard.announces_step(text))
        self.assertFalse(completion_guard.announces_step(None))

    def test_the_nudge_is_short_and_offers_an_honest_stop(self):
        self.assertEqual(completion_guard.CONTINUE,
                         "[Loopcut] Go ahead: do that now with your tools, or say plainly that you are stopping and why.")
        self.assertEqual(completion_guard.MAX_CONTINUES, 2)
        for marker in (completion_guard.CONTINUED, completion_guard.CONTINUED_AT):
            self.assertTrue(marker.startswith("loopcut_"), "private: never sent to the model")


class SharedNudgeTest(unittest.TestCase):
    """What the announced-step nudge and the record correction share: one budget a turn and one honest exit.
    The loop side is in test_agent_loop.UnmadeCallsTest."""
    FIXTURES = Path(__file__).parent / "fixtures"

    def test_the_record_correction_spends_from_the_announced_steps_budget(self):
        g = completion_guard
        for request, left in (({}, 2), ({g.CONTINUED: 1}, 1), ({g.CORRECTED: True}, 1),
                              ({g.CONTINUED: 1, g.CORRECTED: True}, 0), ({g.CONTINUED: 2}, 0)):
            with self.subTest(request=request):
                self.assertEqual(g.nudges_left(request), left)

    def test_a_plain_stop_or_nothing_ran_is_the_honest_exit_for_both(self):
        for text in ("Nothing ran: I only described the steps.", "Nothing has run yet.", "Nothing was run.",
                     "No tool ran, so the scene is as it was.", "No tools were run this turn.",
                     "I'm stopping here because the search failed.", "Stopping here: the search failed.",
                     "I’ll stop here; BlenderKit keeps timing out.", "I will stop now."):
            with self.subTest(text=text[:40]):
                self.assertTrue(completion_guard.stops_honestly(text))
        # Work claims are not an exit, though they say "nothing was ..." or "I'll stop ..." (reviews of the port:
        # each kept a written-out reply from its correction), nor is a wait: ar-artist-2's reply after rc's
        # correction among them, and p6c's, whose "Nothing was done this turn" goes on to report work.
        for text in ("I fixed the left shoulder and restored the pose. Nothing was changed on the legs.",
                     "Updated the material to aged brass; nothing was created, only edited.",
                     "Done: the shovel blade is now iron. Nothing was made from scratch.",
                     "The walk cycle is applied. I'll stop the animation at frame 120 so it loops.",
                     "The walk cycle is applied. I will stop the animation at frame 120.",
                     "I'm stopping the render at 64 samples.", "The import worked; nothing ran into the chest.",
                     "Nothing was done.", "I'll wait for the render to finish.", "All done.", "I'm done.", "Done.",
                     "Nothing looks wrong at the shoulders.", "The steps ran.", None,
                     (self.FIXTURES / "ar-artist-2-after-correction.txt").read_text(),
                     (self.FIXTURES / "p6c-more-details-after-correction.txt").read_text()):
            with self.subTest(text=str(text)[:40]):
                self.assertFalse(completion_guard.stops_honestly(text))

    def test_announces_step_reads_as_at_the_base(self):
        # The honest exit is the loop's (agent.run_loop), not announces_step's: a failed step followed by the next
        # one is still nudged (review of the port), and so is "Nothing ran yet. I'll now ...", until the loop
        # sees the exit (test_agent_loop.UnmadeCallsTest).
        for text in ("The desk import failed, nothing was made. Let me try a broader search.",
                     "Nothing ran yet. I'll now import the desk.", "The desk is in. I'll now import the chair."):
            with self.subTest(text=text):
                self.assertTrue(completion_guard.announces_step(text))

    def test_only_explicit_examples_and_earlier_steps_introduce_a_block(self):
        for line in ("Example:", "In the previous step:", "In the previous steps:", "Earlier steps:",
                     "- **For example**, a record reads:"):
            with self.subTest(line=line):
                self.assertTrue(completion_guard.introduces_example(line))
        # Not an introduction for copied records, though imitates_records reads these as one (_ILLUSTRATION).
        for line in ("What did it print?", "- **Here is the code** I ran:",
                     "I'll redo the shoulder region with a proper geometric rule:",
                     *(self.FIXTURES / "ar-artist-2-written-out.txt").read_text().split("\n")):
            with self.subTest(line=line[:40]):
                self.assertFalse(completion_guard.introduces_example(line))

if __name__ == "__main__":
    unittest.main()
