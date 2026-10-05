"""Production composition of the canonical immutable web bundle onto one Host.

The production host remains the only HTTP server and financial command authority.
This module installs a static-only dispatch adapter *outside* the existing
production admission/store-identity dispatch so canonical Host API requests still
traverse the exact existing authority chain while immutable UI bytes share the
same origin without receiving bearer credentials.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Callable, Mapping
from urllib.parse import urlsplit

from .embedded_web import (
    EmbeddedWebHostApplication,
    ImmutableWebAsset,
    ImmutableWebAssetBundle,
    _CSP,
    _static_error,
    load_immutable_web_bundle,
)
from .host_network import AuthenticatedHostApplication, TransportResponse
from .production_host import (
    ProductionHostConfig,
    ProductionHostRuntime,
    build_production_host,
)
from .security import SecurityBoundary, _authenticated_origin
from .host_network import PrincipalResolver, SnapshotProvider


Dispatch = Callable[..., TransportResponse]


class ProductionEmbeddedWebDispatch:
    """Static-only adapter around an already composed production Host dispatch."""

    def __init__(
        self,
        *,
        base_dispatch: Dispatch,
        public_origin: str,
        web_bundle: ImmutableWebAssetBundle,
    ) -> None:
        if not callable(base_dispatch):
            raise TypeError("base_dispatch must be callable")
        if type(public_origin) is not str:
            raise TypeError("public_origin must be exact text")
        canonical_origin = _authenticated_origin(public_origin)
        if type(web_bundle) is not ImmutableWebAssetBundle:
            raise TypeError("web_bundle must be exact ImmutableWebAssetBundle")

        candidate_assets = web_bundle.assets
        if type(candidate_assets) is not tuple or not candidate_assets:
            raise TypeError("web_bundle assets must remain an exact non-empty tuple")
        asset_bodies: dict[str, bytes] = {}
        for candidate in candidate_assets:
            if type(candidate) is not ImmutableWebAsset:
                raise TypeError(
                    "web_bundle assets must remain exact ImmutableWebAsset values"
                )
            path = candidate.path
            body = candidate.body
            if type(path) is not str or type(body) is not bytes:
                raise TypeError(
                    "web_bundle asset identity/bytes changed after validation"
                )
            if path in asset_bodies:
                raise ValueError("web_bundle asset paths changed after validation")
            asset_bodies[path] = body

        sealed_bundle = load_immutable_web_bundle(
            web_bundle.manifest_bytes,
            asset_bodies,
        )
        self._base_dispatch = base_dispatch
        self.public_origin = canonical_origin
        self._web_routes = MappingProxyType(
            {
                path: (asset.body, asset.sha256_hex, asset.content_type)
                for path, asset in sealed_bundle._routes.items()
            }
        )
        self._web_bundle_sha256 = sealed_bundle.bundle_sha256
        self._web_source_revision = sealed_bundle.source_revision
        self._web_host_api_contract_version = (
            sealed_bundle.host_api_contract_version
        )

    @staticmethod
    def _header_values(
        headers: Mapping[str, str],
        name: str,
    ) -> tuple[str, ...]:
        # Reuse the already-reviewed static-header normalization contract rather
        # than creating another interpretation of credential-bearing headers.
        return EmbeddedWebHostApplication._casefold_header_values(headers, name)

    def dispatch(
        self,
        *,
        method: str,
        target: str,
        headers: Mapping[str, str],
        body: bytes = b"",
    ) -> TransportResponse:
        if type(target) is not str:
            return _static_error(400, "INVALID_STATIC_REQUEST")
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            return self._base_dispatch(
                method=method,
                target=target,
                headers=headers,
                body=body,
            )

        asset = self._web_routes.get(parsed.path)
        if asset is None:
            return self._base_dispatch(
                method=method,
                target=target,
                headers=headers,
                body=body,
            )
        if type(headers) is not dict:
            return _static_error(400, "INVALID_STATIC_REQUEST")

        try:
            if type(method) is not str or method != "GET":
                return _static_error(405, "STATIC_METHOD_NOT_ALLOWED")
            if type(body) is not bytes or body:
                return _static_error(400, "STATIC_BODY_FORBIDDEN")
            if parsed.query:
                return _static_error(400, "STATIC_QUERY_FORBIDDEN")

            origins = self._header_values(headers, "origin")
            if len(origins) > 1:
                return _static_error(400, "DUPLICATE_ORIGIN")
            if origins and (
                not origins[0]
                or _authenticated_origin(origins[0]) != self.public_origin
            ):
                return _static_error(403, "CROSS_ORIGIN_STATIC_REQUEST")
            if self._header_values(headers, "authorization"):
                return _static_error(400, "STATIC_CREDENTIAL_FORBIDDEN")
            if self._header_values(headers, "x-autotrade-actor"):
                return _static_error(400, "STATIC_CREDENTIAL_FORBIDDEN")
        except (TypeError, ValueError):
            return _static_error(400, "INVALID_STATIC_REQUEST")

        asset_body, asset_sha256_hex, asset_content_type = asset
        return TransportResponse(
            status=200,
            content_type=asset_content_type,
            body=asset_body,
            headers=(
                ("Cache-Control", "no-store"),
                ("Content-Security-Policy", _CSP),
                ("Cross-Origin-Resource-Policy", "same-origin"),
                ("Referrer-Policy", "no-referrer"),
                ("X-Frame-Options", "DENY"),
                ("X-Content-Type-Options", "nosniff"),
                ("X-AutoTrade-Web-Bundle", self._web_bundle_sha256),
                ("X-AutoTrade-Source-Revision", self._web_source_revision),
                (
                    "X-AutoTrade-Host-Api-Contract",
                    self._web_host_api_contract_version,
                ),
                ("ETag", '"sha256-' + asset_sha256_hex + '"'),
            ),
        )


def install_production_web_bundle(
    runtime: ProductionHostRuntime,
    web_bundle: ImmutableWebAssetBundle,
) -> ProductionHostRuntime:
    """Install one sealed static bundle before a production runtime starts serving."""

    if type(runtime) is not ProductionHostRuntime:
        raise TypeError("runtime must be exact ProductionHostRuntime")
    if runtime.serving or runtime.shutdown_requested or runtime.closed:
        raise RuntimeError(
            "production web bundle must be installed before host serving or shutdown"
        )
    if type(web_bundle) is not ImmutableWebAssetBundle:
        raise TypeError("web_bundle must be exact ImmutableWebAssetBundle")

    application = runtime.application
    if type(application) is not AuthenticatedHostApplication:
        raise TypeError(
            "production web bundle requires canonical AuthenticatedHostApplication"
        )
    if runtime.server.application is not application:
        raise RuntimeError("production server/application identity is inconsistent")

    router = ProductionEmbeddedWebDispatch(
        base_dispatch=application.dispatch,
        public_origin=application.public_origin,
        web_bundle=web_bundle,
    )
    application.dispatch = router.dispatch  # type: ignore[method-assign]
    return runtime


def build_embedded_production_host(
    config: ProductionHostConfig,
    *,
    security_boundary: SecurityBoundary,
    principal_resolver: PrincipalResolver,
    snapshot_provider: SnapshotProvider,
    web_bundle: ImmutableWebAssetBundle,
    tls_context=None,
    now=None,
) -> ProductionHostRuntime:
    """Build the canonical production host and install one immutable web bundle."""

    runtime = build_production_host(
        config,
        security_boundary=security_boundary,
        principal_resolver=principal_resolver,
        snapshot_provider=snapshot_provider,
        tls_context=tls_context,
        now=now,
    )
    try:
        return install_production_web_bundle(runtime, web_bundle)
    except BaseException:
        runtime.close()
        raise
