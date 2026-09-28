from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from decimal import Decimal
import json
from pathlib import Path
from uuid import uuid4
import tempfile
import unittest
from typing import Mapping

from mvp.autotrade_mvp.durable_financing import (
    DurableFinancingBook,
    _event_payload,
    _revision_book_digest,
    authenticated_financing_event,
)
from mvp.autotrade_mvp.financing import FinancingConflict, FinancingError
from mvp.autotrade_mvp.instruments import InstrumentRegistry, InstrumentVersion
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_activity_accounting import DurableProviderEconomicBook
from mvp.autotrade_mvp.capabilities import (
    CapabilityClaim,
    EvidenceVerification,
    derive_capability_snapshot,
)
from mvp.autotrade_mvp.provider_core import (
    Surface,
    observe_authenticated_json_response,
    prepare_authenticated_read_query,
)
from autotrade_research.artifacts import ArtifactStore


BASE = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


BYBIT_XRP_INSTRUMENT_ID = "77777777-7777-4777-8777-777777777777"
BYBIT_XRP_UNDERLYING_ID = "88888888-8888-4888-8888-888888888888"


def bybit_instrument_registry(
    *,
    symbol: str = "XRPUSDT",
    settlement_currency: str = "USDT",
    provider_id: str = "BYBIT",
    effective_from: datetime | None = None,
):
    version = InstrumentVersion(
        instrument_id=BYBIT_XRP_INSTRUMENT_ID,
        version=1,
        provider_id=provider_id,
        venue_id="BYBIT-LINEAR",
        provider_symbol=symbol,
        asset_class="PERPETUAL",
        base_currency="XRP",
        quote_currency="USDT",
        settlement_currency=settlement_currency,
        quantity_unit="XRP",
        contract_multiplier=Decimal("1"),
        price_tick=Decimal("0.0001"),
        quantity_step=Decimal("0.1"),
        minimum_quantity=Decimal("0.1"),
        calendar_id="CONTINUOUS_24_7",
        timezone_id="UTC",
        effective_from=effective_from or BASE - timedelta(days=30),
        payoff="LINEAR",
        underlying_id=f"{BYBIT_XRP_UNDERLYING_ID}@1",
        settlement_method="CASH",
        funding_schedule={"interval_hours": 8},
        margin_model_id="BYBIT-USDT-PERP",
    )
    registry = InstrumentRegistry(versions=(version,))
    return registry, f"{version.instrument_id}@{version.version}"


def bybit_activity_observation(
    raw: bytes,
    *,
    observed_at: datetime,
    query_category: str = "linear",
    account_type: str = "UNIFIED",
    extra_query: Mapping[str, str] | None = None,
    capability_instrument_version: str | None = None,
):
    capability_observed = BASE - timedelta(minutes=5)
    claims = tuple(
        CapabilityClaim(
            source=source,
            provider_id="BYBIT",
            account_id="acct-1",
            entity_id="bybit-financing-test",
            environment="PAPER",
            instrument_version=(
                capability_instrument_version
                or f"{BYBIT_XRP_INSTRUMENT_ID}@1"
            ),
            observed_at=capability_observed,
            expires_at=BASE + timedelta(hours=1),
            supported_order_types=frozenset({"MARKET"}),
            time_in_force=frozenset({"IOC"}),
            permission_scopes=frozenset({"ORDER.READ"}),
            position_mode="NET",
            native_protection=frozenset(),
            rate_limit_policy_id="bybit-financing-test",
            data_entitlements=frozenset({"ACTIVITIES"}),
            evidence_ref={
                "artifact_id": str(uuid4()),
                "sha256": "sha256:" + "a" * 64,
                "observed_at": capability_observed.isoformat().replace(
                    "+00:00", "Z"
                ),
                "source_uri": (
                    "https://bybit-exchange.github.io/docs/v5/account/"
                    "transaction-log"
                ),
            },
        )
        for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")
    )
    capability = derive_capability_snapshot(
        snapshot_id=str(uuid4()),
        claims=claims,
        observed_at=BASE,
        evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
    )
    query = prepare_authenticated_read_query(
        capability=capability,
        surface=Surface.ACTIVITIES,
        endpoint="/v5/account/transaction-log",
        query={
            "accountType": account_type,
            "category": query_category,
            **({} if extra_query is None else dict(extra_query)),
        },
        at=BASE,
        permission_scope="ORDER.READ",
    )
    return observe_authenticated_json_response(
        query_binding=query,
        http_status=200,
        response_bytes=raw,
        observed_at=observed_at,
    )


