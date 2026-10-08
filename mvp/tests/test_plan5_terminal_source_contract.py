"""Plan-5 final source invariants; this module never grants DONE or release authority.

Executed alongside the existing browser, Desktop, packaging, update and recovery
suites on an exact source SHA. Physical NVDA and signed release belong to Plan 9.
"""
from html.parser import HTMLParser
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web" / "src"
DESKTOP = ROOT / "src" / "AutoTrade.Desktop"
ROUTES = (
    "overview", "accounts", "opportunities", "portfolio", "risk",
    "research", "learning", "models", "history", "settings",
)


class _SemanticIndex(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.primary_links = []
        self.landmarks = []
        self._primary_nav = 0

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag in ("main", "header", "nav"):
            self.landmarks.append((tag, attrs))
        if tag == "nav" and attrs.get("aria-label") == "Primary":
            self._primary_nav += 1
        if tag == "a" and self._primary_nav:
            self.primary_links.append(attrs.get("href"))

    def handle_endtag(self, tag):
        if tag == "nav" and self._primary_nav:
            self._primary_nav -= 1


class Plan5TerminalSourceContractTests(unittest.TestCase):
    def test_canonical_web_navigation_remains_unique_and_semantic(self):
        parser = _SemanticIndex()
        parser.feed((WEB / "index.html").read_text(encoding="utf-8"))
        self.assertEqual(len(parser.ids), len(set(parser.ids)), "duplicate DOM ID")
        self.assertEqual(parser.primary_links, [f"#{name}" for name in ROUTES])
        self.assertIn(("main", {"id": "main", "tabindex": "-1"}), parser.landmarks)
        self.assertEqual(
            sum(tag == "nav" and attrs.get("aria-label") == "Primary"
                for tag, attrs in parser.landmarks), 1
        )
        self.assertTrue(all(name in parser.ids and f"{name}-heading" in parser.ids
                            for name in ROUTES))
        html = (WEB / "index.html").read_text(encoding="utf-8")
        self.assertIn('href="#main">Skip to main content</a>', html)
        self.assertIn('role="status" aria-live="polite"', html)
        self.assertIn('role="alert" aria-live="assertive"', html)
        self.assertIn('id="provider-availability"', html)
        self.assertIn("UNAVAILABLE", html)

    def test_browser_projection_cannot_gain_html_or_financial_authority(self):
        app = (WEB / "app.js").read_text(encoding="utf-8")
        for forbidden in ("innerHTML", "outerHTML", "document.write(", "eval(", "new Function("):
            self.assertNotIn(forbidden, app)
        self.assertIn("textContent", app)
        self.assertIn("aria-current", app)
        self.assertIn("unknown", app.lower())
        self.assertTrue((WEB / "host-api-routes.js").is_file())

    def test_desktop_is_single_webview_shell_with_native_emergency(self):
        src = (DESKTOP / "MainWindow.xaml.cs").read_text(encoding="utf-8")
        self.assertIn("WebView2", src)
        self.assertIn("ProductWebViewHost", src)
        self.assertIn("AreHostObjectsAllowed = false", src)
        self.assertIn("FrameNavigationStarting", src)
        self.assertIn("CoreWebView2PermissionState.Deny", src)
        self.assertIn("IEmergencyHostClient", src)
        self.assertIn("class AuthenticatedEmergencyHostClient", (DESKTOP / "AuthenticatedEmergencyHostClient.cs").read_text(encoding="utf-8"))
        owned = (DESKTOP / "OwnedProviderFreeRuntime.cs").read_text(encoding="utf-8")
        self.assertIn("127.0.0.1", owned)
        self.assertIn('api/v1/session', owned)
        self.assertNotIn("NavigateToString(", src)

    def test_existing_packaging_and_recovery_authorities_are_reused(self):
        required = (
            "mvp/autotrade_mvp/embedded_web.py",
            "mvp/autotrade_mvp/windows_update.py",
            "tools/build_windows_bundle.py",
            "tools/build_windows_install_manifest.py",
            "tools/build_provider_free_candidate.py",
            "mvp/tests/test_windows_bundle.py",
            "mvp/tests/test_windows_update.py",
            "mvp/tests/test_windows_install_manifest.py",
            "mvp/tests/test_runtime_locality_packaging.py",
            "tests/Product/plan5-navigation.cjs",
            ".github/workflows/provider-free-product.yml",
        )
        for relative in required:
            with self.subTest(path=relative):
                self.assertTrue((ROOT / relative).is_file())
        bundle = (ROOT / "tools" / "build_windows_bundle.py").read_text(encoding="utf-8")
        self.assertIn("FORBIDDEN_BASENAMES", bundle)
        self.assertIn("PRIVATE_KEY_MARKERS", bundle)
        self.assertIn("_reject_sensitive_content", bundle)


if __name__ == "__main__":
    unittest.main()
