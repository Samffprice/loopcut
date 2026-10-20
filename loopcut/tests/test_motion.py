"""Motion results (contract 0.7): BVH parsing, bone maps and rig detection, apply_args plumbing, video inputs
and the apply_motion handler, without Blender. Retargeting itself runs in Blender: harness/motion_check.py.
"""

import hashlib
import json
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "addons_core"))
sys.path.insert(0, str(Path(__file__).parent))
sys.modules.setdefault("bpy", types.ModuleType("bpy"))
from loopcut import cloud, cloud_http, cloud_jobs, motion_retarget as mr  # noqa: E402
from test_cloud_jobs import PROJECT, Reply, Transport, job  # noqa: E402
import platform_contract  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "motion"
TARGET = {"type": "string", "minLength": 1, "maxLength": 63, "description": "Armature to put the motion on."}
IN_PLACE = {"type": "boolean", "description": "No root travel."}


def descriptor(name="motion_from_text", **changes):
    value = {
        "name": name, "namespace": "loopcut_cloud", "version": 1, "title": "Motion from text",
        "description": "Make motion.", "kind": "gen", "status": "live", "available": True, "latency": "minutes",
        "input_schema": {"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"],
                         "additionalProperties": False},
        "file_inputs": [], "output": {"type": "motion", "formats": ["bvh", "json"]}, "apply": "apply_motion",
        "apply_args": {"type": "object", "properties": {"target_armature": TARGET, "in_place": IN_PLACE},
                       "additionalProperties": False},
        "cost": {"credits_estimate": 25, "quote_required": False}, "approval": {"confirm_over_credits": 100},
        "backends": ["managed"], "byok": None, "bundle_input": None}
    value.update(changes)
    return value


def video_descriptor():
    return descriptor("motion_from_video", kind="assist", input_schema={"type": "object", "properties": {
        "video_seconds": {"type": "number"}, "start_s": {"type": "number"}, "end_s": {"type": "number"},
        "static_camera": {"type": "boolean"}}, "required": [], "additionalProperties": False},
        file_inputs=[{"field": "video", "accept": ["video/mp4", "video/quicktime", "video/webm"], "max_mb": 200,
                      "required": True}])


def mixamo(prefix="mixamorig:", fingers=True):
    """A Mixamo hierarchy: (name, parent) rows."""
    rows = [("Hips", None)]
    chain = [("Spine", "Hips"), ("Spine1", "Spine"), ("Spine2", "Spine1"), ("Neck", "Spine2"), ("Head", "Neck"),
             ("HeadTop_End", "Head")]
    for side in ("Left", "Right"):
        chain += [(f"{side}Shoulder", "Spine2"), (f"{side}Arm", f"{side}Shoulder"), (f"{side}ForeArm", f"{side}Arm"),
                  (f"{side}Hand", f"{side}ForeArm"), (f"{side}UpLeg", "Hips"), (f"{side}Leg", f"{side}UpLeg"),
                  (f"{side}Foot", f"{side}Leg"), (f"{side}ToeBase", f"{side}Foot"), (f"{side}Toe_End", f"{side}ToeBase")]
        for finger in ("Thumb", "Index", "Middle", "Ring", "Pinky") if fingers else ():
            parent = f"{side}Hand"
            for i in (1, 2, 3, 4):
                chain.append((f"{side}Hand{finger}{i}", parent))
                parent = f"{side}Hand{finger}{i}"
    rows += chain
    return [(prefix + n, prefix + p if p else None) for n, p in rows]


def unreal(spines=5, necks=2):
    rows = [("root", None), ("pelvis", "root")]
    parent = "pelvis"
    for i in range(1, spines + 1):
        rows.append((f"spine_0{i}", parent))
        parent = f"spine_0{i}"
    chest = parent
    for i in range(1, necks + 1):
        rows.append((f"neck_0{i}", parent))
        parent = f"neck_0{i}"
    rows.append(("head", parent))
    for s in "lr":
        rows += [(f"clavicle_{s}", chest), (f"upperarm_{s}", f"clavicle_{s}"), (f"lowerarm_{s}", f"upperarm_{s}"),
                 (f"hand_{s}", f"lowerarm_{s}"), (f"upperarm_twist_01_{s}", f"upperarm_{s}"),
                 (f"thigh_{s}", "pelvis"), (f"calf_{s}", f"thigh_{s}"), (f"foot_{s}", f"calf_{s}"),
                 (f"ball_{s}", f"foot_{s}"), (f"thumb_01_{s}", f"hand_{s}"), (f"thumb_02_{s}", f"thumb_01_{s}"),
                 (f"thumb_03_{s}", f"thumb_02_{s}"), (f"index_metacarpal_{s}", f"hand_{s}"),
                 (f"index_01_{s}", f"index_metacarpal_{s}"), (f"index_02_{s}", f"index_01_{s}"),
                 (f"index_03_{s}", f"index_02_{s}"), (f"ik_hand_{s}", "root")]
    return rows


