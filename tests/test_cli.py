from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest
from unittest import mock
from pathlib import Path

from tests.support import BASIC_CONFIG, DEBIAN_CONFIG, SOURCE_ROOT, RepoTestCase
from vlrelease import __version__
from vlrelease.errors import EXIT_FAILURE, EXIT_INTEGRITY, EXIT_USAGE, IntegrityError
from vlrelease.skill import install_local_skill, install_skill


def run(argv: list[str], cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False, env={**os.environ, **(env or {})})


class EntrypointTests(RepoTestCase):
    def test_vl_release_and_vlr_are_the_same_program(self) -> None:
        root = self.make_repo()
        outputs = []
        for launcher in ("vl-release", "vlr"):
            result = run([sys.executable, str(SOURCE_ROOT / "bin" / launcher), "status", "--json"], root)
            self.assertEqual(result.returncode, 0, result.stderr)
            outputs.append(json.loads(result.stdout))
            version = run([sys.executable, str(SOURCE_ROOT / "bin" / launcher), "--version"], root)
            self.assertEqual(version.stdout.strip(), f"vl-release {__version__}")
        self.assertEqual(outputs[0], outputs[1])

    def test_python_dash_m_from_source_checkout(self) -> None:
        root = self.make_repo()
        result = run([sys.executable, "-m", "vlrelease", "--repo", str(root), "check"], SOURCE_ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("check: ok", result.stdout)

    def test_installed_layout(self) -> None:
        # /usr/bin/vl-release + /usr/lib/python3/dist-packages/vlrelease: the launcher is not next to the package.
        prefix = self.tmp / "usr"
        site = prefix / "lib" / "python3" / "dist-packages"
        shutil.copytree(SOURCE_ROOT / "vlrelease", site / "vlrelease", ignore=shutil.ignore_patterns("__pycache__"))
        (prefix / "bin").mkdir(parents=True)
        shutil.copy2(SOURCE_ROOT / "bin" / "vl-release", prefix / "bin" / "vl-release")
        (prefix / "bin" / "vlr").symlink_to("vl-release")
        root = self.make_repo()
        for launcher in ("vl-release", "vlr"):
            result = run([sys.executable, str(prefix / "bin" / launcher), "--repo", str(root), "status", "--json"], self.tmp, env={"PYTHONPATH": str(site)})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["phase"], "pending")
        skill = run([sys.executable, str(prefix / "bin" / "vlr"), "--repo", str(root), "install-skill"], self.tmp, env={"PYTHONPATH": str(site)})
        self.assertEqual(skill.returncode, 0, skill.stderr)
        self.assertTrue((root / ".claude/skills/vl-release/SKILL.md").is_file())

    def test_version_and_doctor_work_outside_a_repository(self) -> None:
        outside = self.tmp / "nowhere"
        outside.mkdir()
        code, out, _ = self.vlr("doctor", "--json", cwd=outside)
        payload = json.loads(out)
        self.assertEqual(code, 0, payload)
        self.assertIsNone(payload["repository"])
        code, out, _ = self.vlr("--version", cwd=outside)
        self.assertEqual((code, out.strip()), (0, f"vl-release {__version__}"))

    def test_exit_codes(self) -> None:
        outside = self.tmp / "nowhere"
        outside.mkdir()
        self.assertEqual(self.vlr("check", cwd=outside)[0], EXIT_USAGE)
        self.assertEqual(self.vlr("no-such-command", cwd=outside)[0], EXIT_USAGE)
        self.assertEqual(self.vlr(cwd=outside)[0], EXIT_USAGE)
        root = self.make_repo()
        (root / "VERSION").write_text("not-a-version\n")
        self.assertEqual(self.vlr("check", cwd=root)[0], EXIT_FAILURE)
        self.assertEqual(IntegrityError("x").exit_code, EXIT_INTEGRITY)

    def test_check_release_gate_and_tag(self) -> None:
        root = self.make_repo(version="1.0.0")
        self.assertEqual(self.vlr("check", "--release", "--tag", "v1.0.0", cwd=root)[0], 0)
        code, out, _ = self.vlr("check", "--release", "--tag", "refs/tags/v2.0.0", "--json", cwd=root)
        self.assertEqual(code, EXIT_FAILURE)
        self.assertTrue(any("does not match" in error for error in json.loads(out)["errors"]))
        empty = self.make_repo(version="1.0.0", staged=False, name="empty")
        self.assertEqual(self.vlr("check", cwd=empty)[0], 0)  # missing docs is only a warning during development
        self.assertEqual(self.vlr("check", "--release", cwd=empty)[0], EXIT_FAILURE)

    def test_status_github_output(self) -> None:
        root = self.make_repo(version="1.0.0")
        output = self.tmp / "gh-output"
        os.environ["GITHUB_OUTPUT"] = str(output)
        try:
            code, _out, err = self.vlr("status", "--github-output", cwd=root)
        finally:
            os.environ.pop("GITHUB_OUTPUT")
        self.assertEqual(code, 0, err)
        values = dict(line.split("=", 1) for line in output.read_text().splitlines())
        self.assertEqual(values["version"], "1.0.0")
        self.assertEqual(values["tag"], "v1.0.0")
        self.assertEqual(values["phase"], "pending")
        self.assertEqual(values["release_title"], "v1.0.0 — Frobnication arrives")
        self.assertEqual(values["debian"], "false")

    def test_init_scaffolds_and_never_overwrites(self) -> None:
        root = self.make_repo(commit=False)
        for path in ("release.toml", ".release/RELEASE_NOTES_NEXT.md", "VERSION"):
            (root / path).unlink()
        code, out, _ = self.vlr("init", "--name", "Fresh", cwd=root)
        self.assertEqual(code, 0)
        self.assertIn("created release.toml", out)
        self.assertEqual(self.vlr("check", cwd=root)[0], 0)
        before = (root / "release.toml").read_text()
        self.vlr("init", cwd=root)
        self.assertEqual((root / "release.toml").read_text(), before)


