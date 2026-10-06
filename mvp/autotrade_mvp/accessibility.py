"""Plain-text accessibility helpers for the simulated AutoTrade command surface.

This module is deliberately free of ANSI color, cursor movement, and
visual-only symbols so keyboard and screen-reader users receive the same facts.
It is a development fallback surface, not NVDA release qualification.
"""

from __future__ import annotations

from typing import Any


STATE_TEXT = {
    "not_started": "Not started",
    "running": "Running",
    "needs_recovery": "Needs recovery",
    "corrupt": "Corrupt or unreadable state",
    "completed": "Completed",
    "awaiting_order_reconciliation": "Awaiting order reconciliation",
    "busy": "State is changing; read again",
}


def _plain_text(value: Any, default: str = "Unavailable") -> str:
    """Render only inert built-in scalar values.

    The accessibility fallback is an operator-facing failure surface. It must
    remain readable even when upstream state is malformed, and it must not call
    caller-controlled __str__ implementations while trying to explain that
    malformed state.
    """

    if type(value) is str:
        if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
            return default
        return value
    if type(value) is int:
        return str(value)
    if type(value) is float:
        return str(value)
    if type(value) is bool:
        return "true" if value else "false"
    return default


def _value(mapping: dict[str, Any] | None, key: str, default: str = "Unavailable") -> str:
    if type(mapping) is not dict:
        return default
    return _plain_text(mapping.get(key), default)


def _replay_verification_text(value: Any) -> str:
    if value is True:
        return "passed"
    if value is False:
        return "failed"
    return "unavailable"


def accessible_status_state(status: Any) -> str:
    """Return the exact operator state, failing closed to corrupt."""

    if type(status) is not dict:
        return "corrupt"
    state = status.get("status", "corrupt")
    if type(state) is not str or state not in STATE_TEXT:
        return "corrupt"
    return state


def _reservation_lines(status: dict[str, Any]) -> list[str]:
    reservations = status.get("active_reservations", [])
    if type(reservations) is not list:
        return ["Active reservations: unavailable"]

    lines = [f"Active reservations: {len(reservations)}"]
    for index, item in enumerate(reservations, start=1):
        if type(item) is not dict:
            lines.append(f"Reservation {index}: unavailable")
            continue
        remaining = item.get("remaining")
        state = _plain_text(item.get("state"))
        if type(remaining) is not dict:
            lines.append(f"Reservation {index}: unavailable; state: {state}")
            continue
        if not remaining:
            lines.append(f"Reservation {index}: no remaining resources; state: {state}")
            continue
        for resource, amount in remaining.items():
            resource_text = _plain_text(resource)
            amount_text = _plain_text(amount)
            lines.append(
                f"Reserved {resource_text}: {amount_text}; state: {state}"
            )
    return lines


def _cash_bucket_lines(economic_report: dict[str, Any]) -> list[str]:
    buckets = economic_report.get("cash_buckets")
    if type(buckets) is not dict:
        return []

    currency = _value(buckets, "currency")
    return [
        f"Гроші на рахунку ({currency}): {_value(buckets, 'account_cash')}",
        f"Розраховані кошти: {_value(buckets, 'settled_cash')}",
        f"Нерозраховані надходження: {_value(buckets, 'unsettled_receivable')}",
        f"Нерозраховані зобов'язання: {_value(buckets, 'unsettled_payable')}",
        f"Зарезервовані кошти: {_value(buckets, 'reserved_cash')}",
        f"Реально доступні кошти: {_value(buckets, 'available_cash')}",
        f"Реалізований прибуток/збиток: {_value(economic_report, 'realized_pnl')}",
        f"Нереалізований прибуток/збиток: {_value(economic_report, 'unrealized_pnl')}",
    ]


def format_accessible_status(
    status: dict[str, Any],
    economic_report: dict[str, Any] | None = None,
) -> str:
    """Render a stable, copyable, screen-reader-friendly status summary."""

    state = accessible_status_state(status)
    if type(status) is not dict:
        status = {}
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
        lines.extend(
            [
                "Replay verification: unavailable",
                "Action required: read status again after the current operation",
                "Economic edge: unproven",
            ]
        )
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
        lines.append(
            "Action required: recovery or reconciliation is needed before trusting current state"
        )

    state_format = status.get("state_format")
    if type(state_format) is str and state_format == "canonical_journal":
        lines.extend(
            [
                f"Episode: {_value(status, 'episode_id')}",
                f"Session outcome: {_value(status, 'session_status')}",
            ]
        )
        if "completed_episodes" in status:
            lines.extend(
                [
                    f"Autonomous episodes: {_value(status, 'completed_episodes')} of {_value(status, 'total_episodes')}",
                    f"Model mode: {_value(status, 'mode')}",
                ]
            )
        lines.extend(
            [
                f"Cash (USD): {_value(status, 'cash')}",
                f"Position (shares): {_value(status, 'position')}",
                f"Journal sequence: {_value(status, 'journal_sequence')}",
                "Order submission during this read: none",
            ]
        )
        lines.extend(_reservation_lines(status))
        if state == "awaiting_order_reconciliation":
            lines.append(
                "Action required: confirm the terminal order state; a reconciled fill does not confirm order completion"
            )
        session_status = status.get("session_status")
        if type(session_status) is str and session_status == "BLOCKED":
            lines.append(f"Blocked reason: {_value(status, 'reason')}")

    if type(economic_report) is dict:
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
        valuation_status = economic_report.get("valuation_status")
        if type(valuation_status) is str and valuation_status == "MARK_UNAVAILABLE":
            lines.append(
                "Portfolio valuation and profit or loss: unavailable; no retained current market mark"
            )
        lines.extend(_cash_bucket_lines(economic_report))

    lines.append("Economic edge: unproven")
    return "\n".join(lines)
