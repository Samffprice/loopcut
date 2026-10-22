"""Motion results (motion_from_video, motion_from_text): a rig-neutral SOMA77 BVH put on the user's armature.

The service returns motion.bvh (Kimodo's SOMA77 skeleton: standing T-pose rest, cm, Y-up, root "Root", 30 fps)
and motion.json. Retargeting runs here, free and private, on Blender's main thread, onto any humanoid:

    target_world(t) = Y @ D(t) @ A @ F @ target_rest_world        for each mapped bone
    D(t)            = the BVH joint's global rotation (its rest rotation is the identity)

F is one whole-body rotation that maps the target's rest body frame (hips->head up, right->left upper arm)
onto the source's; some rigs store their rest lying down (Mixamo through glTF). A aligns rest directions on
arm, leg and finger chains only, so A-pose rigs take a T-pose motion; it is never used on hips, spine,
neck, head or clavicles, where it tilts the pelvis or shrugs the shoulders. Y turns the motion about world Z
so the first frame faces where the character faces now. The pelvis (midpoint of the thighs) follows the
source pelvis, scaled by the ratio of leg lengths, from where the character stands. Local rotations are
written parent first: basis = chain^-1 @ desired. After the bake, Blender's evaluation is checked: when no limb's
deforming bones follow the motion (mapped controls that drive nothing), the bake is undone and refused.
When the motion does not animate fingers (fingers_animated false), the mapped finger bones get a relaxed hand,
each chain curled toward the palm about a hinge read from its own rest geometry, instead of the rig's rest
(flat on T-pose rigs); finger bones that something already keys are left as they are.

The parsing, bone maps, rig detection and the relaxed hand need no Blender (unit tested without it); retarget()
and apply() run in Blender.
"""

import math
import re

MAX_BVH_BYTES = 64 * 1024 * 1024
MAX_JOINTS = 512
MAX_FRAMES = 36_000  # 20 minutes at 30 fps; the service caps a video at 60 s.
ACTION_PREFIX = "Motion: "
NAME_BYTES = 48  # Of the prompt or clip in an action's name; Blender keeps 63 bytes.


class MotionError(ValueError):
    pass


# ------------------------------------------------------------------------------------------- BVH

class Joint:
    __slots__ = ("name", "parent", "offset", "channels", "column", "children", "end")

    def __init__(self, name, parent):
        self.name, self.parent = name, parent
        self.offset, self.channels, self.column, self.children, self.end = (0.0, 0.0, 0.0), [], 0, [], None


class Bvh:
    def __init__(self, joints, frames, frame_time):
        self.joints, self.frames, self.frame_time = joints, frames, frame_time
        self.by_name = {j.name: j for j in joints}

    @property
    def fps(self):
        return 1.0 / self.frame_time


_CHANNELS = {"Xposition", "Yposition", "Zposition", "Xrotation", "Yrotation", "Zrotation"}


def parse_bvh(text):
    """Joints in file order (parents first), frames as lists of floats. Joint names may hold spaces."""
    if not isinstance(text, str) or len(text) > MAX_BVH_BYTES:
        raise MotionError("motion.bvh is missing or too large")
    lines = text.splitlines()
    # stack: the open blocks, each a Joint or ("end", joint) for an End Site.
    joints, stack, current, n = [], [], None, 0
    while n < len(lines):
        line = lines[n].strip()
        n += 1
        if not line or line == "HIERARCHY":
            continue
        word, _, rest = line.partition(" ")
        owner = stack[-1] if stack else None
        if word in ("ROOT", "JOINT"):
            if (word == "ROOT") != (not stack) or not rest.strip() or not isinstance(owner, (Joint, type(None))):
                raise MotionError(f"motion.bvh line {n}: misplaced {word}")
            current = Joint(rest.strip(), owner)
            if owner is not None:
                owner.children.append(current)
            joints.append(current)
            if len(joints) > MAX_JOINTS:
                raise MotionError("motion.bvh has too many joints")
        elif word == "End":
            if not isinstance(owner, Joint):
                raise MotionError(f"motion.bvh line {n}: End Site outside a joint")
            current = ("end", owner)
        elif word == "{":
            if current is None:
                raise MotionError(f"motion.bvh line {n}: unexpected {{")
            stack.append(current)
            current = None
        elif word == "}":
            if not stack:
                raise MotionError(f"motion.bvh line {n}: unbalanced }}")
            stack.pop()
        elif word == "OFFSET":
            values = tuple(_floats(rest, 3, n))
            if isinstance(owner, tuple):
                owner[1].end = values
            elif isinstance(owner, Joint):
                owner.offset = values
            else:
                raise MotionError(f"motion.bvh line {n}: OFFSET outside a joint")
        elif word == "CHANNELS":
            parts = rest.split()
            if not isinstance(owner, Joint) or not parts or not parts[0].isdigit():
                raise MotionError(f"motion.bvh line {n}: bad CHANNELS")
            names = parts[1:]
            if len(names) != int(parts[0]) or any(c not in _CHANNELS for c in names):
                raise MotionError(f"motion.bvh line {n}: bad CHANNELS")
            owner.channels = names
        elif word == "MOTION":
            break
        else:
            raise MotionError(f"motion.bvh line {n}: unexpected {word[:20]!r}")
    if stack or not joints:
        raise MotionError("motion.bvh has no complete hierarchy")
    column = 0
    for joint in joints:
        joint.column = column
        column += len(joint.channels)
    count, frame_time = None, None
    while n < len(lines) and (count is None or frame_time is None):
        line = lines[n].strip()
        n += 1
        if line.startswith("Frames:"):
            count = _int(line[7:], n)
        elif line.startswith("Frame Time:"):
            frame_time = _floats(line[11:], 1, n)[0]
        elif line:
            raise MotionError(f"motion.bvh line {n}: expected Frames and Frame Time")
    if count is None or frame_time is None or not 0 < count <= MAX_FRAMES or not 1e-4 < frame_time < 10:
        raise MotionError("motion.bvh has no usable frame count or frame time")
    values = " ".join(lines[n:]).split()
    if len(values) != count * column:
        raise MotionError(f"motion.bvh has {len(values)} values for {count} frames of {column} channels")
    try:
        numbers = [float(v) for v in values]
    except ValueError as ex:
        raise MotionError("motion.bvh has a value that is not a number") from ex
    if not all(math.isfinite(v) for v in numbers):
        raise MotionError("motion.bvh has a value that is not finite")
    frames = [numbers[i * column:(i + 1) * column] for i in range(count)]
    return Bvh(joints, frames, frame_time)


def _floats(text, count, line):
    try:
        values = [float(v) for v in text.split()]
    except ValueError:
        values = []
    if len(values) != count or not all(math.isfinite(v) for v in values):
        raise MotionError(f"motion.bvh line {line}: expected {count} numbers")
    return values


def _int(text, line):
    try:
        return int(text.strip())
    except ValueError as ex:
        raise MotionError(f"motion.bvh line {line}: expected a frame count") from ex


# ------------------------------------------------------------------------------- canonical joints

FINGERS = ("index", "middle", "ring", "pinky")
CENTER = ("hips", "spine1", "spine2", "chest", "neck1", "neck2", "head")
SIDED = (("clavicle", "upperarm", "forearm", "hand", "thigh", "calf", "foot", "toe", "thumb1", "thumb2", "thumb3")
         + tuple(f"{f}{i}" for f in FINGERS for i in (0, 1, 2, 3)))
CANONICAL = CENTER + tuple(f"{part}_{s}" for s in "lr" for part in SIDED)
REQUIRED = ("hips", "head") + tuple(f"{p}_{s}" for s in "lr" for p in ("upperarm", "forearm", "hand", "thigh", "calf", "foot"))
FINGER_PARTS = {"thumb1", "thumb2", "thumb3"} | {f"{f}{i}" for f in FINGERS for i in (0, 1, 2, 3)}

# Rest-direction alignment (A): the part's direction runs to this child. Only these chains are aligned.
DIRECTION = {"upperarm": "forearm", "forearm": "hand", "hand": "middle1", "thigh": "calf", "calf": "foot",
             "foot": "toe", "thumb1": "thumb2", "thumb2": "thumb3"}
for _f in FINGERS:
    DIRECTION.update({f"{_f}1": f"{_f}2", f"{_f}2": f"{_f}3"})
# A part without its direction child takes its chain parent's alignment, so it keeps its rest angle there.
ALIGN_PARENT = {"forearm": "upperarm", "hand": "forearm", "calf": "thigh", "foot": "calf", "toe": "foot",
                "thumb1": "hand", "thumb2": "thumb1", "thumb3": "thumb2"}
for _f in FINGERS:
    ALIGN_PARENT.update({f"{_f}0": "hand", f"{_f}1": "hand", f"{_f}2": f"{_f}1", f"{_f}3": f"{_f}2"})
ALIGNED = set(ALIGN_PARENT) | {"upperarm", "thigh"}


def part_of(canonical):
    return canonical[:-2] if canonical[-2:] in ("_l", "_r") else canonical


