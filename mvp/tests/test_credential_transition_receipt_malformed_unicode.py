from __future__ import annotations

import unittest

from mvp.autotrade_mvp import credential_transition_receipt as transition
from mvp.autotrade_mvp.credential_transition_receipt import (
    CredentialTransitionReceiptError,
)


class CredentialTransitionReceiptMalformedUnicodeTests(unittest.TestCase):
    def test_retained_lone_surrogate_is_domain_integrity_failure(self) -> None:
        stored = {
            "schema_version": "2.0.0",
            "receipt_id": "credential-transition/sha256:" + "0" * 64,
            "operation": "ROTATED",
            "handle_id": "credential",
            "account_id": "\ud800",
            "provider": "SIMULATED",
            "environment": "PAPER",
            "provider_environment": "SIMULATED",
            "purpose": "TRADE",
            "prior_generation": 1,
            "successor_generation": 2,
            "active_after": True,
            "owner_identity_sha256": "sha256:" + "1" * 64,
            "vault_authority_sha256": "sha256:" + "2" * 64,
            "record_state_sha256": "sha256:" + "3" * 64,
            "previous_receipt_id": None,
            "transition_sequence": 1,
            "completed_time_ns": 1,
        }

        with self.assertRaisesRegex(
            CredentialTransitionReceiptError,
            "stored credential transition receipt content identity is invalid",
        ):
            transition._parse_receipt(stored)


if __name__ == "__main__":
    unittest.main()
