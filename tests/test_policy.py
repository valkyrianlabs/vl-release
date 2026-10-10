"""Version policies: `semver` (default, unchanged) and `debian-upstream` (UPSTREAM-REVISION)."""

from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

from tests.support import RepoTestCase, git, requires_tools
from vlrelease.apt_index import AptIndex, AptPackageEntry
from vlrelease.artifacts import DebIdentity, validate_artifacts
from vlrelease.checksums import write_sha256sums
from vlrelease.config import load_config
from vlrelease.cut import cut_release
from vlrelease.debbuild import build_debian
from vlrelease.debversion import compare_debian_versions
from vlrelease.errors import ConfigError, ReleaseError
from vlrelease.finalize import finalize_release
from vlrelease.policy import DebianUpstreamPolicy, SemverPolicy, UpstreamRevision, get_policy
from vlrelease.prepare import prepare_release
from vlrelease.publish import ACTION_SKIP, ACTION_UPLOAD, plan_publication
from vlrelease.semver import Version
from vlrelease.state import read_release_state

UPSTREAM_CONFIG = """\
schema_version = 1

[project]
name = "Demo SDK"
package = "demo"
repository = "example/demo"

[version]
canonical = "VERSION"
policy = "debian-upstream"
targets = [{ kind = "regex", path = "src/version.h", pattern = '#define DEMO_VERSION "(?P<version>[^"]+)"' }]

[debian]
maintainer = "Demo Maintainer <demo@example.com>"

[[debian.packages]]
name = "demo"
architecture = "all"
required_paths = ["usr/bin/demo"]
identical_files = [{ member = "usr/share/doc/demo/RELEASE_NOTES.md", source = "RELEASE_NOTES.md" }]
"""
# A plain header, not meson.build: debhelper would pick Meson as the build system.
HEADER = '#define DEMO_VERSION "%s"\n'


POLICY = DebianUpstreamPolicy()


def uv(raw: str) -> UpstreamRevision:
    return POLICY.parse(raw)


