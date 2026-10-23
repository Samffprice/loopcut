"""The agent loop: stream a reply, run the tools it asks for, feed results back, repeat."""

import dataclasses
import json
import threading
import time
import uuid
from concurrent.futures import TimeoutError as FutureTimeout
from copy import deepcopy
from typing import Any, Callable

from . import (allowance, checkpoints, completion_guard, config, context, conversations, image_budget,
               llm, repeats, spending, state, videoref)
from .ui.markdown import finish_reply

SYSTEM_PROMPT = """You are Loopcut, an AI agent inside the user's running Blender session. You act through tools: \
run_python runs bpy code; get_scene_info and get_object_info read the scene; inspect_api is this Blender's \
Python API; capture_viewport shows you the scene.

Each user message starts with <scene_context>: the open file, mode, what the user has selected ("this", \
"it", "these") and, for small scenes, every object. @Name in a message refers to that object, material \
or collection, described in the same block.

- Look before you edit: use the context block, and get_object_info before changing an existing material, \
modifier or rig; change what is there instead of rebuilding the user's work.
- One run_python step per coherent piece of work (a whole material, several objects), not one per object. \
Its result ends with "Scene changes", measured from the scene: trust that; do not print values to check. \
Verify placement and contact with check_contact, not from bounds or captures.
- Your bpy knowledge is from older versions. When unsure of a name, and always after an AttributeError, \
TypeError or "enum not found", look it up with inspect_api before retrying.
- The context lists the running Blender version and available render engines. Use those capabilities. \
Material previews use studio lighting; rendered viewport captures use EEVEE and approximate Cycles. \
Do not rebuild materials or boost lights to compensate for an unverified preview problem. Use a real \
camera render (see_render) when final lighting, glass or reflections need verification.
- inspect_scene returns a labeled contact sheet across named cameras and frames. Use geometry for \
shape, materials for neutral studio lighting, final_lighting for the actual scene engine and lights. \
It renders a copy with a bounded budget while the editor stays responsive; stale results are rejected. \
Use it to check all requested cameras and meaningful animation phases. Its metadata reports the \
engine, quality limits, saved revision and signal measurements. A nearly black image can be a scene \
problem; it is not proof that materials or lights need rebuilding. Inspect the cause first.
- Tool calls in one reply run in order, each after the previous one finished: a look placed after a \
run_python sees its result.
- After a reply whose steps changed the scene you get one capture of the result (three_quarter, or the \
last angle you asked for), unless you looked yourself after the last change in that reply. When it would look the same as your last one you get a note instead of the image. Ask for capture_viewport or compare_with_reference only for another angle, a \
focus, a sheet of four views, a rendered look at lighting, or a comparison; the same look at an \
unchanged scene is refused after 3 times. Never move the user's \
viewport or shading. Fix what is actually wrong in the picture, then finish.
- The <scene_context> of a message lists what changed in the scene since your last step: the user's own \
edits. get_scene_info with changed_only=true lists what this turn has touched so far.
- A reference image the user attached stays in view, with a <reference_card> describing it. To copy it: \
match the viewpoint with the camera, build from the card, then compare_with_reference after each \
change and fix the differences you see; look_at_reference for a detail at full resolution.
- Files: list_files and read_file read the disk (an image file you then see); write_file and move_file \
change it, with the user's approval; nothing deletes. see_render shows the user's last render, or with \
render=true renders now: that, like a bake in run_python, waits for the user's OK \
unless they allowed renders.
- For render/export deliverables use render_job with action start: a saved scene copy renders outside the \
editing process, with an explicit frame list, dimensions, samples and time budget. Approval covers this job \
only. Action status reports progress and verified files; cancel stops it; resume \
reuses verified frames from the SAME saved revision with a fresh approval of the same budget. MP4 jobs \
retain their PNG sequence. Do not poll repeatedly while nothing changes. Give the user the job status \
and output location; never describe a queued/running job as a completed export. Jobs remain in the Jobs \
view after closing the chat. Previews of jobs show their saved revision, not later scene edits.
- Libraries first: a prop, set dressing, material, HDRI or sky that is not the user's own work usually looks \
better from a library than built from primitives, so search_assets before building one (a plain shape or flat \
colour needs no search). Choose with the thumbnail sheet and each row's description, size and polycount; prefer \
what matches the scene's existing look, though a mix that reads well is fine. After import, check its measured \
size and its look in the scene, and swap a wrong pick rather than patch it. Build it yourself when nothing fits \
or the user asks, and never replace what the user made or is modelling unless asked. Say which library each \
asset came from and pass on the credit an import gives (Poly Pizza: its link, and the attribution line for CC-BY).
- Never delete or overwrite the user's objects, materials or files unless asked. Name what you create \
sensibly; real-world scale in meters unless told otherwise.
- Installed add-ons and extensions, if any, are listed at the end of this prompt with their operator \
prefixes. inspect_api describes their operators, modules and preferences \
(bpy.context.preferences.addons[module].preferences) like the rest of the API. An operator that needs \
another editor's context runs with run_python's `editor`.
- A user message wrapped in <steer> arrived while you were working. Fold it into what you are doing \
(it may change the goal, add a constraint or answer a question) and continue; do not start over or redo \
finished work.
- Keep replies short: what you did and anything the user must decide. No code dumps.
- When asked to do something Blender can do, use the tools to do it. Do not send the user through \
menus you have not checked. When they report a missing control or unexpected output, inspect the \
live editors, output settings and API before answering; never repeat instructions they said failed. \
run_python can borrow another editor automatically, even when no 3D viewport is open. For video \
output, inspect ImageFormatSettings: IMAGE and VIDEO media types expose different formats.
- Before finishing a multi-part job, check each requested deliverable against the scene: camera framing \
(frame_camera with report_only measures it), \
animation endpoints and middle, preservation constraints, output settings and actual output files. \
Distinguish a configured scene, a preview, and a completed render or export; say which you verified.
- Start every reply that calls tools with a sentence or two: what you see now and why this step. Older \
steps are later shown as records: that note, whether each call worked, what it printed and what it \
changed; not their code. So say there what you learned, above all what did not work and why, or you may \
try it again. Name the objects, materials and nodes you create so later steps can find them. A \
<conversation_summary> may stand for earlier messages. The scene is the source of truth for exact \
values: read it again rather than trust memory."""

_POLL_SECONDS = 0.1
# Looks at the scene cost an image each. A look after a change is what the loop is for, and the
# agent takes one itself after every change; asking for the same look again at a scene that has
# not changed is capped, and past SOFT_LOOKS a turn the model is told to wrap up.
MAX_IDLE_CAPTURES = 3
SOFT_LOOKS = 8
DEFAULT_ANGLE = "three_quarter"
LOW_ALLOWANCE = 0.8  # Warn with about 20% left in either allowance window.
CAPTURE_TOOLS = {"capture_viewport", "compare_with_reference", "inspect_scene"}
# An explicit step limit (cfg.max_steps > 0) is announced this many steps before it; at it, the model is asked
# once more, for text only, to say where the work stands (_wrap_up).
STEPS_LEFT_NOTICE = 3
STEPS_LEFT_NOTE = ("\n\n({left} steps left in this turn. Finish what matters most, or wrap up: say what is done, "
                   "what is left and what you did not verify.)")
# A worker's own (LoopSpec.steps_left_note), in its system prompt's words: "Finish what matters most"
# read as licence to keep building, and the LOW barrel broke its lid there (analysis §5.6).
WORKER_STEPS_LEFT_NOTE = ("\n\n(About {left} steps left in the budget. Stop improving now: "
                          "call check_brief, then report_back.)")
STEP_LIMIT_PROMPT = ("[Loopcut] This turn has used all {steps} of its steps, so tools are off now: any tool call "
                     "is ignored. Reply in plain text only, in three short parts: what is done, what is left, and "
                     "what you did not verify.")
BUDGET_PROMPT = ("[Loopcut] The job's budget is spent ({reason}), so tools are off now: any tool call is ignored. "
                 "Reply in plain text only, in three short parts: what is done, what is left, and what you did "
                 "not verify.")
NO_PROGRESS_NOTICE = (f"Stopped after {repeats.NO_PROGRESS_FAILURES} identical tool failures in a row "
                      "without a scene change.")
NO_PROGRESS_PROMPT = ("[Loopcut] The same tool call keeps failing without changing the scene, so tools are off "
                      "now: any tool call is ignored. Reply in plain text only: what is done, what is blocked "
                      "and why, and what you did not verify.")
# _request's one retry of a step that hit the output cap (max_tokens, or llm.py's call_max_seconds)
# without producing anything usable: appended to that request only, never saved to the conversation.
CAP_RETRY_PROMPT = ("[Loopcut] That reply stopped without a usable answer or tool call. Decide now and act: "
                    "call one tool, or reply in a few plain sentences.")
# A reply with no text and no tool call (an empty or reasoning-only completion) before the turn did anything:
# _request retries it once, as it was, then shows this instead of ending the turn with nothing on screen. On
# 2026-10-20 a new user's first turn on Loopcut Fast (glm-5.3-flash) came back with 4 output tokens and no text,
# so the panel showed nothing at all and they never came back; 2 of 677 full-size Fast requests did that.
EMPTY_REPLY_MESSAGE = ("The model sent back an empty reply twice, so nothing happened yet. Press Try again, or pick "
                       "another model in the model menu below.")
FILTERED_REPLY_MESSAGE = ("The model provider's content filter stopped this reply before it said anything. Rephrase "
                          "the request, or pick another model in the model menu below.")

REFERENCE_PROMPT = """You are writing a reference card for a 3D artist who must reproduce the attached image in \
Blender as closely as possible. Be terse and concrete, numbers and names over prose, under 300 words:
1. Subject, composition, viewpoint (camera height and angle, wide or long lens), aspect ratio.
2. Every distinct part: shape, size relative to the whole (as ratios), position, orientation, how parts meet.
3. Colors as approximate sRGB hex, materials (matte, glossy, metal, glass; rough or smooth), patterns, textures.
4. Lighting (direction, softness, color) and background.
If several images are attached, describe each under its name."""


class Turn:
    def __init__(self):
        self.cancel = threading.Event()
        self.decided = threading.Event()
        self.approved = False
        self.thread: threading.Thread | None = None
        self.captures = 0        # Looks at the scene the model asked for this turn.
        self.idle_captures = 0   # The same look again, with no scene change since the previous one.
        self.changed = True      # The user's message itself is a change worth a look.
        self.last_look = ""      # Tool and arguments of the model's previous look.
        self.angle = DEFAULT_ANGLE  # The angle the model last asked for: what the automatic looks use.
        self.focus: list = []       # The focus of its last capture_viewport: what the automatic looks keep framing.
        self.shot: dict = {}        # Its camera and view_layer (a worker's production shot): kept the same way.
        self.touched: list = []     # Objects changed since the last look (scene_diff.touched): what the next frames.
        self.roots: tuple = ()      # Folders the file tools read unasked; files.project_roots, set by send().
        # scene_diff snapshots from before the first and after the last scene-changing step.
        self.config: config.Config | None = None  # Loaded on the main thread by send().
        self.scene_before: dict | None = None
        self.scene_after: dict | None = None
        self.notes: list = []       # (text, chat item) sent while the turn runs; folded in at the next step.
        self.notes_lock = threading.Lock()  # Appending, folding, claiming and retiring notes share this lock.
        self.send_notes_on_stop = False  # The queued note's explicit Stop helpers and send now action.
        self.ledger = repeats.Ledger()  # Failures and reads the model repeats; see repeats.py.
        self.outcome = "running"  # completed, cancelled, step_limit, no_progress, budget or error; retained by evals.
        self.stop_reason = ""    # The budget or guard that stopped it; preserved through its wrap-up.
        # With workers on (send() read config.load_roles), who does what, and the loop's spec; None
        # for both is today's single loop.
        self.roles: config.Roles | None = None
        self.spec: "LoopSpec | None" = None
        self.job = None              # waves.Job once this turn delegates: budget, checks, JobLog.
        self.delegations: list = []  # Deliverables of each wave this turn started (waves.prepare).
        self.plan_turn = ""         # Identifies this loop's plan attempts, including while a retry is running.
        self.level_first: int | None = None  # Levels inferred from what the model did; see _note_level.
        self.level_max = 0
        # router.py: whether this turn was routed, the decision (None: none), and whether its one
        # nudge towards delegating was sent.
        self.route_tried = False
        self.routed = None
        self.nudged = False
        # The workspace (workspace.py): whether the router may send this orchestrated turn to the single
        # agent's workspace loop (_configure), and whether it did (router.start_turn's divert).
        self.may_divert = False
        self.diverted = False
        self.codex = None  # config.CodexApp when Codex's own loop runs the turn (CODEX_SPEC, codex_app.py).
        self.try_it = ""  # The setup card that sent this turn's request unedited: its cloud call is on the house.
        # Credits (spending.py, C47): paid calls this turn, workers' included, and the burst guard's limit;
        # the user's spending settings, read on the main thread by _configure.
        self.paid_calls = 0
        self.burst_limit = spending.BURST
        self.spending: spending.Settings | None = None
        # A worker's turn (waves.py): the user's (session, turn), whose ledger, burst guard and panel its paid
        # calls share, and the name its asks show there. None and "" for the user's own turn.
        self.chat: tuple | None = None
        self.worker = ""
        # Steps whose reply called tools: an empty reply after work ends the turn as before; one before any
        # work is an error the user can retry (_request).
        self.tool_steps = 0
        # Actual dispatches, including Try it's first call and delegate's helper waves. tool_steps
        # also counts rejected/skipped calls, so it cannot establish that anything ran.
        self.executed_tools = 0
        self.unrun_claim = False  # One record correction per user turn, including resume.
        self.contact_checks: list = []  # Measurements and evaluated geometry fingerprints, only this turn.
        self.contact_bounced = False   # At most one correction for a pass invalidated by later edits.
        # The turn's usage event (telemetry.py): when it started, what ran it, the first chat item it added,
        # and the machine code of the error that ended it.
        self.started: float | None = None
        self.telemetry: dict = {}
        self.first_item = 0
        self.error_code = ""
        self.allowance_generation = state.ui.get("allowance_generation", 0)
        self.hosted_requests = 0
        self.allowance_notices: list = []  # (items, notice) retained if a refused send restores its draft.
        self.submission = None  # A new send can be returned to the input if its first request is refused.
        self.returned_submission = False
        # Its conversation log (session_logs.py): what ran it, or {} when the conversation is not uploaded.
        self.conversation_log: dict = {}


