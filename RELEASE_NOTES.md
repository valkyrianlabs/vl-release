# vl-release release notes

<!-- vl-release:entry version=0.1.4 -->
## 0.1.4 — Publication verification no longer waits on a lazily rebuilt APT index

_Released 2026-10-04_

`vlr publish-deb` and `vlr verify-published` now request the suite's `InRelease` (falling back to `Release`) before
each read of the `Packages` indexes. Repository managers that rebuild their `dists/` metadata only when a client asks
for the Release files, such as Sonatype Nexus apt-hosted repositories, regenerate the index right away instead of
leaving a successful upload unlisted until some other client runs `apt-get update`. Previously a release could publish
its package and still fail verification after `verify_timeout`.

<!-- vl-release:entry version=0.1.3 -->
## 0.1.3 — No more stale-skill warnings after every release

_Released 2026-10-03_

`vlr check` used to report the agent skill and `PROJECT.md` as stale after every version bump or
vl-release upgrade, even when nothing in them had changed. Skills are now only stale when their
content actually differs. Run `vlr install-skill` and `vlr install-local-skill` once to switch
existing files to the new format.

<!-- vl-release:entry version=0.1.2 -->
## 0.1.2 — Agent skills that work before a repository is set up

_Released 2026-10-03_

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

<!-- vl-release:entry version=0.1.1 -->
## 0.1.1 — One release toolkit for every ValkyrianLabs project

_Released 2026-10-03_

vl-release replaces the release tooling that was copied into each repository with a single
installed tool, `vl-release` (or `vlr`), configured per repository by `release.toml`.

### Highlights

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

### Install

```sh
sudo apt install vl-release              # from the ValkyrianLabs APT repository
brew install valkyrianlabs/tap/vl-release
```
