import ast
from pathlib import Path
import re
import unittest

from mvp.autotrade_mvp.durable_event_taxonomy import (
    CANONICAL_SIMULATION_SESSION,
    HOST_CONTROL,
    MODEL_BUDGET,
    TAXONOMY_VERSION,
    classify_durable_event,
    descriptor_for_aggregate_type,
    financial_qualification_aggregate_types,
    taxonomy_digest,
    taxonomy_records,
)


class DurableEventTaxonomyTests(unittest.TestCase):
    def test_taxonomy_identity_is_deterministic_and_canonical(self):
        records = taxonomy_records()
        self.assertEqual(
            [item["aggregate_type"] for item in records],
            sorted(item["aggregate_type"] for item in records),
        )
        self.assertEqual(len(records), len({item["aggregate_type"] for item in records}))
        self.assertRegex(TAXONOMY_VERSION, r"^\d+\.\d+\.\d+$")
        self.assertRegex(taxonomy_digest(), r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(taxonomy_digest(), taxonomy_digest())

    def test_unknown_or_polymorphic_aggregate_type_fails_closed(self):
        class HostileText(str):
            pass

        with self.assertRaisesRegex(ValueError, "unclassified durable aggregate type"):
            descriptor_for_aggregate_type("future_unclassified_writer")
        with self.assertRaisesRegex(ValueError, "canonical exact string"):
            descriptor_for_aggregate_type(HostileText("economic_book"))
        with self.assertRaisesRegex(ValueError, "unclassified durable aggregate type"):
            classify_durable_event({"aggregate_type": "future_unclassified_writer"})

    def test_financial_visibility_is_explicit_not_equal_to_durable(self):
        financial = set(financial_qualification_aggregate_types())
        self.assertIn("economic_book", financial)
        self.assertIn("submission_attempt", financial)
        self.assertIn("risk_policy_registry", financial)
        self.assertNotIn(MODEL_BUDGET.aggregate_type, financial)
        self.assertNotIn(HOST_CONTROL.aggregate_type, financial)
        self.assertNotIn(CANONICAL_SIMULATION_SESSION.aggregate_type, financial)

    def test_current_package_writer_literals_cannot_bypass_registry(self):
        package_root = Path(__file__).resolve().parents[1] / "autotrade_mvp"
        registered = {item["aggregate_type"] for item in taxonomy_records()}
        discovered: list[tuple[str, str, int]] = []

        for path in sorted(package_root.glob("*.py")):
            if path.name == "durable_event_taxonomy.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = (
                        node.targets
                        if isinstance(node, ast.Assign)
                        else [node.target]
                    )
                    value = node.value
                    names = [
                        target.id
                        for target in targets
                        if isinstance(target, ast.Name)
                    ]
                    if (
                        any("AGGREGATE_TYPE" in name for name in names)
                        and isinstance(value, ast.Constant)
                        and isinstance(value.value, str)
                    ):
                        discovered.append((path.name, value.value, node.lineno))
                if isinstance(node, ast.Dict):
                    for key, value in zip(node.keys, node.values):
                        if (
                            isinstance(key, ast.Constant)
                            and key.value == "aggregate_type"
                            and isinstance(value, ast.Constant)
                            and isinstance(value.value, str)
                        ):
                            discovered.append((path.name, value.value, node.lineno))

        unknown = sorted(
            item for item in discovered if item[1] not in registered
        )
        self.assertEqual(
            unknown,
            [],
            "production writer introduced an unclassified durable aggregate literal",
        )


if __name__ == "__main__":
    unittest.main()
