import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from mvp.autotrade_mvp.information_claims import (
    ClaimStore,
    SourceDocument,
    build_information_event,
)


ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "contracts" / "jsonschema"
BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


class InformationEvidenceContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schemas = {
            path.name: json.loads(path.read_text(encoding="utf-8"))
            for path in SCHEMAS.glob("*.json")
        }
        cls.registry = Registry().with_resources(
            [
                (schema["$id"], Resource.from_contents(schema))
                for schema in cls.schemas.values()
            ]
        )
        common = cls.schemas["common.schema.json"]
        cls.validator = Draft202012Validator(
            {"$ref": f"{common['$id']}#/$defs/EvidenceRef"},
            registry=cls.registry,
            format_checker=FormatChecker(),
        )

    @staticmethod
    def document() -> SourceDocument:
        return SourceDocument.create(
            source_id="official-cpi",
            source_revision="2026-01-r1",
            source_kind="MACRO",
            title="CPI release",
            passage="CPI increased 2.1 percent.",
            published_at=BASE,
            available_at=BASE + timedelta(minutes=1),
            ingested_at=BASE + timedelta(minutes=2),
            rights_basis="official-publication",
            locator="table-1",
        )

    def test_source_artifact_projection_matches_canonical_evidence_ref(self):
        evidence = self.document().to_evidence_ref(
            artifact_id="123e4567-e89b-12d3-a456-426614174000",
            sha256="sha256:" + ("a" * 64),
            observed_at=BASE + timedelta(minutes=2),
            source_uri="https://example.test/cpi/2026-01",
        )
        self.validator.validate(evidence)
        self.assertEqual(evidence["rights_id"], "official-publication")
        self.assertEqual(evidence["observed_at"], "2026-01-01T00:02:00Z")

    def test_information_event_projection_matches_canonical_contract(self):
        document = self.document()
        store = ClaimStore()
        claim = store.build_claim(
            document,
            subject="CPI",
            predicate="headline_value",
            value="2.1 percent",
        )
        event = build_information_event(
            document,
            (claim,),
            information_id="123e4567-e89b-12d3-a456-426614174001",
            revision="1",
            language="en",
            extraction_version="extractor-1.0.0",
            artifact_id="123e4567-e89b-12d3-a456-426614174000",
            artifact_sha256="sha256:" + ("a" * 64),
            observed_at=BASE + timedelta(minutes=2),
            source_uri="https://example.test/cpi/2026-01",
            confidence_by_claim={claim.claim_id: 0.95},
            trust_features={"official_source": True},
        )
        data = self.schemas["data.schema.json"]
        Draft202012Validator(
            {"$ref": f"{data['$id']}#/$defs/InformationEvent"},
            registry=self.registry,
            format_checker=FormatChecker(),
        ).validate(event)
        self.assertEqual(event["content_hash"], "sha256:" + ("a" * 64))
        self.assertEqual(event["trust_features"]["source_revision"], "2026-01-r1")
        self.assertEqual(event["trust_features"]["extraction_version"], "extractor-1.0.0")
        self.assertEqual(event["entities"], ["CPI"])

    def test_information_event_projection_rejects_mixed_or_incomplete_provenance(self):
        document = self.document()
        store = ClaimStore()
        claim = store.build_claim(
            document,
            subject="CPI",
            predicate="headline_value",
            value="2.1 percent",
        )
        other = SourceDocument.create(
            source_id="other-source",
            source_revision="r1",
            source_kind="NEWS",
            title="other",
            passage="other passage",
            published_at=BASE,
            available_at=BASE,
            ingested_at=BASE,
            rights_basis="licensed",
            locator="p1",
        )
        foreign_claim = store.build_claim(
            other,
            subject="X",
            predicate="state",
            value="up",
        )
        common = dict(
            information_id="123e4567-e89b-12d3-a456-426614174001",
            revision="1",
            language="en",
            extraction_version="extractor-1.0.0",
            artifact_id="123e4567-e89b-12d3-a456-426614174000",
            artifact_sha256="sha256:" + ("a" * 64),
            observed_at=BASE + timedelta(minutes=2),
        )

        with self.assertRaisesRegex(ValueError, "provenance"):
            build_information_event(
                document,
                (foreign_claim,),
                confidence_by_claim={foreign_claim.claim_id: 0.5},
                **common,
            )
        with self.assertRaisesRegex(ValueError, "explicit confidence"):
            build_information_event(
                document,
                (claim,),
                confidence_by_claim={},
                **common,
            )
        with self.assertRaisesRegex(ValueError, "exact claim population"):
            build_information_event(
                document,
                (claim,),
                confidence_by_claim={claim.claim_id: 0.5, "extra": 0.1},
                **common,
            )

    def test_source_artifact_projection_fails_closed_on_invalid_provenance(self):
        document = self.document()
        cases = (
            {
                "artifact_id": "not-a-uuid",
                "sha256": "sha256:" + ("a" * 64),
                "observed_at": BASE + timedelta(minutes=2),
            },
            {
                "artifact_id": "123e4567-e89b-12d3-a456-426614174000",
                "sha256": "not-a-digest",
                "observed_at": BASE + timedelta(minutes=2),
            },
            {
                "artifact_id": "123e4567-e89b-12d3-a456-426614174000",
                "sha256": "sha256:" + ("a" * 64),
                "observed_at": BASE,
            },
        )
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(ValueError):
                    document.to_evidence_ref(**case)

        with self.assertRaisesRegex(ValueError, "absolute URI"):
            document.to_evidence_ref(
                artifact_id="123e4567-e89b-12d3-a456-426614174000",
                sha256="sha256:" + ("a" * 64),
                observed_at=BASE + timedelta(minutes=2),
                source_uri="relative/path",
            )


if __name__ == "__main__":
    unittest.main()