def _redraw() -> None:
    from . import mainthread
    mainthread.request_redraw()


def _on_main(fn, is_cancelled=lambda: False, name: str = ""):
    """fn() on the main thread, waited for here; a stop before the pump reaches it means it never runs.
    `name` is what a slow-task line calls it (mainthread.note_slow)."""
    from . import mainthread

    def guarded():
        if is_cancelled():
            raise llm.Cancelled()
        return fn()

    future = mainthread.run_on_main(mainthread.named(name or mainthread.task_name(fn), guarded))
    while True:
        try:
            return future.result(timeout=_POLL_SECONDS)
        except FutureTimeout:
            if future.done():
                return future.result()  # Propagate a tool's TimeoutError; it is not a polling timeout.
            if is_cancelled() and future.cancel():
                raise llm.Cancelled()
            # A native Blender operation already running cannot be interrupted safely. Keep
            # the turn busy until its result is accounted for; do not start another tool.


def _run_tool_on_main(name: str, arguments: str, is_cancelled=lambda: False):
    from . import mainthread, tools
    if name == "delegate":
        # Refresh on the agent thread before the UI thread builds the workers' opening messages.
        from . import cloud, prompt_index
        try:
            cloud.hosted_index()
        except prompt_index.IndexError as ex:
            return tools.ToolResult(f"Could not start the workers: {ex}. Nothing started.", ok=False)
    def call():
        from . import shaders
        with shaders.deferring():  # A look may wait for its shaders in result.then, below.
            return tools.execute(name, arguments)
    result = _on_main(call, is_cancelled, name=f"the {name} tool")
    if getattr(result, "pending_job", None):
        from . import inspection
        return inspection.wait_result(result.pending_job, is_cancelled, mainthread.run_on_main)
    if getattr(result, "then", None):  # Waits here, between main-thread calls, never in the UI pump.
        return result.then(is_cancelled, lambda fn: _on_main(fn, is_cancelled))
    return result


def _inspection_current(token: dict) -> bool:
    from . import inspection, mainthread
    return mainthread.run_on_main(lambda: token == inspection.live_token()).result()


def _fresh_images(images: list) -> list:
    """A later tool in one model batch can invalidate an earlier asynchronous inspection."""
    fresh = []
    for reference, what, file_preview, token, card, message in images:
        if token is not None and not _inspection_current(token):
            note = "\nInspection image omitted: the scene changed later in this tool batch. Inspect the new revision."
            card["status"], card["output"] = "failed", card["output"] + note
            card.pop("image_path", None)  # Not the picture the model got: none on the card either.
            message["content"] += note
        else:
            fresh.append((reference, what, file_preview))
    return fresh


def _approval(name: str, arguments: str, roots) -> str:
    """"" when the call runs unasked; "gate" when the user must approve it unless they said
    "Always allow" or auto-run is on; "heavy" for a render, a bake, a simulation, which can hold
    Blender for minutes: it asks unless they said "Always allow renders"; "install" for code that
    installs packages or downloads code and runs it: it asks every time (tools.INSTALLS); see _asks."""
    from . import files, tools, workspace
    from . import orchestrator
    parsed = _arguments(arguments)
    from . import cloud
    if cloud.is_tool(name):
        return ""  # Cloud execution gates resolved file uploads and cost consent together.
    if tools.installs(name, parsed) or workspace.installs(name, parsed, roots):
        return "install"
    if tools.is_heavy(name, parsed):
        return "heavy"
    if (name in tools.NEEDS_APPROVAL and not _only_reads(name, parsed) and not orchestrator.waits(name, parsed)
            or files.needs_approval(name, parsed, roots) or workspace.needs_approval(name, parsed, roots)):
        return "gate"
    return ""


INSTALL_REFUSAL = ("Not run: installing packages or running downloaded code needs the user's OK, and a worker "
                   "cannot ask. Do without it, or report back what you need.")


def _asks(approval: str, name: str, session: dict, cfg) -> bool:
    """Whether the step waits for the user. "Always allow renders" waives only the render itself:
    code that renders is still code, so it also needs "Always allow" or auto-run."""
    from . import tools
    if approval == "install":
        return True  # Neither auto-run nor any "Always allow" covers putting third-party code on their machine.
    allowed = cfg.auto_run or session["auto_run"]
    if approval == "heavy":
        return not session["auto_heavy"] or (name in tools.NEEDS_APPROVAL and not allowed)
    return bool(approval) and not allowed


def _only_reads(name: str, parsed: dict) -> bool:
    """A scene-changing tool's call that only measures: frame_camera report_only, delegate dry_run."""
    from . import orchestrator, tools
    return tools.only_reads(name, parsed) or orchestrator.only_reads(name, parsed)


def _changes_scene(name: str, arguments: str = "") -> bool:
    from . import cloud, tools
    return cloud.changes_scene(name) or name in tools.CHANGES_SCENE and not _only_reads(name, _arguments(arguments))


def _same_while_unchanged(name: str, arguments: str = "") -> bool:
    from . import tools
    return name in tools.SAME_WHILE_UNCHANGED or _only_reads(name, _arguments(arguments))


def _changes_over_time(name: str, arguments: str) -> bool:
    from . import cloud, tools
    return cloud.tool_name(name) == "job_status" or tools.changes_over_time(name, _arguments(arguments))


def _in_view(step_message: dict) -> bool:
    """Whether a step's results are still sent whole, not folded into its record."""
    return not step_message.get(context.FOLDED)


def _ensure_checkpoint_on_main(session: dict) -> None:
    from . import mainthread
    mainthread.run_on_main(mainthread.named("the turn's checkpoint save",
                                            lambda: checkpoints.ensure_for_turn(session))).result()


def _scene_context(text: str, since: dict | None) -> str:
    from . import scene_context
    return scene_context.for_message(text, since)


def _addons_line() -> str:
    from . import addons, bundled_addons
    try:
        bundled = bundled_addons.packages()
    except bundled_addons.BundledAddonError as ex:  # A broken bundle manifest never stops a turn.
        print(f"Loopcut: bundled add-ons: {ex}")
        bundled = []
    return addons.line(addons.enabled(), bundled)


MODEL_ROUTES = {"chatgpt": "the user's ChatGPT plan (Sign in with ChatGPT)", "loopcut": "the user's Loopcut account",
                "local": "a model server on this computer"}


def _model_line(cfg: config.Config | None) -> str:
    """Main thread: the model this turn runs on and how it is reached, one line, so the model can name itself
    (F0288: it sent the user to ChatGPT's own model picker)."""
    from . import account, cloud, settings
    if cfg is None or not cfg.model:
        return ""
    prefs, source = settings.preferences(), cloud.model_source()
    name = account.model_label(cfg.model) if source == "loopcut" else cfg.model
    route = MODEL_ROUTES.get(source) or (f"the user's own {settings.provider_label(prefs.provider)} key"
                                         if prefs is not None else "")
    head = f"You are {name}, reached through {route}" if route else f"You are {name}"
    return head + "; the user picks the model in Loopcut's model menu, in this panel."


def _setup_line(cfg: config.Config | None) -> str:
    """Main thread: the model line, and the cloud tools this turn can't call and why (cloud.unavailable_line), so
    "can you generate images?" gets a reason and an alternative in a line (F0276). They sit at the prompt's end
    with the add-ons line: the cache pays only when they change."""
    from . import cloud
    lines = []
    for what, make in (("the model", lambda: _model_line(cfg)), ("the unavailable cloud tools", cloud.unavailable_line)):
        try:
            lines.append(make())
        except Exception as ex:  # A fact line is a nicety; the turn goes on without it.
            print(f"Loopcut: could not describe {what}: {ex!r}")
    return "\n".join(line for line in lines if line)


def _system_prompt(session: dict) -> str:
    """The fixed prompt plus what is installed, read at the turn's start on the main thread."""
    extra = session.get("addons_line") or ""
    return f"{SYSTEM_PROMPT}\n\n{extra}" if extra else SYSTEM_PROMPT


def _system_tail(session: dict) -> str:
    extra = session.get("addons_line") or ""
    return "\n\n" + extra if extra else ""


def _scene_snapshot() -> dict:
    from . import scene_diff
    return scene_diff.snapshot()


def _resend_on_main(text: str, note_items: list[dict]) -> None:
    """A note that arrived too late to be folded in starts the next turn by itself."""
    from . import mainthread
    mainthread.run_on_main(lambda: send(text, note_items=note_items))


def _restore_input_on_main(text: str, session: dict | None = None) -> None:
    from . import mainthread
    from .ui import textedit

    def restore():
        target = session if session is not None else state.session()
        if session is not None:
            textedit.set_text(target, "\n".join(t for t in (target["input"], text) if t))
        elif not target["input"]:
            textedit.set_text(target, text)
    mainthread.run_on_main(restore)


def _fold_notes(session: dict, turn: Turn) -> None:
    """What the user sent while the turn ran goes in before the next request, marked so the
    model continues rather than starts over. The chat item drops its 'queued' tag."""
    with turn.notes_lock:
        if turn.send_notes_on_stop:
            return  # The cancelled turn hands these to the next turn after its helpers have stopped.
        while turn.notes:
            text, item = turn.notes.pop(0)
            item.pop("queued", None)
            session["messages"].append({"role": "user", context.STEER: True, "content": f"<steer>{text}</steer>"})


def _project_roots() -> tuple:
    from . import files
    return files.project_roots()


def _keeps_context(cfg: config.Config) -> bool:
    from . import account
    return account.keeps_context(cfg.base_url, cfg.model)


def _keep_mode(cfg: config.Config) -> config.Config:
    """Keep mode switched on when the Loopcut service lists the model with it."""
    if not cfg.keep_context and _keeps_context(cfg):
        cfg = dataclasses.replace(cfg, keep_context=True)
    return cfg


def _turn_config() -> config.Config:
    return _keep_mode(config.load())


def _configure(turn: Turn, session: dict | None = None) -> None:
    """Main thread: the turn's config and spec. With workers on and a known prompt index,
    ORCH_SPEC, keep mode resolved per role; otherwise the single loop. With LOOPCUT_WORKSPACE on, the
    single loop is WORKSPACE_SPEC, and so is a conversation the router sent there (session["mode"]);
    an orchestrated conversation that never delegated may be sent there by the router (_route). With
    LOOPCUT_CODEX_APP on, every turn is the workspace's, run by Codex (CODEX_SPEC). Raises ConfigError."""
    session = session if session is not None else state.session()
    from . import cloud
    cloud.sync_settings()
    turn.spending = config.spending()
    codex = config.codex_app()
    if codex is not None:
        session["mode"] = "workspace"
        turn.config, turn.spec, turn.codex = _turn_config(), CODEX_SPEC, codex
        return
    roles = config.load_roles()
    # The disk cache normally supplies this before the first refresh completes. With no
    # known index yet (or an older server), the open single loop is safe for the first turn.
    orchestrate = roles.orchestrate and cloud.prompt_index_available()
    if roles.workspace and (session.get("mode") == "workspace" or not orchestrate):
        session["mode"] = "workspace"
        turn.config = _workspace_config(_keep_mode(roles.orchestrator) if roles.orchestrate else _turn_config())
        turn.spec = WORKSPACE_SPEC
        return
    if orchestrate:
        turn.roles = dataclasses.replace(roles, orchestrator=_keep_mode(roles.orchestrator),
                                         worker=_keep_mode(roles.worker))
        turn.config, turn.spec = turn.roles.orchestrator, ORCH_SPEC
        turn.may_divert = roles.workspace and _never_delegated(session)
    else:
        turn.config = _keep_mode(roles.orchestrator) if roles.orchestrate else _turn_config()
        turn.spec = SINGLE_SPEC


