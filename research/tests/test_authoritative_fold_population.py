from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from unittest.mock import patch

from autotrade_research.artifacts import ArtifactStore
from autotrade_research.data.vintages import (
    HistoricalConflict,
    HistoricalDataError,
    HistoricalVintageRegistry,
    FrozenMarketPopulation,
    canonical_market_event_population_bytes,
    explicit_missingness,
    market_event_population_digest,
)
from autotrade_research.features.authoritative import (
    AuthoritativeFoldNormalizer,
    HistoricalFeatureInputSpec,
    authoritative_feature_points,
    authoritative_source_values,
    fit_authoritative_fold_normalizer,
    resolve_authoritative_feature_points,
)
from autotrade_research.features.causal import (
    CausalFold,
    FeaturePoint,
    FoldNormalizer,
    fit_normalizer,
)


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest(text: str) -> str:
    return "sha256:" + sha256(text.encode("utf-8")).hexdigest()


def _uuid(prefix: int, value: int) -> str:
    return f"{prefix:08x}-0000-4000-8000-{value:012x}"


def event(
    day: int,
    value: str,
    *,
    revision: int = 1,
    event_id: str | None = None,
    known_at: datetime | None = None,
) -> dict:
    source_at = BASE + timedelta(days=day)
    available = known_at or (source_at + timedelta(minutes=1))
    ingested = available + timedelta(minutes=1)
    identity = event_id or _uuid(1, day + 1)
    return {
        "event_id": identity,
        "instrument_version": "instrument:AAA:v1",
        "kind": "BAR",
        "source_event_at": _iso(source_at),
        "available_at": _iso(available),
        "ingested_at": _iso(ingested),
        "revision": str(revision),
        "availability_basis": "provider-history",
        "quality_flags": [],
        "payload": {"close": value},
        "raw_evidence_ref": {
            "artifact_id": _uuid(2 + revision, day + 1),
            "sha256": _digest(
                f"{identity}:{revision}:{_iso(ingested)}:{value}"
            ),
            "observed_at": _iso(ingested),
            "rights_id": "research-fixture",
        },
    }


class AuthoritativeFoldPopulationTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.registry = HistoricalVintageRegistry(root / "vintages")
        self.artifacts = ArtifactStore(root / "artifacts")
        self.dataset_id = _uuid(10, 1)
        self.spec = HistoricalFeatureInputSpec(
            spec_id="bar-close-return:v1",
            payload_value_field="close",
            window_count=2,
            event_kinds=("BAR",),
        )
        self.fold = CausalFold.create(
            fold_id="fold-authoritative-1",
            train_start=BASE,
            train_end=BASE + timedelta(days=3, hours=12),
            validation_start=BASE + timedelta(days=4),
            validation_end=BASE + timedelta(days=7),
            purge_seconds=0,
        )

    def _manifest(self, content_hashes, *, population_evidence=None, version=1):
        manifest = {
            "dataset_id": self.dataset_id,
            "version": str(version),
            "content_hashes": list(content_hashes),
            "instrument_universe_version": "universe:2026-01",
            "calendar_version": "calendar:2026-a",
            "coverage": {
                "from": _iso(BASE),
                "to": _iso(BASE + timedelta(days=7)),
            },
            "availability_policy": {
                "point_in_time": True,
                "no_future_leakage": True,
                "cutoff": _iso(BASE + timedelta(days=8)),
                "basis": "evidenced-first-availability",
            },
            "revision_policy": {
                "append_only": True,
                "replace_prior_vintages": False,
            },
            "normalization_version": "market-normalization:v1",
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
            "missingness_report": explicit_missingness(
                ["expected-series"], ["expected-series"]
            ).to_dict(),
            "source_evidence": [
                {
                    "artifact_id": _uuid(20, version),
                    "sha256": _digest(f"manifest-source:{version}"),
                    "observed_at": _iso(BASE + timedelta(days=8)),
                    "rights_id": "research-fixture",
                }
            ],
            "created_at": _iso(BASE + timedelta(days=8, seconds=1)),
        }
        if population_evidence is not None:
            manifest["source_evidence"].append(dict(population_evidence))
        return manifest

    @staticmethod
    def _base_events(*, through=6):
        values = ("100", "102", "105", "103", "107", "109", "111")
        return [event(day, values[day]) for day in range(through + 1)]

    def _register(self, rows, *, version=1):
        artifact_id = _uuid(30, version)
        artifact_manifest = self.artifacts.publish_bytes(
            artifact_id=artifact_id,
            data=canonical_market_event_population_bytes(rows),
            media_type="application/vnd.autotrade.market-event-population+json",
            rights={
                "storage": True,
                "export": False,
                "rights_id": "research-fixture",
            },
            source_refs=[f"dataset:{self.dataset_id}:v{version}"],
            metadata={
                "role": "market_event_population",
                "dataset_id": self.dataset_id,
                "dataset_version": version,
            },
        )
        population_evidence = {
            "artifact_id": artifact_id,
            "sha256": artifact_manifest["sha256"],
            "observed_at": _iso(BASE + timedelta(days=8)),
            "rights_id": "research-fixture",
        }
        return self.registry.commit(
            self._manifest(
                [artifact_manifest["sha256"]],
                population_evidence=population_evidence,
                version=version,
            )
        )

    def _validation_point(self, rows, manifest_digest, *, version=1):
        points = resolve_authoritative_feature_points(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=version,
            manifest_digest=manifest_digest,
            artifact_store=self.artifacts,
            events=rows,
            cutoff=BASE + timedelta(days=4, minutes=2),
            spec=self.spec,
        )
        candidates = [
            point
            for point in points
            if self.fold.validation_start
            <= point.decision_time
            <= self.fold.validation_end
        ]
        self.assertEqual(len(candidates), 1)
        return candidates[0]

    def test_exact_registered_population_fits_and_validates(self):
        rows = self._base_events()
        manifest_digest = self._register(rows)
        fitted = fit_authoritative_fold_normalizer(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=1,
            manifest_digest=manifest_digest,
            artifact_store=self.artifacts,
            events=rows,
            fold=self.fold,
            spec=self.spec,
            replay_common_cut_fingerprint="a" * 64,
        )
        point = self._validation_point(rows, manifest_digest)
        value = fitted.transform_validation(
            point,
            fold=self.fold,
            registry=self.registry,
            artifact_store=self.artifacts,
            events=rows,
            spec=self.spec,
            replay_common_cut_fingerprint="a" * 64,
        )
        self.assertIsInstance(value, Decimal)
        self.assertEqual(fitted.fold_normalizer.training_point_count, 3)
        self.assertTrue(fitted.fingerprint.startswith("sha256:"))

    def test_artifact_store_is_sufficient_without_caller_population(self):
        rows = self._base_events()
        manifest_digest = self._register(rows)
        fitted = fit_authoritative_fold_normalizer(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=1,
            manifest_digest=manifest_digest,
            artifact_store=self.artifacts,
            fold=self.fold,
            spec=self.spec,
        )
        points = resolve_authoritative_feature_points(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=1,
            manifest_digest=manifest_digest,
            artifact_store=self.artifacts,
            cutoff=BASE + timedelta(days=4, minutes=2),
            spec=self.spec,
        )
        point = next(
            candidate
            for candidate in points
            if self.fold.validation_start
            <= candidate.decision_time
            <= self.fold.validation_end
        )
        transformed = fitted.transform_validation(
            point,
            fold=self.fold,
            registry=self.registry,
            artifact_store=self.artifacts,
            spec=self.spec,
        )
        self.assertIsInstance(transformed, Decimal)

    def test_same_ids_and_revisions_with_altered_value_are_not_authority(self):
        rows = self._base_events()
        manifest_digest = self._register(rows)
        forged = [dict(row) for row in rows]
        forged[2] = {
            **forged[2],
            "payload": {"close": "999999"},
        }
        with self.assertRaisesRegex(
            HistoricalConflict,
            "caller market event cache differs",
        ):
            fit_authoritative_fold_normalizer(
                registry=self.registry,
                dataset_id=self.dataset_id,
                dataset_version=1,
                manifest_digest=manifest_digest,
                artifact_store=self.artifacts,
                events=forged,
                fold=self.fold,
                spec=self.spec,
            )

    def test_manifest_digest_mismatch_fails_before_fit(self):
        rows = self._base_events()
        self._register(rows)
        with self.assertRaisesRegex(
            HistoricalConflict,
            "manifest digest differs",
        ):
            fit_authoritative_fold_normalizer(
                registry=self.registry,
                dataset_id=self.dataset_id,
                dataset_version=1,
                manifest_digest="sha256:" + "0" * 64,
                artifact_store=self.artifacts,
                events=rows,
                fold=self.fold,
                spec=self.spec,
            )

    def test_stateful_manifest_mapping_cannot_switch_provenance_between_reads(self):
        rows = self._base_events()
        content_digest = market_event_population_digest(rows)
        raw = self._manifest([content_digest])
        bound_evidence = list(raw["source_evidence"]) + [
            {
                "artifact_id": _uuid(42, 1),
                "sha256": content_digest,
                "observed_at": _iso(BASE + timedelta(days=8)),
                "rights_id": "research-fixture",
            }
        ]

        class FlippingManifest(Mapping):
            def __init__(self, payload):
                self.payload = payload
                self.source_reads = 0

            def __iter__(self):
                return iter(self.payload)

            def __len__(self):
                return len(self.payload)

            def __getitem__(self, key):
                if key == "source_evidence":
                    self.source_reads += 1
                    if self.source_reads > 1:
                        return bound_evidence
                return self.payload[key]

        hostile = FlippingManifest(raw)
        with self.assertRaisesRegex(
            HistoricalDataError,
            "dataset-manifest-content-authority-v1",
        ):
            self.registry.commit(hostile)
        self.assertEqual(hostile.source_reads, 1)

    def test_registry_rejects_legacy_unbound_content_hash(self):
        rows = self._base_events()
        dangling_digest = market_event_population_digest(rows)
        with self.assertRaisesRegex(
            HistoricalDataError,
            "dataset-manifest-content-authority-v1",
        ):
            self.registry.commit(self._manifest([dangling_digest]))

    def test_research_manifest_cannot_extend_canonical_dataset_contract(self):
        rows = self._base_events()
        manifest = self._manifest([market_event_population_digest(rows)])
        manifest["content_refs"] = [{"role": "noncanonical"}]
        with self.assertRaisesRegex(HistoricalDataError, "unknown=.*content_refs"):
            self.registry.commit(manifest)


    def test_registered_manifest_uses_only_canonical_dataset_fields(self):
        rows = self._base_events()
        manifest_digest = self._register(rows)
        registered = self.registry.load(self.dataset_id, 1)
        schema_path = (
            Path(__file__).resolve().parents[2]
            / "contracts"
            / "jsonschema"
            / "data.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        definition = schema["$defs"]["DatasetManifest"]
        self.assertFalse(definition["additionalProperties"])
        self.assertEqual(set(registered), set(definition["properties"]))
        self.assertTrue(set(definition["required"]).issubset(registered))
        self.assertEqual(self.registry.digest(self.dataset_id, 1), manifest_digest)


    def test_market_population_role_rejects_multiple_bound_content_objects(self):
        rows_a = self._base_events()
        rows_b = self._base_events(through=5)
        artifacts = []
        for slot, rows in ((1, rows_a), (2, rows_b)):
            artifact_id = _uuid(40, slot)
            artifact_manifest = self.artifacts.publish_bytes(
                artifact_id=artifact_id,
                data=canonical_market_event_population_bytes(rows),
                media_type="application/vnd.autotrade.market-event-population+json",
                rights={
                    "storage": True,
                    "export": False,
                    "rights_id": "research-fixture",
                },
            )
            artifacts.append((artifact_id, artifact_manifest))

        manifest = self._manifest(
            [artifact[1]["sha256"] for artifact in artifacts],
            population_evidence={
                "artifact_id": artifacts[0][0],
                "sha256": artifacts[0][1]["sha256"],
                "observed_at": _iso(BASE + timedelta(days=8)),
                "rights_id": "research-fixture",
            },
        )
        manifest["source_evidence"].append(
            {
                "artifact_id": artifacts[1][0],
                "sha256": artifacts[1][1]["sha256"],
                "observed_at": _iso(BASE + timedelta(days=8)),
                "rights_id": "research-fixture",
            }
        )
        manifest_digest = self.registry.commit(manifest)
        with self.assertRaisesRegex(
            HistoricalDataError,
            "exactly one content hash",
        ):
            fit_authoritative_fold_normalizer(
                registry=self.registry,
                dataset_id=self.dataset_id,
                dataset_version=1,
                manifest_digest=manifest_digest,
                artifact_store=self.artifacts,
                fold=self.fold,
                spec=self.spec,
            )

    def test_market_population_role_rejects_noncanonical_media_type(self):
        rows = self._base_events()
        artifact_id = _uuid(41, 1)
        artifact_manifest = self.artifacts.publish_bytes(
            artifact_id=artifact_id,
            data=canonical_market_event_population_bytes(rows),
            media_type="application/json",
            rights={
                "storage": True,
                "export": False,
                "rights_id": "research-fixture",
            },
        )
        manifest_digest = self.registry.commit(
            self._manifest(
                [artifact_manifest["sha256"]],
                population_evidence={
                    "artifact_id": artifact_id,
                    "sha256": artifact_manifest["sha256"],
                    "observed_at": _iso(BASE + timedelta(days=8)),
                    "rights_id": "research-fixture",
                },
            )
        )
        with self.assertRaisesRegex(
            HistoricalDataError,
            "media type is not canonical",
        ):
            fit_authoritative_fold_normalizer(
                registry=self.registry,
                dataset_id=self.dataset_id,
                dataset_version=1,
                manifest_digest=manifest_digest,
                artifact_store=self.artifacts,
                fold=self.fold,
                spec=self.spec,
            )

    def test_market_population_rechecks_artifact_identity_and_digest(self):
        rows = self._base_events()
        manifest_digest = self._register(rows)
        original = ArtifactStore.read_authenticated_snapshot

        def wrong_identity(store, artifact_id):
            artifact_manifest, raw = original(store, artifact_id)
            return {**dict(artifact_manifest), "artifact_id": _uuid(99, 1)}, raw

        with patch.object(
            ArtifactStore,
            "read_authenticated_snapshot",
            new=wrong_identity,
        ):
            with self.assertRaisesRegex(
                HistoricalConflict,
                "artifact identity differs",
            ):
                fit_authoritative_fold_normalizer(
                    registry=self.registry,
                    dataset_id=self.dataset_id,
                    dataset_version=1,
                    manifest_digest=manifest_digest,
                    artifact_store=self.artifacts,
                    fold=self.fold,
                    spec=self.spec,
                )

        def wrong_digest(store, artifact_id):
            artifact_manifest, raw = original(store, artifact_id)
            return {
                **dict(artifact_manifest),
                "sha256": "sha256:" + "0" * 64,
            }, raw

        with patch.object(
            ArtifactStore,
            "read_authenticated_snapshot",
            new=wrong_digest,
        ):
            with self.assertRaisesRegex(
                HistoricalConflict,
                "artifact digest differs",
            ):
                fit_authoritative_fold_normalizer(
                    registry=self.registry,
                    dataset_id=self.dataset_id,
                    dataset_version=1,
                    manifest_digest=manifest_digest,
                    artifact_store=self.artifacts,
                    fold=self.fold,
                    spec=self.spec,
                )

    def test_market_population_rechecks_authenticated_bytes(self):
        rows = self._base_events()
        manifest_digest = self._register(rows)
        original = ArtifactStore.read_authenticated_snapshot

        def altered_bytes(store, artifact_id):
            artifact_manifest, raw = original(store, artifact_id)
            return artifact_manifest, raw + b" "

        with patch.object(
            ArtifactStore,
            "read_authenticated_snapshot",
            new=altered_bytes,
        ):
            with self.assertRaisesRegex(
                HistoricalConflict,
                "authenticated bytes differ",
            ):
                fit_authoritative_fold_normalizer(
                    registry=self.registry,
                    dataset_id=self.dataset_id,
                    dataset_version=1,
                    manifest_digest=manifest_digest,
                    artifact_store=self.artifacts,
                    fold=self.fold,
                    spec=self.spec,
                )

    def test_population_binding_requires_explicit_rights_identity(self):
        rows = self._base_events()
        artifact_id = _uuid(43, 1)
        artifact_manifest = self.artifacts.publish_bytes(
            artifact_id=artifact_id,
            data=canonical_market_event_population_bytes(rows),
            media_type="application/vnd.autotrade.market-event-population+json",
            rights={
                "storage": True,
                "export": False,
                "rights_id": "research-fixture",
            },
        )
        population_evidence = {
            "artifact_id": artifact_id,
            "sha256": artifact_manifest["sha256"],
            "observed_at": _iso(BASE + timedelta(days=8)),
        }
        manifest_digest = self.registry.commit(
            self._manifest(
                [artifact_manifest["sha256"]],
                population_evidence=population_evidence,
            )
        )
        with self.assertRaisesRegex(
            HistoricalDataError,
            "source evidence must bind rights_id",
        ):
            resolve_authoritative_feature_points(
                registry=self.registry,
                dataset_id=self.dataset_id,
                dataset_version=1,
                manifest_digest=manifest_digest,
                artifact_store=self.artifacts,
                cutoff=BASE + timedelta(days=4, minutes=2),
                spec=self.spec,
            )

    def test_artifact_rights_identity_is_bound_by_dataset_vintage(self):
        rows = self._base_events()
        artifact_id = _uuid(32, 1)
        artifact_manifest = self.artifacts.publish_bytes(
            artifact_id=artifact_id,
            data=canonical_market_event_population_bytes(rows),
            media_type="application/vnd.autotrade.market-event-population+json",
            rights={
                "storage": True,
                "export": False,
                "rights_id": "research-fixture",
            },
        )
        population_evidence = {
            "artifact_id": artifact_id,
            "sha256": artifact_manifest["sha256"],
            "observed_at": _iso(BASE + timedelta(days=8)),
            "rights_id": "different-rights",
        }
        manifest_digest = self.registry.commit(
            self._manifest(
                [artifact_manifest["sha256"]],
                population_evidence=population_evidence,
            )
        )
        with self.assertRaisesRegex(
            HistoricalConflict,
            "rights identity differs",
        ):
            fit_authoritative_fold_normalizer(
                registry=self.registry,
                dataset_id=self.dataset_id,
                dataset_version=1,
                manifest_digest=manifest_digest,
                artifact_store=self.artifacts,
                events=rows,
                fold=self.fold,
                spec=self.spec,
            )

    def test_authenticated_population_cannot_hide_noncanonical_market_field(self):
        rows = self._base_events()
        rows[0]["stream_generation"] = "1"
        manifest_digest = self._register(rows)
        with self.assertRaisesRegex(
            HistoricalDataError,
            "canonical MarketEvent contract",
        ):
            fit_authoritative_fold_normalizer(
                registry=self.registry,
                dataset_id=self.dataset_id,
                dataset_version=1,
                manifest_digest=manifest_digest,
                artifact_store=self.artifacts,
                events=rows,
                fold=self.fold,
                spec=self.spec,
            )

    def test_caller_event_iterable_runs_only_after_authenticated_snapshot(self):
        rows = self._base_events()
        manifest_digest = self._register(rows)
        state = {"authority_read": False}
        original = ArtifactStore.read_authenticated_snapshot

        class ProbeIterable:
            def __iter__(self_inner):
                if not state["authority_read"]:
                    raise AssertionError(
                        "caller event iterable executed before authenticated source read"
                    )
                return iter(rows)

        def counted_snapshot(store, artifact_id):
            manifest, raw = original(store, artifact_id)
            state["authority_read"] = True
            return manifest, raw

        with patch.object(
            ArtifactStore,
            "read_authenticated_snapshot",
            new=counted_snapshot,
        ):
            fitted = fit_authoritative_fold_normalizer(
                registry=self.registry,
                dataset_id=self.dataset_id,
                dataset_version=1,
                manifest_digest=manifest_digest,
                artifact_store=self.artifacts,
                events=ProbeIterable(),
                fold=self.fold,
                spec=self.spec,
            )

        self.assertTrue(state["authority_read"])
        self.assertEqual(fitted.dataset_id, self.dataset_id)

    def test_caller_owned_artifact_store_method_shadow_cannot_replace_authority(self):
        rows = self._base_events()
        manifest_digest = self._register(rows)
        state = {"called": False}

        def hostile_snapshot(*_args, **_kwargs):
            state["called"] = True
            raise AssertionError("caller-owned snapshot override executed")

        self.artifacts.read_authenticated_snapshot = hostile_snapshot
        try:
            fitted = fit_authoritative_fold_normalizer(
                registry=self.registry,
                dataset_id=self.dataset_id,
                dataset_version=1,
                manifest_digest=manifest_digest,
                artifact_store=self.artifacts,
                events=rows,
                fold=self.fold,
                spec=self.spec,
            )
        finally:
            del self.artifacts.read_authenticated_snapshot

        self.assertFalse(state["called"])
        self.assertEqual(fitted.dataset_id, self.dataset_id)

    def test_caller_owned_registry_resolver_shadow_cannot_replace_authority(self):
        rows = self._base_events()
        manifest_digest = self._register(rows)
        state = {"called": False}

        def hostile_resolver(*_args, **_kwargs):
            state["called"] = True
            raise AssertionError("caller-owned registry resolver override executed")

        self.registry.resolve_market_population = hostile_resolver
        try:
            fitted = fit_authoritative_fold_normalizer(
                registry=self.registry,
                dataset_id=self.dataset_id,
                dataset_version=1,
                manifest_digest=manifest_digest,
                artifact_store=self.artifacts,
                events=rows,
                fold=self.fold,
                spec=self.spec,
            )
        finally:
            del self.registry.resolve_market_population

        self.assertFalse(state["called"])
        self.assertEqual(fitted.dataset_id, self.dataset_id)

    def test_caller_owned_registry_load_shadow_cannot_replace_manifest_authority(self):
        rows = self._base_events()
        manifest_digest = self._register(rows)
        state = {"called": False}

        def hostile_load(*_args, **_kwargs):
            state["called"] = True
            raise AssertionError("caller-owned registry load override executed")

        self.registry.load = hostile_load
        try:
            fitted = fit_authoritative_fold_normalizer(
                registry=self.registry,
                dataset_id=self.dataset_id,
                dataset_version=1,
                manifest_digest=manifest_digest,
                artifact_store=self.artifacts,
                events=rows,
                fold=self.fold,
                spec=self.spec,
            )
        finally:
            del self.registry.load

        self.assertFalse(state["called"])
        self.assertEqual(fitted.dataset_id, self.dataset_id)

    def test_caller_constructed_frozen_population_cannot_cross_authority_seam(self):
        rows = self._base_events()
        manifest_digest = self._register(rows)
        cutoff = BASE + timedelta(days=4, minutes=2)
        issued = HistoricalVintageRegistry.resolve_market_population(
            self.registry,
            self.dataset_id,
            1,
            manifest_digest=manifest_digest,
            artifact_store=self.artifacts,
            cutoff=cutoff,
        )
        self.assertTrue(
            authoritative_feature_points(
                issued,
                spec=self.spec,
                registry=self.registry,
                artifact_store=self.artifacts,
            )
        )
        self.assertTrue(
            authoritative_source_values(
                issued,
                spec=self.spec,
                registry=self.registry,
                artifact_store=self.artifacts,
            )
        )
        forged = FrozenMarketPopulation(
            dataset_id=issued.dataset_id,
            version=issued.version,
            manifest_digest=issued.manifest_digest,
            cutoff=issued.cutoff,
            source_artifact_id=issued.source_artifact_id,
            source_content_digest=issued.source_content_digest,
            visible_event_json=issued.visible_event_json,
            fingerprint="sha256:" + "f" * 64,
        )
        for consumer in (
            authoritative_feature_points,
            authoritative_source_values,
        ):
            with self.subTest(consumer=consumer.__name__):
                with self.assertRaisesRegex(
                    HistoricalConflict,
                    "differs from authenticated authority",
                ):
                    consumer(
                        forged,
                        spec=self.spec,
                        registry=self.registry,
                        artifact_store=self.artifacts,
                    )

    def test_caller_forged_validation_feature_is_rejected(self):
        rows = self._base_events()
        manifest_digest = self._register(rows)
        fitted = fit_authoritative_fold_normalizer(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=1,
            manifest_digest=manifest_digest,
            artifact_store=self.artifacts,
            events=rows,
            fold=self.fold,
            spec=self.spec,
        )
        point = self._validation_point(rows, manifest_digest)
        forged = FeaturePoint(
            symbol=point.symbol,
            decision_time=point.decision_time,
            value=point.value + Decimal("1"),
            input_ids=point.input_ids,
            source_revisions=point.source_revisions,
            feature_name=point.feature_name,
        )
        with self.assertRaisesRegex(
            ValueError,
            "caller validation point differs",
        ):
            fitted.transform_validation(
                forged,
                fold=self.fold,
                registry=self.registry,
                artifact_store=self.artifacts,
                events=rows,
                spec=self.spec,
            )

    def test_future_dataset_values_cannot_change_training_fit(self):
        training_only = self._base_events(through=3)
        full = self._base_events(through=6)
        full[4] = event(4, "999999999")
        full[5] = event(5, "-999999999")
        full[6] = event(6, "777777777")
        first_manifest = self._register(training_only, version=1)
        second_manifest = self._register(full, version=2)

        first = fit_authoritative_fold_normalizer(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=1,
            manifest_digest=first_manifest,
            artifact_store=self.artifacts,
            events=training_only,
            fold=self.fold,
            spec=self.spec,
        )
        second = fit_authoritative_fold_normalizer(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=2,
            manifest_digest=second_manifest,
            artifact_store=self.artifacts,
            events=full,
            fold=self.fold,
            spec=self.spec,
        )
        self.assertEqual(first.fold_normalizer, second.fold_normalizer)
        self.assertNotEqual(
            first.training_population_fingerprint,
            second.training_population_fingerprint,
        )

    def test_late_correction_is_invisible_to_training_fit(self):
        baseline = self._base_events(through=3)
        original = baseline[2]
        late = event(
            2,
            "500",
            revision=2,
            event_id=original["event_id"],
            known_at=BASE + timedelta(days=5),
        )
        with_late = baseline + [late]
        first_manifest = self._register(baseline, version=1)
        second_manifest = self._register(with_late, version=2)

        first = fit_authoritative_fold_normalizer(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=1,
            manifest_digest=first_manifest,
            artifact_store=self.artifacts,
            events=baseline,
            fold=self.fold,
            spec=self.spec,
        )
        second = fit_authoritative_fold_normalizer(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=2,
            manifest_digest=second_manifest,
            artifact_store=self.artifacts,
            events=with_late,
            fold=self.fold,
            spec=self.spec,
        )
        self.assertEqual(first.fold_normalizer, second.fold_normalizer)
        self.assertNotEqual(
            first.training_population_fingerprint,
            second.training_population_fingerprint,
        )

    def test_correction_known_before_cutoff_changes_training_fit(self):
        baseline = self._base_events(through=3)
        original = baseline[2]
        early = event(
            2,
            "500",
            revision=2,
            event_id=original["event_id"],
            known_at=BASE + timedelta(days=3, hours=1),
        )
        with_early = baseline + [early]
        first_manifest = self._register(baseline, version=1)
        second_manifest = self._register(with_early, version=2)

        first = fit_authoritative_fold_normalizer(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=1,
            manifest_digest=first_manifest,
            artifact_store=self.artifacts,
            events=baseline,
            fold=self.fold,
            spec=self.spec,
        )
        second = fit_authoritative_fold_normalizer(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=2,
            manifest_digest=second_manifest,
            artifact_store=self.artifacts,
            events=with_early,
            fold=self.fold,
            spec=self.spec,
        )
        self.assertNotEqual(first.fold_normalizer, second.fold_normalizer)
        self.assertNotEqual(
            first.training_population_fingerprint,
            second.training_population_fingerprint,
        )

    def test_pre_cut_correction_preserves_what_was_known_before_correction(self):
        baseline = self._base_events(through=3)
        original = baseline[2]
        correction_known_at = BASE + timedelta(days=3, hours=1)
        correction = event(
            2,
            "500",
            revision=2,
            event_id=original["event_id"],
            known_at=correction_known_at,
        )
        rows = baseline + [correction]
        manifest_digest = self._register(rows)
        points = resolve_authoritative_feature_points(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=1,
            manifest_digest=manifest_digest,
            artifact_store=self.artifacts,
            events=rows,
            cutoff=self.fold.training_information_cutoff,
            spec=self.spec,
        )

        before_correction = next(
            point
            for point in points
            if point.decision_time == BASE + timedelta(days=3, minutes=2)
        )
        after_correction = next(
            point
            for point in points
            if point.decision_time == correction_known_at + timedelta(minutes=1)
        )
        original_id = f"{original['event_id']}@r1"
        correction_id = f"{original['event_id']}@r2"
        self.assertIn(original_id, before_correction.input_ids)
        self.assertNotIn(correction_id, before_correction.input_ids)
        self.assertIn(correction_id, after_correction.input_ids)

    def test_full_dataset_fitted_normalizer_cannot_masquerade_as_fold_fit(self):
        rows = self._base_events()
        manifest_digest = self._register(rows)
        legitimate = fit_authoritative_fold_normalizer(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=1,
            manifest_digest=manifest_digest,
            artifact_store=self.artifacts,
            events=rows,
            fold=self.fold,
            spec=self.spec,
        )
        all_points = resolve_authoritative_feature_points(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=1,
            manifest_digest=manifest_digest,
            artifact_store=self.artifacts,
            events=rows,
            cutoff=BASE + timedelta(days=6, minutes=2),
            spec=self.spec,
        )
        leaked = fit_normalizer(
            all_points,
            fit_cutoff=BASE + timedelta(days=6, minutes=2),
        )
        forged_fold_fit = FoldNormalizer(
            fold_id=legitimate.fold_normalizer.fold_id,
            fold_fingerprint=legitimate.fold_normalizer.fold_fingerprint,
            feature_name=legitimate.fold_normalizer.feature_name,
            normalizer=leaked,
            training_point_count=len(all_points),
        )
        forged = AuthoritativeFoldNormalizer(
            dataset_id=legitimate.dataset_id,
            dataset_version=legitimate.dataset_version,
            manifest_digest=legitimate.manifest_digest,
            training_population_fingerprint=legitimate.training_population_fingerprint,
            feature_spec_fingerprint=legitimate.feature_spec_fingerprint,
            replay_common_cut_fingerprint=None,
            fold_normalizer=forged_fold_fit,
        )
        point = self._validation_point(rows, manifest_digest)
        with self.assertRaisesRegex(
            ValueError,
            "stored fold normalizer differs",
        ):
            forged.transform_validation(
                point,
                fold=self.fold,
                registry=self.registry,
                artifact_store=self.artifacts,
                events=rows,
                spec=self.spec,
            )

    def test_replay_common_cut_must_match_at_validation(self):
        rows = self._base_events()
        manifest_digest = self._register(rows)
        fitted = fit_authoritative_fold_normalizer(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=1,
            manifest_digest=manifest_digest,
            artifact_store=self.artifacts,
            events=rows,
            fold=self.fold,
            spec=self.spec,
            replay_common_cut_fingerprint="a" * 64,
        )
        point = self._validation_point(rows, manifest_digest)
        with self.assertRaisesRegex(ValueError, "replay common-cut"):
            fitted.transform_validation(
                point,
                fold=self.fold,
                registry=self.registry,
                artifact_store=self.artifacts,
                events=rows,
                spec=self.spec,
                replay_common_cut_fingerprint="b" * 64,
            )

    def test_authoritative_fit_and_transform_ignore_ambient_decimal_context(self):
        rows = self._base_events()
        manifest_digest = self._register(rows)
        with localcontext() as context:
            context.prec = 6
            context.rounding = ROUND_FLOOR
            first = fit_authoritative_fold_normalizer(
                registry=self.registry,
                dataset_id=self.dataset_id,
                dataset_version=1,
                manifest_digest=manifest_digest,
                artifact_store=self.artifacts,
                events=rows,
                fold=self.fold,
                spec=self.spec,
            )
            point = self._validation_point(rows, manifest_digest)
            transformed_first = first.transform_validation(
                point,
                fold=self.fold,
                registry=self.registry,
                artifact_store=self.artifacts,
                events=rows,
                spec=self.spec,
            )

        with localcontext() as context:
            context.prec = 37
            context.rounding = ROUND_CEILING
            second = fit_authoritative_fold_normalizer(
                registry=self.registry,
                dataset_id=self.dataset_id,
                dataset_version=1,
                manifest_digest=manifest_digest,
                artifact_store=self.artifacts,
                events=rows,
                fold=self.fold,
                spec=self.spec,
            )
            transformed_second = second.transform_validation(
                point,
                fold=self.fold,
                registry=self.registry,
                artifact_store=self.artifacts,
                events=rows,
                spec=self.spec,
            )

        self.assertEqual(first, second)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(transformed_first, transformed_second)

    def test_simultaneous_distinct_events_require_registered_provider_order_policy(self):
        first = event(2, "105", event_id=_uuid(1, 200))
        second = event(2, "106", event_id=_uuid(1, 201))
        first["source_sequence"] = "40"
        second["source_sequence"] = "41"
        second["raw_evidence_ref"]["artifact_id"] = _uuid(3, 201)
        rows = [event(0, "100"), event(1, "102"), first, second]
        manifest_digest = self._register(rows)

        with self.assertRaisesRegex(
            ValueError,
            "registered provider-order policy",
        ):
            resolve_authoritative_feature_points(
                registry=self.registry,
                dataset_id=self.dataset_id,
                dataset_version=1,
                manifest_digest=manifest_digest,
                artifact_store=self.artifacts,
                events=rows,
                cutoff=BASE + timedelta(days=2, minutes=2),
                spec=self.spec,
            )


    def test_simultaneous_distinct_events_without_sequence_fail_closed(self):
        first = event(2, "105", event_id=_uuid(1, 210))
        second = event(2, "106", event_id=_uuid(1, 211))
        second["raw_evidence_ref"]["artifact_id"] = _uuid(3, 211)
        rows = [event(0, "100"), event(1, "102"), first, second]
        manifest_digest = self._register(rows)

        with self.assertRaisesRegex(
            ValueError,
            "registered provider-order policy",
        ):
            resolve_authoritative_feature_points(
                registry=self.registry,
                dataset_id=self.dataset_id,
                dataset_version=1,
                manifest_digest=manifest_digest,
                artifact_store=self.artifacts,
                events=rows,
                cutoff=BASE + timedelta(days=2, minutes=2),
                spec=self.spec,
            )

    def test_identical_authority_reproduces_exact_fit_identity(self):
        rows = self._base_events()
        manifest_digest = self._register(rows)
        first = fit_authoritative_fold_normalizer(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=1,
            manifest_digest=manifest_digest,
            artifact_store=self.artifacts,
            events=rows,
            fold=self.fold,
            spec=self.spec,
            replay_common_cut_fingerprint="c" * 64,
        )
        second = fit_authoritative_fold_normalizer(
            registry=self.registry,
            dataset_id=self.dataset_id,
            dataset_version=1,
            manifest_digest=manifest_digest,
            artifact_store=self.artifacts,
            events=list(reversed(rows)),
            fold=self.fold,
            spec=self.spec,
            replay_common_cut_fingerprint="c" * 64,
        )
        self.assertEqual(first, second)
        self.assertEqual(first.fingerprint, second.fingerprint)


if __name__ == "__main__":
    unittest.main()
