"""`vlr publish-npm` / `vlr verify-npm`: idempotent, integrity-checked npm publication.

The same contract as APT publication, per `[[publish.npm]]` registry, against the live packument:
  version absent                         -> upload
  present with identical dist.integrity  -> skip (pipeline re-run)
  present with a different integrity     -> refuse (a published version is immutable)
  packument unreadable                   -> refuse (never guess)
Every package (the canonical name and each `[[npm.aliases]]` name) is planned on every registry
before anything is uploaded anywhere, so a conflict on one cannot leave the release half-published.
Aliases are uploaded before the canonical package: when an alias cannot be published (for example
its trusted publisher is not configured yet), the run fails before the canonical package moves, and
a re-run skips whatever is already there with identical bytes. After uploading, each packument is
polled until it lists the version with the expected integrity and the dist-tag points at it.

Uploads go through the npm CLI (`npm publish <tarball>`), which publishes the validated tarball
byte for byte and handles npm's auth schemes, including OIDC trusted publishing. Credentials reach
npm through a private, temporary userconfig file (mode 0600, deleted afterwards), never argv.

Auth per registry (`auth` in [[publish.npm]], required):
  oidc    npm trusted publishing from GitHub Actions (`id-token: write`, npm >= 11.5.1); no secret.
          The normal choice for npmjs.com.
  token   bearer token from `token_env` (default NPM_TOKEN)
  basic   username/password from `username_env`/`password_env` (both required), e.g. a private registry
Publication is gated by RELEASE_PUBLISH_MODE: `disabled` (default) skips it; `enabled` publishes
(`nexus`, which APT workflows set, also publishes).
"""

from __future__ import annotations

import base64
import json
import math
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from vlrelease.apt_index import DEFAULT_HTTP_HEADERS, redact_url
from vlrelease.checksums import SHA256SUMS_NAME, read_sha256sums
from vlrelease.config import Config, NpmRegistryConfig
from vlrelease.errors import ConfigError, IntegrityError, ReleaseError
from vlrelease.npmpkg import NpmIdentity, collect_npm_identities
from vlrelease.semver import SEMVER_PATTERN, Version

# `nexus` is accepted because repositories that also publish to APT set RELEASE_PUBLISH_MODE=nexus;
# for npm it simply means "publish".
PUBLISH_MODES = ("disabled", "enabled", "nexus")
ACTION_UPLOAD = "upload"
ACTION_SKIP = "skip-identical"
MIN_NPM_FOR_OIDC = (11, 5, 1)


class PackumentNotFound(Exception):
    """The registry has never seen this package (HTTP 404)."""


# (url, headers) -> parsed packument; raises PackumentNotFound for 404, ValueError otherwise.
PackumentGet = Callable[[str, Mapping[str, str]], dict]
# (identity, registry settings, dist_tag, access) -> None
Uploader = Callable[["NpmIdentity", "RegistrySettings", str, str], None]


@dataclass(frozen=True)
class RegistrySettings:
    name: str
    url: str  # normalized, with a trailing slash
    auth: str
    username: str = ""
    password: str = ""
    token: str = ""
    provenance: bool = False
    verify_timeout: float = 300.0
    verify_interval: float = 10.0

    def read_headers(self) -> dict[str, str]:
        """Headers for reading the packument (registries may require auth to read)."""
        if self.auth == "basic" and self.username:
            raw = base64.b64encode(f"{self.username}:{self.password}".encode()).decode("ascii")
            return {"Authorization": f"Basic {raw}"}
        if self.auth == "token" and self.token:
            return {"Authorization": f"Bearer {self.token}"}
        return {}


@dataclass(frozen=True)
class NpmPublishPlan:
    registry: str
    action: str
    reason: str
    dist_tag: str | None  # the tag an upload sets; None when nothing is uploaded
    package: str = ""


