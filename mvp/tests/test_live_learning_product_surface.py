from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from mvp.tests.test_provider_free_product import ProductClient


ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "web" / "src" / "app.js"
INDEX = ROOT / "web" / "src" / "index.html"
SCHEMA = ROOT / "contracts" / "jsonschema" / "ui.schema.json"


class LiveLearningProductSurfaceTests(unittest.TestCase):
    def test_snapshot_contract_and_missing_state_are_explicit(self):
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))["$defs"]["UiSnapshot"]
        self.assertIn("whitebit_learning", schema["required"])
        self.assertEqual(schema["properties"]["whitebit_learning"]["type"], "object")
        with TemporaryDirectory() as directory:
            client = ProductClient(directory)
            try:
                learning = client.state()["whitebit_learning"]
                self.assertEqual(learning["status"], "MISSING")
                self.assertFalse(learning["network_orders"])
                self.assertFalse(learning["trading_authority"])
            finally:
                client.close()

    def test_ui_renders_learning_as_text_and_exposes_accessible_regions(self):
        app = APP.read_text(encoding="utf-8")
        page = INDEX.read_text(encoding="utf-8")
        parser = app[app.index("function parseCanonicalSnapshot"):
                     app.index("function parseCommandResult")]
        renderer = app[app.index("function renderWhitebitLearning"):
                       app.index("function normalizedTableQuery")]
        self.assertIn('snapshot.whitebit_learning, "whitebit_learning"', parser)
        self.assertIn('"learning-body"', renderer)
        self.assertIn('"models-body"', renderer)
        self.assertIn("WhiteBIT ZERO learning status is unavailable.", renderer)
        self.assertIn(
            "WhiteBIT ZERO model and evaluation evidence is unavailable.",
            renderer,
        )
        self.assertNotIn("innerHTML", renderer)
        projection = app[app.index("function appendProjectionRow"):
                         app.index("function renderJobs")]
        self.assertIn("cell.textContent = value", projection)
        self.assertNotIn("innerHTML", projection)
        self.assertIn('id="learning-region"', page)
        self.assertIn('aria-label="WhiteBIT ZERO learning lifecycle"', page)
        self.assertIn('id="models-region"', page)
        self.assertIn('aria-label="WhiteBIT ZERO models and cost evidence"', page)
        self.assertIn("deterministic numeric learner", page)
        self.assertIn("It does not use an LLM", page)
        self.assertIn("not foundation-model weights", page)
        self.assertIn("not evidence that an external LLM is active", page)

    def test_agent_report_includes_bounded_redacted_learning_snapshot(self):
        projected = {
            "status": "READY",
            "phase": "<img src=x onerror=alert(1)>",
            "access_token": "secret-sentinel",
            "network_orders": False,
            "trading_authority": False,
        }
        with TemporaryDirectory() as directory, patch(
            "mvp.autotrade_mvp.product_runtime.read_whitebit_live_learning_status",
            return_value=projected,
        ):
            client = ProductClient(directory)
            try:
                state = client.state()
                self.assertEqual(state["whitebit_learning"]["phase"], projected["phase"])
                status, body, _headers = client.request("GET", "/api/v1/agent-report")
                self.assertEqual(status, 200)
                with ZipFile(BytesIO(body)) as archive:
                    exported = json.loads(
                        archive.read("live-learning/status.json")
                    )
                    manifest = json.loads(archive.read("manifest.json"))
                self.assertEqual(exported["phase"], projected["phase"])
                self.assertEqual(exported["access_token"], "[REDACTED]")
                self.assertTrue(manifest["live_learning_status_included"])
                self.assertIn("live-learning/status.json", manifest["members"])
            finally:
                client.close()


if __name__ == "__main__":
    unittest.main()
