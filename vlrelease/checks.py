"""`vlr check`: one aggregated verdict on the repository's release contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath

from vlrelease import debchangelog
from vlrelease.config import Config
from vlrelease.errors import ReleaseError
from vlrelease.state import RELEASABLE_PHASES, ReleaseState, read_release_state
from vlrelease.targets import HOMEBREW_SHA256_PATTERN, HOMEBREW_URL_PATTERN


@dataclass
class CheckReport:
    state: ReleaseState
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict:
        return {"ok": self.ok, "errors": self.errors, "warnings": self.warnings, "state": self.state.as_dict()}


def check_repository(config: Config, *, release: bool = False, tag: str | None = None) -> CheckReport:
    state = read_release_state(config)
    report = CheckReport(state=state, errors=list(state.errors), warnings=list(state.warnings))

    if config.debian is not None:
        control = config.resolve("debian/control")
        if not control.is_file():
            report.errors.append("debian/control is missing ([debian] is enabled)")
        else:
            source = debchangelog.read_control_source(control)
            if state.debian_top is not None and source and state.debian_top.source != source:
                report.errors.append(
                    f"{config.debian.changelog} is for source package {state.debian_top.source}, "
                    f"but debian/control declares {source}"
                )
            declared = set(debchangelog.read_control_packages(control))
            for contract in config.debian.packages:
                if contract.name not in declared:
                    report.errors.append(f"[[debian.packages]] {contract.name} is not a Package in debian/control")

    if config.homebrew is not None:
        formula = config.resolve(config.homebrew.formula)
        if not formula.is_file():
            report.errors.append(f"Homebrew formula template {config.homebrew.formula} is missing")
        else:
            text = formula.read_text(encoding="utf-8")
            if not HOMEBREW_URL_PATTERN.search(text) or not HOMEBREW_SHA256_PATTERN.search(text):
                report.errors.append(f"{config.homebrew.formula} needs top-level `url` and `sha256` lines")

    if config.npm is not None:
        from vlrelease.npmpkg import PACKAGE_JSON, package_names, read_manifest, require_publishable_manifest

        manifest_path = (PurePosixPath(config.npm.package_dir) / PACKAGE_JSON).as_posix()
        try:
            manifest = read_manifest(config)
            require_publishable_manifest(manifest, manifest_path)
            package_names(config, manifest)
        except ReleaseError as exc:
            report.errors.append(str(exc))
        if manifest_path not in {spec.path for spec in config.version.all_targets}:
            report.errors.append(
                f"{manifest_path} is not a version target; add {{ kind = \"package_json\", path = \"{manifest_path}\" }} "
                "(or make it the canonical version) so the npm package always carries the release version"
            )

    from vlrelease.skill import install_local_skill, install_skill

    for install in install_skill(config.root, check=True):
        if install.status in ("missing", "stale"):
            report.warnings.append(
                f"agent skill {install.path.relative_to(config.root)} is {install.status}; run `vlr install-skill`"
            )
    for install in install_local_skill(config, check=True):
        if install.status in ("missing", "stale"):
            report.warnings.append(
                f"project skill context {install.path.relative_to(config.root)} is {install.status}; "
                "run `vlr install-local-skill`"
            )

    if release:
        if state.phase not in RELEASABLE_PHASES and not report.errors:
            report.errors.append(
                f"not releasable: phase is `{state.phase}` (needs `pending`: a new version with staged docs, "
                "or `prepared`: an idempotent re-run)"
            )
        if tag is not None and state.version is not None:
            expected = config.tag_for(state.version)
            if tag.removeprefix("refs/tags/") != expected:
                report.errors.append(f"release tag {tag} does not match the canonical version (expected {expected})")
    elif tag is not None and state.version is not None and tag.removeprefix("refs/tags/") != config.tag_for(state.version):
        report.errors.append(f"tag {tag} does not match the canonical version (expected {config.tag_for(state.version)})")
    return report
