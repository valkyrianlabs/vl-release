from __future__ import annotations

import json
import unittest

from tests.support import BASIC_CONFIG, RepoTestCase
from vlrelease.config import TargetSpec
from vlrelease.errors import ReleaseError
from vlrelease.semver import Version, resolve_target, satisfies
from vlrelease.targets import HOMEBREW_PLACEHOLDER_SHA256, TargetError, read_version_text, replace_version_text
from vlrelease.versioning import read_state, set_version, sync_versions

ALL_TARGETS_CONFIG = BASIC_CONFIG.replace(
    'canonical = "VERSION"',
    """canonical = "VERSION"
targets = [
  { kind = "meson", path = "meson.build" },
  { kind = "package_json", path = "web/package.json" },
  { kind = "pyproject", path = "pyproject.toml" },
  { kind = "regex", path = "src/version.ts", pattern = 'VERSION = "(?P<version>[^"]+)"' },
  { kind = "homebrew", path = "Formula/demo.rb" },
]""",
)

FILES = {
    "meson.build": "project('demo', 'cpp',\n  version : '1.0.0',\n  default_options : ['cpp_std=c++20'])\n",
    "web/package.json": '{\n  "name": "web",\n  "version": "1.0.0",\n  "dependencies": {"left-pad": "1.0.0"},\n  "files": ["a", "b"]\n}\n',
    "pyproject.toml": '[build-system]\nrequires = ["setuptools"]\n\n[project]\nname = "demo"\nversion = "1.0.0"\n\n[tool.other]\nversion = "9.9.9"\n',
    "src/version.ts": 'export const VERSION = "1.0.0"\n',
    "Formula/demo.rb": 'class Demo < Formula\n  url "https://example.com/demo/archive/refs/tags/v1.0.0.tar.gz"\n  sha256 "%s"\nend\n' % ("a" * 64),
}


