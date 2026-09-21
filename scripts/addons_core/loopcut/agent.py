"""The agent loop: stream a reply, run the tools it asks for, feed results back, repeat."""

import json
import threading
from concurrent.futures import TimeoutError as FutureTimeout

from . import checkpoints, config, context, conversations, llm, state

SYSTEM_PROMPT = """You are Loopcut, an AI agent inside the user's running Blender session. You act through tools: \
run_python runs bpy code; get_scene_info and get_object_info read the scene; inspect_api is this Blender's \
Python API; capture_viewport shows you the scene.

Each user message starts with <scene_context>: the open file, mode, what the user has selected ("this", \
"it", "these") and, for small scenes, every object. @Name in a message refers to that object, material \
or collection, described in the same block.

- Look before you edit: use the context block, and get_object_info before changing an existing material, \
modifier or rig; change what is there instead of rebuilding the user's work.
- One run_python step per coherent piece of work (a whole material, several objects), not one per object. \
Its result ends with "Scene changes", measured from the scene: trust that; do not print values to check.
- Your bpy knowledge is from older versions. When unsure of a name, and always after an AttributeError, \
TypeError or "enum not found", look it up with inspect_api before retrying.
- The context lists the running Blender version and available render engines. Use those capabilities. \
Material previews use studio lighting; rendered viewport captures use EEVEE and approximate Cycles. \
Do not rebuild materials or boost lights to compensate for an unverified preview problem. Use a real \
camera render when final lighting, glass or reflections need verification.
- inspect_scene returns a labeled contact sheet across named cameras and frames. Use geometry for \
shape, materials for neutral studio lighting, final_lighting for the actual scene engine and lights. \
It renders a copy with a bounded budget while the editor stays responsive; stale results are rejected. \
Use it to check all requested cameras and meaningful animation phases. Its metadata reports the \
engine, quality limits, saved revision and signal measurements. A nearly black image can be a scene \
problem; it is not proof that materials or lights need rebuilding. Inspect the cause first.
- After every run_python step that changes the scene you get a capture of the result (three_quarter, or \
the last angle you asked for) with object names drawn on and, below it, a strip of your earlier looks \
oldest to newest: your progress. When that capture would look the same as your last one you get a note \
instead of the image. Ask for capture_viewport or compare_with_reference only for another angle, a \
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
render=true renders now: that, like any render or bake in run_python, waits for the user's OK every time.
- For render/export deliverables use start_render_job: a saved scene copy renders outside the editing \
process, with an explicit frame list, dimensions, samples and time budget. Approval covers this job \
only. render_job_status reports progress and verified files; cancel_render_job stops it; resume_render_job \
reuses verified frames from the SAME saved revision with a fresh approval of the same budget. MP4 jobs \
retain their PNG sequence. Do not poll repeatedly while nothing changes. Give the user the job status \
and output location; never describe a queued/running job as a completed export. Jobs remain in the Jobs \
view after closing the chat. Previews of jobs show their saved revision, not later scene edits.
- Poly Haven (CC0, no key) through search_polyhaven and import_polyhaven: scanned models, PBR \
materials and HDRI skies. When the user wants a realistic prop, surface or environment light, search \
it before building one from primitives; choose by the thumbnails, then place and scale what arrives.
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
- Before finishing a multi-part job, check each requested deliverable against the scene: camera framing, \
animation endpoints and middle, preservation constraints, output settings and actual output files. \
Distinguish a configured scene, a preview, and a completed render or export; say which you verified.
- As the conversation grows, the code and results of older steps are cut to a line, and a \
<conversation_summary> may stand for earlier messages. The scene is the source of truth: look again \
rather than trust memory, and write each step so it stands on its own."""

