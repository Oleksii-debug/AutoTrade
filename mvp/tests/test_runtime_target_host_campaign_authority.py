from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mvp.autotrade_mvp.journal_taxonomy import taxonomy_digest
from mvp.autotrade_mvp.performance_qualification import RuntimeBudgetSpec
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.runtime_load_evidence import ExpectedJournalEvent
from mvp.autotrade_mvp.runtime_load_plan import declare_runtime_event_plan
from mvp.autotrade_mvp.runtime_target_host_campaign_authority import (
    RuntimeTargetHostCampaignAuthority,
    RuntimeTargetHostCampaignAuthorityError,
    declare_runtime_target_host_campaign_authority,
    load_runtime_target_host_campaign_authority,
)


SHA = "a" * 40
CONFIG = "sha256:" + ("b" * 64)
HOST = "sha256:" + ("c" * 64)
ARTIFACT_ID = "00000000-0000-0000-0000-000000000065"
ARTIFACT_SHA = "sha256:" + ("d" * 64)


def _spec() -> RuntimeBudgetSpec:
    return RuntimeBudgetSpec(
        scenario_id="target-host-current-stack",
        release_sha=SHA,
        configuration_hash=CONFIG,
        host_fingerprint=HOST,
        strategy_horizon_us=1_000,
        max_p95_financial_latency_us=500,
        max_financial_staleness_us=500,
        max_research_interference_us=500,
        min_financial_samples=1,
        min_research_samples=1,
    )


def _expected(event_id: str, version: int = 1) -> ExpectedJournalEvent:
    return ExpectedJournalEvent(
        event_id=event_id,
        event_type="RuntimeQualificationFinancialEvent",
        aggregate_type="risk_decision",
        aggregate_id="target-host-current-stack",
        aggregate_version=version,
    )


