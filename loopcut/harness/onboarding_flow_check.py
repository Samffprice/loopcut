"""The 0.2.6 first-run setup in the Loopcut build: modal, resumable, sign-in required. One stage per launch,
in a sandbox made with BLENDER_USER_RESOURCES (never a different HOME: a sign-in can open the browser):

    export BLENDER_USER_RESOURCES=$(mktemp -d) LOOPCUT_ENV_FILE=$(mktemp) LOOPCUT_SERVICE_URL=http://127.0.0.1:9
    export LOOPCUT_CHATGPT_DIR=$BLENDER_USER_RESOURCES/chatgpt  # Else your own ChatGPT sign-in shows.
    Loopcut -p 0 0 1440 900 --enable-event-simulate --python loopcut/harness/onboarding_flow_check.py -- \\
        --stage fresh --out out/onboarding-flow

    --stage fresh     no preferences: the splash is modal (an outside click, Esc and the splash image leave it
                      open), Start Fresh and Blender's Continue lead to the account step; signed in (a fixture
                      key), Continue leads to How Loopcut works; then quits half way
    --stage import    fake stock settings (LOOPCUT_STOCK_BLENDER_DIR, never the real ones): Import Blender
                      Settings rereads preferences, which re-enables every add-on; Loopcut comes back whole and
                      the account step is drawn with its buttons, modal
    --stage missing   the sign-in operator unregistered: the setup is not modal and shows a way out, the
                      watchdog leaves it closed, and it heals once the operator is back; then a step drawn with
                      no operator button at all: the C side refuses to make it modal, so a click closes it
    --stage resume    same sandbox: preferences exist, the saved step (How Loopcut works) comes back, still modal
    --stage failed    the service is unreachable: the sign-in fails, "Continue without an account" leads to the
                      model step, and finishing leaves the setup to come back
    --stage existing  a sandbox with preferences and a Loopcut key but no progress file (an install from before
                      the setup): the whole setup from the account, modal, each step showing what it has (signed
                      in, its model connected), every primary pressed once; privacy recorded as seen
    --stage existing-again  the same sandbox relaunched: the normal splash, never the setup again
    --stage screens   every screen and state with fixture data (signed in, a fresh trial, live cloud tools),
                      each checked to fit the window and screenshotted for review (--suffix names the size);
                      then the photo card: the setup closes, the panel docks and the request goes out with the
                      photo (to the unreachable service: no credits); no sign-in. With fake stock settings in
                      LOOPCUT_STOCK_BLENDER_DIR the welcome screen is first.
    --stage clicks    the same fixture, every kind of button (primary, raised, checkbox, its label, ← Back,
                      Start) pressed ONCE after resting on it 1.5 s, as people click, some with the splash rebuilt
                      meanwhile: each acts on the first click

LOOPCUT_SERVICE_URL must point nowhere: the failed sign-in depends on it and no stage may reach the real
service. Writes <out>/<stage>-<n>-<screen>.png and prints ONBOARDING FLOW OK: <stage>.
"""

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path

import bpy

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
parser = argparse.ArgumentParser()
parser.add_argument("--stage", choices=("fresh", "import", "missing", "resume", "failed", "existing", "existing-again",
                                        "screens", "clicks"), required=True)
parser.add_argument("--out", type=Path, required=True)
parser.add_argument("--suffix", default="", help="added to each screenshot's name, e.g. the window size")
ARGS = parser.parse_args(argv)

USER = Path(bpy.utils.resource_path("USER"))
SANDBOX = os.environ.get("BLENDER_USER_RESOURCES", "")


def fail(message: str) -> None:
    print(f"HARNESS FAILED: {message}", file=sys.stderr)
    sys.stderr.flush()
    os._exit(1)


def check(condition, message: str) -> None:
    if not condition:
        fail(message)


check(SANDBOX and Path(SANDBOX) in [USER, *USER.parents], f"not sandboxed: the user folder is {USER}")
check(os.environ.get("LOOPCUT_SERVICE_URL", "").startswith("http://127.0.0.1"), "LOOPCUT_SERVICE_URL must be local")
check(os.environ.get("LOOPCUT_CHATGPT_DIR", "").startswith(SANDBOX), "LOOPCUT_CHATGPT_DIR must be in the sandbox")
import loopcut.onboarding as onboarding  # noqa: E402  (the bundled copy, enabled at startup)
from loopcut import account, config, settings  # noqa: E402

