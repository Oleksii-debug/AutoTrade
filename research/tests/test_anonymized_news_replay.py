from datetime import datetime, timezone, tzinfo
import unittest

from autotrade_research.evaluation.replay.blinding import BlindedCausalFeeder
from autotrade_research.evaluation.replay.news import (
    NewsClaim,
    NewsIdentity,
    NewsReplayBundle,
    NewsReplayError,
    NewsRevision,
    build_news_replay_bundle,
    render_blinded_news,
)


MANIFEST = "sha256:" + ("1" * 64)
SHUFFLE = "sha256:" + ("a" * 64)
CONTENT = "sha256:" + ("2" * 64)
TRUST = "sha256:" + ("3" * 64)
EVIDENCE = "sha256:" + ("4" * 64)
SYNDICATION = "sha256:" + ("5" * 64)


def claim(
    claim_id="earnings",
    *,
    subject_slot="company",
    predicate="EARNINGS_SURPRISE",
    magnitude="17",
    unit="PERCENT",
    direction="POSITIVE",
    relevance_bps=8600,
    qualifier_template=None,
):
    return NewsClaim(
        claim_id=claim_id,
        subject_slot=subject_slot,
        predicate=predicate,
        magnitude=magnitude,
        unit=unit,
        direction=direction,
        relevance_bps=relevance_bps,
        qualifier_template=qualifier_template,
    )


def revision(
    *,
    information_id="story-1",
    revision_number=1,
    revision_kind="ORIGINAL",
    supersedes_revision=None,
    published_at="2024-03-12T10:00:00Z",
    available_at="2024-03-12T10:00:05Z",
    ingested_at="2024-03-12T10:00:07Z",
    source_event_at="2024-03-12T09:45:00Z",
    source_sequence=1,
    source_priority=10,
    source_id="Reuters",
    syndication_sha256=SYNDICATION,
    content_sha256=CONTENT,
    language="en",
    identities=None,
    summary_template="{company} reported quarterly earnings 17% above expectations",
    claims=None,
    contamination_note=(
        "Distinctive numerical phrasing may still reveal historical context; "
        "forward evidence remains required."
    ),
):
    if identities is None:
        identities = (
            NewsIdentity("company", "COMPANY", "Acme Corp"),
        )
    if claims is None:
        claims = (claim(),)
    return NewsRevision(
        information_id=information_id,
        revision=revision_number,
        revision_kind=revision_kind,
        supersedes_revision=supersedes_revision,
        source_id=source_id,
        published_at=published_at,
        available_at=available_at,
        ingested_at=ingested_at,
        source_event_at=source_event_at,
        source_priority=source_priority,
        source_sequence=source_sequence,
        language=language,
        rights_id="rights-news-test",
        content_sha256=content_sha256,
        trust_features_sha256=TRUST,
        syndication_sha256=syndication_sha256,
        evidence_sha256=(EVIDENCE,),
        identities=identities,
        summary_template=summary_template,
        claims=claims,
        residual_contamination_note=contamination_note,
    )


