"""`vlr prepare`: promote staged release docs into the historical documents, in the work tree.

prepare never commits. CI prepares an ephemeral checkout, builds and publishes exactly that
state, and only `vlr finalize` (after publication succeeded) persists it. A failed release
therefore leaves the upstream `_NEXT` files untouched.

Rules for the canonical version V:
  * V already at the top of the history and nothing staged  -> idempotent no-op (retry)
  * V already recorded but new docs staged                  -> refuse (bump first)
  * V new and staged docs complete                          -> render, prepend, reset staging
  * V new and staged docs empty                             -> refuse
      (`--allow-empty-patch` may fill in a maintenance entry, for patch releases only)
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field

from vlrelease import debchangelog, notes, staging
from vlrelease.config import Config
from vlrelease.errors import ReleaseError
from vlrelease.fsutil import atomic_write_text, read_text_or_empty
from vlrelease.state import read_release_state, release_datetime

MAINTENANCE_TITLE = "Maintenance release"
MAINTENANCE_BODY = "Maintenance release with no user-facing changes."
MAINTENANCE_CHANGELOG = "Maintenance release."


@dataclass
class PrepareResult:
    status: str  # "prepared" | "already-prepared"
    version: str
    tag: str
    title: str
    release_title: str
    debian_version: str | None
    date: str
    changed: list[str] = field(default_factory=list)
    record: dict[str, str] = field(default_factory=dict)
    preview: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "version": self.version,
            "tag": self.tag,
            "title": self.title,
            "release_title": self.release_title,
            "debian_version": self.debian_version,
            "date": self.date,
            "changed": self.changed,
            "record": self.record,
        }


def format_release_title(config: Config, *, version: str, title: str) -> str:
    return config.release.title.format(version=version, tag=config.tag_for(version), title=title, name=config.project.name)


def _resolve_maintainer(config: Config, previous: debchangelog.DebianEntry | None) -> str:
    assert config.debian is not None
    if config.debian.maintainer:
        return config.debian.maintainer
    name, email = os.environ.get("DEBFULLNAME", "").strip(), os.environ.get("DEBEMAIL", "").strip()
    if name and email:
        return f"{name} <{email}>"
    if previous is not None:
        return previous.maintainer
    control = debchangelog.read_control_field(config.resolve("debian/control"), "Maintainer")
    if control:
        return control
    raise ReleaseError("Cannot determine the Debian maintainer: set debian.maintainer or DEBFULLNAME/DEBEMAIL.")


def _resolve_source(config: Config, previous: debchangelog.DebianEntry | None) -> str:
    return (
        debchangelog.read_control_source(config.resolve("debian/control"))
        or (previous.source if previous else None)
        or config.project.package
    )


def record_digests(config: Config) -> dict[str, str]:
    """sha256 of every file prepare owns; finalize verifies it is persisting exactly this state."""
    paths = [config.release.notes, config.release.notes_next]
    if config.debian is not None:
        paths += [config.debian.changelog, config.release.changelog_next]
    digests: dict[str, str] = {}
    for relative in paths:
        path = config.resolve(relative)
        digests[relative] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
    return digests


def prepare_release(
    config: Config,
    *,
    allow_empty_patch: bool = False,
    debian_revision: int | None = None,
    dry_run: bool = False,
) -> PrepareResult:
    state = read_release_state(config)
    if state.version is None or state.phase == "invalid":
        raise ReleaseError("Cannot prepare:\n" + "\n".join(f"  - {issue}" for issue in state.errors))
    if state.phase == "inconsistent":
        raise ReleaseError("Cannot prepare: " + "; ".join(state.errors))
    version = str(state.version)
    tag = config.tag_for(version)
    when = release_datetime(config)

    if state.phase == "unbumped":
        raise ReleaseError(
            f"{version} is already recorded in the release history, but new release docs are staged. "
            "Bump the version first (`vlr version bump patch|minor|major`)."
        )
    if state.phase == "prepared":
        entry = state.notes_top
        assert entry is not None
        return PrepareResult(
            status="already-prepared",
            version=version,
            tag=tag,
            title=entry.title,
            release_title=format_release_title(config, version=version, title=entry.title),
            debian_version=state.debian_top.version if state.debian_top else None,
            date=entry.date or when.date().isoformat(),
            record=record_digests(config),
        )

    staged_notes = state.staged_notes
    staged_changelog = state.staged_changelog
    if state.phase == "missing-docs":
        missing = [] if staged_notes else [config.release.notes_next]
        if config.debian is not None and not staged_changelog:
            missing.append(config.release.changelog_next)
        last = state.last_recorded
        if not allow_empty_patch:
            raise ReleaseError(
                f"Refusing to prepare {version}: staged release documentation is empty ({', '.join(missing)}). "
                "Write the release docs, or pass --allow-empty-patch for a deliberate empty patch release."
            )
        if last is None or not state.version.is_patch_successor_of(last):
            raise ReleaseError(
                f"--allow-empty-patch only applies to patch releases of an already-released line; "
                f"{version} after {last or 'no previous release'} is not one. Write the release docs."
            )
        staged_notes = staged_notes or staging.StagedNotes(title=MAINTENANCE_TITLE, body=MAINTENANCE_BODY)
        if config.debian is not None and not staged_changelog:
            staged_changelog = [staging.ChangelogSection(None, [staging.ChangelogItem(MAINTENANCE_CHANGELOG)])]

    assert staged_notes is not None
    writes: list[tuple[str, str]] = []

    notes_path = config.resolve(config.release.notes)
    new_entry = notes.render_entry(version=version, title=staged_notes.title, released=when.date(), body=staged_notes.body)
    writes.append(
        (
            config.release.notes,
            notes.prepend_entry(read_text_or_empty(notes_path), new_entry, project_name=config.project.name),
        )
    )

    debian_version = None
    if config.debian is not None:
        assert staged_changelog is not None
        revision = debian_revision if debian_revision is not None else config.debian.revision
        if revision < 1:
            raise ReleaseError("--debian-revision must be >= 1")
        debian_version = f"{version}-{revision}"
        distribution = os.environ.get("RELEASE_DEBIAN_DISTRIBUTION", "").strip() or config.debian.distribution
        urgency = os.environ.get("RELEASE_DEBIAN_URGENCY", "").strip() or config.debian.urgency
        stanza = debchangelog.render_stanza(
            source=_resolve_source(config, state.debian_top),
            version=debian_version,
            distribution=distribution,
            urgency=urgency,
            maintainer=_resolve_maintainer(config, state.debian_top),
            when=when,
            body=debchangelog.render_body(staged_changelog),
        )
        deb_path = config.resolve(config.debian.changelog)
        writes.append((config.debian.changelog, debchangelog.prepend_stanza(read_text_or_empty(deb_path), stanza)))
        writes.append((config.release.changelog_next, staging.CHANGELOG_NEXT_TEMPLATE))
    writes.append((config.release.notes_next, staging.RELEASE_NOTES_NEXT_TEMPLATE))

    result = PrepareResult(
        status="prepared",
        version=version,
        tag=tag,
        title=staged_notes.title,
        release_title=format_release_title(config, version=version, title=staged_notes.title),
        debian_version=debian_version,
        date=when.date().isoformat(),
        changed=[path for path, _ in writes],
        preview={path: content for path, content in writes},
    )
    if dry_run:
        return result
    # History first, staging resets last: an interruption can only leave docs promoted *and* still
    # staged, which `read_release_state` reports as `unbumped`/inconsistent instead of losing text.
    for relative, content in writes:
        atomic_write_text(config.resolve(relative), content)
    result.record = record_digests(config)
    return result


def current_entry(config: Config) -> notes.NotesEntry:
    """The prepared release-notes entry for the canonical version (after `vlr prepare`)."""
    state = read_release_state(config)
    if state.version is None:
        raise ReleaseError("Cannot read the canonical version: " + "; ".join(state.errors))
    entry = state.notes_top
    if entry is None or entry.version != str(state.version):
        raise ReleaseError(
            f"{state.version} is not prepared: the newest entry in {config.release.notes} is "
            f"{entry.version if entry else 'missing'}. Run `vlr prepare` first (or use --staged)."
        )
    return entry


def staged_entry(config: Config) -> notes.NotesEntry:
    state = read_release_state(config)
    if state.staged_notes is None or state.version is None:
        problems = "; ".join(state.errors) or f"{config.release.notes_next} is empty"
        raise ReleaseError(f"No staged release notes: {problems}")
    return notes.NotesEntry(version=str(state.version), title=state.staged_notes.title, date=None, body=state.staged_notes.body)
