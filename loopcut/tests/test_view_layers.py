"""view_layers: which view layer a read evaluates in and which one a production shot renders, on fake
layers. The renders, the reads in Blender and the worker child are loopcut/harness/prodview_check.py."""

import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

fake_bpy = types.ModuleType("bpy")
fake_bpy.app = types.SimpleNamespace(driver_namespace={})
sys.modules.setdefault("bpy", fake_bpy)

from loopcut import inspection, tools, view_layers, worker_capture, workers  # noqa: E402

N = types.SimpleNamespace


class Objects(list):
    def get(self, name):
        return next((o for o in self if o.name == name), None)


def obj(name, kind="MESH", energy=0.0, hide_render=False, collection_hidden=False):
    item = N(name=name, type=kind, library=None, hide_render=hide_render, data=N(energy=energy),
             users_collection=[N(hide_render=collection_hidden)])
    item.visible_get = lambda view_layer=None, item=item: item in view_layer.objects
    return item


def collection(name, exclude=False, holdout=False, indirect_only=False, children=()):
    return N(name=name, exclude=exclude, holdout=holdout, indirect_only=indirect_only, children=list(children))


def layer(name, objects, use=True, children=(), world=None, material=None, samples=0):
    return N(name=name, objects=Objects(objects), use=use, layer_collection=collection("Scene Collection",
             children=children), world_override=world, material_override=material, samples=samples)


