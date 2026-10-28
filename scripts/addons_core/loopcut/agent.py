"""The agent loop: stream a reply, run the tools it asks for, feed results back, repeat."""

import dataclasses
import functools
import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import TimeoutError as FutureTimeout
from copy import deepcopy
from typing import Any, Callable

from . import (allowance, checkpoints, completion_guard, config, context, conversations, image_budget,
               llm, own_key, repeats, result_store, spending, state, videoref)
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
# On the turn's message, with the account's orchestrator_max_steps (Turn.carry_steps): the steps it has made, so a
# resumed turn (resume) goes on counting from them rather than starting a fresh limit.
STEPS_TAKEN = "loopcut_steps_taken"
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
# repeats.STUCK_STEPS steps in a row failed: said to the user, who saw only finished steps from code that caught
# its own errors and a model that never wrote a word (a user's log, 0.2.14). The turn goes on.
STUCK_NOTICE = (f"The last {repeats.STUCK_STEPS} steps failed and nothing in the scene changed. Stop to tell Loopcut "
                "what to try, or let it keep going.")
# Either repeats.NoProgress limit: one call's failures and refusals, or refused repeats of any calls.
NO_PROGRESS_NOTICE = "Stopped because the same steps kept repeating without changing the scene."
NO_PROGRESS_PROMPT = ("[Loopcut] The same tool calls keep failing or being refused as repeats without changing the "
                      "scene, so tools are off now: any tool call is ignored. Reply in plain text only: what is "
                      "done, what is blocked and why, and what you did not verify.")
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
# Loopcut's gateway could not get an answer from the model's vendor: a 502 with one of the upstream_* codes, the
# same code in an error event mid-stream, or the vendor's 429 (rate_limited) still there after llm.py's retries.
# Never the user's plan or request, so the card offers Try again, as after a dropped connection, in the gateway's
# own words without llm.py's "HTTP 502: " (_upstream_text), or UPSTREAM_MESSAGE, the gateway's own line, when it
# gave none. The 429 always says BUSY_MESSAGE: an older gateway's "Loopcut will retry" is no longer true once
# llm.py's retries are spent.
UPSTREAM_CODES = frozenset({"upstream_error", "upstream_rejected", "upstream_unavailable", "rate_limited"})
UPSTREAM_MESSAGE = "Loopcut's AI is having a problem right now; it's not your plan or your request."
BUSY_MESSAGE = "Loopcut's AI is busy right now; try again in a minute."
# The stall clock stopped the turn (check_stall): nothing came back for its limit (stall_message). Try again.
STALL_MESSAGE = "Loopcut stopped waiting: nothing came back for {limit}."
WAKE_SECONDS = 60  # A gap this long between the pump's looks (a sleep) leaves the turn this long before it stalls.
# A reply that wrote its calls out as text and called nothing (_unmade): what goes back as the turn's record
# correction, what the panel says when it cannot go back, and what the stored reply says instead, for this and
# for a reply the record correction (completion_guard.CORRECTION) sends back.
UNMADE_PROMPT = ("[Loopcut] Your last reply wrote {calls} out as text instead of calling {them}, so nothing ran and "
                 "nothing it reported was seen or measured; the scene is as it was before that reply. Call the "
                 "tools now, or tell the user plainly that nothing ran.")
UNMADE_FINAL = ("Stopped: the assistant again wrote out its steps instead of running them, so none of the work "
                "it described was done or checked.")
UNMADE_STORED = context.UNMADE_STORED
# A <scene_context> line on the first message after the user's Stop. Without it the model cannot tell what a press
# refers to: "pressed by mistake, continue" was read as a mistaken press of continue, and it stopped (0.2.14 logs).
STOPPED_LINE = ("last turn: the user pressed Stop before you finished. If this message says that was a mistake or "
                "asks you to go on, carry on with the stopped work; otherwise do what it asks.")
# A <scene_context> line for the user's extras preference (prefs.extras_mode; cloud-tools-implicit-use section 3)
# when it is not "Use when it helps" (AUTO, which says nothing): per turn, so the cached prompt is the same whichever
# they chose. The balance and prices are the gateway's line (0.2.20+), not ours.
EXTRAS_LINES = {"ASK": "Paid tools: ask the user before each one.",
                "ONLY_ASKED": "Paid tools: only when the user asks for one by name."}

REFERENCE_PROMPT = """You are writing a reference card for a 3D artist who must reproduce the attached image in \
Blender as closely as possible. Be terse and concrete, numbers and names over prose, under 300 words:
1. Subject, composition, viewpoint (camera height and angle, wide or long lens), aspect ratio.
2. Every distinct part: shape, size relative to the whole (as ratios), position, orientation, how parts meet.
3. Colors as approximate sRGB hex, materials (matte, glossy, metal, glass; rough or smooth), patterns, textures.
4. Lighting (direction, softness, color) and background.
If several images are attached, describe each under its name."""


# A study the model refused (_refused_card, or a content filter's finish): the user is told, and the picture still
# goes to the model as it is, named as a failed card is (UNDESCRIBED). Unmarked too: a turn resumed after picking
# another model asks that model.
REFERENCE_REFUSED = ("The model declined to study the attached picture. If it can't work from it either, pick "
                     "another model in the model menu below.")
REFUSED_CARD_CHARS = 300
# "Declined" only as speech about the request (anthropic_messages: "Claude declined to continue this request."), never
# bare: a picture's card may say a slope or a chart declines.
_REFUSAL = re.compile(r"\b(?:rejected|refused|high risk|inappropriate|violat\w*|sorry|apologi[sz]e|(?:can ?not|can't|"
                      r"won't|unable to|not able to) (?:help|assist|describe|process|comply|analy[sz]e)|"
                      r"declin(?:e|ed|es|ing) to (?:continue|help|assist|describe|process|comply|analy[sz]e|answer)|"
                      r"(?:must|have to|'ll) decline)\b", re.I)


class Turn:
    def __init__(self):
        self.cancel = threading.Event()
        self.decided = threading.Event()
        self.approved = False
        self.thread: threading.Thread | None = None
        self.own_key: dict = {}  # own_key.for_turn: whose own key runs this turn ({} on Loopcut or the ChatGPT plan).
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
        self.overlaps_refused: set = set()  # Worker id pairs a delegate refused for overlapping (orchestrator.layout).
        # The id of the delegate call running now that went unasked only because it could start nothing
        # (orchestrator.starts_nothing): delegate holds it to the wait or the refusal. None otherwise.
        self.capped_call: str | None = None
        self.plan_turn = ""         # Identifies this loop's plan attempts, including while a retry is running.
        self.level_first: int | None = None  # Levels inferred from what the model did; see _note_level.
        self.level_max = 0
        # router.py: whether this turn was routed, the decision (None: none), and whether its one
        # nudge towards delegating was sent.
        self.route_tried = False
        self.routed = None
        self.nudged = False
        # The user turned down one of this turn's calls on its card (Reject, or Skip on a paid call): the next
        # move is theirs, so the router's nudge stays out of it (rel018-app).
        self.declined = False
        # The workspace (workspace.py): whether the router may send this orchestrated turn to the single
        # agent's workspace loop (_configure), and whether it did (router.start_turn's divert).
        self.may_divert = False
        self.diverted = False
        self.carry_steps = False  # The account's orchestrator_max_steps: steps counted on the turn's message too.
        self.codex = None  # config.CodexApp when Codex's own loop runs the turn (CODEX_SPEC, codex_app.py).
        self.review_config: config.Controller | None = None
        self.controller = None  # Shared by the lead and workers' paid calls; never owns scene mutation.
        self.pending_reply: dict | None = None  # Final delivery stays private until all gates have decided.
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
        self.unmade_final = ""  # Loopcut's last line after a reply that only wrote its calls out (_unmade).
        self.motion_notice = ""  # Loopcut's line after a last reply that says objects move that do not (_motion_claim).
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
        self.tool_paywall = False  # A paid tool's refusal opened the out-of-credits pop-up (_Consent._paywall): once.
        self.credit_offers: set = set()  # The tools that pop-up or a locked card (nudges.offer_locked) offered.
        self.submission = None  # A new send can be returned to the input if its first request is refused.
        self.returned_submission = False
        # Its conversation log (session_logs.py): what ran it, or {} when the conversation is not uploaded.
        self.conversation_log: dict = {}
        # The stall clock (check_stall): when the turn last made progress, in wall time so that a laptop's sleep
        # counts, what it has waited for since, and its limit in seconds (0: off; set as it launches).
        self.progress_at = time.time()
        self.progress = state.MODEL_REPLY
        self.stall_seconds = 0
        self.checked_at: float | None = None  # The pump's last look (time.time()); a long gap is a sleep.

    def touch(self, label: str) -> None:
        """Real progress (a streamed delta, a tool starting or finishing, a worker's reply, a cloud job moving on):
        the stall clock starts again, now waiting for `label`. Any thread."""
        self.progress_at, self.progress = time.time(), label


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


def _resume_playback(spec) -> None:
    """Playback an inspection paused for its look (inspection.pause_playback) plays again, once the
    batch's looks were checked against the scene (_fresh_images) or the turn ends. Not from a worker's
    loop: workers run beside the lead in this Blender, and their looks are in their own windowless
    Blenders, so a pause is always the lead's (the one loop of a turn that isn't a worker)."""
    from . import inspection, mainthread
    if spec.role != "worker" and inspection.paused():
        try:
            mainthread.run_on_main(inspection.resume_paused).result(timeout=10)
        except Exception as ex:  # Never keeps a step or the turn from going on; the user can press play.
            print(f"Loopcut: could not resume playback: {ex!r}")


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


def _approval(name: str, arguments: str, roots, turn=None) -> str:
    """"" when the call runs unasked; "gate" when the user must approve it unless they said
    "Always allow" or auto-run is on; "heavy" for a render, a bake, a simulation, which can hold
    Blender for minutes: it asks unless they said "Always allow renders"; "install" for code that
    installs packages or downloads code and runs it: it asks every time (tools.INSTALLS); "capped" for a
    delegate that can start none of the turn's workers (orchestrator.starts_nothing): it is not asked, and
    run_loop holds it to that (Turn.capped_call); see _asks."""
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
    if orchestrator.starts_nothing(name, parsed, turn) and not orchestrator.waits(name, parsed):
        return "capped"
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
    if approval == "capped":
        return False  # It starts nothing, and run_loop holds it to that.
    allowed = cfg.auto_run or session["auto_run"]
    if approval == "heavy":
        return not session["auto_heavy"] or (name in tools.NEEDS_APPROVAL and not allowed)
    return bool(approval) and not allowed


def _job_offer(spec, call, turn, card_asks: bool) -> dict | None:
    """A render_job start whose frames would take over 15 minutes here, by a measured frame, while this account has
    the cloud render (render_offer.prepare_job): its card is the cloud-or-this-computer choice. card_asks: today's
    card would ask (renders not always allowed); when it would not, the choice still asks only while
    render_offer.ASK_EVEN_WHEN_ALWAYS_ALLOWED (the owner's open question). None otherwise, and in a loop that cannot
    ask."""
    from . import render_offer
    if not spec.asks or call.name != "render_job" or not (card_asks or render_offer.ASK_EVEN_WHEN_ALWAYS_ALLOWED):
        return None
    parsed = _arguments(call.arguments)
    if parsed.get("action") != "start" or not render_offer.cloud_tool():
        return None
    return _on_main(lambda: render_offer.prepare_job(parsed), turn.cancel.is_set, name="the render's time here")


def _ask_choice(card: dict, offer: dict, turn) -> str | None:
    """The render job's card asks as the choice (ui.render_choice): "cloud", "here", or None for Not now (Esc). Enter
    picks neither: with no quote, the cloud is never Recommended (render_offer.enter_ok)."""
    from . import render_offer
    turn.decided.clear()
    state.awaiting(card, render_choice=render_offer.job_choice(offer), heavy=False, install=False)
    _redraw()
    try:
        approved = _wait_for_approval(turn)
    finally:
        card.pop("render_choice", None)
        picked = card.pop("picked", None)
    return "cloud" if approved else "here" if picked == "here" else None


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