class DebianUpstreamVersionTests(unittest.TestCase):
    def test_parse_and_render(self) -> None:
        self.assertEqual(uv("8.0.2-1"), UpstreamRevision("8.0.2", 1))
        self.assertEqual(str(uv("8.0.2-12")), "8.0.2-12")
        self.assertEqual(uv(" 1.2+git20260101-3\n"), UpstreamRevision("1.2+git20260101", 3))
        self.assertEqual(uv("2026.10-1").upstream, "2026.10")

    def test_invalid_versions(self) -> None:
        for raw in ("8.0.2", "8.0.2-0", "8.0.2-01", "8.0.2-1a", "1:8.0.2-1", "8.0.2~rc1-1", "v8.0.2-1", "08.0.2-1",
                    "8.00.2-1", "8.0.2-1-1", "8.0.2.-1", "-1", "", "8..2-1"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                uv(raw)

    def test_ordering_is_dpkg_ordering_not_semver_precedence(self) -> None:
        ordered = ["7.10.7-1", "8.0.0-1", "8.0.2-1", "8.0.2-2", "8.0.2-10", "8.0.2+dfsg-1", "8.0.3-1", "8.0.10-1", "8.1-1"]
        versions = [uv(v) for v in ordered]
        self.assertEqual(sorted(reversed(versions)), versions)
        self.assertEqual(max(versions), uv("8.1-1"))
        # Under SemVer precedence 8.0.2-1 would be a *prerelease* of 8.0.2; here it is a release.
        self.assertGreater(uv("8.0.2-2"), uv("8.0.2-1"))
        self.assertLessEqual(uv("8.0.2-1"), uv("8.0.2-1"))

    @requires_tools("dpkg")
    def test_comparator_matches_dpkg(self) -> None:
        corpus = ["1.0-1", "1.0-2", "1.0-10", "1.0+b1-1", "1.0~rc1-1", "1.0.0-1", "1.0a-1", "1.0A-1", "1.00-1", "1:0.9-1",
                  "8.0.2-1", "8.0.10-1", "2026.10-1", "1.2+git20260101-3", "1.2-3", "0-1", "1.0", "1.0-0"]
        for left in corpus:
            for right in corpus:
                ours = compare_debian_versions(left, right)
                dpkg_lt = subprocess.run(["dpkg", "--compare-versions", left, "lt", right]).returncode == 0
                dpkg_eq = subprocess.run(["dpkg", "--compare-versions", left, "eq", right]).returncode == 0
                expected = -1 if dpkg_lt else 0 if dpkg_eq else 1
                self.assertEqual((ours > 0) - (ours < 0), expected, f"{left} vs {right}")

    def test_bumps_and_upstream_adoption(self) -> None:
        self.assertEqual(POLICY.resolve(uv("8.0.2-1"), "revision"), uv("8.0.2-2"))
        self.assertEqual(POLICY.resolve(uv("8.0.2-2"), "8.0.3-1"), uv("8.0.3-1"))
        self.assertEqual(POLICY.adopt_upstream(uv("8.0.2-4"), "8.0.3"), uv("8.0.3-1"))
        self.assertEqual(POLICY.adopt_upstream(uv("8.0.9-1"), "8.0.10"), uv("8.0.10-1"))
        with self.assertRaisesRegex(ValueError, "already the current upstream"):
            POLICY.adopt_upstream(uv("8.0.2-1"), "8.0.2")
        with self.assertRaisesRegex(ValueError, "older than the current upstream"):
            POLICY.adopt_upstream(uv("8.0.2-1"), "7.10.7")
        with self.assertRaisesRegex(ValueError, "invalid upstream"):
            POLICY.adopt_upstream(uv("8.0.2-1"), "8.0.3-1")
        for ambiguous in ("patch", "minor", "major"):
            with self.subTest(ambiguous), self.assertRaisesRegex(ValueError, "ambiguous under the debian-upstream"):
                POLICY.resolve(uv("8.0.2-1"), ambiguous)

    def test_transitions(self) -> None:
        self.assertIsNone(POLICY.transition_problem(uv("8.0.2-1"), uv("8.0.2-2")))
        self.assertIsNone(POLICY.transition_problem(uv("8.0.2-1"), uv("8.0.2-3")))  # a skipped revision is harmless
        self.assertIsNone(POLICY.transition_problem(uv("8.0.2-3"), uv("8.0.3-1")))
        self.assertRegex(POLICY.transition_problem(uv("8.0.2-3"), uv("8.0.3-2")) or "", "must restart at 1")
        self.assertTrue(POLICY.is_maintenance_successor(uv("8.0.2-1"), uv("8.0.2-2")))
        self.assertFalse(POLICY.is_maintenance_successor(uv("8.0.2-1"), uv("8.0.3-1")))
        self.assertEqual(POLICY.debian_version(uv("8.0.2-1"), 7), "8.0.2-1")
        self.assertEqual(POLICY.describe(uv("8.0.2-3")), {"upstream_version": "8.0.2", "packaging_revision": 3})


class SemverPolicyUnchangedTests(unittest.TestCase):
    def test_semver_is_the_default_and_behaves_as_before(self) -> None:
        semver = get_policy("semver")
        self.assertIsInstance(semver, SemverPolicy)
        self.assertEqual(semver.resolve(Version(1, 2, 3), "patch"), Version(1, 2, 4))
        self.assertEqual(semver.resolve(Version(1, 2, 3), "minor"), Version(1, 3, 0))
        self.assertEqual(semver.resolve(Version(1, 2, 3), "2.0.0"), Version(2, 0, 0))
        self.assertEqual(semver.debian_version(Version(1, 2, 3), 2), "1.2.3-2")
        self.assertIsNone(semver.transition_problem(Version(1, 2, 3), Version(1, 4, 0)))
        self.assertEqual(semver.describe(Version(1, 0, 0)), {})
        with self.assertRaisesRegex(ValueError, "another version policy"):
            semver.resolve(Version(1, 2, 3), "revision")
        with self.assertRaises(ValueError):
            semver.parse("8.0.2-1")


class PolicyConfigTests(RepoTestCase):
    def load(self, text: str):
        root = self.make_repo(config=text, version="8.0.2-1", files={"src/version.h": HEADER % "8.0.2-1"})
        return load_config(root)

    def test_default_policy_is_semver(self) -> None:
        root = self.make_repo()
        self.assertEqual(load_config(root).version.policy, "semver")
        self.assertEqual(load_config(root).policy.name, "semver")

    def test_debian_upstream_config(self) -> None:
        config = self.load(UPSTREAM_CONFIG)
        self.assertEqual(config.policy.name, "debian-upstream")
        meson = UPSTREAM_CONFIG.replace("targets = [", 'targets = [{ kind = "meson", path = "meson.build" }, ')
        root = self.make_repo(config=meson, version="8.0.2-1", files={
            "src/version.h": HEADER % "8.0.2-1", "meson.build": "project('demo', 'c', version: '8.0.2-1')\n"})
        code, out, _ = self.vlr("--repo", str(root), "version", "bump", "revision")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read(root, "meson.build"), "project('demo', 'c', version: '8.0.2-2')\n")

    def test_rejections(self) -> None:
        cases = {
            "unknown": UPSTREAM_CONFIG.replace('"debian-upstream"', '"calver"'),
            "kind 'package_json' cannot carry": UPSTREAM_CONFIG.replace(
                "targets = [", 'targets = [{ kind = "package_json", path = "package.json" }, '),
            "debian.revision does not apply": UPSTREAM_CONFIG.replace("[debian]\n", "[debian]\nrevision = 2\n"),
            r"\[npm\] is not supported": UPSTREAM_CONFIG + "\n[npm]\n",
        }
        for message, text in cases.items():
            with self.subTest(message), self.assertRaisesRegex(ConfigError, message):
                self.load(text)

    def test_semver_keeps_debian_revision(self) -> None:
        root = self.make_repo(config=UPSTREAM_CONFIG.replace('policy = "debian-upstream"\n', "").replace(
            "[debian]\n", "[debian]\nrevision = 2\n"), files={"src/version.h": HEADER % "1.0.0"})
        self.assertEqual(load_config(root).debian.revision, 2)


