"""Real Blender check of apply_motion's retargeting (motion_retarget.py) on rigs built on the spot.

    <blender> -b --factory-startup --python-exit-code 1 --python loopcut/harness/motion_check.py \\
        [-- --bvh <SOMA77 .bvh> --rigs soma,mixamo,... --render <dir> --ybot <Mixamo .glb>]

Each rig gets the GEM-X dance fixture (or --bvh) and is compared, frame by frame, with SOMA's own skeleton
given the same motion: arm, leg and torso directions in the world (for Rigify, its DEF bones, which follow
the FK controls through constraints), the pelvis path scaled by leg length, and the feet reaching the ground.
Rigs: SOMA (no armature targeted), Mixamo (T-pose, rolled bones, a previous action to stash), Mixamo lying
along -Y (as glTF imports it), Unreal (UE5 names, A-pose, turned and moved, one Euler bone), Auto-Rig Pro
controls (IK to FK switch), a Character Creator-like rig (name heuristic) and a generated Rigify rig.
--render writes 8 frames per rig, seen from the front of the body, for a contact sheet (ffmpeg tile);
--ybot also renders the motion on a Mixamo character from a local GLB (never committed: Adobe's licence).
"""

import argparse
import math
import sys
import time
import traceback
from pathlib import Path

import bpy
from mathutils import Matrix, Vector

sys.path.insert(0, str(Path(__file__).resolve().parent))
import checkout  # noqa: E402
checkout.use()
from loopcut import motion_retarget as mr  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "motion"
RESULTS = []


def check(condition, message):
    RESULTS.append((bool(condition), message))
    print(("PASS " if condition else "FAIL ") + message, flush=True)
    return condition


# ------------------------------------------------------------------------------------------ rigs

def soma_points(bvh):
    return mr.Source(bvh).rest


def make_armature(name, rows, *, location=(0, 0, 0), yaw=0.0, rolls=False):
    """rows: (bone, parent, head, tail, deform)."""
    data = bpy.data.armatures.new(name)
    ob = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(ob)
    ob.location, ob.rotation_euler = location, (0, 0, yaw)
    bpy.context.view_layer.objects.active = ob
    with bpy.context.temp_override(active_object=ob, object=ob, selected_objects=[ob]):
        bpy.ops.object.mode_set(mode="EDIT")
        for n, (bone, parent, head, tail, deform) in enumerate(rows):
            eb = data.edit_bones.new(bone)
            eb.head, eb.tail, eb.use_deform = head, tail, deform
            if (eb.tail - eb.head).length < 1e-3:
                eb.tail = eb.head + Vector((0, 0, 0.03))
            if rolls:
                eb.roll = math.radians((n * 47) % 180 - 90)  # Retargeting must not depend on bone roll.
        for bone, parent, *_ in rows:
            if parent:
                data.edit_bones[bone].parent = data.edit_bones[parent]
        bpy.ops.object.mode_set(mode="OBJECT")
    return ob


def mixamo_rows(P, prefix="mixamorig:", scale=1.08, lying=False):
    turn = Matrix.Rotation(math.radians(90), 3, "X") if lying else Matrix.Identity(3)

    def at(joint):
        return turn @ (P[joint] * scale)
    rows = [("Hips", None, "Hips", "Spine1"), ("Spine", "Hips", "Spine1", "Spine2"), ("Spine1", "Spine", "Spine2", "Chest"),
            ("Spine2", "Spine1", "Chest", "Neck1"), ("Neck", "Spine2", "Neck1", "Head"), ("Head", "Neck", "Head", "HeadEnd")]
    for s in ("Left", "Right"):
        rows += [(f"{s}Shoulder", "Spine2", f"{s}Shoulder", f"{s}Arm"), (f"{s}Arm", f"{s}Shoulder", f"{s}Arm", f"{s}ForeArm"),
                 (f"{s}ForeArm", f"{s}Arm", f"{s}ForeArm", f"{s}Hand"), (f"{s}Hand", f"{s}ForeArm", f"{s}Hand", f"{s}HandMiddle2"),
                 (f"{s}UpLeg", "Hips", f"{s}Leg", f"{s}Shin"), (f"{s}Leg", f"{s}UpLeg", f"{s}Shin", f"{s}Foot"),
                 (f"{s}Foot", f"{s}Leg", f"{s}Foot", f"{s}ToeBase"), (f"{s}ToeBase", f"{s}Foot", f"{s}ToeBase", f"{s}ToeEnd")]
        rows += [(f"{s}HandThumb1", f"{s}Hand", f"{s}HandThumb1", f"{s}HandThumb2"),
                 (f"{s}HandThumb2", f"{s}HandThumb1", f"{s}HandThumb2", f"{s}HandThumb3"),
                 (f"{s}HandThumb3", f"{s}HandThumb2", f"{s}HandThumb3", f"{s}HandThumbEnd")]
        for f in ("Index", "Middle", "Ring", "Pinky"):
            rows += [(f"{s}Hand{f}1", f"{s}Hand", f"{s}Hand{f}2", f"{s}Hand{f}3"),
                     (f"{s}Hand{f}2", f"{s}Hand{f}1", f"{s}Hand{f}3", f"{s}Hand{f}4"),
                     (f"{s}Hand{f}3", f"{s}Hand{f}2", f"{s}Hand{f}4", f"{s}Hand{f}End")]
    return [(prefix + b, prefix + p if p else None, at(h), at(t), True) for b, p, h, t in rows]