_POLL_SECONDS = 0.1
# Looks at the scene cost an image each. A look after a change is what the loop is for, and the
# agent takes one itself after every change; asking for the same look again at a scene that has
# not changed is capped, and past SOFT_LOOKS a turn the model is told to wrap up.
MAX_IDLE_CAPTURES = 3
SOFT_LOOKS = 8
DEFAULT_ANGLE = "three_quarter"
STRIP_LOOKS = 4  # Earlier looks shown in the progress strip under a new capture.
LOW_ALLOWANCE = 0.8  # Share of the week's allowance used at which the conversation gets a heads-up, once.
CAPTURE_TOOLS = {"capture_viewport", "compare_with_reference", "inspect_scene"}

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
        self.roots: tuple = ()      # Folders the file tools read unasked; files.project_roots, set by send().
        # scene_diff snapshots from before the first and after the last scene-changing step.
        self.config: config.Config | None = None  # Loaded on the main thread by send().
        self.scene_before: dict | None = None
        self.scene_after: dict | None = None
        self.notes: list = []       # (text, chat item) sent while the turn runs; folded in at the next step.
        self.outcome = "running"  # completed, cancelled, step_limit or error; retained by evals.


def _redraw() -> None:
    from . import mainthread
    mainthread.request_redraw()


def _run_tool_on_main(name: str, arguments: str, is_cancelled=lambda: False):
    from . import mainthread, tools

    def execute():
        if is_cancelled():
            raise llm.Cancelled()
        return tools.execute(name, arguments)

    future = mainthread.run_on_main(execute)
    while True:
        try:
            result = future.result(timeout=_POLL_SECONDS)
            break
        except FutureTimeout:
            if future.done():
                return future.result()  # Propagate a tool's TimeoutError; it is not a polling timeout.
            if is_cancelled() and future.cancel():
                raise llm.Cancelled()
            # A native Blender operation already running cannot be interrupted safely. Keep
            # the turn busy until its result is accounted for; do not start another tool.
    if getattr(result, "pending_job", None):
        from . import inspection
        return inspection.wait_result(result.pending_job, is_cancelled, mainthread.run_on_main)
    return result


def _inspection_current(token: dict) -> bool:
    from . import inspection, mainthread
    return mainthread.run_on_main(lambda: token == inspection.live_token()).result()


def _fresh_images(images: list) -> list:
    """A later tool in one model batch can invalidate an earlier asynchronous inspection."""
    fresh = []
    for reference, what, token, card, message in images:
        if token is not None and not _inspection_current(token):
            note = "\nInspection image omitted: the scene changed later in this tool batch. Inspect the new revision."
            card["status"], card["output"] = "failed", card["output"] + note
            message["content"] += note
        else:
            fresh.append((reference, what))
    return fresh


def _approval(name: str, arguments: str, roots) -> str:
    """"" when the call runs unasked; "gate" when the user must approve it unless they said
    "Always allow" or auto-run is on; "heavy" when they must approve it every time: a render,
    a bake, a simulation, which can hold Blender for minutes."""
    from . import files, tools
    parsed = _arguments(arguments)
    if tools.is_heavy(name, parsed):
        return "heavy"
    if name in tools.NEEDS_APPROVAL or files.needs_approval(name, parsed, roots):
        return "gate"
    return ""


def _changes_scene(name: str) -> bool:
    from . import tools
    return name in tools.CHANGES_SCENE


def _ensure_checkpoint_on_main(session: dict) -> None:
    from . import mainthread
    mainthread.run_on_main(lambda: checkpoints.ensure_for_turn(session)).result()


def _scene_context(text: str, since: dict | None) -> str:
    from . import scene_context
    return scene_context.for_message(text, since)


def _addons_line() -> str:
    from . import addons
    return addons.line(addons.enabled())


def _system_prompt(session: dict) -> str:
    """The fixed prompt plus what is installed, read at the turn's start on the main thread."""
    extra = session.get("addons_line") or ""
    return f"{SYSTEM_PROMPT}\n\n{extra}" if extra else SYSTEM_PROMPT


def _scene_snapshot() -> dict:
    from . import scene_diff
    return scene_diff.snapshot()


def _resend_on_main(text: str) -> None:
    """A note that arrived too late to be folded in starts the next turn by itself."""
    from . import mainthread
    mainthread.run_on_main(lambda: send(text))


def _restore_input_on_main(text: str) -> None:
    from . import mainthread
    from .ui import textedit

    def restore():
        session = state.session()
        if not session["input"]:
            textedit.set_text(session, text)
    mainthread.run_on_main(restore)


def _fold_notes(session: dict, turn: Turn) -> None:
    """What the user sent while the turn ran goes in before the next request, marked so the
    model continues rather than starts over. The chat item drops its 'queued' tag."""
    while turn.notes:
        text, item = turn.notes.pop(0)
        item.pop("queued", None)
        session["messages"].append({"role": "user", context.STEER: True, "content": f"<steer>{text}</steer>"})


