"""Recognize imitations of Loopcut records, never ordinary claims about work; and a reply that
announces its next step but stops before taking it. Both are sent back from one budget a turn
(nudges_left), and neither when the reply says plainly that it stops or that nothing ran."""

import re


CORRECTION = "No tool ran this turn; nothing was created. Use the tools, or say plainly that nothing was done."
NOTICE = "This reply describes work, but no step ran, so nothing in your scene changed."
# Sent back after a reply that announced a step and called no tool (announces_step); never shown in the panel.
CONTINUE = "[Loopcut] Go ahead: do that now with your tools, or say plainly that you are stopping and why."
# Per user turn, and never two without a tool run in between. The turn's one record correction (CORRECTED:
# CORRECTION, or agent's copied-records retry) spends from the same budget (nudges_left).
MAX_CONTINUES = 2
# Private scalar markers on the original user message: saved, restored and never sent to the model.
EXECUTED = "loopcut_executed_tools"
CORRECTED = "loopcut_unrun_corrected"
CONTINUED = "loopcut_step_continues"        # How many CONTINUE nudges this user turn has had.
CONTINUED_AT = "loopcut_step_continued_at"  # EXECUTED when the last one was sent.

_ACTIONS = (r"create|make|build|add|model|move|rotate|scale|resize|delete|remove|duplicate|rename|"
            r"parent|bevel|extrude|assign|apply|change|edit|fix|repair|replace|position|align|"
            r"place|set|color|colour|texture|sculpt|animate|light")
_REQUEST = re.compile(
    r"^(?:(?:please|now|then|also)\s+)*(?:(?:can|could|would|will)\s+you\s+|"
    r"i\s+(?:want|need|would like)\s+you\s+to\s+)?(?:please\s+)?(?:" + _ACTIONS + r")\b", re.I)
_INFORMATION = re.compile(r"^(?:how|what|why|when|where|explain|describe|tell me|plan|write)\b", re.I)
_DELIVERABLE = re.compile(r"\s+(?:[\w-]+\s+){0,3}(?:plan|script|code|example|tutorial|guide|list)\b", re.I)
_UI_RECORD = re.compile(
    r"^(?:Verified by Loopcut on this scene:\s*\d+ of \d+ checks\.?|"
    r"Scene changes(?:\s*[:(].*)?|"
    r"\d+ (?:added|removed|changed)(?:,\s*\d+ (?:added|removed|changed))*\.?|"
    r"\d+ added\s*/\s*(?:\d+\s+)?changed(?:\s*/\s*\d+ removed)?\.?)$", re.I)
_ILLUSTRATION = re.compile(
    r"\b(?:example|sample|previous (?:step|turn)|earlier (?:step|turn|output|result|record))\b|"
    r"^plan\b|^(?:I'll|I will)\b.*:$|\b(?:here|following)\b.*\b(?:code|script)\b", re.I)
_RESULT = r"(?:ok|done|success(?:ful)?|failed|printed|output|result)\b"
# _ILLUSTRATION's explicit part, plurals included. Not in _ILLUSTRATION itself: RECORD_HEAD begins "Earlier steps.",
# and the 0.2.13 correction must still see the forged records after a copied head (rel015-app).
_EXAMPLE_INTRO = re.compile(r"\b(?:example|sample|previous (?:steps?|turns?)|"
                            r"earlier (?:steps?|turns?|outputs?|results?|records?))\b", re.I)


def scene_request(message: dict) -> bool | None:
    """Resolve informational intent before action clauses, ignoring automatic scene context.

    None leaves an implicit request ("a prop") to the router, if available.
    """
    content = message.get("content", "")
    if isinstance(content, list):
        content = "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    if not isinstance(content, str):
        return False
    text = content.split("</scene_context>", 1)[-1].split("<routing>", 1)[0].strip()
    if _INFORMATION.match(text):
        return False
    parts = [part.strip() for part in re.split(r"[\n.!?]+", text)]
    requests = [(part, _REQUEST.match(part)) for part in parts]
    if any(match and _DELIVERABLE.match(part[match.end():]) for part, match in requests):
        return False
    if any(match for _, match in requests):
        return True
    return False if any(_INFORMATION.match(part) for part in parts) else None


def _plain(line: str) -> str:
    return re.sub(r"^(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+)", "", line.strip()).replace("**", "").replace("`", "")


def introduces_example(line: str) -> bool:
    """A line that introduces an example or an account of earlier steps ("Example:", "In the previous steps:"),
    after which context.copied_records leaves record-shaped text alone. Narrower than imitates_records' reading
    (_ILLUSTRATION): an "I'll ...:" or a question before forged records does not make them an example."""
    return bool(_EXAMPLE_INTRO.search(_plain(line.replace("’", "'"))))