def _update_account(info: dict) -> None:
    from . import account
    account.update_usage(info)


def _hosted_reply(turn: Turn, cfg: config.Config, info: dict | None = None, *, success: bool = False,
                  session: dict | None = None) -> None:
    if not config.on_service(cfg.base_url):
        return
    if success:
        turn.hosted_requests += 1
    from . import mainthread

    def apply():
        if turn.allowance_generation != state.ui.get("allowance_generation", 0):
            return  # A signed-out account or a provider we left cannot repopulate the display/meter.
        if info:
            _update_account(info)
        if success:
            state.clear_allowance_notice()
        if info and session is not None:
            _warn_low_allowance(session, session["items"], info, turn=turn)
    mainthread.run_on_main(apply)


def _hosted_refusal(session: dict, turn: Turn, item: dict, info: dict | None = None) -> None:
    """Return a refused new send to its own draft; retain one account notice, never a send gate."""
    def apply():
        if info and turn.allowance_generation == state.ui.get("allowance_generation", 0):
            _update_account(info)  # A plan change must clear old refusals before installing this one.
        previous = state.ui.get("allowance_notice") or {}
        windows = dict(previous.get("windows") or {})
        previous_item = previous.get("item") or {}
        previous_window = allowance.LIMIT_WINDOWS.get(previous_item.get("reason"))
        if previous_window:
            windows[previous_window] = dict(previous_item)
        owner = turn
        while owner.chat:
            owner = owner.chat[1]
        # A Try it tool can already have changed the scene before the first hosted request.
        snapshot = turn.submission if not turn.hosted_requests and not turn.tool_steps else None
        if snapshot:
            extra = [i["text"] for i in session["items"][len(snapshot["items"]) + 1:] if i.get("kind") == "user"]
            session["items"][:] = snapshot["items"]
            session["items"].extend(notice for items, notice in owner.allowance_notices
                                    if items is session["items"] and not any(row is notice for row in items))
            session["messages"][:] = snapshot["messages"]
            for key in ("references", "scene_seen", "scene_turn_start", "pending_checkpoint", "turn_number"):
                session[key] = snapshot[key]
            from .ui import textedit
            draft = "\n".join(t for t in (snapshot["text"], *extra, session["input"]) if t)
            textedit.set_text(session, draft)
            refs = {a["ref"] for a in snapshot["attachments"]}
            session["attachments"] = snapshot["attachments"] + [a for a in session["attachments"] if a["ref"] not in refs]
            turn.notes.clear()
            turn.returned_submission = True
        latest = state.latest_limit(session["items"])
        if not turn.hosted_requests and latest is not None:
            existing = session["items"][latest]
            existing.clear()
            existing.update(item)
            item_to_show = existing
        else:
            item_to_show = item
            if not snapshot:
                session["items"].append(item)
        if turn.allowance_generation == state.ui.get("allowance_generation", 0):
            account_info = state.ui.get("account") or {}
            window = allowance.LIMIT_WINDOWS.get(item["reason"])
            reported = account_info.get(window) or {}
            if not item_to_show.get("resets_at") and isinstance(reported, dict):
                item_to_show["resets_at"] = reported.get("resets_at") or ""
            if not item_to_show.get("plan"):
                item_to_show["plan"] = account_info.get("plan") or ""
            if window:
                windows[window] = dict(item_to_show)
            state.ui["allowance_notice"] = {"item": item_to_show, "conversation": session["id"], "windows": windows}
    _on_main(apply)
    if info:
        from . import mainthread
        # Only draft restoration waits for the main thread, as in the ordinary refusal path.
        # Its percentage warning is optional and never delays turn finalisation or saving.
        mainthread.run_on_main(lambda: _warn_low_allowance(
            session, session["items"], info, turn=turn, refused=allowance.LIMIT_WINDOWS.get(item["reason"])))


def _allowance_notice_matches(row: dict, name: str, key: str, reset: str | None) -> bool:
    if row.get("kind") != "notice":
        return False
    if row.get("allowance_window"):
        return row["allowance_window"] == name and (row.get("resets_at") or row.get("plan") or "unknown") == key
    # 0.2.9 stored only text. Its dated session reset identifies the window; relative
    # resets and undated weekly notices may get one upgrade duplicate with new metadata.
    if name != "session" or not reset:
        return False
    prefix, separator, suffix = row.get("text", "").partition("%")
    return (bool(separator) and prefix.startswith("About ") and prefix[6:].isdigit()
            and suffix == " of this session's chat allowance is left. "
            f"Resets {state.reset_label(reset, now=0)}.")


def _warn_low_allowance(session: dict, items: list, info: dict, *, turn: Turn | None = None,
                        refused: str | None = None) -> None:
    """Main thread: warn once per server window; the ordinary conversation saves persist it."""
    owner = turn
    while owner is not None and owner.chat:
        session, owner = owner.chat
        items = session["items"]
    if owner is not None:
        if owner.allowance_generation != state.ui.get("allowance_generation", 0):
            return
        if owner.config is not None and not config.on_service(owner.config.base_url):
            return
    live = state.session()
    if session is not live and (not session.get("id") or session.get("id") != live.get("id")):
        return  # The conversation is closed; a later response can carry the same warning.
    # Resolve reopened conversations by id, and unsaved/id-less sessions by object identity.
    # Never append to a captured conversation or save from this callback.
    session, items = live, live["items"]
    refused_windows = allowance.refused_windows(info, state.ui.get("allowance_notice"))
    for name in ("session", "week"):
        if name == refused or name in refused_windows:
            continue
        window = info.get(name)
        used = allowance.used(window)
        if used is None or used < LOW_ALLOWANCE:
            continue
        reset = window.get("resets_at")
        key = reset or info.get("plan") or "unknown"
        flag = f"warned_{name}_allowance"
        warned = state.ui.get(flag) or []
        if isinstance(warned, str):  # A hot reload can retain the old single-window marker.
            warned = [warned]
        in_history = any(_allowance_notice_matches(row, name, key, reset) for row in items)
        if key in warned:
            continue
        state.ui[flag] = [*warned, key]  # Delayed helper replies may arrive out of window order.
        if in_history:
            continue
        when = allowance.reset_sentence(reset)
        item = state.item_notice(f"You've used about {round(used * 100)}% of {allowance.window_label(name)} "
                                 f"chat allowance.{when}")
        item.update(allowance_window=name, resets_at=reset or "", plan=info.get("plan") or "")
        items.append(item)
        if owner is not None:
            owner.allowance_notices.append((items, item))


def _limit_item(ex: llm.LLMError) -> dict:
    """The card for a 402: Loopcut's gateway says which window ran out, when it resets and what
    the next plan offers; any other provider's 402 shows its message with no button."""
    details = ex.details
    upgrade = details.get("upgrade") if isinstance(details.get("upgrade"), dict) else None
    info = ex.account or {}
    window = info.get({"session_limit": "session", "week_limit": "week"}.get(ex.code, "")) or {}
    return state.item_limit(str(details.get("message") or ex), ex.code or "limit",
                            str(details.get("plan") or info.get("plan") or ""),
                            str(details.get("resets_at") or window.get("resets_at") or ""), upgrade)


def _alike_on_main(a, b) -> bool:
    from . import mainthread, tools
    return mainthread.run_on_main(mainthread.named("comparing two captures",
                                                   lambda: tools.images_alike(a, b))).result()


SAME_LOOK_NOTE = ("\n\n(Looked again from {angle}: no visible difference from your last capture at this size, so no "
                  "new image. A small or hidden change may not show at {width} px wide; capture_viewport with a "
                  "focus gives a closer look.)")


def _changes_item(session: dict, turn: Turn) -> dict | None:
    """The card that says what this turn did to the scene, with the way back."""
    from . import scene_diff
    if turn.scene_before is None or turn.scene_after is None:
        return None
    changes = scene_diff.diff(turn.scene_before, turn.scene_after)
    if scene_diff.is_empty(changes):
        return None
    checkpoint = next((i["checkpoint"] for i in reversed(session["items"])
                       if i["kind"] == "user" and i.get("checkpoint")), "")
    return state.item_changes(scene_diff.headline(changes), scene_diff.lines(changes), checkpoint)


def _tool_schemas() -> list[dict]:
    from . import assets, camera, cloud, contact, files, inspection, job_tools, texture_tools, tools, uv_tools, video_attach
    return video_attach.with_clip_tools(
        tools.SCHEMAS + contact.SCHEMAS + files.SCHEMAS + job_tools.SCHEMAS + inspection.SCHEMAS + assets.SCHEMAS
        + camera.SCHEMAS + uv_tools.SCHEMAS + texture_tools.SCHEMAS + cloud.tool_schemas())


def _tool_title(name: str, schemas: Callable | None = None) -> str:
    """A card label from registered metadata, or readable words for an unknown tool."""
    for schema in (schemas or _tool_schemas)():
        function = schema["function"]
        if function["name"] == name:
            title = str(function.get("title") or "").strip()
            if title:
                return title
    return " ".join(name.replace("_", " ").replace(".", " ").split()).capitalize()


def _describe(call: llm.ToolCall, schemas: Callable | None = None) -> tuple[str, str]:
    try:
        arguments = json.loads(call.arguments) if call.arguments.strip() else {}
    except ValueError:
        arguments = None
    if not isinstance(arguments, dict):
        title = "Helper plan is empty or invalid" if call.name == "delegate" else _tool_title(call.name, schemas)
        return title, call.arguments
    if call.name == "inspect_scene":
        from . import inspection
        return inspection.describe(arguments)
    from . import assets, camera, cloud, contact, files, job_tools, tools
    name = call.name
    if tools.RENAMED.get(name, (name,))[0] != name:  # A saved call to a tool since merged.
        name, arguments = tools.current_call(name, arguments)
    from . import orchestrator, paint_tools, texture_tools, uv_tools, workspace
    for module in (job_tools, files, assets, contact, camera, orchestrator, uv_tools, texture_tools, paint_tools,
                   workspace, cloud):
        described = module.describe(name, arguments)
        if described:
            return described
    return str(arguments.get("summary") or _tool_title(name, schemas)), str(arguments.get("code") or "")


def _arguments(arguments: str) -> dict:
    try:
        parsed = json.loads(arguments) if arguments.strip() else {}
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _plan_attempt(card: dict, call: llm.ToolCall, session: dict, turn_id: str) -> None:
    """Record identity before delegate can start workers or replace their continued threads.
    The panel derives recovery from later successful cards still present in this turn."""
    if call.name != "delegate":
        return
    from . import orchestrator, waves
    arguments = _arguments(call.arguments)
    if orchestrator.waits(call.name, arguments):
        return
    card["plan_turn"] = turn_id
    card["plan_dry_run"] = orchestrator.only_reads(call.name, arguments)
    # Identity only, not argument repair: malformed calls still go through tools.execute unchanged.
    # Some providers put workers at the top level or encode wave/workers as JSON strings.
    def decoded(value):
        if isinstance(value, str):
            try:
                return json.loads(value)
            except ValueError:
                return None
        return value

    wave = decoded(arguments.get("wave"))
    crew = decoded(wave.get("workers") if isinstance(wave, dict) else arguments.get("workers"))
    if isinstance(crew, list) and crew and all(
            isinstance(w, dict) and isinstance(w.get("id"), str) and w["id"].strip() for w in crew):
        live = {"threads": orchestrator.threads_of(session),
                "busy": waves.busy(getattr(session.get("turn"), "job", None))}
        card["plan_workers"] = [[w["id"], orchestrator.effective_role(w, live)] for w in crew]
    elif not isinstance(crew, list) or not any(
            isinstance(w, dict) and isinstance(w.get("id"), str) and w["id"].strip() for w in crew):
        # No worker ids: the view must match any supplied names, or (with no usable names)
        # require the very next delegate call in this turn to start.
        card["plan_unidentified"] = True
    if isinstance(crew, list):
        roles = dict(card.get("plan_workers") or [])
        card["plan_names"] = [[value.strip() if isinstance(value, str) else "" for value in
                               (w.get("deliverable"), roles.get(w["id"], w.get("role"))
                                if isinstance(w.get("id"), str) else w.get("role"))]
                              for w in crew if isinstance(w, dict)]
        for pair in card["plan_names"]:
            if pair[1]:
                pair[1] = orchestrator.normalise_role(pair[1])


def _wants_capture(arguments: str) -> bool:
    return bool(_arguments(arguments).get("capture"))


def _asked_angle(call_name: str, arguments: str) -> str:
    """The angle a look asks for, or "" when it is not a look with an angle of its own."""
    parsed = _arguments(arguments)
    if call_name == "run_python":
        return str(parsed.get("capture") or "")
    if call_name in CAPTURE_TOOLS - {"inspect_scene"}:
        return str(parsed.get("angle") or DEFAULT_ANGLE)
    return ""


