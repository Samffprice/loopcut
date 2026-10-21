"""Capture C11 panel fixtures in a windowed Blender, without signing in or calling HTTP.

    /path/to/Blender --factory-startup -p 0 0 1440 900 \
        --python loopcut/harness/c11_shots.py -- --state all --out /tmp/c11-shots

Writes <state>.png, <state>.panel.png and <state>.json (plus .splash.png for onboarding),
using shot.py's settle/double-draw and screenshot/crop sequence. Widths are logical pixels (scaled for Blender's UI scale).
Use --state narrow_job for one fixture, or --keep-open to inspect the last state by hand.
Run in a throwaway window: the script replaces panel service functions with fixtures.
"""

import argparse
import os
import sys
import tempfile
import traceback
from pathlib import Path


STATES = ("signed_out", "first_run", "hints_dismissed", "coming_soon", "coming_soon_clicked",
          "coming_soon_collapsed", "coming_soon_signed_out", "cloud_message", "narrow_job",
          "chatgpt_waiting", "loopcut_waiting", "sign_in_error", "running_extras",
          "onboarding_account", "onboarding_account_failed", "onboarding_connect", "onboarding_connect_chatgpt_error")


def fixtures(state):
    """Canned public UI data only; no credentials, service catalogs or model calls."""
    extras = [{"name": f"future_{n}", "title": title, "state": "ready", "category": category}
              for n, (title, category) in enumerate([
                  ("Texture cleanup", "Images"), ("Image variations", "Images"),
                  ("Voice line", "Audio"), ("Music", "Audio"), ("Sound effects", "Audio"),
                  ("Camera motion", "Animation"), ("Character motion", "Animation"),
                  ("Lip sync", "Animation"), ("Cloud render", "Rendering"),
                  ("Render review", "Rendering"), ("Material preview", "")])]
    job = {"id": "job_fixture", "title": "Preparing a detailed model of the selected prop for the scene",
           "pct": 43, "message": "Preparing geometry; you can keep editing the scene.", "cancelable": True}
    result = {name: {"items": [], "ui": {"hints_dismissed": True, "needs_setup": False}} for name in STATES}
    result["signed_out"]["ui"].update(needs_setup=True, hints_dismissed=False)
    result["first_run"]["ui"]["hints_dismissed"] = False
    result["coming_soon"]["ui"]["coming_soon"] = extras
    result["coming_soon_clicked"]["ui"]["coming_soon"] = [{**r, "state": "thanks"} for r in extras]
    result["coming_soon_collapsed"]["ui"].update(coming_soon=extras, coming_soon_expanded=False)
    result["coming_soon_signed_out"]["ui"]["coming_soon"] = [
        {**r, "state": "notify", "notify_sign_in": True} if n == 0 else r for n, r in enumerate(extras)]
    for name, step in (("onboarding_account", "account"), ("onboarding_account_failed", "account"),
                       ("onboarding_connect", "connect"), ("onboarding_connect_chatgpt_error", "connect")):
        result[name]["ui"]["onboarding_step"] = step
    result["onboarding_account_failed"]["ui"]["sign_in"] = {
        "loopcut": {"status": "error", "error": "HTTP 404: accounts unavailable"}}
    result["onboarding_connect_chatgpt_error"]["ui"]["sign_in"] = {"chatgpt": {  # chatgpt_auth.SIGN_IN_MESSAGES
        "status": "error", "error": "ChatGPT didn't let this account share its plan with Loopcut."}}
    result["cloud_message"]["ui"]["cloud_message"] = "Update Loopcut to use cloud tools"
    result["narrow_job"].update(items=[state.item_user("Prepare a detailed model of this prop.")])
    result["narrow_job"]["ui"]["cloud_jobs"] = [job]
    for route in ("chatgpt", "loopcut"):
        result[route + "_waiting"]["ui"].update(needs_setup=True, sign_in={route: {
            "status": "waiting", "url": "https://sign-in.example/fixture", "user_code": "TEST"}})
    result["sign_in_error"]["ui"].update(needs_setup=True, sign_in={"chatgpt": {
        "status": "error", "error": "Sign-in timed out. Try Sign in with ChatGPT again, or use another route."}})
    assistant = state.item_assistant()
    assistant["text"] = "I'm checking the lights and camera before making changes."
    result["running_extras"].update(busy=True, items=[state.item_user("Add soft studio lighting."), assistant])
    result["running_extras"]["ui"].update(coming_soon=extras, cloud_jobs=[job])
    return result


