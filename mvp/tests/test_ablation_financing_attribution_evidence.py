from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import mvp.autotrade_mvp.ablation_financing_component_evidence as component_module
from mvp.autotrade_mvp.ablation_financing_attribution_evidence import (
    ResolvedAblationFinancingAttributionEvidence,
    resolve_ablation_financing_attribution_evidence,
    reverify_ablation_financing_attribution_evidence,
)
from mvp.autotrade_mvp.ablation_financing_component_evidence import (
    resolve_ablation_financing_component_evidence,
)
from mvp.autotrade_mvp.durable_financing import DurableFinancingBook
from mvp.autotrade_mvp.financing import FinancingConflict
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_activity_accounting import DurableProviderEconomicBook


class _Artifacts:
    def __init__(self) -> None:
        self._items: dict[str, tuple[dict[str, object], bytes]] = {}

    def add(self, artifact_id: str, payload: dict[str, object]) -> None:
        raw = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        self._items[artifact_id] = (
            {
                "artifact_id": artifact_id,
                "sha256": "sha256:" + sha256(raw).hexdigest(),
                "media_type": "application/json",
                "rights": {"storage": True, "export": False},
            },
            raw,
        )

    def read_authenticated_snapshot(self, artifact_id: str):
        return self._items[artifact_id]


def _payload(
    *,
    revision: int,
    amount: str,
    available_at: str,
) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "provider_id": "ALPACA",
        "account_id": "account-1",
        "environment": "SIMULATION",
        "charge_id": "financing-1",
        "revision": revision,
        "kind": "FINAL",
        "effective_at": "2026-01-01T00:00:00Z",
        "available_at": available_at,
        "unit": "USD",
        "amount": amount,
        "source_account": "BORROW_LIABILITY:USD",
        "charge_scope_type": "ACCOUNT",
        "charge_scope_id": "account-1",
    }


