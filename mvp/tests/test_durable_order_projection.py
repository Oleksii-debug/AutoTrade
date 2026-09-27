from decimal import Decimal
from tempfile import TemporaryDirectory
from uuid import uuid4
import sqlite3
import threading
import unittest

from research.autotrade_research.artifacts.store import ArtifactStore

from mvp.autotrade_mvp.durable_order_projection import (
    DurableOrderBookProjection,
)
from mvp.autotrade_mvp.order_projection import OrderProjectionConflict
from mvp.autotrade_mvp.persistence import (
    JournalStore,
    canonical_json,
    payload_digest,
)


T0 = "2026-09-25T05:40:00Z"
T1 = "2026-09-25T05:40:01Z"
T2 = "2026-09-25T05:40:02Z"
T3 = "2026-09-25T05:40:03Z"
T4 = "2026-09-25T05:40:04Z"
T5 = "2026-09-25T05:40:05Z"


def durable(
    store,
    *,
    account_id="acct-1",
    environment="SIMULATION",
    evidence_artifact_store=None,
):
    return DurableOrderBookProjection(
        store,
        provider_id="PROVIDER-A",
        account_id=account_id,
        environment=environment,
        host_id="host-1",
        owner_epoch="1",
        evidence_artifact_store=evidence_artifact_store,
    )


def provider_evidence(
    artifact_store,
    *,
    operation,
    request,
    observed_at,
    account_id="acct-1",
    environment="PAPER",
    rights_id="provider-test-evidence",
):
    artifact_id = str(uuid4())
    source_uri = "https://provider.example.test/evidence"
    payload = canonical_json(
        {
            "operation": operation,
            "request": request,
            "observed_at": observed_at,
        }
    ).encode("utf-8")
    manifest = artifact_store.publish_bytes(
        artifact_id=artifact_id,
        data=payload,
        media_type="application/json",
        rights={"storage": True, "export": False},
        source_refs=[source_uri],
        metadata={
            "provider_id": "PROVIDER-A",
            "account_id": account_id,
            "environment": environment,
            "order_operation": operation,
            "request_hash": payload_digest(request),
            "observed_at": observed_at,
            "rights_id": rights_id,
        },
    )
    return {
        "artifact_id": artifact_id,
        "sha256": manifest["sha256"],
        "source_uri": source_uri,
        "observed_at": observed_at,
        "rights_id": rights_id,
    }


