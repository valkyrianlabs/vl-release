from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import tarfile
import unittest
from pathlib import Path

from tests.support import RepoTestCase, requires_tools
from vlrelease.artifacts import artifact_info, validate_artifacts
from vlrelease.checks import check_repository
from vlrelease.checksums import write_sha256sums
from vlrelease.errors import ConfigError, IntegrityError, ReleaseError
from vlrelease.npm_publish import (
    PackumentNotFound,
    RegistrySettings,
    nerf_dart,
    packument_url,
    publish_npm,
    publish_with_npm,
    render_userconfig,
    verify_npm,
)
from vlrelease.npmpkg import build_npm, identify_tarball, integrity_of, tarball_name
from vlrelease.prepare import prepare_release

NPM_CONFIG = """\
schema_version = 1

[project]
name = "Demo"
package = "demo"
repository = "example/demo"

[version]
canonical = { kind = "package_json", path = "package.json" }

[npm]
required_paths = ["package/package.json", "package/index.js"]
forbidden_paths = ["package/secret*"]
identical_files = [{ member = "package/README.md", source = "README.md" }]

[[publish.npm]]
name = "npmjs"
registry = "https://registry.npmjs.org/"
auth = "oidc"

[[publish.npm]]
name = "private"
registry_env = "PRIVATE_NPM_REGISTRY"
auth = "basic"
username_env = "PRIVATE_NPM_USER"
password_env = "PRIVATE_NPM_PASSWORD"
"""

PACKAGE_JSON = '{\n  "name": "@demo/widget",\n  "version": "%s",\n  "files": ["index.js", "README.md"]\n}\n'

ENV = {
    "RELEASE_PUBLISH_MODE": "enabled",
    "PRIVATE_NPM_REGISTRY": "https://npm.example.com/repository/npm-private",
    "PRIVATE_NPM_USER": "deployer",
    "PRIVATE_NPM_PASSWORD": "s3cr3t-p@ss",
    "ACTIONS_ID_TOKEN_REQUEST_URL": "https://token.actions.example/",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "gh-oidc",
}


