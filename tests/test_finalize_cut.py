from __future__ import annotations

import unittest
from pathlib import Path

from tests.support import DEBIAN_CONFIG, STAGED_CHANGELOG, RepoTestCase, git
from vlrelease import staging
from vlrelease.cut import cut_release
from vlrelease.errors import IntegrityError, ReleaseError
from vlrelease.finalize import finalize_release, subtract_released
from vlrelease.prepare import prepare_release, record_digests
from vlrelease.state import read_release_state


class RemoteTestCase(RepoTestCase):
    def with_remote(self, **kwargs) -> tuple[Path, Path]:
        root = self.make_repo(config=DEBIAN_CONFIG, debian=True, **kwargs)
        bare = self.tmp / "origin.git"
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(bare))
        git(root, "remote", "add", "origin", str(bare))
        git(root, "push", "-q", "origin", "main")
        git(root, "fetch", "-q", "origin")
        return root, bare

    def ci_checkout(self, bare: Path, ref: str = "main", name: str = "ci") -> Path:
        ci = self.tmp / name
        git(self.tmp, "clone", "-q", str(bare), str(ci))
        git(ci, "checkout", "-q", "--detach", ref)
        return ci


class FinalizeTests(RemoteTestCase):
    def test_records_release_on_unchanged_branch(self) -> None:
        _root, bare = self.with_remote(version="1.2.0")
        ci = self.ci_checkout(bare)
        prepare_release(self.config(ci))
        result = finalize_release(self.config(ci), log=lambda _l: None)
        self.assertEqual(result.status, "finalized")
        self.assertEqual(set(result.changed), {"RELEASE_NOTES.md", "debian/changelog", ".release/RELEASE_NOTES_NEXT.md", ".release/CHANGELOG_NEXT.md"})
        self.assertEqual(git(bare, "log", "-1", "--format=%s", "main"), "chore(release): record v1.2.0")
        self.assertEqual(git(bare, "show", "main:RELEASE_NOTES.md"), (ci / "RELEASE_NOTES.md").read_text().rstrip("\n"))
        self.assertEqual(git(bare, "show", "main:.release/CHANGELOG_NEXT.md") + "\n", staging.CHANGELOG_NEXT_TEMPLATE)
        # The branch is now in the `prepared` phase for 1.2.0; a re-run is a no-op.
        again = finalize_release(self.config(ci), log=lambda _l: None)
        self.assertEqual(again.status, "already-finalized")

    def test_branch_advanced_keeps_newer_staged_work(self) -> None:
        root, bare = self.with_remote(version="1.2.0")
        release_commit = git(root, "rev-parse", "HEAD")
        # Development continues after the tag: new staged bullets and code.
        self.write(root, ".release/CHANGELOG_NEXT.md", self.read(root, ".release/CHANGELOG_NEXT.md") + "- A newer change.\n")
        self.write(root, "src.txt", "more code\n")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "more work")
        git(root, "push", "-q", "origin", "main")
        ci = self.ci_checkout(bare, release_commit)
        prepare_release(self.config(ci))
        result = finalize_release(self.config(ci), log=lambda _l: None)
        self.assertEqual(result.status, "finalized")
        staged = git(bare, "show", "main:.release/CHANGELOG_NEXT.md")
        self.assertIn("- A newer change.", staged)
        self.assertNotIn("Add the frobnicator", staged)
        self.assertEqual(git(bare, "show", "main:src.txt"), "more code")
        # Notes were fully released: back to the template.
        self.assertEqual(git(bare, "show", "main:.release/RELEASE_NOTES_NEXT.md") + "\n", staging.RELEASE_NOTES_NEXT_TEMPLATE)
        git(root, "pull", "-q", "--ff-only", "origin", "main")
        state = read_release_state(self.config(root))
        self.assertEqual(state.phase, "unbumped")  # 1.2.0 recorded, new work staged

    def test_edited_released_line_is_refused(self) -> None:
        root, bare = self.with_remote(version="1.2.0")
        release_commit = git(root, "rev-parse", "HEAD")
        self.write(root, ".release/CHANGELOG_NEXT.md", "- Add the frobnicator (reworded).\n- Fix a crash when the widget is empty.\n")
        git(root, "commit", "-q", "-am", "reword")
        git(root, "push", "-q", "origin", "main")
        ci = self.ci_checkout(bare, release_commit)
        prepare_release(self.config(ci))
        with self.assertRaisesRegex(ReleaseError, "was edited on the release branch"):
            finalize_release(self.config(ci), log=lambda _l: None)
        self.assertEqual(git(bare, "log", "-1", "--format=%s", "main"), "reword")

    def test_record_mismatch_is_refused(self) -> None:
        _root, bare = self.with_remote(version="1.2.0")
        ci = self.ci_checkout(bare)
        prepare_release(self.config(ci))
        record = record_digests(self.config(ci))
        record["RELEASE_NOTES.md"] = "0" * 64
        with self.assertRaises(IntegrityError):
            finalize_release(self.config(ci), record=record, log=lambda _l: None)

    def test_requires_prepared_tree(self) -> None:
        _root, bare = self.with_remote(version="1.2.0")
        ci = self.ci_checkout(bare)
        with self.assertRaisesRegex(ReleaseError, "not prepared"):
            finalize_release(self.config(ci), log=lambda _l: None)

    def test_release_commit_must_be_on_branch(self) -> None:
        root, bare = self.with_remote(version="1.2.0")
        git(root, "checkout", "-q", "-b", "side")
        self.write(root, "side.txt", "x")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "side")
        git(root, "push", "-q", "origin", "side")
        ci = self.ci_checkout(bare, "origin/side")
        prepare_release(self.config(ci))
        with self.assertRaisesRegex(ReleaseError, "not on origin/main"):
            finalize_release(self.config(ci), log=lambda _l: None)

    def test_dry_run_pushes_nothing(self) -> None:
        _root, bare = self.with_remote(version="1.2.0")
        ci = self.ci_checkout(bare)
        prepare_release(self.config(ci))
        result = finalize_release(self.config(ci), dry_run=True, log=lambda _l: None)
        self.assertEqual(result.status, "dry-run")
        self.assertEqual(git(bare, "log", "-1", "--format=%s", "main"), "initial")

    def test_subtract_released(self) -> None:
        template = staging.CHANGELOG_NEXT_TEMPLATE
        self.assertEqual(subtract_released(template + "- a\n", template + "- a\n", template=template, source="x"), template)
        kept = subtract_released(template + "- a\n- b\n", template + "- a\n", template=template, source="x")
        self.assertEqual(kept, template + "\n- b\n")
        with self.assertRaises(ReleaseError):
            subtract_released(template + "- A\n", template + "- a\n", template=template, source="x")


