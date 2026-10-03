"""Version consistency across the configured targets: check, sync, set, bump.

Every operation works from the single `[version]` target list in release.toml, so check, sync,
set, bump and `vlr cut` can never disagree about which files carry the version.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from vlrelease.config import Config, TargetSpec
from vlrelease.errors import ReleaseError
from vlrelease.fsutil import atomic_write_text
from vlrelease.semver import Version, resolve_target
from vlrelease.targets import TargetError, read_target, replace_version_text


@dataclass(frozen=True)
class TargetReading:
    spec: TargetSpec
    version: Version | None
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "path": self.spec.path,
            "kind": self.spec.kind,
            "version": str(self.version) if self.version else None,
            "error": self.error,
        }


@dataclass(frozen=True)
class VersionState:
    canonical: Version | None
    readings: tuple[TargetReading, ...]
    issues: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.canonical is not None and not self.issues


def read_state(config: Config) -> VersionState:
    readings: list[TargetReading] = []
    issues: list[str] = []
    for spec in config.version.all_targets:
        try:
            readings.append(TargetReading(spec, read_target(config.root, spec)))
        except TargetError as exc:
            readings.append(TargetReading(spec, None, str(exc)))
            issues.append(f"{spec.describe()}: {exc}")
    canonical = readings[0].version
    if canonical is not None:
        for reading in readings[1:]:
            if reading.version is not None and reading.version != canonical:
                issues.append(
                    f"{reading.spec.describe()} declares {reading.version}, "
                    f"but the canonical {config.version.canonical.path} is {canonical}"
                )
    return VersionState(canonical=canonical, readings=tuple(readings), issues=tuple(issues))


def require_consistent_version(config: Config) -> Version:
    state = read_state(config)
    if not state.ok or state.canonical is None:
        rendered = "\n".join(f"  - {issue}" for issue in state.issues) or "  - canonical version unreadable"
        raise ReleaseError(f"Version targets are not consistent:\n{rendered}\nRun `vlr version sync` or `vlr version set`.")
    return state.canonical


def plan_version_update(
    config: Config, version: Version, *, include_canonical: bool = True
) -> list[tuple[Path, str]]:
    """Return [(path, new_content)] for every target whose content would change."""
    planned: list[tuple[Path, str]] = []
    specs = config.version.all_targets if include_canonical else config.version.targets
    for spec in specs:
        path = config.resolve(spec.path)
        if not path.is_file():
            raise ReleaseError(f"Configured version target {spec.path} does not exist.")
        current = path.read_text(encoding="utf-8")
        try:
            updated = replace_version_text(spec, current, version)
        except (TargetError, ValueError) as exc:
            raise ReleaseError(f"Cannot update {spec.describe()}: {exc}") from exc
        if updated != current:
            planned.append((path, updated))
    return planned


def apply_version(config: Config, version: Version, *, dry_run: bool = False, include_canonical: bool = True) -> list[str]:
    planned = plan_version_update(config, version, include_canonical=include_canonical)
    if not dry_run:
        for path, content in planned:
            atomic_write_text(path, content)
    return [str(path.relative_to(config.root)) for path, _ in planned]


def sync_versions(config: Config, *, dry_run: bool = False) -> tuple[Version, list[str]]:
    state = read_state(config)
    if state.canonical is None:
        raise ReleaseError(
            f"Cannot sync: the canonical version in {config.version.canonical.path} is unreadable "
            f"({state.readings[0].error})."
        )
    return state.canonical, apply_version(config, state.canonical, dry_run=dry_run, include_canonical=False)


def set_version(config: Config, target: str, *, dry_run: bool = False) -> tuple[Version, Version | None, list[str]]:
    """Set every target to `target` (`patch|minor|major` relative to the current version, or X.Y.Z)."""
    state = read_state(config)
    if target in ("major", "minor", "patch"):
        if not state.ok or state.canonical is None:
            rendered = "; ".join(state.issues)
            raise ReleaseError(f"Cannot bump from an inconsistent state ({rendered}). Use `vlr version set X.Y.Z`.")
    try:
        new = resolve_target(state.canonical or Version(0, 0, 0), target)
    except ValueError as exc:
        raise ReleaseError(str(exc)) from exc
    return new, state.canonical, apply_version(config, new, dry_run=dry_run)
