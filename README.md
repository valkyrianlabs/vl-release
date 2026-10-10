# vl-release

The ValkyrianLabs release toolkit. One installed tool, `vl-release` (short: `vlr`), replaces the
per-repository `tools/release` copies: it keeps versions consistent, promotes agent-maintained
release documentation into the published history, builds and validates packages, and publishes
them safely. Each repository describes itself in a `release.toml`.

```
DEVELOPMENT                                  RELEASE CI (from the tag)
agent changes code                           vlr check --release --tag vX.Y.Z
agent maintains                              vlr prepare          (work tree only)
  .release/CHANGELOG_NEXT.md                 build + validate      (from the prepared tree)
  .release/RELEASE_NOTES_NEXT.md             publish + verify      (APT, npm, GitHub, Homebrew)
vlr check                                    vlr finalize          (only after success)
vlr cut patch|minor|major --push  ───tag──▶    commits the promoted history and clears _NEXT
```

There is no AI in the release pipeline. The agent writes the release documentation as part of
normal work; `vl-release` validates it, promotes it deterministically, and refuses to publish
anything inconsistent.

## Install

```sh
sudo apt install vl-release                  # ValkyrianLabs APT repository (Debian/Ubuntu, Python >= 3.12)
brew install valkyrianlabs/tap/vl-release    # Homebrew (macOS/Linux)
```

From a source checkout (no installation, no dependencies beyond Python ≥ 3.12 and git):

```sh
python3 -m vlrelease --version               # or ./bin/vlr
```

A broken installed package therefore never prevents building its own fix.

## The repository contract

```
release.toml                     the per-repository configuration (`vlr help config`)
.release/CHANGELOG_NEXT.md       staged package-facing changes (when [debian] is enabled)
.release/RELEASE_NOTES_NEXT.md   staged user-facing release notes
RELEASE_NOTES.md                 published release-notes history (written by vlr prepare)
debian/changelog                 published Debian history (written by vlr prepare)
```

`.release/` is tracked. `_NEXT` files are the mutable state of unreleased work; the history
files are the record of what was published. Agents edit the former, never the latter.

### Versions

SemVer (`MAJOR.MINOR.PATCH`) is the default and needs no configuration. Repositories that
package someone else's software can opt into `[version] policy = "debian-upstream"`: the
version is then `UPSTREAM-REVISION` (`8.0.2-1`, `8.0.2-2`, `8.0.3-1`), ordered like Debian
versions and used unchanged as tag, release-notes entry and Debian package version, with
`vlr version bump revision` for packaging-only changes and `vlr version upstream X.Y.Z` to adopt
a new upstream release. Details: `vlr help config`.

Adopting vl-release in a repository:

1. `vlr install-skill` installs the generic agent skill (`.claude/skills/vl-release/SKILL.md`, plus
   `.agents/skills/…` when the repository uses that convention). It needs no configuration and
   teaches an agent how to set the repository up.
2. `vlr init` scaffolds `release.toml` and `.release/`; fill it in (`vlr help config`) until
   `vlr check` passes.
3. `vlr install-local-skill` adds `PROJECT.md` next to the skill: this repository's paths, version
   targets, channels and checks, rendered from `release.toml`. Re-run it after changing the config
   (`vlr check` warns when it is stale).

Offline references ship with the tool: `vlr help config`, `vlr help staging`, `vlr help ci`.

### Staged documents

`CHANGELOG_NEXT.md`: concise technical bullets; `vlr prepare` renders the Debian stanza.

```markdown
- Add the frobnicator.
- Fix a crash when the widget is empty.
  Continuation lines are indented.
  - One level of nested detail.
```

Optional `## Section` headings group bullets (rendered as `* Section` / `- item`).

`RELEASE_NOTES_NEXT.md`: line 1 is the title **without a version**; the rest is the body.

```markdown
# Frobnication arrives

You can now frobnicate. ## headings, lists and code blocks are fine.
```

HTML comments are ignored, so the reset templates are pure guidance. Placeholders (`TODO`,
`TBD`, `<title>`, …) are rejected.

## Commands

| Command | Purpose |
|---|---|
| `vlr check [--release] [--tag T] [--json]` | Validate config, versions, staged docs, history; `--release` is the strict CI gate |
| `vlr status [--json] [--github-output]` | Version, tag, release phase, staged state, release title |
| `vlr version show\|check\|sync\|set X.Y.Z\|bump patch\|minor\|major` | Version consistency across all configured targets |
| `vlr version bump revision` / `vlr version upstream X.Y.Z` | `debian-upstream` policy: packaging-only revision / adopt a new upstream release (revision 1) |
| `vlr prepare [--dry-run] [--record FILE] [--allow-empty-patch]` | Promote staged docs into the history (work tree only) |
| `vlr release-title [--staged] [--json]` / `vlr release-body [--staged] [--output F]` | GitHub release title/body from the prepared entry |
| `vlr build-deb` | Build `.deb`s from the prepared work tree inside `build/deb/` (never the parent directory) |
| `vlr source-archive` | Reproducible tarball of the prepared tree (Homebrew source) |
| `vlr homebrew formula` / `vlr homebrew publish --tap-dir DIR` | Render the release formula / commit it to the tap |
| `vlr checksums [--verify]` | `SHA256SUMS` for the output directory |
| `vlr validate-artifacts [--json]` / `vlr artifacts [--json]` | Package contracts, checksums, staged assets / asset listing |
| `vlr publish-deb [--dry-run] [--require-enabled]` | Idempotent, integrity-checked APT publication + verification |
| `vlr verify-published [--timeout S]` | Wait until the APT index lists the built packages by sha256 |
| `vlr build-npm` | Pack the npm package from the prepared work tree (`npm pack` or `pnpm pack`), plus one derived tarball per `[[npm.aliases]]` name |
| `vlr publish-npm [--dry-run] [--registry NAME] [--require-enabled]` | Idempotent, integrity-checked publication to every `[[publish.npm]]` registry + verification |
| `vlr verify-npm [--registry NAME] [--timeout S]` | Wait until the registries list the built tarball by integrity |
| `vlr github-release` | Create/update the GitHub release and upload assets (idempotent) |
| `vlr cut patch\|minor\|major\|revision\|VERSION [--push]` | Bump, commit, annotated tag, atomic push (resumable; the release commit must be a pure version bump) |
| `vlr finalize [--record FILE]` | After publication: commit the promoted history + cleared `_NEXT` to the branch |
| `vlr install-skill` / `vlr install-local-skill` | Generic agent skill / this repository's `PROJECT.md` |
| `vlr help config\|staging\|ci` | Offline references |
| `vlr init` / `vlr doctor` | Scaffold the contract files / check the toolchain (works anywhere) |

