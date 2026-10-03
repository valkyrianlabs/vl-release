"""Where a repository is in the release cycle, derived only from files in the work tree.

Phases (for the canonical version V):

    pending       V is newer than the newest recorded release and staged docs are complete
    prepared      V is the newest recorded release and nothing new is staged
                  (after `vlr prepare`, or after the release was finalized)
    unbumped      V is already recorded but new docs are staged: normal during development;
                  bump the version before releasing
    missing-docs  V is newer than the last release but staged docs are empty/incomplete
    inconsistent  the history files disagree, or V is older than the newest recorded release
    invalid       staged docs or version targets cannot be parsed
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

from vlrelease import debchangelog, notes, staging
from vlrelease.config import Config
from vlrelease.fsutil import read_text_or_empty
from vlrelease.gitutil import head_commit_timestamp
from vlrelease.semver import Version
from vlrelease.versioning import read_state

PHASES = ("pending", "prepared", "unbumped", "missing-docs", "inconsistent", "invalid")
RELEASABLE_PHASES = ("pending", "prepared")


@dataclass
class ReleaseState:
    version: Version | None
    tag: str | None
    phase: str
    notes_top: notes.NotesEntry | None = None
    debian_top: debchangelog.DebianEntry | None = None
    staged_notes: staging.StagedNotes | None = None
    staged_changelog: list[staging.ChangelogSection] | None = None
    notes_staged: bool = False
    changelog_staged: bool | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def last_recorded(self) -> Version | None:
        candidates: list[Version] = []
        if self.notes_top is not None:
            try:
                candidates.append(Version.parse(self.notes_top.version))
            except ValueError:
                pass
        if self.debian_top is not None and (upstream := self.debian_top.upstream_version()) is not None:
            candidates.append(upstream)
        return max(candidates) if candidates else None

    def as_dict(self) -> dict:
        return {
            "version": str(self.version) if self.version else None,
            "tag": self.tag,
            "phase": self.phase,
            "last_recorded": str(self.last_recorded) if self.last_recorded else None,
            "notes_top": self.notes_top.version if self.notes_top else None,
            "debian_top": self.debian_top.version if self.debian_top else None,
            "staged": {
                "release_notes": self.notes_staged,
                "changelog": self.changelog_staged,
                "title": self.staged_notes.title if self.staged_notes else None,
            },
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def read_release_state(config: Config) -> ReleaseState:
    versions = read_state(config)
    version = versions.canonical
    state = ReleaseState(version=version, tag=config.tag_for(version) if version else None, phase="invalid")
    state.errors.extend(versions.issues)

    notes_text = read_text_or_empty(config.resolve(config.release.notes))
    try:
        state.notes_top = notes.parse_top_entry(notes_text, source=config.release.notes)
    except notes.NotesError as exc:
        state.errors.append(str(exc))

    if config.debian is not None:
        deb_text = read_text_or_empty(config.resolve(config.debian.changelog))
        try:
            state.debian_top = debchangelog.parse_top_entry(deb_text, source=config.debian.changelog)
        except debchangelog.DebianChangelogError as exc:
            state.errors.append(str(exc))

    notes_next = config.resolve(config.release.notes_next)
    changelog_next = config.resolve(config.release.changelog_next)
    if not notes_next.is_file():
        state.errors.append(f"{config.release.notes_next} is missing (run `vlr init` to create the staging files)")
    notes_next_text = read_text_or_empty(notes_next)
    state.notes_staged = staging.is_staged(notes_next_text)
    try:
        state.staged_notes = staging.parse_notes_next(notes_next_text, source=config.release.notes_next)
    except staging.StagingError as exc:
        state.errors.append(str(exc))

    changelog_text = read_text_or_empty(changelog_next)
    if config.debian is not None:
        if not changelog_next.is_file():
            state.errors.append(f"{config.release.changelog_next} is missing (run `vlr init`)")
        state.changelog_staged = staging.is_staged(changelog_text)
        try:
            state.staged_changelog = staging.parse_changelog_next(changelog_text, source=config.release.changelog_next)
        except staging.StagingError as exc:
            state.errors.append(str(exc))
    elif staging.is_staged(changelog_text):
        state.errors.append(
            f"{config.release.changelog_next} has content, but [debian] is not enabled so nothing would publish it; "
            "move the content into the release notes or enable [debian]"
        )

    if state.errors or version is None:
        state.phase = "invalid"
        return state

    debian = config.debian is not None
    notes_has = state.notes_top is not None and state.notes_top.version == str(version)
    deb_has = state.debian_top is not None and state.debian_top.upstream == str(version)
    if debian and notes_has != deb_has:
        state.phase = "inconsistent"
        state.errors.append(
            f"{version} is recorded in {config.release.notes if notes_has else config.debian.changelog} "
            f"but not in {config.debian.changelog if notes_has else config.release.notes}; "
            "restore both from Git (`git checkout -- <files>`) and prepare again"
        )
        return state

    staged_any = state.notes_staged or bool(state.changelog_staged)
    staged_complete = state.notes_staged and (state.changelog_staged or not debian)
    if notes_has:
        if staged_any:
            state.phase = "unbumped"
            state.warnings.append(
                f"{version} is already released/prepared but new release docs are staged; "
                "bump the version (`vlr version bump patch|minor|major`) before releasing"
            )
        else:
            state.phase = "prepared"
        return state

    last = state.last_recorded
    if last is not None and version <= last:
        state.phase = "inconsistent"
        state.errors.append(f"version {version} is not newer than the last recorded release {last}")
        return state
    if staged_complete:
        state.phase = "pending"
    else:
        state.phase = "missing-docs"
        missing = [config.release.notes_next] if not state.notes_staged else []
        if debian and not state.changelog_staged:
            missing.append(config.release.changelog_next)
        state.warnings.append(f"{version} is not released yet and staged docs are empty: {', '.join(missing)}")
    return state


def release_datetime(config: Config) -> datetime:
    """The release timestamp: SOURCE_DATE_EPOCH, else the HEAD commit time, else now.

    Using the commit time makes `vlr prepare` deterministic: re-running a release job from the
    same commit renders byte-identical documents, so rebuilt artifacts can match published ones.
    """
    raw = os.environ.get("SOURCE_DATE_EPOCH", "").strip()
    if raw.isdigit():
        return datetime.fromtimestamp(int(raw), tz=timezone.utc)
    stamp = head_commit_timestamp(config.root)
    if stamp is not None:
        return datetime.fromtimestamp(stamp, tz=timezone.utc)
    return datetime.now(tz=timezone.utc).replace(microsecond=0)
