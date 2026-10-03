"""Inspect and validate the release output directory before anything is published."""

from __future__ import annotations

import gzip
import io
import re
import subprocess
import tarfile
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

from vlrelease import debchangelog
from vlrelease.checksums import release_assets, sha256_file, verify_sha256sums
from vlrelease.config import Config, DebianPackageContract, normalize_member
from vlrelease.errors import ReleaseError
from vlrelease.state import read_release_state

DEB_NAME = re.compile(r"^(?P<package>[a-z0-9][a-z0-9+.-]*)_(?P<version>[^_]+)_(?P<arch>[a-z0-9-]+)\.deb$")


@dataclass(frozen=True)
class DebIdentity:
    path: Path
    package: str
    version: str
    architecture: str
    sha256: str


def identify_deb(path: Path) -> DebIdentity:
    match = DEB_NAME.fullmatch(path.name)
    if not match:
        raise ReleaseError(f"{path.name} is not a canonical `<package>_<version>_<arch>.deb` file name")
    return DebIdentity(path, match.group("package"), match.group("version"), match.group("arch"), sha256_file(path))


def deb_files(output_dir: Path) -> tuple[Path, ...]:
    return tuple(path for path in release_assets(output_dir) if path.name.endswith(".deb"))


class DebContents:
    """Members of a .deb's data archive, read through `dpkg-deb --fsys-tarfile`."""

    def __init__(self, path: Path) -> None:
        try:
            completed = subprocess.run(
                ["dpkg-deb", "--fsys-tarfile", str(path)], capture_output=True, check=False
            )
        except FileNotFoundError as exc:
            raise ReleaseError("`dpkg-deb` is required to inspect packages (install dpkg).") from exc
        if completed.returncode != 0:
            raise ReleaseError(
                f"Cannot read {path.name}: {completed.stderr.decode('utf-8', 'replace').strip() or 'dpkg-deb failed'}"
            )
        self._files: dict[str, bytes] = {}
        self.members: set[str] = set()
        with tarfile.open(fileobj=io.BytesIO(completed.stdout), mode="r:*") as archive:
            for info in archive.getmembers():
                name = normalize_member(info.name).rstrip("/")
                if not name:
                    continue
                self.members.add(name)
                if info.isfile():
                    extracted = archive.extractfile(info)
                    if extracted is not None:
                        self._files[name] = extracted.read()

    def read(self, member: str) -> bytes | None:
        return self._files.get(member)

    def matches(self, pattern: str) -> list[str]:
        return sorted(member for member in self.members if fnmatchcase(member, pattern))


def deb_control_field(path: Path, field: str) -> str | None:
    completed = subprocess.run(["dpkg-deb", "-f", str(path), field], capture_output=True, text=True, check=False)
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else None


def check_package_contract(
    config: Config, contract: DebianPackageContract, identity: DebIdentity, contents: DebContents, debian_version: str
) -> list[str]:
    issues: list[str] = []
    name = identity.path.name
    if identity.version != debian_version:
        issues.append(f"{name}: version {identity.version} is not the prepared Debian version {debian_version}")
    if contract.architecture and identity.architecture != contract.architecture:
        issues.append(f"{name}: architecture {identity.architecture}, expected {contract.architecture}")
    for field, expected in (("Package", identity.package), ("Version", identity.version)):
        actual = deb_control_field(identity.path, field)
        if actual != expected:
            issues.append(f"{name}: control field {field}={actual!r} does not match the file name ({expected})")
    for pattern in contract.required_paths:
        if not contents.matches(pattern):
            issues.append(f"{name}: required path missing: {pattern}")
    for pattern in contract.forbidden_paths:
        found = contents.matches(pattern)
        if found:
            issues.append(f"{name}: forbidden path present: {pattern} ({', '.join(found[:3])})")
    for group in contract.any_of:
        if not any(contents.matches(pattern) for pattern in group):
            issues.append(f"{name}: none of the alternative paths is present: {', '.join(group)}")
    for pair in contract.identical_files:
        packaged = contents.read(pair.member)
        source = config.resolve(pair.source)
        if packaged is None:
            issues.append(f"{name}: {pair.member} is missing (expected a copy of {pair.source})")
        elif not source.is_file():
            issues.append(f"{name}: identity source {pair.source} does not exist in the work tree")
        elif packaged != source.read_bytes():
            issues.append(f"{name}: {pair.member} differs from the work tree's {pair.source}")
    doc_dir = f"usr/share/doc/{identity.package}"
    changelog = contents.read(f"{doc_dir}/changelog.Debian.gz") or contents.read(f"{doc_dir}/changelog.gz")
    if changelog is None:
        issues.append(f"{name}: {doc_dir}/changelog.Debian.gz is missing")
    else:
        try:
            top = debchangelog.parse_top_entry(gzip.decompress(changelog).decode("utf-8"))
        except (OSError, ValueError) as exc:
            issues.append(f"{name}: packaged changelog is unreadable: {exc}")
        else:
            if top is None or top.version != debian_version:
                issues.append(
                    f"{name}: packaged changelog starts at {top.version if top else 'nothing'}, not the prepared "
                    f"{debian_version}; the package was not built from the prepared work tree"
                )
    return issues



