"""Immutable same-origin web assets for the canonical authenticated Host application.

This module does not own authentication, financial commands, sessions, or provider
authority. It only makes a release-manifest-bound static web bundle available on
the same origin as AuthenticatedHostApplication so the canonical web client
can keep using relative /api/v1/* routes without CORS or a second server.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from types import MappingProxyType
from typing import Iterable, Mapping
from urllib.parse import urlsplit

from .host_network import AuthenticatedHostApplication, TransportResponse


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_CONTRACT_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){2}$")
_ALLOWED_MEDIA_TYPES = MappingProxyType(
    {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
    }
)
_CSP = (
    "default-src 'none'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self'; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'; "
    "form-action 'self'"
)


class EmbeddedWebError(ValueError):
    """Raised when immutable web-bundle authority is malformed."""


def _canonical_asset_path(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise EmbeddedWebError("web asset path must be exact non-empty text")
    if (
        value.startswith("/")
        or "\\" in value
        or "%" in value
        or "?" in value
        or "#" in value
        or "\x00" in value
    ):
        raise EmbeddedWebError("web asset path is not canonical")
    parts = value.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise EmbeddedWebError("web asset path contains an invalid segment")
    if parts[0].lower() == "api":
        raise EmbeddedWebError("web asset path collides with Host API namespace")
    suffix = "." + parts[-1].rsplit(".", 1)[-1].lower() if "." in parts[-1] else ""
    if suffix not in _ALLOWED_MEDIA_TYPES:
        raise EmbeddedWebError("web asset extension is not allowlisted")
    return value


def _canonical_header_map(headers: Mapping[str, str]) -> Mapping[str, str]:
    if not isinstance(headers, Mapping):
        raise EmbeddedWebError("request headers must be a mapping")
    normalized: dict[str, str] = {}
    for raw_name, raw_value in headers.items():
        name = str(raw_name).strip().lower()
        if not name or name in normalized:
            raise EmbeddedWebError("duplicate or invalid request header")
        normalized[name] = str(raw_value).strip()
    return MappingProxyType(normalized)


def _bundle_digest(
    *,
    source_revision: str,
    host_api_contract_version: str,
    assets: tuple["ImmutableWebAsset", ...],
) -> str:
    manifest = {
        "schema": "autotrade-embedded-web-bundle:v1",
        "source_revision": source_revision,
        "host_api_contract_version": host_api_contract_version,
        "assets": [
            {
                "path": item.path,
                "media_type": item.media_type,
                "sha256": item.sha256_hex,
            }
            for item in assets
        ],
    }
    encoded = json.dumps(
        manifest,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ImmutableWebAsset:
    path: str
    media_type: str
    body: bytes
    sha256_hex: str

    def __post_init__(self) -> None:
        path = _canonical_asset_path(self.path)
        if type(self.body) is not bytes or not self.body:
            raise EmbeddedWebError("web asset body must be exact non-empty bytes")
        if not isinstance(self.sha256_hex, str) or _SHA256.fullmatch(self.sha256_hex) is None:
            raise EmbeddedWebError("web asset SHA-256 must be lowercase hexadecimal")
        if sha256(self.body).hexdigest() != self.sha256_hex:
            raise EmbeddedWebError("web asset SHA-256 does not match exact bytes")
        suffix = "." + path.rsplit(".", 1)[-1].lower()
        if self.media_type != _ALLOWED_MEDIA_TYPES[suffix]:
            raise EmbeddedWebError("web asset media type does not match canonical path")
        object.__setattr__(self, "path", path)


@dataclass(frozen=True)
class ImmutableWebAssetBundle:
    source_revision: str
    host_api_contract_version: str
    assets: tuple[ImmutableWebAsset, ...]
    bundle_sha256: str

    @classmethod
    def build(
        cls,
        *,
        source_revision: str,
        host_api_contract_version: str,
        assets: Iterable[ImmutableWebAsset],
    ) -> "ImmutableWebAssetBundle":
        if not isinstance(source_revision, str) or _GIT_SHA1.fullmatch(source_revision) is None:
            raise EmbeddedWebError("web bundle source revision must be an exact Git SHA-1")
        if (
            not isinstance(host_api_contract_version, str)
            or _CONTRACT_VERSION.fullmatch(host_api_contract_version) is None
        ):
            raise EmbeddedWebError("Host API contract version is not canonical")
        values = tuple(assets)
        if not values or any(type(item) is not ImmutableWebAsset for item in values):
            raise EmbeddedWebError("web bundle assets must be exact ImmutableWebAsset values")
        ordered = tuple(sorted(values, key=lambda item: item.path))
        paths = [item.path for item in ordered]
        if len(paths) != len(set(paths)):
            raise EmbeddedWebError("web bundle contains duplicate asset paths")
        if "index.html" not in paths:
            raise EmbeddedWebError("web bundle must contain index.html")
        return cls(
            source_revision=source_revision,
            host_api_contract_version=host_api_contract_version,
            assets=ordered,
            bundle_sha256=_bundle_digest(
                source_revision=source_revision,
                host_api_contract_version=host_api_contract_version,
                assets=ordered,
            ),
        )

    def __post_init__(self) -> None:
        if not isinstance(self.source_revision, str) or _GIT_SHA1.fullmatch(self.source_revision) is None:
            raise EmbeddedWebError("web bundle source revision must be an exact Git SHA-1")
        if (
            not isinstance(self.host_api_contract_version, str)
            or _CONTRACT_VERSION.fullmatch(self.host_api_contract_version) is None
        ):
            raise EmbeddedWebError("Host API contract version is not canonical")
        if (
            type(self.assets) is not tuple
            or not self.assets
            or any(type(item) is not ImmutableWebAsset for item in self.assets)
        ):
            raise EmbeddedWebError("web bundle assets must be an immutable exact tuple")
        if tuple(sorted(self.assets, key=lambda item: item.path)) != self.assets:
            raise EmbeddedWebError("web bundle assets must be deterministically sorted")
        if len({item.path for item in self.assets}) != len(self.assets):
            raise EmbeddedWebError("web bundle contains duplicate asset paths")
        if "index.html" not in {item.path for item in self.assets}:
            raise EmbeddedWebError("web bundle must contain index.html")
        if not isinstance(self.bundle_sha256, str) or _SHA256.fullmatch(self.bundle_sha256) is None:
            raise EmbeddedWebError("web bundle SHA-256 must be lowercase hexadecimal")
        expected = _bundle_digest(
            source_revision=self.source_revision,
            host_api_contract_version=self.host_api_contract_version,
            assets=self.assets,
        )
        if self.bundle_sha256 != expected:
            raise EmbeddedWebError("web bundle identity conflicts with exact manifest")

    @property
    def by_path(self) -> Mapping[str, ImmutableWebAsset]:
        return MappingProxyType({item.path: item for item in self.assets})

    def asset_for_request_path(self, path: str) -> ImmutableWebAsset | None:
        if path == "/":
            path = "/index.html"
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
            return None
        relative = path[1:]
        if not relative or "%" in relative or "\\" in relative:
            return None
        return self.by_path.get(relative)


class EmbeddedWebHostApplication(AuthenticatedHostApplication):
    """Canonical Host application plus immutable same-origin release web assets."""

    def __init__(self, *args, web_bundle: ImmutableWebAssetBundle, **kwargs) -> None:
        if type(web_bundle) is not ImmutableWebAssetBundle:
            raise TypeError("web_bundle must be exact ImmutableWebAssetBundle")
        super().__init__(*args, **kwargs)
        self._web_bundle = web_bundle

    @property
    def web_bundle(self) -> ImmutableWebAssetBundle:
        return self._web_bundle

    def _dispatch_host(
        self,
        *,
        method: str,
        target: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> TransportResponse:
        return super().dispatch(
            method=method,
            target=target,
            headers=headers,
            body=body,
        )

    def dispatch(
        self,
        *,
        method: str,
        target: str,
        headers: Mapping[str, str],
        body: bytes = b"",
    ) -> TransportResponse:
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            return self._dispatch_host(
                method=method,
                target=target,
                headers=headers,
                body=body,
            )
        asset = self._web_bundle.asset_for_request_path(parsed.path)
        if asset is None:
            return self._dispatch_host(
                method=method,
                target=target,
                headers=headers,
                body=body,
            )

        try:
            normalized_headers = _canonical_header_map(headers)
        except EmbeddedWebError:
            return _static_error(400, "INVALID_STATIC_REQUEST")

        if parsed.query:
            return _static_error(400, "STATIC_QUERY_FORBIDDEN")
        if body:
            return _static_error(400, "STATIC_BODY_FORBIDDEN")
        if method != "GET":
            return _static_error(405, "STATIC_METHOD_FORBIDDEN")
        if "authorization" in normalized_headers or "x-autotrade-actor" in normalized_headers:
            return _static_error(400, "STATIC_CREDENTIAL_HEADER_FORBIDDEN")
        supplied_origin = normalized_headers.get("origin")
        if supplied_origin is not None and supplied_origin != self.public_origin:
            return _static_error(403, "STATIC_ORIGIN_FORBIDDEN")

        return TransportResponse(
            status=200,
            content_type=asset.media_type,
            body=asset.body,
            headers=(
                ("Cache-Control", "no-store"),
                ("Content-Security-Policy", _CSP),
                ("Cross-Origin-Resource-Policy", "same-origin"),
                ("Referrer-Policy", "no-referrer"),
                ("X-Frame-Options", "DENY"),
                ("ETag", '"' + asset.sha256_hex + '"'),
                ("X-AutoTrade-UI-Bundle-SHA256", self._web_bundle.bundle_sha256),
                ("X-AutoTrade-UI-Source-Revision", self._web_bundle.source_revision),
                (
                    "X-AutoTrade-Host-API-Contract-Version",
                    self._web_bundle.host_api_contract_version,
                ),
            ),
        )


def _static_error(status: int, code: str) -> TransportResponse:
    payload = json.dumps(
        {"error": code},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return TransportResponse(
        status=status,
        content_type="application/json; charset=utf-8",
        body=payload,
        headers=(("Cache-Control", "no-store"),),
    )
