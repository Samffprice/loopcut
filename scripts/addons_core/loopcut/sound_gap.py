"""The sound-gap finish check (cloud-tools-implicit-use section 5): Loopcut's own note when a turn ends on an
animation made for a trailer, a cutscene or a video that has no sound yet.

The model forgets what a finished piece of that kind usually has; Loopcut states the gap as one plain sentence
(LINE) the model can act on (the sound tool) or pass on to the user. Advisory, never a gate (owner, 2026-10-26):
it sends the turn on once per chat, and the model's next reply ends it whatever it does. Silent for good in a chat
where the user declined sound: said so in a message ("no music", "I have my own soundtrack"), answered no to a
question about sound, or skipped a sound tool's card, or where the model says the user doesn't want it.

It fires only when all of these hold: the router's purpose names a trailer, an animation, a cutscene or a video, or
its calls_for has sound (router.py, the hosted orchestrator's route; this turn's, else the chat's last purpose
line); a sound tool can be called or unlocked here (not on the user's own key without Loopcut's cloud tools); the
scene's frame range is longer than one frame; something animates; the sequencer has no sound strip. No price in
the sentence: the gateway's credits line says the prices (0.2.20+). Fast models don't act on what a tool result
tells them, so the sentence goes to the model as its own message, in plain words that read as they are.
"""

import re

from . import context, router

LINE = "[Loopcut] This animation has no sound yet; an ambience track would finish it."
PURPOSES = re.compile(r"\b(?:trailers?|animations?|animated|cut-?scenes?|videos?)\b", re.I)
SOUND_TOOLS = ("sfx",)  # The cloud tools that add an ambience or sound effects (Loopcut's catalog names).
_SOUND = r"(?:sounds?|audio|music|soundtrack|sfx|ambien(?:ce|t)|sound effects?)"
# The user turning sound down in their own words, in any message of the chat.
USER_NO = re.compile(rf"\b(?:no|without|skip|drop|mute|don'?t (?:add|want|need|use)|do not (?:add|want|need|use))\b"
                     rf"[^.,;?!\n]{{0,24}}?\b{_SOUND}\b|\b(?:my|our) own {_SOUND}\b", re.I)
# The model saying the user doesn't want it (not "this has no sound yet", which is the gap itself).
MODEL_NO = re.compile(rf"\b(?:you|the user)\s+(?:don'?t|do not|didn'?t|did not|doesn'?t|does not)\s+(?:want|need)\b"
                      rf"[^.,;?!\n]{{0,24}}?\b{_SOUND}\b", re.I)
# A no to a question about sound that the reply before it asked.
REFUSAL = re.compile(r"^\W*(?:no|nope|nah|not now|skip|don'?t|do not|leave it)\b", re.I)
SOUND_WORDS = re.compile(rf"\b{_SOUND}\b", re.I)
_PURPOSE_LINE = re.compile(r"<routing>.*?\nPurpose: ([^\n<]*)", re.S)


def said(session: dict) -> bool:
    """LINE was sent in this chat: once per chat (it is stored with the messages, so a reopened chat knows)."""
    return any(m.get("role") == "user" and router._text(m).startswith(LINE) for m in session.get("messages") or ())


def declined(session: dict) -> bool:
    """The user turned sound down in this chat (module docstring)."""
    reply = ""
    for message in session.get("messages") or ():
        role, text = message.get("role"), router._text(message)
        if role == "assistant":
            if MODEL_NO.search(text):
                return True
            if text.strip() and not message.get("tool_calls"):
                reply = text
        elif role == "user" and not (message.get(context.STEP_LIMIT) or context.is_capture(message)
                                     or context.is_record(message)):
            request = router.user_request(message)
            if USER_NO.search(request):
                return True
            if reply and router.asked(reply) and SOUND_WORDS.search(router._last_line(reply)) and REFUSAL.search(request):
                return True
            reply = ""
    from . import cloud
    return any(item.get("kind") == "tool" and item.get("status") == "rejected"
               and (cloud.tool_name(str(item.get("name") or "")) or item.get("name")) in SOUND_TOOLS
               for item in session.get("items") or ())


def wanted(session: dict, turn) -> bool:
    """The work is for something that usually has sound: this turn's routing decision, else the chat's last purpose
    line (a later turn's "add a camera move" has no purpose of its own)."""
    decision = getattr(turn, "routed", None)
    if decision is not None and (decision.purpose or decision.calls_for):
        return "sound" in decision.calls_for or bool(PURPOSES.search(decision.purpose))
    for message in reversed(session.get("messages") or ()):
        if context.is_turn_start(message):
            found = _PURPOSE_LINE.search(router._text(message))
            if found:
                line = found.group(1)
                return bool(PURPOSES.search(line.split(".", 1)[0]) or router.CALLS_FOR["sound"] in line)
    return False


def _sound_tool() -> bool:
    """A sound tool can be called or unlocked here (cloud._offered): never on a route without Loopcut's cloud tools."""
    from . import cloud
    return any(name in SOUND_TOOLS for name in cloud._offered(cloud._runtime, SOUND_TOOLS))


def scene_gap() -> bool:
    """Main thread: an animation without sound: a frame range longer than one frame, something animates
    (motion_claims._animated: keys, drivers, a simulation, timed Geometry Nodes) and no sound strip in the sequencer."""
    import bpy
    from . import motion_claims
    scene = bpy.context.scene
    if scene.frame_end <= scene.frame_start:
        return False
    editor = scene.sequence_editor
    if editor is not None:
        every = getattr(editor, "strips_all", None)  # With the strips inside meta strips.
        if any(strip.type == "SOUND" for strip in (every if every is not None else editor.sequences_all)):
            return False
    return any(motion_claims._animated(ob) for ob in scene.objects)


def finish_check(session: dict, turn, completion, role: str, run_on_main) -> str | None:
    """agent._final_reply's sound check: LINE to send the model back with, or None. Not for a worker, after a Stop or
    a limit, on a reply that asks the user something, after a call the user declined this turn or with notes still
    to read. run_on_main(fn) runs fn on the main thread and returns its result. Never raises."""
    if (role == "worker" or turn.cancel.is_set() or turn.outcome != "running" or getattr(turn, "declined", False)
            or getattr(turn, "notes", None) or completion is None or router.asked(completion.text or "")):
        return None
    try:
        if said(session) or not wanted(session, turn) or declined(session) or not _sound_tool():
            return None
        if not run_on_main(scene_gap) or turn.cancel.is_set():
            return None
    except Exception as ex:  # Advisory: a check that cannot run says nothing.
        print(f"Loopcut: no sound check: {ex!r}")
        return None
    print("Loopcut: the animation has no sound; said once.")
    return LINE