def _extras_line(cfg: config.Config | None) -> str:
    """Main thread: EXTRAS_LINES for the user's extras preference, or "" at AUTO and on a turn with no Loopcut paid
    tools (the user's own key without Loopcut's cloud tools). Hosted and ChatGPT-plan turns get it. Never raises."""
    try:
        from . import settings
        line = EXTRAS_LINES.get(getattr(settings.preferences(), "extras_mode", "AUTO"))
    except Exception:  # Outside Blender (tests, harness/evals/run.py), or no preferences yet: AUTO.
        return ""
    if not line or cfg is None:
        return ""
    try:
        from . import cloud
        if config.on_service(cfg.base_url) or config.chatgpt_plan(cfg.base_url):
            return line
        runtime = cloud._runtime
        paid = runtime is not None and any(d.get("backend") == "managed" for d in runtime.descriptors())
        return line if paid else ""
    except Exception as ex:  # A fact line is a nicety; the turn goes on without it.
        print(f"Loopcut: could not check for Loopcut's paid tools: {ex!r}")
        return ""


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


def _keeps_reasoning(cfg: config.Config) -> bool | None:
    """The service's keep_reasoning for a model it lists; None off the service (an own key, a direct provider),
    where config's model-id default and LOOPCUT_KEEP_REASONING decide, with no account to ask."""
    if not config.on_service(cfg.base_url):
        return None
    from . import account
    return account.keeps_reasoning(cfg.base_url, cfg.model)


def _keep_mode(cfg: config.Config) -> config.Config:
    """Keep mode, and keep_reasoning, switched on when the Loopcut service lists the model with them."""
    if not cfg.keep_context and _keeps_context(cfg):
        cfg = dataclasses.replace(cfg, keep_context=True)
    listed = _keeps_reasoning(cfg)
    if listed is not None and listed != cfg.keep_reasoning and "LOOPCUT_KEEP_REASONING" not in os.environ:
        cfg = dataclasses.replace(cfg, keep_reasoning=listed)  # The service's word beats the model-id default.
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
    turn.stall_seconds = config.stall_seconds()
    codex = config.codex_app()
    if codex is not None:
        session["mode"] = "workspace"
        turn.config, turn.spec, turn.codex = _turn_config(), CODEX_SPEC, codex
        turn.review_config = config.controller(turn.config)
        return
    roles = config.load_roles()
    # The disk cache normally supplies this before the first refresh completes. With no
    # known index yet (or an older server), the open single loop is safe for the first turn.
    orchestrate = roles.orchestrate and cloud.prompt_index_available()
    if roles.workspace and (session.get("mode") == "workspace" or not orchestrate):
        session["mode"] = "workspace"
        turn.config = _workspace_config(_keep_mode(roles.orchestrator) if roles.orchestrate else _turn_config())
        turn.spec = WORKSPACE_SPEC
        turn.review_config = config.controller(turn.config)
        return
    if orchestrate:
        turn.roles = dataclasses.replace(roles, orchestrator=_keep_mode(roles.orchestrator),
                                         worker=_keep_mode(roles.worker))
        turn.config, turn.spec = turn.roles.orchestrator, ORCH_SPEC
        if roles.orchestrator_max_steps:  # The account's steps per turn, for this loop only (_route's divert).
            turn.config = dataclasses.replace(turn.config, max_steps=roles.orchestrator_max_steps)
            turn.carry_steps = True
        turn.may_divert = roles.workspace and _never_delegated(session)
    else:
        turn.config = _keep_mode(roles.orchestrator) if roles.orchestrate else _turn_config()
        turn.spec = SINGLE_SPEC
    turn.review_config = config.controller(turn.config)


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
        from . import chat_credits
        if success and not chat_credits.is_tool((state.ui.get("allowance_notice") or {}).get("item")):
            state.clear_allowance_notice()  # A paid tool's refusal still stands while chat goes on (_Consent._paywall).
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
            # Its notes go with the request again, and a click's note added meanwhile stays after them.
            session["between_turns"] = [*snapshot.get("between_turns", []), *(session.get("between_turns") or [])]
            from .ui import textedit
            draft = "\n".join(t for t in (snapshot["text"], *extra, session["input"]) if t)
            textedit.set_text(session, draft)
            refs = {a["ref"] for a in snapshot["attachments"]}
            session["attachments"] = snapshot["attachments"] + [a for a in session["attachments"] if a["ref"] not in refs]
            turn.notes.clear()
            turn.returned_submission = True
        from . import chat_credits
        latest = state.latest_limit(session["items"])
        # A paid tool's refusal (_Consent._paywall) is history in the chat, never this send's card to reuse.
        if not turn.hosted_requests and latest is not None and not chat_credits.is_tool(session["items"][latest]):
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
    if info and item["reason"] != "insufficient_credits":  # Out of credits: no chat windows to warn about.
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
    from . import chat_credits
    credits = info.get("billing") == chat_credits.CREDITS or chat_credits.active()
    if credits and type(info.get("credits_balance")) is not int:
        return  # Credits accounts still get the window headers, but no windows apply to them.
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
    if credits:
        _warn_low_credits(items, info, owner)
        return
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


def _warn_low_credits(items: list, info: dict, owner: Turn | None) -> None:
    """Main thread, credits arm on Pro and Max (0.2.15): one quiet heads-up per refill period once a reply's balance
    is down to about 20 credits, with Add credits, before the out-of-credits stop. Never on the free plan: there the
    out-of-credits stop is the offer, and a heads-up let users stop short of it (owner, 2026-10-25). Never raises."""
    from . import chat_credits, nudges
    balance = info.get("credits_balance")
    if (info.get("billing") != chat_credits.CREDITS or type(balance) is not int
            or not 0 < balance <= chat_credits.LOW_BALANCE):
        return
    try:
        account, _ = chat_credits._account()
        if chat_credits.plan_of(account) not in ("pro", "max"):
            return
        if not nudges.first_time("low_chat_credits", chat_credits.low_key(account)):
            return
        item = state.item_notice(chat_credits.low_text(balance, account))
        if config.billing_open():  # Paid plans top up.
            item.update(action_label="Add credits", url=config.credits_url("low_credits"))
        items.append(item)
        if owner is not None:
            owner.allowance_notices.append((items, item))
    except Exception as ex:
        print(f"Loopcut: could not show the low-credit notice: {ex!r}")


def _limit_item(ex: llm.LLMError) -> dict:
    """The card for a 402: Loopcut's gateway says which window ran out, when it resets and what
    the next plan offers; any other provider's 402 shows its message with no button."""
    details = ex.details
    upgrade = details.get("upgrade") if isinstance(details.get("upgrade"), dict) else None
    info = ex.account or {}
    window = info.get({"session_limit": "session", "week_limit": "week"}.get(ex.code, "")) or {}
    item = state.item_limit(str(details.get("message") or ex), ex.code or "limit",
                            str(details.get("plan") or info.get("plan") or ""),
                            str(details.get("resets_at") or window.get("resets_at") or ""), upgrade)
    from . import chat_credits
    refill = chat_credits.refill_of(details.get("refill")) if ex.code == chat_credits.CODE else None
    if refill:  # The credits arm's sooner weekly top-up (contract 0.9); absent, the card is 0.2.14's.
        item["refill"] = refill
    return item


def _upstream_text(ex: llm.LLMError) -> str:
    """What the Try again card says when the gateway's vendor failed (UPSTREAM_CODES)."""
    if ex.code == "rate_limited":
        return BUSY_MESSAGE
    return str(ex.details.get("message") or "").strip() or UPSTREAM_MESSAGE


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
    from . import assets, autorig, camera, cloud, contact, files, inspection, job_tools, texture_tools, tools, uv_tools, video_attach
    remote = cloud.tool_schemas()
    # character_rig only beside the account's cloud auto_rig: without auto-rig access the model has no rig tools.
    rig = autorig.SCHEMAS if any(cloud.tool_name(s["function"]["name"]) == "auto_rig" for s in remote) else []
    return video_attach.with_clip_tools(
        tools.SCHEMAS + contact.SCHEMAS + files.SCHEMAS + job_tools.SCHEMAS + inspection.SCHEMAS + assets.SCHEMAS
        + camera.SCHEMAS + uv_tools.SCHEMAS + texture_tools.SCHEMAS + rig + remote)


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
    from . import autorig, orchestrator, paint_tools, texture_tools, uv_tools, workspace
    for module in (job_tools, files, assets, contact, camera, orchestrator, uv_tools, texture_tools, paint_tools,
                   workspace, autorig, cloud):
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


def _wait_key(card: dict):
    """What a card's own wait is found by: a Turn it into 3D click's key (turn_into_3d), which a reopened chat's copy
    of the card has too, else the card itself."""
    click = card.get("click")
    return tuple(click) if isinstance(click, list) else id(card)


def end_click_asks(conversation: str) -> None:
    """A chat leaves the screen (lifecycle.switch_to), or a turn's end meets a click's ask (run_loop): each Turn it
    into 3D click of that chat still asking is answered No, so it ends rejected with its picture ready and nothing
    submitted, instead of waiting where nobody can answer it."""
    for key, wait in list(_card_waits.items()):
        if isinstance(key, tuple) and key[:1] == (conversation,):
            wait.answer(False)


def _wait_for_card(card: dict, turns) -> bool:
    """A worker's ask: wait for its own card's answer, so a main-turn card waiting at the same time keeps its own.
    A wait registered before the card showed (_Consent._pause) is the one waited on."""
    key = _wait_key(card)
    wait = _card_waits.setdefault(key, _CardWait())
    try:
        while not wait.event.wait(_POLL_SECONDS):
            if any(t.cancel.is_set() for t in turns):
                raise llm.Cancelled()
        if any(t.cancel.is_set() for t in turns):
            raise llm.Cancelled()
        return wait.approved
    finally:
        _card_waits.pop(key, None)


def _save_spend(session: dict) -> None:
    """A charge known after the turn ended (a detached job) is saved with its conversation now; a running turn
    saves it with its next step. A copy no longer on screen writes only its spend into its file: History reopens a
    chat as a new copy, and an older copy must never be written over newer turns."""
    if session is not state.session():
        with spending.lock:
            spend = dict(spending.ledger(session))
        _patch_spend(session, lambda body: body.update(spend=spend))
    elif not session.get("busy"):
        conversations.save(session)


def _patch_spend(session: dict, change) -> None:
    """change(body) on the file of a chat no longer on screen. One nobody has written in has no file (as
    conversations.save leaves it), and a failed write never fails the call whose charge it settles."""
    if not session["items"]:
        return
    try:
        conversations.patch(session["id"], change)
    except Exception as ex:
        print(f"Loopcut: could not save a closed chat's spend: {ex!r}")


QUOTING = " · Getting a credit quote…"  # A quoted call's card title until its quote comes (cloud.describe).


def _priced_title(card: dict, credits, *, charged: bool = False) -> None:
    """A quoted call's card title, in cloud.describe's "<tool> · <price>" form: its quote once it comes, then what it
    was charged ("no charge" when nothing). rel019-cloud2: "Render scene · Getting a credit quote…" stayed the title
    after its quote showed and after the render ran."""
    summary = card.get("summary") or ""
    title = card.get("quoted_title") or (summary[:-len(QUOTING)] if summary.endswith(QUOTING) else None)
    if title is None or type(credits) not in (int, float) or credits < 0:
        return
    card["quoted_title"] = title
    price = "no charge" if charged and not credits else f"{credits:,g} credit{'' if credits == 1 else 's'}"
    card["summary"] = f"{title} · {price}"


def _failed_card(card: dict, result) -> None:
    """A failed call's card: a quote that never came leaves its title (rel019-cloud: "Render scene · Getting a credit
    quote…" on a call refused before any quote), and a refusal of the model's arguments (tools.InputMistake) reads
    plainly on it, the model's text behind its toggle (ui.layout)."""
    if result.ok:
        return
    summary = card.get("summary") or ""
    if summary.endswith(QUOTING):
        card["summary"] = summary[:-len(QUOTING)]
    if getattr(result, "input_mistake", False):
        card["input_mistake"] = True