PREFS = USER / "config" / "userpref.blend"
shots = []


def wm():
    return bpy.context.window_manager  # Never kept: loading the startup file replaces it.


def window():
    return wm().windows[0]


def shot(name: str) -> None:
    """A screenshot of the window as it is now. Screenshots read the frame before the last one drawn, and since the
    setup rebuilds only on a change nothing else draws a frame: draw two first, or the file shows the screen before."""
    ARGS.out.mkdir(parents=True, exist_ok=True)
    shots.append(name)
    with bpy.context.temp_override(window=window()):
        bpy.ops.wm.redraw_timer(type="DRAW_WIN_SWAP", iterations=2)
        suffix = f"-{ARGS.suffix}" if ARGS.suffix else ""
        bpy.ops.screen.screenshot(filepath=str(ARGS.out / f"{ARGS.stage}-{len(shots)}-{name}{suffix}.png"))


def refresh() -> None:
    """Rebuild the splash after a change made outside it, as a click inside it would."""
    region = onboarding._state["region"]
    if region is not None and onboarding._region_exists(region):
        region.tag_refresh_ui()
        region.tag_redraw()
    for area in window().screen.areas:
        area.tag_redraw()


def call(operator, **props) -> None:
    with bpy.context.temp_override(window=window()):
        result = operator(**props)
    check(result == {"FINISHED"}, f"{operator} returned {result}")
    refresh()


def simulate(kind: str, value: str, x: int, y: int) -> None:
    window().event_simulate(type=kind, value=value, x=x, y=y)


def try_to_dismiss() -> None:
    """An outside click in a corner, then Escape, then Enter: a modal splash ignores all three."""
    simulate("MOUSEMOVE", "NOTHING", 40, 40)
    simulate("LEFTMOUSE", "PRESS", 40, 40)
    simulate("LEFTMOUSE", "RELEASE", 40, 40)
    simulate("ESC", "PRESS", 40, 40)
    simulate("ESC", "RELEASE", 40, 40)
    simulate("RET", "PRESS", 40, 40)
    simulate("RET", "RELEASE", 40, 40)


def at(cx: int, cy: int) -> tuple[int, int]:
    """A point given in the popup's own pixels from its top-left, as measured on the review crops at
    1440x900, in window pixels."""
    region = onboarding._state["region"]
    return region.x + cx, region.y + region.height - cy

def hover(cx: int, cy: int):
    def run():
        simulate("MOUSEMOVE", "NOTHING", *at(cx, cy))
    return run

def press(cx: int, cy: int):
    def run():
        x, y = at(cx, cy)
        simulate("LEFTMOUSE", "PRESS", x, y)
        simulate("LEFTMOUSE", "RELEASE", x, y)
    return run

def one_click(name: str, cx: int, cy: int, happened, rebuild: bool = False):
    """As a person clicks: onto the button, a pause (1.5 s, across the setup's 1 s timer), ONE press and
    release; the next tick checks it acted. rebuild: the splash is rebuilt during the pause, as when what it
    shows changes (a sign-in finishing), which once left the button inactive until the pointer moved."""
    def verify():
        check(happened(), f"one click on {name} did nothing")
        print("ONE CLICK OK:", name, "(rebuilt while hovered)" if rebuild else "")
    return [hover(cx, cy), *([onboarding._refresh_splash] if rebuild else []), press(cx, cy), verify]


def primary_center() -> tuple[int, int]:
    """Where the step's filled primary button is, read off a screenshot: its middle, in window pixels."""
    import numpy as np
    path = ARGS.out / "primary-probe.png"
    ARGS.out.mkdir(parents=True, exist_ok=True)
    with bpy.context.temp_override(window=window()):
        bpy.ops.wm.redraw_timer(type="DRAW_WIN_SWAP", iterations=2)
        bpy.ops.screen.screenshot(filepath=str(path))
    image = bpy.data.images.load(str(path))
    w, h = image.size
    px = np.array(image.pixels[:], dtype=np.float32).reshape(h, w, 4)[:, :, :3] * 255  # Rows from the bottom.
    bpy.data.images.remove(image)
    path.unlink()
    region = onboarding._state["region"]
    band = px[region.y:region.y + region.height, region.x + 60:region.x + region.width - 60]
    rows = np.nonzero((np.abs(band - [242, 239, 233]).max(axis=2) < 6).mean(axis=1) > 0.8)[0]
    check(len(rows) > 0, "no filled primary button on screen")
    return region.x + region.width // 2, region.y + int(rows.mean())


