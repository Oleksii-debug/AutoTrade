import re
import unittest

from autotrade_research.evaluation.replay.blinding import (
    BlindedCausalFeeder,
    BlindingProfile,
)
from autotrade_research.evaluation.replay.feeder import CausalDataset, CausalEvent
from autotrade_research.evaluation.replay.news import (
    AnonymizedNewsReplay,
    NewsReplayError,
    anonymize_news_dataset,
)


MANIFEST = "sha256:" + ("2" * 64)
KEY = "sha256:" + ("b" * 64)


def news_event(
    raw_news_id,
    *,
    kind="NEWS",
    event_time="2024-03-12T09:59:00Z",
    published_at="2024-03-12T10:00:00Z",
    ingested_at=None,
    sequence=1,
    canonical_id="ACME",
    display_name="Acme Corp",
    namespace="COMPANY",
    template=None,
    facts=None,
    magnitude=None,
    relevance=("earnings", "equity"),
    corrects_news_id=None,
):
    if template is None:
        template = (
            {"type": "entity", "role": "subject"},
            {"type": "text", "text": " reported quarterly earnings "},
            {"type": "fact", "key": "surprise"},
            {"type": "text", "text": " above expectations"},
        )
    if facts is None:
        facts = {"surprise": "17%"}
    if magnitude is None:
        magnitude = {"earnings_surprise_bp": 1700}
    payload = {
        "news_id": raw_news_id,
        "template": template,
        "entities": {
            "subject": {
                "id": canonical_id,
                "namespace": namespace,
                "aliases": (display_name,),
            }
        },
        "facts": facts,
        "magnitude": magnitude,
        "relevance": relevance,
    }
    if corrects_news_id is not None:
        payload["corrects_news_id"] = corrects_news_id
    return CausalEvent.create(
        event_id=f"raw-event-{raw_news_id}-{sequence}",
        kind=kind,
        event_time=event_time,
        available_at=published_at,
        ingested_at=ingested_at or published_at,
        source_priority=10,
        source_sequence=sequence,
        payload=payload,
    )


def dataset(*events):
    return CausalDataset.create(manifest_sha256=MANIFEST, events=events)


def anonymize(source, *, experiment_id="news-exp", key=KEY):
    return anonymize_news_dataset(
        dataset=source,
        experiment_id=experiment_id,
        shuffle_key_sha256=key,
    )


