import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from mvp.autotrade_mvp.authority import (
    AuthorityConflict,
    AuthorityPolicy,
    AuthorityService,
    InstrumentVersionIdentity,
)
from mvp.autotrade_mvp.persistence import JournalStore


INSTRUMENT_ID = "11111111-1111-4111-8111-111111111111"


def _policy(policy_id: str) -> AuthorityPolicy:
    return AuthorityPolicy.create(
        policy_id=policy_id,
        account_id="account-1",
        environments={"PAPER"},
        instruments={InstrumentVersionIdentity(INSTRUMENT_ID, 1)},
        actions={"BUY"},
        max_notional=Decimal("10"),
        valid_from="2026-10-02T00:00:00Z",
        expires_at="2026-10-03T00:00:00Z",
        autonomous=False,
    )


class _JournalSubclass(JournalStore):
    pass


class AuthorityServiceJournalAuthorityTests(unittest.TestCase):
    def test_constructor_rejects_journal_subclass_before_restore_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            store = _JournalSubclass(Path(directory) / "journal.sqlite")
            with self.assertRaisesRegex(TypeError, "exact JournalStore"):
                AuthorityService(store)

    def test_constructor_rejects_instance_shadow_before_restore_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite")
            executed = False

            def hostile_load_events(*_args, **_kwargs):
                nonlocal executed
                executed = True
                raise AssertionError("instance shadow must not execute")

            store.load_events = hostile_load_events
            with self.assertRaisesRegex(TypeError, "shadowed"):
                AuthorityService(store)
            self.assertFalse(executed)

    def test_post_construction_shadow_fails_before_authority_write(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite")
            service = AuthorityService(store)
            executed = False

            def hostile_next_version(*_args, **_kwargs):
                nonlocal executed
                executed = True
                raise AssertionError("instance shadow must not execute")

            store.next_aggregate_version = hostile_next_version
            with self.assertRaisesRegex(TypeError, "shadowed"):
                service.register_policy(_policy("policy-shadow"))
            self.assertFalse(executed)
            self.assertEqual(
                JournalStore.load_events(store, "authority_state", "canonical"),
                [],
            )

    def test_post_construction_store_swap_fails_before_authority_write(self):
        with tempfile.TemporaryDirectory() as directory:
            first = JournalStore(Path(directory) / "first.sqlite")
            second = JournalStore(Path(directory) / "second.sqlite")
            service = AuthorityService(first)
            service.store = second

            with self.assertRaisesRegex(
                AuthorityConflict,
                "JournalStore changed",
            ):
                service.register_policy(_policy("policy-swap"))
            self.assertEqual(
                JournalStore.load_events(first, "authority_state", "canonical"),
                [],
            )
            self.assertEqual(
                JournalStore.load_events(second, "authority_state", "canonical"),
                [],
            )


if __name__ == "__main__":
    unittest.main()