class SemverTests(unittest.TestCase):
    def test_parse_and_bump(self) -> None:
        version = Version.parse("1.2.3")
        self.assertEqual(str(version.bump("patch")), "1.2.4")
        self.assertEqual(str(version.bump("minor")), "1.3.0")
        self.assertEqual(str(version.bump("major")), "2.0.0")
        self.assertEqual(resolve_target(version, "4.5.6"), Version(4, 5, 6))

    def test_rejects_invalid_versions(self) -> None:
        for raw in ("1.2", "01.2.3", "1.2.3-rc1", "v1.2.3", "", "1.2.3.4"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                Version.parse(raw)

    def test_requirements(self) -> None:
        version = Version(0, 4, 2)
        self.assertTrue(satisfies(version, ">=0.1,<1"))
        self.assertFalse(satisfies(version, ">=0.5"))
        self.assertTrue(satisfies(version, "==0.4.2"))
        with self.assertRaises(ValueError):
            satisfies(version, "~=0.4")

    def test_patch_successor(self) -> None:
        self.assertTrue(Version(1, 2, 4).is_patch_successor_of(Version(1, 2, 3)))
        self.assertFalse(Version(1, 3, 0).is_patch_successor_of(Version(1, 2, 3)))


class AdapterTests(unittest.TestCase):
    def roundtrip(self, kind: str, text: str, *, pattern: str | None = None) -> str:
        spec = TargetSpec(kind=kind, path="f", pattern=pattern)
        self.assertEqual(read_version_text(spec, text), "1.0.0")
        updated = replace_version_text(spec, text, Version(2, 3, 4))
        self.assertEqual(read_version_text(spec, updated), "2.3.4")
        return updated

    def test_file(self) -> None:
        self.assertEqual(self.roundtrip("file", "1.0.0\n"), "2.3.4\n")

    def test_meson_only_touches_project_version(self) -> None:
        updated = self.roundtrip("meson", FILES["meson.build"])
        self.assertIn("default_options : ['cpp_std=c++20']", updated)

    def test_package_json_preserves_formatting(self) -> None:
        updated = self.roundtrip("package_json", FILES["web/package.json"])
        self.assertEqual(updated, FILES["web/package.json"].replace('"version": "1.0.0"', '"version": "2.3.4"'))
        self.assertEqual(json.loads(updated)["dependencies"], {"left-pad": "1.0.0"})

    def test_package_json_nested_version_is_not_mistaken(self) -> None:
        text = '{"engines": {"version": "0.0.1"}, "version": "1.0.0"}'
        updated = self.roundtrip("package_json", text)
        self.assertEqual(json.loads(updated)["engines"]["version"], "0.0.1")

    def test_pyproject_only_project_table(self) -> None:
        updated = self.roundtrip("pyproject", FILES["pyproject.toml"])
        self.assertIn('[tool.other]\nversion = "9.9.9"', updated)

    def test_regex(self) -> None:
        self.roundtrip("regex", FILES["src/version.ts"], pattern='VERSION = "(?P<version>[^"]+)"')

    def test_homebrew_resets_sha_when_version_changes(self) -> None:
        updated = self.roundtrip("homebrew", FILES["Formula/demo.rb"])
        self.assertIn("v2.3.4.tar.gz", updated)
        self.assertIn(HOMEBREW_PLACEHOLDER_SHA256, updated)

    def test_missing_version_errors(self) -> None:
        with self.assertRaises(TargetError):
            read_version_text(TargetSpec("meson", "f"), "project('x')\n")
        with self.assertRaises(TargetError):
            read_version_text(TargetSpec("package_json", "f"), '{"name": "x"}')
        with self.assertRaises(TargetError):
            read_version_text(TargetSpec("pyproject", "f"), "[tool.poetry]\nversion = '1.0.0'\n")


class VersioningTests(RepoTestCase):
    def make(self, **overrides: str):
        files = dict(FILES)
        files.update(overrides)
        return self.config(self.make_repo(files, config=ALL_TARGETS_CONFIG))

    def test_consistent_state(self) -> None:
        state = read_state(self.make())
        self.assertTrue(state.ok, state.issues)
        self.assertEqual(state.canonical, Version(1, 0, 0))
        self.assertEqual(len(state.readings), 6)

    def test_mismatch_detection(self) -> None:
        state = read_state(self.make(**{"src/version.ts": 'export const VERSION = "0.9.0"\n'}))
        self.assertFalse(state.ok)
        self.assertTrue(any("src/version.ts" in issue and "0.9.0" in issue for issue in state.issues))

    def test_missing_configured_target(self) -> None:
        config = self.make()
        (config.root / "meson.build").unlink()
        state = read_state(config)
        self.assertFalse(state.ok)
        self.assertTrue(any("meson.build does not exist" in issue for issue in state.issues))
        with self.assertRaises(ReleaseError):
            set_version(config, "patch")

    def test_sync_writes_canonical_everywhere(self) -> None:
        config = self.make(**{"web/package.json": FILES["web/package.json"].replace("1.0.0", "0.1.0", 1)})
        (config.root / "VERSION").write_text("1.5.0\n", encoding="utf-8")
        version, changed = sync_versions(config)
        self.assertEqual(version, Version(1, 5, 0))
        self.assertEqual(len(changed), 5)
        self.assertTrue(read_state(config).ok)

    def test_bumps_and_explicit_set(self) -> None:
        config = self.make()
        for part, expected in (("patch", "1.0.1"), ("minor", "1.1.0"), ("major", "2.0.0")):
            new, _old, changed = set_version(config, part)
            self.assertEqual(str(new), expected)
            self.assertEqual(len(changed), 6)
            self.assertTrue(read_state(config).ok)
        new, old, _ = set_version(config, "3.1.4")
        self.assertEqual((str(new), str(old)), ("3.1.4", "2.0.0"))

    def test_dry_run_writes_nothing(self) -> None:
        config = self.make()
        _new, _old, changed = set_version(config, "minor", dry_run=True)
        self.assertEqual(len(changed), 6)
        self.assertEqual(read_state(config).canonical, Version(1, 0, 0))

    def test_bump_refuses_inconsistent_state(self) -> None:
        config = self.make(**{"meson.build": FILES["meson.build"].replace("1.0.0", "0.5.0")})
        with self.assertRaises(ReleaseError):
            set_version(config, "patch")
        set_version(config, "1.0.1")  # an explicit version repairs it
        self.assertTrue(read_state(config).ok)

    def test_version_layer_needs_no_debian(self) -> None:
        root = self.make_repo(config=BASIC_CONFIG, name="plain")
        code, out, _ = self.vlr("version", "bump", "minor", cwd=root)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read(root, "VERSION"), "1.1.0\n")

    def test_cli_version_check_json(self) -> None:
        config = self.make()
        code, out, _ = self.vlr("version", "check", "--json", cwd=config.root)
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["version"], "1.0.0")


if __name__ == "__main__":
    unittest.main()
