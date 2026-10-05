from pathlib import Path

DISPATCH = Path("mvp/autotrade_mvp/dispatch.py")
TEST = Path("mvp/tests/test_dispatch_intent_fence.py")

text = DISPATCH.read_text(encoding="utf-8")


def replace_once(old: str, new: str) -> None:
    global text
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected exactly one patch anchor, found {count}: {old[:80]!r}")
    text = text.replace(old, new, 1)


replace_once(
'''def _canonical_journal_authority_snapshot(
''',
'''def submission_intent_aggregate_id(
    *,
    provider: str,
    environment: str,
    account_id: str,
    intent_id: str,
) -> str:
    """Return one durable financial-send identity for one economic intent."""

    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("provider is required")
    normalized_environment = (
        environment.strip().upper() if isinstance(environment, str) else ""
    )
    if normalized_environment not in {"REPLAY", "SIMULATION", "PAPER", "LIVE"}:
        raise ValueError("environment must be REPLAY, SIMULATION, PAPER, or LIVE")
    if not isinstance(account_id, str) or not account_id.strip():
        raise ValueError("account_id is required")
    if not isinstance(intent_id, str) or not intent_id.strip():
        raise ValueError("intent_id is required")
    return "submission-intent:" + _identity_digest(
        provider.strip().lower(),
        normalized_environment,
        account_id.strip(),
        intent_id.strip(),
    )


def _canonical_journal_authority_snapshot(
''')

replace_once(
'''        expected_journal_sequence: int | None = None,
        _journal_commit_command: Callable[..., object] | None = None,
    ):
''',
'''        expected_journal_sequence: int | None = None,
        _journal_commit_command: Callable[..., object] | None = None,
        co_events: list[tuple[dict[str, Any], str]] | None = None,
    ):
''')

replace_once(
'''        if expected_journal_sequence is None:
            return JournalStore.append_event(
                store,
                envelope,
                outbox_topic="autotrade.submission.events",
            )
''',
'''        if expected_journal_sequence is None:
            if co_events:
                raise ValueError("co_events require the journal-cut send barrier")
            return JournalStore.append_event(
                store,
                envelope,
                outbox_topic="autotrade.submission.events",
            )
''')

replace_once(
'''            events=[(envelope, "autotrade.submission.events")],
            expected_journal_sequence=expected_journal_sequence,
''',
'''            events=[
                (envelope, "autotrade.submission.events"),
                *(co_events or []),
            ],
            expected_journal_sequence=expected_journal_sequence,
''')

replace_once(
'''        existing = self._events(attempt_id)
        if existing:
            prepared = existing[0]["payload"]
''',
'''        intent_aggregate_id = submission_intent_aggregate_id(
            provider=provider,
            environment=self.environment,
            account_id=self.account_id,
            intent_id=intent_id,
        )

        existing = self._events(attempt_id)
        if existing:
            prepared = existing[0]["payload"]
''')

replace_once(
'''            return self._recover_existing(
                attempt_id=attempt_id,
                client_order_id=client_order_id,
                now=now,
            )

        prepared_payload = {
''',
'''            return self._recover_existing(
                attempt_id=attempt_id,
                client_order_id=client_order_id,
                now=now,
            )

        # A stable provider client ID is necessary but not sufficient: provider
        # duplicate-ID behavior cannot be the authority for money movement. The
        # canonical intent aggregate makes the first durable send owner unique
        # across process restarts and across fresh attempt_id values.
        store = self._journal_store_authority()
        intent_events = JournalStore.load_events(
            store,
            "submission_intent",
            intent_aggregate_id,
        )
        if intent_events:
            if (
                len(intent_events) != 1
                or intent_events[0].get("event_type") != "SubmissionIntentBound"
            ):
                raise RuntimeError("durable submission intent binding is invalid")
            binding = intent_events[0].get("payload")
            if not isinstance(binding, dict):
                raise RuntimeError("durable submission intent binding payload is invalid")
            expected_binding = {
                "provider": provider,
                "environment": self.environment,
                "account_id": self.account_id,
                "intent_id": intent_id,
                "intent_hash": intent_hash,
                "request_hash": request_hash,
                "client_order_id": client_order_id,
                "submission_scope_hash": submission_scope_hash,
            }
            if any(binding.get(key) != value for key, value in expected_binding.items()):
                raise ValueError("intent_id conflicts with existing submission content")
            canonical_attempt_id = binding.get("attempt_id")
            if not isinstance(canonical_attempt_id, str) or not canonical_attempt_id:
                raise RuntimeError("durable submission intent owner is invalid")
            canonical_events = self._events(canonical_attempt_id)
            if not canonical_events:
                raise RuntimeError("submission intent binding lost its canonical attempt")
            canonical_last = canonical_events[-1]
            if canonical_last["event_type"] in {"SubmissionSent", "SubmissionUnknown"}:
                return self._outcome_from_terminal(canonical_last, client_order_id)
            # SubmissionIntentBound and SubmissionSending are committed in the
            # same SQLite transaction below. Seeing the binding therefore means
            # an irreversible send may already be in flight; never send again.
            if canonical_last["event_type"] == "SubmissionSending":
                return DispatchOutcome(
                    "UNKNOWN",
                    client_order_id,
                    None,
                    "intent_send_already_committed",
                )
            raise RuntimeError(
                "submission intent binding has no irreversible send evidence"
            )

        prepared_payload = {
''')

replace_once(
'''            try:
                self._append(
                    attempt_id=attempt_id,
                    event_type="SubmissionSending",
''',
'''            try:
                intent_binding = _envelope(
                    scope_key=self.scope_key,
                    aggregate_id=intent_aggregate_id,
                    environment=self.environment,
                    attempt_id=attempt_id,
                    event_type="SubmissionIntentBound",
                    version=1,
                    payload={
                        "attempt_id": attempt_id,
                        "provider": provider,
                        "environment": self.environment,
                        "account_id": self.account_id,
                        "intent_id": intent_id,
                        "intent_hash": intent_hash,
                        "request_hash": request_hash,
                        "client_order_id": client_order_id,
                        "submission_scope_hash": submission_scope_hash,
                        "bound_at": _instant(barrier_now).isoformat().replace("+00:00", "Z"),
                    },
                    now=barrier_now,
                    owner_epoch=self.owner_epoch,
                )
                intent_binding["aggregate_type"] = "submission_intent"
                self._append(
                    attempt_id=attempt_id,
                    event_type="SubmissionSending",
''')

replace_once(
'''                    expected_journal_sequence=barrier_journal_sequence,
                    _journal_commit_command=journal_commit_command,
                )
''',
'''                    expected_journal_sequence=barrier_journal_sequence,
                    _journal_commit_command=journal_commit_command,
                    co_events=[
                        (intent_binding, "autotrade.submission.intent.events"),
                    ],
                )
''')

DISPATCH.write_text(text, encoding="utf-8")

TEST.write_text(
'''from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.dispatch import (
    GuardedDispatcher,
    submission_intent_aggregate_id,
)
from mvp.autotrade_mvp.persistence import JournalStore


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
''',
    encoding="utf-8",
)
