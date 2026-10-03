"""Provider-accounting facade with fresh provider-fill authority fences.

The retained facade owns the already-qualified provider cash replay bridge. This
layer changes no financial authority: it prevents generic historical entrypoints
from publishing fresh provider-fill financial effects without the current
reservation/evidence bindings while preserving exact legacy retries.
"""

from __future__ import annotations

from typing import Iterable, Mapping

from . import _provider_activity_accounting_facade as _facade
from ._provider_activity_accounting_facade import *  # noqa: F401,F403
from . import _provider_activity_accounting_impl as _impl
from .persistence import (
    JournalStore,
    journal_store_authority_scope,
)


def __getattr__(name: str):
    """Preserve the retained facade/implementation compatibility surface."""

    return getattr(_facade, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_facade)))


def _provider_fill_transaction_is_bound(
    economic_book: _impl.DurableProviderEconomicBook,
    transaction_id: str,
) -> bool:
    """Resolve transaction ownership from durable provider-fill binding events.

    Binding rows are descriptive evidence committed atomically with the original
    reservation consumption and economics. They are therefore the authority for
    deciding whether a correction must use the reservation-aware provider-fill
    path; transaction-id prefixes or caller-supplied fill metadata are not.
    """

    target = _impl._text(transaction_id, name="reversed transaction_id")
    store, store_identity = _impl._require_exact_financial_book(
        economic_book,
        _impl.DurableProviderEconomicBook,
        name="economic_book",
    )
    with journal_store_authority_scope(store, store_identity):
        events = JournalStore.load_events_by_aggregate_type(
            store,
            _impl._PROVIDER_FILL_BINDING_AGGREGATE_TYPE,
        )

    matches = 0
    for event in events:
        if event.get("event_type") != _impl._PROVIDER_FILL_BINDING_EVENT_TYPE:
            raise _impl.AccountingConflict(
                "provider fill financial binding contains unsupported event type"
            )
        if event.get("aggregate_version") != 1:
            raise _impl.AccountingConflict(
                "provider fill financial binding version is invalid"
            )
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            raise _impl.AccountingConflict(
                "provider fill financial binding payload is invalid"
            )
        if _impl.payload_digest(payload) != event.get("payload_hash"):
            raise _impl.AccountingConflict(
                "provider fill financial binding payload hash is invalid"
            )
        request = payload.get("request")
        if not isinstance(request, Mapping):
            raise _impl.AccountingConflict(
                "provider fill financial binding request is invalid"
            )
        request = dict(request)
        if payload.get("request_digest") != _impl.payload_digest(request):
            raise _impl.AccountingConflict(
                "provider fill financial binding request digest is invalid"
            )
        if (
            payload.get("provider_id") != request.get("provider_id")
            or payload.get("account_id") != request.get("account_id")
            or payload.get("environment") != request.get("environment")
            or payload.get("provider_execution_id")
            != request.get("provider_execution_id")
        ):
            raise _impl.AccountingConflict(
                "provider fill financial binding scope is inconsistent"
            )

        request_environment = request.get("environment")
        request_provider_environment = request.get(
            "provider_environment", request_environment
        )
        in_scope = (
            request.get("provider_id") == economic_book.provider_id
            and request.get("account_id") == economic_book.account_id
            and request_environment == economic_book.environment
            and request_provider_environment == economic_book.provider_environment
        )
        if not in_scope:
            continue
        if request.get("transaction_id") == target:
            matches += 1

    if matches > 1:
        raise _impl.AccountingConflict(
            "provider fill transaction has duplicate financial-binding ownership"
        )
    return matches == 1


def _legacy_provider_fill_correction_is_already_durable(
    economic_book: _impl.DurableProviderEconomicBook,
    settlement_book: _impl.DurableSettlementBook,
    *,
    reversal: _impl.JournalTransaction,
    replacement: _impl.JournalTransaction,
    settlement_obligations: tuple[_impl.SettlementObligation, ...],
    committed_at: str | None,
) -> bool:
    """Admit only already-durable legacy effects, never a fresh publication."""

    economic_plan = economic_book.prepare_batch_mutation(
        (reversal, replacement),
        committed_at=committed_at,
    )
    settlement_plan = settlement_book.prepare_register_mutation(
        settlement_obligations,
        committed_at=committed_at,
    )
    return economic_plan.already_committed and settlement_plan.already_committed


