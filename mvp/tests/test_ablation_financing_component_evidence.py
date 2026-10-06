from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.ablation_financing_component_evidence import (
    ResolvedAblationFinancingComponentEvidence,
    resolve_ablation_financing_component_evidence,
    reverify_ablation_financing_component_evidence,
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
    kind: str = "FINAL",
    available_at: str = "2026-01-01T00:01:00Z",
) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "provider_id": "ALPACA",
        "account_id": "account-1",
        "environment": "SIMULATION",
        "charge_id": "financing-1",
        "revision": revision,
        "kind": kind,
        "effective_at": "2026-01-01T00:00:00Z",
        "available_at": available_at,
        "unit": "USD",
        "amount": amount,
        "source_account": "BORROW_LIABILITY:USD",
        "charge_scope_type": "ACCOUNT",
        "charge_scope_id": "account-1",
    }


class AblationFinancingComponentEvidenceTests(unittest.TestCase):
    def _fixture(
        self,
        root: Path,
        *,
        amount: str = "5",
        kind: str = "FINAL",
    ):
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
        artifacts.add("financing-artifact-1", _payload(revision=1, amount=amount, kind=kind))
        financing.record_authenticated_artifact(
            artifacts,
            artifact_id="financing-artifact-1",
            committed_at="2026-01-01T00:02:00Z",
        )
        return store, economic, financing, artifacts

    def _append_revision_two(
        self,
        financing: DurableFinancingBook,
        artifacts: _Artifacts,
        *,
        amount: str = "7",
    ) -> None:
        artifacts.add(
            "financing-artifact-2",
            _payload(
                revision=2,
                amount=amount,
                available_at="2026-01-01T00:03:00Z",
            ),
        )
        financing.record_authenticated_artifact(
            artifacts,
            artifact_id="financing-artifact-2",
            committed_at="2026-01-01T00:04:00Z",
        )

    def test_nonzero_final_charge_binds_same_frozen_provider_economic_prefix(self):
        with TemporaryDirectory() as directory:
            _store, _economic, financing, _artifacts = self._fixture(Path(directory))

            evidence = resolve_ablation_financing_component_evidence(
                financing,
                charge_id="financing-1",
                expected_aggregate_version=1,
            )

            self.assertIsInstance(evidence, ResolvedAblationFinancingComponentEvidence)
            self.assertEqual(str(evidence.current_final_charge), "5")
            self.assertEqual(evidence.revision, 1)
            self.assertEqual(len(evidence.contributing_transactions), 1)
            self.assertTrue(evidence.provider_economic_cut_digest.startswith("sha256:"))
            self.assertFalse(evidence.terminal_cost_composite)
            evidence.verify_integrity()

    def test_later_correction_does_not_rewrite_frozen_financing_cut(self):
        with TemporaryDirectory() as directory:
            _store, _economic, financing, artifacts = self._fixture(Path(directory))
            first = resolve_ablation_financing_component_evidence(
                financing,
                charge_id="financing-1",
                expected_aggregate_version=1,
            )
            self._append_revision_two(financing, artifacts)

            replayed = reverify_ablation_financing_component_evidence(financing, first)
            second = resolve_ablation_financing_component_evidence(
                financing,
                charge_id="financing-1",
                expected_aggregate_version=2,
            )

            self.assertEqual(replayed, first)
            self.assertEqual(str(first.current_final_charge), "5")
            self.assertEqual(str(second.current_final_charge), "7")
            self.assertNotEqual(first.evidence_digest, second.evidence_digest)
            self.assertEqual(second.revision, 2)
            self.assertEqual(len(second.contributing_transactions), 2)

    def test_old_revision_is_stale_at_later_visibility(self):
        with TemporaryDirectory() as directory:
            _store, _economic, financing, artifacts = self._fixture(Path(directory))
            self._append_revision_two(financing, artifacts)

            with self.assertRaisesRegex(
                FinancingConflict,
                "aggregate version is stale at visibility cut",
            ):
                resolve_ablation_financing_component_evidence(
                    financing,
                    charge_id="financing-1",
                    expected_aggregate_version=1,
                )

    def test_final_zero_charge_is_valid_owned_zero_without_fake_economic_transaction(self):
        with TemporaryDirectory() as directory:
            _store, _economic, financing, _artifacts = self._fixture(
                Path(directory),
                amount="0",
            )

            evidence = resolve_ablation_financing_component_evidence(
                financing,
                charge_id="financing-1",
                expected_aggregate_version=1,
            )

            self.assertEqual(str(evidence.current_final_charge), "0")
            self.assertEqual(evidence.contributing_transactions, ())
            self.assertIsNone(evidence.provider_economic_cut_digest)
            self.assertIsNone(evidence.provider_economic_resulting_book_digest)

    def test_indicated_estimate_is_not_terminal_cost_component_evidence(self):
        with TemporaryDirectory() as directory:
            _store, _economic, financing, _artifacts = self._fixture(
                Path(directory),
                amount="5",
                kind="INDICATED",
            )

            with self.assertRaisesRegex(
                FinancingConflict,
                "not mature FINAL evidence",
            ):
                resolve_ablation_financing_component_evidence(
                    financing,
                    charge_id="financing-1",
                    expected_aggregate_version=1,
                )

    def test_digest_tamper_fails_before_durable_replay(self):
        with TemporaryDirectory() as directory:
            _store, _economic, financing, _artifacts = self._fixture(Path(directory))
            evidence = resolve_ablation_financing_component_evidence(
                financing,
                charge_id="financing-1",
                expected_aggregate_version=1,
            )
            object.__setattr__(evidence, "evidence_digest", "sha256:" + "f" * 64)

            with self.assertRaisesRegex(
                FinancingConflict,
                "evidence digest does not match canonical material",
            ):
                reverify_ablation_financing_component_evidence(financing, evidence)

    def test_instance_shadowing_of_financing_reader_is_rejected(self):
        with TemporaryDirectory() as directory:
            _store, _economic, financing, _artifacts = self._fixture(Path(directory))
            object.__setattr__(financing, "_events", lambda _charge: [])

            with self.assertRaisesRegex(
                FinancingConflict,
                "shadows canonical methods",
            ):
                resolve_ablation_financing_component_evidence(
                    financing,
                    charge_id="financing-1",
                    expected_aggregate_version=1,
                )


if __name__ == "__main__":
    unittest.main()