def _image_what(text: str) -> str:
    """What an image shows, from the tool's own words: the last 'Image attached: ...' line."""
    line = next((ln for ln in reversed(text.splitlines()) if ln.startswith("Image attached: ")), "")
    what = line[len("Image attached: "):].split(" Nearest to the viewpoint", 1)[0]
    return what.rstrip(".")


def _last_look(messages: list) -> str | None:
    """The newest capture's image reference, if there is one."""
    for message in reversed(messages):
        images = context._image_refs(message) if context.is_capture(message) else []
        if images:
            return images[0]
    return None


def _without_capture(arguments: str) -> str:
    parsed = json.loads(arguments)
    parsed.pop("capture", None)
    return json.dumps(parsed)


def _wait_for_approval(turn: Turn) -> bool:
    while not turn.decided.wait(_POLL_SECONDS):
        if turn.cancel.is_set():
            raise llm.Cancelled()
    if turn.cancel.is_set():
        raise llm.Cancelled()
    return turn.approved


_card_waits: dict = {}  # id(card) -> _CardWait: an ask answered on its own card (a worker's, in the user's panel).


class _CardWait:
    def __init__(self):
        self.event, self.approved = threading.Event(), False

    def answer(self, approved: bool) -> None:
        self.approved = approved
        self.event.set()


def _wait_for_card(card: dict, turns) -> bool:
    """A worker's ask: wait for its own card's answer, so a main-turn card waiting at the same time keeps its own."""
    wait = _card_waits[id(card)] = _CardWait()
    try:
        while not wait.event.wait(_POLL_SECONDS):
            if any(t.cancel.is_set() for t in turns):
                raise llm.Cancelled()
        if any(t.cancel.is_set() for t in turns):
            raise llm.Cancelled()
        return wait.approved
    finally:
        _card_waits.pop(id(card), None)


def _save_spend(session: dict) -> None:
    """A charge known after the turn ended (a detached job) is saved with its conversation now; a running turn
    saves it with its next step."""
    if not session.get("busy"):
        conversations.save(session)


class _Consent:
    """One cloud call's consent and charge, bound to its card (cloud_account.approval_context). Cost consent is per
    call, even when ordinary scene edits are auto-approved: spending.decide says whether it asks. Every ask pauses
    the turn on a card (owner, 2026-10-16); Approve runs the call, "Skip this" declines only this call and the model
    is told why. A worker asks in the user's panel, named, and waits. A detached job reports its charge here later
    (cloud_account.charge_reporter), so the chat's spend follows what was really charged."""

    settles = True  # cloud_account.charge_reporter sends it what each call was charged.

    def __init__(self, session, turn, card, asks=True, files_allowed=False):
        self.session, self.turn, self.card = session, turn, card
        self.asks, self.files_allowed = asks, files_allowed
        self.hold = None       # (credits held, ran without asking) once the call may run.
        self.limit_approved = False  # The user approved past a server limit, seeing this call's cost: no second ask.
        self.shown = [card]    # The cards that show its cost: its own, and a worker's ask in the user's panel.

    def chat(self):
        if getattr(self.turn, "chat", None):
            return self.turn.chat
        return (self.session if self.session is not None else state.session()), self.turn

    def __call__(self, details):
        if "error" in details:
            for card in self.shown:
                card["cloud_error"] = details["error"]
                card["expanded"] = True
            _redraw()
            return True
        if "charged" in details:
            self._settle(details["charged"], details.get("text") or "")
            return True
        self.card["cost"] = details["text"]
        if details.get("file_access"):
            if not details["required"] or self.files_allowed:
                return True
            if not self.asks:
                return False
            self.turn.decided.clear()
            self.card.update(status="awaiting", paid_approval=False, heavy=False)
            _redraw()
            approved = _wait_for_approval(self.turn)
            self.card["status"] = "running" if approved else "rejected"
            _redraw()
            return approved
        if details.get("limit"):  # A server limit refused it (cloud_account._confirmed): ask, then it goes again.
            approved = self._ask(details["limit"], details)
            # Its card showed the price unless a quote was still coming; then the price asks on its own.
            self.limit_approved = approved and details.get("credits") is not None
            return approved
        chat_session, chat_turn = self.chat()
        settings = getattr(chat_turn, "spending", None) or spending.Settings()
        explicit, allowed = self.limit_approved, set()  # A limit card that showed the price was its approval.
        while not explicit:
            with spending.lock:
                kind = spending.decide(details, spending.ledger(chat_session), settings, calls=chat_turn.paid_calls,
                                       burst_limit=chat_turn.burst_limit,
                                       vendors=chat_session.setdefault("spend_vendors", []))
            if kind == spending.RUN or kind in allowed:  # Allowed once for this call (a budget smaller than it).
                break
            allowed.add(kind)
            if not self._ask(kind, details):
                return False
            with spending.lock:
                spending.allow(kind, details, spending.ledger(chat_session), settings, chat_turn,
                               chat_session["spend_vendors"])
            if kind in (spending.COST, spending.VENDOR):  # The user approved this very call.
                explicit = True
                break
        with spending.lock:
            chat_turn.paid_calls += 1
            held = spending.start(spending.ledger(chat_session), details, not explicit)
        self.hold = (held, not explicit) if held else None
        _redraw()
        return True

    def _ask(self, kind: str, details: dict) -> bool:
        """Pause on an approval card until the user answers. False when nobody can be asked (a worker with no user
        turn behind it), which refuses the call; "Skip this" raises the declined result the model reads."""
        from . import cloud_http
        chat_session, chat_turn = self.chat()
        worker = self.turn.worker if getattr(self.turn, "chat", None) else ""
        if not self.asks and not worker:
            return False
        settings = getattr(chat_turn, "spending", None) or spending.Settings()
        with spending.lock:
            lines = spending.ask_lines(kind, details, spending.ledger(chat_session), settings,
                                       calls=chat_turn.paid_calls, balance=details.get("balance"), worker=worker)
        fields = {"ask": kind, "ask_lines": lines, "ask_label": spending.approve_label(kind, settings),
                  "paid_approval": True, "heavy": False, "install": False}
        if details.get("url"):
            fields["ask_link"] = details["url"]
        if worker:
            card = state.item_tool(self.card.get("name", ""), f"{worker}: {self.card.get('summary') or ''}".rstrip(": "), "")
            card.update(cost=details["text"], worker=worker, **fields)
            card["status"] = "awaiting"
            if card not in self.shown:
                self.shown.append(card)
            chat_session["items"].append(card)
            _redraw()
            try:
                approved = _wait_for_card(card, (chat_turn, self.turn))
            except llm.Cancelled:
                card["status"] = "rejected"
                raise
        else:
            card = self.card
            self.turn.decided.clear()
            card.update(status="awaiting", **fields)
            _redraw()
            approved = _wait_for_approval(self.turn)
        for key in ("ask", "ask_lines", "ask_label", "ask_link"):
            card.pop(key, None)
        card["status"] = ("done" if worker else "running") if approved else "rejected"
        _redraw()
        if not approved:
            self.card["declined"] = True
            raise cloud_http.CloudError(spending.declined(kind, details, settings, calls=chat_turn.paid_calls),
                                        "declined")
        return True

    def _settle(self, charged, text: str) -> None:
        if self.hold is None and not charged:
            return  # Nothing held and nothing charged: a free call keeps "Free", a skipped one its price.
        if text:
            for card in self.shown:
                card["cost"] = text
        if self.hold is not None and charged is not None:
            held, auto = self.hold
            self.hold = None  # Once: a job's watcher and its job_status may both report.
            chat_session, _ = self.chat()
            with spending.lock:
                spending.settle(spending.ledger(chat_session), held, charged, auto)
            _save_spend(chat_session)
        _redraw()


def _cloud_consent(turn, card, asks, details, *, files_allowed=False, session=None):
    """One answer from a fresh _Consent (tests and simple callers); the loop binds a _Consent per call."""
    return _Consent(session, turn, card, asks, files_allowed)(details)


def _image_message(reference: str, what: str, *, file_preview: bool = False) -> dict:
    """An image reference, expanded when a request is sent. Its origin and what it shows are
    text, so they outlive the image."""
    text = (f"{context.FILE_PREVIEW_LABEL}{what or 'image'}. "
            "This is an image file; viewing it does not change the open scene." if file_preview else
            f"{context.CAPTURE_LABEL}{what or 'the scene'} (after the step above).")
    return {"role": "user", context.CAPTURE: True, "content": [
        {"type": "text", "text": text},
        {"type": "image_url", "image_url": {"url": reference}},
    ]}


def _scene_changed(result) -> bool:
    before = getattr(result, "scene_before", None)
    return before is not None and before != getattr(result, "scene_after", None)


def _capture_refusal(turn: Turn, call_name: str, arguments: str) -> str:
    """Why this look is refused, or "" when it may go ahead. Counts it either way, and remembers
    the angle for the automatic looks. Only the same look repeated at an unchanged scene is
    refused: a look after a change, or from another angle, is what the loop is for."""
    angle = _asked_angle(call_name, arguments)
    if not angle:
        return ""
    turn.captures += 1
    turn.angle = angle
    if call_name == "capture_viewport":
        parsed = _arguments(arguments)
        focus = parsed.get("focus")
        turn.focus = [str(name) for name in focus] if isinstance(focus, list) else []
        turn.shot = {k: parsed[k] for k in ("camera", "view_layer") if isinstance(parsed.get(k), str) and parsed[k]}
    look = f"{call_name} {json.dumps(_arguments(arguments), sort_keys=True)}"
    if call_name != "run_python" and not turn.changed and look == turn.last_look:
        turn.idle_captures += 1
        if turn.idle_captures > MAX_IDLE_CAPTURES:
            return (f"the scene has not changed since your last look, and you have asked for this same look "
                    f"{MAX_IDLE_CAPTURES} times. Change something first, look another way, or finish and tell "
                    f"the user what you could not verify.")
    else:
        turn.idle_captures = 0
    turn.last_look = look
    return ""


def _look_note(turn: Turn) -> str:
    """Appended to a look the model asked for once it has asked for many: a nudge, not a wall."""
    if turn.captures <= SOFT_LOOKS:
        return ""
    return (f"\n\n(Look {turn.captures} this turn. Every look costs the user an image; you already get one "
            f"after each change. Finish soon, and say what you could not verify.)")


def _correlation_headers(session: dict, role: str) -> dict:
    """This session's x-loopcut-conversation / x-loopcut-role (llm.correlation_headers) for the
    given role (orchestrator/worker/single here; router.py and context.py send their own)."""
    return llm.correlation_headers(session["id"], role)


def _reference_card(session: dict, cfg: config.Config, message: dict, is_cancelled, role: str = "single",
                    turn: Turn | None = None) -> list[str]:
    """Describe the images the user just attached, so the description outlives the turn and any
    summary, and the model works from a checklist rather than a glance. Returns their names. A clip's
    contact sheets are left out: they are frames to time an action by, not an image to reproduce, and the
    message describes them already (videoref.user_content)."""
    sheets = {r["ref"] for r in session["references"] if r.get("video")}
    refs = [p["image_url"]["url"] for p in message["content"]
            if p.get("type") == "image_url" and p["image_url"]["url"] not in sheets]
    names = [r["name"] for r in session["references"] if r["ref"] in refs]
    if not refs:
        message[context.REFERENCE_CARD] = True
        return []
    stills = {**message, "content": [p for p in message["content"]
                                     if p.get("type") != "image_url" or p["image_url"]["url"] in refs]}
    wired = conversations.wire_messages(session["id"], [stills])[0]["content"]
    parts = [{"type": "text", "text": "Reference images: " + ", ".join(names) + "."}] + [p for p in wired if p.get("type") == "image_url"]
    completion = llm.stream_chat(
        base_url=cfg.base_url, api_key=cfg.api_key, model=cfg.model,
        messages=[{"role": "system", "content": REFERENCE_PROMPT}, {"role": "user", "content": parts}],
        tools=[], reasoning_effort="low", on_text=lambda _: None, is_cancelled=is_cancelled,
        max_tokens=cfg.max_output_tokens, call_max_seconds=cfg.call_max_seconds,
        headers=_correlation_headers(session, "reference"), on_retry=lambda: None)  # Nothing shown mid-stream to undo.
    if turn is not None:
        _hosted_reply(turn, cfg, completion.account, success=True, session=session)
    card = completion.text.strip()
    if card:
        message["content"][0]["text"] += f"\n\n<reference_card images=\"{', '.join(names)}\">\n{card}\n</reference_card>"
    message[context.REFERENCE_CARD] = True
    return names


