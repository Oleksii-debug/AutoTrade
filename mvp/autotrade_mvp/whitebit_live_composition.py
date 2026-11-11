"""Default fail-closed WhiteBIT LIVE composition for the installed product.

No order is submitted here.  Startup proves the protected least-privilege
TRADE binding, performs fixed read-only account calls, reads current public
metadata for the supported SPOT markets, retains immutable evidence, and then
derives one short-lived current-process capability.  Only the manual confirmed
gateway owns the later single write send.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
from threading import Lock, RLock
import time
from typing import Callable, Mapping
from uuid import uuid4

from autotrade_runtime.artifacts import ArtifactStore

from .capabilities import (
    CapabilityClaim,
    CapabilityError,
    CapabilitySnapshot,
    EvidenceVerification,
    SOURCES,
    artifact_store_evidence_verifier,
    derive_current_process_capability_snapshot,
)
from .durable_capabilities import DurableCapabilityRegistry
from .persistence import JournalStore, canonical_json
from .provider_transport import WhiteBitCredential
from .security import SecurityBoundary
from .whitebit_manual_live_gateway import (
    SUPPORTED_WHITEBIT_SPOT_MARKETS,
    build_protected_whitebit_manual_live_gateway,
    whitebit_spot_rules_from_public_snapshot,
)
from .whitebit_trade_setup import load_whitebit_trade_binding
from .windows_secrets import ProtectedCredentialVault
from research.autotrade_research.integrations.whitebit_private_readonly import (
    WhitebitPrivateReader,
)


_ENTITY_ID = "whitebit-spot-manual"
_EVIDENCE_VERSION = "whitebit-live-current-process-v1"
_PRODUCER_TYPES = {
    "DOCUMENTED": "PROVIDER_DOCUMENTATION",
    "API": "PROVIDER_API",
    "ACCOUNT": "ACCOUNT_CAPABILITY",
    "INSTRUMENT": "INSTRUMENT_CAPABILITY",
}
_SOURCE_URIS = {
    "DOCUMENTED": "https://docs.whitebit.com/",
    "API": "https://whitebit.com/api/v4/public/markets",
    "ACCOUNT": "https://whitebit.com/api/v4/trade-account/balance",
    "INSTRUMENT": "https://whitebit.com/api/v4/public/markets",
}


class WhiteBitLiveCompositionError(ValueError):
    """Current protected WhiteBIT LIVE evidence cannot admit order entry."""


def _utc(value: datetime) -> datetime:
    if type(value) is not datetime or value.tzinfo is None:
        raise TypeError("clock_utc must return an aware datetime")
    return value.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _issuer_identity(source: str) -> tuple[str, str]:
    digest = sha256(
        ("AutoTrade|WHITEBIT|" + source + "|" + _EVIDENCE_VERSION).encode(
            "ascii"
        )
    ).hexdigest()
    return (
        "autotrade-whitebit-" + source.lower() + ":sha256:" + digest,
        "sha256:" + digest,
    )


def _production_account_evidence(
    vault: ProtectedCredentialVault,
    handle,
    *,
    reader_factory=WhitebitPrivateReader,
) -> dict[str, object]:
    """Perform only WhiteBIT's fixed balance reads with the TRADE credential."""

    from .whitebit_account_projection import project_whitebit_account

    with vault.lease(
        handle,
        execution_identity="local-owner",
        account_id=handle.account_id,
        provider="WHITEBIT",
        environment="LIVE",
        provider_environment="LIVE",
        purpose="TRADE",
    ) as plaintext:
        try:
            credential = WhiteBitCredential.parse(plaintext)
        finally:
            plaintext = None
        with reader_factory(
            credential.api_key,
            credential.api_secret,
            enable_network=True,
        ) as reader:
            main = reader.main_balance()
            trading = reader.trading_balance()
        credential = None
    projection = project_whitebit_account(main, trading)
    if (
        projection.get("verified_account_binding") is not True
        or projection.get("provider") != "WHITEBIT"
        or projection.get("environment") != "LIVE"
        or projection.get("account_id") != handle.account_id
    ):
        raise WhiteBitLiveCompositionError(
            "WhiteBIT TRADE credential did not prove the current account binding"
        )
    return {
        "schema": "autotrade-whitebit-live-account-evidence-v1",
        "provider": "WHITEBIT",
        "environment": "LIVE",
        "account_id": handle.account_id,
        "observed_at": projection["observed_at"],
        "verified_account_binding": True,
        "read_routes": [
            "/api/v4/main-account/balance",
            "/api/v4/trade-account/balance",
        ],
        # Retain provider response identities, never balances or credentials.
        "source_sha256": ["sha256:" + value for value in projection["source_sha256"]],
        "contains_balances": False,
        "contains_secrets": False,
    }


