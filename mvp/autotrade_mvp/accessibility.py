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
}


def _value(mapping: dict[str, Any] | None, key: str, default: str = "Unavailable") -> str:
    if mapping is None:
        return default
    value = mapping.get(key)
    if value is None:
        return default
    return str(value)


def format_accessible_status(
    status: dict[str, Any],
    economic_report: dict[str, Any] | None = None,
) -> str:
    """Render a stable, copyable, screen-reader-friendly status summary."""

    state = str(status.get("status", "corrupt"))
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

    replay_verified = status.get("replay_verified")
    lines.extend(
        [
            f"Replay verification: {'passed' if replay_verified is True else 'failed'}",
            f"Instrument: {_value(status, 'symbol')}",
            f"Initial capital: {_value(status, 'initial_cash')}",
            f"Recorded evidence items: {_value(status, 'evidence_count', '0')}",
            f"Recorded fills: {len(status.get('fills', {})) if isinstance(status.get('fills'), dict) else 'Unavailable'}",
        ]
    )

    if state == "needs_recovery":
        lines.append("Action required: recovery or reconciliation is needed before trusting current state")

    if economic_report is not None:
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

    lines.append("Economic edge: unproven")
    return "\n".join(lines)
