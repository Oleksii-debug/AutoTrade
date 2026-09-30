from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.dispatch import (
    GuardedDispatcher,
    PreparedSubmissionAuthorityCheck,
)
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest


class ForgedPreparedAuthority(PreparedSubmissionAuthorityCheck):
    def __call__(self, _intent, _now, _scope, _scope_hash):
        return True, "forged-subclass"


class DelegatingPreparedAuthority:
    def __init__(self, wrapped):
        self.wrapped = wrapped

    def __call__(self, intent, now, scope, scope_hash):
        return self.wrapped(intent, now, scope, scope_hash)


class PreparedAuthorityProvenanceTests(unittest.TestCase):
    def test_paper_rejects_direct_subclass_and_proxy_prepared_authority(self):
        for name, forged in (
            (
                "direct",
                PreparedSubmissionAuthorityCheck(
                    lambda _intent, _now, _scope, _scope_hash: (True, "forged")
                ),
            ),
            (
                "subclass",
                ForgedPreparedAuthority(
                    lambda _intent, _now, _scope, _scope_hash: (True, "forged")
                ),
            ),
            (
                "proxy",
                DelegatingPreparedAuthority(
                    PreparedSubmissionAuthorityCheck(
                        lambda _intent, _now, _scope, _scope_hash: (True, "forged")
                    )
                ),
            ),
        ):
            with self.subTest(name=name), TemporaryDirectory() as directory:
                store = JournalStore(f"{directory}/journal.sqlite3")
                dispatcher = GuardedDispatcher(
                    store,
                    environment="PAPER",
                    account_id="acct",
                    owner_token="owner",
                )
                request = {"side": "BUY", "quantity": "1"}
                scope = {
                    "provider": "SIM",
                    "provider_environment": "MAINNET",
                    "account_id": "acct",
                    "environment": "PAPER",
                    "prepared_request_sha256": payload_digest(request),
                }
                outbound = 0

                def transport(_client_id, _request, _guard):
                    nonlocal outbound
                    outbound += 1
                    raise AssertionError("forged financial authority reached transport")

                result = dispatcher.dispatch(
                    attempt_id=f"forged-{name}",
                    intent_id=f"intent-{name}",
                    intent_hash=f"hash-{name}",
                    provider="SIM",
                    request=request,
                    now="2026-09-30T01:20:00Z",
                    authority_check=forged,
                    transport_send=transport,
                    sender_check=lambda _owner, _epoch: None,
                    submission_scope=scope,
                )
                self.assertEqual(result.status, "BLOCKED")
                self.assertEqual(result.reason, "prepared_scope_authority_required")
                self.assertEqual(outbound, 0)
                events = store.load_events(
                    "submission_attempt",
                    dispatcher._aggregate_id(f"forged-{name}"),
                )
                self.assertEqual(
                    [event["event_type"] for event in events],
                    ["SubmissionPrepared", "SubmissionBlocked"],
                )


if __name__ == "__main__":
    unittest.main()
