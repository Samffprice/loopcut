"""Craft selection, swaps and budgets over the body-free service index."""

import re
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

fake_bpy = types.ModuleType("bpy")
fake_bpy.app = types.SimpleNamespace(driver_namespace={})
sys.modules.setdefault("bpy", fake_bpy)

from loopcut import context, skills  # noqa: E402
from prompt_index_fixture import built_index, select, topics_for, selected_ids, expanded_chars

ROLES = ("modeling", "materials", "lighting", "camera", "animation")


def brief(quote: str, deliverable: str = "Thing", **more) -> dict:
    return {"id": "w1", "deliverable": deliverable, "prefix": "Thing", "requirements": [{"quote": quote, "do": quote}],
            "defaults": [], "checks": [], "owns": [], **more}


BRIEFS = [
    {},
    brief("a leather armchair by the window", "Armchair"),
    brief("a perfume bottle with a gold cap on a dark studio set; the camera slowly orbits it", "Bottle"),
    brief("a cosy living room at night with a ceiling fan that spins and a lamp", "LivingRoom"),
    brief("an icy blue crystal cave with glowing crystals, glass and marble, brass fittings, wood beams", "Cave"),
]


def tokens(text: str) -> float:
    return expanded_chars(text) / context.CHARS_PER_TOKEN


class SizeTest(unittest.TestCase):
    def test_every_role_is_400_to_900_tokens_for_any_brief(self):
        for role in ROLES:
            for b in BRIEFS:
                text = select(role, b)
                self.assertTrue(400 <= tokens(text) <= 900, (role, topics_for(role, b), round(tokens(text))))

    def test_a_shot_worker_gets_its_camera_craft_too(self):
        shot = brief("a slow orbit around the watch on a black set", "Watch", aspects=["lighting", "camera"])
        text = select("lighting", shot)
        self.assertIn('craft/lighting/shot-camera', text)
        self.assertLessEqual(tokens(text), 1200)
        self.assertNotIn('craft/lighting/shot-camera', select("lighting", brief("a watch on a black set")))

    def test_unknown_role_or_odd_brief(self):
        self.assertEqual(select("sculpting", {}), "")
        for odd in (None, "a string", [], {"requirements": "not a list", "defaults": [{"do": 3}]}):
            self.assertTrue(select("modeling", odd))


class TopicTest(unittest.TestCase):
    def test_the_briefs_words_pick_the_topics(self):
        self.assertEqual(topics_for("materials", brief("a clear glass vase with water"))[0], "glass")
        self.assertIn("metal", topics_for("materials", brief("a brushed steel kettle")))
        self.assertEqual(topics_for("modeling", brief("a mid-century sofa", "Sofa"))[:1], ["furniture"])
        self.assertIn("moves", topics_for("camera", brief("the camera orbits the product")))
        self.assertIn("loop", topics_for("animation", brief("blades spin forever")))
        self.assertIn("mood", topics_for("lighting", brief("a dark, moody bar at night")))
        self.assertIn("interior", topics_for("lighting", brief("a kitchen interior in the morning")))

    def test_everyday_topics_when_the_brief_names_none(self):
        for role in ROLES:
            self.assertEqual(topics_for(role, {}), next(e['everyday'] for e in built_index()['skills'] if e.get('craft') == role and e.get('part') == 'core'), role)

    def test_names_it_owns_are_not_words(self):
        self.assertEqual(topics_for("materials", {"owns": ["Glass_Case", "Brass_Lamp"], "deliverable": "Shelf"}),
                         topics_for('materials', {}))

    def test_the_most_mentioned_topic_comes_first_and_at_most_three(self):
        picked = topics_for("materials", BRIEFS[4])
        self.assertLessEqual(len(picked), skills.MAX_TOPICS)
        self.assertEqual(picked[0], "glass")








# The asset pack's barrel brief (eval 20260929-130617): its words contradicted three core items.
PACK = brief("a barrel", "Barrel", requirements=[
    {"quote": "Keep each prop a separate mesh object", "do": "One joined mesh named Barrel, no parent, modifiers applied"},
    {"quote": "no image textures", "do": "Assign only the shared Shop_* palette materials, per face; create no materials"}],
    defaults=["Flat shading"], style="Classic stylised tavern barrel, low-poly and chunky.")
PACK_NOTES = "Matching low-poly fantasy shop pack, flat colours, bevelled edges, flat shading."


class SwapTest(unittest.TestCase):
    """The core's defaults give way to what the brief says (eval 20260929 replay, analysis §5.1)."""


    def test_a_plain_brief_keeps_the_defaults(self):
        self.assertFalse(any('/swap-' in i for i in selected_ids(select('modeling', brief('an oak dining chair')))))

    def test_a_joined_flat_palette_only_brief(self):
        ids = selected_ids(select('modeling', PACK))
        self.assertTrue({'craft/modeling/swap-1', 'craft/modeling/swap-2', 'craft/modeling/swap-3'} <= set(ids))
        self.assertLess(ids.index('craft/modeling/core'), ids.index('craft/modeling/swap-1'))

    def test_each_swap_on_its_own(self):
        for said, identity in [('one joined mesh', 'swap-1'), ('flat shading', 'swap-2'),
                               ('do not create materials', 'swap-3')]:
            with self.subTest(said=said):
                self.assertIn('craft/modeling/' + identity, selected_ids(select('modeling', brief(said))))
        self.assertNotIn('craft/modeling/swap-2', select('modeling', brief('flat colours from one palette')))

    def test_the_waves_notes_count_too(self):
        self.assertNotIn('craft/modeling/swap-2', select('modeling', brief('a barrel')))
        self.assertIn('craft/modeling/swap-2', select('modeling', brief('a barrel'), PACK_NOTES))


