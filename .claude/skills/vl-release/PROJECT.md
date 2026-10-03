<!-- vl-release:generated-local -- regenerate with `vlr install-local-skill` after changing release.toml; local edits are overwritten -->
# vl-release: release specifics

Generated from this repository's `release.toml`. The general workflow is in `SKILL.md`;
this file says how it applies here.

## Files

| Purpose | Path |
|---|---|
| Release contract | `release.toml` |
| Staged changelog (keep current) | `.release/CHANGELOG_NEXT.md` |
| Staged release notes (keep current) | `.release/RELEASE_NOTES_NEXT.md` |
| Published release notes (do not edit) | `RELEASE_NOTES.md` |
| Published Debian changelog (do not edit) | `debian/changelog` |

## Versions

- Canonical: `VERSION`
- Kept in sync by `vlr version …`: `vlrelease/__init__.py` (regex)
- Tags: `vX.Y.Z` on branch `main`; GitHub release title: `vX.Y.Z — <title>`

## Release channels

- **Debian packages** (built by `vlr build-deb` from `debian/`, revision 1):
  - `vl-release`: architecture `all`; must ship `usr/bin/vl-release`, `usr/bin/vlr`, `usr/lib/python3/dist-packages/vlrelease/cli.py`, `usr/lib/python3/dist-packages/vlrelease/skill/SKILL.md.in`, `usr/lib/python3/dist-packages/vlrelease/skill/PROJECT.md.in`, `usr/lib/python3/dist-packages/vlrelease/docs/config.md`, `usr/share/doc/vl-release/README.md`, `usr/share/doc/vl-release/RELEASE_NOTES.md`; must never ship `*__pycache__*`, `*.pyc`, `usr/lib/python3/dist-packages/tests*`; ships byte-identical copies of `RELEASE_NOTES.md`, `vlrelease/__init__.py`
- **APT**: https://apt.valkyrianlabs.com (suite `stable`, components main), published by release CI with `vlr publish-deb`
- **Source archive** of the prepared tree, attached to the GitHub release
- **Homebrew**: formula template `packaging/homebrew/vl-release.rb`, published to `valkyrianlabs/homebrew-tap` as `Formula/vl-release.rb`

## Before considering substantial work complete

```sh
vlr check
python3 -m unittest discover -s tests -t .
```
