# Release CI with vl-release

Workflow YAML provides what only GitHub can: triggers, checkout, credentials, runners and job
ordering. Release semantics live in `vlr` commands. A release job looks like this:

```yaml
- run: vlr check --release --tag "$GITHUB_REF_NAME"
- run: vlr prepare --record release/meta/prepare.json
- run: vlr build-deb && vlr source-archive && vlr homebrew formula
- run: vlr build-npm                               # npm packages: the tarballs that get published
- run: vlr checksums && vlr validate-artifacts
- run: vlr publish-deb --require-enabled          # verifies the APT index by sha256
- run: vlr publish-npm --require-enabled          # verifies every registry by dist.integrity
- run: vlr github-release
- run: vlr homebrew publish --tap-dir homebrew-tap
- run: vlr finalize --record release/meta/prepare.json
```

## The release transaction

1. **Checkout the tag.** It carries the populated `_NEXT` files (`vlr cut` does not promote them).
2. **`vlr prepare`** promotes them into `debian/changelog` / `RELEASE_NOTES.md` *in the CI work
   tree only*.
3. **Build from that work tree.** `build-deb` copies the filesystem state (not a Git commit) into
   `build/deb/src`; `source-archive` reads files from disk. The artifacts therefore contain the
   exact prepared documents, which `validate-artifacts` checks.
4. **Publish and verify.**
5. **Only on success, `vlr finalize`** commits the promoted history and the cleared `_NEXT`
   files onto the current head of the release branch (in a temporary worktree, no force push).

If anything fails before step 5, the repository is untouched: the `_NEXT` files are still
populated and the release can be re-run.

## Re-runs are safe

- `prepare` is deterministic (dates come from the release commit), so a re-run job renders the
  same documents and, on the same toolchain, rebuilds the same bytes.
- `publish-deb`, `publish-npm`, `github-release` and `homebrew publish` skip what is already published with
  identical bytes and **refuse** (exit code 3) anything that would replace published bytes.
  If a rebuild ever produces different bytes for a published version, re-run only the failed
  jobs (reusing the uploaded build artifact) or release a new patch version.
- `finalize` is a no-op when the branch already records the version, and verifies with
  `--record` that it persists exactly the documents that were built and published.

Splitting the workflow into jobs is fine: run `vlr prepare` again in the finalize job (it is
deterministic) and pass `--record` from the build job's artifact.

## Credentials and variables (ValkyrianLabs organization)

| Name | Kind | Purpose |
|---|---|---|
| `NEXUS_USER` | org secret | Nexus upload user |
| `NEXUS_PASSWORD` | org secret | Nexus upload password |
| `NEXUS_APT_REPO` | org variable | Nexus APT hosted repository upload URL |
| `RELEASE_PUBLISH_MODE` | repo variable | `nexus` to publish (default `disabled`) |
| `HOMEBREW_TAP_TOKEN` | secret | push access to `valkyrianlabs/homebrew-tap` |

vl-release also accepts the legacy per-repository names `NEXUS_PASS` and `NEXUS_REPO_URL` as
fallbacks, so existing workflows can migrate without renaming secrets.

## npm

`vlr publish-npm --dry-run` reads every registry (no credentials needed for public reads) and
fails before anything is published if a version already exists with different bytes; run it in
the build job. The publishing job needs:

- `npm` on PATH; for `auth = "oidc"` (npm trusted publishing) npm ≥ 11.5.1
  (`npm install -g npm@latest`) and `permissions: id-token: write`. On npmjs.com the package's
  trusted publisher must name this repository, the workflow file and (if set) the environment.
- for `token` / `basic` registries, the variables named in their tables.
- `RELEASE_PUBLISH_MODE=enabled` (or `--mode enabled`); `disabled` is the default.

Registries are published in the order they are listed; a rerun skips the ones that already
carry the identical tarball.

With `[[npm.aliases]]`, every package name is planned on every registry first (a conflict on any
of them refuses the whole run before an upload), then the aliases are uploaded, then the canonical
package. If an alias upload fails, the canonical package has not moved; fix the cause and re-run
the job: whatever is already published with identical bytes is skipped.

A **new** alias on npmjs.com cannot use trusted publishing yet: npm only lets you configure a
trusted publisher for a package that exists. Bootstrap it once with the CI-built bytes:

1. Let the release run; its publish job fails on the alias (nothing else is uploaded yet).
2. Download the build job's artifact and publish the alias tarball by hand, e.g.
   `npm publish ./release/<alias>-<version>.tgz --access public` (logged in as an owner).
3. On npmjs.com, add the trusted publisher to the new package (same repository, workflow file and
   environment as the canonical package).
4. Re-run the failed jobs: the alias is skipped as identical, the canonical package is published.

## Ordering

`github-release` must run before `homebrew publish` (the formula's URL is a release asset, and
`homebrew publish` downloads it to verify the sha256 before touching the tap). `finalize` runs
last and needs `contents: write` on the repository (and permission to push to the release
branch if it is protected).

## Tag triggers today, prepared tags later

The first version keeps the existing model: `vlr cut` tags the commit that carries the staged
docs and the tag push triggers release CI. Because `prepare`, building and `finalize` never
depend on the tag pointing at a prepared commit, a future workflow can move tag creation after
preparation (e.g. workflow_dispatch → prepare → build → publish → finalize → tag the record
commit) without changing vl-release.
