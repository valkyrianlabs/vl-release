"""`vlr cut`: bump the version, commit, annotate a tag and (with --push) push both atomically.

The tag push is what triggers the release workflow. The release documentation is NOT promoted
here: the tag carries the populated `_NEXT` files and CI's `vlr prepare` promotes them, so a
failed release never loses staged text. Every step is resumable: re-running continues from
the state it finds (release commit present -> tag it; local tag present -> push it).
A release commit must be a pure version bump: before anything is tagged or pushed, every commit `vlr cut` would
publish is checked against the exact substitution `vlr version` makes (`_require_version_only`), so a resumed
release commit amended with other changes is refused instead of released under the release subject.
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
from vlrelease.semver import Version, resolve_target
from vlrelease.state import read_release_state
from vlrelease.targets import TargetError, replace_version_text
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


def _version_only_problems(config: Config, base: str, target: str | None, version: Version) -> list[str]:
    """What, besides `vlr version`'s own substitution to `version`, changes from `base` to `target` (None: the index).

    Exact, not a line heuristic: each version file must be byte-identical to the base content with the version
    replaced by `replace_version_text`, nothing else may change (no other path, mode, rename or deletion), and the
    canonical version must actually change. Anything unreadable is a problem: the check fails closed.
    """
    root = config.root
    diff = ["diff", "--raw", "--no-renames", "-z", base] + ([target] if target else ["--cached"])
    fields = [item for item in run_git(diff, cwd=root).stdout.split("\0") if item]
    targets = {spec.path: spec for spec in config.version.all_targets}
    problems: list[str] = []
    changed: set[str] = set()
    for meta, path in zip(fields[0::2], fields[1::2]):
        old_mode, new_mode, _old, _new, status = meta.lstrip(":").split(" ")
        changed.add(path)
        if path not in targets:
            problems.append(f"{path} is not a version file")
        elif status != "M" or old_mode != new_mode:
            problems.append(f"{path} is {'renamed, added or deleted' if status != 'M' else 'changed in mode'}")
    if len(fields) % 2:
        problems.append("the diff could not be parsed")
    canonical = config.version.canonical.path
    if canonical not in changed:
        problems.append(f"the canonical {canonical} does not change")
    for path in sorted(changed & set(targets)):
        before = run_git(["show", f"{base}:{path}"], cwd=root, check=False)
        after = run_git(["show", f"{target}:{path}" if target else f":{path}"], cwd=root, check=False)
        if before.returncode != 0 or after.returncode != 0:
            problems.append(f"{path} could not be read")
            continue
        try:
            expected = replace_version_text(targets[path], before.stdout, version)
        except (TargetError, ValueError) as exc:
            problems.append(f"{path}: {exc}")
            continue
        if after.stdout != expected:
            problems.append(f"{path} changes more than its version (expected only the bump to {version})")
    return problems


def _require_version_only(config: Config, base: str, target: str | None, version: Version, tag: str, hint: str) -> None:
    problems = _version_only_problems(config, base, target, version)
    if problems:
        what = f"the release commit {target[:12]}" if target else "the staged release commit"
        rendered = "\n".join(f"  - {problem}" for problem in problems)
        raise ReleaseError(
            f"Refusing to release {tag}: {what} must be a pure version bump on top of "
            f"{config.release.remote}/{config.release.branch}, but:\n{rendered}\n"
            "A release commit is only VERSION and the configured version targets, changed by `vlr version`; "
            f"other changes ship untested under the release subject. {hint}"
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
        if head != remote_head:
            _require_version_only(
                config, remote_head, head, version, tag,
                f"Delete the local tag (`git tag -d {tag}`), move the other changes into their own commit on "
                f"{branch} (`git reset --soft {remote}/{branch}` and commit them separately), push them, and cut again.",
            )
            actions.append("check: release commit is a pure version bump")
        actions.append(f"resume: local tag {tag} found")
    elif head_release == str(version):
        if parent != remote_head:
            raise ReleaseError(f"The release commit for {tag} is not directly on top of {remote}/{branch}.")
        _require_version_only(
            config, remote_head, head, version, tag,
            f"Move the other changes into their own commit on {branch} (`git reset --soft {remote}/{branch}` and "
            "commit them separately), push them, and cut again.",
        )
        actions.append("check: release commit is a pure version bump")
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
            try:
                _require_version_only(config, head, None, version, tag, "This is a vl-release bug; nothing was committed.")
            except ReleaseError:
                run_git(["reset", "-q", "--", *changed], cwd=root)
                run_git(["checkout", "-q", "--", *changed], cwd=root)
                raise
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