def write_tarball(path: Path, files: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    stage = path.parent / f".stage-{path.name}"
    with tarfile.open(path, "w:gz") as archive:
        for name, data in files.items():
            source = stage / name
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(data)
            archive.add(source, arcname=name)
    return path


class NpmConfigTests(RepoTestCase):
    def load(self, text: str):
        root = self.make_repo(config=text, files={"package.json": PACKAGE_JSON % "1.0.0"}, commit=False)
        return self.config(root)

    def test_parses_registries(self) -> None:
        config = self.load(NPM_CONFIG)
        self.assertEqual(config.npm.packer, "npm")
        self.assertEqual(config.npm.dist_tag, "latest")
        self.assertEqual([r.name for r in config.npm_registries], ["npmjs", "private"])
        self.assertEqual(config.npm_registries[0].auth, "oidc")
        self.assertIsNone(config.npm_registries[0].username_env)

    def test_auth_is_required_and_basic_needs_its_variables(self) -> None:
        with self.assertRaisesRegex(ConfigError, r"publish.npm\[0\].auth is required"):
            self.load(NPM_CONFIG.replace('auth = "oidc"', ""))
        with self.assertRaisesRegex(ConfigError, "needs username_env and password_env"):
            self.load(NPM_CONFIG.replace('password_env = "PRIVATE_NPM_PASSWORD"\n', ""))
        with self.assertRaisesRegex(ConfigError, "only apply to auth"):
            self.load(NPM_CONFIG.replace('auth = "oidc"', 'auth = "oidc"\nusername_env = "U"'))

    def test_rejects_bad_tables(self) -> None:
        with self.assertRaisesRegex(ConfigError, r"requires \[npm\]"):
            self.load(NPM_CONFIG.replace("[npm]\n", "[npm]\nenabled = false\n"))
        with self.assertRaisesRegex(ConfigError, 'must start with "package/"'):
            self.load(NPM_CONFIG.replace('"package/index.js"', '"index.js"'))
        with self.assertRaisesRegex(ConfigError, "same registry name"):
            self.load(NPM_CONFIG.replace('name = "private"', 'name = "npmjs"'))
        with self.assertRaisesRegex(ConfigError, "unknown key"):
            self.load(NPM_CONFIG.replace('auth = "oidc"', 'auth = "oidc"\npasword = "x"'))
        with self.assertRaisesRegex(ConfigError, "registry_env"):
            self.load(NPM_CONFIG.replace('registry = "https://registry.npmjs.org/"\n', ""))
        with self.assertRaisesRegex(ConfigError, "packer"):
            self.load(NPM_CONFIG.replace("[npm]\n", '[npm]\npacker = "yarn"\n'))

    def test_check_requires_package_json_as_version_target_and_publishable(self) -> None:
        root = self.make_repo(
            config=NPM_CONFIG.replace('canonical = { kind = "package_json", path = "package.json" }', 'canonical = "VERSION"'),
            files={"package.json": PACKAGE_JSON % "1.0.0"},
        )
        report = check_repository(self.config(root))
        self.assertTrue(any("package.json is not a version target" in error for error in report.errors), report.errors)

        root = self.make_repo(config=NPM_CONFIG, files={"package.json": '{"name": "x", "version": "1.0.0", "private": true}\n'})
        report = check_repository(self.config(root))
        self.assertTrue(any('"private": true' in error for error in report.errors), report.errors)

        root = self.make_repo(
            config=NPM_CONFIG,
            files={"package.json": '{"name": "x", "version": "1.0.0", "publishConfig": {"registry": "https://x/"}}\n'},
        )
        report = check_repository(self.config(root))
        self.assertTrue(any("publishConfig.registry" in error for error in report.errors), report.errors)


class NpmNamingTests(unittest.TestCase):
    def test_tarball_and_packument_names(self) -> None:
        self.assertEqual(tarball_name("@valkyrianlabs/payload-markdown", "1.7.0"), "valkyrianlabs-payload-markdown-1.7.0.tgz")
        self.assertEqual(tarball_name("left-pad", "1.0.0"), "left-pad-1.0.0.tgz")
        self.assertEqual(
            packument_url("https://registry.npmjs.org/", "@valkyrianlabs/payload-markdown"),
            "https://registry.npmjs.org/@valkyrianlabs%2Fpayload-markdown",
        )
        self.assertEqual(nerf_dart("https://npm.example.com/repository/npm-private/"), "//npm.example.com/repository/npm-private/")


class PreparedNpmRepo(RepoTestCase):
    def prepared_repo(self, *, version: str = "1.2.0", config: str = NPM_CONFIG) -> Path:
        root = self.make_repo(
            config=config,
            version=version,
            files={
                "package.json": PACKAGE_JSON % version,
                "index.js": "module.exports = 42\n",
                "README.md": "# widget\n",
            },
        )
        prepare_release(self.config(root))
        return root

    def fake_tarball(self, root: Path, *, version: str = "1.2.0", extra: dict[str, bytes] | None = None) -> Path:
        files = {
            "package/package.json": (PACKAGE_JSON % version).encode(),
            "package/index.js": b"module.exports = 42\n",
            "package/README.md": b"# widget\n",
            **(extra or {}),
        }
        path = write_tarball(root / "release" / tarball_name("@demo/widget", version), files)
        write_sha256sums(root / "release")
        return path


@requires_tools("npm")
class NpmBuildTests(PreparedNpmRepo):
    def test_builds_and_validates_the_tarball_from_the_prepared_tree(self) -> None:
        root = self.prepared_repo()
        (root / "release").mkdir()
        (root / "release" / "stale-0.0.1.tgz").write_bytes(b"old")
        result = build_npm(self.config(root), log=lambda _line: None)
        self.assertEqual(result.tarball.name, "demo-widget-1.2.0.tgz")
        self.assertEqual(sorted(p.name for p in (root / "release").glob("*.tgz")), ["demo-widget-1.2.0.tgz"])
        write_sha256sums(root / "release")
        report = validate_artifacts(self.config(root))
        self.assertEqual(report.issues, [])
        self.assertIn("demo-widget-1.2.0.tgz", report.checked)
        kinds = {row["name"]: row["kind"] for row in artifact_info(self.config(root))}
        self.assertEqual(kinds["demo-widget-1.2.0.tgz"], "npm-package")

    def test_json_output_is_not_polluted_by_the_packer(self) -> None:
        root = self.prepared_repo()
        code, out, _err = self.vlr("--repo", str(root), "build-npm", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["tarball"].rsplit("/", 1)[-1], "demo-widget-1.2.0.tgz")

    def test_pre_pack_commands_run_in_the_package_directory(self) -> None:
        config = NPM_CONFIG.replace("[npm]\n", '[npm]\npre_pack = [["sh", "-c", "echo built > index.js"]]\n')
        root = self.prepared_repo(config=config)
        result = build_npm(self.config(root), log=lambda _line: None)
        with tarfile.open(result.tarball) as archive:
            self.assertEqual(archive.extractfile("package/index.js").read(), b"built\n")


class NpmValidationTests(PreparedNpmRepo):
    def test_contract_violations_are_reported(self) -> None:
        root = self.prepared_repo()
        self.fake_tarball(root, extra={"package/secret.key": b"x", "package/README.md": b"# changed\n"})
        report = validate_artifacts(self.config(root))
        text = "\n".join(report.issues)
        self.assertIn("forbidden path present: package/secret*", text)
        self.assertIn("package/README.md differs", text)

    def test_wrong_version_and_extra_tarballs(self) -> None:
        root = self.prepared_repo()
        tarball = self.fake_tarball(root)
        write_tarball(tarball.with_name("demo-widget-1.1.0.tgz"), {"package/package.json": (PACKAGE_JSON % "1.1.0").encode()})
        write_sha256sums(root / "release")
        report = validate_artifacts(self.config(root))
        self.assertTrue(any("unexpected npm tarball demo-widget-1.1.0.tgz" in issue for issue in report.issues))

    def test_pack_runs_without_npm_housekeeping(self) -> None:
        root = self.prepared_repo()
        seen: list[tuple[list[str], dict | None]] = []

        def runner(command, **kwargs):
            seen.append((command, kwargs.get("env")))
            if command[:2] == ["npm", "pack"]:
                write_tarball(root / "release" / "demo-widget-1.2.0.tgz", {"package/package.json": (PACKAGE_JSON % "1.2.0").encode()})
            return subprocess.CompletedProcess(command, 0)

        config_text = NPM_CONFIG.replace("[npm]\n", '[npm]\npre_pack = [["true"]]\n')
        (root / "release.toml").write_text(config_text, encoding="utf-8")
        build_npm(self.config(root), runner=runner, log=lambda _line: None)
        (pre_pack, pre_env), (pack, pack_env) = seen
        self.assertEqual(pre_pack, ["true"])
        self.assertIsNone(pre_env)  # build commands keep the caller's environment
        self.assertEqual(pack[:2], ["npm", "pack"])
        self.assertEqual(pack_env["NPM_CONFIG_UPDATE_NOTIFIER"], "false")
        self.assertEqual(pack_env["NPM_CONFIG_AUDIT"], "false")

    def test_build_refuses_an_unprepared_tree(self) -> None:
        root = self.make_repo(config=NPM_CONFIG, files={"package.json": PACKAGE_JSON % "1.0.0"})
        with self.assertRaisesRegex(ReleaseError, "not prepared"):
            build_npm(self.config(root))


class FakeRegistries:
    """Packuments per registry host; `publish` installs the uploaded version like a registry would."""

    def __init__(self) -> None:
        self.packuments: dict[str, dict] = {}
        self.uploads: list[tuple[str, str, str]] = []
        self.reads: list[tuple[str, dict]] = []
        self.unreadable: set[str] = set()
        self.publish_corrupt = False

    def host(self, url: str) -> str:
        return url.split("/")[2]

    def get(self, url: str, headers) -> dict:
        self.reads.append((url, dict(headers)))
        host = self.host(url)
        if host in self.unreadable:
            raise ValueError("HTTP 500")
        if host not in self.packuments:
            raise PackumentNotFound(url)
        return json.loads(json.dumps(self.packuments[host]))

    def add(self, host: str, version: str, integrity: str | None, shasum: str | None = None, latest: str | None = None) -> None:
        packument = self.packuments.setdefault(host, {"versions": {}, "dist-tags": {}})
        dist = {k: v for k, v in (("integrity", integrity), ("shasum", shasum)) if v}
        packument["versions"][version] = {"dist": dist}
        packument["dist-tags"]["latest"] = latest or version

    def upload(self, identity, settings: RegistrySettings, tag: str, access: str) -> None:
        self.uploads.append((settings.name, tag, access))
        host = self.host(settings.url)
        packument = self.packuments.setdefault(host, {"versions": {}, "dist-tags": {}})
        integrity = "sha512-AAAA" if self.publish_corrupt else identity.integrity
        packument["versions"][identity.version] = {"dist": {"integrity": integrity, "shasum": identity.shasum}}
        packument["dist-tags"][tag] = identity.version


class PublishNpmTests(PreparedNpmRepo):
    NPMJS = "registry.npmjs.org"
    PRIVATE = "npm.example.com"

    def setUp(self) -> None:
        super().setUp()
        self.root = self.prepared_repo()
        self.tarball = self.fake_tarball(self.root)
        self.identity = identify_tarball(self.tarball, "@demo/widget", "1.2.0")
        self.registries = FakeRegistries()

    def publish(self, **kwargs):
        options = dict(env=ENV, uploader=self.registries.upload, packument_get=self.registries.get, sleep=lambda _s: None, log=lambda _l: None)
        options.update(kwargs)
        return publish_npm(self.config(self.root), **options)

    def test_uploads_to_every_registry_and_verifies(self) -> None:
        result = self.publish()
        self.assertEqual(self.registries.uploads, [("npmjs", "latest", "public"), ("private", "latest", "public")])
        self.assertEqual(result.verified, ["npmjs", "private"])
        self.assertEqual(result.as_dict()["integrity"], self.identity.integrity)

    def test_private_registry_reads_use_basic_auth_and_npmjs_reads_are_anonymous(self) -> None:
        self.publish(dry_run=True)
        headers = {self.registries.host(url): h for url, h in self.registries.reads}
        self.assertEqual(headers[self.NPMJS], {})
        self.assertTrue(headers[self.PRIVATE]["Authorization"].startswith("Basic "))

    def test_identical_republish_is_skipped(self) -> None:
        self.registries.add(self.NPMJS, "1.2.0", self.identity.integrity)
        self.registries.add(self.PRIVATE, "1.2.0", None, shasum=self.identity.shasum)
        result = self.publish()
        self.assertEqual(self.registries.uploads, [])
        self.assertEqual([plan.action for plan in result.plans], ["skip-identical", "skip-identical"])

    def test_conflict_on_any_registry_refuses_before_uploading_anywhere(self) -> None:
        self.registries.add(self.PRIVATE, "1.2.0", "sha512-different")
        with self.assertRaisesRegex(IntegrityError, "REFUSING TO OVERWRITE"):
            self.publish()
        self.assertEqual(self.registries.uploads, [])

    def test_published_without_digests_is_refused(self) -> None:
        self.registries.add(self.NPMJS, "1.2.0", None)
        with self.assertRaisesRegex(IntegrityError, "cannot be proven"):
            self.publish()

    def test_unreadable_registry_is_refused(self) -> None:
        self.registries.unreadable.add(self.NPMJS)
        with self.assertRaisesRegex(ReleaseError, "Refusing to publish without knowing"):
            self.publish()
        self.assertEqual(self.registries.uploads, [])

    def test_older_than_latest_is_refused_or_goes_to_the_maintenance_tag(self) -> None:
        self.registries.add(self.NPMJS, "2.0.0", "sha512-x")
        with self.assertRaisesRegex(IntegrityError, "--allow-older-version"):
            self.publish(registries=["npmjs"])
        self.publish(registries=["npmjs"], allow_older_version=True)
        self.assertEqual(self.registries.uploads, [("npmjs", "maintenance", "public")])
        self.assertEqual(self.registries.packuments[self.NPMJS]["dist-tags"]["latest"], "2.0.0")

    def test_verification_detects_wrong_bytes_and_times_out(self) -> None:
        self.registries.publish_corrupt = True
        with self.assertRaisesRegex(IntegrityError, "expected"):
            self.publish(registries=["npmjs"])
        registries = FakeRegistries()
        with self.assertRaisesRegex(ReleaseError, "did not confirm"):
            publish_npm(
                self.config(self.root),
                registries=["npmjs"],
                env=ENV,
                uploader=lambda *args: None,
                packument_get=registries.get,
                verify_timeout=20,
                sleep=lambda _s: None,
                log=lambda _l: None,
            )

    def test_gates_and_credentials(self) -> None:
        result = self.publish(env={**ENV, "RELEASE_PUBLISH_MODE": "disabled"})
        self.assertEqual(result.skipped_reason, "publication mode is disabled")
        with self.assertRaisesRegex(ReleaseError, "required for this run"):
            self.publish(env={**ENV, "RELEASE_PUBLISH_MODE": "disabled"}, require_enabled=True)
        self.publish(env={**ENV, "RELEASE_PUBLISH_MODE": "nexus"}, dry_run=True)  # APT workflows' value also publishes
        without_oidc = {k: v for k, v in ENV.items() if not k.startswith("ACTIONS_")}
        with self.assertRaisesRegex(ConfigError, "id-token: write"):
            self.publish(env=without_oidc)
        without_password = {k: v for k, v in ENV.items() if k != "PRIVATE_NPM_PASSWORD"}
        with self.assertRaisesRegex(ConfigError, "PRIVATE_NPM_PASSWORD"):
            self.publish(env=without_password)
        # A dry run only reads, so it needs no upload credentials.
        self.publish(env={"RELEASE_PUBLISH_MODE": "enabled", "PRIVATE_NPM_REGISTRY": ENV["PRIVATE_NPM_REGISTRY"]}, dry_run=True)
        with self.assertRaisesRegex(ConfigError, "Unknown npm registry"):
            self.publish(registries=["nope"])

    def test_sha256sums_must_match(self) -> None:
        self.tarball.write_bytes(self.tarball.read_bytes() + b"tamper")
        with self.assertRaisesRegex(IntegrityError, "does not match SHA256SUMS"):
            self.publish()

    def test_verify_npm_standalone(self) -> None:
        self.registries.add(self.NPMJS, "1.2.0", self.identity.integrity)
        identity, verified = verify_npm(
            self.config(self.root), registries=["npmjs"], env=ENV, packument_get=self.registries.get, sleep=lambda _s: None, log=lambda _l: None
        )
        self.assertEqual((identity.version, verified), ("1.2.0", ["npmjs"]))


class NpmUploadTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.tarball = write_tarball(Path(self._tmp.name) / "demo-widget-1.2.0.tgz", {"package/package.json": b"{}"})
        integrity, shasum = integrity_of(self.tarball)
        from vlrelease.npmpkg import NpmIdentity

        self.identity = NpmIdentity(self.tarball, "@demo/widget", "1.2.0", "0" * 64, integrity, shasum)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def runner(self, *, npm_version: str = "11.6.0", returncode: int = 0, output: str = ""):
        calls: list[dict] = []

        def run(command, **kwargs):
            if command[:2] == ["npm", "--version"]:
                return subprocess.CompletedProcess(command, 0, stdout=npm_version + "\n", stderr="")
            userconfig = Path(kwargs["env"]["NPM_CONFIG_USERCONFIG"])
            calls.append(
                {
                    "command": command,
                    "env": kwargs["env"],
                    "cwd": kwargs["cwd"],
                    "userconfig": userconfig.read_text(),
                    "mode": stat.S_IMODE(os.stat(userconfig).st_mode),
                }
            )
            return subprocess.CompletedProcess(command, returncode, stdout=output, stderr=output)

        return run, calls

    def test_basic_credentials_live_only_in_a_private_userconfig(self) -> None:
        settings = RegistrySettings(name="private", url="https://npm.example.com/repo/", auth="basic", username="u", password="s3cr3t")
        run, calls = self.runner()
        publish_with_npm(self.identity, settings, "latest", "public", runner=run, env={"npm_config_registry": "https://evil/", "PATH": "/bin"})
        call = calls[0]
        self.assertNotIn("s3cr3t", " ".join(call["command"]))
        self.assertEqual(call["mode"], 0o600)
        self.assertEqual(call["userconfig"], render_userconfig(settings))
        self.assertTrue(call["userconfig"].startswith("//npm.example.com/repo/:_auth="))
        self.assertNotIn("npm_config_registry", call["env"])
        self.assertEqual(call["command"][:3], ["npm", "publish", str(self.tarball.resolve())])
        self.assertIn("--ignore-scripts", call["command"])
        self.assertFalse(Path(call["env"]["NPM_CONFIG_USERCONFIG"]).exists(), "temporary userconfig was not removed")

    def test_oidc_needs_a_recent_npm_and_writes_no_secret(self) -> None:
        settings = RegistrySettings(name="npmjs", url="https://registry.npmjs.org/", auth="oidc", provenance=True)
        run, _calls = self.runner(npm_version="10.9.2")
        with self.assertRaisesRegex(ReleaseError, "npm >= 11.5.1"):
            publish_with_npm(self.identity, settings, "latest", "public", runner=run, env={})
        run, calls = self.runner()
        publish_with_npm(self.identity, settings, "latest", "public", runner=run, env={})
        self.assertEqual(calls[0]["userconfig"], "")
        self.assertIn("--provenance", calls[0]["command"])

    def test_failure_output_is_redacted(self) -> None:
        settings = RegistrySettings(name="private", url="https://npm.example.com/", auth="token", token="tok-123")
        run, _calls = self.runner(returncode=1, output="npm error 401 for token tok-123")
        with self.assertRaises(ReleaseError) as caught:
            publish_with_npm(self.identity, settings, "latest", "public", runner=run, env={})
        self.assertNotIn("tok-123", str(caught.exception))
        self.assertIn("<redacted>", str(caught.exception))


ALIAS_CONFIG = NPM_CONFIG.replace(
    "[[publish.npm]]",
    '[[npm.aliases]]\nname = "widget"\nrewrite = ["package/name.js"]\n\n[[publish.npm]]',
    1,
)


class NpmAliasConfigTests(RepoTestCase):
    def load(self, text: str, package_json: str = PACKAGE_JSON % "1.0.0"):
        root = self.make_repo(config=text, files={"package.json": package_json}, commit=False)
        return self.config(root)

    def test_parses_aliases(self) -> None:
        config = self.load(ALIAS_CONFIG)
        self.assertEqual([(a.name, a.rewrite) for a in config.npm.aliases], [("widget", ("package/name.js",))])
        self.assertEqual(self.load(NPM_CONFIG).npm.aliases, ())

    def test_project_skill_names_the_aliases(self) -> None:
        from vlrelease.skill import render_local_skill

        self.assertIn("also published as `widget`", render_local_skill(self.load(ALIAS_CONFIG)))

    def test_rejects_bad_aliases(self) -> None:
        for bad, message in (
            ('name = "Widget"', "not a valid npm package name"),
            ('name = "@demo/"', "not a valid npm package name"),
            ('name = "widget"\nrewrite = ["name.js"]', 'path under "package/"'),
            ('name = "widget"\nrewrite = ["package/*.js"]', "exact tarball path"),
            ('name = "widget"\nrewrite = ["package/package.json"]', "always renamed"),
            ('name = "widget"\nrenames = []', "unknown key"),
        ):
            text = ALIAS_CONFIG.replace('name = "widget"\nrewrite = ["package/name.js"]', bad)
            with self.subTest(bad=bad), self.assertRaisesRegex(ConfigError, message):
                self.load(text)
        twice = ALIAS_CONFIG.replace("[[publish.npm]]", '[[npm.aliases]]\nname = "widget"\n\n[[publish.npm]]', 1)
        with self.assertRaisesRegex(ConfigError, "same package name more than once"):
            self.load(twice)

    def test_check_rejects_aliases_that_clash_with_the_package(self) -> None:
        for alias, message in (("@demo/widget", "repeats the package.json name"), ("demo-widget", "share a tarball file name")):
            root = self.make_repo(
                config=ALIAS_CONFIG.replace('name = "widget"', f'name = "{alias}"'), files={"package.json": PACKAGE_JSON % "1.0.0"}
            )
            report = check_repository(self.config(root))
            self.assertTrue(any(message in error for error in report.errors), report.errors)


class NpmAliasDerivationTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.canonical = self.dir / "demo-widget-1.2.0.tgz"
        files = {
            "package/package.json": (PACKAGE_JSON % "1.2.0").encode(),
            "package/name.js": b"export const NAME = '@demo/widget'\n",
            "package/index.js": b"// docs mention @demo/widget\n",
            "package/bin/cli.js": b"#!/usr/bin/env node\n",
        }
        write_tarball(self.canonical, files)
        # Give the bin an executable mode, like npm pack does.
        with tarfile.open(self.canonical) as archive:
            members = [(info, archive.extractfile(info).read() if info.isfile() else None) for info in archive.getmembers()]
        with tarfile.open(self.canonical, "w:gz") as archive:
            for info, data in members:
                if info.name.endswith("cli.js"):
                    info.mode = 0o755
                archive.addfile(info, io.BytesIO(data) if data is not None else None)
        from vlrelease.config import NpmAlias

        self.alias = NpmAlias("widget", ("package/name.js",))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def files(self, path: Path) -> dict[str, tuple[bytes, int]]:
        with tarfile.open(path) as archive:
            return {info.name: (archive.extractfile(info).read(), info.mode) for info in archive.getmembers() if info.isfile()}

    def test_only_the_name_and_rewrite_members_change(self) -> None:
        from vlrelease.npmpkg import check_alias_tarball, derive_alias_tarball

        target = self.dir / "widget-1.2.0.tgz"
        derive_alias_tarball(self.canonical, "@demo/widget", self.alias, target)
        before, after = self.files(self.canonical), self.files(target)
        self.assertEqual(sorted(before), sorted(after))
        self.assertEqual(json.loads(after["package/package.json"][0]), {**json.loads(before["package/package.json"][0]), "name": "widget"})
        self.assertEqual(after["package/package.json"][0], before["package/package.json"][0].replace(b'"@demo/widget"', b'"widget"'))
        self.assertEqual(after["package/name.js"][0], b"export const NAME = 'widget'\n")
        self.assertEqual(after["package/index.js"], before["package/index.js"])  # not listed in rewrite
        self.assertEqual(after["package/bin/cli.js"][1], 0o755)
        self.assertEqual(check_alias_tarball(self.canonical, "@demo/widget", self.alias, target), [])

        again = self.dir / "again" / "widget-1.2.0.tgz"
        again.parent.mkdir()
        derive_alias_tarball(self.canonical, "@demo/widget", self.alias, again)
        self.assertEqual(again.read_bytes(), target.read_bytes())  # deterministic: re-runs give identical integrity

    def test_refuses_rewrites_that_cannot_apply(self) -> None:
        from vlrelease.config import NpmAlias
        from vlrelease.npmpkg import derive_alias_tarball, rename_manifest

        target = self.dir / "widget-1.2.0.tgz"
        with self.assertRaisesRegex(ReleaseError, "does not contain @demo/widget"):
            derive_alias_tarball(self.canonical, "@demo/widget", NpmAlias("widget", ("package/bin/cli.js",)), target)
        self.assertFalse(target.exists())
        with self.assertRaisesRegex(ReleaseError, "not in the tarball: package/missing.js"):
            derive_alias_tarball(self.canonical, "@demo/widget", NpmAlias("widget", ("package/missing.js",)), target)
        with self.assertRaisesRegex(ReleaseError, "exactly one"):
            rename_manifest(b'{"name": "@demo/widget", "x": {"name": "@demo/widget"}}', "@demo/widget", "widget", "t")
        with self.assertRaisesRegex(ReleaseError, "is not @demo/widget"):
            rename_manifest(b'{"name": "other"}', "@demo/widget", "widget", "t")

    def test_check_reports_any_drift(self) -> None:
        from vlrelease.npmpkg import check_alias_tarball

        target = self.dir / "widget-1.2.0.tgz"
        files = {name: data for name, (data, _mode) in self.files(self.canonical).items()}
        renamed = {**files, "package/package.json": files["package/package.json"].replace(b'"@demo/widget"', b'"widget"')}

        write_tarball(target, {**renamed, "package/name.js": b"export const NAME = 'widget'\n", "package/index.js": b"changed\n"})
        issues = "\n".join(check_alias_tarball(self.canonical, "@demo/widget", self.alias, target))
        self.assertIn("package/index.js is not the canonical", issues)
        self.assertIn("package/bin/cli.js has mode", issues)

        write_tarball(target, {**renamed, "package/extra.js": b"x"})
        issues = "\n".join(check_alias_tarball(self.canonical, "@demo/widget", self.alias, target))
        self.assertIn("extra: package/extra.js", issues)
        self.assertIn("package/name.js is not the canonical", issues)


class NpmAliasBuildTests(PreparedNpmRepo):
    def alias_repo(self) -> Path:
        root = self.prepared_repo(config=ALIAS_CONFIG)
        return root

    def fake_build(self, root: Path) -> None:
        def runner(command, **kwargs):
            if command[:2] == ["npm", "pack"]:
                write_tarball(
                    root / "release" / "demo-widget-1.2.0.tgz",
                    {
                        "package/package.json": (PACKAGE_JSON % "1.2.0").encode(),
                        "package/index.js": b"module.exports = 42\n",
                        "package/README.md": b"# widget\n",
                        "package/name.js": b"module.exports = '@demo/widget'\n",
                    },
                )
            return subprocess.CompletedProcess(command, 0)

        result = build_npm(self.config(root), runner=runner, log=lambda _line: None)
        self.assertEqual([(name, path.name) for name, path in result.aliases], [("widget", "widget-1.2.0.tgz")])
        self.assertEqual(result.as_dict()["aliases"][0]["integrity"], result.alias_identities[0].integrity)
        write_sha256sums(root / "release")

    def test_build_derives_and_validation_accepts_the_alias(self) -> None:
        root = self.alias_repo()
        self.fake_build(root)
        report = validate_artifacts(self.config(root))
        self.assertEqual(report.issues, [])
        self.assertEqual(sorted(report.checked), ["demo-widget-1.2.0.tgz", "widget-1.2.0.tgz"])
        with tarfile.open(root / "release" / "widget-1.2.0.tgz") as archive:
            self.assertEqual(archive.extractfile("package/name.js").read(), b"module.exports = 'widget'\n")

    def test_validation_reports_a_missing_or_tampered_alias(self) -> None:
        root = self.alias_repo()
        self.fake_build(root)
        alias = root / "release" / "widget-1.2.0.tgz"
        write_tarball(alias, {"package/package.json": (PACKAGE_JSON % "1.2.0").replace("@demo/widget", "widget").encode()})
        issues = "\n".join(validate_artifacts(self.config(root)).issues)
        self.assertIn("members differ from demo-widget-1.2.0.tgz", issues)
        alias.unlink()
        issues = "\n".join(validate_artifacts(self.config(root)).issues)
        self.assertIn("widget-1.2.0.tgz for alias widget is missing", issues)

    def test_dry_run_lists_the_alias(self) -> None:
        root = self.alias_repo()
        code, out, _err = self.vlr("--repo", str(root), "build-npm", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("would derive widget@1.2.0", out)


@requires_tools("npm")
class NpmAliasRealPackTests(PreparedNpmRepo):
    def test_real_npm_pack_is_derived_and_validated(self) -> None:
        config = ALIAS_CONFIG.replace('rewrite = ["package/name.js"]', 'rewrite = ["package/index.js"]')
        root = self.make_repo(
            config=config,
            version="1.2.0",
            files={
                "package.json": PACKAGE_JSON % "1.2.0",
                "index.js": "module.exports = '@demo/widget'\n",
                "README.md": "# widget\n",
            },
        )
        prepare_release(self.config(root))
        build_npm(self.config(root), log=lambda _line: None)
        write_sha256sums(root / "release")
        self.assertEqual(validate_artifacts(self.config(root)).issues, [])
        listing = subprocess.run(["tar", "-tzf", str(root / "release" / "widget-1.2.0.tgz")], capture_output=True, text=True, check=True)
        self.assertEqual(sorted(listing.stdout.split()), ["package/README.md", "package/index.js", "package/package.json"])


class PackageRegistries(FakeRegistries):
    """Packuments per (registry host, package name)."""

    def key(self, url: str) -> str:
        package = url.rsplit("/", 1)[1].replace("%2F", "/").replace("%2f", "/")
        return f"{self.host(url)}/{package}"

    def get(self, url: str, headers) -> dict:
        self.reads.append((url, dict(headers)))
        if self.host(url) in self.unreadable:
            raise ValueError("HTTP 500")
        key = self.key(url)
        if key not in self.packuments:
            raise PackumentNotFound(url)
        return json.loads(json.dumps(self.packuments[key]))

    def add_package(self, host: str, package: str, version: str, integrity: str) -> None:
        packument = self.packuments.setdefault(f"{host}/{package}", {"versions": {}, "dist-tags": {}})
        packument["versions"][version] = {"dist": {"integrity": integrity}}
        packument["dist-tags"]["latest"] = version

    def upload(self, identity, settings: RegistrySettings, tag: str, access: str) -> None:
        if identity.name in getattr(self, "fail_for", ()):
            raise ReleaseError(f"npm publish of {identity.path.name} failed (E404)")
        self.uploads.append((identity.name, settings.name, tag))
        packument = self.packuments.setdefault(f"{self.host(settings.url)}/{identity.name}", {"versions": {}, "dist-tags": {}})
        packument["versions"][identity.version] = {"dist": {"integrity": identity.integrity}}
        packument["dist-tags"][tag] = identity.version


class PublishNpmAliasTests(PreparedNpmRepo):
    NPMJS = "registry.npmjs.org"
    PRIVATE = "npm.example.com"

    def setUp(self) -> None:
        super().setUp()
        self.root = self.prepared_repo(config=ALIAS_CONFIG)
        NpmAliasBuildTests.fake_build(self, self.root)  # type: ignore[arg-type]
        self.canonical = identify_tarball(self.root / "release" / "demo-widget-1.2.0.tgz", "@demo/widget", "1.2.0")
        self.alias = identify_tarball(self.root / "release" / "widget-1.2.0.tgz", "widget", "1.2.0")
        self.registries = PackageRegistries()

    def publish(self, **kwargs):
        options = dict(env=ENV, uploader=self.registries.upload, packument_get=self.registries.get, sleep=lambda _s: None, log=lambda _l: None)
        options.update(kwargs)
        return publish_npm(self.config(self.root), **options)

    def test_publishes_every_name_aliases_first(self) -> None:
        result = self.publish()
        self.assertEqual(
            self.registries.uploads,
            [
                ("widget", "npmjs", "latest"),
                ("widget", "private", "latest"),
                ("@demo/widget", "npmjs", "latest"),
                ("@demo/widget", "private", "latest"),
            ],
        )
        self.assertEqual(
            result.verified, ["widget@npmjs", "widget@private", "@demo/widget@npmjs", "@demo/widget@private"]
        )
        data = result.as_dict()
        self.assertEqual(data["package"], "@demo/widget")
        self.assertEqual([p["package"] for p in data["packages"]], ["@demo/widget", "widget"])
        self.assertTrue(all(r["verified"] for p in data["packages"] for r in p["registries"]))
        self.assertEqual(data["packages"][1]["registries"][0]["reason"], "new package: not on this registry yet")

    def test_conflict_on_the_alias_refuses_before_uploading_anything(self) -> None:
        self.registries.add_package(self.NPMJS, "widget", "1.2.0", "sha512-different")
        with self.assertRaisesRegex(IntegrityError, "REFUSING TO OVERWRITE.*widget@1.2.0"):
            self.publish()
        self.assertEqual(self.registries.uploads, [])

    def test_failed_alias_upload_leaves_the_canonical_package_alone_and_a_rerun_completes(self) -> None:
        self.registries.fail_for = {"widget"}
        with self.assertRaisesRegex(ReleaseError, "E404"):
            self.publish(registries=["npmjs"])
        self.assertEqual(self.registries.uploads, [])
        # The alias is then published by hand (bootstrapping a trusted publisher) with the CI-built bytes.
        self.registries.add_package(self.NPMJS, "widget", "1.2.0", self.alias.integrity)
        self.registries.fail_for = set()
        result = self.publish(registries=["npmjs"])
        self.assertEqual([plan.action for plan in result.plans], ["upload", "skip-identical"])
        self.assertEqual(self.registries.uploads, [("@demo/widget", "npmjs", "latest")])

    def test_rerun_after_success_uploads_nothing(self) -> None:
        self.publish()
        self.registries.uploads.clear()
        result = self.publish()
        self.assertEqual(self.registries.uploads, [])
        self.assertEqual({plan.action for plan in result.plans}, {"skip-identical"})

    def test_every_tarball_must_be_checksummed(self) -> None:
        alias = self.root / "release" / "widget-1.2.0.tgz"
        alias.write_bytes(alias.read_bytes() + b"tamper")
        with self.assertRaisesRegex(IntegrityError, "widget-1.2.0.tgz sha256"):
            self.publish()
        self.assertEqual(self.registries.uploads, [])

    def test_verify_npm_checks_every_name(self) -> None:
        self.publish()
        identity, verified = verify_npm(
            self.config(self.root), registries=["npmjs"], env=ENV, packument_get=self.registries.get, sleep=lambda _s: None, log=lambda _l: None
        )
        self.assertEqual(identity.name, "@demo/widget")
        self.assertEqual(verified, ["@demo/widget@npmjs", "widget@npmjs"])