class Section18AnonymizedNewsReplayTests(unittest.TestCase):
    def bundle(self, *records):
        return build_news_replay_bundle(
            manifest_sha256=MANIFEST,
            records=tuple(records),
        )

    def feeder(self, bundle, *, start_time="2024-03-12T09:40:00Z"):
        return BlindedCausalFeeder(
            dataset=bundle.dataset,
            start_time=start_time,
            experiment_id="section18-exp-a",
            shuffle_key_sha256=SHUFFLE,
            profile=bundle.blinding_profile,
            training_cutoff_uncertainty=(
                "pretraining coverage of the historical news period is unknown"
            ),
        )

    def test_masks_news_identity_and_renders_from_blinded_slots_only(self):
        bundle = self.bundle(revision())
        feeder = self.feeder(bundle)
        published = feeder.advance_to("2024-03-12T10:00:05Z")
        self.assertEqual(len(published), 1)
        item = published[0]
        rendered = render_blinded_news(item)

        company_ref = item.payload["identity_slots"]["company"]
        source_ref = item.payload["source_id"]
        self.assertRegex(company_ref, r"^Company [0-9A-F]{64}$")
        self.assertRegex(source_ref, r"^Source [0-9A-F]{64}$")
        self.assertEqual(
            rendered,
            f"{company_ref} reported quarterly earnings 17% above expectations",
        )
        self.assertNotIn("Acme", rendered)
        self.assertNotIn("Reuters", rendered)
        self.assertNotIn("2024", rendered)
        self.assertNotIn("March", rendered)

    def test_story_reference_links_corrections_without_exposing_information_id(self):
        original_a = revision(information_id="story-a", source_sequence=1)
        original_b = revision(
            information_id="story-b",
            source_sequence=2,
            syndication_sha256="sha256:" + ("8" * 64),
            content_sha256="sha256:" + ("8" * 64),
            published_at="2024-03-12T10:01:00Z",
            available_at="2024-03-12T10:01:05Z",
            ingested_at="2024-03-12T10:01:07Z",
        )
        correction_a = revision(
            information_id="story-a",
            revision_number=2,
            revision_kind="CORRECTION",
            supersedes_revision=1,
            published_at="2024-03-12T10:20:00Z",
            available_at="2024-03-12T10:30:00Z",
            ingested_at="2024-03-12T10:30:01Z",
            source_sequence=3,
            summary_template="{company} corrected the reported earnings surprise to 11%",
            claims=(
                claim(
                    claim_id="earnings-corrected",
                    magnitude="11",
                    relevance_bps=9000,
                ),
            ),
        )
        feeder = self.feeder(self.bundle(original_a, original_b, correction_a))
        feeder.advance_to("2024-03-12T10:30:00Z")
        events = feeder.view().events
        story_a_original = next(
            item for item in events if item.payload["revision"] == 1
            and item.payload["story_ref"] == events[-1].payload["story_ref"]
        )
        story_a_correction = events[-1]
        other_story = next(
            item for item in events
            if item.payload["story_ref"] != story_a_correction.payload["story_ref"]
        )

        self.assertRegex(
            story_a_correction.payload["story_ref"],
            r"^Entity [0-9A-F]{64}$",
        )
        self.assertEqual(
            story_a_original.payload["story_ref"],
            story_a_correction.payload["story_ref"],
        )
        self.assertNotEqual(
            other_story.payload["story_ref"],
            story_a_correction.payload["story_ref"],
        )
        visible = repr(tuple(item.payload for item in events))
        self.assertNotIn("story-a", visible)
        self.assertNotIn("story-b", visible)

    def test_preserves_publication_availability_and_ingest_lags(self):
        bundle = self.bundle(revision())
        feeder = self.feeder(bundle)
        (item,) = feeder.advance_to("2024-03-12T10:00:07Z")

        self.assertEqual(item.available_at_us - item.event_time_us, 5_000_000)
        self.assertEqual(item.ingested_at_us - item.available_at_us, 2_000_000)
        self.assertRegex(item.payload["published_at"], r"^T\+\d+us$")
        # The source event preceded the first visible publication by 15 minutes.
        self.assertEqual(item.payload["source_event_at"], "T-900000000us")

    def test_preserves_structured_magnitude_direction_and_relevance(self):
        bundle = self.bundle(revision())
        feeder = self.feeder(bundle)
        (item,) = feeder.advance_to("2024-03-12T10:00:05Z")
        structured = item.payload["claims"][0]

        self.assertEqual(structured["predicate"], "EARNINGS_SURPRISE")
        self.assertEqual(structured["magnitude"], "17")
        self.assertEqual(structured["unit"], "PERCENT")
        self.assertEqual(structured["direction"], "POSITIVE")
        self.assertEqual(structured["relevance_bps"], 8600)
        self.assertEqual(structured["subject_slot"], "company")

    def test_correction_is_not_visible_before_historical_availability(self):
        original = revision()
        correction = revision(
            revision_number=2,
            revision_kind="CORRECTION",
            supersedes_revision=1,
            published_at="2024-03-12T10:25:00Z",
            available_at="2024-03-12T10:30:00Z",
            ingested_at="2024-03-12T10:30:01Z",
            source_event_at="2024-03-12T09:45:00Z",
            source_sequence=2,
            summary_template="{company} corrected the reported earnings surprise to 11%",
            claims=(
                claim(
                    claim_id="earnings-corrected",
                    magnitude="11",
                    relevance_bps=9000,
                ),
            ),
        )
        bundle = self.bundle(original, correction)
        feeder = self.feeder(bundle)

        first = feeder.advance_to("2024-03-12T10:29:59Z")
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].payload["revision"], 1)
        self.assertEqual(feeder.view().events[-1].payload["revision"], 1)

        second = feeder.advance_to("2024-03-12T10:30:00Z")
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0].payload["revision"], 2)
        self.assertEqual(second[0].payload["supersedes_revision"], 1)
        self.assertEqual(
            [item.payload["revision"] for item in feeder.view().events],
            [1, 2],
        )

    def test_same_availability_revision_cannot_sort_before_predecessor(self):
        original = revision(
            published_at="2024-03-12T10:00:00Z",
            available_at="2024-03-12T10:30:00Z",
            ingested_at="2024-03-12T10:30:00Z",
            source_priority=10,
            source_sequence=2,
        )
        correction = revision(
            revision_number=2,
            revision_kind="CORRECTION",
            supersedes_revision=1,
            published_at="2024-03-12T10:20:00Z",
            available_at="2024-03-12T10:30:00Z",
            ingested_at="2024-03-12T10:30:01Z",
            source_priority=5,
            source_sequence=1,
            summary_template="{company} corrected the reported earnings surprise to 11%",
            claims=(
                claim(
                    claim_id="earnings-corrected",
                    magnitude="11",
                    relevance_bps=9000,
                ),
            ),
        )
        with self.assertRaisesRegex(
            NewsReplayError, "would expose a later revision before its predecessor"
        ):
            self.bundle(original, correction)

    def test_same_availability_revision_preserves_revision_order(self):
        original = revision(
            published_at="2024-03-12T10:00:00Z",
            available_at="2024-03-12T10:30:00Z",
            ingested_at="2024-03-12T10:30:00Z",
            source_priority=10,
            source_sequence=1,
        )
        correction = revision(
            revision_number=2,
            revision_kind="CORRECTION",
            supersedes_revision=1,
            published_at="2024-03-12T10:20:00Z",
            available_at="2024-03-12T10:30:00Z",
            ingested_at="2024-03-12T10:30:01Z",
            source_priority=10,
            source_sequence=2,
            summary_template="{company} corrected the reported earnings surprise to 11%",
            claims=(
                claim(
                    claim_id="earnings-corrected",
                    magnitude="11",
                    relevance_bps=9000,
                ),
            ),
        )
        bundle = self.bundle(original, correction)
        feeder = self.feeder(bundle)
        published = feeder.advance_to("2024-03-12T10:30:00Z")
        self.assertEqual(
            [item.payload["revision"] for item in published],
            [1, 2],
        )

    def test_checkpoint_before_correction_restores_without_future_leak(self):
        original = revision()
        correction = revision(
            revision_number=2,
            revision_kind="UPDATE",
            supersedes_revision=1,
            published_at="2024-03-12T10:20:00Z",
            available_at="2024-03-12T10:30:00Z",
            ingested_at="2024-03-12T10:30:02Z",
            source_sequence=2,
            summary_template="{company} updated guidance after the initial release",
            claims=(
                claim(
                    claim_id="guidance",
                    predicate="GUIDANCE_UPDATE",
                    magnitude=None,
                    unit=None,
                    direction="MIXED",
                    relevance_bps=7500,
                ),
            ),
        )
        bundle = self.bundle(original, correction)
        feeder = self.feeder(bundle)
        feeder.advance_to("2024-03-12T10:10:00Z")
        checkpoint = feeder.checkpoint()

        restored = BlindedCausalFeeder.restore(
            dataset=bundle.dataset,
            checkpoint=checkpoint,
            experiment_id="section18-exp-a",
            shuffle_key_sha256=SHUFFLE,
            profile=bundle.blinding_profile,
            training_cutoff_uncertainty=(
                "pretraining coverage of the historical news period is unknown"
            ),
        )
        self.assertEqual(
            [item.payload["revision"] for item in restored.view().events],
            [1],
        )
        self.assertEqual(
            [item.payload["revision"] for item in restored.advance_to("2024-03-12T10:30:00Z")],
            [2],
        )

    def test_political_and_geopolitical_identities_use_same_fail_closed_path(self):
        record = revision(
            information_id="geo-1",
            identities=(
                NewsIdentity("country", "COUNTRY", "Freedonia"),
                NewsIdentity("leader", "POLITICIAN", "Alice Example"),
            ),
            summary_template=(
                "{leader} announced export restrictions affecting {country}"
            ),
            claims=(
                claim(
                    claim_id="export-restriction",
                    subject_slot="country",
                    predicate="EXPORT_RESTRICTION",
                    magnitude=None,
                    unit=None,
                    direction="NEGATIVE",
                    relevance_bps=9300,
                ),
            ),
        )
        bundle = self.bundle(record)
        feeder = self.feeder(bundle)
        (item,) = feeder.advance_to("2024-03-12T10:00:05Z")
        rendered = render_blinded_news(item)

        self.assertRegex(rendered, r"Person [0-9A-F]{64}")
        self.assertRegex(rendered, r"Country [0-9A-F]{64}")
        self.assertNotIn("Freedonia", rendered)
        self.assertNotIn("Alice Example", rendered)

    def test_declared_alias_cannot_leak_through_template_or_structured_claim(self):
        identity = NewsIdentity(
            "company",
            "COMPANY",
            "Acme Corp",
            ("ACME",),
        )
        with self.assertRaisesRegex(NewsReplayError, "raw identity"):
            revision(
                identities=(identity,),
                summary_template="ACME reported quarterly earnings above expectations",
            )
        with self.assertRaisesRegex(NewsReplayError, "predicate exposes"):
            revision(
                identities=(identity,),
                claims=(claim(predicate="ACME_WARNING"),),
            )

    def test_identity_slot_name_cannot_expose_declared_alias(self):
        with self.assertRaisesRegex(NewsReplayError, "identity slot acme exposes"):
            revision(
                identities=(
                    NewsIdentity("acme", "COMPANY", "Acme Corp", ("ACME",)),
                ),
                summary_template="{acme} reported quarterly earnings above expectations",
                claims=(claim(subject_slot="acme"),),
            )

    def test_later_alias_declaration_rejects_earlier_cross_record_slot_leak(self):
        earlier = revision(
            information_id="story-a",
            identities=(
                NewsIdentity("acme", "COMPANY", "First Corp"),
            ),
            summary_template="{acme} reported quarterly earnings above expectations",
            claims=(claim(subject_slot="acme"),),
        )
        later = revision(
            information_id="story-b",
            source_id="Independent Source",
            source_sequence=2,
            syndication_sha256="sha256:" + ("8" * 64),
            content_sha256="sha256:" + ("8" * 64),
            published_at="2024-03-12T10:01:00Z",
            available_at="2024-03-12T10:01:05Z",
            ingested_at="2024-03-12T10:01:07Z",
            identities=(
                NewsIdentity("company", "COMPANY", "Other Corp", ("ACME",)),
            ),
            summary_template="{company} reported quarterly earnings above expectations",
        )
        with self.assertRaisesRegex(NewsReplayError, "identity slot acme exposes"):
            self.bundle(earlier, later)

    def test_alias_collision_across_distinct_entities_is_rejected(self):
        first = revision(
            information_id="story-a",
            identities=(
                NewsIdentity("company", "COMPANY", "Acme Corp", ("SHARED",)),
            ),
        )
        second = revision(
            information_id="story-b",
            source_id="Independent Source",
            source_sequence=2,
            syndication_sha256="sha256:" + ("8" * 64),
            content_sha256="sha256:" + ("8" * 64),
            published_at="2024-03-12T10:01:00Z",
            available_at="2024-03-12T10:01:05Z",
            ingested_at="2024-03-12T10:01:07Z",
            identities=(
                NewsIdentity("company", "COMPANY", "Other Corp", ("SHARED",)),
            ),
            summary_template="{company} reported quarterly earnings above expectations",
        )
        with self.assertRaisesRegex(
            NewsReplayError, "multiple canonical entities"
        ):
            self.bundle(first, second)

    def test_later_alias_declaration_rejects_earlier_cross_record_leak(self):
        earlier = revision(
            information_id="story-a",
            summary_template="{company} discussed ACME in neutral-looking prose",
        )
        later = revision(
            information_id="story-b",
            source_id="Independent Source",
            source_sequence=2,
            syndication_sha256="sha256:" + ("8" * 64),
            content_sha256="sha256:" + ("8" * 64),
            published_at="2024-03-12T10:01:00Z",
            available_at="2024-03-12T10:01:05Z",
            ingested_at="2024-03-12T10:01:07Z",
            identities=(
                NewsIdentity("company", "COMPANY", "Other Corp", ("ACME",)),
            ),
            summary_template="{company} reported quarterly earnings above expectations",
        )
        with self.assertRaisesRegex(NewsReplayError, "raw identity"):
            self.bundle(earlier, later)

    def test_raw_identity_in_template_is_rejected(self):
        with self.assertRaisesRegex(NewsReplayError, "raw identity"):
            revision(
                summary_template="Acme Corp reported earnings above expectations"
            )

    def test_source_identity_in_template_is_rejected(self):
        with self.assertRaisesRegex(NewsReplayError, "raw identity"):
            revision(
                summary_template="Reuters reported {company} earnings above expectations"
            )

    def test_absolute_calendar_cues_in_template_are_rejected(self):
        for leaking in (
            "{company} reported earnings on 2024-03-12",
            "{company} reported earnings in 2024",
            "{company} reported earnings in March",
        ):
            with self.subTest(leaking=leaking):
                with self.assertRaisesRegex(NewsReplayError, "calendar"):
                    revision(summary_template=leaking)

    def test_undeclared_template_slot_is_rejected(self):
        with self.assertRaisesRegex(NewsReplayError, "undeclared identity slots"):
            revision(
                summary_template="{company} reacted to {country}",
            )

    def test_claim_subject_must_reference_declared_identity_slot(self):
        with self.assertRaisesRegex(NewsReplayError, "undeclared subject slot"):
            revision(
                claims=(
                    claim(subject_slot="country"),
                )
            )

    def test_revision_identity_slot_cannot_switch_entity_binding(self):
        original = revision()
        correction = revision(
            revision_number=2,
            revision_kind="CORRECTION",
            supersedes_revision=1,
            published_at="2024-03-12T10:20:00Z",
            available_at="2024-03-12T10:30:00Z",
            ingested_at="2024-03-12T10:30:01Z",
            source_sequence=2,
            identities=(
                NewsIdentity("company", "COMPANY", "Other Corp"),
            ),
            summary_template="{company} corrected the reported earnings surprise to 11%",
            claims=(
                claim(
                    claim_id="earnings-corrected",
                    magnitude="11",
                    relevance_bps=9000,
                ),
            ),
        )
        with self.assertRaisesRegex(
            NewsReplayError, "identity slot company changes binding"
        ):
            self.bundle(original, correction)

    def test_revision_may_add_new_identity_slot_without_rebinding_existing_slot(self):
        original = revision()
        update = revision(
            revision_number=2,
            revision_kind="UPDATE",
            supersedes_revision=1,
            published_at="2024-03-12T10:20:00Z",
            available_at="2024-03-12T10:30:00Z",
            ingested_at="2024-03-12T10:30:01Z",
            source_sequence=2,
            identities=(
                NewsIdentity("company", "COMPANY", "Acme Corp"),
                NewsIdentity("country", "COUNTRY", "Freedonia"),
            ),
            summary_template="{company} updated guidance for operations in {country}",
            claims=(
                claim(
                    claim_id="guidance",
                    predicate="GUIDANCE_UPDATE",
                    magnitude=None,
                    unit=None,
                    direction="MIXED",
                    relevance_bps=7500,
                ),
            ),
        )
        bundle = self.bundle(original, update)
        self.assertEqual(len(bundle.dataset.events), 2)

    def test_revision_chain_must_be_contiguous_and_historically_ordered(self):
        with self.assertRaisesRegex(NewsReplayError, "contiguous"):
            self.bundle(
                revision(),
                revision(
                    revision_number=3,
                    revision_kind="CORRECTION",
                    supersedes_revision=1,
                    published_at="2024-03-12T10:30:00Z",
                    available_at="2024-03-12T10:30:00Z",
                    ingested_at="2024-03-12T10:30:00Z",
                    source_sequence=3,
                ),
            )

        with self.assertRaisesRegex(NewsReplayError, "must supersede revision 1"):
            self.bundle(
                revision(),
                revision(
                    revision_number=2,
                    revision_kind="UPDATE",
                    supersedes_revision=None,
                    published_at="2024-03-12T10:30:00Z",
                    available_at="2024-03-12T10:30:00Z",
                    ingested_at="2024-03-12T10:30:00Z",
                    source_sequence=2,
                ),
            )

    def test_optional_source_event_time_can_be_absent(self):
        bundle = self.bundle(revision(source_event_at=None))
        feeder = self.feeder(bundle)
        (item,) = feeder.advance_to("2024-03-12T10:00:05Z")
        self.assertNotIn("source_event_at", item.payload)

    def test_privileged_content_hash_rights_and_evidence_are_not_strategy_payload(self):
        bundle = self.bundle(revision())
        feeder = self.feeder(bundle)
        (item,) = feeder.advance_to("2024-03-12T10:00:05Z")

        for forbidden in (
            "content_sha256",
            "rights_id",
            "evidence_sha256",
            "trust_features_sha256",
            "syndication_sha256",
            "information_id",
        ):
            self.assertNotIn(forbidden, item.payload)

    def test_bundle_records_residual_contamination_and_requires_forward_evidence(self):
        bundle = self.bundle(revision())
        self.assertTrue(bundle.forward_evidence_required)
        self.assertFalse(bundle.perfect_anonymization_claimed)
        self.assertRegex(bundle.contamination_notes_sha256, r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(bundle.digest, r"^sha256:[0-9a-f]{64}$")

    def test_bundle_is_deterministic_under_input_permutation(self):
        a = revision(information_id="story-a", source_sequence=1)
        b = revision(
            information_id="story-b",
            source_sequence=2,
            syndication_sha256="sha256:" + ("8" * 64),
            content_sha256="sha256:" + ("8" * 64),
            published_at="2024-03-12T10:01:00Z",
            available_at="2024-03-12T10:01:05Z",
            ingested_at="2024-03-12T10:01:07Z",
        )
        first = self.bundle(a, b)
        second = self.bundle(b, a)

        self.assertEqual(first.dataset.dataset_sha256, second.dataset.dataset_sha256)
        self.assertEqual(first.source_records_sha256, second.source_records_sha256)
        self.assertEqual(first.contamination_notes_sha256, second.contamination_notes_sha256)
        self.assertEqual(first.digest, second.digest)

    def test_identity_slot_namespace_cannot_change_across_records(self):
        first = revision(
            information_id="story-a",
            identities=(NewsIdentity("subject", "COMPANY", "Acme Corp"),),
            summary_template="{subject} announced results",
            claims=(claim(subject_slot="subject"),),
        )
        second = revision(
            information_id="story-b",
            source_sequence=2,
            published_at="2024-03-12T10:01:00Z",
            available_at="2024-03-12T10:01:05Z",
            ingested_at="2024-03-12T10:01:07Z",
            identities=(NewsIdentity("subject", "COUNTRY", "Freedonia"),),
            summary_template="{subject} announced a policy change",
            claims=(
                claim(
                    subject_slot="subject",
                    predicate="POLICY_CHANGE",
                    magnitude=None,
                    unit=None,
                    direction="MIXED",
                ),
            ),
        )
        with self.assertRaisesRegex(NewsReplayError, "changes namespace"):
            self.bundle(first, second)

    def test_modal_may_is_not_mistaken_for_calendar_month(self):
        record = revision(
            summary_template="{company} may revise guidance after the release"
        )
        bundle = self.bundle(record)
        feeder = self.feeder(bundle)
        (item,) = feeder.advance_to("2024-03-12T10:00:05Z")
        self.assertIn("may revise guidance", render_blinded_news(item))

    def test_revisions_must_retain_source_identity(self):
        with self.assertRaisesRegex(NewsReplayError, "one source identity"):
            self.bundle(
                revision(),
                revision(
                    revision_number=2,
                    revision_kind="CORRECTION",
                    supersedes_revision=1,
                    published_at="2024-03-12T10:30:00Z",
                    available_at="2024-03-12T10:30:00Z",
                    ingested_at="2024-03-12T10:30:01Z",
                    source_sequence=2,
                    source_id="Bloomberg",
                ),
            )

    def test_revisions_must_retain_syndication_identity(self):
        with self.assertRaisesRegex(NewsReplayError, "one syndication identity"):
            self.bundle(
                revision(),
                revision(
                    revision_number=2,
                    revision_kind="UPDATE",
                    supersedes_revision=1,
                    published_at="2024-03-12T10:30:00Z",
                    available_at="2024-03-12T10:30:00Z",
                    ingested_at="2024-03-12T10:30:01Z",
                    source_sequence=2,
                    syndication_sha256="sha256:" + ("6" * 64),
                ),
            )

    def test_syndicated_duplicate_is_not_counted_as_independent_news(self):
        duplicate = revision(
            information_id="story-copy",
            source_sequence=2,
            source_id="Wire Mirror",
            published_at="2024-03-12T10:01:00Z",
            available_at="2024-03-12T10:01:05Z",
            ingested_at="2024-03-12T10:01:07Z",
        )
        with self.assertRaisesRegex(NewsReplayError, "syndicated duplicate"):
            self.bundle(revision(), duplicate)

    def test_distinct_syndication_ids_remain_independent_information(self):
        independent = revision(
            information_id="story-independent",
            source_sequence=2,
            source_id="Independent Source",
            syndication_sha256="sha256:" + ("7" * 64),
            content_sha256="sha256:" + ("7" * 64),
            published_at="2024-03-12T10:01:00Z",
            available_at="2024-03-12T10:01:05Z",
            ingested_at="2024-03-12T10:01:07Z",
        )
        bundle = self.bundle(revision(), independent)
        self.assertEqual(len(bundle.dataset.events), 2)

    def test_unsupported_template_language_fails_closed(self):
        with self.assertRaisesRegex(NewsReplayError, "unsupported template language"):
            revision(language="sk")

    def test_exact_content_duplicate_cannot_bypass_syndication_identity(self):
        duplicate = revision(
            information_id="story-exact-copy",
            source_sequence=2,
            source_id="Independent Label",
            syndication_sha256="sha256:" + ("6" * 64),
            published_at="2024-03-12T10:01:00Z",
            available_at="2024-03-12T10:01:05Z",
            ingested_at="2024-03-12T10:01:07Z",
        )
        with self.assertRaisesRegex(NewsReplayError, "exact-content duplicate"):
            self.bundle(revision(), duplicate)

    def test_bundle_detaches_dataset_and_profile_inputs(self):
        source = self.bundle(revision())
        detached = NewsReplayBundle(
            dataset=source.dataset,
            blinding_profile=source.blinding_profile,
            source_records_sha256=source.source_records_sha256,
            contamination_notes_sha256=source.contamination_notes_sha256,
        )
        before_digest = detached.digest
        before_dataset = detached.dataset.dataset_sha256
        before_profile = detached.blinding_profile.digest

        object.__setattr__(
            source.dataset,
            "manifest_sha256",
            "sha256:" + ("9" * 64),
        )
        object.__setattr__(
            source.blinding_profile,
            "price_scale_mode",
            "TAMPERED",
        )

        self.assertEqual(detached.digest, before_digest)
        self.assertEqual(detached.dataset.dataset_sha256, before_dataset)
        self.assertEqual(detached.blinding_profile.digest, before_profile)

    def test_bundle_rejects_dataset_content_that_no_longer_matches_digest(self):
        source = self.bundle(revision())
        object.__setattr__(
            source.dataset,
            "dataset_sha256",
            "sha256:" + ("9" * 64),
        )
        with self.assertRaisesRegex(NewsReplayError, "committed digest"):
            NewsReplayBundle(
                dataset=source.dataset,
                blinding_profile=source.blinding_profile,
                source_records_sha256=source.source_records_sha256,
                contamination_notes_sha256=source.contamination_notes_sha256,
            )

    def test_datetime_with_hostile_tzinfo_is_rejected_before_callback(self):
        calls = []

        class HostileTimezone(tzinfo):
            def utcoffset(self, _dt):
                calls.append("utcoffset")
                raise AssertionError("hostile timezone callback executed")

            def dst(self, _dt):
                calls.append("dst")
                raise AssertionError("hostile timezone callback executed")

            def tzname(self, _dt):
                calls.append("tzname")
                raise AssertionError("hostile timezone callback executed")

        hostile = datetime(
            2024,
            3,
            12,
            10,
            0,
            0,
            tzinfo=HostileTimezone(),
        )
        with self.assertRaisesRegex(NewsReplayError, "built-in datetime.timezone"):
            revision(published_at=hostile)
        self.assertEqual(calls, [])

    def test_render_rejects_hostile_payload_mapping_before_virtual_dispatch(self):
        bundle = self.bundle(revision())
        feeder = self.feeder(bundle)
        (item,) = feeder.advance_to("2024-03-12T10:00:05Z")
        calls = []

        class HostileDict(dict):
            def get(self, *_args, **_kwargs):
                calls.append("get")
                raise AssertionError("hostile mapping callback executed")

            def items(self):
                calls.append("items")
                raise AssertionError("hostile mapping callback executed")

            def keys(self):
                calls.append("keys")
                raise AssertionError("hostile mapping callback executed")

        hostile_payload = HostileDict(dict(item.payload))
        object.__setattr__(item, "payload", hostile_payload)
        with self.assertRaisesRegex(
            NewsReplayError, "must remain an exact frozen mapping"
        ):
            render_blinded_news(item)
        self.assertEqual(calls, [])

    def test_bundle_rejects_hostile_nested_containers_before_iteration(self):
        calls = []

        class HostileTuple(tuple):
            def __iter__(self):
                calls.append("iter")
                raise AssertionError("hostile tuple callback executed")

        source = self.bundle(revision())
        object.__setattr__(
            source.blinding_profile,
            "identity_fields",
            HostileTuple(source.blinding_profile.identity_fields),
        )
        with self.assertRaisesRegex(TypeError, "identity_fields must remain an exact tuple"):
            NewsReplayBundle(
                dataset=source.dataset,
                blinding_profile=source.blinding_profile,
                source_records_sha256=source.source_records_sha256,
                contamination_notes_sha256=source.contamination_notes_sha256,
            )
        self.assertEqual(calls, [])

        source = self.bundle(revision())
        object.__setattr__(
            source.dataset,
            "events",
            HostileTuple(source.dataset.events),
        )
        with self.assertRaisesRegex(TypeError, "events must remain an exact tuple"):
            NewsReplayBundle(
                dataset=source.dataset,
                blinding_profile=source.blinding_profile,
                source_records_sha256=source.source_records_sha256,
                contamination_notes_sha256=source.contamination_notes_sha256,
            )
        self.assertEqual(calls, [])

    def test_binary_float_and_noncanonical_decimal_are_not_admitted(self):
        with self.assertRaisesRegex(NewsReplayError, "canonical decimal"):
            claim(magnitude="01.0")
        with self.assertRaisesRegex(NewsReplayError, "relevance_bps"):
            claim(relevance_bps=True)

    def test_render_rejects_non_news_event(self):
        bundle = self.bundle(revision())
        feeder = self.feeder(bundle)
        (item,) = feeder.advance_to("2024-03-12T10:00:05Z")
        object.__setattr__(item, "kind", "TRADE")
        with self.assertRaisesRegex(NewsReplayError, "not a NEWS"):
            render_blinded_news(item)


if __name__ == "__main__":
    unittest.main()
