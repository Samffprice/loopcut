"""motion_claims: which scene objects a reply says move, and whether they do, on a stand-in scene (FakeScene) whose
objects' world matrices follow the frame. The loop side is in test_agent_loop.MotionClaimTest."""

import math
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))
from loopcut import motion_claims  # noqa: E402

MOONS = ["Planet", "Moon_1", "Moon_2", "Moon_3", "Moon_4", "Moon_5"]
# rel017-app (0.2.17, Free/GLM): the first turn's last reply and the recap, the moons built and never animated.
REL017_SCENE = ["SunLight", "Moons", "Moons_1", "Moons_2", "Moons_3", "Moons_4", "Moons_5", "SaturnPlanet",
                "SaturnPlanet_FRing", "SaturnPlanet_PlanetBody", "SaturnPlanet_Rings", "Camera", "Spaceship"]
REL017_FIRST = ("What's in the scene now:\n- **SaturnPlanet** (worker-built): banded gas giant ~10 m across\n"
                "- **Moons** (worker-built): 5 cratered icy/rocky moons on orbits 17–30 m out\n\n"
                "Want a final F12 render, an orbit animation of the moons, or any tweaks to the ring tilt/looks?")
REL017_RECAP = ("Here's what's in the scene:\n\n1. **SaturnPlanet** — banded gas giant (~10 m sphere)\n"
                "4. **Moons_1–5** — five cratered moons orbiting 17–30 m out\n"
                "5. **Spaceship** — small ship with glowing engines, animated on a 240-frame cruise loop past the "
                "rings")


def turned(degrees, x=0.0, y=0.0, z=0.0):
    """World-matrix rows: a turn about Z, then a move to (x, y, z)."""
    c, s = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
    return [[c, -s, 0.0, x], [s, c, 0.0, y], [0.0, 0.0, 1.0, z], [0.0, 0.0, 0.0, 1.0]]


def orbit(frame, radius=5.0, speed=1.5):
    """A moon on its orbit at a frame: about the origin, `speed` degrees a frame."""
    angle = math.radians(frame * speed)
    return turned(frame * speed, radius * math.cos(angle), radius * math.sin(angle))


class FakeObject:
    def __init__(self, scene, name, kind, at, action, parent, triangles):
        self.scene, self.name, self.type, self.parent = scene, name, kind, parent
        self.at = at or (lambda frame: turned(0.0, 3.0))
        self.animation_data = types.SimpleNamespace(action=object(), nla_tracks=[], drivers=[]) if action else None
        self.data = (types.SimpleNamespace(animation_data=None, shape_keys=None, loop_triangles=range(triangles))
                     if kind == "MESH" else None)
        self.modifiers, self.constraints, self.rigid_body = [], [], None

    @property
    def children_recursive(self):
        def above(ob):
            while ob.parent is not None:
                ob = ob.parent
                yield ob
        return [o for o in self.scene.objects if self in above(o)]

    @property
    def matrix_world(self):
        return self.at(self.scene.frame_current)

    def evaluated_get(self, _graph):
        return self


def node_group(name, *nodes, keyed=False):
    """A Geometry Nodes group as motion_claims reads it: its nodes, and keys of its own."""
    keys = types.SimpleNamespace(action=object(), nla_tracks=[], drivers=[]) if keyed else None
    return types.SimpleNamespace(name=name, nodes=list(nodes), animation_data=keys)


def node(bl_idname, mute=False, group=None):
    return types.SimpleNamespace(bl_idname=bl_idname, mute=mute, node_tree=group)


def with_nodes(ob, group):
    ob.modifiers.append(types.SimpleNamespace(type="NODES", node_group=group))
    return ob


SCENE_TIME = "GeometryNodeInputSceneTime"


class Objects:
    def __init__(self):
        self.named = {}

    def get(self, name):
        return self.named.get(name)

    def __iter__(self):
        return iter(list(self.named.values()))


