"""`vlr install-skill`: install the repository-local agent skill describing this workflow.

Formats (both use the SKILL.md frontmatter convention):
  claude  -> .claude/skills/vl-release/SKILL.md   (Claude Code)
  agents  -> .agents/skills/vl-release/SKILL.md   (Codex and other AGENTS-style tools)

`auto` installs `claude`, plus `agents` when the repository already has an `.agents/` directory.
Generated files carry a marker; reruns update them in place, and a file without the marker
(hand-written) is never overwritten unless --force is given.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from string import Template

from vlrelease import __version__
from vlrelease.config import Config
from vlrelease.errors import ReleaseError
from vlrelease.fsutil import atomic_write_text

SKILL_NAME = "vl-release"
FORMATS = {"claude": ".claude/skills", "agents": ".agents/skills"}
GENERATED_MARKER = re.compile(r"<!-- vl-release:generated version=\S+ ")


@dataclass(frozen=True)
class SkillInstall:
    format: str
    path: Path
    status: str  # installed | updated | unchanged | stale | missing | refused


def resolve_formats(config: Config, requested: str) -> list[str]:
    if requested == "all":
        return list(FORMATS)
    if requested == "auto":
        formats = ["claude"]
        if (config.root / ".agents").is_dir():
            formats.append("agents")
        return formats
    if requested not in FORMATS:
        raise ReleaseError(f"Unknown skill format {requested!r}; choose auto, all, {', '.join(FORMATS)}")
    return [requested]


def render_skill(config: Config) -> str:
    template = Template(resources.files("vlrelease.skill").joinpath("SKILL.md.in").read_text(encoding="utf-8"))
    debian = config.debian is not None
    changelog_section = (
        f"- **`{config.release.changelog_next}`**: concise, technical, package-facing changes for the next\n"
        "  release. One `- ` bullet per change; optional `## Section` headings; indented continuation\n"
        "  lines and one level of nested `  - ` detail bullets. `vlr prepare` renders it into a proper\n"
        f"  `{config.debian.changelog}` stanza, so write no Debian boilerplate.\n"
        if debian and config.debian is not None
        else ""
    )
    channels = []
    if config.apt is not None:
        channels.append("the .deb to the APT repository")
    if config.source_archive is not None or config.homebrew is not None:
        channels.append("the GitHub release")
    if config.homebrew is not None:
        channels.append(f"the Homebrew formula to {config.homebrew.tap}")
    targets = [spec.path for spec in config.version.targets]
    staged = [config.release.changelog_next] if debian else []
    staged.append(config.release.notes_next)
    return template.substitute(
        project_name=config.project.name,
        tool_version=__version__,
        staged_docs=", ".join(staged),
        notes_next=config.release.notes_next,
        notes=config.release.notes,
        changelog_section=changelog_section,
        debian_history_clause=f" and `{config.debian.changelog}`" if config.debian is not None else "",
        canonical=config.version.canonical.path,
        targets_clause=f" ({', '.join(f'`{t}`' for t in targets)})" if targets else "",
        branch=config.release.branch,
        tag_example=config.tag_for("X.Y.Z"),
        build_clause=" (`vlr build-deb`, `vlr validate-artifacts`)" if debian else "",
        publish_clause=f" ({'; '.join(channels)})" if channels else "",
    )


def install_skill(config: Config, *, requested: str = "auto", force: bool = False, check: bool = False) -> list[SkillInstall]:
    content = render_skill(config)
    results: list[SkillInstall] = []
    for fmt in resolve_formats(config, requested):
        path = config.root / FORMATS[fmt] / SKILL_NAME / "SKILL.md"
        if path.is_file():
            current = path.read_text(encoding="utf-8")
            if current == content:
                results.append(SkillInstall(fmt, path, "unchanged"))
                continue
            if not GENERATED_MARKER.search(current) and not force:
                results.append(SkillInstall(fmt, path, "refused"))
                continue
            if check:
                results.append(SkillInstall(fmt, path, "stale"))
                continue
            atomic_write_text(path, content)
            results.append(SkillInstall(fmt, path, "updated"))
        else:
            if check:
                results.append(SkillInstall(fmt, path, "missing"))
                continue
            atomic_write_text(path, content)
            results.append(SkillInstall(fmt, path, "installed"))
    return results