def main():
    import bpy

    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", choices=("all", *STATES), default="all")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--width", type=int, default=420)
    parser.add_argument("--narrow-width", type=int, default=240)
    parser.add_argument("--keep-open", action="store_true")
    args = parser.parse_args(argv)
    if min(args.width, args.narrow_width) < 200:
        parser.error("panel widths must be at least 200 logical pixels")
    args.out.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.mkdtemp(prefix="loopcut-c11-")
    # Override, rather than inherit, so fixtures cannot read the user's saved accounts.
    os.environ["LOOPCUT_DATA_DIR"] = temporary + "/data"
    os.environ["LOOPCUT_CONFIG_DIR"] = temporary + "/config"
    os.environ["LOOPCUT_CHATGPT_DIR"] = temporary + "/chatgpt"
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import checkout
    checkout.use()
    import loopcut
    from loopcut import account, chatgpt_auth, chatgpt_ui, cloud, cloud_jobs, onboarding, settings, state, update
    from loopcut.ui import host

    canned = fixtures(state)
    names = list(STATES) if args.state == "all" else [args.state]
    current = {"index": 0, "ui": {}}
    window = bpy.context.window_manager.windows[0]
    base_ui = dict(state.ui)

    cloud.register = cloud.panel_seen = lambda: None
    cloud.coming_soon = lambda: current["ui"].get("coming_soon", [])
    cloud.ui_message = lambda: current["ui"].get("cloud_message", "")
    cloud_jobs.ui_rows = lambda: current["ui"].get("cloud_jobs", [])
    account.signed_in = lambda: False
    chatgpt_auth.status = lambda: {"signed_in": False}
    account.status = lambda: current["ui"].get("sign_in", {}).get("loopcut", {"status": "idle", "error": ""})
    chatgpt_ui.status = lambda: current["ui"].get("sign_in", {}).get("chatgpt", {"status": "idle", "error": ""})
    onboarding.hints_dismissed = lambda: current["ui"].get("hints_dismissed", False)
    onboarding.dismiss_hints = lambda: None
    update.register = lambda: None
    update.banner = lambda: None
    host._config_cached = lambda: ("fast", current["ui"].get("needs_setup", False), 24000)
    host._checkpoint_statuses = lambda session: {}
    settings.model_label = lambda model: "Fast"

    # Onboarding fixtures are last so one splash can repaint between them without synthetic input.
    def draw_splash(menu, context):
        onboarding.draw_splash(menu.layout, context, first_run=False)
        current["popup"] = context.region_popup

    bpy.types.WM_MT_splash.draw = draw_splash
    bpy.types.WM_MT_splash_quick_setup.draw = draw_splash

    def guarded(fn):
        def tick():
            try:
                return fn()
            except Exception:
                traceback.print_exc()
                sys.stdout.flush()
                sys.stderr.flush()
                os._exit(1)
        return tick

    def later(fn, delay=.3):
        bpy.app.timers.register(guarded(fn), first_interval=delay)

    def open_panel():
        panel = host.find_panel(window)
        if panel is None:
            panel = max((a for a in window.screen.areas if a.type == "VIEW_3D"), key=lambda a: a.x)
            with bpy.context.temp_override(window=window, area=panel):
                bpy.ops.loopcut.open()
        current["panel"] = panel
        apply_fixture()

    def split():
        if host.find_panel(window):
            later(open_panel)
            return
        view = next(a for a in window.screen.areas if a.type == "VIEW_3D")
        region = next(r for r in view.regions if r.type == "WINDOW")
        with bpy.context.temp_override(window=window, area=view, region=region):
            bpy.ops.screen.area_split(direction="VERTICAL", factor=.7)
        later(open_panel)

    def apply_fixture():
        name = names[current["index"]]
        fixture = canned[name]
        current["ui"] = fixture["ui"]
        session = state.reset()
        session.update({k: v for k, v in fixture.items() if k != "ui"})
        state.ui.clear()
        state.ui.update({**base_ui, **fixture["ui"], "view": "chat", "hover": None})
        host._runtime.pop("cloud_panel", None)
        panel = current["panel"]
        scale = bpy.context.preferences.system.ui_scale
        width = round((args.narrow_width if name == "narrow_job" else args.width) * scale)
        delta = panel.width - width
        if abs(delta) > 4 and not current["ui"].get("onboarding_step"):  # The splash's width is its own.
            with bpy.context.temp_override(window=window, area=panel):
                result = bpy.ops.screen.area_move(x=panel.x - 1, y=panel.y + panel.height // 2, delta=delta)
            if result != {"FINISHED"}:
                raise RuntimeError("Could not resize the panel edge; run with the panel docked on the right")
        step = current["ui"].get("onboarding_step")
        if step:
            onboarding._state.update(step=step, own_key=False)
            if "popup" not in current:
                with bpy.context.temp_override(window=window):
                    bpy.ops.wm.splash('INVOKE_DEFAULT')
            else:
                current["popup"].tag_refresh_ui()
                current["popup"].tag_redraw()
        panel.tag_redraw()
        later(settle, .5)

    def settle():
        current["panel"].tag_redraw()
        if "popup" in current:
            current["popup"].tag_redraw()
        later(present, .15)

    def present():
        current["panel"].tag_redraw()
        if "popup" in current:
            current["popup"].tag_redraw()
        later(capture, .15)

    def capture():
        # Same window screenshot and bottom-origin image crop as shot.py.
        import numpy as np
        panel = current["panel"]
        name = names[current["index"]]
        prefix = args.out / name
        host.dump_display(str(prefix.with_suffix(".json")))
        with bpy.context.temp_override(window=window):
            bpy.ops.screen.screenshot(filepath=str(prefix.with_suffix(".png")))
        image = bpy.data.images.load(str(prefix.with_suffix(".png")))
        width, height = image.size
        pixels = np.empty(width * height * 4, dtype=np.float32)
        image.pixels.foreach_get(pixels)
        crop = pixels.reshape(height, width, 4)[panel.y:panel.y + panel.height, panel.x:panel.x + panel.width]
        cropped = bpy.data.images.new("C11 panel", crop.shape[1], crop.shape[0], alpha=True)
        cropped.pixels.foreach_set(np.ascontiguousarray(crop).ravel())
        cropped.filepath_raw = str(prefix.with_suffix(".panel.png"))
        cropped.file_format = "PNG"
        cropped.save()
        bpy.data.images.remove(cropped)
        if current["ui"].get("onboarding_step"):
            popup = current["popup"]
            crop = pixels.reshape(height, width, 4)[popup.y:popup.y + popup.height, popup.x:popup.x + popup.width]
            cropped = bpy.data.images.new("C11 splash", crop.shape[1], crop.shape[0], alpha=True)
            cropped.pixels.foreach_set(np.ascontiguousarray(crop).ravel())
            cropped.filepath_raw = str(prefix.with_suffix(".splash.png"))
            cropped.file_format = "PNG"
            cropped.save()
            bpy.data.images.remove(cropped)
        bpy.data.images.remove(image)
        print(f"C11 SHOT OK: {prefix.with_suffix('.panel.png')}")
        current["index"] += 1
        if current["index"] < len(names):
            later(apply_fixture)
        elif not args.keep_open:
            bpy.ops.wm.quit_blender()
        return None

    bpy.context.preferences.view.show_splash = False
    loopcut.register()
    later(split, .5)


if __name__ == "__main__":
    main()
