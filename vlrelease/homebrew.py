"""Homebrew: render the release formula and publish it to a tap (e.g. valkyrianlabs/homebrew-tap).

The formula in the repository is the template. `vlr homebrew formula` rewrites its top-level
`url`/`sha256` (and `version`, if declared) for the release and stages it under
`<output_dir>/homebrew/`. With `source = "release-asset"` the url points at the reproducible
source archive attached to the GitHub release, whose sha256 is known locally before anything is
published. `vlr homebrew publish` then commits it to a checkout of the tap, refusing to change
the bytes behind an already-published version.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

from vlrelease.config import Config
from vlrelease.errors import ConfigError, IntegrityError, ReleaseError
from vlrelease.gitutil import run_git
from vlrelease.semver import Version
from vlrelease.state import read_release_state
from vlrelease.targets import (
    HOMEBREW_PLACEHOLDER_SHA256,
    HOMEBREW_SHA256_PATTERN,
    HOMEBREW_URL_PATTERN,
    HOMEBREW_VERSION_PATTERN,
)

_HEX64 = re.compile(r"[0-9a-f]{64}")
_SEMVER_IN_TEXT = re.compile(r"(?<![\d.])\d+\.\d+\.\d+(?![\d.])")
BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"


@dataclass(frozen=True)
class FormulaFacts:
    url: str
    sha256: str
    version: str | None


def formula_facts(text: str) -> FormulaFacts:
    url = HOMEBREW_URL_PATTERN.search(text)
    sha = HOMEBREW_SHA256_PATTERN.search(text)
    if not url or not sha:
        raise ReleaseError("formula has no top-level `url` and `sha256`")
    explicit = HOMEBREW_VERSION_PATTERN.search(text)
    found = _SEMVER_IN_TEXT.findall(url.group("url"))
    version = explicit.group("version") if explicit else (found[-1] if found else None)
    return FormulaFacts(url=url.group("url"), sha256=sha.group("sha256").lower(), version=version)


def render_formula_text(template: str, *, url: str, sha256: str, version: str) -> str:
    if not HOMEBREW_URL_PATTERN.search(template) or not HOMEBREW_SHA256_PATTERN.search(template):
        raise ReleaseError("the formula template needs top-level `url \"...\"` and `sha256 \"...\"` lines")
    text = HOMEBREW_URL_PATTERN.sub(lambda m: f"{m.group('prefix')}{url}{m.group('suffix')}", template, count=1)
    text = HOMEBREW_SHA256_PATTERN.sub(lambda m: f"{m.group('prefix')}{sha256}{m.group('suffix')}", text, count=1)
    text = HOMEBREW_VERSION_PATTERN.sub(lambda m: f"{m.group('prefix')}{version}{m.group('suffix')}", text, count=1)
    facts = formula_facts(text)
    if facts.version != version:
        raise ReleaseError(
            f"the rendered formula resolves to version {facts.version!r}, not {version}; "
            "make the url contain the version or declare `version \"...\"`"
        )
    return text


def formula_name(config: Config) -> str:
    assert config.homebrew is not None
    return PurePosixPath(config.homebrew.tap_path).name


def rendered_formula_path(config: Config) -> Path:
    return config.output_dir / "homebrew" / formula_name(config)


def asset_url(config: Config, version: str) -> str:
    from vlrelease.sourcearchive import archive_name

    assert config.project.repository
    return f"https://github.com/{config.project.repository}/releases/download/{config.tag_for(version)}/{archive_name(config, version)}"


def tag_archive_url(config: Config, version: str) -> str:
    assert config.project.repository
    return f"https://github.com/{config.project.repository}/archive/refs/tags/{config.tag_for(version)}.tar.gz"


def fetch_sha256(url: str, *, opener: Callable[..., object] = urllib.request.urlopen) -> str:
    digest = hashlib.sha256()
    request = urllib.request.Request(url, headers={"User-Agent": "vl-release"})
    try:
        with opener(request, timeout=120) as response:  # type: ignore[attr-defined]
            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                digest.update(chunk)
    except Exception as exc:
        raise ReleaseError(f"Cannot download {url} to verify its sha256: {exc}") from exc
    return digest.hexdigest()


def render_formula(config: Config, *, sha256: str | None = None, fetch: bool = False) -> Path:
    if config.homebrew is None:
        raise ConfigError("[homebrew] is not enabled in release.toml")
    state = read_release_state(config)
    if state.phase != "prepared" or state.version is None:
        raise ReleaseError(f"Refusing to render the release formula: the work tree is not prepared (phase {state.phase}).")
    version = str(state.version)
    template_path = config.resolve(config.homebrew.formula)
    if not template_path.is_file():
        raise ReleaseError(f"Homebrew formula template {config.homebrew.formula} does not exist")

    if config.homebrew.source == "release-asset":
        from vlrelease.checksums import sha256_file
        from vlrelease.sourcearchive import archive_name

        url = asset_url(config, version)
        archive = config.output_dir / archive_name(config, version)
        if not archive.is_file():
            raise ReleaseError(f"{archive.name} is missing; run `vlr source-archive` before rendering the formula")
        local = sha256_file(archive)
        if sha256 is not None and sha256.lower() != local:
            raise IntegrityError(f"--sha256 {sha256} does not match the local source archive ({local})")
        resolved = local
    else:
        url = tag_archive_url(config, version)
        resolved = (sha256 or "").lower() or (fetch_sha256(url) if fetch else "")
        if not resolved:
            raise ReleaseError("source = \"tag-archive\" needs --sha256 or --fetch-sha256 (GitHub creates that archive)")
    if not _HEX64.fullmatch(resolved):
        raise ReleaseError(f"invalid sha256 {resolved!r}")

    rendered = render_formula_text(template_path.read_text(encoding="utf-8"), url=url, sha256=resolved, version=version)
    target = rendered_formula_path(config)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered, encoding="utf-8")
    return target


def validate_rendered_formula(config: Config) -> tuple[list[str], str | None]:
    """Issues with the staged formula (used by `vlr validate-artifacts`)."""
    path = rendered_formula_path(config)
    if not path.is_file():
        return [f"rendered Homebrew formula {path.relative_to(config.root)} is missing (run `vlr homebrew formula`)"], None
    issues: list[str] = []
    state = read_release_state(config)
    version = str(state.version)
    try:
        facts = formula_facts(path.read_text(encoding="utf-8"))
    except ReleaseError as exc:
        return [f"{path.name}: {exc}"], path.name
    if facts.version != version:
        issues.append(f"{path.name}: formula version {facts.version} is not {version}")
    if facts.sha256 == HOMEBREW_PLACEHOLDER_SHA256.lower() or not _HEX64.fullmatch(facts.sha256):
        issues.append(f"{path.name}: sha256 is not a real digest ({facts.sha256})")
    assert config.homebrew is not None
    if config.homebrew.source == "release-asset":
        from vlrelease.checksums import sha256_file
        from vlrelease.sourcearchive import archive_name

        archive = config.output_dir / archive_name(config, version)
        if facts.url != asset_url(config, version):
            issues.append(f"{path.name}: url {facts.url} is not the release asset {asset_url(config, version)}")
        if archive.is_file() and sha256_file(archive) != facts.sha256:
            issues.append(f"{path.name}: sha256 does not match {archive.name}")
    if shutil.which("ruby"):
        completed = subprocess.run(["ruby", "-c", str(path)], capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            issues.append(f"{path.name}: Ruby syntax error: {completed.stderr.strip()}")
    return issues, path.name


@dataclass
class TapResult:
    status: str  # "published" | "already-published" | "dry-run"
    formula: str
    version: str
    commit: str | None = None

    def as_dict(self) -> dict:
        return {"status": self.status, "formula": self.formula, "version": self.version, "commit": self.commit}


def _git_identity(tap_dir: Path) -> list[str]:
    email = run_git(["config", "--get", "user.email"], cwd=tap_dir, check=False).stdout.strip()
    if email:
        return []
    return ["-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}"]


def publish_tap(
    config: Config,
    tap_dir: Path,
    *,
    push: bool = True,
    remote: str = "origin",
    verify_url: bool = True,
    allow_older: bool = False,
    dry_run: bool = False,
    fetch: Callable[[str], str] = fetch_sha256,
    log: Callable[[str], None] = print,
) -> TapResult:
    if config.homebrew is None:
        raise ConfigError("[homebrew] is not enabled in release.toml")
    rendered = rendered_formula_path(config)
    if not rendered.is_file():
        raise ReleaseError(f"{rendered} is missing; run `vlr homebrew formula` first")
    text = rendered.read_text(encoding="utf-8")
    facts = formula_facts(text)
    if facts.version is None or not _HEX64.fullmatch(facts.sha256):
        raise ReleaseError(f"{rendered.name} is not a rendered release formula")
    name = formula_name(config)
    if not (tap_dir / ".git").exists():
        raise ReleaseError(f"{tap_dir} is not a Git checkout of the tap {config.homebrew.tap}")
    branch = os.environ.get("HOMEBREW_TAP_BRANCH", "").strip() or config.homebrew.tap_branch

    if verify_url:
        live = fetch(facts.url)
        if live != facts.sha256:
            raise IntegrityError(
                f"{facts.url} serves sha256 {live}, but the formula declares {facts.sha256}; "
                "publish the GitHub release assets before the tap, and never replace them."
            )
        log(f"verified {facts.url} sha256={live}")

    destination = tap_dir / config.homebrew.tap_path
    for attempt in range(1, 4):
        if destination.is_file():
            existing = formula_facts(destination.read_text(encoding="utf-8"))
            if existing.version == facts.version:
                if existing.sha256 != facts.sha256 or existing.url != facts.url:
                    raise IntegrityError(
                        f"REFUSING TO CHANGE A PUBLISHED FORMULA: {name} {facts.version} is already in the tap with "
                        f"url {existing.url} sha256 {existing.sha256}; the release has url {facts.url} sha256 {facts.sha256}."
                    )
                if destination.read_text(encoding="utf-8") == text:
                    log(f"{name} {facts.version} is already published in {config.homebrew.tap}")
                    return TapResult("already-published", name, facts.version)
            elif existing.version and not allow_older:
                try:
                    if Version.parse(existing.version) > Version.parse(facts.version):
                        raise IntegrityError(
                            f"The tap already carries {name} {existing.version}, newer than {facts.version}; "
                            "pass --allow-older only for a deliberate downgrade."
                        )
                except ValueError:
                    pass
        if dry_run:
            return TapResult("dry-run", name, facts.version)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")
        run_git(["add", "--", config.homebrew.tap_path], cwd=tap_dir)
        if run_git(["diff", "--cached", "--quiet"], cwd=tap_dir, check=False).returncode == 0:
            return TapResult("already-published", name, facts.version)
        run_git([*_git_identity(tap_dir), "commit", "-q", "-m", f"{name.removesuffix('.rb')} {facts.version}"], cwd=tap_dir)
        commit = run_git(["rev-parse", "HEAD"], cwd=tap_dir).stdout.strip()
        if not push:
            return TapResult("published", name, facts.version, commit)
        pushed = run_git(["push", remote, f"HEAD:refs/heads/{branch}"], cwd=tap_dir, check=False, timeout=300)
        if pushed.returncode == 0:
            log(f"pushed {name} {facts.version} to {config.homebrew.tap} {branch} ({commit[:12]})")
            return TapResult("published", name, facts.version, commit)
        log(f"tap push rejected (attempt {attempt}/3); rebasing onto {remote}/{branch}")
        run_git(["fetch", remote, branch], cwd=tap_dir, timeout=300)
        run_git(["reset", "--hard", f"{remote}/{branch}"], cwd=tap_dir)
    raise ReleaseError(f"Could not push the formula to {config.homebrew.tap} after 3 attempts")