class WhiteBitCurrentCapabilityProvider:
    """Refresh and serve one admitted capability for the supported SPOT set."""

    def __init__(
        self,
        journal: JournalStore,
        *,
        data_dir: str | Path,
        credential_vault: ProtectedCredentialVault,
        public_snapshot_provider: Callable[[str], Mapping[str, object]],
        account_evidence_provider: Callable[[], Mapping[str, object]] | None = None,
        clock_utc: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if type(journal) is not JournalStore:
            raise TypeError("journal must be exact JournalStore")
        if type(credential_vault) is not ProtectedCredentialVault:
            raise TypeError("credential_vault must be exact ProtectedCredentialVault")
        if not callable(public_snapshot_provider) or not callable(clock_utc):
            raise TypeError("WhiteBIT capability providers must be callable")
        self._journal = journal
        self._registry = DurableCapabilityRegistry(journal)
        self._data_dir = Path(data_dir).resolve(strict=False)
        self._vault = credential_vault
        self._public = public_snapshot_provider
        self._clock = clock_utc
        self._lock = RLock()
        self._snapshot: CapabilitySnapshot | None = None
        self._handle, self._boundary = load_whitebit_trade_binding(
            credential_vault, data_dir=self._data_dir
        )
        self._account = account_evidence_provider or (
            lambda: _production_account_evidence(self._vault, self._handle)
        )
        if not callable(self._account):
            raise TypeError("account_evidence_provider must be callable")
        self.refresh()

    @property
    def handle(self):
        return self._handle

    def _publish(
        self,
        store: ArtifactStore,
        *,
        source: str,
        payload: Mapping[str, object],
        observed_at: str,
        instrument_version: str,
    ) -> dict[str, object]:
        issuer_ref, issuer_sha256 = _issuer_identity(source)
        artifact_id = str(uuid4())
        raw = canonical_json(dict(payload)).encode("utf-8")
        manifest = store.publish_bytes(
            artifact_id=artifact_id,
            data=raw,
            media_type="application/json",
            rights={
                "storage": True,
                "export": False,
                "rights_id": "whitebit-live-capability-evidence",
            },
            source_refs=[_SOURCE_URIS[source]],
            metadata={
                "artifact_kind": "CAPABILITY_EVIDENCE",
                "schema_version": 1,
                "capability_source": source,
                "producer_type": _PRODUCER_TYPES[source],
                "producer_id": "autotrade-whitebit-live-binder",
                "evidence_version": _EVIDENCE_VERSION,
                "provider_id": "WHITEBIT",
                "account_id": self._handle.account_id,
                "entity_id": _ENTITY_ID,
                "environment": "LIVE",
                "provider_environment": "LIVE",
                "instrument_version": instrument_version,
                "observed_at": observed_at,
                "issuer_ref": issuer_ref,
                "issuer_sha256": issuer_sha256,
            },
        )
        return {
            "artifact_id": artifact_id,
            "sha256": manifest["sha256"],
            "observed_at": observed_at,
            "source_uri": _SOURCE_URIS[source],
            "rights_id": "whitebit-live-capability-evidence",
            "issuer_ref": issuer_ref,
            "issuer_sha256": issuer_sha256,
        }

    def refresh(self) -> CapabilitySnapshot:
        """Execute a fresh read-only proof and issue no order operation."""

        with self._lock:
            handle, boundary = load_whitebit_trade_binding(
                self._vault, data_dir=self._data_dir
            )
            if handle != self._handle or boundary != self._boundary:
                raise WhiteBitLiveCompositionError(
                    "WhiteBIT protected TRADE binding changed; restart required"
                )
            public: dict[str, dict[str, object]] = {}
            rules: dict[str, dict[str, object]] = {}
            for market in sorted(SUPPORTED_WHITEBIT_SPOT_MARKETS):
                current = dict(self._public(market))
                canonical_rules = whitebit_spot_rules_from_public_snapshot(current)
                public[market] = current
                rules[market] = {
                    "market": canonical_rules.market,
                    "market_type": canonical_rules.market_type,
                    "step_size": str(canonical_rules.step_size),
                    "tick_size": str(canonical_rules.tick_size),
                    "minimum_amount": str(canonical_rules.min_amount),
                    "minimum_notional": str(canonical_rules.min_total),
                    "trades_enabled": canonical_rules.trades_enabled,
                }
            account = dict(self._account())
            if (
                account.get("verified_account_binding") is not True
                or account.get("account_id") != self._handle.account_id
                or account.get("contains_secrets") is not False
            ):
                raise WhiteBitLiveCompositionError(
                    "current WhiteBIT account evidence is invalid"
                )
            point = _utc(self._clock())
            observed = _utc_text(point)
            instrument_digest = sha256(
                canonical_json(rules).encode("utf-8")
            ).hexdigest()
            instrument_version = "whitebit-spot-" + instrument_digest[:24]
            payloads = {
                "DOCUMENTED": {
                    "schema": "autotrade-whitebit-documented-order-contract-v1",
                    "provider": "WHITEBIT",
                    "environment": "LIVE",
                    "product_family": "SPOT",
                    "write_routes": [
                        "/api/v4/order/cancel",
                        "/api/v4/order/market",
                        "/api/v4/order/new",
                    ],
                    "forbidden_families": [
                        "COLLATERAL", "DEPOSIT", "LEVERAGE", "TRANSFER", "WITHDRAWAL"
                    ],
                },
                "API": {
                    "schema": "autotrade-whitebit-current-api-evidence-v1",
                    "markets": public,
                    "observed_at": observed,
                },
                "ACCOUNT": account,
                "INSTRUMENT": {
                    "schema": "autotrade-whitebit-current-instrument-evidence-v1",
                    "markets": rules,
                    "observed_at": observed,
                },
            }
            evidence_root = self._data_dir / "state" / "capability-evidence"
            store = ArtifactStore(evidence_root)
            refs = {
                source: self._publish(
                    store,
                    source=source,
                    payload=payloads[source],
                    observed_at=observed,
                    instrument_version=instrument_version,
                )
                for source in sorted(SOURCES)
            }

            def issuer(source: str):
                expected_ref, expected_sha = _issuer_identity(source)

                def verify(claim, issuer_ref, issuer_sha256):
                    if (
                        claim.source != source
                        or issuer_ref != expected_ref
                        or issuer_sha256 != expected_sha
                        or claim.provider_id != "WHITEBIT"
                        or claim.account_id != self._handle.account_id
                        or claim.environment != "LIVE"
                        or claim.provider_environment != "LIVE"
                    ):
                        return EvidenceVerification(
                            valid=False,
                            conflicted=True,
                            reason="WhiteBIT capability issuer identity differs",
                        )
                    return EvidenceVerification(valid=True)

                return verify

            verifier = artifact_store_evidence_verifier(
                evidence_root=evidence_root,
                publication_store=store,
                issuer_verifiers={source: issuer(source) for source in SOURCES},
            )
            expiry = point + timedelta(minutes=5)
            common = {
                "provider_id": "WHITEBIT",
                "account_id": self._handle.account_id,
                "entity_id": _ENTITY_ID,
                "environment": "LIVE",
                "provider_environment": "LIVE",
                "instrument_version": instrument_version,
                "observed_at": point,
                "expires_at": expiry,
                "supported_order_types": frozenset({"MARKET", "LIMIT"}),
                "time_in_force": frozenset({"GTC"}),
                "permission_scopes": frozenset(
                    {"ACCOUNT_READ", "ORDER_MANAGEMENT", "ORDER_WRITE"}
                ),
                "position_mode": "NET",
                "native_protection": frozenset(),
                "rate_limit_policy_id": "whitebit-v4-manual-live-v1",
                "data_entitlements": frozenset({"BALANCES", "ORDERS", "SPOT_MARKET"}),
            }
            claims = tuple(
                CapabilityClaim(
                    source=source,
                    evidence_ref=refs[source],
                    **common,
                )
                for source in sorted(SOURCES)
            )
            snapshot = derive_current_process_capability_snapshot(
                snapshot_id=str(uuid4()),
                claims=claims,
                observed_at=point,
                evidence_verifier=verifier,
            )
            if snapshot.status != "VERIFIED" or not getattr(
                snapshot, "_can_admit", False
            ):
                raise CapabilityError(
                    "WhiteBIT current-process capability did not verify"
                )
            self._registry.add(snapshot)
            self._snapshot = snapshot
            return snapshot

    def __call__(self, market: str) -> CapabilitySnapshot:
        if market not in SUPPORTED_WHITEBIT_SPOT_MARKETS:
            raise WhiteBitLiveCompositionError(
                "market is outside the current WhiteBIT SPOT allowlist"
            )
        with self._lock:
            point = _utc(self._clock())
            snapshot = self._snapshot
            if snapshot is None or point + timedelta(seconds=30) >= snapshot.expires_at:
                snapshot = self.refresh()
                point = _utc(self._clock())
            return self._registry.require_verified(
                provider_id=snapshot.provider_id,
                account_id=snapshot.account_id,
                entity_id=snapshot.entity_id,
                environment=snapshot.environment,
                provider_environment=snapshot.provider_environment,
                instrument_version=snapshot.instrument_version,
                at=point,
            )


class WhiteBitManualQuotaGate:
    """Small process-local write budget with a separately reserved cancel lane."""

    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        if not callable(monotonic):
            raise TypeError("monotonic must be callable")
        self._clock = monotonic
        self._lock = Lock()
        self._writes: list[float] = []
        self._cancels: list[float] = []

    def __call__(self, provider, account_id, environment, request_class) -> None:
        if (
            provider != "WHITEBIT"
            or not isinstance(account_id, str)
            or not account_id
            or environment != "LIVE"
            or request_class not in {"ORDER_WRITE", "ORDER_CANCEL_RESERVED"}
        ):
            raise PermissionError("WhiteBIT manual quota scope differs")
        with self._lock:
            now = float(self._clock())
            self._writes[:] = [value for value in self._writes if now - value < 60]
            self._cancels[:] = [value for value in self._cancels if now - value < 60]
            lane, limit = (
                (self._cancels, 30)
                if request_class == "ORDER_CANCEL_RESERVED"
                else (self._writes, 10)
            )
            if len(lane) >= limit:
                raise PermissionError("WhiteBIT manual write quota is exhausted")
            lane.append(now)


def default_whitebit_live_gateway_factory(**kwargs):
    """Concrete installed-product factory; absence/staleness fails closed."""

    required = {
        "journal",
        "data_dir",
        "credential_vault",
        "security_boundary",
        "public_snapshot_provider",
        "session_token_provider",
        "origin_provider",
    }
    if set(kwargs) != required:
        raise TypeError("default WhiteBIT LIVE composition fields differ")
    journal = kwargs["journal"]
    vault = kwargs["credential_vault"]
    boundary = kwargs["security_boundary"]
    if type(journal) is not JournalStore:
        raise TypeError("journal must be exact JournalStore")
    if type(vault) is not ProtectedCredentialVault:
        raise TypeError("credential_vault must be exact ProtectedCredentialVault")
    if type(boundary) is not SecurityBoundary:
        raise TypeError("security_boundary must be exact SecurityBoundary")
    capability = WhiteBitCurrentCapabilityProvider(
        journal,
        data_dir=kwargs["data_dir"],
        credential_vault=vault,
        public_snapshot_provider=kwargs["public_snapshot_provider"],
    )
    return build_protected_whitebit_manual_live_gateway(
        journal,
        data_dir=str(kwargs["data_dir"]),
        credential_vault=vault,
        security_boundary=boundary,
        capability_provider=capability,
        public_snapshot_provider=kwargs["public_snapshot_provider"],
        session_token_provider=kwargs["session_token_provider"],
        origin_provider=kwargs["origin_provider"],
        quota_gate=WhiteBitManualQuotaGate(),
    )


__all__ = [
    "WhiteBitCurrentCapabilityProvider",
    "WhiteBitLiveCompositionError",
    "WhiteBitManualQuotaGate",
    "default_whitebit_live_gateway_factory",
]
