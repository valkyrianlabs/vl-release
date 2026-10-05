from __future__ import annotations

import gzip
import os
import shutil
import tarfile
import unittest
from pathlib import Path

from tests.support import DEBIAN_CONFIG, RepoTestCase, build_fake_deb, git, requires_tools
from vlrelease.artifacts import validate_artifacts
from vlrelease.checksums import SHA256SUMS_NAME, read_sha256sums, verify_sha256sums, write_sha256sums
from vlrelease.debbuild import build_debian
from vlrelease.errors import ReleaseError
from vlrelease.prepare import prepare_release
from vlrelease.sourcearchive import build_source_archive


class ChecksumTests(RepoTestCase):
    def test_write_and_verify(self) -> None:
        out = self.tmp / "release"
        out.mkdir()
        (out / "a.deb").write_bytes(b"a")
        (out / "b.tar.gz").write_bytes(b"b")
        (out / "build-deb.log").write_text("log")
        (out / "meta").mkdir()
        (out / "meta" / "x.json").write_text("{}")
        write_sha256sums(out)
        self.assertEqual(set(read_sha256sums(out / SHA256SUMS_NAME)), {"a.deb", "b.tar.gz"})
        self.assertEqual(verify_sha256sums(out), [])
        (out / "a.deb").write_bytes(b"tampered")
        (out / "c.deb").write_bytes(b"new")
        issues = verify_sha256sums(out)
        self.assertTrue(any("a.deb: sha256" in issue for issue in issues))
        self.assertTrue(any("c.deb is not listed" in issue for issue in issues))

    def test_malformed_and_missing(self) -> None:
        out = self.tmp / "release"
        out.mkdir()
        self.assertIn("missing", verify_sha256sums(out)[0])
        (out / SHA256SUMS_NAME).write_text("nonsense\n")
        self.assertIn("malformed", verify_sha256sums(out)[0])


class PreparedDebianRepo(RepoTestCase):
    def prepared_repo(self) -> Path:
        root = self.make_repo(config=DEBIAN_CONFIG, debian=True, version="1.2.0")
        prepare_release(self.config(root))
        return root


@requires_tools("dpkg-buildpackage", "dpkg-deb", "dh")
@unittest.skipIf(
    os.environ.get("VLR_PACKAGE_BUILD") == "1",
    "nested dpkg-buildpackage: covered by the regular test runs, and minutes slow inside the package build",
)
class DebianBuildTests(PreparedDebianRepo):
    def test_builds_from_prepared_work_tree_inside_the_project(self) -> None:
        root = self.prepared_repo()
        # The prepared docs are uncommitted: a build from Git would not contain them.
        self.assertIn("debian/changelog", git(root, "status", "--porcelain", "--untracked-files=all"))
        siblings_before = sorted(path.name for path in root.parent.iterdir())
        result = build_debian(self.config(root))
        self.assertEqual(sorted(path.name for path in root.parent.iterdir()), siblings_before, "dpkg-buildpackage wrote outside the project")
        names = sorted(path.name for path in result.artifacts)
        self.assertIn("demo_1.2.0-1_all.deb", names)
        self.assertTrue(all(path.parent == root / "release" for path in result.artifacts))
        self.assertTrue((root / "build" / "deb" / "src" / "debian" / "changelog").is_file())
        write_sha256sums(root / "release")
        report = validate_artifacts(self.config(root))
        self.assertEqual(report.issues, [])
        self.assertIn("demo_1.2.0-1_all.deb", report.checked)

    def test_refuses_unprepared_tree(self) -> None:
        root = self.make_repo(config=DEBIAN_CONFIG, debian=True, version="1.2.0")
        with self.assertRaisesRegex(ReleaseError, "not prepared"):
            build_debian(self.config(root))

    def test_build_failure_reports_log(self) -> None:
        root = self.prepared_repo()
        (root / "debian" / "rules").write_text("#!/usr/bin/make -f\n%:\n\tfalse\n")
        with self.assertRaisesRegex(ReleaseError, "build-deb.log"):
            build_debian(self.config(root))
        self.assertTrue((root / "release" / "build-deb.log").is_file())