def auto_rig_pro():
    rows = [("c_traj", None), ("c_root_master.x", "c_traj"), ("c_root.x", "c_root_master.x"),
            ("c_spine_01.x", "c_root_master.x"), ("c_spine_02.x", "c_spine_01.x"), ("c_spine_03.x", "c_spine_02.x"),
            ("c_neck.x", "c_spine_03.x"), ("c_head.x", "c_neck.x")]
    for s in "lr":
        rows += [(f"c_shoulder.{s}", "c_spine_03.x"), (f"c_arm_fk.{s}", f"c_shoulder.{s}"),
                 (f"c_forearm_fk.{s}", f"c_arm_fk.{s}"), (f"c_hand_fk.{s}", f"c_forearm_fk.{s}"),
                 (f"c_thigh_fk.{s}", "c_root.x"), (f"c_leg_fk.{s}", f"c_thigh_fk.{s}"),
                 (f"c_foot_fk.{s}", f"c_leg_fk.{s}"), (f"c_toes_fk.{s}", f"c_foot_fk.{s}"),
                 (f"c_hand_ik.{s}", "c_traj"), (f"c_foot_ik.{s}", "c_traj"), (f"c_index1_base.{s}", f"c_hand_fk.{s}"),
                 (f"c_index1.{s}", f"c_index1_base.{s}"), (f"c_index2.{s}", f"c_index1.{s}"),
                 (f"c_index3.{s}", f"c_index2.{s}")]
    return rows


def character_creator():
    rows = [("CC_Base_BoneRoot", None), ("CC_Base_Hip", "CC_Base_BoneRoot"), ("CC_Base_Pelvis", "CC_Base_Hip"),
            ("CC_Base_Waist", "CC_Base_Hip"), ("CC_Base_Spine01", "CC_Base_Waist"),
            ("CC_Base_Spine02", "CC_Base_Spine01"), ("CC_Base_NeckTwist01", "CC_Base_Spine02"),
            ("CC_Base_NeckTwist02", "CC_Base_NeckTwist01"), ("CC_Base_Head", "CC_Base_NeckTwist02"),
            ("CC_Base_JawRoot", "CC_Base_Head")]
    for s in "LR":
        rows += [(f"CC_Base_{s}_Clavicle", "CC_Base_Spine02"), (f"CC_Base_{s}_Upperarm", f"CC_Base_{s}_Clavicle"),
                 (f"CC_Base_{s}_UpperarmTwist01", f"CC_Base_{s}_Upperarm"),
                 (f"CC_Base_{s}_Forearm", f"CC_Base_{s}_Upperarm"), (f"CC_Base_{s}_Hand", f"CC_Base_{s}_Forearm"),
                 (f"CC_Base_{s}_Thigh", "CC_Base_Pelvis"), (f"CC_Base_{s}_Calf", f"CC_Base_{s}_Thigh"),
                 (f"CC_Base_{s}_Foot", f"CC_Base_{s}_Calf"), (f"CC_Base_{s}_ToeBase", f"CC_Base_{s}_Foot"),
                 (f"CC_Base_{s}_Index1", f"CC_Base_{s}_Hand"), (f"CC_Base_{s}_Index2", f"CC_Base_{s}_Index1"),
                 (f"CC_Base_{s}_Index3", f"CC_Base_{s}_Index2")]
    return rows


