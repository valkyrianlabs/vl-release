# vl-release release notes

<!-- vl-release:entry version=0.2.2 -->
## 0.2.2 — Package builds are fast again

_Released 2026-10-05_

The Debian package build runs the test suite, and since 0.2.0 that took about four minutes on
GitHub runners instead of about half a minute. 0.2.1 blamed npm; the test durations it added to
the build log showed the real cost: the Debian build tests, which run a nested
`dpkg-buildpackage` and take minutes inside the package build. Those tests are now skipped there
(the regular test runs, including CI, still run them), so building the package is quick again.

<!-- vl-release:entry version=0.2.1 -->
## 0.2.1 — Quieter npm builds, Python 3.12

_Released 2026-10-05_

- `vlr build-npm --json` prints clean JSON again: the output of the build commands and of
  `npm pack` now goes to stderr.
- `vlr build-npm` packs with npm's update check, audit and funding notices turned off.
- The Debian package build no longer lets the test suite's `npm pack` calls touch the network or
  the npm cache. In v0.2.0 the package build took over three minutes on GitHub runners instead of
  about half a minute. The build log now lists the slowest tests.
- vl-release now requires Python 3.12 or newer (Ubuntu 24.04, Debian 13 and current Homebrew
  all ship newer versions).

<!-- vl-release:entry version=0.2.0 -->
## 0.2.0 — npm packages, from build to npmjs.com

_Released 2026-10-05_

vl-release can now release npm packages with the same guarantees as Debian packages, so a JavaScript
repository gets version sync, staged release notes, GitHub releases and publication from one
`release.toml`.

- `[npm]` describes the package: where `package.json` lives, `npm` or `pnpm` packing, commands to
  run before packing (for example the build), and a tarball contract: paths the package must ship,
  paths it must never ship, and files that must be byte-identical to the work tree.
- `vlr build-npm` packs the package from the prepared work tree into the release directory. That
  tarball is checksummed, validated by `vlr validate-artifacts`, attached to the GitHub release
  and published unchanged.
- `[[publish.npm]]` lists the registries. npmjs.com works with npm trusted publishing
  (`auth = "oidc"`, no token secret); `token` and `basic` auth cover private registries.
- `vlr publish-npm` checks every registry before uploading anywhere: an identical version
  (proven by `dist.integrity`) is skipped, a different one is refused, and a version older than
  `latest` is refused unless `--allow-older-version` publishes it under a maintenance tag. It then
  waits until each registry lists the exact tarball. `vlr verify-npm` repeats the verification.
- `vlr check` fails when `package.json` is not a version target, is `private`, or sets
  `publishConfig.registry`.

Debian and APT publication are unchanged. Run `vlr install-skill` to update the agent skill.

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