@requires_tools("dpkg-deb")
class ContractTests(PreparedDebianRepo):
    def stage(self, root: Path, files: dict[str, bytes], *, version: str = "1.2.0-1", package: str = "demo") -> Path:
        out = root / "release"
        out.mkdir(exist_ok=True)
        deb = build_fake_deb(out, package=package, version=version, files=files)
        write_sha256sums(out)
        return deb

    def good_files(self, root: Path) -> dict[str, bytes]:
        changelog = gzip.compress((root / "debian" / "changelog").read_bytes())
        return {
            "usr/bin/demo": b"#!/bin/sh\n",
            "usr/share/doc/demo/changelog.Debian.gz": changelog,
            "usr/share/doc/demo/RELEASE_NOTES.md": (root / "RELEASE_NOTES.md").read_bytes(),
        }

    def issues(self, root: Path) -> list[str]:
        return validate_artifacts(self.config(root)).issues

    def test_valid_package(self) -> None:
        root = self.prepared_repo()
        self.stage(root, self.good_files(root))
        self.assertEqual(self.issues(root), [])

    def test_required_path_missing(self) -> None:
        root = self.prepared_repo()
        files = self.good_files(root)
        del files["usr/bin/demo"]
        self.stage(root, files)
        self.assertTrue(any("required path missing: usr/bin/demo" in i for i in self.issues(root)))

    def test_forbidden_path_present(self) -> None:
        root = self.prepared_repo()
        files = self.good_files(root) | {"usr/lib/demo/x.pyc": b"\0"}
        self.stage(root, files)
        self.assertTrue(any("forbidden path present" in i for i in self.issues(root)))

    def test_identical_file_mismatch(self) -> None:
        root = self.prepared_repo()
        files = self.good_files(root) | {"usr/share/doc/demo/RELEASE_NOTES.md": b"stale notes"}
        self.stage(root, files)
        self.assertTrue(any("differs from the work tree's RELEASE_NOTES.md" in i for i in self.issues(root)))

    def test_package_must_carry_prepared_changelog(self) -> None:
        root = self.prepared_repo()
        stale = "demo (1.1.0-1) unstable; urgency=medium\n\n  * old\n\n -- A <a@example.com>  Thu, 01 Oct 2026 12:00:00 +0000\n"
        files = self.good_files(root) | {"usr/share/doc/demo/changelog.Debian.gz": gzip.compress(stale.encode())}
        self.stage(root, files)
        self.assertTrue(any("not built from the prepared work tree" in i for i in self.issues(root)))

    def test_wrong_version_and_unknown_package(self) -> None:
        root = self.prepared_repo()
        self.stage(root, self.good_files(root), version="1.1.0-1")
        self.stage(root, self.good_files(root), package="intruder")
        issues = self.issues(root)
        self.assertTrue(any("not the prepared Debian version" in i for i in issues))
        self.assertTrue(any("intruder has no [[debian.packages]] contract" in i for i in issues))

    def test_checksum_tamper_detected(self) -> None:
        root = self.prepared_repo()
        deb = self.stage(root, self.good_files(root))
        deb.write_bytes(deb.read_bytes() + b"\0")
        self.assertTrue(any("does not match SHA256SUMS" in i for i in self.issues(root)))


class SourceArchiveTests(RepoTestCase):
    CONFIG = DEBIAN_CONFIG + '\n[source_archive]\nexclude = ["secret/*"]\n'

    def prepared(self, name: str = "repo") -> Path:
        root = self.make_repo(config=self.CONFIG, debian=True, version="1.2.0", name=name, files={"secret/key": "x", "src/a.py": "print(1)\n"})
        prepare_release(self.config(root))
        return root

    def test_archive_contains_prepared_tree_and_is_reproducible(self) -> None:
        root = self.prepared()
        (root / "release").mkdir()
        (root / "release" / "junk.deb").write_text("x")
        (root / "untracked.txt").write_text("not shipped")
        first = build_source_archive(self.config(root))
        self.assertEqual(first.name, "demo-1.2.0.tar.gz")
        with tarfile.open(first) as archive:
            names = set(archive.getnames())
            self.assertIn("demo-1.2.0/debian/changelog", names)  # prepared, never committed
            self.assertIn("demo-1.2.0/src/a.py", names)
            self.assertNotIn("demo-1.2.0/secret/key", names)
            self.assertNotIn("demo-1.2.0/untracked.txt", names)
            self.assertFalse(any("/release/" in name for name in names))
            notes = archive.extractfile("demo-1.2.0/RELEASE_NOTES.md").read()
            self.assertEqual(notes, (root / "RELEASE_NOTES.md").read_bytes())
            self.assertEqual(archive.getmember("demo-1.2.0/demo").mode, 0o755)
        digest = first.read_bytes()
        clone = self.tmp / "clone"
        git(self.tmp, "clone", "-q", str(root), str(clone))
        prepare_release(self.config(clone))
        second = build_source_archive(self.config(clone))
        self.assertEqual(second.read_bytes(), digest, "source archive is not reproducible")

    def test_refuses_unprepared_tree(self) -> None:
        root = self.make_repo(config=self.CONFIG, debian=True, version="1.2.0")
        with self.assertRaisesRegex(ReleaseError, "not prepared"):
            build_source_archive(self.config(root))


if __name__ == "__main__":
    unittest.main()
