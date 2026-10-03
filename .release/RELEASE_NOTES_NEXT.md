<!--
vl-release staged release notes: the user-facing story of the NEXT release.
Maintained continuously while working; `vlr prepare` promotes it into the release notes
history (and the GitHub release) and resets this file to this template.

Format: the first line is "# <release title>" WITHOUT a version number (vlr adds it).
Everything after the title is the Markdown release body. Describe the resulting behavior
for users and operators; keep it representative of what actually ships.
-->
# Agent skills that work before a repository is set up

Adopting vl-release no longer has a chicken-and-egg problem.

- `vlr install-skill` now works in any Git repository, with or without a `release.toml`. It
  installs a generic skill that teaches agents both the release workflow and how to set a
  repository up: run `vlr init`, fill in `release.toml`, get `vlr check` passing, and ask before
  choosing publication targets.
- `vlr install-local-skill` adds `PROJECT.md` next to that skill: this repository's staged-doc
  paths, version targets, packages, channels and checks, generated from `release.toml`.
  `vlr check` tells you when it is out of date.
- The references now ship with the tool and work offline: `vlr help config`,
  `vlr help staging` and `vlr help ci`.
- `vlr cut 0.1.0 --push` can release the version already in the version file when it has never
  been released, so a freshly set-up repository no longer has to skip its first version.
