from __future__ import annotations

from hashlib import sha256
import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import UUID

from research.autotrade_research.data.vintages import HistoricalVintageRegistry
from research.autotrade_research.science import (
    ProtocolConflict,
    ProtocolViolation,
    ScientificRegistry,
)


def protocol() -> dict:
    return {
        "hypothesis": "candidate improves net utility over baseline",
        "strategy": {"name": "deterministic-baseline"},
        "features": ["return_5m"],
        "search_space": {"threshold": ["0.01", "0.02"]},
        "train_period": {"start": "2024-01-01", "end": "2024-12-31"},
        "validation_period": {"start": "2025-01-01", "end": "2025-06-30"},
        "test_period": {"start": "2025-07-01", "end": "2025-12-31"},
        "forward_period": {"start": "2026-01-01", "end": "2026-06-30"},
        "labels": ["net_return_after_cost"],
        "horizons": ["1d"],
        "purge_embargo": {"purge": "1d", "embargo": "1d"},
        "universe": ["BTC-USD"],
        "cost_fill_model": {"fee_bps": "10"},
        "baselines": ["cash", "existing_champion"],
        "primary_metrics": [{"name": "net_utility", "direction": "max"}],
        "secondary_metrics": [{"name": "drawdown", "direction": "min"}],
        "trial_budget": 2,
        "stopping_rules": {"max_failures": 2},
        "statistical_estimator": {"name": "block_bootstrap"},
        "multiplicity_treatment": {"method": "holm"},
        "minimum_practical_effect": "0.015",
        "risk_constraints": {"max_drawdown": "0.20"},
        "retention_tolerances": {"max_degradation": "0.02"},
        "promotion_rule": {"lower_bound_gt": "0.015"},
    }


def _token_uuid(token: str) -> str:
    return str(UUID(hex=sha256(("uuid:" + token).encode("utf-8")).hexdigest()[:32]))


def _vintage_manifest(token: str = "a") -> dict:
    return {
        "dataset_id": _token_uuid("dataset:" + token),
        "version": "1",
        "content_hashes": [
            "sha256:" + sha256(("content:" + token).encode("utf-8")).hexdigest()
        ],
        "instrument_universe_version": "universe:test-v1",
        "calendar_version": "calendar:test-v1",
        "coverage": {
            "from": "2026-01-01T00:00:00Z",
            "to": "2026-06-30T23:59:59Z",
        },
        "availability_policy": {
            "point_in_time": True,
            "no_future_leakage": True,
            "cutoff": "2026-06-30T23:59:59Z",
            "basis": "test-fixture-evidence",
        },
        "revision_policy": {
            "append_only": True,
            "replace_prior_vintages": False,
        },
        "normalization_version": "normalization:test-v1",
        "adjustment_policy": {
            "raw_retained": True,
            "adjusted_available": False,
            "method": "none",
        },
        "rights": {
            "storage": True,
            "research_use": True,
            "redistribution": False,
            "basis": "first-party-test-fixture",
        },
        "missingness_report": {
            "expected_count": 1,
            "observed_count": 1,
            "missing_keys": [],
            "invented_count": 0,
        },
        "source_evidence": [
            {
                "artifact_id": _token_uuid("evidence:" + token),
                "sha256": "sha256:"
                + sha256(("evidence:" + token).encode("utf-8")).hexdigest(),
                "observed_at": "2026-06-30T23:59:59Z",
            }
        ],
        "created_at": "2026-07-01T00:00:00Z",
    }