class SkillTests(RepoTestCase):
    def test_base_skill_needs_no_release_toml(self) -> None:
        root = self.make_repo()
        (root / "release.toml").unlink()
        code, out, err = self.vlr("install-skill", cwd=root)
        self.assertEqual(code, 0, err)
        self.assertIn("installed .claude/skills/vl-release/SKILL.md", out)
        self.assertIn("no release.toml yet", out)
        text = (root / ".claude/skills/vl-release/SKILL.md").read_text()
        self.assertTrue(text.startswith("---\nname: vl-release\ndescription: Release workflow and setup"))
        for fragment in ("vlr init", "vlr help config", "vlr install-local-skill", "PROJECT.md", "ask the user", "only\nafter every publication succeeded"):
            self.assertIn(fragment, text)
        self.assertNotIn("$", text.split("```")[0].replace("$tool", ""))  # every template placeholder substituted

    def test_base_skill_is_generic_and_idempotent(self) -> None:
        first = self.make_repo(config=DEBIAN_CONFIG, debian=True)
        second = self.make_repo(config=BASIC_CONFIG, name="other")
        a = install_skill(first)[0]
        b = install_skill(second)[0]
        self.assertEqual(a.path.read_text(), b.path.read_text())
        self.assertNotIn("Demo", a.path.read_text())
        self.assertEqual(install_skill(first)[0].status, "unchanged")
        self.assertEqual(install_skill(first, check=True)[0].status, "unchanged")

    def test_local_skill_requires_base_and_config(self) -> None:
        root = self.make_repo(config=DEBIAN_CONFIG, debian=True)
        code, _out, err = self.vlr("install-local-skill", cwd=root)
        self.assertEqual(code, EXIT_FAILURE)
        self.assertIn("run `vlr install-skill` first", err)
        install_skill(root)
        (root / "release.toml").unlink()
        self.assertEqual(self.vlr("install-local-skill", cwd=root)[0], EXIT_USAGE)

    def test_local_skill_is_repo_aware(self) -> None:
        root = self.make_repo(config=DEBIAN_CONFIG, debian=True)
        install_skill(root)
        results = install_local_skill(self.config(root))
        self.assertEqual([(r.format, r.status) for r in results], [("claude", "installed")])
        text = results[0].path.read_text()
        self.assertEqual(results[0].path, root / ".claude/skills/vl-release/PROJECT.md")
        for fragment in ("# Demo: release specifics", "`.release/CHANGELOG_NEXT.md`", "`debian/changelog`", "`package.json` (package_json)",
                         "`demo`: architecture `all`", "must ship `usr/bin/demo`", "`vX.Y.Z` on branch `main`"):
            self.assertIn(fragment, text)
        self.assertEqual(install_local_skill(self.config(root))[0].status, "unchanged")

    def test_local_skill_without_debian(self) -> None:
        root = self.make_repo(config=BASIC_CONFIG)
        install_skill(root)
        text = install_local_skill(self.config(root))[0].path.read_text()
        self.assertNotIn("CHANGELOG_NEXT", text)
        self.assertNotIn("debian/changelog", text)
        self.assertIn("No publication channels are configured", text)

    def test_local_skill_goes_next_to_every_installed_base(self) -> None:
        root = self.make_repo()
        (root / ".agents").mkdir()
        self.assertEqual(sorted(i.format for i in install_skill(root)), ["agents", "claude"])
        formats = sorted(i.format for i in install_local_skill(self.config(root)))
        self.assertEqual(formats, ["agents", "claude"])
        self.assertTrue((root / ".agents/skills/vl-release/PROJECT.md").is_file())

    def test_staleness_and_hand_written_files(self) -> None:
        root = self.make_repo()
        base = install_skill(root)[0].path
        local = install_local_skill(self.config(root))[0].path
        base.write_text(base.read_text().replace("## 5. Versions", "## 5. Versions (edited)"))
        self.assertEqual(install_skill(root, check=True)[0].status, "stale")
        self.assertEqual(self.vlr("install-skill", "--check", cwd=root)[0], EXIT_FAILURE)
        self.assertEqual(install_skill(root)[0].status, "updated")
        # Changing release.toml makes PROJECT.md stale; check warns about it.
        (root / "release.toml").write_text(BASIC_CONFIG.replace('name = "Demo"', 'name = "Renamed"'))
        self.assertEqual(install_local_skill(self.config(root), check=True)[0].status, "stale")
        code, out, _ = self.vlr("check", "--json", cwd=root)
        self.assertTrue(any("PROJECT.md is stale" in w for w in json.loads(out)["warnings"]))
        self.assertEqual(install_local_skill(self.config(root))[0].status, "updated")
        local.write_text("# my notes\n")
        self.assertEqual(install_local_skill(self.config(root))[0].status, "refused")
        self.assertEqual(local.read_text(), "# my notes\n")
        self.assertEqual(install_local_skill(self.config(root), force=True)[0].status, "updated")

    def test_version_bumps_do_not_make_skills_stale(self) -> None:
        root = self.make_repo(version="1.0.0")
        install_skill(root)
        install_local_skill(self.config(root))
        self.vlr("version", "bump", "minor", cwd=root)
        with mock.patch("vlrelease.__version__", "99.0.0"):
            self.assertEqual(install_skill(root, check=True)[0].status, "unchanged")
            self.assertEqual(install_local_skill(self.config(root), check=True)[0].status, "unchanged")

    def test_legacy_versioned_marker_is_updated_not_refused(self) -> None:
        root = self.make_repo()
        path = install_skill(root)[0].path
        path.write_text(path.read_text().replace("<!-- vl-release:generated -- ", "<!-- vl-release:generated version=0.1.1 -- "))
        self.assertEqual(install_skill(root, check=True)[0].status, "stale")
        self.assertEqual(install_skill(root)[0].status, "updated")
        self.assertEqual(install_skill(root, check=True)[0].status, "unchanged")

    def test_check_warns_about_missing_skills(self) -> None:
        root = self.make_repo()
        warnings = json.loads(self.vlr("check", "--json", cwd=root)[1])["warnings"]
        self.assertTrue(any("SKILL.md is missing" in w for w in warnings))
        install_skill(root)
        warnings = json.loads(self.vlr("check", "--json", cwd=root)[1])["warnings"]
        self.assertTrue(any("PROJECT.md is missing" in w for w in warnings))
        install_local_skill(self.config(root))
        self.assertEqual(json.loads(self.vlr("check", "--json", cwd=root)[1])["warnings"], [])


