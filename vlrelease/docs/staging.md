# Staged release documents and the release history

## The files

| File | Who writes it | What it is |
|---|---|---|
| `.release/CHANGELOG_NEXT.md` | the agent, while working | package-facing changes for the next release (when `[debian]` is enabled) |
| `.release/RELEASE_NOTES_NEXT.md` | the agent, while working | the user-facing release notes for the next release |
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
