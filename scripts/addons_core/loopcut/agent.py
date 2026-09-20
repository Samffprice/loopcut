"""The agent loop: stream a reply, run the tools it asks for, feed results back, repeat."""

import json
import threading

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
- To see the result of a change, pass capture="three_quarter" (or "camera") to the run_python step that \
makes it, or call capture_viewport. At most 3 captures per message. Never move the user's viewport or \
shading. Fix what is actually wrong, then finish.
- Never delete or overwrite the user's objects, materials or files unless asked. Name what you create \
sensibly; real-world scale in meters unless told otherwise.
- Keep replies short: what you did and anything the user must decide. No code dumps.
- The code of all but your last two steps and older tool results are cut to a line, and a \
<conversation_summary> may stand for earlier messages. The scene is the source of truth: look again \
rather than trust memory, and write each step so it stands on its own."""

_POLL_SECONDS = 0.1
MAX_CAPTURES_PER_TURN = 3  # Each capture is resent with every later step of the turn; see context.py.


class Turn:
    def __init__(self):
        self.cancel = threading.Event()
        self.decided = threading.Event()
        self.approved = False
        self.thread: threading.Thread | None = None
        self.captures = 0
        # scene_diff snapshots from before the first and after the last scene-changing step.
        self.config: config.Config | None = None  # Loaded on the main thread by send().
        self.scene_before: dict | None = None
        self.scene_after: dict | None = None


def _redraw() -> None:
    from . import mainthread
    mainthread.request_redraw()


def _run_tool_on_main(name: str, arguments: str):
    from . import mainthread, tools
    return mainthread.run_on_main(lambda: tools.execute(name, arguments)).result()


def _needs_approval(name: str) -> bool:
    from . import tools
    return name in tools.NEEDS_APPROVAL


def _changes_scene(name: str) -> bool:
    from . import tools
    return name in tools.CHANGES_SCENE


def _ensure_checkpoint_on_main(session: dict) -> None:
    from . import mainthread
    mainthread.run_on_main(lambda: checkpoints.ensure_for_turn(session)).result()


def _scene_context(text: str) -> str:
    from . import scene_context
    return scene_context.for_message(text)


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
    from . import tools
    return tools.SCHEMAS


def _describe(call: llm.ToolCall) -> tuple[str, str]:
    try:
        arguments = json.loads(call.arguments) if call.arguments.strip() else {}
    except ValueError:
        return call.name, call.arguments
    if not isinstance(arguments, dict):
        return call.name, call.arguments
    return str(arguments.get("summary") or call.name), str(arguments.get("code") or "")


def _wants_capture(arguments: str) -> bool:
    try:
        parsed = json.loads(arguments) if arguments.strip() else {}
    except ValueError:
        return False
    return isinstance(parsed, dict) and bool(parsed.get("capture"))


def _without_capture(arguments: str) -> str:
    parsed = json.loads(arguments)
    parsed.pop("capture", None)
    return json.dumps(parsed)


def _wait_for_approval(turn: Turn) -> bool:
    turn.decided.clear()
    while not turn.decided.wait(_POLL_SECONDS):
        if turn.cancel.is_set():
            raise llm.Cancelled()
    return turn.approved


def _image_message(reference: str) -> dict:
    # A reference to a file in the conversation's folder; expanded when a request is sent.
    return {"role": "user", context.CAPTURE: True, "content": [
        {"type": "text", "text": context.CAPTURE_TEXT},
        {"type": "image_url", "image_url": {"url": reference}},
    ]}


def _run(session: dict, turn: Turn, run_tool=_run_tool_on_main,
         ensure_checkpoint=_ensure_checkpoint_on_main) -> None:
    items, messages = session["items"], session["messages"]
    try:
        cfg = turn.config or config.load()
        for _ in range(cfg.max_steps):
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
                messages=[{"role": "system", "content": SYSTEM_PROMPT},
                          *conversations.wire_messages(session["id"], prepared)],
                tools=_tool_schemas(), reasoning_effort=cfg.reasoning_effort,
                on_text=on_text, is_cancelled=turn.cancel.is_set,
            )
            reply["streaming"] = False
            if completion.usage:
                for key in ("input", "output"):
                    session["usage"][key] += completion.usage[key]
                session["usage"]["context"] = completion.usage["input"]
                context.calibrate(session, estimated, completion.usage["input"])
            if not reply["text"].strip():
                items.remove(reply)
            messages.append(completion.as_message())
            if not completion.tool_calls:
                return

            images = []
            for call in completion.tool_calls:
                summary, code = _describe(call)
                card = state.item_tool(call.name, summary, code)
                items.append(card)
                if _needs_approval(call.name) and not (cfg.auto_run or session["auto_run"]):
                    card["status"] = "awaiting"
                    _redraw()
                    if not _wait_for_approval(turn):
                        card["status"] = "rejected"
                        messages.append({"role": "tool", "tool_call_id": call.id,
                                         "content": "The user rejected this action. Ask what they want instead."})
                        continue
                card["status"] = "running"
                _redraw()
                arguments, note = call.arguments, ""
                if call.name == "capture_viewport" or (call.name == "run_python" and _wants_capture(arguments)):
                    turn.captures += 1
                    if turn.captures > MAX_CAPTURES_PER_TURN:
                        refusal = (f"you have already looked {MAX_CAPTURES_PER_TURN} times this turn. Finish "
                                   f"with what you know, and tell the user anything you could not verify.")
                        if call.name == "capture_viewport":
                            card["status"] = "failed"
                            card["output"] = f"Not captured: already looked {MAX_CAPTURES_PER_TURN} times this turn."
                            messages.append({"role": "tool", "tool_call_id": call.id,
                                             "content": f"Not captured: {refusal}"})
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
                result = run_tool(call.name, arguments)
                if note:
                    result.text += note
                card["status"] = "done" if result.ok else "failed"
                card["output"] = result.text
                messages.append({"role": "tool", "tool_call_id": call.id, "content": result.text})
                if result.image_path:
                    # Stored now: every capture is written to the same file, so a second capture in
                    # this batch would otherwise replace the first before it is read.
                    images.append(conversations.store_image(session, result.image_path))
                if getattr(result, "scene_before", None) is not None:
                    turn.scene_before = turn.scene_before or result.scene_before
                    turn.scene_after = result.scene_after
            # Tool messages must directly follow the assistant message, so images go after them all.
            messages.extend(_image_message(reference) for reference in images)
            conversations.save(session)  # The history is consistent here: every call has its result.
            _redraw()
        items.append(state.item_error(f"Stopped after {cfg.max_steps} steps without finishing."))
    except llm.Cancelled:
        items.append(state.item_error("Stopped."))
    except (config.ConfigError, llm.LLMError) as ex:
        items.append(state.item_error(str(ex)))
    except Exception as ex:  # Last line of defence for the worker thread: show it, never swallow it.
        import traceback
        traceback.print_exc()
        items.append(state.item_error(f"Internal error: {ex!r}"))
    finally:
        conversations.close_open_tool_calls(messages, "Cancelled before this ran.")
        changed = _changes_item(session, turn)
        if changed:
            items.append(changed)
        for item in items:
            if item.get("streaming"):
                item["streaming"] = False
            if item.get("status") in ("awaiting", "running"):
                item["status"] = "rejected"
        session["busy"] = False
        session["turn"] = None
        try:
            conversations.save(session)
        except OSError as ex:
            items.append(state.item_error(f"Could not save this conversation: {ex}"))
        _redraw()


def send(text: str) -> bool:
    session = state.session()
    text = text.strip()
    attachments = list(session["attachments"])
    if not (text or attachments) or session["busy"]:
        return False
    turn = Turn()
    try:
        turn.config = config.load()
    except config.ConfigError as ex:
        session["items"].append(state.item_error(str(ex)))
        return False
    user_item = state.item_user(text, [a["name"] for a in attachments])
    checkpoints.begin_turn(session, user_item)
    session["items"].append(user_item)
    # The model also gets what the user is looking at; the chat shows only what they typed.
    content = f"{_scene_context(text)}\n\n{text or 'See the attached images.'}"
    if attachments:
        session["messages"].append({"role": "user", conversations.ATTACHED: True, "content": [
            {"type": "text", "text": content},
            *({"type": "image_url", "image_url": {"url": a["ref"]}} for a in attachments)]})
        session["attachments"] = []
    else:
        session["messages"].append({"role": "user", "content": content})
    session["scroll"], session["confirm_restore"] = 0.0, None
    conversations.save(session)
    session["busy"], session["turn"] = True, turn
    turn.thread = threading.Thread(target=_run, args=(session, turn), name="loopcut-turn", daemon=True)
    turn.thread.start()
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