class BvhTest(unittest.TestCase):
    def test_fixtures_parse_as_soma77(self):
        for name, fps in (("gemx_dance_60.bvh", 6.0), ("kimodo_walk_60.bvh", 30.0)):
            with self.subTest(name=name):
                bvh = mr.parse_bvh((FIXTURES / name).read_text())
                self.assertEqual(len(bvh.joints), 78)  # Root plus SOMA's 77.
                self.assertEqual(len(bvh.frames), 60)
                self.assertAlmostEqual(bvh.fps, fps, places=3)
                self.assertEqual((bvh.joints[0].name, bvh.joints[1].name), ("Root", "Hips"))
                self.assertEqual(bvh.by_name["LeftShin"].parent.name, "LeftLeg")
                self.assertEqual(len(bvh.frames[0]), sum(len(j.channels) for j in bvh.joints))

    def test_end_sites_names_with_spaces_and_columns(self):
        text = ("HIERARCHY\nROOT Bip01 Pelvis\n{\n OFFSET 0 90 0\n CHANNELS 6 Xposition Yposition Zposition "
                "Zrotation Xrotation Yrotation\n JOINT Spine\n {\n  OFFSET 0 10 0\n  CHANNELS 3 Zrotation Xrotation "
                "Yrotation\n  End Site\n  {\n   OFFSET 0 5 0\n  }\n }\n}\nMOTION\nFrames: 2\nFrame Time: 0.04\n"
                + "0 90 0 0 0 0 1 2 3\n" * 2)
        bvh = mr.parse_bvh(text)
        self.assertEqual([j.name for j in bvh.joints], ["Bip01 Pelvis", "Spine"])
        self.assertEqual((bvh.joints[1].column, bvh.joints[1].end), (6, (0.0, 5.0, 0.0)))
        self.assertEqual(bvh.frames[1][6:], [1.0, 2.0, 3.0])
        self.assertAlmostEqual(bvh.fps, 25.0)

    def test_malformed_files_are_refused(self):
        good = (FIXTURES / "kimodo_walk_60.bvh").read_text()
        cases = {"short": good.rsplit("\n", 2)[0], "nan": good.replace(" 0 ", " nan ", 1), "no frames":
                 good.replace("Frames: 60", "Frames: 0"), "braces": good.replace("{", "", 1),
                 "channels": good.replace("CHANNELS 3", "CHANNELS 4", 1), "empty": "", "words": "HELLO"}
        for name, text in cases.items():
            with self.subTest(name=name), self.assertRaises(mr.MotionError):
                mr.parse_bvh(text)

    def test_meta_keeps_known_fields_only(self):
        meta = mr.read_meta(json.dumps({"fps": 30, "frames": 240, "source": "gem-x", "fingers_animated": False,
                                        "warnings": ["hands  occluded", 3, "x" * 300] + ["w"] * 9, "fps2": 1}))
        self.assertEqual(meta["fps"], 30)
        self.assertIs(meta["fingers_animated"], False)
        self.assertEqual(meta["warnings"][0], "hands occluded")
        self.assertEqual(len(meta["warnings"][1]), 200)
        self.assertEqual(len(meta["warnings"]), 4)
        self.assertEqual(mr.read_meta("not json"), {})
        self.assertEqual(mr.read_meta("[1]"), {})