def polyline(points, count):
    """count + 1 points evenly spaced along a polyline."""
    lengths = [(b - a).length for a, b in zip(points, points[1:])]
    total, out = sum(lengths), []
    for i in range(count + 1):
        d, k = total * i / count, 0
        while k < len(lengths) - 1 and d > lengths[k]:
            d -= lengths[k]
            k += 1
        out.append(points[k].lerp(points[k + 1], min(1.0, d / lengths[k]) if lengths[k] else 0))
    return out


def unreal_rows(P, scale=0.95, a_pose=45.0):
    """UE5 mannequin names in an A-pose: arms 45 degrees down from the shoulder."""
    def at(joint):
        return P[joint] * scale

    def arm(joint, side):
        pivot = at(f"{side}Arm")
        angle = math.radians(a_pose if side == "Left" else -a_pose)
        return pivot + Matrix.Rotation(angle, 3, "Y") @ (at(joint) - pivot)
    spine = polyline([at("Spine1"), at("Spine2"), at("Chest"), at("Neck1")], 5)
    rows = [("root", None, Vector((0, 0, 0)), Vector((0, 0.2, 0)), False), ("pelvis", "root", at("Hips"), at("Spine1"), True)]
    parent = "pelvis"
    for i in range(5):
        rows.append((f"spine_0{i + 1}", parent, spine[i], spine[i + 1], True))
        parent = f"spine_0{i + 1}"
    rows += [("neck_01", "spine_05", at("Neck1"), at("Neck2"), True), ("neck_02", "neck_01", at("Neck2"), at("Head"), True),
             ("head", "neck_02", at("Head"), at("HeadEnd"), True)]
    for s, side in (("l", "Left"), ("r", "Right")):
        rows += [(f"clavicle_{s}", "spine_05", at(f"{side}Shoulder"), at(f"{side}Arm"), True),
                 (f"upperarm_{s}", f"clavicle_{s}", arm(f"{side}Arm", side), arm(f"{side}ForeArm", side), True),
                 (f"lowerarm_{s}", f"upperarm_{s}", arm(f"{side}ForeArm", side), arm(f"{side}Hand", side), True),
                 (f"hand_{s}", f"lowerarm_{s}", arm(f"{side}Hand", side), arm(f"{side}HandMiddle2", side), True),
                 (f"thigh_{s}", "pelvis", at(f"{side}Leg"), at(f"{side}Shin"), True),
                 (f"calf_{s}", f"thigh_{s}", at(f"{side}Shin"), at(f"{side}Foot"), True),
                 (f"foot_{s}", f"calf_{s}", at(f"{side}Foot"), at(f"{side}ToeBase"), True),
                 (f"ball_{s}", f"foot_{s}", at(f"{side}ToeBase"), at(f"{side}ToeEnd"), True),
                 (f"ik_foot_{s}", "root", at(f"{side}Foot"), at(f"{side}Foot") + Vector((0, 0.1, 0)), False)]
        for i in (1, 2, 3):
            tail = f"{side}HandThumb{i + 1}" if i < 3 else f"{side}HandThumbEnd"
            rows.append((f"thumb_0{i}_{s}", f"thumb_0{i - 1}_{s}" if i > 1 else f"hand_{s}",
                         arm(f"{side}HandThumb{i}", side), arm(tail, side), True))
        for f in ("Index", "Middle", "Ring", "Pinky"):
            low = f.lower()
            rows.append((f"{low}_metacarpal_{s}", f"hand_{s}", arm(f"{side}Hand{f}1", side), arm(f"{side}Hand{f}2", side), True))
            for i in (1, 2, 3):
                tail = f"{side}Hand{f}{i + 2}" if i < 3 else f"{side}Hand{f}End"
                rows.append((f"{low}_0{i}_{s}", f"{low}_0{i - 1}_{s}" if i > 1 else f"{low}_metacarpal_{s}",
                             arm(f"{side}Hand{f}{i + 1}", side), arm(tail, side), True))
    return rows


