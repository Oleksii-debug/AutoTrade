import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.decision_trace import DecisionTraceStore, GENESIS_HASH


def trace(trace_id: str, *, decision: str = "BUY") -> dict:
    return {
        "trace_id": trace_id,
        "input_hash": "a" * 64,
        "strategy_version": "moving-average-v1",
        "decision": decision,
        "decision_reason": "fast_above_slow" if decision == "BUY" else "averages_equal",
        "risk_outcome": "admitted" if decision == "BUY" else "not_applicable",
        "order_id": "intent-1" if decision == "BUY" else None,
        "fill_id": "fill-1" if decision == "BUY" else None,
        "evidence_refs": ["evidence-1"],
    }


class DecisionTraceStoreTests(unittest.TestCase):
    def test_append_is_idempotent_and_chain_is_valid(self):
        with TemporaryDirectory() as directory:
            store = DecisionTraceStore(Path(directory) / "decision-traces.jsonl")
            self.assertTrue(store.append(trace("trace-1")))
            self.assertFalse(store.append(trace("trace-1")))
            self.assertTrue(store.verify())
            rows = store.records()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["previous_hash"], GENESIS_HASH)

    def test_conflicting_retry_fails_closed(self):
        with TemporaryDirectory() as directory:
            store = DecisionTraceStore(Path(directory) / "decision-traces.jsonl")
            store.append(trace("trace-1"))
            with self.assertRaisesRegex(ValueError, "different decision content"):
                store.append(trace("trace-1", decision="HOLD"))

    def test_multiple_records_form_hash_chain(self):
        with TemporaryDirectory() as directory:
            store = DecisionTraceStore(Path(directory) / "decision-traces.jsonl")
            store.append(trace("trace-1"))
            second = trace("trace-2", decision="HOLD")
            second["evidence_refs"] = ["evidence-2"]
            store.append(second)
            rows = store.records()
            self.assertEqual(rows[1]["previous_hash"], rows[0]["record_hash"])
            self.assertTrue(store.verify())

    def test_tampering_is_detected(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "decision-traces.jsonl"
            store = DecisionTraceStore(path)
            store.append(trace("trace-1"))
            row = json.loads(path.read_text(encoding="utf-8"))
            row["decision"] = "SELL"
            path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            self.assertFalse(store.verify())
            with self.assertRaisesRegex(ValueError, "chain is corrupt"):
                store.records()

    def test_duplicate_evidence_refs_are_rejected(self):
        with TemporaryDirectory() as directory:
            store = DecisionTraceStore(Path(directory) / "decision-traces.jsonl")
            item = trace("trace-1")
            item["evidence_refs"] = ["evidence-1", "evidence-1"]
            with self.assertRaisesRegex(ValueError, "must not contain duplicates"):
                store.append(item)

    def test_common_secret_aliases_are_redacted_before_persistence(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "decision-traces.jsonl"
            store = DecisionTraceStore(path)
            item = trace("trace-secret-aliases")
            item["attributes"] = {
                "access-token": "access-value",
                "refresh_token": "refresh-value",
                "client secret": "client-value",
                "Authorization-Header": "Bearer value",
                "x-api-key": "key-value",
                "private-key-pem": "pem-value",
                "token_budget": 100,
            }
            store.append(item)
            persisted = json.loads(path.read_text(encoding="utf-8"))
            attributes = persisted["attributes"]
            for key in (
                "access-token",
                "refresh_token",
                "client secret",
                "Authorization-Header",
                "x-api-key",
                "private-key-pem",
            ):
                self.assertEqual(attributes[key], "[REDACTED]")
            self.assertEqual(attributes["token_budget"], 100)
            raw = path.read_text(encoding="utf-8")
            for leaked in (
                "access-value",
                "refresh-value",
                "client-value",
                "Bearer value",
                "key-value",
                "pem-value",
            ):
                self.assertNotIn(leaked, raw)

    def test_embedded_secrets_in_safe_named_strings_are_redacted(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "decision-traces.jsonl"
            store = DecisionTraceStore(path)
            item = trace("trace-embedded-secrets")
            item["attributes"] = {
                "url": "https://provider.test/orders?api_key=abc123&symbol=BTC",
                "message": "Authorization: CustomScheme bearer-secret",
                "userinfo_url": "https://api-user:url-password@provider.test/orders",
                "dsn": "host=db;password=hunter2;database=autotrade",
                "key_material": "-----BEGIN PRIVATE KEY-----\\nsecret\\n-----END PRIVATE KEY-----",
                "encrypted_key_material": "-----BEGIN ENCRYPTED PRIVATE KEY-----\\nencrypted-secret\\n-----END ENCRYPTED PRIVATE KEY-----",
                "token_text": "token=plain-token-secret",
                "session_text": "session: plain-session-secret",
                "json_message": (
                    '{"Authorization":"Digest json-auth-secret",'
                    '"api_key":"json-key-secret"}'
                ),
                "repr_message": (
                    "{'Authorization': 'Custom repr-auth-secret', "
                    "'api_key': 'repr-key-secret'}"
                ),
                "safe": "symbol=BTC",
            }
            store.append(item)

            raw = path.read_text(encoding="utf-8")
            for leaked in (
                "abc123",
                "bearer-secret",
                "url-password",
                "hunter2",
                "plain-token-secret",
                "plain-session-secret",
                "\\nsecret\\n",
                "encrypted-secret",
                "json-auth-secret",
                "json-key-secret",
                "repr-auth-secret",
                "repr-key-secret",
            ):
                self.assertNotIn(leaked, raw)
            persisted = json.loads(raw)
            attrs = persisted["attributes"]
            self.assertIn("api_key=[REDACTED]", attrs["url"])
            self.assertIn("Authorization: [REDACTED]", attrs["message"])
            self.assertEqual(attrs["userinfo_url"], "https://[REDACTED]@provider.test/orders")
            self.assertIn("password=[REDACTED]", attrs["dsn"])
            self.assertEqual(attrs["key_material"], "[REDACTED]")
            self.assertEqual(attrs["encrypted_key_material"], "[REDACTED]")
            self.assertIn("token=[REDACTED]", attrs["token_text"])
            self.assertIn("session:[REDACTED]", attrs["session_text"])
            self.assertNotIn("json-auth-secret", attrs["json_message"])
            self.assertNotIn("json-key-secret", attrs["json_message"])
            self.assertGreaterEqual(attrs["json_message"].count("[REDACTED]"), 2)
            self.assertNotIn("repr-auth-secret", attrs["repr_message"])
            self.assertNotIn("repr-key-secret", attrs["repr_message"])
            self.assertGreaterEqual(attrs["repr_message"].count("[REDACTED]"), 2)
            self.assertEqual(attrs["safe"], "symbol=BTC")

    def test_compound_and_quoted_embedded_credentials_are_fully_redacted(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "decision-traces.jsonl"
            store = DecisionTraceStore(path)
            item = trace("trace-compound-credentials")
            item["attributes"] = {
                "digest_header": (
                    'Authorization: Digest username="api", realm="trade", '
                    'response="digest-comma-secret"'
                ),
                "json_digest": (
                    '{"Authorization":"Digest username=\\\"api\\\", '
                    'response=\\\"json-comma-secret\\\"","safe":"ok"}'
                ),
                "quoted_key_with_spaces": (
                    '{"api_key":"secret value with spaces","safe":"ok"}'
                ),
            }
            store.append(item)

            raw = path.read_text(encoding="utf-8")
            for leaked in (
                "digest-comma-secret",
                "json-comma-secret",
                "secret value with spaces",
            ):
                self.assertNotIn(leaked, raw)
            persisted = json.loads(raw)["attributes"]
            self.assertIn("[REDACTED]", persisted["digest_header"])
            self.assertIn("[REDACTED]", persisted["json_digest"])
            self.assertIn("[REDACTED]", persisted["quoted_key_with_spaces"])

    def test_non_finite_diagnostic_numbers_cannot_enter_durable_trace(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "decision-traces.jsonl"
            store = DecisionTraceStore(path)
            item = trace("trace-nonfinite")
            item["attributes"] = {"score": float("nan")}
            with self.assertRaises(ValueError):
                store.append(item)
            self.assertFalse(path.exists())

    def test_corrupt_existing_chain_blocks_append(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "decision-traces.jsonl"
            store = DecisionTraceStore(path)
            store.append(trace("trace-1"))
            path.write_text(path.read_text(encoding="utf-8").replace('"risk_outcome":"admitted"', '"risk_outcome":"rejected"'), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "chain is corrupt"):
                store.append(trace("trace-2"))


    def test_whitebit_api_secret_aliases_are_redacted_without_benign_overreach(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "decision-traces.jsonl"
            store = DecisionTraceStore(path)
            item = trace("trace-whitebit-api-secret")
            item["attributes"] = {
                "api_secret": "WHITEBIT-DIRECT-SECRET",
                "api-secret": "WHITEBIT-HYPHEN-SECRET",
                "X-TXC-APIKEY": "WHITEBIT-TXC-APIKEY",
                "X-TXC-PAYLOAD": "WHITEBIT-TXC-PAYLOAD",
                "X-TXC-SIGNATURE": "WHITEBIT-TXC-SIGNATURE",
                "api_secret_rotation_count": 4,
                "token_budget": 8,
                "message": '{"api_secret":"WHITEBIT-JSON-SECRET","safe":"ok"}',
                "repr_message": "{'api-secret': 'WHITEBIT-REPR-SECRET', 'safe': 'ok'}",
                "header_text": "X-TXC-SIGNATURE: WHITEBIT-TEXT-SIGNATURE",
                "signed_url": (
                    "https://provider.test/private?"
                    "X-TXC-SIGNATURE=WHITEBIT-URL-SIGNATURE&symbol=BTC"
                ),
            }
            store.append(item)

            raw = path.read_text(encoding="utf-8")
            for leaked in (
                "WHITEBIT-DIRECT-SECRET",
                "WHITEBIT-HYPHEN-SECRET",
                "WHITEBIT-TXC-APIKEY",
                "WHITEBIT-TXC-PAYLOAD",
                "WHITEBIT-TXC-SIGNATURE",
                "WHITEBIT-JSON-SECRET",
                "WHITEBIT-REPR-SECRET",
                "WHITEBIT-TEXT-SIGNATURE",
                "WHITEBIT-URL-SIGNATURE",
            ):
                self.assertNotIn(leaked, raw)

            attributes = json.loads(raw)["attributes"]
            self.assertEqual(attributes["api_secret"], "[REDACTED]")
            self.assertEqual(attributes["api-secret"], "[REDACTED]")
            self.assertEqual(attributes["X-TXC-APIKEY"], "[REDACTED]")
            self.assertEqual(attributes["X-TXC-PAYLOAD"], "[REDACTED]")
            self.assertEqual(attributes["X-TXC-SIGNATURE"], "[REDACTED]")
            self.assertEqual(attributes["api_secret_rotation_count"], 4)
            self.assertEqual(attributes["token_budget"], 8)
            self.assertIn("[REDACTED]", attributes["message"])
            self.assertIn("[REDACTED]", attributes["repr_message"])
            self.assertEqual(
                attributes["header_text"],
                "X-TXC-SIGNATURE:[REDACTED]",
            )
            self.assertEqual(
                attributes["signed_url"],
                "https://provider.test/private?"
                "X-TXC-SIGNATURE=[REDACTED]&symbol=BTC",
            )


    def test_escaped_json_and_encoded_query_secret_keys_are_redacted(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "decision-traces.jsonl"
            store = DecisionTraceStore(path)
            item = trace("trace-encoded-secret-keys")
            item["attributes"] = {
                "escaped_json": (
                    '{"api\\u005fsecret":"WHITEBIT-ESCAPED-SECRET","safe":"ok"}'
                ),
                "escaped_json_array": (
                    '[{"api\\u005fsecret":"WHITEBIT-ARRAY-SECRET"},{"safe":"ok"}]'
                ),
                "encoded_url_upper": (
                    "https://provider.test/orders?"
                    "api%5Fsecret=WHITEBIT-PERCENT-UPPER&symbol=BTC"
                ),
                "encoded_url_lower": (
                    "https://provider.test/orders?"
                    "api%5fsecret=WHITEBIT-PERCENT-LOWER&symbol=ETH"
                ),
                "benign_encoded_url": (
                    "https://provider.test/orders?"
                    "api%5Fsecret%5Frotation%5Fcount=4&symbol=BTC"
                ),
            }
            store.append(item)

            raw = path.read_text(encoding="utf-8")
            for leaked in (
                "WHITEBIT-ESCAPED-SECRET",
                "WHITEBIT-ARRAY-SECRET",
                "WHITEBIT-PERCENT-UPPER",
                "WHITEBIT-PERCENT-LOWER",
            ):
                self.assertNotIn(leaked, raw)

            attributes = json.loads(raw)["attributes"]
            decoded_object = json.loads(attributes["escaped_json"])
            self.assertEqual(decoded_object["api_secret"], "[REDACTED]")
            self.assertEqual(decoded_object["safe"], "ok")
            decoded_array = json.loads(attributes["escaped_json_array"])
            self.assertEqual(decoded_array[0]["api_secret"], "[REDACTED]")
            self.assertEqual(decoded_array[1]["safe"], "ok")
            self.assertEqual(
                attributes["encoded_url_upper"],
                "https://provider.test/orders?api%5Fsecret=[REDACTED]&symbol=BTC",
            )
            self.assertEqual(
                attributes["encoded_url_lower"],
                "https://provider.test/orders?api%5fsecret=[REDACTED]&symbol=ETH",
            )
            self.assertEqual(
                attributes["benign_encoded_url"],
                "https://provider.test/orders?"
                "api%5Fsecret%5Frotation%5Fcount=4&symbol=BTC",
            )


if __name__ == "__main__":
    unittest.main()