@dataclass
class NpmPublishResult:
    mode: str
    dry_run: bool
    identity: NpmIdentity | None = None  # the canonical package
    identities: list[NpmIdentity] = field(default_factory=list)  # canonical first, then aliases
    plans: list[NpmPublishPlan] = field(default_factory=list)
    verified: list[str] = field(default_factory=list)  # registry names; "<package>@<registry>" with aliases
    verified_pairs: set[tuple[str, str]] = field(default_factory=set)  # (package, registry)
    skipped_reason: str | None = None

    def _plan_dict(self, plan: NpmPublishPlan) -> dict:
        return {
            "registry": plan.registry,
            "action": plan.action,
            "reason": plan.reason,
            "dist_tag": plan.dist_tag,
            "verified": (plan.package, plan.registry) in self.verified_pairs,
        }

    def as_dict(self) -> dict:
        canonical = self.identity.name if self.identity else None
        return {
            "mode": self.mode,
            "dry_run": self.dry_run,
            "skipped_reason": self.skipped_reason,
            "package": canonical,
            "version": self.identity.version if self.identity else None,
            "file": self.identity.path.name if self.identity else None,
            "sha256": self.identity.sha256 if self.identity else None,
            "integrity": self.identity.integrity if self.identity else None,
            "registries": [self._plan_dict(plan) for plan in self.plans if plan.package == canonical],
            "packages": [
                {
                    "package": identity.name,
                    "file": identity.path.name,
                    "sha256": identity.sha256,
                    "integrity": identity.integrity,
                    "registries": [self._plan_dict(plan) for plan in self.plans if plan.package == identity.name],
                }
                for identity in self.identities
            ],
        }


# --- settings -------------------------------------------------------------------------------------


def resolve_mode(mode: str | None, env: Mapping[str, str]) -> str:
    resolved = (mode or env.get("RELEASE_PUBLISH_MODE") or "disabled").strip().lower()
    if resolved not in PUBLISH_MODES:
        raise ConfigError(f"Unsupported publication mode {resolved!r}; expected one of {', '.join(PUBLISH_MODES)}")
    return resolved


