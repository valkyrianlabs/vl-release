"""`vlr build-deb`: build Debian packages from the prepared work tree, inside the project.

dpkg-buildpackage always writes its results to the *parent* of the source tree. To keep that
inside the repository, the current work tree (the filesystem state, including what `vlr prepare`
just wrote, never a `git archive` of a commit) is copied to `<build_dir>/deb/src/`, built there,
and the results are collected from `<build_dir>/deb/` into the release output directory.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from vlrelease import debchangelog
from vlrelease.checksums import write_sha256sums
from vlrelease.config import Config
from vlrelease.errors import ReleaseError
from vlrelease.state import read_release_state

BUILD_PRODUCT_SUFFIXES = (".deb", ".ddeb", ".udeb", ".changes", ".buildinfo")
ALWAYS_EXCLUDED = (".git",)


@dataclass
class BuildResult:
    version: str
    debian_version: str
    source: str
    output_dir: Path
    build_tree: Path
    artifacts: list[Path] = field(default_factory=list)
    log: Path | None = None

    def as_dict(self) -> dict:
        return {
            "version": self.version,
            "debian_version": self.debian_version,
            "source": self.source,
            "output_dir": str(self.output_dir),
            "build_tree": str(self.build_tree),
            "artifacts": [path.name for path in self.artifacts],
            "log": str(self.log) if self.log else None,
        }


def require_prepared_debian(config: Config) -> tuple[str, str, str]:
    """(version, debian_version, source) for a work tree whose docs are prepared for VERSION."""
    if config.debian is None:
        raise ReleaseError("[debian] is not enabled in release.toml")
    state = read_release_state(config)
    if state.phase != "prepared" or state.version is None or state.debian_top is None:
        detail = "; ".join(state.errors + state.warnings) or f"phase is {state.phase}"
        raise ReleaseError(
            f"Refusing to build: the work tree is not prepared for release ({detail}). "
            "Run `vlr prepare` first; packages are built from the prepared tree."
        )
    return str(state.version), state.debian_top.version, state.debian_top.source


def _excluded(config: Config) -> set[str]:
    assert config.debian is not None
    names = set(ALWAYS_EXCLUDED)
    names.add(PurePosixPath(config.release.build_dir).parts[0])
    names.add(PurePosixPath(config.release.output_dir).parts[0])
    names.update(config.debian.build_excludes)
    return names


def copy_work_tree(config: Config, destination: Path) -> None:
    excluded = _excluded(config)
    root = config.root

    def ignore(directory: str, entries: list[str]) -> set[str]:
        relative = Path(directory).resolve().relative_to(root)
        skipped: set[str] = set()
        for entry in entries:
            candidate = (relative / entry).as_posix()
            if candidate.startswith("./"):
                candidate = candidate[2:]
            if candidate in excluded or entry in ALWAYS_EXCLUDED:
                skipped.add(entry)
        return skipped

    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(root, destination, symlinks=True, ignore=ignore)


def _run(command: tuple[str, ...], cwd: Path, log: list[str]) -> None:
    log.append(f"$ {' '.join(command)}   (cwd: {cwd})")
    try:
        completed = subprocess.run(list(command), cwd=cwd, text=True, capture_output=True, check=False)
    except FileNotFoundError as exc:
        raise ReleaseError(f"Build command not found: {command[0]}") from exc
    log.extend([completed.stdout.rstrip(), completed.stderr.rstrip(), f"[exit {completed.returncode}]", ""])
    if completed.returncode != 0:
        tail = "\n".join([line for line in completed.stderr.splitlines() if line.strip()][-25:])
        raise _BuildFailed(f"`{' '.join(command)}` failed with exit code {completed.returncode}.\n{tail}")


class _BuildFailed(ReleaseError):
    pass


def clean_output_dir(output_dir: Path) -> None:
    """Remove previous release assets so stale files can never be checksummed or published."""
    if not output_dir.is_dir():
        return
    for path in output_dir.iterdir():
        if path.is_file() or path.is_symlink():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)


def build_debian(config: Config, *, clean: bool = True, dry_run: bool = False) -> BuildResult:
    version, debian_version, source = require_prepared_debian(config)
    assert config.debian is not None
    control = config.resolve("debian/control")
    if not control.is_file():
        raise ReleaseError("debian/control is missing")
    output_dir = config.output_dir
    deb_root = config.build_dir / "deb"
    tree = deb_root / "src"
    result = BuildResult(version, debian_version, source, output_dir, tree)
    if dry_run:
        return result
    if shutil.which(config.debian.build_command[0]) is None:
        raise ReleaseError(f"`{config.debian.build_command[0]}` is not installed (install dpkg-dev).")

    log: list[str] = []
    output_dir.mkdir(parents=True, exist_ok=True)
    if clean:
        clean_output_dir(output_dir)
    log_path = output_dir / "build-deb.log"
    try:
        for command in config.debian.pre_build:
            _run(command, config.root, log)
        if deb_root.exists():
            shutil.rmtree(deb_root)
        deb_root.mkdir(parents=True)
        copy_work_tree(config, tree)
        env_note = f"SOURCE tree copied from {config.root} (prepared work tree)"
        log.insert(0, env_note)
        _run(config.debian.build_command, tree, log)
    except _BuildFailed as exc:
        log_path.write_text("\n".join(log) + "\n", encoding="utf-8")
        raise ReleaseError(f"{exc}\nFull log: {log_path}") from None
    log_path.write_text("\n".join(log) + "\n", encoding="utf-8")
    result.log = log_path

    packages = set(debchangelog.read_control_packages(control))
    collected: list[Path] = []
    for path in sorted(deb_root.iterdir()):
        if not path.is_file() or not path.name.endswith(BUILD_PRODUCT_SUFFIXES):
            continue
        parts = path.name.split("_")
        if len(parts) < 3 or parts[1] != debian_version:
            continue
        if parts[0] not in packages and parts[0] != source:
            continue
        target = output_dir / path.name
        shutil.copy2(path, target)
        collected.append(target)
    if not any(path.name.endswith(".deb") for path in collected):
        raise ReleaseError(f"dpkg-buildpackage succeeded but produced no {debian_version} .deb under {deb_root}")

    for pattern in config.debian.extra_artifacts:
        matches = sorted(glob.glob(str(config.root / pattern)))
        if not matches:
            raise ReleaseError(f"debian.extra_artifacts pattern {pattern!r} matched nothing after the build")
        for match in matches:
            if os.path.isfile(match):
                target = output_dir / Path(match).name
                shutil.copy2(match, target)
                collected.append(target)
    result.artifacts = collected
    return result


def write_checksums(config: Config) -> Path:
    try:
        return write_sha256sums(config.output_dir)
    except ValueError as exc:
        raise ReleaseError(str(exc)) from exc
