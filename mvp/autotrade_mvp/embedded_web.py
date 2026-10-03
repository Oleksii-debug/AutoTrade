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
_MAX_MANIFEST_BYTES = 256 * 1024
_MAX_ASSETS = 256
_MAX_ASSET_BYTES = 32 * 1024 * 1024
_MAX_BUNDLE_BYTES = 64 * 1024 * 1024
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
    if len(value) > 255:
        raise ValueError("web asset path is too long")
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
        if len(self.body) > _MAX_ASSET_BYTES:
            raise ValueError("web asset body exceeds the release envelope")
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
    manifest_bytes: bytes = field(init=False, repr=False)
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
        if len(self.assets) > _MAX_ASSETS:
            raise ValueError("web bundle has too many assets")
        if any(type(asset) is not ImmutableWebAsset for asset in self.assets):
            raise TypeError("web bundle accepts only exact ImmutableWebAsset values")

        ordered = tuple(sorted(self.assets, key=lambda asset: asset.path))
        if len({asset.path for asset in ordered}) != len(ordered):
            raise ValueError("web bundle asset paths must be unique")
        if sum(len(asset.body) for asset in ordered) > _MAX_BUNDLE_BYTES:
            raise ValueError("web bundle exceeds the release size envelope")
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
        object.__setattr__(self, "manifest_bytes", encoded)
        object.__setattr__(self, "bundle_sha256", sha256(encoded).hexdigest())
        object.__setattr__(self, "_routes", MappingProxyType(routes))

    def asset_for_path(self, path: str) -> ImmutableWebAsset | None:
        if type(path) is not str:
            return None
        return self._routes.get(path)


def load_immutable_web_bundle(
    manifest_bytes: bytes,
    asset_bodies: dict[str, bytes],
) -> ImmutableWebAssetBundle:
    """Rehydrate one canonical release manifest plus its exact immutable bytes."""

    if type(manifest_bytes) is not bytes:
        raise TypeError("web bundle manifest must be immutable bytes")
    if not manifest_bytes or len(manifest_bytes) > _MAX_MANIFEST_BYTES:
        raise ValueError("web bundle manifest size is invalid")
    if type(asset_bodies) is not dict:
        raise TypeError("web bundle bodies must be an exact dict")
    if len(asset_bodies) > _MAX_ASSETS:
        raise ValueError("web bundle body set has too many assets")

    def reject_duplicate_keys(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("web bundle manifest contains a duplicate JSON key")
            result[key] = value
        return result

    def reject_non_finite(constant: str) -> object:
        raise ValueError("web bundle manifest contains a non-finite value: " + constant)

    try:
        decoded = json.loads(
            manifest_bytes.decode("utf-8"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_non_finite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("web bundle manifest is not strict JSON") from error
    if type(decoded) is not dict or set(decoded) != {
        "schema_version",
        "source_revision",
        "host_api_contract_version",
        "assets",
    }:
        raise ValueError("web bundle manifest fields are not canonical")
    if decoded["schema_version"] != "1":
        raise ValueError("web bundle manifest schema version is unsupported")
    records = decoded["assets"]
    if type(records) is not list or not records or len(records) > _MAX_ASSETS:
        raise ValueError("web bundle manifest assets are invalid")

    admitted: list[ImmutableWebAsset] = []
    declared_paths: set[str] = set()
    for record in records:
        if type(record) is not dict or set(record) != {
            "path",
            "sha256",
            "content_type",
            "size",
        }:
            raise ValueError("web bundle manifest asset fields are not canonical")
        path = _canonical_asset_path(record["path"])
        if path in declared_paths:
            raise ValueError("web bundle manifest asset paths must be unique")
        declared_paths.add(path)
        if path not in asset_bodies:
            raise ValueError("web bundle asset body is missing")
        body = asset_bodies[path]
        if type(body) is not bytes:
            raise TypeError("web bundle asset body must be immutable bytes")
        size = record["size"]
        if type(size) is not int or size < 0 or size != len(body):
            raise ValueError("web bundle manifest asset size does not match bytes")
        admitted.append(
            ImmutableWebAsset(
                path=path,
                body=body,
                sha256_hex=record["sha256"],
                content_type=record["content_type"],
            )
        )

    if set(asset_bodies) != declared_paths:
        raise ValueError("web bundle contains undeclared asset bodies")
    result = ImmutableWebAssetBundle(
        source_revision=decoded["source_revision"],
        host_api_contract_version=decoded["host_api_contract_version"],
        assets=tuple(admitted),
    )
    if result.manifest_bytes != manifest_bytes:
        raise ValueError("web bundle manifest is not canonical JSON encoding")
    return result


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