def _settle_spend(chat_session: dict, held, charged, auto: bool) -> None:
    """A call's hold becomes its charge in its chat as it is now: the copy on screen (History may have reopened it
    as a new copy), or else its file, never by saving an older copy over it."""
    live = state.session()
    if live is not chat_session and live.get("id") == chat_session.get("id"):
        chat_session = live
    if chat_session is live:
        with spending.lock:
            spending.settle(spending.ledger(live), held, charged, auto)
        _save_spend(live)
        return

    def change(body):
        if not spending.valid_ledger(body.get("spend")):
            body["spend"] = spending.new_ledger()
        with spending.lock:
            spending.settle(body["spend"], held, charged, auto)
    with spending.lock:
        spending.settle(spending.ledger(chat_session), held, charged, auto)  # This copy's card, if it shows again.
    _patch_spend(chat_session, change)


class _Consent:
    """One cloud call's consent and charge, bound to its card (cloud_account.approval_context). Cost consent is per
    call, even when ordinary scene edits are auto-approved: spending.decide says whether it asks. Every ask pauses
    the turn on a card (owner, 2026-10-16); Approve runs the call, "Skip this" declines only this call and the model
    is told why. A worker asks in the user's panel, named, and waits. A detached job reports its charge here later
    (cloud_account.charge_reporter), so the chat's spend follows what was really charged."""

    settles = True  # cloud_account.charge_reporter sends it what each call was charged.

    def __init__(self, session, turn, card, asks=True, files_allowed=False, clicked=False, shown=None, chosen=False):
        self.session, self.turn, self.card = session, turn, card
        self.asks, self.files_allowed = asks, files_allowed
        # The user picked the cloud on a render job's choice card (render_offer.job_call): its quote, once priced, asks
        # on the plain paid card, never the choice again.
        self.chosen = chosen
        # A Turn it into 3D click made this call (turn_into_3d): no turn of the panel's, so its asks are answered on
        # their own card; shown is the price its button showed, which the click approved, and charged what the call
        # was charged, which goes to its chat when it ends.
        self.clicked, self.shown_credits, self.charged = clicked, shown, None
        self.hold = None       # (credits held, ran without asking) once the call may run.
        self.settled = None    # A rig's last net charge and original auto flag; its refund can reduce it later.
        self.limit_approved = False  # The user approved past a server limit, seeing this call's cost: no second ask.
        self.implied = False   # It may run, and the user neither asked for it nor approved its card (undone means no).
        self.shown = [card]    # The cards that show its cost: its own, and a worker's ask in the user's panel.
        self.rig = card.get('name') in ('loopcut_cloud__auto_rig', 'loopcut_cloud.auto_rig')

    def chat(self):
        if getattr(self.turn, "chat", None):
            return self.turn.chat
        session = self.session if self.session is not None else state.session()
        live = state.session() if self.clicked else session
        if live is not session and live.get("id") == session.get("id"):
            session = live  # A click's chat reopened since: a new dict, the one on screen.
        return session, self.turn

    def _pause(self, card: dict, **fields) -> bool:
        """card asks (state.awaiting) and the user's answer. A click's call (turn_into_3d) waits on its own card, its
        wait registered before the card shows: no turn of the panel's would hear Approve, Skip, Enter or Esc, and
        a later turn's would take the answer as its own (agent.decide)."""
        if self.clicked:
            _card_waits[_wait_key(card)] = _CardWait()
            state.awaiting(card, **fields)
            _redraw()
            return _wait_for_card(card, (self.turn,))
        self.turn.decided.clear()
        state.awaiting(card, **fields)
        _redraw()
        return _wait_for_approval(self.turn)

    def _click_covers(self, details: dict) -> bool:
        """The click approved this call: a quoted price no higher than its button showed."""
        credits = details.get("credits")
        return (self.clicked and type(self.shown_credits) in (int, float) and type(credits) in (int, float)
                and credits <= self.shown_credits and not details.get("vendor"))

    def tool(self) -> str:
        """The paid tool this call is: image_to_3d, sfx, auto_rig..."""
        from . import cloud
        name = str(self.card.get("name") or "")
        return cloud.tool_name(name) or name

    def asked_for(self, chat_session: dict) -> bool:
        """The user asked for this call (spending.asked_for): their latest message names it, or they clicked its
        button or picked the cloud on its choice card."""
        if self.clicked or self.chosen:
            return True
        said = next((item.get("text") for item in reversed(chat_session.get("items") or ())
                     if item.get("kind") == "user"), "")
        return spending.asked_for(self.tool(), said)

    def __call__(self, details):
        if 'progress' in details:  # Only a rig reports here (cloud_account.progress_reporter).
            from . import cloud
            if self.card.get('status') != 'running':
                return True  # A queued redraw must not replace an approval or completed result.
            if self.rig and getattr(self.turn, 'chat', None) and len(self.shown) == 1:
                chat, _ = self.chat()
                card = state.item_tool(self.card['name'], f'{self.turn.worker}: Rig your character', '')
                card.update(status='running')
                self.shown.append(card)
                chat['items'].append(card)
            for card in self.shown:
                if card.get('status') == 'running':
                    cloud._card_progress(card, details['progress'])
            return True
        if "error" in details:
            paywall = details["error"].get("paywall")
            error = {k: v for k, v in details["error"].items() if k != "paywall"}
            for card in self.shown:
                card["cloud_error"] = error
                card["expanded"] = not paywall  # The pop-up has the offer: the call's card keeps its one line.
            if paywall:
                self._paywall(paywall)
            _redraw()
            return True
        if "charged" in details:
            self._settle(details["charged"], details.get("text") or "", details.get("settled"))
            return True
        if "cancel_job" in details:
            return self._ask_cancel(details["cancel_job"])
        previous_cost = self.card.get('cost', '')
        self.card["cost"] = details["text"]
        for card in self.shown:
            _priced_title(card, details.get("credits"))
        if self.rig:
            from . import rig_text
            if details.get('credits') is not None:
                self.card.update(rig_credits=details['credits'], rig_balance=details.get('balance'),
                                 rig_notice=details.get('rig_notice', rig_text.QUOTE))
                self.card['cost'] = rig_text.price(details['credits'], details.get('balance'))
            else:
                self.card['cost'] = previous_cost or rig_text.price(0)
        if details.get("file_access"):
            if not details["required"] or self.files_allowed:
                return True
            if not self.asks:
                return False
            approved = self._pause(self.card, paid_approval=False, heavy=False)
            self.card["status"] = "running" if approved else "rejected"
            if approved:
                self.card["started_at"] = time.time()  # Its time shown counts from the answer, not the ask.
            else:
                self.turn.declined = True
            _redraw()
            return approved
        if details.get("limit"):  # A server limit refused it (cloud_account._confirmed): ask, then it goes again.
            approved = self._ask(details["limit"], details)
            # Its card showed the price unless a quote was still coming; then the price asks on its own.
            self.limit_approved = approved and details.get("credits") is not None
            return approved
        chat_session, chat_turn = self.chat()
        settings = getattr(chat_turn, "spending", None) or spending.Settings()
        # A limit card that showed the price was its approval; a click approved what its button showed (_click_covers).
        explicit = self.limit_approved or self._click_covers(details)
        asked = self.asked_for(chat_session)
        allowed = set()
        reviewer = getattr(chat_turn, "controller", None) if not self.clicked else None
        review = None
        reviewed = False
        value_question = ""
        while True:
            if self.turn.cancel.is_set() or chat_turn.cancel.is_set():
                raise llm.Cancelled()
            settings = getattr(chat_turn, "spending", None) or spending.Settings()
            asked = self.asked_for(chat_session)
            # Permission first: never pay for a value review of a call the user's policy refuses.
            with spending.lock:
                kind = spending.decide(details, spending.ledger(chat_session), settings, calls=chat_turn.paid_calls,
                                       burst_limit=chat_turn.burst_limit,
                                       vendors=chat_session.setdefault("spend_vendors", []), asked=asked)
            if kind == spending.NOT_ASKED and not explicit:
                from . import cloud_http
                self.card["declined"] = True
                raise cloud_http.CloudError(spending.declined(kind, details, settings), "declined")
            if reviewer and reviewer.meaningful(details) and not reviewed:
                from . import cloud_http, controller
                try:
                    review = reviewer.review("paid_action", json.dumps({
                        "tool": self.tool(), "summary": self.card.get("summary", ""),
                        "worker": getattr(self.turn, "worker", ""), "worker_scene_revision": self.turn.ledger.revision,
                        "arguments": str(self.card.get("code", ""))[:1200],
                        "cost": {key: details.get(key) for key in ("credits", "vendor", "balance")}}), details)
                except controller.StaleReview as ex:
                    raise cloud_http.CloudError(str(ex) + " Nothing was charged.", "controller_stale") from ex
                except controller.ReviewError as ex:
                    # A failed review has no authority to spend. A fully priced user decision can proceed.
                    value_question = str(ex) + " Do you want this paid action to run?"
                else:
                    if review.action == "alternative":
                        raise cloud_http.CloudError(review.note() + " Nothing ran and nothing was charged.",
                                                    "controller_alternative")
                    if review.action == "user_decision":
                        value_question = review.reason
                reviewed = True
                continue  # Re-read policy/ledger after potentially slow inference.
            if review is not None and not reviewer.current(review):
                from . import cloud_http
                raise cloud_http.CloudError("The scene or user instruction changed after the paid-action review. "
                                            "Propose the action again for the current request. Nothing was charged.",
                                            "controller_stale")
            with spending.lock:
                # Recheck AND reserve atomically. Another worker may have spent or reserved during the review
                # or any approval wait. Controller approval never changes this permission decision.
                kind = (spending.RUN if explicit else spending.decide(
                    details, spending.ledger(chat_session), settings, calls=chat_turn.paid_calls,
                    burst_limit=chat_turn.burst_limit, vendors=chat_session["spend_vendors"], asked=asked))
                if kind == spending.RUN or kind in allowed:
                    if value_question:
                        kind = spending.VALUE
                    else:
                        if not self.clicked:
                            if spending.paid(details):
                                chat_turn.paid_calls += 1
                            held = spending.start(spending.ledger(chat_session), details, not explicit)
                            self.hold = (held, not explicit) if held else None
                        break
            if kind == spending.NOT_ASKED:
                continue  # The next pass handles a policy change without asking for a forbidden action.
            allowed.add(kind)
            if not self._ask(kind, {**details, "value_reason": value_question}):
                return False
            if kind in (spending.COST, spending.VENDOR, spending.VALUE):
                value_question = ""
            with spending.lock:
                spending.allow(kind, details, spending.ledger(chat_session), settings, chat_turn,
                               chat_session["spend_vendors"])
            if kind in (spending.COST, spending.VENDOR, spending.VALUE):  # The user approved this very call.
                explicit = True
        self.implied = not asked and not explicit
        if self.clicked:  # Nothing held: its charge goes to its chat when it ends (turn_into_3d), wherever that is.
            _redraw()
            return True
        if self.rig:
            for card in self.shown:
                card.update(cost=self.card['cost'])
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
        if self.rig:
            from . import rig_text
            lines = [rig_text.approval(kind, self.card.get('rig_notice'))]
        fields = {"ask": kind, "ask_lines": lines, "ask_label": spending.approve_label(kind, settings),
                  "ask_skip": spending.skip_label(kind), "ask_title": spending.ask_title(kind, details),
                  "paid_approval": True, "heavy": False, "install": False}
        if details.get("url"):
            fields["ask_link"] = details["url"]
        if kind == spending.COST and not self.rig:
            from . import render_offer  # A long render's ask is the choice: the cloud or this computer.
            fields.update(render_offer.card_fields({**details, "render_offer": None} if self.chosen else details))
        if self.rig:
            fields['rig_stage'] = 'Waiting for approval.'
        if worker:
            card = (self.shown[-1] if self.rig and len(self.shown) > 1 else  # Its mirrored progress card.
                    state.item_tool(self.card.get("name", ""), f"{worker}: {self.card.get('summary') or ''}".rstrip(": "), ""))
            state.awaiting(card, cost=self.card["cost"], worker=worker, **fields)
            if card not in self.shown:
                self.shown.append(card)
            if card not in chat_session["items"]:
                chat_session["items"].append(card)
            _redraw()
            try:
                approved = _wait_for_card(card, (chat_turn, self.turn))
            except llm.Cancelled:
                card["status"] = "rejected"
                raise
        else:
            card = self.card
            approved = self._pause(card, **fields)
        choice, picked = card.get("render_choice"), card.pop("picked", None)
        for key in ("ask", "ask_lines", "ask_label", "ask_link", "render_choice", "price", "ask_skip", "ask_title"):
            card.pop(key, None)
        if choice and not approved and picked == "here":
            # "Render on my laptop": this paid call is declined, and the same frames render here instead.
            from . import render_offer
            card.update(status="done" if worker else "running", summary=render_offer.here_summary(choice))
            _redraw()
            raise render_offer.here(choice, card)
        # A rig's worker card keeps running until finish(); other workers' asks are done once answered.
        card["status"] = ("running" if self.rig or not worker else "done") if approved else "rejected"
        if card["status"] == "running":
            card["started_at"] = time.time()  # Its time shown counts from the answer, not the ask (ui.layout).
        _redraw()
        if not approved:
            self.card["declined"] = self.turn.declined = True
            if self.rig:
                for shown in self.shown:
                    shown['cost'] = rig_text.price(0, self.card.get('rig_balance'))
            if choice:  # Esc on the choice card: neither render, and the model asks which.
                from . import render_offer
                raise cloud_http.CloudError(render_offer.SKIPPED, "declined")
            raise cloud_http.CloudError(spending.declined(kind, details, settings, calls=chat_turn.paid_calls),
                                        "declined")
        return True

    def _ask_cancel(self, details: dict) -> bool:
        """The model asked to cancel a running paid job (cloud._job_status): its card asks the user, "Cancel it" or
        "Keep it running" (spending.CANCEL). True to cancel; False when they keep it, or nobody can be asked."""
        from . import cloud_http
        try:
            return self._ask(spending.CANCEL, details)
        except cloud_http.CloudError as ex:
            if ex.code != "declined":
                raise
            return False

    def _paywall(self, refusal: dict) -> None:
        """The credits arm (0.2.19): the server refused this paid call for credits (cloud_account.error_details), so
        the user's chat gets the chat's out-of-credits pop-up for it, at once, in the turn that runs: the same limit
        item, headed by the tool (chat_credits.tool_item). Once a turn: a retry, or another paid tool refused in the
        same turn, keeps the one shown; a worker's call shows in the user's chat, on the user's turn. Never raises."""
        from . import chat_credits, cloud
        chat_session, chat_turn = self.chat()
        live = state.session()
        if chat_session is not live and (not chat_session.get("id") or chat_session.get("id") != live.get("id")):
            return  # The conversation is closed.
        # The turn running now: a detached job's refusal (job_status, a later turn) counts in that turn, once.
        owner = live.get("turn") or chat_turn
        name = self.card.get("name", "")
        tool = cloud.tool_name(name) or name
        if getattr(owner, "tool_paywall", False) or tool in (getattr(owner, "credit_offers", None) or ()):
            return
        try:
            account, _ = chat_credits._account()
            item = chat_credits.tool_item(cloud.plain_title(name), refusal.get("needed"), refusal.get("balance"),
                                          account)
        except Exception as ex:  # The offer is a courtesy; the call's own result still reaches the model.
            print(f"Loopcut: could not show the out-of-credits offer for a tool: {ex!r}")
            return
        if item is not None:
            chat_credits.show_tool_item(chat_session, owner, item, tool)

    def finish(self, result):
        """A rig's mirrored worker card ends with the call's result."""
        if not self.rig:
            return
        for card in self.shown[1:]:
            card.update(status='done' if result.ok else 'rejected' if self.card.get('declined') else 'failed',
                        output=result.text)

    def _settle(self, charged, text: str, settled=None) -> None:
        """settled: a job kept with its chat, whose record settles the chat's spend: its charge for the title alone,
        so the title says what the turn's line says once it settles (chat_credits.turn_line)."""
        if self.rig:
            self._settle_rig(charged, text)
            return
        if self.clicked and charged is not None:
            self.charged = charged
        if self.hold is None and not charged:
            return  # Nothing held and nothing charged: a free call keeps "Free", a skipped one its price.
        if text:
            for card in self.shown:
                card["cost"] = text
        for card in self.shown:
            _priced_title(card, charged if charged is not None else settled, charged=True)
        if self.hold is not None and charged is not None:
            held, auto = self.hold
            self.hold = None  # Once: a job's watcher and its job_status may both report.
            chat_session, _ = self.chat()
            _settle_spend(chat_session, held, charged, auto)
        _redraw()

    def _settle_rig(self, charged, text: str) -> None:
        """A rig's charge can drop after it settled: a failed local apply is refunded (rig_refund)."""
        from . import rig_text
        if charged is not None and not spending._amount(charged):
            return
        chat_session, _ = self.chat()
        with spending.lock:
            previous = self.hold or self.settled
            if previous is None:
                if charged == 0:
                    for card in self.shown:
                        card['cost'] = rig_text.price(0, self.card.get('rig_balance'))
                return  # Free and skipped calls retain their original price text.
            if self.hold is None and (charged is None or charged > previous[0]):
                return  # Historical completion events cannot undo a confirmed refund.
            if charged is not None:
                held, auto = previous
                self.hold = None
                self.settled = (charged, auto)
                spending.settle(spending.ledger(chat_session), held, charged, auto)
            text = rig_text.price(charged, self.card.get('rig_balance'), settled=True)
            for card in self.shown:
                card["cost"] = text
        if charged is not None:
            _save_spend(chat_session)
        _redraw()


