"""`vlr build-npm`: pack the npm package from the prepared work tree, and validate the tarball.

The tarball written to the output directory is the exact artifact that is checksummed, validated,
attached to the GitHub release and published to every `[[publish.npm]]` registry; nothing repacks
it later. Registries identify it by npm's `dist.integrity` (sha512) and `dist.shasum` (sha1).
"""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import tarfile
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Callable

from vlrelease.checksums import release_assets, sha256_file
from vlrelease.config import Config, NpmConfig, normalize_member
from vlrelease.errors import ConfigError, ReleaseError
from vlrelease.state import read_release_state

PACKAGE_JSON = "package.json"
TARBALL_ROOT = "package"


@dataclass(frozen=True)
class NpmManifest:
    name: str
    version: str
    private: bool
    publish_registry: str | None


@dataclass(frozen=True)
class NpmIdentity:
    path: Path
    name: str
    version: str
    sha256: str
    integrity: str  # "sha512-<base64>", npm's dist.integrity
    shasum: str  # sha1 hex, npm's dist.shasum


def _npm(config: Config) -> NpmConfig:
    if config.npm is None:
        raise ConfigError("[npm] is not enabled in release.toml")
    return config.npm


def parse_manifest(text: str, where: str) -> NpmManifest:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReleaseError(f"{where}: invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ReleaseError(f"{where}: must be a JSON object")
    name, version = data.get("name"), data.get("version")
    if not isinstance(name, str) or not name:
        raise ReleaseError(f'{where}: no "name"')
    if not isinstance(version, str) or not version:
        raise ReleaseError(f'{where}: no "version"')
    publish_config = data.get("publishConfig")
    registry = publish_config.get("registry") if isinstance(publish_config, dict) else None
    return NpmManifest(
        name=name,
        version=version,
        private=data.get("private") is True,
        publish_registry=registry if isinstance(registry, str) and registry else None,
    )


def read_manifest(config: Config) -> NpmManifest:
    path = config.resolve(_npm(config).package_dir) / PACKAGE_JSON
    if not path.is_file():
        raise ConfigError(f"[npm] package_dir {_npm(config).package_dir} has no {PACKAGE_JSON}")
    return parse_manifest(path.read_text(encoding="utf-8"), str(path.relative_to(config.root)))


def tarball_name(name: str, version: str) -> str:
    """npm's pack file name: `@scope/pkg` 1.2.3 -> `scope-pkg-1.2.3.tgz`."""
    return f"{name.removeprefix('@').replace('/', '-')}-{version}.tgz"


def integrity_of(path: Path) -> tuple[str, str]:
    sha512 = hashlib.sha512()
    sha1 = hashlib.sha1()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            sha512.update(chunk)
            sha1.update(chunk)
    return "sha512-" + base64.b64encode(sha512.digest()).decode("ascii"), sha1.hexdigest()


def identify_tarball(path: Path, name: str, version: str) -> NpmIdentity:
    integrity, shasum = integrity_of(path)
    return NpmIdentity(path, name, version, sha256_file(path), integrity, shasum)


def npm_tarballs(output_dir: Path) -> tuple[Path, ...]:
    return tuple(path for path in release_assets(output_dir) if path.name.endswith(".tgz"))


def require_prepared_npm(config: Config) -> tuple[str, NpmManifest]:
    """(version, manifest) for a work tree whose docs are prepared and whose package.json carries VERSION."""
    _npm(config)
    state = read_release_state(config)
    if state.phase != "prepared" or state.version is None:
        detail = "; ".join(state.errors + state.warnings) or f"phase is {state.phase}"
        raise ReleaseError(
            f"Refusing to pack: the work tree is not prepared for release ({detail}). "
            "Run `vlr prepare` first; the package is packed from the prepared tree."
        )
    manifest = read_manifest(config)
    version = str(state.version)
    if manifest.version != version:
        raise ReleaseError(
            f"{_npm(config).package_dir}/{PACKAGE_JSON} has version {manifest.version}, but the release is {version}. "
            "Add it to [version].targets so `vlr version` keeps it in sync."
        )
    return version, manifest


def require_publishable_manifest(manifest: NpmManifest, where: str) -> None:
    if manifest.private:
        raise ReleaseError(f'{where}: "private": true; npm refuses to publish it')
    if manifest.publish_registry:
        raise ReleaseError(
            f'{where}: publishConfig.registry ({manifest.publish_registry}) would override the [[publish.npm]] '
            "registries; remove it and configure registries in release.toml"
        )


Runner = Callable[..., subprocess.CompletedProcess]


@dataclass
class NpmBuildResult:
    name: str
    version: str
    tarball: Path
    dry_run: bool
    commands: list[list[str]] = field(default_factory=list)
    identity: NpmIdentity | None = None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "tarball": str(self.tarball),
            "dry_run": self.dry_run,
            "commands": self.commands,
            "sha256": self.identity.sha256 if self.identity else None,
            "integrity": self.identity.integrity if self.identity else None,
        }


def pack_command(packer: str, destination: Path) -> list[str]:
    if packer == "pnpm":
        return ["pnpm", "pack", "--pack-destination", str(destination)]
    return ["npm", "pack", "--pack-destination", str(destination)]


def _run(command: list[str], cwd: Path, runner: Runner, log: Callable[[str], None]) -> None:
    log(f"$ {' '.join(command)}  (in {cwd})")
    try:
        completed = runner(command, cwd=cwd, check=False)
    except FileNotFoundError as exc:
        raise ReleaseError(f"`{command[0]}` is not on PATH") from exc
    if completed.returncode != 0:
        raise ReleaseError(f"`{' '.join(command)}` failed with exit code {completed.returncode}")