def arp_rows(P, scale=1.0):
    def at(joint):
        return P[joint] * scale
    up = Vector((0, 0, 0.1))
    rows = [("c_traj", None, Vector((0, 0, 0)), Vector((0, 0.3, 0)), False),
            ("c_root_master.x", "c_traj", at("Hips"), at("Hips") + up, False),
            ("c_root.x", "c_root_master.x", at("Hips"), at("Hips") - up, True),
            ("c_spine_01.x", "c_root_master.x", at("Spine1"), at("Spine2"), True),
            ("c_spine_02.x", "c_spine_01.x", at("Spine2"), at("Chest"), True),
            ("c_spine_03.x", "c_spine_02.x", at("Chest"), at("Neck1"), True),
            ("c_neck.x", "c_spine_03.x", at("Neck1"), at("Head"), True),
            ("c_head.x", "c_neck.x", at("Head"), at("HeadEnd"), True)]
    for s, side in (("l", "Left"), ("r", "Right")):
        rows += [(f"c_shoulder.{s}", "c_spine_03.x", at(f"{side}Shoulder"), at(f"{side}Arm"), True),
                 (f"c_arm_fk.{s}", f"c_shoulder.{s}", at(f"{side}Arm"), at(f"{side}ForeArm"), True),
                 (f"c_forearm_fk.{s}", f"c_arm_fk.{s}", at(f"{side}ForeArm"), at(f"{side}Hand"), True),
                 (f"c_hand_fk.{s}", f"c_forearm_fk.{s}", at(f"{side}Hand"), at(f"{side}HandMiddle2"), True),
                 (f"c_thigh_fk.{s}", "c_root.x", at(f"{side}Leg"), at(f"{side}Shin"), True),
                 (f"c_leg_fk.{s}", f"c_thigh_fk.{s}", at(f"{side}Shin"), at(f"{side}Foot"), True),
                 (f"c_foot_fk.{s}", f"c_leg_fk.{s}", at(f"{side}Foot"), at(f"{side}ToeBase"), True),
                 (f"c_toes_fk.{s}", f"c_foot_fk.{s}", at(f"{side}ToeBase"), at(f"{side}ToeEnd"), True),
                 (f"c_hand_ik.{s}", "c_traj", at(f"{side}Hand"), at(f"{side}HandMiddle2"), False),
                 (f"c_foot_ik.{s}", "c_traj", at(f"{side}Foot"), at(f"{side}ToeBase"), False)]
        for f in ("index", "middle", "ring", "pinky"):
            F = f.capitalize()
            rows.append((f"c_{f}1_base.{s}", f"c_hand_fk.{s}", at(f"{side}Hand{F}1"), at(f"{side}Hand{F}2"), True))
            for i in (1, 2, 3):
                tail = f"{side}Hand{F}{i + 2}" if i < 3 else f"{side}Hand{F}End"
                rows.append((f"c_{f}{i}.{s}", f"c_{f}{i - 1}.{s}" if i > 1 else f"c_{f}1_base.{s}",
                             at(f"{side}Hand{F}{i + 1}"), at(tail), True))
    return rows


