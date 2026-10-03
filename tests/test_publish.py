from __future__ import annotations

import gzip
import subprocess
import unittest
from pathlib import Path

from tests.support import DEBIAN_CONFIG, RepoTestCase
from vlrelease.apt_index import AptIndex, AptIndexConfig, AptPackageEntry, compare_debian_versions, load_apt_index, parse_packages_index
from vlrelease.checksums import sha256_file, write_sha256sums
from vlrelease.errors import ConfigError, IntegrityError, ReleaseError
from vlrelease.publish import (
    curl_config_for_credentials,
    curl_upload_command,
    publish_debs,
    upload_with_curl,
    verify_published,
)

APT_CONFIG = DEBIAN_CONFIG + """
[publish.apt]
repository_url = "https://apt.example.com"
verify_timeout = 30
verify_interval = 10
"""
NEXUS_ENV = {
    "RELEASE_PUBLISH_MODE": "nexus",
    "NEXUS_APT_REPO": "https://nexus.example.com/repository/apt-hosted/",
    "NEXUS_USER": "deployer",
    "NEXUS_PASSWORD": "s3cr3t-p@ss",
}
LEGACY_ENV = {
    "RELEASE_PUBLISH_MODE": "nexus",
    "NEXUS_REPO_URL": "https://legacy.example.com/repository/apt-hosted/",
    "NEXUS_USER": "deployer",
    "NEXUS_PASS": "legacy-pass",
}


def index_with(*entries: AptPackageEntry) -> AptIndex:
    return AptIndex(entries=list(entries))


class AptIndexTests(unittest.TestCase):
    PACKAGES = (
        "Package: demo\nVersion: 1.2.0-1\nArchitecture: all\nFilename: pool/d/demo.deb\nSize: 10\n"
        "SHA256: ABCDEF\nDescription: multi\n line\n\n"
        "Package: other\nVersion: 2.0.0-1\nArchitecture: amd64\nSHA256: 01\n"
    )

    def test_parse(self) -> None:
        entries = parse_packages_index(self.PACKAGES)
        self.assertEqual([(e.package, e.version, e.sha256) for e in entries], [("demo", "1.2.0-1", "abcdef"), ("other", "2.0.0-1", "01")])
        index = AptIndex(entries=entries)
        self.assertEqual(len(index.lookup("demo", "1.2.0-1", "amd64")), 1)  # arch-all matches any arch
        self.assertEqual(index.newest_version("demo"), "1.2.0-1")

    def test_gzip_preferred_and_fail_closed(self) -> None:
        calls: list[str] = []

        def getter(url: str, headers):
            calls.append(url)
            if url.endswith(".gz"):
                return gzip.compress(self.PACKAGES.encode())
            raise ValueError("unexpected")

        index = load_apt_index(AptIndexConfig("https://apt.example.com"), http_get=getter)
        self.assertEqual(calls, ["https://apt.example.com/dists/stable/main/binary-amd64/Packages.gz"])
        self.assertEqual(len(index.entries), 2)

        def broken(url: str, headers):
            raise ValueError("HTTP 503")

        with self.assertRaisesRegex(ValueError, "refusing to guess"):
            load_apt_index(AptIndexConfig("https://apt.example.com"), http_get=broken)

    def test_basic_auth_header_not_url(self) -> None:
        seen: dict = {}

        def getter(url: str, headers):
            seen.update(headers)
            self.assertNotIn("s3cr3t", url)
            return self.PACKAGES.encode()

        load_apt_index(AptIndexConfig("https://apt.example.com", username="u", password="s3cr3t"), http_get=getter)
        self.assertTrue(seen["Authorization"].startswith("Basic "))

    def test_debian_version_ordering(self) -> None:
        self.assertLess(compare_debian_versions("1.2.0-1", "1.10.0-1"), 0)
        self.assertLess(compare_debian_versions("1.0~rc1-1", "1.0-1"), 0)
        self.assertGreater(compare_debian_versions("1:0.1-1", "9.9-1"), 0)
        self.assertEqual(compare_debian_versions("1.2.0-1", "1.2.0-1"), 0)