def _made_pictures(session: dict, card: dict, call, result) -> None:
    """After a tool call in the user's turn: image_generate's pictures get their card's Turn it into 3D button and
    the model the line saying so (cloud.TO_3D_LINE); image_to_3d done on one marks its button."""
    from . import cloud
    offer = cloud.to_3d_offer(getattr(result, "made_pictures", None))
    if offer:
        card["to_3d"] = offer
        result.model_note = "\n\n".join(filter(None, [getattr(result, "model_note", ""), cloud.TO_3D_LINE]))
    if result.ok and getattr(result, "converted", ""):
        _turned_into_3d(session, result.converted)


def _turned_into_3d(session: dict, job: str) -> None:
    """The picture that job made turned into 3D (by the job, so a renamed picture too): each button for it reads
    Turned into 3D ✓ and runs nothing again."""
    for item in session["items"]:
        for picture in (item.get("to_3d") or {}).get("pictures") or ():
            if job and picture.get("job") == job:
                picture["state"] = "done"


def turn_into_3d(index: int, which: int = 0, shown=None, drawn=None) -> bool:
    """Main thread: the Turn it into 3D button on a made picture's card (owner, 2026-10-26). The click approves the
    price it showed (shown): image_to_3d runs on that picture as a paid call, quoted, with no model call; a higher
    quote, or a server limit, asks on its own card. Its tool card shows it, a refusal for credits goes the usual way
    (the paywall pop-up), and the model reads what happened with the next message. Once per picture and chat: a
    second click, a click while it runs (the chat saved so, state.running_clicks) or while a turn runs does nothing.
    drawn: the card the clicked frame drew (gate018-ui); a frame drawn before the chat changed answers nothing.
    A conversion whose model was made but could not land (another file was open, cloud._apply_to_scene) loads that
    model on the next click, from the job this Blender kept: no new job and no new charge (cloud.apply_finished)."""
    session = state.session()
    items = session["items"]
    if session.get("busy") or not 0 <= index < len(items) or (drawn is not None and drawn[0] != id(items[index])):
        return False
    pictures = (items[index].get("to_3d") or {}).get("pictures") or []
    if not 0 <= which < len(pictures) or pictures[which].get("state") != "ready":
        return False
    picture = pictures[which]
    key = (session["id"], picture.get("job") or picture["image"])
    if key in state.running_clicks:
        return False
    from . import cloud, cloud_account, cloud_jobs, mainthread, tools
    name = cloud.PREFIX + "image_to_3d"
    runtime = cloud._runtime
    descriptor = next((d for d in (runtime.descriptors() if runtime else []) if d["name"] == "image_to_3d"), {})
    image = cloud_jobs.made_name(picture)  # Found by the job that made it: a renamed picture still converts.
    arguments = {cloud_jobs.picture_field(descriptor): image}  # Own-key image_to_3d takes reference.
    summary = (cloud.describe(name, arguments) or (name, ""))[0]
    made = picture.get("unapplied")
    card = state.item_tool(name, f"{summary.split(' · ')[0]} · made already" if made else summary, "")
    card.update(started_at=time.time(), click=list(key))
    items.append(card)
    picture["state"] = "running"
    state.running_clicks[key] = image
    conversations.save(session)  # Reopened meanwhile, the chat shows it running.
    turn = Turn()
    try:
        turn.spending = config.spending()  # The user's Ask above for a quote over the button's price.
    except config.ConfigError:
        pass
    # files_allowed: the picture is Loopcut's own (the chat's copy, when Undo took its datablock away).
    consent = _Consent(session, turn, card, files_allowed=True, clicked=True, shown=shown)

    def done(result):
        try:
            _click_ended(key, card, result, consent.charged)
        finally:
            state.running_clicks.pop(key, None)
            _redraw()

    try:
        with cloud_account.approval_context(consent):
            prepared = cloud.apply_finished(made) if made else tools.execute(name, json.dumps(arguments))
    except Exception as ex:  # As below: the card says so, and the button can be pressed again.
        prepared = tools.ToolResult(f"Could not turn it into 3D: {ex}", ok=False)
    if prepared.then is None:
        done(prepared)
        return True

    def work():
        result = None
        try:
            with cloud_account.approval_context(consent):
                result = prepared.then(turn.cancel.is_set, lambda fn: _on_main(fn, turn.cancel.is_set))
        except BaseException as ex:  # Its card says so: a thread nobody waits on never fails silently.
            result = tools.ToolResult(f"Could not turn it into 3D: {ex}", ok=False)
        finally:
            ended = result or tools.ToolResult("Could not turn it into 3D.", ok=False)
            mainthread.run_on_main(lambda: done(ended))
    threading.Thread(target=work, name="loopcut-to-3d", daemon=True).start()
    _redraw()
    return True


def _click_ended(key: tuple, card: dict, result, charged) -> None:
    """Main thread: a Turn it into 3D click's call ended. Its chat gets the card's result, the charge and the model's
    note: the chat on screen when it is that one, reopened since or not, else that chat's file. Never the dict the
    click began in once it is no longer the chat: saving it would drop what came after."""
    from . import spending
    conversation, made = key
    chat = state.session()
    if chat.get("id") != conversation:
        try:
            chat = conversations.load(conversation)
        except conversations.ConversationError as ex:
            print(f"Loopcut: could not keep a Turn it into 3D result: {ex}")
            return
    own = card if any(i is card for i in chat["items"]) else next(
        (i for i in reversed(chat["items"]) if i.get("click") == list(key)), None)
    if own is None:
        own = card
        chat["items"].append(own)
    if own is not card:  # The chat was reopened: its copy of the card gets what this one heard.
        own.update({k: card[k] for k in ("cost", "cloud_error", "expanded") if k in card})
    own.pop("click", None)
    own["status"] = "done" if result.ok else "rejected" if card.get("declined") else "failed"
    own["output"] = getattr(result, "card_output", "") or result.text
    if getattr(result, "outcome", ""):
        own["outcome"] = result.outcome
    if getattr(result, "receipt", ""):
        own["cost"] = result.receipt
    unapplied = getattr(result, "unapplied", "")  # Made and charged, its model not in this file: the next click loads it.
    for item in chat["items"]:
        for picture in (item.get("to_3d") or {}).get("pictures") or ():
            if (picture.get("job") or picture.get("image")) == made:
                picture["state"] = "done" if result.ok else "ready"
                if unapplied:
                    picture["unapplied"] = unapplied
                else:
                    picture.pop("unapplied", None)
    if type(charged) in (int, float) and charged > 0:
        with spending.lock:
            spending.ledger(chat)["credits"] += charged
    first = next((line for line in result.text.splitlines() if line.strip()), "")[:300]
    chat.setdefault("between_turns", []).append(
        f"since your last turn: the user pressed Turn it into 3D on the picture {state.running_clicks.get(key, made)}; "
        + (f"it ran: {first}" if result.ok else f"it did not run: {first}"))
    conversations.save(chat)


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


