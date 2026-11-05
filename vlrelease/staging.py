"""The agent-maintained staging files: `.release/CHANGELOG_NEXT.md` and `.release/RELEASE_NOTES_NEXT.md`.

CHANGELOG_NEXT.md (package-facing, rendered into debian/changelog):

    ## Optional section heading
    - One bullet per change.
      Continuation lines are indented.
      - A nested detail bullet.

RELEASE_NOTES_NEXT.md (user-facing, rendered into RELEASE_NOTES.md and the GitHub release):

    # Release title (no version number; vlr adds it)

    Markdown body.

Everything inside HTML comments is ignored, so the reset templates are pure guidance comments.

Fragments (`.release/notes.d/*.md`, `.release/changelog.d/*.md`, configurable): one new file per change, so
parallel branches never edit the same staging file (on GitHub every pull request touching RELEASE_NOTES_NEXT.md
conflicted with every other). `vlr prepare` assembles them, in file-name order, after the *_NEXT files' content and
deletes the ones it promoted. A notes fragment is a Markdown chunk that may start with one `## Section` heading
(fragments naming the same section are grouped under it); a changelog fragment uses the CHANGELOG_NEXT.md syntax. The
release title stays in RELEASE_NOTES_NEXT.md (written once, at release time); with fragments staged its body may be
empty. README.md and files starting with `_` or `.` in a fragment directory are ignored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from vlrelease.markdown import has_content, headings, is_placeholder, strip_comments
from vlrelease.semver import SEMVER_PATTERN

CHANGELOG_NEXT_TEMPLATE = """\
<!--
vl-release staged changelog: package-facing changes for the NEXT release.
Maintained continuously while working; `vlr prepare` renders it into debian/changelog
and resets this file to this template.

Format: one "- " bullet per change (concise, technical). Indent continuation lines.
Optional "## Section" headings group bullets; if used, every bullet must be under one.
One level of nested "  - " detail bullets is allowed. Consolidate; don't paste commit logs.

Parallel changes: add a file per change to .release/changelog.d/ (same format) instead of
editing this file; `vlr prepare` merges them in.
-->
"""

RELEASE_NOTES_NEXT_TEMPLATE = """\
<!--
vl-release staged release notes: the user-facing story of the NEXT release.
Maintained continuously while working; `vlr prepare` promotes it into the release notes
history (and the GitHub release) and resets this file to this template.

Format: the first line is "# <release title>" WITHOUT a version number (vlr adds it).
Everything after the title is the Markdown release body. Describe the resulting behavior
for users and operators; keep it representative of what actually ships.

