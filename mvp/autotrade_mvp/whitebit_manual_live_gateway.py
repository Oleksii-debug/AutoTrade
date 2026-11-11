"""Production composition for explicitly confirmed manual WhiteBIT SPOT writes.

The manual UI owns drafting and human confirmation.  This module starts only
after that boundary and composes the existing WhiteBIT adapter, protected
credential lease, guarded dispatcher and single-send HTTP transport.  It owns
no retry loop: a durable UNKNOWN outcome can only be resolved by provider
reconciliation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from typing import Callable, Mapping

from .capabilities import CapabilitySnapshot
from .dispatch import GuardedDispatcher, stable_client_order_id
from .exact_decimal import parse_bounded_exact_decimal
from .persistence import JournalStore, canonical_json
from .provider_transport import (
    ProviderWireClient,
    QuotaGate,
    WHITEBIT_ENDPOINT_POLICIES,
    WhiteBitDurableNonceAllocator,
    WhiteBitHttpTransport,
)
from .security import SecurityBoundary
from .whitebit import (
    WhiteBitCredentialBoundary,
    WhiteBitMarketRules,
    WhiteBitOrderIntent,
    prepare_cancel_request,
    prepare_order_request,
)
from .windows_secrets import PersistentCredentialHandle


SUPPORTED_WHITEBIT_SPOT_MARKETS = frozenset(
    {"BTC_USDT", "ETH_USDT", "SOL_USDT"}
)
_COMMAND_SCHEMA = "autotrade-canonical-manual-command-v1"
_DRAFT_SCHEMA = "autotrade-manual-whitebit-v1"
_DRAFT_FIELDS = frozenset(
    {
        "schema",
        "draft_id",
        "source",
        "action",
        "provider",
        "environment",
        "market",
        "product_family",
        "quantity",
        "order_type",
        "limit_price",
        "client_order_id",
        "prepared_at",
        "expires_at",
        "quote",
        "instrument",
        "execution_performed",
    }
)


class WhiteBitManualLiveGatewayError(ValueError):
    """The confirmed command cannot enter the canonical LIVE send path."""


def _utc(value: datetime, *, name: str) -> tuple[datetime, str]:
    if type(value) is not datetime or value.tzinfo is None:
        raise TypeError(f"{name} must return an aware datetime")
    point = value.astimezone(timezone.utc)
    return point, point.isoformat().replace("+00:00", "Z")


def _exact_text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise WhiteBitManualLiveGatewayError(f"{name} must be canonical text")
    return value


def _validate_binding(
    handle: PersistentCredentialHandle,
    boundary: WhiteBitCredentialBoundary,
) -> None:
    if type(handle) is not PersistentCredentialHandle:
        raise TypeError("credential_handle must be exact PersistentCredentialHandle")
    if type(boundary) is not WhiteBitCredentialBoundary:
        raise TypeError("credential boundary must be exact WhiteBitCredentialBoundary")
    boundary.assert_autotrade_safe()
    if (
        handle.provider != "WHITEBIT"
        or handle.environment != "LIVE"
        or handle.provider_environment != "LIVE"
        or handle.purpose != "TRADE"
        or handle.account_id != boundary.account_id
        or handle.handle_id != boundary.credential_binding_id
        or handle.generation != boundary.credential_generation
    ):
        raise WhiteBitManualLiveGatewayError(
            "WhiteBIT TRADE credential handle differs from safe binding evidence"
        )


def whitebit_spot_rules_from_public_snapshot(
    value: Mapping[str, object],
) -> WhiteBitMarketRules:
    """Convert the product's freshly fetched public snapshot to adapter rules.

    This is the concrete bridge from ``read_whitebit_live_spot_snapshot`` to
    the order preparer.  The caller still fetches a new snapshot for every
    initial/final authority check; this helper only validates and translates
    that exact read and never caches provider constraints.
    """

    if not isinstance(value, Mapping):
        raise TypeError("WhiteBIT public snapshot must be a mapping")
    snapshot = dict(value)
    if (
        snapshot.get("schema") != "autotrade-whitebit-public-quote-v1"
        or snapshot.get("venue") != "WHITEBIT_SPOT"
        or snapshot.get("public_data_only") is not True
        or snapshot.get("trading_authority") is not False
    ):
        raise WhiteBitManualLiveGatewayError(
            "WhiteBIT public snapshot authority boundary differs"
        )
    instrument = snapshot.get("instrument")
    if not isinstance(instrument, Mapping):
        raise WhiteBitManualLiveGatewayError(
            "WhiteBIT public instrument metadata is unavailable"
        )
    metadata = dict(instrument)
    if (
        metadata.get("schema") != "autotrade-whitebit-spot-instrument-v1"
        or metadata.get("market") != snapshot.get("market")
        or metadata.get("trades_enabled") is not True
        or metadata.get("delisted_at") is not None
    ):
        raise WhiteBitManualLiveGatewayError(
            "WhiteBIT public instrument is not a current enabled SPOT market"
        )
    quantity_precision = metadata.get("quantity_precision")
    quote_precision = metadata.get("quote_precision")
    if (
        type(quantity_precision) is not int
        or type(quote_precision) is not int
        or not 0 <= quantity_precision <= 24
        or not 0 <= quote_precision <= 24
    ):
        raise WhiteBitManualLiveGatewayError(
            "WhiteBIT public instrument precision is invalid"
        )
    step_size = format(Decimal(1).scaleb(-quantity_precision), "f")
    tick_size = format(Decimal(1).scaleb(-quote_precision), "f")
    return WhiteBitMarketRules.from_provider(
        {
            "name": metadata["market"],
            "type": "spot",
            "isTradFiFutures": False,
            "isCollateral": False,
            "tradesEnabled": True,
            "stepSize": step_size,
            "tickSize": tick_size,
            "minAmount": metadata.get("minimum_quantity"),
            "minTotal": metadata.get("minimum_notional"),
            "maxTotal": "0",
            "delistedAt": None,
        }
    )


class WhiteBitManualLiveGateway:
    """Callable matching ``ManualTradingController.canonical_command_gateway``.

    Every dependency that can become stale is re-read by the dispatcher's
    initial and final authority checks.  The final check compares the same
    capability, credential generation and public market rules that prepared the
    persisted request.  It therefore cannot silently upgrade an old draft to a
    newer or broader authority.
    """

    def __init__(
        self,
        journal: JournalStore,
        *,
        security_boundary: SecurityBoundary,
        credential_handle: PersistentCredentialHandle,
        credential_boundary_provider: Callable[[], WhiteBitCredentialBoundary],
        capability_provider: Callable[[str], CapabilitySnapshot],
        market_rules_provider: Callable[[str], WhiteBitMarketRules],
        session_token_provider: Callable[[], str],
        origin_provider: Callable[[], str],
        quota_gate: QuotaGate,
        clock_utc: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        clock_millis: Callable[[], int] | None = None,
        wire_client: ProviderWireClient | None = None,
        execution_identity: str = "local-owner",
        owner_token: str = "whitebit-manual-live-owner",
    ) -> None:
        if type(journal) is not JournalStore:
            raise TypeError("journal must be exact JournalStore")
        if type(security_boundary) is not SecurityBoundary:
            raise TypeError("security_boundary must be exact SecurityBoundary")
        for value, name in (
            (credential_boundary_provider, "credential_boundary_provider"),
            (capability_provider, "capability_provider"),
            (market_rules_provider, "market_rules_provider"),
            (session_token_provider, "session_token_provider"),
            (origin_provider, "origin_provider"),
            (quota_gate, "quota_gate"),
            (clock_utc, "clock_utc"),
        ):
            if not callable(value):
                raise TypeError(f"{name} must be callable")
        self._journal = journal
        self._security_boundary = security_boundary
        self._handle = credential_handle
        self._boundary_provider = credential_boundary_provider
        self._capability_provider = capability_provider
        self._market_rules_provider = market_rules_provider
        self._session_token_provider = session_token_provider
        self._origin_provider = origin_provider
        self._quota_gate = quota_gate
        self._clock_utc = clock_utc
        self._clock_millis = clock_millis or (
            lambda: int(_utc(self._clock_utc(), name="clock_utc")[0].timestamp() * 1000)
        )
        self._wire_client = wire_client
        self._execution_identity = _exact_text(
            execution_identity, name="execution_identity"
        )
        initial_boundary = self._boundary_provider()
        _validate_binding(self._handle, initial_boundary)
        self._dispatcher = GuardedDispatcher(
            journal,
            environment="LIVE",
            account_id=self._handle.account_id,
            owner_token=_exact_text(owner_token, name="owner_token"),
        )
        self._nonce_allocator = WhiteBitDurableNonceAllocator(
            journal=journal,
            account_id=self._handle.account_id,
            environment="LIVE",
            clock_millis=self._clock_millis,
            clock_utc=self._clock_utc,
        )

    @staticmethod
    def _current_capability(
        provider: Callable[[str], CapabilitySnapshot],
        market: str,
        at: datetime,
    ) -> CapabilitySnapshot:
        capability = provider(market)
        if type(capability) is not CapabilitySnapshot:
            raise WhiteBitManualLiveGatewayError(
                "WhiteBIT capability provider returned a non-canonical snapshot"
            )
        if (
            capability.provider_id.upper() != "WHITEBIT"
            or capability.account_id == ""
            or capability.environment != "LIVE"
            or capability.provider_environment != "LIVE"
            or capability.status != "VERIFIED"
            or not {"MARKET", "LIMIT"}.issubset(capability.supported_order_types)
            or "GTC" not in capability.time_in_force
            or "ORDER_WRITE" not in capability.permission_scopes
            or not capability.admits(
                at=at,
                order_type="LIMIT",
                time_in_force="GTC",
                permission_scope="ORDER_WRITE",
            )
        ):
            raise WhiteBitManualLiveGatewayError(
                "current WhiteBIT LIVE capability does not admit SPOT order writes"
            )
        return capability

    def capability_projection(self) -> dict[str, object]:
        """Return the secret-free capability shape consumed by the manual UI."""

        try:
            point, _ = _utc(self._clock_utc(), name="clock_utc")
            boundary = self._boundary_provider()
            _validate_binding(self._handle, boundary)
            capabilities = tuple(
                self._current_capability(self._capability_provider, market, point)
                for market in sorted(SUPPORTED_WHITEBIT_SPOT_MARKETS)
            )
            snapshot_ids = {item.snapshot_id for item in capabilities}
            if len(snapshot_ids) != 1 or any(
                item.account_id != self._handle.account_id for item in capabilities
            ):
                raise WhiteBitManualLiveGatewayError(
                    "supported SPOT markets do not share one current capability"
                )
            return {
                "provider_id": "WHITEBIT",
                "environment": "LIVE",
                "account_id": self._handle.account_id,
                "permission_scopes": [
                    "ACCOUNT_READ",
                    "ORDER_MANAGEMENT",
                    "ORDER_WRITE",
                ],
                "capability_snapshot_id": next(iter(snapshot_ids)),
                "current": True,
                "credential_profile": self._handle.handle_id,
                "credential_purpose": "TRADE",
                "forbidden_permissions": [],
                "blocker": None,
            }
        except (OSError, RuntimeError, TypeError, ValueError):
            return {
                "provider_id": "WHITEBIT",
                "environment": "LIVE",
                "account_id": self._handle.account_id,
                "permission_scopes": [],
                "capability_snapshot_id": None,
                "current": False,
                "credential_profile": self._handle.handle_id,
                "credential_purpose": "TRADE",
                "forbidden_permissions": [],
                "blocker": (
                    "WhiteBIT LIVE requires a current exact INFO+TRADING, "
                    "IP-whitelisted TRADE binding and verified SPOT capability"
                ),
            }

    @staticmethod
    def _draft(command: Mapping[str, object]) -> tuple[str, dict[str, object]]:
        if type(command) is not dict or set(command) != {
            "schema", "command_id", "source", "capability_snapshot_id", "draft"
        }:
            raise WhiteBitManualLiveGatewayError(
                "manual WhiteBIT canonical command fields differ"
            )
        if command.get("schema") != _COMMAND_SCHEMA:
            raise WhiteBitManualLiveGatewayError(
                "manual WhiteBIT canonical command schema differs"
            )
        command_id = _exact_text(command.get("command_id"), name="command_id")
        if command.get("source") not in {"MANUAL_CONFIRMED", "ASSISTED_CONFIRMED"}:
            raise WhiteBitManualLiveGatewayError(
                "manual WhiteBIT command source is not explicitly confirmed"
            )
        raw = command.get("draft")
        if type(raw) is not dict or set(raw) != _DRAFT_FIELDS:
            raise WhiteBitManualLiveGatewayError(
                "manual WhiteBIT draft fields differ"
            )
        draft = dict(raw)
        if (
            draft.get("schema") != _DRAFT_SCHEMA
            or draft.get("draft_id") != command_id
            or draft.get("provider") != "WHITEBIT"
            or draft.get("environment") != "LIVE"
            or draft.get("product_family") != "SPOT"
            or draft.get("execution_performed") is not False
        ):
            raise WhiteBitManualLiveGatewayError(
                "manual WhiteBIT draft crosses its confirmed SPOT boundary"
            )
        market = draft.get("market")
        if market not in SUPPORTED_WHITEBIT_SPOT_MARKETS:
            raise WhiteBitManualLiveGatewayError(
                "manual WhiteBIT market is outside the supported SPOT allowlist"
            )
        if draft.get("action") not in {"BUY", "SELL", "CANCEL"}:
            raise WhiteBitManualLiveGatewayError(
                "manual WhiteBIT live action must be BUY, SELL or CANCEL"
            )
        return command_id, draft

    def __call__(self, command: Mapping[str, object]) -> Mapping[str, object]:
        command_id, draft = self._draft(command)
        point, now = _utc(self._clock_utc(), name="clock_utc")
        market = str(draft["market"])
        capability = self._current_capability(
            self._capability_provider, market, point
        )
        if (
            capability.account_id != self._handle.account_id
            or command.get("capability_snapshot_id") != capability.snapshot_id
        ):
            raise WhiteBitManualLiveGatewayError(
                "manual WhiteBIT command capability is not exact current authority"
            )
        boundary = self._boundary_provider()
        _validate_binding(self._handle, boundary)
        rules = self._market_rules_provider(market)
        if type(rules) is not WhiteBitMarketRules:
            raise WhiteBitManualLiveGatewayError(
                "WhiteBIT market rules provider returned non-canonical metadata"
            )
        if (
            rules.market != market
            or rules.market_type != "SPOT"
            or rules.is_collateral
            or rules.is_tradfi_futures
            or not rules.trades_enabled
        ):
            raise WhiteBitManualLiveGatewayError(
                "current WhiteBIT market metadata does not admit SPOT trading"
            )

        action = str(draft["action"])
        intent_id = command_id
        if action == "CANCEL":
            prepared = prepare_cancel_request(
                market=market,
                account_id=self._handle.account_id,
                environment="LIVE",
                capability=capability,
                instrument_version=capability.instrument_version,
                at=point,
                client_order_id=_exact_text(
                    draft.get("client_order_id"), name="client_order_id"
                ),
            )
        else:
            order_type = draft.get("order_type")
            if order_type not in {"MARKET", "LIMIT"}:
                raise WhiteBitManualLiveGatewayError(
                    "manual WhiteBIT order type must be MARKET or LIMIT"
                )
            quantity = parse_bounded_exact_decimal(draft.get("quantity"))
            price: Decimal | None = None
            if order_type == "LIMIT":
                price = parse_bounded_exact_decimal(draft.get("limit_price"))
            elif draft.get("limit_price") is not None:
                raise WhiteBitManualLiveGatewayError(
                    "market order cannot contain a limit price"
                )
            intent = WhiteBitOrderIntent(
                instrument_version=capability.instrument_version,
                product_family="SPOT",
                market=market,
                side=action,
                order_type=order_type,
                amount=quantity,
                price=price,
                time_in_force="GTC",
            )
            client_order_id = stable_client_order_id(
                "WHITEBIT",
                intent_id,
                environment="LIVE",
                account_id=self._handle.account_id,
                max_length=32,
                client_id_format="TOKEN",
            )
            prepared = prepare_order_request(
                intent,
                client_order_id=client_order_id,
                account_id=self._handle.account_id,
                environment="LIVE",
                capability=capability,
                market_rules=rules,
                at=point,
            )

        request = dict(prepared.to_guarded_dispatch_request())
        request["body"] = dict(request["body"])
        command_digest = "sha256:" + sha256(
            canonical_json(dict(command)).encode("utf-8")
        ).hexdigest()

        def authority_check(observed_digest: str, barrier_at: str) -> tuple[bool, str]:
            if observed_digest != command_digest:
                return False, "manual_command_digest_changed"
            barrier_point = datetime.fromisoformat(
                barrier_at.replace("Z", "+00:00")
            ).astimezone(timezone.utc)
            try:
                current_boundary = self._boundary_provider()
                _validate_binding(self._handle, current_boundary)
                current_capability = self._current_capability(
                    self._capability_provider, market, barrier_point
                )
                current_rules = self._market_rules_provider(market)
            except (OSError, RuntimeError, TypeError, ValueError):
                return False, "whitebit_live_authority_unavailable"
            if (
                current_boundary != boundary
                or current_capability.snapshot_id != capability.snapshot_id
                or current_rules != rules
            ):
                return False, "whitebit_live_authority_changed"
            return True, "whitebit_manual_live_authority_current"

        session_token = _exact_text(
            self._session_token_provider(), name="session_token"
        )
        origin = _exact_text(self._origin_provider(), name="origin")
        transport = WhiteBitHttpTransport(
            policy=WHITEBIT_ENDPOINT_POLICIES["LIVE"],
            account_id=self._handle.account_id,
            capability_snapshot_id=capability.snapshot_id,
            secret_resolver=self._security_boundary,
            credential_handle=self._handle,
            session_token=session_token,
            origin=origin,
            execution_identity=self._execution_identity,
            nonce_allocator=self._nonce_allocator,
            quota_gate=self._quota_gate,
            wire_client=self._wire_client,
        )
        outcome = self._dispatcher.dispatch(
            attempt_id="whitebit-manual-" + command_id,
            intent_id=intent_id,
            intent_hash=command_digest,
            provider="WHITEBIT",
            request=request,
            now=now,
            authority_check=authority_check,
            transport_send=transport,
            final_barrier_clock=lambda: _utc(
                self._clock_utc(), name="clock_utc"
            )[1],
            sender_check=lambda owner, epoch: (
                None
                if owner == self._dispatcher.owner_token and epoch == 1
                else (_ for _ in ()).throw(
                    PermissionError("manual WhiteBIT sender fence changed")
                )
            ),
            submission_scope={
                "provider": "WHITEBIT",
                "environment": "LIVE",
                "account_id": self._handle.account_id,
                "market": market,
                "product_family": "SPOT",
                "action": action,
                "capability_snapshot_id": capability.snapshot_id,
                "credential_binding_id": boundary.credential_binding_id,
                "credential_generation": boundary.credential_generation,
                "credential_evidence_ref": boundary.evidence_ref,
            },
        )
        return {
            "schema": "autotrade-whitebit-manual-live-result-v1",
            "outcome": outcome.status,
            "reason": outcome.reason,
            "client_order_id": outcome.client_order_id,
            "provider_send_may_have_occurred": outcome.status in {"SENT", "UNKNOWN"},
            "fill_confirmed": False,
            "requires_reconciliation": outcome.status == "UNKNOWN",
            "attempt_id": "whitebit-manual-" + command_id,
        }


def build_protected_whitebit_manual_live_gateway(
    journal: JournalStore,
    *,
    data_dir: str,
    credential_vault: object,
    security_boundary: SecurityBoundary,
    capability_provider: Callable[[str], CapabilitySnapshot],
    public_snapshot_provider: Callable[[str], Mapping[str, object]],
    session_token_provider: Callable[[], str],
    origin_provider: Callable[[], str],
    quota_gate: QuotaGate,
    clock_utc: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    clock_millis: Callable[[], int] | None = None,
    wire_client: ProviderWireClient | None = None,
) -> WhiteBitManualLiveGateway:
    """Load the protected binding and compose the product's canonical gateway.

    ``capability_provider`` must return a freshly derived, current-process
    admitted ``CapabilitySnapshot``.  This function deliberately cannot turn a
    persisted/self-authored dict into VERIFIED authority.
    """

    from .whitebit_trade_setup import load_whitebit_trade_binding
    from .windows_secrets import ProtectedCredentialVault

    if type(credential_vault) is not ProtectedCredentialVault:
        raise TypeError("credential_vault must be exact ProtectedCredentialVault")
    if not callable(public_snapshot_provider):
        raise TypeError("public_snapshot_provider must be callable")
    handle, binding = load_whitebit_trade_binding(
        credential_vault, data_dir=data_dir
    )
    return WhiteBitManualLiveGateway(
        journal,
        security_boundary=security_boundary,
        credential_handle=handle,
        credential_boundary_provider=lambda: load_whitebit_trade_binding(
            credential_vault, data_dir=data_dir
        )[1],
        capability_provider=capability_provider,
        market_rules_provider=lambda market: whitebit_spot_rules_from_public_snapshot(
            public_snapshot_provider(market)
        ),
        session_token_provider=session_token_provider,
        origin_provider=origin_provider,
        quota_gate=quota_gate,
        clock_utc=clock_utc,
        clock_millis=clock_millis,
        wire_client=wire_client,
    )


__all__ = [
    "SUPPORTED_WHITEBIT_SPOT_MARKETS",
    "WhiteBitManualLiveGateway",
    "WhiteBitManualLiveGatewayError",
    "build_protected_whitebit_manual_live_gateway",
    "whitebit_spot_rules_from_public_snapshot",
]
