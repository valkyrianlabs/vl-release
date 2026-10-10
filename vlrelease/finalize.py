"""`vlr finalize`: after publication succeeded, persist the prepared release record upstream.

The record (new RELEASE_NOTES.md entry, new debian/changelog stanza, cleared `_NEXT` files) is
applied on top of the *current* head of the release branch in a temporary worktree and pushed
without force. If the branch moved on after the release commit:
  * history entries are inserted unless the branch already has them (idempotent);
  * `_NEXT` files lose exactly the lines that were released; newer staged lines survive. If a
    released line was edited on the branch meanwhile, finalize refuses and explains instead of
    guessing.
If the branch already records this version, finalize is a no-op.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from vlrelease import debchangelog, notes, staging
from vlrelease.config import Config
from vlrelease.errors import IntegrityError, ReleaseError
from vlrelease.fsutil import read_text_or_empty
from vlrelease.gitutil import rev, run_git
from vlrelease.markdown import strip_comments
from vlrelease.prepare import record_digests
from vlrelease.state import read_release_state

BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"


@dataclass
class FinalizeResult:
    status: str  # "finalized" | "already-finalized" | "dry-run"
    version: str
    branch: str
    commit: str | None = None
    changed: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "version": self.version,
            "branch": self.branch,
            "commit": self.commit,
            "changed": self.changed,
        }


def subtract_released(branch_text: str, released_text: str, *, template: str, source: str) -> str:
    """Remove the released staging lines from the branch's staging file, keeping newer ones."""
    released = [line.rstrip() for line in strip_comments(released_text).split("\n") if line.strip()]
    remaining = [line.rstrip() for line in strip_comments(branch_text).split("\n")]
    if [line for line in remaining if line.strip()] == released:
        return template
    for line in released:
        try:
            remaining.remove(line)
        except ValueError:
            raise ReleaseError(
                f"Cannot finalize {source} automatically: the released line {line.strip()!r} was edited on the "
                "release branch after the release commit. Publication already succeeded; resolve by removing the "
                f"released content from {source} by hand, then commit the release record."
            ) from None
    body = "\n".join(remaining).strip("\n")
    return template if not body.strip() else template + "\n" + body + "\n"


def _identity_args(cwd: Path) -> list[str]:
    email = run_git(["config", "--get", "user.email"], cwd=cwd, check=False).stdout.strip()
    return [] if email else ["-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}"]


def _show(root: Path, ref: str, path: str) -> str | None:
    completed = run_git(["show", f"{ref}:{path}"], cwd=root, check=False)
    return completed.stdout if completed.returncode == 0 else None


def finalize_release(
    config: Config,
    *,
    remote: str | None = None,
    branch: str | None = None,
    record: dict[str, str] | None = None,
    dry_run: bool = False,
    attempts: int = 3,
    log: Callable[[str], None] = print,
) -> FinalizeResult:
    remote = remote or config.release.remote
    branch = branch or config.release.branch
    state = read_release_state(config)
    if state.phase != "prepared" or state.version is None or state.notes_top is None:
        raise ReleaseError(f"Nothing to finalize: the work tree is not prepared (phase {state.phase}). Run `vlr prepare`.")
    version = str(state.version)
    if record is not None:
        actual = record_digests(config)
        mismatched = sorted(path for path, digest in record.items() if actual.get(path) != digest)
        if mismatched:
            raise IntegrityError(
                "The prepared documents differ from the record of the build that was published "
                f"({', '.join(mismatched)}); refusing to persist a different release record."
            )

    notes_text = read_text_or_empty(config.resolve(config.release.notes))
    entry_block = notes.top_entry_block(notes_text)
    assert entry_block is not None
    stanza = None
    if config.debian is not None:
        stanza = debchangelog.top_stanza_block(read_text_or_empty(config.resolve(config.debian.changelog)))
        if stanza is None:
            raise ReleaseError(f"{config.debian.changelog} has no complete top stanza")

    release_commit = rev(config.root, "HEAD")
    staging_files = [(config.release.notes_next, staging.RELEASE_NOTES_NEXT_TEMPLATE)]
    if config.debian is not None:
        staging_files.append((config.release.changelog_next, staging.CHANGELOG_NEXT_TEMPLATE))
    released_staging = {path: _show(config.root, "HEAD", path) or "" for path, _ in staging_files}

    for attempt in range(1, attempts + 1):
        run_git(["fetch", "--quiet", remote, branch], cwd=config.root, timeout=300)
        remote_ref = f"refs/remotes/{remote}/{branch}"
        remote_head = rev(config.root, remote_ref)
        if remote_head is None:
            raise ReleaseError(f"Remote branch {remote}/{branch} does not exist")
        if release_commit and run_git(
            ["merge-base", "--is-ancestor", release_commit, remote_head], cwd=config.root, check=False
        ).returncode != 0:
            raise ReleaseError(
                f"The release commit {release_commit[:12]} is not on {remote}/{branch}; refusing to record the release "
                "on a branch it was not cut from (pass --branch to choose the release branch)."
            )
        upstream_notes = _show(config.root, remote_ref, config.release.notes) or ""
        if version in notes.entry_versions(upstream_notes):
            log(f"{remote}/{branch} already records {version}; nothing to do")
            return FinalizeResult("already-finalized", version, branch)

        worktree = Path(tempfile.mkdtemp(prefix="vlr-finalize-"))
        try:
            run_git(["worktree", "add", "--quiet", "--detach", str(worktree), remote_head], cwd=config.root)
            changed: list[str] = []

            def write(relative: str, content: str) -> None:
                path = worktree / relative
                current = read_text_or_empty(path)
                if current != content:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding="utf-8")
                    changed.append(relative)

            write(
                config.release.notes,
                notes.prepend_entry(read_text_or_empty(worktree / config.release.notes), entry_block, project_name=config.project.name),
            )
            if config.debian is not None and stanza is not None:
                deb_path = worktree / config.debian.changelog
                branch_deb = read_text_or_empty(deb_path)
                top = debchangelog.parse_top_entry(branch_deb, source=config.debian.changelog)
                if top is None or not config.policy.records(top, state.version):
                    write(config.debian.changelog, debchangelog.prepend_stanza(branch_deb, stanza))
            for relative, template in staging_files:
                write(
                    relative,
                    subtract_released(
                        read_text_or_empty(worktree / relative),
                        released_staging[relative],
                        template=template,
                        source=relative,
                    ),
                )
            if dry_run:
                return FinalizeResult("dry-run", version, branch, changed=changed)
            run_git(["add", "--", *changed], cwd=worktree)
            message = config.release.finalize_commit_message.format(
                version=version, tag=config.tag_for(version), name=config.project.name
            )
            run_git([*_identity_args(worktree), "commit", "-q", "-m", message], cwd=worktree)
            commit = run_git(["rev-parse", "HEAD"], cwd=worktree).stdout.strip()
            pushed = run_git(["push", remote, f"HEAD:refs/heads/{branch}"], cwd=worktree, check=False, timeout=300)
            if pushed.returncode == 0:
                log(f"recorded {version} on {remote}/{branch} ({commit[:12]})")
                return FinalizeResult("finalized", version, branch, commit, changed)
            log(f"push rejected (attempt {attempt}/{attempts}): {pushed.stderr.strip()}")
        finally:
            run_git(["worktree", "remove", "--force", str(worktree)], cwd=config.root, check=False)
            shutil.rmtree(worktree, ignore_errors=True)
    raise ReleaseError(f"Could not push the release record to {remote}/{branch} after {attempts} attempts")
