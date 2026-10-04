import unittest
from decimal import Decimal

from mvp.autotrade_mvp import authority as authority_module
from mvp.autotrade_mvp.authority import (
    AdmissionRecord,
    AuthorityPolicy,
    InstrumentVersionIdentity,
)


INSTRUMENT_ID = "11111111-1111-4111-8111-111111111111"


class AuthorityTrustIngressTests(unittest.TestCase):
    def policy_kwargs(self):
        return dict(
            policy_id="policy-1",
            account_id="account-1",
            environments={"SIMULATION"},
            instruments={(INSTRUMENT_ID, 1)},
            actions={"ORDER.SUBMIT"},
            max_notional="1000",
            valid_from="2026-10-04T00:00:00Z",
            expires_at="2026-10-05T00:00:00Z",
            autonomous=True,
            protection_only=False,
        )

    def test_text_subclass_is_rejected_before_virtual_strip(self):
        calls = []

        class HostileText(str):
            def strip(self, *args, **kwargs):
                calls.append("strip")
                raise AssertionError("hostile strip executed")

        values = self.policy_kwargs()
        values["policy_id"] = HostileText("policy-1")
        with self.assertRaisesRegex(TypeError, "policy_id must be exact text"):
            AuthorityPolicy.create(**values)
        self.assertEqual(calls, [])

    def test_decimal_subclass_is_rejected_before_decimal_callbacks(self):
        calls = []

        class HostileDecimal(Decimal):
            def is_finite(self):
                calls.append("is_finite")
                raise AssertionError("hostile Decimal callback executed")

            def as_tuple(self):
                calls.append("as_tuple")
                raise AssertionError("hostile Decimal callback executed")

        values = self.policy_kwargs()
        values["max_notional"] = HostileDecimal("1000")
        with self.assertRaisesRegex(TypeError, "max_notional must use exact Decimal"):
            AuthorityPolicy.create(**values)
        self.assertEqual(calls, [])

    def test_collection_subclass_is_rejected_before_iteration(self):
        calls = []

        class HostileSet(set):
            def __iter__(self):
                calls.append("iter")
                raise AssertionError("hostile collection iteration")

        values = self.policy_kwargs()
        values["environments"] = HostileSet({"SIMULATION"})
        with self.assertRaisesRegex(TypeError, "environments must be an exact built-in collection"):
            AuthorityPolicy.create(**values)
        self.assertEqual(calls, [])

    def test_instrument_identity_subclass_is_rejected_before_field_reads(self):
        calls = []

        class HostileIdentity(InstrumentVersionIdentity):
            def __getattribute__(self, name):
                if name in {"instrument_id", "version"}:
                    calls.append(name)
                    raise AssertionError("hostile identity field read")
                return super().__getattribute__(name)

        forged = object.__new__(HostileIdentity)
        with self.assertRaisesRegex(TypeError, "must be InstrumentVersionIdentity"):
            authority_module._instrument_identity(forged)
        self.assertEqual(calls, [])

    def test_instrument_version_integer_subclass_is_rejected(self):
        class HostileInt(int):
            def __lt__(self, other):
                raise AssertionError("hostile comparison executed")

        with self.assertRaisesRegex(ValueError, "instrument_version must be a positive integer"):
            InstrumentVersionIdentity(INSTRUMENT_ID, HostileInt(1))

    def test_admission_integer_subclass_is_rejected_before_comparison(self):
        class HostileInt(int):
            def __lt__(self, other):
                raise AssertionError("hostile comparison executed")

        with self.assertRaisesRegex(ValueError, "admission state_version is invalid"):
            AdmissionRecord(
                admission_id="a1",
                policy_id="p1",
                intent_hash="h1",
                account_id="account-1",
                environment="SIMULATION",
                instrument_version=InstrumentVersionIdentity(INSTRUMENT_ID, 1),
                action="ORDER.SUBMIT",
                notional="1",
                risk_reducing=True,
                state_version=HostileInt(1),
                authority_epoch=1,
                outcome="REJECTED",
                admitted_at="2026-10-04T01:00:00Z",
                confirmation_id=None,
                reason="test",
                request_fingerprint="0" * 64,
            )


if __name__ == "__main__":
    unittest.main()