def _project_roots() -> tuple:
    from . import files
    return files.project_roots()


def _update_account(info: dict) -> None:
    from . import account
    account.update_usage(info)


def _warn_low_allowance(session: dict, items: list, info: dict) -> None:
    """Once per conversation, when the week's allowance is nearly used: the moment to upgrade
    is before the stop, not at it."""
    used = (info.get("week") or {}).get("used")
    if session.get("warned_low") or not isinstance(used, (int, float)) or used < LOW_ALLOWANCE:
        return
    session["warned_low"] = True
    left, plan = max(0, round((1 - used) * 100)), str(info.get("plan") or "")
    if plan == "free":
        items.append(state.item_notice(
            f"About {left}% of this week's free allowance is left. Upgrading now keeps this conversation "
            f"going without a stop.", "See plans", url=config.pricing_url("low_allowance")))
    else:
        items.append(state.item_notice(f"About {left}% of this week's allowance is left on the {plan.capitalize()} plan.",
                                       "Account", url=config.account_url()))


def _limit_item(ex: llm.LLMError) -> dict:
    """The card for a 402: Loopcut's gateway says which window ran out, when it resets and what
    the next plan offers; any other provider's 402 shows its message with no button."""
    details = ex.details
    upgrade = details.get("upgrade") if isinstance(details.get("upgrade"), dict) else None
    return state.item_limit(str(details.get("message") or ex), ex.code or "limit", str(details.get("plan") or ""),
                            str(details.get("resets_at") or ""), upgrade)


def _build_strip_on_main(paths: list) -> "Path | None":
    from . import mainthread, tools
    return mainthread.run_on_main(lambda: tools.progress_strip(paths)).result()


def _alike_on_main(a, b) -> bool:
    from . import mainthread, tools
    return mainthread.run_on_main(lambda: tools.images_alike(a, b)).result()


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
    from . import files, inspection, job_tools, polyhaven, tools
    return tools.SCHEMAS + files.SCHEMAS + job_tools.SCHEMAS + inspection.SCHEMAS + polyhaven.SCHEMAS


def _describe(call: llm.ToolCall) -> tuple[str, str]:
    try:
        arguments = json.loads(call.arguments) if call.arguments.strip() else {}
    except ValueError:
        return call.name, call.arguments
    if not isinstance(arguments, dict):
        return call.name, call.arguments
    if call.name == "inspect_scene":
        from . import inspection
        return inspection.describe(arguments)
    from . import files, job_tools, polyhaven
    return job_tools.describe(call.name, arguments) or files.describe(call.name, arguments) or polyhaven.describe(call.name, arguments) or (str(arguments.get("summary") or call.name), str(arguments.get("code") or ""))


def _arguments(arguments: str) -> dict:
    try:
        parsed = json.loads(arguments) if arguments.strip() else {}
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


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
    """What a capture shows, from the tool's own words: the last 'Image attached: ...' line."""
    line = next((ln for ln in reversed(text.splitlines()) if ln.startswith("Image attached: ")), "")
    what = line[len("Image attached: "):].split(" Nearest to the viewpoint", 1)[0]
    return what.rstrip(".")


def _earlier_looks(messages: list) -> list[str]:
    """References to the newest STRIP_LOOKS captures, oldest first: the frames of the next strip.
    A capture message's first image is the capture; a strip it carries is not a look."""
    refs = []
    for message in reversed(messages):
        if context.is_capture(message):
            images = context._image_refs(message)
            if images:
                refs.append(images[0])
            if len(refs) == STRIP_LOOKS:
                break
    return refs[::-1]


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


def _image_message(reference: str, what: str, strip: str | None = None) -> dict:
    """A capture as a message: references to files in the conversation's folder, expanded when a
    request is sent. `what` it shows is text, so it outlives the image. With a strip, the message
    carries the earlier looks too, as one small image."""
    message = {"role": "user", context.CAPTURE: True, "content": [
        {"type": "text", "text": f"{context.CAPTURE_LABEL}{what or 'the scene'} (after the step above)."},
        {"type": "image_url", "image_url": {"url": reference}},
    ]}
    if strip:
        message[context.STRIP] = True
        message["content"] += [
            {"type": "text", "text": "Your earlier looks, oldest on the left to newest on the right: "
                                     "compare them with the capture above to see your progress."},
            {"type": "image_url", "image_url": {"url": strip}},
        ]
    return message


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


