"""Deterministic contract-shaped simulated provider for AutoTrade."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from threading import RLock
from typing import Any, Mapping
from uuid import NAMESPACE_URL, UUID, uuid5

from .exact_decimal import (
    ExactDecimalError,
    canonical_decimal_text,
    exact_abs,
    exact_add,
    exact_multiply,
    exact_subtract,
    exact_sum,
    parse_bounded_exact_decimal,
)


class SimulatedProviderConflict(ValueError):
    """Raised when an immutable simulated provider identity is reused."""


TRANSPORT_SEND_CONTRACT = "simulated-provider-final-guard-before-mutation-v1"


def _decimal(value, *, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use Decimal, string or integer input")
    try:
        return parse_bounded_exact_decimal(value)
    except ExactDecimalError as error:
        raise ValueError(f"{name} must be a finite decimal") from error


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
    try:
        return canonical_decimal_text(value)
    except ExactDecimalError as error:
        raise ValueError("decimal must be finite") from error


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


@dataclass(frozen=True)
class _PreparedSubmission:
    order: SimulatedOrder
    result: dict[str, Any]
    mode: str
    fill: dict[str, Any] | None
    new_cash: Decimal
    new_position: Decimal


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
            type(key) is not str
            or not key
            or key != key.strip()
            or value not in allowed_faults
            for key, value in raw_faults.items()
        ):
            raise ValueError(
                "transport_faults contains an unsupported deterministic fault"
            )
        self._transport_faults = dict(raw_faults)
        self._mutation_lock = RLock()
        self._transport_commit_active = False

    @staticmethod
    def _validate_attempt_id(value: str) -> str:
        text = _text(value, name="attempt_id")
        try:
            UUID(text)
        except ValueError as error:
            raise ValueError("attempt_id must be a UUID") from error
        return text

    def _require_provider_mutation_allowed(self) -> None:
        if self._transport_commit_active:
            raise SimulatedProviderConflict(
                "provider mutation is fenced by the final send barrier"
            )

    @contextmanager
    def _transport_commit_fence(self):
        """Serialize provider mutation across prepare, final guard and commit."""

        self._mutation_lock.acquire()
        if self._transport_commit_active:
            self._mutation_lock.release()
            raise SimulatedProviderConflict(
                "provider transport commit fence is already active"
            )
        self._transport_commit_active = True
        try:
            yield
        finally:
            self._transport_commit_active = False
            self._mutation_lock.release()

    def _prepare_immediate_fill(
        self,
        order: SimulatedOrder,
        canonical_now: str,
    ) -> tuple[dict[str, Any], Decimal, Decimal]:
        """Build one new-order fill completely before any provider mutation."""

        execution_id = (
            "exec-"
            + sha256(order.provider_order_id.encode("utf-8")).hexdigest()[:24]
        )
        if any(
            fill["provider_execution_id"] == execution_id
            or fill["order_ref"] == order.provider_order_id
            for fill in self.fills
        ):
            raise SimulatedProviderConflict(
                "new simulated order collides with existing fill history"
            )
        if order.client_order_id in self._cancelled_orders:
            raise SimulatedProviderConflict(
                "new simulated order collides with cancelled order history"
            )

        fee, new_cash, new_position = self._fill_financial_effect(
            order,
            order.quantity,
            order.price,
        )
        payload = {
            "fill_id": _uuid("fill", order.provider_order_id),
            "provider_execution_id": execution_id,
            "order_ref": order.provider_order_id,
            "intent_ref": None,
            "instrument_version": order.instrument_version,
            "side": order.side,
            "last_quantity": {
                "value": _decimal_text(order.quantity),
                "unit": _unit_id(order.instrument_version),
            },
            "last_price": _decimal_text(order.price),
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
        return fill, new_cash, new_position

    def _prepare_submission(
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
    ) -> _PreparedSubmission:
        """Validate and fully materialize one write without provider mutation."""

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

        proposed = SimulatedOrder(
            attempt_id=aid,
            client_order_id=cid,
            provider_order_id="sim-" + sha256(cid.encode("utf-8")).hexdigest()[:24],
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
            return _PreparedSubmission(
                order=prior_attempt,
                result=self._submission_result(
                    prior_attempt,
                    prior_attempt.submitted_at,
                ),
                mode="EXISTING_ATTEMPT",
                fill=None,
                new_cash=self.cash,
                new_position=self.positions.get(
                    prior_attempt.instrument_version,
                    Decimal("0"),
                ),
            )

        prior_client = self.orders.get(cid)
        if prior_client is not None:
            if not _same_client_order_request(prior_client, proposed):
                raise SimulatedProviderConflict(
                    "client_order_id already has different simulated order content"
                )
            if _instant(canonical_now, name="now") < _instant(
                prior_client.submitted_at,
                name="submitted_at",
            ):
                raise SimulatedProviderConflict(
                    "simulated order retry cannot precede canonical order submission"
                )
            return _PreparedSubmission(
                order=proposed,
                result=self._submission_result(proposed, canonical_now),
                mode="EXISTING_CLIENT",
                fill=None,
                new_cash=self.cash,
                new_position=self.positions.get(
                    proposed.instrument_version,
                    Decimal("0"),
                ),
            )

        fill = None
        new_cash = self.cash
        new_position = self.positions.get(
            proposed.instrument_version,
            Decimal("0"),
        )
        if fill_immediately:
            fill, new_cash, new_position = self._prepare_immediate_fill(
                proposed,
                canonical_now,
            )
        return _PreparedSubmission(
            order=proposed,
            result=self._submission_result(proposed, canonical_now),
            mode="NEW",
            fill=fill,
            new_cash=new_cash,
            new_position=new_position,
        )

    def preflight_submission(
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
    ) -> None:
        """Prove local submission admissibility without crossing the send boundary."""

        with self._mutation_lock:
            self._require_provider_mutation_allowed()
            self._prepare_submission(
                attempt_id=attempt_id,
                client_order_id=client_order_id,
                instrument_version=instrument_version,
                side=side,
                quantity=quantity,
                price=price,
                now=now,
                fill_immediately=fill_immediately,
            )

    def _commit_prepared_submission(
        self,
        prepared: _PreparedSubmission,
    ) -> dict[str, Any]:
        """Apply a fully materialized submission without fresh local decisions."""

        order = prepared.order
        if prepared.mode == "EXISTING_ATTEMPT":
            return deepcopy(prepared.result)
        if prepared.mode == "EXISTING_CLIENT":
            self._attempts[order.attempt_id] = order
            return deepcopy(prepared.result)
        self.orders[order.client_order_id] = order
        self._attempts[order.attempt_id] = order
        if prepared.fill is not None:
            self.cash = prepared.new_cash
            self.positions[order.instrument_version] = prepared.new_position
            self.fills.append(deepcopy(prepared.fill))
        return deepcopy(prepared.result)

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
        with self._mutation_lock:
            self._require_provider_mutation_allowed()
            prepared = self._prepare_submission(
                attempt_id=attempt_id,
                client_order_id=client_order_id,
                instrument_version=instrument_version,
                side=side,
                quantity=quantity,
                price=price,
                now=now,
                fill_immediately=fill_immediately,
            )
            return self._commit_prepared_submission(prepared)

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
        values = [
            _decimal(
                fill["last_quantity"]["value"],
                name="fill.last_quantity",
            )
            for fill in self.fills
            if fill["order_ref"] == order.provider_order_id
        ]
        return exact_sum(values, start=Decimal("0"))

    def _remaining_quantity(self, order: SimulatedOrder) -> Decimal:
        remaining = exact_subtract(
            order.quantity,
            self._filled_quantity(order),
        )
        if remaining < 0:
            raise SimulatedProviderConflict(
                "simulated provider history contains an overfilled order"
            )
        return remaining

    def _fill_financial_effect(
        self,
        order: SimulatedOrder,
        fill_quantity: Decimal,
        fill_price: Decimal,
    ) -> tuple[Decimal, Decimal, Decimal]:
        notional = exact_multiply(fill_quantity, fill_price)
        fee = exact_multiply(exact_abs(notional), self.fee_rate)
        signed_quantity = (
            fill_quantity
            if order.side == "BUY"
            else exact_subtract(Decimal("0"), fill_quantity)
        )
        cash_delta = (
            exact_subtract(Decimal("0"), exact_add(notional, fee))
            if order.side == "BUY"
            else exact_subtract(notional, fee)
        )
        new_cash = exact_add(self.cash, cash_delta)
        new_position = exact_add(
            self.positions.get(order.instrument_version, Decimal("0")),
            signed_quantity,
        )
        return fee, new_cash, new_position

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
        canonical_instant = _instant(canonical_now, name="now")
        if canonical_instant < _instant(
            order.submitted_at,
            name="submitted_at",
        ):
            raise SimulatedProviderConflict(
                "simulated fill cannot precede order submission"
            )
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
            return deepcopy(existing)

        # Chronology is a fence for newly appended provider facts, not for an
        # exact idempotent replay of an already-recorded execution. Evaluate it
        # only after provider_execution_id identity/content has been resolved.
        prior_receipts = [
            _instant(fill["receipt_time"], name="fill receipt_time")
            for fill in self.fills
            if fill["order_ref"] == order.provider_order_id
        ]
        if any(canonical_instant < receipt for receipt in prior_receipts):
            raise SimulatedProviderConflict(
                "simulated fill observation cannot precede existing provider history"
            )

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

        fee, new_cash, new_position = self._fill_financial_effect(
            order,
            fill_quantity,
            fill_price,
        )
        # All exact arithmetic and resource-envelope checks completed above.
        # Commit the provider financial mutation only after that preflight.
        self.cash = new_cash
        self.positions[order.instrument_version] = new_position
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
        return deepcopy(fill)

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

        with self._mutation_lock:
            self._require_provider_mutation_allowed()
            cid = _text(client_order_id, name="client_order_id")
            try:
                order = self.orders[cid]
            except KeyError as error:
                raise KeyError(
                    f"unknown simulated client_order_id: {cid}"
                ) from error
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

        with self._mutation_lock:
            self._require_provider_mutation_allowed()
            return self._cancel_order_unlocked(
                client_order_id=client_order_id,
                now=now,
                race_execution_id=race_execution_id,
                race_fill_quantity=race_fill_quantity,
                race_fill_price=race_fill_price,
            )

    def _cancel_order_unlocked(
        self,
        *,
        client_order_id: str,
        now: str,
        race_execution_id: str | None = None,
        race_fill_quantity=None,
        race_fill_price=None,
    ) -> dict[str, Any]:
        """Canonical cancellation implementation under the provider mutation lock."""

        cid = _text(client_order_id, name="client_order_id")
        try:
            order = self.orders[cid]
        except KeyError as error:
            raise KeyError(f"unknown simulated client_order_id: {cid}") from error
        canonical_now = _utc_text(now, name="now")
        canonical_instant = _instant(canonical_now, name="now")
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
            return deepcopy(existing)

        # Chronology fences newly appended cancellation/provider facts only.
        # An already-recorded deterministic cancellation retry returns the
        # canonical result without creating a new provider observation.
        if canonical_instant < _instant(
            order.submitted_at,
            name="submitted_at",
        ):
            raise SimulatedProviderConflict(
                "simulated cancellation cannot precede order submission"
            )
        prior_receipts = [
            _instant(fill["receipt_time"], name="fill receipt_time")
            for fill in self.fills
            if fill["order_ref"] == order.provider_order_id
        ]
        if any(canonical_instant < receipt for receipt in prior_receipts):
            raise SimulatedProviderConflict(
                "simulated cancellation cannot precede existing fill observation"
            )

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
        return deepcopy(result)

    def transport_send(
        self,
        client_order_id: str,
        request: Mapping[str, Any],
        final_guard,
    ) -> dict[str, Any]:
        """Dispatcher-compatible transport with an atomic local send fence."""

        if not isinstance(request, Mapping):
            raise TypeError("request must be a mapping")
        cid = _text(client_order_id, name="client_order_id")
        with self._transport_commit_fence():
            prepared = self._prepare_submission(
                attempt_id=request.get("attempt_id"),
                client_order_id=cid,
                instrument_version=request.get("instrument_version"),
                side=request.get("side"),
                quantity=request.get("quantity"),
                price=request.get("price"),
                now=request.get("now"),
                fill_immediately=request.get("fill_immediately", True),
            )
            fault = self._transport_faults.get(cid)
            if fault == "BEFORE_SEND_OUTAGE":
                raise ConnectionError(
                    "simulated outage before final send guard"
                )

            # No public provider mutation can interleave between materialization,
            # the authority guard and commit.  After the guard succeeds, the only
            # local state transition is application of the already-prepared fact.
            final_guard()
            self.outbound_request_count += 1
            result = self._commit_prepared_submission(prepared)
            if fault == "AFTER_ACCEPT_RESPONSE_LOST":
                raise TimeoutError(
                    "simulated provider accepted order but response was lost"
                )
            return result

    def export_state(self) -> dict[str, Any]:
        # A restart image must describe one complete provider cut, including
        # orders, fills and their cash/position effects.
        with self._mutation_lock:
            return self._export_state_unlocked()

    def _export_state_unlocked(self) -> dict[str, Any]:
        """Export a canonical restart image of provider-side simulated truth.

        The image is deliberately self-authenticating only for accidental
        corruption detection. Restore also replays fills and validates all
        derived financial state instead of trusting serialized cash/positions.
        """

        body = {
            "schema_version": "simulated-provider-state-v1",
            "config": {
                "account_id": self.account_id,
                "currency": self.currency,
                "initial_cash": _decimal_text(self.initial_cash),
                "fee_rate": _decimal_text(self.fee_rate),
            },
            "cash": _decimal_text(self.cash),
            "positions": {
                key: _decimal_text(value)
                for key, value in sorted(self.positions.items())
                if value != 0
            },
            "orders": [
                {
                    "attempt_id": order.attempt_id,
                    "client_order_id": order.client_order_id,
                    "provider_order_id": order.provider_order_id,
                    "instrument_version": order.instrument_version,
                    "side": order.side,
                    "quantity": _decimal_text(order.quantity),
                    "price": _decimal_text(order.price),
                    "submitted_at": order.submitted_at,
                }
                for order in sorted(
                    self.orders.values(),
                    key=lambda item: item.client_order_id,
                )
            ],
            "attempts": [
                {
                    "attempt_id": attempt_id,
                    "client_order_id": order.client_order_id,
                    "provider_order_id": order.provider_order_id,
                    "instrument_version": order.instrument_version,
                    "side": order.side,
                    "quantity": _decimal_text(order.quantity),
                    "price": _decimal_text(order.price),
                    "submitted_at": order.submitted_at,
                }
                for attempt_id, order in sorted(self._attempts.items())
            ],
            "fills": json.loads(
                json.dumps(
                    self.fills,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
            ),
            "cancel_results": json.loads(
                json.dumps(
                    self._cancel_results,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
            ),
            "cancelled_orders": dict(sorted(self._cancelled_orders.items())),
            "cancellation_requests": json.loads(
                json.dumps(
                    self._cancellation_requests,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
            ),
            "outbound_request_count": self.outbound_request_count,
            "transport_faults": dict(sorted(self._transport_faults.items())),
        }
        encoded = json.dumps(
            body,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        return {
            **body,
            "state_digest": "sha256:" + sha256(encoded).hexdigest(),
        }

    @classmethod
    def from_state(cls, state: Mapping[str, Any]) -> "SimulatedProvider":
        """Restore one exported state image and fail closed on inconsistency."""

        if not isinstance(state, Mapping):
            raise TypeError("state must be a mapping")
        raw = dict(state)
        state_digest = _text(raw.pop("state_digest", None), name="state_digest")
        expected_state_fields = {
            "schema_version",
            "config",
            "cash",
            "positions",
            "orders",
            "attempts",
            "fills",
            "cancel_results",
            "cancelled_orders",
            "cancellation_requests",
            "outbound_request_count",
            "transport_faults",
        }
        if set(raw) != expected_state_fields:
            raise SimulatedProviderConflict(
                "simulated provider state fields mismatch: unsupported or missing fields"
            )
        if not state_digest.startswith("sha256:") or len(state_digest) != 71:
            raise ValueError("state_digest must be a sha256 digest")
        encoded = json.dumps(
            raw,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        if state_digest != "sha256:" + sha256(encoded).hexdigest():
            raise SimulatedProviderConflict("simulated provider state digest mismatch")
        if raw.get("schema_version") != "simulated-provider-state-v1":
            raise ValueError("unsupported simulated provider state schema")
        config = raw.get("config")
        if not isinstance(config, Mapping):
            raise ValueError("simulated provider state config is required")
        if set(config) != {"account_id", "currency", "initial_cash", "fee_rate"}:
            raise ValueError("simulated provider state config fields mismatch")
        provider = cls(
            account_id=config.get("account_id"),
            currency=config.get("currency"),
            initial_cash=config.get("initial_cash"),
            fee_rate=config.get("fee_rate"),
            transport_faults=raw.get("transport_faults"),
        )

        orders = raw.get("orders")
        attempts = raw.get("attempts")
        fills = raw.get("fills")
        cancel_results = raw.get("cancel_results")
        cancelled_orders = raw.get("cancelled_orders")
        cancellation_requests = raw.get("cancellation_requests")
        positions = raw.get("positions")
        outbound_request_count = raw.get("outbound_request_count")
        if not isinstance(orders, list) or not isinstance(attempts, list):
            raise ValueError("orders and attempts must be lists")
        if not isinstance(fills, list):
            raise ValueError("fills must be a list")
        if not isinstance(cancel_results, Mapping):
            raise ValueError("cancel_results must be a mapping")
        if not isinstance(cancelled_orders, Mapping):
            raise ValueError("cancelled_orders must be a mapping")
        if not isinstance(cancellation_requests, Mapping):
            raise ValueError("cancellation_requests must be a mapping")
        if not isinstance(positions, Mapping):
            raise ValueError("positions must be a mapping")
        if (
            not isinstance(outbound_request_count, int)
            or isinstance(outbound_request_count, bool)
            or outbound_request_count < 0
        ):
            raise ValueError("outbound_request_count must be a non-negative integer")

        order_fields = {
            "attempt_id",
            "client_order_id",
            "provider_order_id",
            "instrument_version",
            "side",
            "quantity",
            "price",
            "submitted_at",
        }
        for item in orders:
            if not isinstance(item, Mapping):
                raise ValueError("order state entries must be mappings")
            if set(item) != order_fields:
                raise SimulatedProviderConflict(
                    "simulated order state has unsupported or missing fields"
                )
            aid = provider._validate_attempt_id(item.get("attempt_id"))
            cid = _text(item.get("client_order_id"), name="client_order_id")
            if cid in provider.orders:
                raise SimulatedProviderConflict("duplicate client_order_id in state")
            expected_provider_order_id = (
                "sim-" + sha256(cid.encode("utf-8")).hexdigest()[:24]
            )
            provider_order_id = _text(
                item.get("provider_order_id"),
                name="provider_order_id",
            )
            if provider_order_id != expected_provider_order_id:
                raise SimulatedProviderConflict(
                    "simulated provider order identity mismatch"
                )
            side = _text(item.get("side"), name="side").upper()
            if side not in {"BUY", "SELL"}:
                raise ValueError("side must be BUY or SELL")
            order = SimulatedOrder(
                attempt_id=aid,
                client_order_id=cid,
                provider_order_id=provider_order_id,
                instrument_version=_text(
                    item.get("instrument_version"),
                    name="instrument_version",
                ),
                side=side,
                quantity=_positive(item.get("quantity"), name="quantity"),
                price=_positive(item.get("price"), name="price"),
                submitted_at=_utc_text(item.get("submitted_at"), name="submitted_at"),
            )
            provider.orders[cid] = order

        for item in attempts:
            if not isinstance(item, Mapping):
                raise ValueError("attempt state entries must be mappings")
            if set(item) != order_fields:
                raise SimulatedProviderConflict(
                    "simulated attempt state has unsupported or missing fields"
                )
            aid = provider._validate_attempt_id(item.get("attempt_id"))
            if aid in provider._attempts:
                raise SimulatedProviderConflict("duplicate attempt_id in state")
            cid = _text(item.get("client_order_id"), name="client_order_id")
            try:
                canonical_order = provider.orders[cid]
            except KeyError as error:
                raise SimulatedProviderConflict(
                    "attempt references unknown simulated order"
                ) from error
            attempt_order = SimulatedOrder(
                attempt_id=aid,
                client_order_id=cid,
                provider_order_id=_text(
                    item.get("provider_order_id"),
                    name="provider_order_id",
                ),
                instrument_version=_text(
                    item.get("instrument_version"),
                    name="instrument_version",
                ),
                side=_text(item.get("side"), name="side").upper(),
                quantity=_positive(item.get("quantity"), name="quantity"),
                price=_positive(item.get("price"), name="price"),
                submitted_at=_utc_text(item.get("submitted_at"), name="submitted_at"),
            )
            if not _same_client_order_request(canonical_order, attempt_order):
                raise SimulatedProviderConflict(
                    "attempt economics do not match canonical simulated order"
                )
            if _instant(
                attempt_order.submitted_at,
                name="attempt submitted_at",
            ) < _instant(
                canonical_order.submitted_at,
                name="order submitted_at",
            ):
                raise SimulatedProviderConflict(
                    "serialized attempt precedes canonical order submission"
                )
            provider._attempts[aid] = attempt_order

        for order in provider.orders.values():
            original = provider._attempts.get(order.attempt_id)
            if (
                original is None
                or not _same_attempt_request(order, original)
                or original.submitted_at != order.submitted_at
            ):
                raise SimulatedProviderConflict(
                    "canonical simulated order is missing its original attempt"
                )

        # Re-apply fills through the production mutation path. This validates
        # execution identity, overfill protection, exact fees, cash and positions
        # instead of trusting serialized derived financial state.
        for item in fills:
            if not isinstance(item, Mapping):
                raise ValueError("fill state entries must be mappings")
            order_ref = _text(item.get("order_ref"), name="order_ref")
            matching = [
                order
                for order in provider.orders.values()
                if order.provider_order_id == order_ref
            ]
            if len(matching) != 1:
                raise SimulatedProviderConflict(
                    "fill references unknown or ambiguous simulated order"
                )
            order = matching[0]
            serialized_execution_id = _text(
                item.get("provider_execution_id"),
                name="provider_execution_id",
            )
            serialized_fill_id = _text(
                item.get("fill_id"),
                name="fill_id",
            )
            implicit_execution_id = (
                "exec-"
                + sha256(order.provider_order_id.encode("utf-8")).hexdigest()[:24]
            )
            implicit_fill_id = _uuid("fill", order.provider_order_id)
            explicit_fill_id = _uuid("fill", serialized_execution_id)
            if (
                serialized_execution_id == implicit_execution_id
                and serialized_fill_id == implicit_fill_id
            ):
                replay_execution_id = None
            elif serialized_fill_id == explicit_fill_id:
                replay_execution_id = serialized_execution_id
            else:
                raise SimulatedProviderConflict(
                    "serialized fill identity is not canonical"
                )
            generated = provider._record_fill(
                order,
                item.get("trade_time"),
                quantity=(
                    item.get("last_quantity", {}).get("value")
                    if isinstance(item.get("last_quantity"), Mapping)
                    else None
                ),
                price=item.get("last_price"),
                provider_execution_id=replay_execution_id,
            )
            if generated != dict(item):
                raise SimulatedProviderConflict(
                    "serialized simulated fill does not match canonical economics"
                )

        expected_cash = _decimal(raw.get("cash"), name="cash")
        if provider.cash != expected_cash:
            raise SimulatedProviderConflict(
                "serialized cash does not match replayed simulated fills"
            )
        expected_positions = {
            _text(key, name="position instrument"): _decimal(
                value,
                name="position quantity",
            )
            for key, value in positions.items()
            if _decimal(value, name="position quantity") != 0
        }
        actual_positions = {
            key: value
            for key, value in provider.positions.items()
            if value != 0
        }
        if actual_positions != expected_positions:
            raise SimulatedProviderConflict(
                "serialized positions do not match replayed simulated fills"
            )

        # Establish the immutable cancellation chronology before comparing
        # derived cancel quantities. A later fill must be rejected for the
        # direct semantic violation, not merely because it changes a projection.
        for cid, cancelled_at in cancelled_orders.items():
            cid = _text(cid, name="cancelled client_order_id")
            if cid not in cancel_results or cid not in provider.orders:
                raise SimulatedProviderConflict(
                    "cancelled order is missing canonical cancel result"
                )
            canonical_cancelled_at = _utc_text(
                cancelled_at,
                name="cancelled_at",
            )
            for fill in provider.fills:
                if (
                    fill["order_ref"] == provider.orders[cid].provider_order_id
                    and _instant(fill["trade_time"], name="trade_time")
                    > _instant(canonical_cancelled_at, name="cancelled_at")
                ):
                    raise SimulatedProviderConflict(
                        "serialized fill occurs after acknowledged cancellation"
                    )

        if set(cancel_results) != set(cancellation_requests):
            raise SimulatedProviderConflict(
                "cancel results and request identities do not match"
            )
        for cid, result in cancel_results.items():
            cid = _text(cid, name="cancel client_order_id")
            if cid not in provider.orders or not isinstance(result, Mapping):
                raise SimulatedProviderConflict(
                    "cancel state references unknown simulated order"
                )
            request = cancellation_requests[cid]
            if not isinstance(request, Mapping):
                raise ValueError("cancellation request state must be a mapping")
            if set(request) != {
                "race_execution_id",
                "race_fill_quantity",
                "race_fill_price",
            }:
                raise SimulatedProviderConflict(
                    "cancellation request has unsupported state fields"
                )
            race_execution_id = request.get("race_execution_id")
            race_fill_quantity = request.get("race_fill_quantity")
            race_fill_price = request.get("race_fill_price")
            if race_execution_id is None:
                if race_fill_quantity is not None or race_fill_price is not None:
                    raise SimulatedProviderConflict(
                        "cancellation request has orphan race economics"
                    )
                if "race_execution_id" in result:
                    raise SimulatedProviderConflict(
                        "cancel result race identity is missing from request"
                    )
            else:
                race_execution_id = _text(
                    race_execution_id,
                    name="race_execution_id",
                )
                if result.get("race_execution_id") != race_execution_id:
                    raise SimulatedProviderConflict(
                        "cancel request/result race identity mismatch"
                    )
                race_quantity = _positive(
                    race_fill_quantity,
                    name="race_fill_quantity",
                )
                race_price = _positive(
                    race_fill_price,
                    name="race_fill_price",
                )
                matching_race_fills = [
                    fill
                    for fill in provider.fills
                    if fill["provider_execution_id"] == race_execution_id
                ]
                if len(matching_race_fills) != 1:
                    raise SimulatedProviderConflict(
                        "cancel race execution is missing from fill history"
                    )
                race_fill = matching_race_fills[0]
                if (
                    race_fill["order_ref"]
                    != provider.orders[cid].provider_order_id
                    or race_fill["last_quantity"]["value"]
                    != _decimal_text(race_quantity)
                    or race_fill["last_price"] != _decimal_text(race_price)
                    or race_fill["trade_time"] != result.get("observed_at")
                ):
                    raise SimulatedProviderConflict(
                        "cancel race economics do not match canonical fill"
                    )
            outcome = result.get("outcome")
            common_result_fields = {
                "provider_order_id",
                "client_order_id",
                "observed_at",
                "filled_quantity",
                "remaining_quantity",
                "outcome",
                "evidence",
            }
            expected_result_fields = set(common_result_fields)
            if race_execution_id is not None:
                expected_result_fields.add("race_execution_id")
            if outcome == "ACKNOWLEDGED":
                expected_result_fields.add("cancelled_at")
            elif outcome == "REJECTED":
                expected_result_fields.add("reason_code")
            else:
                raise SimulatedProviderConflict(
                    "unsupported cancellation outcome in state"
                )
            if set(result) != expected_result_fields:
                raise SimulatedProviderConflict(
                    "cancel result has unsupported or missing fields"
                )
            order = provider.orders[cid]
            filled = provider._filled_quantity(order)
            remaining = provider._remaining_quantity(order)
            if (
                result.get("provider_order_id") != order.provider_order_id
                or result.get("client_order_id") != cid
                or result.get("filled_quantity") != _decimal_text(filled)
                or result.get("remaining_quantity") != _decimal_text(remaining)
            ):
                raise SimulatedProviderConflict(
                    "cancel result quantities or order identity do not match fill history"
                )
            observed_at = _utc_text(
                result.get("observed_at"),
                name="observed_at",
            )
            observed_instant = _instant(observed_at, name="observed_at")
            if observed_instant < _instant(
                order.submitted_at,
                name="submitted_at",
            ):
                raise SimulatedProviderConflict(
                    "serialized cancellation precedes order submission"
                )
            if any(
                observed_instant
                < _instant(fill["receipt_time"], name="fill receipt_time")
                for fill in provider.fills
                if fill["order_ref"] == order.provider_order_id
            ):
                raise SimulatedProviderConflict(
                    "serialized cancellation precedes existing fill observation"
                )
            if outcome == "ACKNOWLEDGED":
                cancelled_at = _utc_text(
                    result.get("cancelled_at"),
                    name="cancelled_at",
                )
                if (
                    remaining <= 0
                    or cancelled_orders.get(cid) != cancelled_at
                    or cancelled_at != observed_at
                ):
                    raise SimulatedProviderConflict(
                        "acknowledged cancellation is inconsistent with order state"
                    )
            elif outcome == "REJECTED":
                if (
                    result.get("reason_code") != "ALREADY_FILLED"
                    or remaining != 0
                    or cid in cancelled_orders
                    or "cancelled_at" in result
                ):
                    raise SimulatedProviderConflict(
                        "rejected cancellation is inconsistent with order state"
                    )
            else:
                raise SimulatedProviderConflict(
                    "unsupported cancellation outcome in state"
                )
            sealed_payload = {
                key: value
                for key, value in result.items()
                if key != "evidence"
            }
            expected_evidence = [
                _evidence(
                    "cancel",
                    provider.orders[cid].provider_order_id,
                    result.get("observed_at"),
                    sealed_payload,
                )
            ]
            if result.get("evidence") != expected_evidence:
                raise SimulatedProviderConflict(
                    "cancellation evidence does not match canonical result"
                )

        provider._cancel_results = {
            str(key): deepcopy(dict(value))
            for key, value in cancel_results.items()
        }
        provider._cancelled_orders = {
            str(key): _utc_text(value, name="cancelled_at")
            for key, value in cancelled_orders.items()
        }
        provider._cancellation_requests = {
            str(key): dict(value)
            for key, value in cancellation_requests.items()
        }
        provider.outbound_request_count = outbound_request_count
        return provider

    def activity_fills(self) -> tuple[dict[str, Any], ...]:
        with self._mutation_lock:
            return tuple(deepcopy(fill) for fill in self.fills)

    def account_snapshot(self, *, now: str) -> dict[str, Any]:
        with self._mutation_lock:
            return self._account_snapshot_unlocked(now=now)

    def _account_snapshot_unlocked(self, *, now: str) -> dict[str, Any]:
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
            filled = exact_subtract(order.quantity, remaining)
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
        current = _instant(now, name="now")
        canonical_now = current.isoformat().replace("+00:00", "Z")
        if end < start:
            raise ValueError("coverage_end must not precede coverage_start")
        if not isinstance(pagination_complete, bool):
            raise TypeError("pagination_complete must be boolean")
        searched = ["orders-by-client-id", "activity-fills"]
        window = {
            "start": canonical_start,
            "end": canonical_end,
        }
        order = self.orders.get(cid)
        if order is None and end > current:
            core = {
                "verdict": "INCONCLUSIVE",
                "searched_surfaces": searched,
                "time_window": window,
                "pagination_complete": pagination_complete,
                "consistency_horizon": canonical_now,
                "reason_codes": ["coverage_end_after_query_time"],
            }
            return {
                **core,
                "evidence": [
                    _evidence("query-order", cid, canonical_now, core)
                ],
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
            "consistency_horizon": (
                canonical_now if end > current else canonical_end
            ),
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
