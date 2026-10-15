"""Display-only Markdown and file references. The original reply is never rewritten."""

import re
from pathlib import Path
from urllib.parse import unquote

_FENCE_START = re.compile(r" {0,3}(`{3,}|~{3,})([^\r\n]*)\r?\n?$")
LINK = re.compile(r'''!?\[(?P<label>[^\]\n]*)\]\([ \t]*(?P<target><[^<>\n]+>|(?:[^()<>\s"']|\([^()<>\n]*\))+)(?:[ \t]+(?:"[^"\n]*"|'[^'\n]*'))?[ \t]*\)''')
_INLINE = re.compile(
    r"(?P<code>(?<!`)(?P<ticks>`+)(?!`)(?P<body>.*?)(?<!`)(?P=ticks)(?!`))|"
    + LINK.pattern + r"|(?P<literal>!?\[[^\n]*|`+[^\n]*)", re.DOTALL)
_EMPHASIS = re.compile(r"(?<![\w*\\])\*\*(?![\s*])([^*\n]+?)(?<!\s)\*\*(?![\w*])|"
                       r"(?<![\w\\])__(?![\s_])([^_\n]+?)(?<!\s)__(?!\w)")
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
_QUOTED = re.compile(r'`+([^`\n]+)`+|"([^"\n]+)"|\'([^\'\n]+)\'')
_PATH = re.compile(r"(?<![\w/])(?:[a-zA-Z]:[\\/]|~?/|\.{1,2}/|[\w.-]+/|[\w-]+\.[a-zA-Z0-9]{1,10}\b)[^\s<>\[\]`\"|]*")
REFERENCE_LIMIT = 6


def link_target(match) -> str:
    target = match.group("target")
    return target[1:-1] if target.startswith("<") and target.endswith(">") else target


def link_label(match) -> str:
    target = link_target(match)
    name = target.split("?", 1)[0].split("#", 1)[0]
    return match.group("label") or Path(unquote(name).rstrip("/")).name or target


def _blocks(text):
    """Yield (kind, source, body), keeping fence length/type and unfinished fences intact."""
    lines = text.splitlines(keepends=True)
    start = index = 0
    while index < len(lines):
        opening = _FENCE_START.fullmatch(lines[index])
        if not opening or (opening[1][0] == "`" and "`" in opening[2]):
            index += 1
            continue
        if start < index:
            prose = "".join(lines[start:index])
            yield "p", prose, prose
        marker = opening[1]
        closing = re.compile(r" {0,3}" + re.escape(marker[0]) + "{" + str(len(marker)) + r",}[ \t]*\r?\n?$")
        end = index + 1
        while end < len(lines) and not closing.fullmatch(lines[end]):
            end += 1
        body = "".join(lines[index + 1:end])
        if end < len(lines):
            body = body.removesuffix("\n").removesuffix("\r")
            end += 1
        yield "code", "".join(lines[index:end]), body
        start = index = end
    if start < len(lines):
        prose = "".join(lines[start:])
        yield "p", prose, prose


def _clean_inline(text):
    parts, last = [], 0
    for match in _INLINE.finditer(text):
        parts.append(_EMPHASIS.sub(lambda m: m[1] or m[2], text[last:match.start()]))
        if match.group("code") is not None:
            parts.append(match.group("body"))
        elif match.group("target") is not None:
            parts.append(_clean_inline(link_label(match)))
        else:
            parts.append(match[0])  # Incomplete or unsupported Markdown stays literal while streaming.
        last = match.end()
    parts.append(_EMPHASIS.sub(lambda m: m[1] or m[2], text[last:]))
    return "".join(parts)


def _pipe_cells(line):
    # Pipes in code, link destinations and escaped text are not cell boundaries.
    protected = {i for match in _INLINE.finditer(line) for i in range(match.start(), match.end())}
    pipes = [i for i, char in enumerate(line) if char == "|" and i not in protected
             and (i == 0 or line[i - 1] != "\\")]
    if not pipes:
        return None
    cells = [line[a + 1:b].strip() for a, b in zip([-1] + pipes, pipes + [len(line)])]
    if not cells[0]:
        cells.pop(0)
    if cells and not cells[-1]:
        cells.pop()
    return cells or None