def one_click_primary(name: str, happened):
    """one_click on the step's primary button, wherever it is."""
    spot = {}

    def locate():
        spot["xy"] = primary_center()
        simulate("MOUSEMOVE", "NOTHING", *spot["xy"])

    def click():
        simulate("LEFTMOUSE", "PRESS", *spot["xy"])
        simulate("LEFTMOUSE", "RELEASE", *spot["xy"])

    def verify():
        check(happened(), f"one click on {name} did nothing")
        print("ONE CLICK OK:", name)
    return [locate, click, verify]


def expect_modal(step: str) -> None:
    check(onboarding._state["step"] == step, f"step is {onboarding._state['step']}, expected {step}")
    check(wm().splash_modal, "the splash is not modal during the setup")
    check(onboarding._splash_open(), "the splash is not open")


def progress() -> dict:
    return json.loads((USER / "config" / "loopcut" / onboarding.PROGRESS_FILE).read_text())


def steps(*functions):
    """Each function on its own timer tick, 1.5 s apart, so the splash has drawn in between."""
    queue = list(functions)

    def tick():
        try:
            queue.pop(0)()
            for area in window().screen.areas:  # A screenshot reads the frame before the last one drawn.
                area.tag_redraw()
        except Exception:
            traceback.print_exc()
            fail("a step raised")
        return 1.5 if queue else None
    bpy.app.timers.register(tick, first_interval=3.0, persistent=True)


def done():
    print("ONBOARDING FLOW OK:", ARGS.stage, *shots)
    sys.stdout.flush()
    os._exit(0)  # Quitting half way, as a user would: nothing saved on the way out.


if ARGS.stage == "fresh":
    check(not PREFS.exists(), "preferences already exist: use a new sandbox")

    def welcome():
        check(onboarding._state["step"] in ("import", "setup"), f"step is {onboarding._state['step']}")
        expect_modal(onboarding._state["step"])
        shot("welcome")

    def after_dismiss():
        expect_modal(onboarding._state["step"])
        shot("still-open")
        if onboarding._state["step"] == "import":
            call(bpy.ops.loopcut.onboarding_step, step="setup")  # Start Fresh

    def quick_setup():
        expect_modal("setup")
        shot("quick-setup")
        with bpy.context.temp_override(window=window()):
            bpy.ops.wm.save_userpref()  # Blender's own Continue.
        refresh()

    def account_step():
        expect_modal("account")
        check(progress() == {"version": 1, "step": "account", "done": False}, f"progress is {progress()}")
        shot("account")
        from loopcut import credentials
        credentials.store(account.BASE_URL, "lc_fixture_not_a_real_key")  # As a finished sign-in leaves it.
        refresh()

    def signed_in():
        expect_modal("account")
        check(account.signed_in(), "the fixture key did not sign in")
        call(bpy.ops.loopcut.onboarding_step, step="how")  # The signed-in screen's Continue.

    def how_step():
        expect_modal("how")
        check(progress() == {"version": 1, "step": "how", "done": False}, f"progress is {progress()}")
        shot("how")

    steps(welcome, try_to_dismiss, after_dismiss, quick_setup, account_step, signed_in, how_step, done)

elif ARGS.stage == "import":
    stock = os.environ.get("LOOPCUT_STOCK_BLENDER_DIR", "")
    check(stock and not stock.startswith(str(Path.home() / "Library")), "LOOPCUT_STOCK_BLENDER_DIR must be a fake")
    check(not PREFS.exists(), "preferences already exist: use a new sandbox")

    def welcome():
        expect_modal("import")
        shot("import-offered")
        call(bpy.ops.loopcut.import_blender_settings)

    def imported():
        check(PREFS.is_file(), "the stock preferences were not copied")
        check("loopcut" in bpy.context.preferences.addons, "Loopcut is not enabled after the import")
        check(onboarding._registered and not onboarding.missing_operators(),
              f"Loopcut is half registered: missing {onboarding.missing_operators()}")
        expect_modal("account")
        check(not onboarding._state["starting_since"], "the account step was drawn without its operators")
        shot("account-after-import")
        try_to_dismiss()

    def still_there():
        expect_modal("account")

    steps(welcome, lambda: None, lambda: None, imported, still_there, done)

