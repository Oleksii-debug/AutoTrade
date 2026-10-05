from __future__ import annotations

import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource


ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "contracts" / "jsonschema"


class ConfirmIntentUiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schemas = {
            path.name: json.loads(path.read_text(encoding="utf-8"))
            for path in SCHEMAS.glob("*.json")
        }
        cls.registry = Registry().with_resources(
            [
                (schema["$id"], Resource.from_contents(schema))
                for schema in cls.schemas.values()
            ]
        )
        ui = cls.schemas["ui.schema.json"]
        cls.validator = Draft202012Validator(
            {"$ref": f"{ui['$id']}#/$defs/UiCommand"},
            registry=cls.registry,
            format_checker=FormatChecker(),
        )

    @staticmethod
    def _command(payload: object) -> dict[str, object]:
        return {
            "command_id": "88888888-8888-4888-8888-888888888888",
            "expected_state_version": "43",
            "idempotency_key": "confirm-pending-8888",
            "actor": "owner",
            "session": "sid-" + "8" * 64,
            "account_id": "paper-account-1",
            "environment": "PAPER",
            "action": "CONFIRM_INTENT",
            "payload": payload,
        }

    def test_exact_opaque_pending_intent_payload_is_valid(self) -> None:
        self.assertTrue(
            self.validator.is_valid(
                self._command({"pending_intent_id": "pending-opaque-8888"})
            )
        )

    def test_pending_intent_id_is_required_and_must_be_non_empty(self) -> None:
        for payload in ({}, {"pending_intent_id": ""}):
            with self.subTest(payload=payload):
                self.assertFalse(self.validator.is_valid(self._command(payload)))

    def test_financial_override_fields_are_forbidden(self) -> None:
        for field, value in (
            ("quantity", "999"),
            ("price", "999999"),
            ("notional", "999999999"),
            ("action", "ORDER.SUBMIT"),
            ("risk_intent", {"quantity": "999"}),
            ("risk_policy", {"max_single_notional": "999999999"}),
            ("reservation_requirements", {"CASH:USD": "999999999"}),
            ("authority_policy_version", 999),
            ("instrument_version", 999),
        ):
            payload = {
                "pending_intent_id": "pending-opaque-8888",
                field: value,
            }
            with self.subTest(field=field):
                self.assertFalse(self.validator.is_valid(self._command(payload)))

    def test_payload_must_be_exact_object_not_scalar_or_array(self) -> None:
        for payload in (None, "pending-opaque-8888", ["pending-opaque-8888"]):
            with self.subTest(payload=payload):
                self.assertFalse(self.validator.is_valid(self._command(payload)))


if __name__ == "__main__":
    unittest.main()
