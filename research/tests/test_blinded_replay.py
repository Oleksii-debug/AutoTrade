from datetime import datetime, timezone
import json
import unittest

from autotrade_research.evaluation.replay.blinding import (
    BlindedCausalFeeder,
    BlindedFeederCheckpoint,
    BlindingError,
    BlindingProfile,
    CalendarField,
    IdentityField,
    blind_dataset,
)
from autotrade_research.evaluation.replay.feeder import CausalDataset, CausalEvent


MANIFEST = "sha256:" + ("1" * 64)
SHUFFLE = "sha256:" + ("a" * 64)


def event(
    event_id,
    *,
    instrument="BTC-USD",
    provider="Kraken",
    available_at="2024-03-12T10:00:00Z",
    event_time=None,
    ingested_at=None,
    sequence=0,
    payload_extra=None,
    kind="TRADE",
):
    payload = {
        "instrument_id": instrument,
        "provider_id": provider,
        "price": "71234.50",
        "quantity": "0.125",
        "multiplier": "1",
        "tick": "0.50",
    }
    if payload_extra:
        payload.update(payload_extra)
    return CausalEvent.create(
        event_id=event_id,
        kind=kind,
        event_time=event_time or available_at,
        available_at=available_at,
        ingested_at=ingested_at or available_at,
        source_priority=10,
        source_sequence=sequence,
        payload=payload,
    )


def dataset(*events):
    return CausalDataset.create(manifest_sha256=MANIFEST, events=events)


def profile(*, calendars=()):
    return BlindingProfile(
        identity_fields=(
            IdentityField(("instrument_id",), "INSTRUMENT", required=True),
            IdentityField(("provider_id",), "PROVIDER", required=True),
        ),
        calendar_fields=tuple(calendars),
    )


