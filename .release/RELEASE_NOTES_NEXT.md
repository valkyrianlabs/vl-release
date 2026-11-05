<!--
vl-release staged release notes: the user-facing story of the NEXT release.
Maintained continuously while working; `vlr prepare` promotes it into the release notes
history (and the GitHub release) and resets this file to this template.

Format: the first line is "# <release title>" WITHOUT a version number (vlr adds it).
Everything after the title is the Markdown release body. Describe the resulting behavior
for users and operators; keep it representative of what actually ships.
-->
# Release-doc fragments: parallel pull requests stop conflicting

- Changes can now stage their release notes and changelog entries as **fragments**: one new file per change in
  `.release/notes.d/` and `.release/changelog.d/`, instead of editing the shared `RELEASE_NOTES_NEXT.md` and
  `CHANGELOG_NEXT.md`. Every pull request that edited those files conflicted with every other one on GitHub (which
  ignores `merge=union`), so each merge left the rest of the queue conflicting. New files never conflict.
- `vlr prepare` assembles the fragments after the `_NEXT` content, in file-name order. A notes fragment that starts
  with `## Section` is grouped under that section; changelog bullets of the same section are merged. The prepared
  fragments are deleted, and `vlr finalize` removes exactly the released ones from the release branch, keeping
  fragments merged after the cut for the next release.
- The release title stays on line 1 of `RELEASE_NOTES_NEXT.md`; with fragments staged, its body may be empty.
  Fragments without a title leave the release in `missing-docs`, and `vlr cut` and `vlr prepare` say so.
- `vlr status --json` lists the staged fragments. Fragment directories are configurable
  (`[release] notes_fragments`, `changelog_fragments`); `vlr help staging`, the agent skill and the README
  document the workflow. Repositories without fragment directories behave exactly as before.