def cc_rows(P, scale=1.03):
    def at(joint):
        return P[joint] * scale
    up = Vector((0, 0, 0.05))
    rows = [("CC_Base_BoneRoot", None, Vector((0, 0, 0)), Vector((0, 0, 0.1)), False),
            ("CC_Base_Hip", "CC_Base_BoneRoot", at("Hips"), at("Hips") + up, True),
            ("CC_Base_Pelvis", "CC_Base_Hip", at("Hips"), at("Hips") - up, True),
            ("CC_Base_Waist", "CC_Base_Hip", at("Spine1"), at("Spine2"), True),
            ("CC_Base_Spine01", "CC_Base_Waist", at("Spine2"), at("Chest"), True),
            ("CC_Base_Spine02", "CC_Base_Spine01", at("Chest"), at("Neck1"), True),
            ("CC_Base_NeckTwist01", "CC_Base_Spine02", at("Neck1"), at("Neck2"), True),
            ("CC_Base_NeckTwist02", "CC_Base_NeckTwist01", at("Neck2"), at("Head"), True),
            ("CC_Base_Head", "CC_Base_NeckTwist02", at("Head"), at("HeadEnd"), True)]
    for s, side in (("L", "Left"), ("R", "Right")):
        rows += [(f"CC_Base_{s}_Clavicle", "CC_Base_Spine02", at(f"{side}Shoulder"), at(f"{side}Arm"), True),
                 (f"CC_Base_{s}_Upperarm", f"CC_Base_{s}_Clavicle", at(f"{side}Arm"), at(f"{side}ForeArm"), True),
                 (f"CC_Base_{s}_Forearm", f"CC_Base_{s}_Upperarm", at(f"{side}ForeArm"), at(f"{side}Hand"), True),
                 (f"CC_Base_{s}_Hand", f"CC_Base_{s}_Forearm", at(f"{side}Hand"), at(f"{side}HandMiddle2"), True),
                 (f"CC_Base_{s}_Thigh", "CC_Base_Pelvis", at(f"{side}Leg"), at(f"{side}Shin"), True),
                 (f"CC_Base_{s}_Calf", f"CC_Base_{s}_Thigh", at(f"{side}Shin"), at(f"{side}Foot"), True),
                 (f"CC_Base_{s}_Foot", f"CC_Base_{s}_Calf", at(f"{side}Foot"), at(f"{side}ToeBase"), True),
                 (f"CC_Base_{s}_ToeBase", f"CC_Base_{s}_Foot", at(f"{side}ToeBase"), at(f"{side}ToeEnd"), True)]
        for f in ("Index", "Mid", "Ring", "Pinky"):
            soma = "Middle" if f == "Mid" else f
            for i in (1, 2, 3):
                tail = f"{side}Hand{soma}{i + 2}" if i < 3 else f"{side}Hand{soma}End"
                rows.append((f"CC_Base_{s}_{f}{i}", f"CC_Base_{s}_{f}{i - 1}" if i > 1 else f"CC_Base_{s}_Hand",
                             at(f"{side}Hand{soma}{i + 1}"), at(tail), True))
    return rows


def rigify_rig():
    import addon_utils
    addon_utils.enable("rigify", default_set=True)
    before = set(bpy.data.objects)
    bpy.ops.object.armature_human_metarig_add()
    meta = bpy.context.object
    bpy.ops.pose.rigify_generate()
    rig = next(o for o in bpy.data.objects if o.type == "ARMATURE" and o is not meta and o not in before)
    meta.hide_render = meta.hide_viewport = True
    return rig


def capsule_body(arm_ob, name, prefix=""):
    """Ellipsoids along the deform bones (whose names start with prefix), weighted to them: what renders."""
    import bmesh
    mesh = bpy.data.meshes.new(name)
    bm = bmesh.new()
    layer = bm.verts.layers.deform.verify()
    groups = []
    for bone in arm_ob.data.bones:
        if not bone.use_deform or not bone.name.startswith(prefix) or bone.length < 0.015:
            continue
        axis = bone.tail_local - bone.head_local
        radius = max(0.008, min(0.11, bone.length * 0.22))
        half = axis.length / 2 + radius * 0.3
        rot = Vector((0, 0, 1)).rotation_difference(axis.normalized()).to_matrix().to_4x4()
        matrix = Matrix.Translation((bone.head_local + bone.tail_local) / 2) @ rot @ Matrix.Diagonal((radius, radius, half, 1))
        made = bmesh.ops.create_uvsphere(bm, u_segments=10, v_segments=6, radius=1.0, matrix=matrix)
        groups.append(bone.name)
        for vert in made["verts"]:
            vert[layer][len(groups) - 1] = 1.0
    bm.to_mesh(mesh)
    bm.free()
    body = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(body)
    for group in groups:
        body.vertex_groups.new(name=group)
    body.parent = arm_ob
    body.modifiers.new("Armature", "ARMATURE").object = arm_ob
    return body


