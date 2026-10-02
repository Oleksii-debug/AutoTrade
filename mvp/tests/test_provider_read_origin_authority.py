from datetime import timedelta
from tempfile import TemporaryDirectory
import unittest

import mvp.autotrade_mvp.provider_origin as provider_origin
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_origin import (
    ProviderOriginError,
    ProviderOriginJournal,
    observe_provider_origin_json_response,
)
from mvp.tests.test_provider_transport import READ_NOW, authenticated_read_binding


class ProviderReadOriginAuthorityTests(unittest.TestCase):
    def test_same_process_private_token_cannot_mint_provider_origin(self):
        """A module-private Python object is not external provider-wire authority."""
        query = authenticated_read_binding()
        with TemporaryDirectory() as directory:
            journal = ProviderOriginJournal(
                JournalStore(f"{directory}/journal.sqlite3")
            )
            attempt_id = journal.prepare(
                query,
                transport_identity="UrllibJsonWireClient:v1",
                network_policy_identity="sha256:" + "a" * 64,
                recorded_at=READ_NOW,
            )

            # This deliberately uses only objects available to an ordinary
            # same-process caller. A secure implementation must make at least
            # one step below fail closed unless an independently authenticated
            # provider-wire issuer is present.
            with self.assertRaises((AttributeError, ProviderOriginError)):
                token = getattr(
                    provider_origin,
                    "_PROVIDER_ORIGIN_RECORD_TOKEN",
                )
                binding = journal._record_provider_origin(
                    attempt_id,
                    query,
                    http_status=200,
                    response_bytes=b'{"provider_state":"caller-authored"}',
                    observed_at=READ_NOW + timedelta(seconds=1),
                    _origin_token=token,
                )
                observe_provider_origin_json_response(
                    response_binding=binding,
                    query_binding=query,
                    accepted_success_statuses=frozenset({200}),
                )


if __name__ == "__main__":
    unittest.main()
