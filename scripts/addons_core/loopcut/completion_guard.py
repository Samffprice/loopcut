"""Recognize imitations of Loopcut records, never ordinary claims about work."""

import re


CORRECTION = "No tool ran this turn; nothing was created. Use the tools, or say plainly that nothing was done."
NOTICE = "This reply describes work, but no step ran, so nothing in your scene changed."
# Private scalar markers on the original user message: saved, restored and never sent to the model.
EXECUTED = "loopcut_executed_tools"
CORRECTED = "loopcut_unrun_corrected"

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