class AblationFinancingAttributionEvidenceTests(unittest.TestCase):
    def _fixture(self, root: Path):
        store = JournalStore(root / "journal.sqlite3")
        economic = DurableProviderEconomicBook(
            store,
            provider_id="ALPACA",
            account_id="account-1",
            environment="SIMULATION",
        )
        financing = DurableFinancingBook(
            store,
            economic,
            provider_id="ALPACA",
            account_id="account-1",
            environment="SIMULATION",
        )
        artifacts = _Artifacts()
        artifacts.add(
            "financing-artifact-1",
            _payload(
                revision=1,
                amount="5",
                available_at="2026-01-01T00:01:00Z",
            ),
        )
        financing.record_authenticated_artifact(
            artifacts,
            artifact_id="financing-artifact-1",
            committed_at="2026-01-01T00:02:00Z",
        )
        numeric = resolve_ablation_financing_component_evidence(
            financing,
            charge_id="financing-1",
            expected_aggregate_version=1,
        )
        return store, economic, financing, artifacts, numeric

    def _append_correction(
        self,
        financing: DurableFinancingBook,
        artifacts: _Artifacts,
    ) -> None:
        artifacts.add(
            "financing-artifact-2",
            _payload(
                revision=2,
                amount="7",
                available_at="2026-01-01T00:03:00Z",
            ),
        )
        financing.record_authenticated_artifact(
            artifacts,
            artifact_id="financing-artifact-2",
            committed_at="2026-01-01T00:04:00Z",
        )

    def test_account_scoped_borrow_liability_is_not_relabelled_as_financing(self):
        with TemporaryDirectory() as directory:
            _store, _economic, financing, _artifacts, numeric = self._fixture(
                Path(directory)
            )

            evidence = resolve_ablation_financing_attribution_evidence(
                financing,
                numeric,
            )

            self.assertIsInstance(
                evidence,
                ResolvedAblationFinancingAttributionEvidence,
            )
            self.assertEqual(evidence.source_account, "BORROW_LIABILITY:USD")
            self.assertEqual(evidence.charge_scope_type, "ACCOUNT")
            self.assertEqual(evidence.charge_scope_id, "account-1")
            self.assertEqual(evidence.attribution_status, "UNATTRIBUTED")
            self.assertIsNone(evidence.registered_cost_component)
            self.assertFalse(evidence.terminal_cost_component)
            self.assertEqual(
                evidence.attribution_blocker,
                "registered_financing_cost_component_attribution_unavailable",
            )
            self.assertFalse(hasattr(evidence, "amount"))
            self.assertFalse(hasattr(evidence, "cost"))
            evidence.verify_integrity()

    def test_attribution_binds_exact_resolved_financing_evidence_digest(self):
        with TemporaryDirectory() as directory:
            _store, _economic, financing, _artifacts, numeric = self._fixture(
                Path(directory)
            )

            evidence = resolve_ablation_financing_attribution_evidence(
                financing,
                numeric,
            )

            self.assertEqual(
                evidence.financing_evidence_digest,
                numeric.evidence_digest,
            )
            self.assertEqual(evidence.charge_id, numeric.charge_id)
            self.assertEqual(evidence.aggregate_version, numeric.aggregate_version)
            self.assertEqual(
                evidence.visibility_journal_sequence,
                numeric.visibility_journal_sequence,
            )
            self.assertTrue(evidence.evidence_digest.startswith("sha256:"))

    def test_later_correction_does_not_rewrite_frozen_attribution_cut(self):
        with TemporaryDirectory() as directory:
            _store, _economic, financing, artifacts, numeric = self._fixture(
                Path(directory)
            )
            first = resolve_ablation_financing_attribution_evidence(
                financing,
                numeric,
            )
            self._append_correction(financing, artifacts)

            replayed = reverify_ablation_financing_attribution_evidence(
                financing,
                numeric,
                first,
            )
            second_numeric = resolve_ablation_financing_component_evidence(
                financing,
                charge_id="financing-1",
                expected_aggregate_version=2,
            )
            second = resolve_ablation_financing_attribution_evidence(
                financing,
                second_numeric,
            )

            self.assertEqual(replayed, first)
            self.assertEqual(first.aggregate_version, 1)
            self.assertEqual(second.aggregate_version, 2)
            self.assertNotEqual(first.evidence_digest, second.evidence_digest)
            self.assertEqual(second.attribution_status, "UNATTRIBUTED")

    def test_scope_tamper_fails_integrity_before_replay(self):
        with TemporaryDirectory() as directory:
            _store, _economic, financing, _artifacts, numeric = self._fixture(
                Path(directory)
            )
            evidence = resolve_ablation_financing_attribution_evidence(
                financing,
                numeric,
            )
            object.__setattr__(evidence, "charge_scope_type", "INSTRUMENT")

            with self.assertRaisesRegex(
                FinancingConflict,
                "digest does not match canonical material",
            ):
                reverify_ablation_financing_attribution_evidence(
                    financing,
                    numeric,
                    evidence,
                )

    def test_component_reverification_rebinding_fails_before_hostile_callback(self):
        calls: list[str] = []
        with TemporaryDirectory() as directory:
            _store, _economic, financing, _artifacts, numeric = self._fixture(
                Path(directory)
            )
            original = component_module.reverify_ablation_financing_component_evidence

            def hostile(*_args, **_kwargs):
                calls.append("reverify")
                raise AssertionError("hostile component reverifier executed")

            component_module.reverify_ablation_financing_component_evidence = hostile
            try:
                with self.assertRaisesRegex(
                    FinancingConflict,
                    "reverification executable changed",
                ):
                    resolve_ablation_financing_attribution_evidence(
                        financing,
                        numeric,
                    )
            finally:
                component_module.reverify_ablation_financing_component_evidence = original
            self.assertEqual(calls, [])

    def test_instance_shadowing_of_durable_event_reader_is_rejected(self):
        with TemporaryDirectory() as directory:
            _store, _economic, financing, _artifacts, numeric = self._fixture(
                Path(directory)
            )
            object.__setattr__(financing, "_events", lambda _charge: [])

            with self.assertRaisesRegex(
                FinancingConflict,
                "shadows canonical event reader",
            ):
                resolve_ablation_financing_attribution_evidence(
                    financing,
                    numeric,
                )


if __name__ == "__main__":
    unittest.main()
