"""Shader compilation off the main thread, for looks (loopcut/track/FREEZE-2026-10-10.md).

A look draws the scene offscreen with EEVEE on the main thread, and that draw compiles every shader
it needs synchronously: EEVEE's own on the session's first EEVEE draw, then each new or changed
material. On a cold shader cache that froze the interface for 5-7 s on an M1. So before such a look,
the materials it will draw that were not drawn as they are now get a material preview render. That
is a job thread: it compiles the same passes (Blender's pass cache is keyed by generated code, so
the look finds them ready). The agent thread starts the previews one at a time and waits for each,
polling the main thread, and only then asks for the capture, which is today's blocking draw: the
image is the same, only the compiling has moved.

One thing still blocks the interface: a preview render holds the draw manager's lock
(draw_gpu_context.cc, DRW_lock_start) for the whole render, and a 3D viewport redraw on the main
thread waits for that lock. So nothing here asks for a viewport redraw while a job may hold it: the
preview starts through ed.lib_id_generate_preview, whose NC_ASSET notifier the 3D view ignores
(ID.asset_generate_preview sends NC_ID, which redraws every 3D view at once and so waited for the
whole cold compile, 7.3 s in C32's first probe), and each next job starts only once the previous
one's end-of-job redraw has happened.

Also here: Subprocess shader compilation on Windows and Linux, set once at first run.
"""

import contextlib
import dataclasses
import functools
import os
import sys
import time

import bpy

WAIT_SECONDS = 20.0   # After this the look is taken anyway: the same image, the rest compiled in the draw.
POLL_SECONDS = 0.1
# Idle this long before a preview starts and before the capture: a job's end sends a redraw of every
# region (NC_WINDOW) that must be drawn before the next job takes the draw lock, and a preview job
# waiting behind another render job is suspended, not running, until its own 0.1 s job timer.
SETTLE_SECONDS = 0.3
MAX_CHECKED = 48      # Materials compared per look, focus first: a signature reads every node.
MAX_WARMED = 12       # Preview jobs per look, one at a time; the rest compile in the draw.
EEVEE_ENGINES = {"BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"}
EEVEE_STYLES = {"material", "rendered"}  # capture_viewport's styles that draw with EEVEE.
WAITING = "shaders compiled for a look"  # mainthread.background_work while the agent thread waits.

# {session_uid: signature} of the materials (and worlds) as the last EEVEE look drew them. The
# look compiled them, so the same signature needs nothing; Blender keeps the passes while the
# material lives unchanged.
_seen: dict = {}
_engine_ready = False  # An EEVEE look ran this session: EEVEE's own shaders are compiled.
_deferring = False     # Inside deferring(): the caller runs ToolResult.then on its own thread.


@contextlib.contextmanager
def deferring():
    """Main thread, around a tool call whose caller waits in ToolResult.then on another thread
    (agent._run_tool_on_main). Elsewhere (harness scripts on the main thread, a worker's Blender)
    a look is taken at once, as before: waiting there would hold the very thread the jobs need."""
    global _deferring
    _deferring = True
    try:
        yield
    finally:
        _deferring = False


@dataclasses.dataclass
class Plan:
    signatures: dict                                      # {session_uid: signature} of all this look draws and checked
    stale: list = dataclasses.field(default_factory=list)   # the IDs to preview first, in order
    warmed: list = dataclasses.field(default_factory=list)  # names of those whose previews started


def _editable(idb) -> bool:
    """A preview job renders only local, editable data (render_preview.cc skips linked IDs) and keeps
    a preview the user set by hand: Blender would replace it."""
    if idb is None or idb.library is not None or getattr(idb, "override_library", None) is not None:
        return False
    preview = getattr(idb, "preview", None)
    return not (preview is not None and preview.is_image_custom)


def signature(idb) -> int:
    """What the generated shader depends on, and more: nodes, their settings and unlinked values,
    links, node groups, the material's settings. Values are included because some decide the code
    (a Principled input above zero adds a closure); a spare warm-up costs a preview, a missed one a
    freeze."""
    from . import scene_diff
    if getattr(idb, "id_type", "") == "WORLD":
        parts = [scene_diff.node_tree(idb.node_tree, idb.color)]
    else:
        parts = [scene_diff._material(idb)]
    seen, trees = set(), [idb.node_tree] if idb.node_tree is not None else []
    while trees:
        for node in trees.pop().nodes:
            group = getattr(node, "node_tree", None) if node.type == "GROUP" else None
            if group is not None and group.session_uid not in seen:
                seen.add(group.session_uid)
                parts.append((group.name, scene_diff.node_tree(group)))
                trees.append(group)
    return hash(repr(parts))