def _reference_card(session: dict, cfg: config.Config, message: dict, is_cancelled) -> list[str]:
    """Describe the images the user just attached, so the description outlives the turn and any
    summary, and the model works from a checklist rather than a glance. Returns their names."""
    refs = [p["image_url"]["url"] for p in message["content"] if p.get("type") == "image_url"]
    names = [r["name"] for r in session["references"] if r["ref"] in refs]
    wired = conversations.wire_messages(session["id"], [message])[0]["content"]
    parts = [{"type": "text", "text": "Reference images: " + ", ".join(names) + "."}] + [p for p in wired if p.get("type") == "image_url"]
    completion = llm.stream_chat(
        base_url=cfg.base_url, api_key=cfg.api_key, model=cfg.model,
        messages=[{"role": "system", "content": REFERENCE_PROMPT}, {"role": "user", "content": parts}],
        tools=[], reasoning_effort="low", on_text=lambda _: None, is_cancelled=is_cancelled)
    card = completion.text.strip()
    if card:
        message["content"][0]["text"] += f"\n\n<reference_card images=\"{', '.join(names)}\">\n{card}\n</reference_card>"
    message[context.REFERENCE_CARD] = True
    return names


def _auto_look(session: dict, turn: Turn, run_tool, result, alike) -> None:
    """The look the agent takes after a step that changed the scene, unless the step took one:
    what the model would ask for next anyway, without the request that asking costs. It is
    not a look the model asked for, so it is not counted. A failure is not the step's failure.
    A look that shows the same picture as the previous capture is a note, not an image: the
    capture is free, the image in every later request is not."""
    from . import tools
    try:
        shot = run_tool("capture_viewport", json.dumps({"angle": turn.angle}))
    except llm.Cancelled:
        return  # The mutation already ran; keep its result before honoring Stop.
    if not (shot.ok and shot.image_path):
        result.text += f"\n\nAutomatic preview unavailable: {shot.text}"
        return
    turn.changed = False
    previous = _earlier_looks(session["messages"])[-1:]
    try:
        same = bool(previous) and alike(conversations.image_path(session["id"], previous[0]), shot.image_path)
    except Exception as ex:  # Comparing is a saving, never a reason to lose the look.
        print(f"Loopcut: could not compare the capture with the previous one: {ex!r}")
        same = False
    if same:
        result.text += SAME_LOOK_NOTE.format(angle=turn.angle, width=tools.CAPTURE_WIDTH)
        return
    result.text += f"\n\n{shot.text}"
    result.image_path = shot.image_path