def _normalize_registry_url(url: str, where: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigError(f"{where} must be an absolute http(s) URL, got {redact_url(url)!r}")
    if parsed.username or parsed.password:
        raise ConfigError(f"{where} must not embed credentials; set them through the environment")
    return url if url.endswith("/") else url + "/"


def resolve_registry(
    registry: NpmRegistryConfig, env: Mapping[str, str], *, require_credentials: bool
) -> RegistrySettings:
    where = f"[[publish.npm]] {registry.name}"
    url = (env.get(registry.registry_env) or "").strip() if registry.registry_env else ""
    url = url or registry.registry
    if not url:
        raise ConfigError(f"{where}: the registry URL is not set ({registry.registry_env} is empty)")
    url = _normalize_registry_url(url, f"{where} registry")
    username = (env.get(registry.username_env) or "").strip() if registry.username_env else ""
    password = (env.get(registry.password_env) or "") if registry.password_env else ""
    token = (env.get(registry.token_env) or "").strip()
    if require_credentials:
        if registry.auth == "basic" and not (username and password):
            raise ConfigError(f"{where}: auth = basic needs {registry.username_env} and {registry.password_env}")
        if registry.auth == "token" and not token:
            raise ConfigError(f"{where}: auth = token needs {registry.token_env}")
        if registry.auth == "oidc" and not (env.get("ACTIONS_ID_TOKEN_REQUEST_URL") and env.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")):
            raise ConfigError(
                f"{where}: auth = oidc (npm trusted publishing) runs only in GitHub Actions with "
                "`permissions: id-token: write`, and the package must trust this workflow on the registry"
            )
    return RegistrySettings(
        name=registry.name,
        url=url,
        auth=registry.auth,
        username=username if registry.auth == "basic" else "",
        password=password if registry.auth == "basic" else "",
        token=token if registry.auth == "token" else "",
        provenance=registry.provenance,
        verify_timeout=registry.verify_timeout,
        verify_interval=registry.verify_interval,
    )


def select_registries(config: Config, names: list[str] | None) -> tuple[NpmRegistryConfig, ...]:
    if not config.npm_registries:
        raise ConfigError("No [[publish.npm]] registries are configured in release.toml")
    if not names:
        return config.npm_registries
    known = {registry.name: registry for registry in config.npm_registries}
    unknown = [name for name in names if name not in known]
    if unknown:
        raise ConfigError(f"Unknown npm registry {', '.join(unknown)}; configured: {', '.join(known)}")
    return tuple(known[name] for name in names)


# --- registry reads -------------------------------------------------------------------------------


def packument_url(registry_url: str, package: str) -> str:
    """`@scope/name` is requested as `@scope%2fname`, which every npm registry accepts."""
    return registry_url + quote(package, safe="@")


def default_packument_get(url: str, headers: Mapping[str, str]) -> dict:
    request_headers = dict(DEFAULT_HTTP_HEADERS)
    request_headers["Accept"] = "application/json"
    request_headers.update(dict(headers))
    try:
        with urlopen(Request(url, headers=request_headers), timeout=30) as response:
            payload = response.read()
    except HTTPError as exc:
        if exc.code == 404:
            raise PackumentNotFound(url) from exc
        raise ValueError(f"HTTP {exc.code}") from exc
    except URLError as exc:
        raise ValueError(str(exc.reason)) from exc
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"not a JSON packument: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("not a JSON packument")
    return data


def load_packument(settings: RegistrySettings, package: str, get: PackumentGet) -> dict | None:
    """The packument, or None when the registry has never seen the package."""
    try:
        return get(packument_url(settings.url, package), settings.read_headers())
    except PackumentNotFound:
        return None
    except ValueError as exc:
        raise ReleaseError(
            f"Cannot read {package} from npm registry {settings.name} ({settings.url}): {exc}. "
            "Refusing to publish without knowing what is already there."
        ) from exc


def _published_digests(packument: dict, version: str) -> tuple[bool, str | None, str | None]:
    versions = packument.get("versions")
    entry = versions.get(version) if isinstance(versions, dict) else None
    if not isinstance(entry, dict):
        return False, None, None
    dist = entry.get("dist") if isinstance(entry.get("dist"), dict) else {}
    integrity = dist.get("integrity") if isinstance(dist.get("integrity"), str) else None
    shasum = dist.get("shasum") if isinstance(dist.get("shasum"), str) else None
    return True, integrity, shasum.lower() if shasum else None


def _matches(identity: NpmIdentity, integrity: str | None, shasum: str | None) -> bool | None:
    """True/False when provable from the registry's digests, None when it lists none we can compare."""
    if integrity and integrity.startswith("sha512-"):
        return integrity == identity.integrity
    if shasum:
        return shasum == identity.shasum
    return None


def _dist_tag_version(packument: dict | None, tag: str) -> str | None:
    tags = packument.get("dist-tags") if isinstance(packument, dict) else None
    value = tags.get(tag) if isinstance(tags, dict) else None
    return value if isinstance(value, str) else None


def _older_than(version: str, other: str | None) -> bool:
    if other is None or not SEMVER_PATTERN.fullmatch(other):
        return False
    return Version.parse(version) < Version.parse(other)


def plan_registry(
    identity: NpmIdentity,
    settings: RegistrySettings,
    packument: dict | None,
    *,
    dist_tag: str,
    maintenance_dist_tag: str,
    allow_older_version: bool,
) -> NpmPublishPlan:
    published, integrity, shasum = _published_digests(packument or {}, identity.version)
    current = _dist_tag_version(packument, dist_tag)
    if published:
        verdict = _matches(identity, integrity, shasum)
        if verdict is None:
            raise IntegrityError(
                f"{identity.name}@{identity.version} is already on npm registry {settings.name}, but its packument "
                "carries no dist.integrity or dist.shasum, so identical bytes cannot be proven. Refusing to re-upload."
            )
        if not verdict:
            raise IntegrityError(
                f"REFUSING TO OVERWRITE A PUBLISHED VERSION: {identity.name}@{identity.version} is already on npm "
                f"registry {settings.name} with {integrity or 'sha1 ' + str(shasum)}, but {identity.path.name} is "
                f"{identity.integrity}. Published versions are immutable: release a new version instead."
            )
        return NpmPublishPlan(settings.name, ACTION_SKIP, "already published with identical integrity", None, identity.name)
    if _older_than(identity.version, current):
        if not allow_older_version:
            raise IntegrityError(
                f"Refusing to publish {identity.name}@{identity.version} to {settings.name}: its `{dist_tag}` dist-tag "
                f"is the newer {current}. Pass --allow-older-version for a deliberate maintenance release; it is then "
                f"published under `{maintenance_dist_tag}` so `{dist_tag}` does not move backwards."
            )
        return NpmPublishPlan(
            settings.name, ACTION_UPLOAD, f"version not yet published (older than {current})", maintenance_dist_tag, identity.name
        )
    reason = "version not yet published" if packument is not None else "new package: not on this registry yet"
    return NpmPublishPlan(settings.name, ACTION_UPLOAD, reason, dist_tag, identity.name)


# --- upload ---------------------------------------------------------------------------------------


def nerf_dart(registry_url: str) -> str:
    """npm's per-registry config key prefix: `//host[:port]/path/`."""
    parsed = urlparse(registry_url)
    path = parsed.path if parsed.path.endswith("/") else parsed.path + "/"
    return f"//{parsed.netloc}{path}"


def render_userconfig(settings: RegistrySettings) -> str:
    """The temporary .npmrc for one upload: only the auth line(s) for this registry."""
    prefix = nerf_dart(settings.url)
    if settings.auth == "basic":
        raw = base64.b64encode(f"{settings.username}:{settings.password}".encode()).decode("ascii")
        return f"{prefix}:_auth={raw}\n"
    if settings.auth == "token":
        return f"{prefix}:_authToken={settings.token}\n"
    return ""


def publish_command(identity: NpmIdentity, settings: RegistrySettings, dist_tag: str, access: str) -> list[str]:
    command = [
        "npm",
        "publish",
        str(identity.path.resolve()),
        "--registry",
        settings.url,
        "--tag",
        dist_tag,
        "--access",
        access,
        "--ignore-scripts",
    ]
    if settings.provenance:
        command.append("--provenance")
    return command


def _npm_version(runner) -> tuple[int, int, int] | None:
    try:
        completed = runner(["npm", "--version"], capture_output=True, text=True, check=False, timeout=60)
    except FileNotFoundError:
        return None
    match = SEMVER_PATTERN.search((completed.stdout or "").strip())
    return tuple(int(part) for part in match.groups()) if match else None  # type: ignore[return-value]


def publish_with_npm(
    identity: NpmIdentity,
    settings: RegistrySettings,
    dist_tag: str,
    access: str,
    *,
    runner=subprocess.run,
    env: Mapping[str, str] | None = None,
) -> None:
    version = _npm_version(runner)
    if version is None:
        raise ReleaseError("`npm` is required for npm publication but is not on PATH.")
    if settings.auth == "oidc" and version < MIN_NPM_FOR_OIDC:
        raise ReleaseError(
            f"npm trusted publishing (auth = oidc) needs npm >= {'.'.join(map(str, MIN_NPM_FOR_OIDC))}; "
            f"found {'.'.join(map(str, version))}. Run `npm install -g npm@latest` first."
        )
    base_env = dict(os.environ if env is None else env)
    with tempfile.TemporaryDirectory(prefix="vlr-npm-") as scratch:
        userconfig = Path(scratch) / "npmrc"
        descriptor = os.open(userconfig, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(render_userconfig(settings))
        child_env = {
            key: value
            for key, value in base_env.items()
            # A stray npm_config_* (e.g. from a surrounding `npm run`) must not redirect or re-auth the upload.
            if not key.lower().startswith("npm_config_")
        }
        child_env.update(
            NPM_CONFIG_USERCONFIG=str(userconfig),
            NPM_CONFIG_GLOBALCONFIG=str(Path(scratch) / "npmrc-global"),
            NPM_CONFIG_UPDATE_NOTIFIER="false",
            NPM_CONFIG_FUND="false",
            NPM_CONFIG_AUDIT="false",
        )
        command = publish_command(identity, settings, dist_tag, access)
        try:
            # cwd is the scratch directory so no project .npmrc can influence the upload.
            completed = runner(command, cwd=scratch, env=child_env, capture_output=True, text=True, check=False, timeout=1200)
        except FileNotFoundError as exc:
            raise ReleaseError("`npm` is required for npm publication but is not on PATH.") from exc
    if completed.returncode != 0:
        output = "\n".join(
            line for line in ((completed.stderr or "") + "\n" + (completed.stdout or "")).splitlines() if line.strip()
        )
        tail = "\n".join(output.splitlines()[-20:])
        for secret in (settings.password, settings.token):
            if secret:
                tail = tail.replace(secret, "<redacted>")
        raise ReleaseError(
            f"npm publish of {identity.path.name} to {settings.name} ({settings.url}) failed "
            f"(exit {completed.returncode}).\n{tail}"
        )


# --- verification ---------------------------------------------------------------------------------


def verify_registry(
    identity: NpmIdentity,
    settings: RegistrySettings,
    *,
    dist_tag: str | None,
    get: PackumentGet,
    timeout: float,
    interval: float,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> None:
    """Poll until the packument lists the version with the expected integrity (and `dist_tag`, when given, points at it)."""
    attempts = max(1, math.ceil(timeout / interval) + 1) if timeout > 0 else 1
    last_problem = "no attempt made"
    for attempt in range(1, attempts + 1):
        try:
            packument = get(packument_url(settings.url, identity.name), settings.read_headers())
        except PackumentNotFound:
            last_problem = "package not found yet"
        except ValueError as exc:
            last_problem = f"packument unreadable: {exc}"
        else:
            published, integrity, shasum = _published_digests(packument, identity.version)
            if published:
                verdict = _matches(identity, integrity, shasum)
                if verdict is False:
                    raise IntegrityError(
                        f"npm registry {settings.name} lists {identity.name}@{identity.version} with "
                        f"{integrity or 'sha1 ' + str(shasum)}, expected {identity.integrity} ({identity.path.name})."
                    )
                tagged = _dist_tag_version(packument, dist_tag) if dist_tag else identity.version
                if verdict and tagged == identity.version:
                    suffix = f" ({dist_tag})" if dist_tag else ""
                    log(f"verified on {settings.name}: {identity.name}@{identity.version} {identity.integrity}{suffix}")
                    return
                last_problem = (
                    "no comparable digest in the packument yet"
                    if verdict is None
                    else f"dist-tag {dist_tag} is {tagged or 'unset'}, not {identity.version}"
                )
            else:
                last_problem = f"{identity.version} not listed yet"
        log(f"{settings.name} verification attempt {attempt}/{attempts}: {last_problem}")
        if attempt < attempts:
            sleep(interval)
    raise ReleaseError(
        f"npm registry {settings.name} did not confirm {identity.name}@{identity.version} within {timeout:g}s: {last_problem}"
    )


# --- commands -------------------------------------------------------------------------------------


def require_checksummed(config: Config, identity: NpmIdentity) -> None:
    sums = config.output_dir / SHA256SUMS_NAME
    if not sums.is_file():
        raise IntegrityError(f"{SHA256SUMS_NAME} is missing under {config.output_dir}; refusing to publish unverified artifacts.")
    expected = read_sha256sums(sums).get(identity.path.name)
    if expected is None:
        raise IntegrityError(f"{identity.path.name} is not listed in {SHA256SUMS_NAME}.")
    if expected != identity.sha256:
        raise IntegrityError(
            f"{identity.path.name} sha256 {identity.sha256} does not match {SHA256SUMS_NAME} ({expected}); "
            "the artifact changed after it was built and validated."
        )


def publish_npm(
    config: Config,
    *,
    registries: list[str] | None = None,
    mode: str | None = None,
    dry_run: bool = False,
    require_enabled: bool = False,
    allow_older_version: bool = False,
    verify_timeout: float | None = None,
    env: Mapping[str, str] | None = None,
    uploader: Uploader | None = None,
    packument_get: PackumentGet | None = None,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> NpmPublishResult:
    environment = os.environ if env is None else env
    if config.npm is None:
        raise ConfigError("[npm] is not enabled in release.toml")
    selected = select_registries(config, registries)
    resolved_mode = resolve_mode(mode, environment)
    if resolved_mode == "disabled":
        if require_enabled:
            raise ReleaseError("Publication is required for this run, but RELEASE_PUBLISH_MODE is `disabled`.")
        return NpmPublishResult(mode="disabled", dry_run=dry_run, skipped_reason="publication mode is disabled")

    # A dry run only reads the registries, so it needs no upload credentials (reads still use them when set).
    settings = [resolve_registry(registry, environment, require_credentials=not dry_run) for registry in selected]
    identities = collect_npm_identities(config)
    for identity in identities:
        require_checksummed(config, identity)
    get = packument_get or default_packument_get

    # Plan every package on every registry before uploading anywhere.
    steps: list[tuple[NpmIdentity, RegistrySettings, NpmPublishPlan]] = []
    for identity in identities:
        for registry in settings:
            plan = plan_registry(
                identity,
                registry,
                load_packument(registry, identity.name, get),
                dist_tag=config.npm.dist_tag,
                maintenance_dist_tag=config.npm.maintenance_dist_tag,
                allow_older_version=allow_older_version,
            )
            tag_note = f" under `{plan.dist_tag}`" if plan.dist_tag else ""
            log(f"{identity.path.name} -> {registry.name} ({registry.url}): {plan.action}{tag_note} ({plan.reason})")
            steps.append((identity, registry, plan))
    result = NpmPublishResult(
        mode=resolved_mode, dry_run=dry_run, identity=identities[0], identities=identities, plans=[step[2] for step in steps]
    )
    if dry_run:
        return result

    upload = uploader or (lambda ident, reg, tag, access: publish_with_npm(ident, reg, tag, access, env=environment))
    canonical = identities[0].name
    # Aliases first: an alias that cannot be published stops the run before the canonical package moves.
    for identity, registry, plan in sorted(steps, key=lambda step: step[0].name == canonical):
        if plan.action == ACTION_UPLOAD and plan.dist_tag:
            log(f"publishing {identity.name}@{identity.version} to {registry.name} under `{plan.dist_tag}`")
            upload(identity, registry, plan.dist_tag, config.npm.access)
        verify_registry(
            identity,
            registry,
            dist_tag=plan.dist_tag,
            get=get,
            timeout=registry.verify_timeout if verify_timeout is None else verify_timeout,
            interval=registry.verify_interval,
            sleep=sleep,
            log=log,
        )
        result.verified_pairs.add((identity.name, registry.name))
        result.verified.append(registry.name if len(identities) == 1 else f"{identity.name}@{registry.name}")
    return result


def verify_npm(
    config: Config,
    *,
    registries: list[str] | None = None,
    timeout: float | None = None,
    env: Mapping[str, str] | None = None,
    packument_get: PackumentGet | None = None,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> tuple[NpmIdentity, list[str]]:
    environment = os.environ if env is None else env
    if config.npm is None:
        raise ConfigError("[npm] is not enabled in release.toml")
    identities = collect_npm_identities(config)
    get = packument_get or default_packument_get
    verified: list[str] = []
    for registry in select_registries(config, registries):
        settings = resolve_registry(registry, environment, require_credentials=False)
        for identity in identities:
            packument = load_packument(settings, identity.name, get)
            # The dist-tag is only awaited when this version should be (or become) its target.
            tag: str | None = config.npm.dist_tag
            if _older_than(identity.version, _dist_tag_version(packument, config.npm.dist_tag)):
                tag = None
            verify_registry(
                identity,
                settings,
                dist_tag=tag,
                get=get,
                timeout=settings.verify_timeout if timeout is None else timeout,
                interval=settings.verify_interval,
                sleep=sleep,
                log=log,
            )
            verified.append(settings.name if len(identities) == 1 else f"{identity.name}@{settings.name}")
    return identities[0], verified

