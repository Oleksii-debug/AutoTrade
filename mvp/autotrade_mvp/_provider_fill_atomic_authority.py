"""Fail-closed provenance boundary for atomic PAPER/LIVE fill composition.

PreparedProviderFillBinding is intentionally a transparent audit value, not a
capability type.  Public low-level financial composition must therefore not
infer provider provenance merely because a caller can construct that dataclass.
This module records the exact binding objects emitted by the canonical provider
binding preparer and requires PAPER/LIVE atomic composition to use one of those
objects while rejoining its evidence-derived economics and OMS identity.
"""

from __future__ import annotations

from typing import Mapping
import weakref

from . import provider_activity_accounting as _public
from . import _provider_activity_accounting_impl as _impl


_original_prepare_provider_fill_binding = _impl._prepare_provider_fill_binding
_original_commit_economic_batch = (
    _impl.commit_economic_batch_with_reservation_consumption
)

# id -> weakref avoids relying on dataclass hashing (the binding contains dicts).
_prepared_bindings: dict[int, weakref.ReferenceType] = {}


def _register_binding(binding: object) -> None:
    if type(binding) is not _impl.PreparedProviderFillBinding:
        raise TypeError(
            "canonical provider fill preparer returned non-canonical binding"
        )
    object_id = id(binding)

    def discard(reference, *, object_id=object_id):
        current = _prepared_bindings.get(object_id)
        if current is reference:
            _prepared_bindings.pop(object_id, None)

    _prepared_bindings[object_id] = weakref.ref(binding, discard)


def _require_registered_binding(binding: object) -> None:
    if type(binding) is not _impl.PreparedProviderFillBinding:
        raise TypeError(
            "provider_fill_binding must be exact PreparedProviderFillBinding"
        )
    reference = _prepared_bindings.get(id(binding))
    if reference is None or reference() is not binding:
        raise _impl.AccountingConflict(
            "PAPER/LIVE provider fill binding lacks canonical provider-evidence authority"
        )


def _prepare_provider_fill_binding(*args, **kwargs):
    binding = _original_prepare_provider_fill_binding(*args, **kwargs)
    _register_binding(binding)
    return binding


def _require_binding_matches_atomic_effects(
    binding: object,
    *,
    reservation_id: object,
    usage: object,
    transactions: tuple[object, ...],
    order_fill_plan: object,
) -> None:
    _require_registered_binding(binding)
    request = binding.request
    if type(request) is not dict:
        raise _impl.AccountingConflict(
            "canonical provider fill binding request is not exact durable JSON"
        )
    normalized_reservation = _impl._text(
        reservation_id,
        name="reservation_id",
    )
    normalized_usage = _impl._positive_usage_map(
        usage,
        name="provider-bound atomic financial usage",
    )
    if request.get("reservation_id") != normalized_reservation:
        raise _impl.AccountingConflict(
            "provider-derived financial binding does not match reservation identity"
        )
    if len(transactions) != 1:
        raise _impl.AccountingConflict(
            "provider-derived OMS fill must bind exactly one economic transaction"
        )
    transaction = transactions[0]
    if not isinstance(transaction, _impl.JournalTransaction):
        raise TypeError("transactions must contain JournalTransaction")
    if (
        request.get("transaction_id") != transaction.transaction_id
        or request.get("transaction_digest")
        != _impl.payload_digest(_impl.canonical_transaction(transaction))
        or request.get("derived_usage") != _impl._usage_payload(normalized_usage)
    ):
        raise _impl.AccountingConflict(
            "provider-derived financial binding does not match supplied economics"
        )

    if type(order_fill_plan) is not _impl.PreparedOrderMutation:
        raise TypeError("order_fill_plan must be exact PreparedOrderMutation")
    projected = request.get("projected_fill")
    if type(projected) is not dict:
        raise _impl.AccountingConflict(
            "provider-derived financial binding lacks canonical projected fill"
        )
    order_request = order_fill_plan.request
    if type(order_request) is not dict:
        raise _impl.AccountingConflict(
            "prepared OMS fill request is not canonical"
        )
    expected_order_request = {
        "client_order_id": projected.get("client_order_id"),
        "fill_id": projected.get("fill_id"),
        "provider_execution_id": projected.get("provider_execution_id"),
        "quantity": projected.get("quantity"),
        "price": projected.get("price"),
        "provider_revision": projected.get("provider_revision"),
    }
    if order_request != expected_order_request:
        raise _impl.AccountingConflict(
            "provider-derived financial binding does not own the supplied OMS fill"
        )


def commit_economic_batch_with_reservation_consumption(
    economic_book,
    reservation_book,
    **kwargs,
):
    """Seal PAPER/LIVE low-level composition to canonical provider preparation."""

    environment = getattr(economic_book, "environment", None)
    binding = kwargs.get("provider_fill_binding")
    if environment in {"PAPER", "LIVE"} and binding is not None:
        transaction_batch = tuple(kwargs.get("transactions", ()))
        kwargs["transactions"] = transaction_batch
        _require_binding_matches_atomic_effects(
            binding,
            reservation_id=kwargs.get("reservation_id"),
            usage=kwargs.get("usage"),
            transactions=transaction_batch,
            order_fill_plan=kwargs.get("_order_fill_plan"),
        )
    return _original_commit_economic_batch(
        economic_book,
        reservation_book,
        **kwargs,
    )


# Functions retained in the implementation resolve these names through module
# globals, while external imports resolve the public facade attributes. Bind both
# surfaces so provider-evidence and low-level paths cannot diverge.
_impl._prepare_provider_fill_binding = _prepare_provider_fill_binding
_impl.commit_economic_batch_with_reservation_consumption = (
    commit_economic_batch_with_reservation_consumption
)
_public.commit_economic_batch_with_reservation_consumption = (
    commit_economic_batch_with_reservation_consumption
)
