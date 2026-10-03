"""Parse the top of debian/changelog and render new stanzas (deb-changelog(5))."""

from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass
from datetime import datetime
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path

from vlrelease.semver import Version
from vlrelease.staging import ChangelogSection

HEADER = re.compile(
    r"^(?P<source>[a-z0-9][a-z0-9+.-]+) \((?P<version>[^()\s]+)\) (?P<distribution>[^;]+); (?P<metadata>.+)$"
)
TRAILER = re.compile(r"^ -- (?P<maintainer>.+?)  (?P<date>\S.*)$")
FULL_VERSION = re.compile(r"^(?:(?P<epoch>\d+):)?(?P<upstream>[^-:]+(?:-[^-:]+)*?)(?:-(?P<revision>[^-:]+))?$")
WRAP_WIDTH = 79


class DebianChangelogError(ValueError):
    pass


@dataclass(frozen=True)
class DebianEntry:
    source: str
    version: str
    distribution: str
    urgency: str
    maintainer: str
    date: str

    @property
    def upstream(self) -> str:
        match = FULL_VERSION.match(self.version)
        return match.group("upstream") if match else self.version

    @property
    def revision(self) -> str | None:
        match = FULL_VERSION.match(self.version)
        return match.group("revision") if match else None

    def upstream_version(self) -> Version | None:
        try:
            return Version.parse(self.upstream)
        except ValueError:
            return None


def parse_top_entry(text: str, *, source: str = "debian/changelog") -> DebianEntry | None:
    """The first stanza, or None for an empty/missing changelog."""
    lines = text.split("\n")
    start = next((i for i, line in enumerate(lines) if line.strip()), None)
    if start is None:
        return None
    header = HEADER.match(lines[start])
    if not header:
        raise DebianChangelogError(f"{source}: cannot parse the top entry header {lines[start]!r}")
    for line in lines[start + 1 :]:
        if HEADER.match(line):
            break
        trailer = TRAILER.match(line)
        if trailer:
            urgency = re.search(r"urgency=(\S+)", header.group("metadata"))
            return DebianEntry(
                source=header.group("source"),
                version=header.group("version"),
                distribution=header.group("distribution").strip(),
                urgency=urgency.group(1).rstrip(",") if urgency else "medium",
                maintainer=trailer.group("maintainer"),
                date=trailer.group("date"),
            )
    raise DebianChangelogError(f"{source}: the top entry has no ' -- maintainer  date' trailer line")


def render_body(sections: list[ChangelogSection]) -> list[str]:
    def wrap(text: str, first: str, rest: str) -> list[str]:
        return textwrap.wrap(
            text,
            width=WRAP_WIDTH,
            initial_indent=first,
            subsequent_indent=rest,
            break_long_words=False,
            break_on_hyphens=False,
        ) or [first.rstrip()]

    lines: list[str] = []
    grouped = any(section.heading for section in sections)
    for section in sections:
        if grouped:
            lines.extend(wrap(section.heading or "", "  * ", "    "))
            for item in section.items:
                lines.extend(wrap(item.text, "    - ", "      "))
                for child in item.children:
                    lines.extend(wrap(child, "      + ", "        "))
        else:
            for item in section.items:
                lines.extend(wrap(item.text, "  * ", "    "))
                for child in item.children:
                    lines.extend(wrap(child, "    - ", "      "))
    return lines


def render_stanza(
    *,
    source: str,
    version: str,
    distribution: str,
    urgency: str,
    maintainer: str,
    when: datetime,
    body: list[str],
) -> str:
    if when.tzinfo is None:
        raise ValueError("changelog dates must be timezone-aware")
    date = format_datetime(when)
    parts = [f"{source} ({version}) {distribution}; urgency={urgency}", "", *body, "", f" -- {maintainer}  {date}", ""]
    return "\n".join(parts)


def prepend_stanza(existing: str, stanza: str) -> str:
    rest = existing.lstrip("\n")
    if not rest.strip():
        return stanza
    return stanza + "\n" + rest


def entry_datetime(entry: DebianEntry) -> datetime | None:
    try:
        return parsedate_to_datetime(entry.date)
    except (TypeError, ValueError):
        return None


def read_control_source(control: Path) -> str | None:
    if not control.is_file():
        return None
    for line in control.read_text(encoding="utf-8").splitlines():
        if line.lower().startswith("source:"):
            return line.split(":", 1)[1].strip() or None
    return None


def read_control_field(control: Path, name: str) -> str | None:
    if not control.is_file():
        return None
    prefix = name.lower() + ":"
    for line in control.read_text(encoding="utf-8").splitlines():
        if line.lower().startswith(prefix):
            return line.split(":", 1)[1].strip() or None
    return None


def read_control_packages(control: Path) -> list[str]:
    if not control.is_file():
        return []
    return [
        line.split(":", 1)[1].strip()
        for line in control.read_text(encoding="utf-8").splitlines()
        if line.lower().startswith("package:")
    ]


def top_stanza_block(text: str) -> str | None:
    """The raw first stanza (header through trailer), newline-terminated."""
    lines = text.split("\n")
    start = next((i for i, line in enumerate(lines) if line.strip()), None)
    if start is None or not HEADER.match(lines[start]):
        return None
    for index in range(start + 1, len(lines)):
        if TRAILER.match(lines[index]):
            return "\n".join(lines[start : index + 1]) + "\n"
    return None
