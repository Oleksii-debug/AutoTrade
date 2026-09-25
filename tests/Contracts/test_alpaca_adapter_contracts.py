from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from mvp.autotrade_mvp.alpaca import (
    AlpacaOrderIntent,
    parse_submission_response,
    prepare_order_request,
)
from mvp.autotrade_mvp.capabilities import (
    CapabilityClaim,
    EvidenceVerification,
    derive_capability_snapshot,
)
from mvp.autotrade_mvp.dispatch import (
    ExactJsonTransportResponse,
    GuardedDispatcher,
    load_submission_response_binding,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_core import observe_submission_json_response


ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "contracts" / "jsonschema"
NOW = datetime(2026, 9, 24, 20, tzinfo=timezone.utc)


def capability():
    observed_at = NOW - timedelta(hours=1)
    claims = tuple(
        CapabilityClaim(
            source=source,
            provider_id="ALPACA",
            account_id="contract-account",
            entity_id="contract-entity",
            environment="PAPER",
            instrument_version="AAPL:v1",
            observed_at=observed_at,
            expires_at=NOW + timedelta(hours=1),
            supported_order_types=frozenset({"MARKET"}),
            time_in_force=frozenset({"DAY"}),
            permission_scopes=frozenset({"ORDER_WRITE"}),
            position_mode="NET",
            native_protection=frozenset(),
            rate_limit_policy_id="contract",
            data_entitlements=frozenset(),
            evidence_ref={
                "artifact_id": str(uuid4()),
                "sha256": "sha256:" + "1" * 64,
                "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
            },
        )
        for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")
    )
    return derive_capability_snapshot(
        snapshot_id=str(uuid4()),
        claims=claims,
        observed_at=NOW,
        evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
    )


def prepared(client_order_id: str):
    return prepare_order_request(
        AlpacaOrderIntent.create(
            instrument_version="AAPL:v1",
            asset_class="EQUITY",
            symbol="AAPL",
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        ),
        client_order_id=client_order_id,
        account_id="contract-account",
        environment="PAPER",
        capability=capability(),
        at=NOW,
    )


def durable_observation(prepared_request, *, attempt_id: str, payload):
    raw = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    with TemporaryDirectory() as directory:
        store = JournalStore(f"{directory}/journal.sqlite3")
        dispatcher = GuardedDispatcher(
            store,
            environment="PAPER",
            account_id="contract-account",
            owner_token="contract-owner",
        )
        outcome = dispatcher.dispatch(
            attempt_id=attempt_id,
            intent_id="contract-intent",
            intent_hash="contract-intent-hash",
            provider="ALPACA",
            request=prepared_request.body,
            now="2026-09-24T20:00:00Z",
            authority_check=lambda _hash, _now: (True, "allowed"),
            transport_send=lambda _cid, _request, guard: (
                guard(),
                ExactJsonTransportResponse(raw),
            )[1],
            sender_check=lambda _owner, _epoch: None,
            submission_scope={
                "endpoint": prepared_request.endpoint,
                "prepared_request_sha256": prepared_request.body_sha256,
                "capability_snapshot_ids": list(
                    prepared_request.capability_snapshot_ids
                ),
                "instrument_versions": list(prepared_request.instrument_versions),
            },
        )
        if outcome.status != "SENT":
            raise AssertionError(f"expected SENT, got {outcome.status}")
        binding = load_submission_response_binding(
            store,
            environment="PAPER",
            account_id="contract-account",
            attempt_id=attempt_id,
        )
        return observe_submission_json_response(
            response_binding=binding,
            provider_id="ALPACA",
            endpoint=prepared_request.endpoint,
            prepared_request_sha256=prepared_request.body_sha256,
            capability_snapshot_ids=prepared_request.capability_snapshot_ids,
            instrument_versions=prepared_request.instrument_versions,
        )


class AlpacaAdapterContractTests(unittest.TestCase):
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

    def validate_submission(self, value):
        schema = self.schemas["provider.schema.json"]
        Draft202012Validator(
            {"$ref": f"{schema['$id']}#/$defs/SubmissionResult"},
            registry=self.registry,
            format_checker=FormatChecker(),
        ).validate(value)

    def test_submission_matches_canonical_provider_contract(self):
        attempt_id = str(uuid4())
        prepared_request = prepared("contract-1")
        observation = durable_observation(
            prepared_request,
            attempt_id=attempt_id,
            payload={
                "id": str(uuid4()),
                "client_order_id": "contract-1",
                "status": "accepted",
            },
        )
        value = parse_submission_response(
            attempt_id=attempt_id,
            prepared_request=prepared_request,
            observation=observation,
        )
        self.assertNotIn("provider_received_at", value)
        self.assertEqual(value["evidence"][0]["sha256"], observation.response_sha256)
        self.validate_submission(value)

        unknown = parse_submission_response(
            attempt_id=str(uuid4()),
            prepared_request=prepared("contract-unknown"),
            observation=None,
            transport_ambiguous=True,
        )
        self.assertNotIn("provider_received_at", unknown)
        self.assertNotIn("observed_at", unknown)
        self.validate_submission(unknown)


if __name__ == "__main__":
    unittest.main()
