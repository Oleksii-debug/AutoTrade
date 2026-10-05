from __future__ import annotations

from dataclasses import replace
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.durable_financial_request_binding import (
    DurableFinancialRequestBindingError,
    DurableFinancialRequestBindingRegistry,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.tests.test_durable_financial_request_binding import (
    DurableFinancialRequestBindingTests,
)


class DurableFinancialRequestBindingCutTests(unittest.TestCase):
    def test_binding_cannot_claim_a_future_journal_cut(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            fixture = DurableFinancialRequestBindingTests(methodName="runTest")
            _source, admitted = fixture._admitted_case(store)
            material = fixture._material(store, admitted)
            future = replace(
                material,
                admitted_journal_sequence_cut=(
                    store.current_journal_sequence() + 1
                ),
            )

            with self.assertRaisesRegex(
                DurableFinancialRequestBindingError,
                "admitted_journal_sequence_cut",
            ):
                DurableFinancialRequestBindingRegistry(store).bind(
                    admission_id=admitted.admission_id,
                    material=future,
                )


if __name__ == "__main__":
    unittest.main()