elif ARGS.stage == "missing":
    def to_account():
        onboarding._go("account")
        if not PREFS.exists():
            with bpy.context.temp_override(window=window()):
                bpy.ops.wm.save_userpref()
        refresh()

    def drop_operator():
        expect_modal("account")
        bpy.utils.unregister_class(bpy.types.LOOPCUT_OT_sign_in)
        refresh()

    def starting():
        check(not wm().splash_modal, "modal with an operator missing")
        check(onboarding._splash_open(), "the splash closed by itself")
        shot("operator-missing")
        try_to_dismiss()

    def left():
        check(not onboarding._splash_open(), "the splash without its operators did not close on an outside click")
        from loopcut import account as account_module
        bpy.utils.register_class(account_module.LOOPCUT_OT_sign_in)

    def healed():
        expect_modal("account")  # The watchdog reopened it once the operator was back.
        shot("healed")
        bpy.app.timers.unregister(onboarding._watch)  # The last phase must not be reopened under it.

        def no_buttons(layout, context, first_run):
            onboarding._set_modal(context, True)
            layout.label(text="A step that drew no button")
            return True
        onboarding._draw_splash = no_buttons
        refresh()

    def no_way_out_is_not_modal():
        check(wm().splash_modal, "the add-on's flag should still ask for modal here")
        shot("no-button-not-modal")
        try_to_dismiss()

    def closed():
        check(not onboarding._splash_open(), "a modal splash with no button was not closed by an outside click")

    steps(to_account, drop_operator, starting, left, lambda: None, healed, no_way_out_is_not_modal, closed, done)

elif ARGS.stage == "resume":
    check(PREFS.exists(), "run --stage fresh in this sandbox first")

    def resumed():
        expect_modal("how")  # fresh quit on How Loopcut works, signed in.
        shot("resumed")
        try_to_dismiss()

    def still_there():
        expect_modal("how")

    steps(resumed, still_there, done)

elif ARGS.stage == "failed":
    def start_sign_in():
        check(onboarding._state["step"] in ("account", "import", "setup"), f"step is {onboarding._state['step']}")
        if onboarding._state["step"] != "account":
            onboarding._go("account")
            with bpy.context.temp_override(window=window()):
                bpy.ops.wm.save_userpref()
            refresh()
        call(bpy.ops.loopcut.sign_in)

    def failed():
        check(account.status()["status"] == "error", f"sign-in status is {account.status()['status']}")
        refresh()

    def failed_screen():
        expect_modal("account")
        shot("sign-in-failed")
        call(bpy.ops.loopcut.onboarding_no_account)

    def model_step():
        expect_modal("connect")
        check(onboarding._state["no_account"], "no_account was not recorded")
        shot("model-without-account")
        call(bpy.ops.loopcut.onboarding_step, step="privacy")  # Skip for now (offered without an account).

    def privacy_step():
        expect_modal("privacy")
        call(bpy.ops.loopcut.onboarding_step, step="done", save=True)

    def try_step():
        expect_modal("done")
        shot("try-without-account")
        call(bpy.ops.loopcut.onboarding_try, card="")

    def closed():
        check(not wm().splash_modal, "still modal after finishing")
        check(not onboarding._splash_open(), "the splash did not close")
        check(progress() == {"version": 1, "step": "account", "done": False, "privacy_seen": 1},  # Its privacy step
              f"progress is {progress()}")                                                   # was passed.
        shot("closed-not-done")

    steps(start_sign_in, failed, failed_screen, lambda: None, model_step, privacy_step, lambda: None, try_step,
          closed, done)