class UpstreamRepoTestCase(RepoTestCase):
    def upstream_repo(self, version: str = "8.0.2-1", **kwargs) -> Path:
        return self.make_repo(config=UPSTREAM_CONFIG, version=version, debian=True,
                              files={"src/version.h": HEADER % version, **kwargs.pop("files", {})}, **kwargs)


class VersionCommandTests(UpstreamRepoTestCase):
    def test_bump_revision_and_adopt_upstream(self) -> None:
        root = self.upstream_repo()
        code, out, _ = self.vlr("--repo", str(root), "version", "bump", "revision")
        self.assertEqual(code, 0, out)
        self.assertIn("8.0.2-1 -> 8.0.2-2", out)
        self.assertEqual((self.read(root, "VERSION"), self.read(root, "src/version.h")), ("8.0.2-2\n", HEADER % "8.0.2-2"))
        code, out, _ = self.vlr("--repo", str(root), "version", "upstream", "8.0.3")
        self.assertEqual(code, 0, out)
        self.assertEqual((self.read(root, "VERSION"), self.read(root, "src/version.h")), ("8.0.3-1\n", HEADER % "8.0.3-1"))
        code, out, _ = self.vlr("--repo", str(root), "version", "check", "--json")
        self.assertEqual((code, json.loads(out)["version"]), (0, "8.0.3-1"))

    def test_refusals_change_nothing(self) -> None:
        root = self.upstream_repo()
        for args, message in (
            (("version", "bump", "patch"), "does not apply to version.policy = 'debian-upstream'"),
            (("version", "upstream", "8.0.2"), "already the current upstream"),
            (("version", "upstream", "8.0.1"), "older than the current upstream"),
            (("version", "set", "8.0.4"), "UPSTREAM-REVISION"),
        ):
            with self.subTest(args):
                code, out, err = self.vlr("--repo", str(root), *args)
                self.assertNotEqual(code, 0)
                self.assertIn(message, out + err)
                self.assertEqual(self.read(root, "VERSION"), "8.0.2-1\n")

    def test_semver_repositories_refuse_upstream_operations(self) -> None:
        root = self.make_repo(version="1.2.3")
        code, out, err = self.vlr("--repo", str(root), "version", "upstream", "2.0.0")
        self.assertNotEqual(code, 0)
        self.assertIn("needs version.policy", out + err)
        code, out, err = self.vlr("--repo", str(root), "version", "bump", "revision")
        self.assertNotEqual(code, 0)
        code, out, _ = self.vlr("--repo", str(root), "version", "bump", "patch")
        self.assertEqual((code, self.read(root, "VERSION")), (0, "1.2.4\n"))


