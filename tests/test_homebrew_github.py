from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

from tests.support import DEBIAN_CONFIG, RepoTestCase, git
from vlrelease.checksums import sha256_file, write_sha256sums
from vlrelease.errors import IntegrityError, ReleaseError
from vlrelease.github_release import publish_github_release
from vlrelease.homebrew import formula_facts, publish_tap, render_formula, validate_rendered_formula
from vlrelease.prepare import prepare_release
from vlrelease.sourcearchive import build_source_archive
from vlrelease.targets import HOMEBREW_PLACEHOLDER_SHA256

FORMULA = f"""# frozen_string_literal: true

class Demo < Formula
  desc "Demo tool"
  homepage "https://github.com/example/demo"
  url "https://github.com/example/demo/releases/download/v1.0.0/demo-1.0.0.tar.gz"
  sha256 "{HOMEBREW_PLACEHOLDER_SHA256}"
  license "MIT"

  def install
    bin.install "demo"
  end
end
"""

HOMEBREW_CONFIG = DEBIAN_CONFIG + """
[source_archive]

[homebrew]
formula = "packaging/demo.rb"
tap = "example/homebrew-tap"
"""


class HomebrewTests(RepoTestCase):
    def prepared(self) -> Path:
        root = self.make_repo(config=HOMEBREW_CONFIG, debian=True, version="1.2.0", files={"packaging/demo.rb": FORMULA})
        prepare_release(self.config(root))
        build_source_archive(self.config(root))
        return root

    def tap(self) -> tuple[Path, Path]:
        bare = self.tmp / "tap.git"
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(bare))
        seed = self.tmp / "tap-seed"
        git(self.tmp, "clone", "-q", str(bare), str(seed))
        (seed / "Formula").mkdir()
        (seed / "Formula" / ".keep").write_text("")
        (seed / "README.md").write_text("tap\n")
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "-m", "seed")
        git(seed, "push", "-q", "origin", "HEAD:main")
        checkout = self.tmp / "tap-checkout"
        git(self.tmp, "clone", "-q", str(bare), str(checkout))
        return bare, checkout

    def test_render_points_at_release_asset_with_local_sha(self) -> None:
        root = self.prepared()
        path = render_formula(self.config(root))
        facts = formula_facts(path.read_text())
        self.assertEqual(path, root / "release" / "homebrew" / "demo.rb")
        self.assertEqual(facts.url, "https://github.com/example/demo/releases/download/v1.2.0/demo-1.2.0.tar.gz")
        self.assertEqual(facts.sha256, sha256_file(root / "release" / "demo-1.2.0.tar.gz"))
        self.assertEqual(facts.version, "1.2.0")
        self.assertEqual(validate_rendered_formula(self.config(root))[0], [])
        with self.assertRaises(IntegrityError):
            render_formula(self.config(root), sha256="0" * 64)

    def test_render_requires_source_archive(self) -> None:
        root = self.make_repo(config=HOMEBREW_CONFIG, debian=True, version="1.2.0", files={"packaging/demo.rb": FORMULA})
        prepare_release(self.config(root))
        with self.assertRaisesRegex(ReleaseError, "source-archive"):
            render_formula(self.config(root))

    def publish(self, root: Path, checkout: Path, **kwargs):
        sha = formula_facts((root / "release" / "homebrew" / "demo.rb").read_text()).sha256
        return publish_tap(self.config(root), checkout, fetch=kwargs.pop("fetch", lambda _url: sha), log=lambda _l: None, **kwargs)

    def test_publish_is_idempotent_and_immutable(self) -> None:
        root = self.prepared()
        render_formula(self.config(root))
        bare, checkout = self.tap()
        first = self.publish(root, checkout)
        self.assertEqual(first.status, "published")
        self.assertEqual(git(bare, "log", "-1", "--format=%s", "main"), "demo 1.2.0")
        published = git(bare, "show", "main:Formula/demo.rb")
        self.assertEqual(published, (root / "release/homebrew/demo.rb").read_text().rstrip("\n"))
        self.assertEqual(self.publish(root, checkout).status, "already-published")
        # Same version, different bytes in the tap -> refused.
        tampered = (checkout / "Formula/demo.rb").read_text().replace(formula_facts(published).sha256, "e" * 64)
        (checkout / "Formula/demo.rb").write_text(tampered)
        with self.assertRaisesRegex(IntegrityError, "REFUSING TO CHANGE A PUBLISHED FORMULA"):
            self.publish(root, checkout)

    def test_url_must_serve_declared_sha(self) -> None:
        root = self.prepared()
        render_formula(self.config(root))
        _bare, checkout = self.tap()
        with self.assertRaisesRegex(IntegrityError, "serves sha256"):
            self.publish(root, checkout, fetch=lambda _url: "0" * 64)

    def test_refuses_downgrade(self) -> None:
        root = self.prepared()
        render_formula(self.config(root))
        _bare, checkout = self.tap()
        newer = FORMULA.replace("v1.0.0/demo-1.0.0", "v9.0.0/demo-9.0.0").replace(HOMEBREW_PLACEHOLDER_SHA256, "a" * 64)
        (checkout / "Formula/demo.rb").write_text(newer)
        with self.assertRaisesRegex(IntegrityError, "newer than 1.2.0"):
            self.publish(root, checkout)

    def test_push_race_is_retried(self) -> None:
        root = self.prepared()
        render_formula(self.config(root))
        bare, checkout = self.tap()
        other = self.tmp / "other"
        git(self.tmp, "clone", "-q", str(bare), str(other))
        (other / "Formula/other.rb").write_text("class Other < Formula\nend\n")
        git(other, "add", "-A")
        git(other, "commit", "-q", "-m", "other 1.0")
        git(other, "push", "-q", "origin", "HEAD:main")
        self.assertEqual(self.publish(root, checkout).status, "published")
        self.assertEqual(git(bare, "log", "--format=%s", "main").splitlines()[:2], ["demo 1.2.0", "other 1.0"])