class Section17BlindedReplayTests(unittest.TestCase):
    def blind(self, source, **kwargs):
        return blind_dataset(
            dataset=source,
            experiment_id=kwargs.pop("experiment_id", "section17-exp-a"),
            shuffle_key_sha256=kwargs.pop("shuffle_key_sha256", SHUFFLE),
            profile=kwargs.pop("profile", profile()),
            training_cutoff_uncertainty=kwargs.pop(
                "training_cutoff_uncertainty",
                "pretraining coverage of the historical period is unknown",
            ),
            **kwargs,
        )

    def test_masks_event_identity_instrument_provider_and_absolute_time(self):
        result = self.blind(dataset(event("btc-2024-03-12-kraken")))
        item = result.events[0]
        self.assertEqual(item.event_id, "Event 000001")
        self.assertRegex(item.payload["instrument_id"], r"^Instrument [0-9A-F]{64}$")
        self.assertRegex(item.payload["provider_id"], r"^Provider [0-9A-F]{64}$")
        self.assertEqual(item.event_time_us, 0)
        self.assertEqual(item.available_at_us, 0)
        text = json.dumps(
            {
                "event_id": item.event_id,
                "payload": dict(item.payload),
                "event_time_us": item.event_time_us,
            },
            sort_keys=True,
        )
        self.assertNotIn("BTC", text)
        self.assertNotIn("Kraken", text)
        self.assertNotIn("2024-03-12", text)
        self.assertFalse(hasattr(item, "source_priority"))
        self.assertFalse(hasattr(item, "source_sequence"))

    def test_preserves_financial_economics_exactly_in_identity_mode(self):
        result = self.blind(dataset(event("one")))
        payload = result.events[0].payload
        self.assertEqual(payload["price"], "71234.50")
        self.assertEqual(payload["quantity"], "0.125")
        self.assertEqual(payload["multiplier"], "1")
        self.assertEqual(payload["tick"], "0.50")
        self.assertEqual(result.price_scale_mode, "IDENTITY")

    def test_non_identity_scaling_is_rejected(self):
        with self.assertRaisesRegex(BlindingError, "non-identity price scaling"):
            BlindingProfile(
                identity_fields=(
                    IdentityField(("instrument_id",), "INSTRUMENT"),
                ),
                price_scale_mode="SCALE_10X",
            )

    def test_strict_leak_scan_cannot_be_disabled(self):
        with self.assertRaisesRegex(BlindingError, "cannot be disabled"):
            BlindingProfile(
                identity_fields=(
                    IdentityField(("instrument_id",), "INSTRUMENT"),
                ),
                strict_text_scan=False,
            )

    def test_rejects_raw_identity_repeated_in_unbound_text(self):
        source = dataset(
            event(
                "one",
                payload_extra={"description": "BTC-USD spot market snapshot"},
            )
        )
        with self.assertRaisesRegex(BlindingError, "repeats a declared raw identity"):
            self.blind(source)

    def test_short_country_like_identity_does_not_false_match_unrelated_word(self):
        source = dataset(
            event(
                "one",
                instrument="US",
                payload_extra={"status": "active"},
            )
        )
        result = self.blind(source)
        self.assertEqual(result.events[0].payload["status"], "active")

    def test_rejects_declared_identity_used_as_dynamic_payload_key(self):
        for leaking_key in ("BTC-USD", "BTC-USD_close"):
            with self.subTest(leaking_key=leaking_key):
                source = dataset(
                    event(
                        "one",
                        payload_extra={leaking_key: "dynamic-key-leak"},
                    )
                )
                with self.assertRaisesRegex(BlindingError, "payload key"):
                    self.blind(source)

    def test_rejects_absolute_calendar_string_not_declared_for_masking(self):
        source = dataset(
            event(
                "one",
                payload_extra={"expiry": "2024-06-28"},
            )
        )
        with self.assertRaisesRegex(BlindingError, "undeclared absolute date"):
            self.blind(source)

    def test_identity_aliases_share_one_pseudonym(self):
        source = dataset(
            event(
                "one",
                payload_extra={
                    "instrument_name": "Bitcoin",
                    "ticker": "BTC",
                },
            )
        )
        alias_profile = BlindingProfile(
            identity_fields=(
                IdentityField(
                    ("instrument_id",),
                    "INSTRUMENT",
                    required=True,
                    alias_paths=(("instrument_name",), ("ticker",)),
                ),
                IdentityField(("provider_id",), "PROVIDER", required=True),
            )
        )
        result = self.blind(source, profile=alias_profile)
        item = result.events[0]
        token = item.payload["instrument_id"]
        self.assertRegex(token, r"^Instrument [0-9A-F]{64}$")
        self.assertEqual(item.payload["instrument_name"], token)
        self.assertEqual(item.payload["ticker"], token)

    def test_identity_alias_is_included_in_leak_scan(self):
        source = dataset(
            event(
                "one",
                payload_extra={
                    "instrument_name": "Bitcoin",
                    "description": "Bitcoin rallied sharply",
                },
            )
        )
        alias_profile = BlindingProfile(
            identity_fields=(
                IdentityField(
                    ("instrument_id",),
                    "INSTRUMENT",
                    required=True,
                    alias_paths=(("instrument_name",),),
                ),
                IdentityField(("provider_id",), "PROVIDER", required=True),
            )
        )
        with self.assertRaisesRegex(BlindingError, "repeats a declared raw identity"):
            self.blind(source, profile=alias_profile)

    def test_alias_without_canonical_identity_fails_closed(self):
        source = CausalDataset.create(
            manifest_sha256=MANIFEST,
            events=[
                CausalEvent.create(
                    event_id="one",
                    kind="TRADE",
                    event_time="2024-03-12T10:00:00Z",
                    available_at="2024-03-12T10:00:00Z",
                    ingested_at="2024-03-12T10:00:00Z",
                    source_priority=1,
                    source_sequence=1,
                    payload={
                        "instrument_name": "Bitcoin",
                        "provider_id": "Kraken",
                        "price": "1",
                    },
                )
            ],
        )
        alias_profile = BlindingProfile(
            identity_fields=(
                IdentityField(
                    ("instrument_id",),
                    "INSTRUMENT",
                    alias_paths=(("instrument_name",),),
                ),
                IdentityField(("provider_id",), "PROVIDER", required=True),
            )
        )
        with self.assertRaisesRegex(BlindingError, "without canonical identity"):
            self.blind(source, profile=alias_profile)

    def test_same_alias_cannot_name_two_canonical_identities(self):
        source = dataset(
            event(
                "one",
                instrument="ASSET-A",
                payload_extra={"instrument_name": "Shared Name"},
                sequence=1,
            ),
            event(
                "two",
                instrument="ASSET-B",
                provider="Coinbase",
                available_at="2024-03-12T10:01:00Z",
                payload_extra={"instrument_name": "Shared Name"},
                sequence=2,
            ),
        )
        alias_profile = BlindingProfile(
            identity_fields=(
                IdentityField(
                    ("instrument_id",),
                    "INSTRUMENT",
                    required=True,
                    alias_paths=(("instrument_name",),),
                ),
                IdentityField(("provider_id",), "PROVIDER", required=True),
            )
        )
        with self.assertRaisesRegex(BlindingError, "multiple canonical identities"):
            self.blind(source, profile=alias_profile)

    def test_alias_cannot_collide_with_other_canonical_identity(self):
        source = dataset(
            event(
                "one",
                instrument="ASSET-A",
                payload_extra={"instrument_name": "ASSET-B"},
                sequence=1,
            ),
            event(
                "two",
                instrument="ASSET-B",
                provider="Coinbase",
                available_at="2024-03-12T10:01:00Z",
                payload_extra={"instrument_name": "Second Name"},
                sequence=2,
            ),
        )
        alias_profile = BlindingProfile(
            identity_fields=(
                IdentityField(
                    ("instrument_id",),
                    "INSTRUMENT",
                    required=True,
                    alias_paths=(("instrument_name",),),
                ),
                IdentityField(("provider_id",), "PROVIDER", required=True),
            )
        )
        with self.assertRaisesRegex(BlindingError, "collides with another canonical"):
            self.blind(source, profile=alias_profile)

    def test_rejects_multiple_absolute_calendar_spellings(self):
        leaking_dates = (
            "2024-03-12",
            "2024/03/12",
            "2024.03.12",
            "12/03/2024",
            "12.03.2024",
            "March 12, 2024",
            "12 March 2024",
        )
        for leaked in leaking_dates:
            with self.subTest(leaked=leaked):
                source = dataset(
                    event("one", payload_extra={"note": f"known on {leaked}"})
                )
                with self.assertRaisesRegex(BlindingError, "absolute date"):
                    self.blind(source)

    def test_rejects_unregistered_identity_namespace(self):
        with self.assertRaisesRegex(BlindingError, "registered neutral"):
            IdentityField(("instrument_id",), "SECRET_CUSTOM_ROLE")

    def test_rejects_parent_child_path_overlap(self):
        with self.assertRaisesRegex(BlindingError, "non-overlapping"):
            BlindingProfile(
                identity_fields=(
                    IdentityField(("entity",), "ENTITY"),
                    IdentityField(("entity", "provider"), "PROVIDER"),
                )
            )

    def test_masks_declared_payload_calendar_as_relative_offset(self):
        source = dataset(
            event(
                "one",
                available_at="2024-03-12T10:00:00Z",
                payload_extra={"expiry": "2024-06-28T00:00:00Z"},
            )
        )
        result = self.blind(
            source,
            profile=profile(calendars=(CalendarField(("expiry",), required=True),)),
        )
        expiry = result.events[0].payload["expiry"]
        self.assertTrue(expiry.startswith("T+"))
        self.assertTrue(expiry.endswith("us"))
        self.assertNotIn("2024", expiry)

    def test_date_only_calendar_field_is_supported_without_exposing_date(self):
        source = dataset(event("one", payload_extra={"expiry": "2024-06-28"}))
        result = self.blind(
            source,
            profile=profile(calendars=(CalendarField(("expiry",), required=True),)),
        )
        self.assertRegex(result.events[0].payload["expiry"], r"^T\+\d+us$")

    def test_required_identity_path_must_exist(self):
        source = CausalDataset.create(
            manifest_sha256=MANIFEST,
            events=[
                CausalEvent.create(
                    event_id="one",
                    kind="TRADE",
                    event_time="2024-01-01T00:00:00Z",
                    available_at="2024-01-01T00:00:00Z",
                    ingested_at="2024-01-01T00:00:00Z",
                    source_priority=1,
                    source_sequence=1,
                    payload={"provider_id": "Kraken", "price": "1"},
                )
            ],
        )
        with self.assertRaisesRegex(BlindingError, "required identity path"):
            self.blind(source)

    def test_required_calendar_path_must_exist(self):
        source = dataset(event("one"))
        with self.assertRaisesRegex(BlindingError, "required calendar path"):
            self.blind(
                source,
                profile=profile(
                    calendars=(CalendarField(("expiry",), required=True),)
                ),
            )

    def test_identity_alias_paths_must_be_unique(self):
        with self.assertRaisesRegex(BlindingError, "alias paths"):
            IdentityField(
                ("instrument_id",),
                "INSTRUMENT",
                alias_paths=(("instrument_name",), ("instrument_name",)),
            )
        with self.assertRaisesRegex(BlindingError, "alias paths"):
            IdentityField(
                ("instrument_id",),
                "INSTRUMENT",
                alias_paths=(("instrument_id",),),
            )

    def test_identity_and_calendar_paths_must_not_overlap(self):
        with self.assertRaisesRegex(BlindingError, "unique and non-overlapping"):
            BlindingProfile(
                identity_fields=(IdentityField(("x",), "INSTRUMENT"),),
                calendar_fields=(CalendarField(("x",)),),
            )

    def test_mapping_is_deterministic_for_same_experiment(self):
        source = dataset(
            event("one", instrument="AAA", provider="P1", sequence=1),
            event(
                "two",
                instrument="BBB",
                provider="P2",
                available_at="2024-03-12T10:01:00Z",
                sequence=2,
            ),
            event(
                "three",
                instrument="CCC",
                provider="P3",
                available_at="2024-03-12T10:02:00Z",
                sequence=3,
            ),
        )
        first = self.blind(source)
        second = self.blind(source)
        self.assertEqual(first, second)
        self.assertEqual(first.mapping_sha256, second.mapping_sha256)
        self.assertEqual(first.blinded_dataset_sha256, second.blinded_dataset_sha256)

    def test_different_experiment_id_changes_mapping_commitment_and_assignments(self):
        source = dataset(
            event("one", instrument="AAA", provider="P1", sequence=1),
            event(
                "two",
                instrument="BBB",
                provider="P2",
                available_at="2024-03-12T10:01:00Z",
                sequence=2,
            ),
            event(
                "three",
                instrument="CCC",
                provider="P3",
                available_at="2024-03-12T10:02:00Z",
                sequence=3,
            ),
        )
        first = self.blind(source, experiment_id="experiment-alpha")
        second = self.blind(source, experiment_id="experiment-beta")
        self.assertNotEqual(first.mapping_sha256, second.mapping_sha256)
        self.assertNotEqual(first.blinded_dataset_sha256, second.blinded_dataset_sha256)
        first_ids = [item.payload["instrument_id"] for item in first.events]
        second_ids = [item.payload["instrument_id"] for item in second.events]
        self.assertNotEqual(first_ids, second_ids)

    def test_namespace_separation_prevents_cross_role_alias_reuse(self):
        source = dataset(event("one", instrument="SAME", provider="SAME"))
        result = self.blind(source)
        item = result.events[0]
        self.assertRegex(item.payload["instrument_id"], r"^Instrument [0-9A-F]{64}$")
        self.assertRegex(item.payload["provider_id"], r"^Provider [0-9A-F]{64}$")
        self.assertNotEqual(item.payload["instrument_id"], item.payload["provider_id"])

    def test_relative_timing_preserves_causal_and_ingest_lags(self):
        source = dataset(
            event(
                "one",
                event_time="2024-03-12T09:59:00Z",
                available_at="2024-03-12T10:00:00Z",
                ingested_at="2024-03-12T10:00:02Z",
            ),
            event(
                "two",
                instrument="ETH-USD",
                provider="Coinbase",
                event_time="2024-03-13T09:59:30Z",
                available_at="2024-03-13T10:00:00Z",
                ingested_at="2024-03-13T10:00:05Z",
                sequence=2,
            ),
        )
        result = self.blind(source)
        first, second = result.events
        self.assertEqual(first.event_time_us, 0)
        self.assertEqual(first.available_at_us, 60_000_000)
        self.assertEqual(first.ingested_at_us, 62_000_000)
        self.assertEqual(second.session_index, 2)
        self.assertEqual(second.moment_index, 2)
        self.assertGreater(second.available_at_us, first.available_at_us)

    def test_same_available_time_shares_moment_index_and_causal_order(self):
        source = dataset(
            event("a", instrument="AAA", provider="P1", sequence=1),
            event("b", instrument="BBB", provider="P2", sequence=2),
        )
        result = self.blind(source)
        self.assertEqual([item.moment_index for item in result.events], [1, 1])
        self.assertEqual([item.event_id for item in result.events], ["Event 000001", "Event 000002"])

    def test_training_cutoff_uncertainty_is_required_and_forward_evidence_remains_required(self):
        source = dataset(event("one"))
        with self.assertRaisesRegex(BlindingError, "training_cutoff_uncertainty"):
            blind_dataset(
                dataset=source,
                experiment_id="x",
                shuffle_key_sha256=SHUFFLE,
                profile=profile(),
                training_cutoff_uncertainty=" ",
            )
        result = self.blind(source)
        self.assertTrue(result.forward_evidence_required)
        self.assertIn("unknown", result.training_cutoff_uncertainty)

    def test_source_dataset_tampering_after_construction_fails_closed(self):
        source = dataset(event("one"))
        object.__setattr__(source.events[0], "payload", {"instrument_id": "EVIL", "provider_id": "P", "price": "999"})
        with self.assertRaisesRegex(TypeError, "must remain an exact frozen mapping"):
            self.blind(source)

    def test_mutating_original_input_after_blinding_does_not_change_artifact(self):
        original = event("one")
        source = dataset(original)
        result = self.blind(source)
        digest = result.blinded_dataset_sha256
        object.__setattr__(original, "payload", {"instrument_id": "EVIL", "provider_id": "EVIL"})
        self.assertEqual(result.blinded_dataset_sha256, digest)
        self.assertEqual(result.events[0].payload["price"], "71234.50")

    def test_rejects_raw_identity_in_event_kind(self):
        source = dataset(event("one", kind="BTC-USD_TRADE"))
        with self.assertRaisesRegex(BlindingError, "event kind"):
            self.blind(source)

    def test_rejects_absolute_date_in_event_kind(self):
        source = dataset(event("one", kind="TRADE_2024-03-12"))
        with self.assertRaisesRegex(BlindingError, "event kind"):
            self.blind(source)

    def test_privileged_artifact_retains_only_shuffle_seed_commitment(self):
        result = self.blind(dataset(event("one")))
        self.assertFalse(hasattr(result, "shuffle_key_sha256"))
        self.assertRegex(
            result.shuffle_key_commitment_sha256,
            r"^sha256:[0-9a-f]{64}$",
        )
        self.assertNotEqual(result.shuffle_key_commitment_sha256, SHUFFLE)

    def test_profile_and_dataset_digests_are_exact_and_stable(self):
        source = dataset(event("one"))
        p = profile()
        result = self.blind(source, profile=p)
        self.assertRegex(p.digest, r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(result.mapping_sha256, r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(result.blinded_dataset_sha256, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(result.schema_version, 1)

    def test_empty_dataset_is_not_a_meaningful_blinded_replay(self):
        source = CausalDataset.create(manifest_sha256=MANIFEST, events=[])
        with self.assertRaisesRegex(BlindingError, "requires at least one"):
            self.blind(source)

    def test_invalid_shuffle_digest_is_rejected_before_mapping(self):
        with self.assertRaisesRegex(BlindingError, "shuffle_key_sha256"):
            blind_dataset(
                dataset=dataset(event("one")),
                experiment_id="x",
                shuffle_key_sha256="not-a-digest",
                profile=profile(),
                training_cutoff_uncertainty="unknown",
            )

    def test_causal_blinded_view_starts_empty_before_first_availability(self):
        source = dataset(
            event(
                "one",
                event_time="2024-03-12T09:59:00Z",
                available_at="2024-03-12T10:00:00Z",
            )
        )
        feeder = BlindedCausalFeeder(
            dataset=source,
            start_time="2024-03-12T09:58:00Z",
            experiment_id="causal-prefix",
            shuffle_key_sha256=SHUFFLE,
            profile=profile(),
            training_cutoff_uncertainty="unknown",
        )
        self.assertEqual(feeder.published_count, 0)
        self.assertEqual(feeder.view().events, ())
        self.assertLess(feeder.view().simulation_time_us, 0)

    def test_late_correction_cannot_retroactively_move_feeder_anchor(self):
        source = dataset(
            event(
                "first",
                event_time="2024-03-12T10:00:00Z",
                available_at="2024-03-12T10:00:00Z",
                sequence=1,
            ),
            event(
                "late-correction",
                instrument="ETH-USD",
                provider="Coinbase",
                event_time="2024-03-12T09:00:00Z",
                available_at="2024-03-12T11:00:00Z",
                sequence=2,
            ),
        )
        feeder = BlindedCausalFeeder(
            dataset=source,
            start_time="2024-03-12T10:00:00Z",
            experiment_id="late-correction-anchor",
            shuffle_key_sha256=SHUFFLE,
            profile=profile(),
            training_cutoff_uncertainty="unknown",
        )

        self.assertEqual(feeder.simulation_time_us, 0)
        first = feeder.view().events[0]
        self.assertEqual(first.event_time_us, 0)
        correction = feeder.advance_to("2024-03-12T11:00:00Z")[0]
        self.assertLess(correction.event_time_us, 0)
        self.assertEqual(correction.available_at_us, 3_600_000_000)

    def test_causal_blinded_feeder_never_publishes_future_event(self):
        source = dataset(
            event("one", available_at="2024-03-12T10:00:00Z", sequence=1),
            event(
                "two",
                instrument="ETH-USD",
                provider="Coinbase",
                available_at="2024-03-12T10:02:00Z",
                sequence=2,
            ),
        )
        feeder = BlindedCausalFeeder(
            dataset=source,
            start_time="2024-03-12T09:59:00Z",
            experiment_id="causal-prefix",
            shuffle_key_sha256=SHUFFLE,
            profile=profile(),
            training_cutoff_uncertainty="unknown",
        )
        self.assertEqual(feeder.advance_to("2024-03-12T09:59:59Z"), ())
        first = feeder.advance_to("2024-03-12T10:00:00Z")
        self.assertEqual([item.event_id for item in first], ["Event 000001"])
        self.assertEqual([item.event_id for item in feeder.view().events], ["Event 000001"])
        self.assertEqual(feeder.advance_to("2024-03-12T10:01:59Z"), ())
        self.assertEqual(feeder.published_count, 1)
        second = feeder.advance_to("2024-03-12T10:02:00Z")
        self.assertEqual([item.event_id for item in second], ["Event 000002"])

    def test_causal_blinded_advance_next_time_preserves_same_time_batch(self):
        source = dataset(
            event("a", instrument="AAA", provider="P1", sequence=1),
            event("b", instrument="BBB", provider="P2", sequence=2),
            event(
                "c",
                instrument="CCC",
                provider="P3",
                available_at="2024-03-12T10:03:00Z",
                sequence=3,
            ),
        )
        feeder = BlindedCausalFeeder(
            dataset=source,
            start_time="2024-03-12T09:00:00Z",
            experiment_id="same-time",
            shuffle_key_sha256=SHUFFLE,
            profile=profile(),
            training_cutoff_uncertainty="unknown",
        )
        first = feeder.advance_next_time()
        self.assertEqual(len(first), 2)
        self.assertEqual([item.moment_index for item in first], [1, 1])
        self.assertEqual(feeder.published_count, 2)
        self.assertEqual(len(feeder.advance_next_time()), 1)
        self.assertEqual(feeder.published_count, 3)
        self.assertEqual(feeder.advance_next_time(), ())

    def test_blinded_input_evidence_binds_exact_causal_prefix(self):
        source = dataset(
            event("one"),
            event(
                "two",
                instrument="ETH-USD",
                provider="Coinbase",
                available_at="2024-03-12T10:01:00Z",
                sequence=2,
            ),
        )
        feeder = BlindedCausalFeeder(
            dataset=source,
            start_time="2024-03-12T09:00:00Z",
            experiment_id="evidence",
            shuffle_key_sha256=SHUFFLE,
            profile=profile(),
            training_cutoff_uncertainty="unknown",
        )
        before = feeder.input_evidence()
        feeder.advance_next_time()
        after = feeder.input_evidence()
        self.assertNotEqual(before.published_prefix_sha256, after.published_prefix_sha256)
        self.assertNotEqual(before.digest, after.digest)
        self.assertFalse(hasattr(after, "blinded_dataset_sha256"))

    def test_blinded_checkpoint_contains_no_absolute_calendar_or_raw_identity(self):
        source = dataset(
            event(
                "btc-2024-03-12-kraken",
                event_time="2024-03-12T09:59:00Z",
                available_at="2024-03-12T10:00:00Z",
            )
        )
        feeder = BlindedCausalFeeder(
            dataset=source,
            start_time="2024-03-12T10:00:00Z",
            experiment_id="checkpoint",
            shuffle_key_sha256=SHUFFLE,
            profile=profile(),
            training_cutoff_uncertainty="unknown",
        )
        record = feeder.checkpoint().to_record()
        serialized = json.dumps(record, sort_keys=True)
        self.assertNotIn("2024-03-12", serialized)
        self.assertNotIn("BTC", serialized)
        self.assertNotIn("Kraken", serialized)
        self.assertEqual(record["cursor"], 1)

    def test_blinded_checkpoint_resume_matches_uninterrupted_prefix_and_tail(self):
        source = dataset(
            event("one", sequence=1),
            event(
                "two",
                instrument="ETH-USD",
                provider="Coinbase",
                available_at="2024-03-12T10:01:00Z",
                sequence=2,
            ),
            event(
                "three",
                instrument="SOL-USD",
                provider="ProviderX",
                available_at="2024-03-12T10:02:00Z",
                sequence=3,
            ),
        )
        kwargs = dict(
            dataset=source,
            experiment_id="resume",
            shuffle_key_sha256=SHUFFLE,
            profile=profile(),
            training_cutoff_uncertainty="unknown",
        )
        live = BlindedCausalFeeder(start_time="2024-03-12T09:00:00Z", **kwargs)
        live.advance_to("2024-03-12T10:01:00Z")
        checkpoint = live.checkpoint()
        prefix = live.view()
        uninterrupted_tail = live.advance_to("2024-03-12T10:03:00Z")
        uninterrupted_final = live.view()

        resumed = BlindedCausalFeeder.restore(checkpoint=checkpoint, **kwargs)
        self.assertEqual(resumed.view(), prefix)
        resumed_tail = resumed.advance_to("2024-03-12T10:03:00Z")
        self.assertEqual(resumed_tail, uninterrupted_tail)
        self.assertEqual(resumed.view(), uninterrupted_final)
        self.assertEqual(resumed.checkpoint(), live.checkpoint())

    def test_blinded_restore_rejects_wrong_dataset_experiment_profile_and_prefix(self):
        source = dataset(event("one"))
        kwargs = dict(
            dataset=source,
            experiment_id="resume",
            shuffle_key_sha256=SHUFFLE,
            profile=profile(),
            training_cutoff_uncertainty="unknown",
        )
        live = BlindedCausalFeeder(start_time="2024-03-12T10:00:00Z", **kwargs)
        checkpoint = live.checkpoint()

        wrong_experiment = BlindedFeederCheckpoint.from_record(
            {**checkpoint.to_record(), "experiment_id": "other"}
        )
        with self.assertRaisesRegex(BlindingError, "experiment"):
            BlindedCausalFeeder.restore(checkpoint=wrong_experiment, **kwargs)

        wrong_prefix = BlindedFeederCheckpoint.from_record(
            {
                **checkpoint.to_record(),
                "published_prefix_sha256": "sha256:" + ("f" * 64),
            }
        )
        with self.assertRaisesRegex(BlindingError, "prefix digest"):
            BlindedCausalFeeder.restore(checkpoint=wrong_prefix, **kwargs)

        wrong_profile = BlindingProfile(
            identity_fields=(
                IdentityField(("instrument_id",), "COMPANY", required=True),
                IdentityField(("provider_id",), "PROVIDER", required=True),
            )
        )
        with self.assertRaisesRegex(BlindingError, "blinded dataset|profile"):
            BlindedCausalFeeder.restore(
                dataset=source,
                checkpoint=checkpoint,
                experiment_id="resume",
                shuffle_key_sha256=SHUFFLE,
                profile=wrong_profile,
                training_cutoff_uncertainty="unknown",
            )

        other_source = dataset(event("other", instrument="ETH-USD", provider="Coinbase"))
        with self.assertRaisesRegex(BlindingError, "source dataset|blinded dataset"):
            BlindedCausalFeeder.restore(
                dataset=other_source,
                checkpoint=checkpoint,
                experiment_id="resume",
                shuffle_key_sha256=SHUFFLE,
                profile=profile(),
                training_cutoff_uncertainty="unknown",
            )

    def test_blinded_checkpoint_record_rejects_missing_or_extra_keys(self):
        source = dataset(event("one"))
        feeder = BlindedCausalFeeder(
            dataset=source,
            start_time="2024-03-12T10:00:00Z",
            experiment_id="record",
            shuffle_key_sha256=SHUFFLE,
            profile=profile(),
            training_cutoff_uncertainty="unknown",
        )
        record = feeder.checkpoint().to_record()
        missing = dict(record)
        missing.pop("cursor")
        with self.assertRaisesRegex(BlindingError, "keys mismatch"):
            BlindedFeederCheckpoint.from_record(missing)
        with self.assertRaisesRegex(BlindingError, "keys mismatch"):
            BlindedFeederCheckpoint.from_record({**record, "extra": 1})

    def test_unregistered_identity_namespace_cannot_encode_hidden_name(self):
        with self.assertRaisesRegex(BlindingError, "registered neutral"):
            IdentityField(("instrument_id",), "BITCOIN", required=True)

    def test_identity_and_calendar_prefix_paths_are_rejected(self):
        with self.assertRaisesRegex(BlindingError, "unique and non-overlapping"):
            BlindingProfile(
                identity_fields=(IdentityField(("meta",), "ENTITY"),),
                calendar_fields=(CalendarField(("meta", "published_at")),),
            )

    def test_rejects_natural_language_absolute_calendar_cue(self):
        source = dataset(
            event(
                "one",
                payload_extra={"description": "Released March 12, 2024 after close"},
            )
        )
        with self.assertRaisesRegex(BlindingError, "undeclared absolute date"):
            self.blind(source)

    def test_rejects_numeric_absolute_calendar_cue(self):
        source = dataset(
            event(
                "one",
                payload_extra={"description": "Released 03/12/2024 after close"},
            )
        )
        with self.assertRaisesRegex(BlindingError, "undeclared absolute date"):
            self.blind(source)

    def test_strategy_view_exposes_no_privileged_blinding_metadata(self):
        source = dataset(event("btc-2024-03-12-kraken"))
        feeder = BlindedCausalFeeder(
            dataset=source,
            start_time="2024-03-12T10:00:00Z",
            experiment_id="strategy-boundary",
            shuffle_key_sha256=SHUFFLE,
            profile=profile(),
            training_cutoff_uncertainty="unknown",
        )
        view = feeder.view()
        for name in (
            "source_dataset_sha256",
            "experiment_id",
            "profile_sha256",
            "mapping_sha256",
            "shuffle_key_sha256",
            "shuffle_key_commitment_sha256",
            "training_cutoff_uncertainty",
        ):
            self.assertFalse(hasattr(view, name), name)
        self.assertRegex(view.events[0].payload["instrument_id"], r"^Instrument [0-9A-F]{64}$")
        self.assertRegex(view.events[0].payload["provider_id"], r"^Provider [0-9A-F]{64}$")

    def test_visible_prefix_is_invariant_to_unseen_future_identity_and_calendar(self):
        base = dataset(
            event(
                "visible",
                instrument="BTC-USD",
                provider="Kraken",
                available_at="2024-03-12T10:00:00Z",
            )
        )
        extended = dataset(
            event(
                "visible",
                instrument="BTC-USD",
                provider="Kraken",
                available_at="2024-03-12T10:00:00Z",
            ),
            event(
                "future",
                instrument="FUTURE-9",
                provider="FutureProvider",
                event_time="2024-03-01T10:00:00Z",
                available_at="2024-03-13T10:00:00Z",
                sequence=2,
                payload_extra={"expiry": "2020-01-01"},
            ),
        )
        p = profile(calendars=(CalendarField(("expiry",)),))
        first = self.blind(base, experiment_id="prefix-invariance", profile=p)
        second = self.blind(
            extended, experiment_id="prefix-invariance", profile=p
        )
        self.assertEqual(first.events[0], second.events[0])
        self.assertEqual(
            first.events[0].payload["instrument_id"],
            second.events[0].payload["instrument_id"],
        )
        self.assertEqual(first.events[0].event_time_us, 0)
        self.assertEqual(second.events[0].event_time_us, 0)
        self.assertLess(second.events[1].event_time_us, 0)

    def test_strategy_prefix_evidence_is_invariant_to_unseen_future(self):
        base = dataset(
            event(
                "visible",
                instrument="BTC-USD",
                provider="Kraken",
                available_at="2024-03-12T10:00:00Z",
            )
        )
        extended = dataset(
            event(
                "visible",
                instrument="BTC-USD",
                provider="Kraken",
                available_at="2024-03-12T10:00:00Z",
            ),
            event(
                "future-correction",
                instrument="FUTURE-9",
                provider="FutureProvider",
                event_time="2024-03-01T10:00:00Z",
                available_at="2024-03-13T10:00:00Z",
                sequence=2,
            ),
        )
        common = dict(
            start_time="2024-03-12T10:00:00Z",
            experiment_id="prefix-evidence",
            shuffle_key_sha256=SHUFFLE,
            profile=profile(),
            training_cutoff_uncertainty="unknown",
        )
        first = BlindedCausalFeeder(dataset=base, **common)
        second = BlindedCausalFeeder(dataset=extended, **common)
        self.assertEqual(first.view(), second.view())
        self.assertEqual(first.input_evidence(), second.input_evidence())
        self.assertNotEqual(
            first.blinded_dataset_sha256,
            second.blinded_dataset_sha256,
        )

    def test_string_subclass_cannot_bypass_payload_identity_leak_scan(self):
        callbacks = []

        class HiddenIdentity(str):
            def strip(self, *args, **kwargs):
                callbacks.append("strip")
                return super().strip(*args, **kwargs)

            def casefold(self):
                callbacks.append("casefold")
                return super().casefold()

        source = dataset(
            event(
                "one",
                payload_extra={
                    "description": HiddenIdentity(
                        "BTC-USD hidden raw identity in an unbound field"
                    )
                },
            )
        )

        with self.assertRaisesRegex(TypeError, "exact JSON-like blinded value types"):
            self.blind(source)
        self.assertEqual(callbacks, [])

    def test_price_scale_mode_subclass_is_rejected_before_virtual_comparison(self):
        callbacks = []

        class HostileMode(str):
            def strip(self, *args, **kwargs):
                callbacks.append("strip")
                return super().strip(*args, **kwargs)

            def __eq__(self, other):
                callbacks.append("eq")
                return super().__eq__(other)

            def __ne__(self, other):
                callbacks.append("ne")
                return super().__ne__(other)

        with self.assertRaisesRegex(BlindingError, "price_scale_mode"):
            BlindingProfile(
                identity_fields=(
                    IdentityField(("instrument_id",), "INSTRUMENT"),
                ),
                price_scale_mode=HostileMode("IDENTITY"),
            )
        self.assertEqual(callbacks, [])

    def test_tampered_source_graph_is_rejected_before_callbacks(self):
        callbacks = []
        source = dataset(event("one"))

        class HostilePayload(dict):
            def items(self):
                callbacks.append("payload-items")
                return super().items()

            def __iter__(self):
                callbacks.append("payload-iter")
                return super().__iter__()

        object.__setattr__(
            source.events[0],
            "payload",
            HostilePayload(dict(source.events[0].payload)),
        )
        with self.assertRaisesRegex(TypeError, "payload must remain an exact frozen mapping"):
            self.blind(source)
        self.assertEqual(callbacks, [])

    def test_tampered_dataset_events_are_rejected_before_iteration(self):
        callbacks = []
        source = dataset(event("one"))

        class HostileEvents(tuple):
            def __iter__(self):
                callbacks.append("events-iter")
                return super().__iter__()

        object.__setattr__(source, "events", HostileEvents(source.events))
        with self.assertRaisesRegex(TypeError, "events must remain an exact tuple"):
            self.blind(source)
        self.assertEqual(callbacks, [])

    def test_tampered_profile_collections_are_rejected_before_iteration(self):
        callbacks = []
        selected = profile()

        class HostileFields(tuple):
            def __iter__(self):
                callbacks.append("profile-iter")
                return super().__iter__()

        object.__setattr__(
            selected,
            "identity_fields",
            HostileFields(selected.identity_fields),
        )
        with self.assertRaisesRegex(TypeError, "identity_fields must remain an exact tuple"):
            self.blind(dataset(event("one")), profile=selected)
        self.assertEqual(callbacks, [])

    def test_hostile_dataset_and_profile_subclasses_are_rejected(self):
        class HostileDataset(CausalDataset):
            pass
        class HostileProfile(BlindingProfile):
            pass
        with self.assertRaisesRegex(TypeError, "exact CausalDataset"):
            blind_dataset(
                dataset=object.__new__(HostileDataset),
                experiment_id="x",
                shuffle_key_sha256=SHUFFLE,
                profile=profile(),
                training_cutoff_uncertainty="unknown",
            )
        with self.assertRaisesRegex(TypeError, "exact BlindingProfile"):
            blind_dataset(
                dataset=dataset(event("one")),
                experiment_id="x",
                shuffle_key_sha256=SHUFFLE,
                profile=object.__new__(HostileProfile),
                training_cutoff_uncertainty="unknown",
            )


if __name__ == "__main__":
    unittest.main()
