import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools.contract_version_guard import evaluate


def write_tree(root: Path, version="1.0.0", schemas=None, defs=None):
    schemas = ["a.schema.json"] if schemas is None else schemas
    defs = {"A": {"type": "string"}} if defs is None else defs
    (root / "contracts" / "jsonschema").mkdir(parents=True)
    (root / "contracts" / "manifest.json").write_text(
        json.dumps({
            "contract_version": version,
            "schemas": schemas,
        }),
        encoding="utf-8",
    )
    for name in schemas:
        (root / "contracts" / "jsonschema" / name).write_text(
            json.dumps({"$defs": defs}),
            encoding="utf-8",
        )


class ContractVersionGuardTests(unittest.TestCase):
    def test_unchanged_surface_allows_same_version(self):
        with TemporaryDirectory() as left, TemporaryDirectory() as right:
            write_tree(Path(left))
            write_tree(Path(right))
            self.assertEqual(evaluate(Path(left), Path(right)), [])

    def test_changed_surface_requires_version_increment(self):
        with TemporaryDirectory() as left, TemporaryDirectory() as right:
            write_tree(Path(left))
            write_tree(Path(right), defs={"A": {"type": "number"}})
            errors = evaluate(Path(left), Path(right))
            self.assertTrue(any("without increasing" in item for item in errors))

    def test_removed_definition_requires_major_increment(self):
        with TemporaryDirectory() as left, TemporaryDirectory() as right:
            write_tree(Path(left), defs={"A": {}, "B": {}})
            write_tree(Path(right), version="1.1.0", defs={"A": {}})
            errors = evaluate(Path(left), Path(right))
            self.assertTrue(any("new major version" in item for item in errors))

    def test_removed_definition_allows_major_increment(self):
        with TemporaryDirectory() as left, TemporaryDirectory() as right:
            write_tree(Path(left), defs={"A": {}, "B": {}})
            write_tree(Path(right), version="2.0.0", defs={"A": {}})
            self.assertEqual(evaluate(Path(left), Path(right)), [])

    def test_version_cannot_decrease(self):
        with TemporaryDirectory() as left, TemporaryDirectory() as right:
            write_tree(Path(left), version="2.0.0")
            write_tree(Path(right), version="1.9.9")
            errors = evaluate(Path(left), Path(right))
            self.assertTrue(any("never decrease" in item for item in errors))


if __name__ == "__main__":
    unittest.main()