def _materials(objects) -> list:
    found, uids = [], set()
    for obj in objects:
        for slot in obj.material_slots:
            material = slot.material
            if material is not None and material.session_uid not in uids and not material.is_grease_pencil:
                uids.add(material.session_uid)
                found.append(material)
    return found


def eevee_look(style: str, angle: str) -> bool:
    """The user's own view is drawn as they have it; their viewport compiles its own shaders."""
    return style in EEVEE_STYLES and angle != "user"


def prepare(style: str = "material", angle: str = "three_quarter", focus=None) -> "Plan | None":
    """Main thread, quick: which IDs this look draws that need a preview first. None when it needs
    no warm-up at all: not an EEVEE look, a background Blender (no previews, no GPU draws), or a
    scene engine whose previews are not EEVEE's. The preview renders with the scene's engine
    (render_preview.cc copies scene->r.engine): Cycles would compile nothing the look uses, and
    Workbench renders no material previews."""
    if bpy.app.background or not eevee_look(style, angle):
        return None
    scene = bpy.context.scene
    if scene.render.engine not in EEVEE_ENGINES:
        return None
    from . import tools
    visible = [o for o in scene.objects if o.visible_get() and o.type not in tools._HIDDEN_TYPES]
    wanted = set(focus or ())
    visible.sort(key=lambda o: o.name not in wanted)  # Stable: the focus first, then scene order.
    candidates = _materials(visible)[:MAX_CHECKED]
    if style == "rendered" and scene.world is not None:
        candidates.append(scene.world)  # Drawn with the scene's world; Material Preview uses a studio light.
    plan = Plan({})
    for idb in candidates:
        plan.signatures[idb.session_uid] = mark = signature(idb)
        if _seen.get(idb.session_uid) != mark and _editable(idb):
            plan.stale.append(idb)
    if not plan.stale and not _engine_ready and _editable(scene.world):
        # Nothing new to draw, but EEVEE has never drawn in this session: its own shaders are most
        # of a first look (FREEZE §2). Any EEVEE preview compiles them; the world's is always there.
        plan.stale.append(scene.world)
    del plan.stale[MAX_WARMED:]
    return plan


def start(idb) -> bool:
    """Main thread: one preview render job for idb, the way Generate Preview does it. Called from a
    script the operator pushes no undo step; it stops other preview jobs, so it runs when none is."""
    from . import tools
    try:
        window = tools._view3d_override()["window"]
        with bpy.context.temp_override(window=window, id=idb):
            return "FINISHED" in bpy.ops.ed.lib_id_generate_preview()
    except (RuntimeError, ReferenceError, TypeError, tools.ToolError) as ex:  # Removed meanwhile, say.
        print(f"Loopcut: could not start compiling shaders for a look: {ex}")
        return False


def busy() -> bool:
    """Main thread: a preview job or a shader compile is still running (any, not only ours: the
    user's own viewport compiling the same material counts too)."""
    return bpy.app.is_job_running("RENDER_PREVIEW") or bpy.app.is_job_running("SHADER_COMPILATION")


def wait(is_cancelled, on_main, deadline: float, clock=time.monotonic, sleep=time.sleep) -> bool:
    """Agent thread: until nothing compiles for SETTLE_SECONDS, never past `deadline` (clock time).
    Each check is one short main-thread task, so the interface stays live and the freeze log stays
    quiet. False at the deadline. Stop raises llm.Cancelled."""
    from . import llm, mainthread
    idle_since = None
    check = mainthread.named("a check on the look's shaders", lambda: busy())
    while True:
        if is_cancelled():
            raise llm.Cancelled()
        now = clock()
        if on_main(check):
            idle_since = None
        elif idle_since is None:
            idle_since = now
        elif now - idle_since >= SETTLE_SECONDS:
            return True
        if now >= deadline:
            return False
        sleep(POLL_SECONDS)


def warm(plan: Plan, is_cancelled, on_main, limit: float = WAIT_SECONDS, clock=time.monotonic,
         sleep=time.sleep) -> "float | None":
    """Agent thread: preview plan.stale one at a time, each after the last has finished, then wait
    for the compiler to settle. A late pump meanwhile (a job's own hitch) is logged as happening
    during this wait. Seconds taken, or None when `limit` ran out (logged; the look goes ahead)."""
    from . import mainthread
    started = clock()
    deadline = started + limit
    mainthread.background_work = WAITING
    try:
        for idb in plan.stale:
            if not wait(is_cancelled, on_main, deadline, clock, sleep):
                break
            if on_main(mainthread.named("starting a look's shader preview", functools.partial(start, idb))):
                plan.warmed.append(getattr(idb, "name", "?"))
        else:
            if wait(is_cancelled, on_main, deadline, clock, sleep):
                return clock() - started
        print(f"Loopcut: the look's shaders were still compiling after {limit:.0f} s; taking it anyway "
              f"(the interface may pause while the rest compile).", flush=True)
        return None
    finally:
        mainthread.background_work = ""


