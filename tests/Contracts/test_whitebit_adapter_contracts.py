import json
from datetime import datetime, timezone
from pathlib import Path
import unittest
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from mvp.autotrade_mvp.whitebit import (
    WhiteBitAdapterError,
    WhiteBitPreparedRequest,
    WhiteBitSubmissionResult,
    canonical_submission_result,
    parse_submission_result,
)

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "contracts" / "jsonschema"
NOW = datetime(2026, 9, 24, 20, 0, tzinfo=timezone.utc)
NOW_TEXT = "2026-09-24T20:00:00Z"
SOURCE = "https://whitebit.com/api/v4/order/new"
DIGEST = "sha256:" + "a" * 64


def response_evidence(
    result: WhiteBitSubmissionResult,
    *,
    artifact_id: str | None = None,
    digest: str | None = None,
    observed_at: str = NOW_TEXT,
    source_uri: str = SOURCE,
):
    return {
        "artifact_id": artifact_id or str(uuid4()),
        "sha256": digest or result.response_sha256,
        "observed_at": observed_at,
        "source_uri": source_uri,
        "rights_id": "whitebit-contract-fixture",
    }


def prepared(client_order_id: str) -> WhiteBitPreparedRequest:
    return WhiteBitPreparedRequest(
        endpoint="/api/v4/order/new",
        body={"clientOrderId": client_order_id, "market": "BTC_USDT"},
        capability_snapshot_id=str(uuid4()),
        documentation_refs=(),
    )

class WhiteBitAdapterContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schemas = {
            path.name: json.loads(path.read_text(encoding="utf-8"))
            for path in SCHEMAS.glob("*.json")
        }
        cls.registry = Registry().with_resources(
            [(schema["$id"], Resource.from_contents(schema)) for schema in cls.schemas.values()]
        )

    def validate_submission(self, value):
        schema = self.schemas["provider.schema.json"]
        Draft202012Validator(
            {"$ref": f"{schema['$id']}#/$defs/SubmissionResult"},
            registry=self.registry,
            format_checker=FormatChecker(),
        ).validate(value)

    def _result(self, **overrides):
        values = {
            "attempt_id": str(uuid4()),
            "account_id": "account-1",
            "environment": "PAPER",
            "client_order_id": "contract-1",
            "outcome": "ACKNOWLEDGED",
            "next_action": "OBSERVE_OR_RECONCILE",
            "observed_at": NOW,
            "response_sha256": DIGEST,
            "http_status": 200,
            "provider_order_id": "provider-order-1",
        }
        values.update(overrides)
        return WhiteBitSubmissionResult(**values)

    def test_ack_reject_and_unknown_project_to_canonical_contract(self):
        ack_internal = self._result()
        acknowledged = canonical_submission_result(
            ack_internal,
            response_evidence=response_evidence(ack_internal),
        )
        self.validate_submission(acknowledged)
        self.assertNotIn("environment", acknowledged)
        self.assertNotIn("observed_at", acknowledged)
        self.assertNotIn("provider_received_at", acknowledged)

        reject_internal = self._result(
            client_order_id="contract-reject",
            outcome="REJECTED",
            next_action="DO_NOT_RETRY_BLINDLY",
            http_status=422,
            provider_order_id=None,
            rejection_code="30",
            rejection_message="Validation failed",
        )
        rejected = canonical_submission_result(
            reject_internal,
            response_evidence=response_evidence(reject_internal),
        )
        self.validate_submission(rejected)
        self.assertEqual(rejected["retry_disposition"], "NEVER")

        unknown = canonical_submission_result(
            self._result(
                client_order_id="contract-unknown",
                outcome="UNKNOWN",
                next_action="RECONCILE_FIRST",
                response_sha256=None,
                http_status=None,
                provider_order_id=None,
            )
        )
        self.validate_submission(unknown)
        self.assertEqual(unknown["retry_disposition"], "RECONCILE_FIRST")
        self.assertNotIn("provider_received_at", unknown)
        self.assertEqual(unknown["evidence"], [])

    def test_non_uuid_attempt_fails_closed_at_canonical_boundary(self):
        with self.assertRaisesRegex(WhiteBitAdapterError, "UUID"):
            internal = self._result(attempt_id="attempt-not-a-uuid")
            canonical_submission_result(
                internal,
                response_evidence=response_evidence(internal),
            )

    def test_ack_without_provider_order_id_fails_closed(self):
        with self.assertRaisesRegex(WhiteBitAdapterError, "provider_order_id"):
            self._result(provider_order_id=None)

    def test_unknown_cannot_claim_response_evidence_or_status(self):
        with self.assertRaisesRegex(WhiteBitAdapterError, "UNKNOWN submission"):
            internal = self._result(
                outcome="UNKNOWN",
                next_action="RECONCILE_FIRST",
                provider_order_id=None,
                response_sha256=DIGEST,
                http_status=200,
            )
            canonical_submission_result(
                internal,
                response_evidence=response_evidence(internal),
            )

    def test_malformed_response_digest_is_rejected(self):
        with self.assertRaisesRegex(WhiteBitAdapterError, "response_sha256"):
            self._result(response_sha256="sha256:not-a-digest")

    def test_authoritative_response_requires_exact_immutable_evidence(self):
        internal = self._result()
        with self.assertRaisesRegex(
            WhiteBitAdapterError,
            "requires immutable response EvidenceRef",
        ):
            canonical_submission_result(internal)

        with self.assertRaisesRegex(WhiteBitAdapterError, "digest does not match"):
            canonical_submission_result(
                internal,
                response_evidence=response_evidence(
                    internal,
                    digest="sha256:" + "b" * 64,
                ),
            )

        with self.assertRaisesRegex(WhiteBitAdapterError, "artifact_id must be a UUID"):
            canonical_submission_result(
                internal,
                response_evidence=response_evidence(
                    internal,
                    artifact_id="not-a-uuid",
                ),
            )

        with self.assertRaisesRegex(WhiteBitAdapterError, "does not match local"):
            canonical_submission_result(
                internal,
                response_evidence=response_evidence(
                    internal,
                    observed_at="2026-09-24T20:00:01Z",
                ),
            )

    def test_unqualified_environment_and_source_are_rejected(self):
        invalid_environment = self._result(environment="MARS")
        with self.assertRaisesRegex(WhiteBitAdapterError, "environment"):
            canonical_submission_result(
                invalid_environment,
                response_evidence=response_evidence(invalid_environment),
            )

        internal = self._result()
        with self.assertRaisesRegex(WhiteBitAdapterError, "source_uri"):
            canonical_submission_result(
                internal,
                response_evidence=response_evidence(
                    internal,
                    source_uri="https://example.invalid/api/v4/order/new",
                ),
            )

    def test_transport_ambiguity_flag_must_be_boolean(self):
        with self.assertRaisesRegex(
            WhiteBitAdapterError,
            "transport_ambiguous must be boolean",
        ):
            parse_submission_result(
                prepared("whitebit-ambiguous-int"),
                attempt_id=str(uuid4()),
                account_id="paper-account",
                environment="PAPER",
                observed_at=NOW,
                response_body=None,
                http_status=None,
                transport_ambiguous=1,
            )

if __name__ == "__main__":
    unittest.main()