def _soma():
    m = {"hips": "Hips", "spine1": "Spine1", "spine2": "Spine2", "chest": "Chest", "neck1": "Neck1",
         "neck2": "Neck2", "head": "Head"}
    for s, side in (("l", "Left"), ("r", "Right")):
        # SOMA's Leg is the thigh and Shin the calf.
        m.update({f"clavicle_{s}": f"{side}Shoulder", f"upperarm_{s}": f"{side}Arm", f"forearm_{s}": f"{side}ForeArm",
                  f"hand_{s}": f"{side}Hand", f"thigh_{s}": f"{side}Leg", f"calf_{s}": f"{side}Shin",
                  f"foot_{s}": f"{side}Foot", f"toe_{s}": f"{side}ToeBase"})
        for i in (1, 2, 3):
            m[f"thumb{i}_{s}"] = f"{side}HandThumb{i}"
        for f in FINGERS:
            for i in (0, 1, 2, 3):  # SOMA's finger 1 is the metacarpal: canonical 0.
                m[f"{f}{i}_{s}"] = f"{side}Hand{f.capitalize()}{i + 1}"
    return m


SOMA = _soma()
SOMA_EXTRA = ("Jaw", "LeftEye", "RightEye")  # Animated on SOMA's own skeleton only.

RIG_LABELS = {"soma": "SOMA skeleton", "mixamo": "Mixamo", "rigify": "Rigify", "rigify_metarig": "Rigify metarig",
              "auto_rig_pro": "Auto-Rig Pro", "unreal": "Unreal mannequin", "generic": "humanoid matched by bone names"}


class Rig:
    """bones: canonical -> target bone. extra: target bone -> SOMA joint, outside the canonical set.
    root: the bone that carries the travel (an ancestor of the hips and both thighs, or the hips).
    switches: (bone, property, value) set and keyed so the mapped controls drive the deform bones."""

    def __init__(self, kind, bones, root=None, extra=None, switches=()):
        self.kind, self.bones, self.root = kind, dict(bones), root
        self.extra, self.switches = dict(extra or {}), list(switches)

    @property
    def label(self):
        return RIG_LABELS.get(self.kind, self.kind)


# ----------------------------------------------------------------------------------- rig detection

def _hierarchy(bones):
    """bones: iterable of (name, parent or None[, deform[, head xyz]]) -> parents, deform flags, heads."""
    parents, deform, heads = {}, {}, {}
    for row in bones:
        parents[row[0]] = row[1]
        deform[row[0]] = row[2] if len(row) > 2 else True
        if len(row) > 3 and row[3] is not None:
            heads[row[0]] = tuple(row[3])
    return parents, deform, heads


def ancestors(parents, name):
    out, seen = [], set()
    name = parents.get(name)
    while name is not None and name not in seen:
        seen.add(name)
        out.append(name)
        name = parents.get(name)
    return out


def _is_below(parents, child, ancestor):
    return ancestor in ancestors(parents, child)


def _lca(parents, names):
    names = [n for n in names if n]
    if not names:
        return None
    common = None
    for name in names:
        chain = [name] + ancestors(parents, name)
        common = chain if common is None else [n for n in common if n in chain]
    return common[0] if common else None