def remember(plan: "Plan | None") -> None:
    """Main thread, after an EEVEE look succeeded: it compiled everything it drew."""
    global _engine_ready
    if plan is not None:
        _seen.update(plan.signatures)
        _engine_ready = True


def before_look(capture, style: str = "material", angle: str = "three_quarter", focus=None,
                name: str = "the capture_viewport tool"):
    """Main thread. capture() (a look returning a ToolResult) right away, or, when shaders it needs
    should compile in preview jobs first, a ToolResult whose then() runs them on the agent thread
    and captures afterwards (agent._run_tool_on_main calls it). `name` is the capture task's name in
    the freeze log. A ToolError from capture() raises here, and is a failed result from then().
    Outside deferring(), capture() at once."""
    from . import mainthread, tools
    if not _deferring:
        return capture()
    try:
        plan = prepare(style, angle, focus)
    except Exception as ex:  # A saving, never a reason to lose the look; capture() reports a missing viewport.
        print(f"Loopcut: could not prepare the look's shaders: {ex!r}")
        plan = None
    if plan is None or not plan.stale:
        result = capture()
        remember(plan)
        return result

    def look():
        try:
            result = capture()
        except tools.ToolError as ex:
            return tools.ToolResult(str(ex), ok=False)
        remember(plan)
        return result

    def then(is_cancelled, on_main):
        warm(plan, is_cancelled, on_main)
        return on_main(mainthread.named(name, look))

    return tools.ToolResult("Compiling shaders before the look.", then=then)


# ------------------------------------------------------------------ Subprocess compilation

SUBPROCESS_PLATFORMS = ("win32", "linux")  # sys.platform; BLI_SUBPROCESS_SUPPORT is _WIN32 or __linux__.


def default_to_subprocess(preferences, addon_prefs, platform: str = sys.platform,
                          prefs_saved: bool = True) -> str:
    """Once per profile, on Windows and Linux: Blender's Thread shader compilation (the default)
    becomes Subprocess. That compiles EEVEE's engine shaders and preview renders in parallel and
    keeps Blender's own on-disk shader cache, which Thread never writes; it takes effect after a
    restart. A user who already chose Subprocess is left alone, and addon_prefs remembers it was
    done, so switching back to Thread is never undone. macOS has no such option (Metal; the
    preference is hidden there and compiled out).

    The change marks the preferences modified, so Blender's Save on Exit keeps it, or not, as the
    user has set; nothing is saved here. Before the first save of a new profile (prefs_saved False)
    the modified mark is put back: an exit before the first-run setup must not write preferences
    behind it, and that setup's own save keeps the change. Returns what was done: "subprocess",
    "kept" or ""."""
    if not platform.startswith(SUBPROCESS_PLATFORMS) or addon_prefs is None:
        return ""
    if getattr(addon_prefs, "shader_compilation_once", ""):
        return ""
    system = preferences.system
    dirty = preferences.is_dirty
    if system.shader_compilation_method == "THREAD":
        system.shader_compilation_method = "SUBPROCESS"
        done = "subprocess"
    else:
        done = "kept"
    addon_prefs.shader_compilation_once = done
    if prefs_saved:
        preferences.is_dirty = True  # An add-on property alone does not mark them; the flag must persist too.
    else:
        preferences.is_dirty = dirty
    return done


def _prefs_saved() -> bool:
    config = bpy.utils.user_resource("CONFIG")
    return bool(config) and os.path.isfile(os.path.join(config, "userpref.blend"))


def _apply_default() -> None:
    from . import settings
    try:
        done = default_to_subprocess(bpy.context.preferences, settings.preferences(), prefs_saved=_prefs_saved())
    except (AttributeError, TypeError, RuntimeError) as ex:  # An older Blender without the option.
        print(f"Loopcut: could not set shader compilation to Subprocess: {ex}")
        return
    if done == "subprocess":
        print("Loopcut: shader compilation set to Subprocess (Preferences > System); it applies after a restart.")


def register() -> None:
    """The Loopcut build only (__init__): a stock Blender's preferences are the user's own."""
    if not bpy.app.background and not bpy.app.timers.is_registered(_apply_default):
        bpy.app.timers.register(_apply_default, first_interval=0.0)


def unregister() -> None:
    if bpy.app.timers.is_registered(_apply_default):
        bpy.app.timers.unregister(_apply_default)