class HelpTests(RepoTestCase):
    def test_topics_work_anywhere(self) -> None:
        outside = self.tmp / "nowhere"
        outside.mkdir()
        code, out, _ = self.vlr("help", cwd=outside)
        self.assertEqual(code, 0)
        for topic in ("config", "staging", "ci"):
            self.assertIn(topic, out)
            code, text, _ = self.vlr("help", topic, cwd=outside)
            self.assertEqual(code, 0)
            self.assertTrue(text.startswith("# "))

    def test_config_reference_covers_the_schema(self) -> None:
        from vlrelease.config import TARGET_KINDS

        _code, text, _ = self.vlr("help", "config", cwd=self.tmp)
        for kind in TARGET_KINDS:
            self.assertIn(f"`{kind}`", text)
        for section in ("[tool]", "[project]", "[version]", "[release]", "[debian]", "[[debian.packages]]", "[source_archive]", "[publish.apt]", "[homebrew]"):
            self.assertIn(section, text)
        for key in ("requires", "repository", "canonical", "targets", "changelog_next", "test_command", "pre_build",
                    "build_excludes", "extra_artifacts", "required_paths", "forbidden_paths", "any_of", "identical_files",
                    "verify_timeout", "tap_path", "source"):
            self.assertIn(f"`{key}`", text)