def validate_source_archive(config: Config, path: Path) -> list[str]:
    from vlrelease.sourcearchive import archive_prefix, prepared_documents

    issues: list[str] = []
    state = read_release_state(config)
    prefix = archive_prefix(config, str(state.version))
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            names = {info.name: info for info in archive.getmembers()}
            for relative in prepared_documents(config):
                member = f"{prefix}/{relative}"
                info = names.get(member)
                if info is None or not info.isfile():
                    issues.append(f"{path.name}: {member} is missing")
                    continue
                extracted = archive.extractfile(info)
                if extracted is None or extracted.read() != config.resolve(relative).read_bytes():
                    issues.append(f"{path.name}: {relative} differs from the prepared work tree")
            for name in names:
                if name != prefix and not name.startswith(prefix + "/"):
                    issues.append(f"{path.name}: member {name} is outside the {prefix}/ prefix")
                    break
    except (OSError, tarfile.TarError) as exc:
        issues.append(f"{path.name}: unreadable gzip tar archive: {exc}")
    return issues


@dataclass
class ValidationReport:
    issues: list[str]
    checked: list[str]

    @property
    def ok(self) -> bool:
        return not self.issues


def validate_artifacts(config: Config) -> ValidationReport:
    output_dir = config.output_dir
    issues: list[str] = []
    checked: list[str] = []
    if not output_dir.is_dir():
        return ValidationReport([f"output directory {output_dir} does not exist"], [])
    state = read_release_state(config)
    if state.phase != "prepared" or state.version is None:
        issues.append(f"the work tree is not prepared (phase {state.phase}); validate after `vlr prepare` + build")
        return ValidationReport(issues, checked)
    issues.extend(verify_sha256sums(output_dir))

    if config.debian is not None:
        debian_version = state.debian_top.version if state.debian_top else ""
        contracts = {contract.name: contract for contract in config.debian.packages}
        seen: dict[str, Path] = {}
        for path in deb_files(output_dir):
            try:
                identity = identify_deb(path)
            except ReleaseError as exc:
                issues.append(str(exc))
                continue
            contract = contracts.get(identity.package)
            if contract is None:
                issues.append(f"{path.name}: package {identity.package} has no [[debian.packages]] contract")
                continue
            if identity.package in seen:
                issues.append(f"{path.name}: more than one .deb for {identity.package}")
                continue
            seen[identity.package] = path
            issues.extend(check_package_contract(config, contract, identity, DebContents(path), debian_version))
            checked.append(path.name)
        for name in contracts:
            if name not in seen:
                issues.append(f"no .deb for configured package {name} ({debian_version}) in {output_dir}")

    if config.source_archive is not None:
        from vlrelease.sourcearchive import archive_name

        archive = output_dir / archive_name(config, str(state.version))
        if not archive.is_file():
            issues.append(f"source archive {archive.name} is missing (run `vlr source-archive`)")
        else:
            issues.extend(validate_source_archive(config, archive))
            checked.append(archive.name)

    if config.homebrew is not None:
        from vlrelease.homebrew import validate_rendered_formula

        problems, formula_name = validate_rendered_formula(config)
        issues.extend(problems)
        if formula_name:
            checked.append(formula_name)
    return ValidationReport(issues, checked)


def artifact_info(config: Config) -> list[dict]:
    rows: list[dict] = []
    for path in release_assets(config.output_dir):
        row: dict = {"name": path.name, "sha256": sha256_file(path), "size": path.stat().st_size}
        match = DEB_NAME.fullmatch(path.name)
        if match:
            row.update(kind="deb", package=match.group("package"), version=match.group("version"), architecture=match.group("arch"))
        elif path.name.endswith(".tar.gz"):
            row["kind"] = "source-archive"
        elif path.name.endswith((".changes", ".buildinfo")):
            row["kind"] = "debian-metadata"
        else:
            row["kind"] = "other"
        rows.append(row)
    return rows
