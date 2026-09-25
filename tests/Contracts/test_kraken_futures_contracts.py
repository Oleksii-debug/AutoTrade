from datetime import datetime, timedelta, timezone
import json
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
from mvp.autotrade_mvp.kraken_futures import (
    parse_submission_response,
    prepare_order_request,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_core import observe_submission_json_response


ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "contracts" / "jsonschema"
NOW_DT = datetime(2026, 9, 24, 20, tzinfo=timezone.utc)
NOW = "2026-09-24T20:00:00Z"


def capability():
    observed = NOW_DT - timedelta(hours=1)
    claims = tuple(
        CapabilityClaim(
            source=source,
            provider_id="KRAKEN",
            account_id="contract-account",
            entity_id="contract-entity",
            environment="PAPER",
            instrument_version="PI_XBTUSD@v1",
            observed_at=observed,
            expires_at=NOW_DT + timedelta(hours=1),
            supported_order_types=frozenset({"MARKET"}),
            time_in_force=frozenset({"GTC"}),
            permission_scopes=frozenset({"ORDER_WRITE"}),
            position_mode="NET",
            native_protection=frozenset(),
            rate_limit_policy_id="contract",
            data_entitlements=frozenset(),
            evidence_ref={
                "artifact_id": str(uuid4()),
                "sha256": "sha256:" + "3" * 64,
                "observed_at": observed.isoformat().replace("+00:00", "Z"),
            },
        )
        for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")
    )
    return derive_capability_snapshot(
        snapshot_id=str(uuid4()),
        claims=claims,
        observed_at=NOW_DT,
        evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
    )


def prepared(intent_id: str):
    client_id = stable_client_order_id(
        "KRAKEN",
        intent_id,
        environment="PAPER",
        account_id="contract-account",
        max_length=36,
        client_id_format="UUID",
    )
    return prepare_order_request(
        capability=capability(),
        account_id="contract-account",
        provider_environment="DEMO",
        instrument_version="PI_XBTUSD@v1",
        at=NOW_DT,
        symbol="PI_XBTUSD",
        side="BUY",
        order_type="MARKET",
        size="1",
        client_order_id=client_id,
    )


def durable_observation(payload, *, intent_id: str):
    request = prepared(intent_id)
    attempt = str(uuid4())
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    with TemporaryDirectory() as directory:
        store = JournalStore(f"{directory}/journal.sqlite3")
        dispatcher = GuardedDispatcher(
            store,
            environment="PAPER",
            account_id="contract-account",
            owner_token="owner",
        )
        outcome = dispatcher.dispatch(
            attempt_id=attempt,
            intent_id=intent_id,
            intent_hash="contract-intent-hash",
            provider="KRAKEN",
            request=request.body,
            now=NOW,
            authority_check=lambda _hash, _now: (True, "allowed"),
            transport_send=lambda _cid, _request, guard: (
                guard(),
                ExactJsonTransportResponse(raw),
            )[1],
            client_id_max_length=36,
            client_id_format="UUID",
            sender_check=lambda _owner, _epoch: None,
            submission_scope={
                "endpoint": request.endpoint,
                "prepared_request_sha256": request.body_sha256,
                "capability_snapshot_ids": [request.capability_snapshot_id],
                "instrument_versions": [request.instrument_version],
            },
        )
        if outcome.status != "SENT":
            raise AssertionError("contract fixture submission was not SENT")
        binding = load_submission_response_binding(
            store,
            environment="PAPER",
            account_id="contract-account",
            attempt_id=attempt,
        )
        observation = observe_submission_json_response(
            response_binding=binding,
            provider_id="KRAKEN",
            endpoint=request.endpoint,
            prepared_request_sha256=request.body_sha256,
            capability_snapshot_ids=(request.capability_snapshot_id,),
            instrument_versions=(request.instrument_version,),
        )
    return attempt, request, observation


class KrakenFuturesContractTests(unittest.TestCase):
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

    def test_acknowledged_and_unknown_match_canonical_provider_contract(self):
        attempt, request, observation = durable_observation(
            {
                "result": "success",
                "sendStatus": {"order_id": "provider-order-1", "status": "placed"},
            },
            intent_id="fut-contract-1",
        )
        acknowledged = parse_submission_response(
            attempt_id=attempt,
            prepared_request=request,
            observation=observation,
        )
        self.assertNotIn("provider_received_at", acknowledged)
        self.validate_submission(acknowledged)

        unknown = parse_submission_response(
            attempt_id=str(uuid4()),
            prepared_request=prepared("fut-contract-2"),
            observation=None,
            transport_ambiguous=True,
        )
        self.assertNotIn("provider_received_at", unknown)
        self.assertNotIn("observed_at", unknown)
        self.validate_submission(unknown)


if __name__ == "__main__":
    unittest.main()