elif ARGS.stage == "existing":
    def step_is(step: str):
        return lambda: onboarding._state["step"] == step

    def account_step():
        check(PREFS.exists(), "the sandbox needs preferences")
        check(account.signed_in(), "the sandbox needs a Loopcut key")
        expect_modal("account")  # Set up before 0.2.6: the setup too, from the account, modal.
        check(onboarding.is_existing() and progress().get("existing") is True, f"progress is {progress()}")
        check(not onboarding.privacy_seen(), "privacy seen before its step")
        check(not onboarding._credits_pending(), "still waiting for the credits")
        shot("account")
        try_to_dismiss()

    def how_step():
        expect_modal("how")
        shot("how")

    def model_step():
        expect_modal("connect")
        check(onboarding._connected(), "the sandbox's model is not connected")
        shot("model")

    def privacy_step():
        expect_modal("privacy")
        check(settings.preferences().share_conversations, "sharing is not on by default")
        shot("privacy")

    def try_step():
        expect_modal("done")
        check(onboarding.privacy_seen(), "privacy not recorded as seen")
        shot("try-it")
        call(bpy.ops.loopcut.onboarding_try, card="")

    def closed():
        check(not wm().splash_modal and not onboarding._splash_open(), "the setup did not close")
        check(progress() == {"version": 1, "step": "done", "done": True, "privacy_seen": 1, "existing": True},
              f"progress is {progress()}")

    steps(*[lambda: None] * 11,  # The credits check gives up after 15 s (the service is unreachable).
          account_step, lambda: expect_modal("account"),
          *one_click_primary("Continue (signed in)", step_is("how")), how_step,
          *one_click_primary("Continue (how it works)", step_is("connect")), model_step,
          *one_click_primary("Continue (the model it has)", step_is("privacy")), privacy_step,
          *one_click_primary("Continue (privacy)", step_is("done")), try_step, closed, done)

elif ARGS.stage == "existing-again":
    def again():
        check(PREFS.exists() and progress().get("privacy_seen") == 1, "run --stage existing in this sandbox first")
        check(onboarding._state["step"] is None and onboarding._state["finished"], f"state is {onboarding._state}")
        check(not wm().splash_modal, "the splash is modal after the notice was seen")
        shot("normal-splash")
        try_to_dismiss()

    def dismissed():
        refresh()
        shot("after-outside-click")

    steps(again, dismissed, done)