class RuntimeTargetHostCampaignAuthorityTests(unittest.TestCase):
    def _store(self, root: str, name: str = "journal.sqlite3") -> JournalStore:
        return JournalStore(Path(root) / name)

    def _plan(
        self,
        store: JournalStore,
        *,
        plan_id: str = "financial-plan",
        event_id: str = "financial-1",
    ):
        return declare_runtime_event_plan(
            store,
            plan_id=plan_id,
            spec=_spec(),
            expected_events=(_expected(event_id),),
        )

    def test_declaration_binds_current_plan_store_taxonomy_and_release_before_samples(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            plan = self._plan(store)

            authority = declare_runtime_target_host_campaign_authority(
                store,
                _spec(),
                authority_id="target-host-run-1",
                financial_plan_id=plan.plan_id,
                release_artifact_id=ARTIFACT_ID,
                release_artifact_sha256=ARTIFACT_SHA,
            )

            self.assertEqual(authority.source_sha, SHA)
            self.assertEqual(authority.spec_digest, _spec().digest)
            self.assertEqual(authority.configuration_hash, CONFIG)
            self.assertEqual(authority.host_fingerprint, HOST)
            self.assertEqual(authority.release_artifact_id, ARTIFACT_ID)
            self.assertEqual(authority.release_artifact_sha256, ARTIFACT_SHA)
            self.assertEqual(authority.financial_plan_id, plan.plan_id)
            self.assertEqual(authority.financial_plan_digest, plan.digest)
            self.assertEqual(authority.store_identity_digest, plan.store_identity_digest)
            self.assertEqual(authority.journal_taxonomy_digest, taxonomy_digest())
            self.assertGreater(
                authority.declared_journal_sequence,
                plan.declared_journal_sequence,
            )

            event = store.get_event(authority.event_id)
            self.assertIsNotNone(event)
            self.assertEqual(
                event["event_type"],
                "RuntimeQualificationTargetHostAuthorityDeclared",
            )
            self.assertEqual(event["aggregate_type"], "runtime_qualification_plan")
            self.assertEqual(authority.digest, event["payload_hash"])

            loaded = load_runtime_target_host_campaign_authority(
                store,
                _spec(),
                authority_id=authority.authority_id,
            )
            self.assertEqual(loaded, authority)

    def test_authority_id_may_equal_financial_plan_id_without_aggregate_collision(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            plan = self._plan(store, plan_id="same-id")

            authority = declare_runtime_target_host_campaign_authority(
                store,
                _spec(),
                authority_id=plan.plan_id,
                financial_plan_id=plan.plan_id,
                release_artifact_id=ARTIFACT_ID,
                release_artifact_sha256=ARTIFACT_SHA,
            )

            plan_event = store.get_event(plan.event_id)
            authority_event = store.get_event(authority.event_id)
            self.assertIsNotNone(plan_event)
            self.assertIsNotNone(authority_event)
            self.assertNotEqual(plan_event["aggregate_id"], authority_event["aggregate_id"])
            self.assertGreater(
                authority.declared_journal_sequence,
                plan.declared_journal_sequence,
            )

    def test_exact_redeclaration_is_idempotent_and_does_not_append(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            plan = self._plan(store)
            first = declare_runtime_target_host_campaign_authority(
                store,
                _spec(),
                authority_id="target-host-run-idempotent",
                financial_plan_id=plan.plan_id,
                release_artifact_id=ARTIFACT_ID,
                release_artifact_sha256=ARTIFACT_SHA,
            )
            after_first = store.current_journal_sequence()

            second = declare_runtime_target_host_campaign_authority(
                store,
                _spec(),
                authority_id="target-host-run-idempotent",
                financial_plan_id=plan.plan_id,
                release_artifact_id=ARTIFACT_ID,
                release_artifact_sha256=ARTIFACT_SHA,
            )

            self.assertEqual(second, first)
            self.assertEqual(store.current_journal_sequence(), after_first)

    def test_same_authority_id_cannot_be_reused_for_another_release_artifact(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            plan = self._plan(store)
            declare_runtime_target_host_campaign_authority(
                store,
                _spec(),
                authority_id="target-host-run-conflict",
                financial_plan_id=plan.plan_id,
                release_artifact_id=ARTIFACT_ID,
                release_artifact_sha256=ARTIFACT_SHA,
            )

            with self.assertRaisesRegex(
                RuntimeTargetHostCampaignAuthorityError,
                "already used for different content",
            ):
                declare_runtime_target_host_campaign_authority(
                    store,
                    _spec(),
                    authority_id="target-host-run-conflict",
                    financial_plan_id=plan.plan_id,
                    release_artifact_id=ARTIFACT_ID,
                    release_artifact_sha256="sha256:" + ("e" * 64),
                )

    def test_reload_fails_closed_if_current_taxonomy_no_longer_matches_declaration(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            plan = self._plan(store)
            declare_runtime_target_host_campaign_authority(
                store,
                _spec(),
                authority_id="target-host-run-taxonomy",
                financial_plan_id=plan.plan_id,
                release_artifact_id=ARTIFACT_ID,
                release_artifact_sha256=ARTIFACT_SHA,
            )

            with (
                patch(
                    "mvp.autotrade_mvp.runtime_target_host_campaign_authority.taxonomy_digest",
                    return_value="sha256:" + ("f" * 64),
                ),
                self.assertRaisesRegex(
                    RuntimeTargetHostCampaignAuthorityError,
                    "journal_taxonomy_digest",
                ),
            ):
                load_runtime_target_host_campaign_authority(
                    store,
                    _spec(),
                    authority_id="target-host-run-taxonomy",
                )

    def test_authority_is_bound_to_exact_financial_plan_and_store_generation(self):
        with tempfile.TemporaryDirectory() as root:
            first_store = self._store(root, "first.sqlite3")
            first_plan = self._plan(first_store)
            authority = declare_runtime_target_host_campaign_authority(
                first_store,
                _spec(),
                authority_id="target-host-run-store",
                financial_plan_id=first_plan.plan_id,
                release_artifact_id=ARTIFACT_ID,
                release_artifact_sha256=ARTIFACT_SHA,
            )

            second_store = self._store(root, "second.sqlite3")
            second_plan = self._plan(second_store)
            self.assertNotEqual(
                second_plan.store_identity_digest,
                authority.store_identity_digest,
            )
            with self.assertRaisesRegex(
                RuntimeTargetHostCampaignAuthorityError,
                "not durably declared",
            ):
                load_runtime_target_host_campaign_authority(
                    second_store,
                    _spec(),
                    authority_id=authority.authority_id,
                )

    def test_public_dataclass_cannot_self_mint_authority(self):
        with self.assertRaisesRegex(
            RuntimeTargetHostCampaignAuthorityError,
            "must come from canonical JournalStore",
        ):
            RuntimeTargetHostCampaignAuthority(
                authority_id="forged",
                event_id="forged-event",
                scenario_id="forged",
                spec_digest=CONFIG,
                source_sha=SHA,
                configuration_hash=CONFIG,
                host_fingerprint=HOST,
                release_artifact_id=ARTIFACT_ID,
                release_artifact_sha256=ARTIFACT_SHA,
                financial_plan_id="forged-plan",
                financial_plan_digest=CONFIG,
                store_identity_digest=CONFIG,
                journal_taxonomy_digest=CONFIG,
                declared_journal_sequence=1,
                payload_hash=CONFIG,
            )


if __name__ == "__main__":
    unittest.main()
