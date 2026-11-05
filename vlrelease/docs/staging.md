# Staged release documents and the release history

## The files

| File | Who writes it | What it is |
|---|---|---|
| `.release/CHANGELOG_NEXT.md` | the agent, while working | package-facing changes for the next release (when `[debian]` is enabled) |
| `.release/RELEASE_NOTES_NEXT.md` | the agent, while working | the user-facing release notes for the next release |
| `.release/notes.d/*.md` | one new file per change | release-note fragments, assembled into the notes |
| `.release/changelog.d/*.md` | one new file per change | changelog fragments, merged into the changelog |
| `debian/changelog` | `vlr prepare` only | published Debian history |
| `RELEASE_NOTES.md` | `vlr prepare` only | published release-notes history |

`.release/` is tracked. HTML comments are ignored everywhere, so the reset templates (pure
comments) count as empty. Paths are configurable in `release.toml` (`vlr help config`).

## `CHANGELOG_NEXT.md`

Concise, technical, one bullet per change. No Debian boilerplate: `vlr prepare` renders the
stanza (header, wrapping, maintainer trailer, date).

```markdown
- Add the frobnicator.
- Fix a crash when the widget is empty.
  Continuation lines are indented by two or more spaces.
  - One level of nested detail bullets is allowed.
```

Optional `## Section` headings group bullets; if one is used, every bullet must be under a
section. Rendered as `  * Section` / `    - item` / `      + detail`. Nothing else is allowed
(no paragraphs, no other heading levels). Placeholders (`TODO`, `TBD`, `<describe>`, `...`)
are rejected.

## `RELEASE_NOTES_NEXT.md`

```markdown
# Frobnication arrives

Markdown body: paragraphs, `##` sections, lists, code blocks.
```

- Line 1 is `# <title>` **without a version number**; the version is chosen at release time and
  `vlr prepare` adds it. The GitHub release title is `[release] title` (default
  `v{version} — {title}`).
- The body (everything after the title) must not be empty and may not use `#` or `######`
  headings.
- Describe what users and operators get; keep it representative of what actually ships.

## Fragments: one file per change (use these on shared branches)

Every branch that edits `RELEASE_NOTES_NEXT.md` or `CHANGELOG_NEXT.md` conflicts with every other branch that does,
and GitHub ignores `merge=union`, so with several pull requests open each merge re-dirties the rest. Fragments avoid
that: each change adds **new files** that no other branch touches.

- `.release/notes.d/<name>.md`: user-facing Markdown for this change. It may start with one `## Section` heading;
  fragments naming the same section are grouped under it (joining a `## Section` the body of
  `RELEASE_NOTES_NEXT.md` already has). No other `#`/`##` headings, no placeholders.
- `.release/changelog.d/<name>.md` (with `[debian]`): the same bullet format as `CHANGELOG_NEXT.md`; bullets of the
  same `## Section` are merged.
- Name files after the change, e.g. `316-offline-startup.md`; they are assembled in file-name order after the
  `_NEXT` files' content. `README.md` and names starting with `_` or `.` are ignored.
- The release **title** still comes from line 1 of `RELEASE_NOTES_NEXT.md`, written once when the release is
  planned; with fragments staged, its body may be empty. Fragments without a title leave the release in
  `missing-docs` (`vlr status` says so) and `vlr prepare` refuses.
- `vlr prepare` deletes the fragments it promoted; `vlr finalize` removes the released ones from the release
  branch and keeps fragments added after the cut for the next release.
- Directories are configurable: `[release] notes_fragments`, `changelog_fragments`.

## Maintaining them well

- Update them in the same change as the code they describe.
- Consolidate: merge and rewrite bullets so the files describe the resulting behavior instead of
  accumulating a log of every commit, fix-up or abandoned attempt.
- When a change is reverted or reshaped, update or remove its entry.
- Purely internal refactors with no observable effect need no entry.
- Preview with `vlr prepare --dry-run`, `vlr release-title --staged`, `vlr release-body --staged`.

## How `vlr prepare` promotes them

For the canonical version V (CI runs this on the release checkout; it never commits):

| Newest recorded release | Staged docs | Result |
|---|---|---|
| older than V | complete | render, prepend to the history files, reset staging to the templates |
| older than V | empty | error (`--allow-empty-patch` writes a maintenance entry, patch releases only) |
| V | empty | idempotent no-op (re-run of an already prepared release) |
| V | populated | error: bump the version first (`vlr version bump …`) |
| newer than V, or the history files disagree | – | error |

Release dates come from the release commit (or `SOURCE_DATE_EPOCH`), so preparing the same
commit twice produces byte-identical files.

`RELEASE_NOTES.md` entries look like this (the marker makes them machine-readable; body
headings are shifted down one level and restored by `vlr release-body`):

```markdown
<!-- vl-release:entry version=1.4.0 -->
## 1.4.0 — Frobnication arrives

_Released 2026-10-03_

Body…
```

After publication succeeds, `vlr finalize` commits the promoted history and the cleared staging
files to the release branch. Until then the repository keeps the populated `_NEXT` files.
