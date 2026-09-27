"""Shared real durable-order preparation for provider contract fixtures."""

from __future__ import annotations

from mvp.autotrade_mvp.durable_order_projection import DurableOrderBookProjection
from mvp.autotrade_mvp.persistence import JournalStore


def make_contract_order_preparer(
    store: JournalStore,
    *,
    provider_id: str,
    account_id: str,
    environment: str,
    owner_token: str = "contract-owner",
    owner_epoch: str = "1",
):
    orders = DurableOrderBookProjection(
        store,
        provider_id=provider_id,
        account_id=account_id,
        environment=environment,
        host_id=owner_token,
        owner_epoch=owner_epoch,
    )

    def prepare_order(
        client_order_id,
        attempt_id,
        intent_id,
        provider,
        _request,
        order_preparation_binding,
        prepared_at,
    ):
        if str(provider).upper() != provider_id.upper():
            raise AssertionError("contract provider scope mismatch")
        orders.create_order(
            event_key=f"dispatch-order:{attempt_id}",
            client_order_id=client_order_id,
            instrument=order_preparation_binding["instrument"],
            side=order_preparation_binding["side"],
            requested_quantity=order_preparation_binding["requested_quantity"],
            quantity_unit=order_preparation_binding["quantity_unit"],
            origin_intent_id=intent_id,
            committed_at=prepared_at,
        )

    return prepare_order