class Staged(unittest.TestCase):
    """The Arcane chest staging: the active layer excludes Stage and does not render; ChestStage
    excludes Pack, renders, and overrides the world; Ghost is in a collection every layer excludes."""

    def setUp(self):
        self.crate, self.pack_light = obj("Crate"), obj("PackLight", "LIGHT", 800.0)
        self.chest, self.camera = obj("Chest"), obj("HeroCam", "CAMERA")
        self.key, self.rim = obj("Key", "LIGHT", 250.0), obj("Rim", "LIGHT", 120.0)
        self.off, self.ghost = obj("Off", "LIGHT", 0.0), obj("Ghost")
        self.viewport = layer("ViewLayer", [self.crate, self.pack_light], use=False,
                              children=[collection("Pack"), collection("Stage", exclude=True),
                                        collection("Hidden", exclude=True)])
        self.stage = layer("ChestStage", [self.chest, self.camera, self.key, self.rim, self.off], world=N(name="Dusk"),
                           children=[collection("Pack", exclude=True, children=[collection("Inner")]),
                                     collection("Stage", children=[collection("Glass", holdout=True)]),
                                     collection("Hidden", exclude=True)])
        self.scene = N(name="Scene", view_layers=Objects([self.viewport, self.stage]), world=N(name="World"),
                       render=N(use_compositing=False, engine="CYCLES"),
                       objects=[self.crate, self.pack_light, self.chest, self.camera, self.key, self.rim, self.off,
                                self.ghost])
        for item in self.scene.view_layers:
            item.id_data = self.scene
        context = N(view_layer=self.viewport, scene=self.scene)
        patcher = mock.patch.object(view_layers, "bpy", N(context=context))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_object_is_read_in_the_layer_that_includes_it_the_active_one_first(self):
        self.assertIs(view_layers.home(self.crate, self.scene), self.viewport)
        self.assertIs(view_layers.home(self.camera, self.scene), self.stage)
        self.assertIsNone(view_layers.home(self.ghost, self.scene), "excluded everywhere: no placement to read")

    def test_one_layer_for_a_camera_and_its_subject_must_include_the_camera(self):
        self.assertEqual(view_layers.common([self.camera, self.chest], self.scene), (self.stage, []))
        layer_, missing = view_layers.common([self.camera, self.crate, self.pack_light], self.scene,
                                             required=[self.camera])
        self.assertIs(layer_, self.stage, "the most of the subject is in ViewLayer, but the camera is not")
        self.assertEqual(missing, [self.crate, self.pack_light])
        self.assertEqual(view_layers.common([], self.scene), (self.viewport, []))

    def test_the_note_says_which_layer_and_what_the_active_one_excludes(self):
        self.assertEqual(view_layers.elsewhere(self.stage, [self.camera, self.chest]),
                         "Evaluated in view layer ChestStage: the active one (ViewLayer) excludes HeroCam, Chest")
        self.assertEqual(view_layers.elsewhere(self.viewport, [self.crate]), "")
        self.assertEqual(view_layers.unavailable(["Ghost"]), "Ghost is excluded from every view layer: its placement "
                         "and geometry are unavailable in this context")

    def test_the_production_layer_is_the_one_that_renders(self):
        self.assertIs(view_layers.production(self.scene), self.stage, "the active one does not render")
        self.viewport.use = True
        self.assertIs(view_layers.production(self.scene), self.viewport, "the active one when it renders")
        self.assertIs(view_layers.production(self.scene, "ChestStage"), self.stage)
        with self.assertRaises(tools.ToolError) as caught:
            view_layers.production(self.scene, "Stage")
        self.assertIn("ViewLayer, ChestStage", str(caught.exception))

    def test_the_shot_is_said_by_name_layer_world_and_lights(self):
        self.assertEqual(view_layers.summary(self.scene, self.stage),
                         "view layer ChestStage (excludes Pack, Hidden; holdout Glass; world override Dusk)")
        self.assertEqual(view_layers.summary(self.scene, layer("Plain", [])), "view layer Plain")
        self.assertEqual([o.name for o in view_layers.lights(self.scene, self.stage)], ["Key", "Rim"],
                         "PackLight is excluded, Off has no power")
        self.assertEqual(view_layers.world(self.scene, self.stage).name, "Dusk")
        self.assertEqual(view_layers.world(self.scene, self.viewport).name, "World")
        self.assertEqual(view_layers.described(self.scene, self.stage),
                         {"view_layer": "view layer ChestStage (excludes Pack, Hidden; holdout Glass; world override Dusk)",
                          "world": "Dusk", "lights": ["Key", "Rim"]})

    def test_a_compositor_mixing_layers_is_the_shot_unless_one_is_asked_for(self):
        self.assertFalse(view_layers.composited(self.scene, False))
        self.viewport.use, self.scene.render.use_compositing = True, True
        self.assertTrue(view_layers.composited(self.scene, False))
        self.assertFalse(view_layers.composited(self.scene, True))
        self.assertIn("the compositor's mix of ViewLayer, ChestStage",
                      view_layers.described(self.scene, self.stage, asked=False)["view_layer"])

    def test_get_scene_info_lists_layers_only_when_there_is_more_than_one_plain_one(self):
        overview = view_layers.overview(self.scene)
        self.assertEqual(overview["active"], "ViewLayer")
        self.assertEqual(overview["layers"]["ChestStage"], {"use": True, "excludes": ["Pack", "Hidden"],
                                                            "holdout": ["Glass"], "world_override": "Dusk"})
        self.assertIsNone(view_layers.overview(N(view_layers=[layer("ViewLayer", [])])))

    def test_a_contact_sheet_cell_counts_only_the_layer_s_lights(self):
        self.assertFalse(worker_capture.unlit(self.scene, self.stage))
        self.assertTrue(worker_capture.unlit(self.scene, layer("Dark", [self.chest, self.off])))


class Interface(unittest.TestCase):
    def test_workers_can_ask_for_the_production_shot_and_are_told_when(self):
        capture = next(s for s in workers.schemas() if s["function"]["name"] == "capture_viewport")
        properties = capture["function"]["parameters"]["properties"]
        self.assertEqual(properties["camera"]["type"], "string")
        self.assertEqual(properties["view_layer"]["type"], "string")

    def test_the_orchestrator_s_inspection_takes_a_view_layer(self):
        properties = inspection.SCHEMAS[0]["function"]["parameters"]["properties"]
        self.assertEqual(properties["view_layer"], {"type": "string"})
        self.assertIn("view_layer", json.dumps(inspection.SCHEMAS))


if __name__ == "__main__":
    unittest.main()
