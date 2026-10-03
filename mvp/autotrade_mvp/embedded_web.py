"""Immutable same-origin semantic web bundle composition for the canonical host.

This module is deliberately transport-only. It does not mint sessions, accept
financial commands, or create a second HTTP server. Static UI bytes share the
existing AuthenticatedHostApplication origin so the canonical web client can
continue to use relative /api/v1 routes without CORS or bearer material in JS.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
import re
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlsplit

from .host_network import AuthenticatedHostApplication, TransportResponse
from .security import _authenticated_origin


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_CONTRACT_VERSION_RE = re.compile(r"^[1-9][0-9]*\.[0-9]+\.[0-9]+$")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_ALLOWED_CONTENT_TYPES = MappingProxyType(
    {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
    }
)
_CSP = (
    "default-src 'self'; "
    "base-uri 'none'; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "form-action 'self'; "
    "connect-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self'"
)


def _canonical_asset_path(value: str) -> str:
    if type(value) is not str or not value:
        raise TypeError("web asset path must be a non-empty exact str")
    if (
        value.startswith("/")
        or "\\" in value
        or "%" in value
        or "?" in value
        or "#" in value
        or "\x00" in value
    ):
        raise ValueError("web asset path is not canonical")
    parts = value.split("/")
    if any(
        part in ("", ".", "..")
        or _SEGMENT_RE.fullmatch(part) is None
        for part in parts
    ):
        raise ValueError("web asset path contains an invalid segment")
    if parts[0].casefold() == "api":
        raise ValueError("web assets cannot occupy the Host API namespace")
    return value


def _content_type_for(path: str) -> str:
    for suffix, content_type in _ALLOWED_CONTENT_TYPES.items():
        if path.endswith(suffix):
            return content_type
    raise ValueError("web asset type is not allowlisted")


@dataclass(frozen=True, slots=True)
class ImmutableWebAsset:
    path: str
    body: bytes
    sha256_hex: str
    content_type: str

    def __post_init__(self) -> None:
        path = _canonical_asset_path(self.path)
        if type(self.body) is not bytes:
            raise TypeError("web asset body must be immutable bytes")
        if type(self.sha256_hex) is not str or _SHA256_RE.fullmatch(
            self.sha256_hex
        ) is None:
            raise ValueError("web asset sha256 must be canonical lowercase hex")
        if sha256(self.body).hexdigest() != self.sha256_hex:
            raise ValueError("web asset sha256 does not match immutable bytes")
        expected_content_type = _content_type_for(path)
        if type(self.content_type) is not str or self.content_type != expected_content_type:
            raise ValueError("web asset content type does not match its path")
        object.__setattr__(self, "path", path)


@dataclass(frozen=True, slots=True)
class ImmutableWebAssetBundle:
    source_revision: str
    host_api_contract_version: str
    assets: tuple[ImmutableWebAsset, ...]
    bundle_sha256: str = field(init=False)
    _routes: Mapping[str, ImmutableWebAsset] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if type(self.source_revision) is not str or _SOURCE_REVISION_RE.fullmatch(
            self.source_revision
        ) is None:
            raise ValueError("web bundle source revision must be lowercase 40-hex")
        if (
            type(self.host_api_contract_version) is not str
            or _CONTRACT_VERSION_RE.fullmatch(self.host_api_contract_version) is None
        ):
            raise ValueError("web bundle Host API contract version is not canonical")
        if type(self.assets) is not tuple or not self.assets:
            raise TypeError("web bundle assets must be a non-empty exact tuple")
        if any(type(asset) is not ImmutableWebAsset for asset in self.assets):
            raise TypeError("web bundle accepts only exact ImmutableWebAsset values")

        ordered = tuple(sorted(self.assets, key=lambda asset: asset.path))
        if len({asset.path for asset in ordered}) != len(ordered):
            raise ValueError("web bundle asset paths must be unique")
        by_path = {asset.path: asset for asset in ordered}
        if "index.html" not in by_path:
            raise ValueError("web bundle requires index.html")

        manifest = {
            "schema_version": "1",
            "source_revision": self.source_revision,
            "host_api_contract_version": self.host_api_contract_version,
            "assets": [
                {
                    "path": asset.path,
                    "sha256": asset.sha256_hex,
                    "content_type": asset.content_type,
                    "size": len(asset.body),
                }
                for asset in ordered
            ],
        }
        encoded = json.dumps(
            manifest,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        routes: dict[str, ImmutableWebAsset] = {
            "/" + asset.path: asset for asset in ordered
        }
        routes["/"] = by_path["index.html"]
        object.__setattr__(self, "assets", ordered)
        object.__setattr__(self, "bundle_sha256", sha256(encoded).hexdigest())
        object.__setattr__(self, "_routes", MappingProxyType(routes))

    def asset_for_path(self, path: str) -> ImmutableWebAsset | None:
        if type(path) is not str:
            return None
        return self._routes.get(path)


def _static_error(status: int, code: str) -> TransportResponse:
    body = json.dumps(
        {"error": code},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return TransportResponse(
        status=status,
        content_type="application/json; charset=utf-8",
        body=body,
        headers=(("Cache-Control", "no-store"),),
    )


class EmbeddedWebHostApplication(AuthenticatedHostApplication):
    """Compose immutable UI bytes into the existing authenticated Host origin."""

    def __init__(self, *args, web_bundle: ImmutableWebAssetBundle, **kwargs) -> None:
        if type(web_bundle) is not ImmutableWebAssetBundle:
            raise TypeError("web_bundle must be an exact ImmutableWebAssetBundle")
        super().__init__(*args, **kwargs)
        self.web_bundle = web_bundle

    @staticmethod
    def _casefold_header_values(
        headers: Mapping[str, str], name: str
    ) -> tuple[str, ...]:
        values: list[str] = []
        for key, value in headers.items():
            if type(key) is not str or type(value) is not str:
                raise ValueError("static request headers must be exact strings")
            if key.casefold() == name:
                values.append(value.strip())
        return tuple(values)

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
            return super().dispatch(
                method=method, target=target, headers=headers, body=body
            )

        asset = self.web_bundle.asset_for_path(parsed.path)
        if asset is None:
            return super().dispatch(
                method=method, target=target, headers=headers, body=body
            )

        try:
            if type(method) is not str or method != "GET":
                return _static_error(405, "STATIC_METHOD_NOT_ALLOWED")
            if type(body) is not bytes or body:
                return _static_error(400, "STATIC_BODY_FORBIDDEN")
            if parsed.query:
                return _static_error(400, "STATIC_QUERY_FORBIDDEN")

            origins = self._casefold_header_values(headers, "origin")
            if len(origins) > 1:
                return _static_error(400, "DUPLICATE_ORIGIN")
            if origins:
                if (
                    not origins[0]
                    or _authenticated_origin(origins[0]) != self.public_origin
                ):
                    return _static_error(403, "CROSS_ORIGIN_STATIC_REQUEST")

            if self._casefold_header_values(headers, "authorization"):
                return _static_error(400, "STATIC_CREDENTIAL_FORBIDDEN")
            if self._casefold_header_values(headers, "x-autotrade-actor"):
                return _static_error(400, "STATIC_CREDENTIAL_FORBIDDEN")
        except (TypeError, ValueError):
            return _static_error(400, "INVALID_STATIC_REQUEST")

        return TransportResponse(
            status=200,
            content_type=asset.content_type,
            body=asset.body,
            headers=(
                ("Cache-Control", "no-store"),
                ("Content-Security-Policy", _CSP),
                ("Cross-Origin-Resource-Policy", "same-origin"),
                ("Referrer-Policy", "no-referrer"),
                ("X-Frame-Options", "DENY"),
                ("X-AutoTrade-Web-Bundle", self.web_bundle.bundle_sha256),
                ("X-AutoTrade-Source-Revision", self.web_bundle.source_revision),
                (
                    "X-AutoTrade-Host-Api-Contract",
                    self.web_bundle.host_api_contract_version,
                ),
                ("ETag", '"sha256-' + asset.sha256_hex + '"'),
            ),
        )