def commit_economic_correction_with_settlement_replacement(
    economic_book: _impl.DurableProviderEconomicBook,
    settlement_book: _impl.DurableSettlementBook,
    *,
    command_id: str,
    idempotency_key: str,
    reversal: _impl.JournalTransaction,
    replacement: _impl.JournalTransaction,
    settlement_obligations: Iterable[_impl.SettlementObligation],
    committed_at: str | None = None,
    reservation_book: _impl.DurableReservationBook | None = None,
    reservation_id: str | None = None,
    provider_fill_correction_binding: _impl.PreparedProviderFillCorrectionBinding | None = None,
) -> bool:
    """Fence fresh generic corrections of reservation-owned provider fills.

    Exact legacy retry/readback remains compatible when both economic and
    settlement effects are already durable. A first-time mutation of a current
    provider-fill-owned transaction must instead arrive through
    ``commit_provider_fill_correction_with_settlement_replacement`` so the
    conservative reservation high-water binding participates atomically.
    """

    items = tuple(settlement_obligations)
    if (
        reservation_book is None
        and provider_fill_correction_binding is None
        and type(reversal) is _impl.JournalTransaction
        and reversal.reverses_transaction_id is not None
        and _provider_fill_transaction_is_bound(
            economic_book,
            reversal.reverses_transaction_id,
        )
        and not _legacy_provider_fill_correction_is_already_durable(
            economic_book,
            settlement_book,
            reversal=reversal,
            replacement=replacement,
            settlement_obligations=items,
            committed_at=committed_at,
        )
    ):
        raise _impl.AccountingConflict(
            "provider-fill-owned correction requires reservation-aware correction authority"
        )

    return _impl.commit_economic_correction_with_settlement_replacement(
        economic_book,
        settlement_book,
        command_id=command_id,
        idempotency_key=idempotency_key,
        reversal=reversal,
        replacement=replacement,
        settlement_obligations=items,
        committed_at=committed_at,
        reservation_book=reservation_book,
        reservation_id=reservation_id,
        provider_fill_correction_binding=provider_fill_correction_binding,
    )


def commit_economic_batch_with_reservation_consumption(
    economic_book: _impl.DurableProviderEconomicBook,
    reservation_book: _impl.DurableReservationBook,
    *,
    command_id: str,
    idempotency_key: str,
    reservation_id: str,
    usage: Mapping[str, object],
    transactions: Iterable[_impl.JournalTransaction],
    reservation_expected_snapshot_digest: str | None = None,
    committed_at: str | None = None,
    settlement_book: _impl.DurableSettlementBook | None = None,
    settlement_obligations: Iterable[_impl.SettlementObligation] = (),
    provider_fill_binding: _impl.PreparedProviderFillBinding | None = None,
) -> bool:
    """Fence public generic publication of provider-fill-bound financial effects.

    The retained generic barrier remains available for its legacy non-provider
    callers. A provider fill, however, must enter through
    ``commit_provider_fill_with_reservation_consumption``. That canonical
    entrypoint derives both the economic transaction and reservation usage from
    the same independently evidenced fill and admission-bound reservation cut.

    Accepting a caller-supplied ``PreparedProviderFillBinding`` here would let a
    generic caller pair descriptive provider evidence with independently chosen
    ``usage``/``transactions``. The implementation's private provider-fill
    entrypoint still calls the underlying atomic barrier directly after deriving
    those values, so this facade fence does not weaken or duplicate authority.
    """

    if provider_fill_binding is not None:
        raise _impl.AccountingConflict(
            "provider-fill-bound atomic commit requires evidence-derived provider fill entrypoint"
        )

    return _impl.commit_economic_batch_with_reservation_consumption(
        economic_book,
        reservation_book,
        command_id=command_id,
        idempotency_key=idempotency_key,
        reservation_id=reservation_id,
        usage=usage,
        transactions=transactions,
        reservation_expected_snapshot_digest=reservation_expected_snapshot_digest,
        committed_at=committed_at,
        settlement_book=settlement_book,
        settlement_obligations=settlement_obligations,
        provider_fill_binding=None,
    )
