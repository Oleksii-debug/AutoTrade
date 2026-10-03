import json
import unittest

from mvp.autotrade_mvp.runtime_target_host_durable_financial import (
    CLOCK_CONTRACT_ID,
    DurableFinancialIdentityBinding,
    DurableTargetHostFinancialBinding,
    RuntimeTargetHostDurableFinancialError,
)
from mvp.autotrade_mvp.runtime_target_host_durable_financial_codec import (
    parse_durable_target_host_financial_binding,
)


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
DIGEST_D = "sha256:" + "d" * 64


def binding() -> DurableTargetHostFinancialBinding:
    identity = DurableFinancialIdentityBinding(
        event_id="financial-1",
        event_journal_sequence=7,
        event_payload_hash=DIGEST_A,
        latency_measurement_event_id="latency-financial-1",
        latency_measurement_journal_sequence=8,
        durable_latency_sample_digest=DIGEST_B,
        latency_start_monotonic_ns=100_000,
        latency_end_monotonic_ns=101_500,
        latency_us=2,
        target_sample_id="target-financial-1",
    )
    return DurableTargetHostFinancialBinding(
        target_host_measurement_digest=DIGEST_C,
        source_sha="e" * 40,
        spec_digest=DIGEST_D,
        declared_plan_id="declared-plan-1",
        declared_plan_digest=DIGEST_A,
        clock_contract_id=CLOCK_CONTRACT_ID,
        bindings=(identity,),
    )


class RuntimeTargetHostDurableFinancialCodecTests(unittest.TestCase):
    def test_canonical_binding_round_trips_exact_bytes(self):
        value = binding()
        parsed = parse_durable_target_host_financial_binding(value.canonical_bytes())
        self.assertEqual(parsed, value)
        self.assertEqual(parsed.digest, value.digest)

    def test_trailing_whitespace_is_not_canonical(self):
        with self.assertRaisesRegex(
            RuntimeTargetHostDurableFinancialError,
            "not canonical JSON",
        ):
            parse_durable_target_host_financial_binding(
                binding().canonical_bytes() + b"\n"
            )

    def test_duplicate_json_key_is_rejected(self):
        raw = binding().canonical_bytes()
        duplicate = raw[:-1] + b',"schema_version":"1.0.0"}'
        with self.assertRaisesRegex(
            RuntimeTargetHostDurableFinancialError,
            "duplicate JSON object key",
        ):
            parse_durable_target_host_financial_binding(duplicate)

    def test_unknown_nested_identity_field_is_rejected(self):
        payload = json.loads(binding().canonical_bytes().decode("utf-8"))
        payload["bindings"][0]["invented"] = "authority-bypass"
        raw = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        with self.assertRaisesRegex(
            RuntimeTargetHostDurableFinancialError,
            "identity binding fields are non-canonical",
        ):
            parse_durable_target_host_financial_binding(raw)

    def test_nonfinite_json_constant_is_rejected(self):
        raw = binding().canonical_bytes()
        raw = raw.replace(b'"event_journal_sequence":7', b'"event_journal_sequence":NaN')
        with self.assertRaisesRegex(
            RuntimeTargetHostDurableFinancialError,
            "invalid JSON constant",
        ):
            parse_durable_target_host_financial_binding(raw)


if __name__ == "__main__":
    unittest.main()
