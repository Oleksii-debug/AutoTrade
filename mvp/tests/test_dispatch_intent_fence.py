from hashlib import sha256
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.dispatch import (
    GuardedDispatcher,
    stable_client_order_id,
    submission_attempt_aggregate_id,
    submission_intent_aggregate_id,
)
from mvp.autotrade_mvp.persistence import JournalStore, canonical_json


class ExplodingText(str):
    def strip(self, *args, **kwargs):
        raise AssertionError("polymorphic strip callback executed")

    def lower(self):
        raise AssertionError("polymorphic lower callback executed")

    def upper(self):
        raise AssertionError("polymorphic upper callback executed")

    def replace(self, *args, **kwargs):
        raise AssertionError("polymorphic replace callback executed")


class SimulatedProcessDeath(BaseException):
    pass


class SubmissionIntentFenceTests(unittest.TestCase):
    def store(self, directory):
        return JournalStore(f"{directory}/journal.sqlite3")

    @staticmethod
    def authority(_intent_hash, _now):
        return True, "allowed"

    def dispatch(
        self,
        dispatcher,
        *,
        attempt_id,
        request,
        transport,
        intent_hash="ih",
        submission_scope=None,
    ):
        return dispatcher.dispatch(
            attempt_id=attempt_id,
            intent_id="economic-intent-1",
            intent_hash=intent_hash,
            provider="sim",
            request=request,
            now="2026-10-05T08:00:00Z",
            authority_check=self.authority,
            transport_send=transport,
            submission_scope=submission_scope,
        )

    def seed_legacy_attempt(
        self,
        dispatcher,
        *,
        attempt_id,
        request,
        terminal,
        intent_hash="ih",
        submission_scope=None,
    ):
        scope = {} if submission_scope is None else dict(submission_scope)
        request_text = canonical_json(dict(request))
        request_hash = "sha256:" + sha256(request_text.encode("utf-8")).hexdigest()
        scope_text = canonical_json(scope)
        scope_hash = "sha256:" + sha256(scope_text.encode("utf-8")).hexdigest()
        client_order_id = stable_client_order_id(
            "sim",
            "economic-intent-1",
            environment="SIMULATION",
            account_id="acct",
        )
        dispatcher._append(
            attempt_id=attempt_id,
            event_type="SubmissionPrepared",
            version=1,
            payload={
                "attempt_id": attempt_id,
                "intent_id": "economic-intent-1",
                "intent_hash": intent_hash,
                "provider": "sim",
                "request_hash": request_hash,
                "client_order_id": client_order_id,
                "environment": "SIMULATION",
                "account_id": "acct",
                "owner_token": dispatcher.owner_token,
                "owner_epoch": dispatcher.owner_epoch,
                "prepared_at": "2026-10-05T08:00:00Z",
                "submission_scope": scope,
                "submission_scope_hash": scope_hash,
            },
            now="2026-10-05T08:00:00Z",
        )
        if terminal == "SubmissionBlocked":
            dispatcher._append(
                attempt_id=attempt_id,
                event_type=terminal,
                version=2,
                payload={"client_order_id": client_order_id, "reason": "legacy_block"},
                now="2026-10-05T08:00:01Z",
            )
            return client_order_id
        dispatcher._append(
            attempt_id=attempt_id,
            event_type="SubmissionSending",
            version=2,
            payload={
                "client_order_id": client_order_id,
                "reason": "legacy_final_send_barrier_passed",
            },
            now="2026-10-05T08:00:01Z",
        )
        if terminal == "SubmissionSent":
            dispatcher._append(
                attempt_id=attempt_id,
                event_type=terminal,
                version=3,
                payload={
                    "client_order_id": client_order_id,
                    "response": {"provider_order_id": "legacy-provider-order"},
                },
                now="2026-10-05T08:00:02Z",
            )
        elif terminal == "SubmissionUnknown":
            dispatcher._append(
                attempt_id=attempt_id,
                event_type=terminal,
                version=3,
                payload={
                    "client_order_id": client_order_id,
                    "reason": "legacy_ambiguous_send",
                },
                now="2026-10-05T08:00:02Z",
            )
        elif terminal != "SubmissionSending":
            raise AssertionError("unsupported legacy terminal")
        return client_order_id

    def test_financial_send_identity_rejects_polymorphic_text_before_callbacks(self):
        evil = ExplodingText("sim")
        with self.assertRaises(ValueError):
            stable_client_order_id(
                evil,
                "economic-intent-1",
                environment="SIMULATION",
                account_id="acct",
            )
        with self.assertRaises(ValueError):
            submission_attempt_aggregate_id(
                environment="SIMULATION",
                account_id="acct",
                attempt_id=ExplodingText("attempt"),
            )
        with self.assertRaises(ValueError):
            submission_intent_aggregate_id(
                provider=evil,
                environment="SIMULATION",
                account_id="acct",
                intent_id="economic-intent-1",
            )

        with TemporaryDirectory() as directory:
            store = self.store(directory)
            with self.assertRaises(ValueError):
                GuardedDispatcher(
                    store,
                    environment=ExplodingText("SIMULATION"),
                    account_id="acct",
                    owner_token="owner",
                )

            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            with self.assertRaises(ValueError):
                dispatcher.dispatch(
                    attempt_id="attempt",
                    intent_id="economic-intent-1",
                    intent_hash="ih",
                    provider=evil,
                    request={"qty": "1"},
                    now="2026-10-05T08:00:00Z",
                    authority_check=self.authority,
                    transport_send=lambda *_args: self.fail(
                        "polymorphic provider reached transport"
                    ),
                )
            with self.assertRaises(ValueError):
                dispatcher.dispatch(
                    attempt_id="attempt",
                    intent_id="economic-intent-1",
                    intent_hash="ih",
                    provider="sim",
                    request={"qty": "1"},
                    now=ExplodingText("2026-10-05T08:00:00Z"),
                    authority_check=self.authority,
                    transport_send=lambda *_args: self.fail(
                        "polymorphic clock reached transport"
                    ),
                )


    def test_pre_fence_sent_attempt_replays_without_provider_send(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            dispatcher = GuardedDispatcher(
                store, environment="SIMULATION", account_id="acct", owner_token="owner"
            )
            self.seed_legacy_attempt(
                dispatcher,
                attempt_id="legacy-sent",
                request={"qty": "1"},
                terminal="SubmissionSent",
            )
            result = self.dispatch(
                dispatcher,
                attempt_id="post-upgrade",
                request={"qty": "1"},
                transport=lambda *_args: self.fail("legacy SENT replay reached provider"),
            )
            self.assertEqual(result.status, "SENT")
            self.assertEqual(
                result.response, {"provider_order_id": "legacy-provider-order"}
            )

    def test_pre_fence_sending_attempt_becomes_unknown_without_second_send(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            dispatcher = GuardedDispatcher(
                store, environment="SIMULATION", account_id="acct", owner_token="owner"
            )
            self.seed_legacy_attempt(
                dispatcher,
                attempt_id="legacy-sending",
                request={"qty": "1"},
                terminal="SubmissionSending",
            )
            result = self.dispatch(
                dispatcher,
                attempt_id="post-upgrade",
                request={"qty": "1"},
                transport=lambda *_args: self.fail("legacy SENDING replay reached provider"),
            )
            self.assertEqual(result.status, "UNKNOWN")
            events = store.load_events(
                "submission_attempt", dispatcher._aggregate_id("legacy-sending")
            )
            self.assertEqual(events[-1]["event_type"], "SubmissionUnknown")

    def test_pre_fence_blocked_attempt_does_not_consume_send_ownership(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            dispatcher = GuardedDispatcher(
                store, environment="SIMULATION", account_id="acct", owner_token="owner"
            )
            self.seed_legacy_attempt(
                dispatcher,
                attempt_id="legacy-blocked",
                request={"qty": "1"},
                terminal="SubmissionBlocked",
            )
            sends = []

            def transport(client_id, request, final_guard):
                final_guard()
                sends.append((client_id, dict(request)))
                return {"provider_order_id": "new-provider-order"}

            result = self.dispatch(
                dispatcher,
                attempt_id="post-upgrade",
                request={"qty": "1"},
                transport=transport,
            )
            self.assertEqual(result.status, "SENT")
            self.assertEqual(len(sends), 1)

    def test_pre_fence_same_intent_with_divergent_request_fails_closed(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            dispatcher = GuardedDispatcher(
                store, environment="SIMULATION", account_id="acct", owner_token="owner"
            )
            self.seed_legacy_attempt(
                dispatcher,
                attempt_id="legacy-sent",
                request={"qty": "1"},
                terminal="SubmissionSent",
            )
            with self.assertRaisesRegex(
                ValueError, "historical submission content"
            ):
                self.dispatch(
                    dispatcher,
                    attempt_id="post-upgrade",
                    request={"qty": "2"},
                    transport=lambda *_args: self.fail("divergent legacy replay reached provider"),
                )

    def test_pre_fence_provider_case_alias_cannot_bypass_history(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            dispatcher = GuardedDispatcher(
                store, environment="SIMULATION", account_id="acct", owner_token="owner"
            )
            self.seed_legacy_attempt(
                dispatcher,
                attempt_id="legacy-provider-case",
                request={"qty": "1"},
                terminal="SubmissionSent",
            )
            with self.assertRaisesRegex(
                ValueError, "historical submission content"
            ):
                dispatcher.dispatch(
                    attempt_id="post-upgrade",
                    intent_id="economic-intent-1",
                    intent_hash="ih",
                    provider="SIM",
                    request={"qty": "1"},
                    now="2026-10-05T08:00:00Z",
                    authority_check=self.authority,
                    transport_send=lambda *_args: self.fail(
                        "provider-case alias replay reached provider"
                    ),
                )

    def test_multiple_pre_fence_nonblocked_attempts_fail_closed(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            dispatcher = GuardedDispatcher(
                store, environment="SIMULATION", account_id="acct", owner_token="owner"
            )
            for attempt_id in ("legacy-a", "legacy-b"):
                self.seed_legacy_attempt(
                    dispatcher,
                    attempt_id=attempt_id,
                    request={"qty": "1"},
                    terminal="SubmissionSent",
                )
            with self.assertRaisesRegex(
                RuntimeError, "multiple durable non-blocked submission attempts"
            ):
                self.dispatch(
                    dispatcher,
                    attempt_id="post-upgrade",
                    request={"qty": "1"},
                    transport=lambda *_args: self.fail("ambiguous legacy history reached provider"),
                )

    def test_success_replay_with_new_attempt_never_sends_twice(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            dispatcher = GuardedDispatcher(
                store, environment="SIMULATION", account_id="acct", owner_token="owner"
            )
            sends = []

            def transport(client_id, request, final_guard):
                final_guard()
                sends.append((client_id, dict(request)))
                return {"provider_order_id": "provider-1"}

            first = self.dispatch(
                dispatcher, attempt_id="attempt-1", request={"side": "BUY"}, transport=transport
            )
            second = self.dispatch(
                dispatcher,
                attempt_id="attempt-2",
                request={"side": "BUY"},
                transport=lambda *_args: self.fail("replay reached provider transport"),
            )

            self.assertEqual(first.status, "SENT")
            self.assertEqual(second.status, "SENT")
            self.assertEqual(second.response, first.response)
            self.assertEqual(len(sends), 1)
            self.assertEqual(second.client_order_id, first.client_order_id)

    def test_process_death_after_barrier_blocks_fresh_attempt_send(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            dispatcher = GuardedDispatcher(
                store, environment="SIMULATION", account_id="acct", owner_token="owner"
            )
            sends = []

            def dying_transport(client_id, request, final_guard):
                final_guard()
                sends.append(client_id)
                raise SimulatedProcessDeath()

            with self.assertRaises(SimulatedProcessDeath):
                self.dispatch(
                    dispatcher,
                    attempt_id="attempt-crash",
                    request={"side": "SELL"},
                    transport=dying_transport,
                )

            replay = self.dispatch(
                dispatcher,
                attempt_id="attempt-after-restart",
                request={"side": "SELL"},
                transport=lambda *_args: self.fail("ambiguous replay reached provider transport"),
            )
            self.assertEqual(replay.status, "UNKNOWN")
            self.assertEqual(replay.reason, "intent_send_already_committed")
            self.assertEqual(len(sends), 1)

    def test_unknown_terminal_replays_without_second_send(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            dispatcher = GuardedDispatcher(
                store, environment="SIMULATION", account_id="acct", owner_token="owner"
            )
            sends = []

            def ambiguous_transport(client_id, request, final_guard):
                final_guard()
                sends.append(client_id)
                raise TimeoutError("provider response lost")

            first = self.dispatch(
                dispatcher,
                attempt_id="attempt-unknown",
                request={"qty": "1"},
                transport=ambiguous_transport,
            )
            replay = self.dispatch(
                dispatcher,
                attempt_id="attempt-replay",
                request={"qty": "1"},
                transport=lambda *_args: self.fail("unknown replay reached provider transport"),
            )
            self.assertEqual(first.status, "UNKNOWN")
            self.assertEqual(replay.status, "UNKNOWN")
            self.assertEqual(len(sends), 1)

    def test_same_intent_with_different_economic_request_fails_closed(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            dispatcher = GuardedDispatcher(
                store, environment="SIMULATION", account_id="acct", owner_token="owner"
            )

            def transport(_client_id, _request, final_guard):
                final_guard()
                return {"provider_order_id": "provider-1"}

            self.dispatch(
                dispatcher,
                attempt_id="attempt-original",
                request={"qty": "1"},
                transport=transport,
            )
            with self.assertRaisesRegex(ValueError, "intent_id conflicts"):
                self.dispatch(
                    dispatcher,
                    attempt_id="attempt-divergent",
                    request={"qty": "2"},
                    transport=lambda *_args: self.fail("divergent replay reached provider"),
                )

    def test_same_intent_with_different_submission_scope_fails_closed(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            dispatcher = GuardedDispatcher(
                store, environment="SIMULATION", account_id="acct", owner_token="owner"
            )

            def transport(_client_id, _request, final_guard):
                final_guard()
                return {"provider_order_id": "provider-1"}

            self.dispatch(
                dispatcher,
                attempt_id="attempt-scope-original",
                request={"qty": "1"},
                submission_scope={"risk_epoch": "1", "strategy": "alpha"},
                transport=transport,
            )
            with self.assertRaisesRegex(ValueError, "intent_id conflicts"):
                self.dispatch(
                    dispatcher,
                    attempt_id="attempt-scope-divergent",
                    request={"qty": "1"},
                    submission_scope={"risk_epoch": "2", "strategy": "alpha"},
                    transport=lambda *_args: self.fail("scope-divergent replay reached provider"),
                )

    def test_binding_is_durable_and_atomic_with_send_barrier(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            dispatcher = GuardedDispatcher(
                store, environment="SIMULATION", account_id="acct", owner_token="owner"
            )

            def dying_transport(_client_id, _request, final_guard):
                final_guard()
                raise SimulatedProcessDeath()

            with self.assertRaises(SimulatedProcessDeath):
                self.dispatch(
                    dispatcher,
                    attempt_id="attempt-atomic",
                    request={"symbol": "BTCUSD"},
                    submission_scope={"risk_epoch": "7"},
                    transport=dying_transport,
                )

            aggregate_id = submission_intent_aggregate_id(
                provider="sim",
                environment="SIMULATION",
                account_id="acct",
                intent_id="economic-intent-1",
            )
            binding = store.load_events("submission_intent", aggregate_id)
            attempt = store.load_events(
                "submission_attempt", dispatcher._aggregate_id("attempt-atomic")
            )
            self.assertEqual([event["event_type"] for event in binding], ["SubmissionIntentBound"])
            self.assertEqual(binding[0]["payload"]["attempt_id"], "attempt-atomic")
            self.assertEqual(
                binding[0]["payload"]["submission_scope_hash"],
                attempt[0]["payload"]["submission_scope_hash"],
            )
            self.assertEqual(
                [event["event_type"] for event in attempt],
                ["SubmissionPrepared", "SubmissionSending"],
            )

    def test_provider_scope_has_independent_intent_fences(self):
        first = submission_intent_aggregate_id(
            provider="sim", environment="LIVE", account_id="acct", intent_id="i"
        )
        second = submission_intent_aggregate_id(
            provider="other", environment="LIVE", account_id="acct", intent_id="i"
        )
        paper = submission_intent_aggregate_id(
            provider="sim", environment="PAPER", account_id="acct", intent_id="i"
        )
        account = submission_intent_aggregate_id(
            provider="sim", environment="LIVE", account_id="other", intent_id="i"
        )
        self.assertEqual(len({first, second, paper, account}), 4)


if __name__ == "__main__":
    unittest.main()
