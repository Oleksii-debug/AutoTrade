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


def _utc_text(value: str, *, name: str = "timestamp") -> str:
    return _instant(value, name=name).isoformat().replace("+00:00", "Z")


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


def _same_attempt_request(left: SimulatedOrder, right: SimulatedOrder) -> bool:
    """Compare one local attempt identity/economics, excluding receipt time."""

    return (
        left.attempt_id == right.attempt_id
        and _same_client_order_request(left, right)
    )


def _same_client_order_request(left: SimulatedOrder, right: SimulatedOrder) -> bool:
    """Compare provider-idempotent order economics across local attempts."""

    return (
        left.client_order_id == right.client_order_id
        and left.provider_order_id == right.provider_order_id
        and left.instrument_version == right.instrument_version
        and left.side == right.side
        and left.quantity == right.quantity
        and left.price == right.price
    )


class SimulatedProvider:
    """Network-free deterministic provider with exact contract-shaped outputs."""

    def __init__(
        self,
        *,
        account_id: str = "sim-account",
        currency: str = "USD",
        initial_cash="100000",
        fee_rate="0.001",
        transport_faults: Mapping[str, str] | None = None,
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
        self._cancel_results: dict[str, dict[str, Any]] = {}
        self._cancelled_orders: dict[str, str] = {}
        self._cancellation_requests: dict[str, dict[str, str | None]] = {}
        self.outbound_request_count = 0
        allowed_faults = {"BEFORE_SEND_OUTAGE", "AFTER_ACCEPT_RESPONSE_LOST"}
        raw_faults = {} if transport_faults is None else dict(transport_faults)
        if any(
            not isinstance(key, str)
            or not key.strip()
            or value not in allowed_faults
            for key, value in raw_faults.items()
        ):
            raise ValueError(
                "transport_faults contains an unsupported deterministic fault"
            )
        self._transport_faults = dict(raw_faults)

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
        canonical_now = _utc_text(now, name="now")
        if type(fill_immediately) is not bool:
            raise TypeError("fill_immediately must be boolean")
        provider_order_id = "sim-" + sha256(cid.encode("utf-8")).hexdigest()[:24]
        proposed = SimulatedOrder(
            attempt_id=aid,
            client_order_id=cid,
            provider_order_id=provider_order_id,
            instrument_version=instrument,
            side=normalized_side,
            quantity=qty,
            price=px,
            submitted_at=canonical_now,
        )

        prior_attempt = self._attempts.get(aid)
        if prior_attempt is not None:
            if not _same_attempt_request(prior_attempt, proposed):
                raise SimulatedProviderConflict(
                    "attempt_id already has different simulated order content"
                )
            return self._submission_result(prior_attempt, canonical_now)

        prior_client = self.orders.get(cid)
        if prior_client is not None:
            if not _same_client_order_request(prior_client, proposed):
                raise SimulatedProviderConflict(
                    "client_order_id already has different simulated order content"
                )
            self._attempts[aid] = proposed
            return self._submission_result(proposed, canonical_now)

        self.orders[cid] = proposed
        self._attempts[aid] = proposed
        if fill_immediately:
            self._record_fill(proposed, canonical_now)
        return self._submission_result(proposed, canonical_now)

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

    def _filled_quantity(self, order: SimulatedOrder) -> Decimal:
        total = Decimal("0")
        for fill in self.fills:
            if fill["order_ref"] != order.provider_order_id:
                continue
            total += _decimal(
                fill["last_quantity"]["value"],
                name="fill.last_quantity",
            )
        return total

    def _remaining_quantity(self, order: SimulatedOrder) -> Decimal:
        remaining = order.quantity - self._filled_quantity(order)
        if remaining < 0:
            raise SimulatedProviderConflict(
                "simulated provider history contains an overfilled order"
            )
        return remaining

    def _record_fill(
        self,
        order: SimulatedOrder,
        now: str,
        *,
        quantity=None,
        price=None,
        provider_execution_id: str | None = None,
    ) -> dict[str, Any]:
        canonical_now = _utc_text(now, name="now")
        fill_quantity = (
            self._remaining_quantity(order)
            if quantity is None
            else _positive(quantity, name="fill_quantity")
        )
        fill_price = (
            order.price
            if price is None
            else _positive(price, name="fill_price")
        )
        execution_id = (
            "exec-" + sha256(order.provider_order_id.encode("utf-8")).hexdigest()[:24]
            if provider_execution_id is None
            else _text(provider_execution_id, name="provider_execution_id")
        )

        existing = next(
            (
                item
                for item in self.fills
                if item["provider_execution_id"] == execution_id
            ),
            None,
        )
        if existing is not None:
            exact = (
                existing["order_ref"] == order.provider_order_id
                and existing["instrument_version"] == order.instrument_version
                and existing["side"] == order.side
                and existing["last_quantity"]["value"]
                == _decimal_text(fill_quantity)
                and existing["last_price"] == _decimal_text(fill_price)
                and existing["trade_time"] == canonical_now
            )
            if not exact:
                raise SimulatedProviderConflict(
                    "provider_execution_id already has different simulated fill content"
                )
            return existing

        if order.client_order_id in self._cancelled_orders:
            raise SimulatedProviderConflict(
                "cancelled simulated order cannot receive a later fill"
            )
        remaining = self._remaining_quantity(order)
        if remaining <= 0:
            raise SimulatedProviderConflict(
                "fully filled simulated order cannot receive another execution"
            )
        if fill_quantity > remaining:
            raise SimulatedProviderConflict(
                "simulated execution would overfill the provider order"
            )

        notional = fill_quantity * fill_price
        fee = abs(notional) * self.fee_rate
        signed_quantity = (
            fill_quantity if order.side == "BUY" else -fill_quantity
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
            "fill_id": _uuid(
                "fill",
                order.provider_order_id
                if provider_execution_id is None
                else execution_id,
            ),
            "provider_execution_id": execution_id,
            "order_ref": order.provider_order_id,
            "intent_ref": None,
            "instrument_version": order.instrument_version,
            "side": order.side,
            "last_quantity": {
                "value": _decimal_text(fill_quantity),
                "unit": _unit_id(order.instrument_version),
            },
            "last_price": _decimal_text(fill_price),
            "trade_time": canonical_now,
            "receipt_time": canonical_now,
            "fees": [
                {
                    "amount": _decimal_text(fee),
                    "currency": self.currency,
                }
            ],
            "settlement_date": _instant(canonical_now).date().isoformat(),
        }
        fill = {
            **payload,
            "evidence": [
                _evidence(
                    "fill",
                    execution_id,
                    canonical_now,
                    payload,
                )
            ],
        }
        self.fills.append(fill)
        return fill

    def record_fill(
        self,
        *,
        client_order_id: str,
        provider_execution_id: str,
        quantity,
        now: str,
        price=None,
    ) -> dict[str, Any]:
        """Apply one deterministic provider execution to an accepted order."""

        cid = _text(client_order_id, name="client_order_id")
        try:
            order = self.orders[cid]
        except KeyError as error:
            raise KeyError(f"unknown simulated client_order_id: {cid}") from error
        return self._record_fill(
            order,
            now,
            quantity=quantity,
            price=price,
            provider_execution_id=provider_execution_id,
        )

    def cancel_order(
        self,
        *,
        client_order_id: str,
        now: str,
        race_execution_id: str | None = None,
        race_fill_quantity=None,
        race_fill_price=None,
    ) -> dict[str, Any]:
        """Acknowledge cancel after an optional deterministic fill-before-cancel race."""

        cid = _text(client_order_id, name="client_order_id")
        try:
            order = self.orders[cid]
        except KeyError as error:
            raise KeyError(f"unknown simulated client_order_id: {cid}") from error
        canonical_now = _utc_text(now, name="now")
        if (race_execution_id is None) != (race_fill_quantity is None):
            raise ValueError(
                "race_execution_id and race_fill_quantity must be supplied together"
            )
        if race_execution_id is None and race_fill_price is not None:
            raise ValueError(
                "race_fill_price requires a deterministic race execution"
            )
        normalized_race_execution_id = (
            None
            if race_execution_id is None
            else _text(race_execution_id, name="race_execution_id")
        )
        normalized_race_quantity = (
            None
            if race_fill_quantity is None
            else _positive(race_fill_quantity, name="race_fill_quantity")
        )
        normalized_race_price = (
            None
            if normalized_race_execution_id is None
            else (
                order.price
                if race_fill_price is None
                else _positive(race_fill_price, name="race_fill_price")
            )
        )
        race_request = {
            "race_execution_id": normalized_race_execution_id,
            "race_fill_quantity": (
                None
                if normalized_race_quantity is None
                else _decimal_text(normalized_race_quantity)
            ),
            "race_fill_price": (
                None
                if normalized_race_price is None
                else _decimal_text(normalized_race_price)
            ),
        }
        existing = self._cancel_results.get(cid)
        if existing is not None:
            if self._cancellation_requests[cid] != race_request:
                raise SimulatedProviderConflict(
                    "cancel retry changed deterministic race semantics"
                )
            return existing

        if normalized_race_execution_id is not None:
            self._record_fill(
                order,
                canonical_now,
                quantity=normalized_race_quantity,
                price=normalized_race_price,
                provider_execution_id=normalized_race_execution_id,
            )

        filled = self._filled_quantity(order)
        remaining = self._remaining_quantity(order)
        payload = {
            "provider_order_id": order.provider_order_id,
            "client_order_id": cid,
            "observed_at": canonical_now,
            "filled_quantity": _decimal_text(filled),
            "remaining_quantity": _decimal_text(remaining),
        }
        if normalized_race_execution_id is not None:
            payload["race_execution_id"] = normalized_race_execution_id
        if remaining == 0:
            outcome = "REJECTED"
            reason_code = "ALREADY_FILLED"
        else:
            outcome = "ACKNOWLEDGED"
            reason_code = None
            payload["cancelled_at"] = canonical_now

        # The sealed evidence must bind the semantic cancellation verdict, not
        # only quantities/timestamps. Otherwise identical provider state could
        # be presented later as ACKNOWLEDGED or REJECTED without changing the
        # evidence digest.
        payload["outcome"] = outcome
        if reason_code is not None:
            payload["reason_code"] = reason_code
        result = {
            **payload,
            "evidence": [
                _evidence("cancel", order.provider_order_id, canonical_now, payload)
            ],
        }
        self._cancellation_requests[cid] = race_request
        self._cancel_results[cid] = result
        if outcome == "ACKNOWLEDGED":
            self._cancelled_orders[cid] = canonical_now
        return result

    def transport_send(
        self,
        client_order_id: str,
        request: Mapping[str, Any],
        final_guard,
    ) -> dict[str, Any]:
        """Dispatcher-compatible transport; guard is immediately before send."""

        if not isinstance(request, Mapping):
            raise TypeError("request must be a mapping")
        fault = self._transport_faults.get(client_order_id)
        if fault == "BEFORE_SEND_OUTAGE":
            raise ConnectionError("simulated outage before final send guard")
        final_guard()
        self.outbound_request_count += 1
        result = self.submit_order(
            attempt_id=request["attempt_id"],
            client_order_id=client_order_id,
            instrument_version=request["instrument_version"],
            side=request["side"],
            quantity=request["quantity"],
            price=request["price"],
            now=request["now"],
            fill_immediately=request.get("fill_immediately", True),
        )
        if fault == "AFTER_ACCEPT_RESPONSE_LOST":
            raise TimeoutError(
                "simulated provider accepted order but response was lost"
            )
        return result

    def activity_fills(self) -> tuple[dict[str, Any], ...]:
        return tuple(self.fills)

    def account_snapshot(self, *, now: str) -> dict[str, Any]:
        canonical_now = _utc_text(now, name="now")
        balances = [
            {
                "currency": self.currency,
                "total": _decimal_text(self.cash),
                "available": _decimal_text(self.cash),
                "reserved": "0",
                "liability": "0",
                "as_of": canonical_now,
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
                "as_of": canonical_now,
            }
            for instrument, quantity in sorted(self.positions.items())
            if quantity != 0
        ]
        open_orders = []
        for order in sorted(
            self.orders.values(),
            key=lambda item: item.client_order_id,
        ):
            remaining = self._remaining_quantity(order)
            if remaining <= 0 or order.client_order_id in self._cancelled_orders:
                continue
            filled = order.quantity - remaining
            open_orders.append(
                {
                    "provider_order_id": order.provider_order_id,
                    "client_order_id": order.client_order_id,
                    "instrument_version": order.instrument_version,
                    "side": order.side,
                    "quantity": _decimal_text(order.quantity),
                    "filled_quantity": _decimal_text(filled),
                    "remaining_quantity": _decimal_text(remaining),
                    "price": _decimal_text(order.price),
                    "submitted_at": order.submitted_at,
                }
            )
        core = {
            "account_id": self.account_id,
            "environment": "SIMULATION",
            "query_started_at": canonical_now,
            "query_completed_at": canonical_now,
            "provider_as_of": canonical_now,
            "consistency": "ATOMIC",
            "balances": balances,
            "positions": positions,
            "open_orders": open_orders,
            "margin": {
                "account_id": self.account_id,
                "as_of": canonical_now,
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
                _evidence("snapshot", snapshot_id, canonical_now, core)
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
        canonical_start = start.isoformat().replace("+00:00", "Z")
        canonical_end = end.isoformat().replace("+00:00", "Z")
        canonical_now = _utc_text(now, name="now")
        if end < start:
            raise ValueError("coverage_end must not precede coverage_start")
        if not isinstance(pagination_complete, bool):
            raise TypeError("pagination_complete must be boolean")
        order = self.orders.get(cid)
        searched = ["orders-by-client-id", "activity-fills"]
        window = {
            "start": canonical_start,
            "end": canonical_end,
        }
        if order is not None:
            verdict = "FOUND"
            filled = self._filled_quantity(order)
            remaining = self._remaining_quantity(order)
            if remaining == 0:
                status = "FILLED"
            elif order.client_order_id in self._cancelled_orders:
                status = "CANCELLED"
            elif filled > 0:
                status = "PARTIALLY_FILLED"
            else:
                status = "WORKING"
            order_payload = {
                "provider_order_id": order.provider_order_id,
                "client_order_id": order.client_order_id,
                "instrument_version": order.instrument_version,
                "side": order.side,
                "quantity": _decimal_text(order.quantity),
                "filled_quantity": _decimal_text(filled),
                "remaining_quantity": _decimal_text(remaining),
                "price": _decimal_text(order.price),
                "submitted_at": order.submitted_at,
                "status": status,
            }
            if status == "CANCELLED":
                order_payload["cancelled_at"] = self._cancelled_orders[cid]
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
            "consistency_horizon": canonical_end,
        }
        if order_payload is not None:
            core["order"] = order_payload
        return {
            **core,
            "evidence": [
                _evidence("query-order", cid, canonical_now, core)
            ],
        }

    def health(self, *, now: str) -> dict[str, Any]:
        canonical_now = _utc_text(now, name="now")
        return {
            "component": "SIMULATED_PROVIDER",
            "as_of": canonical_now,
            "status": "READY",
            "affected_scope": ["SIMULATION"],
            "reason_codes": [],
            "last_good_at": canonical_now,
            "next_action": "none",
        }
