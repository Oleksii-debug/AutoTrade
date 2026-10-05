"""Semantic, keyboard-native HTML surface for local AutoTrade state.

The renderer has no visual-only status semantics and escapes all dynamic text.
It is an implementation foundation, not a claim of completed NVDA qualification.
"""

from __future__ import annotations

from html import escape
from typing import Iterable, Mapping


def _text(value: object, fallback: str = "Unavailable") -> str:
    if value is None:
        return fallback
    return str(value)


def _rows(items: Iterable[tuple[str, object]]) -> str:
    return "".join(
        f"<dt>{escape(label)}</dt><dd>{escape(_text(value))}</dd>"
        for label, value in items
    )


def render_operation_list(operations: Mapping[str, object]) -> str:
    if not operations:
        return "<p>No operations are currently recorded.</p>"
    parts = ["<ul>"]
    for operation_id, phase in sorted(operations.items()):
        parts.append(
            "<li>"
            f"<span class=\"operation-id\">{escape(str(operation_id))}</span>: "
            f"<strong>{escape(str(phase))}</strong>"
            "</li>"
        )
    parts.append("</ul>")
    return "".join(parts)


def render_semantic_page(
    snapshot: Mapping[str, object],
    *,
    status_text: str,
    announcement: str = "",
    command_message: str = "",
) -> str:
    """Render a complete document that remains understandable without CSS or script."""

    state_version = _text(snapshot.get("state_version"), "0")
    event_cursor = _text(snapshot.get("event_cursor"), "0")
    operations = snapshot.get("operations")
    if not isinstance(operations, Mapping):
        operations = {}

    safe_status_lines = [line for line in str(status_text).splitlines() if line.strip()]
    status_html = "".join(f"<p>{escape(line)}</p>" for line in safe_status_lines)
    announcement_html = escape(str(announcement))
    command_html = escape(str(command_message))

    return (
        "<!doctype html>"
        "<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>AutoTrade local control</title></head><body>"
        "<a href=\"#main\">Skip to main content</a>"
        "<header><h1>AutoTrade local control</h1>"
        "<p>Live order submission is unavailable unless separately qualified and authorized.</p>"
        "</header>"
        "<main id=\"main\">"
        "<section aria-labelledby=\"status-heading\">"
        "<h2 id=\"status-heading\">System status</h2>"
        f"<div role=\"status\" aria-live=\"polite\" aria-atomic=\"true\">{announcement_html}</div>"
        f"{status_html}"
        "<dl>"
        f"{_rows((('State version', state_version), ('Event cursor', event_cursor)))}"
        "</dl></section>"
        "<section aria-labelledby=\"operations-heading\">"
        "<h2 id=\"operations-heading\">Operations</h2>"
        f"{render_operation_list(operations)}"
        "</section>"
        "<section aria-labelledby=\"commands-heading\">"
        "<h2 id=\"commands-heading\">Commands</h2>"
        "<p>Command acceptance does not mean financial completion.</p>"
        "<form method=\"post\" action=\"/v1/commands-ui\">"
        f"<input type=\"hidden\" name=\"expected_state_version\" value=\"{escape(state_version)}\">"
        "<fieldset><legend>Choose a host command</legend>"
        "<label for=\"action\">Action</label>"
        "<select id=\"action\" name=\"action\" required>"
        "<option value=\"PAUSE_NEW_RISK\">Pause new risk</option>"
        "<option value=\"RESUME_AFTER_RECOVERY\">Request resume after recovery</option>"
        "<option value=\"REFRESH_STATE\">Refresh state</option>"
        "</select>"
        "<button type=\"submit\">Submit command</button>"
        "</fieldset></form>"
        f"<div role=\"alert\" aria-live=\"assertive\">{command_html}</div>"
        "</section>"
        "</main>"
        "<footer><p>Local accessible control surface.</p></footer>"
        "</body></html>"
    )


def command_result_message(result: Mapping[str, object]) -> str:
    """Plain-language command result for a live region."""

    status = str(result.get("status", "UNKNOWN"))
    command_id = _text(result.get("command_id"), "unknown")
    operation_id = result.get("operation_id")
    reasons = result.get("reason_codes")
    if isinstance(reasons, (list, tuple)):
        reason_text = ", ".join(str(item) for item in reasons) or "none"
    else:
        reason_text = "none"

    if status == "ACCEPTED":
        return (
            f"Command {command_id} was accepted for processing. "
            f"Operation {_text(operation_id, 'unknown')} is not yet a completed financial result."
        )
    if status == "CONFLICT":
        return f"Command {command_id} was not accepted because of a state conflict. Reasons: {reason_text}."
    return f"Command {command_id} has status {status}. Reasons: {reason_text}."
