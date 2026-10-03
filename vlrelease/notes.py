"""The published release-notes history (RELEASE_NOTES.md).

Each release is one entry, newest first:

    <!-- vl-release:entry version=1.4.0 -->
    ## 1.4.0 — Title

    _Released 2026-10-03_

    Body (its headings are shifted one level down so they nest under the release heading).

The marker makes entries unambiguous to parse; `release-title`/`release-body` read the top entry
back out byte-for-byte deterministically (headings shifted back up).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from vlrelease.markdown import shift_headings

ENTRY_MARKER = re.compile(r"^<!-- vl-release:entry version=(?P<version>\S+) -->$", re.MULTILINE)
_ENTRY_HEADING = re.compile(r"^## (?P<version>\S+) — (?P<title>.+?)\s*$")
_RELEASED_LINE = re.compile(r"^_Released (?P<date>\d{4}-\d{2}-\d{2})_$")


class NotesError(ValueError):
    pass


@dataclass(frozen=True)
class NotesEntry:
    version: str
    title: str
    date: str | None
    body: str  # as written in the staged notes (headings restored)


def default_preamble(project_name: str) -> str:
    return f"# {project_name} release notes\n"


def render_entry(*, version: str, title: str, released: date, body: str) -> str:
    shifted = shift_headings(body.strip("\n"), 1)
    return (
        f"<!-- vl-release:entry version={version} -->\n"
        f"## {version} — {title}\n\n"
        f"_Released {released.isoformat()}_\n\n"
        f"{shifted}\n"
    )


def prepend_entry(existing: str, entry: str, *, project_name: str) -> str:
    """Insert `entry` above the newest release, keeping the preamble and every older entry intact."""
    if not existing.strip():
        return default_preamble(project_name) + "\n" + entry
    marker = ENTRY_MARKER.search(existing)
    if marker:
        position = marker.start()
    else:
        # Legacy history without markers: insert before the first release-level heading.
        legacy = re.search(r"^## ", existing, re.MULTILINE)
        position = legacy.start() if legacy else len(existing)
    preamble = existing[:position].rstrip("\n")
    rest = existing[position:]
    joined = (preamble + "\n\n" if preamble else "") + entry
    if rest.strip():
        joined += "\n" + rest.lstrip("\n")
    return joined


def parse_top_entry(text: str, *, source: str = "RELEASE_NOTES.md") -> NotesEntry | None:
    markers = list(ENTRY_MARKER.finditer(text))
    if not markers:
        return None
    first = markers[0]
    end = markers[1].start() if len(markers) > 1 else len(text)
    block = text[first.end() : end].strip("\n")
    lines = block.split("\n")
    heading = _ENTRY_HEADING.match(lines[0]) if lines else None
    if not heading:
        raise NotesError(f"{source}: the entry for {first.group('version')} has no '## <version> — <title>' heading")
    if heading.group("version") != first.group("version"):
        raise NotesError(
            f"{source}: entry marker says {first.group('version')} but its heading says {heading.group('version')}"
        )
    rest = lines[1:]
    while rest and not rest[0].strip():
        rest.pop(0)
    released = None
    if rest and (match := _RELEASED_LINE.match(rest[0].strip())):
        released = match.group("date")
        rest.pop(0)
    body = "\n".join(rest).strip("\n").rstrip()
    try:
        restored = shift_headings(body, -1)
    except ValueError as exc:
        raise NotesError(f"{source}: entry {first.group('version')} has a heading above level 3: {exc}") from exc
    return NotesEntry(version=first.group("version"), title=heading.group("title"), date=released, body=restored)


def entry_versions(text: str) -> list[str]:
    return [match.group("version") for match in ENTRY_MARKER.finditer(text)]


def top_entry_block(text: str) -> str | None:
    """The raw text of the newest entry (marker through the line before the next marker)."""
    markers = list(ENTRY_MARKER.finditer(text))
    if not markers:
        return None
    end = markers[1].start() if len(markers) > 1 else len(text)
    return text[markers[0].start() : end].rstrip("\n") + "\n"
