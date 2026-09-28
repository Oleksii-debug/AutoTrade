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
    parse_submission_result,
    to_canonical_submission_result,
)


ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "contracts" / "jsonschema"
NOW = datetime(2026, 9, 24, 20, 0, tzinfo=timezone.utc)
NOW_TEXT = "2026-09-24T20:00:00Z"


def prepared(client_order_id: str) -> WhiteBitPreparedRequest:
    return WhiteBitPreparedRequest(
        endpoint="/api/v4/order/new",
        body={
            "clientOrderId": client_order_id,
            "market": "BTC_USDT",
        },
        account_id="paper-account",
        environment="PAPER",
        capability_snapshot_id=str(uuid4()),
        documentation_refs=("https://docs.whitebit.com/api-reference/overview",),
    )


def evidence_for(result: WhiteBitSubmissionResult, *, digest: str | None = None):
    return {
        "artifact_id": str(uuid4()),
        "sha256": digest or result.response_sha256,
        "observed_at": NOW_TEXT,
        "source_uri": "https://docs.whitebit.com/api-reference/overview",
        "rights_id": "whitebit-contract-fixture",
    }


class WhiteBitAdapterContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schemas = {
            file.name: json.loads(file.read_text(encoding="utf-8"))
            for file in SCHEMAS.glob("*.json")
        }
        cls.registry = Registry().with_resources(
            [
                (schema["$id"], Resource.from_contents(schema))
                for schema in cls.schemas.values()
            ]
        )

    def validate_submission(self, value):
        schema = self.schemas["provider.schema.json"]
        Draft202012Validator(
            {"$ref": f"{schema['$id']}#/$defs/SubmissionResult"},
            registry=self.registry,
            format_checker=FormatChecker(),
        ).validate(value)

    def test_ack_reject_and_transport_unknown_match_canonical_contract(self):
        ack_internal = parse_submission_result(
            prepared("whitebit-ack"),
            attempt_id=str(uuid4()),
            account_id="paper-account",
            environment="PAPER",
            observed_at=NOW,
            response_body=json.dumps(
                {
                    "orderId": 12345,
                    "clientOrderId": "whitebit-ack",
                    "market": "BTC_USDT",
                    "status": "NEW",
                },
                separators=(",", ":"),
            ),
            http_status=200,
        )
        acknowledged = to_canonical_submission_result(
            ack_internal,
            response_evidence=evidence_for(ack_internal),
        )
        self.assertEqual(acknowledged["outcome"], "ACKNOWLEDGED")
        self.assertEqual(acknowledged["provider_order_id"], "12345")
        self.assertEqual(acknowledged["retry_disposition"], "NEVER")
        self.assertNotIn("provider_received_at", acknowledged)
        self.assertNotIn("observed_at", acknowledged)
        self.validate_submission(acknowledged)

        reject_internal = parse_submission_result(
            prepared("whitebit-reject"),
            attempt_id=str(uuid4()),
            account_id="paper-account",
            environment="PAPER",
            observed_at=NOW,
            response_body=json.dumps(
                {
                    "code": 422001,
                    "message": "validation failed",
                    "errors": {"amount": ["too small"]},
                },
                separators=(",", ":"),
            ),
            http_status=422,
        )
        rejected = to_canonical_submission_result(
            reject_internal,
            response_evidence=evidence_for(reject_internal),
        )
        self.assertEqual(rejected["outcome"], "REJECTED")
        self.assertEqual(rejected["reason_code"], "422001")
        self.assertEqual(rejected["retry_disposition"], "NEVER")
        self.assertNotIn("provider_received_at", rejected)
        self.validate_submission(rejected)

        unknown_internal = parse_submission_result(
            prepared("whitebit-unknown"),
            attempt_id=str(uuid4()),
            account_id="paper-account",
            environment="PAPER",
            observed_at=NOW,
            response_body=None,
            http_status=None,
            transport_ambiguous=True,
        )
        unknown = to_canonical_submission_result(unknown_internal)
        self.assertEqual(unknown["outcome"], "UNKNOWN")
        self.assertEqual(unknown["retry_disposition"], "RECONCILE_FIRST")
        self.assertEqual(unknown["evidence"], [])
        self.assertNotIn("provider_received_at", unknown)
        self.assertNotIn("observed_at", unknown)
        self.validate_submission(unknown)

    def test_transport_ambiguity_flag_must_be_boolean(self):
        with self.assertRaisesRegex(
            WhiteBitAdapterError,
            "transport_ambiguous must be a boolean",
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

    def test_canonical_conversion_requires_uuid_attempt_identity(self):
        internal = parse_submission_result(
            prepared("whitebit-invalid-attempt"),
            attempt_id="not-a-uuid",
            account_id="paper-account",
            environment="PAPER",
            observed_at=NOW,
            response_body=json.dumps(
                {
                    "orderId": 12345,
                    "clientOrderId": "whitebit-invalid-attempt",
                    "market": "BTC_USDT",
                },
                separators=(",", ":"),
            ),
            http_status=200,
        )
        with self.assertRaisesRegex(WhiteBitAdapterError, "attempt_id must be a UUID"):
            to_canonical_submission_result(
                internal,
                response_evidence=evidence_for(internal),
            )

    def test_ack_cannot_exist_without_provider_order_id(self):
        with self.assertRaisesRegex(
            WhiteBitAdapterError,
            "acknowledged submission requires provider_order_id",
        ):
            WhiteBitSubmissionResult(
                attempt_id=str(uuid4()),
                account_id="paper-account",
                environment="PAPER",
                client_order_id="whitebit-no-order",
                outcome="ACKNOWLEDGED",
                next_action="OBSERVE_OR_RECONCILE",
                observed_at=NOW,
                response_sha256="sha256:" + "a" * 64,
                http_status=200,
                provider_order_id=None,
            )

    def test_unknown_cannot_claim_authoritative_response_evidence(self):
        internal = parse_submission_result(
            prepared("whitebit-unknown-evidence"),
            attempt_id=str(uuid4()),
            account_id="paper-account",
            environment="PAPER",
            observed_at=NOW,
            response_body=None,
            http_status=None,
            transport_ambiguous=True,
        )
        with self.assertRaisesRegex(
            WhiteBitAdapterError,
            "cannot claim provider response evidence",
        ):
            to_canonical_submission_result(
                internal,
                response_evidence={
                    "artifact_id": str(uuid4()),
                    "sha256": "sha256:" + "a" * 64,
                    "observed_at": NOW_TEXT,
                },
            )

    def test_authoritative_response_requires_exact_immutable_evidence_digest(self):
        internal = parse_submission_result(
            prepared("whitebit-evidence"),
            attempt_id=str(uuid4()),
            account_id="paper-account",
            environment="PAPER",
            observed_at=NOW,
            response_body=json.dumps(
                {
                    "orderId": 12345,
                    "clientOrderId": "whitebit-evidence",
                    "market": "BTC_USDT",
                },
                separators=(",", ":"),
            ),
            http_status=200,
        )
        with self.assertRaisesRegex(
            WhiteBitAdapterError,
            "digest does not match",
        ):
            to_canonical_submission_result(
                internal,
                response_evidence=evidence_for(
                    internal,
                    digest="sha256:" + "b" * 64,
                ),
            )

        with self.assertRaisesRegex(
            WhiteBitAdapterError,
            "requires immutable response EvidenceRef",
        ):
            to_canonical_submission_result(internal)

    def test_ambiguous_transport_requires_real_boolean_and_canonical_environment(self):
        request = prepared("whitebit-bool-boundary")
        with self.assertRaisesRegex(
            WhiteBitAdapterError,
            "transport_ambiguous must be a boolean",
        ):
            parse_submission_result(
                request,
                attempt_id=str(uuid4()),
                account_id="paper-account",
                environment="PAPER",
                observed_at=NOW,
                response_body=None,
                http_status=None,
                transport_ambiguous=1,
            )

        with self.assertRaisesRegex(
            WhiteBitAdapterError,
            "environment",
        ):
            parse_submission_result(
                request,
                attempt_id=str(uuid4()),
                account_id="paper-account",
                environment="MAINNET",
                observed_at=NOW,
                response_body=None,
                http_status=None,
                transport_ambiguous=True,
            )

    def test_internal_response_digest_is_fail_closed(self):
        with self.assertRaisesRegex(
            WhiteBitAdapterError,
            "response_sha256 must be canonical SHA-256",
        ):
            WhiteBitSubmissionResult(
                attempt_id=str(uuid4()),
                account_id="paper-account",
                environment="PAPER",
                client_order_id="whitebit-bad-digest",
                outcome="REJECTED",
                next_action="DO_NOT_RETRY_BLINDLY",
                observed_at=NOW,
                response_sha256="bad-digest",
                http_status=422,
                rejection_code="422001",
            )


if __name__ == "__main__":
    unittest.main()