class LayoutTests(unittest.TestCase):
    def test_every_test_directory_is_a_package(self) -> None:
        """unittest discovery silently skips directories without __init__.py; never allow that."""
        tests_root = SOURCE_ROOT / "tests"
        for directory in [tests_root, *[p for p in tests_root.rglob("*") if p.is_dir() and p.name != "__pycache__"]]:
            self.assertTrue((directory / "__init__.py").is_file(), f"{directory} is missing __init__.py")

    def test_discovery_finds_every_test_module(self) -> None:
        modules = sorted(p.stem for p in (SOURCE_ROOT / "tests").glob("test_*.py"))
        suite = unittest.defaultTestLoader.discover(str(SOURCE_ROOT / "tests"), top_level_dir=str(SOURCE_ROOT))
        found: set[str] = set()

        def walk(item) -> None:
            if isinstance(item, unittest.TestSuite):
                for child in item:
                    walk(child)
            else:
                found.add(type(item).__module__.rsplit(".", 1)[-1])

        walk(suite)
        self.assertEqual(sorted(found & set(modules)), modules)


class DogfoodTests(unittest.TestCase):
    def test_this_repository_satisfies_its_own_contract(self) -> None:
        if not (SOURCE_ROOT / ".git").exists():
            self.skipTest("not a Git checkout (e.g. inside the Debian build tree)")
        result = run([sys.executable, "-m", "vlrelease", "check", "--json"], SOURCE_ROOT)
        payload = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0, payload["errors"])
        # Release-phase warnings (e.g. docs staged before the next bump) are normal during development;
        # the repository's own agent skill and PROJECT.md must always be current.
        self.assertEqual([w for w in payload["warnings"] if "skill" in w], [])


if __name__ == "__main__":
    unittest.main()