def _clean_prose(text):
    lines, output, index = text.split("\n"), [], 0
    multiline = set()
    for match in _INLINE.finditer(text):
        if "\n" in match[0]:
            first = text.count("\n", 0, match.start())
            multiline.update(range(first, first + match[0].count("\n") + 1))
    while index < len(lines):
        header = _pipe_cells(lines[index]) if index not in multiline else None
        rule = _pipe_cells(lines[index + 1]) if index + 1 < len(lines) and index + 1 not in multiline else None
        if header and rule and len(header) == len(rule) and all(re.fullmatch(r":?-{3,}:?", c) for c in rule):
            output.append("    ".join(header))
            index += 2
            while index < len(lines):
                cells = _pipe_cells(lines[index]) if index not in multiline else None
                if not cells or len(cells) != len(header):
                    break
                output.append("    ".join(cells))
                index += 1
        else:
            output.append(lines[index])
            index += 1
    # Clean contiguous prose together so code spans may contain line breaks.
    return _clean_inline("\n".join(output))


def clean_prose(text: str) -> str:
    return "".join(raw if kind == "code" else _clean_prose(body) for kind, raw, body in _blocks(text))


def split_markdown(text: str) -> list[tuple[str, str]]:
    return [(kind, body if kind == "code" else _clean_prose(body.strip("\n")))
            for kind, _, body in _blocks(text) if kind == "code" or body.strip("\n")]


def _path_intent(raw, *, link=False):
    parts = [part for part in raw.replace("\\", "/").split("/") if part not in {"", ".", "..", "~"}]
    if parts and re.fullmatch(r"[a-zA-Z]:", parts[0]):
        parts.pop(0)
    return bool(parts) and (link or len(parts) > 1 or bool(re.fullmatch(r"[^.\s][^/]*\.[a-zA-Z0-9]{1,10}", parts[0])))


def collect_references(text: str, base: Path | None, *, exists=Path.exists) -> list[dict]:
    """Resolve once at completion, with the tools' base and an injectable filesystem predicate.

    Explicit links and quoted paths may contain spaces. For bare paths try the longest existing
    prefix on the line, so an unquoted absolute path with spaces is still reachable.
    """
    from ..files import resolve

    found, seen = [], set()

    def add(raw, label="", *, link=False):
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            return False
        if _SCHEME.match(raw) and not re.match(r"^[a-zA-Z]:[\\/]", raw):
            if link and raw.partition(":")[0].lower() in {"http", "https", "mailto"}:
                key = ("url", raw)
                if key not in seen:
                    found.append({"kind": "url", "target": raw, "name": clean_prose(label or raw)})
                    seen.add(key)
            return False
        try:
            raw = unquote(raw) if link else raw
            if not _path_intent(raw, link=link):
                return False
            if base is None and (raw.startswith("//") or not Path(raw).expanduser().is_absolute()):
                return False
            path = resolve(raw, base=base)
            if not exists(path):
                return False
        except (OSError, ValueError, RuntimeError):
            return False
        key = ("file", str(path))
        if key not in seen:
            found.append({"kind": "file", "target": str(path), "name": path.name or str(path)})
            seen.add(key)
        return True

    prose = "\n".join(body for kind, _, body in _blocks(text) if kind == "p")

    def take_inline(match):
        if match.group("target") is not None:
            add(link_target(match), link_label(match), link=True)
        elif match.group("code") is not None:
            add(match.group("body"))
        return " "

    prose = _INLINE.sub(take_inline, prose)

    def take_quoted(match):
        add(next(part for part in match.groups() if part is not None))
        return " "

    prose = _QUOTED.sub(take_quoted, prose)
    prose = re.sub(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://\S+", " ", prose)
    for line in prose.splitlines():
        consumed = 0
        for match in _PATH.finditer(line):
            if match.start() < consumed:
                continue
            if not _path_intent(match[0].rstrip(".,;:!?*)}")):
                continue
            end = re.search(r'[<>\[\]`"|]', line[match.start():])
            tail = line[match.start():match.start() + end.start()] if end else line[match.start():]
            words = list(re.finditer(r"\S+", tail))
            for word in reversed(words):
                candidate = tail[:word.end()]
                if add(candidate) or add(candidate.rstrip(".,;:!?*)}")):
                    consumed = match.start() + word.end()
                    break
    return found


def finish_reply(item: dict, base: Path | None) -> None:
    """Persist derived UI data once, separately from the provider's text."""
    if "file_references" not in item:
        item["file_references"] = collect_references(item["text"], base)