class Product18AnonymizedNewsReplayTests(unittest.TestCase):
    def test_preserves_economic_meaning_magnitude_relevance_and_timing(self):
        source_event = news_event("news-001")
        result = anonymize(dataset(source_event))
        self.assertIsInstance(result, AnonymizedNewsReplay)
        item = result.dataset.events[0]
        token = item.payload["news_ref"]
        self.assertRegex(token, r"^News [0-9A-F]{64}$")
        self.assertRegex(
            item.payload["statement"],
            r"^Company [0-9A-F]{64} reported quarterly earnings 17% above expectations$",
        )
        self.assertEqual(item.payload["facts"]["surprise"], "17%")
        self.assertEqual(item.payload["magnitude"]["earnings_surprise_bp"], 1700)
        self.assertEqual(item.payload["relevance"], ("earnings", "equity"))
        self.assertEqual(item.event_time, source_event.event_time)
        self.assertEqual(item.available_at, source_event.available_at)
        self.assertEqual(item.ingested_at, source_event.ingested_at)

    def test_raw_names_aliases_and_news_ids_are_not_in_strategy_payload(self):
        result = anonymize(dataset(news_event("secret-news-ACME")))
        item = result.dataset.events[0]
        visible = (
            item.event_id
            + " "
            + item.payload["news_ref"]
            + " "
            + item.payload["statement"]
            + " "
            + " ".join(item.payload["relevance"])
        )
        self.assertNotIn("ACME", visible)
        self.assertNotIn("Acme Corp", visible)
        self.assertNotIn("secret-news", visible)

    def test_same_entity_is_stable_across_news_items_in_one_experiment(self):
        source = dataset(
            news_event("one", sequence=1),
            news_event(
                "two",
                published_at="2024-03-12T10:01:00Z",
                sequence=2,
            ),
        )
        result = anonymize(source)
        first = result.dataset.events[0].payload["statement"].split(" reported", 1)[0]
        second = result.dataset.events[1].payload["statement"].split(" reported", 1)[0]
        self.assertEqual(first, second)

    def test_entity_pseudonym_changes_across_experiments(self):
        source = dataset(news_event("one"))
        first = anonymize(source, experiment_id="alpha")
        second = anonymize(source, experiment_id="beta")
        self.assertNotEqual(
            first.dataset.events[0].payload["statement"],
            second.dataset.events[0].payload["statement"],
        )

    def test_news_reference_changes_across_experiments(self):
        source = dataset(news_event("one"))
        first = anonymize(source, experiment_id="alpha")
        second = anonymize(source, experiment_id="beta")
        self.assertNotEqual(
            first.dataset.events[0].payload["news_ref"],
            second.dataset.events[0].payload["news_ref"],
        )

    def test_same_inputs_are_deterministic(self):
        source = dataset(news_event("one"))
        first = anonymize(source)
        second = anonymize(source)
        self.assertEqual(first, second)
        self.assertEqual(first.dataset.dataset_sha256, second.dataset.dataset_sha256)

    def test_only_key_commitment_is_retained(self):
        result = anonymize(dataset(news_event("one")))
        self.assertRegex(
            result.shuffle_key_commitment_sha256,
            r"^sha256:[0-9a-f]{64}$",
        )
        self.assertNotEqual(result.shuffle_key_commitment_sha256, KEY)
        self.assertFalse(hasattr(result, "shuffle_key_sha256"))

    def test_correction_relation_points_to_anonymized_earlier_news(self):
        source = dataset(
            news_event("original", sequence=1),
            news_event(
                "correction",
                kind="NEWS_CORRECTION",
                published_at="2024-03-12T10:05:00Z",
                sequence=2,
                facts={"surprise": "12%"},
                magnitude={"earnings_surprise_bp": 1200},
                corrects_news_id="original",
            ),
        )
        result = anonymize(source)
        first, correction = result.dataset.events
        self.assertEqual(correction.payload["relation"]["type"], "CORRECTS")
        self.assertEqual(
            correction.payload["relation"]["news_ref"],
            first.payload["news_ref"],
        )
        self.assertNotIn("original", str(correction.payload["relation"]))

    def test_update_relation_points_to_anonymized_earlier_news(self):
        source = dataset(
            news_event("original", sequence=1),
            news_event(
                "update",
                kind="NEWS_UPDATE",
                published_at="2024-03-12T10:02:00Z",
                sequence=2,
                corrects_news_id="original",
            ),
        )
        result = anonymize(source)
        self.assertEqual(result.dataset.events[1].payload["relation"]["type"], "UPDATES")

    def test_correction_cannot_reference_future_news(self):
        source = dataset(
            news_event(
                "correction",
                kind="NEWS_CORRECTION",
                published_at="2024-03-12T10:00:00Z",
                sequence=1,
                corrects_news_id="later",
            ),
            news_event(
                "later",
                published_at="2024-03-12T10:05:00Z",
                sequence=2,
            ),
        )
        with self.assertRaisesRegex(NewsReplayError, "earlier published"):
            anonymize(source)

    def test_correction_cannot_reference_unknown_news(self):
        source = dataset(
            news_event(
                "correction",
                kind="NEWS_CORRECTION",
                corrects_news_id="missing",
            )
        )
        with self.assertRaisesRegex(NewsReplayError, "earlier published"):
            anonymize(source)

    def test_plain_news_cannot_smuggle_relation(self):
        event = news_event("one")
        payload = dict(event.payload)
        payload["corrects_news_id"] = "anything"
        rebuilt = CausalEvent.create(
            event_id="raw-one",
            kind="NEWS",
            event_time=event.event_time,
            available_at=event.available_at,
            ingested_at=event.ingested_at,
            source_priority=event.source_priority,
            source_sequence=event.source_sequence,
            payload=payload,
        )
        with self.assertRaisesRegex(NewsReplayError, "only NEWS_UPDATE"):
            anonymize(dataset(rebuilt))

    def test_update_requires_relation_target(self):
        source = dataset(news_event("update", kind="NEWS_UPDATE"))
        with self.assertRaises(ValueError):
            anonymize(source)

    def test_template_cannot_repeat_raw_identity_or_alias(self):
        for leaked in ("ACME rallied", "Acme Corp rallied"):
            with self.subTest(leaked=leaked):
                source = dataset(
                    news_event(
                        "one",
                        template=({"type": "text", "text": leaked},),
                    )
                )
                with self.assertRaisesRegex(NewsReplayError, "raw identity"):
                    anonymize(source)

    def test_neutral_fields_cannot_expose_absolute_calendar(self):
        cases = (
            dict(template=({"type": "text", "text": "on March 12, 2024"},)),
            dict(facts={"surprise": "2024-03-12"}),
            dict(magnitude={"window": "12/03/2024"}),
            dict(relevance=("earnings", "2024.03.12")),
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(NewsReplayError, "absolute calendar"):
                    anonymize(dataset(news_event("one", **kwargs)))

    def test_template_rejects_unknown_entity_role(self):
        source = dataset(
            news_event(
                "one",
                template=(
                    {"type": "entity", "role": "missing"},
                    {"type": "text", "text": " moved"},
                ),
            )
        )
        with self.assertRaisesRegex(NewsReplayError, "unknown entity role"):
            anonymize(source)

    def test_template_rejects_unknown_fact(self):
        source = dataset(
            news_event(
                "one",
                template=(
                    {"type": "entity", "role": "subject"},
                    {"type": "fact", "key": "missing"},
                ),
            )
        )
        with self.assertRaisesRegex(NewsReplayError, "unknown fact"):
            anonymize(source)

    def test_declared_entity_must_be_rendered(self):
        source = dataset(
            news_event(
                "one",
                template=(
                    {"type": "text", "text": "quarterly earnings surprised positively"},
                ),
            )
        )
        with self.assertRaisesRegex(NewsReplayError, "not rendered"):
            anonymize(source)

    def test_unregistered_entity_namespace_is_rejected(self):
        source = dataset(news_event("one", namespace="SECRET_ROLE"))
        with self.assertRaisesRegex(NewsReplayError, "registered neutral"):
            anonymize(source)

    def test_alias_cannot_name_two_entities_across_events(self):
        source = dataset(
            news_event(
                "one",
                canonical_id="COMPANY-A",
                display_name="Shared Alias",
                sequence=1,
            ),
            news_event(
                "two",
                canonical_id="COMPANY-B",
                display_name="Shared Alias",
                published_at="2024-03-12T10:01:00Z",
                sequence=2,
            ),
        )
        with self.assertRaisesRegex(NewsReplayError, "multiple canonical"):
            anonymize(source)

    def test_duplicate_news_id_is_rejected(self):
        source = dataset(
            news_event("same", sequence=1),
            news_event(
                "same",
                published_at="2024-03-12T10:01:00Z",
                sequence=2,
            ),
        )
        with self.assertRaisesRegex(NewsReplayError, "duplicate news_id"):
            anonymize(source)

    def test_unsupported_event_kind_is_rejected(self):
        source = dataset(news_event("one", kind="TRADE"))
        with self.assertRaisesRegex(NewsReplayError, "unsupported historical news kind"):
            anonymize(source)

    def test_empty_news_dataset_is_rejected(self):
        source = CausalDataset.create(manifest_sha256=MANIFEST, events=())
        with self.assertRaisesRegex(NewsReplayError, "at least one"):
            anonymize(source)

    def test_strategy_does_not_see_correction_until_historical_publication_time(self):
        source = dataset(
            news_event("original", sequence=1),
            news_event(
                "correction",
                kind="NEWS_CORRECTION",
                published_at="2024-03-12T10:05:00Z",
                sequence=2,
                facts={"surprise": "12%"},
                magnitude={"earnings_surprise_bp": 1200},
                corrects_news_id="original",
            ),
        )
        prepared = anonymize(source)
        feeder = BlindedCausalFeeder(
            dataset=prepared.dataset,
            start_time="2024-03-12T10:00:00Z",
            experiment_id="news-exp",
            shuffle_key_sha256=KEY,
            profile=BlindingProfile(identity_fields=()),
            training_cutoff_uncertainty="unknown",
        )
        self.assertEqual(len(feeder.view().events), 1)
        self.assertEqual(feeder.view().events[0].kind, "NEWS")
        self.assertEqual(feeder.advance_to("2024-03-12T10:04:59Z"), ())
        correction = feeder.advance_to("2024-03-12T10:05:00Z")
        self.assertEqual(len(correction), 1)
        self.assertEqual(correction[0].kind, "NEWS_CORRECTION")

    def test_derived_manifest_and_dataset_bind_source_and_experiment(self):
        source = dataset(news_event("one"))
        first = anonymize(source, experiment_id="alpha")
        second = anonymize(source, experiment_id="beta")
        self.assertRegex(first.dataset.manifest_sha256, r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(first.dataset.dataset_sha256, r"^sha256:[0-9a-f]{64}$")
        self.assertNotEqual(first.dataset.manifest_sha256, MANIFEST)
        self.assertNotEqual(first.dataset.manifest_sha256, second.dataset.manifest_sha256)

    def test_source_dataset_tampering_is_rejected(self):
        source = dataset(news_event("one"))
        object.__setattr__(source.events[0], "payload", {"news_id": "evil"})
        with self.assertRaises((TypeError, ValueError)):
            anonymize(source)


if __name__ == "__main__":
    unittest.main()