class RigDetectionTest(unittest.TestCase):
    def test_soma_maps_leg_to_thigh_and_finger_two_to_proximal(self):
        bvh = mr.parse_bvh((FIXTURES / "gemx_dance_60.bvh").read_text())
        rig = mr.detect([(j.name, j.parent.name if j.parent else None) for j in bvh.joints])
        self.assertEqual(rig.kind, "soma")
        self.assertEqual((rig.bones["thigh_l"], rig.bones["calf_l"]), ("LeftLeg", "LeftShin"))
        self.assertEqual((rig.bones["index0_l"], rig.bones["index1_l"]), ("LeftHandIndex1", "LeftHandIndex2"))
        self.assertEqual(rig.root, "Hips")
        self.assertEqual(set(rig.extra), {"Jaw", "LeftEye", "RightEye"})

    def test_mixamo_with_any_prefix(self):
        for prefix in ("mixamorig:", "", "mixamorig1:", "mixamorig_"):
            with self.subTest(prefix=prefix):
                rig = mr.detect(mixamo(prefix))
                self.assertEqual(rig.kind, "mixamo")
                self.assertEqual(rig.bones["thigh_l"], prefix + "LeftUpLeg")
                self.assertEqual(rig.bones["calf_l"], prefix + "LeftLeg")
                self.assertEqual(rig.bones["index1_r"], prefix + "RightHandIndex1")
                self.assertEqual([rig.bones[k] for k in ("spine1", "spine2", "chest", "neck1")],
                                 [prefix + n for n in ("Spine", "Spine1", "Spine2", "Neck")])
                self.assertEqual(rig.root, prefix + "Hips")
                self.assertNotIn(prefix + "LeftHandIndex4", rig.bones.values())

    def test_mixamo_without_fingers_still_maps_the_body(self):
        rig = mr.detect(mixamo(fingers=False))
        self.assertEqual(rig.kind, "mixamo")
        self.assertFalse(any(mr.part_of(k) in mr.FINGER_PARTS for k in rig.bones))

    def test_unreal_spreads_five_spine_bones_and_keeps_twists_out(self):
        rig = mr.detect(unreal())
        self.assertEqual(rig.kind, "unreal")
        self.assertEqual([rig.bones[k] for k in ("spine1", "spine2", "chest", "neck1", "neck2")],
                         ["spine_01", "spine_03", "spine_05", "neck_01", "neck_02"])
        self.assertEqual((rig.bones["forearm_l"], rig.bones["toe_r"], rig.bones["index0_l"]),
                         ("lowerarm_l", "ball_r", "index_metacarpal_l"))
        self.assertNotIn("upperarm_twist_01_l", rig.bones.values())
        self.assertEqual(rig.root, "pelvis")
        ue4 = mr.detect(unreal(spines=3, necks=1))
        self.assertEqual([ue4.bones[k] for k in ("spine1", "spine2", "chest", "neck1")],
                         ["spine_01", "spine_02", "spine_03", "neck_01"])
        self.assertNotIn("neck2", ue4.bones)
        upper = mr.detect([(n.upper() if n != "root" else n, p.upper() if p and p != "root" else p)
                           for n, p in unreal()])
        self.assertEqual((upper.kind, upper.bones["hips"]), ("unreal", "PELVIS"))

    def test_auto_rig_pro_controls_switch_to_fk_and_travel_on_the_master(self):
        rig = mr.detect(auto_rig_pro())
        self.assertEqual(rig.kind, "auto_rig_pro")
        self.assertEqual((rig.bones["hips"], rig.root), ("c_root.x", "c_root_master.x"))
        self.assertEqual((rig.bones["upperarm_l"], rig.bones["calf_r"], rig.bones["toe_l"]),
                         ("c_arm_fk.l", "c_leg_fk.r", "c_toes_fk.l"))
        self.assertEqual((rig.bones["index0_l"], rig.bones["index1_l"]), ("c_index1_base.l", "c_index1.l"))
        self.assertIn(("c_hand_ik.l", "ik_fk_switch", 1.0), rig.switches)
        self.assertIn(("c_foot_ik.r", "ik_fk_switch", 1.0), rig.switches)

    def test_rigify_metarig_and_generated_rig(self):
        meta = [("spine", None), ("spine.001", "spine"), ("spine.002", "spine.001"), ("spine.003", "spine.002"),
                ("spine.004", "spine.003"), ("spine.005", "spine.004"), ("spine.006", "spine.005")]
        for s in "LR":
            meta += [(f"shoulder.{s}", "spine.003"), (f"upper_arm.{s}", f"shoulder.{s}"),
                     (f"forearm.{s}", f"upper_arm.{s}"), (f"hand.{s}", f"forearm.{s}"), (f"thigh.{s}", "spine"),
                     (f"shin.{s}", f"thigh.{s}"), (f"foot.{s}", f"shin.{s}"), (f"toe.{s}", f"foot.{s}"),
                     (f"palm.01.{s}", f"hand.{s}"), (f"f_index.01.{s}", f"palm.01.{s}")]
        rig = mr.detect(meta)
        self.assertEqual(rig.kind, "rigify_metarig")
        self.assertEqual((rig.bones["hips"], rig.bones["head"], rig.bones["calf_l"], rig.bones["index0_l"]),
                         ("spine", "spine.006", "shin.L", "palm.01.L"))
        generated = [("root", None), ("torso", "root"), ("hips", "torso"), ("chest", "torso"), ("DEF-spine", None),
                     ("neck", "chest"), ("head", "neck")]
        for s in "LR":
            generated += [(f"shoulder.{s}", "chest"), (f"upper_arm_parent.{s}", "root"),
                          (f"upper_arm_fk.{s}", f"upper_arm_parent.{s}"), (f"forearm_fk.{s}", f"upper_arm_fk.{s}"),
                          (f"hand_fk.{s}", f"forearm_fk.{s}"), (f"thigh_parent.{s}", "root"),
                          (f"thigh_fk.{s}", f"thigh_parent.{s}"), (f"shin_fk.{s}", f"thigh_fk.{s}"),
                          (f"foot_fk.{s}", f"shin_fk.{s}"), (f"toe_fk.{s}", f"foot_fk.{s}")]
        rig = mr.detect(generated)
        self.assertEqual(rig.kind, "rigify")
        self.assertEqual((rig.bones["hips"], rig.bones["chest"], rig.root), ("hips", "chest", "torso"))
        fk = mr.detect(generated + [("spine_fk.001", "torso"), ("spine_fk", "spine_fk.001"), ("spine_fk.002", "torso"),
                                    ("spine_fk.003", "spine_fk.002")])
        self.assertEqual([fk.bones[k] for k in ("hips", "spine1", "spine2", "chest", "neck1")],
                         ["spine_fk", "spine_fk.001", "spine_fk.002", "spine_fk.003", "neck"])
        self.assertEqual((rig.bones["upperarm_l"], rig.bones["calf_r"]), ("upper_arm_fk.L", "shin_fk.R"))
        self.assertIn(("upper_arm_parent.L", "IK_FK", 1.0), rig.switches)
        self.assertIn(("thigh_parent.R", "IK_FK", 1.0), rig.switches)

    def test_generic_names_chains_neck_and_twists(self):
        rig = mr.detect(character_creator())
        self.assertEqual(rig.kind, "generic")
        self.assertEqual((rig.bones["hips"], rig.root), ("CC_Base_Hip", "CC_Base_Hip"))
        self.assertEqual([rig.bones[k] for k in ("spine1", "spine2", "chest", "neck1", "neck2")],
                         ["CC_Base_Waist", "CC_Base_Spine01", "CC_Base_Spine02", "CC_Base_NeckTwist01",
                          "CC_Base_NeckTwist02"])
        self.assertEqual((rig.bones["forearm_l"], rig.bones["thigh_r"], rig.bones["index1_l"]),
                         ("CC_Base_L_Forearm", "CC_Base_R_Thigh", "CC_Base_L_Index1"))
        self.assertNotIn("CC_Base_L_UpperarmTwist01", rig.bones.values())

    def test_generic_four_bone_fingers_use_the_wrist_distance(self):
        rows = [(n.replace("mixamorig:", "Bone_"), p.replace("mixamorig:", "Bone_") if p else None)
                for n, p in mixamo()]
        heads = {"Bone_LeftHand": (0.5, 0, 1.4), "Bone_LeftHandIndex1": (0.51, 0, 1.4),
                 "Bone_LeftHandIndex2": (0.6, 0, 1.4)}
        rig = mr.detect([(n, p, True, heads.get(n)) for n, p in rows])
        self.assertEqual(rig.kind, "mixamo")  # Unprefixed Mixamo names after "Bone_".
        renamed = [(n.replace("Bone_Left", "L_").replace("Bone_Right", "R_").replace("Bone_", ""),
                    p.replace("Bone_Left", "L_").replace("Bone_Right", "R_").replace("Bone_", "") if p else None)
                   for n, p in rows]
        heads = {k.replace("Bone_Left", "L_"): v for k, v in heads.items()}
        meta = mr.detect([(n, p, True, heads.get(n)) for n, p in renamed])
        self.assertEqual(meta.kind, "generic")
        self.assertEqual((meta.bones["index0_l"], meta.bones["index1_l"]), ("L_HandIndex1", "L_HandIndex2"))
        tips = mr.detect(renamed)  # No positions: the fourth bone is a tip's end bone.
        self.assertEqual(tips.bones["index1_l"], "L_HandIndex1")
        self.assertNotIn("index0_l", tips.bones)

    def test_classify_names_from_common_rigs(self):
        cases = {"lThighBend": ("thigh", "l"), "lShldrBend": ("upperarm", "l"), "rForearmBend": ("forearm", "r"),
                 "Bip01 L Thigh": ("thigh", "l"), "Bip01 R UpperArm": ("upperarm", "r"), "thigh_stretch.l": ("thigh", "l"),
                 "leg_stretch.l": ("leg", "l"), "LeftUpLeg": ("thigh", "l"), "LeftToeBase": ("toe", "l"),
                 "Pelvis": ("hips", None), "spine_03": ("spine", None), "neck_01": ("neck", None), "Head": ("head", None),
                 "upperarm_twist_01_l": None, "HeadTop_End": None, "ik_foot_l": None, "Armature": None, "root": None,
                 "L_Index_Metacarpal": ("index0", "l"), "RightHandPinky2": ("pinky", "r"), "jaw": None}
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(mr.classify(name), expected)

    def test_non_humanoids_are_refused(self):
        self.assertIsNone(mr.detect([("Bone", None), ("Bone.001", "Bone")]))
        tail = [(f"tail_{i}", f"tail_{i - 1}" if i else None) for i in range(10)]
        self.assertIsNone(mr.detect(tail))
        legs_only = [r for r in mixamo() if "Arm" not in r[0] and "Hand" not in r[0]]
        self.assertIsNone(mr.detect(legs_only))

    def test_names_and_resampling(self):
        self.assertEqual(mr.action_name("  walk   and wave "), "Motion: walk and wave")
        long = mr.action_name("a dancer spins twice, then bows deeply to the audience and walks off stage")
        self.assertLessEqual(len(long.encode()), len(mr.ACTION_PREFIX) + mr.NAME_BYTES)
        self.assertTrue(long.endswith("…"))
        self.assertEqual(mr.action_name(""), "Motion: motion")
        times = mr.sample_times(240, 30, 24)
        self.assertEqual(len(times), 192)
        self.assertEqual(times[:3], [0.0, 1.25, 2.5])
        self.assertLessEqual(times[-1], 239)
        self.assertEqual(len(mr.sample_times(240, 30, 30)), 240)
        self.assertEqual(mr.smooth([(0, 0, 0), (3, 0, 0), (6, 0, 0)], 3)[1], (3.0, 0.0, 0.0))
        steady = [(0.05 * i, 0.0, 1.0) for i in range(48)]  # A steady walk: in place it must not drift at the ends.
        self.assertLess(max(abs(a[0] - b[0]) for a, b in zip(steady, mr.smooth(steady, 24))), 1e-9)