def imitates_records(text: str, tool_names) -> bool:
    """Only record-shaped text using this session's tools or the runtime's UI labels.

    Prose, quoted records and ordinary code fences are not execution records. Explicit examples,
    plans and historical explanations also leave their following block alone.
    """
    names = "|".join(re.escape(name) for name in tool_names) or r"(?!)"
    tool = re.compile(r"^(?:" + names + r")(?:\s*\(.*\)(?:\s*:\s*" + _RESULT + r".*)?|"
                      r"\s*:\s*(?:" + _RESULT + r"|\{).*|"
                      r'\s+["\u201c].+?["\u201d]\s*:\s*' + _RESULT + r".*|\s+" + _RESULT + r"[.!]?)$", re.I)
    label = re.compile(r"^(?:" + names + r")\s*:?$", re.I)
    json_name = re.compile(r'^\{?\s*"(?:name|tool)"\s*:\s*"(?:' + names + r')"\s*[,}]', re.I)
    payload = re.compile(r'"(?:arguments|parameters|code|status|output|result)"\s*:', re.I)
    following = re.compile(r"^(?:```|~~~|" + _RESULT + r"|import\s|from\s|bpy\.|\{)", re.I)
    lines = text.replace("’", "'").splitlines()
    fence, example, example_body = "", False, False
    code_fence = False
    for index, raw in enumerate(lines):
        line = raw.strip()
        if line.startswith(">"):
            continue
        if line.startswith(("```", "~~~")):
            if fence:
                if line.startswith(fence):
                    fence, code_fence = "", False
                    example_body = True
            else:
                fence = line[:3]
                language = line[3:].strip()
                if not example and label.fullmatch(language):
                    return True
                code_fence = language.lower() not in ("", "json", "text", "plaintext")
            continue
        if not line:
            if example_body and not fence:
                example, example_body = False, False
            continue
        line = _plain(line)
        if not fence and (_ILLUSTRATION.search(line) or line.endswith("?")):
            example, example_body = True, False
            continue
        if example:
            example_body = True
            continue
        if code_fence:
            continue
        if _UI_RECORD.fullmatch(line) or tool.fullmatch(line):
            return True
        if label.fullmatch(line):
            next_line = next((s for s in lines[index + 1:] if s.strip()), "")
            if following.match(next_line.strip()) or next_line.startswith(("    ", "\t")):
                return True
        if json_name.match(line) and payload.search("\n".join([line, *lines[index + 1:index + 4]])):
            return True
    return False


# --- Announced steps -------------------------------------------------------------------------------------
# "I will next import ...", "I'll now search ...", "Let's try ...": first person, about to act, with a verb
# for something the tools do. English only, and narrow on purpose: a missed nudge costs the user one
# "go on"; a false one makes Loopcut talk over a question it asked.
_STEP_VERBS = (r"import|search|look|browse|find|fetch|download|grab|bring|load|add|create|make|build|model|place|"
               r"put|position|move|rotate|scale|resize|arrange|align|set|adjust|apply|assign|tweak|refine|fix|"
               r"repair|replace|swap|remove|delete|duplicate|generate|render|capture|check|inspect|verify|test|try|"
               r"retry|attempt|start|begin|continue|proceed|work|focus|populate|furnish|decorate|light|texture|"
               r"sculpt|animate|rig|run|organi[sz]e|parent|join|combine|assemble|scatter|configure|update|change|"
               r"edit|improve|conduct|perform|measure")
_LEAD = (r"(?:(?:now|next|then|first|also|just|quickly|immediately|again|instead|finally|go\s+ahead\s+and|"
         r"go\s+on\s+to|move\s+on\s+to|proceed\s+to|need\s+to|have\s+to|try\s+to|start\s+by|begin\s+by|"
         r"continue\s+by)[\s,]+){0,3}")
_INTENT = re.compile(r"\b(?:i\s+will|i'll|i\s+shall|i(?:'m|\s+am)\s+(?:now\s+)?(?:going|about)\s+to|let\s+me|"
                     r"let's|let\s+us)\s+" + _LEAD + r"(?:" + _STEP_VERBS + r")\b", re.I)
# The step waits on the user, a condition or a later turn: not something to do now. Any "you" in the
# sentence ("the one you pick"), any condition ("If this looks good, I'll..."), and a step that follows
# something else ("Say the word and I'll...", "Sign in..., then I'll...") all count as waiting.
_DEFERRED = re.compile(
    r"\byou(?:r|'re|'d)?\b|\b(?:if|once|assuming|provided|whichever)\b|\b(?:and|then),?\s+(?:i'll|i\s+will)\b|"
    r"\b(?:later|next\s+time|another\s+time|tomorrow)\b|"
    r"\bin\s+(?:the|a)\s+(?:next|future|later|follow-?up)\s+(?:turn|message|reply|response|session|conversation|request)\b",
    re.I)
