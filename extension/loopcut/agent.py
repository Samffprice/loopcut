"""The agent loop: stream a reply, run the tools it asks for, feed results back, repeat."""

import base64
import json
import threading

from . import checkpoints, config, llm, state

SYSTEM_PROMPT = """You are Loopcut, an AI agent working inside the user's running Blender session.

You act through tools. run_python executes bpy code in the live session, get_scene_info reports \
what is in the scene, and capture_viewport shows you the 3D viewport.

How to work:
- Look before you edit: call get_scene_info unless you already know the scene's current state.
- Make changes in small run_python steps. If a step raises, read the traceback and fix the cause.
- After changes that affect how things look, capture_viewport once and check the result. It frames \
the objects for you; do not move the user's viewport, change shading, or re-capture just for a nicer \
angle. Fix what is actually wrong with the scene, then finish.
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


def _image_message(path) -> dict:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"role": "user", "content": [
        {"type": "text", "text": "Viewport capture from capture_viewport:"},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
    ]}


def _run(session: dict, turn: Turn, run_tool=_run_tool_on_main,
         ensure_checkpoint=_ensure_checkpoint_on_main) -> None:
    items, messages = session["items"], session["messages"]
    try:
        cfg = config.load()
        for _ in range(cfg.max_steps):
            reply = state.item_assistant()
            items.append(reply)

            def on_text(delta: str, reply=reply) -> None:
                reply["text"] += delta
                _redraw()

            completion = llm.stream_chat(
                base_url=cfg.base_url, api_key=cfg.api_key, model=cfg.model,
                messages=[{"role": "system", "content": SYSTEM_PROMPT}, *messages],
                tools=_tool_schemas(), reasoning_effort=cfg.reasoning_effort,
                on_text=on_text, is_cancelled=turn.cancel.is_set,
            )
            reply["streaming"] = False
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
                if _needs_approval(call.name) and not cfg.auto_run:
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
                    images.append(result.image_path)
            # Tool messages must directly follow the assistant message, so images go after them all.
            messages.extend(_image_message(path) for path in images)
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
        for item in items:
            if item.get("streaming"):
                item["streaming"] = False
            if item.get("status") in ("awaiting", "running"):
                item["status"] = "rejected"
        session["busy"] = False
        session["turn"] = None
        _redraw()


def send(text: str) -> bool:
    session = state.session()
    text = text.strip()
    if not text or session["busy"]:
        return False
    user_item = state.item_user(text)
    checkpoints.begin_turn(session, user_item)
    session["items"].append(user_item)
    session["messages"].append({"role": "user", "content": text})
    session["scroll"], session["confirm_restore"] = 0.0, None
    turn = Turn()
    session["busy"], session["turn"] = True, turn
    turn.thread = threading.Thread(target=_run, args=(session, turn), name="loopcut-turn", daemon=True)
    turn.thread.start()
    return True


def decide(approved: bool) -> bool:
    """Answer the pending approval card, if there is one."""
    session = state.session()
    turn = session["turn"]
    if turn is None or not any(i.get("status") == "awaiting" for i in session["items"]):
        return False
    turn.approved = approved
    turn.decided.set()
    return True


def stop() -> None:
    turn = state.session()["turn"]
    if turn is not None:
        turn.cancel.set()