class FakeGh:
    """Records `gh` invocations and serves a canned release."""

    def __init__(self, release: dict | None) -> None:
        self.release = release
        self.calls: list[list[str]] = []

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        if args[1] == "api":
            if self.release is None:
                return subprocess.CompletedProcess(args, 1, "", "gh: Not Found (HTTP 404)")
            return subprocess.CompletedProcess(args, 0, json.dumps(self.release), "")
        return subprocess.CompletedProcess(args, 0, "", "")

    def commands(self) -> list[str]:
        return [" ".join(call[1:3]) for call in self.calls]


class GitHubReleaseTests(RepoTestCase):
    def prepared(self) -> Path:
        root = self.make_repo(config=DEBIAN_CONFIG, debian=True, version="1.2.0")
        prepare_release(self.config(root))
        out = root / "release"
        out.mkdir()
        (out / "demo_1.2.0-1_all.deb").write_bytes(b"deb")
        write_sha256sums(out)
        return root

    def test_creates_release_with_prepared_title_and_body(self) -> None:
        root = self.prepared()
        gh = FakeGh(None)
        result = publish_github_release(self.config(root), runner=gh, log=lambda _l: None)
        self.assertTrue(result.created)
        create = next(call for call in gh.calls if call[1:3] == ["release", "create"])
        self.assertIn("--verify-tag", create)
        self.assertEqual(create[create.index("--title") + 1], "v1.2.0 — Frobnication arrives")
        self.assertEqual(sorted(result.uploaded), ["SHA256SUMS", "demo_1.2.0-1_all.deb"])
        self.assertNotIn("--clobber", " ".join(" ".join(c) for c in gh.calls))

    def test_existing_identical_assets_are_skipped(self) -> None:
        root = self.prepared()
        assets = [
            {"name": path.name, "digest": f"sha256:{sha256_file(path)}"}
            for path in (root / "release").iterdir()
        ]
        body = (root / "RELEASE_NOTES.md").read_text()
        from vlrelease.prepare import current_entry

        release = {"name": "v1.2.0 — Frobnication arrives", "body": current_entry(self.config(root)).body, "assets": assets}
        gh = FakeGh(release)
        result = publish_github_release(self.config(root), runner=gh, log=lambda _l: None)
        self.assertFalse(result.created or result.edited)
        self.assertEqual(result.uploaded, [])
        self.assertEqual(len(result.skipped), 2)
        self.assertTrue(body)

    def test_existing_release_with_stale_notes_is_edited(self) -> None:
        root = self.prepared()
        gh = FakeGh({"name": "old", "body": "old", "assets": []})
        result = publish_github_release(self.config(root), runner=gh, log=lambda _l: None)
        self.assertTrue(result.edited)
        self.assertIn("release edit", gh.commands())

    def test_different_existing_asset_is_refused(self) -> None:
        root = self.prepared()
        gh = FakeGh({"name": "x", "body": "x", "assets": [{"name": "demo_1.2.0-1_all.deb", "digest": "sha256:" + "0" * 64}]})
        with self.assertRaisesRegex(IntegrityError, "REFUSING TO REPLACE"):
            publish_github_release(self.config(root), runner=gh, log=lambda _l: None)

    def test_requires_prepared_release(self) -> None:
        root = self.make_repo(config=DEBIAN_CONFIG, debian=True, version="1.2.0")
        with self.assertRaisesRegex(ReleaseError, "not prepared"):
            publish_github_release(self.config(root), runner=FakeGh(None), log=lambda _l: None)


if __name__ == "__main__":
    unittest.main()
