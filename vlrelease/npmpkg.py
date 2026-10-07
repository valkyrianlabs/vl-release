"""`vlr build-npm`: pack the npm package from the prepared work tree, and validate the tarball.

The tarball written to the output directory is the exact artifact that is checksummed, validated,
attached to the GitHub release and published to every `[[publish.npm]]` registry; nothing repacks
it later. Registries identify it by npm's `dist.integrity` (sha512) and `dist.shasum` (sha1).

`[[npm.aliases]]` publish the same build under more names. Each alias tarball is derived from the
canonical tarball's bytes (never from a second pack or build): the same members, modes and
contents, except the package.json `name` and every occurrence of the canonical name in the
alias's `rewrite` members. Validation re-derives every alias and requires an exact match, so the
packages cannot drift apart.
"""

from __future__ import annotations

import base64
import copy
import gzip
import hashlib
import io
import os
import json
import re
import subprocess
import sys
import tarfile
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Callable

from vlrelease.checksums import release_assets, sha256_file
from vlrelease.config import Config, NpmAlias, NpmConfig, normalize_member
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


def package_names(config: Config, manifest: NpmManifest) -> list[str]:
    """Every name this build is published under: the package.json name first, then `[[npm.aliases]]`."""
    names = [manifest.name, *(alias.name for alias in _npm(config).aliases)]
    if manifest.name in names[1:]:
        raise ConfigError(f"[[npm.aliases]] repeats the package.json name {manifest.name}")
    tarballs = [tarball_name(name, manifest.version) for name in names]
    if len(set(tarballs)) != len(tarballs):
        raise ConfigError(f"npm package names {', '.join(names)} would share a tarball file name")
    return names


# --- aliases --------------------------------------------------------------------------------------


def rename_manifest(raw: bytes, canonical: str, alias: str, where: str) -> bytes:
    """package.json with only its top-level `name` changed, formatting untouched."""
    try:
        text = raw.decode("utf-8")
        data = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"{where}: unreadable package.json: {exc}") from exc
    if not isinstance(data, dict) or data.get("name") != canonical:
        raise ReleaseError(f"{where}: package.json name is not {canonical}")
    pattern = re.compile(r'("name"\s*:\s*)' + re.escape(json.dumps(canonical)))
    if len(pattern.findall(text)) != 1:
        raise ReleaseError(f'{where}: expected exactly one `"name": {json.dumps(canonical)}` in package.json')
    renamed = pattern.sub(lambda match: match.group(1) + json.dumps(alias), text, count=1)
    if json.loads(renamed) != {**data, "name": alias}:
        raise ReleaseError(f"{where}: renaming package.json to {alias} changed more than its name")
    return renamed.encode("utf-8")


def alias_member(member: str, data: bytes, canonical: str, alias: NpmAlias, where: str) -> bytes:
    """The bytes `member` must have in the alias tarball."""
    if member == f"{TARBALL_ROOT}/{PACKAGE_JSON}":
        return rename_manifest(data, canonical, alias.name, where)
    if member in alias.rewrite:
        needle = canonical.encode("utf-8")
        if needle not in data:
            raise ReleaseError(f"{where}: rewrite member {member} does not contain {canonical}")
        return data.replace(needle, alias.name.encode("utf-8"))
    return data


def derive_alias_tarball(canonical_path: Path, canonical: str, alias: NpmAlias, destination: Path) -> None:
    """Write the alias tarball for `alias` from the canonical tarball (deterministic: gzip mtime 0)."""
    where = f"{destination.name} (alias of {canonical_path.name})"
    found: set[str] = set()
    try:
        with tarfile.open(canonical_path, mode="r:gz") as source, destination.open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as compressed:
                with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as target:
                    for info in source.getmembers():
                        member = normalize_member(info.name).rstrip("/")
                        extracted = source.extractfile(info) if info.isfile() else None
                        if extracted is None:
                            target.addfile(info)
                            continue
                        found.add(member)
                        data = alias_member(member, extracted.read(), canonical, alias, where)
                        entry = copy.copy(info)
                        entry.size = len(data)
                        target.addfile(entry, io.BytesIO(data))
    except (OSError, tarfile.TarError) as exc:
        destination.unlink(missing_ok=True)
        raise ReleaseError(f"{where}: cannot derive the alias tarball: {exc}") from exc
    except ReleaseError:
        destination.unlink(missing_ok=True)
        raise
    missing = [member for member in alias.rewrite if member not in found]
    if missing:
        destination.unlink(missing_ok=True)
        raise ReleaseError(f"{where}: rewrite members not in the tarball: {', '.join(missing)}")


