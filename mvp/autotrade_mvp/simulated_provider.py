"""Deterministic contract-shaped simulated provider for AutoTrade."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from typing import Any, Mapping
from uuid import NAMESPACE_URL, UUID, uuid5


class SimulatedProviderConflict(ValueError):
    """Raised when an immutable simulated provider identity is reused."""


def _decimal(value, *, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use Decimal, string or integer input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return result


def _positive(value, *, name: str) -> Decimal:
    result = _decimal(value, name=name)
    if result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _instant(value: str, *, name: str = "timestamp") -> datetime:
    text = _text(value, name=name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc)


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("decimal must be finite")
    if value == 0:
        return "0"
    rendered = format(value.normalize(), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _uuid(namespace: str, key: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"https://sim.autotrade.local/{namespace}/{key}"))


def _unit_id(instrument_version: str) -> str:
    digest = sha256(instrument_version.encode("utf-8")).hexdigest()[:16]
    return f"unit:contract:{digest}"


def _evidence(kind: str, key: str, observed_at: str, payload: Any) -> dict[str, str]:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return {
        "artifact_id": _uuid("evidence", f"{kind}:{key}"),
        "sha256": "sha256:" + sha256(encoded).hexdigest(),
        "source_uri": f"https://sim.autotrade.local/evidence/{kind}/{key}",
        "observed_at": observed_at,
        "rights_id": "simulated-first-party",
    }


@dataclass(frozen=True)
class SimulatedOrder:
    attempt_id: str
    client_order_id: str
    provider_order_id: str
    instrument_version: str
    side: str
    quantity: Decimal
    price: Decimal
    submitted_at: str


class SimulatedProvider:
    """Network-free deterministic provider with exact contract-shaped outputs."""

    def __init__(
        self,
        *,
        account_id: str = "sim-account",
        currency: str = "USD",
        initial_cash="100000",
        fee_rate="0.001",
    ) -> None:
        self.account_id = _text(account_id, name="account_id")
        self.currency = _text(currency, name="currency").upper()
        self.initial_cash = _decimal(initial_cash, name="initial_cash")
        self.fee_rate = _decimal(fee_rate, name="fee_rate")
        if self.initial_cash < 0 or self.fee_rate < 0:
            raise ValueError("initial_cash and fee_rate must be non-negative")
        self.cash = self.initial_cash
        self.positions: dict[str, Decimal] = {}
        self.orders: dict[str, SimulatedOrder] = {}
        self._attempts: dict[str, SimulatedOrder] = {}
        self.fills: list[dict[str, Any]] = []
        self.outbound_request_count = 0

    @staticmethod
    def _validate_attempt_id(value: str) -> str:
        text = _text(value, name="attempt_id")
        try:
            UUID(text)
        except ValueError as error:
            raise ValueError("attempt_id must be a UUID") from error
        return text

    def submit_order(
        self,
        *,
        attempt_id: str,
        client_order_id: str,
        instrument_version: str,
        side: str,
        quantity,
        price,
        now: str,
        fill_immediately: bool = True,
    ) -> dict[str, Any]:
        aid = self._validate_attempt_id(attempt_id)
        cid = _text(client_order_id, name="client_order_id")
        instrument = _text(instrument_version, name="instrument_version")
        normalized_side = _text(side, name="side").upper()
        if normalized_side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        qty = _positive(quantity, name="quantity")
        px = _positive(price, name="price")
        _instant(now, name="now")
        provider_order_id = "sim-" + sha256(cid.encode("utf-8")).hexdigest()[:24]
        proposed = SimulatedOrder(
            attempt_id=aid,
            client_order_id=cid,
            provider_order_id=provider_order_id,
            instrument_version=instrument,
            side=normalized_side,
            quantity=qty,
            price=px,
            submitted_at=now,
        )

        prior_attempt = self._attempts.get(aid)
        if prior_attempt is not None:
            if prior_attempt != proposed:
                raise SimulatedProviderConflict(
                    "attempt_id already has different simulated order content"
                )
            return self._submission_result(prior_attempt, now)

        prior_client = self.orders.get(cid)
        if prior_client is not None:
            if prior_client != proposed:
                raise SimulatedProviderConflict(
                    "client_order_id already has different simulated order content"
                )
            self._attempts[aid] = prior_client
            return self._submission_result(prior_client, now)

        self.orders[cid] = proposed
        self._attempts[aid] = proposed
        if fill_immediately:
            self._record_fill(proposed, now)
        return self._submission_result(proposed, now)

    def _submission_result(
        self, order: SimulatedOrder, now: str
    ) -> dict[str, Any]:
        payload = {
            "attempt_id": order.attempt_id,
            "provider_order_id": order.provider_order_id,
            "client_order_id": order.client_order_id,
            "provider_received_at": now,
        }
        return {
            **payload,
            "outcome": "ACKNOWLEDGED",
            "evidence": [
                _evidence(
                    "submission",
                    order.attempt_id,
                    now,
                    payload,
                )
            ],
            "retry_disposition": "NEVER",
        }

    def _record_fill(self, order: SimulatedOrder, now: str) -> dict[str, Any]:
        fill_id = _uuid("fill", order.provider_order_id)
        provider_execution_id = "exec-" + sha256(
            order.provider_order_id.encode("utf-8")
        ).hexdigest()[:24]
        if any(
            item["provider_execution_id"] == provider_execution_id
            for item in self.fills
        ):
            return next(
                item
                for item in self.fills
                if item["provider_execution_id"] == provider_execution_id
            )

        notional = order.quantity * order.price
        fee = abs(notional) * self.fee_rate
        signed_quantity = (
            order.quantity if order.side == "BUY" else -order.quantity
        )
        cash_delta = (
            -notional - fee if order.side == "BUY" else notional - fee
        )
        self.cash += cash_delta
        self.positions[order.instrument_version] = (
            self.positions.get(order.instrument_version, Decimal("0"))
            + signed_quantity
        )
        payload = {
            "fill_id": fill_id,
            "provider_execution_id": provider_execution_id,
            "order_ref": order.provider_order_id,
            "intent_ref": None,
            "instrument_version": order.instrument_version,
            "side": order.side,
            "last_quantity": {
                "value": _decimal_text(order.quantity),
                "unit": _unit_id(order.instrument_version),
            },
            "last_price": _decimal_text(order.price),
            "trade_time": now,
            "receipt_time": now,
            "fees": [
                {
                    "amount": _decimal_text(fee),
                    "currency": self.currency,
                }
            ],
            "settlement_date": _instant(now).date().isoformat(),
        }
        fill = {
            **payload,
            "evidence": [
                _evidence(
                    "fill",
                    provider_execution_id,
                    now,
                    payload,
                )
            ],
        }
        self.fills.append(fill)
        return fill

    def transport_send(
        self,
        client_order_id: str,
        request: Mapping[str, Any],
        final_guard,
    ) -> dict[str, Any]:
        """Dispatcher-compatible transport; guard is immediately before send."""

        if not isinstance(request, Mapping):
            raise TypeError("request must be a mapping")
        final_guard()
        self.outbound_request_count += 1
        return self.submit_order(
            attempt_id=request["attempt_id"],
            client_order_id=client_order_id,
            instrument_version=request["instrument_version"],
            side=request["side"],
            quantity=request["quantity"],
            price=request["price"],
            now=request["now"],
            fill_immediately=request.get("fill_immediately", True),
        )

    def activity_fills(self) -> tuple[dict[str, Any], ...]:
        return tuple(self.fills)

    def account_snapshot(self, *, now: str) -> dict[str, Any]:
        _instant(now, name="now")
        balances = [
            {
                "currency": self.currency,
                "total": _decimal_text(self.cash),
                "available": _decimal_text(self.cash),
                "reserved": "0",
                "liability": "0",
                "as_of": now,
            }
        ]
        positions = [
            {
                "instrument_version": instrument,
                "position_side": "NET",
                "quantity": {
                    "value": _decimal_text(quantity),
                    "unit": _unit_id(instrument),
                },
                "source": "SIMULATED_PROVIDER",
                "as_of": now,
            }
            for instrument, quantity in sorted(self.positions.items())
            if quantity != 0
        ]
        open_orders: list[dict[str, Any]] = []
        core = {
            "account_id": self.account_id,
            "environment": "SIMULATION",
            "query_started_at": now,
            "query_completed_at": now,
            "provider_as_of": now,
            "consistency": "ATOMIC",
            "balances": balances,
            "positions": positions,
            "open_orders": open_orders,
            "margin": {
                "account_id": self.account_id,
                "as_of": now,
                "model_id": "sim-cash-v1",
                "source": "SIMULATED_PROVIDER",
                "quality": "DETERMINISTIC",
            },
        }
        snapshot_id = _uuid(
            "snapshot",
            sha256(
                json.dumps(
                    core,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        )
        return {
            "snapshot_id": snapshot_id,
            **core,
            "evidence": [
                _evidence("snapshot", snapshot_id, now, core)
            ],
        }

    def query_order(
        self,
        *,
        client_order_id: str,
        coverage_start: str,
        coverage_end: str,
        pagination_complete: bool,
        now: str,
    ) -> dict[str, Any]:
        cid = _text(client_order_id, name="client_order_id")
        start = _instant(coverage_start, name="coverage_start")
        end = _instant(coverage_end, name="coverage_end")
        _instant(now, name="now")
        if end < start:
            raise ValueError("coverage_end must not precede coverage_start")
        if not isinstance(pagination_complete, bool):
            raise TypeError("pagination_complete must be boolean")
        order = self.orders.get(cid)
        searched = ["orders-by-client-id", "activity-fills"]
        window = {
            "start": coverage_start,
            "end": coverage_end,
        }
        if order is not None:
            verdict = "FOUND"
            order_payload = {
                "provider_order_id": order.provider_order_id,
                "client_order_id": order.client_order_id,
                "instrument_version": order.instrument_version,
                "side": order.side,
                "quantity": _decimal_text(order.quantity),
                "price": _decimal_text(order.price),
                "submitted_at": order.submitted_at,
            }
        elif pagination_complete:
            verdict = "PROVEN_ABSENT"
            order_payload = None
        else:
            verdict = "INCONCLUSIVE"
            order_payload = None
        core = {
            "verdict": verdict,
            "searched_surfaces": searched,
            "time_window": window,
            "pagination_complete": pagination_complete,
            "consistency_horizon": coverage_end,
        }
        if order_payload is not None:
            core["order"] = order_payload
        return {
            **core,
            "evidence": [
                _evidence("query-order", cid, now, core)
            ],
        }

    def health(self, *, now: str) -> dict[str, Any]:
        _instant(now, name="now")
        return {
            "component": "SIMULATED_PROVIDER",
            "as_of": now,
            "status": "READY",
            "affected_scope": ["SIMULATION"],
            "reason_codes": [],
            "last_good_at": now,
            "next_action": "none",
        }
