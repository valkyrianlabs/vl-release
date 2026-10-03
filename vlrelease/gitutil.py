"""Thin, injectable wrappers around the `git` executable."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Sequence

from vlrelease.errors import ReleaseError


def run_git(
    args: Sequence[str],
    *,
    cwd: Path,
    check: bool = True,
    timeout: float = 120.0,
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
            input=input,
            env=env,
        )
    except FileNotFoundError as exc:
        raise ReleaseError("`git` is not installed or not on PATH.") from exc
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise ReleaseError(f"`git {' '.join(args)}` failed (exit {completed.returncode}): {detail}")
    return completed


def git_out(args: Sequence[str], *, cwd: Path, timeout: float = 120.0) -> str:
    return run_git(args, cwd=cwd, timeout=timeout).stdout.strip()


def find_repo_root(start: Path) -> Path | None:
    """The top level of the Git work tree containing `start`, or None outside a repository."""
    try:
        completed = run_git(["rev-parse", "--show-toplevel"], cwd=start, check=False)
    except ReleaseError:
        return None
    if completed.returncode != 0:
        return None
    return Path(completed.stdout.strip()).resolve()


def rev(root: Path, ref: str) -> str | None:
    completed = run_git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=root, check=False)
    sha = completed.stdout.strip()
    return sha if completed.returncode == 0 and sha else None


def head_commit_timestamp(root: Path) -> int | None:
    completed = run_git(["log", "-1", "--format=%ct", "HEAD"], cwd=root, check=False)
    raw = completed.stdout.strip()
    return int(raw) if completed.returncode == 0 and raw.isdigit() else None


def local_tag_exists(root: Path, tag: str) -> bool:
    return run_git(["rev-parse", "--verify", "--quiet", f"refs/tags/{tag}"], cwd=root, check=False).returncode == 0


def tracked_files(root: Path) -> list[str]:
    output = run_git(["ls-files", "-z", "--cached"], cwd=root).stdout
    return sorted({item for item in output.split("\0") if item})


def dirty_tracked_paths(root: Path) -> list[str]:
    output = run_git(["status", "--porcelain", "--untracked-files=no", "-z"], cwd=root).stdout
    paths: list[str] = []
    entries = [item for item in output.split("\0") if item]
    index = 0
    while index < len(entries):
        entry = entries[index]
        status, path = entry[:2], entry[3:]
        paths.append(path)
        if status[0] in {"R", "C"}:
            index += 1  # rename/copy entries carry the source path as a separate field
        index += 1
    return paths