class FakeScene:
    """bpy's scene as motion_claims reads it: frames, objects and frame_set, each call recorded in `set`."""

    def __init__(self, start=1, end=250, current=42, subframe=0.5):
        self.frame_start, self.frame_end, self.frame_current, self.frame_subframe = start, end, current, subframe
        self.objects, self.set = Objects(), []

    def frame_set(self, frame, subframe=0.0):
        self.set.append((frame, subframe))
        self.frame_current, self.frame_subframe = frame, subframe

    def add(self, name, kind="MESH", at=None, action=False, parent=None, triangles=12):
        ob = self.objects.named[name] = FakeObject(self, name, kind, at, action, parent, triangles)
        return ob

    def bpy(self):
        graph = lambda: types.SimpleNamespace(objects=list(self.objects))
        return types.SimpleNamespace(context=types.SimpleNamespace(scene=self, evaluated_depsgraph_get=graph))

    def check(self, text):
        with mock.patch.dict(sys.modules, {"bpy": self.bpy()}):
            return motion_claims.check(text)


def moons(how="still", **kwargs):
    """Planet and Moon_1…Moon_5, the moons still, keyed, turned by a pivot (keyed, or itself on a path, which only
    stepping frames shows), on a path of their own, or wobbling in Geometry Nodes that read Scene Time."""
    scene = FakeScene(**kwargs)
    scene.add("Planet", at=lambda frame: turned(0.0))
    pivot = scene.add("Pivot", "EMPTY", at=lambda frame: turned(frame * 1.5), action=how == "keyed pivot") \
        if how in ("keyed pivot", "pivot on a path") else None
    for i in range(1, 6):
        moving = how in ("pivot on a path", "path") or (how == "keyed pivot" and i % 2)  # Coinciding frames: keys say.
        moon = scene.add(f"Moon_{i}", at=(lambda frame, r=10.0 + i: orbit(frame, r)) if moving else None,
                         action=how == "keyed", parent=pivot)
        if how == "scene time":  # Nested a group down; the object itself never moves.
            time = node_group("Time", node(SCENE_TIME))
            with_nodes(moon, node_group("Wobble", node("GeometryNodeGroup", group=time)))
    return scene


class ClaimsTest(unittest.TestCase):
    def test_rel017_first_reply_and_recap_name_the_moons(self):
        group = ["Moons", "Moons_1", "Moons_2", "Moons_3", "Moons_4", "Moons_5"]
        self.assertEqual(motion_claims.claims(REL017_FIRST, REL017_SCENE), [("orbits", group)])
        self.assertEqual(motion_claims.claims(REL017_RECAP, REL017_SCENE),
                         [("orbiting", group), ("animated", ["Spaceship"])])

    def test_five_moons_orbiting_matches_moon_1_to_moon_5(self):
        for text in ("5 moons orbiting the planet.", "- **5 moons** orbiting", "Each moon orbits at its own speed.",
                     "Added 5 small orbiting moons.", "Moon_1, Moon_2, Moon_3, Moon_4 and Moon_5 orbit Planet."):
            with self.subTest(text=text):
                self.assertTrue(motion_claims.may_claim(text))
                [(_, names)] = motion_claims.claims(text, MOONS)
                self.assertEqual(names, MOONS[1:])
        self.assertEqual(motion_claims.claims("Moon.001 and Moon.002 spin.", ["Moon.001", "Moon.002"]),
                         [("spin", ["Moon.001", "Moon.002"])])

    def test_what_moves_is_the_subject_not_what_it_moves_around(self):
        self.assertEqual(motion_claims.claims("Five moons orbit the planet.", MOONS), [("orbit", MOONS[1:])])
        self.assertEqual(motion_claims.claims("A planet with 5 moons orbiting it.", MOONS), [("orbiting", MOONS[1:])])
        self.assertEqual(motion_claims.claims("The planet spins and Moon_2 orbits it.", MOONS),
                         [("spins", ["Planet"]), ("orbits", ["Moon_2"])])

    def test_conditional_future_offered_or_denied_is_no_claim(self):
        for text in ("Want me to animate the orbit next?", "The moons could orbit.", "I'll make the moons orbit.",
                     "If you like, the moons can orbit faster.", "The moons will orbit once keyed.",
                     "Options: the moons orbit, or the camera moves.", "The moons don't move yet.",
                     "Moon_1 is not animated.", "Would the moons orbiting help?"):
            with self.subTest(text=text):
                self.assertFalse(motion_claims.may_claim(text))
                self.assertEqual(motion_claims.claims(text, MOONS), [])

    def test_unrelated_replies_claim_nothing(self):
        for text in ("Built a planet and five moons.", "I moved the planet up and rotated Moon_1.",
                     "The script runs fine.", "The moons are floating in the black.",
                     "```python\nmoon.rotation_euler.z += 1  # Moon_1 moves\n```", "Done.", ""):
            with self.subTest(text=text[:30]):
                self.assertEqual(motion_claims.claims(text, MOONS), [])


