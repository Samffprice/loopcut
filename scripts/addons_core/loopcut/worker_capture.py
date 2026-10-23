"""A worker's looks, rendered in its background Blender, set up for its role (07_m4_design §2.7).

A child has no viewport to draw, so a look is a small real render, 1024 px on its long side, in one
of these shadings (SHADINGS, a role's own in ROLE_SHADING; capture_viewport's `shading` picks one
or two for any angle):
- clay (modeling): Workbench, one grey, of the worker's own deliverable, framed close, one large
  view and two smaller ones from the front and from behind (the parts a single angle hides);
- studio (materials): EEVEE in a neutral light calibrated so that a face toward the view shows about
  0.9 of its albedo (_studio); it replaced studio.exr fixed in world space, whose front half lit a
  painted cream label at 0.07-0.15 of its value and bluish, so workers saw slate and repainted for
  an hour (Arcane run W0025-W0044);
- albedo: each surface's Base Color, its value or whatever feeds it (a texture), as emission with
  no light: paint judged apart from lighting (_albedo);
- flat: Workbench, each material's Base Color value, unlit;
- solid (camera, animation): Workbench with the materials' viewport colours, thirds and the brief's
  region for the camera role, a sheet of frames all framed the same for animation;
- scene (lighting, and angle final for any role): the scene's own engine, world, lights and colour
  management, capped, through the brief's camera (else the scene's). These looks also say, in
  numbers, how bright the subject and the frame are (display values, the same measure as
  inspect_scene's), so "reads well" can be checked.
Every look is framed, measured and rendered in one view layer (_shot_layer): capture_viewport's
view_layer, else the one that renders (view_layers.production), alone, with its exclusions,
holdouts and overrides; its camera is capture_viewport's camera when one is named. That is the
production shot: a staged layer with its own world through a saved camera (Arcane run
20260930-154037, where workers could only judge paint in studio light), its caption naming the
layer, the world and the lights it used. Shadings other than scene clear the layer's world and
material overrides for the panel.
All but scene render with a transparent film over a neutral grey (BACKDROP). A blank panel, two
panels showing the same picture, and other objects hiding the subject (_hidden_by; isolate hides
them) are warned about. Every caption says how the image was made (provenance): engine, samples,
shading, view transform, size. Angle crops (_review) renders through the camera at its output size
and crops each object at one common scale, with its size on screen, cut edges and brightness.
Without a focus, a look frames what the worker is judging (its own objects, the brief's subjects),
never a whole set whose floor would shrink the product to a dot. Named angles use a temporary
camera placed with framing.fit. Modeling/material looks supply the wave card's preview too: until
there is geometry to frame they return no image, and empty meshes never enlarge the fitted bounds.
Everything temporary (cameras, the studio world and key light,
albedo's material copies) is tagged lc_temp and removed afterwards; every scene, render, object and
material setting a look changes, and the frame, is put back, so a look changes nothing a later
tool, check or the result can see (the originals of a material are never edited). With
labels, names are drawn onto the image afterwards, from inspection_worker.projections, with blf.
A look first repairs the worker's own meshes (repair_meshes): a face model code added without its
corners crashes the render's triangulation, and with it the child.
"""

import math
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path

import bpy

from . import framing, tools, view_layers
from .tools import ToolError, ToolResult

TEMP = "lc_temp"          # ownership.TEMP: excluded from tokens and results.
WIDTH = 1024              # A look's long side, px: enough to judge a bevel or a hinge, still a few seconds.
ASPECT = 0.75             # Named angles are 4:3; through a camera the shape follows its render size.
MARGIN = 0.08
MIN_CLIP = 1e-6           # Camera RNA's minimum clipping distance (rna_camera.cc).
MAX_WORLD_BOUND = 1e12    # Leave headroom for camera fitting and float32 render matrix products.
LABEL_LIMIT = tools.LABEL_LIMIT
LABELLED_TILE = 480       # Names are drawn on tiles at least this wide; smaller ones would be covered.
ANGLES = ("three_quarter", "front", "side", "back", "top", "camera", "sheet", "final", "crops")
PANEL_ANGLES = ("three_quarter", "front", "side", "back", "top", "camera")   # capture_viewport's angles.
SHEET_ANGLES = ("three_quarter", "front", "side", "top")
DETAIL_ANGLES = ("three_quarter", "front", "back")   # Modeling's look: a large view and two small ones.
VIEWS = {**framing.NAMED, "back": (62.0, 0.0, 218.0)}  # "back": three-quarter from the opposite side.
MAX_FRAMES = 6
CELL = 384                # A wave's contact sheet: one cell per worker, px on its long side (cell()).
REFERENCE_SIDE = 320      # The brief's reference beside a look, px on its long side: one small tile to compare
                          # with, not a second image (the worker has it at 768 px in its brief).
REFERENCE_GAP = 4
OWN_FOCUS = ("modeling", "materials")   # Roles whose looks frame their own work unless told otherwise, final too.
MAX_CAMERAS = 4
MAX_PANELS = 6            # A multiview sheet: angles x shadings.
CAPPED_SAMPLES, CAPPED_SECONDS = 16, 20
DARK = 0.02               # A pixel whose brightest channel is below this reads as black.
SHADINGS = ("clay", "flat", "studio", "albedo", "scene")
ROLE_SHADING = {"modeling": "clay", "materials": "studio", "lighting": "scene", "camera": "solid",
                "animation": "solid"}
LIGHTING = {  # What each shading lights with, for the caption.
    "clay": "one grey, studio light, cavity, outlines",
    "flat": "each material's Base Color value, unlit (a textured one shows its viewport colour)",
    "solid": "materials' viewport colours (not their textures), studio light",
    "studio": "a white world and a key light from the view's upper left; the scene's lights and world off",
    "albedo": "each surface's Base Color (value or texture) as emission, no light",
}
# Measured in EEVEE and Cycles: a white diffuse face gets w from a white world of strength w, and s / pi
# from a sun of strength s at normal incidence. So a face toward the key shows its albedo, one toward the
# view (the key 40 degrees off it) 0.9 of it, one turned away 0.55.
STUDIO_AMBIENT, STUDIO_KEY = 0.55, 0.45
KEY_FROM = (-0.35, 0.55, 0.76)   # Where the key comes from, in the view's camera space: upper left, near the axis.
BACKDROP = (118, 118, 118)       # Behind all but scene looks: 18 % grey, neutral for judging colour.
BLANK = 0.003                    # A panel where anything covers less than this share of it is blank.
HIDDEN, HIDDEN_RAYS = 0.25, 256  # Warn when other objects hide this share of the subject (_hidden_by).
ALIKE = 2.5                      # Mean difference (0-255) under which two panels are the same picture.
SIGNATURE = (16, 12)             # Panels are compared at this size.
REVIEW_MAX = 1920                # crops: the output size, capped on its long side.
MAX_CROPS = 8
ID_LEVELS = (0.0, 0.5, 1.0)      # crops' object mask: one flat Workbench colour per object from these.
REPAIR_NOTE = ("Loopcut repaired the mesh of {names}: faces with fewer than three corners or out-of-range indices, "
               "which crash Blender's renderer, were fixed or removed (Mesh.validate()). Build faces with "
               "from_pydata or bmesh, never polygons.add without their loops.")
_NOT_SHOWN = {"CAMERA", "LIGHT", "EMPTY", "SPEAKER", "LIGHT_PROBE", "LATTICE", "ARMATURE"}
_SUBJECT_FIELDS = ("name", "names", "a", "b", "subject", "objects", "target")
# What each look is for and what is not this worker's call: in the text and on the image.
PURPOSE = {
    "modeling": ("clay", "For shape, proportion, detail, placement and contact. Materials and lighting are not "
                         "judged here; angle final shows them as the user will see them."),
    "materials": ("look-dev", "For materials only: a face toward the view shows about 0.9 of its colour, so what "
                              "you see is what you painted. The scene's lights, world and camera are not your "
                              "call; angle final shows your materials in the scene's own light."),
    "lighting": ("lighting", "For lighting. Exposure and colour management are the orchestrator's: file a change "
                             "request."),
    "camera": ("framing", "Rule-of-thirds lines{region}, for framing. Models, materials and lights are not your "
                          "call; angle final shows the lit shot."),
    "animation": ("motion", "Frames {frames}, every frame framed the same, for motion and timing. Models, materials "
                            "and lighting are not your call."),
    "final": ("final look", "What the user will see. No names are drawn."),
}
SHADING_PURPOSE = {  # An asked-for shading's caption, instead of the role's.
    "clay": "Shape only.",
    "flat": "Which material is where, in its flat colour.",
    "studio": "Materials in a neutral light: a face toward the view shows about 0.9 of its colour.",
    "albedo": "Paint without light: each surface's colour exactly as painted, not how it will look lit.",
    "scene": "The scene's own light: what the user will see.",
}

_role = {"role": "modeling", "folder": None, "width": WIDTH, "region": None, "count": 0,
         "deliverable": "", "prefix": "", "owned": set(), "subjects": [], "cameras": [], "reference": None}
_shot = {"layer": "", "mixed": False}   # The view layer a look renders alone (_shot_layer), or "".


def configure(role: str, folder: Path, width: int = WIDTH, region=None, brief: dict | None = None) -> None:
    """Set once, from the child's init: the role's setup, where captures go, the long side in px,
    the brief's region ([x0, y0, x1, y1] of the frame, 0..1 from bottom-left) for the camera role,
    and what of the brief a look needs: deliverable, prefix, owned keys, and the checks, whose
    object names are the subjects a look frames and measures and whose cameras it looks through, and
    its reference ({path, name}: the user's image every look shows beside the render, waves.reference_tile)."""
    if role not in PURPOSE or role == "final":
        raise ToolError(f"role must be one of {', '.join(r for r in PURPOSE if r != 'final')}")
    brief = brief or {}
    checks = [c for c in brief.get("checks") or () if isinstance(c, dict)]
    _role.update(role=role, folder=Path(folder), width=int(width), region=region, count=0,
                 deliverable=str(brief.get("deliverable") or ""), prefix=str(brief.get("prefix") or ""),
                 owned=set(brief.get("owned") or ()), subjects=subject_names(checks),
                 cameras=[c["camera"] for c in checks if isinstance(c.get("camera"), str)],
                 reference=_reference_of(brief.get("reference")))
    Path(folder).mkdir(parents=True, exist_ok=True)


def _reference_of(value) -> dict | None:
    if isinstance(value, dict) and isinstance(value.get("path"), str) and Path(value["path"]).is_file():
        return {"path": Path(value["path"]), "name": str(value.get("name") or Path(value["path"]).name)}
    return None


def reference_size(size, long_side: int = REFERENCE_SIDE, max_height: int | None = None) -> tuple[int, int]:
    """(width, height) of the reference tile: the image's shape, long_side px on its long side, and no
    taller than max_height (the look's own height)."""
    width, height = max(1, int(size[0])), max(1, int(size[1]))
    scale = long_side / max(width, height)
    if max_height:
        scale = min(scale, max_height / height)
    return max(1, round(width * scale)), max(1, round(height * scale))


