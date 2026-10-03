from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from tests.support import BASIC_CONFIG, DEBIAN_CONFIG, SOURCE_ROOT, RepoTestCase
from vlrelease import __version__
from vlrelease.errors import EXIT_FAILURE, EXIT_INTEGRITY, EXIT_USAGE, IntegrityError
from vlrelease.skill import install_skill


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
    def test_install_is_idempotent_and_repo_aware(self) -> None:
        root = self.make_repo(config=DEBIAN_CONFIG, debian=True)
        first = install_skill(self.config(root))
        self.assertEqual([(i.format, i.status) for i in first], [("claude", "installed")])
        text = first[0].path.read_text()
        self.assertTrue(text.startswith("---\nname: vl-release\ndescription: Release workflow for Demo"))
        for fragment in (".release/CHANGELOG_NEXT.md", "debian/changelog", "vlr prepare", "only after publication succeeded"):
            self.assertIn(fragment, text)
        self.assertEqual(install_skill(self.config(root))[0].status, "unchanged")
        self.assertEqual(install_skill(self.config(root), check=True)[0].status, "unchanged")

    def test_non_debian_rendering_omits_debian(self) -> None:
        root = self.make_repo(config=BASIC_CONFIG)
        text = install_skill(self.config(root))[0].path.read_text()
        self.assertNotIn("CHANGELOG_NEXT", text)
        self.assertNotIn("debian/changelog", text)
        self.assertIn("(.release/RELEASE_NOTES_NEXT.md)", text)

    def test_update_check_and_refuse(self) -> None:
        root = self.make_repo()
        path = install_skill(self.config(root))[0].path
        path.write_text(path.read_text().replace("generated version=", "generated version=0.0.0-old "))
        self.assertEqual(install_skill(self.config(root), check=True)[0].status, "stale")
        self.assertEqual(self.vlr("install-skill", "--check", cwd=root)[0], EXIT_FAILURE)
        self.assertEqual(install_skill(self.config(root))[0].status, "updated")
        path.write_text("# my own hand-written skill\n")
        self.assertEqual(install_skill(self.config(root))[0].status, "refused")
        self.assertEqual(path.read_text(), "# my own hand-written skill\n")
        self.assertEqual(install_skill(self.config(root), force=True)[0].status, "updated")

    def test_agents_format(self) -> None:
        root = self.make_repo()
        (root / ".agents").mkdir()
        formats = sorted(i.format for i in install_skill(self.config(root)))
        self.assertEqual(formats, ["agents", "claude"])
        self.assertTrue((root / ".agents/skills/vl-release/SKILL.md").is_file())


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
        self.assertEqual(payload["warnings"], [])


if __name__ == "__main__":
    unittest.main()