class PublishTests(RepoTestCase):
    def staged(self) -> Path:
        root = self.make_repo(config=APT_CONFIG, debian=True, version="1.2.0")
        out = root / "release"
        out.mkdir()
        (out / "demo_1.2.0-1_all.deb").write_bytes(b"debian package bytes")
        write_sha256sums(out)
        return root

    def run_publish(self, root: Path, index: AptIndex | list[AptIndex], **kwargs):
        sequence = index if isinstance(index, list) else [index]
        uploads: list[tuple] = []
        state = {"calls": 0}

        def loader() -> AptIndex:
            value = sequence[min(state["calls"], len(sequence) - 1)]
            state["calls"] += 1
            return value

        result = publish_debs(
            self.config(root),
            env=kwargs.pop("env", NEXUS_ENV),
            uploader=lambda *args: uploads.append(args),
            index_loader=loader,
            sleep=lambda _seconds: None,
            log=lambda _line: None,
            **kwargs,
        )
        return result, uploads

    def entry(self, root: Path, *, sha: str | None = None, version: str = "1.2.0-1") -> AptPackageEntry:
        digest = sha if sha is not None else sha256_file(root / "release" / "demo_1.2.0-1_all.deb")
        return AptPackageEntry("demo", version, "all", digest)

    def test_uploads_absent_version_then_verifies_by_sha(self) -> None:
        root = self.staged()
        result, uploads = self.run_publish(root, [index_with(), index_with(), index_with(self.entry(root))])
        self.assertEqual(len(uploads), 1)
        self.assertEqual(uploads[0][1:], (NEXUS_ENV["NEXUS_APT_REPO"], "deployer", "s3cr3t-p@ss"))
        self.assertTrue(result.verified)
        self.assertEqual(result.plans[0].action, "upload")

    def test_identical_republish_is_skipped(self) -> None:
        root = self.staged()
        result, uploads = self.run_publish(root, index_with(self.entry(root)))
        self.assertEqual(uploads, [])
        self.assertEqual(result.plans[0].action, "skip-identical")
        self.assertTrue(result.verified)

    def test_different_bytes_for_published_version_are_refused(self) -> None:
        root = self.staged()
        with self.assertRaisesRegex(IntegrityError, "REFUSING TO OVERWRITE"):
            self.run_publish(root, index_with(self.entry(root, sha="0" * 64)))

    def test_published_without_sha_is_refused(self) -> None:
        root = self.staged()
        entry = AptPackageEntry("demo", "1.2.0-1", "all", None)
        with self.assertRaisesRegex(IntegrityError, "no SHA256"):
            self.run_publish(root, index_with(entry))

    def test_older_version_than_published_is_refused(self) -> None:
        root = self.staged()
        newer = AptPackageEntry("demo", "1.3.0-1", "all", "f" * 64)
        with self.assertRaisesRegex(IntegrityError, "newer"):
            self.run_publish(root, index_with(newer))
        _result, uploads = self.run_publish(root, [index_with(newer), index_with(newer, self.entry(root))], allow_older_version=True)
        self.assertEqual(len(uploads), 1)

    def test_verification_times_out(self) -> None:
        root = self.staged()
        with self.assertRaisesRegex(ReleaseError, "did not confirm"):
            self.run_publish(root, index_with())

    def test_verification_detects_wrong_sha_after_upload(self) -> None:
        root = self.staged()
        with self.assertRaisesRegex(IntegrityError, "expected"):
            self.run_publish(root, [index_with(), index_with(self.entry(root, sha="1" * 64))])

    def test_disabled_mode_and_require_enabled(self) -> None:
        root = self.staged()
        result, uploads = self.run_publish(root, index_with(), env={})
        self.assertEqual((result.mode, uploads), ("disabled", []))
        with self.assertRaisesRegex(ReleaseError, "disabled"):
            self.run_publish(root, index_with(), env={}, require_enabled=True)

    def test_missing_credentials(self) -> None:
        root = self.staged()
        with self.assertRaisesRegex(ConfigError, "NEXUS_PASSWORD or NEXUS_PASS"):
            self.run_publish(root, index_with(), env={**NEXUS_ENV, "NEXUS_PASSWORD": ""})

    def test_organization_names_win_over_legacy_names(self) -> None:
        root = self.staged()
        _result, uploads = self.run_publish(root, [index_with(), index_with(self.entry(root))], env={**LEGACY_ENV, **NEXUS_ENV})
        self.assertEqual(uploads[0][1:], (NEXUS_ENV["NEXUS_APT_REPO"], "deployer", "s3cr3t-p@ss"))

    def test_legacy_names_still_work(self) -> None:
        root = self.staged()
        _result, uploads = self.run_publish(root, [index_with(), index_with(self.entry(root))], env=LEGACY_ENV)
        self.assertEqual(uploads[0][1:], (LEGACY_ENV["NEXUS_REPO_URL"], "deployer", "legacy-pass"))

    def test_sha256sums_must_match(self) -> None:
        root = self.staged()
        (root / "release" / "demo_1.2.0-1_all.deb").write_bytes(b"changed after validation")
        with self.assertRaisesRegex(IntegrityError, "changed after it was built"):
            self.run_publish(root, index_with())

    def test_dry_run_plans_without_uploading(self) -> None:
        root = self.staged()
        result, uploads = self.run_publish(root, index_with(), dry_run=True)
        self.assertEqual(uploads, [])
        self.assertFalse(result.verified)
        self.assertEqual(result.plans[0].action, "upload")

    def test_verify_published_standalone(self) -> None:
        root = self.staged()
        identities = verify_published(
            self.config(root), env={}, index_loader=lambda: index_with(self.entry(root)), sleep=lambda _s: None, log=lambda _l: None
        )
        self.assertEqual(identities[0].package, "demo")


class CredentialHandlingTests(unittest.TestCase):
    def test_credentials_never_reach_argv(self) -> None:
        captured: dict = {}

        def runner(args, **kwargs):
            captured["args"] = args
            captured["input"] = kwargs.get("input")
            return subprocess.CompletedProcess(args, 0, "", "")

        upload_with_curl(Path("/tmp/demo.deb"), "https://nexus.example.com/repo/", "deployer", "s3cr3t-p@ss", runner=runner)
        argv = " ".join(captured["args"])
        self.assertNotIn("s3cr3t", argv)
        self.assertNotIn("deployer", argv)
        self.assertIn("--config", captured["args"])
        self.assertEqual(captured["input"], 'user = "deployer:s3cr3t-p@ss"\n')

    def test_config_quoting(self) -> None:
        self.assertEqual(curl_config_for_credentials('a"b', "c\\d\ne"), 'user = "a\\"b:c\\\\d\\ne"\n')
        self.assertNotIn("user", " ".join(curl_upload_command(Path("x.deb"), "https://h/")))

    def test_failed_upload_reports_without_credentials(self) -> None:
        def runner(args, **kwargs):
            return subprocess.CompletedProcess(args, 22, "", "curl: (22) The requested URL returned error: 401")

        with self.assertRaises(ReleaseError) as caught:
            upload_with_curl(Path("/tmp/demo.deb"), "https://user:pw@nexus.example.com/repo/", "u", "pw", runner=runner)
        self.assertNotIn("pw@", str(caught.exception))
        self.assertIn("401", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
