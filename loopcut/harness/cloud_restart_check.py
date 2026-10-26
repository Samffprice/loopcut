"""A cloud render that outlives Blender, in real Blenders against the contract mock (0.2.19, lane R1).

    python3 loopcut/harness/cloud_restart_check.py --out <folder> --contract <contract folder> [--blender <binary>]

Run it under the shared heavy lock. Outside Blender it is the runner: it starts contract/mock/server.py on a free
local port, with a render's media as the live service sends them for contract 0.10 (serve_mock: three
preview_<frame>.jpg and animation.mp4 beside the frames), then launches this checkout in Blenders that share one
isolated profile under <folder>/profile:
  start    GUI: save garden.blend, ask for a 24-frame cloud render through the agent's tool path, Approve its card,
           screenshot the running row (Safe to close), then quit with wm.quit_blender (a real exit: exit_pre, then
           every add-on disabled). The runner checks the mock job is still running, never canceled, and waits for
           it to finish.
  reopen   GUI, garden.blend opened: the chat comes back with its "finished while you were away" card and the
           previews as its thumbnails; screenshot, press Load the frames, see one image strip of 24 frames (no
           preview among them, no animation.mp4 strip); Undo (ed.undo): the strip goes and the card offers Load
           again; Load again; quit WITHOUT saving.
  reload   GUI, garden.blend opened again: the file never got the frames, so the card is back (the review's item
           1); Load the frames again, the frames kept beside the .blend in //renders/cloud-<job>/; save and quit.
           The runner then clears this Blender's temp folder, as the system may.
  again    GUI, garden.blend opened again: no card, still one strip whose frames are on disk beside the .blend,
           job_status says they are in this file already; quit.
  disable  background: a long job, then the add-on disabled as Preferences does; the mock job is canceled.
  unpickable  GUI, as start, but its job made one no chat picks up (as a rig is): the quit itself cancels it in the
           cloud (the review's item 10), before Blender's exit ends the thread that follows it.
  control  GUI, as start but with 0.2.18's quit (cloud_jobs.leave_all made a no-op, so unregister cancels): what the
           mock job does then, for comparison; reported, never a failure.
Each GUI quit logs QUIT-PATH lines: exit_pre ran, then cloud.unregister, and whether jobs were left or canceled.
Evidence: <folder>/summary.json, a log per phase, screenshots and the panel's display lists.
"""

import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path

try:
    import bpy
except ImportError:
    bpy = None

FRAMES = 24
JOB_SECONDS = 50


# ------------------------------------------------------------------------------------------------ the runner

