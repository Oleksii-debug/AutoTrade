from __future__ import annotations

from copy import copy
from dataclasses import replace
from hashlib import sha256
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.persistence import JournalStore, canonical_json
from mvp.autotrade_mvp.provider_qualification_authority import (
    DurableProviderQualificationRegistry,
    ProviderQualificationAuthorityError,
)
from mvp.tests.test_provider_qualification_authority import ARTIFACT_B, _q


class ProviderQualificationPostIssuanceSealTests(unittest.TestCase):
    def test_mutated_issued_q_fails_before_durable_mutation(self):
        qualification = _q()
        tampered_campaign = replace(
            qualification.campaign,
            account_class="MARGIN",
        )
        object.__setattr__(qualification, "campaign", tampered_campaign)

        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            registry = DurableProviderQualificationRegistry(store)
            before_sequence = store.current_journal_sequence()

            with self.assertRaises(ProviderQualificationAuthorityError):
                registry.register(qualification)

            self.assertEqual(store.current_journal_sequence(), before_sequence)

    def test_copied_issued_q_is_not_durable_authority(self):
        qualification = _q()
        forged_copy = copy(qualification)

        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            registry = DurableProviderQualificationRegistry(store)
            before_sequence = store.current_journal_sequence()

            with self.assertRaises(ProviderQualificationAuthorityError):
                registry.register(forged_copy)

            self.assertEqual(store.current_journal_sequence(), before_sequence)

    def test_mutated_supersession_replacement_fails_before_durable_mutation(self):
        current = _q()
        replacement = _q(artifact_id=ARTIFACT_B)
        object.__setattr__(
            replacement,
            "campaign",
            replace(replacement.campaign, account_class="MARGIN"),
        )

        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            registry = DurableProviderQualificationRegistry(store)
            self.assertTrue(registry.register(current))
            before_sequence = store.current_journal_sequence()

            with self.assertRaises(ProviderQualificationAuthorityError):
                registry.supersede(current.qualification_id, replacement)

            self.assertEqual(store.current_journal_sequence(), before_sequence)
            self.assertEqual(
                registry.historical_current_for(current),
                registry.historical(current.qualification_id),
            )

    def test_mutated_issued_q_with_recomputed_id_still_fails_before_mutation(self):
        qualification = _q()
        tampered_campaign = replace(
            qualification.campaign,
            account_class="MARGIN",
        )
        object.__setattr__(qualification, "campaign", tampered_campaign)
        forged_id = "sha256:" + sha256(
            canonical_json(qualification.authority_payload()).encode("utf-8")
        ).hexdigest()
        object.__setattr__(qualification, "qualification_id", forged_id)

        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            registry = DurableProviderQualificationRegistry(store)
            before_sequence = store.current_journal_sequence()

            with self.assertRaises(ProviderQualificationAuthorityError):
                registry.register(qualification)

            self.assertEqual(store.current_journal_sequence(), before_sequence)


if __name__ == "__main__":
    unittest.main()
