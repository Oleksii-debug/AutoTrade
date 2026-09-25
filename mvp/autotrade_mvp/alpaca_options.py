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
from typing import Mapping

from .alpaca import AlpacaAdapterError, AlpacaOrderIntent


def _instant(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise AlpacaAdapterError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AlpacaAdapterError(f"{name} is required")
    return value.strip()


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
            and self.observed_at <= point <= self.expires_at
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
    activity_type: str
    symbol: str
    observed_at: datetime
    effective_at: datetime
    provider_activity_id: str
    source_sha256: str

    def __post_init__(self) -> None:
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
        object.__setattr__(self, "activity_type", activity_type)
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "effective_at", effective_at)
        object.__setattr__(self, "provider_activity_id", provider_activity_id)
        object.__setattr__(self, "source_sha256", digest)


def parse_polled_option_activity(payload: Mapping[str, object], *, observed_at: datetime) -> AlpacaOptionLifecycleObservation:
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
        activity_type=mapping[activity_type],
        symbol=symbol,
        observed_at=observed_at,
        effective_at=effective,
        provider_activity_id=activity_id,
        source_sha256=sha256(encoded).hexdigest(),
    )