class FakeArtifactStore:
    def __init__(self):
        self.items: dict[str, bytes] = {}

    def put(
        self,
        artifact_id: str,
        *,
        revision: int,
        kind: str = "FINAL",
        amount: str = "1.20",
        available_at: datetime | None = None,
        provider_id: str = "BYBIT",
        account_id: str = "acct-1",
        environment: str = "SIMULATION",
        unit: str = "BTC",
        source_account: str = "BORROW_LIABILITY:BTC",
        charge_scope_type: str = "ACCOUNT",
        charge_scope_id: str | None = None,
    ) -> None:
        payload = {
            "schema_version": "1.0.0",
            "provider_id": provider_id,
            "account_id": account_id,
            "environment": environment,
            "charge_id": "borrow-btc-2026-09-28",
            "revision": revision,
            "kind": kind,
            "effective_at": BASE.isoformat().replace("+00:00", "Z"),
            "available_at": (available_at or BASE)
            .isoformat()
            .replace("+00:00", "Z"),
            "unit": unit,
            "amount": amount,
            "source_account": source_account,
            "charge_scope_type": charge_scope_type,
            "charge_scope_id": account_id if charge_scope_id is None else charge_scope_id,
        }
        self.items[artifact_id] = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def read_authenticated_snapshot(self, artifact_id: str):
        data = self.items[artifact_id]
        return (
            {
                "artifact_id": artifact_id,
                "sha256": "sha256:" + sha256(data).hexdigest(),
                "media_type": "application/json",
                "rights": {"storage": True, "export": False},
            },
            data,
        )


class RejectingArtifactStore:
    def read_authenticated_snapshot(self, artifact_id: str):
        raise ValueError("integrity failure")


class DurableFinancingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = JournalStore(Path(self.temp.name) / "journal.sqlite3")
        self.economic = DurableProviderEconomicBook(
            self.store,
            provider_id="BYBIT",
            account_id="acct-1",
            environment="SIMULATION",
        )
        self.financing = DurableFinancingBook(
            self.store,
            self.economic,
            provider_id="BYBIT",
            account_id="acct-1",
            environment="SIMULATION",
        )
        self.artifacts = FakeArtifactStore()

    def tearDown(self):
        self.temp.cleanup()

    def test_final_revision_and_economics_commit_atomically_and_restart(self):
        self.artifacts.put("00000000-0000-0000-0000-000000000001", revision=1)
        result = self.financing.record_authenticated_artifact(
            self.artifacts,
            artifact_id="00000000-0000-0000-0000-000000000001",
            committed_at=BASE.isoformat(),
        )
        self.assertTrue(result.inserted)
        self.assertEqual(str(result.update.economic_delta), "1.20")
        self.assertEqual(
            self.economic.balance("FINANCING_EXPENSE:BTC", "BTC"),
            result.update.economic_delta,
        )
        self.assertIsNotNone(result.economic_transaction)
        self.assertEqual(
            result.economic_transaction.economic_effective_at,
            BASE.isoformat().replace("+00:00", "Z"),
        )
        self.assertEqual(
            result.economic_transaction.observed_at,
            BASE.isoformat().replace("+00:00", "Z"),
        )
        self.assertIn(
            "provider-financing:",
            result.economic_transaction.economic_order_key,
        )

        restarted_economic = DurableProviderEconomicBook(
            self.store,
            provider_id="BYBIT",
            account_id="acct-1",
            environment="SIMULATION",
        )
        restarted = DurableFinancingBook(
            self.store,
            restarted_economic,
            provider_id="BYBIT",
            account_id="acct-1",
            environment="SIMULATION",
        )
        latest = restarted.latest("borrow-btc-2026-09-28")
        self.assertIsNotNone(latest)
        self.assertEqual(latest.revision, 1)
        self.assertEqual(str(latest.amount), "1.20")
        self.assertEqual(
            restarted_economic.balance("FINANCING_EXPENSE:BTC", "BTC"),
            result.update.economic_delta,
        )

    def test_later_final_revision_posts_only_durable_delta(self):
        first = "00000000-0000-0000-0000-000000000011"
        second = "00000000-0000-0000-0000-000000000012"
        self.artifacts.put(first, revision=1, amount="1.20")
        self.financing.record_authenticated_artifact(
            self.artifacts, artifact_id=first, committed_at=BASE.isoformat()
        )
        self.artifacts.put(
            second,
            revision=2,
            amount="1.10",
            available_at=BASE + timedelta(minutes=1),
        )
        result = self.financing.record_authenticated_artifact(
            self.artifacts,
            artifact_id=second,
            committed_at=(BASE + timedelta(minutes=1)).isoformat(),
        )
        self.assertEqual(result.update.economic_delta, Decimal("-0.10"))
        self.assertEqual(
            self.economic.balance("FINANCING_EXPENSE:BTC", "BTC"),
            result.event.amount,
        )

    def test_exact_response_loss_retry_is_idempotent(self):
        artifact = "00000000-0000-0000-0000-000000000021"
        self.artifacts.put(artifact, revision=1)
        self.assertTrue(
            self.financing.record_authenticated_artifact(
                self.artifacts, artifact_id=artifact, committed_at=BASE.isoformat()
            ).inserted
        )
        retried = self.financing.record_authenticated_artifact(
            self.artifacts, artifact_id=artifact, committed_at=BASE.isoformat()
        )
        self.assertFalse(retried.inserted)
        self.assertEqual(retried.update.economic_delta, 0)
        self.assertEqual(
            len(
                self.store.load_events(
                    "provider_financing_charge",
                    self.financing._aggregate_id("borrow-btc-2026-09-28"),
                )
            ),
            1,
        )

    def test_conflicting_same_revision_fails_closed(self):
        artifact = "00000000-0000-0000-0000-000000000031"
        self.artifacts.put(artifact, revision=1, amount="1.20")
        self.financing.record_authenticated_artifact(
            self.artifacts, artifact_id=artifact, committed_at=BASE.isoformat()
        )
        self.artifacts.put(artifact, revision=1, amount="9.00")
        with self.assertRaises(FinancingConflict):
            self.financing.record_authenticated_artifact(
                self.artifacts,
                artifact_id=artifact,
                committed_at=BASE.isoformat(),
            )
        self.assertEqual(
            str(self.economic.balance("FINANCING_EXPENSE:BTC", "BTC")),
            "1.20",
        )

    def test_indicated_revision_is_durable_but_non_economic(self):
        artifact = "00000000-0000-0000-0000-000000000041"
        self.artifacts.put(artifact, revision=1, kind="INDICATED", amount="3.25")
        result = self.financing.record_authenticated_artifact(
            self.artifacts, artifact_id=artifact, committed_at=BASE.isoformat()
        )
        self.assertTrue(result.inserted)
        self.assertIsNone(result.economic_transaction)
        self.assertEqual(result.update.economic_delta, 0)
        self.assertEqual(
            self.economic.balance("FINANCING_EXPENSE:BTC", "BTC"),
            0,
        )

    def test_real_artifact_store_authenticated_snapshot_is_accepted(self):
        artifact = "00000000-0000-0000-0000-000000000049"
        self.artifacts.put(artifact, revision=1, amount="0.75")
        real_store = ArtifactStore(Path(self.temp.name) / "artifacts")
        real_store.publish_bytes(
            artifact_id=artifact,
            data=self.artifacts.items[artifact],
            media_type="application/json",
            rights={"storage": True, "export": False},
            source_refs=["provider-fixture:bybit-financing-r1"],
            metadata={"evidence_class": "provider_financing"},
        )
        result = self.financing.record_authenticated_artifact(
            real_store,
            artifact_id=artifact,
            committed_at=BASE.isoformat(),
        )
        self.assertTrue(result.inserted)
        self.assertEqual(
            str(self.economic.balance("FINANCING_EXPENSE:BTC", "BTC")),
            "0.75",
        )

    def test_authenticated_snapshot_identity_and_media_type_are_bound(self):
        artifact = "00000000-0000-0000-0000-000000000050"
        self.artifacts.put(artifact, revision=1)
        data = self.artifacts.items[artifact]

        class WrongIdentityStore:
            def read_authenticated_snapshot(self, artifact_id: str):
                return (
                    {
                        "artifact_id": "00000000-0000-0000-0000-00000000ffff",
                        "sha256": "sha256:" + sha256(data).hexdigest(),
                        "media_type": "application/json",
                    },
                    data,
                )

        class WrongMediaStore:
            def read_authenticated_snapshot(self, artifact_id: str):
                return (
                    {
                        "artifact_id": artifact_id,
                        "sha256": "sha256:" + sha256(data).hexdigest(),
                        "media_type": "text/plain",
                    },
                    data,
                )

        with self.assertRaisesRegex(FinancingError, "identity"):
            self.financing.record_authenticated_artifact(
                WrongIdentityStore(),
                artifact_id=artifact,
                committed_at=BASE.isoformat(),
            )
        with self.assertRaisesRegex(FinancingError, "application/json"):
            self.financing.record_authenticated_artifact(
                WrongMediaStore(),
                artifact_id=artifact,
                committed_at=BASE.isoformat(),
            )
        self.assertIsNone(self.financing.latest("borrow-btc-2026-09-28"))

    def test_authenticated_manifest_digest_must_match_returned_bytes(self):
        artifact = "00000000-0000-0000-0000-000000000052"
        self.artifacts.put(artifact, revision=1)
        valid = self.artifacts.items[artifact]

        class LyingSnapshotStore:
            def read_authenticated_snapshot(self, artifact_id: str):
                return (
                    {
                        "artifact_id": artifact_id,
                        "sha256": "sha256:" + sha256(b"different-bytes").hexdigest(),
                        "media_type": "application/json",
                        "rights": {"storage": True, "export": False},
                    },
                    valid,
                )

        with self.assertRaisesRegex(FinancingError, "digest does not match returned bytes"):
            self.financing.record_authenticated_artifact(
                LyingSnapshotStore(),
                artifact_id=artifact,
                committed_at=BASE.isoformat(),
            )
        self.assertIsNone(self.financing.latest("borrow-btc-2026-09-28"))
        self.assertEqual(
            self.economic.balance("FINANCING_EXPENSE:BTC", "BTC"),
            0,
        )

    def test_forged_or_unreadable_artifact_cannot_grant_economics(self):
        with self.assertRaises(FinancingError):
            self.financing.record_authenticated_artifact(
                RejectingArtifactStore(),
                artifact_id="00000000-0000-0000-0000-000000000051",
                committed_at=BASE.isoformat(),
            )
        self.assertEqual(
            self.economic.balance("FINANCING_EXPENSE:BTC", "BTC"),
            0,
        )

    def test_exact_retry_detects_historical_partial_final_state(self):
        artifact = "00000000-0000-0000-0000-000000000059"
        self.artifacts.put(artifact, revision=1)
        (
            event,
            artifact_digest,
            charge_scope_type,
            charge_scope_id,
        ) = authenticated_financing_event(
            self.artifacts,
            artifact_id=artifact,
            provider_id="BYBIT",
            account_id="acct-1",
            environment="SIMULATION",
        )
        aggregate_id = self.financing._aggregate_id(event.charge_id)
        payload = _event_payload(
            provider_id="BYBIT",
            account_id="acct-1",
            environment="SIMULATION",
            event=event,
            artifact_id=artifact,
            artifact_digest=artifact_digest,
            charge_scope_type=charge_scope_type,
            charge_scope_id=charge_scope_id,
            previous_revision_digest=_revision_book_digest([]),
            resulting_revision_digest=_revision_book_digest([event]),
            resulting_final_charge=event.amount,
            economic_delta=event.amount,
        )
        self.store.append_event(
            {
                "event_id": "00000000-0000-0000-0000-00000000f059",
                "event_type": "ProviderFinancingRevisionAccepted",
                "aggregate_type": "provider_financing_charge",
                "aggregate_id": aggregate_id,
                "aggregate_version": "1",
                "committed_at": BASE.isoformat(),
                "payload": payload,
                "payload_hash": __import__("mvp.autotrade_mvp.persistence", fromlist=["payload_digest"]).payload_digest(payload),
            }
        )
        with self.assertRaisesRegex(FinancingConflict, "missing its economic posting"):
            self.financing.record_authenticated_artifact(
                self.artifacts,
                artifact_id=artifact,
                committed_at=BASE.isoformat(),
            )
        self.assertEqual(
            self.economic.balance("FINANCING_EXPENSE:BTC", "BTC"),
            0,
        )

    def test_indicated_retry_rejects_stray_economic_posting(self):
        artifact = "00000000-0000-0000-0000-000000000060"
        self.artifacts.put(artifact, revision=1, kind="INDICATED", amount="2.50")
        first = self.financing.record_authenticated_artifact(
            self.artifacts,
            artifact_id=artifact,
            committed_at=BASE.isoformat(),
        )
        self.assertTrue(first.inserted)
        durable_event = self.store.load_events(
            "provider_financing_charge",
            self.financing._aggregate_id("borrow-btc-2026-09-28"),
        )[0]
        from mvp.autotrade_mvp.financing import book_financing_delta
        stray = book_financing_delta(
            transaction_id="stray-financing-economic",
            cause_event_id=durable_event["event_id"],
            unit="BTC",
            source_account="BORROW_LIABILITY:BTC",
            economic_delta="1",
        )
        self.economic.append(stray)
        with self.assertRaisesRegex(FinancingConflict, "non-economic"):
            self.financing.record_authenticated_artifact(
                self.artifacts,
                artifact_id=artifact,
                committed_at=BASE.isoformat(),
            )

    def test_command_failure_exposes_neither_revision_nor_economics(self):
        artifact = "00000000-0000-0000-0000-000000000061"
        self.artifacts.put(artifact, revision=1)
        original = self.store.commit_command

        def fail_commit(**kwargs):
            raise RuntimeError("injected SQL/commit failure")

        self.store.commit_command = fail_commit
        try:
            with self.assertRaisesRegex(RuntimeError, "injected"):
                self.financing.record_authenticated_artifact(
                    self.artifacts,
                    artifact_id=artifact,
                    committed_at=BASE.isoformat(),
                )
        finally:
            self.store.commit_command = original

        self.assertIsNone(self.financing.latest("borrow-btc-2026-09-28"))
        self.assertEqual(
            self.economic.balance("FINANCING_EXPENSE:BTC", "BTC"),
            0,
        )
        reopened_store = JournalStore(Path(self.temp.name) / "journal.sqlite3")
        reopened_economic = DurableProviderEconomicBook(
            reopened_store,
            provider_id="BYBIT",
            account_id="acct-1",
            environment="SIMULATION",
        )
        reopened_financing = DurableFinancingBook(
            reopened_store,
            reopened_economic,
            provider_id="BYBIT",
            account_id="acct-1",
            environment="SIMULATION",
        )
        self.assertIsNone(reopened_financing.latest("borrow-btc-2026-09-28"))
        self.assertEqual(
            reopened_economic.balance("FINANCING_EXPENSE:BTC", "BTC"),
            0,
        )

    def test_financing_charge_scope_and_unit_are_fail_closed(self):
        wrong_scope = "00000000-0000-0000-0000-000000000069"
        self.artifacts.put(
            wrong_scope,
            revision=1,
            charge_scope_type="ACCOUNT",
            charge_scope_id="other-account",
        )
        with self.assertRaisesRegex(FinancingError, "scope"):
            self.financing.record_authenticated_artifact(
                self.artifacts,
                artifact_id=wrong_scope,
                committed_at=BASE.isoformat(),
            )

        wrong_unit = "00000000-0000-0000-0000-000000000070"
        self.artifacts.put(
            wrong_unit,
            revision=1,
            unit="USD",
            source_account="BORROW_LIABILITY:BTC",
        )
        with self.assertRaisesRegex(FinancingError, "explicitly denominated"):
            self.financing.record_authenticated_artifact(
                self.artifacts,
                artifact_id=wrong_unit,
                committed_at=BASE.isoformat(),
            )
        self.assertIsNone(self.financing.latest("borrow-btc-2026-09-28"))

    def test_provider_evidence_cannot_redirect_internal_financing_account(self):
        first = "00000000-0000-0000-0000-000000000072"
        malicious = "00000000-0000-0000-0000-000000000073"
        self.artifacts.put(first, revision=1, amount="1.20")
        self.financing.record_authenticated_artifact(
            self.artifacts,
            artifact_id=first,
            committed_at=BASE.isoformat(),
        )

        self.artifacts.put(
            malicious,
            revision=2,
            amount="1.50",
            available_at=BASE + timedelta(minutes=1),
            source_account="ARBITRARY_INTERNAL_BUCKET:BTC",
        )
        with self.assertRaisesRegex(
            FinancingError,
            "cannot select an internal ledger account",
        ):
            self.financing.record_authenticated_artifact(
                self.artifacts,
                artifact_id=malicious,
                committed_at=(BASE + timedelta(minutes=1)).isoformat(),
            )

        self.assertEqual(
            str(self.economic.balance("FINANCING_EXPENSE:BTC", "BTC")),
            "1.20",
        )
        self.assertEqual(
            str(self.economic.balance("BORROW_LIABILITY:BTC", "BTC")),
            "-1.20",
        )

        restarted_economic = DurableProviderEconomicBook(
            self.store,
            provider_id="BYBIT",
            account_id="acct-1",
            environment="SIMULATION",
        )
        restarted = DurableFinancingBook(
            self.store,
            restarted_economic,
            provider_id="BYBIT",
            account_id="acct-1",
            environment="SIMULATION",
        )
        latest = restarted.latest("borrow-btc-2026-09-28")
        self.assertIsNotNone(latest)
        self.assertEqual(latest.revision, 1)
        self.assertEqual(latest.source_account, "BORROW_LIABILITY:BTC")
        self.assertEqual(
            str(restarted_economic.balance("FINANCING_EXPENSE:BTC", "BTC")),
            "1.20",
        )
        self.assertEqual(
            restarted_economic.balance("ARBITRARY_INTERNAL_BUCKET:BTC", "BTC"),
            0,
        )

    def test_commit_cannot_predate_authenticated_evidence_availability(self):
        artifact = "00000000-0000-0000-0000-000000000070"
        available_at = BASE + timedelta(minutes=5)
        self.artifacts.put(
            artifact,
            revision=1,
            available_at=available_at,
        )

        with self.assertRaisesRegex(
            FinancingError,
            "cannot be committed before available_at",
        ):
            self.financing.record_authenticated_artifact(
                self.artifacts,
                artifact_id=artifact,
                committed_at=BASE.isoformat(),
            )

        self.assertIsNone(self.financing.latest("borrow-btc-2026-09-28"))
        self.assertEqual(
            self.economic.balance("FINANCING_EXPENSE:BTC", "BTC"),
            0,
        )

        committed = self.financing.record_authenticated_artifact(
            self.artifacts,
            artifact_id=artifact,
            committed_at=available_at.isoformat(),
        )
        self.assertTrue(committed.inserted)
        self.assertEqual(
            str(self.economic.balance("FINANCING_EXPENSE:BTC", "BTC")),
            "1.20",
        )

    def test_bybit_exact_activity_funding_can_grant_paper_economics(self):
        transaction_time = int(BASE.timestamp() * 1000)
        response = {
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "nextPageCursor": "",
                "list": [
                    {
                        "id": "funding-row-1",
                        "symbol": "XRPUSDT",
                        "category": "linear",
                        "side": "Buy",
                        "transactionTime": str(transaction_time),
                        "type": "SETTLEMENT",
                        "funding": "-0.003676",
                        "currency": "USDT",
                        "fee": "0",
                        "cashFlow": "0",
                        "change": "-0.003676",
                    }
                ],
            },
            "time": transaction_time + 1000,
        }
        raw = json.dumps(
            response,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        observed_at = BASE + timedelta(seconds=2)
        observation = bybit_activity_observation(raw, observed_at=observed_at)
        artifact = "00000000-0000-0000-0000-000000000080"
        raw_store = ArtifactStore(Path(self.temp.name) / "bybit-raw-artifacts")
        raw_store.publish_bytes(
            artifact_id=artifact,
            data=raw,
            media_type="application/json",
            rights={"storage": True, "export": False},
            source_refs=[observation.evidence_ref],
            metadata={"evidence_class": "provider_response"},
        )
        paper_economic = DurableProviderEconomicBook(
            self.store,
            provider_id="BYBIT",
            account_id="acct-1",
            environment="PAPER",
        )
        paper_financing = DurableFinancingBook(
            self.store,
            paper_economic,
            provider_id="BYBIT",
            account_id="acct-1",
            environment="PAPER",
        )
        result = paper_financing.record_bybit_funding_observation(
            observation,
            raw_store,
            artifact_id=artifact,
            row_id="funding-row-1",
            instrument_registry=bybit_instrument_registry()[0],
            instrument_versions={"XRPUSDT": f"{BYBIT_XRP_INSTRUMENT_ID}@1"},
            committed_at=observed_at.isoformat(),
        )
        self.assertTrue(result.inserted)
        self.assertEqual(result.event.amount, Decimal("0.003676"))
        self.assertEqual(result.event.unit, "USDT")
        self.assertEqual(result.event.source_account, "CASH:USDT")
        self.assertEqual(
            paper_economic.balance("FINANCING_EXPENSE:USDT", "USDT"),
            Decimal("0.003676"),
        )
        self.assertEqual(
            result.economic_transaction.economic_effective_at,
            BASE.isoformat().replace("+00:00", "Z"),
        )
        self.assertEqual(
            result.economic_transaction.observed_at,
            observed_at.isoformat().replace("+00:00", "Z"),
        )

    def test_bybit_funding_requires_canonical_instrument_version_authority(self):
        transaction_time = int(BASE.timestamp() * 1000)
        response = {
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "nextPageCursor": "",
                "list": [
                    {
                        "id": "funding-row-instrument",
                        "symbol": "XRPUSDT",
                        "category": "linear",
                        "side": "Buy",
                        "transactionTime": str(transaction_time),
                        "type": "SETTLEMENT",
                        "funding": "-0.002",
                        "currency": "USDT",
                    }
                ],
            },
            "time": transaction_time + 1000,
        }
        raw = json.dumps(response, sort_keys=True, separators=(",", ":")).encode()
        observed_at = BASE + timedelta(seconds=2)
        observation = bybit_activity_observation(raw, observed_at=observed_at)
        artifact = "00000000-0000-0000-0000-000000000083"
        raw_store = ArtifactStore(Path(self.temp.name) / "bybit-instrument-artifacts")
        raw_store.publish_bytes(
            artifact_id=artifact,
            data=raw,
            media_type="application/json",
            rights={"storage": True, "export": False},
            source_refs=[observation.evidence_ref],
            metadata={"evidence_class": "provider_response"},
        )
        paper_economic = DurableProviderEconomicBook(
            self.store,
            provider_id="BYBIT",
            account_id="acct-1",
            environment="PAPER",
        )
        paper_financing = DurableFinancingBook(
            self.store,
            paper_economic,
            provider_id="BYBIT",
            account_id="acct-1",
            environment="PAPER",
        )
        registry, exact_ref = bybit_instrument_registry()

        with self.assertRaisesRegex(FinancingError, "canonical registry authority"):
            paper_financing.record_bybit_funding_observation(
                observation,
                raw_store,
                artifact_id=artifact,
                row_id="funding-row-instrument",
                instrument_registry=registry,
                instrument_versions={
                    "XRPUSDT": "99999999-9999-4999-8999-999999999999@1"
                },
                committed_at=observed_at.isoformat(),
            )

        wrong_currency_registry, _ = bybit_instrument_registry(
            settlement_currency="USD"
        )
        with self.assertRaisesRegex(FinancingError, "settlement unit"):
            paper_financing.record_bybit_funding_observation(
                observation,
                raw_store,
                artifact_id=artifact,
                row_id="funding-row-instrument",
                instrument_registry=wrong_currency_registry,
                instrument_versions={"XRPUSDT": exact_ref},
                committed_at=observed_at.isoformat(),
            )

        future_registry, _ = bybit_instrument_registry(
            effective_from=BASE + timedelta(days=1)
        )
        with self.assertRaisesRegex(FinancingError, "event time"):
            paper_financing.record_bybit_funding_observation(
                observation,
                raw_store,
                artifact_id=artifact,
                row_id="funding-row-instrument",
                instrument_registry=future_registry,
                instrument_versions={"XRPUSDT": exact_ref},
                committed_at=observed_at.isoformat(),
            )

        self.assertEqual(
            paper_economic.balance("FINANCING_EXPENSE:USDT", "USDT"),
            0,
        )

    def test_bybit_funding_binds_row_and_query_to_same_product_scope(self):
        transaction_time = int(BASE.timestamp() * 1000)
        base_row = {
            "id": "funding-row-scope",
            "symbol": "XRPUSDT",
            "category": "linear",
            "side": "Buy",
            "transactionTime": str(transaction_time),
            "type": "SETTLEMENT",
            "funding": "-0.002",
            "currency": "USDT",
        }

        def attempt(
            *,
            row_category: str = "linear",
            query_category: str = "linear",
            capability_instrument_version: str | None = None,
            expected: str,
        ):
            response = {
                "retCode": 0,
                "retMsg": "OK",
                "result": {
                    "nextPageCursor": "",
                    "list": [{**base_row, "category": row_category}],
                },
                "time": transaction_time + 1000,
            }
            raw = json.dumps(
                response,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            observed_at = BASE + timedelta(seconds=2)
            observation = bybit_activity_observation(
                raw,
                observed_at=observed_at,
                query_category=query_category,
                capability_instrument_version=capability_instrument_version,
            )
            artifact = str(uuid4())
            raw_store = ArtifactStore(
                Path(self.temp.name) / ("scope-artifacts-" + artifact)
            )
            raw_store.publish_bytes(
                artifact_id=artifact,
                data=raw,
                media_type="application/json",
                rights={"storage": True, "export": False},
                source_refs=[observation.evidence_ref],
                metadata={"evidence_class": "provider_response"},
            )
            paper_economic = DurableProviderEconomicBook(
                self.store,
                provider_id="BYBIT",
                account_id="acct-1",
                environment="PAPER",
            )
            paper_financing = DurableFinancingBook(
                self.store,
                paper_economic,
                provider_id="BYBIT",
                account_id="acct-1",
                environment="PAPER",
            )
            with self.assertRaisesRegex(FinancingError, expected):
                paper_financing.record_bybit_funding_observation(
                    observation,
                    raw_store,
                    artifact_id=artifact,
                    row_id="funding-row-scope",
                    instrument_registry=bybit_instrument_registry()[0],
                    instrument_versions={
                        "XRPUSDT": f"{BYBIT_XRP_INSTRUMENT_ID}@1"
                    },
                    committed_at=observed_at.isoformat(),
                )
            self.assertEqual(
                paper_economic.balance("FINANCING_EXPENSE:USDT", "USDT"),
                0,
            )

        attempt(row_category="inverse", expected="row category")
        attempt(query_category="inverse", expected="row category")
        attempt(
            capability_instrument_version=(
                "99999999-9999-4999-8999-999999999999@1"
            ),
            expected="authenticated query",
        )

    def test_bybit_funding_rejects_snapshot_bytes_that_do_not_match_manifest(self):
        transaction_time = int(BASE.timestamp() * 1000)
        response = {
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "nextPageCursor": "",
                "list": [
                    {
                        "id": "funding-row-digest",
                        "symbol": "XRPUSDT",
                        "category": "linear",
                        "side": "Buy",
                        "transactionTime": str(transaction_time),
                        "type": "SETTLEMENT",
                        "funding": "-0.001",
                        "currency": "USDT",
                    }
                ],
            },
            "time": transaction_time + 1000,
        }
        raw = json.dumps(response, sort_keys=True, separators=(",", ":")).encode()
        observed_at = BASE + timedelta(seconds=2)
        observation = bybit_activity_observation(raw, observed_at=observed_at)
        artifact = "00000000-0000-0000-0000-000000000082"

        class LyingBybitSnapshotStore:
            def read_authenticated_snapshot(self, artifact_id: str):
                altered = raw.replace(b"-0.001", b"-9.999")
                return (
                    {
                        "artifact_id": artifact_id,
                        "sha256": observation.response_sha256,
                        "media_type": "application/json",
                        "rights": {"storage": True, "export": False},
                    },
                    altered,
                )

        paper_economic = DurableProviderEconomicBook(
            self.store,
            provider_id="BYBIT",
            account_id="acct-1",
            environment="PAPER",
        )
        paper_financing = DurableFinancingBook(
            self.store,
            paper_economic,
            provider_id="BYBIT",
            account_id="acct-1",
            environment="PAPER",
        )
        with self.assertRaisesRegex(FinancingError, "digest does not match returned bytes"):
            paper_financing.record_bybit_funding_observation(
                observation,
                LyingBybitSnapshotStore(),
                artifact_id=artifact,
                row_id="funding-row-digest",
                instrument_registry=bybit_instrument_registry()[0],
            instrument_versions={"XRPUSDT": f"{BYBIT_XRP_INSTRUMENT_ID}@1"},
                committed_at=observed_at.isoformat(),
            )
        self.assertEqual(
            paper_economic.balance("FINANCING_EXPENSE:USDT", "USDT"),
            0,
        )

    def test_bybit_funding_rejects_income_or_mismatched_artifact(self):
        transaction_time = int(BASE.timestamp() * 1000)
        response = {
            "retCode": 0,
            "result": {
                "nextPageCursor": "",
                "list": [
                    {
                        "id": "funding-income",
                        "symbol": "XRPUSDT",
                        "category": "linear",
                        "side": "Sell",
                        "transactionTime": str(transaction_time),
                        "type": "SETTLEMENT",
                        "funding": "0.50",
                        "currency": "USDT",
                    }
                ],
            },
        }
        raw = json.dumps(response, sort_keys=True, separators=(",", ":")).encode()
        observation = bybit_activity_observation(
            raw,
            observed_at=BASE + timedelta(seconds=1),
        )
        artifact = "00000000-0000-0000-0000-000000000081"
        raw_store = ArtifactStore(Path(self.temp.name) / "bybit-reject-artifacts")
        raw_store.publish_bytes(
            artifact_id=artifact,
            data=raw,
            media_type="application/json",
            rights={"storage": True, "export": False},
            source_refs=[observation.evidence_ref],
            metadata={"evidence_class": "provider_response"},
        )
        paper_economic = DurableProviderEconomicBook(
            self.store,
            provider_id="BYBIT",
            account_id="acct-1",
            environment="PAPER",
        )
        paper_financing = DurableFinancingBook(
            self.store,
            paper_economic,
            provider_id="BYBIT",
            account_id="acct-1",
            environment="PAPER",
        )
        with self.assertRaisesRegex(FinancingError, "paid funding"):
            paper_financing.record_bybit_funding_observation(
                observation,
                raw_store,
                artifact_id=artifact,
                row_id="funding-income",
                instrument_registry=bybit_instrument_registry()[0],
            instrument_versions={"XRPUSDT": f"{BYBIT_XRP_INSTRUMENT_ID}@1"},
                committed_at=(BASE + timedelta(seconds=1)).isoformat(),
            )
        self.assertEqual(
            paper_economic.balance("FINANCING_EXPENSE:USDT", "USDT"),
            0,
        )

    def test_generic_artifact_cannot_grant_paper_or_live_financial_authority(self):
        artifact = "00000000-0000-0000-0000-000000000070"
        self.artifacts.put(
            artifact,
            revision=1,
            environment="PAPER",
        )
        paper_economic = DurableProviderEconomicBook(
            self.store,
            provider_id="BYBIT",
            account_id="acct-1",
            environment="PAPER",
        )
        paper_financing = DurableFinancingBook(
            self.store,
            paper_economic,
            provider_id="BYBIT",
            account_id="acct-1",
            environment="PAPER",
        )
        with self.assertRaisesRegex(FinancingError, "provider-specific"):
            paper_financing.record_authenticated_artifact(
                self.artifacts,
                artifact_id=artifact,
                committed_at=BASE.isoformat(),
            )
        self.assertEqual(
            paper_economic.balance("FINANCING_EXPENSE:BTC", "BTC"),
            0,
        )

    def test_authenticated_scope_mismatch_fails_before_journal_mutation(self):
        artifact = "00000000-0000-0000-0000-000000000071"
        self.artifacts.put(artifact, revision=1, account_id="other-account")
        with self.assertRaisesRegex(FinancingError, "account"):
            self.financing.record_authenticated_artifact(
                self.artifacts,
                artifact_id=artifact,
                committed_at=BASE.isoformat(),
            )
        self.assertIsNone(self.financing.latest("borrow-btc-2026-09-28"))


if __name__ == "__main__":
    unittest.main()