def run():
    import argparse
    import shutil
    import socket
    import subprocess
    import urllib.request
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--blender", type=Path,
                        default=Path(__file__).resolve().parents[3] / "tools/Blender.app/Contents/MacOS/Blender")
    args = parser.parse_args()
    out = args.out.resolve()
    profile = out / "profile"
    shutil.rmtree(profile, ignore_errors=True)
    for name in ("blender-user", "config", "data", "tmp", "project"):
        (profile / name).mkdir(parents=True)
    (profile / "empty.env").touch()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    service = f"http://127.0.0.1:{port}"
    mock_log = (out / "mock.log").open("w")
    mock = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "serve-mock", str(args.contract),
                             "--port", str(port), "--job-seconds", str(JOB_SECONDS), "--live-tools",
                             "render,image_generate"], stdout=mock_log, stderr=subprocess.STDOUT)
    summary = {"service": service, "blender": str(args.blender), "phases": {}}

    def mock_get(path):
        request = urllib.request.Request(service + path, headers={"Authorization": "Bearer lc_demo",
                                                                  "x-loopcut-contract": "0.10"})
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)

    def blender(phase, *extra, background=False):
        env = {**os.environ, "BLENDER_USER_RESOURCES": str(profile / "blender-user"),
               "LOOPCUT_CONFIG_DIR": str(profile / "config"), "LOOPCUT_DATA_DIR": str(profile / "data"),
               "LOOPCUT_ENV_FILE": str(profile / "empty.env"), "LOOPCUT_TELEMETRY": "off",
               "LOOPCUT_SERVICE_URL": service, "TMPDIR": str(profile / "tmp"),
               "LOOPCUT_NO_GENERATION": "false", "LOOPCUT_PRIVATE_MODE": "true"}
        command = [str(args.blender), "--factory-startup"]
        command += ["-b"] if background else ["--enable-event-simulate", "-p", "0", "0", "1280", "860"]
        command += [*extra, "--python", str(Path(__file__).resolve()), "--", phase, str(out), str(profile)]
        started = time.monotonic()
        with (out / f"{phase}.log").open("w") as log:
            code = subprocess.call(command, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=600)
        result = json.loads((out / f"{phase}.json").read_text()) if (out / f"{phase}.json").is_file() else {}
        summary["phases"][phase] = {"exit": code, "seconds": round(time.monotonic() - started, 1), **result}
        print(phase, "exit", code, json.dumps(result)[:400], flush=True)
        if code != 0 or not result.get("ok"):
            raise SystemExit(f"{phase} failed; see {out / (phase + '.log')}")
        return result

    try:
        for _ in range(50):
            try:
                mock_get("/v1/manifest")
                break
            except OSError:
                time.sleep(.2)
        started = blender("start")
        job_id = started["job_id"]
        after_quit = mock_get("/v1/jobs/" + job_id)
        summary["after_quit"] = {"status": after_quit["status"], "progress": after_quit["progress"]}
        if after_quit["status"] not in ("queued", "running"):
            raise SystemExit(f"Quitting Blender left the job {after_quit['status']}")
        deadline = time.monotonic() + JOB_SECONDS + 60
        while mock_get("/v1/jobs/" + job_id)["status"] not in ("succeeded", "failed", "canceled"):
            if time.monotonic() > deadline:
                raise SystemExit("The mock job never finished")
            time.sleep(1)
        final = mock_get("/v1/jobs/" + job_id)
        summary["finished_in_the_cloud"] = {"status": final["status"], "credits": final["credits"],
                                            "created_at": final.get("created_at"), "finished_at": final.get("finished_at"),
                                            "outputs": [o["name"] for o in final["outputs"]]}
        media = [o for o in final["outputs"] if o["name"].startswith("preview_") or o["name"] == "animation.mp4"]
        if len(media) != 4:
            raise SystemExit(f"The mock's render has no previews or animation.mp4: {summary['finished_in_the_cloud']}")
        project = profile / "project" / "garden.blend"
        blender("reopen", str(project))
        blender("reload", str(project))
        shutil.rmtree(profile / "tmp")  # The system clears the temp folder; the strip's frames must not be there.
        (profile / "tmp").mkdir()
        blender("again", str(project))
        disabled = blender("disable", background=True)
        summary["disable"] = {"job": disabled["job_id"], "status": mock_get("/v1/jobs/" + disabled["job_id"])["status"]}
        if summary["disable"]["status"] != "canceled":
            raise SystemExit("Disabling the add-on did not cancel its job")
        unpickable = blender("unpickable")
        summary["unpickable"] = {"job": unpickable["job_id"],
                                 "status": mock_get("/v1/jobs/" + unpickable["job_id"])["status"]}
        if summary["unpickable"]["status"] != "canceled":
            raise SystemExit("Quitting left a job no chat picks up running in the cloud")
        summary["ok"] = True
        control = blender("control")
        time.sleep(3)
        summary["control_0_2_18_quit"] = {"job": control["job_id"],
                                          "status": mock_get("/v1/jobs/" + control["job_id"])["status"]}
        print("CLOUD RESTART CHECK PASSED", flush=True)
    finally:
        mock.terminate()  # The Popen object's own PID; nothing else is stopped.
        try:
            mock.wait(timeout=10)
        except subprocess.TimeoutExpired:
            mock.kill()
        mock_log.close()
        (out / "summary.json").write_text(json.dumps(summary, indent=2))
        for name in ("blender-user", "tmp"):
            shutil.rmtree(profile / name, ignore_errors=True)


