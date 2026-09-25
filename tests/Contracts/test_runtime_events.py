import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.pipeline import run_vertical_slice


ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "contracts" / "jsonschema"


class RuntimeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schemas = {p.name: json.loads(p.read_text(encoding="utf-8")) for p in SCHEMAS.glob("*.json")}
        cls.registry = Registry().with_resources(
            [(schema["$id"], Resource.from_contents(schema)) for schema in cls.schemas.values()]
        )

    def test_simulation_journal_event_matches_canonical_event_envelope(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            outbox = JournalStore(Path(directory) / "journal.sqlite3").pending_outbox()
            self.assertEqual(len(outbox), 1)
            envelope = outbox[0]["payload"]
            schema = self.schemas["event.schema.json"]
            Draft202012Validator(
                {"$ref": f"{schema['$id']}#/$defs/EventEnvelope"},
                registry=self.registry,
                format_checker=FormatChecker(),
            ).validate(envelope)


if __name__ == "__main__":
    unittest.main()