def _after_framing(turn: Turn, call_name: str, arguments: str, result) -> None:
    """Once frame_camera has framed the scene camera, the automatic looks go through it until the model
    asks for another angle: at its last look's angle, a model fell back to its own camera math (about 15
    tries, 2026-10-25). A camera other than the scene camera is not what angle camera shows."""
    if (call_name == "frame_camera" and getattr(result, "ok", False) and not _only_reads(call_name, _arguments(arguments))
            and "Not the scene camera" not in (getattr(result, "text", "") or "")):
        turn.angle = "camera"
        turn.shot.pop("camera", None)


def _look_note(turn: Turn) -> str:
    """Appended to a look the model asked for once it has asked for many: a nudge, not a wall."""
    if turn.captures <= SOFT_LOOKS:
        return ""
    return (f"\n\n(Look {turn.captures} this turn. Every look costs the user an image; you already get one "
            f"after each change. Finish soon, and say what you could not verify.)")


# On the user's message a Try it card sent unedited: the card, so a resumed turn (Try again) is still its turn.
TRY_IT = "loopcut_try_it"


def try_it_card(turn) -> str:
    """The setup Try it card whose unedited request started this turn (Turn.try_it); for a worker's turn, the
    user's turn it works for (Turn.chat). "" for any other turn, a later one in the same conversation included.
    Every chat request of such a turn says so (llm.correlation_headers' x-loopcut-try-it)."""
    if turn is None:
        return ""
    chat = getattr(turn, "chat", None)
    return getattr(chat[1] if chat else turn, "try_it", "") or ""


def _correlation_headers(session: dict, role: str, turn=None) -> dict:
    """This session's x-loopcut-conversation / x-loopcut-role (llm.correlation_headers) for the
    given role (orchestrator/worker/single here; router.py and context.py send their own), with the
    turn's Try it card (try_it_card) when it has one."""
    return llm.correlation_headers(session["id"], role, try_it_card(turn))


# In place of the card when describing failed or came back empty: the model still gets the images' names, which tools
# take (photo→3D, compare_with_reference). Without them, a model passed "character reference" to photo→3D
# (2026-10-25). Not marked REFERENCE_CARD, so a resumed turn asks again; a card that then comes back replaces it.
UNDESCRIBED = '\n\n<reference_card images="{names}">(not described: the description request failed)</reference_card>'


def _stills(session: dict, message: dict) -> tuple[list[str], list[str]]:
    """The references and names of the stills attached to the message. A clip's contact sheets are left
    out: they are frames to time an action by, not an image to reproduce, and the message describes them
    already (videoref.user_content)."""
    sheets = {r["ref"] for r in session["references"] if r.get("video")}
    refs = [p["image_url"]["url"] for p in message["content"]
            if p.get("type") == "image_url" and p["image_url"]["url"] not in sheets]
    return refs, [r["name"] for r in session["references"] if r["ref"] in refs]


def _undescribed(session: dict, message: dict) -> None:
    """UNDESCRIBED on the message, once."""
    names = _stills(session, message)[1] if isinstance(message.get("content"), list) else []
    note = UNDESCRIBED.format(names=", ".join(names))
    if names and note not in message["content"][0]["text"]:
        message["content"][0]["text"] += note


def _reference_card(session: dict, cfg: config.Config, message: dict, is_cancelled, role: str = "single",
                    turn: Turn | None = None) -> list[str]:
    """Describe the images the user just attached (_stills), so the description outlives the turn and any
    summary, and the model works from a checklist rather than a glance. Returns their names."""
    refs, names = _stills(session, message)
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
        headers=_correlation_headers(session, "reference", turn), on_retry=lambda: None,  # Nothing shown to undo.
        on_progress=(lambda: turn.touch(state.MODEL_REPLY)) if turn is not None else None)
    if turn is not None:
        _hosted_reply(turn, cfg, completion.account, success=True, session=session)
    card = completion.text.strip()
    message["content"][0]["text"] = message["content"][0]["text"].replace(UNDESCRIBED.format(names=", ".join(names)), "")
    if getattr(completion, "finish_reason", None) in llm.FILTERED_FINISHES or _refused_card(card):
        raise llm.LLMError(REFERENCE_REFUSED, code=llm.CONTENT_FILTER)  # run_loop: a notice, and UNDESCRIBED.
    if not card:
        _undescribed(session, message)
        return []
    message["content"][0]["text"] += f"\n\n<reference_card images=\"{', '.join(names)}\">\n{card}\n</reference_card>"
    message[context.REFERENCE_CARD] = True
    return names


def _refused_card(card: str) -> bool:
    """A short reply that refuses rather than describes. A provider's filter answered a picture with one ordinary
    line, "The request was rejected because it was considered high risk", and it went to the model as the study of
    that picture (a user's log, 0.2.14). A real card is a checklist: only a short one is read this way."""
    return len(card) < REFUSED_CARD_CHARS and bool(_REFUSAL.search(card))


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
    if turn.try_it and (turn.tool_steps or turn.executed_tools):
        return  # The card already selected and ran its tool; continue with its result, without routing again.
    from . import router
    roles = getattr(turn, "roles", None)
    divert = router.to_workspace if getattr(turn, "may_divert", False) else None
    def stream(**kwargs):
        card = try_it_card(turn)
        if card:  # The router's request is one of the card's turn too (router.ask sends the correlation headers).
            kwargs["headers"] = {**(kwargs.get("headers") or {}), llm.TRY_IT_HEADER: card}
        completion = llm.stream_chat(**kwargs, on_progress=lambda: turn.touch(state.MODEL_REPLY))
        _hosted_reply(turn, roles.router, completion.account, success=True, session=session)
        return completion
    router.start_turn(session, turn, roles.router if roles else None, turn.cancel.is_set, stream=stream, divert=divert)
    if getattr(turn, "diverted", False):
        session["mode"] = "workspace"
        turn.spec = WORKSPACE_SPEC
        # The orchestrator's own limits stay behind: the workspace loop has the main config's step limit.
        base = getattr(roles, "orchestrator", None)
        cfg = dataclasses.replace(turn.config, max_steps=base.max_steps) if base is not None else turn.config
        turn.config, turn.carry_steps = _workspace_config(cfg), False
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
    gave none) once the router ran, nudged once it nudged and stopped while its last run ended on Stop
    (router.previous_note: a routed build stopped before it ran is still to do); a resumed turn updates
    its own entry."""
    levels = session.setdefault("turn_levels", [])
    number = session.get("turn_number") or 1
    entry = {"turn": number, "first": turn.level_first or 0, "final": turn.level_max}
    if getattr(turn, "route_tried", False):
        entry["routed"] = turn.routed.level if turn.routed else None
    if getattr(turn, "nudged", False):
        entry["nudged"] = True
    if turn.outcome == "cancelled":
        entry["stopped"] = True
    if levels and levels[-1]["turn"] == number:
        previous = levels[-1]
        entry = {**previous, **entry, "first": previous["first"] or entry["first"],
                 "final": max(previous["final"], entry["final"])}
        if turn.outcome != "cancelled":
            entry.pop("stopped", None)
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
    card = try_it_card(turn)  # A summary made in a Try it card's turn is one of its requests too.

    def on_progress() -> None:
        turn.touch(state.MODEL_REPLY)

    summarize = functools.partial(context.summarize, try_it=card, on_progress=on_progress)
    prepared, compacted = context.prepare(session, cfg, turn.cancel.is_set, fixed=fixed, skill_index=index,
                                          summarize=summarize)
    if compacted:
        items.append(state.item_notice(
            f"Summarized {compacted} earlier messages so requests stay small."))
    estimated = context.estimate_tokens(prepared, fixed, skill_index=index)
    reply = state.item_assistant()
    held = turn.controller is not None and spec.role != "worker"
    if not held:
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
                                          full_refs=full_refs, reasoning_model=cfg.model if cfg.keep_reasoning else "")]

    def call(messages: list[dict], effort: str) -> llm.Completion:
        try:
            return llm.stream_chat(
                base_url=cfg.base_url, api_key=cfg.api_key, model=cfg.model,
                messages=messages, tools=schemas, reasoning_effort=effort,
                on_text=on_text, is_cancelled=turn.cancel.is_set,
                max_tokens=cfg.max_output_tokens, call_max_seconds=cfg.call_max_seconds,
                headers=_correlation_headers(session, spec.role, turn), on_retry=on_retry,
                prompt_id=hosted, on_progress=on_progress,
            )
        except llm.LLMError as ex:
            if config.on_service(cfg.base_url) and ex.code in state.HOSTED_LIMITS:
                _drop_empty(items, reply)
            elif ex.code in (llm.CONNECTION_LOST, llm.STREAM_FAILED) or (
                    ex.code in UPSTREAM_CODES and config.on_service(cfg.base_url)):
                reply["text"] = ""  # Its card's Try again asks for this reply afresh: no fragment of it above the card.
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
            if not reply["text"].strip() and reply in items:
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
    _drop_copied_records(completion, reply, items, spec, session["messages"])
    reply["streaming"] = False
    finish_reply(reply, turn.roots[0] if turn.roots else None)
    if held:
        if completion.tool_calls and turn.outcome == "running":
            if reply["text"].strip():
                items.append(reply)  # This is a progress update; the following tools have yet to run.
        else:
            turn.pending_reply = reply
    elif not reply["text"].strip() and reply in items:
        items.remove(reply)
    return completion


def _drop_copied_records(completion: llm.Completion, reply: dict, items: list, spec: LoopSpec,
                         messages: list) -> None:
    """A reply that copied the records of earlier steps (context.fold) has them taken out of what is shown
    and stored: a call written out that way never ran, so the panel says so instead (completion.unmade).
    A recap of a call in messages that ran stays. A reply left with nothing is stored as UNMADE_STORED."""
    text, unmade = context.copied_records(completion.text, {call.name for call in completion.tool_calls}, messages)
    if text == completion.text:
        return
    print(f"Loopcut: {spec.role} reply copied an earlier-steps record"
          + (f" of {', '.join(dict.fromkeys(unmade))}, which it did not call" if unmade else "")
          + "; removed from the reply.")
    reply["text"] = text
    completion.text = text or ("" if completion.tool_calls else UNMADE_STORED)  # Never an empty stored reply.
    completion.unmade = list(dict.fromkeys(unmade))
    if completion.unmade:
        items.append(state.item_notice(_unmade_text(completion.unmade)))


def _unmade_text(names: list[str]) -> str:
    from . import cloud
    labels = [(cloud.describe(name, {}) or (name,))[0].split(" · ")[0] for name in names]  # A cloud tool's card title.
    if len(labels) == 1:
        return f"{labels[0]} was only written out in the reply, not called: it did not run and nothing was charged."
    return (f"{', '.join(labels[:-1])} and {labels[-1]} were only written out in the reply, not called: they did "
            "not run and nothing was charged.")


def _unmade(session: dict, turn: Turn, request: dict, completion: llm.Completion, added: list,
            may_retry: bool = True) -> str | None:
    """A reply that called nothing and only wrote its calls out would end the turn on an account of steps
    that never happened (ar-artist-1: "both sides fixed"; ar-artist-2: "restored to the exact pre-fix state";
    p6c: a whole detail pass, nothing but copied records). Its card comes down, so that account is never the
    last word. While the turn has a nudge left (completion_guard.nudges_left, shared with announced steps) and
    has had no record correction, the model is sent back once to call the tools or say plainly that nothing ran:
    the returned text, and the reply is marked context.UNRUN. Otherwise None, and the stored reply says what
    happened; run_loop ends the turn on UNMADE_FINAL unless a finish gate sends it on. added: the items this
    request added. may_retry: False at the step limit or once the turn has an outcome, as for an announced step."""
    items, messages = session["items"], session["messages"]
    one = len(completion.unmade) == 1
    names = ", ".join(completion.unmade[:-1]) + " and " * (not one) + completion.unmade[-1]
    text = _unmade_text(completion.unmade)
    notice = next((i for i in added if i["kind"] == "notice" and i["text"] == text), None)
    retry = may_retry and not turn.unrun_claim and completion_guard.nudges_left(request) > 0
    # Out by identity, from the end: a note the user queues meanwhile is appended, never lost (turn.notes).
    gone = {id(i) for i in added if i["kind"] == "assistant" or (i is notice and not retry)}
    for index in range(len(items) - 1, -1, -1):
        if id(items[index]) in gone:
            del items[index]
    if retry:
        # The retry reads what is left of the reply; from the next turn on, what happened (send).
        messages[-1][context.UNRUN] = True
        turn.unrun_claim = request[completion_guard.CORRECTED] = True
        if notice is not None:
            notice["text"] += f" Asked the assistant to run {'it' if one else 'them'}."
        print(f"Loopcut: reply only wrote out {names}; sent back once to call them or say nothing ran.")
        return UNMADE_PROMPT.format(calls=names, them="it" if one else "them")
    completion.text = messages[-1]["content"] = UNMADE_STORED  # The next turn reads what happened, not the claim.
    if turn.pending_reply is not None:
        turn.pending_reply["text"] = ""
    print(f"Loopcut: reply only wrote out {names} and the turn has no correction left; not sent back.")
    return None


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
        if turn.pending_reply is not None:
            from . import controller
            messages[-1][controller.DRAFT] = True
        return completion
    messages.pop()  # Unanswered, it would tell the next turn that tools are off.
    return None


def _end_at_limit(session: dict, turn: Turn, cfg: config.Config, spec: LoopSpec, prompt: str = "") -> None:
    """The turn ends short of finishing: the wrap-up, then the spec's last word (it cannot bounce)."""
    completion = _wrap_up(session, turn, cfg, spec, prompt)
    _final_reply(session, turn, completion, spec)
    _review_finish(session, turn, completion, can_correct=False)
    _publish_reply(session, turn)