def _run(session: dict, turn: Turn, run_tool=_run_tool_on_main,
         ensure_checkpoint=_ensure_checkpoint_on_main, build_strip=None, alike=None) -> None:
    items, messages = session["items"], session["messages"]
    build_strip = build_strip or _build_strip_on_main
    alike = alike or _alike_on_main
    if run_tool is _run_tool_on_main:
        run_tool = lambda name, arguments: _run_tool_on_main(name, arguments, turn.cancel.is_set)
    try:
        cfg = turn.config or config.load()
        last = messages[-1] if messages else {}
        if last.get(conversations.ATTACHED) and not last.get(context.REFERENCE_CARD):
            try:
                names = _reference_card(session, cfg, last, turn.cancel.is_set)
                items.append(state.item_notice("Studied the reference: " + ", ".join(names)))
            except llm.LLMError as ex:
                print(f"Loopcut: could not describe the attached images: {ex}")
        for _ in range(cfg.max_steps):
            if turn.cancel.is_set():
                raise llm.Cancelled()
            _fold_notes(session, turn)
            prepared, compacted = context.prepare(session, cfg, turn.cancel.is_set)
            if compacted:
                items.append(state.item_notice(
                    f"Summarized {compacted} earlier messages so requests stay small."))
            estimated = context.estimate_tokens(prepared)
            reply = state.item_assistant()
            items.append(reply)

            def on_text(delta: str, reply=reply) -> None:
                reply["text"] += delta
                _redraw()

            completion = llm.stream_chat(
                base_url=cfg.base_url, api_key=cfg.api_key, model=cfg.model,
                messages=[{"role": "system", "content": _system_prompt(session)},
                          *conversations.wire_messages(session["id"], prepared, context.unpinned_refs(session))],
                tools=_tool_schemas(), reasoning_effort=cfg.reasoning_effort,
                on_text=on_text, is_cancelled=turn.cancel.is_set,
            )
            reply["streaming"] = False
            if completion.usage:
                for key in ("input", "output"):
                    session["usage"][key] += completion.usage[key]
                session["usage"]["context"] = completion.usage["input"]
                context.calibrate(session, estimated, completion.usage["input"])
            if completion.account:
                _update_account(completion.account)
                _warn_low_allowance(session, items, completion.account)
            if not reply["text"].strip():
                items.remove(reply)
            messages.append(completion.as_message())
            if not completion.tool_calls:
                turn.outcome = "cancelled" if turn.cancel.is_set() else "completed"
                return

            images = []
            for call in completion.tool_calls:
                if turn.cancel.is_set():
                    raise llm.Cancelled()
                summary, code = _describe(call)
                card = state.item_tool(call.name, summary, code)
                items.append(card)
                approval = _approval(call.name, call.arguments, turn.roots)
                if approval == "heavy" or (approval and not (cfg.auto_run or session["auto_run"])):
                    turn.decided.clear()
                    card["status"], card["heavy"] = "awaiting", approval == "heavy"
                    _redraw()
                    if not _wait_for_approval(turn):
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
                        continue
                    arguments, note = _without_capture(arguments), f"\n\nCapture skipped: {refusal}"
                if _changes_scene(call.name):
                    try:
                        ensure_checkpoint(session)
                    except checkpoints.CheckpointError as ex:
                        # No way back means no going forward: the step is not run.
                        card["status"] = "failed"
                        card["output"] = f"Not run. {ex}"
                        messages.append({"role": "tool", "tool_call_id": call.id, "content":
                                         f"This step was NOT run, because a checkpoint could not be saved "
                                         f"first: {ex} Tell the user; do not retry."})
                        continue
                if turn.cancel.is_set():
                    raise llm.Cancelled()
                result = run_tool(call.name, arguments)
                if note:
                    result.text += note
                asked_look = call.name in CAPTURE_TOOLS or (call.name == "run_python" and _wants_capture(arguments))
                if asked_look:
                    result.text += _look_note(turn)
                    turn.changed = False
                if _scene_changed(result):
                    turn.changed, turn.idle_captures = True, 0
                    if cfg.auto_look and not asked_look and not refusal and not turn.cancel.is_set():
                        _auto_look(session, turn, run_tool, result, alike)
                card["status"] = "done" if result.ok else "failed"
                card["output"] = result.text
                messages.append({"role": "tool", "tool_call_id": call.id, "content": result.text})
                if result.image_path:
                    # Stored now: every capture is written to the same file, so a second capture in
                    # this batch would otherwise replace the first before it is read.
                    images.append((conversations.store_image(session, result.image_path), _image_what(result.text),
                                   getattr(result, "evidence_token", None), card, messages[-1]))
                if getattr(result, "scene_before", None) is not None:
                    turn.scene_before = turn.scene_before or result.scene_before
                    turn.scene_after = result.scene_after
            # Tool messages must directly follow the assistant message, so images go after them all.
            # The newest carries the strip of earlier looks, built before this step's are among them.
            images = _fresh_images(images)
            strip = None
            earlier = _earlier_looks(messages) if images else []
            if earlier:
                try:
                    path = build_strip([conversations.image_path(session["id"], ref) for ref in earlier])
                    strip = conversations.store_image(session, path) if path else None
                except Exception as ex:  # A strip is a bonus; a step never fails for want of one.
                    print(f"Loopcut: could not build the progress strip: {ex!r}")
            messages.extend(_image_message(reference, what, strip if n == len(images) - 1 else None)
                            for n, (reference, what) in enumerate(images))
            conversations.save(session)  # The history is consistent here: every call has its result.
            _redraw()
        turn.outcome = "step_limit"
        items.append(state.item_error(f"Stopped after {cfg.max_steps} steps without finishing."))
    except llm.Cancelled:
        turn.outcome = "cancelled"
        items.append(state.item_error("Stopped."))
    except (config.ConfigError, llm.LLMError) as ex:
        turn.outcome = "error"
        if getattr(ex, "status", None) == 402:
            items.append(_limit_item(ex))
        else:
            items.append(state.item_error(str(ex)))
    except Exception as ex:  # Last line of defence for the worker thread: show it, never swallow it.
        turn.outcome = "error"
        import traceback
        traceback.print_exc()
        items.append(state.item_error(f"Internal error: {ex!r}"))
    finally:
        conversations.close_open_tool_calls(messages, "Cancelled before this ran.")
        changed = _changes_item(session, turn)
        if changed:
            items.append(changed)
        if turn.scene_after is not None:
            session["scene_seen"] = turn.scene_after  # What the next message's context diffs against.
        for item in items:
            if item.get("streaming"):
                item["streaming"] = False
            if item.get("status") in ("awaiting", "running"):
                item["status"] = "rejected"
        session["turn_outcome"] = turn.outcome
        session["busy"] = False
        session["turn"] = None
        # Notes that arrived after the last fold: sent as the next message, or, after a stop, put
        # back in the input box for the user to decide.
        late = [(text, item) for text, item in turn.notes]
        turn.notes.clear()
        for _, item in late:
            if item in items:
                items.remove(item)
        try:
            conversations.save(session)
        except OSError as ex:
            items.append(state.item_error(f"Could not save this conversation: {ex}"))
        if late:
            joined = "\n".join(text for text, _ in late)
            (_restore_input_on_main if turn.cancel.is_set() else _resend_on_main)(joined)
        _redraw()