else:  # screens and clicks: the same fixture
    from datetime import datetime, timezone

    granted = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    credits = {"balance": 100, "trial": {"amount": 100, "granted_at": granted}}
    live = [{"name": "motion_from_text", "backend": "managed", "cost": {"credits_estimate": 10}},
            {"name": "image_to_3d", "backend": "managed", "cost": {"credits_estimate": 80}}]
    signed_in = {"value": False}
    from loopcut import chatgpt_ui
    account.signed_in = lambda: signed_in["value"]
    account.account_line = lambda: "you@example.com  ·  Free plan"
    onboarding._credits = lambda: credits if signed_in["value"] else None
    onboarding._descriptors = lambda: live if signed_in["value"] else []
    sales = {"open": True, "plan": "free"}
    config.billing_open = lambda: sales["open"]
    onboarding._account_plan = lambda: sales["plan"] if signed_in["value"] else ""

    def selling(open_: bool, plan: str):
        def run():
            sales.update(open=open_, plan=plan)
            refresh()
        return run
    onboarding._connected = lambda: signed_in["value"]

    def go(step: str, **state):
        def run():
            onboarding._state.update(step=step, **state)
            onboarding.save_progress(step)
            refresh()
        return run

    def capture(step: str, name: str):
        def run():
            expect_modal(step)
            region, areas = onboarding._state["region"], window().screen.areas
            # In region pixels (native on a Retina display), as the window's areas measure it.
            width = max(a.x + a.width for a in areas)
            height = max(a.y + a.height for a in areas)
            print(f"POPUP {name} {region.x} {region.y} {region.width} {region.height} window {width} {height}")
            check(region.y >= 0 and region.y + region.height <= height and region.width <= width,
                  f"{name} does not fit the window: popup {region.width}x{region.height} at y {region.y}, "
                  f"window {width}x{height}")
            shot(name)
        return run

    def welcome():
        if onboarding._state["step"] == "import":
            capture("import", "0-welcome")()

    def waiting():
        account._state.update(status="waiting", user_code="TQ7-4KD", url="https://loopcut.org/link?code=TQ7-4KD")
        refresh()

    def failed():
        account._state.update(status="error", error="fixture", user_code="", url="")
        refresh()

    def chatgpt(status: str, error: str = ""):
        def run():
            chatgpt_ui._state.update(status=status, error=error, url="https://auth.openai.com/fixture")
            refresh()
        return run

    def sign_in_now():
        account._state.update(status="idle", user_code="", url="", error="")
        signed_in["value"] = True
        settings.preferences().tier = "fast"  # A new account's tier, whatever the imported preferences held.
        settings.preferences().provider = "LOOPCUT"  # What a sign-in without a ChatGPT plan picks.
        refresh()

    def claim():
        call(bpy.ops.loopcut.onboarding_claim)

    def ensure_prefs():
        if not PREFS.exists():
            with bpy.context.temp_override(window=window()):
                bpy.ops.wm.save_userpref()
        refresh()

    def photo_card():
        with bpy.context.temp_override(window=window()):
            check(bpy.ops.loopcut.onboarding_try(card="photo_3d") == {"FINISHED"}, "the card did not run")

    def sent():
        from loopcut import state
        from loopcut.ui import host
        check(not wm().splash_modal and not onboarding._splash_open(), "the setup did not close")
        check(host.find_panel(window()) is not None, "no Loopcut panel after the card")
        users = [item for item in state.session()["items"] if item["kind"] == "user"]
        check(users and "attached photo" in users[-1]["text"], f"the card's request was not sent: {users}")
        check(users[-1]["images"], "the sample photo was not attached")
        check(progress() == {"version": 1, "step": "done", "done": True}, f"progress is {progress()}")
        shot("5-sent")

    def prefs():
        return settings.preferences()

    if ARGS.stage == "clicks":
        before = {}

        def remember():
            before["auto_run"] = prefs().auto_run

        def sign_in_started():
            started = account.status()["status"] != "idle"
            account.cancel_sign_in()
            account._state.update(status="idle", user_code="", url="", error="")
            return started

        steps(ensure_prefs, go("account"), lambda: expect_modal("account"),
              *one_click("Create account", 519, 487, sign_in_started),
              sign_in_now, lambda: expect_modal("account"),
              *one_click("Claim credits", 519, 441, lambda: onboarding._state["claimed"], rebuild=True),
              *one_click("Continue (signed in)", 519, 441, lambda: onboarding._state["step"] == "how"),
              lambda: None,  # How Loopcut works is taller: one more frame before pointing at it.
              *one_click("← Back (how it works)", 117, 1072, lambda: onboarding._state["step"] == "account"),
              *one_click("Continue (signed in)", 519, 441, lambda: onboarding._state["step"] == "how"),
              lambda: None,
              *one_click("Continue (how it works)", 519, 1003, lambda: onboarding._state["step"] == "connect",
                         rebuild=True),
              *one_click("Use Loopcut", 773, 467,
                         lambda: onboarding._state["step"] == "privacy" and prefs().provider == "LOOPCUT"),
              remember,
              *one_click("a checkbox", 114, 637, lambda: prefs().auto_run != before["auto_run"]),
              *one_click("a checkbox's label", 260, 637, lambda: prefs().auto_run == before["auto_run"],
                         rebuild=True),
              *one_click("Continue (privacy)", 519, 965, lambda: onboarding._state["step"] == "done"),
              *one_click("← Back (try it)", 117, 1018, lambda: onboarding._state["step"] == "privacy"),
              *one_click("← Back (privacy)", 117, 1034, lambda: onboarding._state["step"] == "connect",
                         rebuild=True),
              *one_click("← Back (model)", 117, 758, lambda: onboarding._state["step"] == "how"),
              go("done"), lambda: expect_modal("done"),
              *one_click("Start", 814, 823, lambda: not onboarding._splash_open() and not wm().splash_modal),
              done)
    else:
        steps(welcome, ensure_prefs, go("account"), capture("account", "1-account"), waiting,
              capture("account", "1b-account-waiting"), failed, capture("account", "1c-account-failed"),
              sign_in_now, capture("account", "1d-signed-in-claim"), claim, capture("account", "1e-claimed"),
              go("how"), capture("how", "1f-how-free"), selling(True, "pro"), capture("how", "1g-how-pro-account"),
              selling(True, "max"), capture("how", "1h-how-max-account"), selling(False, ""),
              capture("how", "1i-how-no-sales"), selling(True, "free"),
              go("connect"), capture("connect", "2-model"), chatgpt("waiting"),
              capture("connect", "2b-model-chatgpt-waiting"),
              chatgpt("error", "This ChatGPT account has no Plus or Pro plan"), capture("connect", "2c-model-chatgpt-error"),
              chatgpt("idle"), go("privacy"), capture("privacy", "3-privacy"), go("done"), capture("done", "4-try-it"),
              photo_card, lambda: None, sent, done)
