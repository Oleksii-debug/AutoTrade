import unittest
from unittest.mock import patch

from mvp.autotrade_mvp import _provider_activity_accounting_impl as _impl
from mvp.autotrade_mvp import provider_activity_accounting as provider_accounting


class ProviderFillPublicEntrypointFenceTests(unittest.TestCase):
    def test_public_generic_barrier_is_not_replaced_by_internal_provenance_wrapper(self):
        self.assertIsNot(
            provider_accounting.commit_economic_batch_with_reservation_consumption,
            _impl.commit_economic_batch_with_reservation_consumption,
        )
        self.assertEqual(
            provider_accounting.commit_economic_batch_with_reservation_consumption.__module__,
            provider_accounting.__name__,
        )

    def test_public_generic_barrier_rejects_provider_fill_binding_before_dispatch(self):
        binding = _impl.PreparedProviderFillBinding(
            aggregate_id="provider-fill-binding-test",
            envelope=None,
            request={"provider_execution_id": "execution-1"},
            result={"plan_digest": "sha256:" + "0" * 64},
            aggregate_version=1,
            already_committed=True,
        )

        with patch.object(
            _impl,
            "commit_economic_batch_with_reservation_consumption",
        ) as underlying:
            with self.assertRaisesRegex(
                _impl.AccountingConflict,
                "requires evidence-derived provider fill entrypoint",
            ):
                provider_accounting.commit_economic_batch_with_reservation_consumption(
                    object(),
                    object(),
                    command_id="generic-provider-fill-command",
                    idempotency_key="generic-provider-fill-idempotency",
                    reservation_id="reservation-1",
                    usage={"CASH:USD": "99"},
                    transactions=(),
                    provider_fill_binding=binding,
                )

        underlying.assert_not_called()

    def test_legacy_unbound_generic_barrier_still_delegates_unchanged(self):
        with patch.object(
            _impl,
            "commit_economic_batch_with_reservation_consumption",
            return_value=True,
        ) as underlying:
            result = provider_accounting.commit_economic_batch_with_reservation_consumption(
                "economic-book",
                "reservation-book",
                command_id="legacy-command",
                idempotency_key="legacy-idempotency",
                reservation_id="reservation-1",
                usage={"CASH:USD": "1"},
                transactions=("legacy-transaction",),
                reservation_expected_snapshot_digest="sha256:" + "1" * 64,
                committed_at="2026-10-05T12:40:00Z",
                settlement_book=None,
                settlement_obligations=(),
            )

        self.assertTrue(result)
        underlying.assert_called_once_with(
            "economic-book",
            "reservation-book",
            command_id="legacy-command",
            idempotency_key="legacy-idempotency",
            reservation_id="reservation-1",
            usage={"CASH:USD": "1"},
            transactions=("legacy-transaction",),
            reservation_expected_snapshot_digest="sha256:" + "1" * 64,
            committed_at="2026-10-05T12:40:00Z",
            settlement_book=None,
            settlement_obligations=(),
            provider_fill_binding=None,
        )

    def test_canonical_provider_fill_entrypoint_remains_exported(self):
        self.assertIs(
            provider_accounting.commit_provider_fill_with_reservation_consumption,
            _impl.commit_provider_fill_with_reservation_consumption,
        )


if __name__ == "__main__":
    unittest.main()
