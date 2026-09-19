"""First run of the Loopcut build in a throwaway home folder: the import step shows, importing
copies stock Blender's settings without touching them, and the later steps draw.

Run through scripts/onboarding_check.sh, which builds the home folder and points HOME at it.
Uses the bundled add-on (the splash looks it up by its bundled name): `ninja -C build/lite install`
first. Writes out/onboarding/<stage>.png.

    --stage first     no Loopcut preferences yet: screenshot, import, screenshot again
    --stage fresh     no preferences: click Start Fresh, then Blender's Continue (needs
                      --enable-event-simulate); the model step must follow
    --stage connect   preferences exist: the model step
    --stage privacy   the privacy step
    --stage done      the closing lines above the normal splash
"""

import argparse
import hashlib
import os
import sys
import traceback
from pathlib import Path

import bpy

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
parser = argparse.ArgumentParser()
parser.add_argument("--stage", choices=("first", "fresh", "connect", "privacy", "done"), required=True)
parser.add_argument("--out", type=Path, required=True)
parser.add_argument("--click-y", type=int, nargs="*", default=[], help="fresh: window y of Start Fresh, then Continue")
ARGS = parser.parse_args(argv)

HOME = Path(os.environ["HOME"])
USER = Path(bpy.utils.resource_path("USER"))


def fail(message: str) -> None:
    print(f"HARNESS FAILED: {message}", file=sys.stderr)
    sys.stderr.flush()
    os._exit(1)


def check(condition, message: str) -> None:
    if not condition:
        fail(message)


def digest(folder: Path) -> str:
    sha = hashlib.sha256()
    for path in sorted(folder.rglob("*")):
        sha.update(str(path.relative_to(folder)).encode())
        if path.is_file():
            sha.update(path.read_bytes())
    return sha.hexdigest()


def shot(name: str) -> None:
    ARGS.out.mkdir(parents=True, exist_ok=True)
    with bpy.context.temp_override(window=bpy.context.window_manager.windows[0]):
        bpy.ops.screen.screenshot(filepath=str(ARGS.out / f"{name}.png"))


def steps(*functions):
    """Run each function on its own timer tick, 1.5 s apart, so the splash has drawn in between."""
    queue = list(functions)

    def tick():
        try:
            queue.pop(0)()
            # A screenshot reads the frame before the last one drawn, so draw once more per step.
            for area in bpy.context.window_manager.windows[0].screen.areas:
                area.tag_redraw()
        except Exception:
            traceback.print_exc()
            fail("a step raised")
        return 1.5 if queue else None
    # Persistent: importing loads the startup file, which drops ordinary timers.
    bpy.app.timers.register(tick, first_interval=2.0, persistent=True)


def quit_ok():
    print("ONBOARDING CHECK OK:", ARGS.stage)
    sys.stdout.flush()
    os._exit(0)


check(HOME in USER.parents and "Loopcut" in USER.parts, f"not sandboxed, user folder is {USER}")
import loopcut.onboarding as onboarding  # noqa: E402  (the bundled copy, enabled at startup)

if ARGS.stage == "first":
    from loopcut import blender_import
    stock = blender_import.stock_root()
    check(HOME in stock.parents, f"stock root {stock} is outside the sandbox")
    check(not (USER / "config" / "userpref.blend").exists(), "Loopcut preferences already exist")
    before = digest(stock)

    def import_page():
        check(onboarding._state["step"] == "import", f"step is {onboarding._state['step']}, expected import")
        shot("1-import")
        with bpy.context.temp_override(window=bpy.context.window_manager.windows[0]):
            check(bpy.ops.loopcut.import_blender_settings() == {"FINISHED"}, "import did not run")

    def imported():
        check((USER / "config" / "userpref.blend").is_file(), "preferences were not copied")
        check(bpy.context.preferences.view.show_tooltips_python, "the imported preference is not in effect")
        check("loopcut" in bpy.context.preferences.addons, "the bundled add-on is not enabled after import")
        check(digest(stock) == before, "stock Blender's folder changed")
        check(onboarding._state["imported"] is not None, "import result not recorded")
        from loopcut.ui import host
        check(host.find_panel(bpy.context.window_manager.windows[0]), "no Loopcut panel after import")
        shot("2-connect")

    steps(import_page, imported, quit_ok)
elif ARGS.stage == "fresh":
    check(not (USER / "config" / "userpref.blend").exists(), "Loopcut preferences already exist")

    def click(x: int, y: int):
        window = bpy.context.window_manager.windows[0]
        for kind, value in (("MOUSEMOVE", "NOTHING"), ("LEFTMOUSE", "PRESS"), ("LEFTMOUSE", "RELEASE")):
            window.event_simulate(type=kind, value=value, x=x, y=y)

    # Button centres measured from 1-import.png and fresh-1-setup.png at -p 0 0 1440 900, 2x display.
    def start_fresh():
        check(onboarding._state["step"] == "import", f"step is {onboarding._state['step']}, expected import")
        click(1440, ARGS.click_y[0])

    def quick_setup():
        check(onboarding._state["step"] == "setup", f"step is {onboarding._state['step']} after Start Fresh")
        shot("fresh-1-setup")
        if len(ARGS.click_y) > 1:
            click(1440, ARGS.click_y[1])

    def after_continue():
        shot("fresh-2-after")
        if len(ARGS.click_y) > 1:
            check((USER / "config" / "userpref.blend").is_file(), "Continue did not save preferences")
            check(onboarding._state["step"] in ("connect", "done"), f"step is {onboarding._state['step']} after Continue")

    steps(start_fresh, quick_setup, after_continue, quit_ok)
else:
    check((USER / "config" / "userpref.blend").is_file(), "run --stage first before this one")
    onboarding._state.update(step=ARGS.stage, imported=(5, 2), failed=["old_rig_tools", "sketchy_io"])
    steps(lambda: shot({"connect": "2b-connect", "privacy": "3-privacy", "done": "4-done"}[ARGS.stage]), quit_ok)
