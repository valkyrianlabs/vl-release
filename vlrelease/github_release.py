"""`vlr github-release`: create/update the GitHub release from the prepared notes, idempotently.

Title and body come from the prepared release-notes entry (never regenerated). Assets are
uploaded without --clobber; an asset that already exists is compared by sha256 (GitHub's asset
`digest`, or a download when the API doesn't report one): identical -> skipped, different -> refused.
Uses the `gh` CLI, which reads GH_TOKEN/GITHUB_TOKEN in CI.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from vlrelease.checksums import SHA256SUMS_NAME, release_assets, sha256_file
from vlrelease.config import Config
from vlrelease.errors import ConfigError, IntegrityError, ReleaseError
from vlrelease.prepare import current_entry, format_release_title

Runner = Callable[..., subprocess.CompletedProcess]


@dataclass
class GitHubReleaseResult:
    tag: str
    repository: str
    created: bool = False
    edited: bool = False
    uploaded: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    dry_run: bool = False

    def as_dict(self) -> dict:
        return {
            "tag": self.tag,
            "repository": self.repository,
            "created": self.created,
            "edited": self.edited,
            "uploaded": self.uploaded,
            "skipped": self.skipped,
            "dry_run": self.dry_run,
        }


def _gh(runner: Runner, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    try:
        completed = runner(["gh", *args], text=True, capture_output=True, check=False)
    except FileNotFoundError as exc:
        raise ReleaseError("The GitHub CLI `gh` is required for `vlr github-release`.") from exc
    if check and completed.returncode != 0:
        raise ReleaseError(f"`gh {' '.join(args[:3])} ...` failed: {(completed.stderr or completed.stdout).strip()}")
    return completed


def release_body(config: Config) -> str:
    return current_entry(config).body.rstrip() + "\n"


def publish_github_release(
    config: Config,
    *,
    repository: str | None = None,
    dry_run: bool = False,
    runner: Runner = subprocess.run,
    log: Callable[[str], None] = print,
) -> GitHubReleaseResult:
    repo = repository or config.project.repository or os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        raise ConfigError("Set project.repository (owner/name) or GITHUB_REPOSITORY for `vlr github-release`.")
    entry = current_entry(config)
    tag = config.tag_for(entry.version)
    title = format_release_title(config, version=entry.version, title=entry.title)
    body = release_body(config)
    assets = list(release_assets(config.output_dir))
    if (config.output_dir / SHA256SUMS_NAME).is_file():
        assets.append(config.output_dir / SHA256SUMS_NAME)
    if not assets:
        raise ReleaseError(f"No release assets under {config.output_dir}")
    result = GitHubReleaseResult(tag=tag, repository=repo, dry_run=dry_run)

    view = _gh(runner, ["api", f"repos/{repo}/releases/tags/{tag}"], check=False)
    existing: dict | None = None
    if view.returncode == 0:
        existing = json.loads(view.stdout)
    elif "404" not in (view.stderr or "") and "Not Found" not in (view.stderr or ""):
        raise ReleaseError(f"Cannot query the GitHub release {tag}: {(view.stderr or '').strip()}")

    with tempfile.TemporaryDirectory(prefix="vlr-gh-") as scratch:
        notes_file = Path(scratch) / "notes.md"
        notes_file.write_text(body, encoding="utf-8")
        if existing is None:
            log(f"creating GitHub release {tag} in {repo}")
            if not dry_run:
                _gh(runner, ["release", "create", tag, "--repo", repo, "--verify-tag", "--title", title, "--notes-file", str(notes_file)])
            result.created = True
            remote_assets: dict[str, dict] = {}
        else:
            remote_assets = {asset["name"]: asset for asset in existing.get("assets", [])}
            if existing.get("name") != title or (existing.get("body") or "").rstrip() != body.rstrip():
                log(f"updating title/notes of GitHub release {tag}")
                if not dry_run:
                    _gh(runner, ["release", "edit", tag, "--repo", repo, "--title", title, "--notes-file", str(notes_file)])
                result.edited = True

        for asset in assets:
            local = sha256_file(asset)
            remote = remote_assets.get(asset.name)
            if remote is not None:
                remote_digest = _remote_digest(runner, repo, tag, remote, scratch)
                if remote_digest != local:
                    raise IntegrityError(
                        f"REFUSING TO REPLACE A PUBLISHED ASSET: {asset.name} on {tag} has sha256 {remote_digest}, "
                        f"the local file has {local}. Release assets are immutable; cut a new version."
                    )
                result.skipped.append(asset.name)
                continue
            log(f"uploading {asset.name} sha256={local}")
            if not dry_run:
                _gh(runner, ["release", "upload", tag, str(asset), "--repo", repo])
            result.uploaded.append(asset.name)
    return result


def _remote_digest(runner: Runner, repo: str, tag: str, asset: dict, scratch: str) -> str:
    digest = str(asset.get("digest") or "")
    if digest.startswith("sha256:"):
        return digest.split(":", 1)[1].lower()
    target = Path(scratch) / f"download-{asset['name']}"
    _gh(runner, ["release", "download", tag, "--repo", repo, "--pattern", asset["name"], "--output", str(target), "--clobber"])
    return hashlib.sha256(target.read_bytes()).hexdigest()
