"""`vlr publish-deb` / `vlr verify-published`: idempotent, integrity-checked APT publication.

Per `.deb`, against the live APT `Packages` index:
  version absent                 -> upload
  present with identical sha256  -> skip (pipeline re-run)
  present with different sha256  -> refuse (a published version is immutable)
  index unreadable               -> refuse (never guess)
After uploading, the index is polled until every artifact is listed with its expected sha256.

Ported from vaulthalla's tools/release/packaging/publication.py (the stronger of the two forks).
Credentials reach curl through `--config -` on stdin, never argv (argv is world-readable via ps).

Environment (ValkyrianLabs organization-level names first, legacy per-repo names as fallback):
  RELEASE_PUBLISH_MODE              disabled | nexus     (default: disabled)
  NEXUS_APT_REPO   (NEXUS_REPO_URL) upload URL of the Nexus APT hosted repository
  NEXUS_USER                        upload user
  NEXUS_PASSWORD   (NEXUS_PASS)     upload password
  RELEASE_APT_REPOSITORY_URL / _SUITE / _COMPONENTS / _ARCHITECTURES
                          override [publish.apt] for reading the published index
"""

from __future__ import annotations

import math
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlparse

from vlrelease.apt_index import AptIndex, AptIndexConfig, compare_debian_versions, load_apt_index, redact_url
from vlrelease.artifacts import DebIdentity, deb_files, identify_deb
from vlrelease.checksums import SHA256SUMS_NAME, read_sha256sums
from vlrelease.config import Config
from vlrelease.errors import ConfigError, IntegrityError, ReleaseError

PUBLISH_MODES = ("disabled", "nexus")
ACTION_UPLOAD = "upload"
ACTION_SKIP = "skip-identical"

Uploader = Callable[[Path, str, str, str], None]
IndexLoader = Callable[[], AptIndex]


@dataclass(frozen=True)
class PublishSettings:
    mode: str
    upload_url: str
    username: str
    password: str
    index: AptIndexConfig
    verify_timeout: float
    verify_interval: float


@dataclass(frozen=True)
class PublishPlan:
    artifact: DebIdentity
    action: str
    reason: str


@dataclass
class PublishResult:
    mode: str
    dry_run: bool
    plans: list[PublishPlan] = field(default_factory=list)
    verified: bool = False
    skipped_reason: str | None = None

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "dry_run": self.dry_run,
            "verified": self.verified,
            "skipped_reason": self.skipped_reason,
            "artifacts": [
                {
                    "file": plan.artifact.path.name,
                    "package": plan.artifact.package,
                    "version": plan.artifact.version,
                    "architecture": plan.artifact.architecture,
                    "sha256": plan.artifact.sha256,
                    "action": plan.action,
                    "reason": plan.reason,
                }
                for plan in self.plans
            ],
        }


