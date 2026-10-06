from html.parser import HTMLParser
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
INDEX = ROOT / "web" / "src" / "index.html"


class _CopyButtonParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.buttons: dict[str, dict[str, object]] = {}
        self._active_id: str | None = None

    def handle_starttag(self, tag, attrs):
        if tag != "button":
            return
        attributes = dict(attrs)
        button_id = attributes.get("id")
        if button_id not in {
            "permissions-copy",
            "strategy-copy",
            "portfolio-copy",
            "operations-copy",
            "risk-copy",
            "jobs-copy",
            "event-history-copy",
        }:
            return
        self._active_id = button_id
        self.buttons[button_id] = {
            "aria_label": attributes.get("aria-label"),
            "text": [],
        }

    def handle_data(self, data):
        if self._active_id is not None:
            self.buttons[self._active_id]["text"].append(data)

    def handle_endtag(self, tag):
        if tag == "button":
            self._active_id = None


class CopyButtonAccessibleNameTests(unittest.TestCase):
    def test_copy_buttons_have_unique_scope_specific_accessible_names(self):
        parser = _CopyButtonParser()
        parser.feed(INDEX.read_text(encoding="utf-8"))

        expected_scope_terms = {
            "permissions-copy": ("permission", "capability"),
            "strategy-copy": ("strategy", "decision"),
            "portfolio-copy": ("portfolio",),
            "operations-copy": ("host", "operation"),
            "risk-copy": ("risk", "authority"),
            "jobs-copy": ("research", "replay"),
            "event-history-copy": ("host", "event"),
        }
        self.assertEqual(set(parser.buttons), set(expected_scope_terms))

        names = {}
        for button_id, required_terms in expected_scope_terms.items():
            button = parser.buttons[button_id]
            visible_text = " ".join("".join(button["text"]).split())
            aria_label = button["aria_label"]
            accessible_name = (
                aria_label.strip()
                if isinstance(aria_label, str) and aria_label.strip()
                else visible_text
            )
            self.assertTrue(accessible_name, button_id)
            normalized = accessible_name.casefold()
            for term in required_terms:
                self.assertIn(term, normalized, button_id)
            names[button_id] = accessible_name

        self.assertEqual(len(set(names.values())), len(names))


if __name__ == "__main__":
    unittest.main()
