"""Command-line interface. `vl-release` and `vlr` are the same program."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

from vlrelease import __version__
from vlrelease.config import Config, discover_root, load_config
from vlrelease.errors import EXIT_FAILURE, EXIT_OK, EXIT_USAGE, ReleaseError
from vlrelease.gitutil import local_tag_exists


def _prog() -> str:
    name = Path(sys.argv[0]).name if sys.argv and sys.argv[0] else ""
    return name if name in ("vl-release", "vlr") else "vlr"


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _log(args: argparse.Namespace) -> Callable[[str], None]:
    """Progress lines go to stderr when stdout carries JSON."""
    if getattr(args, "json", False):
        return lambda line: print(line, file=sys.stderr)
    return print


def _config(args: argparse.Namespace) -> Config:
    return load_config(discover_root(args.repo))


def _github_output(values: dict[str, Any]) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    if not target:
        raise ReleaseError("--github-output needs the GITHUB_OUTPUT environment variable (set by GitHub Actions)")
    with open(target, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            text = "" if value is None else (str(value).lower() if isinstance(value, bool) else str(value))
            if "\n" in text:
                delimiter = f"VLR_EOF_{os.urandom(8).hex()}"
                handle.write(f"{key}<<{delimiter}\n{text}\n{delimiter}\n")
            else:
                handle.write(f"{key}={text}\n")


# --- commands -------------------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    from vlrelease.scaffold import scaffold

    root = discover_root(args.repo)
    debian = True if args.debian else (False if args.no_debian else None)
    created = scaffold(root, name=args.name, package=args.package, debian=debian)
    for path in created:
        print(f"created {path}")
    if not created:
        print("nothing to do: the contract files already exist")
    print("next: fill in release.toml (`vlr help config`), run `vlr check`, then `vlr install-local-skill`")
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    from vlrelease.doctor import run_doctor

    config: Config | None = None
    config_error: str | None = None
    try:
        root = discover_root(args.repo)
        if (root / "release.toml").is_file():
            config = load_config(root)
    except ReleaseError as exc:
        if args.repo is not None or (Path.cwd() / "release.toml").exists():
            config_error = str(exc)
    checks = run_doctor(config, strict=args.strict)
    failed = [check for check in checks if check.required and not check.ok]
    if args.json:
        _print_json(
            {
                "ok": not failed and config_error is None,
                "checks": [check.as_dict() for check in checks],
                "repository": str(config.root) if config else None,
                "config_error": config_error,
            }
        )
    else:
        for check in checks:
            mark = "ok " if check.ok else ("ERR" if check.required else "-- ")
            print(f"[{mark}] {check.name:<18} {check.detail}")
        if config is not None:
            print(f"repository: {config.root} ({config.project.name})")
        elif config_error:
            print(f"repository: {config_error}")
        else:
            print("repository: none (run inside a Git repository with a release.toml for project checks)")
        print("status: ok" if not failed and config_error is None else "status: problems found")
    return EXIT_OK if not failed and config_error is None else EXIT_FAILURE


def cmd_check(args: argparse.Namespace) -> int:
    from vlrelease.checks import check_repository

    report = check_repository(_config(args), release=args.release, tag=args.tag)
    if args.json:
        _print_json(report.as_dict())
    else:
        state = report.state
        print(f"version: {state.version}  phase: {state.phase}")
        for warning in report.warnings:
            print(f"warning: {warning}")
        for error in report.errors:
            print(f"error: {error}")
        print("check: ok" if report.ok else "check: FAILED")
    return EXIT_OK if report.ok else EXIT_FAILURE


def cmd_status(args: argparse.Namespace) -> int:
    from vlrelease.prepare import format_release_title
    from vlrelease.state import read_release_state

    config = _config(args)
    state = read_release_state(config)
    payload = state.as_dict()
    title = None
    if state.notes_top is not None and state.version is not None and state.notes_top.version == str(state.version):
        title = state.notes_top.title
    elif state.staged_notes is not None:
        title = state.staged_notes.title
    payload.update(
        project=config.project.name,
        package=config.project.package,
        repository=config.project.repository,
        branch=config.release.branch,
        tag_exists_locally=bool(state.tag and local_tag_exists(config.root, state.tag)),
        release_title=format_release_title(config, version=str(state.version), title=title) if title and state.version else None,
        channels={
            "debian": config.debian is not None,
            "apt": config.apt is not None,
            "source_archive": config.source_archive is not None,
            "homebrew": config.homebrew is not None,
        },
    )
    if args.github_output:
        _github_output(
            {
                "version": payload["version"],
                "tag": payload["tag"],
                "phase": payload["phase"],
                "debian_version": payload["debian_top"],
                "release_title": payload["release_title"],
                "debian": payload["channels"]["debian"],
                "apt": payload["channels"]["apt"],
                "homebrew": payload["channels"]["homebrew"],
            }
        )
    if args.json:
        _print_json(payload)
    else:
        print(f"project:   {config.project.name} ({config.project.package})")
        print(f"version:   {payload['version']}  tag: {payload['tag']} ({'exists' if payload['tag_exists_locally'] else 'not tagged'} locally)")
        print(f"phase:     {payload['phase']}")
        print(f"last:      {payload['last_recorded'] or 'no release recorded yet'}")
        staged = payload["staged"]
        print(
            f"staged:    release notes: {'yes' if staged['release_notes'] else 'empty'}"
            + (f", changelog: {'yes' if staged['changelog'] else 'empty'}" if staged["changelog"] is not None else "")
        )
        if payload["release_title"]:
            print(f"title:     {payload['release_title']}")
        for warning in state.warnings:
            print(f"warning: {warning}")
        for error in state.errors:
            print(f"error: {error}")
    return EXIT_OK if not state.errors else EXIT_FAILURE


def cmd_version(args: argparse.Namespace) -> int:
    from vlrelease import versioning

    config = _config(args)
    action = args.version_action
    args.json = getattr(args, "json", False)
    if action in (None, "show", "check"):
        state = versioning.read_state(config)
        if args.json:
            _print_json(
                {
                    "version": str(state.canonical) if state.canonical else None,
                    "ok": state.ok,
                    "targets": [reading.as_dict() for reading in state.readings],
                    "issues": list(state.issues),
                }
            )
        elif action == "show":
            print(state.canonical if state.canonical else "unknown")
        else:
            for reading in state.readings:
                print(f"{reading.spec.describe():<40} {reading.version or ('ERROR: ' + str(reading.error))}")
            for issue in state.issues:
                print(f"error: {issue}")
            print("versions: ok" if state.ok else "versions: INCONSISTENT")
        if action == "show":
            return EXIT_OK if state.canonical else EXIT_FAILURE
        return EXIT_OK if state.ok else EXIT_FAILURE
    if action == "sync":
        version, changed = versioning.sync_versions(config, dry_run=args.dry_run)
        new, old = version, version
    else:
        target = args.target if action == "set" else args.part
        new, old, changed = versioning.set_version(config, target, dry_run=args.dry_run)
    if args.json:
        _print_json({"version": str(new), "previous": str(old) if old else None, "changed": changed, "dry_run": args.dry_run})
    else:
        verb = "would update" if args.dry_run else "updated"
        print(f"{old} -> {new}" if old != new else f"version {new}")
        for path in changed:
            print(f"  {verb} {path}")
        if not changed:
            print("  all targets already up to date")
    return EXIT_OK


def cmd_prepare(args: argparse.Namespace) -> int:
    from vlrelease.prepare import prepare_release

    config = _config(args)
    result = prepare_release(
        config, allow_empty_patch=args.allow_empty_patch, debian_revision=args.debian_revision, dry_run=args.dry_run
    )
    if args.record and not args.dry_run:
        record_path = Path(args.record)
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(json.dumps(result.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.json:
        payload = result.as_dict()
        if args.dry_run:
            payload["preview"] = result.preview
        _print_json(payload)
        return EXIT_OK
    if result.status == "already-prepared":
        print(f"{result.version} is already prepared ({result.release_title}); nothing to do")
        return EXIT_OK
    if args.dry_run:
        for path, content in result.preview.items():
            print(f"--- {path} (would write) ---")
            print(content if len(content) < 4000 else content[:4000] + "\n[...]")
        return EXIT_OK
    print(f"prepared {result.version}: {result.release_title}")
    for path in result.changed:
        print(f"  wrote {path}")
    return EXIT_OK


def cmd_release_title(args: argparse.Namespace) -> int:
    from vlrelease.prepare import current_entry, format_release_title, staged_entry

    config = _config(args)
    entry = staged_entry(config) if args.staged else current_entry(config)
    title = format_release_title(config, version=entry.version, title=entry.title)
    if args.json:
        _print_json({"version": entry.version, "tag": config.tag_for(entry.version), "title": entry.title, "release_title": title})
    else:
        print(title)
    return EXIT_OK


def cmd_release_body(args: argparse.Namespace) -> int:
    from vlrelease.prepare import current_entry, staged_entry

    config = _config(args)
    entry = staged_entry(config) if args.staged else current_entry(config)
    body = entry.body.rstrip() + "\n"
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(body, encoding="utf-8")
    else:
        sys.stdout.write(body)
    return EXIT_OK


def cmd_build_deb(args: argparse.Namespace) -> int:
    from vlrelease.debbuild import build_debian

    result = build_debian(_config(args), clean=not args.no_clean, dry_run=args.dry_run)
    if args.json:
        _print_json(result.as_dict())
    else:
        print(f"{'would build' if args.dry_run else 'built'} {result.source} {result.debian_version} in {result.build_tree}")
        for artifact in result.artifacts:
            print(f"  {artifact.relative_to(result.output_dir.parent) if artifact.is_relative_to(result.output_dir.parent) else artifact}")
    return EXIT_OK


def cmd_source_archive(args: argparse.Namespace) -> int:
    from vlrelease.checksums import sha256_file
    from vlrelease.sourcearchive import build_source_archive

    path = build_source_archive(_config(args))
    if args.json:
        _print_json({"file": path.name, "path": str(path), "sha256": sha256_file(path)})
    else:
        print(f"wrote {path} sha256={sha256_file(path)}")
    return EXIT_OK


def cmd_checksums(args: argparse.Namespace) -> int:
    from vlrelease.checksums import verify_sha256sums, write_sha256sums

    config = _config(args)
    if args.verify:
        issues = verify_sha256sums(config.output_dir)
        for issue in issues:
            print(f"error: {issue}")
        print("checksums: ok" if not issues else "checksums: FAILED")
        return EXIT_OK if not issues else EXIT_FAILURE
    try:
        path = write_sha256sums(config.output_dir)
    except ValueError as exc:
        raise ReleaseError(str(exc)) from exc
    print(path.read_text(encoding="utf-8"), end="")
    return EXIT_OK


def cmd_validate_artifacts(args: argparse.Namespace) -> int:
    from vlrelease.artifacts import validate_artifacts

    report = validate_artifacts(_config(args))
    if args.json:
        _print_json({"ok": report.ok, "checked": report.checked, "issues": report.issues})
    else:
        for name in report.checked:
            print(f"checked {name}")
        for issue in report.issues:
            print(f"error: {issue}")
        print("artifacts: ok" if report.ok else "artifacts: FAILED")
    return EXIT_OK if report.ok else EXIT_FAILURE


def cmd_artifacts(args: argparse.Namespace) -> int:
    from vlrelease.artifacts import artifact_info

    rows = artifact_info(_config(args))
    if args.json:
        _print_json({"artifacts": rows})
    else:
        for row in rows:
            print(f"{row['sha256']}  {row['size']:>10}  {row['kind']:<16} {row['name']}")
    return EXIT_OK


def cmd_publish_deb(args: argparse.Namespace) -> int:
    from vlrelease.publish import publish_debs

    result = publish_debs(
        _config(args),
        mode=args.mode,
        dry_run=args.dry_run,
        require_enabled=args.require_enabled,
        allow_older_version=args.allow_older_version,
        verify_timeout=args.verify_timeout,
        log=_log(args),
    )
    if args.json:
        _print_json(result.as_dict())
    elif result.skipped_reason:
        print(f"publication skipped: {result.skipped_reason}")
    else:
        print(f"publication {'planned (dry run)' if result.dry_run else 'verified'}")
    return EXIT_OK


def cmd_verify_published(args: argparse.Namespace) -> int:
    from vlrelease.publish import verify_published

    identities = verify_published(_config(args), timeout=args.timeout, log=_log(args))
    if args.json:
        _print_json({"verified": [{"package": i.package, "version": i.version, "sha256": i.sha256} for i in identities]})
    return EXIT_OK


def cmd_github_release(args: argparse.Namespace) -> int:
    from vlrelease.github_release import publish_github_release

    result = publish_github_release(_config(args), repository=args.repository, dry_run=args.dry_run, log=_log(args))
    if args.json:
        _print_json(result.as_dict())
    else:
        print(
            f"GitHub release {result.tag} ({result.repository}): "
            f"{'created' if result.created else 'exists'}{', notes updated' if result.edited else ''}; "
            f"uploaded {len(result.uploaded)}, unchanged {len(result.skipped)}"
        )
    return EXIT_OK


def cmd_homebrew(args: argparse.Namespace) -> int:
    from vlrelease import homebrew

    config = _config(args)
    if args.homebrew_action == "formula":
        path = homebrew.render_formula(config, sha256=args.sha256, fetch=args.fetch_sha256)
        facts = homebrew.formula_facts(path.read_text(encoding="utf-8"))
        if args.json:
            _print_json({"path": str(path), "url": facts.url, "sha256": facts.sha256, "version": facts.version})
        else:
            print(f"wrote {path}\n  url    {facts.url}\n  sha256 {facts.sha256}")
        return EXIT_OK
    result = homebrew.publish_tap(
        config,
        Path(args.tap_dir).resolve(),
        push=not args.no_push,
        remote=args.remote,
        verify_url=not args.no_verify_url,
        allow_older=args.allow_older,
        dry_run=args.dry_run,
        log=_log(args),
    )
    if args.json:
        _print_json(result.as_dict())
    else:
        print(f"{result.formula} {result.version}: {result.status}")
    return EXIT_OK


def cmd_cut(args: argparse.Namespace) -> int:
    from vlrelease.cut import cut_release

    result = cut_release(_config(args), args.target, push=args.push, skip_tests=args.skip_tests, fetch=not args.no_fetch, log=_log(args))
    if args.json:
        _print_json(result.as_dict())
    else:
        for action in result.actions:
            print(f"  {action}")
        print(f"{result.tag} at {result.commit[:12]}{' pushed' if result.pushed else ''}")
    return EXIT_OK


def cmd_finalize(args: argparse.Namespace) -> int:
    from vlrelease.finalize import finalize_release

    record = None
    if args.record:
        record = json.loads(Path(args.record).read_text(encoding="utf-8")).get("record")
        if not isinstance(record, dict) or not record:
            raise ReleaseError(f"{args.record} carries no prepare record (write it with `vlr prepare --record`)")
    result = finalize_release(
        _config(args), remote=args.remote, branch=args.branch, record=record, dry_run=args.dry_run, log=_log(args)
    )
    if args.json:
        _print_json(result.as_dict())
    else:
        print(f"{result.version}: {result.status}" + (f" ({result.commit[:12]})" if result.commit else ""))
        for path in result.changed:
            print(f"  {path}")
    return EXIT_OK


def _report_skill_results(results, root: Path, *, check: bool) -> int:
    failed = False
    for item in results:
        relative = item.path.relative_to(root)
        if item.status == "refused":
            print(f"refused  {relative}: not generated by vl-release (use --force to replace it)")
            failed = True
        elif item.status == "no-base":
            print(f"no-base  {relative}: install the base skill first (`vlr install-skill`)")
            failed = True
        else:
            print(f"{item.status:<9} {relative}")
            failed = failed or (check and item.status in ("stale", "missing"))
    return EXIT_FAILURE if failed else EXIT_OK


def cmd_install_skill(args: argparse.Namespace) -> int:
    from vlrelease.skill import install_skill

    root = discover_root(args.repo)
    code = _report_skill_results(install_skill(root, requested=args.format, force=args.force, check=args.check), root, check=args.check)
    if not args.check and code == EXIT_OK:
        if (root / "release.toml").is_file():
            print("next: `vlr install-local-skill` adds this repository's specifics (PROJECT.md)")
        else:
            print("next: this repository has no release.toml yet; see the skill's setup section (`vlr init`)")
    return code


def cmd_install_local_skill(args: argparse.Namespace) -> int:
    from vlrelease.skill import install_local_skill

    config = _config(args)
    results = install_local_skill(config, requested=args.format, force=args.force, check=args.check)
    return _report_skill_results(results, config.root, check=args.check)


HELP_TOPICS = {
    "config": ("config.md", "release.toml reference"),
    "staging": ("staging.md", "staged release documents, history files and how prepare promotes them"),
    "ci": ("ci.md", "the release CI transaction and workflow conventions"),
}


def cmd_help(args: argparse.Namespace) -> int:
    from importlib import resources

    if args.topic is None:
        print("vlr help TOPIC   (offline references shipped with vl-release)\n")
        for name, (_file, summary) in HELP_TOPICS.items():
            print(f"  {name:<8} {summary}")
        print("\nCommand usage: vlr --help, vlr COMMAND --help")
        return EXIT_OK
    filename, _summary = HELP_TOPICS[args.topic]
    sys.stdout.write(resources.files("vlrelease.docs").joinpath(filename).read_text(encoding="utf-8"))
    return EXIT_OK


# --- parser ---------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=_prog(),
        description="vl-release: deterministic release toolkit (versions, staged release docs, packaging, publication).",
    )
    parser.add_argument("--version", action="version", version=f"vl-release {__version__}")
    parser.add_argument("--repo", metavar="PATH", help="operate on the Git repository containing PATH (default: cwd)")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    def add(name: str, func: Callable[[argparse.Namespace], int], help_text: str, *, json_flag: bool = True) -> argparse.ArgumentParser:
        command = sub.add_parser(name, help=help_text, description=help_text)
        command.set_defaults(func=func)
        if json_flag:
            command.add_argument("--json", action="store_true", help="machine-readable output on stdout")
        return command

    p = add("init", cmd_init, "create release.toml and the .release/ staging files (never overwrites)", json_flag=False)
    p.add_argument("--name")
    p.add_argument("--package")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--debian", action="store_true", help="include a [debian] section")
    group.add_argument("--no-debian", action="store_true")

    p = add("doctor", cmd_doctor, "check the toolchain (works outside a repository)")
    p.add_argument("--strict", action="store_true", help="require the tools for every enabled capability")

    p = add("check", cmd_check, "validate the repository's release contract")
    p.add_argument("--release", action="store_true", help="also require a releasable state (CI gate before prepare)")
    p.add_argument("--tag", help="require this tag to match the canonical version")

    p = add("status", cmd_status, "show version, release phase and staged-doc state")
    p.add_argument("--github-output", action="store_true", help="also write key=value pairs to $GITHUB_OUTPUT")

    p = add("version", cmd_version, "inspect or change the version across all configured targets", json_flag=False)
    vsub = p.add_subparsers(dest="version_action", metavar="ACTION")
    for name, help_text in (("show", "print the canonical version"), ("check", "verify every target matches")):
        vp = vsub.add_parser(name, help=help_text)
        vp.add_argument("--json", action="store_true")
    vp = vsub.add_parser("sync", help="write the canonical version into every other target")
    vp.add_argument("--dry-run", action="store_true")
    vp.add_argument("--json", action="store_true")
    vp = vsub.add_parser("set", help="set an explicit version everywhere")
    vp.add_argument("target", metavar="X.Y.Z")
    vp.add_argument("--dry-run", action="store_true")
    vp.add_argument("--json", action="store_true")
    vp = vsub.add_parser("bump", help="bump patch|minor|major everywhere")
    vp.add_argument("part", choices=("patch", "minor", "major"))
    vp.add_argument("--dry-run", action="store_true")
    vp.add_argument("--json", action="store_true")

    p = add("prepare", cmd_prepare, "promote the staged release docs into the history files (work tree only)")
    p.add_argument("--dry-run", action="store_true", help="show what would be written")
    p.add_argument("--allow-empty-patch", action="store_true", help="permit an empty patch release (maintenance entry)")
    p.add_argument("--debian-revision", type=int, help="Debian revision for the new stanza (default: release.toml)")
    p.add_argument("--record", metavar="FILE", help="write the prepare result (with file digests) as JSON")

    p = add("release-title", cmd_release_title, "the GitHub release title of the prepared release")
    p.add_argument("--staged", action="store_true", help="from the staged notes instead of the prepared history")
    p = add("release-body", cmd_release_body, "the GitHub release body of the prepared release", json_flag=False)
    p.add_argument("--staged", action="store_true", help="from the staged notes instead of the prepared history")
    p.add_argument("--output", metavar="FILE", help="write to FILE instead of stdout")

    p = add("build-deb", cmd_build_deb, "build the Debian packages from the prepared work tree (inside build/)")
    p.add_argument("--no-clean", action="store_true", help="keep existing files in the output directory")
    p.add_argument("--dry-run", action="store_true")
    add("source-archive", cmd_source_archive, "write the reproducible source archive of the prepared work tree")
    p = add("checksums", cmd_checksums, "write (or --verify) SHA256SUMS for the output directory", json_flag=False)
    p.add_argument("--verify", action="store_true")
    add("validate-artifacts", cmd_validate_artifacts, "validate checksums, package contracts and staged assets")
    add("artifacts", cmd_artifacts, "list release assets with their sha256")

    p = add("publish-deb", cmd_publish_deb, "publish the .deb files to the APT repository (idempotent, verified)")
    p.add_argument("--mode", choices=("disabled", "nexus"), help="override RELEASE_PUBLISH_MODE")
    p.add_argument("--dry-run", action="store_true", help="plan against the live index without uploading")
    p.add_argument("--require-enabled", action="store_true", help="fail if publication is disabled")
    p.add_argument("--allow-older-version", action="store_true")
    p.add_argument("--verify-timeout", type=float, metavar="SECONDS")
    p = add("verify-published", cmd_verify_published, "wait until the APT index lists the built .deb files by sha256")
    p.add_argument("--timeout", type=float, metavar="SECONDS")

    p = add("github-release", cmd_github_release, "create/update the GitHub release and upload assets (idempotent)")
    p.add_argument("--repository", metavar="OWNER/NAME")
    p.add_argument("--dry-run", action="store_true")

    p = add("homebrew", cmd_homebrew, "render the release formula / publish it to the tap", json_flag=False)
    hsub = p.add_subparsers(dest="homebrew_action", metavar="ACTION", required=True)
    hp = hsub.add_parser("formula", help="render the formula for the prepared release")
    hp.add_argument("--sha256", help="expected archive sha256")
    hp.add_argument("--fetch-sha256", action="store_true", help="download the archive to compute its sha256")
    hp.add_argument("--json", action="store_true")
    hp = hsub.add_parser("publish", help="commit the rendered formula to a tap checkout and push it")
    hp.add_argument("--tap-dir", required=True, metavar="DIR", help="a Git checkout of the tap repository")
    hp.add_argument("--remote", default="origin")
    hp.add_argument("--no-push", action="store_true")
    hp.add_argument("--no-verify-url", action="store_true", help="skip downloading the url to verify its sha256")
    hp.add_argument("--allow-older", action="store_true")
    hp.add_argument("--dry-run", action="store_true")
    hp.add_argument("--json", action="store_true")

    p = add("cut", cmd_cut, "bump, commit and tag a release (push with --push); resumable")
    p.add_argument("target", metavar="patch|minor|major|X.Y.Z")
    p.add_argument("--push", action="store_true")
    p.add_argument("--skip-tests", action="store_true")
    p.add_argument("--no-fetch", action="store_true")

    p = add("finalize", cmd_finalize, "after publication: commit the prepared history + reset staging to the branch")
    p.add_argument("--remote")
    p.add_argument("--branch")
    p.add_argument("--record", metavar="FILE", help="require the prepared docs to match this `prepare --record` file")
    p.add_argument("--dry-run", action="store_true")

    p = add("install-skill", cmd_install_skill, "install/update the generic agent skill (no release.toml needed)", json_flag=False)
    p.add_argument("--format", default="auto", choices=("auto", "all", "claude", "agents"))
    p.add_argument("--force", action="store_true", help="replace a hand-written skill file")
    p.add_argument("--check", action="store_true", help="exit 1 if the skill is missing or outdated")

    p = add(
        "install-local-skill",
        cmd_install_local_skill,
        "add this repository's specifics (PROJECT.md) to the installed skill",
        json_flag=False,
    )
    p.add_argument("--format", default="auto", choices=("auto", "all", "claude", "agents"))
    p.add_argument("--force", action="store_true", help="replace a hand-written PROJECT.md")
    p.add_argument("--check", action="store_true", help="exit 1 if PROJECT.md is missing or outdated")

    p = add("help", cmd_help, "offline references: config, staging, ci", json_flag=False)
    p.add_argument("topic", nargs="?", choices=sorted(HELP_TOPICS))
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_USAGE
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_USAGE
    try:
        return args.func(args)
    except ReleaseError as exc:
        print(f"{parser.prog}: error: {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        return 130