class CutTests(RemoteTestCase):
    def test_cut_patch_commits_tags_and_pushes(self) -> None:
        root, bare = self.with_remote(version="1.2.0")
        result = cut_release(self.config(root), "patch", push=True, log=lambda _l: None)
        self.assertEqual((result.version, result.tag, result.pushed), ("1.2.1", "v1.2.1", True))
        self.assertEqual(self.read(root, "VERSION"), "1.2.1\n")
        self.assertIn('"version": "1.2.1"', self.read(root, "package.json"))
        self.assertEqual(git(bare, "log", "-1", "--format=%s", "main"), "chore(release): v1.2.1")
        self.assertEqual(git(bare, "rev-parse", "v1.2.1^{commit}"), git(root, "rev-parse", "HEAD"))
        # Staged docs travel with the tag; they are promoted in CI, not here.
        self.assertIn(STAGED_CHANGELOG.splitlines()[0], git(bare, "show", "v1.2.1:.release/CHANGELOG_NEXT.md"))

    def test_refuses_without_staged_docs(self) -> None:
        root, _bare = self.with_remote(version="1.2.0", staged=False)
        with self.assertRaisesRegex(ReleaseError, "would fail `vlr prepare`"):
            cut_release(self.config(root), "minor", log=lambda _l: None)

    def test_refuses_dirty_tree_and_wrong_branch(self) -> None:
        root, _bare = self.with_remote(version="1.2.0")
        self.write(root, "VERSION", "9.9.9\n")
        with self.assertRaisesRegex(ReleaseError, "uncommitted"):
            cut_release(self.config(root), "patch", log=lambda _l: None)
        git(root, "checkout", "-q", "--", "VERSION")
        git(root, "checkout", "-q", "-b", "feature")
        with self.assertRaisesRegex(ReleaseError, "releases are cut from `main`"):
            cut_release(self.config(root), "patch", log=lambda _l: None)

    def test_resume_pushes_existing_local_tag(self) -> None:
        root, bare = self.with_remote(version="1.2.0")
        first = cut_release(self.config(root), "minor", push=False, log=lambda _l: None)
        self.assertFalse(first.pushed)
        resumed = cut_release(self.config(root), "1.3.0", push=True, log=lambda _l: None)
        self.assertTrue(resumed.pushed)
        self.assertIn("resume: local tag v1.3.0 found", resumed.actions)
        self.assertEqual(git(bare, "rev-parse", "v1.3.0^{commit}"), first.commit)
        with self.assertRaisesRegex(ReleaseError, "already exists on origin"):
            cut_release(self.config(root), "1.3.0", push=True, log=lambda _l: None)

    def test_failing_tests_block_the_cut(self) -> None:
        config_text = DEBIAN_CONFIG + '\n[release]\ntest_command = ["false"]\n'
        root = self.make_repo(config=config_text, debian=True, version="1.2.0", name="tested")
        bare = self.tmp / "tested.git"
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(bare))
        git(root, "remote", "add", "origin", str(bare))
        git(root, "push", "-q", "origin", "main")
        with self.assertRaisesRegex(ReleaseError, "test_command failed"):
            cut_release(self.config(root), "patch", log=lambda _l: None)
        self.assertEqual(self.read(root, "VERSION"), "1.2.0\n")


if __name__ == "__main__":
    unittest.main()