def _auto_look(session: dict, turn: Turn, run_tool, alike) -> tuple[str, "Path | None"]:
    """The look the agent takes once a reply's steps changed the scene, unless the model looked
    itself after the last change: what it would ask for next anyway, without the request that
    asking costs. Taken after the whole reply, so a look the model placed after its change is
    never doubled by this one. Not counted as a look the model asked for; a failure is not the
    step's failure. A look that shows the same picture as the previous capture is a note, not an
    image: the capture is free, the image in every later request is not.
    Returns (text for the step's result, image path or None)."""
    from . import tools
    latest = (turn.scene_after or {}).get("objects") or {}
    focus = [name for name in dict.fromkeys(turn.touched) if name in latest]
    # Else the model's own last focus, kept like its angle: a worker's changes carry no snapshots, so
    # its auto-look went through the camera after a focused final look (analysis §5.5).
    focus = focus or [name for name in turn.focus if name in latest or not latest]
    turn.touched = []
    try:
        shot = run_tool("capture_viewport", json.dumps({"angle": turn.angle, **turn.shot,
                                                        **({"focus": focus} if focus else {})}))
        if (focus or turn.shot) and not shot.ok:  # Something named is gone after all: the whole scene, as before.
            shot = run_tool("capture_viewport", json.dumps({"angle": turn.angle}))
    except llm.Cancelled:
        return "", None  # The mutation already ran; keep its result before honoring Stop.
    if not (shot.ok and shot.image_path):
        return f"\n\nAutomatic preview unavailable: {shot.text}", None
    turn.changed = False
    previous = _last_look(session["messages"])
    path = shot.image_path
    try:
        path = image_budget.compact_file(path)  # As stored, like the previous look: JPEG's noise is no change.
        same = previous is not None and alike(conversations.image_path(session["id"], previous), path)
    except Exception as ex:  # Comparing is a saving, never a reason to lose the look.
        print(f"Loopcut: could not compare the capture with the previous one: {ex!r}")
        same = False
    if same:
        return SAME_LOOK_NOTE.format(angle=turn.angle, width=tools.CAPTURE_WIDTH), None
    return f"\n\n{shot.text}", path


class BudgetStop(Exception):
    """Raised by a LoopSpec's before_request when a budget shared beyond this loop is spent (a
    job's dollars or a worker's time): the loop stops asking for tools and wraps up."""


@dataclasses.dataclass
class LoopSpec:
    """Every choice the loop makes that differs by role, so one loop serves the single agent
    (today's), the orchestrator and the workers. The prompt and tools are fixed for a
    conversation's life, so the provider's cached prefix holds."""
    role: str                                   # "orchestrator" | "worker" | "single" (today)
    system: Callable[[dict], str]               # session -> system prompt
    schemas: Callable[[], list]                 # the tools offered
    save: Callable[[dict], None]                # conversations.save, or a worker's transcript writer
    asks: bool = True                           # False: never waits for approval (workers)
    checkpoints: bool = True                    # False: never checkpoints the live scene (workers)
    changes_card: bool = True                   # False: no "what changed" card at the end (workers)
    scene_changed: Callable = _scene_changed    # result -> whether it changed the scene
    before_request: Callable[[int], None] | None = None    # step -> None; may wait, or raise BudgetStop
    on_usage: Callable[[dict], None] | None = None          # after every completion, with its usage ({} if none)
    after_step: Callable[[dict, "Turn"], bool] | None = None  # True: stop now (a worker reported back)
    # A reply with no tool calls, or a stopped turn's wrap-up: text to send back as a message
    # that continues the turn (the finish gate's bounce), or a result whose text and image go back
    # (the workers it waited for, with their contact sheet), or None to let the turn end.
    on_final: Callable[[dict, "Turn", "llm.Completion | None"], Any] | None = None
    # Before the first request (the router): may add to the message that started the turn.
    on_turn_start: Callable[[dict, "Turn"], None] | None = None
    steps_left_note: str = STEPS_LEFT_NOTE      # Appended STEPS_LEFT_NOTICE steps before the step limit.
    # (session, tool name, result) -> text appended to that result: the orchestrator's note when its own
    # run_python changed an object a worker built (orchestrator.edit_note). Informational only.
    after_tool: Callable[[dict, str, Any], str] | None = None
    # (session, turn) as the turn ends, however it ends: the orchestrator's workers still running stop
    # after a Stop, and are waited for and merged after an error (waves.end_turn).
    on_end: Callable[[dict, "Turn"], None] | None = None
    # False: attached images go to the model as they are, with no <reference_card> (the workspace agent,
    # which has them at full size in its folder; a card's wording is a cheaper reading of them).
    reference_card: bool = True
    # (session, turn, cfg, run_tool, ensure_checkpoint) -> outcome: another agent loop runs the turn after
    # on_turn_start (Codex's, codex_app.run_turn), its tool calls run through run_tool; the turn's start and
    # end, Stop and errors stay this loop's.
    drive: Callable | None = None
    prompt_id: str | None = None                # Hosted injection; workspace/summary remain open.


# Today's single loop. The lambdas look the functions up when called, so tests can replace them.
SINGLE_SPEC = LoopSpec(role="single", system=lambda session: _system_prompt(session),
                       schemas=lambda: _tool_schemas(), save=lambda session: conversations.save(session), prompt_id="single")


def _orchestrator_schemas() -> list[dict]:
    from . import orchestrator, uv_tools
    return [s for s in _tool_schemas() if s["function"]["name"] not in uv_tools.SINGLE_ONLY] + orchestrator.SCHEMAS


def _session_schemas(session: dict, spec: LoopSpec) -> list[dict]:
    schemas = spec.schemas()
    if session.get("workers_unavailable"):
        from . import orchestrator, waves
        job = getattr(session.get("turn"), "job", None)
        collecting = job is not None and waves.pending(job)
        schemas = [orchestrator.WAIT_SCHEMA if s["function"]["name"] == "delegate" else s
                   for s in schemas if collecting or s["function"]["name"] != "delegate"]
    return schemas


def _acceptance_run(checks: list, spawn=None) -> list:
    from . import acceptance
    return acceptance.run(checks, spawn)


def _route(session: dict, turn: Turn) -> None:
    """The router's decision for this turn, as a note on the user's message (router.py). With the
    workspace on, a conversation that never delegated and whose turn is a look (router.to_workspace)
    goes to the single agent's workspace loop instead, for this turn and the rest of the conversation:
    no note, the orchestrator's model, WORKSPACE_SPEC (run_loop picks up turn.spec)."""
    if turn.try_it and turn.tool_steps:
        return  # The card already selected and ran its tool; continue with its result, without routing again.
    from . import router
    roles = getattr(turn, "roles", None)
    divert = router.to_workspace if getattr(turn, "may_divert", False) else None
    def stream(**kwargs):
        completion = llm.stream_chat(**kwargs)
        _hosted_reply(turn, roles.router, completion.account, success=True, session=session)
        return completion
    router.start_turn(session, turn, roles.router if roles else None, turn.cancel.is_set, stream=stream, divert=divert)
    if getattr(turn, "diverted", False):
        session["mode"] = "workspace"
        turn.spec = WORKSPACE_SPEC
        turn.config = _workspace_config(turn.config)
        turn.roots = _on_main(_project_roots, turn.cancel.is_set)  # Relative paths now mean the workspace.
        _workspace_turn(session, turn)


def _never_delegated(session: dict) -> bool:
    """No orchestrator-only tool was ever called: the single agent's tools cover the whole history."""
    from . import orchestrator
    own = {s["function"]["name"] for s in orchestrator.SCHEMAS}
    return not any(call.get("function", {}).get("name") in own for m in session["messages"]
                   for call in m.get("tool_calls") or ())


def _orchestrator_edit_note(session: dict, name: str, result) -> str:
    from . import orchestrator
    return orchestrator.edit_note(session, name, result)


def _orchestrator_final(session: dict, turn: Turn, completion) -> str | None:
    """A level-3 turn that delegated nothing is nudged once; then the finish gate."""
    from . import router
    nudge = None if session.get("workers_unavailable") else router.nudge(turn, completion)
    return nudge or _finish_gate(session, turn, completion)


def _orchestrator_end(session: dict, turn: Turn) -> None:
    from . import waves
    waves.end_turn(session, turn)


def _finish_gate(session: dict, turn: Turn, completion):
    """A turn that delegated ends only once Loopcut has run its checks on the live scene."""
    from . import orchestrator
    job = getattr(turn, "job", None)
    if job is None:
        return None
    run_checks = job.checker or (lambda checks: _on_main(lambda: _acceptance_run(checks, job.baseline),
                                                         turn.cancel.is_set))
    return orchestrator.finish_gate(session, turn, completion, run_checks)


# The orchestrator: today's prompt and tools (level 1 unchanged) plus the workers section and
# delegate, checks and accept_quarantine; the router's note before its first request,
# its nudge and the finish gate on its last reply.
ORCH_SPEC = LoopSpec(role="orchestrator", system=lambda session: _system_tail(session), prompt_id="orchestrator",
                     schemas=lambda: _orchestrator_schemas(), save=lambda session: conversations.save(session),
                     on_final=lambda session, turn, completion: _orchestrator_final(session, turn, completion),
                     on_turn_start=lambda session, turn: _route(session, turn),
                     after_tool=lambda session, name, result: _orchestrator_edit_note(session, name, result),
                     on_end=lambda session, turn: _orchestrator_end(session, turn))


def _workspace_prompt(session: dict) -> str:
    from . import workspace
    return workspace.system_prompt(session.get("addons_line") or "")


def _workspace_schemas() -> list[dict]:
    from . import cloud, video_attach, workspace
    # An attached clip's tools too (the ChatGPT plan's route), as its <video_reference> note names them.
    live = workspace.LIVE_TOOLS + (video_attach.CLIP_TOOLS if video_attach.has_clip(state.session()) else ())
    return [s for s in _tool_schemas() if s["function"]["name"] in live
            or cloud.is_tool(s["function"]["name"])] + workspace.SCHEMAS


def _workspace_config(cfg: config.Config) -> config.Config:
    """Keep mode, and a budget of at least context.WORKSPACE_BUDGET: the workspace agent's steps are not
    rewritten into records mid-turn (Codex keeps a thread whole until its window fills)."""
    return dataclasses.replace(cfg, keep_context=True,
                               context_budget=max(cfg.context_budget, context.WORKSPACE_BUDGET))


def _workspace_turn(session: dict, turn: Turn) -> None:
    """The workspace made and noted on the turn's message, with the craft notes its words call for."""
    from . import cloud, skills, workspace
    options = cloud.skill_options()
    craft = None
    if config.load_roles().single_craft:
        if getattr(turn, "codex", None):
            from . import codex_app
            available = {s["name"] for s in codex_app.dynamic_tools()} | {"shell"}
        else:
            available = {s["function"]["name"] for s in _workspace_schemas()}
        options = {**options, "available_tools": available | set(options.get("available_tools", ()))}
        craft = lambda text: skills.single_notes(text, role="workspace", **options)
    workspace.start_turn(session, turn.cancel.is_set, lambda fn: _on_main(fn, turn.cancel.is_set), craft=craft)


# The single agent in workspace mode (workspace.py, LOOPCUT_WORKSPACE=on): its own short prompt, the
# workspace tools and a few live ones, as a coding agent works in a terminal that has Blender.
WORKSPACE_SPEC = LoopSpec(role="single", system=lambda session: _workspace_prompt(session),
                          schemas=lambda: _workspace_schemas(), save=lambda session: conversations.save(session),
                          on_turn_start=lambda session, turn: _workspace_turn(session, turn), reference_card=False)


def _codex_turn(session: dict, turn: Turn, cfg: config.Config, run_tool, ensure_checkpoint) -> str:
    from . import codex_app
    return codex_app.run_turn(session, turn, cfg, run_tool, ensure_checkpoint)


# LOOPCUT_CODEX_APP: the workspace turn run by Codex's own agent loop, Loopcut's live tools as its dynamic tools.
CODEX_SPEC = dataclasses.replace(
    WORKSPACE_SPEC, drive=lambda session, turn, cfg, run_tool, ensure_checkpoint: _codex_turn(
        session, turn, cfg, run_tool, ensure_checkpoint))


# Tools that act without being scene changes checkpoints cover: files written, renders started, and the
# workspace's commands and edits.
_ACTS = {"write_file", "move_file", "shell", "run_blender", "edit_file"}


def _acts(name: str, arguments: str) -> bool:
    parsed = _arguments(arguments)
    return (_changes_scene(name, arguments) or name in _ACTS
            or name == "render_job" and parsed.get("action") in ("start", "resume")
            or name == "see_render" and str(parsed.get("render")).lower() == "true")


def _note_level(turn: Turn, name: str, arguments: str, delegations_before: int) -> None:
    """Level from actions, no declaration: a read is 0, any other action 1, a wave that started 2
    for one deliverable and 3 for more; over the turn, 3 once two or more deliverables were handed
    out in all. first is the first action's level, so a later delegate after level-1 work is an
    escalation (final > first). A refused or dry-run delegate is no action."""
    if name == "delegate":
        started = turn.delegations[delegations_before:]
        level = (3 if max(len(d) for d in started) >= 2 else 2) if started else 0
    else:
        level = 1 if _acts(name, arguments) else 0
    if not level:
        return
    if turn.level_first is None:
        turn.level_first = level
    handed = {deliverable for wave in turn.delegations for deliverable in wave}
    turn.level_max = max(turn.level_max, level, 3 if len(handed) >= 2 else 0)


