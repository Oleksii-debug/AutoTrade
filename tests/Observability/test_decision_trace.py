import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
import unittest
from unittest.mock import patch

import mvp.autotrade_mvp.decision_trace as decision_trace_module
from autotrade_runtime.artifacts.durable_publish import (
    DurablePublishLockError,
    durable_path_lock,
    validate_publication_destination,
)
from mvp.autotrade_mvp.decision_trace import BoundedMetricBacklog, DecisionTraceStore


def evidence_trace(trace_id: str = "decision-1") -> dict:
    return {
        "trace_id": trace_id,
        "input_hash": "b" * 64,
        "strategy_version": "baseline-v2",
        "decision": "NO_TRADE",
        "decision_reason": "insufficient_after_cost_edge",
        "risk_outcome": "not_applicable",
        "evidence_refs": ["dataset-1", "risk-evidence-1"],
        "correlation_id": "corr-1",
        "event_ids": ["event-market", "event-decision"],
        "attributes": {
            "strategy": "baseline",
            "token": "super-secret",
            "nested": {"api_key": "hidden", "safe": "ok"},
        },
    }


class DecisionTraceEvidenceTests(unittest.TestCase):
    def test_missing_trace_read_does_not_create_parent_directory(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "not-created" / "decision-traces.jsonl"
            store = DecisionTraceStore(path)

            self.assertEqual(store.records(), [])
            self.assertTrue(store.verify())
            self.assertFalse(path.parent.exists())

    def test_relative_backing_path_is_frozen_across_cwd_change(self):
        original_cwd = os.getcwd()
        with TemporaryDirectory() as source_directory, TemporaryDirectory() as other_directory:
            try:
                source_root = Path(source_directory)
                other_root = Path(other_directory)
                os.chdir(source_root)
                store = DecisionTraceStore(Path("nested") / "decision-traces.jsonl")
                expected_path = source_root / "nested" / "decision-traces.jsonl"

                os.chdir(other_root)
                self.assertTrue(store.append(evidence_trace("decision-cwd-frozen")))

                self.assertEqual(store.path, expected_path)
                self.assertTrue(expected_path.exists())
                self.assertFalse(
                    (other_root / "nested" / "decision-traces.jsonl").exists()
                )
                self.assertTrue(store.verify())
            finally:
                os.chdir(original_cwd)

    def test_constructor_rejects_pathlike_callbacks_before_execution(self):
        calls = []

        class ExplodingPath:
            def __fspath__(self):
                calls.append("fspath")
                raise AssertionError("caller path callback must not execute")

        class ExplodingStr(str):
            def __fspath__(self):
                calls.append("str-fspath")
                raise AssertionError("string-subclass path callback must not execute")

        with self.assertRaisesRegex(TypeError, "exact str or pathlib Path"):
            DecisionTraceStore(ExplodingPath())
        with self.assertRaisesRegex(TypeError, "exact str or pathlib Path"):
            DecisionTraceStore(ExplodingStr("decision-traces.jsonl"))
        self.assertEqual(calls, [])

    def test_diagnostic_scalar_subclasses_and_objects_fail_before_callbacks(self):
        calls = []

        class ExplodingInt(int):
            def __repr__(self):
                calls.append("int-repr")
                raise AssertionError("caller integer callback must not execute")

            def __str__(self):
                calls.append("int-str")
                raise AssertionError("caller integer callback must not execute")

        class ExplodingObject:
            def __repr__(self):
                calls.append("object-repr")
                raise AssertionError("caller object callback must not execute")

            def __str__(self):
                calls.append("object-str")
                raise AssertionError("caller object callback must not execute")

        with TemporaryDirectory() as directory:
            path = Path(directory) / "decision-traces.jsonl"
            store = DecisionTraceStore(path)

            hostile_number = evidence_trace("decision-hostile-number")
            hostile_number["attributes"]["score"] = ExplodingInt(7)
            with self.assertRaisesRegex(ValueError, "JSON scalars"):
                store.append(hostile_number)
            self.assertFalse(path.exists())

            hostile_object = evidence_trace("decision-hostile-object")
            hostile_object["attributes"]["object"] = ExplodingObject()
            with self.assertRaisesRegex(ValueError, "JSON scalars"):
                store.append(hostile_object)
            self.assertFalse(path.exists())

        self.assertEqual(calls, [])

    def test_append_rejects_integer_outside_strict_json_resource_domain(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "decision-traces.jsonl"
            store = DecisionTraceStore(path)
            item = evidence_trace("decision-oversized-integer")
            item["attributes"]["huge_counter"] = int("9" * 641)

            with self.assertRaisesRegex(ValueError, "strict JSON resource domain"):
                store.append(item)

            self.assertFalse(path.exists())

    def test_append_rejects_excessive_json_nesting_before_publication(self):
        nested = 0
        for _ in range(129):
            nested = [nested]

        with TemporaryDirectory() as directory:
            path = Path(directory) / "decision-traces.jsonl"
            store = DecisionTraceStore(path)
            item = evidence_trace("decision-excessive-nesting")
            item["attributes"]["nested_depth"] = nested

            with self.assertRaisesRegex(ValueError, "strict JSON resource domain"):
                store.append(item)

            self.assertFalse(path.exists())

    def test_loader_rejects_preexisting_row_outside_strict_json_resource_domain(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "decision-traces.jsonl"
            store = DecisionTraceStore(path)
            path.write_text(
                '{"oversized":' + ("9" * 641) + '}\n',
                encoding="utf-8",
                newline="\n",
            )

            self.assertFalse(store.verify())
            with self.assertRaisesRegex(ValueError, "Decision trace chain is corrupt"):
                store.records()

    def test_noncanonical_jsonl_bytes_fail_verification(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "decision-traces.jsonl"
            store = DecisionTraceStore(path)
            store.append(evidence_trace("decision-noncanonical"))
            raw = path.read_text(encoding="utf-8")
            path.write_text(raw.replace("{", "{ ", 1), encoding="utf-8")
            self.assertFalse(store.verify())
            with self.assertRaisesRegex(ValueError, "chain is corrupt"):
                store.records()

    def test_blank_row_and_missing_final_newline_fail_verification(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "decision-traces.jsonl"
            store = DecisionTraceStore(path)
            store.append(evidence_trace("decision-canonical-row"))
            raw = path.read_text(encoding="utf-8")

            path.write_text(raw.rstrip("\n"), encoding="utf-8")
            self.assertFalse(store.verify())

            path.write_text(raw + "\n", encoding="utf-8")
            self.assertFalse(store.verify())

    def test_duplicate_key_textual_tamper_fails_verification(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "decision-traces.jsonl"
            store = DecisionTraceStore(path)
            store.append(evidence_trace("decision-duplicate-key"))
            raw = path.read_text(encoding="utf-8")
            tampered = raw.replace(
                "{",
                '{"trace_id":"shadow-duplicate",',
                1,
            )
            path.write_text(tampered, encoding="utf-8")
            self.assertFalse(store.verify())
            with self.assertRaisesRegex(ValueError, "chain is corrupt"):
                store.records()

    def test_bool_and_int_retries_are_not_semantically_conflated(self):
        with TemporaryDirectory() as directory:
            store = DecisionTraceStore(Path(directory) / "decision-traces.jsonl")
            original = evidence_trace("decision-json-type")
            original["attributes"]["semantic_value"] = True
            self.assertTrue(store.append(original))

            conflicting = evidence_trace("decision-json-type")
            conflicting["attributes"]["semantic_value"] = 1
            with self.assertRaisesRegex(ValueError, "different decision content"):
                store.append(conflicting)

    def test_tuple_diagnostics_normalize_to_json_arrays_idempotently(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "decision-traces.jsonl"
            store = DecisionTraceStore(path)
            item = evidence_trace("decision-tuple-roundtrip")
            item["attributes"]["levels"] = ("one", "two")

            self.assertTrue(store.append(item))
            self.assertFalse(store.append(item))
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["attributes"]["levels"], ["one", "two"])

    def test_append_rejects_store_owned_chain_fields(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "decision-traces.jsonl"
            store = DecisionTraceStore(path)
            for field, value in (
                ("recorded_at", "2000-01-01T00:00:00+00:00"),
                ("previous_hash", "0" * 64),
                ("record_hash", "1" * 64),
            ):
                with self.subTest(field=field):
                    item = evidence_trace("decision-owned-" + field)
                    item[field] = value
                    with self.assertRaisesRegex(ValueError, "store-owned fields"):
                        store.append(item)
                    self.assertFalse(path.exists())

    def test_accessible_export_rejects_polymorphic_trace_id_before_callbacks(self):
        calls = []

        class ExplodingStr(str):
            def strip(self, *args, **kwargs):
                calls.append("strip")
                raise AssertionError("trace-id callback must not execute")

            def __eq__(self, other):
                calls.append("eq")
                raise AssertionError("trace-id equality callback must not execute")

        with TemporaryDirectory() as directory:
            store = DecisionTraceStore(Path(directory) / "decision-traces.jsonl")
            store.append(evidence_trace("decision-export-ingress"))
            with self.assertRaisesRegex(ValueError, "exact non-empty string"):
                store.accessible_export(ExplodingStr("decision-export-ingress"))

        self.assertEqual(calls, [])

    def test_append_waits_for_shared_cross_process_writer_lock(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "decision-traces.jsonl"
            store = DecisionTraceStore(path)
            started = Event()
            completed = Event()
            results = []
            errors = []

            def writer():
                started.set()
                try:
                    results.append(store.append(evidence_trace("decision-locked")))
                except BaseException as error:
                    errors.append(error)
                finally:
                    completed.set()

            with durable_path_lock(path):
                thread = Thread(target=writer, daemon=True)
                thread.start()
                self.assertTrue(started.wait(timeout=1.0))
                self.assertFalse(
                    completed.wait(timeout=0.20),
                    "append bypassed the shared decision-trace writer lock",
                )

            self.assertTrue(completed.wait(timeout=5.0))
            thread.join(timeout=1.0)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(results, [True])
            self.assertTrue(store.verify())

    def test_records_hold_writer_lock_for_one_stable_generation(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "decision-traces.jsonl"
            store = DecisionTraceStore(path)
            store.append(evidence_trace("decision-existing"))

            reader_entered = Event()
            release_reader = Event()
            reader_completed = Event()
            writer_completed = Event()
            reader_results = []
            writer_results = []
            errors = []
            real_read = decision_trace_module._read_trace_text_descriptor_bound

            def blocked_read(candidate):
                reader_entered.set()
                if not release_reader.wait(timeout=5.0):
                    raise AssertionError("reader release timed out")
                return real_read(candidate)

            def reader():
                try:
                    reader_results.append(store.records())
                except BaseException as error:
                    errors.append(error)
                finally:
                    reader_completed.set()

            def writer():
                try:
                    writer_results.append(
                        store.append(evidence_trace("decision-concurrent"))
                    )
                except BaseException as error:
                    errors.append(error)
                finally:
                    writer_completed.set()

            with patch(
                "mvp.autotrade_mvp.decision_trace._read_trace_text_descriptor_bound",
                side_effect=blocked_read,
            ):
                reader_thread = Thread(target=reader, daemon=True)
                reader_thread.start()
                self.assertTrue(reader_entered.wait(timeout=1.0))

                writer_thread = Thread(target=writer, daemon=True)
                writer_thread.start()
                self.assertFalse(
                    writer_completed.wait(timeout=0.20),
                    "append crossed a retained trace read generation",
                )

                release_reader.set()
                self.assertTrue(reader_completed.wait(timeout=5.0))
                self.assertTrue(writer_completed.wait(timeout=5.0))
                reader_thread.join(timeout=1.0)
                writer_thread.join(timeout=1.0)

            self.assertEqual(errors, [])
            self.assertEqual(
                [record["trace_id"] for record in reader_results[0]],
                ["decision-existing"],
            )
            self.assertEqual(writer_results, [True])
            self.assertEqual(
                [record["trace_id"] for record in store.records()],
                ["decision-existing", "decision-concurrent"],
            )

    def test_records_validate_the_exact_loaded_snapshot_without_second_read(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "decision-traces.jsonl"
            store = DecisionTraceStore(path)
            store.append(evidence_trace())
            valid = json.loads(path.read_text(encoding="utf-8"))
            corrupt = dict(valid)
            corrupt["previous_hash"] = "1" * 64

            class SwitchingStore(DecisionTraceStore):
                def __init__(self, backing_path):
                    super().__init__(backing_path)
                    self.load_calls = 0

                def _load(self):
                    self.load_calls += 1
                    if self.load_calls == 1:
                        return [dict(corrupt)]
                    return [dict(valid)]

            switching = SwitchingStore(path)
            with self.assertRaisesRegex(ValueError, "chain is corrupt"):
                switching.records()
            self.assertEqual(switching.load_calls, 1)

    def test_atomic_publication_failure_preserves_previous_trace(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "decision-traces.jsonl"
            store = DecisionTraceStore(path)
            store.append(evidence_trace("decision-before-failure"))
            before = path.read_bytes()

            with patch(
                "mvp.autotrade_mvp.decision_trace.atomic_write_bytes",
                side_effect=OSError("injected publication failure"),
            ):
                with self.assertRaisesRegex(OSError, "injected publication failure"):
                    store.append(evidence_trace("decision-after-failure"))

            self.assertEqual(path.read_bytes(), before)
            self.assertTrue(store.verify())
            self.assertEqual(
                [item["trace_id"] for item in store.records()],
                ["decision-before-failure"],
            )

    def test_reader_rejects_validation_to_open_path_swap(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "decision-traces.jsonl"
            original_backup = root / "decision-traces-original.jsonl"
            attacker_path = root / "decision-traces-attacker.jsonl"

            store = DecisionTraceStore(path)
            store.append(evidence_trace("decision-original"))

            attacker_store = DecisionTraceStore(attacker_path)
            attacker_store.append(evidence_trace("decision-attacker"))

            swapped = False

            def validate_then_swap(candidate):
                nonlocal swapped
                validate_publication_destination(candidate)
                if not swapped and Path(candidate) == path:
                    path.replace(original_backup)
                    attacker_path.replace(path)
                    swapped = True

            with patch(
                "mvp.autotrade_mvp.decision_trace.validate_publication_destination",
                side_effect=validate_then_swap,
            ):
                with self.assertRaisesRegex(ValueError, "chain is corrupt"):
                    store.records()

            self.assertTrue(swapped)
            self.assertTrue(original_backup.exists())
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["trace_id"],
                "decision-attacker",
            )

    def test_descriptor_reader_accepts_same_inode_with_cross_api_ctime_variance(self):
        # Some Windows filesystems expose different creation-time resolution
        # through stat(path) and fstat(fd); no alias or mutation occurred.
        with TemporaryDirectory() as directory:
            path = Path(directory) / "decision-traces.jsonl"
            store = DecisionTraceStore(path)
            self.assertTrue(store.append(evidence_trace("decision-ctime-variance")))
            original_fstat = os.fstat

            class DescriptorStat:
                def __init__(self, value):
                    self._stat = value

                def __getattr__(self, name):
                    if name == "st_ctime_ns":
                        return self._stat.st_ctime_ns + 1000000000
                    return getattr(self._stat, name)

            with patch.object(
                decision_trace_module.os,
                "fstat",
                side_effect=lambda fd: DescriptorStat(original_fstat(fd)),
            ):
                content = decision_trace_module._read_trace_text_descriptor_bound(
                    path
                )
            self.assertEqual(
                json.loads(content.strip())["trace_id"],
                "decision-ctime-variance",
            )

    def test_reader_rejects_symlink_alias_instead_of_following_trace(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "decision-traces.jsonl"
            target = root / "decision-traces-target.jsonl"
            store = DecisionTraceStore(path)
            store.append(evidence_trace("decision-before-symlink"))
            path.replace(target)
            try:
                os.symlink(target.name, path)
            except OSError as error:
                self.skipTest(f"symbolic links unavailable: {error}")

            self.assertFalse(store.verify())
            with self.assertRaisesRegex(ValueError, "chain is corrupt"):
                store.records()

    def test_reader_rejects_hardlink_alias_instead_of_trusting_shared_inode(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "decision-traces.jsonl"
            alias = root / "decision-traces-alias.jsonl"
            store = DecisionTraceStore(path)
            store.append(evidence_trace("decision-before-hardlink"))
            try:
                os.link(path, alias)
            except OSError as error:
                self.skipTest(f"hard links unavailable: {error}")

            self.assertFalse(store.verify())
            with self.assertRaisesRegex(ValueError, "chain is corrupt"):
                store.records()

    def test_hardlink_alias_cannot_split_decision_trace_lock_identity(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "decision-traces.jsonl"
            alias = root / "decision-traces-alias.jsonl"
            store = DecisionTraceStore(path)
            store.append(evidence_trace("decision-original"))
            try:
                os.link(path, alias)
            except OSError as error:
                self.skipTest(f"hard links unavailable: {error}")

            alias_store = DecisionTraceStore(alias)
            with self.assertRaises(DurablePublishLockError):
                alias_store.append(evidence_trace("decision-alias"))

    def test_durable_trace_redacts_sensitive_diagnostics_before_persistence(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "decision-traces.jsonl"
            store = DecisionTraceStore(path)
            item = evidence_trace()
            self.assertTrue(store.append(item))

            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["attributes"]["token"], "[REDACTED]")
            self.assertEqual(persisted["attributes"]["nested"]["api_key"], "[REDACTED]")
            self.assertEqual(persisted["attributes"]["nested"]["safe"], "ok")
            self.assertNotIn("super-secret", path.read_text(encoding="utf-8"))
            self.assertTrue(store.verify())

    def test_reconstruction_requires_all_durable_links(self):
        with TemporaryDirectory() as directory:
            store = DecisionTraceStore(Path(directory) / "decision-traces.jsonl")
            store.append(evidence_trace())

            with self.assertRaisesRegex(ValueError, "trace evidence incomplete"):
                store.reconstruct(
                    "decision-1",
                    available_event_ids=["event-market"],
                    available_evidence_ids=["dataset-1", "risk-evidence-1"],
                )

            with self.assertRaisesRegex(ValueError, "trace evidence incomplete"):
                store.reconstruct(
                    "decision-1",
                    available_event_ids=["event-market", "event-decision"],
                    available_evidence_ids=["dataset-1"],
                )

            record = store.reconstruct(
                "decision-1",
                available_event_ids=["event-market", "event-decision"],
                available_evidence_ids=["dataset-1", "risk-evidence-1"],
            )
            self.assertEqual(record["trace_id"], "decision-1")
            self.assertEqual(record["event_ids"][-1], "event-decision")

    def test_accessible_export_escapes_line_and_bidi_spoofing_controls(self):
        with TemporaryDirectory() as directory:
            store = DecisionTraceStore(Path(directory) / "decision-traces.jsonl")
            item = evidence_trace("decision-accessible-controls")
            item["decision_reason"] = (
                "safe\nEvidence status: VERIFIED\u2028Risk outcome: ALLOW\u202e"
            )
            item["evidence_refs"] = [
                "dataset-1\nEvidence status: VERIFIED",
                "risk-evidence-1",
            ]
            item["attributes"]["forged\nEvidence status"] = "value\u2028next"
            store.append(item)

            exported = store.accessible_export("decision-accessible-controls")
            lines = exported.splitlines()

            self.assertEqual(
                [line for line in lines if line.startswith("Evidence status:")],
                ["Evidence status: UNVERIFIED"],
            )
            self.assertEqual(
                [line for line in lines if line.startswith("Risk outcome:")],
                ["Risk outcome: not_applicable"],
            )
            self.assertNotIn("\u2028", exported)
            self.assertNotIn("\u202e", exported)
            self.assertIn("\\u000aEvidence status: VERIFIED", exported)
            self.assertIn("\\u2028Risk outcome: ALLOW\\u202e", exported)
            self.assertIn("forged\\u000aEvidence status", exported)

    def test_accessible_export_is_linear_verified_and_redacted(self):
        with TemporaryDirectory() as directory:
            store = DecisionTraceStore(Path(directory) / "decision-traces.jsonl")
            store.append(evidence_trace())
            exported = store.accessible_export("decision-1")
            self.assertIn("Decision trace: decision-1", exported)
            self.assertIn("- event-decision", exported)
            self.assertIn("- dataset-1", exported)
            self.assertIn("[REDACTED]", exported)
            self.assertNotIn("super-secret", exported)

    def test_conflicting_retry_compares_redacted_persisted_semantics(self):
        with TemporaryDirectory() as directory:
            store = DecisionTraceStore(Path(directory) / "decision-traces.jsonl")
            first = evidence_trace()
            second = evidence_trace()
            second["attributes"]["token"] = "different-secret"
            self.assertTrue(store.append(first))
            self.assertFalse(store.append(second))

            third = evidence_trace()
            third["attributes"]["strategy"] = "changed"
            with self.assertRaisesRegex(ValueError, "different decision content"):
                store.append(third)

    def test_event_identity_must_be_unique_and_non_empty(self):
        with TemporaryDirectory() as directory:
            store = DecisionTraceStore(Path(directory) / "decision-traces.jsonl")
            duplicate = evidence_trace()
            duplicate["event_ids"] = ["event-1", "event-1"]
            with self.assertRaisesRegex(ValueError, "must not contain duplicates"):
                store.append(duplicate)

    def test_input_hash_must_be_canonical_sha256(self):
        with TemporaryDirectory() as directory:
            store = DecisionTraceStore(Path(directory) / "decision-traces.jsonl")
            for invalid in ("abc", "B" * 64, "g" * 64):
                item = evidence_trace()
                item["input_hash"] = invalid
                with self.assertRaisesRegex(ValueError, "lowercase SHA-256"):
                    store.append(item)

    def test_link_identities_reject_surrounding_whitespace(self):
        with TemporaryDirectory() as directory:
            store = DecisionTraceStore(Path(directory) / "decision-traces.jsonl")
            item = evidence_trace()
            item["evidence_refs"] = [" dataset-1"]
            with self.assertRaisesRegex(ValueError, "canonical"):
                store.append(item)

            item = evidence_trace()
            item["event_ids"] = ["event-market "]
            with self.assertRaisesRegex(ValueError, "canonical"):
                store.append(item)

    def test_non_finite_attributes_never_enter_durable_hash_chain(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "decision-traces.jsonl"
            store = DecisionTraceStore(path)
            item = evidence_trace()
            item["attributes"]["diagnostic_score"] = float("nan")
            with self.assertRaisesRegex(ValueError, "strict JSON resource domain"):
                store.append(item)
            self.assertFalse(path.exists())

    def test_tampered_non_finite_json_is_not_verified(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "decision-traces.jsonl"
            store = DecisionTraceStore(path)
            store.append(evidence_trace())
            raw = path.read_text(encoding="utf-8")
            raw = raw.replace('"strategy":"baseline"', '"strategy":NaN')
            path.write_text(raw, encoding="utf-8")
            self.assertFalse(store.verify())

    def test_idempotent_retry_never_masks_existing_chain_corruption(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "decision-traces.jsonl"
            store = DecisionTraceStore(path)
            item = evidence_trace()
            self.assertTrue(store.append(item))

            records = path.read_text(encoding="utf-8")
            records = records.replace(
                '"previous_hash":"' + "0" * 64 + '"',
                '"previous_hash":"' + "1" * 64 + '"',
            )
            path.write_text(records, encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "chain is corrupt"):
                store.append(item)

    def test_secret_aliases_remain_redacted_after_semantic_merge(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "decision-traces.jsonl"
            store = DecisionTraceStore(path)
            item = evidence_trace()
            item["attributes"].update(
                {
                    "Authorization-Header": "Bearer hidden",
                    "client.secret": "hidden-client",
                    "refresh-token": "hidden-refresh",
                    "private key pem": "hidden-key",
                }
            )
            store.append(item)
            persisted = json.loads(path.read_text(encoding="utf-8"))
            attrs = persisted["attributes"]
            self.assertEqual(attrs["Authorization-Header"], "[REDACTED]")
            self.assertEqual(attrs["client.secret"], "[REDACTED]")
            self.assertEqual(attrs["refresh-token"], "[REDACTED]")
            self.assertEqual(attrs["private key pem"], "[REDACTED]")

    def test_metric_backlog_rejects_non_finite_or_non_numeric_values(self):
        backlog = BoundedMetricBacklog(max_items=2)
        for value in (
            float("nan"),
            float("inf"),
            float("-inf"),
            True,
            "1.0",
        ):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError,
                "finite number",
            ):
                backlog.record("queue.delay", value)
        self.assertEqual(backlog.snapshot(), ())
        backlog.record("queue.delay", 1)
        self.assertEqual(backlog.snapshot()[0]["value"], 1)

    def test_metric_backlog_rejects_polymorphic_scalars_before_callbacks(self):
        class ExplodingInt(int):
            def __le__(self, other):
                raise AssertionError("integer comparison callback executed")

        class ExplodingFloat(float):
            def __float__(self):
                raise AssertionError("float callback executed")

        class ExplodingStr(str):
            def strip(self, *args, **kwargs):
                raise AssertionError("string callback executed")

        with self.assertRaisesRegex(ValueError, "positive integer"):
            BoundedMetricBacklog(max_items=ExplodingInt(2))

        backlog = BoundedMetricBacklog(max_items=2)
        with self.assertRaisesRegex(ValueError, "metric name is required"):
            backlog.record(ExplodingStr("queue.delay"), 1.0)
        with self.assertRaisesRegex(ValueError, "finite number"):
            backlog.record("queue.delay", ExplodingFloat(1.0))
        self.assertEqual(backlog.snapshot(), ())

    def test_metric_labels_reject_non_finite_json_values_at_ingress(self):
        backlog = BoundedMetricBacklog(max_items=2)
        for invalid in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ValueError,
                "JSON compliant",
            ):
                backlog.record("queue.delay", 1.0, diagnostic=invalid)
        self.assertEqual(backlog.snapshot(), ())

    def test_metric_value_rejects_integer_outside_strict_json_resource_domain(self):
        backlog = BoundedMetricBacklog(max_items=2)
        huge_value = int("9" * 641)

        with self.assertRaisesRegex(ValueError, "strict JSON resource domain"):
            backlog.record("queue.delay", huge_value)

        self.assertEqual(backlog.snapshot(), ())

    def test_metric_labels_reject_excessive_strict_json_nesting(self):
        nested = 0
        for _ in range(129):
            nested = [nested]

        backlog = BoundedMetricBacklog(max_items=2)
        with self.assertRaisesRegex(ValueError, "strict JSON resource domain"):
            backlog.record("queue.delay", 1.0, diagnostic=nested)

        self.assertEqual(backlog.snapshot(), ())

    def test_metric_snapshot_is_detached_from_internal_redacted_state(self):
        backlog = BoundedMetricBacklog(max_items=2)
        backlog.record(
            "queue.delay",
            1.0,
            nested={"safe": "ok", "token": "super-secret"},
        )

        first = backlog.snapshot()
        self.assertEqual(first[0]["labels"]["nested"]["token"], "[REDACTED]")
        first[0]["labels"]["nested"]["safe"] = "caller-mutated"
        first[0]["labels"]["nested"]["token"] = "caller-injected-secret"

        second = backlog.snapshot()
        self.assertEqual(second[0]["labels"]["nested"]["safe"], "ok")
        self.assertEqual(second[0]["labels"]["nested"]["token"], "[REDACTED]")

    def test_metric_backlog_is_bounded_and_redacts_labels(self):
        backlog = BoundedMetricBacklog(max_items=2)
        backlog.record("queue.delay", 1.0, token="a")
        backlog.record("queue.delay", 2.0, provider="sim")
        backlog.record("queue.delay", 3.0, password="b")
        self.assertEqual(backlog.dropped, 1)
        snapshot = backlog.snapshot()
        self.assertEqual(len(snapshot), 2)
        self.assertEqual(snapshot[-1]["labels"]["password"], "[REDACTED]")


if __name__ == "__main__":
    unittest.main()