def mannequin(arm_ob, names, name):
    """A capsule mannequin (SOMA's generated body's proportions) on canonical joints, weighted to names' bones."""
    import bmesh
    bones = arm_ob.data.bones
    at = {c: bones[n].head_local for c, n in names.items() if n in bones}
    if not all(c in at for c in ("hips", "head", "thigh_l", "thigh_r")):
        return capsule_body(arm_ob, name)
    up = (at["head"] - at["hips"]).normalized()
    s = (at["head"] - at["hips"]).length / 0.62
    parts = [("hips", at["thigh_l"], at["thigh_r"], 0.10)]
    torso = [c for c in ("hips", "spine1", "spine2", "chest", "neck1", "head") if c in at]
    radius = {"hips": 0.11, "spine1": 0.11, "spine2": 0.12, "chest": 0.13, "neck1": 0.05}
    parts += [(a, at[a], at[b], radius[a]) for a, b in zip(torso, torso[1:])]
    parts.append(("head", at["head"], at["head"] + up * 0.2 * s, 0.10))
    for side in "lr":
        chain = [(f"clavicle_{side}", f"upperarm_{side}", 0.05), (f"upperarm_{side}", f"forearm_{side}", 0.055),
                 (f"forearm_{side}", f"hand_{side}", 0.045), (f"thigh_{side}", f"calf_{side}", 0.075),
                 (f"calf_{side}", f"foot_{side}", 0.055), (f"foot_{side}", f"toe_{side}", 0.045)]
        parts += [(a, at[a], at[b], r) for a, b, r in chain if a in at and b in at]
        hand, fore = at.get(f"hand_{side}"), at.get(f"forearm_{side}")
        if hand is not None and fore is not None:
            parts.append((f"hand_{side}", hand, hand + (hand - fore).normalized() * 0.08 * s, 0.04))
        toe, foot = at.get(f"toe_{side}"), at.get(f"foot_{side}")
        if toe is not None and foot is not None:
            parts.append((f"toe_{side}", toe, toe + (toe - foot).normalized() * 0.06 * s, 0.03))
    mesh = bpy.data.meshes.new(name)
    bm = bmesh.new()
    layer = bm.verts.layers.deform.verify()
    groups = []
    for canonical, a, b, r in parts:
        axis = b - a
        if axis.length < 1e-4:
            continue
        r *= s
        rot = Vector((0, 0, 1)).rotation_difference(axis.normalized()).to_matrix().to_4x4()
        matrix = Matrix.Translation((a + b) / 2) @ rot @ Matrix.Diagonal((r, r, axis.length / 2 + r * 0.35, 1))
        made = bmesh.ops.create_uvsphere(bm, u_segments=12, v_segments=8, radius=1.0, matrix=matrix)
        bone = names[canonical]
        if bone not in groups:
            groups.append(bone)
        for vert in made["verts"]:
            vert[layer][groups.index(bone)] = 1.0
    bm.to_mesh(mesh)
    bm.free()
    for poly in mesh.polygons:
        poly.use_smooth = True
    body = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(body)
    for group in groups:
        body.vertex_groups.new(name=group)
    body.parent = arm_ob
    body.modifiers.new("Armature", "ARMATURE").object = arm_ob
    return body


# ---------------------------------------------------------------------------------------- checks

SEGMENTS = {"upperarm_l": "forearm_l", "forearm_l": "hand_l", "upperarm_r": "forearm_r", "forearm_r": "hand_r",
            "thigh_l": "calf_l", "calf_l": "foot_l", "thigh_r": "calf_r", "calf_r": "foot_r", "hips": "head"}
RIGIFY_DEF = {"hips": "DEF-spine", "head": "DEF-spine.006"}
for _s, _S in (("l", "L"), ("r", "R")):
    RIGIFY_DEF.update({f"upperarm_{_s}": f"DEF-upper_arm.{_S}", f"forearm_{_s}": f"DEF-forearm.{_S}",
                       f"hand_{_s}": f"DEF-hand.{_S}", f"thigh_{_s}": f"DEF-thigh.{_S}", f"calf_{_s}": f"DEF-shin.{_S}",
                       f"foot_{_s}": f"DEF-foot.{_S}"})


def head(ob, bone):
    return ob.matrix_world @ ob.pose.bones[bone].head


def compare(name, ob, names, ref, frames, yaw, k, ground_t, *, travel=True, limit=(6.0, 15.0)):
    """Directions against SOMA's own skeleton (ref), turned by the target's yaw, over frames."""
    scene = bpy.context.scene
    turn = Matrix.Rotation(-yaw, 3, "Z")
    worst, total, count, toes = 0.0, 0.0, 0, []
    path_t, path_r = [], []
    soma = {c: mr.SOMA[c] for c in SEGMENTS.values()} | {c: mr.SOMA[c] for c in SEGMENTS}
    for f in frames:
        scene.frame_set(f)
        for a, b in SEGMENTS.items():
            dt = turn @ (head(ob, names[b]) - head(ob, names[a]))
            dr = head(ref, soma[b]) - head(ref, soma[a])
            angle = math.degrees(dt.angle(dr, 0.0))
            worst, total, count = max(worst, angle), total + angle, count + 1
        toes.append(min(head(ob, names[f"foot_{s}"]).z for s in "lr"))
        path_t.append(turn @ ((head(ob, names["thigh_l"]) + head(ob, names["thigh_r"])) / 2))
        path_r.append((head(ref, "LeftLeg") + head(ref, "RightLeg")) / 2)
    mean = total / count
    check(mean <= limit[0] and worst <= limit[1], f"{name}: limb and torso directions follow SOMA "
          f"(mean {mean:.1f}°, worst {worst:.1f}° over {len(frames)} frames)")
    if travel:
        moved_t, moved_r = path_t[-1] - path_t[0], path_r[-1] - path_r[0]
        error = (moved_t - moved_r * k).length
        check(error <= 0.05 + 0.08 * moved_r.length * k, f"{name}: pelvis travel {moved_t.length:.2f} m is SOMA's "
              f"{moved_r.length:.2f} m x {k:.2f} (error {error:.3f} m)")
    lowest = min(toes)
    return lowest