def _record_level(session: dict, turn: Turn) -> None:
    """session["turn_levels"]: {turn, first, final}, plus routed (the router's level, None when it
    gave none) once the router ran and nudged once it nudged; a resumed turn updates its own entry."""
    levels = session.setdefault("turn_levels", [])
    number = session.get("turn_number") or 1
    entry = {"turn": number, "first": turn.level_first or 0, "final": turn.level_max}
    if getattr(turn, "route_tried", False):
        entry["routed"] = turn.routed.level if turn.routed else None
    if getattr(turn, "nudged", False):
        entry["nudged"] = True
    if levels and levels[-1]["turn"] == number:
        previous = levels[-1]
        entry = {**previous, **entry, "first": previous["first"] or entry["first"],
                 "final": max(previous["final"], entry["final"])}
        levels[-1] = entry
    else:
        levels.append(entry)
    job = getattr(turn, "job", None)
    if job is not None:
        job.log["level"] = entry["final"]
        job.write()


def _completion_unusable(completion: llm.Completion) -> bool:
    """Hit the output cap (the model's own finish_reason "length", or llm.py's call_max_seconds
    reporting the same way) without giving the loop anything it can act on: no reply text, and no
    tool call whose arguments survived intact -- one cut mid-argument is invalid JSON, not a usable
    call, so it is treated the same as no call at all."""
    if completion.finish_reason != "length" or completion.text.strip():
        return False
    for call in completion.tool_calls:
        if not call.arguments.strip():
            continue
        try:
            json.loads(call.arguments)
        except ValueError:
            return True
    return not completion.tool_calls


def _completion_empty(completion: llm.Completion) -> bool:
    """Nothing to show and nothing to run: no text, no tool call, whatever the finish reason (stop, a
    provider's own "network_error", a content filter's "sensitive"), except the output cap, which
    _completion_unusable already handles."""
    return (not completion.tool_calls and not completion.text.strip() and completion.finish_reason != "length"
            and not completion.timed_out)


def _completion_fragment(completion: llm.Completion) -> bool:
    """A text-only stop of a few reported output tokens that does not end a sentence ("I'll"): a reply cut off,
    not an answer. Glm-5.3-flash never answered under 28 tokens in ~200 sampled first turns (2026-10-20); user
    D's had 4, counting reasoning."""
    text = completion.text.strip()
    tokens = (completion.usage or {}).get("output")
    return (not completion.tool_calls and completion.finish_reason == "stop" and 0 < len(text) <= 16
            and text[-1] not in ".!?)…:" and isinstance(tokens, int) and tokens <= 4)


def _empty_kind(completion: llm.Completion) -> str:
    """For the log: which kind of nothing."""
    tokens = (completion.usage or {}).get("output")
    what = "reasoning only" if completion.reasoning_chars else "empty"
    return f"{what} (finish_reason {completion.finish_reason!r}, {tokens if tokens is not None else '?'} output tokens)"


def _drop_empty(items: list, reply: dict) -> None:
    reply["streaming"] = False
    if not reply["text"].strip() and reply in items:
        items.remove(reply)


def _request(session: dict, turn: Turn, cfg: config.Config, spec: LoopSpec = SINGLE_SPEC) -> llm.Completion:
    """One request with the conversation as it stands, streamed into a reply card. A reply that
    hits the output cap (max_tokens, or call_max_seconds -- llm.py reports both as finish_reason
    "length") without a usable reply or tool call is retried once: the same messages, plus a short
    instruction to decide and act now, and at effort "high" instead of "max". A second cap hit in a
    row raises llm.LLMError, which run_loop already shows as this step's error."""
    items = session["items"]
    from . import cloud, prompt_index
    hosted = spec.prompt_id if config.on_service(cfg.base_url) else None
    if spec.prompt_id in ("worker", "orchestrator") and not hosted:
        raise config.ConfigError("Workers and orchestration require the Loopcut service")
    try:
        index = cloud.hosted_index(required=hosted != "single") if hosted else None
    except prompt_index.IndexError as ex:
        if hosted == "orchestrator":
            raise config.ConfigError("Loopcut could not load its service prompt index; try again in a moment") from ex
        if hosted != "single":
            raise
        print("Loopcut: could not load the service prompt index; using the open single-loop prompt")
        index = None
    if hosted == "single" and index is None:
        hosted = None  # Older servers and unavailable manifests use the open prompt and its budget.
    system = _system_tail(session) if hosted in ("single", "orchestrator") else spec.system(session)
    if session.get("workers_unavailable"):
        from . import waves
        system += "\n\n" + waves.unavailable_note(session)
    schemas = _session_schemas(session, spec)
    fixed = context.fixed_tokens(system, schemas, prompt_id=hosted, index=index)
    prepared, compacted = context.prepare(session, cfg, turn.cancel.is_set, fixed=fixed, skill_index=index)
    if compacted:
        items.append(state.item_notice(
            f"Summarized {compacted} earlier messages so requests stay small."))
    estimated = context.estimate_tokens(prepared, fixed, skill_index=index)
    reply = state.item_assistant()
    items.append(reply)

    def on_text(delta: str) -> None:
        reply["text"] += delta
        _redraw()

    def on_retry() -> None:
        """A read timeout or reset broke the stream after some of this reply had already shown; the
        card is reset so the retried request's own text does not land on top of the stale partial
        reply (llm.stream_chat retries once, from scratch, only when this is given)."""
        reply["text"] = ""
        _redraw()

    full_refs = {r["ref"]: r.get("full") or r["ref"] for r in session.get("references") or []} \
        if session.get("mode") == "workspace" else None
    wired = [*([{"role": "system", "content": system}] if system else []),
             *conversations.wire_messages(session["id"], prepared, context.unpinned_refs(session), cfg.keep_context,
                                          full_refs=full_refs)]

    def call(messages: list[dict], effort: str) -> llm.Completion:
        try:
            return llm.stream_chat(
                base_url=cfg.base_url, api_key=cfg.api_key, model=cfg.model,
                messages=messages, tools=schemas, reasoning_effort=effort,
                on_text=on_text, is_cancelled=turn.cancel.is_set,
                max_tokens=cfg.max_output_tokens, call_max_seconds=cfg.call_max_seconds,
                headers=_correlation_headers(session, spec.role), on_retry=on_retry,
                prompt_id=hosted,
            )
        except llm.LLMError as ex:
            if config.on_service(cfg.base_url) and ex.code in state.HOSTED_LIMITS:
                _drop_empty(items, reply)
            raise

    def record(completion: llm.Completion) -> None:
        _hosted_reply(turn, cfg, completion.account, success=True, session=session)
        if completion.usage:
            for key in ("input", "cached", "output"):
                session["usage"][key] += completion.usage.get(key, 0)
            session["usage"]["context"] = completion.usage["input"]
            context.calibrate(session, estimated, completion.usage["input"])
        bucket = session.setdefault("usage_by_role", {}).setdefault(
            spec.role, {"input": 0, "cached": 0, "output": 0, "requests": 0})
        for key in ("input", "cached", "output"):
            bucket[key] += (completion.usage or {}).get(key, 0)
        bucket["requests"] += 1
        if spec.on_usage:
            spec.on_usage(completion.usage or {})

    def cap_reason(completion: llm.Completion) -> str:
        return "the wall-clock limit" if completion.timed_out else f"the {cfg.max_output_tokens}-token output cap"

    completion = call(wired, cfg.reasoning_effort)
    record(completion)
    if _completion_unusable(completion):
        retry_effort = "high" if cfg.reasoning_effort == "max" else cfg.reasoning_effort
        print(f"Loopcut: {spec.role} step hit {cap_reason(completion)} with no usable reply or tool call; "
              f"retrying once at effort={retry_effort}.")
        completion = call(wired + [{"role": "user", "content": CAP_RETRY_PROMPT}], retry_effort)
        record(completion)
        if _completion_unusable(completion):
            reason = cap_reason(completion)
            print(f"Loopcut: {spec.role} step hit {reason} again on the retry; giving up on this step.")
            reply["streaming"] = False
            if not reply["text"].strip():
                items.remove(reply)
            raise llm.LLMError(f"{spec.role.capitalize()} step stopped at {reason} twice in a row, "
                               f"without a usable reply or tool call.")
    fragment = not getattr(turn, "tool_steps", 0) and _completion_fragment(completion)
    if (_completion_empty(completion) or fragment) and not turn.cancel.is_set():
        if completion.finish_reason in llm.FILTERED_FINISHES:
            _drop_empty(items, reply)
            print(f"Loopcut: {spec.role} reply stopped by the provider's content filter "
                  f"(finish_reason {completion.finish_reason!r}).")
            raise llm.LLMError(FILTERED_REPLY_MESSAGE, code=llm.CONTENT_FILTER)
        print(f"Loopcut: {spec.role} step came back "
              f"{'a cut-off fragment' if fragment else _empty_kind(completion)}; retrying once.")
        reply["text"] = ""  # The retry's own text, not appended to a fragment.
        _redraw()
        completion = call(wired, cfg.reasoning_effort)
        record(completion)
        if _completion_empty(completion) and not turn.cancel.is_set():
            print(f"Loopcut: {spec.role} step came back {_empty_kind(completion)} again.")
            if completion.finish_reason in llm.FILTERED_FINISHES:
                _drop_empty(items, reply)
                raise llm.LLMError(FILTERED_REPLY_MESSAGE, code=llm.CONTENT_FILTER)
            if not getattr(turn, "tool_steps", 0):  # Nothing happened this turn: say so, with Try again.
                _drop_empty(items, reply)
                raise llm.LLMError(EMPTY_REPLY_MESSAGE, code=llm.EMPTY_REPLY)
    reply["streaming"] = False
    finish_reply(reply, turn.roots[0] if turn.roots else None)
    if not reply["text"].strip():
        items.remove(reply)
    return completion


def _wrap_up(session: dict, turn: Turn, cfg: config.Config, spec: LoopSpec = SINGLE_SPEC,
             prompt: str = "") -> llm.Completion | None:
    """After a guard or budget stops the turn, one more request, asking for text only, so it ends with the
    model saying where the work stands rather than a bare stop. The tools stay in the request, so
    it extends the last one and the provider's cached prefix holds. tool_choice "none" would say
    it better, but measured 2026-09-27 GLM and MiMo ignore it and Muse Spark refuses it (HTTP 400);
    all three answered this appended message in text. A tool call in the reply is dropped; with no
    text, or any failure, the bare stop stands, without a retry. Returns the completion kept, if any."""
    if turn.cancel.is_set():
        return None
    items, messages = session["items"], session["messages"]
    first = len(items)
    # Marked so it continues the turn: as a turn start it would fold the turn's steps (context.py).
    messages.append({"role": "user", context.STEP_LIMIT: True,
                     "content": prompt or STEP_LIMIT_PROMPT.format(steps=cfg.max_steps)})
    try:
        completion = _request(session, turn, cfg, spec)
    except Exception as ex:  # Cancelled too. Whatever went wrong, the turn still ends as it would have.
        print(f"Loopcut: no summary after {turn.outcome}: {ex!r}")
        completion = None
    for item in [i for i in items[first:] if i["kind"] == "assistant" and not i["text"].strip()]:
        items.remove(item)  # The reply card of a request that failed, or of a reply with only tool calls.
    if completion and completion.text.strip():
        # The text only: a tool call can no longer run, and one left unanswered breaks the history.
        messages.append({"role": "assistant", "content": completion.text})
        return completion
    messages.pop()  # Unanswered, it would tell the next turn that tools are off.
    return None


def _end_at_limit(session: dict, turn: Turn, cfg: config.Config, spec: LoopSpec, prompt: str = "") -> None:
    """The turn ends short of finishing: the wrap-up, then the spec's last word (it cannot bounce)."""
    completion = _wrap_up(session, turn, cfg, spec, prompt)
    _final_reply(session, turn, completion, spec)


def _final_reply(session: dict, turn: Turn, completion, spec: LoopSpec):
    """After the existing finish gate (including late merges), validate this turn's contact evidence."""
    if turn.cancel.is_set():
        return None
    bounce = spec.on_final(session, turn, completion) if spec.on_final else None
    if bounce and turn.outcome == "running":
        return bounce
    from . import contact
    return contact.finish_gate(session, turn, completion,
                               lambda: _on_main(lambda: contact.refresh(turn), turn.cancel.is_set))


