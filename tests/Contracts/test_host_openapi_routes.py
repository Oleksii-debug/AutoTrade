from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
OPENAPI = ROOT / "contracts" / "openapi" / "host-api.yaml"


class HostOpenApiRouteTests(unittest.TestCase):
    def test_versioned_host_routes_use_single_canonical_api_prefix(self):
        text = OPENAPI.read_text(encoding="utf-8")
        for route in (
            "/api/v1/state",
            "/api/v1/commands",
            "/api/v1/operations/{operation_id}",
            "/api/v1/events",
            "/api/v1/health",
        ):
            self.assertIn(f"  {route}:", text)
        self.assertNotIn("\n  /v1/", text)

    def test_cursor_gap_description_points_to_canonical_state_route(self):
        text = OPENAPI.read_text(encoding="utf-8")
        self.assertIn("requires /api/v1/state resnapshot", text)
        self.assertNotIn("requires /v1/state resnapshot", text)


if __name__ == "__main__":
    unittest.main()