def check_alias_tarball(canonical_path: Path, canonical: str, alias: NpmAlias, alias_path: Path) -> list[str]:
    """Issues when the alias tarball is not exactly what `derive_alias_tarball` makes of the canonical one."""
    where = alias_path.name
    try:
        expected, actual = NpmTarballContents(canonical_path), NpmTarballContents(alias_path)
    except ReleaseError as exc:
        return [str(exc)]
    issues: list[str] = []
    if expected.members != actual.members:
        extra = sorted(actual.members - expected.members)
        missing = sorted(expected.members - actual.members)
        issues.append(
            f"{where}: members differ from {canonical_path.name}"
            + (f"; extra: {', '.join(extra[:5])}" if extra else "")
            + (f"; missing: {', '.join(missing[:5])}" if missing else "")
        )
    for member in sorted(expected.members & actual.members):
        data = expected.read(member)
        if expected.modes.get(member) != actual.modes.get(member):
            issues.append(f"{where}: {member} has mode {actual.modes.get(member):o}, the canonical tarball {expected.modes.get(member):o}")
        if data is None:
            continue
        try:
            wanted = alias_member(member, data, canonical, alias, where)
        except ReleaseError as exc:
            issues.append(str(exc))
            continue
        if actual.read(member) != wanted:
            issues.append(f"{where}: {member} is not the canonical {member} renamed for {alias.name}")
    for member in alias.rewrite:
        if member not in expected.members:
            issues.append(f"{where}: rewrite member {member} is not in {canonical_path.name}")
    return issues


Runner = Callable[..., subprocess.CompletedProcess]


@dataclass
class NpmBuildResult:
    name: str
    version: str
    tarball: Path
    dry_run: bool
    commands: list[list[str]] = field(default_factory=list)
    identity: NpmIdentity | None = None
    aliases: list[tuple[str, Path]] = field(default_factory=list)  # (name, tarball)
    alias_identities: list[NpmIdentity] = field(default_factory=list)

    def as_dict(self) -> dict:
        identities = {identity.name: identity for identity in self.alias_identities}
        return {
            "name": self.name,
            "version": self.version,
            "tarball": str(self.tarball),
            "dry_run": self.dry_run,
            "commands": self.commands,
            "sha256": self.identity.sha256 if self.identity else None,
            "integrity": self.identity.integrity if self.identity else None,
            "aliases": [
                {
                    "name": name,
                    "tarball": str(path),
                    "sha256": identities[name].sha256 if name in identities else None,
                    "integrity": identities[name].integrity if name in identities else None,
                }
                for name, path in self.aliases
            ],
        }


def pack_command(packer: str, destination: Path) -> list[str]:
    if packer == "pnpm":
        return ["pnpm", "pack", "--pack-destination", str(destination)]
    return ["npm", "pack", "--pack-destination", str(destination)]


# npm's own housekeeping has no place in a release build: no update check, audit or funding notices.
PACK_ENV = {"NPM_CONFIG_UPDATE_NOTIFIER": "false", "NPM_CONFIG_AUDIT": "false", "NPM_CONFIG_FUND": "false"}


