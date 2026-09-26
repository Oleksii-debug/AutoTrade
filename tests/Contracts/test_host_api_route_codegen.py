from pathlib import Path
import unittest

from tools.generate_host_api_routes import (
    Operation,
    parse_operations,
    render_csharp,
    render_web,
)


ROOT = Path(__file__).resolve().parents[2]
OPENAPI = ROOT / "contracts" / "openapi" / "host-api.yaml"
CSHARP = ROOT / "src" / "AutoTrade.Contracts" / "HostApiRoutes.cs"
WEB = ROOT / "web" / "src" / "host-api-routes.js"
DESKTOP = (
    ROOT / "src" / "AutoTrade.Desktop"
    / "AuthenticatedEmergencyHostClient.cs"
)
WEB_APP = ROOT / "web" / "src" / "app.js"
WEB_INDEX = ROOT / "web" / "src" / "index.html"


class HostApiRouteCodegenTests(unittest.TestCase):
    def test_canonical_openapi_operations_are_exact(self):
        operations = parse_operations(
            OPENAPI.read_text(encoding="utf-8")
        )
        self.assertEqual(
            operations,
            (
                Operation("get", "/api/v1/state", "getState", ()),
                Operation(
                    "post",
                    "/api/v1/commands",
                    "submitCommand",
                    (),
                ),
                Operation(
                    "get",
                    "/api/v1/operations/{operation_id}",
                    "getOperation",
                    ("operation_id",),
                ),
                Operation(
                    "get",
                    "/api/v1/events",
                    "streamEvents",
                    (),
                ),
                Operation(
                    "get",
                    "/api/v1/health",
                    "getHealth",
                    (),
                ),
            ),
        )

    def test_generated_bindings_are_current(self):
        operations = parse_operations(
            OPENAPI.read_text(encoding="utf-8")
        )
        self.assertEqual(
            CSHARP.read_text(encoding="utf-8"),
            render_csharp(operations),
        )
        self.assertEqual(
            WEB.read_text(encoding="utf-8"),
            render_web(operations),
        )

    def test_consumers_do_not_redeclare_canonical_route_literals(self):
        desktop = DESKTOP.read_text(encoding="utf-8")
        web = WEB_APP.read_text(encoding="utf-8")
        self.assertNotIn('"api/v1/', desktop)
        self.assertNotIn('"/api/v1', web)
        self.assertIn("HostApiRoutes.GetState", desktop)
        self.assertIn("HostApiRoutes.SubmitCommand", desktop)
        self.assertIn("HostApiRoutes.GetOperation(canonicalId)", desktop)
        for operation_id in (
            "getState",
            "submitCommand",
            "getOperation",
            "streamEvents",
        ):
            self.assertIn(
                f'HOST_API.route("{operation_id}"',
                web,
            )

    def test_web_loads_generated_routes_before_consumer(self):
        html = WEB_INDEX.read_text(encoding="utf-8")
        generated = html.index('src="/host-api-routes.js"')
        consumer = html.index('src="/app.js"')
        self.assertLess(generated, consumer)

    def test_missing_operation_id_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "missing operationId"):
            parse_operations(
                """openapi: 3.1.0
paths:
  /api/v1/state:
    get:
      responses: {}
"""
            )

    def test_duplicate_operation_id_fails_closed(self):
        with self.assertRaisesRegex(
            ValueError,
            "duplicate OpenAPI operationId",
        ):
            parse_operations(
                """openapi: 3.1.0
paths:
  /api/v1/state:
    get:
      operationId: sameOperation
  /api/v1/health:
    get:
      operationId: sameOperation
"""
            )

    def test_noncanonical_prefix_and_ambiguous_templates_fail_closed(self):
        with self.assertRaisesRegex(
            ValueError,
            "canonical /api/v1/ prefix",
        ):
            parse_operations(
                """openapi: 3.1.0
paths:
  /v1/state:
    get:
      operationId: getState
"""
            )
        with self.assertRaisesRegex(
            ValueError,
            "unsupported path-template syntax",
        ):
            parse_operations(
                """openapi: 3.1.0
paths:
  /api/v1/operations/{operation-id}:
    get:
      operationId: getOperation
"""
            )


if __name__ == "__main__":
    unittest.main()