def _reference_pixels(path: Path, long_side: int = REFERENCE_SIDE, max_height: int | None = None):
    """The reference as RGB uint8, rows bottom-up as imbuf keeps them, at reference_size. None when it
    cannot be read: a look never fails over it."""
    import imbuf
    import numpy as np
    try:
        loaded = imbuf.load(str(path))
    except Exception as ex:
        print(f"Loopcut: could not read the reference {path}: {ex}")
        return None
    try:
        loaded.resize(reference_size(tuple(loaded.size), long_side, max_height), method="BILINEAR")
        with loaded.with_buffer() as buffer:
            pixels = np.array(buffer, copy=True)
    finally:
        loaded.free()
    if pixels.dtype != np.uint8:  # A float buffer (a 16-bit or HDR file): display values 0..1.
        pixels = np.round(np.clip(pixels, 0.0, 1.0) * 255).astype(np.uint8)
    return _over_backdrop(pixels)[0]


def reference_cell(path: Path, size: tuple[int, int], out: Path) -> Path | None:
    """The user's reference as one cell of a wave's contact sheet (waves.contact_sheet): fitted into
    size (width, height, landscape) on the sheet's grey, labelled reference. None when it cannot be read."""
    import blf
    import imbuf
    import numpy as np
    pixels = _reference_pixels(path, size[0], size[1])
    if pixels is None:
        return None
    cell = imbuf.new(size)
    try:
        h, w = pixels.shape[:2]
        x, y = (size[0] - w) // 2, (size[1] - h) // 2
        with cell.with_buffer(write=True) as buffer:
            np.asarray(buffer)[:] = (38, 38, 38, 255)  # asset_common.sheet's background, 0.15.
            np.asarray(buffer)[y:y + h, x:x + w, :3] = pixels
        with blf.bind_imbuf(0, cell):
            blf.size(0, tools.LABEL_SIZE)
            blf.color(0, 1, 1, 1, 1)
            _tag(cell, "reference", 4, size[1] - 20, size[0])
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        cell.file_type = "PNG"
        imbuf.write(cell, filepath=str(out))
    finally:
        cell.free()
    return Path(out)


def configured() -> bool:
    """Whether configure() has run: this Blender is a worker's child, and its looks have a folder."""
    return _role["folder"] is not None


def subject_names(checks: list) -> list[str]:
    """The object names a brief's checks are about, in order, without repeats."""
    names = []
    for check in checks:
        for key in _SUBJECT_FIELDS:
            value = check.get(key)
            for name in [value] if isinstance(value, str) else value if isinstance(value, list) else ():
                if isinstance(name, str) and name and name not in names:
                    names.append(name)
    return names


def warm() -> float:
    """One tiny render with the role's setup, so the first real look does not pay for starting the
    render engine and compiling EEVEE's shaders. Seconds taken."""
    started = time.monotonic()
    folder = _role["folder"] / "warm"
    try:
        shot = _look(None, None, None, long_side=64, folder=folder, labels=False)
        shot.image_path.unlink(missing_ok=True)
    except ToolError:
        pass  # Nothing to frame yet: the first real look warms up instead.
    return round(time.monotonic() - started, 3)


def look(angle: str | None = None, focus: list[str] | None = None, frames: list[int] | None = None,
         step: int | None = None, angles: list[str] | None = None, shading=None,
         isolate: bool = False, camera: str | None = None, view_layer: str | None = None,
         labels: bool = False) -> ToolResult:
    """angle None is the role's own default (three_quarter means the same, since it is the schema's
    default): modeling's close views of its deliverable, lighting's and camera's view through the
    camera, and so on (plan). angles: one labelled sheet of these views instead (PANEL_ANGLES);
    shading: one of SHADINGS or a list, every view in each (default the role's own; angle final
    means scene); isolate: everything but what the look frames is hidden from its render. Angle
    crops is the camera-scale review (_review), in shading scene unless one is asked for.
    camera and view_layer make it the production shot (Arcane run 20260930-154037: workers judged
    paint in studio light and generic angles while the shot was a staged layer through saved
    cameras): camera is what every view through a camera looks through, and with no angle the
    look is final (animation: its frames through it); view_layer is the layer rendered and
    measured, its exclusions, holdouts and overrides (default view_layers.production). labels:
    object names drawn on (never on final); off by default, since they covered the subject."""
    if angle is not None and angle not in ANGLES:
        raise ToolError(f"angle must be one of {', '.join(ANGLES)}")
    if not isinstance(labels, bool):
        raise ToolError("labels must be true or false")
    views, shadings = angles_of(angles), shadings_of(shading)
    chosen = _named_camera(camera) if camera else None
    view_layer = str(view_layer or "")
    view_layers.production(bpy.context.scene, view_layer)   # A name the scene lacks fails before counting.
    if chosen is not None and angle in (None, "three_quarter"):   # The schema's default: the automatic looks send it.
        angle = {"animation": "camera", "lighting": None, "camera": None}.get(_role["role"], "final")
    _role["count"] += 1
    if angle == "crops":
        return _review(focus, shadings[0] if shadings else "scene", bool(isolate), step, chosen, view_layer)
    return _look(angle, focus, frames, labels=labels, step=step, views=views, shadings=shadings,
                 isolate=bool(isolate), reference=_role["reference"], camera=chosen, view_layer=view_layer)


def _named_camera(name: str):
    camera = bpy.data.objects.get(str(name))
    if camera is None or camera.type != "CAMERA":
        names = [o.name for o in bpy.context.scene.objects if o.type == "CAMERA" and not o.get(TEMP)]
        raise ToolError(f"No camera named {name!r}; the scene's cameras: {', '.join(names) or 'none'}.")
    return camera


def cell(tag: str, folder: Path) -> Path:
    """The worker's deliverable for its wave's contact sheet (waves.contact_sheet): its final look,
    framed on its own work (own_focus) or through the camera for a lighting or camera worker, CELL px,
    its id drawn in the corner; in a studio light when the scene has none of its own (unlit), so the
    cell is not black. Raises ToolError when there is nothing to show."""
    return _look("final", None, None, long_side=CELL, folder=folder, labels=False, tag=tag, studio=True).image_path


def unlit(scene, layer=None) -> bool:
    """Whether the scene's own light would leave a render dark: an engine that renders lights (not
    Workbench) and no light object that renders (hidden from render or at zero power does not count;
    with a view layer, nor one it excludes: view_layers.lights)."""
    if scene.render.engine == "BLENDER_WORKBENCH":
        return False
    if layer is not None:
        return not view_layers.lights(scene, layer)
    return not any(o.type == "LIGHT" and not o.hide_render and getattr(o.data, "energy", 0) > 0 for o in scene.objects)


# ------------------------------------------------------------------ what a look shows (pure)

def angles_of(angles) -> tuple:
    """capture_viewport's angles: validated, in order, without repeats."""
    names = [angles] if isinstance(angles, str) else list(angles or ())
    bad = [a for a in names if a not in PANEL_ANGLES]
    if bad:
        raise ToolError(f"angles must be among {', '.join(PANEL_ANGLES)}")
    return tuple(dict.fromkeys(names))


def shadings_of(shading) -> tuple:
    """capture_viewport's shading: one name or a list, validated, in order, without repeats."""
    names = [shading] if isinstance(shading, str) else list(shading or ())
    bad = [s for s in names if s not in SHADINGS]
    if bad:
        raise ToolError(f"shading must be among {', '.join(SHADINGS)}")
    return tuple(dict.fromkeys(names))


def shading_for(role: str, setup: str, asked: str | None = None) -> str:
    """A panel's shading: the one asked for, else scene for a final look, else the role's own."""
    return asked or ("scene" if setup == "final" else ROLE_SHADING[role])


def panel_warnings(panels: list[dict]) -> list[str]:
    """What is wrong with a look's panels, said instead of left to be noticed: a blank one (the subject
    covers under BLANK of it, or the picture is one flat colour) and one showing the same picture as
    an earlier one (their SIGNATURE-sized copies differ by under ALIKE on average: a symmetric object
    from front and back, a view hidden behind the same occluder), and one where other objects hide
    HIDDEN or more of the subject. panels: {"label", "coverage" (None for an opaque film), "uniform",
    "signature" (numbers 0-255), "hidden" (share, names)}."""
    def name(i):
        return panels[i]["label"] or (f"panel {i + 1}" if len(panels) > 1 else "the image")
    warnings = []
    for i, panel in enumerate(panels):
        coverage = panel.get("coverage")
        if panel.get("uniform") or coverage is not None and coverage < BLANK:
            warnings.append(f"{name(i)} is blank: nothing of the subject is in view.")
        share, blockers = panel.get("hidden") or (0.0, [])
        if share >= HIDDEN and blockers:
            warnings.append(f"In {name(i)}, {', '.join(blockers)} hide{'s' if len(blockers) == 1 else ''} "
                            f"{share * 100:.0f} % of the subject: isolate shows it alone.")
    for j in range(1, len(panels)):
        b = panels[j].get("signature") or []
        for i in range(j):
            a = panels[i].get("signature") or []
            if a and len(a) == len(b) and sum(abs(x - y) for x, y in zip(a, b)) / len(a) < ALIKE:
                warnings.append(f"{name(j)} shows the same picture as {name(i)}.")
                break
    return warnings


def own_focus(role: str, angle: str | None, focus) -> bool:
    """Whether a look with no focus frames the worker's own work: modeling and materials, every angle
    but an explicit camera, the final look too. A final look without it went through the pack's camera
    and showed the deliverable as a speck (m4_effort_replay_analysis §5.5)."""
    return not focus and role in OWN_FOCUS and angle != "camera"


def plan(role: str, angle: str | None, has_camera: bool, has_focus: bool, own_camera: bool = False) -> tuple:
    """(setup, layout, views) for a look: setup "role" or "final"; layout "single", "grid" or
    "detail"; views are VIEWS names, "camera" (through the look's camera), "cameras" (one tile per
    camera of the camera worker's) or "frames:<view>" (animation: one tile per frame, through the
    camera or all framed the same from a named angle). A focus that names objects to frame wins
    over looking through the camera, except for an explicit angle camera. own_camera: the worker
    made or owns a camera (an animation worker moving one looks through it)."""
    if angle == "final":
        return "final", "single", ("camera",) if has_camera and not has_focus else ("three_quarter",)
    if role == "animation":
        through = has_camera and (angle in ("camera", "sheet") or angle in (None, "three_quarter") and own_camera
                                  and not has_focus)
        return "role", "grid", ("frames:camera" if through else f"frames:{angle if angle in VIEWS else 'three_quarter'}",)
    if angle == "camera":
        return "role", "single", ("camera",)
    if angle == "sheet":
        return "role", "grid", ("cameras",) if role == "camera" else SHEET_ANGLES
    if angle in (None, "three_quarter"):
        if role in ("lighting", "camera") and has_camera and not has_focus:
            return "role", "single", ("camera",)
        if role == "modeling":
            return "role", "detail", DETAIL_ANGLES
        return "role", "single", ("three_quarter",)
    return "role", "single", (angle,)


