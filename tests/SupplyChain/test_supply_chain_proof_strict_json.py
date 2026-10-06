from __future__ import annotations

import unittest

from mvp.autotrade_mvp.supply_chain_qualification import (
    parse_supply_chain_proof_bytes,
)


class SupplyChainProofStrictJsonTests(unittest.TestCase):
    def test_duplicate_object_key_is_rejected_before_semantic_parse(self):
        raw = (
            b'{"schema_version":"1.0.0","schema_version":"1.0.0",'
            b'"evidence":{},"receipt":{}}'
        )
        with self.assertRaisesRegex(ValueError, "duplicate JSON object key"):
            parse_supply_chain_proof_bytes(raw)

    def test_excessive_nesting_is_rejected_before_json_decoder_recursion(self):
        raw = (b"[" * 129) + b"0" + (b"]" * 129)
        with self.assertRaisesRegex(ValueError, "JSON nesting exceeds 128"):
            parse_supply_chain_proof_bytes(raw)

    def test_oversized_document_is_rejected_by_shared_resource_fence(self):
        raw = (b" " * 1_000_001) + b"{}"
        with self.assertRaisesRegex(ValueError, "JSON document exceeds 1000000"):
            parse_supply_chain_proof_bytes(raw)

    def test_huge_integer_is_rejected_before_unbounded_integer_materialization(self):
        raw = b'{"value":' + (b"1" * 641) + b"}"
        with self.assertRaisesRegex(ValueError, "JSON integer exceeds 640 digits"):
            parse_supply_chain_proof_bytes(raw)

    def test_non_finite_constant_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "non-finite JSON constant"):
            parse_supply_chain_proof_bytes(b'{"value":NaN}')


if __name__ == "__main__":
    unittest.main()
