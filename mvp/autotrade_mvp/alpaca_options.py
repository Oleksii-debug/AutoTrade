"""Alpaca option-account entitlement and lifecycle evidence.

This module does not send provider requests. It binds option intent admission to
fresh account evidence and models polled lifecycle observations separately from
order-stream acknowledgement.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any, Mapping
from uuid import NAMESPACE_URL, uuid5

from .alpaca import AlpacaAdapterError, AlpacaOrderIntent
from .persistence import JournalStore, payload_digest


def _instant(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise AlpacaAdapterError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AlpacaAdapterError(f"{name} is required")
    return value.strip()


def _instant_text(value: datetime, *, name: str) -> str:
    return _instant(value, name=name).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class AlpacaOptionAccountEvidence:
    account_id: str
    environment: str
    observed_at: datetime
    expires_at: datetime
    options_trading_level: int
    trading_blocked: bool
    account_blocked: bool
    source_sha256: str

    def __post_init__(self) -> None:
        account_id = _text(self.account_id, name="account_id")
        environment = _text(self.environment, name="environment").upper()
        if environment not in {"PAPER", "LIVE"}:
            raise AlpacaAdapterError("environment must be PAPER or LIVE")
        observed_at = _instant(self.observed_at, name="observed_at")
        expires_at = _instant(self.expires_at, name="expires_at")
        if expires_at <= observed_at:
            raise AlpacaAdapterError("expires_at must be after observed_at")
        if isinstance(self.options_trading_level, bool) or not isinstance(self.options_trading_level, int):
            raise AlpacaAdapterError("options_trading_level must be an integer")
        if self.options_trading_level < 0 or self.options_trading_level > 3:
            raise AlpacaAdapterError("options_trading_level must be between 0 and 3")
        if type(self.trading_blocked) is not bool or type(self.account_blocked) is not bool:
            raise AlpacaAdapterError("account block flags must be boolean")
        digest = _text(self.source_sha256, name="source_sha256").lower()
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise AlpacaAdapterError("source_sha256 must be 64 hex characters")
        object.__setattr__(self, "account_id", account_id)
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(self, "source_sha256", digest)

    def admits(self, *, required_level: int, at: datetime) -> bool:
        point = _instant(at, name="at")
        if isinstance(required_level, bool) or not isinstance(required_level, int):
            raise AlpacaAdapterError("required_level must be an integer")
        if required_level < 1 or required_level > 3:
            raise AlpacaAdapterError("required_level must be between 1 and 3")
        return (
            not self.trading_blocked
            and not self.account_blocked
            and self.observed_at <= point < self.expires_at
            and self.options_trading_level >= required_level
        )


def require_option_entitlement(
    intent: AlpacaOrderIntent,
    *,
    account: AlpacaOptionAccountEvidence,
    account_id: str,
    environment: str,
    required_level: int,
    at: datetime,
) -> AlpacaOrderIntent:
    """Return the same intent only when exact scoped account evidence admits it."""

    if not isinstance(intent, AlpacaOrderIntent):
        raise TypeError("intent must be AlpacaOrderIntent")
    if intent.asset_class != "OPTION":
        raise AlpacaAdapterError("option entitlement can only qualify OPTION intents")
    if not isinstance(account, AlpacaOptionAccountEvidence):
        raise TypeError("account must be AlpacaOptionAccountEvidence")
    expected_account = _text(account_id, name="account_id")
    expected_environment = _text(environment, name="environment").upper()
    if expected_environment not in {"PAPER", "LIVE"}:
        raise AlpacaAdapterError("environment must be PAPER or LIVE")
    if account.account_id != expected_account:
        raise AlpacaAdapterError("Alpaca option entitlement account_id mismatch")
    if account.environment != expected_environment:
        raise AlpacaAdapterError("Alpaca option entitlement environment mismatch")
    if not account.admits(required_level=required_level, at=at):
        raise AlpacaAdapterError("fresh Alpaca account evidence does not admit this option intent")
    return intent


@dataclass(frozen=True)
class AlpacaOptionLifecycleObservation:
    account_id: str
    environment: str
    activity_type: str
    symbol: str
    observed_at: datetime
    effective_at: datetime
    provider_activity_id: str
    source_sha256: str

    def __post_init__(self) -> None:
        account_id = _text(self.account_id, name="account_id")
        environment = _text(self.environment, name="environment").upper()
        if environment not in {"PAPER", "LIVE"}:
            raise AlpacaAdapterError("environment must be PAPER or LIVE")
        activity_type = _text(self.activity_type, name="activity_type").upper()
        if activity_type not in {"OPTION_EXERCISE", "OPTION_ASSIGNMENT", "OPTION_EXPIRATION"}:
            raise AlpacaAdapterError("unsupported option lifecycle activity")
        symbol = _text(self.symbol, name="symbol").upper()
        observed_at = _instant(self.observed_at, name="observed_at")
        effective_at = _instant(self.effective_at, name="effective_at")
        if effective_at > observed_at:
            raise AlpacaAdapterError("effective_at cannot be after observed_at")
        provider_activity_id = _text(self.provider_activity_id, name="provider_activity_id")
        digest = _text(self.source_sha256, name="source_sha256").lower()
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise AlpacaAdapterError("source_sha256 must be 64 hex characters")
        object.__setattr__(self, "account_id", account_id)
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "activity_type", activity_type)
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "effective_at", effective_at)
        object.__setattr__(self, "provider_activity_id", provider_activity_id)
        object.__setattr__(self, "source_sha256", digest)


def parse_polled_option_activity(
    payload: Mapping[str, object],
    *,
    account_id: str,
    environment: str,
    observed_at: datetime,
) -> AlpacaOptionLifecycleObservation:
    """Parse a separately polled account activity.

    Order-stream absence is never interpreted as lifecycle absence.
    """

    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    activity_type = _text(payload.get("activity_type"), name="activity_type").upper()
    mapping = {
        "OPEXC": "OPTION_EXERCISE",
        "OPASN": "OPTION_ASSIGNMENT",
        "OPEXP": "OPTION_EXPIRATION",
        "OPTION_EXERCISE": "OPTION_EXERCISE",
        "OPTION_ASSIGNMENT": "OPTION_ASSIGNMENT",
        "OPTION_EXPIRATION": "OPTION_EXPIRATION",
    }
    if activity_type not in mapping:
        raise AlpacaAdapterError("unsupported polled Alpaca option activity")
    symbol = _text(payload.get("symbol"), name="symbol")
    activity_id = _text(payload.get("id"), name="id")
    effective_raw = _text(payload.get("transaction_time"), name="transaction_time")
    try:
        effective = datetime.fromisoformat(effective_raw.replace("Z", "+00:00"))
    except ValueError as error:
        raise AlpacaAdapterError("transaction_time must be ISO timestamp") from error
    if effective.tzinfo is None:
        raise AlpacaAdapterError("transaction_time must include timezone")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    return AlpacaOptionLifecycleObservation(
        account_id=_text(account_id, name="account_id"),
        environment=_text(environment, name="environment").upper(),
        activity_type=mapping[activity_type],
        symbol=symbol,
        observed_at=observed_at,
        effective_at=effective,
        provider_activity_id=activity_id,
        source_sha256=sha256(encoded).hexdigest(),
    )


_ALPACA_OPTION_LIFECYCLE_AGGREGATE_TYPE = "alpaca_option_lifecycle_obligation"
_ALPACA_OPTION_LIFECYCLE_OUTBOX_TOPIC = "autotrade.alpaca.option-lifecycle.events"


@dataclass(frozen=True)
class AlpacaOptionLifecycleObligation:
    """Durable unresolved/resolved lifecycle evidence for one economic change.

    This is reconciliation evidence only. It never applies position/cash effects
    and therefore cannot double-apply provider economics when delayed activities
    arrive or are replayed after restart.
    """

    economic_change_id: str
    account_id: str
    environment: str
    symbol: str
    economic_effect_observed_at: datetime
    status: str
    provider_activity_id: str | None = None
    activity_type: str | None = None
    activity_source_sha256: str | None = None
    activity_effective_at: datetime | None = None
    activity_observed_at: datetime | None = None

    def __post_init__(self) -> None:
        economic_change_id = _text(
            self.economic_change_id, name="economic_change_id"
        )
        account_id = _text(self.account_id, name="account_id")
        environment = _text(self.environment, name="environment").upper()
        if environment not in {"PAPER", "LIVE"}:
            raise AlpacaAdapterError("environment must be PAPER or LIVE")
        symbol = _text(self.symbol, name="symbol").upper()
        observed = _instant(
            self.economic_effect_observed_at,
            name="economic_effect_observed_at",
        )
        status = _text(self.status, name="status").upper()
        if status not in {"PROVISIONAL", "RESOLVED"}:
            raise AlpacaAdapterError(
                "lifecycle obligation status must be PROVISIONAL or RESOLVED"
            )

        activity_fields = (
            self.provider_activity_id,
            self.activity_type,
            self.activity_source_sha256,
            self.activity_effective_at,
            self.activity_observed_at,
        )
        if status == "PROVISIONAL":
            if any(value is not None for value in activity_fields):
                raise AlpacaAdapterError(
                    "provisional lifecycle obligation cannot claim provider activity"
                )
        else:
            if any(value is None for value in activity_fields):
                raise AlpacaAdapterError(
                    "resolved lifecycle obligation requires complete provider activity"
                )
            activity_type = _text(
                self.activity_type, name="activity_type"
            ).upper()
            if activity_type not in {
                "OPTION_EXERCISE",
                "OPTION_ASSIGNMENT",
                "OPTION_EXPIRATION",
            }:
                raise AlpacaAdapterError(
                    "resolved lifecycle obligation has unsupported activity type"
                )
            digest = _text(
                self.activity_source_sha256,
                name="activity_source_sha256",
            ).lower()
            if len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest
            ):
                raise AlpacaAdapterError(
                    "activity_source_sha256 must be 64 hex characters"
                )
            activity_effective_at = _instant(
                self.activity_effective_at,
                name="activity_effective_at",
            )
            activity_observed_at = _instant(
                self.activity_observed_at,
                name="activity_observed_at",
            )
            if activity_effective_at > activity_observed_at:
                raise AlpacaAdapterError(
                    "activity_effective_at cannot be after activity_observed_at"
                )
            object.__setattr__(
                self,
                "provider_activity_id",
                _text(self.provider_activity_id, name="provider_activity_id"),
            )
            object.__setattr__(self, "activity_type", activity_type)
            object.__setattr__(self, "activity_source_sha256", digest)
            object.__setattr__(
                self, "activity_effective_at", activity_effective_at
            )
            object.__setattr__(
                self, "activity_observed_at", activity_observed_at
            )

        object.__setattr__(self, "economic_change_id", economic_change_id)
        object.__setattr__(self, "account_id", account_id)
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(
            self, "economic_effect_observed_at", observed
        )
        object.__setattr__(self, "status", status)

    @property
    def unresolved(self) -> bool:
        return self.status == "PROVISIONAL"


def classify_option_lifecycle_evidence(
    *,
    order_stream_quiet: bool,
    activity: AlpacaOptionLifecycleObservation | None,
) -> str:
    """Never infer option lifecycle absence from a quiet order stream.

    Alpaca option assignment is discovered via polled account activities; PAPER
    activity publication may lag the economic position/balance change. Therefore
    stream silence plus no activity is INCONCLUSIVE, not proof of absence.
    """

    if type(order_stream_quiet) is not bool:
        raise TypeError("order_stream_quiet must be boolean")
    if activity is None:
        return "INCONCLUSIVE"
    if not isinstance(activity, AlpacaOptionLifecycleObservation):
        raise TypeError("activity must be AlpacaOptionLifecycleObservation")
    return "OBSERVED"


def _lifecycle_aggregate_id(
    *,
    economic_change_id: str,
    account_id: str,
    environment: str,
) -> str:
    payload = {
        "provider_id": "ALPACA",
        "economic_change_id": _text(
            economic_change_id, name="economic_change_id"
        ),
        "account_id": _text(account_id, name="account_id"),
        "environment": _text(environment, name="environment").upper(),
    }
    if payload["environment"] not in {"PAPER", "LIVE"}:
        raise AlpacaAdapterError("environment must be PAPER or LIVE")
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + sha256(encoded).hexdigest()


def _open_payload(
    *,
    economic_change_id: str,
    account_id: str,
    environment: str,
    symbol: str,
    economic_effect_observed_at: datetime,
) -> dict[str, str]:
    return {
        "provider_id": "ALPACA",
        "economic_change_id": _text(
            economic_change_id, name="economic_change_id"
        ),
        "account_id": _text(account_id, name="account_id"),
        "environment": _text(environment, name="environment").upper(),
        "symbol": _text(symbol, name="symbol").upper(),
        "economic_effect_observed_at": _instant_text(
            economic_effect_observed_at,
            name="economic_effect_observed_at",
        ),
        "status": "PROVISIONAL",
        "reason": "economic_change_without_matching_option_activity",
    }


def _attachment_payload(
    *,
    economic_change_id: str,
    observation: AlpacaOptionLifecycleObservation,
) -> dict[str, str]:
    if not isinstance(observation, AlpacaOptionLifecycleObservation):
        raise TypeError("observation must be AlpacaOptionLifecycleObservation")
    return {
        "provider_id": "ALPACA",
        "economic_change_id": _text(
            economic_change_id, name="economic_change_id"
        ),
        "account_id": observation.account_id,
        "environment": observation.environment,
        "symbol": observation.symbol,
        "provider_activity_id": observation.provider_activity_id,
        "activity_type": observation.activity_type,
        "activity_source_sha256": observation.source_sha256,
        "activity_effective_at": _instant_text(
            observation.effective_at, name="activity_effective_at"
        ),
        "activity_observed_at": _instant_text(
            observation.observed_at, name="activity_observed_at"
        ),
        "status": "RESOLVED",
    }


def _append_lifecycle_event(
    store: JournalStore,
    *,
    aggregate_id: str,
    event_type: str,
    payload: Mapping[str, Any],
    committed_at: datetime,
    host_id: str,
    owner_epoch: str,
) -> None:
    if not isinstance(store, JournalStore):
        raise TypeError("store must be JournalStore")
    version = store.next_aggregate_version(
        _ALPACA_OPTION_LIFECYCLE_AGGREGATE_TYPE,
        aggregate_id,
    )
    committed = _instant_text(committed_at, name="committed_at")
    digest = payload_digest(dict(payload))
    event_id = str(
        uuid5(
            NAMESPACE_URL,
            "https://events.autotrade.local/alpaca-option-lifecycle/"
            f"{aggregate_id}/{version}/{digest}",
        )
    )
    envelope = {
        "event_id": event_id,
        "event_type": event_type,
        "schema_version": "1.0.0",
        "aggregate_type": _ALPACA_OPTION_LIFECYCLE_AGGREGATE_TYPE,
        "aggregate_id": aggregate_id,
        "aggregate_version": str(version),
        "host_id": _text(host_id, name="host_id"),
        "owner_epoch": _text(owner_epoch, name="owner_epoch"),
        "environment": _text(
            payload.get("environment"), name="environment"
        ).upper(),
        "occurred_at": committed,
        "observed_at": committed,
        "committed_at": committed,
        "correlation_id": event_id,
        "causation_id": None,
        "payload": dict(payload),
        "payload_hash": digest,
        "evidence_refs": [],
    }
    store.append_event(
        envelope,
        outbox_topic=_ALPACA_OPTION_LIFECYCLE_OUTBOX_TOPIC,
    )


def _replay_lifecycle_obligation(
    events: list[dict[str, Any]],
) -> AlpacaOptionLifecycleObligation | None:
    if not events:
        return None
    opened: dict[str, Any] | None = None
    attached: dict[str, Any] | None = None
    expected_version = 1
    for event in events:
        if event.get("aggregate_version") != expected_version:
            raise AlpacaAdapterError(
                "Alpaca lifecycle obligation journal versions are not contiguous"
            )
        expected_version += 1
        payload = event.get("payload")
        if not isinstance(payload, dict):
            raise AlpacaAdapterError(
                "Alpaca lifecycle obligation payload must be an object"
            )
        if event.get("payload_hash") != payload_digest(payload):
            raise AlpacaAdapterError(
                "Alpaca lifecycle obligation payload hash mismatch"
            )
        event_type = event.get("event_type")
        if event_type == "OptionLifecycleObligationOpened":
            if opened is not None or event.get("aggregate_version") != 1:
                raise AlpacaAdapterError(
                    "Alpaca lifecycle obligation has invalid opening event"
                )
            expected = {
                "provider_id",
                "economic_change_id",
                "account_id",
                "environment",
                "symbol",
                "economic_effect_observed_at",
                "status",
                "reason",
            }
            if set(payload) != expected:
                raise AlpacaAdapterError(
                    "Alpaca lifecycle opening payload fields mismatch"
                )
            if (
                payload.get("provider_id") != "ALPACA"
                or payload.get("status") != "PROVISIONAL"
                or payload.get("reason")
                != "economic_change_without_matching_option_activity"
            ):
                raise AlpacaAdapterError(
                    "Alpaca lifecycle opening payload is not canonical"
                )
            opened = payload
        elif event_type == "OptionLifecycleActivityAttached":
            if opened is None or attached is not None:
                raise AlpacaAdapterError(
                    "Alpaca lifecycle obligation has invalid attachment event"
                )
            expected = {
                "provider_id",
                "economic_change_id",
                "account_id",
                "environment",
                "symbol",
                "provider_activity_id",
                "activity_type",
                "activity_source_sha256",
                "activity_effective_at",
                "activity_observed_at",
                "status",
            }
            if set(payload) != expected:
                raise AlpacaAdapterError(
                    "Alpaca lifecycle attachment payload fields mismatch"
                )
            if payload.get("provider_id") != "ALPACA" or payload.get(
                "status"
            ) != "RESOLVED":
                raise AlpacaAdapterError(
                    "Alpaca lifecycle attachment payload is not canonical"
                )
            for field in (
                "economic_change_id",
                "account_id",
                "environment",
                "symbol",
            ):
                if payload.get(field) != opened.get(field):
                    raise AlpacaAdapterError(
                        f"Alpaca lifecycle attachment {field} mismatch"
                    )
            attached = payload
        else:
            raise AlpacaAdapterError(
                "Alpaca lifecycle obligation journal contains unsupported event type"
            )

    if opened is None:
        raise AlpacaAdapterError(
            "Alpaca lifecycle obligation journal lacks opening event"
        )
    observed_at = datetime.fromisoformat(
        str(opened["economic_effect_observed_at"]).replace("Z", "+00:00")
    )
    if attached is None:
        return AlpacaOptionLifecycleObligation(
            economic_change_id=opened["economic_change_id"],
            account_id=opened["account_id"],
            environment=opened["environment"],
            symbol=opened["symbol"],
            economic_effect_observed_at=observed_at,
            status="PROVISIONAL",
        )
    return AlpacaOptionLifecycleObligation(
        economic_change_id=opened["economic_change_id"],
        account_id=opened["account_id"],
        environment=opened["environment"],
        symbol=opened["symbol"],
        economic_effect_observed_at=observed_at,
        status="RESOLVED",
        provider_activity_id=attached["provider_activity_id"],
        activity_type=attached["activity_type"],
        activity_source_sha256=attached["activity_source_sha256"],
        activity_effective_at=datetime.fromisoformat(
            str(attached["activity_effective_at"]).replace("Z", "+00:00")
        ),
        activity_observed_at=datetime.fromisoformat(
            str(attached["activity_observed_at"]).replace("Z", "+00:00")
        ),
    )


def load_option_lifecycle_obligation(
    store: JournalStore,
    *,
    economic_change_id: str,
    account_id: str,
    environment: str,
) -> AlpacaOptionLifecycleObligation | None:
    if not isinstance(store, JournalStore):
        raise TypeError("store must be JournalStore")
    aggregate_id = _lifecycle_aggregate_id(
        economic_change_id=economic_change_id,
        account_id=account_id,
        environment=environment,
    )
    return _replay_lifecycle_obligation(
        store.load_events(
            _ALPACA_OPTION_LIFECYCLE_AGGREGATE_TYPE,
            aggregate_id,
        )
    )


def open_provisional_option_lifecycle_obligation(
    store: JournalStore,
    *,
    economic_change_id: str,
    account_id: str,
    environment: str,
    symbol: str,
    economic_effect_observed_at: datetime,
    host_id: str,
    owner_epoch: str,
) -> AlpacaOptionLifecycleObligation:
    """Persist an unresolved lifecycle obligation without inventing a subtype."""

    if not isinstance(store, JournalStore):
        raise TypeError("store must be JournalStore")
    payload = _open_payload(
        economic_change_id=economic_change_id,
        account_id=account_id,
        environment=environment,
        symbol=symbol,
        economic_effect_observed_at=economic_effect_observed_at,
    )
    aggregate_id = _lifecycle_aggregate_id(
        economic_change_id=payload["economic_change_id"],
        account_id=payload["account_id"],
        environment=payload["environment"],
    )
    existing_events = store.load_events(
        _ALPACA_OPTION_LIFECYCLE_AGGREGATE_TYPE,
        aggregate_id,
    )
    existing = _replay_lifecycle_obligation(existing_events)
    if existing is not None:
        if (
            existing.symbol != payload["symbol"]
            or _instant_text(
                existing.economic_effect_observed_at,
                name="economic_effect_observed_at",
            )
            != payload["economic_effect_observed_at"]
        ):
            raise AlpacaAdapterError(
                "economic_change_id conflicts with existing lifecycle obligation"
            )
        return existing

    _append_lifecycle_event(
        store,
        aggregate_id=aggregate_id,
        event_type="OptionLifecycleObligationOpened",
        payload=payload,
        committed_at=economic_effect_observed_at,
        host_id=host_id,
        owner_epoch=owner_epoch,
    )
    result = load_option_lifecycle_obligation(
        store,
        economic_change_id=payload["economic_change_id"],
        account_id=payload["account_id"],
        environment=payload["environment"],
    )
    if result is None:
        raise RuntimeError("Alpaca lifecycle obligation was not persisted")
    return result


def _reject_reused_provider_activity(
    store: JournalStore,
    *,
    aggregate_id: str,
    observation: AlpacaOptionLifecycleObservation,
) -> None:
    for event in store.load_events_by_aggregate_type(
        _ALPACA_OPTION_LIFECYCLE_AGGREGATE_TYPE
    ):
        if event.get("event_type") != "OptionLifecycleActivityAttached":
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            raise AlpacaAdapterError(
                "Alpaca lifecycle attachment payload must be an object"
            )
        if (
            payload.get("provider_activity_id")
            == observation.provider_activity_id
            and payload.get("account_id") == observation.account_id
            and payload.get("environment") == observation.environment
            and event.get("aggregate_id") != aggregate_id
        ):
            raise AlpacaAdapterError(
                "provider activity already resolves another economic change"
            )


def attach_polled_option_activity(
    store: JournalStore,
    *,
    economic_change_id: str,
    observation: AlpacaOptionLifecycleObservation,
    host_id: str,
    owner_epoch: str,
) -> AlpacaOptionLifecycleObligation:
    """Bind delayed provider activity idempotently to one existing economic change."""

    if not isinstance(store, JournalStore):
        raise TypeError("store must be JournalStore")
    if not isinstance(observation, AlpacaOptionLifecycleObservation):
        raise TypeError("observation must be AlpacaOptionLifecycleObservation")
    aggregate_id = _lifecycle_aggregate_id(
        economic_change_id=economic_change_id,
        account_id=observation.account_id,
        environment=observation.environment,
    )
    events = store.load_events(
        _ALPACA_OPTION_LIFECYCLE_AGGREGATE_TYPE,
        aggregate_id,
    )
    current = _replay_lifecycle_obligation(events)
    if current is None:
        raise AlpacaAdapterError(
            "provider activity cannot resolve a missing lifecycle obligation"
        )
    if current.account_id != observation.account_id:
        raise AlpacaAdapterError(
            "provider activity account_id differs from lifecycle obligation"
        )
    if current.environment != observation.environment:
        raise AlpacaAdapterError(
            "provider activity environment differs from lifecycle obligation"
        )
    if current.symbol != observation.symbol:
        raise AlpacaAdapterError(
            "provider activity symbol differs from lifecycle obligation"
        )

    expected_payload = _attachment_payload(
        economic_change_id=economic_change_id,
        observation=observation,
    )
    if current.status == "RESOLVED":
        existing_payload = events[-1].get("payload")
        if existing_payload == expected_payload:
            return current
        raise AlpacaAdapterError(
            "lifecycle obligation is already resolved by different provider activity"
        )

    _reject_reused_provider_activity(
        store,
        aggregate_id=aggregate_id,
        observation=observation,
    )
    _append_lifecycle_event(
        store,
        aggregate_id=aggregate_id,
        event_type="OptionLifecycleActivityAttached",
        payload=expected_payload,
        committed_at=observation.observed_at,
        host_id=host_id,
        owner_epoch=owner_epoch,
    )
    result = load_option_lifecycle_obligation(
        store,
        economic_change_id=economic_change_id,
        account_id=observation.account_id,
        environment=observation.environment,
    )
    if result is None or result.status != "RESOLVED":
        raise RuntimeError("Alpaca lifecycle activity attachment was not persisted")
    return result
