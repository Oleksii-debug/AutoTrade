"""Fail-closed admission gate for a bounded-real trial.

This module cannot transmit orders.  It only verifies that a single requested action
fits an explicit, expiring owner-approved envelope after release and forward evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json


def _dec(value, *, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} requires exact decimal input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{name} must be decimal") from error
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


def _ts(value: str, *, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ValueError(f"{name} must be ISO-8601") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{name} requires timezone")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class BoundedRealEnvelope:
    authorization_id: str
    owner_confirmation_id: str
    owner_confirmed: bool
    release_candidate_hash: str
    forward_evidence_hash: str
    provider_id: str
    account_fingerprint: str
    allowed_assets: frozenset[str]
    max_order_notional: Decimal | str | int
    max_total_notional: Decimal | str | int
    max_daily_loss: Decimal | str | int
    max_orders: int
    expires_at: str

    def validate(self) -> None:
        strings = (
            self.authorization_id,
            self.owner_confirmation_id,
            self.release_candidate_hash,
            self.forward_evidence_hash,
            self.provider_id,
            self.account_fingerprint,
        )
        if any(not isinstance(value, str) or not value.strip() for value in strings):
            raise ValueError("all authorization identities are required")
        if self.owner_confirmed is not True:
            raise ValueError("explicit owner confirmation is required")
        if not self.allowed_assets:
            raise ValueError("at least one allowed asset is required")
        if self.max_orders <= 0:
            raise ValueError("max_orders must be positive")
        order_cap = _dec(self.max_order_notional, name="max_order_notional")
        total_cap = _dec(self.max_total_notional, name="max_total_notional")
        loss_cap = _dec(self.max_daily_loss, name="max_daily_loss")
        if order_cap <= 0 or total_cap <= 0 or loss_cap <= 0:
            raise ValueError("financial caps must be positive")
        if order_cap > total_cap:
            raise ValueError("per-order cap cannot exceed total cap")
        _ts(self.expires_at, name="expires_at")

    def fingerprint(self) -> str:
        self.validate()
        payload = {
            "authorization_id": self.authorization_id,
            "owner_confirmation_id": self.owner_confirmation_id,
            "release_candidate_hash": self.release_candidate_hash,
            "forward_evidence_hash": self.forward_evidence_hash,
            "provider_id": self.provider_id,
            "account_fingerprint": self.account_fingerprint,
            "allowed_assets": sorted(self.allowed_assets),
            "max_order_notional": str(_dec(self.max_order_notional, name="max_order_notional")),
            "max_total_notional": str(_dec(self.max_total_notional, name="max_total_notional")),
            "max_daily_loss": str(_dec(self.max_daily_loss, name="max_daily_loss")),
            "max_orders": self.max_orders,
            "expires_at": self.expires_at,
        }
        return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class BoundedRealState:
    provider_id: str
    account_fingerprint: str
    current_total_notional: Decimal | str | int
    realized_daily_loss: Decimal | str | int
    orders_already_sent: int
    reconciliation_fresh: bool
    kill_switch_ready: bool
    release_qualified: bool
    forward_qualified: bool


@dataclass(frozen=True)
class BoundedRealRequest:
    asset_family: str
    requested_notional: Decimal | str | int
    now: str


@dataclass(frozen=True)
class BoundedRealAdmission:
    allowed: bool
    reasons: tuple[str, ...]
    envelope_fingerprint: str


def admit_bounded_real(
    envelope: BoundedRealEnvelope,
    state: BoundedRealState,
    request: BoundedRealRequest,
) -> BoundedRealAdmission:
    envelope.validate()
    now = _ts(request.now, name="now")
    reasons: list[str] = []
    if now >= _ts(envelope.expires_at, name="expires_at"):
        reasons.append("authorization_expired")
    if state.provider_id != envelope.provider_id:
        reasons.append("provider_mismatch")
    if state.account_fingerprint != envelope.account_fingerprint:
        reasons.append("account_mismatch")
    if request.asset_family not in envelope.allowed_assets:
        reasons.append("asset_not_authorized")
    amount = _dec(request.requested_notional, name="requested_notional")
    if amount <= 0:
        reasons.append("requested_notional_not_positive")
    if amount > _dec(envelope.max_order_notional, name="max_order_notional"):
        reasons.append("per_order_cap_exceeded")
    current = _dec(state.current_total_notional, name="current_total_notional")
    if current < 0:
        reasons.append("invalid_current_total_notional")
    elif current + max(amount, Decimal("0")) > _dec(envelope.max_total_notional, name="max_total_notional"):
        reasons.append("total_notional_cap_exceeded")
    loss = _dec(state.realized_daily_loss, name="realized_daily_loss")
    if loss < 0:
        reasons.append("daily_loss_must_be_nonnegative_magnitude")
    elif loss >= _dec(envelope.max_daily_loss, name="max_daily_loss"):
        reasons.append("daily_loss_cap_reached")
    if state.orders_already_sent < 0 or state.orders_already_sent >= envelope.max_orders:
        reasons.append("order_count_cap_reached")
    if not state.reconciliation_fresh:
        reasons.append("reconciliation_not_fresh")
    if not state.kill_switch_ready:
        reasons.append("kill_switch_not_ready")
    if not state.release_qualified:
        reasons.append("release_not_qualified")
    if not state.forward_qualified:
        reasons.append("forward_not_qualified")
    return BoundedRealAdmission(
        allowed=not reasons,
        reasons=tuple(reasons),
        envelope_fingerprint=envelope.fingerprint(),
    )