# Anywhere in the reply: a question or choice for the user, a hand-off, or a report that the work is done.
_HANDOFF = re.compile(
    r"\?|"
    r"\b(?:let\s+me\s+know|feel\s+free|would\s+you\s+(?:like|prefer|rather)|do\s+you\s+want|want\s+me\s+to|"
    r"shall\s+i|should\s+i|your\s+call|up\s+to\s+you|"
    r"if\s+you(?:'d|\s+would)?\s+(?:like|want|prefer|rather)|"
    r"you\s+(?:can|could|might|may)\s+(?:now|also|then|want|wish|choose|pick|tell|let)|"
    r"your\s+(?:approval|confirmation|go-?ahead|ok|okay|input|feedback|preference|choice)|"
    r"i(?:'m|\s+am)\s+(?:stopping|done|finished)|i(?:'ll|\s+will)\s+(?:stop|pause|wait|end\s+here|leave\s+it)|"
    r"stopping\s+here|(?:that|this)(?:'s|\s+is)\s+(?:all|it)|all\s+(?:done|set)|"
    r"(?:is|are)\s+(?:now\s+)?(?:done|complete|completed|finished|ready))\b|"
    r"(?:^|[.!:]\s+)(?:please\s+)?(?:done|finished|choose|pick|select|confirm|tell\s+me|reply)\b",
    re.I | re.M)
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def nudges_left(request: dict) -> int:
    """What is left of the user turn's MAX_CONTINUES: the announced-step nudges sent (CONTINUED) and the one
    record correction (CORRECTED), counted on the user's message so a resumed turn keeps the count."""
    return MAX_CONTINUES - request.get(CONTINUED, 0) - bool(request.get(CORRECTED))


# The whole reply stopping, or nothing having run: "I'm stopping here.", "I'll stop now, since...", "Nothing ran.",
# "No tools were run this turn." Each ends there, at a clause: "I'll stop the animation at frame 120" and "nothing
# ran into the chest" are work, and "nothing was changed on the legs" is part of a claim. This exit turns the
# corrections off, so a false yes leaves a false account standing.
_WHEN = r"(?:\s+(?:here|now|there|yet|at\s+all|for\s+now|this\s+turn|in\s+this\s+(?:reply|turn)))?"
_END = r"(?=\s*(?:[.!;:,\u2014\u2013-]|$)|\s+(?:because|since|as|until|and|but|so)\b)"
_STOPPING = re.compile(r"\b(?:i(?:'m|\s+am)\s+stopping|i(?:'ll|\s+will)\s+stop|stopping\s+here|"
                       r"nothing\s+(?:ran|has\s+run|was\s+run)|no\s+(?:tools?|steps?|calls?)\s+(?:ran|were\s+run))"
                       + _WHEN + _END, re.I | re.M)


def stops_honestly(text) -> bool:
    """A reply that says plainly it is stopping or that nothing ran: the answer every nudge offers, so
    agent.run_loop sends it back for neither an announced step nor a record correction."""
    return isinstance(text, str) and bool(_STOPPING.search(text.replace("’", "'")))


def announces_step(text: str) -> bool:
    """Whether a reply with no tool calls ends by announcing work it is about to do now ("I will next import
    the desk.", "Let's try a broader search."), so the turn should go on instead of ending.

    Only the last two sentences of the last paragraph count, and only a first-person step with a tool-shaped
    verb, in a sentence with no "you", no condition ("if", "once"...), no "and/then I'll" after something the
    user does, and no later turn; nor one that introduces something (ends in ":").
    Never when the reply asks anything ("?" anywhere), offers a choice, hands off ("let me know if..."),
    or says the work is done or stopping; nor when it ends on a list (a plan laid out, not a step begun).
    """
    if not isinstance(text, str):
        return False
    prose = re.sub(r"(?:```|~~~).*?(?:```|~~~|\Z)", "\n\n", text.replace("’", "'"), flags=re.S)
    if _HANDOFF.search(prose):
        return False
    paragraphs = [p for p in re.split(r"\n\s*\n", prose) if p.strip()]
    if not paragraphs:
        return False
    lines = [line for line in paragraphs[-1].splitlines() if line.strip()]
    if _LIST_ITEM.match(lines[-1]):
        return False
    sentences = [s.strip() for line in lines for s in re.split(r"(?<=[.!])\s+", _plain(line)) if s.strip()]
    return any(_INTENT.search(s) and not _DEFERRED.search(s) and not s.rstrip(" .!*_").endswith(":")
               for s in sentences[-2:])