class ApplyArgsTest(unittest.TestCase):
    def test_descriptor_takes_closed_apply_args_only(self):
        self.assertTrue(cloud._descriptor(descriptor()))
        bad = {"open": {"type": "object", "properties": {"target_armature": TARGET}},
               "name": {"type": "object", "properties": {"Target": TARGET}, "additionalProperties": False},
               "repeats input": {"type": "object", "properties": {"prompt": TARGET}, "additionalProperties": False},
               "not object": ["target_armature"],
               "required unknown": {"type": "object", "properties": {}, "required": ["x"], "additionalProperties": False}}
        for name, value in bad.items():
            with self.subTest(name=name):
                self.assertFalse(cloud._descriptor(descriptor(apply_args=value)))
        self.assertTrue(cloud._descriptor({k: v for k, v in descriptor().items() if k != "apply_args"}))

    def test_model_sees_apply_args_next_to_inputs(self):
        parameters = cloud.schema({**descriptor(), "backend": "managed"})["function"]["parameters"]
        self.assertEqual(parameters["properties"]["target_armature"], TARGET)
        self.assertEqual(parameters["required"], ["prompt"])
        cloud._validate_arguments({**descriptor(), "backend": "managed"}, {"prompt": "walk", "target_armature": "Rig"})
        video = cloud.schema({**video_descriptor(), "backend": "managed"})["function"]["parameters"]
        self.assertIn("movie clip or video strip", video["properties"]["video"]["description"])

    def test_run_strips_apply_args_and_keeps_them_for_the_handler(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        cloud_jobs._registry.clear()
        final = job()
        final.update(tool="motion_from_text")
        final["provenance"]["tool"] = "motion_from_text"
        final["outputs"][0].update(name="motion.bvh", type="motion", mime="application/x-bvh")
        transport = Transport(final=final)
        with patch.object(cloud_jobs, "_client", return_value=transport.client()), \
                patch.object(cloud_jobs.tempfile, "mkdtemp", return_value=str(root)), \
                patch.object(cloud_jobs, "_redraw"):
            result = cloud_jobs.run(descriptor(), {"prompt": "walk", "target_armature": "Hero", "in_place": True}, {},
                                    project=PROJECT)
        self.assertEqual(transport.posts[0]["input"], {"prompt": "walk"})
        self.assertTrue(result["outputs"][0]["path"].endswith(".bvh"))
        self.assertEqual(cloud_jobs._kept(result, "apply_args"), {"target_armature": "Hero", "in_place": True})
        self.assertEqual(cloud_jobs._tool_input(result), {"prompt": "walk"})


class FakeClip:
    def __init__(self, filepath, fps=30.0, frames=300, source="MOVIE"):
        self.filepath, self.fps, self.frame_duration, self.source = filepath, fps, frames, source


class FakeClips(dict):
    def __init__(self, probe):
        super().__init__()
        self.probe, self.loaded = probe, []

    def load(self, path, check_existing=False):
        clip = FakeClip(path, *self.probe)
        self.loaded.append(clip)
        return clip

    def remove(self, clip):
        self.loaded.remove(clip)


class VideoInputTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        cloud_jobs._probes.clear()
        self.video = self.root / "dance.mp4"
        self.video.write_bytes(b"\0" * 1000)

    def fake(self, probe=(30.0, 300), clips=(), strips=()):
        cloud_jobs._probes.clear()  # Each fake is another video at the same path.
        movieclips = FakeClips(probe)
        movieclips.update({clip.name: clip for clip in clips})
        editor = types.SimpleNamespace(strips_all={s.name: s for s in strips})
        return types.SimpleNamespace(data=types.SimpleNamespace(images={}, objects={}, movieclips=movieclips),
                                     context=types.SimpleNamespace(scene=types.SimpleNamespace(sequence_editor=editor),
                                                                   view_layer=types.SimpleNamespace(
                                                                       objects=types.SimpleNamespace(active=None)),
                                                                   selected_objects=[]),
                                     path=types.SimpleNamespace(abspath=lambda path: path))

    def resolve(self, fake, arguments):
        with patch.dict(sys.modules, {"bpy": fake}):
            files = cloud_jobs.resolve_files(video_descriptor(), arguments, self.root / "out")
            return files, cloud_jobs.prepare_inputs(video_descriptor(), arguments, files)

    def test_file_path_gets_its_measured_length(self):
        fake = self.fake()
        files, prepared = self.resolve(fake, {"video": str(self.video)})
        self.assertEqual(files["video"], self.video.resolve())
        self.assertEqual(prepared["video_seconds"], 10.0)
        self.assertNotIn("start_s", prepared)
        self.assertEqual(fake.data.movieclips.loaded, [])  # The probe's clip is removed.

    def test_movie_clip_and_trimmed_strip(self):
        clip = FakeClip(str(self.video))
        clip.name = "Take 3"
        strip = types.SimpleNamespace(name="dance", type="MOVIE", filepath=str(self.video), frame_duration=240,
                                      frame_offset_start=48, frame_final_duration=96)
        fake = self.fake(clips=[clip], strips=[strip])
        files, prepared = self.resolve(fake, {"video": "Take 3"})
        self.assertEqual(files["video"], self.video.resolve())
        files, prepared = self.resolve(fake, {"video": "dance"})
        self.assertEqual((prepared["start_s"], prepared["end_s"], prepared["video_seconds"]), (2.0, 6.0, 10.0))
        files, prepared = self.resolve(fake, {"video": "dance", "start_s": 1.0})
        self.assertEqual((prepared["start_s"], "end_s" in prepared), (1.0, False))  # The call's own choice wins.

    def test_long_short_wrong_kind_and_missing_are_refused_before_upload(self):
        long = self.fake(probe=(30.0, 30 * 75))
        with self.assertRaisesRegex(cloud_http.CloudError, "at most 60 s"):
            self.resolve(long, {"video": str(self.video)})
        files, prepared = self.resolve(long, {"video": str(self.video), "start_s": 10, "end_s": 70})
        self.assertEqual(prepared["video_seconds"], 75.0)
        with self.assertRaisesRegex(cloud_http.CloudError, "outside the video"):
            self.resolve(self.fake(), {"video": str(self.video), "end_s": 30})
        gif = self.root / "dance.gif"
        gif.write_bytes(b"GIF89a")
        with self.assertRaisesRegex(cloud_http.CloudError, "not an MP4"):
            self.resolve(self.fake(), {"video": str(gif)})
        sequence = FakeClip(str(self.video), source="SEQUENCE")
        sequence.name = "frames"
        with self.assertRaisesRegex(cloud_http.CloudError, "image sequence"):
            self.resolve(self.fake(clips=[sequence]), {"video": "frames"})
        with self.assertRaises((cloud_http.CloudError, Exception)):
            self.resolve(self.fake(), {"video": str(self.root / "missing.mp4")})
        real_stat = Path.stat

        def big(path, *args, **kwargs):  # Only the video looks oversized: mkdir's is_dir still needs real stats.
            if path.name == self.video.name:
                return types.SimpleNamespace(st_size=201 * 1048576, st_mtime_ns=1, st_mode=0o100644)
            return real_stat(path, *args, **kwargs)

        with patch.object(Path, "stat", big), self.assertRaisesRegex(cloud_http.CloudError, "200 MB"):
            self.resolve(self.fake(), {"video": str(self.video)})


class FakeArmature:
    def __init__(self, name, kind="ARMATURE"):
        self.name, self.type, self.modifiers, self.parent = name, kind, [], None


class ApplyMotionTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        cloud_jobs._registry.clear()
        self.bvh = (FIXTURES / "kimodo_walk_60.bvh").read_bytes()

    def motion_job(self, **remembered):
        sidecar = json.dumps({"fps": 30, "fingers_animated": False, "warnings": ["feet slide"]}).encode()
        result = job()
        result.update(tool="motion_from_text")
        result["provenance"]["tool"] = "motion_from_text"
        outputs = []
        for n, (name, mime, data) in enumerate((("motion.bvh", "application/x-bvh", self.bvh),
                                                ("motion.json", "application/json", sidecar))):
            path = self.root / f"{n}"
            path.write_bytes(data)
            outputs.append({"name": name, "type": "motion", "mime": mime, "sha256": hashlib.sha256(data).hexdigest(),
                            "size": len(data), "path": str(path), "meta": {}})
        result["outputs"] = outputs
        cloud_jobs._registry[result["id"]] = {"job": result, "ready": True, **remembered}
        return result

    def apply(self, result, objects):
        data = types.SimpleNamespace(objects=objects)
        calls = []

        def applied(text, meta, **options):
            calls.append((text, meta, options))
            return "Motion 'Motion: walk' on 'Hero'"
        rig = object()
        with patch.dict(sys.modules, {"bpy": types.SimpleNamespace(data=data)}), \
                patch.object(mr, "apply", side_effect=applied), \
                patch.object(mr, "rig_of", side_effect=lambda ob: rig if ob is not None and ob.name != "Lamp" else None):
            summary = cloud_jobs.apply_result({"apply": "apply_motion"}, result)
        return summary, calls

    def test_kept_target_prompt_and_sidecar_reach_the_retarget(self):
        hero = FakeArmature("Hero")
        result = self.motion_job(input={"prompt": "walk forward and wave"}, apply_args={"target_armature": "Hero"})
        summary, calls = self.apply(result, {"Hero": hero})
        text, meta, options = calls[0]
        self.assertEqual(text, self.bvh.decode())
        self.assertEqual((meta["fps"], meta["fingers_animated"], meta["warnings"]), (30, False, ["feet slide"]))
        self.assertIs(options["target"], hero)
        self.assertEqual(options["title"], "walk forward and wave")
        self.assertIs(options["in_place"], False)
        self.assertEqual(summary, "Applied motion: Motion 'Motion: walk' on 'Hero'. Job job_test; provenance recorded.")

    def test_in_place_apply_arg_reaches_the_retarget(self):
        hero = FakeArmature("Hero")
        _, calls = self.apply(self.motion_job(apply_args={"target_armature": "Hero", "in_place": True}), {"Hero": hero})
        self.assertIs(calls[0][2]["in_place"], True)
        _, calls = self.apply(self.motion_job(apply_args={"in_place": "yes"}), {})
        self.assertIs(calls[0][2]["in_place"], False)  # Only a real boolean true.

    def test_mesh_reference_uses_its_armature_and_a_lost_target_gets_a_skeleton(self):
        hero = FakeArmature("Hero")
        body = FakeArmature("Body", "MESH")
        body.modifiers = [types.SimpleNamespace(type="ARMATURE", object=hero)]
        _, calls = self.apply(self.motion_job(apply_args={"target_armature": "Body"}, references={"video": "dance.mp4"}),
                              {"Body": body})
        self.assertIs(calls[0][2]["target"], hero)
        self.assertEqual(calls[0][2]["title"], "dance")
        summary, calls = self.apply(self.motion_job(apply_args={"target_armature": "Gone"}), {})
        self.assertIsNone(calls[0][2]["target"])
        self.assertIn("'Gone' is no longer a humanoid armature", summary)

    def test_missing_bvh_and_bad_bvh_fail_as_apply_errors(self):
        result = self.motion_job()
        result["outputs"] = result["outputs"][1:]
        with patch.dict(sys.modules, {"bpy": types.SimpleNamespace(data=types.SimpleNamespace(objects={}))}), \
                self.assertRaisesRegex(cloud_http.CloudError, "no motion.bvh"):
            cloud_jobs.apply_result({"apply": "apply_motion"}, result)
        result = self.motion_job()
        with patch.dict(sys.modules, {"bpy": types.SimpleNamespace(data=types.SimpleNamespace(objects={}))}), \
                patch.object(mr, "apply", side_effect=mr.MotionError("motion.bvh is not a SOMA skeleton")), \
                self.assertRaises(cloud_http.CloudError) as caught:
            cloud_jobs.apply_result({"apply": "apply_motion"}, result)
        self.assertEqual(caught.exception.code, "apply_failed")

    def test_call_time_target_resolution(self):
        hero, lamp = FakeArmature("Hero"), FakeArmature("Lamp")
        body = FakeArmature("Body", "MESH")
        body.parent = hero
        objects = {"Hero": hero, "Lamp": lamp, "Body": body, "Cube": FakeArmature("Cube", "MESH")}

        def resolve(name, active=None, selected=()):
            view_layer = types.SimpleNamespace(objects=types.SimpleNamespace(active=active))
            fake = types.SimpleNamespace(data=types.SimpleNamespace(objects=objects),
                                         context=types.SimpleNamespace(view_layer=view_layer, selected_objects=list(selected)))
            rig = object()
            with patch.dict(sys.modules, {"bpy": fake}), \
                    patch.object(mr, "rig_of", side_effect=lambda ob: rig if ob is not None and ob.name != "Lamp" else None):
                return cloud_jobs.motion_target(name)
        self.assertIs(resolve("Body"), hero)
        self.assertIs(resolve(None, active=body), hero)
        self.assertIs(resolve(None, active=objects["Cube"], selected=[hero, objects["Cube"]]), hero)
        self.assertIsNone(resolve(None, active=lamp))  # Not a humanoid: a new skeleton, not an error.
        self.assertIsNone(resolve(None, selected=[hero, lamp]))  # Two armatures selected: none chosen.
        for name, message in (("Cube", "not an armature"), ("Lamp", "humanoid"), ("Nope", "not an armature")):
            with self.subTest(name=name), self.assertRaisesRegex(cloud_http.CloudError, message):
                resolve(name)


class ContractFixturesTest(unittest.TestCase):
    """The platform's motion descriptors and outputs, when this checkout can see the private contract."""

    def test_motion_descriptors_are_callable_and_strip_their_apply_args(self):
        folder = platform_contract.require("fixtures", "tool-descriptor")
        for name in ("motion_from_text", "motion_from_video"):
            path = folder / f"{name}.json"
            if not path.exists():
                self.skipTest("contract before 0.7")
            value = json.loads(path.read_text())
            with self.subTest(name=name):
                self.assertTrue(cloud._descriptor(value))
                self.assertEqual(value["apply"], "apply_motion")
                self.assertEqual(set(cloud.apply_args(value)), {"target_armature", "in_place"})
                parameters = cloud.schema({**value, "backend": "managed"})["function"]["parameters"]
                self.assertIn("target_armature", parameters["properties"])
        output = json.loads(platform_contract.require("fixtures", "output", "motion_bvh.json").read_text())
        cloud_jobs._output(output)
        self.assertEqual(cloud_jobs._SUFFIXES[output["mime"]], ".bvh")


if __name__ == "__main__":
    unittest.main()