def _manifest_digest(token: str = "a") -> str:
    raw = json.dumps(
        _vintage_manifest(token),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + sha256(raw).hexdigest()


def holdout_identity(dataset_digit="a", *, start="2026-01-01", end="2026-06-30", role="LOCKED_FORWARD"):
    return {
        "dataset_digest": _manifest_digest(dataset_digit),
        "segment_start": start,
        "segment_end": end,
        "role": role,
    }


def preregister_holdout(
    registry: ScientificRegistry,
    protocol_id: str,
    *,
    dataset_digit: str = "a",
):
    vintages = HistoricalVintageRegistry(
        registry.path.parent / "historical-vintages"
    )
    manifest = _vintage_manifest(dataset_digit)
    committed_digest = vintages.commit(manifest)
    if committed_digest != _manifest_digest(dataset_digit):
        raise AssertionError("test vintage digest drifted from canonical manifest")
    return registry.preregister_locked_holdout(
        protocol_id,
        vintage_registry=vintages,
        dataset_id=manifest["dataset_id"],
        dataset_version=1,
    )


def exhaust_trials(registry: ScientificRegistry, protocol_id: str) -> None:
    """Close the registered development population before testing holdout semantics."""
    try:
        registry.locked_holdout_registration(protocol_id)
    except ProtocolViolation as error:
        if "lacks preregistered physical locked holdout" not in str(error):
            raise
        preregister_holdout(registry, protocol_id)
    remaining = registry.completeness(protocol_id)["remaining_trial_budget"]
    for index in range(remaining):
        registry.record_trial(
            protocol_id,
            status="FAILED",
            payload={"reason": "preregistered negative result", "index": index},
        )


class ProtocolRegistryHardeningTests(unittest.TestCase):
    def test_protocol_identity_is_immutable_and_idempotent(self):
        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")
            protocol_id = "00000000-0000-0000-0000-000000000001"
            first = registry.register_protocol(protocol(), protocol_id=protocol_id)
            second = registry.register_protocol(protocol(), protocol_id=protocol_id)
            self.assertEqual(first.protocol_hash, second.protocol_hash)

            changed = protocol()
            changed["minimum_practical_effect"] = "0.001"
            with self.assertRaisesRegex(ProtocolConflict, "immutable"):
                registry.register_protocol(changed, protocol_id=protocol_id)

    def test_stored_protocol_integrity_is_rechecked_before_every_authority_use(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "science.sqlite3"
            registry = ScientificRegistry(path)
            registered = registry.register_protocol(protocol())

            # Model storage corruption or a legacy/manual writer that bypassed the
            # append-only trigger.  The original protocol hash must remain the
            # authority; the altered trial budget may not be consumed anywhere.
            with sqlite3.connect(path) as connection:
                connection.execute("DROP TRIGGER protocols_no_update")
                cursor = connection.execute(
                    "UPDATE protocols SET payload_json="
                    "REPLACE(payload_json, ?, ?) WHERE protocol_id=?",
                    (
                        '"trial_budget":2',
                        '"trial_budget":3',
                        registered.protocol_id,
                    ),
                )
                self.assertEqual(cursor.rowcount, 1)
                connection.commit()

            for operation in (
                lambda: registry.protocol_registration(registered.protocol_id),
                lambda: registry.completeness(registered.protocol_id),
                lambda: registry.record_trial(
                    registered.protocol_id,
                    status="FAILED",
                    payload={"reason": "must-not-consume-corrupt-budget"},
                ),
                lambda: registry.register_evaluation(
                    registered.protocol_id,
                    holdout_id="corrupt-protocol-holdout",
                    holdout_identity=holdout_identity(),
                    result={"score": "0.1"},
                ),
            ):
                with self.subTest(operation=operation):
                    with self.assertRaisesRegex(
                        ProtocolViolation,
                        "registered protocol integrity mismatch",
                    ):
                        operation()

            self.assertEqual(
                registry.holdout_access_count(
                    registered.protocol_id,
                    "corrupt-protocol-holdout",
                ),
                0,
            )

    def test_binary_float_is_rejected_from_frozen_scientific_evidence(self):
        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")
            bad = protocol()
            bad["minimum_practical_effect"] = 0.015
            with self.assertRaisesRegex(ProtocolViolation, "binary float"):
                registry.register_protocol(bad)

    def test_protocol_rejects_overlapping_or_reversed_causal_periods(self):
        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")

            overlap = protocol()
            overlap["validation_period"] = {
                "start": "2024-12-31",
                "end": "2025-06-30",
            }
            with self.assertRaisesRegex(ProtocolViolation, "must end before"):
                registry.register_protocol(overlap)

            reversed_period = protocol()
            reversed_period["test_period"] = {
                "start": "2025-12-31",
                "end": "2025-07-01",
            }
            with self.assertRaisesRegex(ProtocolViolation, "start cannot follow end"):
                registry.register_protocol(reversed_period)

            malformed = protocol()
            malformed["forward_period"] = {
                "start": "2026/01/01",
                "end": "2026-06-30",
            }
            with self.assertRaisesRegex(ProtocolViolation, "ISO calendar dates"):
                registry.register_protocol(malformed)

    def test_causal_dates_reject_noncanonical_iso_aliases(self):
        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")

            basic_protocol = protocol()
            basic_protocol["train_period"] = {
                "start": "20240101",
                "end": "2024-12-31",
            }
            with self.assertRaisesRegex(ProtocolViolation, "canonical YYYY-MM-DD"):
                registry.register_protocol(basic_protocol)

            registered = registry.register_protocol(protocol())
            basic_holdout = holdout_identity(start="20260101")
            with self.assertRaisesRegex(ProtocolViolation, "canonical YYYY-MM-DD"):
                registry.record_holdout_access(
                    registered.protocol_id,
                    holdout_id="basic-date-alias",
                    holdout_identity=basic_holdout,
                    purpose="manual-inspection",
                )

    def test_purge_embargo_covers_horizon_and_actual_gap(self):
        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")

            short_purge = protocol()
            short_purge["purge_embargo"] = {"purge": "12h", "embargo": "1d"}
            with self.assertRaisesRegex(ProtocolViolation, "purge must cover"):
                registry.register_protocol(short_purge)

            short_embargo = protocol()
            short_embargo["purge_embargo"] = {"purge": "1d", "embargo": "12h"}
            with self.assertRaisesRegex(ProtocolViolation, "embargo must cover"):
                registry.register_protocol(short_embargo)

            short_gap = protocol()
            short_gap["horizons"] = ["2d"]
            short_gap["purge_embargo"] = {"purge": "2d", "embargo": "2d"}
            with self.assertRaisesRegex(ProtocolViolation, "gap is shorter"):
                registry.register_protocol(short_gap)

            exact_gap = protocol()
            exact_gap["horizons"] = ["2d"]
            exact_gap["purge_embargo"] = {"purge": "2d", "embargo": "2d"}
            exact_gap["train_period"] = {"start": "2024-01-01", "end": "2024-12-30"}
            exact_gap["validation_period"] = {"start": "2025-01-01", "end": "2025-06-29"}
            exact_gap["test_period"] = {"start": "2025-07-01", "end": "2025-12-30"}
            registered = registry.register_protocol(exact_gap)
            self.assertTrue(registered.protocol_hash.startswith("sha256:"))

    def test_purge_embargo_rejects_noncanonical_duration_shapes(self):
        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")

            malformed = protocol()
            malformed["horizons"] = ["1 day"]
            with self.assertRaisesRegex(ProtocolViolation, "canonical duration"):
                registry.register_protocol(malformed)

            whitespace = protocol()
            whitespace["horizons"] = [" 1d "]
            with self.assertRaisesRegex(ProtocolViolation, "canonical duration"):
                registry.register_protocol(whitespace)

            boolean = protocol()
            boolean["horizons"] = [True]
            with self.assertRaisesRegex(ProtocolViolation, "canonical duration"):
                registry.register_protocol(boolean)

            negative = protocol()
            negative["purge_embargo"] = {"purge": -1, "embargo": "1d"}
            with self.assertRaisesRegex(ProtocolViolation, "canonical duration"):
                registry.register_protocol(negative)

            missing = protocol()
            missing["purge_embargo"] = {"purge": "1d"}
            with self.assertRaisesRegex(ProtocolViolation, "exactly purge and embargo"):
                registry.register_protocol(missing)

            empty = protocol()
            empty["horizons"] = []
            with self.assertRaisesRegex(
                ProtocolViolation,
                "required protocol fields cannot be empty: horizons",
            ):
                registry.register_protocol(empty)

    def test_failed_and_discarded_trials_consume_registered_budget(self):
        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")
            registered = registry.register_protocol(protocol())
            preregister_holdout(registry, registered.protocol_id)
            registry.record_trial(
                registered.protocol_id,
                status="FAILED",
                payload={"reason": "fit_failed"},
            )
            registry.record_trial(
                registered.protocol_id,
                status="DISCARDED",
                payload={"reason": "invalid_candidate"},
            )
            completeness = registry.completeness(registered.protocol_id)
            self.assertEqual(completeness["recorded_trials"], 2)
            self.assertEqual(completeness["remaining_trial_budget"], 0)
            self.assertTrue(completeness["includes_non_successes"])

            with self.assertRaisesRegex(ProtocolViolation, "budget exhausted"):
                registry.record_trial(
                    registered.protocol_id,
                    status="COMPLETED",
                    payload={"metric": "0.02"},
                )

    def test_locked_holdout_vintage_must_cover_full_forward_period(self):
        cases = (
            (
                "late-start",
                {
                    "from": "2026-01-02T00:00:00Z",
                    "to": "2026-06-30T23:59:59Z",
                },
            ),
            (
                "early-end",
                {
                    "from": "2026-01-01T00:00:00Z",
                    "to": "2026-06-29T23:59:59Z",
                },
            ),
        )
        for token, coverage in cases:
            with self.subTest(token=token), TemporaryDirectory() as directory:
                root = Path(directory)
                science = ScientificRegistry(root / "science.sqlite3")
                registered = science.register_protocol(protocol())
                vintages = HistoricalVintageRegistry(root / "historical-vintages")
                manifest = _vintage_manifest(token)
                manifest["coverage"] = coverage
                vintages.commit(manifest)

                with self.assertRaisesRegex(
                    ProtocolViolation,
                    "does not cover protocol forward_period",
                ):
                    science.preregister_locked_holdout(
                        registered.protocol_id,
                        vintage_registry=vintages,
                        dataset_id=manifest["dataset_id"],
                        dataset_version=1,
                    )

                with self.assertRaisesRegex(
                    ProtocolViolation,
                    "lacks preregistered physical locked holdout",
                ):
                    science.locked_holdout_registration(registered.protocol_id)
                self.assertEqual(
                    science.completeness(registered.protocol_id)["recorded_trials"],
                    0,
                )

    def test_locked_holdout_vintage_availability_cutoff_must_cover_full_forward_period(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            science = ScientificRegistry(root / "science.sqlite3")
            registered = science.register_protocol(protocol())
            vintages = HistoricalVintageRegistry(root / "historical-vintages")
            manifest = _vintage_manifest("stale-cutoff")
            manifest["availability_policy"]["cutoff"] = "2026-06-29T23:59:59Z"
            vintages.commit(manifest)

            with self.assertRaisesRegex(
                ProtocolViolation,
                "availability cutoff does not cover protocol forward_period",
            ):
                science.preregister_locked_holdout(
                    registered.protocol_id,
                    vintage_registry=vintages,
                    dataset_id=manifest["dataset_id"],
                    dataset_version=1,
                )

            with self.assertRaisesRegex(
                ProtocolViolation,
                "lacks preregistered physical locked holdout",
            ):
                science.locked_holdout_registration(registered.protocol_id)
            self.assertEqual(
                science.completeness(registered.protocol_id)["recorded_trials"],
                0,
            )

    def test_trial_admission_requires_physical_holdout_preregistered_first(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "science.sqlite3"
            registry = ScientificRegistry(path)
            registered = registry.register_protocol(protocol())

            with self.assertRaisesRegex(
                ProtocolViolation,
                "lacks preregistered physical locked holdout",
            ):
                registry.record_trial(
                    registered.protocol_id,
                    status="FAILED",
                    payload={"reason": "must-not-start-search"},
                )
            with self.assertRaisesRegex(
                ProtocolViolation,
                "lacks preregistered physical locked holdout",
            ):
                registry.record_holdout_access(
                    registered.protocol_id,
                    holdout_id="unbound-peek",
                    holdout_identity=holdout_identity(),
                    purpose="must-not-select-after-observation",
                )
            self.assertEqual(
                registry.completeness(registered.protocol_id)["recorded_trials"],
                0,
            )

            first = preregister_holdout(
                registry,
                registered.protocol_id,
                dataset_digit="a",
            )
            again = preregister_holdout(
                registry,
                registered.protocol_id,
                dataset_digit="a",
            )
            self.assertEqual(first, again)

            registry.record_trial(
                registered.protocol_id,
                status="FAILED",
                payload={"reason": "registered-negative-result"},
            )
            reopened = ScientificRegistry(path)
            self.assertEqual(
                reopened.locked_holdout_registration(registered.protocol_id),
                first,
            )
            self.assertEqual(
                preregister_holdout(
                    reopened,
                    registered.protocol_id,
                    dataset_digit="a",
                ),
                first,
            )

            with self.assertRaisesRegex(
                ProtocolConflict,
                "immutable",
            ):
                preregister_holdout(
                    reopened,
                    registered.protocol_id,
                    dataset_digit="b",
                )

    def test_legacy_trial_cannot_gain_first_physical_holdout_after_outcome(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "science.sqlite3"
            registry = ScientificRegistry(path)
            registered = registry.register_protocol(protocol())
            payload = {"reason": "legacy-trial-before-holdout-freeze"}
            canonical = json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            )
            payload_hash = (
                "sha256:" + sha256(canonical.encode("utf-8")).hexdigest()
            )

            with sqlite3.connect(path) as connection:
                connection.execute(
                    "INSERT INTO trials("
                    "trial_id,protocol_id,status,payload_hash,payload_json,created_at"
                    ") VALUES(?,?,?,?,?,?)",
                    (
                        "11111111-1111-4111-8111-111111111111",
                        registered.protocol_id,
                        "FAILED",
                        payload_hash,
                        canonical,
                        "2026-09-28T12:00:00Z",
                    ),
                )
                connection.commit()

            with self.assertRaisesRegex(
                ProtocolViolation,
                "before the first trial",
            ):
                preregister_holdout(
                    registry,
                    registered.protocol_id,
                    dataset_digit="a",
                )
            with self.assertRaisesRegex(
                ProtocolViolation,
                "lacks preregistered physical locked holdout",
            ):
                registry.locked_holdout_registration(registered.protocol_id)

    def test_fresh_dataset_cannot_replace_preregistered_holdout_after_search(self):
        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")
            registered = registry.register_protocol(protocol())
            frozen = preregister_holdout(
                registry,
                registered.protocol_id,
                dataset_digit="a",
            )
            exhaust_trials(registry, registered.protocol_id)

            with self.assertRaisesRegex(
                ProtocolViolation,
                "match preregistered physical holdout",
            ):
                registry.register_evaluation(
                    registered.protocol_id,
                    holdout_id="fresh-b-after-search",
                    holdout_identity=holdout_identity("b"),
                    result={"net_utility": "0.999"},
                )
            self.assertEqual(
                registry.holdout_access_count(
                    registered.protocol_id,
                    "fresh-b-after-search",
                ),
                0,
            )

            accepted = registry.register_evaluation(
                registered.protocol_id,
                holdout_id="frozen-a",
                holdout_identity=frozen.identity(),
                result={"net_utility": "0.020"},
            )
            self.assertEqual(accepted["prior_access_count"], 0)
            self.assertEqual(accepted["untouched"], 1)

    def test_vintage_registry_instance_shadow_cannot_mint_holdout_authority(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            science = ScientificRegistry(root / "science.sqlite3")
            registered = science.register_protocol(protocol())
            vintages = HistoricalVintageRegistry(root / "historical-vintages")
            manifest = _vintage_manifest("shadow")
            vintages.commit(manifest)

            # Python instances permit method shadowing through __dict__.  The
            # science boundary must reconstruct a clean WP-10 owner rather than
            # dispatching an attacker-supplied instance attribute.
            vintages.load = lambda *_args, **_kwargs: _vintage_manifest("b")
            with self.assertRaisesRegex(
                TypeError,
                "unexpected mutable instance state",
            ):
                science.preregister_locked_holdout(
                    registered.protocol_id,
                    vintage_registry=vintages,
                    dataset_id=manifest["dataset_id"],
                    dataset_version=1,
                )
            self.assertEqual(
                science.completeness(registered.protocol_id)["recorded_trials"],
                0,
            )

    def test_generic_parent_binding_schema_migrates_fail_closed(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "science.sqlite3"
            science = ScientificRegistry(path)
            registered = science.register_protocol(protocol())
            identity = holdout_identity()
            identity_hash = "sha256:" + sha256(
                json.dumps(
                    identity,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()

            with sqlite3.connect(path) as connection:
                connection.execute("DROP TABLE protocol_locked_holdouts")
                connection.execute(
                    "CREATE TABLE protocol_locked_holdouts("
                    "protocol_id TEXT PRIMARY KEY,"
                    "holdout_identity_hash TEXT NOT NULL,"
                    "created_at TEXT NOT NULL"
                    ")"
                )
                connection.execute(
                    "INSERT OR IGNORE INTO holdouts("
                    "holdout_identity_hash,identity_json,created_at"
                    ") VALUES(?,?,?)",
                    (
                        identity_hash,
                        json.dumps(
                            identity,
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=False,
                            allow_nan=False,
                        ),
                        "2026-09-28T11:00:00+00:00",
                    ),
                )
                connection.execute(
                    "INSERT INTO protocol_locked_holdouts("
                    "protocol_id,holdout_identity_hash,created_at"
                    ") VALUES(?,?,?)",
                    (
                        registered.protocol_id,
                        identity_hash,
                        "2026-09-28T11:00:00+00:00",
                    ),
                )
                connection.commit()

            reopened = ScientificRegistry(path)
            with sqlite3.connect(path) as connection:
                columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(protocol_locked_holdouts)"
                    )
                }
            self.assertTrue(
                {"dataset_id", "dataset_version", "dataset_digest", "binding_hash"}
                <= columns
            )
            with self.assertRaisesRegex(
                ProtocolViolation,
                "dataset identity is corrupt|binding integrity mismatch",
            ):
                reopened.locked_holdout_registration(registered.protocol_id)

    def test_locked_holdout_dataset_uuid_is_exact_durable_identity(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "science.sqlite3"
            science = ScientificRegistry(path)
            registered = science.register_protocol(protocol())
            vintages = HistoricalVintageRegistry(root / "historical-vintages")
            manifest = _vintage_manifest("uuid-exact")
            vintages.commit(manifest)

            with self.assertRaisesRegex(
                ProtocolViolation,
                "dataset_id must be a canonical UUID",
            ):
                science.preregister_locked_holdout(
                    registered.protocol_id,
                    vintage_registry=vintages,
                    dataset_id="{" + manifest["dataset_id"] + "}",
                    dataset_version=1,
                )

            locked = science.preregister_locked_holdout(
                registered.protocol_id,
                vintage_registry=vintages,
                dataset_id=manifest["dataset_id"],
                dataset_version=1,
            )
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "DROP TRIGGER protocol_locked_holdouts_no_update"
                )
                cursor = connection.execute(
                    "UPDATE protocol_locked_holdouts SET dataset_id=? "
                    "WHERE protocol_id=?",
                    ("{" + locked.dataset_id + "}", registered.protocol_id),
                )
                self.assertEqual(cursor.rowcount, 1)
                connection.commit()

            with self.assertRaisesRegex(
                ProtocolViolation,
                "dataset identity is noncanonical",
            ):
                science.locked_holdout_registration(registered.protocol_id)
            with self.assertRaisesRegex(
                ProtocolViolation,
                "dataset identity is noncanonical",
            ):
                science.record_trial(
                    registered.protocol_id,
                    status="FAILED",
                    payload={"reason": "must-not-consume-aliased-dataset-id"},
                )
            self.assertEqual(
                science.completeness(registered.protocol_id)["recorded_trials"],
                0,
            )

    def test_corrupt_preregistration_binding_fails_closed(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "science.sqlite3"
            science = ScientificRegistry(path)
            registered = science.register_protocol(protocol())
            preregister_holdout(science, registered.protocol_id)

            with sqlite3.connect(path) as connection:
                connection.execute(
                    "DROP TRIGGER protocol_locked_holdouts_no_update"
                )
                cursor = connection.execute(
                    "UPDATE protocol_locked_holdouts SET dataset_version=? "
                    "WHERE protocol_id=?",
                    (2, registered.protocol_id),
                )
                self.assertEqual(cursor.rowcount, 1)
                connection.commit()

            with self.assertRaisesRegex(
                ProtocolViolation,
                "binding integrity mismatch",
            ):
                science.locked_holdout_registration(registered.protocol_id)
            with self.assertRaisesRegex(
                ProtocolViolation,
                "binding integrity mismatch",
            ):
                science.record_trial(
                    registered.protocol_id,
                    status="FAILED",
                    payload={"reason": "must-not-admit-under-corrupt-prereg"},
                )
            self.assertEqual(
                science.completeness(registered.protocol_id)["recorded_trials"],
                0,
            )

    def test_repeated_holdout_use_cannot_remain_untouched(self):
        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")
            registered = registry.register_protocol(protocol())
            exhaust_trials(registry, registered.protocol_id)
            holdout = "forward-2026-h1"

            first = registry.register_evaluation(
                registered.protocol_id,
                holdout_id=holdout,
                holdout_identity=holdout_identity(),
                result={"net_utility": "0.020"},
            )
            self.assertEqual(first["prior_access_count"], 0)
            self.assertEqual(first["untouched"], 1)

            second = registry.register_evaluation(
                registered.protocol_id,
                holdout_id=holdout,
                holdout_identity=holdout_identity(),
                result={"net_utility": "0.021"},
            )
            self.assertEqual(second["prior_access_count"], 1)
            self.assertEqual(second["untouched"], 0)
            self.assertEqual(
                registry.holdout_access_count(registered.protocol_id, holdout), 2
            )

    def test_holdout_access_by_one_protocol_contaminates_other_protocols(self):
        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")
            first_protocol = registry.register_protocol(protocol())
            preregister_holdout(registry, first_protocol.protocol_id)

            second_payload = protocol()
            second_payload["hypothesis"] = "independent candidate over same locked segment"
            second_protocol = registry.register_protocol(second_payload)
            exhaust_trials(registry, second_protocol.protocol_id)

            holdout = "forward-2026-h1"
            registry.record_holdout_access(
                first_protocol.protocol_id,
                holdout_id=holdout,
                holdout_identity=holdout_identity(),
                purpose="candidate-A-inspection",
            )

            evaluation = registry.register_evaluation(
                second_protocol.protocol_id,
                holdout_id=holdout,
                holdout_identity=holdout_identity(),
                result={"net_utility": "0.019"},
            )
            self.assertEqual(evaluation["prior_access_count"], 1)
            self.assertEqual(evaluation["untouched"], 0)


    def test_holdout_alias_rename_cannot_restore_untouched_status(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "science.sqlite3"
            first = ScientificRegistry(path)
            p1 = first.register_protocol(protocol())
            identity = holdout_identity()
            preregister_holdout(first, p1.protocol_id)
            first.record_holdout_access(
                p1.protocol_id,
                holdout_id="forward-display-A",
                holdout_identity=identity,
                purpose="manual-inspection",
            )

            # Reopen to prove the contamination key is durable, then use a
            # different display alias for the exact same evidence segment.
            reopened = ScientificRegistry(path)
            second_payload = protocol()
            second_payload["hypothesis"] = "second candidate after prior holdout exposure"
            p2 = reopened.register_protocol(second_payload)
            exhaust_trials(reopened, p2.protocol_id)
            evaluation = reopened.register_evaluation(
                p2.protocol_id,
                holdout_id="forward-display-B",
                holdout_identity=identity,
                result={"net_utility": "0.030"},
            )
            self.assertEqual(evaluation["prior_access_count"], 1)
            self.assertEqual(evaluation["untouched"], 0)

    def test_locked_evaluation_must_match_preregistered_forward_period_and_role(self):
        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")
            p = registry.register_protocol(protocol())
            exhaust_trials(registry, p.protocol_id)

            wrong_window = holdout_identity(
                start="2026-02-01",
                end="2026-06-30",
            )
            with self.assertRaisesRegex(
                ProtocolViolation,
                "match preregistered physical holdout",
            ):
                registry.register_evaluation(
                    p.protocol_id,
                    holdout_id="wrong-window",
                    holdout_identity=wrong_window,
                    result={"net_utility": "0.030"},
                )

            wrong_role = holdout_identity(role="VALIDATION")
            with self.assertRaisesRegex(
                ProtocolViolation,
                "match preregistered physical holdout",
            ):
                registry.register_evaluation(
                    p.protocol_id,
                    holdout_id="wrong-role",
                    holdout_identity=wrong_role,
                    result={"net_utility": "0.030"},
                )

            accepted = registry.register_evaluation(
                p.protocol_id,
                holdout_id="registered-forward",
                holdout_identity=holdout_identity(),
                result={"net_utility": "0.030"},
            )
            self.assertEqual(accepted["untouched"], 1)

    def test_holdout_alias_cannot_be_rebound_to_different_evidence(self):
        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")
            first = registry.register_protocol(protocol())
            preregister_holdout(registry, first.protocol_id, dataset_digit="a")
            registry.record_holdout_access(
                first.protocol_id,
                holdout_id="locked-forward",
                holdout_identity=holdout_identity("a"),
                purpose="initial-inspection",
            )

            second_payload = protocol()
            second_payload["hypothesis"] = "independent protocol bound to dataset B"
            second = registry.register_protocol(second_payload)
            preregister_holdout(registry, second.protocol_id, dataset_digit="b")
            exhaust_trials(registry, second.protocol_id)

            with self.assertRaisesRegex(ProtocolConflict, "cannot be rebound"):
                registry.register_evaluation(
                    second.protocol_id,
                    holdout_id="locked-forward",
                    holdout_identity=holdout_identity("b"),
                    result={"net_utility": "0.030"},
                )

    def test_holdout_identity_rejects_malformed_or_reversed_segment(self):
        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")
            p = registry.register_protocol(protocol())
            malformed = holdout_identity()
            malformed["dataset_digest"] = "sha256:ABC"
            with self.assertRaisesRegex(ProtocolViolation, "canonical sha256"):
                registry.record_holdout_access(
                    p.protocol_id,
                    holdout_id="locked",
                    holdout_identity=malformed,
                    purpose="inspection",
                )
            with self.assertRaisesRegex(ProtocolViolation, "cannot follow"):
                registry.record_holdout_access(
                    p.protocol_id,
                    holdout_id="locked",
                    holdout_identity=holdout_identity(
                        start="2026-06-30",
                        end="2026-01-01",
                    ),
                    purpose="inspection",
                )

    def test_manual_holdout_access_contaminates_later_locked_evaluation(self):
        with TemporaryDirectory() as directory:
            registry = ScientificRegistry(Path(directory) / "science.sqlite3")
            registered = registry.register_protocol(protocol())
            exhaust_trials(registry, registered.protocol_id)
            holdout = "forward-2026-h1"
            registry.record_holdout_access(
                registered.protocol_id,
                holdout_id=holdout,
                holdout_identity=holdout_identity(),
                purpose="parameter_selection",
            )
            evaluation = registry.register_evaluation(
                registered.protocol_id,
                holdout_id=holdout,
                holdout_identity=holdout_identity(),
                result={"net_utility": "0.019"},
            )
            self.assertEqual(evaluation["prior_access_count"], 1)
            self.assertEqual(evaluation["untouched"], 0)

    def test_database_triggers_block_destructive_rewrites(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "science.sqlite3"
            registry = ScientificRegistry(path)
            registered = registry.register_protocol(protocol())
            locked = preregister_holdout(registry, registered.protocol_id)
            connection = sqlite3.connect(path)
            try:
                with self.assertRaisesRegex(sqlite3.DatabaseError, "append-only"):
                    connection.execute(
                        "UPDATE protocols SET payload_json='{}' WHERE protocol_id=?",
                        (registered.protocol_id,),
                    )
                with self.assertRaisesRegex(sqlite3.DatabaseError, "append-only"):
                    connection.execute(
                        "DELETE FROM protocols WHERE protocol_id=?",
                        (registered.protocol_id,),
                    )
                with self.assertRaisesRegex(sqlite3.DatabaseError, "append-only"):
                    connection.execute(
                        "UPDATE protocol_locked_holdouts SET dataset_version=? "
                        "WHERE protocol_id=?",
                        (locked.dataset_version, registered.protocol_id),
                    )
                with self.assertRaisesRegex(sqlite3.DatabaseError, "append-only"):
                    connection.execute(
                        "DELETE FROM protocol_locked_holdouts WHERE protocol_id=?",
                        (registered.protocol_id,),
                    )
            finally:
                connection.close()

    def test_registry_state_survives_restart(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "science.sqlite3"
            first = ScientificRegistry(path)
            registered = first.register_protocol(protocol())
            preregister_holdout(first, registered.protocol_id)
            first.record_trial(
                registered.protocol_id,
                status="FAILED",
                payload={"reason": "fit_failed"},
            )

            reopened = ScientificRegistry(path)
            completeness = reopened.completeness(registered.protocol_id)
            self.assertEqual(completeness["recorded_trials"], 1)
            self.assertEqual(completeness["statuses"], {"FAILED": 1})


if __name__ == "__main__":
    unittest.main()
