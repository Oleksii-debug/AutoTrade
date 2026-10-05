from decimal import Decimal, ROUND_DOWN, ROUND_HALF_EVEN, ROUND_UP, localcontext
import unittest
from uuid import NAMESPACE_URL, uuid5

from mvp.autotrade_mvp.bounded_real import (
    BoundedRealEnvelope,
    ImmutableEvidenceRef,
)


SHA = "a" * 40


def _envelope() -> BoundedRealEnvelope:
    return BoundedRealEnvelope.create(
        envelope_id="bounded-context-independent",
        source_sha=SHA,
        account_id="account-context-independent",
        provider_id="provider-context-independent",
        policy_id="policy-context-independent",
        allowed_actions={"ORDER.SUBMIT", "ORDER.CANCEL", "FLATTEN"},
        max_capital=Decimal("12345678901234567890.1234567890123456789"),
        max_single_notional=Decimal("1234567890123456789.01234567890123456789"),
        max_gross_leverage=Decimal("1.23456789012345678901234567890123456789"),
    )


class BoundedRealDecimalIdentityTests(unittest.TestCase):
    def test_envelope_digest_and_signed_scope_ignore_ambient_decimal_context(self):
        identities = set()
        requirements = set()
        evidence_scopes = set()

        for precision in (6, 10, 28, 80):
            for rounding in (ROUND_DOWN, ROUND_UP, ROUND_HALF_EVEN):
                with self.subTest(precision=precision, rounding=rounding):
                    with localcontext() as context:
                        context.prec = precision
                        context.rounding = rounding
                        bounded = _envelope()
                        digest = bounded.envelope_digest
                        identities.add(digest)
                        requirements.add(f"envelope/{digest}")
                        evidence_ref = ImmutableEvidenceRef(
                            artifact_id=str(
                                uuid5(
                                    NAMESPACE_URL,
                                    "autotrade:bounded-context-independent",
                                )
                            ),
                            sha256="sha256:" + "1" * 64,
                            evidence_kind="ACTUAL_FILL",
                            source_sha=SHA,
                            envelope_id=bounded.envelope_id,
                            envelope_digest=digest,
                            provider_id=bounded.provider_id,
                            account_id=bounded.account_id,
                        )
                        evidence_scopes.add(
                            (
                                evidence_ref.envelope_id,
                                evidence_ref.envelope_digest,
                                evidence_ref.provider_id,
                                evidence_ref.account_id,
                            )
                        )

        self.assertEqual(len(identities), 1)
        self.assertEqual(len(requirements), 1)
        self.assertEqual(len(evidence_scopes), 1)

    def test_over_envelope_risk_limit_fails_before_identity_is_emitted(self):
        with self.assertRaisesRegex(ValueError, "resource envelope"):
            BoundedRealEnvelope.create(
                envelope_id="bounded-over-envelope",
                source_sha=SHA,
                account_id="account-1",
                provider_id="provider-1",
                policy_id="policy-1",
                allowed_actions={"ORDER.SUBMIT"},
                max_capital=Decimal("1" * 257),
                max_single_notional=Decimal("1"),
                max_gross_leverage=Decimal("1"),
            )


if __name__ == "__main__":
    unittest.main()