def _run(session: dict, turn: Turn, run_tool=_run_tool_on_main,
         ensure_checkpoint=_ensure_checkpoint_on_main, alike=None) -> None:
    """The turn's loop with the spec send() chose; today's single loop when it chose none."""
    try:
        run_loop(session, turn, getattr(turn, "spec", None) or SINGLE_SPEC, run_tool, ensure_checkpoint, alike)
    finally:
        try:
            from . import telemetry
            telemetry.turn_end(turn, list(session["items"]))  # Off, signed out of stats or headless: nothing.
        except Exception as ex:
            print(f"Loopcut: turn usage event not recorded: {type(ex).__name__}")
        try:
            from . import session_logs
            session_logs.on_turn_end(session, turn)  # Sharing off, LOOPCUT_TELEMETRY off or headless: nothing.
        except Exception as ex:
            print(f"Loopcut: conversation log not sent: {type(ex).__name__}")


def _error_code(ex) -> str:
    """A machine code for the turn's usage event: the API's code, else its HTTP status. Never the message."""
    code = getattr(ex, "code", "")
    if isinstance(code, str) and code:
        return code
    status = getattr(ex, "status", None)
    return f"http_{status}" if isinstance(status, int) else ("config" if isinstance(ex, config.ConfigError) else "llm_error")


def run_loop(session: dict, turn: Turn, spec: LoopSpec, run_tool=_run_tool_on_main,
             ensure_checkpoint=_ensure_checkpoint_on_main, alike=None) -> None:
    items, messages = session["items"], session["messages"]
    first_item = len(items)
    request = next((m for m in reversed(messages) if context.is_turn_start(m)), {})
    # A resumed loop has a fresh Turn, but these facts belong to the original user message.
    # Keeping them there also preserves them through save/load and checkpoint restoration.
    turn.executed_tools = request.get(completion_guard.EXECUTED, turn.executed_tools)
    turn.unrun_claim = request.get(completion_guard.CORRECTED, turn.unrun_claim)
    scene_action = completion_guard.scene_request(request)
    plan_turn = uuid.uuid4().hex
    alike = alike or _alike_on_main
    if run_tool is _run_tool_on_main:
        run_tool = lambda name, arguments: _run_tool_on_main(name, arguments, turn.cancel.is_set)
    try:
        cfg = turn.config or config.load()
        first = None
        if turn.try_it and not turn.tool_steps:
            from . import try_it
            call = try_it.first_call(turn.try_it, session, _session_schemas(session, spec))
            if call is not None:
                first = llm.Completion(tool_calls=[call])
        # A card's tool consumes its reference directly. No reference-model request is needed for it.
        describe_reference = first is None
        user_message = messages[-1] if messages else {}
        started = False
        no_progress = repeats.NoProgress()
        plan_turn = turn.plan_turn = uuid.uuid4().hex
        session["active_turn_id"] = plan_turn
        step = 0
        while not cfg.max_steps or step < cfg.max_steps:
            if turn.cancel.is_set():
                raise llm.Cancelled()
            if first is None and not started:
                if (describe_reference and spec.reference_card and user_message.get(conversations.ATTACHED)
                        and not user_message.get(context.REFERENCE_CARD)):
                    try:
                        names = _reference_card(session, cfg, user_message, turn.cancel.is_set, spec.role, turn)
                        if names:
                            items.append(state.item_notice("Studied the reference: " + ", ".join(names)))
                    except llm.LLMError as ex:
                        if config.on_service(cfg.base_url) and ex.code in state.HOSTED_LIMITS:
                            raise
                        print(f"Loopcut: could not describe the attached images: {ex}")
                if spec.on_turn_start:
                    spec.on_turn_start(session, turn)
                    if getattr(turn, "spec", None) not in (None, spec):  # The router sent the turn to another loop.
                        spec = turn.spec
                if scene_action is None:
                    scene_action = bool(turn.routed and turn.routed.level > 0)
                if spec.drive:
                    turn.outcome = spec.drive(session, turn, cfg, run_tool, ensure_checkpoint)
                    return
                started = True
            step += 1
            if spec.before_request:
                spec.before_request(step)
            if turn.notes:
                turn.ledger.scene_changed()  # What the user says mid-turn may come with edits of their own.
                if no_progress:
                    no_progress.reset()
            _fold_notes(session, turn)
            # Synthetic and model tool calls share cards, consent, checkpoints, results and saving below.
            completion = first if first is not None else _request(session, turn, cfg, spec)
            first = None
            messages.append(completion.as_message())
            if not completion.tool_calls:
                bounce = None
                if (spec.role != "worker" and scene_action and not turn.executed_tools
                        and not turn.unrun_claim and not turn.cancel.is_set()
                        and completion_guard.imitates_records(completion.text, (
                            s["function"]["name"] for s in _session_schemas(session, spec)))):
                    turn.unrun_claim = request[completion_guard.CORRECTED] = True
                    bounce = completion_guard.CORRECTION
                if not bounce:
                    bounce = _final_reply(session, turn, completion, spec)
                if bounce:  # Sent back, marked so it continues the turn, and the loop goes on.
                    text = bounce if isinstance(bounce, str) else bounce.text
                    messages.append({"role": "user", context.STEP_LIMIT: True, "content": text})
                    if getattr(bounce, "image_path", None):  # The late workers' contact sheet.
                        messages.append(_image_message(conversations.store_image(session, bounce.image_path),
                                                       _image_what(text), file_preview=True))
                    spec.save(session)
                    _redraw()
                    continue
                turn.outcome = "cancelled" if turn.cancel.is_set() else "completed"
                return

            step_message = messages[-1]
            turn.tool_steps += 1
            images = []
            changed_by = None  # (card, tool message) of the reply's last step that changed the scene.
            # Call key -> [its number in this reply, the ledger's revision after it]: the same call twice
            # in one reply runs once, unless something that may change the scene ran in between (a
            # run_python that moved to another frame, then the same look again).
            seen = {}
            stalled = False
            for number, call in enumerate(completion.tool_calls, 1):
                if turn.cancel.is_set():
                    raise llm.Cancelled()
                summary, code = _describe(call, spec.schemas)
                card = state.item_tool(call.name, summary, code)
                card["turn_id"] = plan_turn  # Keep correction status scoped to this turn, including folded notes.
                items.append(card)
                key = repeats.call_key(call.name, call.arguments)
                read = _same_while_unchanged(call.name, call.arguments)
                earlier = seen.get(key)
                twice = earlier is not None and earlier[1] == turn.ledger.revision
                skip = ("" if _changes_over_time(call.name, call.arguments)  # A job's status moves on by itself.
                        else f"Not run: duplicate of call #{earlier[0]} in this step; see its result." if twice
                        else turn.ledger.refusal(key, _in_view) if read else "")
                if not twice:
                    seen[key] = [number, turn.ledger.revision]
                if skip:  # Not run, so not failed: "rejected" is the card of a step that did not run.
                    card["status"], card["output"] = "rejected", skip
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": skip})
                    if no_progress and no_progress.skip(key):
                        stalled = True
                        break
                    continue
                approval = _approval(call.name, call.arguments, turn.roots)
                if approval == "install" and not spec.asks:  # A worker cannot ask, so it cannot install.
                    card["status"] = "rejected"
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": INSTALL_REFUSAL})
                    continue
                if spec.asks and _asks(approval, call.name, session, cfg):
                    turn.decided.clear()
                    # heavy: asking because it renders, so the card's "always" allows renders.
                    card["status"] = "awaiting"
                    card["heavy"] = approval == "heavy" and not session["auto_heavy"]
                    card["install"] = approval == "install"
                    _redraw()
                    if not _wait_for_approval(turn):
                        if no_progress:
                            no_progress.reset()
                        card["status"] = "rejected"
                        messages.append({"role": "tool", "tool_call_id": call.id,
                                         "content": "The user rejected this action. Ask what they want instead."})
                        continue
                card["status"] = "running"
                _redraw()
                arguments, note = call.arguments, ""
                refusal = _capture_refusal(turn, call.name, arguments)
                if refusal:
                    if call.name in CAPTURE_TOOLS:
                        card["status"] = "failed"
                        card["output"] = "Not captured: " + refusal.split(".")[0] + "."
                        messages.append({"role": "tool", "tool_call_id": call.id, "content": f"Not captured: {refusal}"})
                        if no_progress and no_progress.skip(key):
                            stalled = True
                            break
                        continue
                    arguments, note = _without_capture(arguments), f"\n\nCapture skipped: {refusal}"
                if spec.checkpoints and _changes_scene(call.name, call.arguments):
                    try:
                        ensure_checkpoint(session)
                    except checkpoints.CheckpointError as ex:
                        # No way back means no going forward: the step is not run.
                        card["status"] = "failed"
                        card["output"] = f"Not run. {ex}"
                        messages.append({"role": "tool", "tool_call_id": call.id, "content":
                                         f"This step was NOT run, because a checkpoint could not be saved "
                                         f"first: {ex} Tell the user; do not retry."})
                        if no_progress and no_progress.skip(key):
                            stalled = True
                            break
                        continue
                if turn.cancel.is_set():
                    raise llm.Cancelled()
                delegations = len(turn.delegations)
                _plan_attempt(card, call, session, plan_turn)
                from . import cloud_account
                with cloud_account.approval_context(_Consent(session, turn, card, spec.asks,
                                                             files_allowed=cfg.auto_run or session['auto_run'])):
                    turn.executed_tools += 1
                    request[completion_guard.EXECUTED] = turn.executed_tools
                    result = run_tool(call.name, arguments)
                if spec.role != "worker":
                    _note_level(turn, call.name, call.arguments, delegations)
                changed = spec.scene_changed(result)
                may_change = _changes_scene(call.name, call.arguments)
                if may_change or changed:
                    # Any run of a scene-changing tool, not only a measured change: scene_diff does
                    # not see every setting, and a read after such a step may well differ.
                    turn.ledger.scene_changed()
                    seen[key][1] = turn.ledger.revision  # Its own double right after it is still one call.
                if no_progress:
                    # Workers return the child's measured change flag instead of full snapshots.
                    # Without either, a potentially mutating tool is conservatively progress.
                    measured = ((spec.role == "worker" and hasattr(result, "changed"))
                                or (getattr(result, "scene_before", None) is not None
                                    and getattr(result, "scene_after", None) is not None))
                    stalled = no_progress.record(key, result.ok, changed or (may_change and not measured))
                result.text += turn.ledger.record(step, key, result.text, result.ok, read, step_message)
                if note:
                    result.text += note
                if spec.after_tool:
                    result.text += spec.after_tool(session, call.name, result) or ""
                asked_look = call.name in CAPTURE_TOOLS or (call.name == "run_python" and _wants_capture(arguments))
                if changed:
                    turn.changed, turn.idle_captures = True, 0
                    if getattr(result, "scene_before", None) is not None and getattr(result, "scene_after", None) is not None:
                        from . import scene_diff
                        turn.touched += scene_diff.touched(result.scene_before, result.scene_after)
                if asked_look:  # After the change: run_python's own capture is taken after its code ran.
                    result.text += _look_note(turn)
                    turn.changed, turn.touched = False, []
                # A paid call the user skipped did not fail: it is shown as skipped (spending.declined).
                card["status"] = "done" if result.ok else "rejected" if card.get("declined") else "failed"
                card["output"] = result.text
                if card.get("plan_turn") and getattr(result, "plan_refused", False):
                    card["plan_repair"] = "needed"
                if getattr(result, "receipt", ""):
                    card["cost"] = result.receipt  # What the paid call used, in place of its estimate (M4).
                for guard in getattr(result, "guard_notes", None) or ():  # Already in result.text too.
                    items.append(state.item_guard(guard["text"], guard["modifiers"]))
                messages.append({"role": "tool", "tool_call_id": call.id, "content": result.text})
                if changed and not asked_look and not refusal:
                    changed_by = (card, messages[-1])
                if result.image_path:
                    # Stored now: every capture is written to the same file, so a second capture in
                    # this batch would otherwise replace the first before it is read.
                    images.append((conversations.store_image(session, result.image_path, card), _image_what(result.text),
                                   getattr(result, "file_image", False), getattr(result, "evidence_token", None),
                                   card, messages[-1]))
                if getattr(result, "scene_before", None) is not None:
                    turn.scene_before = turn.scene_before or result.scene_before
                    turn.scene_after = result.scene_after
                if stalled:
                    break
            if stalled:
                # A failure or refusal can stop the batch; close every pending call before the wrap-up.
                conversations.close_open_tool_calls(messages, "Not run: stopped because no progress was made.")
            last = card, messages[-1]  # The step's last result, where a note on the step budget goes.
            # One automatic look per reply, after all of it, unless the model looked after its last change.
            if changed_by and turn.changed and cfg.auto_look and not turn.cancel.is_set():
                card, message = changed_by
                text, path = _auto_look(session, turn, run_tool, alike)
                card["output"] += text
                message["content"] += text
                if path:
                    images.append((conversations.store_image(session, path, card), _image_what(text), False, None, card, message))
            if not stalled and cfg.max_steps and cfg.max_steps - step == STEPS_LEFT_NOTICE:
                note = spec.steps_left_note.format(left=STEPS_LEFT_NOTICE)
                last[0]["output"] += note
                last[1]["content"] += note
            # Tool messages must directly follow the assistant message, so images go after them all.
            messages.extend(_image_message(reference, what, file_preview=file_preview)
                            for reference, what, file_preview in _fresh_images(images))
            spec.save(session)  # The history is consistent here: every call has its result.
            _redraw()
            if stalled:
                turn.outcome = "no_progress"
                turn.stop_reason = "the no-progress guard (repeated failures or refusals without a scene change)"
                items.append(state.item_notice(NO_PROGRESS_NOTICE))
                _end_at_limit(session, turn, cfg, spec, NO_PROGRESS_PROMPT)
                return
            if spec.after_step and spec.after_step(session, turn):
                turn.outcome = "completed"
                return
        turn.outcome = "step_limit"
        items.append(state.item_error(f"Stopped after {cfg.max_steps} steps without finishing."))
        _end_at_limit(session, turn, cfg, spec)
    except BudgetStop as ex:
        turn.outcome = "budget"
        turn.stop_reason = str(ex)
        items.append(state.item_error(f"Stopped by the budget: {ex}"))
        _end_at_limit(session, turn, cfg, spec, BUDGET_PROMPT.format(reason=ex))
    except llm.Cancelled:
        turn.outcome = "cancelled"
        items.append(state.item_error("Stopped."))
    except (config.ConfigError, llm.LLMError) as ex:
        turn.outcome = "error"
        turn.error_code = _error_code(ex)
        handled_refusal = False
        from . import nudges
        stopped = nudges.chatgpt_card(getattr(ex, "code", "")) if spec.role != "worker" else None
        if stopped is not None:
            items.append(stopped)  # The ChatGPT plan stopped: offer Loopcut's models for the same chat (M2).
        elif getattr(ex, "status", None) == 402:
            item = _limit_item(ex)
            if config.on_service(cfg.base_url) and item["reason"] in state.HOSTED_LIMITS:
                _hosted_refusal(session, turn, item, ex.account)
                handled_refusal = True
            else:
                items.append(item)
            if config.on_service(cfg.base_url) and not ex.account:
                from . import account
                account.refresh_account()  # Older gateways may omit usage headers on a refusal.
        else:
            items.append(state.item_error(str(ex), retry=getattr(ex, "code", "") in (llm.CONNECTION_LOST,
                                                                                     llm.EMPTY_REPLY)))
        if getattr(ex, "account", None) and not handled_refusal:
            _hosted_reply(turn, cfg, ex.account, session=session)
    except Exception as ex:  # Last line of defence for the worker thread: show it, never swallow it.
        turn.outcome = "error"
        turn.error_code = "connection_lost" if llm.dropped_connection(ex) else "internal"
        import traceback
        traceback.print_exc()
        if llm.dropped_connection(ex):  # A provider's client let a broken connection through: plain words.
            items.append(state.item_error(str(llm.connection_lost("the model", ex)), retry=True))
        else:
            items.append(state.item_error(f"Internal error: {ex!r}"))
    finally:
        conversations.close_open_tool_calls(messages, "Cancelled before this ran.")
        end_failed = False
        if spec.on_end:
            try:
                spec.on_end(session, turn)
            except Exception as ex:  # Never keeps the turn from ending.
                import traceback
                traceback.print_exc()
                items.append(state.item_error(f"Internal error while ending the turn: {ex!r}"))
                end_failed = True
        if spec.role != "worker" and not turn.returned_submission:
            _record_level(session, turn)
        changed = _changes_item(session, turn) if spec.changes_card and not turn.returned_submission else None
        if changed:
            items.append(changed)
        if spec.role != "worker" and turn.outcome != "error" and not turn.cancel.is_set():
            from . import nudges
            nudges.flush(session)  # A waiting trial or low-credit card, after the reply, never during it.
        if turn.scene_after is not None and not turn.returned_submission:
            session["scene_seen"] = turn.scene_after  # What the next message's context diffs against.
        for item in items:
            if item.get("streaming"):
                item["streaming"] = False
                finish_reply(item, turn.roots[0] if turn.roots else None)
            if item.get("status") in ("awaiting", "running"):
                item["status"] = "rejected"
        # Keep the display outcome on the last tool, including account notices and teardown errors.
        # Error/stop/notice items keep their existing shape. Restoring before this tool drops the marker.
        turn_cards = [i for i in items[first_item:] if i.get("kind") == "tool" and i.get("turn_id") == plan_turn]
        if turn_cards:
            turn_cards[-1]["turn_outcome"] = "error" if end_failed else turn.outcome
        # Notes that arrived after the last fold: sent as the next message, or, after a stop, put
        # back in the input box for the user to decide. Retire under the append lock so a sender
        # that already captured this turn either joins this drain or retries against the session.
        with turn.notes_lock:
            session["turn_outcome"] = turn.outcome
            session["busy"] = False
            session.pop("active_turn_id", None)
            session["turn"] = None
            late = list(turn.notes)
            turn.notes.clear()
        resend = (spec.role != "worker" and turn.error_code not in state.HOSTED_LIMITS
                  and (not turn.cancel.is_set() or turn.send_notes_on_stop))
        for _, item in late:
            if resend:
                # Keep the original note and its helper receipts in history, including this save.
                item.pop("queued", None)
                item.pop("sending", None)
            elif item in items:
                items.remove(item)
        # Only a correction warrants this notice. Finish hooks may have appended real runtime
        # records to the UI since the model's final text was checked above.
        if (spec.role != "worker" and not spec.drive and not turn.executed_tools
                and turn.unrun_claim):
            items.append(state.item_notice(completion_guard.NOTICE))
        try:
            spec.save(session)
        except OSError as ex:
            items.append(state.item_error(f"Could not save this conversation: {ex}"))
            if turn_cards:
                turn_cards[-1]["turn_outcome"] = "error"
        if late and spec.role != "worker":  # Workers' copies of a note never send or restore into the user's chat.
            joined = "\n".join(text for text, _ in late)
            if turn.error_code in state.HOSTED_LIMITS:
                _restore_input_on_main(joined, session)  # Refused for allowance: the user decides.
            else:
                if resend:
                    _resend_on_main(joined, [item for _, item in late])
                else:
                    _restore_input_on_main(joined)
        _redraw()


