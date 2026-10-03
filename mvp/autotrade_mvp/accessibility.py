"""Plain-text accessibility helpers for the simulated AutoTrade command surface.

This module is deliberately free of ANSI color, cursor movement, and
visual-only symbols so keyboard and screen-reader users receive the same facts.
It is a development fallback surface, not NVDA release qualification.
"""

from __future__ import annotations

from math import isfinite
from typing import Any

from .exact_decimal import ExactDecimalError, parse_canonical_decimal_text


STATE_TEXT = {
    "not_started": "Not started",
    "running": "Running",
    "needs_recovery": "Needs recovery",
    "corrupt": "Corrupt or unreadable state",
    "completed": "Completed",
    "awaiting_order_reconciliation": "Awaiting order reconciliation",
    "busy": "State is changing; read again",
}


def _safe_text(value: Any, default: str = "Unavailable") -> str:
    """Render only JSON-like scalar values without invoking arbitrary objects."""

    if value is None:
        return default
    if type(value) in {str, int, bool}:
        return str(value)
    if type(value) is float and isfinite(value):
        return str(value)
    return default


def _reservation_resource_text(value: Any) -> str:
    if type(value) is not str or not value or value != value.strip():
        return ""
    return value


def _reservation_amount_text(value: Any) -> str:
    if type(value) is not str or not value or value != value.strip():
        return ""
    try:
        amount = parse_canonical_decimal_text(value)
    except ExactDecimalError:
        return ""
    if amount < 0:
        return ""
    return value


def _value(mapping: dict[str, Any] | None, key: str, default: str = "Unavailable") -> str:
    if mapping is None:
        return default
    return _safe_text(mapping.get(key), default)


def _replay_verification_text(value: Any) -> str:
    if value is True:
        return "passed"
    if value is False:
        return "failed"
    return "unavailable"


def format_accessible_status(
    status: dict[str, Any],
    economic_report: dict[str, Any] | None = None,
) -> str:
    """Render a stable, copyable, screen-reader-friendly status summary."""

    if type(status) is not dict:
        status = {"status": "corrupt"}
    state = _safe_text(status.get("status", "corrupt"), "corrupt")
    if state not in STATE_TEXT:
        state = "corrupt"
    lines = [
        "AutoTrade status",
        "Mode: simulation only",
        "Live order submission: unavailable",
        f"System state: {STATE_TEXT.get(state, 'Unknown state')}",
    ]

    if state == "not_started":
        lines.extend(
            [
                "Replay verification: unavailable",
                "Economic edge: unproven",
            ]
        )
        return "\n".join(lines)

    if state == "corrupt":
        lines.extend(
            [
                "Replay verification: unavailable",
                "Action required: inspect or restore the simulated state before continuing",
                "Economic edge: unproven",
            ]
        )
        return "\n".join(lines)

    if state == "busy":
        lines.extend(["Replay verification: unavailable",
                      "Action required: read status again after the current operation",
                      "Economic edge: unproven"])
        return "\n".join(lines)

    replay_verified = status.get("replay_verified")
    fills = status.get("fills", {})
    recorded_fills = len(fills) if type(fills) is dict else "Unavailable"
    lines.extend(
        [
            f"Replay verification: {_replay_verification_text(replay_verified)}",
            f"Instrument: {_value(status, 'symbol')}",
            f"Initial capital: {_value(status, 'initial_cash')}",
            f"Recorded evidence items: {_value(status, 'evidence_count', '0')}",
            f"Recorded fills: {recorded_fills}",
        ]
    )

    if state == "needs_recovery":
        lines.append("Action required: recovery or reconciliation is needed before trusting current state")

    state_format = _safe_text(status.get("state_format"), "")
    if state_format == "canonical_journal":
        lines.extend([
            f"Episode: {_value(status, 'episode_id')}",
            f"Session outcome: {_value(status, 'session_status')}",
            f"Cash (USD): {_value(status, 'cash')}",
            f"Position (shares): {_value(status, 'position')}",
            f"Journal sequence: {_value(status, 'journal_sequence')}",
            "Order submission during this read: none",
        ])
        reservations = status.get("active_reservations", [])
        if type(reservations) not in {list, tuple}:
            lines.append("Active reservations: unavailable; malformed state")
            lines.append(
                "Action required: inspect or restore reservation state before relying on exposure status"
            )
        else:
            malformed_reservations = False
            valid_reservations = 0
            reservation_lines: list[str] = []
            for item in reservations:
                if type(item) is not dict:
                    malformed_reservations = True
                    reservation_lines.append(
                        "Reservation detail: unavailable; malformed state"
                    )
                    continue
                remaining = item.get("remaining")
                raw_state = item.get("state")
                state_text = _safe_text(raw_state, "")
                state_readable = state_text in {"WORKING", "UNKNOWN"}
                if not state_readable:
                    malformed_reservations = True
                    state_text = "Unavailable"
                    reservation_lines.append(
                        "Reservation state: unavailable; malformed value"
                    )
                    # Once reservation state is not canonical, remaining-resource
                    # values are not exposure truth and must not be announced.
                    continue
                if type(remaining) is not dict:
                    malformed_reservations = True
                    reservation_lines.append(
                        f"Reservation detail: unavailable; state: {state_text}; malformed remaining resources"
                    )
                    continue
                reservation_readable = state_readable
                for resource, amount in remaining.items():
                    resource_text = _reservation_resource_text(resource)
                    amount_text = _reservation_amount_text(amount)
                    if not resource_text or not amount_text:
                        malformed_reservations = True
                        reservation_readable = False
                        reservation_lines.append(
                            "Reservation resource detail: unavailable; malformed value"
                        )
                        continue
                    reservation_lines.append(
                        f"Reserved {resource_text}: {amount_text}; state: {state_text}"
                    )
                if reservation_readable:
                    valid_reservations += 1
            if malformed_reservations:
                lines.append(
                    "Active reservations: unavailable; one or more reservation entries are malformed"
                )
                lines.append(f"Structurally readable reservation entries: {valid_reservations}")
                lines.append(
                    "Action required: inspect or restore reservation state before relying on exposure status"
                )
            else:
                lines.append(f"Active reservations: {valid_reservations}")
            lines.extend(reservation_lines)
        if state == "awaiting_order_reconciliation":
            lines.append("Action required: confirm the terminal order state; a reconciled fill does not confirm order completion")

    if economic_report is not None:
        if type(economic_report) is not dict:
            lines.append("Economic report: unavailable; malformed state")
        else:
            lines.extend(
                [
                    f"Final equity: {_value(economic_report, 'final_equity')}",
                    f"Net profit or loss: {_value(economic_report, 'net_pnl')}",
                    f"Total fees: {_value(economic_report, 'total_fees')}",
                    f"Turnover: {_value(economic_report, 'turnover')}",
                    f"Maximum drawdown: {_value(economic_report, 'max_drawdown')}",
                    f"Economic reconciliation: {'passed' if economic_report.get('reconciled') is True else 'not confirmed'}",
                ]
            )
            valuation_status = _safe_text(
                economic_report.get("valuation_status"),
                "",
            )
            if valuation_status == "MARK_UNAVAILABLE":
                lines.append("Portfolio valuation and profit or loss: unavailable; no retained current market mark")

    lines.append("Economic edge: unproven")
    return "\n".join(lines)