Every project command discovers the Git root from the current directory (or `--repo PATH`) and
reads `release.toml` there; it never searches above the Git root.

Exit codes: `0` success · `1` check/operation failed · `2` usage or configuration error ·
`3` publication refused because it would contradict something already published.

## `vlr prepare` rules

For the canonical version V:

| History top | Staged docs | Result |
|---|---|---|
| older than V | complete | render, prepend to history, reset staging |
| older than V | empty | **error** (`--allow-empty-patch` fills a maintenance entry, patch releases only) |
| V | empty | idempotent no-op (re-run / retry) |
| V | populated | **error**: bump the version first |
| newer than V, or files disagree | – | **error** |

prepare is deterministic: release dates come from the release commit (or `SOURCE_DATE_EPOCH`),
so re-running a release job renders byte-identical documents and rebuilds identical packages.

## Publication safety

Publishing is boring and unforgiving:

- **APT** (ported from vaulthalla): the live `Packages` index decides per `.deb`: absent → upload;
  identical sha256 → skip; different sha256 → refuse; unreadable index → refuse. After upload the
  index is polled until every package is listed with its expected sha256. Credentials reach curl
  through `--config -` on stdin, never argv.
- **npm** (npmjs.com via trusted publishing, or any npm registry): every registry's packument decides before
  anything is uploaded anywhere: absent → upload; identical `dist.integrity` → skip; different →
  refuse; unreadable → refuse. Uploads publish the validated tarball itself through
  `npm publish`, with credentials in a temporary 0600 userconfig, then each registry is polled
  until it lists the integrity and the dist-tag. A release below the current `latest` is refused
  (or published under a maintenance tag), so `latest` never moves backwards. Trusted publishing
  (GitHub OIDC) is supported without secrets. `[[npm.aliases]]` publish the same build under more
  names (e.g. scoped and unscoped): each alias tarball is derived from the canonical tarball's
  bytes, validated to differ only in its name, and published alongside it.
- **GitHub releases**: title/body come from the prepared entry; assets are never clobbered: an
  existing asset with different bytes is refused.
- **Homebrew**: the formula points at the source-archive release asset; before touching the tap,
  the URL is downloaded and its sha256 checked; a published formula version is never changed.
- `SHA256SUMS` must match before anything is published; package contracts must pass; versions
  must agree everywhere.

## Environment

| Variable | Used by |
|---|---|
| `RELEASE_PUBLISH_MODE` (`disabled`\|`nexus`) | `publish-deb` (default `disabled`) |
| `RELEASE_PUBLISH_MODE` (`disabled`\|`enabled`) | `publish-npm` (default `disabled`; `nexus` also publishes) |
| `registry_env` / `token_env` / `username_env` / `password_env` | per `[[publish.npm]]` table (npmjs.com with `auth = "oidc"` needs none) |
| `NEXUS_APT_REPO` (fallback `NEXUS_REPO_URL`) | Nexus APT upload URL |
| `NEXUS_USER`, `NEXUS_PASSWORD` (fallback `NEXUS_PASS`) | Nexus credentials |
| `RELEASE_APT_REPOSITORY_URL`, `_SUITE`, `_COMPONENTS`, `_ARCHITECTURES` | override `[publish.apt]` for index reads |
| `RELEASE_DEBIAN_DISTRIBUTION`, `RELEASE_DEBIAN_URGENCY` | override the rendered stanza |
| `DEBFULLNAME`, `DEBEMAIL` | Debian maintainer when `debian.maintainer` is unset |
| `HOMEBREW_TAP_BRANCH` | tap branch override |
| `SOURCE_DATE_EPOCH` | release date override |
| `GH_TOKEN` / `GITHUB_TOKEN` | `github-release` (through `gh`) |

## CI

See `vlr help ci` ([vlrelease/docs/ci.md](vlrelease/docs/ci.md)) and this repository's own
[`.github/workflows/release.yml`](.github/workflows/release.yml), which releases vl-release with
vl-release.

## Development

```sh
python3 -m unittest discover -s tests -t .    # stdlib unittest; Python 3.12 and 3.14
./bin/vlr check                               # this repository satisfies its own contract
```

Runtime dependencies: Python ≥ 3.12 standard library and `git`. `dpkg-dev` (build/validate),
`curl` (APT upload), `npm` (npm packages) and `gh` (GitHub releases) are needed only for the commands that use them.

vl-release is maintained with its own workflow: keep `.release/*_NEXT.md` current as you change
it (see `.claude/skills/vl-release/SKILL.md`).
