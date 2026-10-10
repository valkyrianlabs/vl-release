"""Read-only access to a published APT repository's `Packages` indexes.

Publication uses this to decide, per artifact, whether an upload is needed (version absent),
redundant (same version, same SHA256), or forbidden (same version, different bytes), and to
verify the upload by checksum afterwards. Nexus accepts re-uploads of an existing version, so
this check is the only thing standing between a pipeline re-run and a silently replaced package.

Ported from vaulthalla's tools/release/packaging/apt_index.py.
"""

from __future__ import annotations

import base64
import gzip
import re
from dataclasses import dataclass, field
from typing import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen

from vlrelease import __version__
from vlrelease.debversion import compare_debian_versions  # noqa: F401 (re-exported: dpkg ordering lives there)

HttpGet = Callable[[str, Mapping[str, str]], bytes]

DEFAULT_HTTP_HEADERS: dict[str, str] = {
    "User-Agent": f"vl-release/{__version__} (+https://github.com/valkyrianlabs/vl-release)",
    "Accept": "*/*",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


@dataclass(frozen=True)
class AptIndexConfig:
    repository_url: str
    suite: str = "stable"
    components: tuple[str, ...] = ("main",)
    architectures: tuple[str, ...] = ("amd64",)
    username: str | None = None
    password: str | None = None


@dataclass(frozen=True)
class AptPackageEntry:
    package: str
    version: str
    architecture: str
    sha256: str | None
    filename: str | None = None
    size: int | None = None


@dataclass
class AptIndex:
    entries: list[AptPackageEntry] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    def lookup(self, package: str, version: str, architecture: str) -> list[AptPackageEntry]:
        return [
            entry
            for entry in self.entries
            if entry.package == package
            and _strip_epoch(entry.version) == _strip_epoch(version)
            and entry.architecture in {architecture, "all"}
        ]

    def versions(self, package: str) -> set[str]:
        return {entry.version for entry in self.entries if entry.package == package}

    def newest_version(self, package: str) -> str | None:
        newest: str | None = None
        for version in self.versions(package):
            if newest is None or compare_debian_versions(version, newest) > 0:
                newest = version
        return newest


def package_index_urls(config: AptIndexConfig) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Return ((label, (Packages.gz url, Packages url)), ...) per component/architecture."""
    base = config.repository_url.rstrip("/")
    if base.endswith("/Packages") or base.endswith("/Packages.gz"):
        return ((base, (base,)),)
    groups: list[tuple[str, tuple[str, ...]]] = []
    for component in config.components:
        for arch in config.architectures:
            index_base = f"{base}/dists/{config.suite}/{component}/binary-{arch}/Packages"
            groups.append((f"{config.suite}/{component}/binary-{arch}", (f"{index_base}.gz", index_base)))
    return tuple(groups)


def load_apt_index(config: AptIndexConfig, *, http_get: HttpGet | None = None) -> AptIndex:
    """Fetch every configured Packages index. Fails closed if any component/arch is unreadable."""
    getter = default_http_get if http_get is None else http_get
    headers = _auth_headers(config)
    index = AptIndex()
    unreadable: list[str] = []
    for label, urls in package_index_urls(config):
        errors: list[str] = []
        loaded = False
        for url in urls:
            try:
                content = getter(url, headers)
                if url.endswith(".gz"):
                    content = gzip.decompress(content)
            except Exception as exc:
                errors.append(f"{redact_url(url)} ({exc})")
                continue
            index.entries.extend(parse_packages_index(content.decode("utf-8", errors="replace")))
            index.sources.append(redact_url(url) or url)
            loaded = True
            break
        if not loaded:
            unreadable.append(f"{label}: " + "; ".join(errors))
    if unreadable:
        raise ValueError(
            "Unable to read APT Packages index (refusing to guess whether the version is already published): "
            + " | ".join(unreadable)
        )
    return index


def release_file_urls(config: AptIndexConfig) -> tuple[str, ...]:
    """The suite's InRelease and Release URLs (none when repository_url points straight at a Packages file)."""
    base = config.repository_url.rstrip("/")
    if base.endswith("/Packages") or base.endswith("/Packages.gz"):
        return ()
    return (f"{base}/dists/{config.suite}/InRelease", f"{base}/dists/{config.suite}/Release")


def request_index_refresh(config: AptIndexConfig, *, http_get: HttpGet | None = None) -> bool:
    """Request the suite's InRelease (falling back to Release) and discard it; True if one was served.

    Some repository managers rebuild the dists/ metadata when a client asks for the Release files rather than when a
    package is uploaded (Sonatype Nexus apt-hosted repositories do). A verifier that only polls the Packages indexes
    then waits for a rebuild nothing triggers: vaulthalla v1.9.0 sat unlisted for 20 minutes until the next
    `apt-get update` elsewhere asked for InRelease, and the index regenerated within seconds. Best effort: failures are
    ignored, since the Packages read that follows decides.
    """
    getter = default_http_get if http_get is None else http_get
    headers = _auth_headers(config)
    for url in release_file_urls(config):
        try:
            getter(url, headers)
            return True
        except Exception:
            continue
    return False


def parse_packages_index(content: str) -> list[AptPackageEntry]:
    entries: list[AptPackageEntry] = []
    for stanza in re.split(r"\n\s*\n", content):
        fields: dict[str, str] = {}
        current_key: str | None = None
        for line in stanza.splitlines():
            if not line.strip():
                continue
            if line.startswith((" ", "\t")) and current_key:
                fields[current_key] += "\n" + line.strip()
                continue
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            current_key = key.strip()
            fields[current_key] = value.strip()
        name = fields.get("Package")
        version = fields.get("Version")
        if not name or not version:
            continue
        size_raw = fields.get("Size")
        entries.append(
            AptPackageEntry(
                package=name,
                version=version,
                architecture=fields.get("Architecture", ""),
                sha256=(fields.get("SHA256") or "").lower() or None,
                filename=fields.get("Filename"),
                size=int(size_raw) if size_raw and size_raw.isdigit() else None,
            )
        )
    return entries


def default_http_get(url: str, headers: Mapping[str, str]) -> bytes:
    request_headers = dict(DEFAULT_HTTP_HEADERS)
    request_headers.update(dict(headers))
    request = Request(url, headers=request_headers)
    try:
        with urlopen(request, timeout=30) as response:
            return response.read()
    except HTTPError as exc:
        raise ValueError(f"HTTP {exc.code}") from exc
    except URLError as exc:
        raise ValueError(str(exc.reason)) from exc


def redact_url(url: str | None) -> str | None:
    if url is None:
        return None
    parsed = urlparse(url)
    if not parsed.username and not parsed.password:
        return url
    netloc = parsed.hostname or ""
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urlunparse((parsed.scheme, f"<redacted>@{netloc}", parsed.path, parsed.params, parsed.query, parsed.fragment))


def _auth_headers(config: AptIndexConfig) -> dict[str, str]:
    if not config.username or not config.password:
        return {}
    token = base64.b64encode(f"{config.username}:{config.password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def _strip_epoch(version: str) -> str:
    return version.split(":", 1)[1] if ":" in version else version
