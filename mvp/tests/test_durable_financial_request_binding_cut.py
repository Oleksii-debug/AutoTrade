from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.durable_financial_request_binding import (
    DurableFinancialRequestBindingError,
    DurableFinancialRequestBindingRegistry,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.tests.test_confirmed_pending_admission import NOW
from mvp.tests.test_durable_financial_request_binding import (
    DurableFinancialRequestBindingTests,
)


class DurableFinancialRequestBindingCutTests(unittest.TestCase):
    def test_binding_cannot_claim_any_cut_other_than_exact_admission_event(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            fixture = DurableFinancialRequestBindingTests(methodName="runTest")
            _source, admitted = fixture._admitted_case(store)
            material = fixture._material(store, admitted)
            registry = DurableFinancialRequestBindingRegistry(store)
            bound_at = (NOW + timedelta(seconds=2)).isoformat().replace(
                "+00:00", "Z"
            )

            for claimed in (
                material.admitted_journal_sequence_cut - 1,
                store.current_journal_sequence() + 1,
            ):
                with self.subTest(claimed=claimed):
                    with self.assertRaisesRegex(
                        DurableFinancialRequestBindingError,
                        "admitted_journal_sequence_cut",
                    ):
                        registry.bind(
                            admission_id=admitted.admission_id,
                            material=replace(
                                material,
                                admitted_journal_sequence_cut=claimed,
                            ),
                            bound_at=bound_at,
                        )


if __name__ == "__main__":
    unittest.main()