def serve_mock():
    """python3 cloud_restart_check.py serve-mock <contract folder> <mock/server.py's arguments>: the contract mock,
    whose render jobs also return f0's live media for contract 0.10 (02:22 UTC): preview_<frame>.jpg for the first,
    middle and last frame (type image, meta {frame, preview: true}) and animation.mp4 (type video). Neither may load
    as a frame. The mock's own render returns frames and its report only."""
    import importlib.util
    contract = Path(sys.argv[2]).resolve()
    spec = importlib.util.spec_from_file_location("contract_mock_server", contract / "mock/server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    original = server.State.outputs

    def outputs(self, record, base_url):
        result = original(self, record, base_url)
        frames = [o["meta"]["frame"] for o in result if o["type"] == "render_frames"]
        if self.catalog[record["job"]["tool"]]["name"] != "render" or not frames:
            return result
        media = [(f"preview_{f:04d}.jpg", "image", "image/jpeg", "jpg", server.png(), {"frame": f, "preview": True})
                 for f in sorted({frames[0], frames[len(frames) // 2], frames[-1]})]
        movie = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + bytes(64)  # Never decoded: it must not load.
        media.append(("animation.mp4", "video", "video/mp4", "mp4", movie, {"frames": len(frames), "fps": 24}))
        expiry = int(time.time()) + 3600
        for name, kind, mime, extension, data, meta in media:
            hash_value = server.digest(data)
            path = f"/mock-files/{hash_value}.{extension}"
            self.output_files[path] = (data, mime)
            result.append({"name": name, "type": kind, "mime": mime, "sha256": hash_value, "size": len(data),
                           "url": f"{base_url}{path}?X-Mock-Expires={expiry}&X-Mock-Signature={hash_value[:24]}",
                           "expires_at": server.timestamp(expiry), "meta": meta})
        return result
    server.State.outputs = outputs
    sys.argv = [str(contract / "mock/server.py"), *sys.argv[3:]]
    server.main()


# ------------------------------------------------------------------------------------------------ in Blender

PHASE = OUT = PROFILE = None
STEPS, NOTES, SHOTS = [], {}, []


def finish(ok: bool, message: str, quit_blender: bool = True):
    NOTES["ok"] = ok
    NOTES["message"] = message
    NOTES["screenshots"] = SHOTS
    (OUT / f"{PHASE}.json").write_text(json.dumps(NOTES, indent=2))
    print(("PHASE OK " if ok else "PHASE FAILED ") + f"{PHASE}: {message}", flush=True)
    sys.stdout.flush()
    sys.stderr.flush()
    if not ok:
        os._exit(1)
    if quit_blender:
        bpy.context.preferences.view.use_save_prompt = False
        window = bpy.context.window_manager.windows[0]
        with bpy.context.temp_override(window=window):
            bpy.ops.wm.quit_blender()  # A real exit: exit_pre, then addon_utils.disable_all, then Python ends.


def step(fn):
    STEPS.append(fn)
    return fn


def tick():
    """Each step returns True when done, or a number of seconds to wait and be asked again."""
    try:
        while STEPS:
            done = STEPS[0]()
            if done is True:
                STEPS.pop(0)
                continue
            return done
        return None
    except Exception:
        traceback.print_exc()
        finish(False, f"{STEPS[0].__name__ if STEPS else '?'} raised")
        return None


def screenshot(name):
    from loopcut.ui import host
    window = bpy.context.window_manager.windows[0]
    for area in window.screen.areas:
        area.tag_redraw()
    with bpy.context.temp_override(window=window):
        bpy.ops.wm.redraw_timer(type="DRAW_WIN_SWAP", iterations=2)
        bpy.ops.screen.screenshot(filepath=str(OUT / f"{PHASE}-{name}.png"))
    host.dump_display(str(OUT / f"{PHASE}-{name}.display.json"))
    SHOTS.append(f"{PHASE}-{name}.png")


def texts():
    from loopcut.ui import host
    path = OUT / f"{PHASE}-texts.json"
    host.dump_display(str(path))
    display = json.loads(path.read_text())
    path.unlink()
    return " ".join(p.get("text", "") for p in display["prims"])


def setup():
    """This checkout's add-on, enabled the way Blender enables add-ons (so quitting disables it), signed in to the
    mock with its demo key, its panel docked."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import checkout
    checkout.use()
    import loopcut
    from loopcut import config, credentials
    bpy.context.preferences.view.show_splash = False
    bpy.context.preferences.filepaths.file_preview_type = "NONE"
    credentials.store(config.SERVICE_BASE_URL, "lc_demo")
    loopcut.register()
    loopcut.__addon_enabled__ = True  # addon_utils.enable's mark: Blender's exit disables it (disable_all).
    from loopcut import cloud, cloud_jobs
    unregister = cloud.unregister

    def logged_unregister():
        print(f"QUIT-PATH cloud.unregister ran; jobs left in the cloud: {cloud_jobs._leaving.is_set()}", flush=True)
        unregister()
    cloud.unregister = logged_unregister

    @bpy.app.handlers.persistent
    def after_ours(*_):  # Appended after cloud's own exit_pre handler, so it runs after it.
        print(f"QUIT-PATH exit_pre ran; jobs left in the cloud: {cloud_jobs._leaving.is_set()}", flush=True)
    bpy.app.handlers.exit_pre.append(after_ours)
    if not bpy.app.background:
        from loopcut.ui import host
        window = bpy.context.window_manager.windows[0]
        if not host.has_panel(window):
            host.dock(window)


def phase_start():
    from loopcut import agent, cloud, cloud_account, cloud_jobs, conversations, mainthread, state
    from loopcut.ui import host
    run = {}

    @step
    def scene():
        if host.find_panel(bpy.context.window_manager.windows[0]) is None:
            return .3
        scene = bpy.context.scene
        scene.render.resolution_x, scene.render.resolution_y, scene.render.resolution_percentage = 3840, 2160, 100
        scene.frame_start, scene.frame_end = 1, FRAMES
        scene.render.engine = "CYCLES"
        name = "garden.blend" if PHASE == "start" else PHASE + ".blend"
        bpy.ops.wm.save_as_mainfile(filepath=str(PROFILE / "project" / name))
        return True

    @step
    def ask():
        session = state.session()
        text = "Render the garden animation in the cloud."
        session["items"].append(state.item_user(text))
        session["messages"].append({"role": "user", "content": text})
        arguments = {"frame_start": 1, "frame_end": FRAMES, "engine": "cycles", "samples": 16}
        call = {"id": "call_render", "type": "function",
                "function": {"name": cloud.PREFIX + "render", "arguments": json.dumps(arguments)}}
        session["messages"].append({"role": "assistant", "content": "", "tool_calls": [call]})
        card = state.item_tool(cloud.PREFIX + "render", "Render the garden in the cloud", "")
        card.update(status="running")
        session["items"].append(card)
        turn = agent.Turn()
        session.update(busy=True, turn=turn)
        consent = agent._Consent(session, turn, card)
        run.update(card=card, turn=turn, call=call)

        def work():
            try:
                # The catalog, refreshed off main as the panel does, before the call reads it.
                mainthread.run_on_main(cloud._snapshot).result(timeout=30).refresh()
                with cloud_account.approval_context(consent):
                    run["result"] = agent._run_tool_on_main(cloud.PREFIX + "render", json.dumps(arguments),
                                                            turn.cancel.is_set)
            except BaseException:
                run["error"] = traceback.format_exc()
        threading.Thread(target=work, daemon=True).start()
        return True

    @step
    def approve():
        if "error" in run:
            raise RuntimeError(run["error"])
        if "result" in run:
            return True  # Ran within the chat's budget without asking.
        card = run["card"]
        if card.get("status") != "awaiting":
            return .3
        screenshot("approve")
        session = state.session()
        host._do_action(session, ("approve", session["items"].index(card)))  # The card's Approve button.
        NOTES["approved"] = card.get("ask_lines")
        return True

    @step
    def handed_off():
        if "error" in run:
            raise RuntimeError(run["error"])
        if "result" not in run:
            return .3
        result = run["result"]
        if not result.ok:
            raise RuntimeError("render refused: " + result.text)
        job_id = json.loads(result.text)["job_id"]
        session = state.session()
        run["card"].update(status="done", output=result.text)
        session["messages"].append({"role": "tool", "tool_call_id": run["call"]["id"], "content": result.text})
        reply = state.item_assistant()
        reply.update(text="Your render is running in the cloud.", streaming=False)
        session["items"].append(reply)
        session["messages"].append({"role": "assistant", "content": reply["text"]})
        session.update(busy=False, turn=None)
        conversations.save(session)
        NOTES.update(job_id=job_id, conversation=session["id"])
        saved = json.loads((conversations.root() / session["id"] / "conversation.json").read_text())
        NOTES["record"] = next(r for r in saved["cloud_jobs"] if r["id"] == job_id)
        run["shown_at"] = time.monotonic()
        return True

    @step
    def running_row():
        rows = cloud_jobs.ui_rows()
        if not rows or time.monotonic() - run["shown_at"] < 1.5:
            return .3
        NOTES["row"] = rows[0]
        if "Safe to close" not in texts():
            raise AssertionError("The running row has no Safe to close note")
        screenshot("running")
        bpy.ops.wm.save_mainfile()
        if PHASE == "control":
            cloud_jobs.leave_all = lambda: None  # 0.2.18: nothing tells unregister that Blender is quitting.
        if PHASE == "unpickable":
            cloud_jobs._registry[NOTES["job_id"]].pop("resumable")  # As a rig's: nothing applies it later.
        finish(True, f"job {NOTES['job_id']} running; quitting Blender")
        return True


def phase_reopen():
    from loopcut import cloud, state
    from loopcut.ui import host
    run = {"since": time.monotonic()}

    def card():
        return next((i for i in state.session()["items"] if i.get("kind") == "cloud_back"), None)

    def strips():
        editor = bpy.context.scene.sequence_editor
        return list(editor.strips) if editor is not None else []

    @step
    def shown():
        if card() is None:
            if time.monotonic() - run["since"] > 30:
                raise AssertionError("No finished-while-away card after 30 s")
            return .3
        if host.find_panel(bpy.context.window_manager.windows[0]) is None:
            return .3
        item = card()
        if len(item.get("thumbnails", [])) < 3 and time.monotonic() - run["since"] < 30:
            return .3  # The render's three previews, fetched for the card.
        NOTES["card"] = {k: item[k] for k in ("title", "detail", "actions", "status", "thumbnail_labels")}
        NOTES["thumbnails"] = len(item.get("thumbnails", []))
        NOTES["strips_before"] = len(strips())
        screenshot("card")
        if NOTES["thumbnails"] != 3:
            raise AssertionError(f"The card shows {NOTES['thumbnails']} preview thumbnails, not 3")
        return True

    def press_load():
        session = state.session()
        item = card()
        choice = [a["label"] for a in item["actions"]].index("Load the frames")
        host._do_action(session, ("cloud_back", (session["items"].index(item), choice)))
        run["pressed"] = time.monotonic()

    def check_loaded(item, key):
        """One image strip of the render's frames, beside the .blend: no preview among them, no animation.mp4."""
        found = [s for s in strips() if s.type == "IMAGE"]
        movies = [s.name for s in strips() if s.type == "MOVIE"]
        NOTES[key] = {"resolved": item["resolved"], "movies": movies,
                      "strips": [{"name": s.name, "frames": len(s.elements),
                                  "first": s.elements[0].filename if len(s.elements) else ""} for s in found]}
        if len(found) != 1 or len(found[0].elements) != FRAMES or movies:
            raise AssertionError(f"Expected one {FRAMES}-frame strip and no movie, got {NOTES[key]}")
        return found[0]

    @step
    def load():
        press_load()
        return True

    @step
    def loaded():
        item = card()
        if item.get("busy"):
            if time.monotonic() - run["pressed"] > 120:
                raise AssertionError("Load the frames never finished")
            return .3
        if item.get("error") or not item.get("resolved"):
            raise AssertionError("Load the frames failed: " + str(item.get("error")))
        strip = check_loaded(item, "loaded")
        job_id = item["job_id"]
        again = cloud._job_status({"job_id": job_id})  # The agent asking afterwards: the same apply, not a second.
        NOTES["job_status_after"] = again.text[:300]
        if len([s for s in strips() if s.type == "IMAGE"]) != 1:
            raise AssertionError("job_status applied the frames a second time")
        folder = Path(bpy.path.abspath(strip.directory))
        NOTES.update(directory=strip.directory, frames_on_disk=len([e for e in strip.elements
                                                                   if (folder / e.filename).is_file()]))
        if folder.parent.name != "renders" or folder.parent.parent != Path(bpy.data.filepath).parent:
            raise AssertionError(f"Frames are not kept beside the .blend: {folder}")
        screenshot("loaded")
        if PHASE == "reload":
            bpy.ops.wm.save_mainfile()
            finish(True, "card back after quitting unsaved, frames loaded beside the .blend, saved")
        return True

    if PHASE != "reopen":
        return

    @step
    def undo():
        window = bpy.context.window_manager.windows[0]
        with bpy.context.temp_override(window=window):
            bpy.ops.ed.undo()  # Ctrl+Z: the Load's undo step.
        return True

    @step
    def card_back():
        item = card()
        NOTES["after_undo"] = {"strips": len(strips()), "resolved": item.get("resolved"),
                               "actions": [a["label"] for a in item["actions"]]}
        if strips() or item.get("resolved") or "Load the frames" not in NOTES["after_undo"]["actions"]:
            raise AssertionError(f"Undo after Load did not bring the card's Load back: {NOTES['after_undo']}")
        screenshot("undone")
        press_load()
        return True

    @step
    def loaded_again():
        item = card()
        if item.get("busy"):
            if time.monotonic() - run["pressed"] > 120:
                raise AssertionError("Load the frames never finished after Undo")
            return .3
        if item.get("error") or not item.get("resolved"):
            raise AssertionError("Load the frames after Undo failed: " + str(item.get("error")))
        check_loaded(item, "loaded_again")
        screenshot("again-loaded")
        finish(True, "card shown with its previews, frames loaded once; Undo brought Load back, loaded again; "
                     "quitting without saving")
        return True


def phase_again():
    from loopcut import cloud, state
    from loopcut.ui import host
    run = {"since": time.monotonic()}

    @step
    def nothing_twice():
        if host.find_panel(bpy.context.window_manager.windows[0]) is None or not state.session()["items"]:
            return .3
        if time.monotonic() - run["since"] < 6:
            return .5  # Long enough for a pickup to have asked the mock and drawn a card, had there been one.
        session = state.session()
        cards = [i for i in session["items"] if i.get("kind") == "cloud_back"]
        editor = bpy.context.scene.sequence_editor
        found = [s for s in editor.strips if s.type == "IMAGE"] if editor else []
        job_id = session["cloud_jobs"][0]["id"]
        answer = cloud._job_status({"job_id": job_id})
        NOTES.update(cards=len(cards), strips=len(found), job_status=answer.text[:300],
                     record=session["cloud_jobs"][0])
        folder = Path(bpy.path.abspath(found[0].directory)) if found else None
        NOTES["frames_on_disk"] = len([e for e in found[0].elements if (folder / e.filename).is_file()]) if found else 0
        if cards or len(found) != 1 or "in this file already" not in answer.text or NOTES["frames_on_disk"] != FRAMES:
            raise AssertionError(f"Second relaunch: {NOTES}")
        screenshot("again")
        finish(True, "no second card or apply")
        return True


def phase_disable():
    """Background: a long job, then the add-on disabled the way Preferences does it."""
    import addon_utils
    from loopcut import cloud, cloud_account, mainthread, state
    runtime = cloud._snapshot()
    runtime.refresh()
    next(d for d in runtime.manifest["tools"] if d["name"] == "image_generate")["latency"] = "long"
    session = state.session()
    session["items"].append(state.item_user("make a tiny prop image"))
    results = []

    def work():
        try:
            with cloud_account.approval_context(lambda details: True):
                from loopcut import agent
                results.append(agent._run_tool_on_main(cloud.PREFIX + "image_generate",
                                                       json.dumps({"prompt": "tiny prop"})))
        except BaseException as ex:
            results.append(ex)
    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    deadline = time.monotonic() + 60
    while worker.is_alive() and time.monotonic() < deadline:
        mainthread._pump()
        time.sleep(.02)
    result = results[0]
    if isinstance(result, BaseException) or not result.ok:
        raise RuntimeError(f"image_generate: {result}")
    NOTES["job_id"] = json.loads(result.text)["job_id"]
    time.sleep(1)
    addon_utils.disable("loopcut")  # Preferences > Add-ons: unregister without exit_pre.
    time.sleep(3)  # Its job thread asks the service to cancel.
    finish(True, "disabled with a job running", quit_blender=False)


def main():
    global PHASE, OUT, PROFILE
    PHASE, OUT, PROFILE = sys.argv[sys.argv.index("--") + 1:][:3]
    OUT, PROFILE = Path(OUT), Path(PROFILE)
    try:
        setup()
        if PHASE == "disable":
            phase_disable()
            return
        {"start": phase_start, "control": phase_start, "unpickable": phase_start, "reopen": phase_reopen,
         "reload": phase_reopen, "again": phase_again}[PHASE]()
    except Exception:
        traceback.print_exc()
        finish(False, "setup raised")
    bpy.app.timers.register(tick, first_interval=1.0, persistent=True)


if __name__ == "__main__":
    if bpy is None and sys.argv[1:2] == ["serve-mock"]:
        serve_mock()
    elif bpy is None:
        run()
    else:
        main()
