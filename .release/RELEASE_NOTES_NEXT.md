<!--
vl-release staged release notes: the user-facing story of the NEXT release.
Maintained continuously while working; `vlr prepare` promotes it into the release notes
history (and the GitHub release) and resets this file to this template.

Format: the first line is "# <release title>" WITHOUT a version number (vlr adds it).
Everything after the title is the Markdown release body. Describe the resulting behavior
for users and operators; keep it representative of what actually ships.
-->
# One build, several npm names

An npm package can now be published under more than one name from a single build, for example a
scoped `@org/tool` and an unscoped `tool`, at the same version and in the same release:

```toml
[[npm.aliases]]
name = "tool"
rewrite = ["package/dist/package-name.js"]   # optional: files that must carry the package's own name
```

- **One build, identical bytes.** `vlr build-npm` packs once and derives each alias tarball from
  the canonical tarball. Only the package.json `"name"` changes, plus the canonical name inside any
  `rewrite` members you list.
- **No drift.** `vlr validate-artifacts` applies the tarball contract to every package and
  re-derives each alias. Any difference in members, file modes or bytes fails the release.
- **All-or-nothing publication with safe re-runs.** `vlr publish-npm` plans every name on every
  registry before uploading anything. It uploads the aliases before the canonical package, so a
  failing alias stops the release before the canonical version moves. A re-run skips whatever is
  already published with identical bytes. `vlr verify-npm` checks every name.
- **New names on npmjs.com.** Trusted publishing can only be configured for a package that
  exists. `vlr help ci` describes the one-time bootstrap: publish the CI-built alias tarball by
  hand, add the trusted publisher, and re-run the job.

`vlr publish-npm --json` keeps its existing fields for the canonical package and adds a `packages`
list covering every name. Repositories without `[[npm.aliases]]` behave exactly as before.
