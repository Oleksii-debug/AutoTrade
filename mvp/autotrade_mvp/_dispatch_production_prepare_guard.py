"""Require verified durable canonical order preparation before PAPER/LIVE send."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from functools import wraps
from typing import Any, Mapping

from .dispatch import GuardedDispatcher


class _MissingDurableOrderPreparation(PermissionError):
    """Internal fail-closed signal consumed by dispatcher preparation handling."""


def _required_scope(scope: object) -> tuple[str, str, Decimal, str]:
    if not isinstance(scope, Mapping):
        raise _MissingDurableOrderPreparation(
            "production submission requires canonical durable order scope"
        )
    instrument = scope.get("instrument")
    side = scope.get("side")
    quantity = scope.get("requested_quantity")
    quantity_unit = scope.get("quantity_unit")
    if not isinstance(instrument, str) or not instrument.strip():
        raise _MissingDurableOrderPreparation(
            "production submission scope requires instrument"
        )
    if not isinstance(side, str) or not side.strip():
        raise _MissingDurableOrderPreparation(
            "production submission scope requires side"
        )
    if not isinstance(quantity, str) or not quantity.strip():
        raise _MissingDurableOrderPreparation(
            "production submission scope requires canonical requested_quantity text"
        )
    try:
        requested_quantity = Decimal(quantity)
    except (InvalidOperation, ValueError) as error:
        raise _MissingDurableOrderPreparation(
            "production submission scope requested_quantity is invalid"
        ) from error
    if not requested_quantity.is_finite() or requested_quantity <= 0:
        raise _MissingDurableOrderPreparation(
            "production submission scope requested_quantity must be positive"
        )
    if not isinstance(quantity_unit, str) or not quantity_unit.strip():
        raise _MissingDurableOrderPreparation(
            "production submission scope requires quantity_unit"
        )
    return instrument.strip(), side.strip().upper(), requested_quantity, quantity_unit.strip()


def install_production_prepare_guard() -> None:
    """Make the existing pre-send canonical-order boundary mandatory and provable.

    The callback remains the provider/composition adapter which creates the order.
    After it returns, this layer replays the existing DurableOrderBookProjection from
    the same JournalStore and proves the exact production order was durably created
    for this submission attempt.  No second order ledger or dispatcher is introduced.
    """

    original = GuardedDispatcher.dispatch
    if getattr(original, "_autotrade_requires_production_prepare", False):
        return

    @wraps(original)
    def guarded_dispatch(self: GuardedDispatcher, *args: Any, **kwargs: Any):
        if self.environment not in {"PAPER", "LIVE"}:
            return original(self, *args, **kwargs)

        supplied = kwargs.get("prepare_order")
        if kwargs.get("order_preparation_binding") is None:
            def missing_order_binding(*_args: Any, **_kwargs: Any) -> None:
                raise _MissingDurableOrderPreparation(
                    "explicit order_preparation_binding is required for PAPER/LIVE submission"
                )

            # Never invoke a caller preparation callback when its independent
            # canonical order binding is absent. The generic dispatcher records
            # a local SubmissionBlocked result and cannot reach transport.
            kwargs["prepare_order"] = missing_order_binding
            return original(self, *args, **kwargs)

        if supplied is None:
            def missing_prepare_order(*_args: Any, **_kwargs: Any) -> None:
                raise _MissingDurableOrderPreparation(
                    "durable canonical order preparation is required before provider send"
                )

            kwargs["prepare_order"] = missing_prepare_order
            return original(self, *args, **kwargs)

        if not callable(supplied):
            def invalid_prepare_order(*_args: Any, **_kwargs: Any) -> None:
                raise _MissingDurableOrderPreparation(
                    "durable canonical order preparation must be callable"
                )

            kwargs["prepare_order"] = invalid_prepare_order
            return original(self, *args, **kwargs)

        @wraps(supplied)
        def verified_prepare_order(
            client_order_id: str,
            attempt_id: str,
            intent_id: str,
            provider: str,
            request: Mapping[str, Any],
            scope: Mapping[str, Any],
            prepared_at: str,
        ) -> None:
            instrument, side, requested_quantity, quantity_unit = _required_scope(scope)
            supplied(
                client_order_id,
                attempt_id,
                intent_id,
                provider,
                request,
                scope,
                prepared_at,
            )

            # Lazy import avoids the durable projection's intentional import of
            # dispatch.submission_attempt_aggregate_id during module composition.
            from .durable_order_projection import DurableOrderBookProjection

            projection = DurableOrderBookProjection(
                self.store,
                provider_id=provider,
                account_id=self.account_id,
                environment=self.environment,
                host_id=self.owner_token,
                owner_epoch=str(self.owner_epoch),
            )
            try:
                snapshot = projection.order(client_order_id).snapshot()
            except Exception as error:
                raise _MissingDurableOrderPreparation(
                    "prepare_order did not durably create the canonical client order"
                ) from error

            expected_event_key = f"dispatch-order:{attempt_id}"
            idempotency_entry = projection._idempotency.get(expected_event_key)
            if idempotency_entry is None:
                raise _MissingDurableOrderPreparation(
                    "canonical order is not bound to this submission attempt"
                )
            # The key's mere presence is insufficient: a callback could bind a
            # different CREATE event to this attempt while preparing the target
            # order under another key. Verify the immutable event behind the
            # binding is the CREATE for this exact order and intent.
            bound_event = self.store.get_event(idempotency_entry[2])
            bound_payload = bound_event.get("payload") if bound_event else None
            bound_request = (
                bound_payload.get("request")
                if isinstance(bound_payload, Mapping)
                else None
            )
            if (
                bound_event is None
                or bound_event.get("aggregate_type") != "order_projection_book"
                or bound_event.get("aggregate_id") != projection.aggregate_id
                or not isinstance(bound_payload, Mapping)
                or bound_payload.get("operation") != "CREATE"
                or not isinstance(bound_request, Mapping)
                or bound_request.get("client_order_id") != client_order_id
                or bound_request.get("origin_intent_id") != intent_id
                or bound_request.get("instrument") != instrument
                or bound_request.get("side") != side
                or bound_request.get("quantity_unit") != quantity_unit
            ):
                raise _MissingDurableOrderPreparation(
                    "submission attempt is bound to a different canonical order event"
                )
            try:
                bound_quantity = Decimal(bound_request["requested_quantity"])
            except (KeyError, InvalidOperation, TypeError, ValueError) as error:
                raise _MissingDurableOrderPreparation(
                    "submission attempt is bound to an invalid canonical order quantity"
                ) from error
            if bound_quantity != requested_quantity:
                raise _MissingDurableOrderPreparation(
                    "submission attempt is bound to a different canonical order quantity"
                )
            if snapshot.client_order_id != client_order_id:
                raise _MissingDurableOrderPreparation(
                    "durable order client identity differs from submission"
                )
            if snapshot.origin_intent_id != intent_id:
                raise _MissingDurableOrderPreparation(
                    "durable order origin intent differs from submission"
                )
            if snapshot.instrument != instrument:
                raise _MissingDurableOrderPreparation(
                    "durable order instrument differs from submission scope"
                )
            if snapshot.side != side:
                raise _MissingDurableOrderPreparation(
                    "durable order side differs from submission scope"
                )
            if snapshot.requested_quantity != requested_quantity:
                raise _MissingDurableOrderPreparation(
                    "durable order quantity differs from submission scope"
                )
            if projection._quantity_units.get(client_order_id) != quantity_unit:
                raise _MissingDurableOrderPreparation(
                    "durable order quantity unit differs from submission scope"
                )
            if snapshot.state != "PENDING":
                raise _MissingDurableOrderPreparation(
                    "pre-send durable order must still be PENDING"
                )

        kwargs["prepare_order"] = verified_prepare_order
        return original(self, *args, **kwargs)

    guarded_dispatch._autotrade_requires_production_prepare = True  # type: ignore[attr-defined]
    GuardedDispatcher.dispatch = guarded_dispatch
