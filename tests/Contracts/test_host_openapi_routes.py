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

    def test_protected_routes_require_session_and_actor_headers(self):
        text = OPENAPI.read_text(encoding="utf-8")
        self.assertIn("AutoTradeSession:", text)
        self.assertIn("name: Authorization", text)
        self.assertIn("AutoTradeActor:", text)
        self.assertIn("name: X-AutoTrade-Actor", text)
        self.assertIn(
            "security:\n  - AutoTradeSession: []\n    AutoTradeActor: []",
            text,
        )

    def test_health_is_the_explicit_unauthenticated_exception(self):
        text = OPENAPI.read_text(encoding="utf-8")
        health = text.split("  /api/v1/health:", 1)[1].split("components:", 1)[0]
        self.assertIn("security: []", health)
        for route in (
            "/api/v1/state:",
            "/api/v1/commands:",
            "/api/v1/operations/{operation_id}:",
            "/api/v1/events:",
        ):
            section = text.split(f"  {route}", 1)[1].split("\n  /api/v1/", 1)[0]
            self.assertNotIn("security: []", section)


if __name__ == "__main__":
    unittest.main()
