"""Shared PAPER/LIVE dispatcher fixture for durable-order integration tests."""

from mvp.autotrade_mvp.durable_order_projection import DurableOrderBookProjection


def durable_order_preparation(
    dispatcher,
    *,
    instrument,
    side,
    quantity,
    quantity_unit,
):
    binding = {
        "instrument": instrument,
        "side": side,
        "requested_quantity": quantity,
        "quantity_unit": quantity_unit,
    }

    def prepare_order(
        client_order_id,
        attempt_id,
        intent_id,
        provider,
        _request,
        order_binding,
        prepared_at,
    ):
        projection = DurableOrderBookProjection(
            dispatcher.store,
            provider_id=provider,
            account_id=dispatcher.account_id,
            environment=dispatcher.environment,
            host_id=dispatcher.owner_token,
            owner_epoch=str(dispatcher.owner_epoch),
        )
        try:
            existing = projection.order(client_order_id)
        except KeyError:
            existing = None
        if existing is not None and existing.state == "PRE_SEND_ABORTED":
            projection.rearm_submission(
                event_key=f"dispatch-order:{attempt_id}",
                client_order_id=client_order_id,
                attempt_id=attempt_id,
                committed_at=prepared_at,
            )
            return
        projection.create_order(
            event_key=f"dispatch-order:{attempt_id}",
            client_order_id=client_order_id,
            instrument=order_binding["instrument"],
            side=order_binding["side"],
            requested_quantity=order_binding["requested_quantity"],
            quantity_unit=order_binding["quantity_unit"],
            origin_intent_id=intent_id,
            committed_at=prepared_at,
        )

    return {
        "order_preparation_binding": binding,
        "prepare_order": prepare_order,
    }