class PrepareAndStateTests(UpstreamRepoTestCase):
    def test_prepare_renders_the_version_everywhere(self) -> None:
        root = self.upstream_repo()
        result = prepare_release(self.config(root))
        self.assertEqual((result.version, result.tag, result.debian_version), ("8.0.2-1", "v8.0.2-1", "8.0.2-1"))
        self.assertEqual(result.release_title, "v8.0.2-1 — Frobnication arrives")
        self.assertTrue(self.read(root, "debian/changelog").startswith("demo (8.0.2-1) unstable; urgency=medium\n"))
        self.assertIn("<!-- vl-release:entry version=8.0.2-1 -->\n## 8.0.2-1 — Frobnication arrives", self.read(root, "RELEASE_NOTES.md"))
        state = read_release_state(self.config(root))
        self.assertEqual((state.phase, str(state.last_recorded)), ("prepared", "8.0.2-1"))
        self.assertEqual(prepare_release(self.config(root)).status, "already-prepared")
        code, out, _ = self.vlr("--repo", str(root), "status", "--json")
        payload = json.loads(out)
        self.assertEqual((payload["version_policy"], payload["upstream_version"], payload["packaging_revision"]),
                         ("debian-upstream", "8.0.2", 1))

    def test_debian_revision_flag_is_refused(self) -> None:
        root = self.upstream_repo()
        with self.assertRaisesRegex(ReleaseError, "--debian-revision does not apply"):
            prepare_release(self.config(root), debian_revision=2)

    def released(self, version: str = "8.0.2-1") -> Path:
        root = self.upstream_repo(version)
        prepare_release(self.config(root))
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "record")
        return root

    def stage_docs(self, root: Path) -> None:
        self.write(root, ".release/RELEASE_NOTES_NEXT.md", "# Packaging fix\n\nRebuilt.\n")
        self.write(root, ".release/CHANGELOG_NEXT.md", "- Rebuild.\n")

    def set_version(self, root: Path, version: str) -> None:
        self.write(root, "VERSION", version + "\n")
        self.write(root, "src/version.h", HEADER % version)

    def test_invalid_transitions_are_inconsistent(self) -> None:
        root = self.released("8.0.2-3")
        self.stage_docs(root)
        for version, message in (("8.0.3-2", "must restart at 1"), ("8.0.2-2", "not newer"), ("8.0.1-9", "not newer")):
            with self.subTest(version):
                self.set_version(root, version)
                state = read_release_state(self.config(root))
                self.assertEqual(state.phase, "inconsistent")
                self.assertTrue(any(message in error for error in state.errors), state.errors)
        for version in ("8.0.2-4", "8.0.3-1"):
            self.set_version(root, version)
            self.assertEqual(read_release_state(self.config(root)).phase, "pending", version)

    def test_unbumped_hint_names_the_policy_operations(self) -> None:
        root = self.released()
        self.stage_docs(root)
        state = read_release_state(self.config(root))
        self.assertEqual(state.phase, "unbumped")
        self.assertIn("vlr version bump revision", state.warnings[0])

    def test_empty_maintenance_release_only_for_packaging_revisions(self) -> None:
        root = self.released()
        self.set_version(root, "8.0.2-2")
        result = prepare_release(self.config(root), allow_empty_patch=True)
        self.assertEqual(result.debian_version, "8.0.2-2")
        root = self.released()
        self.set_version(root, "8.0.3-1")
        with self.assertRaisesRegex(ReleaseError, "packaging revisions of an already-released upstream"):
            prepare_release(self.config(root), allow_empty_patch=True)


