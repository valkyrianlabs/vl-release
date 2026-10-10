"""Agent skills.

`vlr install-skill` installs the generic base skill (`SKILL.md`): what vl-release is, how to set
a repository up (`vlr init`, `release.toml`), the development workflow and releasing. It needs
only a Git repository, so it is the first thing installed when adopting vl-release.

`vlr install-local-skill` extends an installed base skill with `PROJECT.md`, rendered from the
repository's `release.toml` (paths, version targets, channels, contracts, checks). The base skill
tells agents to read it, and to generate it when it is missing.

Formats (both use the SKILL.md frontmatter convention):
  claude  -> .claude/skills/vl-release/   (Claude Code)
  agents  -> .agents/skills/vl-release/   (Codex and other AGENTS-style tools)

Generated files carry a marker; reruns update them in place, and a file without the marker
(hand-written) is never overwritten unless --force is given.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from string import Template

from vlrelease.config import Config
from vlrelease.errors import ReleaseError
from vlrelease.fsutil import atomic_write_text

SKILL_NAME = "vl-release"
BASE_FILE = "SKILL.md"
LOCAL_FILE = "PROJECT.md"
FORMATS = {"claude": ".claude/skills", "agents": ".agents/skills"}
# Markers identify generated files. They carry no tool version on purpose: a vl-release upgrade
# (or this repository's own version bump) must not make an unchanged skill look stale.
# The older `version=X.Y.Z` form is still recognized so those files are updated, not refused.
BASE_MARKER = re.compile(r"<!-- vl-release:generated(?: version=\S+)? ")
LOCAL_MARKER = re.compile(r"<!-- vl-release:generated-local(?: version=\S+)? ")


@dataclass(frozen=True)
class SkillInstall:
    format: str
    path: Path
    status: str  # installed | updated | unchanged | stale | missing | refused | no-base


def skill_dir(root: Path, fmt: str) -> Path:
    return root / FORMATS[fmt] / SKILL_NAME


def _template(name: str) -> Template:
    return Template(resources.files("vlrelease.skill").joinpath(name).read_text(encoding="utf-8"))


def render_base_skill() -> str:
    return _template("SKILL.md.in").substitute()


def _base_formats(root: Path, requested: str) -> list[str]:
    if requested == "all":
        return list(FORMATS)
    if requested == "auto":
        formats = ["claude"]
        if (root / ".agents").is_dir():
            formats.append("agents")
        return formats
    if requested not in FORMATS:
        raise ReleaseError(f"Unknown skill format {requested!r}; choose auto, all, {', '.join(FORMATS)}")
    return [requested]


def _write(path: Path, content: str, marker: re.Pattern[str], fmt: str, *, force: bool, check: bool) -> SkillInstall:
    if path.is_file():
        current = path.read_text(encoding="utf-8")
        if current == content:
            return SkillInstall(fmt, path, "unchanged")
        if not marker.search(current) and not force:
            return SkillInstall(fmt, path, "refused")
        if check:
            return SkillInstall(fmt, path, "stale")
        atomic_write_text(path, content)
        return SkillInstall(fmt, path, "updated")
    if check:
        return SkillInstall(fmt, path, "missing")
    atomic_write_text(path, content)
    return SkillInstall(fmt, path, "installed")


def install_skill(root: Path, *, requested: str = "auto", force: bool = False, check: bool = False) -> list[SkillInstall]:
    """Install/update the generic base skill. Needs only the repository root."""
    content = render_base_skill()
    return [
        _write(skill_dir(root, fmt) / BASE_FILE, content, BASE_MARKER, fmt, force=force, check=check)
        for fmt in _base_formats(root, requested)
    ]


def installed_formats(root: Path) -> list[str]:
    return [fmt for fmt in FORMATS if (skill_dir(root, fmt) / BASE_FILE).is_file()]


def render_local_skill(config: Config) -> str:
    debian = config.debian
    staged_rows = f"| Staged release notes (keep current) | `{config.release.notes_next}` |\n"
    if debian is not None:
        staged_rows = f"| Staged changelog (keep current) | `{config.release.changelog_next}` |\n" + staged_rows
    debian_history_row = f"| Published Debian changelog (do not edit) | `{debian.changelog}` |\n" if debian else ""

    targets = [f"`{spec.path}` ({spec.kind})" for spec in config.version.targets]
    channels: list[str] = []
    if debian is not None:
        channels.append(f"- **Debian packages** (built by `vlr build-deb` from `debian/`, revision {debian.revision}):")
        for contract in debian.packages:
            details = [f"architecture `{contract.architecture}`"] if contract.architecture else []
            if contract.required_paths:
                details.append("must ship " + ", ".join(f"`{p}`" for p in contract.required_paths))
            if contract.forbidden_paths:
                details.append("must never ship " + ", ".join(f"`{p}`" for p in contract.forbidden_paths))
            if contract.identical_files:
                details.append(
                    "ships byte-identical copies of " + ", ".join(f"`{pair.source}`" for pair in contract.identical_files)
                )
            channels.append(f"  - `{contract.name}`" + (": " + "; ".join(details) if details else ""))
        if debian.pre_build:
            channels.append("  - pre-build: " + "; ".join(f"`{' '.join(cmd)}`" for cmd in debian.pre_build))
    if config.apt is not None:
        where = config.apt.repository_url or "the configured APT repository"
        channels.append(
            f"- **APT**: {where} (suite `{config.apt.suite}`, components {', '.join(config.apt.components)}), "
            "published by release CI with `vlr publish-deb`"
        )
    if config.npm is not None:
        npm = config.npm
        details = [f"packed with `{npm.packer}` from `{npm.package_dir}`", f"dist-tag `{npm.dist_tag}`"]
        if npm.pre_pack:
            details.append("pre-pack: " + "; ".join(f"`{' '.join(cmd)}`" for cmd in npm.pre_pack))
        if npm.aliases:
            details.append("also published as " + ", ".join(f"`{alias.name}`" for alias in npm.aliases) + " (`[[npm.aliases]]`, derived from the same tarball)")
        channels.append("- **npm package** (built by `vlr build-npm`): " + ", ".join(details))
        for registry in config.npm_registries:
            where = registry.registry or f"the URL in `{registry.registry_env}`"
            channels.append(f"  - registry `{registry.name}`: {where} (auth `{registry.auth}`), published with `vlr publish-npm`")
    if config.source_archive is not None:
        channels.append("- **Source archive** of the prepared tree, attached to the GitHub release")
    if config.homebrew is not None:
        channels.append(
            f"- **Homebrew**: formula template `{config.homebrew.formula}`, published to "
            f"`{config.homebrew.tap}` as `{config.homebrew.tap_path}`"
        )
    if not channels:
        channels.append("- No publication channels are configured; releases are tags plus release notes.")

    checks = ["vlr check"]
    if config.release.test_command:
        checks.append(" ".join(config.release.test_command))
    policy = config.policy
    version_example = "X.Y.Z" if policy.name == "semver" else policy.example
    policy_rows = ""  # semver repositories render exactly as before
    if policy.name == "debian-upstream":
        policy_rows = (
            "\n- Version policy `debian-upstream`: the version is `UPSTREAM-REVISION` (e.g. `8.0.2-1`), ordered like "
            "Debian versions; it is also the Debian package version.\n"
            "  - packaging-only change: `vlr version bump revision` / `vlr cut revision` (`8.0.2-1` -> `8.0.2-2`)\n"
            "  - new upstream release: `vlr version upstream X.Y.Z` (revision restarts at 1), then "
            "`vlr cut X.Y.Z-1`\n"
            "  - never `patch|minor|major` (refused as ambiguous)"
        )
    return _template("PROJECT.md.in").substitute(
        policy_rows=policy_rows,
        project_name=config.project.name,
        staged_rows=staged_rows,
        notes=config.release.notes,
        debian_history_row=debian_history_row,
        canonical=config.version.canonical.path,
        targets=", ".join(targets) if targets else "no other files",
        tag_example=config.tag_for(version_example),
        branch=config.release.branch,
        title_example=config.release.title.format(
            version=version_example, tag=config.tag_for(version_example), title="<title>", name=config.project.name
        ),
        channels="\n".join(channels),
        checks="\n".join(checks),
    )


def install_local_skill(
    config: Config, *, requested: str = "auto", force: bool = False, check: bool = False
) -> list[SkillInstall]:
    """Write PROJECT.md next to every installed base skill (or the requested format)."""
    if requested == "auto":
        formats = installed_formats(config.root)
        if not formats:
            if check:
                return [SkillInstall("claude", skill_dir(config.root, "claude") / LOCAL_FILE, "no-base")]
            raise ReleaseError("No base skill is installed; run `vlr install-skill` first.")
    else:
        formats = _base_formats(config.root, requested)
    content = render_local_skill(config)
    results: list[SkillInstall] = []
    for fmt in formats:
        directory = skill_dir(config.root, fmt)
        if not (directory / BASE_FILE).is_file():
            if check:
                results.append(SkillInstall(fmt, directory / LOCAL_FILE, "no-base"))
                continue
            raise ReleaseError(f"No base skill at {directory / BASE_FILE}; run `vlr install-skill --format {fmt}` first.")
        results.append(_write(directory / LOCAL_FILE, content, LOCAL_MARKER, fmt, force=force, check=check))
    return results