def layout(kind: str, count: int, long_side: int, aspect: float) -> tuple[tuple[int, int], list[tuple]]:
    """((width, height), [(x, y, w, h), ...]) with y from the top: one image of count tiles whose
    long side is long_side. aspect is height / width of one tile ("detail" makes its own squares)."""
    long_side = max(16, int(long_side))
    if kind == "detail":
        hero = round(long_side * 0.656)
        side, half = long_side - hero, hero // 2
        return (long_side, hero), [(0, 0, hero, hero), (hero, 0, side, half), (hero, half, side, hero - half)]
    columns = 1 if count <= 1 or kind == "single" else 2 if count <= 4 else 3
    rows = math.ceil(max(1, count) / columns)
    tile = long_side // columns
    w, h = (tile, max(16, round(tile * aspect))) if aspect <= 1 else (max(16, round(tile / aspect)), tile)
    rects = [((i % columns) * w, (i // columns) * h, w, h) for i in range(max(1, count))]
    return (w * columns, h * rows), rects


def backdrops(boxes: dict) -> set:
    """Names of what only surrounds the subject: a floor, wall or backdrop plane (flat: its thinnest
    side under a tenth of its longest) at least 3 times the size of the largest solid object, or a
    room shell or cove whose box holds every other object's whole box and is 3 times their size
    (a plinth under a product does not hold it: the product stands above its top). boxes: name ->
    (min xyz, max xyz). Never all of them."""
    def dims(box):
        return [box[1][i] - box[0][i] for i in range(3)]

    def holds(outer, inner, tolerance=1e-3):
        return all(outer[0][i] - tolerance <= inner[0][i] and inner[1][i] <= outer[1][i] + tolerance for i in range(3))
    flat = {n for n, b in boxes.items() if min(dims(b)) < 0.1 * max(dims(b))}
    solid = [max(dims(b)) for n, b in boxes.items() if n not in flat]
    largest = max(solid) if solid else None
    found = {n for n in flat if largest and max(dims(boxes[n])) >= 3 * largest}
    for name, box in boxes.items():
        others = [b for n, b in boxes.items() if n != name and n not in found]
        if name in found or not others:
            continue
        if all(holds(box, b) for b in others) and max(dims(box)) >= 3 * max(max(dims(b)) for b in others):
            found.add(name)
    return found if len(found) < len(boxes) else set()


def signal_text(numbers: dict) -> str:
    """The exposure numbers of a look, in one line."""
    parts = []
    if numbers.get("subject") is not None:
        parts.append(f"subject {numbers['subject']:.2f}")
    parts.append(f"frame {numbers['mean']:.2f}")
    if numbers.get("background") is not None:
        parts.append(f"around the subject {numbers['background']:.2f}")
    parts.append(f"clipped {numbers['clipped'] * 100:.1f} %")
    parts.append(f"near black {numbers['dark'] * 100:.0f} %")
    if numbers.get("hue") is not None:
        parts.append(f"tint hue {numbers['hue']} deg, saturation {numbers['saturation']:.2f}")
    return "Exposure (mean display brightness 0-1): " + ", ".join(parts) + "."


# ------------------------------------------------------------------ one look

@dataclass
class _Tile:
    label: str
    view: str                    # A VIEWS name, or "camera".
    frame: int | None = None
    camera: object = None        # The camera looked through, for view "camera".
    rect: tuple = (0, 0, 0, 0)   # x, y from the top, w, h.
    path: Path | None = None
    numbers: dict = field(default_factory=dict)
    shading: str = ""            # A SHADINGS name, or "solid".
    provenance: str = ""         # How it was rendered, read from the settings it rendered with.
    place: str = ""              # Where its camera stood (_place), read while the camera exists.
    stamp: str = ""              # A short provenance drawn in its corner (a contact sheet cell's).
    coverage: float | None = None   # Share of the panel anything covers (transparent film only).
    uniform: bool = False        # One flat colour: nothing rendered.
    signature: list = field(default_factory=list)
    hidden: tuple = (0.0, [])    # _hidden_by: the share of the subject others hide, and which.


def _look(angle, focus, frames, long_side=None, folder=None, labels=True, step=None, tag="",
          studio=False, views=(), shadings=(), isolate=False, reference=None, camera=None,
          view_layer: str = "") -> ToolResult:
    """tag: a label drawn on a single look's corner (the worker's id on a wave's contact sheet).
    studio: a final look of an unlit scene gets the studio light (a contact sheet cell). views,
    shadings, isolate, camera: look's angles, shading, isolate and camera. reference ({path,
    name}): the user's image, REFERENCE_SIDE px, to the right of the render and labelled reference.
    Framed, measured and rendered in one view layer (_shot_layer)."""
    scene = bpy.context.scene
    with _Restore() as setup, _shot_layer(scene, view_layer, setup):
        return _look_in(angle, focus, frames, long_side, folder, labels, step, tag, studio, views, shadings,
                        isolate, reference, camera)


@contextmanager
def _shot_layer(scene, asked: str, restore):
    """A look's view layer (view_layers.production: the asked one, else the active one when it
    renders, else the first that renders), the context's while the look runs, so what it frames,
    measures and labels is what that layer evaluates; the render renders it alone (_render), its
    use on for the look. A shot that is the compositor's mix of several layers (none asked, and a
    compositor that does something: _compositor) renders them all. Yields the layer."""
    layer = view_layers.production(scene, asked)
    mixed = view_layers.composited(scene, bool(asked)) and _compositor(scene)
    if not mixed and not layer.use:
        restore.set(layer, "use", True)   # Only a layer that renders renders, named or not.
    _shot.update(layer="" if mixed else layer.name, mixed=mixed)
    try:
        with view_layers.evaluating(layer):
            yield layer
    finally:
        _shot.update(layer="", mixed=False)


def _look_in(angle, focus, frames, long_side, folder, labels, step, tag, studio, views, shadings, isolate,
             reference, chosen) -> ToolResult:
    role = _role["role"]
    long_side = long_side or _role["width"]
    folder = Path(folder or _role["folder"])
    folder.mkdir(parents=True, exist_ok=True)
    scene = bpy.context.scene
    started = time.monotonic()
    own = own_objects(scene)
    repaired = repair_meshes(own)
    shown = _focus_objects(focus)
    mine_only = own_focus(role, angle, focus)
    targets = shown or _default_targets(scene, role, own)
    if not targets and role in OWN_FOCUS:
        # These captures also supply the wave card's preview (Worker.after_step). Before the
        # helper builds anything, do not substitute the private scene or its saved camera.
        raise ToolError("Nothing built yet: no visible helper-owned geometry to frame.")
    bounds = _corners(targets) if role in OWN_FOCUS else None
    camera = chosen or _camera_for(scene, focus, own)
    own_camera = any(o.type == "CAMERA" for o in own)
    # A camera asked for is looked through whatever the focus: the focus is then what is measured.
    setup, kind, planned = plan(role, angle, camera is not None, (bool(shown) or mine_only) and chosen is None,
                                own_camera or chosen is not None)
    if views:
        kind, planned = "grid", views
    tiles = _tiles(scene, planned, camera, frames, own)
    count = len(tiles)
    tiles, dropped = _panels(tiles, [shading_for(role, setup, s) for s in shadings or (None,)])
    if len(tiles) != count and kind == "detail":
        kind = "grid"   # The large view and two small ones hold three panels, not six.
    if tag and len(tiles) == 1:
        tiles[0].label = tag
    through = any(t.view == "camera" for t in tiles)
    if not through and not targets:
        raise ToolError("Nothing visible to frame.")
    aspect = _camera_aspect(scene) if through else ASPECT
    canvas, rects = layout(kind, len(tiles), long_side, aspect)
    for tile, rect in zip(tiles, rects):
        tile.rect = rect
    beside = _reference_pixels(reference["path"], REFERENCE_SIDE, canvas[1]) if reference else None
    if beside is not None:
        canvas = (canvas[0] + REFERENCE_GAP + beside.shape[1], canvas[1])
    name = f"{step if step is not None else _role['count']:03d}-{role}-{angle or 'default'}"
    lit = any(t.shading == "scene" for t in tiles)
    subject = (shown or (targets if mine_only else _subjects(scene)) or targets) if lit else []
    hidden = {}   # By view and frame: the shadings of one view share it.
    with _Restore(scene) as restore:
        compositor = _output(scene, restore)
        if isolate:
            _isolate(scene, targets, restore)
        fixed = _points_over(scene, targets, [t.frame for t in tiles]) if role == "animation" and not through else None
        for index, tile in enumerate(tiles):
            if tile.frame is not None:
                scene.frame_set(tile.frame)
            size = tile.rect[2:]
            if tile.view != "camera":
                tile.camera = _fit_camera(scene, tile.view, fixed or bounds or _corners(targets), size, restore)
            if views or len(shadings) > 1:
                tile.place = _place(tile, targets)
            if tile.view != "camera" and targets and not isolate:
                if (tile.view, tile.frame) not in hidden:
                    hidden[tile.view, tile.frame] = _hidden_by(scene, tile.camera, targets)
                tile.hidden = hidden[tile.view, tile.frame]
            tile.path = folder / f"{name}-{index}.png"
            with _Restore() as panel:   # Each panel's shading, put back before the next.
                key, used = _shade(scene, tile.shading, panel, studio, compositor)
                if key is not None:
                    _aim_key(key, tile.camera)
                note = "; the scene has no light of its own" if used != tile.shading else ""
                tile.provenance = _provenance(scene, used, size, note)
                if tag:
                    tile.stamp = _stamp(scene, used)
                _render(scene, tile.camera, size, tile.path)
            if used == "scene":
                tile.numbers = _exposure(scene, tile, subject)
        image = folder / f"{name}.png"
        _compose(scene, tiles, canvas, targets, image, labels and setup != "final", role, beside)
        for tile in tiles:
            tile.path.unlink(missing_ok=True)
    seconds = time.monotonic() - started
    own_ids = {o.session_uid for o in own}
    mine = bool(targets) and all(o.session_uid in own_ids for o in targets)
    text = _text(scene, role, setup, kind, tiles, targets, shown, focus, mine, canvas, seconds,
                 shadings=shadings, multiview=bool(views) or len(shadings) > 1, dropped=dropped, isolate=isolate)
    text += _left_out(own)
    if beside is not None:
        text += (f"\nRight, labelled reference: the user's {reference['name']} ({beside.shape[1]}x{beside.shape[0]} px), "
                 f"to compare your render with.")
    return ToolResult(f"{repair_note(repaired)}\n\n{text}" if repaired else text, image_path=image)


def _panels(tiles, shadings) -> tuple[list, int]:
    """Every tile in every shading, the shadings of one view side by side, at most MAX_PANELS, each
    labelled with its view and shading when there is more than one shading. (panels, how many were
    left out)."""
    panels = []
    for tile in tiles:
        for shading in shadings:
            label = tile.label if len(shadings) == 1 else f"{tile.label or tile.view.replace('_', ' ')} - {shading}"
            panels.append(replace(tile, shading=shading, label=label))
    return panels[:MAX_PANELS], max(0, len(panels) - MAX_PANELS)


def _text(scene, role, setup, kind, tiles, targets, shown, focus, mine, canvas, seconds, shadings=(),
          multiview=False, dropped=0, isolate=False) -> str:
    if setup == "final":
        what, purpose = PURPOSE["final"]
    elif shadings:
        what, purpose = ", ".join(shadings), " ".join(SHADING_PURPOSE[s] for s in shadings)
    else:
        what, purpose = PURPOSE[role]
    purpose = purpose.format(region=" and the brief's region" if _role["region"] else "",
                             frames=", ".join(str(t.frame) for t in tiles if t.frame is not None))
    renders = list(dict.fromkeys(t.provenance for t in tiles if t.provenance))
    shown_as = _describe(kind, tiles, targets, shown, focus, mine) + (", the rest hidden (isolate)" if isolate else "")
    text = (f"Image attached: {what}, {shown_as}, {canvas[0]}x{canvas[1]} px, {seconds:.1f} s. "
            f"Render: {'; '.join(renders)}. {purpose}")
    if dropped:
        text += f" {dropped} more panel{'s' if dropped > 1 else ''} left out: at most {MAX_PANELS}."
    measured = [t for t in tiles if t.numbers]
    for tile in measured[:MAX_CAMERAS]:
        label = "" if len(measured) == 1 else f"{tile.camera.name if tile.view == 'camera' else tile.label}: "
        text += f"\n{label}{signal_text(tile.numbers)}"
    if multiview:
        text += "\nPanels: " + "; ".join(f"{i} {t.label or t.view.replace('_', ' ')}: {t.place}"
                                         for i, t in enumerate(tiles, 1)) + "."
    warnings = panel_warnings([{"label": t.label, "coverage": t.coverage, "uniform": t.uniform,
                                "signature": t.signature, "hidden": t.hidden} for t in tiles])
    if warnings:
        text += "\nWarning: " + " ".join(warnings)
    if role == "materials" and setup != "final" and any(t.shading in ("studio", "albedo", "flat") for t in tiles):
        text += _material_values(targets)
    return text


def _place(tile: _Tile, targets) -> str:
    """A panel's camera: where it stands (or which camera), its lens and how far it is from the
    subject's centre."""
    camera = tile.camera
    where = f"through {camera.name}" if tile.view == "camera" else \
        "camera at ({:.2f}, {:.2f}, {:.2f})".format(*camera.matrix_world.translation)
    where += f", {camera.data.lens:.0f} mm"
    if targets and tile.view != "camera":
        points = _corners(targets)
        centre = [sum(p[i] for p in points) / len(points) for i in range(3)]
        where += f", {math.dist(camera.matrix_world.translation, centre):.2f} m from the subject"
    return where


def _engine_name(engine: str) -> str:
    return {"BLENDER_EEVEE": "EEVEE", "CYCLES": "Cycles", "BLENDER_WORKBENCH": "Workbench"}.get(engine, engine)


def _provenance(scene, shading: str, size, note: str = "") -> str:
    """How a panel was made, read from the settings it renders with (called inside its setup): engine
    and samples, shading and what lights it, view transform, look and exposure, size in px."""
    render, view = scene.render, scene.view_settings
    if render.engine == "CYCLES":
        quality = f"{scene.cycles.samples} samples" + (" denoised" if scene.cycles.use_denoising else "")
    elif render.engine == "BLENDER_EEVEE":
        quality = f"{scene.eevee.taa_render_samples} samples"
    else:
        quality = f"AA {scene.display.render_aa}"
    if shading == "scene":
        lit = _scene_light(scene)
    else:
        lit = LIGHTING[shading]
    colour = view.view_transform + (f", look {view.look}" if view.look not in ("", "None") else "") + \
        (f", exposure {view.exposure:+.1f}" if abs(view.exposure) > 1e-3 else "")
    composited = ", its compositor" if render.use_compositing else ""
    return (f"{_engine_name(render.engine)} {quality}, {shading} ({lit}{note}), {colour} view{composited}, "
            f"{size[0]}x{size[1]} px")


def _scene_light(scene) -> str:
    """What a scene-shaded panel renders with, by name: its view layer when the scene has more than
    one or this one excludes, holds out or overrides anything (view_layers.summary), the world it
    uses (the layer's override or the scene's) and its lights (view_layers.lights)."""
    layer = bpy.context.view_layer
    world = view_layers.world(scene, layer)
    names = [o.name for o in view_layers.lights(scene, layer)]
    lit = (f"world {world.name if world else 'none'}{' (the layer override)' if getattr(layer, 'world_override', None) else ''}"
           f" and {len(names)} light{'s' if len(names) != 1 else ''}{': ' + view_layers.listed(names) if names else ''}")
    if _shot["mixed"]:
        return f"the compositor's mix of view layers {', '.join(l.name for l in view_layers.rendering(scene))}; {lit}"
    words = view_layers.summary(scene, layer)
    return f"{words}, {lit}" if len(scene.view_layers) > 1 or words != f"view layer {layer.name}" else lit


def _stamp(scene, shading: str) -> str:
    """_provenance in a few words, drawn on a contact sheet cell: the orchestrator sees many cells
    under one line, so each says how it was rendered."""
    render = scene.render
    samples = scene.cycles.samples if render.engine == "CYCLES" else \
        scene.eevee.taa_render_samples if render.engine == "BLENDER_EEVEE" else None
    return (f"{_engine_name(render.engine)}{f' {samples} spp' if samples else ''}, "
            f"{'scene' if shading == 'scene' else shading} light, {scene.view_settings.view_transform}")


def _describe(kind, tiles, targets, shown, focus, mine) -> str:
    frames = [t.frame for t in tiles if t.frame is not None]
    frames = f", frames {', '.join(map(str, frames))}" if frames else ""
    cameras = list(dict.fromkeys(t.camera.name for t in tiles if t.view == "camera"))
    if cameras:
        return f"through {', '.join(cameras)}{frames}"
    names = list(dict.fromkeys(t.view.replace("_", " ") for t in tiles))
    views = ", ".join(names) + (" view" if len(names) == 1 else " views")
    if kind == "detail":
        views = "a large three-quarter view with front and back views beside it"
    count = f"{len(targets)} object{'s' if len(targets) != 1 else ''}"
    if shown:
        subject = ", ".join(focus[:6])
    elif mine:
        subject = f"your {_role['deliverable'] or 'work'} ({count})"
    else:
        subject = count
    return f"{views} of {subject}{frames}"


# ------------------------------------------------------------------ camera-scale review

def _review(focus, shading: str, isolate: bool, step, chosen=None, view_layer: str = "") -> ToolResult:
    """_review_in, framed, measured and rendered in the look's view layer (_shot_layer)."""
    scene = bpy.context.scene
    with _Restore() as setup, _shot_layer(scene, view_layer, setup):
        return _review_in(focus, shading, isolate, step, chosen)


def _review_in(focus, shading: str, isolate: bool, step, chosen=None) -> ToolResult:
    """Angle crops: one render through the look's camera at the scene's output size (capped at
    REVIEW_MAX), the whole frame on top and each object (focus, else the worker's own, else the
    brief's subjects; MAX_CROPS) cropped from it below, every crop at one common scale. Per object:
    its size on screen in output px and about how many px a centimetre of it spans, the frame edges
    that cut it, and brightness, spread, saturation, clipping and near black of its own visible
    pixels (display values). Which object a pixel shows comes from a second render, Workbench with
    one flat colour per object (_id_render), so what covers an object is not counted as it. Fine
    marks show here as the user's render will show them (Arcane run: brush marks painted at 1 cm
    vanished at pack scale)."""
    import numpy as np
    role = _role["role"]
    scene = bpy.context.scene
    started = time.monotonic()
    own = own_objects(scene)
    repaired = repair_meshes(own)
    camera = chosen or _camera_for(scene, focus, own)
    if camera is None:
        raise ToolError("The scene has no camera to look through (scene.camera is None).")
    visible = {o.session_uid for o in _visible(scene)}
    targets = _focus_objects(focus) or [o for o in own if o.session_uid in visible and o.type not in _NOT_SHOWN] \
        or _subjects(scene)
    if not targets:
        raise ToolError("Nothing visible to review.")
    left_out, targets = targets[MAX_CROPS:], targets[:MAX_CROPS]
    render = scene.render
    output = (max(1, round(render.resolution_x * render.resolution_percentage / 100)),
              max(1, round(render.resolution_y * render.resolution_percentage / 100)))
    scale = min(1.0, REVIEW_MAX / max(output))
    size = (max(16, round(output[0] * scale)), max(16, round(output[1] * scale)))
    folder = _role["folder"]
    name = f"{step if step is not None else _role['count']:03d}-{role}-crops"
    frame_path, ids_path = folder / f"{name}-frame.png", folder / f"{name}-ids.png"
    with _Restore(scene) as restore:
        compositor = _output(scene, restore)
        if isolate:
            _isolate(scene, targets, restore)
        with _Restore() as panel:
            key, used = _shade(scene, shading, panel, compositor=compositor)
            if key is not None:
                _aim_key(key, camera)
            provenance = _provenance(scene, used, size)
            _render(scene, camera, size, frame_path)
        _id_render(scene, camera, size, targets, ids_path)
        with _Resolution(scene, size):
            projected = _projections(scene, camera, targets)
            hidden = [(0.0, []) if isolate else _hidden_by(scene, camera, [o]) for o in targets]
        dimensions = [max(o.dimensions) for o in targets]
    try:
        rgb = _over_backdrop(_top_down(frame_path))[0]
        ids = _top_down(ids_path)
    finally:
        frame_path.unlink(missing_ok=True)
        ids_path.unlink(missing_ok=True)
    rows = []
    for index, (obj, found, extent) in enumerate(zip(targets, projected, dimensions)):
        mask = _id_mask(ids, index)
        visible = round(int(mask.sum()) * (output[0] / size[0]) * (output[1] / size[1]))
        stats = _pixel_stats(rgb[mask]) if visible else None
        rows.append({"name": obj.name, "box": _mask_box(mask),
                     "text": review_row(obj.name, found["bounds"], extent, output, visible, stats, hidden[index])})
    image = folder / f"{name}.png"
    crop_scale = _compose_review(rgb, rows, image)
    seconds = time.monotonic() - started
    shown = f", {len(left_out)} more left out" if left_out else ""
    ratio = f"{int(crop_scale)}:1" if crop_scale >= 1 else f"1:{round(1 / crop_scale)}"
    text = (f"Image attached: camera-scale review through {camera.name} at its output size {output[0]}x{output[1]}"
            f"{f' (rendered at {size[0]}x{size[1]})' if size != output else ''}: the whole frame on top, each object "
            f"below cropped from that one render at {ratio}{shown}, {seconds:.1f} s. Render: {provenance}. "
            "A painted mark reads when it spans about 3 px or more.\nPer object (output px; brightness and saturation "
            "are display values 0-1 of its own visible pixels):")
    for index, row in enumerate(rows, 1):
        text += f"\n{index} {row['text']}"
    text += _left_out(own)
    return ToolResult(f"{repair_note(repaired)}\n\n{text}" if repaired else text, image_path=image)


def _left_out(own) -> str:
    """The worker's own objects the look's view layer excludes, said: they are not in its image, and
    not in a render of that layer either."""
    layer = bpy.context.view_layer
    names = [o.name for o in own if o.type not in _NOT_SHOWN and not view_layers.includes(layer, o)]
    if not names:
        return ""
    return (f"\nNot in view layer {layer.name} (its collections exclude them), so not in this look or its render: "
            f"{view_layers.listed(names)}.")


def review_row(name: str, bounds, extent: float, output, visible: int, stats: dict | None,
               hidden=(0.0, [])) -> str:
    """One object's line in the review: bounds, its projected box (0..1 from bottom-left, beyond 0..1
    where the frame cuts it, None behind the camera); extent, its longest side in metres; output, the
    output size in px; visible, its visible pixels there; stats, _pixel_stats of them (None when
    none shows); hidden, _hidden_by's."""
    if bounds is None:
        return f"{name}: not in view (behind the camera)."
    x0, y0, x1, y1 = bounds
    if x1 < 0 or x0 > 1 or y1 < 0 or y0 > 1:
        return f"{name}: outside the frame."
    wide, tall = round((x1 - x0) * output[0]), round((y1 - y0) * output[1])
    per_cm = max(wide, tall) / (extent * 100) if extent > 0 else 0.0
    cut = [edge for edge, out in (("left", x0 < 0), ("right", x1 > 1), ("bottom", y0 < 0), ("top", y1 > 1)) if out]
    text = (f"{name}: {wide}x{tall} px on screen, about {per_cm:.1f} px per cm; "
            f"{'cut by the frame at the ' + ' and '.join(cut) if cut else 'whole in frame'}; ")
    if hidden[0] >= 0.05 and hidden[1]:
        text += f"{hidden[0] * 100:.0f} % hidden behind {', '.join(hidden[1])}; "
    if not visible or not stats:
        return text + "no pixel of it visible: covered by something in front."
    return text + (f"{visible:,} px visible; brightness {stats['mean']:.2f} (spread {stats['spread']:.2f}), "
                   f"saturation {stats['saturation']:.2f}, clipped {stats['clipped'] * 100:.1f} %, near black "
                   f"{stats['dark'] * 100:.0f} %.")


def _pixel_stats(pixels) -> dict:
    """Display RGB pixels (N x 3, 0-255): mean brightness (Rec. 709) and its spread, mean saturation,
    the share with a channel clipped at 255 and the share near black."""
    import numpy as np
    values = pixels.astype(np.float32) / 255.0
    luminance = values @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
    high, low = values.max(axis=1), values.min(axis=1)
    saturation = np.where(high > 1e-6, (high - low) / np.maximum(high, 1e-6), 0.0)
    return {"mean": float(luminance.mean()), "spread": float(luminance.std()), "saturation": float(saturation.mean()),
            "clipped": float(np.mean(high >= 254.5 / 255)), "dark": float(np.mean(high < DARK))}


def _top_down(path: Path):
    """A PNG's pixels, rows from the top, uint8 RGBA."""
    import imbuf
    import numpy as np
    image = imbuf.load(str(path))
    try:
        with image.with_buffer() as buffer:
            return np.array(buffer)[::-1].copy()
    finally:
        image.free()


def _id_colours() -> list:
    """Object colours for _id_render, far apart once 8-bit: every mix of ID_LEVELS but black and white."""
    import itertools
    return [c for c in itertools.product(ID_LEVELS, repeat=3) if len(set(c)) > 1 or c[0] not in (0.0, 1.0)]


def _id_render(scene, camera, size, targets, path: Path) -> None:
    """Workbench, flat, no anti-aliasing or dither, transparent film: each target in its own colour
    (_id_colours), everything else black, so each pixel says which object it shows."""
    colours = _id_colours()
    with _Restore() as ids:
        render, display = scene.render, scene.display.shading
        ids.set(render, "engine", "BLENDER_WORKBENCH")
        ids.set(render, "film_transparent", True)
        ids.set(render, "dither_intensity", 0.0)
        ids.set(display, "light", "FLAT")
        ids.set(display, "color_type", "OBJECT")
        for attr in ("show_cavity", "show_object_outline", "show_shadows", "show_xray", "show_specular_highlight"):
            ids.set(display, attr, False)
        ids.set(scene.display, "render_aa", "OFF")
        _standard_view(scene, ids)
        index = {o.session_uid: i for i, o in enumerate(targets)}
        for obj in scene.objects:
            if obj.type not in _NOT_SHOWN:
                i = index.get(obj.session_uid)
                try:
                    ids.set(obj, "color", (*colours[i], 1.0) if i is not None else (0.0, 0.0, 0.0, 1.0))
                except (AttributeError, TypeError, RuntimeError):
                    pass   # Linked: drawn in its own colour, which no target has.
        _render(scene, camera, size, path)


def _id_mask(ids, index: int):
    """Where _id_render drew target index (top-down, like ids)."""
    import numpy as np
    encoded = [round(255 * (v if v <= 0.0031308 else 1.055 * v ** (1 / 2.4) - 0.055)) for v in _id_colours()[index]]
    near = np.abs(ids[..., :3].astype(np.int16) - np.array(encoded, dtype=np.int16)).max(axis=-1) <= 24
    return near & (ids[..., 3] > 127)


def _mask_box(mask):
    import numpy as np
    rows, columns = np.nonzero(mask.any(axis=1))[0], np.nonzero(mask.any(axis=0))[0]
    if not len(rows):
        return None
    return int(columns[0]), int(rows[0]), int(columns[-1]) + 1, int(rows[-1]) + 1


def _compose_review(rgb, rows, image: Path) -> float:
    """The review sheet: the whole frame fitted into WIDTH x WIDTH * 3/8 with each object's visible box
    outlined and numbered, then every object's crop (its box and a margin) at one common scale, the
    largest of 4, 3, 2, 1, 1/2 ... 1/8 that fits the rest of a WIDTH-square sheet, numbered. The
    scale."""
    import imbuf
    import numpy as np
    h, w = rgb.shape[:2]
    shrink = min(WIDTH / w, WIDTH * 3 / 8 / h)
    ow, oh = max(1, int(w * shrink)), max(1, int(h * shrink))
    ys, xs = (np.arange(oh) / shrink).astype(int).clip(0, h - 1), (np.arange(ow) / shrink).astype(int).clip(0, w - 1)
    overview = rgb[ys][:, xs].copy()
    pad, gap = 6, 4
    crops = []
    for number, row in enumerate(rows, 1):
        box = row["box"]
        if box is None:
            continue
        x0, y0, x1, y1 = max(0, box[0] - pad), max(0, box[1] - pad), min(w, box[2] + pad), min(h, box[3] + pad)
        crops.append((number, rgb[y0:y1, x0:x1]))
        bx0, by0 = int(box[0] * shrink), int(box[1] * shrink)
        bx1, by1 = min(ow - 1, max(bx0 + 1, int(box[2] * shrink))), min(oh - 1, max(by0 + 1, int(box[3] * shrink)))
        for x in (bx0, bx1):
            overview[by0:by1 + 1, x] = (255, 200, 40)
        for y in (by0, by1):
            overview[y, bx0:bx1 + 1] = (255, 200, 40)
    room = WIDTH - oh - gap
    scale, placed = 1.0, []
    for scale in (4, 3, 2, 1, 1 / 2, 1 / 3, 1 / 4, 1 / 6, 1 / 8):
        placed = _shelves([(n, _scaled(c, scale)) for n, c in crops], WIDTH, room, gap)
        if placed is not None:
            break
    placed = placed or []
    height = oh + (gap + max(y + c.shape[0] for _n, c, _x, y in placed) if placed else 0)
    canvas = np.zeros((height, WIDTH, 3), dtype=np.uint8)
    canvas[:] = (20, 23, 29)
    canvas[:oh, :ow] = overview
    for _number, crop, x, y in placed:
        canvas[oh + gap + y:oh + gap + y + crop.shape[0], x:x + crop.shape[1]] = crop
    sheet = imbuf.new((WIDTH, height))
    try:
        with sheet.with_buffer(write=True) as buffer:
            pixels = np.asarray(buffer)
            pixels[..., :3] = canvas[::-1]
            pixels[..., 3] = 255
        import blf
        with blf.bind_imbuf(0, sheet):
            blf.size(0, tools.LABEL_SIZE)
            blf.color(0, 1, 1, 1, 1)
            for number, row in enumerate(rows, 1):
                if row["box"] is not None:
                    _tag(sheet, str(number), int(row["box"][0] * shrink), height - int(row["box"][1] * shrink) - 18, 40)
            for number, crop, x, y in placed:
                label = f"{number} {rows[number - 1]['name']}" if crop.shape[1] >= 90 else str(number)
                _tag(sheet, label, x, height - (oh + gap + y) - 18, max(24, crop.shape[1]))
        sheet.file_type = "PNG"
        imbuf.write(sheet, filepath=str(image))
    finally:
        sheet.free()
    return scale


def _scaled(crop, scale: float):
    """A crop at an integer scale up (each pixel a block) or 1/k down (block means)."""
    import numpy as np
    if scale >= 1:
        k = int(scale)
        return np.repeat(np.repeat(crop, k, axis=0), k, axis=1)
    k = round(1 / scale)
    h, w = (crop.shape[0] // k) * k, (crop.shape[1] // k) * k
    if not h or not w:
        return crop[:1, :1]
    return crop[:h, :w].astype(np.float32).reshape(h // k, k, w // k, k, 3).mean(axis=(1, 3)).astype(np.uint8)


def _shelves(crops, width: int, height: int, gap: int):
    """[(number, crop, x, y)] packed left to right in rows within width x height, in order, or None."""
    placed, x, y, row = [], 0, 0, 0
    for number, crop in crops:
        h, w = crop.shape[:2]
        if w > width:
            return None
        if x and x + w > width:
            x, y, row = 0, y + row + gap, 0
        if y + h > height:
            return None
        placed.append((number, crop, x, y))
        x, row = x + w + gap, max(row, h)
    return placed


# ------------------------------------------------------------------ what to frame

def _visible(scene) -> list:
    return [o for o in scene.objects if o.type not in _NOT_SHOWN and not o.get(TEMP) and o.visible_get()
            and not o.hide_render]


def _focus_objects(focus) -> list:
    """What focus names that a picture can show: the objects and their children, without lights,
    cameras and empties. A focus of only those (the automatic look after moving a light) is no
    focus: the look frames what the role judges instead."""
    if not focus:
        return []
    missing = [n for n in focus if n not in bpy.data.objects]
    if missing:
        raise ToolError(f"No such object(s): {', '.join(missing)}")
    chosen = [bpy.data.objects[n] for n in focus]
    return list(dict.fromkeys(o for obj in chosen for o in (obj, *obj.children_recursive) if o.type not in _NOT_SHOWN))


def own_objects(scene) -> list:
    """The worker's own objects: created in this copy, owned by the brief, or under the deliverable
    (what its looks frame and facts.deliverables measures)."""
    from . import ownership
    made = {idb.session_uid for idb in ownership.created_ids() if idb.id_type == "OBJECT"}
    root = bpy.data.objects.get(_role["deliverable"]) if _role["deliverable"] else None
    under = {o.session_uid for o in ((root, *root.children_recursive) if root else ())}
    owned = _role["owned"]
    return [o for o in scene.objects if not o.get(TEMP) and (
        o.session_uid in made or o.session_uid in under or ownership.key_of(o) in owned)]


def repair_meshes(objects) -> list[str]:
    """Mesh.validate() on these objects' meshes before anything renders or measures them, as the merge
    (merge._validate_meshes) and check_contact do: a face added without its corners (polygons.add
    with no loops.add, left behind when the model's code raised halfway) segfaults the render's
    triangulation (Mesh.corner_tris), and a bad index anything that reads the mesh. A valid mesh is
    left as it is; about 2 ms per 10,000 faces. The names of the objects whose mesh was fixed."""
    fixed, seen = [], set()
    for obj in objects:
        mesh = obj.data if obj.type == "MESH" else None
        if mesh is None or mesh.library is not None or mesh.session_uid in seen:
            continue
        seen.add(mesh.session_uid)
        if mesh.validate(verbose=False, clean_customdata=False):
            fixed.append(obj.name)
    return fixed


def repair_note(names: list[str]) -> str:
    return REPAIR_NOTE.format(names=", ".join(names))


def _mine(material) -> bool:
    from . import ownership
    return not material.get(ownership.STAMP) or ownership.key_of(material) in _role["owned"]


def _named_subjects(scene) -> list:
    visible = {o.session_uid for o in _visible(scene)}
    found = []
    for name in _role["subjects"]:
        obj = bpy.data.objects.get(name)
        if obj is not None:
            found += [o for o in (obj, *obj.children_recursive) if o.session_uid in visible]
    return list(dict.fromkeys(found))


def _subjects(scene) -> list:
    """What a look measures and, without a camera, frames for lighting: the brief's subjects, else
    the scene's objects without the floor and backdrops."""
    named = _named_subjects(scene)
    if named:
        return named
    visible = _visible(scene)
    boxes = {}
    for obj in visible:
        try:
            points = _corners([obj])
        except ToolError:  # An empty mesh has no subject bounds yet.
            continue
        boxes[obj.name] = (tuple(min(p[i] for p in points) for i in range(3)), tuple(max(p[i] for p in points) for i in range(3)))
    dropped = backdrops(boxes)
    return [o for o in visible if o.name in boxes and o.name not in dropped]


def _default_targets(scene, role, own) -> list:
    """Frame the deliverable, never surrounding scene geometry when it has not been built yet."""
    visible = _visible(scene)
    ids = {o.session_uid for o in visible}
    own = [o for o in own if o.session_uid in ids]
    if role in ("modeling", "materials", "animation") and own:
        return own
    if role == "materials":
        wearing = [o for o in visible if any(s.material is not None and _mine(s.material) for s in o.material_slots)]
        return wearing
    if role in ("modeling", "animation"):
        return []
    return _subjects(scene) or own or visible


def _camera_for(scene, focus, own):
    """The camera a look goes through: one focus names; for the camera and animation roles their
    own (created first, then the scene's if owned, then other owned); the brief's (a check's
    camera); the scene's; the only one there is."""
    named = [bpy.data.objects[n] for n in focus or () if n in bpy.data.objects and bpy.data.objects[n].type == "CAMERA"]
    if named:
        return named[0]
    if _role["role"] in ("camera", "animation"):
        mine = _own_cameras(scene, own)
        if mine:
            return mine[0]
    for name in _role["cameras"]:
        camera = bpy.data.objects.get(name)
        if camera is not None and camera.type == "CAMERA":
            return camera
    if scene.camera is not None:
        return scene.camera
    cameras = [o for o in scene.objects if o.type == "CAMERA" and not o.get(TEMP)]
    return cameras[0] if len(cameras) == 1 else None


def _own_cameras(scene, own) -> list:
    """The worker's cameras: those it made (by name), then owned ones, the scene's first."""
    from . import ownership
    cameras = [o for o in own if o.type == "CAMERA"]
    made = sorted((o for o in cameras if not o.get(ownership.STAMP)), key=lambda o: o.name)
    owned = sorted((o for o in cameras if o.get(ownership.STAMP)), key=lambda o: (o != scene.camera, o.name))
    return made + owned


def _tiles(scene, views, camera, frames, own) -> list:
    if len(views) == 1 and views[0].startswith("frames:"):
        view = views[0].split(":", 1)[1]
        return [_Tile(f"frame {f}", view, f, camera if view == "camera" else None) for f in _frames(scene, frames)]
    if views == ("cameras",):
        cameras = _own_cameras(scene, own)[:MAX_CAMERAS]
        if len(cameras) >= 2:
            return [_Tile(c.name, "camera", None, c) for c in cameras]
        views = SHEET_ANGLES
    tiles = []
    for view in views:
        if view == "camera" and camera is None:
            raise ToolError("The scene has no camera to look through (scene.camera is None).")
        label = "" if len(views) == 1 else view.replace("_", " ")
        tiles.append(_Tile(label, view, None, camera if view == "camera" else None))
    return tiles


def _frames(scene, frames) -> list[int]:
    if frames:
        return [int(f) for f in list(frames)[:MAX_FRAMES]]
    start, end = scene.frame_start, scene.frame_end
    count = min(MAX_FRAMES, end - start + 1)
    if count <= 1:
        return [start]
    return sorted({round(start + (end - start) * i / (count - 1)) for i in range(count)})


# ------------------------------------------------------------------ setup and restore

class _Restore:
    """Remembers settings before they change and temporary datablocks as they are made; on exit
    puts every setting back, in reverse, removes the datablocks and returns to the frame it found."""

    def __init__(self, scene=None):
        self.saved, self.temporary = [], []
        self.scene = scene
        self.frame = scene.frame_current if scene is not None else None

    def set(self, owner, attr: str, value) -> None:
        before = getattr(owner, attr)
        if hasattr(before, "__len__") and not isinstance(before, str):
            before = tuple(before)   # A colour is a live array: keep its values, not the array.
        self.saved.append((owner, attr, before))
        setattr(owner, attr, value)

    def keep(self, datablock):
        datablock[TEMP] = True
        self.temporary.append(datablock)
        return datablock

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        for owner, attr, value in reversed(self.saved):
            try:
                setattr(owner, attr, value)
            except (AttributeError, TypeError, ReferenceError):
                pass
        for datablock in reversed(self.temporary):
            collection = _collection_of(datablock)
            try:
                if collection is not None:
                    collection.remove(datablock)
            except ReferenceError:
                pass
        if self.scene is not None and self.scene.frame_current != self.frame:
            self.scene.frame_set(self.frame)
        result = bpy.data.images.get("Render Result")
        if result is not None and result.type == "RENDER_RESULT":
            bpy.data.images.remove(result)
        return False


def _collection_of(datablock):
    kind = datablock.id_type
    return {"OBJECT": bpy.data.objects, "CAMERA": bpy.data.cameras, "WORLD": bpy.data.worlds,
            "IMAGE": bpy.data.images, "LIGHT": bpy.data.lights, "MATERIAL": bpy.data.materials}.get(kind)


# Compositor nodes that only pass the render through: a tree of these alone is no compositor.
_PASS_NODES = {"R_LAYERS", "COMPOSITE", "GROUP_OUTPUT", "GROUP_INPUT", "VIEWER", "REROUTE", "FRAME"}


def _compositor(scene) -> bool:
    """Whether a render of the scene runs its own compositor: turned on, with a node group (5.x:
    scene.compositing_node_group) that does more than pass the render through (a Glare's bloom)."""
    group = getattr(scene, "compositing_node_group", None)
    if group is None and getattr(scene, "use_nodes", False):
        group = getattr(scene, "node_tree", None)   # Before 5.0.
    return bool(scene.render.use_compositing and group is not None
                and any(n.type not in _PASS_NODES for n in group.nodes))


def _output(scene, restore: _Restore) -> bool:
    """The look's output settings, the compositor off. Returns whether the scene had one of its own
    (_compositor), which the scene shading turns back on (_shade), so bloom and glow show."""
    render = scene.render
    compositor = _compositor(scene)
    restore.set(render, "resolution_percentage", 100)
    restore.set(render, "use_border", False)
    restore.set(render, "use_compositing", False)   # Nothing downstream writes files or changes the look.
    restore.set(render, "use_sequencer", False)
    restore.set(render, "film_transparent", False)
    restore.set(render, "use_file_extension", False)
    restore.set(render.image_settings, "file_format", "PNG")
    restore.set(render.image_settings, "color_mode", "RGBA")
    restore.set(render.image_settings, "color_depth", "8")
    if hasattr(render.image_settings, "color_management"):
        restore.set(render.image_settings, "color_management", "FOLLOW_SCENE")  # The caption's view transform.
    return compositor


def _shade(scene, shading: str, restore: _Restore, studio: bool = False, compositor: bool = False) -> tuple:
    """Set one panel's shading up; restore puts it back when the panel is done. (the studio key light
    to aim at the panel's camera (_aim_key) or None, the shading used). studio: scene falls back to
    the studio light when the scene has none of its own (a contact sheet cell). compositor: the
    scene's own (_output's), which the scene shading keeps on; every other shading renders without."""
    render = scene.render
    layer = bpy.context.view_layer
    if shading == "scene" and not (studio and unlit(scene, layer)):
        _setup_final(scene, restore)
        if getattr(layer, "samples", 0) > CAPPED_SAMPLES:   # A layer's own samples win over the scene's.
            restore.set(layer, "samples", CAPPED_SAMPLES)
        if compositor:
            restore.set(render, "use_compositing", True)
        return None, "scene"
    for attr in ("world_override", "material_override"):   # Each shading's own light and colours, not the layer's.
        if getattr(layer, attr, None) is not None:
            restore.set(layer, attr, None)
    restore.set(render, "film_transparent", True)   # Over BACKDROP in _compose; its alpha is the subject's cover.
    _standard_view(scene, restore)
    if shading in ("clay", "flat", "solid"):
        restore.set(render, "engine", "BLENDER_WORKBENCH")
        display = scene.display.shading
        restore.set(display, "light", "FLAT" if shading == "flat" else "STUDIO")
        restore.set(display, "color_type", "SINGLE" if shading == "clay" else "MATERIAL")
        if shading == "clay":
            restore.set(display, "single_color", (0.8, 0.78, 0.74))
        if shading == "flat":   # Workbench shows the viewport colour: set it to the Base Color for this panel.
            for material in _materials(scene):
                socket = _base_socket(material)
                if socket is not None and not socket.is_linked:
                    restore.set(material, "diffuse_color", _colour(socket))
        restore.set(display, "show_cavity", True)
        restore.set(display, "cavity_type", "BOTH")
        restore.set(display, "show_object_outline", True)
        restore.set(display, "show_shadows", False)
        restore.set(scene.display, "render_aa", "8")
        return None, shading
    restore.set(render, "engine", "BLENDER_EEVEE")
    restore.set(scene.eevee, "taa_render_samples", CAPPED_SAMPLES)
    if shading == "albedo":
        _albedo(scene, restore)
        return None, "albedo"
    return _studio(scene, restore), "studio"


def _setup_final(scene, restore: _Restore) -> None:
    """The scene's own engine, world, lights and colour management; only the samples capped (never
    raised), denoising on and, for Cycles, a time limit and the CPU (a factory child has no GPU
    preferences)."""
    render = scene.render
    if render.engine == "CYCLES":
        restore.set(scene.cycles, "samples", max(1, min(scene.cycles.samples, CAPPED_SAMPLES)))
        restore.set(scene.cycles, "use_denoising", True)
        restore.set(scene.cycles, "time_limit", float(CAPPED_SECONDS))
        restore.set(scene.cycles, "device", "CPU")
    elif render.engine == "BLENDER_EEVEE":
        restore.set(scene.eevee, "taa_render_samples", max(1, min(scene.eevee.taa_render_samples, CAPPED_SAMPLES)))


def _standard_view(scene, restore: _Restore) -> None:
    """Standard colour on an sRGB display, no look, exposure, curves or white balance: a colour shows
    as its value, what the studio light and albedo are set for."""
    view = scene.view_settings
    restore.set(view, "view_transform", "Standard")
    restore.set(view, "look", "None")
    restore.set(view, "exposure", 0.0)
    restore.set(view, "gamma", 1.0)
    restore.set(view, "use_curve_mapping", False)
    if hasattr(view, "use_white_balance"):
        restore.set(view, "use_white_balance", False)
    try:
        restore.set(scene.display_settings, "display_device", "sRGB")
    except (TypeError, AttributeError):
        pass


def _white_world(restore: _Restore, name: str, strength: float):
    world = restore.keep(bpy.data.worlds.new(name))
    world.use_nodes = True
    background = next(n for n in world.node_tree.nodes if n.type == "BACKGROUND")
    background.inputs["Color"].default_value = (1.0, 1.0, 1.0, 1.0)
    background.inputs["Strength"].default_value = strength
    return world


def _lights_off(scene, restore: _Restore) -> None:
    for obj in scene.objects:
        if obj.type == "LIGHT" and not obj.hide_render and not obj.get(TEMP):
            restore.set(obj, "hide_render", True)


def _studio(scene, restore: _Restore):
    """The look-dev light (STUDIO_AMBIENT, STUDIO_KEY): a white world, the scene's lights off, and a
    sun that _aim_key turns for each view so the key always comes from its upper left. The sun."""
    restore.set(scene, "world", _white_world(restore, "lc_studio", STUDIO_AMBIENT))
    _lights_off(scene, restore)
    data = restore.keep(bpy.data.lights.new("lc_key", "SUN"))
    data.energy = STUDIO_KEY * math.pi
    data.angle = math.radians(12)   # Soft-edged shadows.
    key = restore.keep(bpy.data.objects.new("lc_key", data))
    scene.collection.objects.link(key)
    return key


def _aim_key(key, camera) -> None:
    from mathutils import Vector
    toward = camera.matrix_world.to_3x3().normalized() @ Vector(KEY_FROM)   # Where the light comes from.
    key.rotation_mode = "QUATERNION"
    key.rotation_quaternion = toward.to_track_quat("Z", "Y")                # A sun shines along its -Z.


def _albedo(scene, restore: _Restore) -> None:
    """Each material shown swapped, for this panel, for a copy (temporary) whose surface is its Base
    Color as emission: the value, or whatever feeds it (a painted texture through its UV map, a mix).
    The originals are never edited. The world is white at 1 and the scene's lights are off, so a
    surface with nothing to copy (no material) shows its colour evenly lit instead of black."""
    restore.set(scene, "world", _white_world(restore, "lc_albedo", 1.0))
    _lights_off(scene, restore)
    copies = {}
    for obj in _visible(scene):
        for slot in obj.material_slots:
            material = slot.material
            if material is None:
                continue
            if material.name not in copies:
                copies[material.name] = restore.keep(_emitting(material))
            try:
                restore.set(slot, "material", copies[material.name])
            except (AttributeError, TypeError, RuntimeError):
                pass   # Linked data: it shows lit by the white world.


def _emitting(material):
    copy = material.copy()
    copy.name = "lc_albedo"
    tree = copy.node_tree
    if tree is None:
        return copy
    socket = _base_socket(copy)
    emission = tree.nodes.new("ShaderNodeEmission")
    emission.inputs["Strength"].default_value = 1.0
    if socket is not None and socket.is_linked:
        tree.links.new(socket.links[0].from_socket, emission.inputs["Color"])
    else:
        emission.inputs["Color"].default_value = _colour(socket) if socket is not None else tuple(material.diffuse_color)
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    output.target = "ALL"
    tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])
    for node in tree.nodes:
        if node.type == "OUTPUT_MATERIAL":
            node.is_active_output = node == output
    return copy


def _materials(scene) -> list:
    return list(dict.fromkeys(s.material for o in _visible(scene) for s in o.material_slots if s.material))


def _base_socket(material):
    """The input that sets a material's colour: Base Color (or Color) of the first shader found
    upstream of its active output's surface, else of any Principled BSDF; None without one."""
    tree = material.node_tree
    if tree is None:
        return None
    outputs = [n for n in tree.nodes if n.type == "OUTPUT_MATERIAL"]
    output = next((n for n in outputs if n.is_active_output), outputs[0] if outputs else None)
    queue = [link.from_node for link in output.inputs["Surface"].links] if output else []
    seen = set()
    while queue:
        node = queue.pop(0)
        if node.name in seen:
            continue
        seen.add(node.name)
        if node.type.startswith("BSDF") or node.type in ("EMISSION", "SUBSURFACE_SCATTERING"):
            socket = node.inputs.get("Base Color") or node.inputs.get("Color")
            if socket is not None:
                return socket
        queue += [link.from_node for socket in node.inputs for link in socket.links]
    principled = next((n for n in tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
    return principled.inputs.get("Base Color") if principled else None


def _colour(socket) -> tuple:
    value = socket.default_value
    try:
        values = tuple(value)
    except TypeError:
        values = (float(value),) * 3
    return (*values[:3], 1.0) if len(values) >= 3 else (values[0],) * 3 + (1.0,)


def _isolate(scene, keep, restore: _Restore) -> None:
    """Everything a render would show hidden but keep and its children: an object in front is gone,
    the lights and world stay."""
    kept = {o.session_uid for obj in keep for o in (obj, *obj.children_recursive)}
    for obj in scene.objects:
        if obj.type not in _NOT_SHOWN and not obj.hide_render and obj.session_uid not in kept:
            restore.set(obj, "hide_render", True)


# ------------------------------------------------------------------ cameras and rendering

def _fit_camera(scene, view: str, points, size, restore: _Restore):
    data = restore.keep(bpy.data.cameras.new("lc_look"))
    data.lens, data.sensor_width, data.sensor_fit = 50.0, 36.0, "AUTO"
    camera = restore.keep(bpy.data.objects.new("lc_look", data))
    scene.collection.objects.link(camera)
    degrees = VIEWS[view]
    basis = framing.euler_basis(degrees)
    frame = framing.frame(tuple(size), lens=data.lens, sensor_width=data.sensor_width)
    try:
        fitted = framing.fit(points, basis, frame, MARGIN)
    except ValueError as ex:
        raise ToolError(f"Nothing to frame: {ex}.") from ex
    depths = [framing.dot(tuple(c - p for c, p in zip(fitted.location, point)), basis[2]) for point in points]
    if not all(math.isfinite(v) for v in (*fitted.location, *depths)):
        raise ToolError("Nothing to frame: unusable world bounds.")
    near, far = min(depths), max(depths)
    if near <= 2 * MIN_CLIP:
        raise ToolError("Nothing to frame: geometry is too small for the preview camera's clipping range.")
    camera.location = fitted.location
    camera.rotation_mode = "XYZ"
    camera.rotation_euler = [math.radians(a) for a in degrees]
    data.clip_start, data.clip_end = max(MIN_CLIP, near * 0.5), far * 2.0
    bpy.context.view_layer.update()   # Its matrix_world, for what reads it before the render does.
    return camera


def _hidden_by(scene, camera, targets) -> tuple:
    """How much of the subject other objects hide from this camera, measured, not rendered: rays from
    the camera to up to HIDDEN_RAYS of the subject's vertices that land in frame; (the share whose
    first hit is something else, those objects' names, most first). Rays the subject stops itself
    (its far side) do not count. A focus look of a prop behind another kept showing the other one
    (Arcane run W0005, W0029, W0048, W0065); this names it."""
    from bpy_extras.object_utils import world_to_camera_view
    from mathutils import Vector
    graph = bpy.context.evaluated_depsgraph_get()
    subject = {o.name for o in targets}
    meshes = [o.evaluated_get(graph) for o in targets if o.type == "MESH"]
    total = sum(len(o.data.vertices) for o in meshes)
    if not total:
        return 0.0, []
    stride = max(1, total // HIDDEN_RAYS)
    eye = camera.matrix_world.translation
    forward = camera.matrix_world.to_3x3() @ Vector((0.0, 0.0, -1.0))
    ortho = camera.data.type == "ORTHO"
    counted, blockers = 0, {}
    for obj in meshes:
        world = obj.matrix_world
        vertices = obj.data.vertices
        for index in range(0, len(vertices), stride):
            point = world @ vertices[index].co
            seen = world_to_camera_view(scene, camera, point)
            if seen.z <= 0 or not (0 <= seen.x <= 1 and 0 <= seen.y <= 1):
                continue
            origin = point - forward * (point - eye).dot(forward) if ortho else eye
            ray = point - origin
            length = ray.length
            if length < 1e-6:
                continue
            counted += 1
            hit, _at, _normal, _index, found, _matrix = scene.ray_cast(graph, origin, ray / length,
                                                                       distance=length * (1 - 1e-4))
            if hit and found is not None and found.name not in subject and not found.hide_render \
                    and not found.get(TEMP):
                blockers[found.name] = blockers.get(found.name, 0) + 1
    if not counted:
        return 0.0, []
    return round(sum(blockers.values()) / counted, 3), sorted(blockers, key=blockers.get, reverse=True)[:3]


def _corners(objects) -> list:
    """Evaluated world bounds only: empty meshes have a collapsed sentinel box, not geometry.
    Including that box (often far from a small deliverable) pulls the fitted camera back."""
    from mathutils import Vector
    graph = bpy.context.evaluated_depsgraph_get()
    points = []
    for obj in objects:
        evaluated = obj.evaluated_get(graph)
        corners = [tuple(corner) for corner in evaluated.bound_box]
        if not corners or not all(math.isfinite(v) for corner in corners for v in corner):
            continue
        if not any(max(c[i] for c in corners) > min(c[i] for c in corners) for i in range(3)):
            continue
        world = [tuple(evaluated.matrix_world @ Vector(corner)) for corner in corners]
        if not all(math.isfinite(v) and abs(v) <= MAX_WORLD_BOUND for corner in world for v in corner):
            raise ToolError(f"Nothing to frame: {obj.name} has non-finite or too large world bounds.")
        if not any(max(c[i] for c in world) > min(c[i] for c in world) for i in range(3)):
            continue
        points += world
    if not points:
        raise ToolError("Nothing visible to frame.")
    return points


def _points_over(scene, objects, frames) -> list:
    """The objects' corners at every frame: one framing for a sheet of frames, so what moves is
    seen moving instead of each frame centring it again."""
    points = []
    for frame in dict.fromkeys(f for f in frames if f is not None):
        scene.frame_set(frame)
        points += _corners(objects)
    return points


def _camera_aspect(scene) -> float:
    render = scene.render
    return (render.resolution_y * render.pixel_aspect_y) / (render.resolution_x * render.pixel_aspect_x)


class _Resolution:
    """The scene's render size set to a tile's while projecting, so names land where they render."""

    def __init__(self, scene, size):
        self.render, self.size = scene.render, size

    def __enter__(self):
        render = self.render
        self.saved = (render.resolution_x, render.resolution_y, render.resolution_percentage)
        render.resolution_x, render.resolution_y = self.size
        render.resolution_percentage = 100

    def __exit__(self, *exc):
        self.render.resolution_x, self.render.resolution_y, self.render.resolution_percentage = self.saved
        return False


def _render(scene, camera, size, path: Path) -> None:
    """One still through camera at size: the look's view layer alone (_shot), else (the compositor's
    mix of several, or outside a look) every layer that renders."""
    render = scene.render
    saved = (scene.camera, render.resolution_x, render.resolution_y, render.filepath)
    single = {"layer": _shot["layer"]} if _shot["layer"] else {}
    try:
        scene.camera = camera
        render.resolution_x, render.resolution_y = size
        render.filepath = str(path)
        bpy.ops.render.render(write_still=True, scene=scene.name, **single)
    finally:
        scene.camera, render.resolution_x, render.resolution_y, render.filepath = saved
    if not path.is_file():
        raise ToolError("The look did not render (no image was written).")


def _exposure(scene, tile: _Tile, subject: list) -> dict:
    """Mean display brightness (Rec. 709 weights) of the frame, of the subject's box in it and of
    the rest; the share of pixels with a channel at 255 and of near-black ones; the frame's tint."""
    import colorsys
    import imbuf
    import numpy as np
    image = imbuf.load(str(tile.path))
    try:
        with image.with_buffer() as buffer:
            pixels = np.asarray(buffer)[..., :3].astype(np.float32) / 255.0
    finally:
        image.free()
    luminance = pixels @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
    numbers = {"mean": round(float(luminance.mean()), 3),
               "clipped": round(float(np.mean(pixels.max(axis=2) >= 254.5 / 255.0)), 4),
               "dark": round(float(np.mean(pixels.max(axis=2) < DARK)), 4)}
    box = _subject_box(scene, tile, subject)
    if box is not None:
        h, w = luminance.shape
        x0, y0, x1, y1 = (int(round(v * s)) for v, s in zip(box, (w, h, w, h)))
        if x1 - x0 >= 2 and y1 - y0 >= 2:
            inside = luminance[y0:y1, x0:x1]
            numbers["subject"] = round(float(inside.mean()), 3)
            rest = luminance.sum() - inside.sum()
            count = luminance.size - inside.size
            if count > 0.05 * luminance.size:
                numbers["background"] = round(float(rest / count), 3)
    r, g, b = (float(v) for v in pixels.reshape(-1, 3).mean(axis=0))
    hue, saturation, _value = colorsys.rgb_to_hsv(r, g, b)
    if saturation >= 0.05:
        numbers["hue"], numbers["saturation"] = round(hue * 360) % 360, round(saturation, 2)
    return numbers


def _subject_box(scene, tile: _Tile, subject: list):
    """The subject's box in the tile, 0..1 from bottom-left and clipped to the frame, or None."""
    if not subject:
        return None
    with _Resolution(scene, tile.rect[2:]):
        found = [p["bounds"] for p in _projections(scene, tile.camera, subject) if p["bounds"] is not None]
    if not found:
        return None
    box = [min(b[0] for b in found), min(b[1] for b in found), max(b[2] for b in found), max(b[3] for b in found)]
    box = [min(max(v, 0.0), 1.0) for v in box]
    return box if box[2] > box[0] and box[3] > box[1] else None


# ------------------------------------------------------------------ labels and composition

def _projections(scene, camera, objects) -> list:
    """inspection_worker.projections, imported the way the render worker imports it: its folder on
    sys.path for jobs and job_worker. The path is put back afterwards, so model code in this child
    does not see Loopcut's modules as top-level ones."""
    saved = list(sys.path)
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from . import inspection_worker
    finally:
        sys.path[:] = saved
    return inspection_worker.projections(scene, camera, objects)


def _compose(scene, tiles, canvas, targets, image: Path, labels: bool, role: str, beside=None) -> None:
    """The tiles at their rects, names and labels drawn on; beside (RGB, bottom-up), the reference tile,
    at the top right, labelled reference."""
    import blf
    import imbuf
    import numpy as np
    width, height = canvas
    sheet = imbuf.new((width, height))
    try:
        with sheet.with_buffer(write=True) as buffer:
            np.asarray(buffer)[:] = (20, 23, 29, 255)
        for tile in tiles:
            x, top, w, h = tile.rect
            y = height - top - h  # imbuf rows run from the bottom.
            loaded = imbuf.load(str(tile.path))
            try:
                with loaded.with_buffer() as source, sheet.with_buffer(write=True) as dest:
                    rgb, alpha = _over_backdrop(np.asarray(source)[:h, :w])
                    np.asarray(dest)[y:y + h, x:x + w, :3] = rgb
                    np.asarray(dest)[y:y + h, x:x + w, 3] = 255
                    _measure(tile, rgb, alpha)
            finally:
                loaded.free()
            if role == "camera" and tile.view == "camera":
                _guides(sheet, x, y, (w, h), _role["region"])
            with blf.bind_imbuf(0, sheet):
                blf.size(0, tools.LABEL_SIZE)
                blf.color(0, 1, 1, 1, 1)
                if labels and targets and w >= LABELLED_TILE:
                    names = _visible(scene) if tile.view == "camera" else targets
                    with _Resolution(scene, (w, h)):
                        _names(sheet, scene, tile.camera, names, x, y, (w, h))
                if tile.label:
                    _tag(sheet, tile.label, x + 4, y + h - 20, w)
                if tile.stamp:
                    _tag(sheet, tile.stamp, x + 4, y + 4, w)
        if beside is not None:
            h, w = beside.shape[:2]
            x, y = width - w, height - h
            with sheet.with_buffer(write=True) as dest:
                np.asarray(dest)[y:y + h, x:x + w, :3] = beside
            with blf.bind_imbuf(0, sheet):
                blf.size(0, tools.LABEL_SIZE)
                blf.color(0, 1, 1, 1, 1)
                _tag(sheet, "reference", x + 4, y + h - 20, w)
        sheet.file_type = "PNG"
        imbuf.write(sheet, filepath=str(image))
    finally:
        sheet.free()


def _over_backdrop(pixels):
    """A rendered tile (straight alpha, as PNG keeps it) over BACKDROP: (rgb as uint8, alpha 0..1).
    An opaque render (scene shading) comes out as it was."""
    import numpy as np
    pixels = pixels.astype(np.float32)
    alpha = pixels[..., 3:4] / 255.0
    rgb = pixels[..., :3] * alpha + np.array(BACKDROP, dtype=np.float32) * (1.0 - alpha)
    return np.round(rgb).astype(np.uint8), alpha[..., 0]


def _measure(tile: _Tile, rgb, alpha) -> None:
    """What panel_warnings reads: how much of the panel anything covers (a transparent film's alpha),
    whether it is one flat colour, and a SIGNATURE-sized copy to compare panels by."""
    import numpy as np
    if tile.shading != "scene":
        tile.coverage = round(float((alpha > 0.5).mean()), 4)
    tile.uniform = float(rgb.std()) < 1.0
    rows, columns = SIGNATURE[1], SIGNATURE[0]
    h, w = rgb.shape[:2]
    bh, bw = max(1, h // rows), max(1, w // columns)
    if h >= rows and w >= columns:
        small = rgb[:bh * rows, :bw * columns].astype(np.float32).reshape(rows, bh, columns, bw, 3).mean(axis=(1, 3))
        tile.signature = [round(float(v), 1) for v in small.ravel()]


def _box(sheet, x0: int, y0: int, x1: int, y1: int, color=(15, 18, 25, 255)) -> None:
    import numpy as np
    with sheet.with_buffer(write=True) as buffer:
        pixels = np.asarray(buffer)
        pixels[max(0, y0):max(0, y1), max(0, x0):max(0, x1)] = color


def _tag(sheet, text: str, x: int, y: int, room: int) -> None:
    import blf
    while text and blf.dimensions(0, text)[0] > room - 12:
        text = text[:-1]  # Never let one label bleed into the next tile.
    width = math.ceil(blf.dimensions(0, text)[0]) + 8
    _box(sheet, x, y, x + width, y + 18)
    blf.position(0, x + 4, y + 4, 0)
    blf.draw_buffer(0, text)


def _names(sheet, scene, camera, targets, x: int, y: int, size) -> None:
    """Object names where they are in the frame, nearest first, skipping one that would cover a
    name already placed."""
    eye = camera.matrix_world.translation
    ordered = sorted(targets, key=lambda o: (o.matrix_world.translation - eye).length)[:LABEL_LIMIT * 2]
    placed = []
    for found in _projections(scene, camera, ordered):
        box = found["bounds"]
        if box is None or found["hidden_render"]:
            continue
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        if not (0 <= cx <= 1 and 0 <= cy <= 1):
            continue
        import blf
        label = found["name"][:28]
        w = math.ceil(blf.dimensions(0, label)[0]) + 8
        lx = x + max(2, min(size[0] - w - 2, int(cx * size[0] - w / 2)))
        ly = y + max(2, min(size[1] - 20, int(cy * size[1])))
        mine = (lx, ly, lx + w, ly + 18)
        if any(not (mine[2] < b[0] or b[2] < mine[0] or mine[3] < b[1] or b[3] < mine[1]) for b in placed):
            continue
        placed.append(mine)
        _tag(sheet, label, lx, ly, size[0])
        if len(placed) == LABEL_LIMIT:
            break


def _guides(sheet, x: int, y: int, size, region) -> None:
    """Rule-of-thirds lines, and the brief's region as a rectangle."""
    import numpy as np
    w, h = size
    with sheet.with_buffer(write=True) as buffer:
        pixels = np.asarray(buffer)
        for i in (1, 2):
            pixels[y:y + h, x + w * i // 3] = (255, 255, 255, 255)
            pixels[y + h * i // 3, x:x + w] = (255, 255, 255, 255)
        if region and len(region) == 4:
            x0, y0, x1, y1 = (int(min(max(v, 0.0), 1.0) * s) for v, s in zip(region, (w, h, w, h)))
            x1, y1 = max(x0 + 1, min(x1, w - 1)), max(y0 + 1, min(y1, h - 1))
            color = (255, 200, 40, 255)
            for t in range(2):
                pixels[y + y0 + t, x + x0:x + x1] = color
                pixels[y + y1 - t, x + x0:x + x1] = color
                pixels[y + y0:y + y1, x + x0 + t] = color
                pixels[y + y0:y + y1, x + x1 - t] = color


def _material_values(targets) -> str:
    """The numbers behind the look: a render can't tell 0.4 roughness from 0.5."""
    lines, seen = [], set()
    for obj in targets:
        for slot in obj.material_slots:
            material = slot.material
            if material is None or material.name in seen:
                continue
            seen.add(material.name)
            bsdf = next((n for n in material.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None) \
                if material.node_tree else None
            if bsdf is None:
                lines.append(f"{material.name}: no Principled BSDF")
                continue
            values = []
            for socket, short in (("Base Color", "base"), ("Metallic", "metallic"), ("Roughness", "roughness"),
                                  ("Transmission Weight", "transmission"), ("Emission Strength", "emission")):
                if socket not in bsdf.inputs:
                    continue
                entry = bsdf.inputs[socket]
                if entry.is_linked:
                    values.append(f"{short} from nodes")
                    continue
                value = entry.default_value
                if hasattr(value, "__len__"):
                    values.append(f"{short} ({', '.join(f'{v:.2f}' for v in list(value)[:3])})")
                elif short in ("base", "metallic", "roughness") or value:
                    values.append(f"{short} {value:.2f}")
            lines.append(f"{material.name}: {', '.join(values)}")
            if len(lines) == 8:
                break
    return ("\nMaterials: " + "; ".join(lines)) if lines else "\nNo materials on these objects."
