import unittest

from mvp.autotrade_mvp.operator_authority_commands import (
    canonical_operator_payload,
    execute_operator_authority_action,
    observed_authority_operation_effects,
    validate_authority_success_evidence,
)
from mvp.autotrade_mvp.persistence import JournalStore


class OperatorAuthorityJournalBoundaryTests(unittest.TestCase):
    @staticmethod
    def _forged_store():
        class ForgedStore(JournalStore):
            pass

        return object.__new__(ForgedStore)

    def test_payload_derivation_rejects_journal_subclass_before_action_parsing(self):
        with self.assertRaisesRegex(TypeError, "exact JournalStore"):
            canonical_operator_payload(
                self._forged_store(),
                object(),
                object(),
                "command",
                "acct",
                "SIMULATION",
            )

    def test_execution_rejects_journal_subclass_before_payload_processing(self):
        with self.assertRaisesRegex(TypeError, "exact JournalStore"):
            execute_operator_authority_action(
                self._forged_store(),
                object(),
                object(),
                object(),
                "acct",
                "SIMULATION",
                "not-a-time",
            )

    def test_observation_rejects_journal_subclass_before_payload_processing(self):
        with self.assertRaisesRegex(TypeError, "exact JournalStore"):
            observed_authority_operation_effects(
                self._forged_store(),
                object(),
                object(),
                object(),
                "acct",
                "SIMULATION",
                "not-a-time",
            )

    def test_success_validation_rejects_journal_subclass_before_payload_processing(self):
        with self.assertRaisesRegex(TypeError, "exact JournalStore"):
            validate_authority_success_evidence(
                self._forged_store(),
                object(),
                object(),
                object(),
                "acct",
                "SIMULATION",
                "not-a-time",
                (),
                (),
            )


if __name__ == "__main__":
    unittest.main()
