from __future__ import annotations

import unittest
from pathlib import Path

from tests.support import BASIC_CONFIG, RepoTestCase, git
from vlrelease import __version__
from vlrelease.config import discover_root, load_config, load_project
from vlrelease.errors import EXIT_USAGE, ConfigError


class DiscoveryTests(RepoTestCase):
    def test_discovers_git_root_from_subdirectory(self) -> None:
        root = self.make_repo({"src/deep/file.txt": "x"})
        self.assertEqual(discover_root(None, cwd=root / "src" / "deep"), root.resolve())
        self.assertEqual(load_project(cwd=root / "src" / "deep").root, root.resolve())

    def test_explicit_repo_argument(self) -> None:
        root = self.make_repo()
        config = load_project(repo=str(root / "VERSION"))
        self.assertEqual(config.root, root.resolve())

    def test_outside_git_repository_is_a_usage_error(self) -> None:
        plain = self.tmp / "plain"
        plain.mkdir()
        with self.assertRaises(ConfigError) as caught:
            discover_root(None, cwd=plain)
        self.assertIn("not inside a Git repository", str(caught.exception))

    def test_missing_release_toml(self) -> None:
        root = self.make_repo()
        (root / "release.toml").unlink()
        with self.assertRaises(ConfigError) as caught:
            load_config(root)
        self.assertIn("No release.toml", str(caught.exception))

    def test_does_not_search_above_git_root(self) -> None:
        # A release.toml in the parent of a repository must never be picked up.
        (self.tmp / "release.toml").write_text(BASIC_CONFIG, encoding="utf-8")
        inner = self.tmp / "inner"
        inner.mkdir()
        git(inner, "init", "-q")
        with self.assertRaises(ConfigError):
            load_project(cwd=inner)

    def test_nested_repo_uses_its_own_root(self) -> None:
        outer = self.make_repo(name="outer")
        inner = outer / "vendor" / "inner"
        inner.mkdir(parents=True)
        git(inner, "init", "-q")
        with self.assertRaises(ConfigError):
            load_project(cwd=inner)

    def test_cli_reports_missing_config_with_usage_exit_code(self) -> None:
        root = self.make_repo()
        (root / "release.toml").unlink()
        code, _out, err = self.vlr("check", cwd=root)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("No release.toml", err)


class SchemaTests(RepoTestCase):
    def load_with(self, config: str):
        root = self.make_repo(config=config)
        return load_config(root)

    def assert_config_error(self, config: str, fragment: str) -> None:
        with self.assertRaises(ConfigError) as caught:
            self.load_with(config)
        self.assertIn(fragment, str(caught.exception))

    def test_minimal_config(self) -> None:
        config = self.load_with(BASIC_CONFIG)
        self.assertEqual(config.project.package, "demo")
        self.assertEqual(config.version.canonical.kind, "file")
        self.assertIsNone(config.debian)
        self.assertEqual(config.tag_for("1.2.3"), "v1.2.3")

    def test_malformed_toml(self) -> None:
        self.assert_config_error("schema_version = [", "invalid TOML")

    def test_missing_schema_version(self) -> None:
        self.assert_config_error(BASIC_CONFIG.replace("schema_version = 1\n", ""), "schema_version is required")

    def test_unsupported_schema_version(self) -> None:
        self.assert_config_error(BASIC_CONFIG.replace("schema_version = 1", "schema_version = 99"), "unsupported schema_version")

    def test_unknown_keys_are_rejected(self) -> None:
        self.assert_config_error(BASIC_CONFIG + "\n[release]\nbranchh = 'main'\n", "unknown key(s): branchh")
        self.assert_config_error(BASIC_CONFIG + "\nsurprise = true\n", "unknown key(s): surprise")

    def test_incompatible_tool_requirement(self) -> None:
        self.assert_config_error(BASIC_CONFIG + '\n[tool]\nrequires = ">=999"\n', f"running version is {__version__}")

    def test_compatible_tool_requirement(self) -> None:
        self.assertEqual(self.load_with(BASIC_CONFIG + '\n[tool]\nrequires = ">=0.1,<999"\n').tool_requires, ">=0.1,<999")

    def test_paths_must_stay_inside_repository(self) -> None:
        self.assert_config_error(BASIC_CONFIG.replace('"VERSION"', '"../VERSION"'), "relative path inside the repository")
        self.assert_config_error(BASIC_CONFIG.replace('"VERSION"', '"/etc/VERSION"'), "relative path inside the repository")

    def test_regex_target_requires_version_group(self) -> None:
        config = BASIC_CONFIG + '\n'
        config = config.replace(
            'canonical = "VERSION"',
            'canonical = "VERSION"\ntargets = [{ kind = "regex", path = "x.py", pattern = "v = (.*)" }]',
        )
        self.assert_config_error(config, "(?P<version>")

    def test_unknown_target_kind(self) -> None:
        config = BASIC_CONFIG.replace('canonical = "VERSION"', 'canonical = { kind = "cargo", path = "Cargo.toml" }')
        self.assert_config_error(config, "kind must be one of")

    def test_duplicate_target(self) -> None:
        config = BASIC_CONFIG.replace('canonical = "VERSION"', 'canonical = "VERSION"\ntargets = ["VERSION"]')
        self.assert_config_error(config, "more than once")

    def test_debian_requires_package_contract(self) -> None:
        self.assert_config_error(BASIC_CONFIG + "\n[debian]\n", "at least one [[debian.packages]]")

    def test_debian_can_be_disabled(self) -> None:
        self.assertIsNone(self.load_with(BASIC_CONFIG + "\n[debian]\nenabled = false\n").debian)

    def test_apt_requires_debian(self) -> None:
        self.assert_config_error(BASIC_CONFIG + "\n[publish.apt]\nrepository_url = 'https://apt.example.com'\n", "requires [debian]")

    def test_homebrew_release_asset_requires_source_archive(self) -> None:
        config = BASIC_CONFIG.replace('package = "demo"', 'package = "demo"\nrepository = "o/demo"')
        self.assert_config_error(
            config + '\n[homebrew]\nformula = "demo.rb"\ntap = "o/homebrew-tap"\n', "requires [source_archive]"
        )

    def test_title_template_validation(self) -> None:
        self.assert_config_error(BASIC_CONFIG + '\n[release]\ntitle = "v{version}"\n', "must contain {title}")
        self.assert_config_error(BASIC_CONFIG + '\n[release]\ntag = "{nope}"\n', "unknown placeholder")

    def test_invalid_debian_token(self) -> None:
        config = BASIC_CONFIG + '\n[debian]\ndistribution = "bad dist"\n[[debian.packages]]\nname = "demo"\n'
        self.assert_config_error(config, "not a valid Debian changelog token")


if __name__ == "__main__":
    unittest.main()