class StillTest(unittest.TestCase):
    def test_a_still_object_is_named_with_the_frames_compared(self):
        self.assertEqual(moons().check("Five moons orbit the planet."),
                         {"names": MOONS[1:], "move": "orbit", "frames": (1, 125)})
        self.assertEqual(moons(start=1, end=30).check("Moon_3 spins.")["frames"], (1, 25), "24 frames on at least")

    def test_keyed_objects_a_moving_pivot_and_a_path_move(self):
        for how in ("keyed", "keyed pivot", "pivot on a path", "path", "scene time"):
            with self.subTest(how=how):
                scene = moons(how)
                self.assertIsNone(scene.check("Five moons orbit the planet."))
                # Keys and time nodes are read without stepping frames; a pivot or moon moved by anything else is
                # stepped through.
                self.assertEqual(len(scene.set), 3 if how in ("pivot on a path", "path") else 0)

    def test_geometry_nodes_that_read_the_frame_move_without_stepping_frames(self):
        # Their motion is inside an unmoving object: no world matrix shows it (a false nudge would re-animate it).
        nested = node_group("Wobble", node("GeometryNodeSetPosition"),
                            node("GeometryNodeGroup", group=node_group("Time", node(SCENE_TIME))))
        for how, group in (("scene time, nested", nested),
                           ("simulation zone", node_group("Sim", node("GeometryNodeSimulationInput"))),
                           ("keys in the group", node_group("Pulse", node("GeometryNodeTransform"), keyed=True))):
            with self.subTest(how=how):
                scene = FakeScene()
                with_nodes(scene.add("Jelly"), group)
                self.assertIsNone(scene.check("The jelly bounces."))
                self.assertEqual(scene.set, [])

    def test_geometry_nodes_without_a_time_node_are_checked_by_frames(self):
        looped = node_group("A", node("GeometryNodeTransform"))
        looped.nodes.append(node("GeometryNodeGroup", group=node_group("B", node("GeometryNodeGroup", group=looped))))
        for how, group in (("no time node", node_group("Scatter", node("GeometryNodeDistributePointsOnFaces"))),
                           ("a muted Scene Time", node_group("Off", node(SCENE_TIME, mute=True))),
                           ("groups nesting each other", looped)):
            with self.subTest(how=how):
                scene = FakeScene()
                with_nodes(scene.add("Jelly"), group)
                self.assertEqual(scene.check("The jelly bounces."),
                                 {"names": ["Jelly"], "move": "bounces", "frames": (1, 125)})
                self.assertEqual(len(scene.set), 3)

    def test_an_action_with_no_slot_assigned_still_counts(self):
        # Blender 5's slotted actions: an action on the object, its data or shape keys whose slot is unassigned plays
        # nothing, but counts as moving (a missed claim is the cheaper mistake).
        unslotted = lambda: types.SimpleNamespace(action=object(), action_slot=None, nla_tracks=[], drivers=[])
        for owner in ("object", "data", "shape keys"):
            with self.subTest(owner=owner):
                scene = FakeScene()
                ob = scene.add("Jelly")
                if owner == "object":
                    ob.animation_data = unslotted()
                elif owner == "data":
                    ob.data.animation_data = unslotted()
                else:
                    ob.data.shape_keys = types.SimpleNamespace(animation_data=unslotted())
                self.assertIsNone(scene.check("The jelly bounces."))
                self.assertEqual(scene.set, [])

    def test_a_rig_shape_keys_or_a_simulation_count_as_moving(self):
        scene = FakeScene()
        dog = scene.add("Dog")
        rig = scene.add("Dog Rig", "ARMATURE", action=True)
        dog.modifiers.append(types.SimpleNamespace(type="ARMATURE", object=rig))
        scene.add("Blob").data.shape_keys = types.SimpleNamespace(
            animation_data=types.SimpleNamespace(action=object(), nla_tracks=[]))
        scene.add("Ball").rigid_body = types.SimpleNamespace(type="ACTIVE")  # A jump between frames runs no simulation.
        self.assertIsNone(scene.check("The dog gallops, the blob bounces and the ball bounces."))
        self.assertEqual(scene.set, [])

    def test_the_current_frame_and_subframe_are_put_back(self):
        scene = moons()
        scene.check("Moon_1 orbits.")
        self.assertEqual((scene.frame_current, scene.frame_subframe), (42, 0.5))
        self.assertEqual(scene.set, [(1, 0.0), (125, 0.0), (42, 0.5)])
        scene = moons()
        scene.frame_set = mock.Mock(side_effect=[None, RuntimeError("handler failed"), None])
        with self.assertRaises(RuntimeError):
            scene.check("Moon_1 orbits.")
        self.assertEqual(scene.frame_set.call_args, mock.call(42, subframe=0.5), "put back after a failure too")

    def test_a_heavy_scene_is_not_stepped_through(self):
        scene = moons()
        scene.add("Sculpt", triangles=motion_claims.MAX_TRIANGLES + 1)
        self.assertIsNone(scene.check("Five moons orbit the planet."))
        self.assertEqual(scene.set, [])

    def test_a_groups_empty_moves_with_what_it_holds_and_goes_unsaid(self):
        for how in ("keyed", "on paths", "still"):
            with self.subTest(how=how):
                scene = FakeScene()
                group = scene.add("Moons", "EMPTY")
                for i in range(1, 4):
                    scene.add(f"Moons_{i}", action=how == "keyed", parent=group,
                              at=(lambda frame, r=10.0 + i: orbit(frame, r)) if how == "on paths" else None)
                found = scene.check("The moons orbit Saturn.")
                still = ["Moons_1", "Moons_2", "Moons_3"] if how == "still" else None
                self.assertEqual(found and found["names"], still)

    def test_nudge_and_notice_texts(self):
        found = {"names": MOONS[1:], "move": "orbiting", "frames": (1, 125)}
        self.assertEqual(motion_claims.nudge(found),
                         "[Loopcut] Your reply says Moon_1, Moon_2, Moon_3, Moon_4 and Moon_5 orbiting, but they do "
                         "not move between frames 1 and 125: animate them now, or say plainly that they don't move "
                         "yet.")
        self.assertEqual(motion_claims.notice(found), "Moon_1, Moon_2, Moon_3, Moon_4 and Moon_5 don't move yet.")
        one = {"names": ["Planet"], "move": "spins", "frames": (1, 25)}
        self.assertEqual(motion_claims.nudge(one),
                         "[Loopcut] Your reply says Planet spins, but it does not move between frames 1 and 25: "
                         "animate it now, or say plainly that it doesn't move yet.")
        self.assertEqual(motion_claims.notice(one), "Planet doesn't move yet.")
        many = {"names": [f"Bird_{i}" for i in range(1, 9)], "move": "fly", "frames": (1, 125)}
        self.assertEqual(motion_claims.notice(many), "Bird_1, Bird_2, Bird_3, Bird_4 and 4 more don't move yet.")


if __name__ == "__main__":
    unittest.main()
