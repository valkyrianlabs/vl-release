from __future__ import annotations

import json
import shutil
import subprocess
import unittest

from tests.support import DEBIAN_CONFIG, STAGED_NOTES, RepoTestCase, git, requires_tools
from vlrelease import staging
from vlrelease.errors import ReleaseError
from vlrelease.prepare import current_entry, prepare_release, staged_entry
from vlrelease.state import read_release_state


class PrepareTests(RepoTestCase):
    def debian_repo(self, **kwargs):
        return self.make_repo(config=DEBIAN_CONFIG, debian=True, **kwargs)

    def prepare(self, root, **kwargs):
        return prepare_release(self.config(root), **kwargs)

    def test_populated_staging_is_promoted_and_reset(self) -> None:
        root = self.debian_repo(version="1.2.0")
        result = self.prepare(root)
        self.assertEqual(result.status, "prepared")
        self.assertEqual(result.debian_version, "1.2.0-1")
        self.assertEqual(result.release_title, "v1.2.0 — Frobnication arrives")
        changelog = self.read(root, "debian/changelog")
        self.assertTrue(changelog.startswith("demo (1.2.0-1) unstable; urgency=medium\n\n  * Add the frobnicator.\n"))
        self.assertIn(" -- Demo Maintainer <demo@example.com>  Thu, 01 Oct 2026 12:00:00 +0000\n", changelog)
        notes = self.read(root, "RELEASE_NOTES.md")
        self.assertTrue(notes.startswith("# Demo release notes\n\n<!-- vl-release:entry version=1.2.0 -->\n## 1.2.0 — Frobnication arrives\n"))
        self.assertIn("_Released 2026-10-01_", notes)
        self.assertIn("### Upgrading", notes)
        self.assertEqual(self.read(root, ".release/CHANGELOG_NEXT.md"), staging.CHANGELOG_NEXT_TEMPLATE)
        self.assertEqual(self.read(root, ".release/RELEASE_NOTES_NEXT.md"), staging.RELEASE_NOTES_NEXT_TEMPLATE)
        self.assertEqual(read_release_state(self.config(root)).phase, "prepared")
        # prepare never commits
        self.assertIn("RELEASE_NOTES.md", git(root, "status", "--porcelain"))

    @requires_tools("dpkg-parsechangelog")
    def test_debian_stanza_is_valid_for_dpkg(self) -> None:
        root = self.debian_repo(version="1.2.0")
        self.prepare(root)
        parsed = subprocess.run(["dpkg-parsechangelog", "-l", "debian/changelog"], cwd=root, capture_output=True, text=True, check=True).stdout
        self.assertIn("Version: 1.2.0-1", parsed)
        self.assertIn("Source: demo", parsed)

    def test_history_is_preserved(self) -> None:
        root = self.debian_repo(version="1.2.0")
        self.prepare(root)
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "record 1.2.0")
        first_changelog = self.read(root, "debian/changelog")
        first_notes = self.read(root, "RELEASE_NOTES.md")
        self.write(root, ".release/CHANGELOG_NEXT.md", staging.CHANGELOG_NEXT_TEMPLATE + "- Next change.\n")
        self.write(root, ".release/RELEASE_NOTES_NEXT.md", staging.RELEASE_NOTES_NEXT_TEMPLATE + "# Next\n\nMore.\n")
        self.write(root, "VERSION", "1.3.0\n")
        self.write(root, "package.json", self.read(root, "package.json").replace("1.2.0", "1.3.0"))
        self.prepare(root)
        changelog = self.read(root, "debian/changelog")
        notes = self.read(root, "RELEASE_NOTES.md")
        self.assertTrue(changelog.startswith("demo (1.3.0-1)"))
        self.assertTrue(changelog.endswith(first_changelog))
        self.assertIn(first_notes.split("\n", 2)[2], notes)
        self.assertLess(notes.index("version=1.3.0"), notes.index("version=1.2.0"))

    def test_repeated_prepare_is_idempotent(self) -> None:
        root = self.debian_repo(version="1.2.0")
        self.prepare(root)
        snapshot = {p: self.read(root, p) for p in ("debian/changelog", "RELEASE_NOTES.md")}
        again = self.prepare(root)
        self.assertEqual(again.status, "already-prepared")
        self.assertEqual(again.title, "Frobnication arrives")
        self.assertEqual({p: self.read(root, p) for p in snapshot}, snapshot)

    def test_same_version_retry_reuses_existing_history(self) -> None:
        # Publication failed after a committed preparation: history has 1.2.0 and staging is empty.
        root = self.debian_repo(version="1.2.0")
        self.prepare(root)
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "record")
        clone = self.tmp / "retry"
        git(self.tmp, "clone", "-q", str(root), str(clone))
        result = self.prepare(clone)
        self.assertEqual(result.status, "already-prepared")
        self.assertEqual(self.read(clone, "RELEASE_NOTES.md").count("vl-release:entry version=1.2.0"), 1)
        self.assertEqual(self.read(clone, "debian/changelog").count("(1.2.0-1)"), 1)

    def test_prepare_is_deterministic_across_checkouts(self) -> None:
        root = self.debian_repo(version="1.2.0")
        clone = self.tmp / "clone"
        git(self.tmp, "clone", "-q", str(root), str(clone))
        self.prepare(root)
        self.prepare(clone)
        for path in ("debian/changelog", "RELEASE_NOTES.md"):
            self.assertEqual(self.read(root, path), self.read(clone, path))

    def test_new_release_with_empty_staging_fails(self) -> None:
        root = self.debian_repo(version="1.2.0", staged=False)
        with self.assertRaisesRegex(ReleaseError, "staged release documentation is empty"):
            self.prepare(root)

    def test_partially_staged_debian_release_fails(self) -> None:
        root = self.debian_repo(version="1.2.0")
        self.write(root, ".release/CHANGELOG_NEXT.md", staging.CHANGELOG_NEXT_TEMPLATE)
        with self.assertRaisesRegex(ReleaseError, "CHANGELOG_NEXT"):
            self.prepare(root)

    def _released(self, version: str):
        root = self.debian_repo(version=version)
        self.prepare(root)
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "record")
        return root

    def _bump(self, root, version: str) -> None:
        self.write(root, "VERSION", f"{version}\n")
        code, out, err = self.vlr("version", "sync", cwd=root)
        self.assertEqual(code, 0, out + err)

    def test_empty_minor_and_major_fail_even_with_override(self) -> None:
        for version in ("1.3.0", "2.0.0"):
            with self.subTest(version=version):
                root = self._released("1.2.0")
                self._bump(root, version)
                with self.assertRaisesRegex(ReleaseError, "only applies to patch releases"):
                    self.prepare(root, allow_empty_patch=True)
                with self.assertRaisesRegex(ReleaseError, "staged release documentation is empty"):
                    self.prepare(root)

    def test_empty_patch_requires_explicit_override(self) -> None:
        root = self._released("1.2.0")
        self._bump(root, "1.2.1")
        with self.assertRaises(ReleaseError):
            self.prepare(root)
        result = self.prepare(root, allow_empty_patch=True)
        self.assertEqual(result.title, "Maintenance release")
        self.assertIn("  * Maintenance release.", self.read(root, "debian/changelog"))

    def test_first_release_cannot_be_empty_patch(self) -> None:
        root = self.debian_repo(version="0.0.1", staged=False)
        with self.assertRaisesRegex(ReleaseError, "no previous release"):
            self.prepare(root, allow_empty_patch=True)

    def test_placeholders_are_rejected(self) -> None:
        root = self.debian_repo(version="1.2.0")
        self.write(root, ".release/CHANGELOG_NEXT.md", "- TODO\n")
        with self.assertRaisesRegex(ReleaseError, "placeholder"):
            self.prepare(root)

    def test_unbumped_version_with_new_staging_is_refused(self) -> None:
        root = self._released("1.2.0")
        self.write(root, ".release/CHANGELOG_NEXT.md", "- Another change.\n")
        self.write(root, ".release/RELEASE_NOTES_NEXT.md", STAGED_NOTES)
        state = read_release_state(self.config(root))
        self.assertEqual(state.phase, "unbumped")
        with self.assertRaisesRegex(ReleaseError, "Bump the version"):
            self.prepare(root)

    def test_version_must_increase(self) -> None:
        root = self._released("1.2.0")
        self.write(root, ".release/CHANGELOG_NEXT.md", "- Another change.\n")
        self.write(root, ".release/RELEASE_NOTES_NEXT.md", STAGED_NOTES)
        self._bump(root, "1.1.0")
        with self.assertRaisesRegex(ReleaseError, "not newer than the last recorded release"):
            self.prepare(root)

    def test_inconsistent_history_is_refused(self) -> None:
        root = self._released("1.2.0")
        self.write(root, "RELEASE_NOTES.md", "# Demo release notes\n")
        with self.assertRaisesRegex(ReleaseError, "recorded in debian/changelog but not in RELEASE_NOTES.md"):
            self.prepare(root)

    def test_mismatched_versions_block_prepare(self) -> None:
        root = self.debian_repo(version="1.2.0")
        self.write(root, "package.json", self.read(root, "package.json").replace("1.2.0", "1.1.0"))
        with self.assertRaisesRegex(ReleaseError, "package.json"):
            self.prepare(root)

    def test_debian_revision_and_environment_overrides(self) -> None:
        root = self.debian_repo(version="1.2.0")
        import os

        os.environ["RELEASE_DEBIAN_DISTRIBUTION"] = "stable"
        try:
            result = self.prepare(root, debian_revision=3)
        finally:
            os.environ.pop("RELEASE_DEBIAN_DISTRIBUTION")
        self.assertEqual(result.debian_version, "1.2.0-3")
        self.assertTrue(self.read(root, "debian/changelog").startswith("demo (1.2.0-3) stable; urgency=medium"))

    def test_dry_run_writes_nothing(self) -> None:
        root = self.debian_repo(version="1.2.0")
        result = self.prepare(root, dry_run=True)
        self.assertIn("debian/changelog", result.preview)
        self.assertFalse((root / "debian/changelog").exists())
        self.assertEqual(read_release_state(self.config(root)).phase, "pending")

    def test_non_debian_repository(self) -> None:
        root = self.make_repo(version="0.3.0")
        result = self.prepare(root)
        self.assertIsNone(result.debian_version)
        self.assertEqual(result.changed, ["RELEASE_NOTES.md", ".release/RELEASE_NOTES_NEXT.md"])

    def test_changelog_next_content_without_debian_is_an_error(self) -> None:
        root = self.make_repo(version="0.3.0", files={".release/CHANGELOG_NEXT.md": "- orphaned\n"})
        with self.assertRaisesRegex(ReleaseError, "nothing would publish it"):
            self.prepare(root)

    def test_title_and_body_extraction(self) -> None:
        root = self.debian_repo(version="1.2.0")
        staged = staged_entry(self.config(root))
        self.assertEqual(staged.title, "Frobnication arrives")
        with self.assertRaisesRegex(ReleaseError, "not prepared"):
            current_entry(self.config(root))
        self.prepare(root)
        entry = current_entry(self.config(root))
        self.assertEqual(entry.body, staged.body)
        code, out, _ = self.vlr("release-title", "--json", cwd=root)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["release_title"], "v1.2.0 — Frobnication arrives")
        code, out, _ = self.vlr("release-body", cwd=root)
        self.assertEqual(out, staged.body + "\n")
        self.vlr("release-body", "--output", str(self.tmp / "body.md"), cwd=root)
        self.assertEqual((self.tmp / "body.md").read_text(), staged.body + "\n")

    def test_cli_prepare_json_and_record(self) -> None:
        root = self.debian_repo(version="1.2.0")
        record = self.tmp / "meta" / "prepare.json"
        code, out, err = self.vlr("prepare", "--json", "--record", str(record), cwd=root)
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "prepared")
        self.assertEqual(json.loads(record.read_text())["record"], payload["record"])
        self.assertEqual(set(payload["record"]), {"RELEASE_NOTES.md", ".release/RELEASE_NOTES_NEXT.md", "debian/changelog", ".release/CHANGELOG_NEXT.md"})

    def test_maintainer_falls_back_to_previous_entry_and_control(self) -> None:
        config_text = DEBIAN_CONFIG.replace('maintainer = "Demo Maintainer <demo@example.com>"\n', "")
        root = self.make_repo(config=config_text, debian=True, version="1.2.0")
        self.prepare(root)
        self.assertIn(" -- Demo Maintainer <demo@example.com>  ", self.read(root, "debian/changelog"))


if __name__ == "__main__":
    unittest.main()
