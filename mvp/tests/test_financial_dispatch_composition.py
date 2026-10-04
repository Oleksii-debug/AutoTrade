from inspect import signature
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp import financial_dispatch as financial_dispatch_module
from mvp.autotrade_mvp.authority import AuthorityConflict, AuthorityService
from mvp.autotrade_mvp.dispatch import GuardedDispatcher
from mvp.autotrade_mvp.financial_dispatch import ProductionFinancialDispatcher
from mvp.autotrade_mvp.persistence import JournalStore


INSTRUMENT_ID = "11111111-1111-4111-8111-111111111111"
START = "2026-10-04T07:00:00Z"
BARRIER = "2026-10-04T07:00:07Z"


class ProductionFinancialDispatchCompositionTests(unittest.TestCase):
    def store(self, directory: str, name: str = "journal.sqlite3") -> JournalStore:
        return JournalStore(Path(directory) / name)

    def test_production_seam_permits_only_paper_or_live(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            authority = AuthorityService(store)
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            with self.assertRaisesRegex(ValueError, "PAPER or LIVE"):
                ProductionFinancialDispatcher(dispatcher, authority)

    def test_production_seam_requires_same_durable_journal_authority(self):
        with TemporaryDirectory() as directory:
            dispatch_store = self.store(directory, "dispatch.sqlite3")
            authority_store = self.store(directory, "authority.sqlite3")
            dispatcher = GuardedDispatcher(
                dispatch_store,
                environment="PAPER",
                account_id="acct",
                owner_token="owner",
            )
            authority = AuthorityService(authority_store)
            with self.assertRaisesRegex(AuthorityConflict, "share one durable journal"):
                ProductionFinancialDispatcher(dispatcher, authority)

    def test_production_api_does_not_expose_replaceable_authority_or_barrier_clock(self):
        parameters = signature(ProductionFinancialDispatcher.dispatch).parameters
        self.assertNotIn("authority_check", parameters)
        self.assertNotIn("final_barrier_clock", parameters)
        self.assertIn("sender_check", parameters)
        self.assertIn("transport_send", parameters)

    def test_scope_retarget_after_dispatcher_construction_fails_closed(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            dispatcher = GuardedDispatcher(
                store,
                environment="PAPER",
                account_id="acct",
                owner_token="owner",
            )
            production = ProductionFinancialDispatcher(
                dispatcher,
                AuthorityService(store),
            )
            dispatcher.account_id = "retargeted-account"

            with self.assertRaisesRegex(PermissionError, "scope changed"):
                production.dispatch(
                    admission_id="admission",
                    attempt_id="attempt",
                    intent_id="intent",
                    intent_hash="sha256:" + "a" * 64,
                    provider="provider",
                    request={},
                    now=START,
                    instrument_id=INSTRUMENT_ID,
                    instrument_version=1,
                    action="ORDER.SUBMIT",
                    transport_send=lambda *_args: {"ok": True},
                    sender_check=lambda _owner, _epoch: None,
                )

    def test_canonical_guard_is_rechecked_with_fresh_internal_barrier_time(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            authority = AuthorityService(store)
            dispatcher = GuardedDispatcher(
                store,
                environment="PAPER",
                account_id="acct",
                owner_token="owner",
            )
            production = ProductionFinancialDispatcher(dispatcher, authority)
            checks = []
            sends = []

            def guard(intent_hash, now):
                checks.append((intent_hash, now))
                return True, "allowed"

            def transport(client_order_id, request, final_guard):
                final_guard()
                sends.append((client_order_id, dict(request)))
                return {"provider_order_id": "provider-order"}

            with patch.object(
                AuthorityService,
                "dispatch_guard",
                return_value=guard,
            ) as bind_guard, patch.object(
                financial_dispatch_module,
                "_utc_now_text",
                return_value=BARRIER,
            ):
                outcome = production.dispatch(
                    admission_id="admission",
                    attempt_id="attempt",
                    intent_id="intent",
                    intent_hash="sha256:" + "a" * 64,
                    provider="provider",
                    request={"side": "BUY"},
                    now=START,
                    instrument_id=INSTRUMENT_ID,
                    instrument_version=1,
                    action="ORDER.SUBMIT",
                    capability_snapshot_id="capability",
                    transport_send=transport,
                    sender_check=lambda _owner, _epoch: None,
                )

            self.assertEqual(outcome.status, "SENT")
            self.assertEqual(len(sends), 1)
            self.assertEqual(
                checks,
                [
                    ("sha256:" + "a" * 64, START),
                    ("sha256:" + "a" * 64, BARRIER),
                ],
            )
            bind_guard.assert_called_once_with(
                authority,
                "admission",
                account_id="acct",
                environment="PAPER",
                instrument_id=INSTRUMENT_ID,
                instrument_version=1,
                action="ORDER.SUBMIT",
                capability_snapshot_id="capability",
            )


if __name__ == "__main__":
    unittest.main()
