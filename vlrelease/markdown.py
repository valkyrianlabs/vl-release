"""Small, deterministic Markdown helpers (comments, placeholders, fence-aware heading shifts)."""

from __future__ import annotations

import re

_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_ATX = re.compile(r"^(?P<hashes>#{1,6})(?P<rest>(?:[ \t].*)?)$")
_PLACEHOLDER = re.compile(
    r"(?:todo|tbd|tba|fixme|xxx+|placeholder|lorem ipsum.*|n/?a|none|\.\.\.|…|-|<[^>]*>|\[[^\]]*\])[.!:]?",
    re.IGNORECASE,
)


def strip_comments(text: str) -> str:
    return _COMMENT.sub("", text)


def has_content(text: str) -> bool:
    return bool(strip_comments(text).strip())


def is_placeholder(text: str) -> bool:
    """True for text that is obviously a stand-in (`TODO`, `TBD`, `<title>`, `...`)."""
    return bool(_PLACEHOLDER.fullmatch(text.strip()))


def leading_comment(text: str) -> str:
    """The HTML comment block at the very start of `text` (the template guidance), if any."""
    match = re.match(r"\s*(<!--.*?-->)", text, re.DOTALL)
    return match.group(1) if match else ""


def shift_headings(text: str, delta: int) -> str:
    """Shift every ATX heading level by `delta`, ignoring fenced code blocks."""
    lines = text.split("\n")
    in_fence: str | None = None
    out: list[str] = []
    for line in lines:
        fence = _FENCE.match(line)
        if fence:
            marker = fence.group(1)
            if in_fence is None:
                in_fence = marker[0] * 3
            elif marker.startswith(in_fence):
                in_fence = None
            out.append(line)
            continue
        if in_fence is None:
            heading = _ATX.match(line)
            if heading:
                level = len(heading.group("hashes")) + delta
                if not 1 <= level <= 6:
                    raise ValueError(f"heading {line.strip()!r} cannot be shifted to level {level}")
                line = "#" * level + heading.group("rest")
        out.append(line)
    return "\n".join(out)


def headings(text: str) -> list[tuple[int, str]]:
    """(level, text) for every ATX heading outside fenced code."""
    result: list[tuple[int, str]] = []
    in_fence: str | None = None
    for line in text.split("\n"):
        fence = _FENCE.match(line)
        if fence:
            marker = fence.group(1)
            if in_fence is None:
                in_fence = marker[0] * 3
            elif marker.startswith(in_fence):
                in_fence = None
            continue
        if in_fence is None:
            heading = _ATX.match(line)
            if heading:
                result.append((len(heading.group("hashes")), heading.group("rest").strip().rstrip("#").strip()))
    return result
