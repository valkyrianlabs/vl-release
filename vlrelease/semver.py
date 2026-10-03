"""Strict `MAJOR.MINOR.PATCH` versions and the small requirement syntax used by `[tool] requires`."""

from __future__ import annotations

import re
from dataclasses import dataclass

SEMVER_PATTERN = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
BUMP_PARTS = ("major", "minor", "patch")


@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, raw: str) -> "Version":
        if not isinstance(raw, str):
            raise ValueError(f"version must be a string, got {type(raw).__name__}")
        match = SEMVER_PATTERN.fullmatch(raw.strip())
        if not match:
            raise ValueError(f"invalid version {raw!r}: expected MAJOR.MINOR.PATCH (e.g. 1.4.2)")
        return cls(*(int(part) for part in match.groups()))

    def bump(self, part: str) -> "Version":
        if part == "major":
            return Version(self.major + 1, 0, 0)
        if part == "minor":
            return Version(self.major, self.minor + 1, 0)
        if part == "patch":
            return Version(self.major, self.minor, self.patch + 1)
        raise ValueError(f"unknown bump part {part!r}; expected one of {', '.join(BUMP_PARTS)}")

    def is_patch_successor_of(self, previous: "Version") -> bool:
        return (self.major, self.minor) == (previous.major, previous.minor) and self.patch > previous.patch

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


def resolve_target(current: Version, target: str) -> Version:
    """`patch|minor|major` relative to `current`, or an explicit version."""
    if target in BUMP_PARTS:
        return current.bump(target)
    return Version.parse(target)


_REQUIREMENT_PATTERN = re.compile(r"(>=|<=|==|!=|>|<)\s*(\d+(?:\.\d+){0,2})")


def _as_tuple(raw: str) -> tuple[int, int, int]:
    parts = [int(part) for part in raw.split(".")]
    while len(parts) < 3:
        parts.append(0)
    return parts[0], parts[1], parts[2]


def satisfies(version: Version, requirement: str) -> bool:
    """Evaluate a comma-separated requirement such as `>=0.1,<1` (missing components are 0)."""
    current = (version.major, version.minor, version.patch)
    clauses = [clause.strip() for clause in requirement.split(",") if clause.strip()]
    if not clauses:
        raise ValueError("empty version requirement")
    for clause in clauses:
        match = _REQUIREMENT_PATTERN.fullmatch(clause)
        if not match:
            raise ValueError(f"invalid requirement clause {clause!r} (expected e.g. '>=0.1' or '<1')")
        operator, bound_raw = match.groups()
        bound = _as_tuple(bound_raw)
        ok = {
            ">=": current >= bound,
            "<=": current <= bound,
            ">": current > bound,
            "<": current < bound,
            "==": current == bound,
            "!=": current != bound,
        }[operator]
        if not ok:
            return False
    return True
