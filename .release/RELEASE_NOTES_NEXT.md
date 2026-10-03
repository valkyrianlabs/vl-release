<!--
vl-release staged release notes: the user-facing story of the NEXT release.
Maintained continuously while working; `vlr prepare` promotes it into the release notes
history (and the GitHub release) and resets this file to this template.

Format: the first line is "# <release title>" WITHOUT a version number (vlr adds it).
Everything after the title is the Markdown release body. Describe the resulting behavior
for users and operators; keep it representative of what actually ships.
-->
# One release toolkit for every ValkyrianLabs project

vl-release replaces the release tooling that was copied into each repository with a single
installed tool, `vl-release` (or `vlr`), configured per repository by `release.toml`.

## Highlights

- **Agents write the release docs as they work.** Changes are staged in
  `.release/CHANGELOG_NEXT.md` and `.release/RELEASE_NOTES_NEXT.md`; release CI promotes them
  deterministically with `vlr prepare`. There is no AI in the release pipeline.
- **Transactional releases.** CI prepares, builds, publishes and verifies; only after
  publication succeeds does `vlr finalize` commit the promoted history back. A failed release
  loses nothing and can simply be re-run.
- **Safe publication.** APT uploads, GitHub release assets and Homebrew formulas are
  idempotent and immutable: identical re-publication is skipped, different bytes for an
  already-published version are refused.
- **Thin CI.** Release semantics live in testable `vlr` commands instead of workflow scripts.

## Install

```sh
sudo apt install vl-release              # from the ValkyrianLabs APT repository
brew install valkyrianlabs/tap/vl-release
```
