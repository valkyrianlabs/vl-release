"""Version policies: how a repository's version is spelled, ordered and advanced.

A repository selects one with `[version] policy` (default `semver`). Everything that depends on
the shape of a version (parsing, bumps, ordering, which release a debian/changelog stanza records,
the Debian package version, allowed release transitions) asks the policy instead of assuming
SemVer, so adding a policy never means special cases across the CLI.

`semver` (default)
    MAJOR.MINOR.PATCH. `vlr version bump|cut patch|minor|major`. The Debian package version is
    `VERSION-<debian.revision>` (e.g. 1.4.0-1). Unchanged from earlier vl-release releases.

`debian-upstream`
    For repositories that package someone else's software: `UPSTREAM-REVISION`, e.g. `8.0.2-1`
    (upstream 8.0.2, first packaging revision). The version *is* the Debian version: tag
    `v8.0.2-1`, release notes entry `8.0.2-1`, package `8.0.2-1`. Ordering is dpkg's
    (deb-version(5)), never SemVer precedence (where `8.0.2-1` would be a prerelease of 8.0.2).
    Advancing it is explicit: `revision` (8.0.2-1 -> 8.0.2-2, packaging-only change) or
    `vlr version upstream 8.0.3` (-> 8.0.3-1, revision restarts at 1). `patch|minor|major` are
    refused as ambiguous. Restrictions that keep every version a valid Git tag and a plain
    Debian version: no epoch, no `~`, integer revision >= 1, upstream starting with a digit.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Union

from vlrelease.debversion import compare_debian_versions
from vlrelease.semver import BUMP_PARTS, Version

if TYPE_CHECKING:
    from vlrelease.debchangelog import DebianEntry

DEFAULT_POLICY = "semver"

# upstream: alphanumeric components separated by `.` or `+`, starting with a digit, no leading
# zeros in numeric components (so equal versions are spelled identically).
_UPSTREAM = re.compile(r"[0-9][A-Za-z0-9]*(?:[.+][A-Za-z0-9]+)*")
_LEADING_ZERO = re.compile(r"(?:^|[.+])0[0-9]")
_UPSTREAM_REVISION = re.compile(r"(?P<upstream>[^-]+)-(?P<revision>[1-9][0-9]*)")


@functools.total_ordering
@dataclass(frozen=True, eq=True)
class UpstreamRevision:
    """`UPSTREAM-REVISION`, ordered like dpkg orders Debian versions."""

    upstream: str
    revision: int

    def __str__(self) -> str:
        return f"{self.upstream}-{self.revision}"

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, UpstreamRevision):
            return NotImplemented
        return compare_debian_versions(str(self), str(other)) < 0


ReleaseVersion = Union[Version, UpstreamRevision]


def parse_upstream(raw: str) -> str:
    value = raw.strip()
    if not _UPSTREAM.fullmatch(value) or _LEADING_ZERO.search(value):
        problem = "contains `-`" if "-" in value else "contains `~`" if "~" in value else "contains `:` (epochs are not supported)" if ":" in value else None
        raise ValueError(
            f"invalid upstream version {raw!r}: expected alphanumeric components separated by `.` or `+`, starting with "
            f"a digit, without leading zeros (e.g. 8.0.2)" + (f"; it {problem}" if problem else "")
        )
    return value


class VersionPolicy:
    """Interface; see the module docstring for the two policies."""

    name: str
    bump_parts: tuple[str, ...]
    example: str
    bump_hint: str
    # version target kinds whose ecosystems read the version the same way this policy does
    target_kinds: frozenset[str]

    def parse(self, raw: str) -> ReleaseVersion:
        raise NotImplementedError

    def bump(self, current: ReleaseVersion, part: str) -> ReleaseVersion:
        raise NotImplementedError

    def resolve(self, current: ReleaseVersion | None, target: str) -> ReleaseVersion:
        """`target` is one of `bump_parts` (relative to `current`) or an explicit version."""
        if target in self.bump_parts:
            if current is None:
                raise ValueError(f"cannot bump {target}: the current version is unreadable")
            return self.bump(current, target)
        foreign = _foreign_bump_message(self, target)
        if foreign:
            raise ValueError(foreign)
        return self.parse(target)

    def debian_version(self, version: ReleaseVersion, revision: int) -> str:
        """The Debian package version `vlr prepare` writes for `version`."""
        raise NotImplementedError

    def records(self, entry: "DebianEntry", version: ReleaseVersion) -> bool:
        """Whether a debian/changelog stanza is the stanza of release `version`."""
        raise NotImplementedError

    def recorded_version(self, entry: "DebianEntry") -> ReleaseVersion | None:
        """The release a debian/changelog stanza records, or None if it is not one of ours."""
        raise NotImplementedError

    def transition_problem(self, last: ReleaseVersion, new: ReleaseVersion) -> str | None:
        """A policy rule (beyond plain ordering) that releasing `new` after `last` violates."""
        return None

    def is_maintenance_successor(self, last: ReleaseVersion, new: ReleaseVersion) -> bool:
        """May `new` be released with a generated maintenance entry (`prepare --allow-empty-patch`)?"""
        raise NotImplementedError

    def describe(self, version: ReleaseVersion) -> dict[str, str | int]:
        """Extra machine-readable facts about a version (status output); empty for semver."""
        return {}


class SemverPolicy(VersionPolicy):
    name = "semver"
    bump_parts = BUMP_PARTS
    example = "1.4.2"
    bump_hint = "`vlr version bump patch|minor|major`"
    target_kinds = frozenset({"file", "meson", "package_json", "pyproject", "regex", "homebrew"})

    def parse(self, raw: str) -> Version:
        return Version.parse(raw)

    def bump(self, current: ReleaseVersion, part: str) -> Version:
        assert isinstance(current, Version)
        return current.bump(part)

    def debian_version(self, version: ReleaseVersion, revision: int) -> str:
        return f"{version}-{revision}"

    def records(self, entry: "DebianEntry", version: ReleaseVersion) -> bool:
        return entry.upstream == str(version)

    def recorded_version(self, entry: "DebianEntry") -> Version | None:
        return entry.upstream_version()

    def is_maintenance_successor(self, last: ReleaseVersion, new: ReleaseVersion) -> bool:
        return isinstance(last, Version) and isinstance(new, Version) and new.is_patch_successor_of(last)


class DebianUpstreamPolicy(VersionPolicy):
    name = "debian-upstream"
    bump_parts = ("revision",)
    example = "8.0.2-1"
    bump_hint = "`vlr version bump revision` (packaging change) or `vlr version upstream X.Y.Z` (new upstream release)"
    # npm (package.json) and Python (pyproject) would read `-1` as a prerelease / post-release, and
    # Homebrew has its own revision field: only plain files, regex and Meson carry this version.
    target_kinds = frozenset({"file", "meson", "regex"})

    def parse(self, raw: str) -> UpstreamRevision:
        value = raw.strip()
        match = _UPSTREAM_REVISION.fullmatch(value)
        if not match:
            raise ValueError(
                f"invalid version {raw!r}: the debian-upstream policy expects UPSTREAM-REVISION with an integer "
                f"packaging revision >= 1 (e.g. 8.0.2-1)"
            )
        return UpstreamRevision(parse_upstream(match.group("upstream")), int(match.group("revision")))

    def bump(self, current: ReleaseVersion, part: str) -> UpstreamRevision:
        assert isinstance(current, UpstreamRevision) and part == "revision"
        return UpstreamRevision(current.upstream, current.revision + 1)

    def adopt_upstream(self, current: ReleaseVersion | None, upstream: str) -> UpstreamRevision:
        """`upstream`, packaging revision 1; refuses anything not newer than the current upstream."""
        new_upstream = parse_upstream(upstream)
        if isinstance(current, UpstreamRevision):
            order = compare_debian_versions(new_upstream, current.upstream)
            if order == 0:
                raise ValueError(
                    f"upstream {new_upstream} is already the current upstream ({current}); for a packaging-only change "
                    "use `vlr version bump revision`"
                )
            if order < 0:
                raise ValueError(f"upstream {new_upstream} is older than the current upstream {current.upstream}")
        return UpstreamRevision(new_upstream, 1)

    def debian_version(self, version: ReleaseVersion, revision: int) -> str:
        return str(version)

    def records(self, entry: "DebianEntry", version: ReleaseVersion) -> bool:
        return entry.version == str(version)

    def recorded_version(self, entry: "DebianEntry") -> UpstreamRevision | None:
        try:
            return self.parse(entry.version)
        except ValueError:
            return None

    def transition_problem(self, last: ReleaseVersion, new: ReleaseVersion) -> str | None:
        if isinstance(last, UpstreamRevision) and isinstance(new, UpstreamRevision):
            if new.upstream != last.upstream and new.revision != 1:
                return (
                    f"{new} adopts upstream {new.upstream} after {last}, so its packaging revision must restart at 1 "
                    f"({new.upstream}-1); use `vlr version upstream {new.upstream}`"
                )
        return None

    def is_maintenance_successor(self, last: ReleaseVersion, new: ReleaseVersion) -> bool:
        return (
            isinstance(last, UpstreamRevision)
            and isinstance(new, UpstreamRevision)
            and new.upstream == last.upstream
            and new.revision > last.revision
        )

    def describe(self, version: ReleaseVersion) -> dict[str, str | int]:
        assert isinstance(version, UpstreamRevision)
        return {"upstream_version": version.upstream, "packaging_revision": version.revision}


POLICIES: dict[str, VersionPolicy] = {policy.name: policy for policy in (SemverPolicy(), DebianUpstreamPolicy())}
ALL_BUMP_PARTS: tuple[str, ...] = tuple(dict.fromkeys(part for p in POLICIES.values() for part in p.bump_parts))


def get_policy(name: str) -> VersionPolicy:
    try:
        return POLICIES[name]
    except KeyError:
        raise ValueError(f"unknown version policy {name!r}; available: {', '.join(POLICIES)}") from None


def _foreign_bump_message(policy: VersionPolicy, target: str) -> str | None:
    if target not in ALL_BUMP_PARTS:
        return None
    if isinstance(policy, DebianUpstreamPolicy):
        return (
            f"`{target}` is ambiguous under the debian-upstream version policy: use `revision` for a packaging-only "
            "change, or `vlr version upstream X.Y.Z` to adopt a new upstream release (revision restarts at 1)"
        )
    return (
        f"`{target}` bumps belong to another version policy; this repository uses {policy.name} "
        f"({'|'.join(policy.bump_parts)} or an explicit {policy.example})"
    )
