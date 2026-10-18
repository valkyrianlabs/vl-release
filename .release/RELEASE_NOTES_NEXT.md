<!--
vl-release staged release notes: the user-facing story of the NEXT release.
Maintained continuously while working; `vlr prepare` promotes it into the release notes
history (and the GitHub release) and resets this file to this template.

Format: the first line is "# <release title>" WITHOUT a version number (vlr adds it).
Everything after the title is the Markdown release body. Describe the resulting behavior
for users and operators; keep it representative of what actually ships.
-->

# Release several npm packages from one repository

A repository can now publish more than one independently built npm package in the same release.
Each `[[npm.packages]]` table describes one more package next to `[npm]`: its own `package_dir`
and package.json (which must be a version target, so every package carries the release version),
its own `pre_pack` commands, tarball contract, dist-tags, access and aliases.

```toml
[[npm.packages]]
package_dir = "packages/client"
pre_pack = [["pnpm", "build"]]
required_paths = ["package/package.json", "package/dist/index.js"]
forbidden_paths = ["package/src/*"]
```

- `vlr build-npm` packs every package, each in its own directory. `vlr validate-artifacts` checks
  each tarball against its own contract. Checksums, `vlr artifacts` and `vlr github-release`
  include every tarball.
- `vlr publish-npm` and `vlr verify-npm` cover every package with that package's dist-tag and
  access. Every name is planned on every registry before anything is uploaded. Aliases go first,
  then the `[[npm.packages]]` packages, then the `[npm]` package, so a name that cannot be
  published stops the run before the main package moves.
- `vlr check` rejects a package.json that is not a version target, and names or tarball file
  names that clash across packages. `vlr status --json` lists the package directories under
  `channels.npm_package_dirs`.
- Existing `[npm]` configurations behave exactly as before. A brand-new package on npmjs.com
  needs the same one-time trusted-publishing bootstrap as a new alias (`vlr help ci`).
