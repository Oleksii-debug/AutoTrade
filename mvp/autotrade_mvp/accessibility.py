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


def _value(mapping: dict[str, Any] | None, key: str, default: str = "Unavailable") -> str:
    if mapping is None:
        return default
    value = mapping.get(key)
    if value is None:
        return default
    return str(value)


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

    if state == "busy":
        lines.extend(["Replay verification: unavailable",
                      "Action required: read status again after the current operation",
                      "Economic edge: unproven"])
        return "\n".join(lines)

    replay_verified = status.get("replay_verified")
    lines.extend(
        [
            f"Replay verification: {_replay_verification_text(replay_verified)}",
            f"Instrument: {_value(status, 'symbol')}",
            f"Initial capital: {_value(status, 'initial_cash')}",
            f"Recorded evidence items: {_value(status, 'evidence_count', '0')}",
            f"Recorded fills: {len(status.get('fills', {})) if isinstance(status.get('fills'), dict) else 'Unavailable'}",
        ]
    )

    if state == "needs_recovery":
        lines.append("Action required: recovery or reconciliation is needed before trusting current state")

    if status.get("state_format") == "canonical_journal":
        lines.extend([
            f"Episode: {_value(status, 'episode_id')}",
            f"Session outcome: {_value(status, 'session_status')}",
            *([f"Autonomous episodes: {status['completed_episodes']} of {status['total_episodes']}",
               f"Model mode: {status.get('mode', 'Unavailable')}"] if 'completed_episodes' in status else []),
            f"Cash (USD): {_value(status, 'cash')}",
            f"Position (shares): {_value(status, 'position')}",
            f"Journal sequence: {_value(status, 'journal_sequence')}",
            "Order submission during this read: none",
        ])
        reservations = status.get("active_reservations", [])
        lines.append(f"Active reservations: {len(reservations)}")
        for item in reservations:
            for resource, amount in item["remaining"].items():
                lines.append(f"Reserved {resource}: {amount}; state: {item['state']}")
        if state == "awaiting_order_reconciliation":
            lines.append("Action required: confirm the terminal order state; a reconciled fill does not confirm order completion")
        if status.get("session_status") == "BLOCKED":
            lines.append(f"Blocked reason: {_value(status, 'reason')}")

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
        if economic_report.get("valuation_status") == "MARK_UNAVAILABLE":
            lines.append("Portfolio valuation and profit or loss: unavailable; no retained current market mark")
        if isinstance(economic_report.get("cash_buckets"), dict):
            buckets = economic_report["cash_buckets"]
            lines.extend([
                f"Гроші на рахунку ({buckets['currency']}): {buckets['account_cash']}",
                f"Розраховані кошти: {buckets['settled_cash']}",
                f"Нерозраховані надходження: {buckets['unsettled_receivable']}",
                f"Нерозраховані зобов'язання: {buckets['unsettled_payable']}",
                f"Зарезервовані кошти: {buckets['reserved_cash']}",
                f"Реально доступні кошти: {buckets['available_cash']}",
                f"Реалізований прибуток/збиток: {economic_report['realized_pnl']}",
                f"Нереалізований прибуток/збиток: {economic_report['unrealized_pnl']}",
            ])

    lines.append("Economic edge: unproven")
    return "\n".join(lines)