class StylisedTest(unittest.TestCase):
    """A stylised or low-poly brief always gets the stylised topic, ahead of product or furniture (§5.2)."""

    def test_it_leads_over_more_mentioned_topics(self):
        potions = brief("three potion bottles: a glass bottle, a jar and a bottle with a cork", "Potions",
                        style="stylised low-poly potion bottles")
        self.assertEqual(topics_for("modeling", potions)[0], "stylised")
        shelf = brief("a wooden shelf with two shelves", "Shelf")
        self.assertEqual(topics_for("modeling", shelf)[0], "furniture")
        self.assertEqual(topics_for("modeling", shelf, PACK_NOTES)[0], "stylised")


    def test_it_fits(self):
        for b, notes in ((PACK, ""), (brief("a wooden shelf", "Shelf"), PACK_NOTES),
                         (brief("a stylised perfume bottle and jar", "Bottle"), "")):
            text = select("modeling", b, notes)
            self.assertIn("craft/modeling/stylised", text)
            self.assertLessEqual(expanded_chars(text), skills.MAX_CHARS + 2)

    def test_only_its_own_words_lead(self):
        self.assertNotEqual(topics_for("modeling", brief("a toy chest and a wooden chair and a table", "Chest"))[0],
                            "stylised")

    def test_paint_tools_offer_recipe_parameters(self):
        from loopcut import paint_tools
        offered = {s["function"]["name"]: s["function"]["parameters"]["properties"] for s in paint_tools.SCHEMAS}
        for name in ("unlit", "step", "rim", "ao", "paint", "normal", "orm", "key", "slot"):
            self.assertIn(name, offered["stylize_material"])
        self.assertIn("key", offered["bake_paint_masks"])



class PainterlyTest(unittest.TestCase):
    """A painted or toon look gets its tested recipes first, and the realism core's "subtle" gives way
    (the Arcane pass, eval 20260929-235231, came out restrained)."""
    ARCANE = brief("repaint the potion bottle", "PotionBottle", style="Arcane-style hand-painted look, stepped shading")

    def test_it_leads_for_materials_and_lighting(self):
        self.assertEqual(topics_for("materials", self.ARCANE)[0], "painterly")
        self.assertEqual(topics_for("materials", brief("a wooden chest"), "Look pass: painterly brush strokes")[0],
                         "painterly")
        self.assertEqual(topics_for("materials", brief("a cel-shaded robot", "Robot"))[0], "toon")
        self.assertEqual(topics_for("lighting", self.ARCANE)[0], "painterly")
        for plain in (brief("arcane runes carved on an oak door", "Door"), brief("a clear glass vase", "Vase")):
            self.assertNotIn("painterly", topics_for("materials", plain))



    def test_it_fits(self):
        for b, notes in ((self.ARCANE, ""), (brief("a wooden chest"), "painterly"), (brief("a toon robot"), "")):
            self.assertLessEqual(expanded_chars(select("lighting", b, notes)), skills.MAX_CHARS + 2, notes)
            for role in ("modeling", "materials"):
                self.assertLessEqual(expanded_chars(select(role, b, notes)), skills.PAINT_MAX_CHARS, (role, notes))
        shot = brief("a slow orbit", "Shot", aspects=["lighting", "camera"], style="painterly")
        self.assertLessEqual(expanded_chars(select("lighting", shot)), skills.SHOT_MAX_CHARS)

    def test_hand_painted_is_not_plastic(self):
        self.assertNotIn("plastic", topics_for("materials", self.ARCANE))
        self.assertIn("plastic", topics_for("materials", brief("a painted toy car", "Car")))


class PaintRoutingTest(unittest.TestCase):
    """The paint recipes follow the brief, not the role: eval 20260930-105528 delegated all 28 workers of an
    Arcane look pass as modeling, and 0 of 137 worker prompts carried the recipe."""
    ARCANE = brief("rework the chest's lid and repaint it", "Chest", style="Arcane-style, hand-painted")

    def test_a_modeling_worker_on_a_painted_brief_gets_the_recipe(self):
        text = select("modeling", self.ARCANE)
        self.assertEqual(topics_for("modeling", self.ARCANE)[0], "painterly")
        self.assertIn('craft/modeling/painterly', text)
        self.assertIn('craft/modeling/swap-4', text)
        notes = select("modeling", brief("a wooden chest", "Chest"), "Look pass: Arcane-style, hand-painted")
        self.assertIn('craft/modeling/painterly', notes)
        toon = select("modeling", brief("a cel-shaded robot", "Robot"))
        self.assertIn('craft/modeling/toon', toon)

    def test_the_series_by_name_is_painterly(self):
        # Eval 20260930-151649: the user asked for "the look of the animated series Arcane".
        for said in ("the look of the animated series Arcane", "styled like the show Arcane"):
            self.assertIn('craft/modeling/painterly', select("modeling", brief("a wooden chest", "Chest"), said))

    def test_a_plain_modeling_brief_does_not(self):
        for plain in (brief("a wooden chest with iron bands", "Chest"), brief("a painted steel locker", "Locker"),
                      brief("arcane runes carved on an oak door", "Door")):
            text = select("modeling", plain)
            self.assertNotIn("/painterly", text)
            self.assertNotIn("/toon", text)
            self.assertNotIn('craft/modeling/swap-4', text)
            self.assertLessEqual(expanded_chars(text), skills.MAX_CHARS + 2)

    def test_roles_that_do_not_paint(self):
        for role in ("camera", "animation"):
            self.assertNotIn("/painterly", select(role, self.ARCANE))
        light = select("lighting", self.ARCANE)
        self.assertIn("craft/lighting/painterly", light)
        self.assertNotIn('craft/modeling/painterly', light)


if __name__ == "__main__":
    unittest.main()
