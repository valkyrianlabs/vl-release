---
name: vl-release
description: Release workflow for vl-release (vl-release). Use whenever a change affects users, operators, security, compatibility, packaging, deployment or other substantial behavior, to keep the staged release docs (.release/CHANGELOG_NEXT.md, .release/RELEASE_NOTES_NEXT.md) current; and before touching versions, release history, packaging or the release workflow.
---
<!-- vl-release:generated version=0.1.0 -- regenerate with `vlr install-skill`; local edits are overwritten -->

# Releasing vl-release with vl-release

`vl-release` (`vlr`) is the release toolkit installed on this machine. This repository's contract
lives in `release.toml`; the commands below operate on the Git repository you are in.

## While you work: keep the staged release docs current

For any material change (user-visible behavior, operations/deployment, security, compatibility,
packaging, or a substantial internal change) update the staged documents **in the same change**:

- **`.release/CHANGELOG_NEXT.md`**: concise, technical, package-facing changes for the next
  release. One `- ` bullet per change; optional `## Section` headings; indented continuation
  lines and one level of nested `  - ` detail bullets. `vlr prepare` renders it into a proper
  `debian/changelog` stanza, so write no Debian boilerplate.
- **`.release/RELEASE_NOTES_NEXT.md`**: the user-facing release notes for the next release. Line 1 is
  `# <release title>` *without* a version number (`vlr prepare` adds it); everything after it is
  the Markdown body. Describe what users and operators get, not how the code changed.

Quality rules:

- Consolidate. Edit and merge existing bullets so the files describe the *resulting* behavior;
  do not append a log of every commit, fix-up or intermediate attempt.
- Keep them truthful: when a change is reverted or reshaped, update or remove its entry.
- No placeholders (`TODO`, `TBD`, `<title>`); `vlr check` rejects them.
- Purely internal refactors with no observable effect don't need an entry.

Before considering substantial work complete, run:

```sh
vlr check            # release.toml, version targets, staged docs, history files
```

## Do not edit the published history

`RELEASE_NOTES.md` and `debian/changelog` are the **historical record of published releases**. Do not
add entries to them by hand and do not promote staged notes yourself: `vlr prepare` (in release CI)
owns promotion, and `vlr finalize` persists it after publication succeeds.

## Versions

The canonical version is in `VERSION`; `vlr` keeps every configured target in sync (`vlrelease/__init__.py`).
Never edit version numbers by hand:

```sh
vlr version check
vlr version bump patch|minor|major     # or: vlr version set X.Y.Z
```

## Releasing

Cut a release from `main` (bumps the version, commits, tags `vX.Y.Z`):

```sh
vlr status           # where the repository is in the release cycle
vlr cut patch|minor|major --push
```

Release CI then runs, in order:

1. check out the tag, `vlr check --release --tag <tag>`;
2. `vlr prepare` promotes the staged docs into the history files **in the CI work tree only**;
3. builds and validates artifacts **from that prepared work tree** (`vlr build-deb`, `vlr validate-artifacts`);
4. publishes (the .deb to the APT repository; the GitHub release; the Homebrew formula to valkyrianlabs/homebrew-tap) and verifies the publication;
5. **only after publication succeeded**, `vlr finalize` commits the promoted history and the reset
   `_NEXT` files back to `main`.

If anything fails before step 5, the repository keeps its populated `_NEXT` files, so nothing
is lost and the release can simply be re-run. Re-runs are safe: `prepare` is deterministic and
idempotent, and publication skips artifacts that are already published with identical bytes
while refusing to replace anything published with different bytes.

## Useful commands

| Command | Purpose |
|---|---|
| `vlr check [--release]` | Validate the repository (strict release gate with `--release`) |
| `vlr status [--json]` | Version, release phase, staged-doc state |
| `vlr prepare --dry-run` | Preview the rendered history entries without writing |
| `vlr release-title --staged` / `vlr release-body --staged` | Preview the GitHub release title/body |
| `vlr doctor` | Check the local toolchain |
