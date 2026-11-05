"""Per-change release-doc fragments (`.release/notes.d/`, `.release/changelog.d/`).

Every pull request that edited RELEASE_NOTES_NEXT.md / CHANGELOG_NEXT.md conflicted with every other one on GitHub
(which ignores `merge=union`), so each merge re-dirtied the rest of the queue. A fragment is a new file per change:
parallel branches never touch the same path, `vlr prepare` assembles them, and finalize removes exactly the ones
that shipped.
"""

from __future__ import annotations

import unittest

from tests.support import DEBIAN_CONFIG, git
from tests.test_finalize_cut import RemoteTestCase
from vlrelease import staging
from vlrelease.cut import _require_staged_docs
from vlrelease.errors import ReleaseError
from vlrelease.finalize import finalize_release
from vlrelease.prepare import prepare_release
from vlrelease.state import read_release_state

TITLE_ONLY = staging.RELEASE_NOTES_NEXT_TEMPLATE + "# Parallel fixes\n"


class FragmentParsingTests(unittest.TestCase):
    def test_notes_fragment_with_and_without_section(self) -> None:
        loose = staging.parse_notes_fragment("- Sign-in errors are generic.\n", source="a.md")
        self.assertEqual(loose, staging.NotesFragment("a.md", None, "- Sign-in errors are generic."))
        grouped = staging.parse_notes_fragment("<!-- hi -->\n## S3 gateway\n\n- Faster uploads.\n", source="b.md")
        self.assertEqual(grouped, staging.NotesFragment("b.md", "S3 gateway", "- Faster uploads."))
        self.assertIsNone(staging.parse_notes_fragment("<!-- only a comment -->\n", source="c.md"))

    def test_notes_fragment_rejections(self) -> None:
        cases = {
            "# Title\nx\n": "level-1",
            "## A\n\n- x\n\n## B\n- y\n": "only the first line",
            "## A\n": "no text",
            "TODO\n": "placeholder",
            "###### deep\n": "level-6",
        }
        for text, message in cases.items():
            with self.subTest(text=text), self.assertRaisesRegex(staging.StagingError, message):
                staging.parse_notes_fragment(text, source="f.md")

    def test_assembly_order_and_sections(self) -> None:
        fragments = [
            staging.NotesFragment("1.md", None, "- Loose one."),
            staging.NotesFragment("2.md", "S3 gateway", "- Gateway change."),
            staging.NotesFragment("3.md", "Sharing", "- Share change."),
            staging.NotesFragment("4.md", None, "- Loose two."),
        ]
        body = staging.assemble_notes("Intro.\n\n## S3 gateway\n\n- Existing.", fragments)
        self.assertEqual(
            body,
            "Intro.\n\n- Loose one.\n\n- Loose two.\n\n## S3 gateway\n\n- Existing.\n\n- Gateway change.\n\n"
            "## Sharing\n\n- Share change.",
        )
        self.assertEqual(staging.assemble_notes("", fragments[:1]), "- Loose one.")

    def test_changelog_merge(self) -> None:
        a = staging.parse_changelog_next("## Auth\n- One.\n")
        b = staging.parse_changelog_next("## Auth\n- Two.\n## S3\n- Three.\n")
        merged = staging.merge_changelog([a, b])
        self.assertEqual([(s.heading, [i.text for i in s.items]) for s in merged], [("Auth", ["One.", "Two."]), ("S3", ["Three."])])
        flat = staging.merge_changelog([staging.parse_changelog_next("- x\n"), staging.parse_changelog_next("- y\n")])
        self.assertEqual([i.text for i in flat[0].items], ["x", "y"])
        with self.assertRaisesRegex(staging.StagingError, "mixes bullets"):
            staging.merge_changelog([staging.parse_changelog_next("- x\n"), a])

    def test_ignored_files(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            for name in ("b.md", "a.md", "README.md", "_draft.md", ".hidden.md", "notes.txt"):
                (d / name).write_text("- x\n")
            self.assertEqual([p.name for p in staging.fragment_files(d)], ["a.md", "b.md"])
        self.assertEqual(staging.fragment_files(Path("/nonexistent/vlr")), [])


class FragmentReleaseTests(RemoteTestCase):
    def repo_with_fragments(self, **kwargs):
        root = self.make_repo(
            config=DEBIAN_CONFIG,
            debian=True,
            staged=False,
            files={
                ".release/RELEASE_NOTES_NEXT.md": TITLE_ONLY,
                ".release/notes.d/316-offline.md": "- Starts without internet.\n",
                ".release/notes.d/317-share.md": "## Sharing\n\n- Email codes are delivered.\n",
                ".release/changelog.d/316-offline.md": "- Bundle the password lists (#316).\n",
                ".release/changelog.d/317-share.md": "- Deliver email challenge codes (#317).\n",
            },
            **kwargs,
        )
        return root

    def test_parallel_branches_never_conflict(self) -> None:
        root = self.make_repo(config=DEBIAN_CONFIG, debian=True, staged=False, files={".release/RELEASE_NOTES_NEXT.md": TITLE_ONLY})
        for name in ("one", "two", "three"):
            git(root, "checkout", "-q", "-b", name, "main")
            self.write(root, f".release/notes.d/{name}.md", f"- Change {name}.\n")
            self.write(root, f".release/changelog.d/{name}.md", f"- Change {name}.\n")
            git(root, "add", "-A")
            git(root, "commit", "-q", "-m", name)
        git(root, "checkout", "-q", "main")
        for name in ("one", "two", "three"):
            git(root, "merge", "-q", "--no-ff", "--no-edit", name)  # raises on a conflict
        state = read_release_state(self.config(root))
        self.assertEqual([i.text for i in state.staged_changelog[0].items], ["Change one.", "Change three.", "Change two."])
        self.assertEqual(state.staged_notes.body, "- Change one.\n\n- Change three.\n\n- Change two.")

    def test_prepare_assembles_and_removes_the_fragments(self) -> None:
        root = self.repo_with_fragments(version="1.2.0")
        state = read_release_state(self.config(root))
        self.assertEqual(state.phase, "pending")
        self.assertEqual(len(state.fragment_paths), 4)
        result = prepare_release(self.config(root))
        self.assertEqual(sorted(result.removed), sorted(state.fragment_paths))
        notes = self.read(root, "RELEASE_NOTES.md")
        self.assertIn("## 1.2.0 — Parallel fixes", notes)
        self.assertIn("- Starts without internet.", notes)
        self.assertIn("### Sharing\n\n- Email codes are delivered.", notes)  # shifted under the release heading
        changelog = self.read(root, "debian/changelog")
        self.assertIn("Bundle the password lists (#316).", changelog)
        self.assertIn("Deliver email challenge codes (#317).", changelog)
        for path in state.fragment_paths:
            self.assertFalse((root / path).exists(), path)
        self.assertEqual(read_release_state(self.config(root)).phase, "prepared")

    def test_fragments_without_a_title_are_not_releasable(self) -> None:
        root = self.repo_with_fragments(version="1.2.0")
        self.write(root, ".release/RELEASE_NOTES_NEXT.md", staging.RELEASE_NOTES_NEXT_TEMPLATE)
        state = read_release_state(self.config(root))
        self.assertEqual(state.phase, "missing-docs")
        self.assertTrue(any("no '# <release title>'" in w for w in state.warnings), state.warnings)
        with self.assertRaisesRegex(ReleaseError, "release title"):
            prepare_release(self.config(root))
        with self.assertRaisesRegex(ReleaseError, "no release title"):
            _require_staged_docs(self.config(root))

    def test_invalid_fragment_is_an_error_naming_the_file(self) -> None:
        root = self.repo_with_fragments(version="1.2.0")
        self.write(root, ".release/notes.d/999-bad.md", "# Not allowed\n")
        state = read_release_state(self.config(root))
        self.assertEqual(state.phase, "invalid")
        self.assertTrue(any(".release/notes.d/999-bad.md" in e for e in state.errors), state.errors)

    def test_finalize_removes_released_fragments_and_keeps_later_ones(self) -> None:
        root = self.repo_with_fragments(version="1.2.0")
        bare = self.tmp / "origin.git"
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(bare))
        git(root, "remote", "add", "origin", str(bare))
        git(root, "push", "-q", "origin", "main")
        release_commit = git(root, "rev-parse", "HEAD")
        # Work merged after the cut adds its own fragment.
        self.write(root, ".release/notes.d/400-later.md", "- Arrives in the next release.\n")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "later work")
        git(root, "push", "-q", "origin", "main")

        ci = self.ci_checkout(bare, release_commit)
        prepare_release(self.config(ci))
        result = finalize_release(self.config(ci), log=lambda _l: None)
        self.assertEqual(result.status, "finalized")
        remaining = git(bare, "ls-tree", "-r", "--name-only", "main", "--", ".release/notes.d/", ".release/changelog.d/")
        self.assertEqual(remaining.splitlines(), [".release/notes.d/400-later.md"])
        git(root, "pull", "-q", "--ff-only", "origin", "main")
        state = read_release_state(self.config(root))
        self.assertEqual(state.phase, "unbumped")  # 1.2.0 recorded; the later fragment is staged for next time


if __name__ == "__main__":
    unittest.main()