def _split(raw: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if raw is None or not raw.strip():
        return default
    return tuple(item for item in re.split(r"[,\s]+", raw.strip()) if item) or default


UPLOAD_URL_ENV = ("NEXUS_APT_REPO", "NEXUS_REPO_URL")
USER_ENV = ("NEXUS_USER",)
PASSWORD_ENV = ("NEXUS_PASSWORD", "NEXUS_PASS")


def _first_env(environment: Mapping[str, str], names: tuple[str, ...]) -> str:
    for name in names:
        value = (environment.get(name) or "").strip()
        if value:
            return value
    return ""


def _validate_url(url: str, name: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigError(f"{name} must be an absolute http(s) URL, got {redact_url(url)!r}")


def resolve_settings(config: Config, *, mode: str | None = None, env: Mapping[str, str] | None = None) -> PublishSettings:
    if config.apt is None:
        raise ConfigError("[publish.apt] is not enabled in release.toml")
    environment = os.environ if env is None else env
    resolved_mode = (mode or environment.get("RELEASE_PUBLISH_MODE") or "disabled").strip().lower()
    if resolved_mode not in PUBLISH_MODES:
        raise ConfigError(f"Unsupported publication mode {resolved_mode!r}; expected one of {', '.join(PUBLISH_MODES)}")
    upload_url = _first_env(environment, UPLOAD_URL_ENV)
    username = _first_env(environment, USER_ENV)
    password = _first_env(environment, PASSWORD_ENV)
    index_url = (environment.get("RELEASE_APT_REPOSITORY_URL") or config.apt.repository_url or upload_url).strip()
    if resolved_mode == "nexus":
        missing = [
            " or ".join(names)
            for names, value in ((UPLOAD_URL_ENV, upload_url), (USER_ENV, username), (PASSWORD_ENV, password))
            if not value
        ]
        if missing:
            raise ConfigError(f"Publication mode is `nexus` but {', '.join(missing)} is not set")
        _validate_url(upload_url, "NEXUS_APT_REPO")
    if index_url:
        _validate_url(index_url, "the APT repository URL")
    return PublishSettings(
        mode=resolved_mode,
        upload_url=upload_url,
        username=username,
        password=password,
        index=AptIndexConfig(
            repository_url=index_url,
            suite=(environment.get("RELEASE_APT_SUITE") or config.apt.suite).strip(),
            components=_split(environment.get("RELEASE_APT_COMPONENTS"), config.apt.components),
            architectures=_split(environment.get("RELEASE_APT_ARCHITECTURES"), config.apt.architectures),
            username=username or None,
            password=password or None,
        ),
        verify_timeout=config.apt.verify_timeout,
        verify_interval=config.apt.verify_interval,
    )


def plan_publication(
    artifacts: tuple[DebIdentity, ...], index: AptIndex, *, allow_older_version: bool = False
) -> list[PublishPlan]:
    plans: list[PublishPlan] = []
    for artifact in artifacts:
        published = index.lookup(artifact.package, artifact.version, artifact.architecture)
        if published:
            digests = {entry.sha256 for entry in published}
            if None in digests:
                raise IntegrityError(
                    f"{artifact.package} {artifact.version} ({artifact.architecture}) is already published, but the "
                    "APT index carries no SHA256 for it, so identical bytes cannot be proven. Refusing to re-upload."
                )
            if digests != {artifact.sha256}:
                rendered = ", ".join(sorted(str(item) for item in digests))
                raise IntegrityError(
                    f"REFUSING TO OVERWRITE A PUBLISHED VERSION: {artifact.package} {artifact.version} "
                    f"({artifact.architecture}) is already published with sha256 {rendered}, but {artifact.path.name} "
                    f"has sha256 {artifact.sha256}. Published versions are immutable: release a new version instead."
                )
            plans.append(PublishPlan(artifact, ACTION_SKIP, "already published with identical sha256"))
            continue
        newest = index.newest_version(artifact.package)
        if newest is not None and compare_debian_versions(artifact.version, newest) < 0 and not allow_older_version:
            raise IntegrityError(
                f"Refusing to publish {artifact.package} {artifact.version}: the repository already carries the newer "
                f"{newest}. Pass --allow-older-version only for a deliberate out-of-order release."
            )
        plans.append(PublishPlan(artifact, ACTION_UPLOAD, "version not yet published"))
    return plans


def verify_publication(
    artifacts: tuple[DebIdentity, ...],
    *,
    index_loader: IndexLoader,
    timeout: float,
    interval: float,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> None:
    """Poll until every artifact is listed with the expected sha256; a different sha256 fails at once."""
    attempts = max(1, math.ceil(timeout / interval) + 1) if timeout > 0 else 1
    last_problem = "no attempt made"
    for attempt in range(1, attempts + 1):
        try:
            index = index_loader()
        except Exception as exc:  # network blips are retried until the deadline
            last_problem = f"index unreadable: {exc}"
        else:
            pending: list[str] = []
            for artifact in artifacts:
                listed = index.lookup(artifact.package, artifact.version, artifact.architecture)
                if not listed:
                    pending.append(f"{artifact.package} {artifact.version} ({artifact.architecture}) not listed yet")
                    continue
                digests = {entry.sha256 for entry in listed}
                if digests != {artifact.sha256}:
                    rendered = ", ".join(sorted(str(item) for item in digests))
                    raise IntegrityError(
                        f"The APT index lists {artifact.package} {artifact.version} with sha256 {rendered}, "
                        f"expected {artifact.sha256} ({artifact.path.name})."
                    )
            if not pending:
                log("verified: " + ", ".join(f"{a.package} {a.version} sha256={a.sha256}" for a in artifacts))
                return
            last_problem = "; ".join(pending)
        log(f"verification attempt {attempt}/{attempts}: {last_problem}")
        if attempt < attempts:
            sleep(interval)
    raise ReleaseError(f"The APT index did not confirm the release within {timeout:g}s: {last_problem}")


def require_sha256sums(output_dir: Path, artifacts: tuple[DebIdentity, ...]) -> None:
    sums = output_dir / SHA256SUMS_NAME
    if not sums.is_file():
        raise IntegrityError(f"{SHA256SUMS_NAME} is missing under {output_dir}; refusing to publish unverified artifacts.")
    entries = read_sha256sums(sums)
    for artifact in artifacts:
        expected = entries.get(artifact.path.name)
        if expected is None:
            raise IntegrityError(f"{artifact.path.name} is not listed in {SHA256SUMS_NAME}.")
        if expected != artifact.sha256:
            raise IntegrityError(
                f"{artifact.path.name} sha256 {artifact.sha256} does not match {SHA256SUMS_NAME} ({expected}); "
                "the artifact changed after it was built and validated."
            )


def collect_identities(config: Config) -> tuple[DebIdentity, ...]:
    files = deb_files(config.output_dir)
    if not files:
        raise ReleaseError(f"No .deb files under {config.output_dir}; run `vlr build-deb` first.")
    return tuple(identify_deb(path) for path in files)


def publish_debs(
    config: Config,
    *,
    mode: str | None = None,
    dry_run: bool = False,
    require_enabled: bool = False,
    allow_older_version: bool = False,
    verify_timeout: float | None = None,
    env: Mapping[str, str] | None = None,
    uploader: Uploader | None = None,
    index_loader: IndexLoader | None = None,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> PublishResult:
    settings = resolve_settings(config, mode=mode, env=env)
    if settings.mode == "disabled":
        if require_enabled:
            raise ReleaseError("Publication is required for this run, but RELEASE_PUBLISH_MODE is `disabled`.")
        return PublishResult(mode="disabled", dry_run=dry_run, skipped_reason="publication mode is disabled")

    identities = collect_identities(config)
    require_sha256sums(config.output_dir, identities)
    if not settings.index.repository_url:
        raise ConfigError("Set [publish.apt].repository_url (or RELEASE_APT_REPOSITORY_URL) to read the APT index.")
    loader = index_loader or (lambda: load_apt_index(settings.index))
    try:
        index = loader()
    except ValueError as exc:
        raise ReleaseError(str(exc)) from exc
    plans = plan_publication(identities, index, allow_older_version=allow_older_version)
    for plan in plans:
        log(f"{plan.artifact.path.name} sha256={plan.artifact.sha256}: {plan.action} ({plan.reason})")
    result = PublishResult(mode=settings.mode, dry_run=dry_run, plans=plans)
    if dry_run:
        return result
    upload = uploader or upload_with_curl
    for plan in plans:
        if plan.action == ACTION_UPLOAD:
            log(f"uploading {plan.artifact.path.name} -> {redact_url(settings.upload_url)}")
            upload(plan.artifact.path, settings.upload_url, settings.username, settings.password)
    verify_publication(
        identities,
        index_loader=loader,
        timeout=settings.verify_timeout if verify_timeout is None else verify_timeout,
        interval=settings.verify_interval,
        sleep=sleep,
        log=log,
    )
    result.verified = True
    return result


def verify_published(
    config: Config,
    *,
    timeout: float | None = None,
    env: Mapping[str, str] | None = None,
    index_loader: IndexLoader | None = None,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> tuple[DebIdentity, ...]:
    settings = resolve_settings(config, mode="disabled", env=env)
    if not settings.index.repository_url:
        raise ConfigError("Set [publish.apt].repository_url (or RELEASE_APT_REPOSITORY_URL) to read the APT index.")
    identities = collect_identities(config)
    verify_publication(
        identities,
        index_loader=index_loader or (lambda: load_apt_index(settings.index)),
        timeout=settings.verify_timeout if timeout is None else timeout,
        interval=settings.verify_interval,
        sleep=sleep,
        log=log,
    )
    return identities


def curl_config_for_credentials(username: str, password: str) -> str:
    """curl `--config -` text: keeps credentials out of argv, which any local user can read."""

    def quote(value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
        return f'"{escaped}"'

    return f"user = {quote(f'{username}:{password}')}\n"


def curl_upload_command(artifact: Path, target_url: str) -> list[str]:
    return [
        "curl",
        "--fail",
        "--silent",
        "--show-error",
        "--retry",
        "3",
        "--retry-all-errors",
        "--connect-timeout",
        "10",
        "--max-time",
        "600",
        "--config",
        "-",
        "-H",
        "Content-Type: multipart/form-data",
        "--data-binary",
        f"@{artifact}",
        target_url,
    ]


def upload_with_curl(artifact: Path, target_url: str, username: str, password: str, *, runner=subprocess.run) -> None:
    """POST the .deb to the Nexus APT hosted repository (Nexus's documented upload form)."""
    try:
        completed = runner(
            curl_upload_command(artifact, target_url),
            input=curl_config_for_credentials(username, password),
            text=True,
            capture_output=True,
            check=False,
            timeout=1200,
        )
    except FileNotFoundError as exc:
        raise ReleaseError("`curl` is required for APT publication but is not on PATH.") from exc
    if completed.returncode != 0:
        tail = "\n".join([line for line in (completed.stderr or "").splitlines() if line.strip()][-20:])
        raise ReleaseError(
            f"Upload of {artifact.name} to {redact_url(target_url)} failed (curl exit {completed.returncode}).\n{tail}"
        )
