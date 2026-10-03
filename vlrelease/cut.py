"""`vlr cut`: bump the version, commit, annotate a tag and (with --push) push both atomically.

The tag push is what triggers the release workflow. The release documentation is NOT promoted
here: the tag carries the populated `_NEXT` files and CI's `vlr prepare` promotes them, so a
failed release never loses staged text. Every step is resumable: re-running continues from
the state it finds (release commit present -> tag it; local tag present -> push it).
Adapted from vaulthalla's tools/release/cut.py.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Callable

from vlrelease import staging
from vlrelease.config import Config
from vlrelease.errors import ReleaseError
from vlrelease.fsutil import read_text_or_empty
from vlrelease.gitutil import dirty_tracked_paths, git_out, rev, run_git
from vlrelease.semver import resolve_target
from vlrelease.state import read_release_state
from vlrelease.versioning import apply_version, require_consistent_version


@dataclass
class CutResult:
    version: str
    tag: str
    commit: str
    pushed: bool
    actions: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"version": self.version, "tag": self.tag, "commit": self.commit, "pushed": self.pushed, "actions": self.actions}


def _require_staged_docs(config: Config) -> None:
    problems: list[str] = []
    try:
        if staging.parse_notes_next(read_text_or_empty(config.resolve(config.release.notes_next))) is None:
            problems.append(f"{config.release.notes_next} is empty")
        if config.debian is not None and not staging.parse_changelog_next(
            read_text_or_empty(config.resolve(config.release.changelog_next))
        ):
            problems.append(f"{config.release.changelog_next} is empty")
    except staging.StagingError as exc:
        problems.append(str(exc))
    if problems:
        raise ReleaseError(
            "Refusing to cut a release whose staged documentation would fail `vlr prepare` in CI: " + "; ".join(problems)
        )


def cut_release(
    config: Config,
    target: str,
    *,
    push: bool = False,
    skip_tests: bool = False,
    fetch: bool = True,
    test_runner: Callable[[list[str]], int] | None = None,
    log: Callable[[str], None] = print,
) -> CutResult:
    root = config.root
    remote, branch = config.release.remote, config.release.branch
    actions: list[str] = []

    current_branch = git_out(["rev-parse", "--abbrev-ref", "HEAD"], cwd=root)
    if current_branch != branch:
        raise ReleaseError(f"On branch `{current_branch}`; releases are cut from `{branch}`.")
    dirty = dirty_tracked_paths(root)
    if dirty:
        raise ReleaseError("Tracked files have uncommitted changes: " + ", ".join(dirty))
    if fetch:
        run_git(["fetch", "--quiet", remote, branch], cwd=root, timeout=300)
    remote_head = rev(root, f"refs/remotes/{remote}/{branch}")
    if remote_head is None:
        raise ReleaseError(f"Remote branch {remote}/{branch} is unknown; push the branch first.")
    head = rev(root, "HEAD") or ""
    current = require_consistent_version(config)

    subject = git_out(["log", "-1", "--format=%s", "HEAD"], cwd=root)
    resumable = {
        config.release.cut_commit_message.format(version=v, tag=config.tag_for(v), name=config.project.name): v
        for v in {str(current)}
    }
    head_release = resumable.get(subject) if head != remote_head else None
    if target in ("major", "minor", "patch") and head_release:
        raise ReleaseError(f"HEAD is an unpushed release commit for {head_release}; resume with `vlr cut {head_release}`.")
    try:
        version = resolve_target(current, target)
    except ValueError as exc:
        raise ReleaseError(str(exc)) from exc
    tag = config.tag_for(version)
    if run_git(["ls-remote", "--tags", remote, f"refs/tags/{tag}"], cwd=root, timeout=60).stdout.strip():
        raise ReleaseError(f"{tag} already exists on {remote}; it has been released (or is releasing).")

    local_tag = rev(root, f"refs/tags/{tag}")
    parent = rev(root, "HEAD~1")
    if local_tag is not None:
        if local_tag != head or current != version:
            raise ReleaseError(f"Local tag {tag} exists but does not point at a release commit for {version} at HEAD.")
        if head != remote_head and parent != remote_head:
            raise ReleaseError(f"Local tag {tag} is not exactly one commit ahead of {remote}/{branch}.")
        actions.append(f"resume: local tag {tag} found")
    elif head_release == str(version):
        if parent != remote_head:
            raise ReleaseError(f"The release commit for {tag} is not directly on top of {remote}/{branch}.")
        _require_staged_docs(config)
        run_git(["tag", "-a", tag, "-m", f"{config.project.name} {tag}"], cwd=root)
        actions.append(f"resume: tagged the existing release commit as {tag}")
    else:
        if head != remote_head:
            raise ReleaseError(f"{branch} is not in sync with {remote}/{branch}; push or pull first.")
        if version < current:
            raise ReleaseError(f"Target {version} is older than the current version {current}.")
        state = read_release_state(config)
        if state.last_recorded is not None and version <= state.last_recorded:
            raise ReleaseError(f"Target {version} is not newer than the last recorded release {state.last_recorded}.")
        # version == current is allowed: it was never tagged (checked above) nor recorded, so
        # releasing it just tags HEAD (e.g. the first release of a freshly set-up repository).
        _require_staged_docs(config)
        actions.append("check: version targets consistent; staged release docs present")
        if config.release.test_command and not skip_tests:
            log(f"[cut] running {' '.join(config.release.test_command)}")
            runner = test_runner or (lambda cmd: subprocess.run(cmd, cwd=root, check=False).returncode)
            if runner(list(config.release.test_command)) != 0:
                raise ReleaseError("release.test_command failed; not cutting a release.")
            actions.append("tests: passed")
        if version == current:
            actions.append(f"version: {version} is already set and unreleased; tagging HEAD")
        else:
            changed = apply_version(config, version)
            if not changed:
                raise ReleaseError(f"Setting {version} changed no files")
            run_git(["add", "--", *changed], cwd=root)
            message = config.release.cut_commit_message.format(version=version, tag=tag, name=config.project.name)
            run_git(["commit", "-q", "-m", message], cwd=root)
            actions.append(f"commit: {message}")
        run_git(["tag", "-a", tag, "-m", f"{config.project.name} {tag}"], cwd=root)
        actions.append(f"tag: {tag}")

    commit = rev(root, "HEAD") or ""
    if not push:
        actions.append(f"push: not requested (git push --atomic {remote} HEAD:refs/heads/{branch} refs/tags/{tag})")
        return CutResult(str(version), tag, commit, False, actions)
    run_git(["push", "--atomic", remote, f"HEAD:refs/heads/{branch}", f"refs/tags/{tag}"], cwd=root, timeout=300)
    actions.append(f"push: {branch} + {tag} -> {remote}")
    return CutResult(str(version), tag, commit, True, actions)