def _spread(torso, neck):
    """Map SOMA's three torso and two neck joints onto however many the target has (bottom to top)."""
    out, n = {}, len(torso)
    if n == 1:
        out["chest"] = torso[0]
    elif n == 2:
        out["spine1"], out["chest"] = torso
    elif n >= 3:
        out["spine1"], out["spine2"], out["chest"] = torso[0], torso[(n - 1) // 2] if n > 3 else torso[1], torso[-1]
    if neck:
        out["neck1"] = neck[0]
        if len(neck) > 1:
            out["neck2"] = neck[-1]
    return out


def _root_for(parents, bones):
    """The travel bone: the deepest common ancestor of the hips and both thighs (the hips on most rigs)."""
    return _lca(parents, [bones.get("hips"), bones.get("thigh_l"), bones.get("thigh_r")]) or bones.get("hips")


def _finish(kind, parents, bones, root=None, extra=None, switches=()):
    bones = {k: v for k, v in bones.items() if v in parents}
    missing = [k for k in REQUIRED if k not in bones]
    if missing:
        return None
    return Rig(kind, bones, root or _root_for(parents, bones), extra, switches)


def _soma_rig(parents):
    if not all(n in parents for n in ("Hips", "LeftLeg", "LeftShin", "RightShin", "LeftForeArm")):
        return None
    extra = {n: n for n in SOMA_EXTRA if n in parents}
    return _finish("soma", parents, SOMA, extra=extra)


def _mixamo_prefix(parents):
    for name in parents:
        if name.endswith("Hips"):
            prefix = name[:-4]
            if prefix + "LeftUpLeg" in parents and prefix + "LeftForeArm" in parents:
                return prefix
    return None


def _mixamo(parents):
    prefix = _mixamo_prefix(parents)
    if prefix is None:
        return None
    p = prefix
    torso = [p + n for n in ("Spine", "Spine1", "Spine2") if p + n in parents]
    neck = [p + n for n in ("Neck", "Neck1") if p + n in parents]
    m = {"hips": p + "Hips", "head": p + "Head", **_spread(torso, neck)}
    for s, side in (("l", "Left"), ("r", "Right")):
        m.update({f"clavicle_{s}": f"{p}{side}Shoulder", f"upperarm_{s}": f"{p}{side}Arm",
                  f"forearm_{s}": f"{p}{side}ForeArm", f"hand_{s}": f"{p}{side}Hand", f"thigh_{s}": f"{p}{side}UpLeg",
                  f"calf_{s}": f"{p}{side}Leg", f"foot_{s}": f"{p}{side}Foot", f"toe_{s}": f"{p}{side}ToeBase"})
        for i in (1, 2, 3):  # Mixamo's finger 1 is the proximal phalanx; 4 is the tip's end bone.
            m[f"thumb{i}_{s}"] = f"{p}{side}HandThumb{i}"
            for f in FINGERS:
                m[f"{f}{i}_{s}"] = f"{p}{side}Hand{f.capitalize()}{i}"
    return _finish("mixamo", parents, m)


def _unreal(parents):
    lower = {n.lower(): n for n in parents}
    if not all(k in lower for k in ("pelvis", "thigh_l", "upperarm_l", "lowerarm_l", "calf_l")):
        return None
    torso = [lower[f"spine_0{i}"] for i in range(1, 10) if f"spine_0{i}" in lower]
    neck = [lower[f"neck_0{i}"] for i in range(1, 10) if f"neck_0{i}" in lower]
    m = {"hips": "pelvis", "head": "head", **{k: v.lower() for k, v in _spread(torso, neck).items()}}
    for s in "lr":
        m.update({f"clavicle_{s}": f"clavicle_{s}", f"upperarm_{s}": f"upperarm_{s}", f"forearm_{s}": f"lowerarm_{s}",
                  f"hand_{s}": f"hand_{s}", f"thigh_{s}": f"thigh_{s}", f"calf_{s}": f"calf_{s}",
                  f"foot_{s}": f"foot_{s}", f"toe_{s}": f"ball_{s}"})
        for i in (1, 2, 3):
            m[f"thumb{i}_{s}"] = f"thumb_0{i}_{s}"
            for f in FINGERS:
                m[f"{f}{i}_{s}"] = f"{f}_0{i}_{s}"
        for f in FINGERS:
            m[f"{f}0_{s}"] = f"{f}_metacarpal_{s}"  # UE5
    return _finish("unreal", parents, {k: lower[v.lower()] for k, v in m.items() if v.lower() in lower})


def _rigify_metarig(parents):
    if not all(n in parents for n in ("spine", "spine.001", "upper_arm.L", "shin.L", "thigh.R")) or any(
            n.startswith(("DEF-", "ORG-", "MCH-")) for n in parents):
        return None
    m = {"hips": "spine", "spine1": "spine.001", "spine2": "spine.002", "chest": "spine.003", "neck1": "spine.004",
         "neck2": "spine.005", "head": "spine.006"}
    for s, side in (("l", "L"), ("r", "R")):
        m.update({f"clavicle_{s}": f"shoulder.{side}", f"upperarm_{s}": f"upper_arm.{side}",
                  f"forearm_{s}": f"forearm.{side}", f"hand_{s}": f"hand.{side}", f"thigh_{s}": f"thigh.{side}",
                  f"calf_{s}": f"shin.{side}", f"foot_{s}": f"foot.{side}", f"toe_{s}": f"toe.{side}"})
        for i in (1, 2, 3):
            m[f"thumb{i}_{s}"] = f"thumb.0{i}.{side}"
            for f in FINGERS:
                m[f"{f}{i}_{s}"] = f"f_{f}.0{i}.{side}"
        for n, f in enumerate(FINGERS, 1):
            m[f"{f}0_{s}"] = f"palm.0{n}.{side}"
    return _finish("rigify_metarig", parents, m)


def _rigify(parents):
    """A generated Rigify rig. Its DEF bones follow the ORG/MCH/tweak chains through constraints, so the motion
    goes on the FK controls (spine_fk*, neck, head, shoulder, *_fk limbs, fingers), every limb switched to FK
    (IK_FK = 1 on the limb's parent control), and the torso control carries the travel. retarget() solves those
    controls against Blender's evaluation of the rig, since the constraints reroute the hierarchy."""
    if not all(n in parents for n in ("torso", "upper_arm_fk.L", "thigh_fk.R", "DEF-spine")):
        return None
    if all(n in parents for n in ("spine_fk", "spine_fk.001", "spine_fk.002", "spine_fk.003")):
        m = {"hips": "spine_fk", "spine1": "spine_fk.001", "spine2": "spine_fk.002", "chest": "spine_fk.003"}
    else:  # Generated without FK spine controls.
        m = {"hips": "hips", "chest": "chest"}
    m.update({"neck1": "neck", "head": "head"})
    switches = []
    for s, side in (("l", "L"), ("r", "R")):
        m.update({f"clavicle_{s}": f"shoulder.{side}", f"upperarm_{s}": f"upper_arm_fk.{side}",
                  f"forearm_{s}": f"forearm_fk.{side}", f"hand_{s}": f"hand_fk.{side}",
                  f"thigh_{s}": f"thigh_fk.{side}", f"calf_{s}": f"shin_fk.{side}", f"foot_{s}": f"foot_fk.{side}",
                  f"toe_{s}": f"toe_fk.{side}" if f"toe_fk.{side}" in parents else f"toe.{side}"})
        for i in (1, 2, 3):
            m[f"thumb{i}_{s}"] = f"thumb.0{i}.{side}"
            for f in FINGERS:
                m[f"{f}{i}_{s}"] = f"f_{f}.0{i}.{side}"
        switches += [(f"upper_arm_parent.{side}", "IK_FK", 1.0), (f"thigh_parent.{side}", "IK_FK", 1.0)]
    return _finish("rigify", parents, m, root="torso", switches=[w for w in switches if w[0] in parents])


def _auto_rig_pro(parents):
    if not ("c_root_master.x" in parents and "c_arm_fk.l" in parents and "c_thigh_fk.l" in parents):
        return None
    torso = [n for n in ("c_spine_01.x", "c_spine_02.x", "c_spine_03.x") if n in parents]
    neck = [n for n in ("c_subneck_1.x", "c_neck.x") if n in parents]
    m = {"hips": "c_root.x" if "c_root.x" in parents else "c_root_master.x", "head": "c_head.x", **_spread(torso, neck)}
    switches = []
    for s in "lr":
        m.update({f"clavicle_{s}": f"c_shoulder.{s}", f"upperarm_{s}": f"c_arm_fk.{s}",
                  f"forearm_{s}": f"c_forearm_fk.{s}", f"hand_{s}": f"c_hand_fk.{s}", f"thigh_{s}": f"c_thigh_fk.{s}",
                  f"calf_{s}": f"c_leg_fk.{s}", f"foot_{s}": f"c_foot_fk.{s}",
                  f"toe_{s}": f"c_toes_fk.{s}" if f"c_toes_fk.{s}" in parents else f"c_toes.{s}"})
        for i in (1, 2, 3):
            m[f"thumb{i}_{s}"] = f"c_thumb{i}.{s}"
            for f in FINGERS:
                m[f"{f}{i}_{s}"] = f"c_{f}{i}.{s}"
        for f in FINGERS:
            m[f"{f}0_{s}"] = f"c_{f}1_base.{s}"
        switches += [(f"c_hand_ik.{s}", "ik_fk_switch", 1.0), (f"c_foot_ik.{s}", "ik_fk_switch", 1.0)]
    return _finish("auto_rig_pro", parents, m, root="c_root_master.x",
                   switches=[w for w in switches if w[0] in parents])


# Generic humanoids: classify each bone by its name's words, then keep the chains that hang together.
_LEFT, _RIGHT = {"l", "left", "lft", "lt"}, {"r", "right", "rgt", "rt"}
_SKIP = {"twist", "roll", "ik", "pole", "target", "tgt", "mch", "org", "tweak", "helper", "end", "nub", "tip", "null",
         "dummy", "socket", "attach", "weapon", "prop", "jiggle", "breast", "eye", "eyes", "jaw", "tongue", "teeth",
         "brow", "lid", "lip", "cheek", "ear", "hair", "corrective", "offset", "aim", "hinge", "pivot", "top", "vis",
         "share", "adjust", "upvector", "nose", "mouth", "face"}
_FINGER_WORDS = {"thumb": "thumb", "index": "index", "pointer": "index", "middle": "middle", "mid": "middle",
                 "ring": "ring", "pinky": "pinky", "pinkie": "pinky", "little": "pinky", "small": "pinky"}


def tokens(name):
    name = re.split(r"[:|]", name)[-1]
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    name = re.sub(r"([A-Za-z])(\d)", r"\1 \2", name)
    words = []
    for word in re.split(r"[^A-Za-z0-9]+", name.lower()):
        for side in ("left", "right"):
            if word.startswith(side) and len(word) > len(side):
                words.append(side)
                word = word[len(side):]
        if word:
            words.append(word)
    return words


def classify(name):
    """(part, side) for one bone name, or None. Finger parts come back without their number."""
    words = tokens(name)
    found = set(words)
    side = "l" if found & _LEFT else "r" if found & _RIGHT else None
    found -= _LEFT | _RIGHT
    if found & _SKIP:
        return None
    joined = "".join(w for w in words if w not in _LEFT | _RIGHT and not w.isdigit())
    finger = next((_FINGER_WORDS[w] for w in words if w in _FINGER_WORDS), None)
    if finger:
        if side is None:
            return None
        meta = bool(found & {"meta", "metacarpal", "carpal", "palm"}) or "metacarpal" in joined
        return (finger + ("0" if meta and finger != "thumb" else ""), side)
    if "palm" in found or "metacarpal" in joined:
        return None
    rules = (
        ("hand", ("hand", "wrist")),
        ("forearm", ("forearm", "lowerarm", "elbow", "lowarm", "forearms")),
        ("upperarm", ("upperarm", "uparm", "humerus", "arm", "shldr")),
        ("clavicle", ("clavicle", "collar", "shoulder", "clav", "collarbone")),
        ("toe", ("toe", "toes", "toebase", "ball")),
        ("foot", ("foot", "ankle")),
        ("calf", ("calf", "shin", "knee", "lowerleg", "lowleg", "crus")),
        ("thigh", ("thigh", "upleg", "upperleg", "femur")),
    )
    for part, keys in rules:
        if any(key == joined or key in found for key in keys):
            if part == "upperarm" and found & {"fore", "lower", "low"}:
                part = "forearm"
            return (part, side) if side else None
    if "leg" in found or joined == "leg":
        if side is None:
            return None
        return ("thigh", side) if found & {"up", "upper"} else ("calf", side) if found & {"lower", "low"} else ("leg", side)
    if "arm" in joined and side:
        return ("forearm" if found & {"fore", "lower", "low"} else "upperarm", side)
    if found & {"hip", "hips"}:
        return ("thigh", side) if side else ("hips", None)
    if side is None:
        if found & {"pelvis"}:
            return ("hips", None)
        if found & {"spine", "chest", "torso", "abdomen", "back", "waist", "belly", "abdomen", "spine1", "ribcage"}:
            return ("spine", None)
        if "neck" in found:
            return ("neck", None)
        if "head" in found:
            return ("head", None)
    return None


def _distance(heads, a, b):
    return math.dist(heads[a], heads[b]) if a in heads and b in heads else None


def _generic(parents, deform, heads):
    depth = {n: len(ancestors(parents, n)) for n in parents}
    found = {}
    for name in parents:
        hit = classify(name)
        if hit:
            found.setdefault(hit, []).append(name)
    for key in found:
        found[key].sort(key=lambda n: (depth[n], not deform.get(n, True), n))
    m = {}

    def pick(key, below=None):
        for name in found.get(key, []):
            if below is None or _is_below(parents, name, below):
                return name
        return None

    for s in "lr":
        # A side's "leg" is the thigh when the rig has no thigh word, else the calf (Auto-Rig Pro's "leg").
        legs = found.get(("leg", s), [])
        thigh = pick(("thigh", s)) or (legs[0] if legs else None)
        calf = pick(("calf", s), thigh) or next((n for n in legs if n != thigh and thigh
                                                 and _is_below(parents, n, thigh)), None)
        foot = pick(("foot", s), calf)
        toe = pick(("toe", s), foot)
        upper = pick(("upperarm", s))
        fore = pick(("forearm", s), upper)
        hand = pick(("hand", s), fore)
        clav = pick(("clavicle", s))
        if clav and upper and not _is_below(parents, upper, clav):
            clav = None
        m.update({f"thigh_{s}": thigh, f"calf_{s}": calf, f"foot_{s}": foot, f"toe_{s}": toe,
                  f"upperarm_{s}": upper, f"forearm_{s}": fore, f"hand_{s}": hand, f"clavicle_{s}": clav})
        for finger in ("thumb",) + FINGERS:
            chain = [n for n in found.get((finger, s), []) if hand and _is_below(parents, n, hand)]
            if finger != "thumb" and len(chain) == 4:
                # Four bones: a metacarpal first (its head at the wrist) or a tip's end bone last.
                wrist, first = _distance(heads, hand, chain[0]), _distance(heads, chain[0], chain[1])
                if wrist is not None and first and wrist < 0.5 * first:
                    m[f"{finger}0_{s}"], chain = chain[0], chain[1:]
            meta = pick((finger + "0", s), hand) if finger != "thumb" else None
            if meta:
                m[f"{finger}0_{s}"] = meta
            for i, name in enumerate(chain[:3], 1):
                m[f"{finger}{i}_{s}"] = name
    hips = pick(("hips", None)) or _lca(parents, [m.get("thigh_l"), m.get("thigh_r")])
    head = pick(("head", None))
    m["hips"], m["head"] = hips, head
    if hips and head:
        top = _lca(parents, [hips, head])
        path = [n for n in ancestors(parents, head) if n != top and top in ancestors(parents, n)] if top else []
        path.reverse()  # Bottom to top.
        necks = [n for n in path if classify(n) == ("neck", None)]
        first_neck = path.index(necks[0]) if necks else len(path)
        if not necks:
            # No neck word: the bones above the clavicles' common parent are the neck.
            chest = _lca(parents, [m.get("clavicle_l") or m.get("upperarm_l"), m.get("clavicle_r") or m.get("upperarm_r")])
            if chest in path:
                first_neck = path.index(chest) + 1
        m.update(_spread(path[:first_neck], path[first_neck:]))
    return _finish("generic", parents, {k: v for k, v in m.items() if v})


def detect(bones):
    """The rig a list of (name, parent[, deform]) describes, or None when it is not a mappable humanoid."""
    parents, deform, heads = _hierarchy(bones)
    for finder in (_soma_rig, _mixamo, _rigify, _rigify_metarig, _auto_rig_pro, _unreal):
        rig = finder(parents)
        if rig is not None:
            return rig
    return _generic(parents, deform, heads)


def rig_of(ob):
    """detect() for an armature object; None when it is not one or not a humanoid."""
    if ob is None or getattr(ob, "type", None) != "ARMATURE":
        return None
    return detect([(b.name, b.parent.name if b.parent else None, b.use_deform, tuple(b.head_local))
                   for b in ob.data.bones])


def armature_of(ob):
    """The armature an object stands for: itself, its Armature modifier's object, or its armature parent."""
    if ob is None:
        return None
    if ob.type == "ARMATURE":
        return ob
    for modifier in getattr(ob, "modifiers", ()):
        if modifier.type == "ARMATURE" and modifier.object is not None:
            return modifier.object
    parent = ob.parent
    return parent if parent is not None and parent.type == "ARMATURE" else None


def action_name(text):
    text = " ".join(str(text or "").split()) or "motion"
    if len(text.encode("utf-8")) > NAME_BYTES:
        cut = text
        while len((cut + "…").encode("utf-8")) > NAME_BYTES:
            cut = cut[:-1]
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        text = cut.rstrip(" ,.;:") + "…"
    return ACTION_PREFIX + text


def smooth(values, window):
    """Centered moving average of 3-tuples. The ends are padded by point reflection (2 p0 - p_i), so a steady
    path averages to itself there too instead of lagging."""
    half, n = max(0, int(window) // 2), len(values)
    if n < 2 or half == 0:
        return [tuple(v) for v in values]
    head = [tuple(2 * values[0][k] - values[min(i, n - 1)][k] for k in range(3)) for i in range(half, 0, -1)]
    tail = [tuple(2 * values[-1][k] - values[max(n - 1 - i, 0)][k] for k in range(3)) for i in range(1, half + 1)]
    padded = head + [tuple(v) for v in values] + tail
    return [tuple(sum(v[k] for v in padded[i:i + 2 * half + 1]) / (2 * half + 1) for k in range(3)) for i in range(n)]


def sample_times(frames, source_fps, scene_fps):
    """Source frame (fractional) for each scene frame, so a 30 fps motion keeps its timing at 24 fps."""
    if frames < 1 or source_fps <= 0 or scene_fps <= 0:
        return []
    count = int(math.floor((frames - 1) * scene_fps / source_fps + 1e-6)) + 1
    return [min(frames - 1.0, i * source_fps / scene_fps) for i in range(count)]


# ---------------------------------------------------------------------------------------- relaxed hands

# A motion that does not animate fingers (Kimodo's: motion.json fingers_animated false) would leave the rig's own
# rest on them, flat and straight on T-pose rigs. Those hands get a relaxed pose instead. Bend in degrees at each
# joint from the knuckle out, toward the palm: most at the middle joint, more toward the pinky. A joint the rig
# already bends that far at rest is not bent further. The thumb turns a little toward the palm and the fingers.
RELAXED = {"index": (12.0, 22.0, 12.0), "middle": (15.0, 26.0, 14.0), "ring": (18.0, 30.0, 16.0),
           "pinky": (22.0, 34.0, 18.0)}
RELAXED_THUMB = (6.0, 10.0, 12.0)  # Added at each thumb joint.
RELAXED_TAIL = math.cos(math.radians(75.0))  # A tail turned further from its chain is not read as a direction.
RELAXED_KEY = "loopcut_relaxed_hands"  # On an action: the bones it keys with a relaxed hand (a JSON list).


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _unit(a):
    length = math.sqrt(_dot(a, a))
    return (a[0] / length, a[1] / length, a[2] / length) if length > 1e-9 else None


def _bend(u, v, axis):
    """Degrees from direction u to direction v about axis, seen in the plane across the axis."""
    u = _sub(u, tuple(axis[i] * _dot(u, axis) for i in range(3)))
    v = _sub(v, tuple(axis[i] * _dot(v, axis) for i in range(3)))
    return math.degrees(math.atan2(_dot(axis, _cross(u, v)), _dot(u, v)))


def _turn(matrix, axis, degrees):
    """The pose basis (w, x, y, z) that turns a bone with rest rotation matrix (rows) about axis (armature space)."""
    local = _unit(tuple(sum(matrix[i][j] * axis[i] for i in range(3)) for j in range(3)))
    half = math.radians(degrees) / 2
    return (math.cos(half),) + tuple(math.sin(half) * c for c in local)


def relaxed_hand(side, rest):
    """A relaxed pose for one hand, side "l" or "r", from its rest geometry; no Blender needed.
    rest: canonical -> (head, tail, rotation rows) in armature space for the mapped hand, finger and thumb bones.
    Returns canonical -> pose basis rotation (w, x, y, z), only for the bones it turns.

    The palm faces (fingers' direction) x (index to pinky) on a right hand, the mirror on a left one, whatever the
    rest pose. Each finger bends about one hinge, its first bone's direction x the palm normal, so the whole chain
    curls in its own plane toward the palm in every rig's own bone axes (rolls, Mixamo's, Unreal's, Rigify's)."""
    def at(part):
        return rest.get(f"{part}_{side}")

    hand = at("hand")
    knuckles = [at(f"{f}1")[0] for f in FINGERS if at(f"{f}1")]
    if hand is None or not knuckles:
        return {}
    wrist = hand[0]
    along = _unit(_sub(tuple(sum(k[i] for k in knuckles) / len(knuckles) for i in range(3)), wrist))
    if len(knuckles) > 1:
        across = _sub(knuckles[-1], knuckles[0])
    elif at("thumb1"):  # One finger: the thumb is on the index side.
        across = _sub(knuckles[0], at("thumb1")[0])
    else:
        return {}
    if along is None:
        return {}
    across = _unit(_sub(across, tuple(along[i] * _dot(across, along) for i in range(3))))
    if across is None:
        return {}
    normal = _cross(along, across) if side == "r" else _cross(across, along)
    out = {}
    for f in FINGERS:
        chain = []
        for i in (1, 2, 3):
            if at(f"{f}{i}") is None:
                break
            chain.append((f"{f}{i}_{side}", at(f"{f}{i}")))
        if not chain:
            continue
        base = at(f"{f}0")
        dirs = [_unit(_sub(chain[0][1][0], base[0] if base else wrist)) or along]
        for n, (_c, (head, tail, _m)) in enumerate(chain):
            d = _unit(_sub(chain[n + 1][1][0] if n + 1 < len(chain) else tail, head))
            dirs.append(d if d is not None and _dot(d, dirs[-1]) > RELAXED_TAIL else dirs[-1])
        hinge = _unit(_cross(dirs[1], normal)) or _unit(_cross(along, normal))
        for n, (c, (_h, _t, matrix)) in enumerate(chain):
            want = RELAXED[f][n]
            add = min(max(0.0, want - _bend(dirs[n], dirs[n + 1], hinge)), want + 30.0)
            if add > 0.1:
                out[c] = _turn(matrix, hinge, add)
    thumb = []
    for i in (1, 2, 3):
        if at(f"thumb{i}") is None:
            break
        thumb.append((f"thumb{i}_{side}", at(f"thumb{i}")))
    if thumb:
        head, tail, _m = thumb[0][1]
        d = _unit(_sub(thumb[-1][1][0], head) if len(thumb) > 1 else _sub(tail, head))
        toward = _unit(tuple(normal[i] + across[i] for i in range(3)))
        hinge = _unit(_cross(d, toward)) if d is not None else None
        if hinge is not None:
            for n, (c, (_h, _t, matrix)) in enumerate(thumb):
                out[c] = _turn(matrix, hinge, RELAXED_THUMB[n])
    return out


# ------------------------------------------------------------------------------- Blender: source

class Source:
    """The BVH in Blender's axes (Z up, -Y forward, metres): joint global rotations and positions."""

    def __init__(self, bvh, scale=0.01):
        from mathutils import Quaternion, Vector
        self.bvh, self.scale = bvh, scale
        self._axis = Quaternion((1.0, 0.0, 0.0), math.radians(90.0))  # BVH Y up -> Z up, +Z facing -> -Y.
        self._axis_inv = self._axis.inverted()
        self._unit = {"X": Vector((1, 0, 0)), "Y": Vector((0, 1, 0)), "Z": Vector((0, 0, 1))}
        self._local = {}
        self.index = {j.name: n for n, j in enumerate(bvh.joints)}
        rest = []
        for joint in bvh.joints:
            offset = Vector(joint.offset)
            rest.append(offset if joint.parent is None else rest[self.index[joint.parent.name]] + offset)
        self.rest = {j.name: self._point(rest[n]) for n, j in enumerate(bvh.joints)}

    def _point(self, v):
        return (self._axis @ v) * self.scale

    def _local_frame(self, i):
        from mathutils import Quaternion, Vector
        cached = self._local.get(i)
        if cached is not None:
            return cached
        values, out = self.bvh.frames[i], []
        for joint in self.bvh.joints:
            q, position = Quaternion(), None
            for k, channel in enumerate(joint.channels):
                value = values[joint.column + k]
                if channel.endswith("rotation"):
                    q = q @ Quaternion(self._unit[channel[0]], math.radians(value))
                else:
                    if position is None:
                        position = Vector(joint.offset)
                    position["XYZ".index(channel[0])] = value
            out.append((q, position))
        if len(self._local) > 4096:
            self._local.clear()
        self._local[i] = out
        return out

    def pose(self, s):
        """Globals at fractional frame s: {joint: (rotation Quaternion, position Vector)}, Blender axes."""
        from mathutils import Vector
        i0 = max(0, min(len(self.bvh.frames) - 1, int(math.floor(s))))
        i1, t = min(i0 + 1, len(self.bvh.frames) - 1), s - i0
        a = self._local_frame(i0)
        b = self._local_frame(i1) if t > 1e-6 and i1 != i0 else a
        rotations, positions, out = [], [], {}
        for n, joint in enumerate(self.bvh.joints):
            (qa, pa), (qb, pb) = a[n], b[n]
            if b is a:
                q, p = qa, pa
            else:
                q = qa.slerp(qb if qa.dot(qb) >= 0 else -qb, t)
                p = pa.lerp(pb, t) if pa is not None and pb is not None else pa
            offset = p if p is not None else Vector(joint.offset)
            if joint.parent is None:
                g, x = q, offset
            else:
                k = self.index[joint.parent.name]
                g, x = rotations[k] @ q, positions[k] + rotations[k] @ offset
            rotations.append(g)
            positions.append(x)
            out[joint.name] = (self._axis @ g @ self._axis_inv, self._point(x))
        return out


# ------------------------------------------------------------------------------- Blender: retarget

def body_frame(hips, head, left, right):
    """Columns: left, up, forward, from four points."""
    from mathutils import Matrix
    up = (head - hips).normalized()
    side = left - right
    side = (side - up * side.dot(up)).normalized()
    return Matrix((side, up, side.cross(up))).transposed()


def _yaw_between(a, b):
    from mathutils import Matrix
    a, b = a.copy(), b.copy()
    a.z = b.z = 0.0
    if a.length < 1e-6 or b.length < 1e-6:
        return Matrix.Identity(3)
    angle = math.atan2(a.x * b.y - a.y * b.x, a.x * b.x + a.y * b.y)
    return Matrix.Rotation(angle, 3, "Z")


def _leg_length(points, side_names):
    total = 0.0
    for thigh, calf, foot in side_names:
        total += (points[calf] - points[thigh]).length + (points[foot] - points[calf]).length
    return total / max(1, len(side_names))


def retarget(source, ob, rig, *, scene, frame_start, in_place=False, fingers=True, name="Motion: motion"):
    """Bake the motion onto armature object ob (rig from detect()). Returns a report dict.
    fingers=False (a motion that does not animate them): the mapped finger bones get a relaxed hand, held over the
    action's frames, except bones that what plays on ob now already keys (those are left as they are)."""
    import json
    import bpy
    from mathutils import Matrix, Quaternion, Vector
    bones, data = ob.data.bones, ob.data
    warnings = []
    pairs = {}  # target bone -> (SOMA joint, canonical or None)
    for canonical, bone in rig.bones.items():
        joint = SOMA.get(canonical)
        if joint in source.rest and bone in bones and (fingers or part_of(canonical) not in FINGER_PARTS):
            pairs[bone] = (joint, canonical)
    for bone, joint in rig.extra.items():
        if joint in source.rest and bone in bones:
            pairs.setdefault(bone, (joint, None))
    root = rig.root if rig.root in bones else rig.bones["hips"]
    relaxed, kept = {}, []
    if not fingers:
        keyed = _keyed_bones(ob)  # Before the new action stashes the current one.
        hands = {c: b for c, b in rig.bones.items() if b in bones and part_of(c) in FINGER_PARTS | {"hand"}}
        kept = sorted(b for c, b in hands.items() if b in keyed and part_of(c) != "hand")
        rest = {c: (tuple(bones[b].head_local), tuple(bones[b].tail_local),
                    [tuple(row) for row in bones[b].matrix_local.to_3x3()]) for c, b in hands.items()}
        for s in "lr":
            relaxed.update({hands[c]: q for c, q in relaxed_hand(s, rest).items() if hands[c] not in keyed})

    bpy.context.view_layer.update()
    mw = ob.matrix_world.copy()
    mw_inv = mw.inverted()
    mw3_inv = mw.to_3x3().normalized().inverted()
    rest = {b.name: mw @ b.head_local for b in bones}
    current = {pb.name: mw @ pb.head for pb in ob.pose.bones}
    src_rest = source.rest
    tb = rig.bones

    F = body_frame(src_rest["Hips"], src_rest["Head"], src_rest["LeftArm"], src_rest["RightArm"]) @ body_frame(
        rest[tb["hips"]], rest[tb["head"]], rest[tb["upperarm_l"]], rest[tb["upperarm_r"]]).transposed()

    align = {}
    for part in ("upperarm", "forearm", "hand", "thigh", "calf", "foot", "toe", "thumb1", "thumb2", "thumb3") + tuple(
            f"{f}{i}" for f in FINGERS for i in (0, 1, 2, 3)):
        for s in "lr":
            c = f"{part}_{s}"
            if c not in tb:
                continue
            child = DIRECTION.get(part)
            child_c = f"{child}_{s}" if child else None
            if child_c in tb:
                s_dir = src_rest[SOMA[child_c]] - src_rest[SOMA[c]]
                t_dir = F @ (rest[tb[child_c]] - rest[tb[c]])
                if s_dir.length > 1e-6 and t_dir.length > 1e-6:
                    align[c] = t_dir.rotation_difference(s_dir).to_matrix()
                    continue
            parent = ALIGN_PARENT.get(part)
            align[c] = align.get(f"{parent}_{s}", Matrix.Identity(3)) if parent else Matrix.Identity(3)

    legs_t = [(tb[f"thigh_{s}"], tb[f"calf_{s}"], tb[f"foot_{s}"]) for s in "lr"]
    legs_s = [(SOMA[f"thigh_{s}"], SOMA[f"calf_{s}"], SOMA[f"foot_{s}"]) for s in "lr"]
    k = _leg_length(rest, legs_t) / max(1e-6, _leg_length(src_rest, legs_s))

    fps_scene = scene.render.fps / scene.render.fps_base
    times = sample_times(len(source.bvh.frames), source.bvh.fps, fps_scene)
    poses = [source.pose(s) for s in times]

    def pelvis(points):
        return (points["LeftLeg"] + points["RightLeg"]) * 0.5

    src_pelvis = [tuple(pelvis({j: p[j][1] for j in ("LeftLeg", "RightLeg")})) for p in poses]
    # Ground: the lowest ball of the foot over the motion, or the ankle when the target has no toes.
    toes = all(f"toe_{s}" in tb for s in "lr")
    ground_s = min(min(p[side + ("ToeBase" if toes else "Foot")][1].z for side in ("Left", "Right")) for p in poses)
    ground_t = min(current[tb[f"{'toe' if toes else 'foot'}_{s}"]].z for s in "lr")
    anchor = (current[tb["thigh_l"]] + current[tb["thigh_r"]]) * 0.5
    first = poses[0]
    psi = _yaw_between((first["LeftLeg"][1] - first["RightLeg"][1]).cross(Vector((0, 0, 1))),
                       (current[tb["thigh_l"]] - current[tb["thigh_r"]]).cross(Vector((0, 0, 1))))
    travel = src_pelvis
    if in_place:  # Keep the sway, drop the travel: subtract a one-second moving average of the path.
        trend = smooth(src_pelvis, round(fps_scene))
        travel = [(p[0] - t[0], p[1] - t[1], p[2]) for p, t in zip(src_pelvis, trend)]

    # Bones whose pose the chain math assumes: the mapped ones, the travel bone and their ancestors.
    needed = set(pairs) | {root}
    for bone_name in list(needed):
        needed.update(b.name for b in bones[bone_name].parent_recursive)
    order = sorted(needed, key=lambda n: len(bones[n].parent_recursive))
    rel = {}
    for n in order:
        b = bones[n]
        rel[n] = b.parent.matrix_local.inverted() @ b.matrix_local if b.parent else b.matrix_local.copy()
    # What a refused bake puts back: the pose, the switches and the animation data.
    saved = {n: (ob.pose.bones[n].rotation_mode, ob.pose.bones[n].matrix_basis.copy())
             for n in needed | set(relaxed)}
    saved_switches = [(b, p, ob.pose.bones[b][p]) for b, p, _v in rig.switches
                      if b in ob.pose.bones and p in ob.pose.bones[b].keys()]
    ad = ob.animation_data
    previous = (ad is not None, ad.action if ad else None, getattr(ad, "action_slot", None) if ad else None)
    for n in needed:
        pb = ob.pose.bones[n]
        pb.matrix_basis = Matrix.Identity(4)
        if pb.rotation_mode == "AXIS_ANGLE":
            pb.rotation_mode = "QUATERNION"
    for n in relaxed:
        if ob.pose.bones[n].rotation_mode == "AXIS_ANGLE":
            ob.pose.bones[n].rotation_mode = "QUATERNION"
    for n in pairs:
        pb = ob.pose.bones[n]
        if any(c.type == "IK" and c.influence > 0 and not c.mute for c in pb.constraints):
            warnings.append(f"{n} has an IK constraint that overrides the motion")
    if bones[root].use_connect:
        warnings.append(f"{root} is connected to its parent, so the character cannot travel")
    series = []
    for bone, prop, value in rig.switches:  # Before baking: the switches decide what the controls drive.
        pb = ob.pose.bones.get(bone)
        if pb is not None and prop in pb.keys():
            pb[prop] = value
            series.append((f'pose.bones["{_escape(bone)}"]["{prop}"]', 0, bone, [value]))
    # Constraints between the controls (Rigify, Auto-Rig Pro) reroute the hierarchy the chain math assumes:
    # then each frame is corrected against Blender's own evaluation of the rig.
    evaluate = any(not c.mute and c.influence > 0 for n in needed for c in ob.pose.bones[n].constraints)
    action, stashed = _new_action(ob, name)  # Empty and active now, so the old action cannot override the bake.

    targets = {n: (joint, align.get(c, Matrix.Identity(3)) if c else Matrix.Identity(3)) for n, (joint, c) in pairs.items()}
    base = {n: F @ (mw.to_3x3().normalized() @ bones[n].matrix_local.to_3x3()) for n in pairs}
    rotations = {n: [] for n in pairs}
    locations = []
    pelvis_bones = (tb["thigh_l"], tb["thigh_r"])
    mapped_order = [n for n in order if n in targets]
    muted = _mute_deform(ob) if evaluate else []
    clamped = 0
    try:
        # The evaluated rig may not carry the pelvis with the travel bone (constraint-placed thighs: CloudRig's
        # hinges sent Snow's hips 2 km): an axis it does not carry is never pushed, and each frame's location
        # stays within two leg lengths of the chain math's.
        gain = travel_gain(_travel_response(ob, root, pelvis_bones)) if evaluate else (1.0, 1.0, 1.0)
        reach = 2 * _leg_length({b.name: b.head_local for b in bones}, legs_t)
        for f, pose in enumerate(poses):
            arm, desired = {}, {}
            for n in order:
                b = bones[n]
                chain = arm[b.parent.name] @ rel[n] if b.parent and b.parent.name in arm else rel[n]
                if n in targets:
                    joint, a = targets[n]
                    desired[n] = (mw3_inv @ (psi @ pose[joint][0].to_matrix() @ a @ base[n])).normalized()
                    basis = (chain.to_3x3().inverted() @ desired[n]).normalized()
                    rotations[n].append(basis)
                    arm[n] = chain @ basis.to_4x4()
                else:
                    arm[n] = chain
            p = travel[f]
            want = mw_inv @ (Vector((anchor.x, anchor.y, ground_t))
                             + psi @ (Vector((p[0] - travel[0][0], p[1] - travel[0][1], p[2] - ground_s)) * k))
            p0 = (arm[pelvis_bones[0]].translation + arm[pelvis_bones[1]].translation) * 0.5
            b = bones[root]
            chain = arm[b.parent.name] @ rel[root] if b.parent and b.parent.name in arm else rel[root]
            locations.append(chain.to_3x3().inverted() @ (want - p0))
            if evaluate:
                guess = locations[-1] if any(gain) else Vector()  # A bone that carries nothing stays at rest.
                start = {n: rotations[n][-2] for n in targets} if f else {n: rotations[n][-1] for n in targets}
                start_loc = locations[-2] if f and any(gain) else guess
                solved, loc = _solve(ob, mapped_order, desired, start, start_loc, root, pelvis_bones, want, gain)
                for n in targets:
                    rotations[n][-1] = solved[n]
                if (loc - guess).length > reach:
                    loc, clamped = guess.copy(), clamped + 1
                locations[-1] = loc
    finally:
        for modifier in muted:
            modifier.show_viewport = True
    if not any(gain):
        warnings.append(f"moving {root} does not move the hips, so the character stays where it stands")
    elif not all(gain):
        warnings.append(f"moving {root} does not move the hips along every axis, so part of the travel is lost")
    if clamped:
        warnings.append(f"{root} did not settle on {clamped} frames, where it follows the motion's path instead")

    frames = [frame_start + i for i in range(len(times))]
    for n, mats in rotations.items():
        mode = ob.pose.bones[n].rotation_mode
        values = _rotation_values(mats, mode)
        path = "rotation_quaternion" if mode == "QUATERNION" else "rotation_euler"
        series += [(f'pose.bones["{_escape(n)}"].{path}', i, n, [v[i] for v in values]) for i in range(len(values[0]))]
    series += [(f'pose.bones["{_escape(root)}"].location', i, root, [v[i] for v in locations]) for i in range(3)]
    ends = frames[:1] + frames[1:][-1:]  # A relaxed hand: one value, keyed at the first and last frame.
    for n, q in relaxed.items():
        mode = ob.pose.bones[n].rotation_mode
        (value,) = _rotation_values([Quaternion(q).to_matrix()], mode)
        path = "rotation_quaternion" if mode == "QUATERNION" else "rotation_euler"
        series += [(f'pose.bones["{_escape(n)}"].{path}', i, n, [value[i]] * len(ends), ends)
                   for i in range(len(value))]
    _write(ob, action, frames, series)
    if relaxed:
        action[RELAXED_KEY] = json.dumps(sorted(relaxed))
    ob.pose.bones[root].location = locations[0]
    missed, judged = _unreached(ob, rig, scene, frames, poses, k)
    if missed and len(missed) == judged:
        _put_back(ob, action, previous, saved, saved_switches)
        raise MotionError(_refusal(ob, rig, len(pairs), missed))
    if missed:
        warnings.append(f"the {' and '.join(missed)} did not follow the motion: their mapped bones do not move "
                        "the deforming bones (a limb left in IK mode?)")
    return {"action": action, "frames": (frames[0], frames[-1]), "count": len(frames), "fps": fps_scene,
            "mapped": len(pairs), "stashed": stashed, "warnings": warnings, "scale": k, "evaluated": evaluate,
            "relaxed": len(relaxed), "kept": len(kept)}


def _rotation_values(mats, mode):
    values, prev = [], None
    for m in mats:
        if mode == "QUATERNION":
            q = m.to_quaternion()
            if prev is not None:
                q.make_compatible(prev)
            prev = q
        else:
            q = m.to_euler(mode, prev) if prev is not None else m.to_euler(mode)
            prev = q
        values.append(tuple(q))
    return values


def _set_rotation(pb, matrix):
    if pb.rotation_mode == "QUATERNION":
        q = matrix.to_quaternion()
        if q.dot(pb.rotation_quaternion) < 0:
            q.negate()
        pb.rotation_quaternion = q
    else:
        pb.rotation_euler = matrix.to_euler(pb.rotation_mode, pb.rotation_euler)


def _mute_deform(ob):
    """Meshes this armature deforms are not evaluated while each frame is solved; turned back on after."""
    import bpy
    muted = []
    for other in bpy.data.objects:
        for modifier in getattr(other, "modifiers", ()):
            if modifier.type == "ARMATURE" and modifier.object == ob and modifier.show_viewport:
                modifier.show_viewport = False
                muted.append(modifier)
    return muted


SOLVE_STEPS = 8
SOLVE_DEGREES = 0.25


def _travel_response(ob, root, pelvis_bones, step=0.1):
    """How much of a move of the travel bone along each armature axis reaches the pelvis, on Blender's evaluation
    with every rotation held: 1 when it carries the pelvis rigidly, 0 when constraints place the thighs."""
    import bpy
    from mathutils import Vector
    view_layer = bpy.context.view_layer
    pb = ob.pose.bones[root]

    def pelvis():
        view_layer.update()
        return (ob.pose.bones[pelvis_bones[0]].head + ob.pose.bones[pelvis_bones[1]].head) * 0.5

    saved, before = pb.location.copy(), pelvis()
    own = (pb.matrix.to_3x3() @ pb.matrix_basis.to_3x3().inverted_safe()).inverted_safe()
    response = []
    for axis in range(3):
        move = Vector((0.0, 0.0, 0.0))
        move[axis] = step
        pb.location = saved + own @ move
        response.append((pelvis() - before)[axis] / step)
    pb.location = saved
    view_layer.update()
    return tuple(response)


def travel_gain(response):
    """Per armature axis, how far to move the travel bone per unit the pelvis is off: the inverse of its response
    (_travel_response), 1 when rigid, and 0 where under half of a move reaches the pelvis, so it is never pushed."""
    return tuple(0.0 if r < 0.5 else 1.0 if abs(r - 1.0) < 0.05 else 1.0 / r for r in response)


def _solve(ob, order, desired, start, start_loc, root, pelvis_bones, want, gain):
    """Correct each mapped bone against the evaluated rig until its armature-space rotation is the desired one:
    pose = E @ basis, so basis' = basis @ current^-1 @ desired. The travel bone moves the pelvis along the axes it
    carries (gain: travel_gain())."""
    import bpy
    from mathutils import Vector
    view_layer = bpy.context.view_layer
    basis = {n: start[n].copy() for n in order}
    loc = start_loc.copy()
    pbs = {n: ob.pose.bones[n] for n in order}
    root_pb = ob.pose.bones[root]
    for n in order:
        _set_rotation(pbs[n], basis[n])
    root_pb.location = loc
    for _step in range(SOLVE_STEPS):
        view_layer.update()
        worst = 0.0
        for n in order:
            current = pbs[n].matrix.to_3x3().normalized()
            correction = current.inverted() @ desired[n]
            angle = correction.to_quaternion().angle
            worst = max(worst, min(angle, 2 * math.pi - angle))
            basis[n] = (basis[n] @ correction).normalized()
            _set_rotation(pbs[n], basis[n])
        pelvis = (ob.pose.bones[pelvis_bones[0]].head + ob.pose.bones[pelvis_bones[1]].head) * 0.5
        offset = Vector([o * g for o, g in zip(want - pelvis, gain)])
        if any(gain):
            own = root_pb.matrix.to_3x3() @ root_pb.matrix_basis.to_3x3().inverted_safe()
            loc = loc + own.inverted_safe() @ offset
            root_pb.location = loc
        if math.degrees(worst) < SOLVE_DEGREES and offset.length < 1e-3:
            break
    view_layer.update()
    return {n: basis[n] for n in order}, loc


def _escape(name):
    return name.replace("\\", "\\\\").replace('"', '\\"')


_POSE_BONE = re.compile(r'^pose\.bones\["((?:[^"\\]|\\.)*)"\]')


def _curves_of(action):
    if hasattr(action, "layers"):
        return [c for layer in action.layers for strip in layer.strips for bag in strip.channelbags
                for c in bag.fcurves]
    return list(action.fcurves)  # Before slotted actions.


def _keyed_bones(ob):
    """Bones that what plays on ob now keys: its action and the actions of its unmuted NLA strips. A relaxed hand
    this module keyed (listed under RELAXED_KEY) that nobody has changed since (flat, two keys at most) is not."""
    import json
    ad = ob.animation_data
    if ad is None:
        return set()
    actions = [ad.action] if ad.action is not None else []
    actions += [s.action for t in ad.nla_tracks if not t.mute for s in t.strips if s.action is not None and not s.mute]
    keyed = set()
    for action in actions:
        try:
            ours = set(json.loads(action.get(RELAXED_KEY) or "[]"))
        except (TypeError, ValueError):
            ours = set()
        curves = {}
        for curve in _curves_of(action):
            match = _POSE_BONE.match(curve.data_path)
            if match:
                curves.setdefault(re.sub(r"\\(.)", r"\1", match.group(1)), []).append(curve)
        for bone, found in curves.items():
            if bone in ours and all(len(c.keyframe_points) <= 2
                                    and len({round(p.co[1], 6) for p in c.keyframe_points}) <= 1 for c in found):
                continue
            keyed.add(bone)
    return keyed


def _new_action(ob, name):
    """A fresh active action; the previous one goes to a muted "[Action Stash]" NLA track, as Blender does."""
    import bpy
    ad = ob.animation_data or ob.animation_data_create()
    stashed = None
    if ad.action is not None:
        old, slot = ad.action, getattr(ad, "action_slot", None)
        track = ad.nla_tracks.new()
        track.name = "[Action Stash]"
        strip = track.strips.new(old.name, int(old.frame_range[0]), old)
        if slot is not None and getattr(strip, "action_slot", None) is None:
            try:
                strip.action_slot = slot
            except (AttributeError, TypeError, RuntimeError):
                pass
        track.mute = True
        stashed = old.name
        ad.action = None
    action = bpy.data.actions.new(name)
    ad.action = action
    if hasattr(action, "slots"):
        slot = action.slots.new(id_type="OBJECT", name=ob.name)
        ad.action_slot = slot
    return action, stashed


def _write(ob, action, frames, series):
    """Write whole curves at once: keyframe_insert per bone per frame is slow on long clips.
    series: (data path, index, group, values[, their frames when not frames])."""
    ad = ob.animation_data
    if hasattr(action, "layers"):
        from bpy_extras import anim_utils
        bag = anim_utils.action_ensure_channelbag_for_slot(action, ad.action_slot)
        curves, groups = bag.fcurves, bag.groups
    else:  # Before slotted actions.
        curves, groups = action.fcurves, action.groups
    made = {}
    for path, index, group, values, *keys in series:
        curve = curves.new(path, index=index)
        if group not in made:
            made[group] = groups.get(group) or groups.new(group)
        curve.group = made[group]
        points = curve.keyframe_points
        points.add(len(values))
        flat = []
        for frame, value in zip(keys[0] if keys else frames, values):
            flat += (float(frame), float(value))
        points.foreach_set("co", flat)
        for point in points:
            point.interpolation = "LINEAR" if len(values) > 1 else "CONSTANT"
        curve.update()


REACH_MOVE = 0.08  # Metres at the character's size: a limb end the source moves less than this is not judged.
REACH_SHARE = 0.25  # A limb follows when its deforming bones swing at least this share of the source's swing.
LIMBS = (("left arm", "hand_l", "LeftHand"), ("right arm", "hand_r", "RightHand"),
         ("left leg", "foot_l", "LeftFoot"), ("right leg", "foot_r", "RightFoot"))


def _unreached(ob, rig, scene, frames, poses, k):
    """After the bake, on Blender's evaluation of up to 16 frames: the limbs whose deforming bones do not follow
    (mapped controls that drive nothing, such as FK bones on a limb left in IK mode), and how many were judged.
    Each limb end is the deforming bone nearest its mapped bone, measured from the pelvis, against the source's
    swing scaled to the character."""
    bones, tb = ob.data.bones, rig.bones
    deform = [b for b in bones if b.use_deform]
    if not deform:
        return [], 0  # Nothing deforms (props parented to bones): nothing to judge.

    def stand_in(name):
        head = bones[name].head_local
        return min(deform, key=lambda b: (b.head_local - head).length).name

    ends = {c: stand_in(tb[c]) for _limb, c, _joint in LIMBS}
    thighs = [stand_in(tb[f"thigh_{s}"]) for s in "lr"]
    picks = sorted({round(i * (len(frames) - 1) / 15) for i in range(16)})
    mw3 = ob.matrix_world.to_3x3()
    swings = {c: [] for c in ends}
    current, muted = scene.frame_current, _mute_deform(ob)
    try:
        for i in picks:
            scene.frame_set(frames[i])
            pbs = ob.pose.bones
            pelvis = (pbs[thighs[0]].head + pbs[thighs[1]].head) * 0.5
            for c, n in ends.items():
                swings[c].append(mw3 @ (pbs[n].head - pelvis))
    finally:
        for modifier in muted:
            modifier.show_viewport = True
        scene.frame_set(current)
    measured = []
    for limb, c, joint in LIMBS:
        src = [(poses[i][joint][1] - (poses[i]["LeftLeg"][1] + poses[i]["RightLeg"][1]) * 0.5) * k for i in picks]
        measured.append((limb, max((v - src[0]).length for v in src), max((v - swings[c][0]).length for v in swings[c])))
    return unfollowed(measured)


def unfollowed(swings):
    """swings: (limb, the source's swing, the deforming bones' swing), metres at the character's size -> the limbs
    that did not follow, and how many were judged (a limb the source hardly moves is not)."""
    judged = [(limb, want, got) for limb, want, got in swings if want >= REACH_MOVE]
    return [limb for limb, want, got in judged if got < REACH_SHARE * want], len(judged)


def _put_back(ob, action, previous, saved, switches):
    """Undo a refused bake: the previous action active again (its stash track gone), the pose and switches as
    they were, the new action deleted."""
    import bpy
    had, old, slot = previous
    ad = ob.animation_data
    if ad is not None:
        ad.action = None
        if old is not None:
            stash = next((t for t in reversed(ad.nla_tracks) if t.name.startswith("[Action Stash]")
                          and any(s.action == old for s in t.strips)), None)
            if stash is not None:
                ad.nla_tracks.remove(stash)
            ad.action = old
            if slot is not None:
                try:
                    ad.action_slot = slot
                except (AttributeError, TypeError, RuntimeError):
                    pass
        if not had:
            ob.animation_data_clear()
    bpy.data.actions.remove(action)
    for bone, prop, value in switches:
        ob.pose.bones[bone][prop] = value
    for n, (mode, matrix) in saved.items():
        pb = ob.pose.bones[n]
        pb.rotation_mode = mode
        pb.matrix_basis = matrix
    bpy.context.view_layer.update()


def _refusal(ob, rig, mapped, missed):
    """Why nothing was applied, and what the user can do about it."""
    text = (f"Nothing was applied to '{ob.name}': it was matched as a {rig.label} ({mapped} bones), but turning "
            f"those bones does not move the deforming bones of its {', '.join(missed)}, so the character would not "
            "move.")
    if ob.data.get("is_generated_cloudrig") or "cloudrig" in ob.keys():
        return text + (" It is a CloudRig rig (like Blender Studio's Rain and Snow), which motion retargeting does "
                       "not support yet: with its arms and legs switched to FK (the rig's IK/FK settings) the limbs "
                       "follow, but not the hips, spine or travel. Rigify, Auto-Rig Pro, Mixamo, Unreal-style and "
                       "plain deform-bone humanoids work.")
    return text + (" If its arms and legs are in IK mode, switch them to FK and apply the motion again; otherwise "
                   "this rig type is not supported yet. Rigify, Auto-Rig Pro, Mixamo, Unreal-style and plain "
                   "deform-bone humanoids work.")


# ----------------------------------------------------------------------- no target: SOMA's own body

# (weight bone, from joint, to joint, radius in metres at SOMA's size) for the generated mannequin.
def _body_parts():
    parts = [("Hips", "LeftLeg", "RightLeg", 0.10), ("Spine1", "Spine1", "Spine2", 0.11),
             ("Spine2", "Spine2", "Chest", 0.12), ("Chest", "Chest", "Neck1", 0.13), ("Neck1", "Neck1", "Head", 0.05),
             ("Head", "Head", "HeadEnd", 0.10)]
    for side in ("Left", "Right"):
        parts += [(f"{side}Shoulder", f"{side}Shoulder", f"{side}Arm", 0.05),
                  (f"{side}Arm", f"{side}Arm", f"{side}ForeArm", 0.055),
                  (f"{side}ForeArm", f"{side}ForeArm", f"{side}Hand", 0.045),
                  (f"{side}Hand", f"{side}Hand", f"{side}HandMiddle2", 0.04),
                  (f"{side}Leg", f"{side}Leg", f"{side}Shin", 0.075),
                  (f"{side}Shin", f"{side}Shin", f"{side}Foot", 0.055),
                  (f"{side}Foot", f"{side}Foot", f"{side}ToeBase", 0.045),
                  (f"{side}ToeBase", f"{side}ToeBase", f"{side}ToeEnd", 0.03)]
        for i, j in ((1, 2), (2, 3)):
            parts.append((f"{side}HandThumb{i}", f"{side}HandThumb{i}", f"{side}HandThumb{j}", 0.013))
        parts.append((f"{side}HandThumb3", f"{side}HandThumb3", f"{side}HandThumbEnd", 0.012))
        for finger in ("Index", "Middle", "Ring", "Pinky"):
            for i, j in ((2, 3), (3, 4), (4, "End")):
                parts.append((f"{side}Hand{finger}{i}", f"{side}Hand{finger}{i}", f"{side}Hand{finger}{j}", 0.011))
    return parts


def build_skeleton(source, scene, location, name="Motion skeleton"):
    """SOMA's rest skeleton as an armature with a simple capsule body (never a licensed character)."""
    import bmesh
    import bpy
    from mathutils import Matrix, Vector
    joints = [j for j in source.bvh.joints if j.parent is not None and not j.name.endswith("End")]
    forward = source._point(Vector((0.0, 0.0, 1.0))).normalized()  # The way SOMA's rest faces.
    data = bpy.data.armatures.new(name)
    ob = bpy.data.objects.new(name, data)
    scene.collection.objects.link(ob)
    ob.location = location
    rest = source.rest
    view_layer = bpy.context.view_layer
    if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    previous = view_layer.objects.active
    view_layer.objects.active = ob
    try:
        with bpy.context.temp_override(active_object=ob, object=ob, selected_objects=[ob]):
            bpy.ops.object.mode_set(mode="EDIT")
            edit = data.edit_bones
            for joint in joints:
                bone = edit.new(joint.name)
                bone.head = rest[joint.name]
                kids = [c for c in joint.children]
                prefer = [c for c in kids if c.name in ("Spine1", "Neck1") or c.name.endswith(("Middle1", "Middle2"))]
                tail = (prefer or kids)
                if tail:
                    bone.tail = rest[tail[0].name]
                elif joint.end is not None:
                    bone.tail = rest[joint.name] + source._point(Vector(joint.end))
                else:
                    # A leaf without an End Site (SOMA's Jaw and eyes). A new edit bone's tail is the armature's
                    # origin, which made each one a 1.6 m bone from the face to the floor: a short one looking ahead.
                    parent = edit.get(joint.parent.name)
                    bone.tail = bone.head + forward * max(0.02, 0.25 * parent.length if parent else 0.0)
                if (bone.tail - bone.head).length < 1e-3:
                    bone.tail = bone.head + Vector((0, 0, 0.02))
            for joint in joints:
                if joint.parent.name in edit:
                    edit[joint.name].parent = edit[joint.parent.name]
            bpy.ops.object.mode_set(mode="OBJECT")
    finally:
        view_layer.objects.active = previous
    mesh = bpy.data.meshes.new("Motion body")
    bm = bmesh.new()
    groups = []
    deform = bm.verts.layers.deform.verify()
    for bone, a, b, radius in _body_parts():
        if a not in rest or b not in rest or bone not in data.bones:
            continue
        pa, pb = rest[a], rest[b]
        axis = pb - pa
        half = axis.length / 2 + radius * 0.35
        if axis.length < 1e-6:
            continue
        rot = Vector((0, 0, 1)).rotation_difference(axis.normalized()).to_matrix().to_4x4()
        matrix = Matrix.Translation((pa + pb) / 2) @ rot @ Matrix.Diagonal((radius, radius, half, 1.0))
        made = bmesh.ops.create_uvsphere(bm, u_segments=12, v_segments=8, radius=1.0, matrix=matrix)
        if bone not in groups:
            groups.append(bone)
        index = groups.index(bone)
        for vert in made["verts"]:
            vert[deform][index] = 1.0
    bm.to_mesh(mesh)
    bm.free()
    for poly in mesh.polygons:
        poly.use_smooth = True
    body = bpy.data.objects.new("Motion body", mesh)
    scene.collection.objects.link(body)
    for bone in groups:
        body.vertex_groups.new(name=bone)
    body.parent = ob
    modifier = body.modifiers.new("Armature", "ARMATURE")
    modifier.object = ob
    return ob, body


# ----------------------------------------------------------------------------------------- apply

def read_meta(text):
    """motion.json's useful fields; anything malformed is ignored, never fatal."""
    import json
    try:
        meta = json.loads(text) if text else {}
    except (ValueError, RecursionError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    out = {}
    for key in ("source", "skeleton", "rest", "units", "up"):
        if isinstance(meta.get(key), str):
            out[key] = meta[key][:80]
    for key in ("fps", "frames", "seconds"):
        if type(meta.get(key)) in (int, float) and math.isfinite(meta[key]) and meta[key] > 0:
            out[key] = meta[key]
    if type(meta.get("fingers_animated")) is bool:
        out["fingers_animated"] = meta["fingers_animated"]
    warnings = meta.get("warnings")
    if isinstance(warnings, list):
        out["warnings"] = [" ".join(w.split())[:200] for w in warnings[:5] if isinstance(w, str) and w.strip()]
    return out


def apply(bvh_text, meta, *, target=None, title="motion", in_place=False, frame_start=None, scene=None):
    """Main thread. target: an armature object, or None for SOMA's own skeleton with a generated body.
    Returns the result line for the model."""
    import bpy
    scene = scene or bpy.context.scene
    bvh = parse_bvh(bvh_text)
    source = Source(bvh, 0.01 if meta.get("units", "cm") == "cm" else 1.0)
    missing = [j for j in ("Hips", "Head", "LeftArm", "RightArm", "LeftLeg", "RightLeg", "LeftToeBase",
                           "RightToeBase") if j not in source.rest]
    if missing:
        raise MotionError("motion.bvh is not a SOMA skeleton (missing " + ", ".join(missing) + ")")
    fingers = meta.get("fingers_animated", True)
    start = scene.frame_start if frame_start is None else int(frame_start)
    created = None
    if target is None:
        target, body = build_skeleton(source, scene, scene.cursor.location.copy())
        created = body
    rig = rig_of(target)
    if rig is None:
        raise MotionError(f"{target.name} does not look like a humanoid armature (hips, legs, arms and head)")
    empty = not any(o is not target and o.animation_data is not None and (o.animation_data.action is not None
                    or len(o.animation_data.nla_tracks)) for o in scene.objects)
    report = retarget(source, target, rig, scene=scene, frame_start=start, in_place=in_place, fingers=fingers,
                      name=action_name(title))
    first, last = report["frames"]
    if empty:
        scene.frame_start, scene.frame_end = first, last
    scene.frame_set(scene.frame_current)
    seconds = (report["count"] - 1) / report["fps"] if report["count"] > 1 else 0.0
    rate = f"{report['fps']:g} fps"
    if abs(report["fps"] - bvh.fps) > 1e-3:
        rate += f", resampled from {bvh.fps:g}"
    where = (f"on a new skeleton '{target.name}' with a simple body '{created.name}' (no armature was targeted)"
             if created else f"on '{target.name}' ({rig.label}, {report['mapped']} bones mapped)")
    notes = []
    if report["stashed"]:
        notes.append(f"its previous action '{report['stashed']}' is kept in a muted NLA track")
    if empty:
        notes.append(f"scene frame range set to {first}–{last}")
    n = report["kept"]
    kept = f"keys already on {n} finger bone{'s' if n != 1 else ''} were left as they are" if n else ""
    if report["relaxed"]:
        notes.append("fingers set to a relaxed pose; the model doesn't animate fingers"
                     + (f" ({kept})" if kept else ""))
    elif fingers is False:
        notes.append("fingers not animated by this model" + (f"; {kept}" if kept else ""))
    notes += meta.get("warnings", [])[:5]
    notes += report["warnings"][:5]
    line = (f"Motion '{report['action'].name}' {where}: frames {first}–{last} at {rate} ({seconds:.1f} s), "
            f"root motion {'in place' if in_place else 'in world'}")
    if notes:
        notes[0] = notes[0][:1].upper() + notes[0][1:]
    return line + (". " + "; ".join(notes) if notes else "")