def send(text: str) -> bool:
    session = state.session()
    text = text.strip()
    attachments = list(session["attachments"])
    if session["busy"]:
        # Mid-turn: the note is queued and folded in at the agent's next step; see _fold_notes.
        turn = session.get("turn")
        if not text or turn is None:
            return False
        item = state.item_user(text)
        item["queued"] = True
        session["items"].append(item)
        turn.notes.append((text, item))
        return True
    if not (text or attachments):
        return False
    turn = Turn()
    try:
        turn.config = config.load()
    except config.ConfigError as ex:
        session["items"].append(state.item_error(str(ex)))
        return False
    turn.roots = _project_roots()
    user_item = state.item_user(text, [a["name"] for a in attachments])
    checkpoints.begin_turn(session, user_item)
    session["items"].append(user_item)
    # The model also gets what the user is looking at, and what changed since it last acted; the
    # chat shows only what they typed. The snapshot taken here is what changed_only measures from.
    content = f"{_scene_context(text, session.get('scene_seen'))}\n\n{text or 'See the attached images.'}"
    session["scene_seen"] = session["scene_turn_start"] = _scene_snapshot()
    session["addons_line"] = _addons_line()
    if attachments:
        session["messages"].append({"role": "user", conversations.ATTACHED: True, "content": [
            {"type": "text", "text": content},
            *({"type": "image_url", "image_url": {"url": a["ref"]}} for a in attachments)]})
        known = {r["ref"] for r in session["references"]}
        session["references"] += [{"ref": a["ref"], "full": a.get("full", a["ref"]), "name": a["name"], "pinned": True}
                                  for a in attachments if a["ref"] not in known]
        session["attachments"] = []
    else:
        session["messages"].append({"role": "user", "content": content})
    conversations.save(session)
    _launch(session, turn)
    return True


def _launch(session: dict, turn: Turn) -> None:
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
    messages = session["messages"]
    if session["busy"] or not messages or messages[-1].get("role") == "assistant":
        return False
    turn = Turn()
    try:
        turn.config = config.load()
    except config.ConfigError as ex:
        session["items"].append(state.item_error(str(ex)))
        return False
    turn.roots = _project_roots()
    _launch(session, turn)
    return True


def decide(approved: bool, always: bool = False) -> bool:
    """Answer the pending approval card, if there is one. `always` stops asking in this conversation."""
    session = state.session()
    turn = session["turn"]
    if turn is None or not any(i.get("status") == "awaiting" for i in session["items"]):
        return False
    if approved and always:
        session["auto_run"] = True
    turn.approved = approved
    turn.decided.set()
    return True


def stop() -> None:
    turn = state.session()["turn"]
    if turn is not None:
        turn.cancel.set()