class CutTests(UpstreamRepoTestCase):
    def with_remote(self, version: str = "8.0.2-1") -> tuple[Path, Path]:
        root = self.upstream_repo(version)
        bare = self.tmp / "origin.git"
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(bare))
        git(root, "remote", "add", "origin", str(bare))
        git(root, "push", "-q", "origin", "main")
        git(root, "fetch", "-q", "origin")
        return root, bare

    def test_cut_revision(self) -> None:
        root, bare = self.with_remote()
        result = cut_release(self.config(root), "revision", push=True, log=lambda _l: None)
        self.assertEqual((result.version, result.tag), ("8.0.2-2", "v8.0.2-2"))
        self.assertEqual(git(bare, "show", "v8.0.2-2:VERSION"), "8.0.2-2")
        self.assertEqual(git(bare, "show", "v8.0.2-2:src/version.h"), (HEADER % "8.0.2-2").rstrip("\n"))
        self.assertEqual(git(bare, "log", "-1", "--format=%s", "main"), "chore(release): v8.0.2-2")

    def test_cut_refuses_semver_bumps_and_bad_transitions(self) -> None:
        root, bare = self.with_remote("8.0.2-3")
        ci = self.tmp / "ci"
        git(self.tmp, "clone", "-q", str(bare), str(ci))
        prepare_release(self.config(ci))  # release 8.0.2-3 ...
        finalize_release(self.config(ci), log=lambda _l: None)  # ... and record it on main
        git(root, "pull", "-q", "--ff-only", "origin", "main")
        self.write(root, ".release/RELEASE_NOTES_NEXT.md", "# Next\n\nMore.\n")
        self.write(root, ".release/CHANGELOG_NEXT.md", "- More.\n")
        git(root, "commit", "-qam", "stage")
        git(root, "push", "-q", "origin", "main")
        with self.assertRaisesRegex(ReleaseError, "ambiguous"):
            cut_release(self.config(root), "patch", log=lambda _l: None)
        with self.assertRaisesRegex(ReleaseError, "must restart at 1"):
            cut_release(self.config(root), "8.0.3-2", log=lambda _l: None)
        self.assertEqual(self.read(root, "VERSION"), "8.0.2-3\n")
        self.assertEqual(cut_release(self.config(root), "8.0.3-1", log=lambda _l: None).tag, "v8.0.3-1")


class PublicationPlanTests(unittest.TestCase):
    def test_apt_planning_orders_revisions_like_dpkg(self) -> None:
        deb = DebIdentity(Path("demo_8.0.2-10_all.deb"), "demo", "8.0.2-10", "all", "a" * 64)
        index = AptIndex(entries=[AptPackageEntry("demo", "8.0.2-9", "all", "b" * 64)])
        self.assertEqual(plan_publication((deb,), index)[0].action, ACTION_UPLOAD)  # 10 > 9, not "10" < "9"
        published = AptIndex(entries=[AptPackageEntry("demo", "8.0.2-10", "all", "a" * 64)])
        self.assertEqual(plan_publication((deb,), published)[0].action, ACTION_SKIP)
        newer = AptIndex(entries=[AptPackageEntry("demo", "8.0.3-1", "all", "c" * 64)])
        with self.assertRaisesRegex(ReleaseError, "already carries the newer"):
            plan_publication((deb,), newer)


