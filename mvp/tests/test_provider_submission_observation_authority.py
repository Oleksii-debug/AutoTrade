from hashlib import sha256
from tempfile import TemporaryDirectory
import unittest

import mvp.autotrade_mvp.provider_core as provider_core
from mvp.autotrade_mvp.dispatch import (
    ExactJsonTransportResponse,
    GuardedDispatcher,
    load_submission_response_binding,
)
from mvp.autotrade_mvp.persistence import JournalStore, canonical_json
from mvp.autotrade_mvp.provider_core import (
    ProviderCoreError,
    ProviderSubmissionObservation,
    observe_submission_json_response,
    provider_submission_observation_projection,
)


class ProviderSubmissionObservationAuthorityTests(unittest.TestCase):
    ENDPOINT = "/v5/order/create"
    CAPABILITIES = ("capability-snapshot-1",)
    INSTRUMENTS = ("instrument-version-1",)

    def _binding(self, path: str, *, ambiguous: bool = False):
        request = {"side": "BUY", "symbol": "BTCUSDT"}
        request_sha = (
            "sha256:" + sha256(canonical_json(request).encode("utf-8")).hexdigest()
        )
        scope = {
            "endpoint": self.ENDPOINT,
            "prepared_request_sha256": request_sha,
            "capability_snapshot_ids": list(self.CAPABILITIES),
            "instrument_versions": list(self.INSTRUMENTS),
        }
        store = JournalStore(path)
        dispatcher = GuardedDispatcher(
            store,
            environment="SIMULATION",
            account_id="acct",
            owner_token="owner",
        )
        response = ExactJsonTransportResponse(
            b'{"retCode":0,"result":{"orderId":"provider-1"}}',
            http_status=200,
            requires_reconciliation=ambiguous,
            ambiguity_reason="provider_response_ambiguous" if ambiguous else None,
        )
        outcome = dispatcher.dispatch(
            attempt_id="provider-observation-a1",
            intent_id="intent-1",
            intent_hash="sha256:" + "1" * 64,
            provider="BYBIT",
            request=request,
            now="2026-10-06T14:00:00Z",
            authority_check=lambda _hash, _now: (True, "allowed"),
            transport_send=lambda _cid, _request, guard: (
                guard(),
                response,
            )[1],
            submission_scope=scope,
        )
        self.assertEqual(outcome.status, "UNKNOWN" if ambiguous else "SENT")
        binding = load_submission_response_binding(
            JournalStore(path),
            environment="SIMULATION",
            account_id="acct",
            attempt_id="provider-observation-a1",
        )
        return binding, request_sha

    def _observation(self, path: str):
        binding, request_sha = self._binding(path)
        observation = observe_submission_json_response(
            response_binding=binding,
            provider_id="BYBIT",
            endpoint=self.ENDPOINT,
            prepared_request_sha256=request_sha,
            capability_snapshot_ids=self.CAPABILITIES,
            instrument_versions=self.INSTRUMENTS,
        )
        return binding, request_sha, observation

    def test_factory_mints_registered_descriptor_free_projection(self):
        with TemporaryDirectory() as directory:
            _binding, request_sha, observation = self._observation(
                f"{directory}/journal.sqlite3"
            )
            projected = provider_submission_observation_projection(observation)
            self.assertEqual(projected["provider_id"], "BYBIT")
            self.assertEqual(projected["endpoint"], self.ENDPOINT)
            self.assertEqual(projected["request_sha256"], request_sha)
            self.assertEqual(projected["terminal_state"], "SENT")
            self.assertEqual(projected["response_encoding"], "utf-8-json")
            self.assertEqual(projected["http_status"], 200)
            self.assertEqual(
                projected["capability_snapshot_ids"],
                self.CAPABILITIES,
            )
            self.assertEqual(projected["instrument_versions"], self.INSTRUMENTS)

    def test_imported_token_cannot_self_mint_observation_authority(self):
        with TemporaryDirectory() as directory:
            binding, _request_sha, _observation = self._observation(
                f"{directory}/journal.sqlite3"
            )
            forged = ProviderSubmissionObservation(
                response_binding=binding,
                endpoint=self.ENDPOINT,
                capability_snapshot_ids=self.CAPABILITIES,
                instrument_versions=self.INSTRUMENTS,
                evidence_ref="provider-write:sha256:" + "0" * 64,
                payload={"retCode": 0},
                _observation_token=provider_core._SUBMISSION_OBSERVED_RESPONSE_TOKEN,
            )
            with self.assertRaisesRegex(
                ProviderCoreError,
                "provider submission observation authority is unavailable",
            ):
                provider_submission_observation_projection(forged)

    def test_uninitialized_exact_type_clone_cannot_self_mint_authority(self):
        with TemporaryDirectory() as directory:
            _binding, _request_sha, observation = self._observation(
                f"{directory}/journal.sqlite3"
            )
            clone = object.__new__(ProviderSubmissionObservation)
            source_state = object.__getattribute__(observation, "__dict__")
            clone_state = object.__getattribute__(clone, "__dict__")
            for key, value in source_state.items():
                dict.__setitem__(clone_state, key, value)
            with self.assertRaisesRegex(
                ProviderCoreError,
                "provider submission observation authority is unavailable",
            ):
                provider_submission_observation_projection(clone)

    def test_post_mint_field_retarget_fails_closed(self):
        with TemporaryDirectory() as directory:
            _binding, _request_sha, observation = self._observation(
                f"{directory}/journal.sqlite3"
            )
            state = object.__getattribute__(observation, "__dict__")
            dict.__setitem__(state, "endpoint", "/forged")
            with self.assertRaisesRegex(
                ProviderCoreError,
                "provider submission observation authority is unavailable",
            ):
                provider_submission_observation_projection(observation)

    def test_property_shadow_does_not_execute_before_projection(self):
        calls = 0

        def trap(_self):
            nonlocal calls
            calls += 1
            raise AssertionError("shadow property executed")

        with TemporaryDirectory() as directory:
            _binding, _request_sha, observation = self._observation(
                f"{directory}/journal.sqlite3"
            )
            had_endpoint = "endpoint" in ProviderSubmissionObservation.__dict__
            original_endpoint = ProviderSubmissionObservation.__dict__.get("endpoint")
            try:
                ProviderSubmissionObservation.endpoint = property(trap)
                projected = provider_submission_observation_projection(observation)
                self.assertEqual(projected["endpoint"], self.ENDPOINT)
                self.assertEqual(calls, 0)
            finally:
                if had_endpoint:
                    ProviderSubmissionObservation.endpoint = original_endpoint
                else:
                    delattr(ProviderSubmissionObservation, "endpoint")

    def test_require_scope_rebinding_is_not_executed_by_projection(self):
        calls = 0

        def trap(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise AssertionError("require_scope executed")

        with TemporaryDirectory() as directory:
            _binding, _request_sha, observation = self._observation(
                f"{directory}/journal.sqlite3"
            )
            original = ProviderSubmissionObservation.require_scope
            try:
                ProviderSubmissionObservation.require_scope = trap
                projected = provider_submission_observation_projection(observation)
                self.assertEqual(projected["provider_id"], "BYBIT")
                self.assertEqual(calls, 0)
            finally:
                ProviderSubmissionObservation.require_scope = original

    def test_binding_projection_alias_rebinding_fails_before_callback(self):
        calls = 0

        def trap(_value):
            nonlocal calls
            calls += 1
            raise AssertionError("rebound binding projection executed")

        with TemporaryDirectory() as directory:
            _binding, _request_sha, observation = self._observation(
                f"{directory}/journal.sqlite3"
            )
            original = provider_core.submission_response_binding_projection
            try:
                provider_core.submission_response_binding_projection = trap
                with self.assertRaisesRegex(
                    ProviderCoreError,
                    "provider submission observation authority is unavailable",
                ):
                    provider_submission_observation_projection(observation)
                self.assertEqual(calls, 0)
            finally:
                provider_core.submission_response_binding_projection = original

    def test_unknown_binding_cannot_be_promoted_to_provider_observation(self):
        with TemporaryDirectory() as directory:
            binding, request_sha = self._binding(
                f"{directory}/journal.sqlite3",
                ambiguous=True,
            )
            with self.assertRaisesRegex(
                ProviderCoreError,
                "requires definitive SENT response",
            ):
                observe_submission_json_response(
                    response_binding=binding,
                    provider_id="BYBIT",
                    endpoint=self.ENDPOINT,
                    prepared_request_sha256=request_sha,
                    capability_snapshot_ids=self.CAPABILITIES,
                    instrument_versions=self.INSTRUMENTS,
                )


if __name__ == "__main__":
    unittest.main()