def _publish_reply(session: dict, turn: Turn) -> None:
    from . import controller
    reply, turn.pending_reply = turn.pending_reply, None
    if reply is not None and reply.get("text", "").strip() and not turn.cancel.is_set():
        reply["streaming"] = False
        finish_reply(reply, turn.roots[0] if turn.roots else None)
        session["items"].append(reply)
        for message in session["messages"]:
            message.pop(controller.DRAFT, None)


def _discard_reply(session: dict, turn: Turn) -> None:
    from . import controller
    turn.pending_reply = None
    for message in session["messages"]:
        if message.pop(controller.DRAFT, False):
            message["content"] = ("[Loopcut: proposed completion withheld; it was not accepted or delivered.]\n"
                                  + (message.get("content") or ""))


def _incomplete_review(session: dict, turn: Turn, reason: str) -> None:
    text = "The result is incomplete or not independently verified. " + reason
    reply = turn.pending_reply or state.item_assistant()
    reply.update(text=text, streaming=False)
    reply.pop("verification", None)
    turn.pending_reply = reply
    last = session["messages"][-1] if session["messages"] else {}
    if last.get("role") == "assistant" and not last.get("tool_calls"):
        last["content"] = text
    else:
        session["messages"].append({"role": "assistant", "content": text})
    if turn.outcome in ("running", "completed"):
        turn.outcome = "incomplete"


def _review_finish(session: dict, turn: Turn, completion, *, can_correct=True):
    from . import controller
    reviewer = turn.controller
    if reviewer is None or turn.cancel.is_set():
        return None
    can_correct = can_correct and turn.outcome == "running" and reviewer.corrections < reviewer.settings.max_corrections
    turn.touch("checking the result")
    if not any(r.get("event") == "finish" for r in reviewer.records):
        session["items"].append(state.item_notice("Checking the result before finishing."))
    try:
        decision = reviewer.review("finish", completion.text if completion else "No final reply was produced.")
    except controller.StaleReview as ex:
        if can_correct:
            reviewer.corrections += 1
            reviewer.save()
            return f"[Loopcut review] {ex}"
        _incomplete_review(session, turn, str(ex))
        return None
    except controller.ReviewError as ex:
        _incomplete_review(session, turn, str(ex))
        return None
    if decision.action == "correct" and can_correct:
        reviewer.corrections += 1
        reviewer.save()
        return decision.note()
    if decision.action in ("correct", "incomplete"):
        _incomplete_review(session, turn, decision.reason)
    return None


def _review_failure(session: dict, turn: Turn, *, hard_stop=False):
    """At most one early recovery review; it may steer a repair, never reset the existing hard stop."""
    from . import controller
    reviewer = turn.controller
    if reviewer is None or reviewer.failures:
        return None
    reviewer.failures += 1
    reviewer.save()
    try:
        decision = reviewer.review("repeated_failure", "The existing failure tracker detected repeated trouble."
                                   + (" Its hard stop has been reached." if hard_stop else ""))
    except controller.ReviewError as ex:
        return f"[Loopcut review] {ex} Existing failure limits still apply."
    if decision.action == "stop":
        turn.outcome, turn.stop_reason = "no_progress", decision.reason
    return decision.note()


def _final_reply(session: dict, turn: Turn, completion, spec: LoopSpec):
    """After the existing finish gate (including late merges), Loopcut's own check of a rig this turn animated or
    reweighted (rig_gate), its advisory note on an animation without sound (sound_gap, once per chat), then this
    turn's contact evidence."""
    if turn.cancel.is_set():
        return None
    bounce = spec.on_final(session, turn, completion) if spec.on_final else None
    if bounce and turn.outcome == "running":
        return bounce
    from . import contact, rig_gate, sound_gap
    bounce = rig_gate.finish_gate(session, turn, completion, spec.role,
                                  lambda fn: _on_main(fn, turn.cancel.is_set, "rig check"))
    if bounce:
        return bounce
    bounce = sound_gap.finish_check(session, turn, completion, spec.role,
                                    lambda fn: _on_main(fn, turn.cancel.is_set, "sound check"))
    if bounce:
        return bounce
    return contact.finish_gate(session, turn, completion,
                               lambda: _on_main(lambda: contact.refresh(turn), turn.cancel.is_set))


def _continue_announced(session: dict, turn: Turn, spec: LoopSpec, request: dict, completion, cfg: config.Config,
                        step: int, scene_action) -> bool:
    """A reply that announces its next step and calls no tool ("I will next import the desk.") goes on with
    completion_guard.CONTINUE instead of ending the turn. At most MAX_CONTINUES per user turn, the turn's record
    correction included (completion_guard.nudges_left), and never twice without a tool run in between, counted
    on the user's message so a resumed turn keeps the count. Never for a worker, after a Stop, at the step
    limit, on a question or other request for no scene work (even after a read tool ran), with a note the
    user sent during the reply (it goes to the model next), while a card waits for the user anywhere in the
    turn, before the router's own nudge to delegate on a level-3 turn, or while workers have yet to report
    (the finish gate collects them and continues the turn)."""
    if (spec.role == "worker" or turn.cancel.is_set() or turn.outcome != "running" or turn.notes
            or (cfg.max_steps and step >= cfg.max_steps)
            or scene_action is False or not (scene_action or turn.executed_tools)):
        return False
    used = request.get(completion_guard.CONTINUED, 0)
    if (completion_guard.nudges_left(request) <= 0
            or turn.executed_tools <= request.get(completion_guard.CONTINUED_AT, -1)):
        return False
    routed = getattr(turn, "routed", None)
    if (spec.role == "orchestrator" and not session.get("workers_unavailable") and routed is not None
            and routed.level >= 3 and not turn.delegations and not getattr(turn, "nudged", False)):
        return False  # router.nudge, in the finish gates, asks for the delegation first.
    from . import chat_credits  # A paid tool's refusal (0.2.19) stops nothing: the turn may still go on.
    if any(item.get("status") == "awaiting" or (item.get("kind") == "offer" and not item.get("resolved"))
           or (item.get("kind") == "limit" and item.get("status") != "done" and not chat_credits.is_tool(item))
           for item in _turn_items(session, request)):
        return False
    job = getattr(turn, "job", None)
    if job is not None:
        from . import waves
        try:
            if waves.pending(job):
                return False
        except Exception:  # Unknown: leave the turn to the finish gate.
            return False
    if not completion_guard.announces_step(completion.text):
        return False
    request[completion_guard.CONTINUED] = used + 1
    request[completion_guard.CONTINUED_AT] = turn.executed_tools
    return True


def _motion_on_main(text: str, is_cancelled):
    """motion_claims.check on the main thread: it reads and steps the live scene's frames."""
    from . import motion_claims
    return _on_main(lambda: motion_claims.check(text), is_cancelled, "the motion check")


def _motion_claim(session: dict, turn: Turn, spec: LoopSpec, request: dict, completion, cfg: config.Config,
                  step: int) -> str | None:
    """A reply that would end the turn saying scene objects move when they do not (motion_claims; rel017-app
    recapped moons it never animated as "orbiting") goes back once a user turn with motion_claims.nudge, from the
    nudges it shares with announced steps (completion_guard.MOTION_CHECKED). A later reply that still says so, or
    one that cannot go back (no nudge left, the step limit), ends the turn on motion_claims.notice. Never for a
    worker (nor a helper's report_back), after a Stop, with a note the user sent during the reply (it goes to the
    model next), or while workers have yet to report (the finish gate merges them and continues the turn)."""
    from . import motion_claims
    turn.motion_notice = ""
    if (spec.role == "worker" or turn.cancel.is_set() or turn.notes
            or not motion_claims.may_claim(completion.text)):
        return None
    job = getattr(turn, "job", None)
    if job is not None:
        from . import waves
        try:
            if waves.pending(job):
                return None
        except Exception:  # Unknown: leave the turn to the finish gate.
            return None
    try:
        found = _motion_on_main(completion.text, turn.cancel.is_set)
    except llm.Cancelled:
        raise
    except Exception as ex:  # Never in the way of the reply.
        print(f"Loopcut: motion check not run: {type(ex).__name__}: {ex}")
        return None
    if not found:
        return None
    if (not request.get(completion_guard.MOTION_CHECKED) and turn.outcome == "running"
            and not (cfg.max_steps and step >= cfg.max_steps) and completion_guard.nudges_left(request) > 0):
        request[completion_guard.MOTION_CHECKED] = True
        print(f"Loopcut: the reply says {len(found['names'])} objects move that do not; sent back once.")
        return motion_claims.nudge(found)
    turn.motion_notice = motion_claims.notice(found)
    return None


def _turn_items(session: dict, request: dict) -> list:
    """The chat items of the user's turn, across resumes: from its own message's item on. Notes sent during
    the turn are user items too: those folded into the history (STEER) and those still queued are passed over."""
    messages, items = session["messages"], session["items"]
    start = next((i for i in range(len(messages) - 1, -1, -1) if messages[i] is request), None)
    if start is None:
        return items
    notes = sum(1 for m in messages[start + 1:] if m.get(context.STEER))
    for index in range(len(items) - 1, -1, -1):
        if items[index].get("kind") != "user" or items[index].get("queued"):
            continue
        if not notes:
            return items[index:]
        notes -= 1
    return items


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
        from . import chat_credits
        chat_credits.turn_end(session, turn)  # The credits arm's "Done · N credits"; never raises.


def _tool_dropped(name: str, ex: BaseException):
    """A network connection a tool used broke and the tool let it through (2026-10-25: a library thumbnail's read
    timing out): that step failed, and the turn goes on. Before, it ended the turn on "The connection to the model
    dropped", which it had not."""
    from . import tools
    print(f"Loopcut: the {name} tool's connection dropped: {ex!r}")
    return tools.ToolResult(f"{name} failed: its network connection dropped ({llm.short_cause(ex)}). "
                            "Try it again, or go on without it.", ok=False)


def _read_result(session: dict, arguments: str):
    """read_result, answered by the loop itself from its own conversation's store (result_store): the user's chat
    for the single agent, the orchestrator and the workspace agent; a worker's job folder for a worker, whose
    tools otherwise run in its child Blender. No scene, no main thread."""
    from . import tools
    given = _arguments(arguments)
    unknown = sorted(set(given) - {"id", "grep", "lines"})
    if unknown:
        return tools.ToolResult(f"read_result takes id, grep and lines, not {', '.join(unknown)}.", ok=False)
    if not given.get("id"):
        return tools.ToolResult("read_result needs the id a cut result's last line names, such as r12.", ok=False)
    try:
        return tools.ToolResult(result_store.read(conversations.results_folder(session), given["id"],
                                                  given.get("grep"), given.get("lines")))
    except result_store.StoreError as ex:
        return tools.ToolResult(str(ex), ok=False)