@requires_tools("dpkg-buildpackage", "dpkg-deb", "dh")
@unittest.skipIf(os.environ.get("VLR_PACKAGE_BUILD") == "1", "nested dpkg-buildpackage")
class EndToEndReleaseTests(UpstreamRepoTestCase):
    """cut -> CI checkout of the tag -> check --release -> prepare -> build-deb -> checksums ->
    validate -> release title -> finalize, then a packaging-only revision, all as 8.0.2-1."""

    def test_release_8_0_2_1_then_revision(self) -> None:
        root = self.upstream_repo()
        bare = self.tmp / "origin.git"
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(bare))
        git(root, "remote", "add", "origin", str(bare))
        git(root, "push", "-q", "origin", "main")
        code, out, err = self.vlr("--repo", str(root), "cut", "8.0.2-1", "--push")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(git(bare, "rev-parse", "v8.0.2-1^{commit}"), git(root, "rev-parse", "HEAD"))

        ci = self.tmp / "ci"
        git(self.tmp, "clone", "-q", "--branch", "v8.0.2-1", str(bare), str(ci))
        for args in (("check", "--release", "--tag", "v8.0.2-1"), ("prepare", "--record", "release/meta/prepare.json"),
                     ("build-deb",), ("checksums",), ("validate-artifacts",)):
            code, out, err = self.vlr("--repo", str(ci), *args)
            self.assertEqual(code, 0, f"vlr {' '.join(args)}: {out}{err}")
        deb = ci / "release" / "demo_8.0.2-1_all.deb"
        self.assertTrue(deb.is_file(), sorted(p.name for p in (ci / "release").iterdir()))
        control = subprocess.run(["dpkg-deb", "-f", str(deb), "Version"], capture_output=True, text=True, check=True)
        self.assertEqual(control.stdout.strip(), "8.0.2-1")
        code, out, _ = self.vlr("--repo", str(ci), "release-title")
        self.assertEqual(out.strip(), "v8.0.2-1 — Frobnication arrives")
        code, out, err = self.vlr("--repo", str(ci), "finalize", "--record", "release/meta/prepare.json")
        self.assertEqual(code, 0, out + err)
        self.assertTrue(git(bare, "show", "main:debian/changelog").startswith("demo (8.0.2-1) unstable;"))

        # A packaging-only revision of the same upstream release.
        git(root, "pull", "-q", "--ff-only", "origin", "main")
        self.write(root, ".release/RELEASE_NOTES_NEXT.md", "# Packaging fix\n\nRebuilt with new flags.\n")
        self.write(root, ".release/CHANGELOG_NEXT.md", "- Rebuild with new flags.\n")
        git(root, "commit", "-qam", "packaging change")
        git(root, "push", "-q", "origin", "main")
        code, out, err = self.vlr("--repo", str(root), "cut", "revision", "--push")
        self.assertEqual(code, 0, out + err)
        ci2 = self.tmp / "ci2"
        git(self.tmp, "clone", "-q", "--branch", "v8.0.2-2", str(bare), str(ci2))
        for args in (("check", "--release", "--tag", "v8.0.2-2"), ("prepare",), ("build-deb",), ("checksums",),
                     ("validate-artifacts",)):
            code, out, err = self.vlr("--repo", str(ci2), *args)
            self.assertEqual(code, 0, f"vlr {' '.join(args)}: {out}{err}")
        self.assertTrue((ci2 / "release" / "demo_8.0.2-2_all.deb").is_file())
        changelog = (ci2 / "debian" / "changelog").read_text()
        self.assertTrue(changelog.startswith("demo (8.0.2-2) unstable;"))
        self.assertIn("demo (8.0.2-1) unstable;", changelog)
        shutil.rmtree(ci2 / "release")


if __name__ == "__main__":
    unittest.main()