def build_npm(
    config: Config,
    *,
    clean: bool = True,
    dry_run: bool = False,
    runner: Runner = subprocess.run,
    log: Callable[[str], None] = print,
) -> NpmBuildResult:
    npm = _npm(config)
    version, manifest = require_prepared_npm(config)
    require_publishable_manifest(manifest, f"{npm.package_dir}/{PACKAGE_JSON}")
    package_dir = config.resolve(npm.package_dir)
    output_dir = config.output_dir
    tarball = output_dir / tarball_name(manifest.name, version)
    commands = [list(command) for command in npm.pre_pack] + [pack_command(npm.packer, output_dir)]
    result = NpmBuildResult(manifest.name, version, tarball, dry_run, commands)
    if dry_run:
        return result

    output_dir.mkdir(parents=True, exist_ok=True)
    if clean:
        for stale in npm_tarballs(output_dir):
            stale.unlink()
    for command in commands:
        _run(command, package_dir, runner, log)
    if not tarball.is_file():
        produced = ", ".join(path.name for path in npm_tarballs(output_dir)) or "nothing"
        raise ReleaseError(f"{npm.packer} pack did not write {tarball.name} to {output_dir} (found: {produced})")
    result.identity = identify_tarball(tarball, manifest.name, version)
    log(f"packed {tarball.name} sha256={result.identity.sha256} integrity={result.identity.integrity}")
    return result


class NpmTarballContents:
    """Members and file bytes of an npm tarball (gzip tar with a `package/` root)."""

    def __init__(self, path: Path) -> None:
        self.members: set[str] = set()
        self._files: dict[str, bytes] = {}
        try:
            with tarfile.open(path, mode="r:gz") as archive:
                for info in archive.getmembers():
                    name = normalize_member(info.name).rstrip("/")
                    if not name:
                        continue
                    self.members.add(name)
                    if info.isfile():
                        extracted = archive.extractfile(info)
                        if extracted is not None:
                            self._files[name] = extracted.read()
        except (OSError, tarfile.TarError) as exc:
            raise ReleaseError(f"{path.name}: unreadable npm tarball: {exc}") from exc

    def read(self, member: str) -> bytes | None:
        return self._files.get(member)

    def matches(self, pattern: str) -> list[str]:
        return sorted(member for member in self.members if fnmatchcase(member, pattern))


def check_npm_tarball(config: Config, path: Path, version: str, expected_name: str) -> list[str]:
    npm = _npm(config)
    name = path.name
    issues: list[str] = []
    try:
        contents = NpmTarballContents(path)
    except ReleaseError as exc:
        return [str(exc)]
    outside = sorted(member for member in contents.members if member != TARBALL_ROOT and not member.startswith(f"{TARBALL_ROOT}/"))
    if outside:
        issues.append(f"{name}: members outside {TARBALL_ROOT}/: {', '.join(outside[:3])}")
    raw_manifest = contents.read(f"{TARBALL_ROOT}/{PACKAGE_JSON}")
    if raw_manifest is None:
        issues.append(f"{name}: {TARBALL_ROOT}/{PACKAGE_JSON} is missing")
    else:
        try:
            manifest = parse_manifest(raw_manifest.decode("utf-8"), f"{name}:{TARBALL_ROOT}/{PACKAGE_JSON}")
            require_publishable_manifest(manifest, f"{name}:{TARBALL_ROOT}/{PACKAGE_JSON}")
        except (ReleaseError, UnicodeDecodeError) as exc:
            issues.append(str(exc))
        else:
            if manifest.name != expected_name:
                issues.append(f"{name}: packaged name {manifest.name} is not {expected_name}")
            if manifest.version != version:
                issues.append(f"{name}: packaged version {manifest.version} is not the prepared version {version}")
    for pattern in npm.required_paths:
        if not contents.matches(pattern):
            issues.append(f"{name}: required path missing: {pattern}")
    for pattern in npm.forbidden_paths:
        found = contents.matches(pattern)
        if found:
            issues.append(f"{name}: forbidden path present: {pattern} ({', '.join(found[:3])})")
    for group in npm.any_of:
        if not any(contents.matches(pattern) for pattern in group):
            issues.append(f"{name}: none of the alternative paths is present: {', '.join(group)}")
    for pair in npm.identical_files:
        packaged = contents.read(pair.member)
        source = config.resolve(pair.source)
        if packaged is None:
            issues.append(f"{name}: {pair.member} is missing (expected a copy of {pair.source})")
        elif not source.is_file():
            issues.append(f"{name}: identity source {pair.source} does not exist in the work tree")
        elif packaged != source.read_bytes():
            issues.append(f"{name}: {pair.member} differs from the work tree's {pair.source}")
    return issues


def validate_npm_artifacts(config: Config, version: str) -> tuple[list[str], list[str]]:
    """(issues, checked file names) for the npm tarball in the output directory."""
    manifest = read_manifest(config)
    expected = tarball_name(manifest.name, version)
    tarballs = npm_tarballs(config.output_dir)
    issues: list[str] = []
    for extra in tarballs:
        if extra.name != expected:
            issues.append(f"unexpected npm tarball {extra.name} (expected only {expected})")
    path = config.output_dir / expected
    if not path.is_file():
        issues.append(f"npm tarball {expected} is missing (run `vlr build-npm`)")
        return issues, []
    issues.extend(check_npm_tarball(config, path, version, manifest.name))
    return issues, [expected]


def collect_npm_identity(config: Config) -> NpmIdentity:
    """The built tarball for the prepared version (what `publish-npm` uploads)."""
    version, manifest = require_prepared_npm(config)
    path = config.output_dir / tarball_name(manifest.name, version)
    if not path.is_file():
        raise ReleaseError(f"{path.name} is not under {config.output_dir}; run `vlr build-npm` first.")
    return identify_tarball(path, manifest.name, version)