def _shown(session: dict, name: str, text: str, failed: bool) -> str:
    """A tool's own text as the model gets it: whole, or its start and a pointer to the whole of it, stored with
    the conversation (result_store.keep). A store that cannot be written leaves the text whole, never a pointer
    to nothing."""
    try:
        return result_store.keep(lambda: conversations.results_folder(session), text, name, failed=failed)
    except (OSError, conversations.ConversationError) as ex:
        print(f"Loopcut: could not store the {name} result, so it is sent whole: {ex}")
        return text


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
    if turn.try_it:  # send() found the card's request unedited; resume() has only this mark.
        request[TRY_IT] = turn.try_it
    turn.try_it = request.get(TRY_IT, "")
    scene_action = completion_guard.scene_request(request)
    plan_turn = uuid.uuid4().hex
    consent = None
    alike = alike or _alike_on_main
    if run_tool is _run_tool_on_main:
        run_tool = lambda name, arguments: _run_tool_on_main(name, arguments, turn.cancel.is_set)
    try:
        cfg = turn.config or config.load()
        if spec.role != "worker" and turn.controller is None and turn.review_config is not None:
            from . import controller
            turn.controller = controller.attach(session, turn, turn.review_config, run_tool)
        first = None
        if turn.try_it and not turn.tool_steps and not turn.executed_tools:  # Resumed, the card's call has run.
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
        carry = getattr(turn, "carry_steps", False)
        step = int(request.get(STEPS_TAKEN) or 0) if carry else 0  # A resume goes on counting.
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
                        if ex.code == llm.CONTENT_FILTER:
                            items.append(state.item_notice(str(ex)))
                        print(f"Loopcut: could not describe the attached images: {ex}")
                        _undescribed(session, user_message)
                if spec.on_turn_start:
                    spec.on_turn_start(session, turn)
                    if getattr(turn, "spec", None) not in (None, spec):  # The router sent the turn to another loop.
                        spec, cfg = turn.spec, turn.config or cfg  # With that loop's config (_route).
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
            shown = {id(item) for item in items}  # Not an index: a request may drop an earlier setup error.
            # Synthetic and model tool calls share cards, consent, checkpoints, results and saving below.
            completion = first if first is not None else _request(session, turn, cfg, spec)
            first = None
            messages.append(completion.as_message(cfg.model if cfg.keep_reasoning else ""))
            if turn.pending_reply is not None:
                from . import controller
                messages[-1][controller.DRAFT] = True
            if carry and getattr(turn, "carry_steps", False):  # A failed request is made again by a resume:
                request[STEPS_TAKEN] = step  # only a reply counts.
            if not completion.tool_calls:
                bounce = None
                # The turn's one record correction, from the nudges it shares with announced steps: for calls the
                # reply only wrote out (_unmade), else for other imitated records. Like the nudge, never for a
                # worker, after a Stop, on a question, or on a reply that says plainly it stops or nothing ran.
                honest = completion_guard.stops_honestly(completion.text)
                written_out = bool(completion.unmade and spec.role != "worker" and not turn.cancel.is_set()
                                   and scene_action is not False and (scene_action or turn.executed_tools)
                                   and not honest)
                if written_out:
                    bounce = _unmade(session, turn, request, completion, [i for i in items if id(i) not in shown],
                                     turn.outcome == "running" and not (cfg.max_steps and step >= cfg.max_steps))
                elif (spec.role != "worker" and scene_action and not turn.executed_tools
                        and not turn.unrun_claim and not turn.cancel.is_set() and not honest
                        and completion_guard.nudges_left(request) > 0
                        and completion_guard.imitates_records(completion.text, (
                            s["function"]["name"] for s in _session_schemas(session, spec)))):
                    turn.unrun_claim = request[completion_guard.CORRECTED] = True
                    bounce = completion_guard.CORRECTION
                    messages[-1][context.UNRUN] = True  # Read as UNMADE_STORED from the next turn on (send).
                # Before the finish gates: they stamp Verified records on a reply they let end the turn. Never on
                # a reply that only wrote its calls out (one nudge per reply), or that stops honestly.
                if not bounce and not written_out and not honest and _continue_announced(
                        session, turn, spec, request, completion, cfg, step, scene_action):
                    bounce = completion_guard.CONTINUE
                # Before the finish gates too, and on a question as well: rel017-app's false recap answered one.
                if not bounce and not written_out and not honest:
                    bounce = _motion_claim(session, turn, spec, request, completion, cfg, step)
                if not bounce:
                    bounce = _final_reply(session, turn, completion, spec)
                if not bounce:
                    bounce = _review_finish(session, turn, completion,
                                            can_correct=not (cfg.max_steps and step >= cfg.max_steps))
                if bounce:  # Sent back, marked so it continues the turn, and the loop goes on.
                    _discard_reply(session, turn)
                    text = bounce if isinstance(bounce, str) else bounce.text
                    messages.append({"role": "user", context.STEP_LIMIT: True, "content": text})
                    if getattr(bounce, "image_path", None):  # The late workers' contact sheet.
                        messages.append(_image_message(conversations.store_image(session, bounce.image_path),
                                                       _image_what(text), file_preview=True))
                    spec.save(session)
                    _redraw()
                    continue
                if written_out:  # Not sent back, and nothing sent the turn on: it ends on Loopcut's line.
                    turn.unmade_final = UNMADE_FINAL if turn.unrun_claim else UNMADE_FINAL.replace(" again", "")
                _publish_reply(session, turn)
                if turn.outcome == "running":
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
                # Decided before the card's text: worker slots only free up between delegate calls, so a card
                # titled as the wait never asks.
                approval = _approval(call.name, call.arguments, turn.roots, turn)
                # Unasked only because it can start nothing: held to that when it runs, whatever frees by then.
                capped = spec.asks and approval == "capped" and _asks("gate", call.name, session, cfg)
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
                    if no_progress and (no_progress.skip(key) if twice else no_progress.refused(key)):
                        stalled = True
                        break
                    continue
                if approval == "install" and not spec.asks:  # A worker cannot ask, so it cannot install.
                    card["status"] = "rejected"
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": INSTALL_REFUSAL})
                    continue
                # A long render job's card is the choice: the cloud or this computer (render_offer.prepare_job).
                offer = _job_offer(spec, call, turn, _asks(approval, call.name, session, cfg))
                pick = _ask_choice(card, offer, turn) if offer else None
                if offer and pick is None:  # Not now (Esc): neither runs, and the model asks which.
                    if no_progress:
                        no_progress.reset()
                    card["status"], turn.declined = "rejected", True
                    from . import render_offer
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": render_offer.SKIPPED})
                    continue
                if not offer and spec.asks and _asks(approval, call.name, session, cfg):
                    turn.decided.clear()
                    # heavy: asking because it renders, so the card's "always" allows renders.
                    state.awaiting(card)
                    card["heavy"] = approval == "heavy" and not session["auto_heavy"]
                    card["install"] = approval == "install"
                    _redraw()
                    if not _wait_for_approval(turn):
                        if no_progress:
                            no_progress.reset()
                        card["status"], turn.declined = "rejected", True
                        messages.append({"role": "tool", "tool_call_id": call.id,
                                         "content": "The user rejected this action. Ask what they want instead."})
                        continue
                card["status"], card["started_at"] = "running", time.time()  # The card shows its time (ui.layout).
                name, arguments, note = call.name, call.arguments, ""
                if pick:  # The cloud: its render of the same frames runs on this card; here, the job as asked.
                    from . import render_offer
                    name, arguments = render_offer.job_call(pick, offer, call.arguments, card)
                _redraw()
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
                if spec.checkpoints and _changes_scene(name, call.arguments):
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
                consent = _Consent(session, turn, card, spec.asks, files_allowed=cfg.auto_run or session['auto_run'],
                                   chosen=pick == "cloud")
                with cloud_account.approval_context(consent):
                    turn.executed_tools += 1
                    request[completion_guard.EXECUTED] = turn.executed_tools
                    turn.capped_call = call.id if capped else None
                    turn.touch(f"the {call.name} tool")
                    try:
                        result = (_read_result(session, arguments) if name == result_store.TOOL
                                  else run_tool(name, arguments))
                    except Exception as ex:
                        if not llm.dropped_connection(ex):
                            raise
                        result = _tool_dropped(name, ex)
                    finally:
                        turn.capped_call = None
                if pick:  # What the user chose, and what ran in the render job's place.
                    from . import render_offer
                    render_offer.job_result(pick, offer, call.arguments, arguments, result)
                turn.touch(state.MODEL_REPLY)
                if spec.role != "worker":
                    _note_level(turn, call.name, call.arguments, delegations)
                _after_framing(turn, call.name, call.arguments, result)
                changed = spec.scene_changed(result)
                may_change = _changes_scene(name, call.arguments)
                if may_change or changed:
                    # Any run of a scene-changing tool, not only a measured change: scene_diff does
                    # not see every setting, and a read after such a step may well differ.
                    turn.ledger.scene_changed()
                    seen[key][1] = turn.ledger.revision  # Its own double right after it is still one call.
                # Code that caught its own exception and printed it failed too, unless it changed the scene.
                worked = result.ok and (changed or not repeats.printed_traceback(call.name, result.text))
                if no_progress:
                    # Workers return the child's measured change flag instead of full snapshots.
                    # Without either, a potentially mutating tool is conservatively progress.
                    measured = ((spec.role == "worker" and hasattr(result, "changed"))
                                or (getattr(result, "scene_before", None) is not None
                                    and getattr(result, "scene_after", None) is not None))
                    stalled = no_progress.record(key, worked, changed or (may_change and not measured))
                ledger_note = turn.ledger.record(step, key, result.text, worked, read, step_message)
                if consent.rig:  # The rig card shows its result text; the ledger's note is for the model.
                    result.model_note = (getattr(result, 'model_note', '') or '') + ledger_note
                    ledger_note = ""
                # Nor on any other card: "Same error as before. Already tried: step 14 {...}" on a failed step read
                # as something broken (a free-plan persona, 2026-10-24). The model's copy keeps it where it was.
                own = len(result.text)
                result.text += ledger_note
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
                # A paid call the user skipped did not fail: it is shown as skipped (spending.declined). A delegate
                # whose workers merged and only kept parts aside is done; the model's result still says what is held
                # back (waves.held_back_only).
                card["status"] = ("done" if result.ok or getattr(result, "held_back", False)
                                  else "rejected" if card.get("declined") else "failed")
                _failed_card(card, result)
                # An asset search's card keeps its report's JSON (card_output); the model's copy is a table.
                card_text = getattr(result, "card_output", "") or result.text[:own]
                card["output"] = card_text + result.text[own + len(ledger_note):]
                if getattr(result, "outcome", ""):  # A report's line; its JSON stays behind the card's toggle.
                    card["outcome"] = result.outcome
                if card.get("plan_turn") and getattr(result, "plan_refused", False):
                    card["plan_repair"] = "needed"
                elif card.get("plan_turn") and getattr(result, "plan_capped", False):
                    card["plan_repair"] = "capped"  # The turn's worker limit: a neutral card (ui.layout).
                if getattr(result, "timed_out", 0):
                    card["timed_out"] = result.timed_out  # Its card says Blender was frozen, not a mistake (ui.layout).
                consent.finish(result)
                if getattr(result, "receipt", "") and not consent.rig:
                    card["cost"] = result.receipt  # What the paid call used, in place of its estimate (M4).
                if spec.role != "worker":
                    _made_pictures(session, card, call, result)
                for guard in getattr(result, "guard_notes", None) or ():  # Already in result.text too.
                    items.append(state.item_guard(guard["text"], guard["modifiers"]))
                if getattr(result, "notice", ""):
                    items.append(state.item_notice(result.notice))
                model_note = getattr(result, 'model_note', '')
                # Cut as it is made, the tool's own text only: what Loopcut appended after it stays in view. The card
                # keeps the whole text; a failed call's end shows beside its start.
                failed = not result.ok or repeats.printed_traceback(call.name, result.text)
                content = _shown(session, call.name, result.text[:own], failed) + result.text[own:]
                messages.append({"role": "tool", "tool_call_id": call.id,
                                 "content": content + ('\n\n' + model_note if model_note else '')})
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
            stuck_steps = no_progress.end_step() if no_progress else 0
            if spec.role != "worker" and (stuck_steps == repeats.STUCK_STEPS or stalled):
                note = _review_failure(session, turn, hard_stop=stalled)
                if note:
                    messages.append({"role": "user", context.STEP_LIMIT: True, "content": note})
                stalled = stalled or turn.outcome == "no_progress"
                if not stalled and stuck_steps == repeats.STUCK_STEPS:
                    items.append(state.item_notice(STUCK_NOTICE))
            _resume_playback(spec)
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
        if turn.stop_reason == "stalled":  # The stall clock, not the user: an error another try may get past.
            turn.outcome, turn.error_code = "error", "stalled"
            items.append(state.item_error(stall_message(turn.stall_seconds), retry=True))
        else:
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
        elif own_key_text := own_key.chat_text(ex, getattr(turn, "own_key", None),
                                                    getattr(turn.config, "model", "")):
            # The user's own key: the sentence its check shows in Preferences and above the composer (own_key.py).
            items.append(state.item_error(own_key_text, retry=getattr(ex, "status", None) == 429))
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
        elif getattr(ex, "code", "") in UPSTREAM_CODES and config.on_service(cfg.base_url):
            items.append(state.item_error(_upstream_text(ex), retry=True))
        else:
            retry = getattr(ex, "code", "") in (llm.CONNECTION_LOST, llm.EMPTY_REPLY, llm.STREAM_FAILED)
            items.append(state.item_error(str(ex), retry=retry))
        if getattr(ex, "account", None) and not handled_refusal:
            _hosted_reply(turn, cfg, ex.account, session=session)
    except Exception as ex:  # Last line of defence for the worker thread: show it, never swallow it.
        from . import version_guard
        turn.outcome = "error"
        turn.error_code = "connection_lost" if llm.dropped_connection(ex) else "internal"
        import traceback
        traceback.print_exc()
        # A provider's client let a broken connection through: plain words. Never a tool's (_tool_dropped).
        if llm.dropped_connection(ex):
            items.append(state.item_error(str(llm.connection_lost("the model", ex)), retry=True))
        elif isinstance(ex, version_guard.RestartNeeded):  # Updated under this Blender: only a restart helps.
            turn.error_code = "restart_needed"
            items.append(state.item_error(str(ex)))
        else:
            items.append(state.item_error(f"Internal error: {ex!r}"))
    finally:
        _discard_reply(session, turn)  # Cancellation/errors cannot turn a private draft into accepted history.
        conversations.close_open_tool_calls(messages, "Cancelled before this ran.")
        _resume_playback(spec)
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
        for item in items + (consent.shown[1:] if consent else []):
            if item.get("streaming"):
                item["streaming"] = False
                finish_reply(item, turn.roots[0] if turn.roots else None)
            click = tuple(item.get("click") or ())
            if item.get("status") in ("awaiting", "running") and click in state.running_clicks:
                if item["status"] == "awaiting" and click in _card_waits:
                    _card_waits[click].answer(False)  # Its ask ends with the turn, as the turn's own asks do.
                continue  # A Turn it into 3D click's call is not this turn's: it goes on (turn_into_3d).
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
        # records to the UI since the model's final text was checked above. A reply that only wrote its
        # calls out and ended the turn has Loopcut's own line instead.
        if turn.unmade_final:
            items.append(state.item_notice(turn.unmade_final))
        elif (spec.role != "worker" and not spec.drive and not turn.executed_tools
                and turn.unrun_claim):
            items.append(state.item_notice(completion_guard.NOTICE))
        if turn.motion_notice:
            items.append(state.item_notice(turn.motion_notice))
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
    from . import nudges, video_attach
    waiting = video_attach.preparing(session)
    if waiting:  # First: a chip's clip still preparing is not yet attached, but must not be asked for again.
        session["items"].append(state.item_notice(f"Still preparing {', '.join(waiting)}; send once it is ready."))
        return False
    missing = nudges.chip_needs(text, attachments, session.get("references") or [])
    if missing:  # A trial chip's request about the user's own file, with none attached: it waits in the input.
        from . import cloud
        other_way = nudges.make_one_first(cloud.make_one_prices()) if missing == "a picture" else ""
        session["items"].append(state.item_notice(
            " ".join(filter(None, [f"Attach {missing} first: drop it on the panel or pick it from +.", other_way]))))
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
    stopped = note_items is None and session.get("turn_outcome") == "cancelled"  # Not "send now", which stops too.
    # What a shell command left running and has ended since (workspace.ended_notes) goes in before turn.submission is
    # taken, so a refused first request keeps it, as a click's note.
    from . import workspace
    session.setdefault("between_turns", []).extend(workspace.ended_notes(session["id"]))
    messages = session["messages"]
    last = str(messages[-1].get("content")) if messages and messages[-1].get(context.STEP_LIMIT) else ""
    from . import motion_claims
    if (last in (completion_guard.CONTINUE, completion_guard.CORRECTION) or last.startswith(UNMADE_PROMPT[:30])
            or last.startswith(motion_claims.NUDGE_HEAD)):
        # A nudge or record correction left last by a Stop or an error; resume() goes on from it, a new message
        # replaces it (it would ask the model to run what the user stopped).
        messages.pop()
    for message in messages:
        if message.pop(context.UNRUN, False):  # A reply sent back last turn: its claims are not read as work done.
            message["content"] = UNMADE_STORED
    if turn.config is not None and config.on_service(turn.config.base_url):
        turn.submission = {"text": draft, "attachments": attachments, "items": list(session["items"]),
                           "messages": deepcopy(session["messages"]), "references": list(session["references"]),
                           "between_turns": list(session.get("between_turns") or []),
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
    scene = _scene_context(text, session.get("scene_seen"))
    if stopped:
        scene = scene.replace("\n</scene_context>", f"\n{STOPPED_LINE}\n</scene_context>", 1)
    extras = _extras_line(turn.config)
    if extras:
        scene = scene.replace("\n</scene_context>", f"\n{extras}\n</scene_context>", 1)
    # What the user started from a card since the last turn (turn_into_3d), and a click still running: the model's
    # only. Taken after turn.submission keeps them for a first request the service refuses.
    between = list(session.get("between_turns") or [])
    session["between_turns"] = []
    between += [f"running now: the user's Turn it into 3D on the picture {name}; its model is not in the scene yet."
                for (chat, _), name in list(state.running_clicks.items()) if chat == session["id"]]
    between += workspace.running_notes(session["id"])  # And what a shell command left running that still runs.
    if between:
        scene = scene.replace("\n</scene_context>", "\n" + "\n".join(between) + "\n</scene_context>", 1)
    content = f"{scene}\n\n{text or 'See the attached images.'}"
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
    _launch(session, turn)
    return True


def _launch(session: dict, turn: Turn) -> None:
    # Saved as running first (send's message, resume's retry): if Blender dies before the turn's last save (a crash,
    # a kill, a power cut), the next load still ends it with the closing card and Try again (conversations.load).
    conversations.save(session, running=True)
    from . import session_logs, telemetry
    turn.started, turn.first_item = time.monotonic(), len(session["items"])
    turn.telemetry = telemetry.turn_start(turn.config)  # Main thread: which kind of model runs this turn.
    turn.own_key = own_key.for_turn(turn.config)  # The own key's provider, to word a setup failure (run_loop).
    turn.conversation_log = session_logs.turn_start(session, turn.config)  # {} when it is not uploaded.
    from . import chat_credits
    chat_credits.turn_start(session, turn)  # The balance it starts from, in the credits arm; never raises.
    if chat_credits.is_tool((state.ui.get("allowance_notice") or {}).get("item")):
        state.ui.pop("allowance_notice")  # A paid tool's refusal was its turn's; its card stays, as history.
    session["scroll"], session["confirm_restore"] = 0.0, None
    if llm._is_loopback(getattr(turn.config, "base_url", "")):
        turn.stall_seconds = 0  # The Claude Code bridge may take minutes, as call_max_seconds allows it (llm.py).
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


def decide(approved: bool, always: bool = False, index: int | None = None, drawn: tuple | None = None) -> bool:
    """Answer a pending approval card: the one at `index` when it is waiting (a click), else the first waiting.
    drawn: (id, ask_serial) of the card a panel frame drew at `index` (ui.host: a click, Enter, Esc); then only that
    card, in that ask, is answered, and a frame drawn before the items moved or the card asked again answers none.
    `always` stops asking in this conversation: for renders when the card is a render's, otherwise for everything
    else; never for paid or install cards. A worker's ask (a paid call in the background) is answered on its own
    card, so a card of the user's turn waiting at the same time keeps its own answer."""
    session = state.session()
    items = session["items"]
    waiting = index is not None and 0 <= index < len(items) and items[index].get("status") == "awaiting"
    if drawn is not None and not (waiting and (id(items[index]), items[index].get("ask_serial")) == tuple(drawn)):
        return False
    card = items[index] if waiting else next((i for i in items if i.get("status") == "awaiting"), None)
    if card is None:
        return False
    if approved and card.get("render_choice") and card.get("picked") != "cloud":
        from . import render_offer  # Enter on the choice card: only a Recommended cloud the balance covers.
        if not render_offer.enter_ok(card["render_choice"], render_offer.live_balance()):
            return False
    wait = _card_waits.get(_wait_key(card))
    if wait is not None:
        wait.answer(approved)
        if card.get("click"):  # A reopened chat's copy shows the answer as the click's own card does (_Consent._ask).
            card["status"] = "running" if approved else "rejected"
            for key in ("ask", "ask_lines", "ask_label", "ask_link", "ask_skip", "ask_title", "image_path"):
                card.pop(key, None)
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


def check_stall(held: float = 0.0, now: float | None = None) -> None:
    """Main thread, from mainthread's pump while a turn runs: its stall clock. held: the seconds the pump spent in
    main-thread tasks, or ran late because Blender held its thread, since its last call; they never count. The clock
    waits while the turn could not move on by itself (_stall_paused). Past turn.stall_seconds with nothing new, the
    turn is stopped, and run_loop's Cancelled branch says so with Try again."""
    session = state.session()
    turn = session.get("turn")
    if turn is None or not getattr(turn, "stall_seconds", 0) or turn.cancel.is_set():
        return
    now = time.time() if now is None else now
    checked, turn.checked_at = getattr(turn, "checked_at", None) or now, now
    if _stall_paused(session, turn):
        turn.progress_at = now
        return
    if held:
        turn.progress_at = min(now, turn.progress_at + held)
    if now - checked > WAKE_SECONDS:  # The pump did not run (the Mac slept): what was in flight gets time to land.
        turn.progress_at = max(turn.progress_at, now - max(0.0, turn.stall_seconds - WAKE_SECONDS))
    silent = now - turn.progress_at
    if silent >= turn.stall_seconds:
        print(f"Loopcut: turn stalled: nothing new for {silent:.0f} s, waiting for {turn.progress}", flush=True)
        turn.stop_reason = "stalled"
        turn.cancel.set()


def stall_message(seconds: int) -> str:
    """STALL_MESSAGE for the turn's limit: "15 minutes" at config.DEFAULT_STALL_SECONDS, else what
    LOOPCUT_STALL_SECONDS set, in the largest unit it is a whole number of."""
    seconds = max(1, int(seconds))
    count, unit = next((seconds // size, unit) for size, unit in ((3600, "hour"), (60, "minute"), (1, "second"))
                       if seconds % size == 0)
    return STALL_MESSAGE.format(limit=f"{count} {unit}{'s' if count != 1 else ''}")


def _stall_paused(session: dict, turn: Turn) -> bool:
    """A card waits for the user (the turn's own or a worker's ask); a command runs that has a time limit of its own
    and that Stop kills (a workspace process: run_blender, shell; Codex's shell, its card running); or the turn's
    workers are within their own time (worker_max_seconds and waves.HARD_GRACE) while the job's scheduler runs them."""
    if any(item.get("status") == "awaiting" or (item.get("name") == "shell" and item.get("status") == "running")
           for item in session["items"]):
        return True
    from . import waves, workspace
    if workspace.live_rows():
        return True
    job = getattr(turn, "job", None)
    scheduler = getattr(job, "scheduler", None)
    if scheduler is None or not scheduler.is_alive():
        return False
    limit, clock = job.budget.seconds + waves.HARD_GRACE, job.budget.clock()
    return any(w.finished is None and (w.thread is None or clock - w.started <= limit) for w in list(job.crew))