class DurableOrderProjectionTests(unittest.TestCase):
    def test_provider_execution_id_has_durable_single_order_ownership_through_bust(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            book = durable(store)
            for order_id in ("c1", "c2"):
                book.create_order(
                    event_key=f"create-{order_id}",
                    client_order_id=order_id,
                    instrument="ABC",
                    side="BUY",
                    requested_quantity="2",
                    committed_at=T0,
                )
            book.record_fill(
                event_key="fill-c1",
                client_order_id="c1",
                fill_id="f1",
                provider_execution_id="exec-owned-by-c1",
                quantity="1",
                price="100",
                provider_revision="r1",
                committed_at=T1,
            )

            def reject_cross_order_reuse(instance, event_key):
                before = len(
                    store.load_events("order_projection_book", instance.aggregate_id)
                )
                with self.assertRaisesRegex(
                    OrderProjectionConflict,
                    "already owned by another order fill",
                ):
                    instance.record_fill(
                        event_key=event_key,
                        client_order_id="c2",
                        fill_id=f"{event_key}-fill",
                        provider_execution_id="exec-owned-by-c1",
                        quantity="1",
                        price="100",
                        provider_revision="r1",
                        committed_at=T1,
                    )
                self.assertEqual(
                    len(store.load_events("order_projection_book", instance.aggregate_id)),
                    before,
                )
                self.assertEqual(instance.order("c2").filled_quantity, Decimal("0"))

            reject_cross_order_reuse(book, "duplicate-before-bust")
            book.bust_fill(
                event_key="bust-c1",
                client_order_id="c1",
                fill_id="f1",
                correction_fill_id="f1-bust",
                provider_revision="r2",
                committed_at=T2,
            )
            restarted = durable(store)
            reject_cross_order_reuse(restarted, "duplicate-after-restart-and-bust")
            self.assertEqual(restarted.order("c1").filled_quantity, Decimal("0"))

    def test_concurrent_stale_order_snapshot_is_rejected_without_poisoning_replay(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            seed = durable(store)
            seed.create_order(
                event_key="create-seed",
                client_order_id="seed",
                instrument="ABC",
                side="BUY",
                requested_quantity="2",
                committed_at=T0,
            )

            stale_writer = durable(store)
            concurrent_writer = durable(store)
            applied = threading.Event()
            resume = threading.Event()
            original_apply = stale_writer._apply

            def pause_after_stale_apply(book, operation, request):
                result = original_apply(book, operation, request)
                if (
                    operation == "CREATE"
                    and request.get("client_order_id") == "stale"
                    and threading.current_thread().name == "stale-order-writer"
                ):
                    applied.set()
                    if not resume.wait(timeout=5):
                        raise TimeoutError("concurrency regression did not resume")
                return result

            stale_writer._apply = pause_after_stale_apply
            failures = []

            def write_stale_snapshot():
                try:
                    stale_writer.create_order(
                        event_key="create-stale",
                        client_order_id="stale",
                        instrument="ABC",
                        side="BUY",
                        requested_quantity="2",
                        committed_at=T1,
                    )
                except Exception as error:  # captured for assertion in test thread
                    failures.append(error)

            thread = threading.Thread(
                target=write_stale_snapshot,
                name="stale-order-writer",
            )
            thread.start()
            self.assertTrue(applied.wait(timeout=5), "stale writer never reached its cut")
            concurrent_writer.create_order(
                event_key="create-concurrent",
                client_order_id="concurrent",
                instrument="ABC",
                side="BUY",
                requested_quantity="2",
                committed_at=T1,
            )
            resume.set()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive(), "stale writer failed to exit")
            self.assertEqual(len(failures), 1)
            self.assertRegex(str(failures[0]), "aggregate_version must be 3")

            events = store.load_events(
                "order_projection_book",
                stale_writer.aggregate_id,
            )
            self.assertEqual(len(events), 2)
            restarted = durable(store)
            self.assertEqual(
                {snapshot.client_order_id for snapshot in restarted.snapshots},
                {"seed", "concurrent"},
            )

    def test_create_ack_fill_restart_rebuilds_exact_projection(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            book = durable(store)
            created = book.create_order(
                event_key="create-1",
                client_order_id="c1",
                instrument="ABC",
                side="BUY",
                requested_quantity="2",
                committed_at=T0,
            )
            self.assertTrue(created.inserted)
            acknowledged = book.acknowledge(
                event_key="ack-1",
                client_order_id="c1",
                provider_order_id="provider-order-1",
                committed_at=T1,
            )
            self.assertEqual(acknowledged.snapshot.state, "WORKING")
            filled = book.record_fill(
                event_key="fill-1",
                client_order_id="c1",
                fill_id="f1",
                provider_execution_id="exec-1",
                quantity="2",
                price="100",
                committed_at=T2,
            )
            self.assertEqual(filled.snapshot.state, "FILLED")

            restarted = durable(store)
            self.assertEqual(restarted.snapshots, book.snapshots)
            self.assertEqual(restarted.order("c1").filled_quantity, Decimal("2"))
            self.assertEqual(len(restarted.effective_fills()), 1)

    def test_canonical_execution_fill_ingest_rebuilds_after_restart(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            book = durable(store)
            book.create_order(
                event_key="create",
                client_order_id="c1",
                instrument="instrument-v1",
                side="BUY",
                requested_quantity="2",
                quantity_unit="unit",
                origin_intent_id="intent-1",
                committed_at=T0,
            )
            result = book.ingest_execution_fill(
                event_key="canonical-fill",
                client_order_id="c1",
                committed_at=T2,
                execution_fill={
                    "fill_id": "fill-1",
                    "provider_execution_id": "exec-1",
                    "provider_revision": "rev-1",
                    "order_ref": "c1",
                    "intent_ref": "intent-1",
                    "instrument_version": "instrument-v1",
                    "side": "BUY",
                    "last_quantity": {"value": "1.25", "unit": "unit"},
                    "last_price": "101.5",
                    "trade_time": T1,
                    "receipt_time": T2,
                    "fees": [],
                    "settlement_date": "2026-09-27",
                    "evidence": [],
                },
            )
            self.assertEqual(result.snapshot.filled_quantity, Decimal("1.25"))
            restarted = durable(store)
            self.assertEqual(
                restarted.order("c1").filled_quantity,
                Decimal("1.25"),
            )
            self.assertEqual(
                restarted.order("c1").fill_history[0].provider_execution_id,
                "exec-1",
            )
            self.assertEqual(restarted.order("c1").origin_intent_id, "intent-1")

    def test_amendment_parent_order_and_originating_intent_are_distinct(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            book = durable(store)
            book.create_order(
                event_key="create-parent",
                client_order_id="order-1",
                instrument="instrument-v1",
                side="BUY",
                requested_quantity="2",
                quantity_unit="unit",
                origin_intent_id="intent-origin-1",
                committed_at=T0,
            )
            book.create_order(
                event_key="create-child",
                client_order_id="order-2",
                instrument="instrument-v1",
                side="BUY",
                requested_quantity="2",
                quantity_unit="unit",
                parent_intent_id="order-1",
                origin_intent_id="intent-origin-2",
                committed_at=T1,
            )

            child = durable(store).order("order-2")
            self.assertEqual(child.parent_intent_id, "order-1")
            self.assertEqual(child.origin_intent_id, "intent-origin-2")
            self.assertEqual(durable(store)._book.amendment_child("order-1"), "order-2")

            wrong_intent = {
                "fill_id": "fill-child",
                "provider_execution_id": "exec-child",
                "order_ref": "order-2",
                "intent_ref": "order-1",
                "instrument_version": "instrument-v1",
                "side": "BUY",
                "last_quantity": {"value": "1", "unit": "unit"},
                "last_price": "100",
                "trade_time": T1,
                "receipt_time": T2,
                "fees": [],
                "settlement_date": "2026-09-27",
                "evidence": [],
            }
            with self.assertRaisesRegex(
                OrderProjectionConflict,
                "intent_ref differs from target order",
            ):
                book.ingest_execution_fill(
                    event_key="wrong-intent-ref",
                    client_order_id="order-2",
                    committed_at=T2,
                    execution_fill=wrong_intent,
                )
            wrong_intent["intent_ref"] = "intent-origin-2"
            accepted = book.ingest_execution_fill(
                event_key="correct-intent-ref",
                client_order_id="order-2",
                committed_at=T2,
                execution_fill=wrong_intent,
            )
            self.assertEqual(accepted.snapshot.filled_quantity, Decimal("1"))
            self.assertEqual(durable(store).order("order-2").origin_intent_id, "intent-origin-2")

    def test_canonical_execution_fill_scope_mismatch_fails_before_mutation(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            book = durable(store)
            book.create_order(
                event_key="create",
                client_order_id="c1",
                instrument="instrument-v1",
                side="BUY",
                requested_quantity="2",
                quantity_unit="unit",
                committed_at=T0,
            )
            base_fill = {
                "fill_id": "fill-1",
                "provider_execution_id": "exec-1",
                "order_ref": "c1",
                "instrument_version": "instrument-v1",
                "side": "BUY",
                "last_quantity": {"value": "1", "unit": "unit"},
                    "last_price": "100",
                "trade_time": T1,
                "receipt_time": T2,
                "fees": [],
                "settlement_date": "2026-09-27",
                "evidence": [],
            }
            with self.assertRaisesRegex(
                OrderProjectionConflict,
                "instrument differs",
            ):
                book.ingest_execution_fill(
                    event_key="bad-instrument",
                    client_order_id="c1",
                    committed_at=T2,
                    execution_fill={
                        **base_fill,
                        "instrument_version": "instrument-v2",
                    },
                )
            with self.assertRaisesRegex(
                OrderProjectionConflict,
                "unsupported fields",
            ):
                book.ingest_execution_fill(
                    event_key="unknown-field",
                    client_order_id="c1",
                    committed_at=T2,
                    execution_fill={**base_fill, "raw_provider_status": "filled"},
                )
            self.assertEqual(book.order("c1").filled_quantity, Decimal("0"))
            self.assertEqual(
                len(store.load_events("order_projection_book", book.aggregate_id)),
                1,
            )

            with self.assertRaisesRegex(
                OrderProjectionConflict,
                "side must be BUY or SELL",
            ):
                book.ingest_execution_fill(
                    event_key="lowercase-side",
                    client_order_id="c1",
                    committed_at=T2,
                    execution_fill={**base_fill, "side": "buy"},
                )
            with self.assertRaisesRegex(
                OrderProjectionConflict,
                "settlement_date must be an ISO calendar date",
            ):
                book.ingest_execution_fill(
                    event_key="invalid-settlement-date",
                    client_order_id="c1",
                    committed_at=T2,
                    execution_fill={**base_fill, "settlement_date": "2026-02-30"},
                )

            before_invalid_evidence = len(
                store.load_events("order_projection_book", book.aggregate_id)
            )
            invalid_evidence = {
                "artifact_id": str(uuid4()),
                "sha256": "sha256:" + "a" * 64,
                "source_uri": "not a canonical URI",
                "observed_at": T2,
            }
            with self.assertRaisesRegex(ValueError, "source_uri must be an absolute URI"):
                book.ingest_execution_fill(
                    event_key="invalid-evidence-uri",
                    client_order_id="c1",
                    committed_at=T2,
                    execution_fill={**base_fill, "evidence": [invalid_evidence]},
                )
            self.assertEqual(
                len(store.load_events("order_projection_book", book.aggregate_id)),
                before_invalid_evidence,
            )
            restarted = durable(store)
            self.assertEqual(restarted.order("c1").filled_quantity, Decimal("0"))
            self.assertEqual(book.order("c1").filled_quantity, Decimal("0"))

    def test_canonical_execution_fill_cannot_redirect_order_or_change_quantity_unit(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            book = durable(store)
            for order_id in ("c1", "c2"):
                book.create_order(
                    event_key=f"create-{order_id}",
                    client_order_id=order_id,
                    instrument="instrument-v1",
                    side="BUY",
                    requested_quantity="2",
                    quantity_unit="unit",
                    committed_at=T0,
                )

            canonical = {
                "fill_id": "fill-identity-1",
                "provider_execution_id": "exec-identity-1",
                "order_ref": "c1",
                "instrument_version": "instrument-v1",
                "side": "BUY",
                "last_quantity": {"value": "1", "unit": "unit"},
                "last_price": "100",
                "trade_time": T1,
                "receipt_time": T2,
                "fees": [],
                "settlement_date": "2026-09-27",
                "evidence": [],
            }

            missing_ref = dict(canonical)
            missing_ref.pop("order_ref")
            with self.assertRaisesRegex(
                OrderProjectionConflict,
                "missing required fields: order_ref",
            ):
                book.ingest_execution_fill(
                    event_key="missing-order-ref",
                    client_order_id="c2",
                    committed_at=T2,
                    execution_fill=missing_ref,
                )

            with self.assertRaisesRegex(
                OrderProjectionConflict,
                "order_ref differs from target order",
            ):
                book.ingest_execution_fill(
                    event_key="redirected-order-ref",
                    client_order_id="c2",
                    committed_at=T2,
                    execution_fill=canonical,
                )

            wrong_unit = {
                **canonical,
                "last_quantity": {"value": "2", "unit": "contracts"},
            }
            with self.assertRaisesRegex(
                OrderProjectionConflict,
                "quantity unit differs from target order",
            ):
                book.ingest_execution_fill(
                    event_key="wrong-quantity-unit",
                    client_order_id="c1",
                    committed_at=T2,
                    execution_fill=wrong_unit,
                )

            self.assertEqual(book.order("c1").filled_quantity, Decimal("0"))
            self.assertEqual(book.order("c2").filled_quantity, Decimal("0"))
            self.assertEqual(
                len(store.load_events("order_projection_book", book.aggregate_id)),
                2,
            )
            restarted = durable(store)
            self.assertEqual(restarted.order("c1").filled_quantity, Decimal("0"))
            self.assertEqual(restarted.order("c2").filled_quantity, Decimal("0"))

    def test_canonical_execution_fill_event_key_binds_full_fill_content(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            book = durable(store)
            book.create_order(
                event_key="create",
                client_order_id="c1",
                instrument="instrument-v1",
                side="BUY",
                requested_quantity="2",
                quantity_unit="unit",
                committed_at=T0,
            )
            fill = {
                "fill_id": "fill-1",
                "provider_execution_id": "exec-1",
                "order_ref": "c1",
                "instrument_version": "instrument-v1",
                "side": "BUY",
                "last_quantity": {"value": "1", "unit": "unit"},
                "last_price": "100",
                "trade_time": T1,
                "receipt_time": T2,
                "fees": [],
                "settlement_date": "2026-09-27",
                "evidence": [],
            }
            first = book.ingest_execution_fill(
                event_key="canonical-fill-1",
                client_order_id="c1",
                committed_at=T2,
                execution_fill=fill,
            )
            exact_retry = book.ingest_execution_fill(
                event_key="canonical-fill-1",
                client_order_id="c1",
                committed_at=T2,
                execution_fill=dict(fill),
            )
            self.assertFalse(exact_retry.inserted)
            self.assertEqual(exact_retry.event_id, first.event_id)

            changed = dict(fill)
            changed["fees"] = [{"amount": "1", "currency": "USD"}]
            with self.assertRaisesRegex(
                OrderProjectionConflict,
                "event_key was already used",
            ):
                book.ingest_execution_fill(
                    event_key="canonical-fill-1",
                    client_order_id="c1",
                    committed_at=T2,
                    execution_fill=changed,
                )

            event = store.load_events(
                "order_projection_book",
                book.aggregate_id,
            )[-1]
            self.assertTrue(
                event["payload"]["request"]["canonical_execution_fill_hash"].startswith(
                    "sha256:"
                )
            )
            self.assertEqual(
                durable(store).order("c1").filled_quantity,
                Decimal("1"),
            )

    def test_canonical_fill_economics_are_immutable_across_event_keys_and_restart(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            book = durable(store)
            book.create_order(
                event_key="create",
                client_order_id="c1",
                instrument="instrument-v1",
                side="BUY",
                requested_quantity="2",
                quantity_unit="unit",
                origin_intent_id="intent-1",
                committed_at=T0,
            )
            fill = {
                "fill_id": "fill-1",
                "provider_execution_id": "exec-1",
                "order_ref": "c1",
                "intent_ref": "intent-1",
                "instrument_version": "instrument-v1",
                "side": "BUY",
                "last_quantity": {"value": "1", "unit": "unit"},
                "last_price": "100",
                "trade_time": T1,
                "receipt_time": T2,
                "fees": [],
                "settlement_date": "2026-09-27",
                "evidence": [],
            }
            book.ingest_execution_fill(
                event_key="canonical-fill-A",
                client_order_id="c1",
                committed_at=T2,
                execution_fill=fill,
            )
            before = len(
                store.load_events("order_projection_book", book.aggregate_id)
            )
            changed = dict(fill)
            changed["fees"] = [{"amount": "1", "currency": "USD"}]
            with self.assertRaisesRegex(
                OrderProjectionConflict,
                "different canonical economics",
            ):
                book.ingest_execution_fill(
                    event_key="canonical-fill-B",
                    client_order_id="c1",
                    committed_at=T2,
                    execution_fill=changed,
                )
            self.assertEqual(
                len(store.load_events("order_projection_book", book.aggregate_id)),
                before,
            )

            restarted = durable(store)
            with self.assertRaisesRegex(
                OrderProjectionConflict,
                "different canonical economics",
            ):
                restarted.ingest_execution_fill(
                    event_key="canonical-fill-C",
                    client_order_id="c1",
                    committed_at=T2,
                    execution_fill=changed,
                )
            self.assertEqual(
                len(store.load_events("order_projection_book", book.aggregate_id)),
                before,
            )

    def test_canonical_correction_economics_are_immutable_by_provider_revision(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            book = durable(store)
            book.create_order(
                event_key="create",
                client_order_id="c1",
                instrument="instrument-v1",
                side="BUY",
                requested_quantity="2",
                quantity_unit="unit",
                origin_intent_id="intent-1",
                committed_at=T0,
            )
            original = {
                "fill_id": "fill-1",
                "provider_execution_id": "exec-1",
                "order_ref": "c1",
                "intent_ref": "intent-1",
                "provider_revision": "r1",
                "instrument_version": "instrument-v1",
                "side": "BUY",
                "last_quantity": {"value": "1", "unit": "unit"},
                "last_price": "100",
                "trade_time": T1,
                "receipt_time": T2,
                "fees": [],
                "settlement_date": "2026-09-27",
                "evidence": [],
            }
            book.ingest_execution_fill(
                event_key="fill-r1",
                client_order_id="c1",
                committed_at=T2,
                execution_fill=original,
            )
            correction = {
                **original,
                "fill_id": "fill-r2",
                "provider_revision": "r2",
                "receipt_time": T3,
                "last_quantity": {"value": "1.5", "unit": "unit"},
                "last_price": "101",
                "correction_reference": "fill-1",
            }
            book.ingest_execution_fill(
                event_key="correction-r2-A",
                client_order_id="c1",
                committed_at=T3,
                execution_fill=correction,
            )
            before = len(
                store.load_events("order_projection_book", book.aggregate_id)
            )
            changed = {**correction, "fees": [{"amount": "1", "currency": "USD"}]}
            with self.assertRaisesRegex(
                OrderProjectionConflict,
                "different canonical economics",
            ):
                book.ingest_execution_fill(
                    event_key="correction-r2-B",
                    client_order_id="c1",
                    committed_at=T3,
                    execution_fill=changed,
                )
            restarted = durable(store)
            with self.assertRaisesRegex(
                OrderProjectionConflict,
                "different canonical economics",
            ):
                restarted.ingest_execution_fill(
                    event_key="correction-r2-C",
                    client_order_id="c1",
                    committed_at=T3,
                    execution_fill=changed,
                )

    def test_delayed_correction_cannot_roll_order_back_from_latest_predecessor(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            book = durable(store)
            book.create_order(
                event_key="create",
                client_order_id="c1",
                instrument="instrument-v1",
                side="BUY",
                requested_quantity="2",
                quantity_unit="unit",
                origin_intent_id="intent-1",
                committed_at=T0,
            )
            original = {
                "fill_id": "fill-r1",
                "provider_execution_id": "exec-1",
                "order_ref": "c1",
                "intent_ref": "intent-1",
                "provider_revision": "r1",
                "instrument_version": "instrument-v1",
                "side": "BUY",
                "last_quantity": {"value": "1", "unit": "unit"},
                "last_price": "100",
                "trade_time": T1,
                "receipt_time": T2,
                "fees": [],
                "settlement_date": "2026-09-27",
                "evidence": [],
            }
            book.ingest_execution_fill(
                event_key="fill-r1",
                client_order_id="c1",
                committed_at=T2,
                execution_fill=original,
            )
            newer = {
                **original,
                "fill_id": "fill-r3",
                "provider_revision": "r3",
                "last_quantity": {"value": "0.75", "unit": "unit"},
                "last_price": "99",
                "receipt_time": T3,
                "correction_reference": "fill-r1",
            }
            book.ingest_execution_fill(
                event_key="fill-r3",
                client_order_id="c1",
                committed_at=T3,
                execution_fill=newer,
            )
            count = len(
                store.load_events("order_projection_book", book.aggregate_id)
            )

            restarted = durable(store)
            delayed = {
                **original,
                "fill_id": "fill-r2",
                "provider_revision": "r2",
                "last_quantity": {"value": "1.5", "unit": "unit"},
                "last_price": "101",
                "receipt_time": T4,
                "correction_reference": "fill-r1",
            }
            with self.assertRaisesRegex(
                OrderProjectionConflict,
                "correction_reference must extend the latest authoritative provider revision",
            ):
                restarted.ingest_execution_fill(
                    event_key="delayed-fill-r2",
                    client_order_id="c1",
                    committed_at=T4,
                    execution_fill=delayed,
                )

            self.assertEqual(restarted.order("c1").filled_quantity, Decimal("0.75"))
            self.assertEqual(
                len(store.load_events("order_projection_book", book.aggregate_id)),
                count,
            )

    def test_canonical_execution_fill_correction_uses_existing_fill_lineage(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            book = durable(store)
            book.create_order(
                event_key="create",
                client_order_id="c1",
                instrument="instrument-v1",
                side="BUY",
                requested_quantity="2",
                quantity_unit="unit",
                committed_at=T0,
            )
            book.ingest_execution_fill(
                event_key="fill-r1",
                client_order_id="c1",
                committed_at=T2,
                execution_fill={
                    "fill_id": "fill-1",
                    "provider_execution_id": "exec-1",
                    "order_ref": "c1",
                    "provider_revision": "r1",
                    "instrument_version": "instrument-v1",
                    "side": "BUY",
                    "last_quantity": {"value": "1", "unit": "unit"},
                    "last_price": "100",
                    "trade_time": T1,
                    "receipt_time": T2,
                    "fees": [],
                    "settlement_date": "2026-09-27",
                    "evidence": [],
                },
            )
            corrected = book.ingest_execution_fill(
                event_key="fill-r2",
                client_order_id="c1",
                committed_at=T4,
                execution_fill={
                    "fill_id": "fill-1-r2",
                    "provider_execution_id": "exec-1",
                    "order_ref": "c1",
                    "provider_revision": "r2",
                    "instrument_version": "instrument-v1",
                    "side": "BUY",
                    "last_quantity": {"value": "1.5", "unit": "unit"},
                    "last_price": "101",
                    "trade_time": T1,
                    "receipt_time": T3,
                    "fees": [],
                    "settlement_date": "2026-09-27",
                    "correction_reference": "fill-1",
                    "evidence": [],
                },
            )
            self.assertEqual(corrected.snapshot.filled_quantity, Decimal("1.5"))
            restarted = durable(store)
            self.assertEqual(
                [item.fill_id for item in restarted.order("c1").fill_history],
                ["fill-1", "fill-1-r2"],
            )

    def test_canonical_execution_fill_supports_chained_correction_reference(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            book = durable(store)
            book.create_order(
                event_key="create",
                client_order_id="c1",
                instrument="instrument-v1",
                side="BUY",
                requested_quantity="2",
                quantity_unit="unit",
                committed_at=T0,
            )
            book.ingest_execution_fill(
                event_key="fill-r1",
                client_order_id="c1",
                committed_at=T2,
                execution_fill={
                    "fill_id": "fill-1",
                    "provider_execution_id": "exec-1",
                    "order_ref": "c1",
                    "provider_revision": "r1",
                    "instrument_version": "instrument-v1",
                    "side": "BUY",
                    "last_quantity": {"value": "1", "unit": "unit"},
                    "last_price": "100",
                    "trade_time": T1,
                    "receipt_time": T2,
                    "fees": [],
                    "settlement_date": "2026-09-27",
                    "evidence": [],
                },
            )
            book.ingest_execution_fill(
                event_key="fill-r2",
                client_order_id="c1",
                committed_at=T4,
                execution_fill={
                    "fill_id": "fill-1-r2",
                    "provider_execution_id": "exec-1",
                    "order_ref": "c1",
                    "provider_revision": "r2",
                    "instrument_version": "instrument-v1",
                    "side": "BUY",
                    "last_quantity": {"value": "1.5", "unit": "unit"},
                    "last_price": "101",
                    "trade_time": T1,
                    "receipt_time": T3,
                    "fees": [],
                    "settlement_date": "2026-09-27",
                    "correction_reference": "fill-1",
                    "evidence": [],
                },
            )
            corrected = book.ingest_execution_fill(
                event_key="fill-r3",
                client_order_id="c1",
                committed_at=T5,
                execution_fill={
                    "fill_id": "fill-1-r3",
                    "provider_execution_id": "exec-1",
                    "order_ref": "c1",
                    "provider_revision": "r3",
                    "instrument_version": "instrument-v1",
                    "side": "BUY",
                    "last_quantity": {"value": "0.75", "unit": "unit"},
                    "last_price": "99",
                    "trade_time": T1,
                    "receipt_time": T4,
                    "fees": [],
                    "settlement_date": "2026-09-27",
                    "correction_reference": "fill-1-r2",
                    "evidence": [],
                },
            )

            self.assertEqual(corrected.snapshot.filled_quantity, Decimal("0.75"))
            restarted = durable(store)
            self.assertEqual(restarted.order("c1").filled_quantity, Decimal("0.75"))
            self.assertEqual(
                [item.fill_id for item in restarted.order("c1").fill_history],
                ["fill-1", "fill-1-r2", "fill-1-r3"],
            )

    def test_canonical_execution_fill_correction_rejects_cross_execution_identity(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            book = durable(store)
            book.create_order(
                event_key="create",
                client_order_id="c1",
                instrument="instrument-v1",
                side="BUY",
                requested_quantity="2",
                quantity_unit="unit",
                committed_at=T0,
            )
            book.ingest_execution_fill(
                event_key="fill-r1",
                client_order_id="c1",
                committed_at=T2,
                execution_fill={
                    "fill_id": "fill-1",
                    "provider_execution_id": "exec-1",
                    "order_ref": "c1",
                    "provider_revision": "r1",
                    "instrument_version": "instrument-v1",
                    "side": "BUY",
                    "last_quantity": {"value": "1", "unit": "unit"},
                    "last_price": "100",
                    "trade_time": T1,
                    "receipt_time": T2,
                    "fees": [],
                    "settlement_date": "2026-09-27",
                    "evidence": [],
                },
            )
            before_events = len(
                store.load_events("order_projection_book", book.aggregate_id)
            )
            with self.assertRaisesRegex(
                OrderProjectionConflict,
                "provider_execution_id differs",
            ):
                book.ingest_execution_fill(
                    event_key="fill-cross-exec",
                    client_order_id="c1",
                    committed_at=T4,
                    execution_fill={
                        "fill_id": "fill-1-r2",
                        "provider_execution_id": "exec-other",
                        "order_ref": "c1",
                        "provider_revision": "r2",
                        "instrument_version": "instrument-v1",
                        "side": "BUY",
                        "last_quantity": {"value": "1.5", "unit": "unit"},
                    "last_price": "101",
                        "trade_time": T1,
                        "receipt_time": T3,
                        "fees": [],
                        "settlement_date": "2026-09-27",
                        "correction_reference": "fill-1",
                        "evidence": [],
                    },
                )
            self.assertEqual(book.order("c1").filled_quantity, Decimal("1"))
            self.assertEqual(
                len(store.load_events("order_projection_book", book.aggregate_id)),
                before_events,
            )
            self.assertEqual(
                durable(store).order("c1").fill_history[0].provider_execution_id,
                "exec-1",
            )

    def test_exact_event_retry_is_idempotent_and_conflict_fails_closed(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            book = durable(store)
            args = dict(
                event_key="create-stable",
                client_order_id="c1",
                instrument="ABC",
                side="BUY",
                requested_quantity="1.00",
                committed_at=T0,
            )
            first = book.create_order(**args)
            retry = book.create_order(**{**args, "requested_quantity": "1"})
            self.assertTrue(first.inserted)
            self.assertFalse(retry.inserted)
            self.assertEqual(first.event_id, retry.event_id)
            self.assertEqual(
                len(store.load_events("order_projection_book", book.aggregate_id)),
                1,
            )

            with self.assertRaisesRegex(
                OrderProjectionConflict,
                "different order request",
            ):
                book.create_order(
                    **{**args, "instrument": "XYZ"}
                )
            self.assertEqual(book.order("c1").instrument, "ABC")

    def test_rejected_cancel_resolution_survives_restart_and_retry(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            book = durable(store)
            book.create_order(
                event_key="create-cancel-reject",
                client_order_id="c-reject",
                instrument="ABC",
                side="BUY",
                requested_quantity="2",
                committed_at=T0,
            )
            book.request_cancel(
                event_key="request-cancel-reject",
                client_order_id="c-reject",
                command_id="cancel-command-1",
                committed_at=T1,
            )
            filled = book.record_fill(
                event_key="fill-cancel-reject",
                client_order_id="c-reject",
                fill_id="fill-before-reject",
                provider_execution_id="exec-before-reject",
                quantity="1",
                price="100",
                committed_at=T2,
            )
            self.assertEqual(
                filled.snapshot.state,
                "PARTIALLY_FILLED_CANCEL_REQUESTED",
            )
            rejected = book.reject_cancel(
                event_key="reject-cancel",
                client_order_id="c-reject",
                command_id="cancel-command-1",
                reason_code="TOO_LATE_TO_CANCEL",
                committed_at=T3,
            )
            self.assertTrue(rejected.inserted)
            self.assertEqual(rejected.snapshot.state, "PARTIALLY_FILLED")
            self.assertFalse(rejected.snapshot.cancel_requested)
            self.assertIsNone(rejected.snapshot.cancel_command_id)

            retry = book.reject_cancel(
                event_key="reject-cancel",
                client_order_id="c-reject",
                command_id="cancel-command-1",
                reason_code="TOO_LATE_TO_CANCEL",
                committed_at=T3,
            )
            self.assertFalse(retry.inserted)
            self.assertEqual(retry.event_id, rejected.event_id)

            restarted = durable(store)
            snapshot = restarted.order("c-reject").snapshot()
            self.assertEqual(snapshot.state, "PARTIALLY_FILLED")
            self.assertEqual(snapshot.filled_quantity, Decimal("1"))
            self.assertFalse(snapshot.cancel_requested)
            self.assertIsNone(snapshot.cancel_command_id)

            second = restarted.request_cancel(
                event_key="request-cancel-second",
                client_order_id="c-reject",
                command_id="cancel-command-2",
                committed_at=T4,
            )
            self.assertEqual(
                second.snapshot.state,
                "PARTIALLY_FILLED_CANCEL_REQUESTED",
            )
            self.assertEqual(
                second.snapshot.cancel_command_id,
                "cancel-command-2",
            )

    def test_rejected_cancel_event_key_conflict_preserves_pending_action(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            book = durable(store)
            book.create_order(
                event_key="create",
                client_order_id="c1",
                instrument="ABC",
                side="BUY",
                requested_quantity="2",
                committed_at=T0,
            )
            book.request_cancel(
                event_key="request-cancel",
                client_order_id="c1",
                command_id="cancel-command",
                committed_at=T1,
            )
            book.reject_cancel(
                event_key="reject-cancel",
                client_order_id="c1",
                command_id="cancel-command",
                reason_code="NOT_FOUND",
                committed_at=T2,
            )
            with self.assertRaisesRegex(
                OrderProjectionConflict,
                "different order request",
            ):
                book.reject_cancel(
                    event_key="reject-cancel",
                    client_order_id="c1",
                    command_id="cancel-command",
                    reason_code="DIFFERENT_REASON",
                    committed_at=T2,
                )
            self.assertFalse(book.order("c1").cancel_requested)

    def test_unknown_submission_resolution_survives_restart(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            book = durable(store)
            book.create_order(
                event_key="create",
                client_order_id="c1",
                instrument="ABC",
                side="BUY",
                requested_quantity="1",
                committed_at=T0,
            )
            unknown = book.acknowledge(
                event_key="ack-unknown",
                client_order_id="c1",
                provider_order_id="p1",
                status="UNKNOWN",
                committed_at=T1,
            )
            self.assertEqual(unknown.snapshot.state, "UNKNOWN")
            resolved = book.acknowledge(
                event_key="ack-resolved",
                client_order_id="c1",
                provider_order_id="p1",
                status="ACCEPTED",
                committed_at=T2,
            )
            self.assertEqual(resolved.snapshot.state, "WORKING")
            self.assertEqual(durable(store).order("c1").state, "WORKING")

    def test_pending_cancel_and_late_fill_restart_without_erasing_economics(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            book = durable(store)
            book.create_order(
                event_key="create",
                client_order_id="c1",
                instrument="ABC",
                side="BUY",
                requested_quantity="2",
                committed_at=T0,
            )
            book.request_cancel(
                event_key="cancel-request",
                client_order_id="c1",
                command_id="cancel-command-1",
                committed_at=T1,
            )
            late = book.record_fill(
                event_key="late-fill",
                client_order_id="c1",
                fill_id="f-late",
                provider_execution_id="exec-late",
                quantity="1",
                price="101",
                committed_at=T2,
            )
            self.assertEqual(
                late.snapshot.state,
                "PARTIALLY_FILLED_CANCEL_REQUESTED",
            )
            restarted = durable(store)
            self.assertEqual(restarted.order("c1").filled_quantity, Decimal("1"))
            self.assertTrue(restarted.order("c1").cancel_requested)

    def test_correction_and_bust_history_replays_exactly(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            book = durable(store)
            book.create_order(
                event_key="create",
                client_order_id="c1",
                instrument="ABC",
                side="BUY",
                requested_quantity="2",
                committed_at=T0,
            )
            book.record_fill(
                event_key="fill",
                client_order_id="c1",
                fill_id="f1",
                provider_execution_id="exec-1",
                quantity="1",
                price="100",
                provider_revision="r1",
                committed_at=T1,
            )
            book.correct_fill(
                event_key="correct",
                client_order_id="c1",
                fill_id="f1",
                correction_fill_id="f1-r2",
                quantity="1.5",
                price="101",
                provider_revision="r2",
                committed_at=T2,
            )
            book.bust_fill(
                event_key="bust",
                client_order_id="c1",
                fill_id="f1",
                correction_fill_id="f1-r3-bust",
                provider_revision="r3",
                committed_at=T3,
            )
            restarted = durable(store)
            order = restarted.order("c1")
            self.assertEqual(order.filled_quantity, Decimal("0"))
            self.assertEqual(order.open_quantity, Decimal("2"))
            self.assertEqual(
                [item.fill_id for item in order.fill_history],
                ["f1", "f1-r2", "f1-r3-bust"],
            )

    def test_historical_oco_breach_survives_bust_and_restart(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            book = durable(store)
            for order_id in ("take", "stop"):
                book.create_order(
                    event_key=f"create-{order_id}",
                    client_order_id=order_id,
                    instrument="ABC",
                    side="SELL",
                    requested_quantity="1",
                    oco_group_id="g1",
                    committed_at=T0,
                )
                book.record_fill(
                    event_key=f"fill-{order_id}",
                    client_order_id=order_id,
                    fill_id=f"fill-{order_id}",
                    provider_execution_id=f"exec-{order_id}",
                    quantity="1",
                    price="100",
                    committed_at=T1,
                )
            book.bust_fill(
                event_key="bust-stop",
                client_order_id="stop",
                fill_id="fill-stop",
                provider_revision="bust-1",
                committed_at=T2,
            )
            restarted = durable(store)
            self.assertEqual(
                restarted.oco_breaches()["g1"],
                ("stop", "take"),
            )
            self.assertEqual(restarted.active_oco_breaches(), {})
            snapshots = {
                item.client_order_id: item
                for item in restarted.snapshots
            }
            self.assertTrue(snapshots["take"].oco_violation)
            self.assertTrue(snapshots["stop"].oco_violation)

    def test_same_client_id_is_isolated_by_account_scope(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            first = durable(store, account_id="acct-1")
            second = durable(store, account_id="acct-2")
            for book in (first, second):
                book.create_order(
                    event_key="create",
                    client_order_id="same-client",
                    instrument="ABC",
                    side="BUY",
                    requested_quantity="1",
                    committed_at=T0,
                )
            self.assertNotEqual(first.aggregate_id, second.aggregate_id)
            self.assertEqual(len(first.snapshots), 1)
            self.assertEqual(len(second.snapshots), 1)

    def test_replace_and_expiry_are_durable_pending_terminal_facts(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            book = durable(store)
            book.create_order(
                event_key="create",
                client_order_id="replace",
                instrument="ABC",
                side="BUY",
                requested_quantity="2",
                committed_at=T0,
            )
            pending = book.request_replace(
                event_key="replace-request",
                client_order_id="replace",
                command_id="replace-command-1",
                committed_at=T1,
            )
            self.assertEqual(pending.snapshot.state, "REPLACE_REQUESTED")

            expiring = durable(store, account_id="acct-2")
            expiring.create_order(
                event_key="create-expiring",
                client_order_id="expire",
                instrument="ABC",
                side="BUY",
                requested_quantity="2",
                committed_at=T0,
            )
            expired = expiring.confirm_expired(
                event_key="expire-confirmed",
                client_order_id="expire",
                committed_at=T1,
            )
            self.assertEqual(expired.snapshot.state, "EXPIRED")
            self.assertTrue(durable(store, account_id="acct-2").order("expire").expired)


    def test_unknown_without_provider_order_id_restarts_and_resolves(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            book = durable(store)
            book.create_order(
                event_key="create-no-provider-id",
                client_order_id="c-no-provider-id",
                instrument="ABC",
                side="BUY",
                requested_quantity="1",
                committed_at=T0,
            )
            unknown = book.acknowledge(
                event_key="unknown-no-provider-id",
                client_order_id="c-no-provider-id",
                status="UNKNOWN",
                committed_at=T1,
            )
            self.assertEqual(unknown.snapshot.state, "UNKNOWN")
            self.assertIsNone(unknown.snapshot.provider_order_id)

            restarted = durable(store)
            accepted = restarted.acknowledge(
                event_key="resolve-provider-id",
                client_order_id="c-no-provider-id",
                provider_order_id="p-resolved",
                status="ACCEPTED",
                committed_at=T2,
            )
            self.assertEqual(accepted.snapshot.state, "WORKING")
            self.assertEqual(accepted.snapshot.provider_order_id, "p-resolved")


    def test_stale_process_reloads_durable_cut_before_next_mutation(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            first = durable(store)
            stale = durable(store)

            first.create_order(
                event_key="writer-one",
                client_order_id="one",
                instrument="ABC",
                side="BUY",
                requested_quantity="1",
                committed_at=T0,
            )
            second = stale.create_order(
                event_key="writer-two",
                client_order_id="two",
                instrument="ABC",
                side="BUY",
                requested_quantity="1",
                committed_at=T1,
            )
            self.assertTrue(second.inserted)
            restarted = durable(store)
            self.assertEqual(
                tuple(item.client_order_id for item in restarted.snapshots),
                ("one", "two"),
            )
            events = store.load_events(
                "order_projection_book",
                restarted.aggregate_id,
            )
            self.assertEqual(
                [item["aggregate_version"] for item in events],
                [1, 2],
            )

    def test_restart_fails_closed_on_tampered_journal_payload(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            book = durable(store)
            event = book.create_order(
                event_key="create-before-tamper",
                client_order_id="tamper",
                instrument="ABC",
                side="BUY",
                requested_quantity="1",
                committed_at=T0,
            )
            connection = sqlite3.connect(store.path)
            try:
                connection.execute(
                    "UPDATE events SET payload_json = ? WHERE event_id = ?",
                    ('{"tampered":true}', event.event_id),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(
                ValueError,
                "payload hash does not match",
            ):
                durable(store)

    def test_replay_rejects_unknown_but_well_hashed_order_operation(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            book = durable(store)
            book.create_order(
                event_key="create-known",
                client_order_id="known",
                instrument="ABC",
                side="BUY",
                requested_quantity="1",
                committed_at=T0,
            )
            request = {"client_order_id": "known"}
            payload = {
                "schema_version": "1.0.0",
                "scope": {
                    "provider_id": "PROVIDER-A",
                    "account_id": "acct-1",
                    "environment": "SIMULATION",
                },
                "event_key": "unsupported-op",
                "operation": "DELETE_ALL_ECONOMIC_TRUTH",
                "request": request,
                "request_hash": payload_digest(request),
                "snapshot": {},
            }
            envelope = {
                "event_id": "unsupported-order-operation-event",
                "event_type": "OrderProjectionMutationCommitted",
                "schema_version": "1.0.0",
                "aggregate_type": "order_projection_book",
                "aggregate_id": book.aggregate_id,
                "aggregate_version": "2",
                "host_id": "host-1",
                "owner_epoch": "1",
                "environment": "SIMULATION",
                "occurred_at": T1,
                "observed_at": T1,
                "committed_at": T1,
                "correlation_id": "unsupported-order-operation-event",
                "causation_id": None,
                "payload": payload,
                "payload_hash": payload_digest(payload),
                "evidence_refs": [],
            }
            store.append_event(envelope)

            with self.assertRaisesRegex(
                OrderProjectionConflict,
                "unsupported durable order operation",
            ):
                durable(store)


    def test_action_command_identity_survives_restart(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            book = durable(store)
            book.create_order(
                event_key="create-action-lineage",
                client_order_id="action-lineage",
                instrument="ABC",
                side="BUY",
                requested_quantity="2",
                committed_at=T0,
            )
            result = book.request_cancel(
                event_key="request-cancel-lineage",
                client_order_id="action-lineage",
                command_id="cancel-command-durable",
                committed_at=T1,
            )
            self.assertEqual(
                result.snapshot.cancel_command_id,
                "cancel-command-durable",
            )
            restarted = durable(store)
            self.assertEqual(
                restarted.order("action-lineage").snapshot().cancel_command_id,
                "cancel-command-durable",
            )


    def test_paper_cancel_rejection_requires_scoped_immutable_evidence(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            artifacts = ArtifactStore(f"{directory}/artifacts")
            book = durable(
                store,
                environment="PAPER",
                evidence_artifact_store=artifacts,
            )
            book.create_order(
                event_key="create-paper-reject",
                client_order_id="paper-reject",
                instrument="ABC",
                side="BUY",
                requested_quantity="1",
                committed_at=T0,
            )
            book.request_cancel(
                event_key="request-paper-reject",
                client_order_id="paper-reject",
                command_id="cancel-paper-1",
                committed_at=T1,
            )
            request = {
                "client_order_id": "paper-reject",
                "command_id": "cancel-paper-1",
                "reason_code": "ALREADY_FILLED",
            }
            with self.assertRaisesRegex(
                OrderProjectionConflict,
                "requires immutable evidence",
            ):
                book.reject_cancel(
                    event_key="reject-without-evidence",
                    client_order_id="paper-reject",
                    command_id="cancel-paper-1",
                    reason_code="ALREADY_FILLED",
                    committed_at=T2,
                )

            ref = provider_evidence(
                artifacts,
                operation="REJECT_CANCEL",
                request=request,
                observed_at=T2,
            )
            rejected = book.reject_cancel(
                event_key="reject-with-evidence",
                client_order_id="paper-reject",
                command_id="cancel-paper-1",
                reason_code="ALREADY_FILLED",
                committed_at=T2,
                evidence_refs=[ref],
            )
            self.assertFalse(rejected.snapshot.cancel_requested)
            self.assertFalse(rejected.snapshot.cancel_confirmed)
            self.assertEqual(rejected.snapshot.state, "PENDING")

            restarted = durable(
                store,
                environment="PAPER",
                evidence_artifact_store=artifacts,
            )
            self.assertFalse(
                restarted.order("paper-reject").snapshot().cancel_requested
            )

    def test_paper_provider_fact_requires_immutable_evidence(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            book = durable(store, environment="PAPER")
            book.create_order(
                event_key="create-paper",
                client_order_id="paper-1",
                instrument="ABC",
                side="BUY",
                requested_quantity="1",
                committed_at=T0,
            )
            with self.assertRaisesRegex(
                OrderProjectionConflict,
                "requires immutable evidence",
            ):
                book.acknowledge(
                    event_key="ack-without-evidence",
                    client_order_id="paper-1",
                    provider_order_id="provider-1",
                    committed_at=T1,
                )

    def test_paper_provider_evidence_must_resolve_in_artifact_store(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            artifacts = ArtifactStore(f"{directory}/artifacts")
            book = durable(
                store,
                environment="PAPER",
                evidence_artifact_store=artifacts,
            )
            book.create_order(
                event_key="create-paper",
                client_order_id="paper-1",
                instrument="ABC",
                side="BUY",
                requested_quantity="1",
                committed_at=T0,
            )
            forged = {
                "artifact_id": str(uuid4()),
                "sha256": "sha256:" + ("0" * 64),
                "source_uri": "https://provider.example.test/evidence",
                "observed_at": T1,
                "rights_id": "provider-test-evidence",
            }
            with self.assertRaisesRegex(
                OrderProjectionConflict,
                "not resolvable and intact",
            ):
                book.acknowledge(
                    event_key="ack-forged",
                    client_order_id="paper-1",
                    provider_order_id="provider-1",
                    committed_at=T1,
                    evidence_refs=[forged],
                )

    def test_provider_evidence_scope_and_request_are_bound(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            artifacts = ArtifactStore(f"{directory}/artifacts")
            book = durable(
                store,
                environment="PAPER",
                evidence_artifact_store=artifacts,
            )
            book.create_order(
                event_key="create-paper",
                client_order_id="paper-1",
                instrument="ABC",
                side="BUY",
                requested_quantity="1",
                committed_at=T0,
            )
            request = {
                "client_order_id": "paper-1",
                "provider_order_id": "provider-1",
                "status": "ACCEPTED",
                "attempt_id": None,
            }
            wrong_scope = provider_evidence(
                artifacts,
                operation="ACKNOWLEDGE",
                request=request,
                observed_at=T1,
                account_id="acct-other",
            )
            with self.assertRaisesRegex(
                OrderProjectionConflict,
                "metadata mismatch: account_id",
            ):
                book.acknowledge(
                    event_key="ack-wrong-scope",
                    client_order_id="paper-1",
                    provider_order_id="provider-1",
                    committed_at=T1,
                    evidence_refs=[wrong_scope],
                )

    def test_verified_ack_and_fill_evidence_survive_restart(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            artifacts = ArtifactStore(f"{directory}/artifacts")
            book = durable(
                store,
                environment="PAPER",
                evidence_artifact_store=artifacts,
            )
            book.create_order(
                event_key="create-paper",
                client_order_id="paper-1",
                instrument="ABC",
                side="BUY",
                requested_quantity="2",
                committed_at=T0,
            )
            ack_request = {
                "client_order_id": "paper-1",
                "provider_order_id": "provider-1",
                "status": "ACCEPTED",
                "attempt_id": None,
            }
            ack_ref = provider_evidence(
                artifacts,
                operation="ACKNOWLEDGE",
                request=ack_request,
                observed_at=T1,
            )
            ack = book.acknowledge(
                event_key="ack-evidenced",
                client_order_id="paper-1",
                provider_order_id="provider-1",
                committed_at=T1,
                evidence_refs=[ack_ref],
            )
            self.assertEqual(ack.snapshot.state, "WORKING")

            fill_request = {
                "client_order_id": "paper-1",
                "fill_id": "fill-1",
                "provider_execution_id": "execution-1",
                "quantity": "2",
                "price": "100",
                "provider_revision": None,
            }
            fill_ref = provider_evidence(
                artifacts,
                operation="RECORD_FILL",
                request=fill_request,
                observed_at=T2,
            )
            fill = book.record_fill(
                event_key="fill-evidenced",
                client_order_id="paper-1",
                fill_id="fill-1",
                provider_execution_id="execution-1",
                quantity="2",
                price="100",
                committed_at=T2,
                evidence_refs=[fill_ref],
            )
            self.assertEqual(fill.snapshot.state, "FILLED")

            events = store.load_events(
                "order_projection_book",
                book.aggregate_id,
            )
            self.assertEqual(events[1]["evidence_refs"], [ack_ref])
            self.assertEqual(events[2]["evidence_refs"], [fill_ref])

            restarted = durable(
                store,
                environment="PAPER",
                evidence_artifact_store=artifacts,
            )
            self.assertEqual(restarted.order("paper-1").state, "FILLED")
            self.assertEqual(
                restarted.order("paper-1").filled_quantity,
                Decimal("2"),
            )

    def test_evidence_identity_participates_in_idempotency(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            artifacts = ArtifactStore(f"{directory}/artifacts")
            book = durable(
                store,
                environment="PAPER",
                evidence_artifact_store=artifacts,
            )
            book.create_order(
                event_key="create-paper",
                client_order_id="paper-1",
                instrument="ABC",
                side="BUY",
                requested_quantity="1",
                committed_at=T0,
            )
            request = {
                "client_order_id": "paper-1",
                "provider_order_id": "provider-1",
                "status": "ACCEPTED",
                "attempt_id": None,
            }
            first_ref = provider_evidence(
                artifacts,
                operation="ACKNOWLEDGE",
                request=request,
                observed_at=T1,
            )
            first = book.acknowledge(
                event_key="ack-stable",
                client_order_id="paper-1",
                provider_order_id="provider-1",
                committed_at=T1,
                evidence_refs=[first_ref],
            )
            retry = book.acknowledge(
                event_key="ack-stable",
                client_order_id="paper-1",
                provider_order_id="provider-1",
                committed_at=T1,
                evidence_refs=[first_ref],
            )
            self.assertTrue(first.inserted)
            self.assertFalse(retry.inserted)

            second_ref = provider_evidence(
                artifacts,
                operation="ACKNOWLEDGE",
                request=request,
                observed_at=T1,
            )
            with self.assertRaisesRegex(
                OrderProjectionConflict,
                "different order request",
            ):
                book.acknowledge(
                    event_key="ack-stable",
                    client_order_id="paper-1",
                    provider_order_id="provider-1",
                    committed_at=T1,
                    evidence_refs=[second_ref],
                )

    def test_unknown_submission_remains_evidence_free_and_restart_safe(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            book = durable(store, environment="PAPER")
            book.create_order(
                event_key="create-unknown",
                client_order_id="paper-unknown",
                instrument="ABC",
                side="BUY",
                requested_quantity="1",
                committed_at=T0,
            )
            unknown = book.acknowledge(
                event_key="unknown",
                client_order_id="paper-unknown",
                status="UNKNOWN",
                committed_at=T1,
            )
            self.assertEqual(unknown.snapshot.state, "UNKNOWN")
            restarted = durable(store, environment="PAPER")
            self.assertEqual(restarted.order("paper-unknown").state, "UNKNOWN")

    def test_local_command_cannot_claim_provider_evidence(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            artifacts = ArtifactStore(f"{directory}/artifacts")
            book = durable(
                store,
                environment="PAPER",
                evidence_artifact_store=artifacts,
            )
            book.create_order(
                event_key="create-paper",
                client_order_id="paper-1",
                instrument="ABC",
                side="BUY",
                requested_quantity="1",
                committed_at=T0,
            )
            request = {"client_order_id": "paper-1"}
            ref = provider_evidence(
                artifacts,
                operation="CONFIRM_CANCEL",
                request=request,
                observed_at=T1,
            )
            with self.assertRaisesRegex(
                OrderProjectionConflict,
                "requires immutable evidence",
            ):
                book.confirm_cancel(
                    event_key="cancel-without-evidence",
                    client_order_id="paper-1",
                    committed_at=T1,
                )
            confirmed = book.confirm_cancel(
                event_key="cancel-with-evidence",
                client_order_id="paper-1",
                committed_at=T1,
                evidence_refs=[ref],
            )
            self.assertTrue(confirmed.snapshot.cancel_confirmed)


if __name__ == "__main__":
    unittest.main()
