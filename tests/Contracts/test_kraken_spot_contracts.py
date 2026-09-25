import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from mvp.autotrade_mvp.capabilities import (
    CapabilityClaim,
    EvidenceVerification,
    derive_capability_snapshot,
)
from mvp.autotrade_mvp.dispatch import (
    ExactJsonTransportResponse,
    GuardedDispatcher,
    load_submission_response_binding,
    stable_client_order_id,
)
from mvp.autotrade_mvp.kraken_spot import (
    KrakenSpotOrderIntent,
    parse_spot_submission_response,
    prepare_spot_order_request,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_core import observe_submission_json_response


ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "contracts" / "jsonschema"
NOW_DT = datetime(2026, 9, 24, 20, tzinfo=timezone.utc)
SOURCE = "https://api.kraken.com/0/private/AddOrder"
ACCOUNT_ID = "spot-contract-account"


class KrakenSpotContractTests(unittest.TestCase):
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

    def prepared(self, intent_id, *, environment="LIVE"):
        client_order_id = stable_client_order_id(
            "KRAKEN",
            intent_id,
            environment=environment,
            account_id=ACCOUNT_ID,
            max_length=36,
            client_id_format="UUID",
        )
        claim_observed_at = NOW_DT - timedelta(minutes=1)
        claims = tuple(
            CapabilityClaim(
                source=source,
                provider_id="KRAKEN",
                account_id=ACCOUNT_ID,
                entity_id="kraken-spot",
                environment=environment,
                instrument_version="XBTUSD:v1",
                observed_at=claim_observed_at,
                expires_at=NOW_DT + timedelta(minutes=1),
                supported_order_types=frozenset({"MARKET"}),
                time_in_force=frozenset({"GTC"}),
                permission_scopes=frozenset({"ORDER_WRITE"}),
                position_mode="CASH",
                native_protection=frozenset(),
                rate_limit_policy_id="kraken-spot-contract",
                data_entitlements=frozenset({"ORDERS"}),
                evidence_ref={
                    "artifact_id": str(uuid4()),
                    "sha256": "sha256:" + "a" * 64,
                    "observed_at": "2026-09-24T19:59:00Z",
                    "source_uri": "https://www.kraken.com/features/trading-api",
                },
            )
            for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")
        )
        capability = derive_capability_snapshot(
            snapshot_id=str(uuid4()),
            claims=claims,
            observed_at=NOW_DT,
            evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
        )
        intent = KrakenSpotOrderIntent.create(
            instrument_version="XBTUSD:v1",
            pair="XBTUSD",
            side="BUY",
            order_type="MARKET",
            volume="0.01",
        )
        return prepare_spot_order_request(
            intent,
            client_order_id=client_order_id,
            account_id=ACCOUNT_ID,
            environment=environment,
            capability=capability,
            at=NOW_DT,
        )

    def durable_observation(self, payload, *, intent_id):
        prepared_request = self.prepared(intent_id)
        attempt_id = str(uuid4())
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
                environment=prepared_request.environment,
                account_id=prepared_request.account_id,
                owner_token="contract-owner",
            )
            outcome = dispatcher.dispatch(
                attempt_id=attempt_id,
                intent_id=intent_id,
                intent_hash="kraken-spot-contract-intent",
                provider="KRAKEN",
                request=prepared_request.body,
                now="2026-09-24T20:00:00Z",
                authority_check=lambda _hash, _now: (True, "allowed"),
                transport_send=lambda _cid, _request, guard: (
                    guard(),
                    ExactJsonTransportResponse(raw),
                )[1],
                client_id_max_length=36,
                client_id_format="UUID",
                sender_check=lambda _owner, _epoch: None,
                submission_scope={
                    "endpoint": prepared_request.endpoint,
                    "prepared_request_sha256": prepared_request.body_sha256,
                    "capability_snapshot_ids": [
                        prepared_request.capability_snapshot_id
                    ],
                    "instrument_versions": [
                        prepared_request.instrument_version
                    ],
                },
            )
            self.assertEqual(outcome.status, "SENT")
            binding = load_submission_response_binding(
                store,
                environment=prepared_request.environment,
                account_id=prepared_request.account_id,
                attempt_id=attempt_id,
            )
            observation = observe_submission_json_response(
                response_binding=binding,
                provider_id="KRAKEN",
                endpoint=prepared_request.endpoint,
                prepared_request_sha256=prepared_request.body_sha256,
                capability_snapshot_ids=(
                    prepared_request.capability_snapshot_id,
                ),
                instrument_versions=(
                    prepared_request.instrument_version,
                ),
            )
        return attempt_id, prepared_request, observation

    def test_ack_reject_and_transport_unknown_match_canonical_contract(self):
        attempt_id, prepared_request, observation = self.durable_observation(
            {"error": [], "result": {"txid": ["OABC-D123-E456"]}},
            intent_id="spot-contract-1",
        )
        acknowledged = parse_spot_submission_response(
            attempt_id=attempt_id,
            prepared_request=prepared_request,
            source_uri=SOURCE,
            observation=observation,
        )
        self.assertNotIn("provider_received_at", acknowledged)
        self.assertEqual(
            acknowledged["evidence"][0]["sha256"],
            observation.response_sha256,
        )
        self.validate_submission(acknowledged)

        attempt_id, prepared_request, observation = self.durable_observation(
            {"error": ["EOrder:Insufficient funds"], "result": None},
            intent_id="spot-contract-2",
        )
        rejected = parse_spot_submission_response(
            attempt_id=attempt_id,
            prepared_request=prepared_request,
            source_uri=SOURCE,
            observation=observation,
        )
        self.assertNotIn("provider_received_at", rejected)
        self.assertEqual(
            rejected["evidence"][0]["sha256"],
            observation.response_sha256,
        )
        self.validate_submission(rejected)

        unknown = parse_spot_submission_response(
            attempt_id=str(uuid4()),
            prepared_request=self.prepared("spot-contract-3"),
            source_uri=SOURCE,
            observation=None,
            transport_ambiguous=True,
        )
        self.assertNotIn("provider_received_at", unknown)
        self.assertNotIn("observed_at", unknown)
        self.validate_submission(unknown)

        with self.assertRaisesRegex(ValueError, "qualified only for LIVE"):
            parse_spot_submission_response(
                attempt_id=str(uuid4()),
                prepared_request=self.prepared(
                    "spot-invalid-env",
                    environment="PAPER",
                ),
                source_uri=SOURCE,
                observation=None,
                transport_ambiguous=True,
            )


if __name__ == "__main__":
    unittest.main()