def run_rig(kind, bvh_text, meta, bvh, args):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.fps = args.fps
    P = soma_points(bvh)
    ref = None
    yaw, k, location = 0.0, 1.0, Vector((0, 0, 0))
    title = "kathak dance"
    if kind == "soma":
        line = mr.apply(bvh_text, meta, target=None, title=title)
        ob = bpy.data.objects["Motion skeleton"]
        check("simple body" in line and bpy.data.objects.get("Motion body") is not None,
              f"soma: no armature targeted makes a skeleton with a generated body ({line[:90]}…)")
        check(not any("Y Bot" in o.name or "mixamo" in o.name.lower() for o in bpy.data.objects),
              "soma: no licensed character is imported")
        names = {c: mr.SOMA[c] for c in mr.CANONICAL}
        return ob, names, line, 0.0
    scene.cursor.location = (-1.5, 0, 0)
    mr.apply(bvh_text, meta, target=None, title="reference")
    ref = bpy.data.objects["Motion skeleton"]
    ref.hide_render = True
    bpy.data.objects["Motion body"].hide_render = True
    scene.cursor.location = (0, 0, 0)
    if kind == "mixamo":
        ob = make_armature("Hero", mixamo_rows(P), rolls=True)
        old = bpy.data.actions.new("Idle")
        ob.animation_data_create().action = old
        ob.pose.bones["mixamorig:Hips"].keyframe_insert("location", frame=1)
    elif kind == "mixamo_lying":
        ob = make_armature("Y Bot rig", mixamo_rows(P, lying=True), rolls=True)
    elif kind == "unreal":
        yaw, location = math.radians(90), Vector((1.0, 2.0, 0.0))
        ob = make_armature("SK_Mannequin", unreal_rows(P), location=location, yaw=yaw)
        ob.pose.bones["lowerarm_l"].rotation_mode = "XYZ"
    elif kind == "auto_rig_pro":
        ob = make_armature("rig", arp_rows(P))
        for s in "lr":
            ob.pose.bones[f"c_hand_ik.{s}"]["ik_fk_switch"] = 0.0
            ob.pose.bones[f"c_foot_ik.{s}"]["ik_fk_switch"] = 0.0
    elif kind == "generic":
        ob = make_armature("CC_Base_Body_Rig", cc_rows(P))
    elif kind == "rigify":
        ob = rigify_rig()
    else:
        raise ValueError(kind)
    bpy.context.view_layer.update()
    rig = mr.rig_of(ob)
    check(rig is not None and rig.kind == kind.replace("_lying", ""), f"{kind}: detected as {rig.kind if rig else None}")
    weights = dict(rig.bones)
    if kind == "rigify":
        weights.update(RIGIFY_DEF)
        weights.update({"spine1": "DEF-spine.001", "spine2": "DEF-spine.002", "chest": "DEF-spine.003",
                        "neck1": "DEF-spine.004", "clavicle_l": "DEF-shoulder.L", "clavicle_r": "DEF-shoulder.R",
                        "toe_l": "DEF-toe.L", "toe_r": "DEF-toe.R"})
    mannequin(ob, weights, f"{ob.name} body")
    feet = {s: rig.bones.get(f"foot_{s}") for s in "lr"}
    ground_t = min(head(ob, feet[s]).z for s in "lr")
    began = time.perf_counter()
    line = mr.apply(bvh_text, meta, target=ob, title=title)
    print("   ", line, f"[{time.perf_counter() - began:.1f} s]", flush=True)
    action = ob.animation_data.action
    check(action is not None and action.name == "Motion: kathak dance", f"{kind}: action named from the request")
    names = dict(rig.bones)
    if kind == "rigify":
        names.update(RIGIFY_DEF)
    legs = lambda points: sum((points[f"calf_{s}"] - points[f"thigh_{s}"]).length
                              + (points[f"foot_{s}"] - points[f"calf_{s}"]).length for s in "lr")
    rest_t = {c: ob.matrix_world @ ob.data.bones[rig.bones[c]].head_local for c in rig.bones}
    rest_s = {c: P[mr.SOMA[c]] for c in mr.SOMA}
    k = legs(rest_t) / legs(rest_s)
    frames = list(range(scene.frame_start, scene.frame_end + 1, max(1, (scene.frame_end - scene.frame_start) // 40)))
    lowest = compare(kind, ob, names, ref, frames, yaw, k, ground_t)
    if kind == "mixamo":
        tracks = ob.animation_data.nla_tracks
        check(len(tracks) == 1 and tracks[0].name == "[Action Stash]" and tracks[0].mute
              and tracks[0].strips[0].action.name == "Idle", "mixamo: the previous action is stashed in a muted NLA track")
    if kind == "auto_rig_pro":
        curves = [c.data_path for c in _curves(action)]
        check(all(ob.pose.bones[f"c_{p}_ik.{s}"]["ik_fk_switch"] == 1.0 for p in ("hand", "foot") for s in "lr")
              and 'pose.bones["c_hand_ik.l"]["ik_fk_switch"]' in curves, "auto_rig_pro: limbs switched to FK and keyed")
        check(any(c.endswith('"c_root_master.x"].location') for c in curves), "auto_rig_pro: travel keyed on c_root_master.x")
    if kind == "rigify":
        curves = [c.data_path for c in _curves(action)]
        check(all(ob.pose.bones[f"{p}_parent.{s}"]["IK_FK"] == 1.0 for p in ("upper_arm", "thigh") for s in "LR")
              and 'pose.bones["upper_arm_parent.L"]["IK_FK"]' in curves, "rigify: limbs switched to FK and keyed")
    if kind == "unreal":
        curves = [c.data_path for c in _curves(action)]
        check('pose.bones["lowerarm_l"].rotation_euler' in curves, "unreal: an Euler bone is keyed in Euler")
    check(abs(lowest - ground_t) <= 0.04 + 0.04 * k, f"{kind}: the feet reach the ground ({lowest - ground_t:+.3f} m)")
    return ob, names, line, yaw


def _curves(action):
    if hasattr(action, "layers"):
        return [c for layer in action.layers for strip in layer.strips for bag in strip.channelbags for c in bag.fcurves]
    return list(action.fcurves)


def walk_checks(args):
    """Root motion in place, resampling to the scene rate, and a fresh scene's frame range."""
    text = (FIXTURES / "kimodo_walk_60.bvh").read_text()
    meta = {"fps": 30, "fingers_animated": False}
    bvh = mr.parse_bvh(text)
    P = soma_points(bvh)
    for in_place in (False, True):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        scene = bpy.context.scene
        scene.render.fps = 24
        ob = make_armature("Hero", mixamo_rows(P))
        line = mr.apply(text, meta, target=ob, title="walk and wave", in_place=in_place)
        xs = []
        for f in range(scene.frame_start, scene.frame_end + 1):
            scene.frame_set(f)
            pelvis = (head(ob, "mixamorig:LeftUpLeg") + head(ob, "mixamorig:RightUpLeg")) / 2
            xs.append(pelvis.to_2d())
        span = max((a - xs[0]).length for a in xs)
        if in_place:
            check(span < 0.2, f"walk in place: the pelvis stays within {span:.2f} m")
        else:
            check(span > 0.6, f"walk in world: the pelvis travels {span:.2f} m")
            check((scene.frame_start, scene.frame_end) == (1, 48) and "resampled from 30" in line,
                  f"walk at 24 fps: 60 frames at 30 fps become frames 1-48 ({scene.frame_start}-{scene.frame_end})")
            check("fingers not animated" in line, "walk: fingers left alone when the model does not animate them")
            fingers = [c for c in _curves(ob.animation_data.action) if "HandIndex" in c.data_path]
            check(not fingers, "walk: no finger curves for a body-only motion")


def render(ob, kind, out_dir, frames=8, size=(360, 480)):
    """frames evenly spread, seen from the front of the body (its mean facing turned 30 degrees)."""
    scene = bpy.context.scene
    rig = mr.rig_of(ob) if ob.type == "ARMATURE" else None
    names = rig.bones if rig else {}
    picks = [round(scene.frame_start + i * (scene.frame_end - scene.frame_start) / (frames - 1)) for i in range(frames)]
    hips, facing, toes = [], Vector(), []
    for f in range(scene.frame_start, scene.frame_end + 1):
        scene.frame_set(f)
        l, r = head(ob, names["thigh_l"]), head(ob, names["thigh_r"])
        hips.append((l + r) / 2)
        left = Vector(((l - r).x, (l - r).y, 0))
        if left.length > 1e-6:
            facing += left.normalized().cross(Vector((0, 0, 1)))
        toes.append(min(head(ob, names[f"foot_{s}"]).z for s in "lr"))
    facing = facing.normalized() if facing.length > 1e-6 else Vector((0, -1, 0))
    centre = sum(hips, Vector()) / len(hips)
    bpy.ops.mesh.primitive_plane_add(size=40, location=(centre.x, centre.y, min(toes) - 0.06))
    cam = bpy.data.objects.new("cam", bpy.data.cameras.new("cam"))
    scene.collection.objects.link(cam)
    target = bpy.data.objects.new("aim", None)
    scene.collection.objects.link(target)
    cam.constraints.new("TRACK_TO").target = target
    scene.camera = cam
    height = max(0.5, centre.z - min(toes))
    offset = (Matrix.Rotation(math.radians(30), 3, "Z") @ facing) * (height * 4.6) + Vector((0, 0, height * 0.4))
    for i, f in enumerate(range(scene.frame_start, scene.frame_end + 1)):
        window = hips[max(0, i - 15):i + 16]
        target.location = sum(window, Vector()) / len(window)
        cam.location = target.location + offset
        target.keyframe_insert("location", frame=f)
        cam.keyframe_insert("location", frame=f)
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.display.shading.light = "STUDIO"
    scene.display.shading.color_type = "SINGLE"
    scene.display.shading.show_shadows = True
    scene.render.resolution_x, scene.render.resolution_y = size
    scene.render.image_settings.file_format = "PNG"
    folder = Path(out_dir) / kind
    folder.mkdir(parents=True, exist_ok=True)
    for n, f in enumerate(picks):
        scene.frame_set(f)
        scene.render.filepath = str(folder / f"f_{n:02d}.png")
        bpy.ops.render.render(write_still=True)
    print("RENDERED", kind, folder, flush=True)


def ybot(path, bvh_text, meta, out_dir):
    """The motion on a local Mixamo Y Bot GLB (rest lying along -Y), for the owner's eyes only."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.context.scene.render.fps = 30
    bpy.ops.import_scene.gltf(filepath=path)
    for o in list(bpy.context.scene.objects):
        if o.type == "MESH" and o.name.startswith("Icosphere"):
            bpy.data.objects.remove(o)
    ob = next(o for o in bpy.context.scene.objects if o.type == "ARMATURE")
    line = mr.apply(bvh_text, meta, target=ob, title="kathak dance")
    print("   ", line, flush=True)
    check("Mixamo" in line, "ybot: the Y Bot is detected as Mixamo")
    if out_dir:
        render(ob, "mixamo_ybot", out_dir)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bvh", default=str(FIXTURES / "gemx_dance_60.bvh"))
    parser.add_argument("--rigs", default="soma,mixamo,mixamo_lying,unreal,auto_rig_pro,generic,rigify")
    parser.add_argument("--render", default="")
    parser.add_argument("--ybot", default="")
    parser.add_argument("--fps", type=int, default=24)
    parser.add_argument("--no-walk", action="store_true")
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else [])
    text = Path(args.bvh).read_text()
    bvh = mr.parse_bvh(text)
    meta = {"fps": round(bvh.fps, 3), "fingers_animated": True, "units": "cm"}
    for kind in args.rigs.split(","):
        try:
            ob, names, line, yaw = run_rig(kind, text, meta, bvh, args)
            if args.render:
                render(ob, kind, args.render)
        except Exception:
            traceback.print_exc()
            check(False, f"{kind}: ran without an error")
    if not args.no_walk:
        try:
            walk_checks(args)
        except Exception:
            traceback.print_exc()
            check(False, "walk checks ran without an error")
    if args.ybot:
        try:
            ybot(args.ybot, text, meta, args.render)
        except Exception:
            traceback.print_exc()
            check(False, "ybot ran without an error")
    failed = [m for ok, m in RESULTS if not ok]
    print(f"{'FAIL' if failed else 'PASS'} motion check: {len(RESULTS) - len(failed)}/{len(RESULTS)}", flush=True)
    if failed:
        sys.exit(1)


main()
