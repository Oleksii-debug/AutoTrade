from __future__ import annotations

import unittest

from mvp.autotrade_mvp.provider_qualification_authority import (
    PrivateStreamSemantics,
    ProviderQualificationAuthorityError,
    ReconciliationSemantics,
    ReconciliationSurfaceSemantics,
    _parse_reconciliation,
)


def _surface() -> ReconciliationSurfaceSemantics:
    return ReconciliationSurfaceSemantics(
        surface_id="BALANCES",
        endpoint="/v5/account/wallet-balance",
        category="ACCOUNT",
        exclusion_authority=False,
        pagination_rule_id="bybit-v5-cursor",
        consistency_horizon_ms=1000,
        cache_policy_id="no-cache",
    )


class ProviderQualificationPrivateStreamSemanticsTests(unittest.TestCase):
    def test_existing_reconciliation_semantics_keep_exact_v1_identity(self):
        semantics = ReconciliationSemantics(
            generation_scheme="SERIALIZED_ACQUISITION_GENERATION",
            surfaces=(_surface(),),
        )

        self.assertEqual(
            semantics.canonical(),
            {
                "generation_scheme": "SERIALIZED_ACQUISITION_GENERATION",
                "schema_version": "1.0.0",
                "surfaces": [
                    {
                        "cache_policy_id": "no-cache",
                        "category": "ACCOUNT",
                        "consistency_horizon_ms": 1000,
                        "endpoint": "/v5/account/wallet-balance",
                        "exclusion_authority": False,
                        "pagination_rule_id": "bybit-v5-cursor",
                        "surface_id": "BALANCES",
                    }
                ],
            },
        )
        self.assertEqual(
            semantics.content_sha256,
            "sha256:63b1bee7b51bf989a879d9a239396112fd39aa94836e1fffc3e1d62308d8f6e6",
        )

    def test_stream_semantics_round_trip_parser_policy_and_recovery_identity(self):
        semantics = ReconciliationSemantics(
            generation_scheme="SERIALIZED_ACQUISITION_GENERATION",
            surfaces=(_surface(),),
            stream_topics=(
                PrivateStreamSemantics(
                    topic_id="execution",
                    parser_id="bybit-v5-private-execution",
                    parser_version="1",
                    sequence_policy="ARITHMETIC_SEQUENCE",
                    sequence_scope="account-topic",
                    recovery_method_id="snapshot-plus-backfill-v1",
                ),
            ),
        )

        payload = semantics.canonical()
        self.assertEqual(payload["schema_version"], "2.0.0")
        self.assertEqual(
            payload["stream_topics"],
            [
                {
                    "parser_id": "bybit-v5-private-execution",
                    "parser_version": "1",
                    "recovery_method_id": "snapshot-plus-backfill-v1",
                    "sequence_policy": "ARITHMETIC_SEQUENCE",
                    "sequence_scope": "account-topic",
                    "topic_id": "execution",
                }
            ],
        )
        self.assertEqual(_parse_reconciliation(payload), semantics)

    def test_no_provider_sequence_requires_none_scope(self):
        semantics = PrivateStreamSemantics(
            topic_id="trade-updates",
            parser_id="alpaca-trade-updates",
            parser_version="1",
            sequence_policy="NO_PROVIDER_SEQUENCE",
            sequence_scope="none",
            recovery_method_id="snapshot-readback-v1",
        )
        self.assertEqual(semantics.sequence_scope, "none")
        with self.assertRaises(ProviderQualificationAuthorityError):
            PrivateStreamSemantics(
                topic_id="trade-updates",
                parser_id="alpaca-trade-updates",
                parser_version="1",
                sequence_policy="NO_PROVIDER_SEQUENCE",
                sequence_scope="account",
                recovery_method_id="snapshot-readback-v1",
            )

    def test_qualified_sequence_requires_non_none_scope(self):
        for policy in ("ARITHMETIC_SEQUENCE", "MONOTONIC_NONCONTIGUOUS"):
            with self.subTest(policy=policy):
                with self.assertRaises(ProviderQualificationAuthorityError):
                    PrivateStreamSemantics(
                        topic_id="orders",
                        parser_id="private-orders",
                        parser_version="1",
                        sequence_policy=policy,
                        sequence_scope="none",
                        recovery_method_id="snapshot-readback-v1",
                    )

    def test_duplicate_topic_semantics_are_rejected(self):
        topic = PrivateStreamSemantics(
            topic_id="orders",
            parser_id="private-orders",
            parser_version="1",
            sequence_policy="MONOTONIC_NONCONTIGUOUS",
            sequence_scope="account-topic",
            recovery_method_id="snapshot-readback-v1",
        )
        with self.assertRaises(ProviderQualificationAuthorityError):
            ReconciliationSemantics(
                generation_scheme="SERIALIZED_ACQUISITION_GENERATION",
                surfaces=(_surface(),),
                stream_topics=(topic, topic),
            )

    def test_v2_stream_list_must_be_nonempty_and_exact(self):
        payload = ReconciliationSemantics(
            generation_scheme="SERIALIZED_ACQUISITION_GENERATION",
            surfaces=(_surface(),),
        ).canonical()
        payload["schema_version"] = "2.0.0"
        payload["stream_topics"] = []
        with self.assertRaises(ProviderQualificationAuthorityError):
            _parse_reconciliation(payload)


if __name__ == "__main__":
    unittest.main()