def send(text: str, *, note_items: list[dict] | None = None) -> bool:
    """Send a new message, or reuse the original notes handed off by a completed turn."""
    session = state.session()
    draft = text
    text = text.strip()
    attachments = list(session["attachments"])
    while session["busy"]:
        # Mid-turn: the note is queued and folded in at the agent's next step; see _fold_notes.
        turn = session.get("turn")
        if not text:
            return False
        if turn is None:
            continue  # Teardown retired it after the busy check; retry normal sending.
        with turn.notes_lock:
            if session.get("turn") is not turn:
                continue
            queued = note_items if note_items is not None else [state.item_user(text)]
            for item in queued:
                item["queued"] = True
                if turn.send_notes_on_stop:
                    item["sending"] = True
                if not any(existing is item for existing in session["items"]):
                    session["items"].append(item)
                turn.notes.append((item["text"], item))
        if turn.job is not None:
            from . import waves
            for item in queued:
                waves.steer(turn.job, item["text"], item)
        return True
    if not (text or attachments):
        return False
    from . import video_attach
    waiting = video_attach.preparing(session)
    if waiting:
        session["items"].append(state.item_notice(f"Still preparing {', '.join(waiting)}; send once it is ready."))
        return False
    from . import telemetry
    telemetry.first_use("prompt_sent")
    turn = Turn()
    try:
        _configure(turn, session)
    except config.ConfigError as ex:
        session["items"].append(state.item_error(str(ex)))
        telemetry.first_use("result_error")
        return False
    if turn.config is not None and config.on_service(turn.config.base_url):
        turn.submission = {"text": draft, "attachments": attachments, "items": list(session["items"]),
                           "messages": deepcopy(session["messages"]), "references": list(session["references"]),
                           **{k: session.get(k) for k in ("scene_seen", "scene_turn_start", "pending_checkpoint", "turn_number")}}
    turn.roots = _project_roots()
    card, prompt = session.pop("try_it", None) or ("", "")
    if card and prompt.strip() == text:  # Edited first, it is the user's own request.
        turn.try_it = card
    session["turn_number"] = session.get("turn_number", 0) + 1
    user_items = note_items if note_items is not None else [state.item_user(text, [a["name"] for a in attachments])]
    if note_items is not None and attachments:
        user_items[0]["images"] = [*user_items[0].get("images", []), *[a["name"] for a in attachments]]
    checkpoints.begin_turn(session, user_items[0], text=text)
    for item in user_items:
        item.pop("queued", None)
        item.pop("sending", None)
        if not any(existing is item for existing in session["items"]):
            session["items"].append(item)
    # The model also gets what the user is looking at, and what changed since it last acted; the
    # chat shows only what they typed. The snapshot taken here is what changed_only measures from.
    content = f"{_scene_context(text, session.get('scene_seen'))}\n\n{text or 'See the attached images.'}"
    session["scene_seen"] = session["scene_turn_start"] = _scene_snapshot()
    # The prompt's last lines, read here on the main thread: the model and cloud tools, then the add-ons.
    session["addons_line"] = "\n".join(line for line in (_setup_line(turn.config), _addons_line()) if line)
    if attachments:
        # A clip's sheets are images like any other; videoref.user_content adds the clip's own description.
        session["messages"].append({"role": "user", conversations.ATTACHED: True,
                                    "content": videoref.user_content(content, attachments)})
        known = {r["ref"] for r in session["references"]}
        session["references"] += [{"ref": a["ref"], "full": a.get("full", a["ref"]), "name": a["name"], "pinned": True,
                                   **({"source": a["source"]} if a.get("source") else {}),
                                   **({"video": a["video"]} if a.get("video") else {})}
                                  for a in attachments if a["ref"] not in known]
        session["attachments"] = []
    else:
        session["messages"].append({"role": "user", "content": content})
    conversations.save(session)
    _launch(session, turn)
    return True


def _launch(session: dict, turn: Turn) -> None:
    from . import session_logs, telemetry
    turn.started, turn.first_item = time.monotonic(), len(session["items"])
    turn.telemetry = telemetry.turn_start(turn.config)  # Main thread: which kind of model runs this turn.
    turn.conversation_log = session_logs.turn_start(session, turn.config)  # {} when it is not uploaded.
    session["scroll"], session["confirm_restore"] = 0.0, None
    session["busy"], session["turn"] = True, turn
    session["turn_outcome"] = "running"
    turn.thread = threading.Thread(target=_run, args=(session, turn), name="loopcut-turn", daemon=True)
    turn.thread.start()


def resume() -> bool:
    """Go on with the turn that stopped short: after a limit card, once the plan allows it, or
    after any error the user wants retried. Nothing is added to the conversation; the request
    is simply made again. False when a turn is running or the last one finished."""
    session = state.session()
    if not state.can_resume(session):
        return False
    turn = Turn()
    try:
        _configure(turn, session)
    except config.ConfigError as ex:
        session["items"].append(state.item_error(str(ex)))
        return False
    turn.roots = _project_roots()
    _launch(session, turn)
    return True


def decide(approved: bool, always: bool = False, index: int | None = None) -> bool:
    """Answer a pending approval card: the one at `index` when it is waiting (a click), else the first waiting
    (Enter, Esc). `always` stops asking in this conversation: for renders when the card is a render's, otherwise
    for everything else; never for paid or install cards. A worker's ask (a paid call in the background) is
    answered on its own card, so a card of the user's turn waiting at the same time keeps its own answer."""
    session = state.session()
    items = session["items"]
    card = items[index] if index is not None and 0 <= index < len(items) and items[index].get("status") == "awaiting" \
        else next((i for i in items if i.get("status") == "awaiting"), None)
    if card is None:
        return False
    wait = _card_waits.get(id(card))
    if wait is not None:
        wait.answer(approved)
        return True
    turn = session["turn"]
    if turn is None:
        return False
    if approved and always and not card.get("paid_approval") and not card.get("install"):
        session["auto_heavy" if card.get("heavy") else "auto_run"] = True
    turn.approved = approved
    turn.decided.set()
    return True


def send_queued_now() -> bool:
    """Stop helpers through the usual turn cancellation, then send every waiting note once.
    Cleanup and any in-flight main-thread tool finish before the next turn starts."""
    session = state.session()
    turn = session["turn"]
    if turn is None or turn.job is None:
        return False
    with turn.notes_lock:
        if session.get("turn") is not turn or not turn.notes or turn.cancel.is_set():
            return False
        turn.send_notes_on_stop = True
        for _, item in turn.notes:
            item["sending"] = True
        turn.cancel.set()
    _redraw()
    return True


def stop() -> None:
    turn = state.session()["turn"]
    if turn is not None:
        with turn.notes_lock:
            turn.send_notes_on_stop = False
            turn.cancel.set()
