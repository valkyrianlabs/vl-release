"""Version target adapters.

Each adapter is a pair of pure text functions (`read`, `replace`), so a multi-file version change
can be computed completely before anything is written. Edits are surgical: only the version
token changes and the rest of the file keeps its exact formatting.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from vlrelease.config import TargetSpec
from vlrelease.policy import DEFAULT_POLICY, ReleaseVersion, VersionPolicy, get_policy

HOMEBREW_PLACEHOLDER_SHA256 = "TODO_REPLACE_WITH_RELEASE_ARCHIVE_SHA256"

_MESON_PATTERN = re.compile(
    r"""(?P<prefix>\bproject\s*\(.*?\bversion\s*:\s*['"])(?P<version>[^'"]+)(?P<suffix>['"])""",
    re.DOTALL,
)
_JSON_VERSION_PATTERN = re.compile(r'(?P<prefix>"version"\s*:\s*")(?P<version>[^"\\]*)(?P<suffix>")')
_PYPROJECT_SECTION = re.compile(r"^\[project\]\s*$", re.MULTILINE)
_TOML_HEADER = re.compile(r"^\s*\[", re.MULTILINE)
_PYPROJECT_VERSION = re.compile(
    r"""^(?P<prefix>version\s*=\s*["'])(?P<version>[^"']+)(?P<suffix>["'])""", re.MULTILINE
)
HOMEBREW_URL_PATTERN = re.compile(r"""^(?P<prefix>\s*url\s+["'])(?P<url>[^"']+)(?P<suffix>["'])""", re.MULTILINE)
HOMEBREW_SHA256_PATTERN = re.compile(
    r"""^(?P<prefix>\s*sha256\s+["'])(?P<sha256>[^"']+)(?P<suffix>["'])""", re.MULTILINE
)
HOMEBREW_VERSION_PATTERN = re.compile(
    r"""^(?P<prefix>\s*version\s+["'])(?P<version>[^"']+)(?P<suffix>["'])""", re.MULTILINE
)
_SEMVER_IN_TEXT = re.compile(r"(?<![\d.])\d+\.\d+\.\d+(?!\.?\d)")


class TargetError(ValueError):
    pass


def read_version_text(spec: TargetSpec, text: str) -> str:
    """The raw version string a target currently declares."""
    if spec.kind == "file":
        value = text.strip()
        if not value:
            raise TargetError("file is empty")
        return value
    if spec.kind == "meson":
        match = _MESON_PATTERN.search(text)
        if not match:
            raise TargetError("no `project(..., version: '...')` declaration found")
        return match.group("version")
    if spec.kind == "package_json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise TargetError(f"invalid JSON: {exc}") from exc
        value = data.get("version") if isinstance(data, dict) else None
        if not isinstance(value, str):
            raise TargetError('no top-level "version" string')
        return value
    if spec.kind == "pyproject":
        start, end = _pyproject_project_span(text)
        match = _PYPROJECT_VERSION.search(text, start, end)
        if not match:
            raise TargetError("no `version = \"...\"` in the [project] table")
        return match.group("version")
    if spec.kind == "regex":
        match = re.compile(spec.pattern or "", re.MULTILINE).search(text)
        if not match:
            raise TargetError(f"pattern {spec.pattern!r} did not match")
        return match.group("version")
    if spec.kind == "homebrew":
        explicit = HOMEBREW_VERSION_PATTERN.search(text)
        if explicit:
            return explicit.group("version")
        url = HOMEBREW_URL_PATTERN.search(text)
        if not url:
            raise TargetError("no top-level `url \"...\"` line")
        found = _SEMVER_IN_TEXT.findall(url.group("url"))
        if not found:
            raise TargetError(f"cannot find a version in the formula url {url.group('url')!r}")
        return found[-1]
    raise TargetError(f"unsupported target kind {spec.kind!r}")


def replace_version_text(spec: TargetSpec, text: str, version: ReleaseVersion) -> str:
    new = str(version)
    if spec.kind == "file":
        return f"{new}\n"
    if spec.kind == "meson":
        read_version_text(spec, text)
        return _MESON_PATTERN.sub(lambda m: f"{m.group('prefix')}{new}{m.group('suffix')}", text, count=1)
    if spec.kind == "package_json":
        before = json.loads(text)
        for match in _JSON_VERSION_PATTERN.finditer(text):
            candidate = text[: match.start("version")] + new + text[match.end("version") :]
            try:
                after = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(after, dict) and after.get("version") == new and {**before, "version": new} == after:
                return candidate
        raise TargetError('could not locate the top-level "version" field for an in-place edit')
    if spec.kind == "pyproject":
        start, end = _pyproject_project_span(text)
        match = _PYPROJECT_VERSION.search(text, start, end)
        if not match:
            raise TargetError("no `version = \"...\"` in the [project] table")
        return text[: match.start("version")] + new + text[match.end("version") :]
    if spec.kind == "regex":
        match = re.compile(spec.pattern or "", re.MULTILINE).search(text)
        if not match:
            raise TargetError(f"pattern {spec.pattern!r} did not match")
        return text[: match.start("version")] + new + text[match.end("version") :]
    if spec.kind == "homebrew":
        return replace_homebrew_version(text, version)
    raise TargetError(f"unsupported target kind {spec.kind!r}")


def replace_homebrew_version(text: str, version: ReleaseVersion) -> str:
    """Point a formula at another version; the sha256 becomes a placeholder until release rendering."""
    current = read_version_text(TargetSpec(kind="homebrew", path="<formula>"), text)
    if current == str(version):
        return text
    url = HOMEBREW_URL_PATTERN.search(text)
    updated = text
    if url:
        new_url = url.group("url").replace(current, str(version))
        updated = updated[: url.start("url")] + new_url + updated[url.end("url") :]
    updated = HOMEBREW_VERSION_PATTERN.sub(
        lambda m: f"{m.group('prefix')}{version}{m.group('suffix')}", updated, count=1
    )
    return HOMEBREW_SHA256_PATTERN.sub(
        lambda m: f"{m.group('prefix')}{HOMEBREW_PLACEHOLDER_SHA256}{m.group('suffix')}", updated, count=1
    )


def _pyproject_project_span(text: str) -> tuple[int, int]:
    section = _PYPROJECT_SECTION.search(text)
    if not section:
        raise TargetError("no [project] table")
    following = _TOML_HEADER.search(text, section.end())
    return section.end(), following.start() if following else len(text)


def read_target(root: Path, spec: TargetSpec, policy: VersionPolicy | None = None) -> ReleaseVersion:
    """The version a target declares, parsed by the repository's version policy (default semver)."""
    path = root / spec.path
    if not path.is_file():
        raise TargetError(f"{spec.path} does not exist")
    raw = read_version_text(spec, path.read_text(encoding="utf-8"))
    try:
        return (policy or get_policy(DEFAULT_POLICY)).parse(raw)
    except ValueError as exc:
        raise TargetError(str(exc)) from exc