def _run(
    command: list[str], cwd: Path, runner: Runner, log: Callable[[str], None], env: dict[str, str] | None = None
) -> None:
    log(f"$ {' '.join(command)}  (in {cwd})")
    extra = {"env": {**os.environ, **env}} if env else {}
    try:
        # Child output goes to stderr: stdout is reserved for vlr's own (possibly JSON) result.
        try:
            stderr_fd = sys.stderr.fileno()
        except (AttributeError, OSError, ValueError):  # e.g. a redirected, in-memory stderr
            stderr_fd = None
        if stderr_fd is None:
            completed = runner(
                command, cwd=cwd, check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, **extra
            )
            sys.stderr.write(completed.stdout or "")
        else:
            completed = runner(command, cwd=cwd, check=False, stdout=stderr_fd, **extra)
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
    names = package_names(config, manifest)
    tarball = output_dir / tarball_name(manifest.name, version)
    commands = [list(command) for command in npm.pre_pack] + [pack_command(npm.packer, output_dir)]
    result = NpmBuildResult(manifest.name, version, tarball, dry_run, commands)
    result.aliases = [(name, output_dir / tarball_name(name, version)) for name in names[1:]]
    if dry_run:
        return result

    output_dir.mkdir(parents=True, exist_ok=True)
    if clean:
        for stale in npm_tarballs(output_dir):
            stale.unlink()
    for command in commands[:-1]:
        _run(command, package_dir, runner, log)
    _run(commands[-1], package_dir, runner, log, PACK_ENV)
    if not tarball.is_file():
        produced = ", ".join(path.name for path in npm_tarballs(output_dir)) or "nothing"
        raise ReleaseError(f"{npm.packer} pack did not write {tarball.name} to {output_dir} (found: {produced})")
    result.identity = identify_tarball(tarball, manifest.name, version)
    log(f"packed {tarball.name} sha256={result.identity.sha256} integrity={result.identity.integrity}")
    for alias, (name, path) in zip(npm.aliases, result.aliases):
        derive_alias_tarball(tarball, manifest.name, alias, path)
        identity = identify_tarball(path, name, version)
        result.alias_identities.append(identity)
        log(f"derived {path.name} ({name}) sha256={identity.sha256} integrity={identity.integrity}")
    return result


class NpmTarballContents:
    """Members and file bytes of an npm tarball (gzip tar with a `package/` root)."""

    def __init__(self, path: Path) -> None:
        self.members: set[str] = set()
        self.modes: dict[str, int] = {}
        self._files: dict[str, bytes] = {}
        try:
            with tarfile.open(path, mode="r:gz") as archive:
                for info in archive.getmembers():
                    name = normalize_member(info.name).rstrip("/")
                    if not name:
                        continue
                    self.members.add(name)
                    self.modes[name] = info.mode
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
    """(issues, checked file names) for the npm tarballs (canonical and aliases) in the output directory."""
    manifest = read_manifest(config)
    names = package_names(config, manifest)
    expected = [tarball_name(name, version) for name in names]
    issues: list[str] = []
    for extra in npm_tarballs(config.output_dir):
        if extra.name not in expected:
            issues.append(f"unexpected npm tarball {extra.name} (expected only {', '.join(expected)})")
    canonical = config.output_dir / expected[0]
    if not canonical.is_file():
        issues.append(f"npm tarball {expected[0]} is missing (run `vlr build-npm`)")
        return issues, []
    issues.extend(check_npm_tarball(config, canonical, version, manifest.name))
    checked = [expected[0]]
    for alias, file_name in zip(_npm(config).aliases, expected[1:]):
        path = config.output_dir / file_name
        if not path.is_file():
            issues.append(f"npm tarball {file_name} for alias {alias.name} is missing (run `vlr build-npm`)")
            continue
        issues.extend(check_npm_tarball(config, path, version, alias.name))
        issues.extend(check_alias_tarball(canonical, manifest.name, alias, path))
        checked.append(file_name)
    return issues, checked


def collect_npm_identities(config: Config) -> list[NpmIdentity]:
    """The built tarballs for the prepared version (what `publish-npm` uploads): canonical first, then aliases."""
    version, manifest = require_prepared_npm(config)
    identities: list[NpmIdentity] = []
    for name in package_names(config, manifest):
        path = config.output_dir / tarball_name(name, version)
        if not path.is_file():
            raise ReleaseError(f"{path.name} is not under {config.output_dir}; run `vlr build-npm` first.")
        identities.append(identify_tarball(path, name, version))
    return identities


def collect_npm_identity(config: Config) -> NpmIdentity:
    """The canonical built tarball for the prepared version."""
    return collect_npm_identities(config)[0]

