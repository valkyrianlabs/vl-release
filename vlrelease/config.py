"""`release.toml`: the per-repository release contract.

The loader is deliberately strict: unknown keys are errors (a typo must not silently disable a
safety check), paths must stay inside the repository, and the schema is versioned.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from vlrelease import __version__
from vlrelease.errors import ConfigError
from vlrelease.gitutil import find_repo_root
from vlrelease.semver import Version, satisfies

CONFIG_FILENAME = "release.toml"
SUPPORTED_SCHEMA_VERSIONS = (1,)
TARGET_KINDS = ("file", "meson", "package_json", "pyproject", "regex", "homebrew")
HOMEBREW_SOURCES = ("release-asset", "tag-archive")
NPM_PACKERS = ("npm", "pnpm")
NPM_AUTH_MODES = ("oidc", "token", "basic")
NPM_ACCESS = ("public", "restricted")
_NPM_REGISTRY_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Tags npm accepts and that cannot be mistaken for a semver range.
_NPM_DIST_TAG = re.compile(r"[a-z][a-z0-9._-]*")
# npm package names: lowercase, optionally scoped, at most 214 characters (length checked by npm itself).
_NPM_PACKAGE_NAME = re.compile(r"(?:@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]*")
_PACKAGE_NAME = re.compile(r"[a-z0-9][a-z0-9+.-]+")
_DEBIAN_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9+.-]*")
_REPOSITORY_SLUG = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class TargetSpec:
    kind: str
    path: str
    pattern: str | None = None

    def describe(self) -> str:
        return self.path if self.kind == "file" else f"{self.path} ({self.kind})"


@dataclass(frozen=True)
class ProjectConfig:
    name: str
    package: str
    repository: str | None = None


@dataclass(frozen=True)
class VersionConfig:
    canonical: TargetSpec
    targets: tuple[TargetSpec, ...] = ()

    @property
    def all_targets(self) -> tuple[TargetSpec, ...]:
        return (self.canonical, *self.targets)


@dataclass(frozen=True)
class ReleaseConfig:
    branch: str = "main"
    remote: str = "origin"
    tag: str = "v{version}"
    title: str = "v{version} — {title}"
    notes: str = "RELEASE_NOTES.md"
    notes_next: str = ".release/RELEASE_NOTES_NEXT.md"
    changelog_next: str = ".release/CHANGELOG_NEXT.md"
    output_dir: str = "release"
    build_dir: str = "build"
    test_command: tuple[str, ...] = ()
    cut_commit_message: str = "chore(release): v{version}"
    finalize_commit_message: str = "chore(release): record v{version}"


@dataclass(frozen=True)
class IdenticalFile:
    member: str
    source: str


@dataclass(frozen=True)
class DebianPackageContract:
    name: str
    architecture: str | None = None
    required_paths: tuple[str, ...] = ()
    forbidden_paths: tuple[str, ...] = ()
    any_of: tuple[tuple[str, ...], ...] = ()
    identical_files: tuple[IdenticalFile, ...] = ()


@dataclass(frozen=True)
class DebianConfig:
    changelog: str = "debian/changelog"
    distribution: str = "unstable"
    urgency: str = "medium"
    maintainer: str | None = None
    revision: int = 1
    build_command: tuple[str, ...] = ("dpkg-buildpackage", "-us", "-uc", "-b")
    pre_build: tuple[tuple[str, ...], ...] = ()
    build_excludes: tuple[str, ...] = ()
    extra_artifacts: tuple[str, ...] = ()
    packages: tuple[DebianPackageContract, ...] = ()


@dataclass(frozen=True)
class SourceArchiveConfig:
    name: str = "{package}-{version}.tar.gz"
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class AptConfig:
    repository_url: str = ""
    suite: str = "stable"
    components: tuple[str, ...] = ("main",)
    architectures: tuple[str, ...] = ("amd64",)
    verify_timeout: float = 600.0
    verify_interval: float = 15.0


@dataclass(frozen=True)
class NpmAlias:
    """Another npm name the same build is published under (`[[npm.aliases]]`).

    The alias tarball is derived from the canonical one: identical members and bytes, except the
    package.json `name` and, in each `rewrite` member, every occurrence of the canonical name.
    """

    name: str
    rewrite: tuple[str, ...] = ()


@dataclass(frozen=True)
class NpmConfig:
    """The npm package built from this repository (`vlr build-npm`)."""

    package_dir: str = "."
    packer: str = "npm"
    pre_pack: tuple[tuple[str, ...], ...] = ()
    required_paths: tuple[str, ...] = ()
    forbidden_paths: tuple[str, ...] = ()
    any_of: tuple[tuple[str, ...], ...] = ()
    identical_files: tuple[IdenticalFile, ...] = ()
    dist_tag: str = "latest"
    maintenance_dist_tag: str = "maintenance"
    access: str = "public"
    aliases: tuple[NpmAlias, ...] = ()


@dataclass(frozen=True)
class NpmRegistryConfig:
    """One `[[publish.npm]]` registry. Credentials always come from the environment."""

    name: str
    auth: str
    registry: str = ""
    registry_env: str | None = None
    token_env: str = "NPM_TOKEN"
    username_env: str | None = None
    password_env: str | None = None
    provenance: bool = False
    verify_timeout: float = 300.0
    verify_interval: float = 10.0


@dataclass(frozen=True)
class HomebrewConfig:
    formula: str
    tap: str
    tap_branch: str = "main"
    tap_path: str = ""
    source: str = "release-asset"


@dataclass(frozen=True)
class Config:
    root: Path
    path: Path
    schema_version: int
    tool_requires: str | None
    project: ProjectConfig
    version: VersionConfig
    release: ReleaseConfig
    debian: DebianConfig | None = None
    source_archive: SourceArchiveConfig | None = None
    apt: AptConfig | None = None
    homebrew: HomebrewConfig | None = None
    npm: NpmConfig | None = None
    npm_registries: tuple[NpmRegistryConfig, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def resolve(self, relative: str) -> Path:
        return self.root / relative

    def tag_for(self, version: Version | str) -> str:
        return self.release.tag.format(version=version)

    @property
    def output_dir(self) -> Path:
        return self.resolve(self.release.output_dir)

    @property
    def build_dir(self) -> Path:
        return self.resolve(self.release.build_dir)


# --- discovery ------------------------------------------------------------------------------------


def discover_root(repo: str | os.PathLike[str] | None, cwd: Path | None = None) -> Path:
    """The Git work-tree root to operate on. Never searches above it."""
    start = Path(repo).expanduser() if repo is not None else (cwd or Path.cwd())
    if not start.exists():
        raise ConfigError(f"--repo path does not exist: {start}")
    resolved = start.resolve()
    root = find_repo_root(resolved if resolved.is_dir() else resolved.parent)
    if root is None:
        where = f"--repo {start}" if repo is not None else f"the current directory ({start})"
        raise ConfigError(f"{where} is not inside a Git repository; vl-release operates on a Git work tree.")
    return root


def load_config(root: Path) -> Config:
    path = root / CONFIG_FILENAME
    if not path.is_file():
        raise ConfigError(
            f"No {CONFIG_FILENAME} at the repository root {root}. "
            "Run `vlr init` to scaffold one, or pass --repo to select another repository."
        )
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    return parse_config(data, root=root, path=path)


def load_project(repo: str | os.PathLike[str] | None = None, cwd: Path | None = None) -> Config:
    return load_config(discover_root(repo, cwd))


# --- parsing --------------------------------------------------------------------------------------


class _Table:
    """Key-checked access to one TOML table; unknown keys are reported by `finish()`."""

    def __init__(self, data: Any, where: str) -> None:
        if not isinstance(data, dict):
            raise ConfigError(f"{where} must be a table")
        self.data = data
        self.where = where
        self.seen: set[str] = set()

    def _get(self, key: str) -> Any:
        self.seen.add(key)
        return self.data.get(key)

    def has(self, key: str) -> bool:
        return key in self.data

    def string(self, key: str, default: str | None = None, *, required: bool = False) -> str | None:
        value = self._get(key)
        if value is None:
            if required:
                raise ConfigError(f"{self.where}.{key} is required")
            return default
        if not isinstance(value, str) or (required and not value.strip()):
            raise ConfigError(f"{self.where}.{key} must be a non-empty string")
        return value

    def boolean(self, key: str, default: bool) -> bool:
        value = self._get(key)
        if value is None:
            return default
        if not isinstance(value, bool):
            raise ConfigError(f"{self.where}.{key} must be true or false")
        return value

    def integer(self, key: str, default: int) -> int:
        value = self._get(key)
        if value is None:
            return default
        if not isinstance(value, int) or isinstance(value, bool):
            raise ConfigError(f"{self.where}.{key} must be an integer")
        return value

    def number(self, key: str, default: float) -> float:
        value = self._get(key)
        if value is None:
            return default
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise ConfigError(f"{self.where}.{key} must be a positive number")
        return float(value)

    def strings(self, key: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
        value = self._get(key)
        if value is None:
            return default
        if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
            raise ConfigError(f"{self.where}.{key} must be a list of non-empty strings")
        return tuple(value)

    def command(self, key: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
        return self.strings(key, default)

    def commands(self, key: str) -> tuple[tuple[str, ...], ...]:
        value = self._get(key)
        if value is None:
            return ()
        if not isinstance(value, list) or not all(
            isinstance(cmd, list) and cmd and all(isinstance(arg, str) for arg in cmd) for cmd in value
        ):
            raise ConfigError(f"{self.where}.{key} must be a list of commands (each a non-empty list of strings)")
        return tuple(tuple(cmd) for cmd in value)

    def table(self, key: str) -> "_Table | None":
        value = self._get(key)
        if value is None:
            return None
        return _Table(value, f"{self.where}.{key}" if self.where else key)

    def raw(self, key: str) -> Any:
        return self._get(key)

    def finish(self) -> None:
        unknown = sorted(set(self.data) - self.seen)
        if unknown:
            location = self.where or CONFIG_FILENAME
            raise ConfigError(f"{location}: unknown key(s): {', '.join(unknown)}")


def _relative_path(value: str, where: str) -> str:
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or ".." in candidate.parts or not value.strip():
        raise ConfigError(f"{where} must be a relative path inside the repository, got {value!r}")
    return str(candidate)


def _parse_target(raw: Any, where: str) -> TargetSpec:
    if isinstance(raw, str):
        return TargetSpec(kind="file", path=_relative_path(raw, where))
    table = _Table(raw, where)
    kind = table.string("kind", required=True)
    if kind not in TARGET_KINDS:
        raise ConfigError(f"{where}.kind must be one of {', '.join(TARGET_KINDS)}, got {kind!r}")
    path = _relative_path(table.string("path", required=True) or "", f"{where}.path")
    pattern = table.string("pattern")
    if kind == "regex":
        if not pattern:
            raise ConfigError(f"{where}.pattern is required for kind = \"regex\"")
        try:
            compiled = re.compile(pattern, re.MULTILINE)
        except re.error as exc:
            raise ConfigError(f"{where}.pattern is not a valid regular expression: {exc}") from exc
        if "version" not in compiled.groupindex:
            raise ConfigError(f"{where}.pattern must contain a named group (?P<version>...)")
    elif pattern is not None:
        raise ConfigError(f"{where}.pattern is only valid for kind = \"regex\"")
    table.finish()
    return TargetSpec(kind=kind, path=path, pattern=pattern)


def _check_format(template: str, where: str, allowed: set[str], required: set[str]) -> None:
    names = set(re.findall(r"{(\w+)}", template))
    unknown = names - allowed
    if unknown:
        raise ConfigError(f"{where} uses unknown placeholder(s) {sorted(unknown)}; allowed: {sorted(allowed)}")
    missing = required - names
    if missing:
        raise ConfigError(f"{where} must contain {', '.join('{' + m + '}' for m in sorted(missing))}")


def parse_config(data: dict[str, Any], *, root: Path, path: Path) -> Config:
    top = _Table(data, "")
    schema_version = top.raw("schema_version")
    if schema_version is None:
        raise ConfigError(f"{path}: schema_version is required (current schema: {SUPPORTED_SCHEMA_VERSIONS[-1]})")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ConfigError(
            f"{path}: unsupported schema_version {schema_version!r}; vl-release {__version__} supports "
            f"{', '.join(map(str, SUPPORTED_SCHEMA_VERSIONS))}. Upgrade vl-release."
        )

    tool_requires = None
    tool = top.table("tool")
    if tool is not None:
        tool_requires = tool.string("requires")
        tool.finish()
    if tool_requires:
        try:
            ok = satisfies(Version.parse(__version__), tool_requires)
        except ValueError as exc:
            raise ConfigError(f"tool.requires: {exc}") from exc
        if not ok:
            raise ConfigError(
                f"This repository requires vl-release {tool_requires}, but the running version is {__version__}. "
                "Install a matching vl-release (or run it from a matching source checkout)."
            )

    project_table = top.table("project")
    if project_table is None:
        raise ConfigError("[project] is required")
    name = project_table.string("name", required=True) or ""
    package = project_table.string("package", name) or name
    repository = project_table.string("repository")
    project_table.finish()
    if not _PACKAGE_NAME.fullmatch(package):
        raise ConfigError(f"project.package {package!r} must be a lowercase package name ([a-z0-9][a-z0-9+.-]+)")
    if repository is not None and not _REPOSITORY_SLUG.fullmatch(repository):
        raise ConfigError(f"project.repository must be a GitHub `owner/name` slug, got {repository!r}")
    project = ProjectConfig(name=name, package=package, repository=repository)

    version_table = top.table("version")
    if version_table is None:
        raise ConfigError("[version] is required")
    canonical_raw = version_table.raw("canonical")
    if canonical_raw is None:
        raise ConfigError("version.canonical is required (e.g. canonical = \"VERSION\")")
    canonical = _parse_target(canonical_raw, "version.canonical")
    targets_raw = version_table.raw("targets") or []
    if not isinstance(targets_raw, list):
        raise ConfigError("version.targets must be a list")
    targets = tuple(_parse_target(item, f"version.targets[{index}]") for index, item in enumerate(targets_raw))
    version_table.finish()
    seen_paths: set[str] = set()
    for spec in (canonical, *targets):
        if spec.path in seen_paths and spec.kind != "regex":
            raise ConfigError(f"version target {spec.path} is listed more than once")
        seen_paths.add(spec.path)

    release_table = top.table("release") or _Table({}, "release")
    defaults = ReleaseConfig()
    release = ReleaseConfig(
        branch=release_table.string("branch", defaults.branch) or defaults.branch,
        remote=release_table.string("remote", defaults.remote) or defaults.remote,
        tag=release_table.string("tag", defaults.tag) or defaults.tag,
        title=release_table.string("title", defaults.title) or defaults.title,
        notes=_relative_path(release_table.string("notes", defaults.notes) or "", "release.notes"),
        notes_next=_relative_path(release_table.string("notes_next", defaults.notes_next) or "", "release.notes_next"),
        changelog_next=_relative_path(
            release_table.string("changelog_next", defaults.changelog_next) or "", "release.changelog_next"
        ),
        output_dir=_relative_path(release_table.string("output_dir", defaults.output_dir) or "", "release.output_dir"),
        build_dir=_relative_path(release_table.string("build_dir", defaults.build_dir) or "", "release.build_dir"),
        test_command=release_table.command("test_command"),
        cut_commit_message=release_table.string("cut_commit_message", defaults.cut_commit_message)
        or defaults.cut_commit_message,
        finalize_commit_message=release_table.string("finalize_commit_message", defaults.finalize_commit_message)
        or defaults.finalize_commit_message,
    )
    release_table.finish()
    _check_format(release.tag, "release.tag", {"version"}, {"version"})
    _check_format(release.title, "release.title", {"version", "tag", "title", "name"}, {"title"})
    _check_format(release.cut_commit_message, "release.cut_commit_message", {"version", "tag", "name"}, {"version"})
    _check_format(
        release.finalize_commit_message, "release.finalize_commit_message", {"version", "tag", "name"}, {"version"}
    )

    debian = _parse_debian(top.table("debian"), project)
    source_archive = _parse_source_archive(top.table("source_archive"))
    apt = None
    npm_registries: tuple[NpmRegistryConfig, ...] = ()
    publish = top.table("publish")
    if publish is not None:
        apt = _parse_apt(publish.table("apt"))
        npm_registries = _parse_npm_registries(publish.raw("npm"))
        publish.finish()
    homebrew = _parse_homebrew(top.table("homebrew"), project)
    npm = _parse_npm(top.table("npm"))
    top.finish()

    if npm_registries and npm is None:
        raise ConfigError("[[publish.npm]] requires [npm] (it publishes the tarball that `vlr build-npm` builds)")

    if apt is not None and debian is None:
        raise ConfigError("[publish.apt] requires [debian] to be enabled (APT publishes the built .deb files)")
    if homebrew is not None and homebrew.source == "release-asset":
        if source_archive is None:
            raise ConfigError(
                "[homebrew] source = \"release-asset\" requires [source_archive] (the formula points at that asset)"
            )
        if project.repository is None:
            raise ConfigError("[homebrew] requires project.repository to build the release asset URL")
    if homebrew is not None and homebrew.source == "tag-archive" and project.repository is None:
        raise ConfigError("[homebrew] requires project.repository to build the tag archive URL")

    return Config(
        root=root,
        path=path,
        schema_version=schema_version,
        tool_requires=tool_requires,
        project=project,
        version=VersionConfig(canonical=canonical, targets=targets),
        release=release,
        debian=debian,
        source_archive=source_archive,
        apt=apt,
        homebrew=homebrew,
        npm=npm,
        npm_registries=npm_registries,
        raw=data,
    )


def _parse_debian(table: _Table | None, project: ProjectConfig) -> DebianConfig | None:
    if table is None:
        return None
    enabled = table.boolean("enabled", True)
    defaults = DebianConfig()
    maintainer = table.string("maintainer")
    config = DebianConfig(
        changelog=_relative_path(table.string("changelog", defaults.changelog) or "", "debian.changelog"),
        distribution=table.string("distribution", defaults.distribution) or defaults.distribution,
        urgency=table.string("urgency", defaults.urgency) or defaults.urgency,
        maintainer=maintainer,
        revision=table.integer("revision", defaults.revision),
        build_command=table.command("build_command", defaults.build_command),
        pre_build=table.commands("pre_build"),
        build_excludes=tuple(
            _relative_path(item, "debian.build_excludes") for item in table.strings("build_excludes")
        ),
        extra_artifacts=table.strings("extra_artifacts"),
        packages=_parse_packages(table.raw("packages")),
    )
    table.finish()
    if config.revision < 1:
        raise ConfigError("debian.revision must be >= 1")
    for key in ("distribution", "urgency"):
        if not _DEBIAN_TOKEN.fullmatch(getattr(config, key)):
            raise ConfigError(f"debian.{key} {getattr(config, key)!r} is not a valid Debian changelog token")
    if not config.build_command:
        raise ConfigError("debian.build_command must not be empty")
    if not enabled:
        return None
    if not config.packages:
        raise ConfigError(
            "[debian] needs at least one [[debian.packages]] contract (name plus the paths the package must ship)"
        )
    return config


def _parse_any_of(table: _Table, where: str) -> tuple[tuple[str, ...], ...]:
    any_of_raw = table.raw("any_of") or []
    if not isinstance(any_of_raw, list) or not all(
        isinstance(group, list) and group and all(isinstance(p, str) for p in group) for group in any_of_raw
    ):
        raise ConfigError(f"{where}.any_of must be a list of non-empty path lists")
    return tuple(tuple(_normalize_member(p) for p in group) for group in any_of_raw)


def _parse_identical_files(table: _Table, where: str) -> tuple[IdenticalFile, ...]:
    identical_raw = table.raw("identical_files") or []
    if not isinstance(identical_raw, list):
        raise ConfigError(f"{where}.identical_files must be a list of {{ member, source }} tables")
    identical: list[IdenticalFile] = []
    for position, entry in enumerate(identical_raw):
        entry_table = _Table(entry, f"{where}.identical_files[{position}]")
        member = entry_table.string("member", required=True) or ""
        source = entry_table.string("source", required=True) or ""
        entry_table.finish()
        identical.append(
            IdenticalFile(
                member=_normalize_member(member),
                source=_relative_path(source, f"{where}.identical_files[{position}].source"),
            )
        )
    return tuple(identical)


def _parse_packages(raw: Any) -> tuple[DebianPackageContract, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError("debian.packages must be an array of tables ([[debian.packages]])")
    contracts: list[DebianPackageContract] = []
    for index, item in enumerate(raw):
        where = f"debian.packages[{index}]"
        table = _Table(item, where)
        name = table.string("name", required=True) or ""
        if not _PACKAGE_NAME.fullmatch(name):
            raise ConfigError(f"{where}.name {name!r} is not a valid Debian package name")
        contracts.append(
            DebianPackageContract(
                name=name,
                architecture=table.string("architecture"),
                required_paths=tuple(_normalize_member(p) for p in table.strings("required_paths")),
                forbidden_paths=tuple(_normalize_member(p) for p in table.strings("forbidden_paths")),
                any_of=_parse_any_of(table, where),
                identical_files=_parse_identical_files(table, where),
            )
        )
        table.finish()
    names = [contract.name for contract in contracts]
    if len(names) != len(set(names)):
        raise ConfigError("debian.packages lists the same package more than once")
    return tuple(contracts)


def _normalize_member(path: str) -> str:
    return normalize_member(path)


def normalize_member(path: str) -> str:
    """Package/archive member path without leading `./` or `/` (keeps dot-files intact)."""
    member = path.strip()
    while member.startswith("./"):
        member = member[2:]
    return member.lstrip("/")


def _parse_source_archive(table: _Table | None) -> SourceArchiveConfig | None:
    if table is None:
        return None
    enabled = table.boolean("enabled", True)
    defaults = SourceArchiveConfig()
    config = SourceArchiveConfig(
        name=table.string("name", defaults.name) or defaults.name,
        exclude=table.strings("exclude"),
    )
    table.finish()
    _check_format(config.name, "source_archive.name", {"package", "version", "tag"}, {"version"})
    if not config.name.endswith(".tar.gz") or "/" in config.name:
        raise ConfigError("source_archive.name must be a plain file name ending in .tar.gz")
    return config if enabled else None


def _parse_apt(table: _Table | None) -> AptConfig | None:
    if table is None:
        return None
    enabled = table.boolean("enabled", True)
    defaults = AptConfig()
    config = AptConfig(
        repository_url=table.string("repository_url", "") or "",
        suite=table.string("suite", defaults.suite) or defaults.suite,
        components=table.strings("components", defaults.components),
        architectures=table.strings("architectures", defaults.architectures),
        verify_timeout=table.number("verify_timeout", defaults.verify_timeout),
        verify_interval=table.number("verify_interval", defaults.verify_interval),
    )
    table.finish()
    return config if enabled else None


def _parse_homebrew(table: _Table | None, project: ProjectConfig) -> HomebrewConfig | None:
    if table is None:
        return None
    enabled = table.boolean("enabled", True)
    formula = _relative_path(table.string("formula", required=True) or "", "homebrew.formula")
    tap = table.string("tap", required=True) or ""
    source = table.string("source", "release-asset") or "release-asset"
    config = HomebrewConfig(
        formula=formula,
        tap=tap,
        tap_branch=table.string("tap_branch", "main") or "main",
        tap_path=_relative_path(
            table.string("tap_path", f"Formula/{PurePosixPath(formula).name}") or "", "homebrew.tap_path"
        ),
        source=source,
    )
    table.finish()
    if not _REPOSITORY_SLUG.fullmatch(tap):
        raise ConfigError(f"homebrew.tap must be a GitHub `owner/name` slug, got {tap!r}")
    if source not in HOMEBREW_SOURCES:
        raise ConfigError(f"homebrew.source must be one of {', '.join(HOMEBREW_SOURCES)}")
    if not formula.endswith(".rb"):
        raise ConfigError("homebrew.formula must be a Ruby formula file (*.rb)")
    return config if enabled else None


def _parse_npm(table: _Table | None) -> NpmConfig | None:
    if table is None:
        return None
    enabled = table.boolean("enabled", True)
    defaults = NpmConfig()
    config = NpmConfig(
        package_dir=_relative_path(table.string("package_dir", defaults.package_dir) or ".", "npm.package_dir"),
        packer=table.string("packer", defaults.packer) or defaults.packer,
        pre_pack=table.commands("pre_pack"),
        required_paths=tuple(_normalize_member(p) for p in table.strings("required_paths")),
        forbidden_paths=tuple(_normalize_member(p) for p in table.strings("forbidden_paths")),
        any_of=_parse_any_of(table, "npm"),
        identical_files=_parse_identical_files(table, "npm"),
        dist_tag=table.string("dist_tag", defaults.dist_tag) or defaults.dist_tag,
        maintenance_dist_tag=table.string("maintenance_dist_tag", defaults.maintenance_dist_tag)
        or defaults.maintenance_dist_tag,
        access=table.string("access", defaults.access) or defaults.access,
        aliases=_parse_npm_aliases(table.raw("aliases")),
    )
    table.finish()
    if config.packer not in NPM_PACKERS:
        raise ConfigError(f"npm.packer must be one of {', '.join(NPM_PACKERS)}, got {config.packer!r}")
    if config.access not in NPM_ACCESS:
        raise ConfigError(f"npm.access must be one of {', '.join(NPM_ACCESS)}, got {config.access!r}")
    for key in ("dist_tag", "maintenance_dist_tag"):
        if not _NPM_DIST_TAG.fullmatch(getattr(config, key)):
            raise ConfigError(f"npm.{key} {getattr(config, key)!r} must be a lowercase npm dist-tag such as \"latest\"")
    if config.dist_tag == config.maintenance_dist_tag:
        raise ConfigError("npm.maintenance_dist_tag must differ from npm.dist_tag")
    for pattern in (*config.required_paths, *config.forbidden_paths, *(p for group in config.any_of for p in group)):
        if not pattern.startswith("package/"):
            raise ConfigError(f"npm contract path {pattern!r} must start with \"package/\" (npm tarball members do)")
    for pair in config.identical_files:
        if not pair.member.startswith("package/"):
            raise ConfigError(f"npm.identical_files member {pair.member!r} must start with \"package/\"")
    return config if enabled else None


def _parse_npm_aliases(raw: Any) -> tuple[NpmAlias, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError("npm.aliases must be an array of tables ([[npm.aliases]], one per extra package name)")
    aliases: list[NpmAlias] = []
    for index, item in enumerate(raw):
        where = f"npm.aliases[{index}]"
        table = _Table(item, where)
        alias = NpmAlias(
            name=table.string("name", required=True) or "",
            rewrite=tuple(_normalize_member(p) for p in table.strings("rewrite")),
        )
        table.finish()
        if not _NPM_PACKAGE_NAME.fullmatch(alias.name):
            raise ConfigError(f"{where}.name {alias.name!r} is not a valid npm package name")
        for member in alias.rewrite:
            if not member.startswith("package/") or member == "package/package.json" or "*" in member:
                raise ConfigError(
                    f"{where}.rewrite member {member!r} must be an exact tarball path under \"package/\" "
                    "(package.json is always renamed)"
                )
        aliases.append(alias)
    names = [alias.name for alias in aliases]
    if len(names) != len(set(names)):
        raise ConfigError("npm.aliases lists the same package name more than once")
    return tuple(aliases)


def _parse_npm_registries(raw: Any) -> tuple[NpmRegistryConfig, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError("publish.npm must be an array of tables ([[publish.npm]], one per registry)")
    registries: list[NpmRegistryConfig] = []
    for index, item in enumerate(raw):
        where = f"publish.npm[{index}]"
        table = _Table(item, where)
        enabled = table.boolean("enabled", True)
        defaults = NpmRegistryConfig(name="", auth="")
        config = NpmRegistryConfig(
            name=table.string("name", required=True) or "",
            auth=table.string("auth", required=True) or "",
            registry=table.string("registry", "") or "",
            registry_env=table.string("registry_env"),
            token_env=table.string("token_env", defaults.token_env) or defaults.token_env,
            username_env=table.string("username_env"),
            password_env=table.string("password_env"),
            provenance=table.boolean("provenance", defaults.provenance),
            verify_timeout=table.number("verify_timeout", defaults.verify_timeout),
            verify_interval=table.number("verify_interval", defaults.verify_interval),
        )
        table.finish()
        if not _NPM_REGISTRY_NAME.fullmatch(config.name):
            raise ConfigError(f"{where}.name {config.name!r} must be lowercase letters, digits and dashes")
        if config.auth not in NPM_AUTH_MODES:
            raise ConfigError(f"{where}.auth must be one of {', '.join(NPM_AUTH_MODES)}, got {config.auth!r}")
        if config.auth == "basic" and not (config.username_env and config.password_env):
            raise ConfigError(f"{where}: auth = \"basic\" needs username_env and password_env (the variables holding them)")
        if config.auth != "basic" and (config.username_env or config.password_env):
            raise ConfigError(f"{where}: username_env/password_env only apply to auth = \"basic\"")
        if not config.registry and not config.registry_env:
            raise ConfigError(f"{where} needs `registry` (a URL) or `registry_env` (the environment variable holding it)")
        if config.registry and not re.fullmatch(r"https?://[^\s/]+(/\S*)?", config.registry):
            raise ConfigError(f"{where}.registry must be an absolute http(s) URL, got {config.registry!r}")
        for key in ("registry_env", "token_env", "username_env", "password_env"):
            value = getattr(config, key)
            if value is not None and not _ENV_NAME.fullmatch(value):
                raise ConfigError(f"{where}.{key} {value!r} is not an environment variable name")
        if enabled:
            registries.append(config)
    names = [registry.name for registry in registries]
    if len(names) != len(set(names)):
        raise ConfigError("publish.npm lists the same registry name more than once")
    return tuple(registries)