Parallel changes: add a file per change to .release/notes.d/ (and .release/changelog.d/)
instead of editing this file; `vlr prepare` appends them below this body. The title here
is still required; the body may then be empty.
-->
"""

_SECTION = re.compile(r"^##[ \t]+(?P<text>\S.*?)[ \t]*#*[ \t]*$")
_ITEM = re.compile(r"^[-*+][ \t]+(?P<text>\S.*)$")
_CHILD = re.compile(r"^[ \t]{2,}[-*+][ \t]+(?P<text>\S.*)$")
_CONTINUATION = re.compile(r"^[ \t]{2,}(?P<text>\S.*)$")
_TITLE = re.compile(r"^#[ \t]+(?P<title>\S.*?)[ \t]*#*[ \t]*$")


class StagingError(ValueError):
    pass


@dataclass
class ChangelogItem:
    text: str
    children: list[str] = field(default_factory=list)


@dataclass
class ChangelogSection:
    heading: str | None
    items: list[ChangelogItem] = field(default_factory=list)


@dataclass(frozen=True)
class StagedNotes:
    title: str
    body: str


def parse_changelog_next(text: str, *, source: str = "CHANGELOG_NEXT.md") -> list[ChangelogSection]:
    """Parse staged changelog bullets. Returns [] when the file holds no content."""
    content = strip_comments(text)
    if not content.strip():
        return []
    sections: list[ChangelogSection] = []
    current_section: ChangelogSection | None = None
    last_item: ChangelogItem | None = None
    last_was_child = False
    for number, raw in enumerate(content.split("\n"), start=1):
        line = raw.rstrip()
        if not line.strip():
            continue
        if section := _SECTION.match(line):
            current_section = ChangelogSection(heading=section.group("text"))
            sections.append(current_section)
            last_item, last_was_child = None, False
            continue
        if line.lstrip().startswith("#"):
            raise StagingError(f"{source}:{number}: only '## Section' headings are allowed, got {line.strip()!r}")
        if item := _ITEM.match(line):
            if current_section is None:
                current_section = ChangelogSection(heading=None)
                sections.append(current_section)
            elif current_section.heading is None and any(s.heading for s in sections):
                raise StagingError(f"{source}:{number}: bullet outside a section (sections are in use)")
            last_item = ChangelogItem(text=item.group("text").strip())
            current_section.items.append(last_item)
            last_was_child = False
            continue
        if child := _CHILD.match(line):
            if last_item is None:
                raise StagingError(f"{source}:{number}: nested bullet without a parent bullet")
            last_item.children.append(child.group("text").strip())
            last_was_child = True
            continue
        if continuation := _CONTINUATION.match(line):
            if last_item is None:
                raise StagingError(f"{source}:{number}: indented text without a bullet")
            if last_was_child:
                last_item.children[-1] += " " + continuation.group("text").strip()
            else:
                last_item.text += " " + continuation.group("text").strip()
            continue
        raise StagingError(
            f"{source}:{number}: expected '- bullet', '## Section' or an indented continuation, got {line.strip()!r}"
        )
    if sections and sections[0].heading is None and len(sections) > 1:
        raise StagingError(f"{source}: bullets appear before the first '## Section' heading")
    for section in sections:
        if not section.items:
            raise StagingError(f"{source}: section {section.heading!r} has no bullets")
        if section.heading is not None and is_placeholder(section.heading):
            raise StagingError(f"{source}: section heading {section.heading!r} is a placeholder")
        for item in section.items:
            for text in (item.text, *item.children):
                if is_placeholder(text):
                    raise StagingError(f"{source}: bullet {text!r} is a placeholder, not a change description")
    return sections


def parse_notes_next(
    text: str, *, source: str = "RELEASE_NOTES_NEXT.md", allow_empty_body: bool = False
) -> StagedNotes | None:
    """Parse staged release notes. Returns None when the file holds no content.

    allow_empty_body: the title alone is enough (release-note fragments supply the body)."""
    content = strip_comments(text).strip("\n")
    if not content.strip():
        return None
    lines = content.split("\n")
    first_index = next(index for index, line in enumerate(lines) if line.strip())
    match = _TITLE.match(lines[first_index].strip())
    if not match:
        raise StagingError(f"{source}: the first line must be '# <release title>', got {lines[first_index].strip()!r}")
    title = match.group("title").strip()
    if is_placeholder(title):
        raise StagingError(f"{source}: release title {title!r} is a placeholder")
    if re.match(rf"v?{SEMVER_PATTERN.pattern}\b", title):
        raise StagingError(
            f"{source}: the release title {title!r} contains a version number; leave it out "
            "(the version is chosen at release time and added by `vlr prepare`)"
        )
    body = "\n".join(lines[first_index + 1 :]).strip("\n").rstrip()
    if not body.strip():
        if allow_empty_body:
            return StagedNotes(title=title, body="")
        raise StagingError(f"{source}: the release body (text after the title) is empty")
    if is_placeholder(body):
        raise StagingError(f"{source}: the release body is a placeholder")
    for level, heading in headings(body):
        if level == 1:
            raise StagingError(f"{source}: only the title may be a level-1 heading; found '# {heading}' in the body")
        if level == 6:
            raise StagingError(f"{source}: level-6 headings cannot nest under the release heading; use level 5 or less")
    return StagedNotes(title=title, body=body)


def is_staged(text: str) -> bool:
    return has_content(text)


# --- fragments -------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class NotesFragment:
    source: str
    section: str | None
    body: str


def fragment_files(directory) -> list:
    """The fragment files of `directory` (a Path), in name order. Missing directory: none."""
    if not directory.is_dir():
        return []
    return sorted(
        (path for path in directory.iterdir()
         if path.is_file() and path.suffix == ".md" and path.name != "README.md" and not path.name.startswith(("_", "."))),
        key=lambda path: path.name,
    )


def parse_notes_fragment(text: str, *, source: str) -> NotesFragment | None:
    """A release-notes fragment: Markdown, optionally starting with one `## Section` heading. None when empty."""
    content = strip_comments(text).strip("\n")
    if not content.strip():
        return None
    lines = content.split("\n")
    first = next(index for index, line in enumerate(lines) if line.strip())
    section = None
    if match := _SECTION.match(lines[first].strip()):
        section = match.group("text").strip()
        lines = lines[first + 1 :]
    else:
        lines = lines[first:]
    body = "\n".join(lines).strip("\n").rstrip()
    if not body.strip():
        raise StagingError(f"{source}: the fragment has a section heading but no text")
    if is_placeholder(body) or (section is not None and is_placeholder(section)):
        raise StagingError(f"{source}: the fragment is a placeholder")
    for level, heading in headings(body):
        if level == 1:
            raise StagingError(f"{source}: a fragment may not use a level-1 heading ('# {heading}')")
        if level == 2:
            raise StagingError(
                f"{source}: only the first line of a fragment may be a '## Section' heading; found '## {heading}' later"
            )
        if level == 6:
            raise StagingError(f"{source}: level-6 headings cannot nest under the release heading; use level 5 or less")
    return NotesFragment(source=source, section=section, body=body)


def assemble_notes(base_body: str, fragments: list[NotesFragment]) -> str:
    """The release body: the RELEASE_NOTES_NEXT.md body, then fragments without a section, then each section's
    fragments under its `## Section` heading (sections in order of first appearance; an existing `## Section` in
    the base body is extended in place)."""
    loose = [fragment.body for fragment in fragments if fragment.section is None]
    grouped: dict[str, list[str]] = {}
    for fragment in fragments:
        if fragment.section is not None:
            grouped.setdefault(fragment.section, []).append(fragment.body)

    # Split the base body at its level-2 headings so a fragment section can join an existing one.
    preamble: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    for line in base_body.split("\n") if base_body.strip() else []:
        if match := _SECTION.match(line.strip()):
            sections.append((match.group("text").strip(), []))
        elif sections:
            sections[-1][1].append(line)
        else:
            preamble.append(line)

    def block(lines: list[str]) -> str:
        return "\n".join(lines).strip("\n")

    parts: list[str] = []
    if block(preamble):
        parts.append(block(preamble))
    parts.extend(loose)
    for heading, lines in sections:
        chunk = [part for part in (block(lines), *grouped.pop(heading, [])) if part]
        parts.append(f"## {heading}\n\n" + "\n\n".join(chunk) if chunk else f"## {heading}")
    for heading, bodies in grouped.items():
        parts.append(f"## {heading}\n\n" + "\n\n".join(bodies))
    return "\n\n".join(parts).rstrip()


def merge_changelog(lists: list[list[ChangelogSection]]) -> list[ChangelogSection]:
    """Merge staged changelog sections: bullets of the same section heading are combined, in order; unsectioned
    bullets come first. Mixing unsectioned and sectioned bullets across sources is refused (as within one file)."""
    merged: dict[str | None, ChangelogSection] = {}
    for sections in lists:
        for section in sections:
            target = merged.setdefault(section.heading, ChangelogSection(heading=section.heading))
            target.items.extend(section.items)
    if None in merged and len(merged) > 1:
        raise StagingError(
            "the staged changelog mixes bullets without a section and '## Section' headings across "
            "CHANGELOG_NEXT.md and the fragments; put every bullet under a section"
        )
    result = [merged[None]] if None in merged else []
    result.extend(section for heading, section in merged.items() if heading is not None)
    return result

