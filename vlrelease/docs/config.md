# `release.toml` reference (schema 1)

`release.toml` lives at the Git root. Unknown keys are errors, paths are relative to the
repository and may not escape it. A minimal file:

```toml
schema_version = 1

[project]
name = "Demo"

[version]
canonical = "VERSION"
```

## Top level

| Key | Required | Meaning |
|---|---|---|
| `schema_version` | yes | `1` |

## `[tool]`

| Key | Default | Meaning |
|---|---|---|
| `requires` | – | vl-release versions this repository accepts, e.g. `">=0.1,<1"` (`>= <= > < == !=`, comma-separated). A mismatch fails every command with exit code 2. |

## `[project]`

| Key | Default | Meaning |
|---|---|---|
| `name` | required | Display name (release notes heading, skill, tag annotation) |
| `package` | `name` | Package name (`[a-z0-9][a-z0-9+.-]+`): source archive prefix, defaults |
| `repository` | – | GitHub `owner/name`; required for `[homebrew]` and used by `github-release` |

## `[version]`

| Key | Meaning |
|---|---|
| `canonical` | The source of truth: a path (plain version file) or a target table |
| `targets` | Every other file carrying the version |

Target tables: `{ kind = "...", path = "...", pattern = "..." }`

| `kind` | Reads/writes |
|---|---|
| `file` | the whole file is the version (`X.Y.Z\n`) |
| `meson` | `project(..., version: 'X.Y.Z')` |
| `package_json` | top-level `"version"` (formatting preserved) |
| `pyproject` | `version = "X.Y.Z"` inside `[project]` |
| `regex` | `pattern` with a `(?P<version>...)` group, e.g. `'VERSION = "(?P<version>[^"]+)"'` |
| `homebrew` | the version in the formula's `url` (or `version "..."`); a version change resets `sha256` to a placeholder |

`debian/changelog` is deliberately **not** a version target: it is published history, extended
only by `vlr prepare`.

## `[release]`

| Key | Default | Meaning |
|---|---|---|
| `branch` | `"main"` | Release branch (`cut`, `finalize`) |
| `remote` | `"origin"` | Remote for `cut`/`finalize` |
| `tag` | `"v{version}"` | Tag format |
| `title` | `"v{version} — {title}"` | GitHub release title; placeholders `{version} {tag} {title} {name}` |
| `notes` | `"RELEASE_NOTES.md"` | Release-notes history |
| `notes_next` | `".release/RELEASE_NOTES_NEXT.md"` | Staged release notes |
| `changelog_next` | `".release/CHANGELOG_NEXT.md"` | Staged changelog (requires `[debian]`) |
| `output_dir` | `"release"` | Release assets (gitignore it) |
| `build_dir` | `"build"` | Isolated build trees (gitignore it) |
| `test_command` | `[]` | Run by `vlr cut` before tagging |
| `cut_commit_message` | `"chore(release): v{version}"` | |
| `finalize_commit_message` | `"chore(release): record v{version}"` | |

## `[debian]`

Enabled by its presence (`enabled = false` turns it off).

| Key | Default | Meaning |
|---|---|---|
| `changelog` | `"debian/changelog"` | |
| `distribution` / `urgency` | `"unstable"` / `"medium"` | Rendered stanza; env `RELEASE_DEBIAN_DISTRIBUTION`/`_URGENCY` override |
| `maintainer` | – | `Name <email>`; else `DEBFULLNAME`+`DEBEMAIL`, the previous entry, `debian/control` |
| `revision` | `1` | Debian revision for new stanzas (`prepare --debian-revision` overrides) |
| `build_command` | `["dpkg-buildpackage","-us","-uc","-b"]` | Run inside `build/deb/src` |
| `pre_build` | `[]` | Commands run in the repository root before the tree is copied (e.g. a web build) |
| `build_excludes` | `[]` | Extra top-level paths not copied into the build tree (`.git`, `build/`, `release/` never are) |
| `extra_artifacts` | `[]` | Globs copied into the output directory after the build |

### `[[debian.packages]]`: package contracts

One table per binary package. Every built `.deb` must have a contract and vice versa.

| Key | Meaning |
|---|---|
| `name` | Binary package name |
| `architecture` | Expected architecture (`all`, `amd64`, …) |
| `required_paths` | Globs that must match at least one member |
| `forbidden_paths` | Globs that must match nothing |
| `any_of` | Lists of globs; at least one per list must match |
| `identical_files` | `{ member, source }`: the packaged file must equal the work-tree file byte for byte |

Always checked: control `Package`/`Version` match the file name, the version is the prepared one,
and `usr/share/doc/<pkg>/changelog.Debian.gz` starts with the prepared stanza.

## `[source_archive]`

| Key | Default | Meaning |
|---|---|---|
| `name` | `"{package}-{version}.tar.gz"` | Asset name |
| `exclude` | `[]` | Tracked paths/globs to leave out |

## `[publish.apt]`

