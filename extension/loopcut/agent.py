"""The agent loop: stream a reply, run the tools it asks for, feed results back, repeat."""

import json
import threading

from . import checkpoints, config, conversations, llm, state

SYSTEM_PROMPT = """You are Loopcut, an AI agent working inside the user's running Blender session.

You act through tools. run_python executes bpy code in the live session. get_scene_info reports what \
is in the scene and get_object_info how an object is set up (modifiers, material and geometry node \
trees, animation). inspect_api is the Python API of this exact Blender version. capture_viewport \
shows you the scene.

Each user message starts with a <scene_context> block: the open file, mode, and what the user has \
selected. "This", "it" and "these" mean the selection. @Name in a message refers to the object, \
material or collection with that name, described in the same block.

How to work:
- Look before you edit: call get_scene_info unless the context block already tells you enough. Before \
changing an existing material, modifier or rig, read it with get_object_info and change what is \
there; do not rebuild the user's work from scratch.
- Make changes in small run_python steps. Its result ends with "Scene changes", measured from the \
scene: that is what your code really did. Do not write extra code just to print and verify values.
- Your memory of bpy is from older Blender versions. When you are not sure of a name, and always \
after an AttributeError, TypeError or "enum not found", look it up with inspect_api before trying again.
- After changes that affect how things look, capture_viewport once and check the result. It frames \
the objects for you; do not move the user's viewport, change shading, or re-capture just for a nicer \
angle. Use angle "camera" to check what the camera sees. Fix what is actually wrong, then finish.
- Never delete or overwrite the user's existing objects, materials or files unless they asked.
- Name the things you create sensibly. Use real-world scale in meters unless told otherwise.
- Keep messages to the user short: what you did and anything they need to decide. No code dumps; \
they can expand each step to see the code."""

_POLL_SECONDS = 0.1


class Turn:
    def __init__(self):
        self.cancel = threading.Event()
        self.decided = threading.Event()
        self.approved = False
        self.thread: threading.Thread | None = None
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


def _wait_for_approval(turn: Turn) -> bool:
    turn.decided.clear()
    while not turn.decided.wait(_POLL_SECONDS):
        if turn.cancel.is_set():
            raise llm.Cancelled()
    return turn.approved


def _image_message(reference: str) -> dict:
    # A reference to a file in the conversation's folder; expanded when a request is sent.
    return {"role": "user", "content": [
        {"type": "text", "text": "Viewport capture from capture_viewport:"},
        {"type": "image_url", "image_url": {"url": reference}},
    ]}


def _run(session: dict, turn: Turn, run_tool=_run_tool_on_main,
         ensure_checkpoint=_ensure_checkpoint_on_main) -> None:
    items, messages = session["items"], session["messages"]
    try:
        cfg = turn.config or config.load()
        for _ in range(cfg.max_steps):
            reply = state.item_assistant()
            items.append(reply)

            def on_text(delta: str, reply=reply) -> None:
                reply["text"] += delta
                _redraw()

            completion = llm.stream_chat(
                base_url=cfg.base_url, api_key=cfg.api_key, model=cfg.model,
                messages=[{"role": "system", "content": SYSTEM_PROMPT},
                          *conversations.wire_messages(session["id"], messages)],
                tools=_tool_schemas(), reasoning_effort=cfg.reasoning_effort,
                on_text=on_text, is_cancelled=turn.cancel.is_set,
            )
            reply["streaming"] = False
            if completion.usage:
                for key in ("input", "output"):
                    session["usage"][key] += completion.usage[key]
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
                result = run_tool(call.name, call.arguments)
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