Requires `[debian]`. Upload credentials and the upload URL come from the environment.

| Key | Default | Meaning |
|---|---|---|
| `repository_url` | – | Public APT base URL for reading `dists/<suite>/<component>/binary-<arch>/Packages` |
| `suite` / `components` / `architectures` | `"stable"` / `["main"]` / `["amd64"]` | Index locations (`all` packages appear in every `binary-*` index) |
| `verify_timeout` / `verify_interval` | `600` / `15` | Seconds to wait for reindexing; each poll first requests the suite's `InRelease` (falling back to `Release`), which makes repositories that rebuild lazily (Nexus apt-hosted) regenerate the index |

## `[npm]`

Enabled by its presence. `vlr build-npm` packs the package from the prepared work tree into the
output directory; that tarball is checksummed, validated, attached to the GitHub release and
published unchanged. The package name and version come from `package_dir/package.json`, which
must be a version target (or the canonical version) and must not set `"private": true` or
`publishConfig.registry`.

| Key | Default | Meaning |
|---|---|---|
| `package_dir` | `"."` | Directory containing `package.json` |
| `packer` | `"npm"` | `npm` (`npm pack`) or `pnpm` (`pnpm pack`, which applies `publishConfig` overrides and rewrites `workspace:` ranges) |
| `pre_pack` | `[]` | Commands run in `package_dir` before packing, e.g. `[["pnpm", "build"]]` |
| `required_paths` / `forbidden_paths` / `any_of` | `[]` | Tarball contract, as for `[[debian.packages]]`; members start with `package/` |
| `identical_files` | `[]` | `{ member = "package/README.md", source = "README.md" }`: packaged bytes must equal the work tree |
| `dist_tag` | `"latest"` | dist-tag a release moves |
| `maintenance_dist_tag` | `"maintenance"` | dist-tag for `publish-npm --allow-older-version` releases below `dist_tag`, which never moves backwards |
| `access` | `"public"` | `public` or `restricted` (scoped packages) |

## `[[publish.npm]]`

One table per registry; requires `[npm]`. `vlr publish-npm` plans every registry against its
live packument before uploading anywhere: absent → upload, identical `dist.integrity` (sha512,
else `dist.shasum`) → skip, different → refuse (exit 3), unreadable → refuse. After uploading it
polls each registry until the version is listed with the expected integrity and the dist-tag
points at it. Uploads use `npm publish <tarball>` with any credentials in a temporary 0600
userconfig, never argv.

| Key | Default | Meaning |
|---|---|---|
| `name` | required | Label used by `--registry NAME` (`[a-z0-9-]`) |
| `auth` | required | `oidc`: npm trusted publishing from GitHub Actions (`id-token: write`, npm ≥ 11.5.1, no secret; the usual choice for npmjs.com). `token`: bearer token from `token_env`. `basic`: `username_env` + `password_env` (e.g. a private registry) |
| `registry` | – | Registry URL, e.g. `https://registry.npmjs.org/` |
| `registry_env` | – | Environment variable holding the URL instead (wins over `registry`) |
| `token_env` | `"NPM_TOKEN"` | Token variable for `auth = "token"` |
| `username_env` / `password_env` | – | Required for `auth = "basic"` (also used to read the packument) |
| `provenance` | `false` | Pass `--provenance` (trusted publishing adds provenance on its own) |
| `verify_timeout` / `verify_interval` | `300` / `10` | Seconds to wait for the registry to list the version |

Publication is gated by `RELEASE_PUBLISH_MODE`: `disabled` (default) skips it, `enabled`
publishes (`nexus`, set by APT workflows, also publishes).

npmjs.com with trusted publishing (configure the trusted publisher on npmjs.com for this
repository and workflow file):

```toml
[npm]
pre_pack = [["pnpm", "build"]]
required_paths = ["package/package.json", "package/dist/index.js"]
forbidden_paths = ["package/src/*", "package/node_modules/*"]
identical_files = [{ member = "package/README.md", source = "README.md" }]

[[publish.npm]]
name = "npmjs"
registry = "https://registry.npmjs.org/"
auth = "oidc"
```

A private registry instead of (or next to) npmjs.com:

```toml
[[publish.npm]]
name = "private"
registry_env = "PRIVATE_NPM_REGISTRY"
auth = "basic"
username_env = "PRIVATE_NPM_USER"
password_env = "PRIVATE_NPM_PASSWORD"
```

## `[homebrew]`

| Key | Default | Meaning |
|---|---|---|
| `formula` | required | Formula template in this repository |
| `tap` | required | Tap repository, e.g. `valkyrianlabs/homebrew-tap` |
| `tap_branch` | `"main"` | |
| `tap_path` | `Formula/<template name>` | Path inside the tap |
| `source` | `"release-asset"` | `release-asset` (the source archive attached to the GitHub release; requires `[source_archive]`) or `tag-archive` (GitHub's tag tarball; does **not** contain prepared docs) |
