"""Plain-text accessibility helpers for the simulated AutoTrade command surface.

This module is deliberately free of ANSI color, cursor movement, and
visual-only symbols so keyboard and screen-reader users receive the same facts.
It is a development fallback surface, not NVDA release qualification.
"""

from __future__ import annotations

from math import isfinite
from typing import Any

from ._generated_common_scalars import is_valid_common_scalar
from .exact_decimal import (
    ExactDecimalError,
    exact_sum,
    exact_subtract,
    parse_canonical_decimal_text,
)


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


def _has_exact_text_keys(value: Any) -> bool:
    """Inspect exact dict keys without invoking caller-owned key methods."""

    return type(value) is dict and all(type(key) is str for key in value)


def _is_zero_mode(status: dict[str, Any]) -> bool:
    mode = status.get("mode")
    return type(mode) is str and mode == "ZERO"


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


def _canonical_decimal_value(
    mapping: dict[str, Any] | None,
    key: str,
    default: str = "Unavailable",
) -> str:
    if mapping is None:
        return default
    value = mapping.get(key)
    try:
        parse_canonical_decimal_text(value)
    except ExactDecimalError:
        return default
    return value


def _canonical_sequence_value(
    mapping: dict[str, Any] | None,
    key: str,
    default: str = "Unavailable",
) -> str:
    if mapping is None:
        return default
    value = mapping.get(key)
    if type(value) is int and value >= 0:
        return str(value)
    return value if is_valid_common_scalar("Sequence", value) else default


def _value(mapping: dict[str, Any] | None, key: str, default: str = "Unavailable") -> str:
    if mapping is None:
        return default
    return _safe_text(mapping.get(key), default)


def _canonical_economic_report_is_readable(
    report: dict[str, Any],
    *,
    status: dict[str, Any],
) -> bool:
    if (_is_zero_mode(status) and type(report.get("evidence_class")) is str
            and report["evidence_class"] == "SIMULATION"):
        return _canonical_zero_economic_report_is_readable(report, status=status)
    status_journal_sequence = status.get("journal_sequence")
    report_journal_sequence = report.get("journal_sequence")
    if report.get("reconciled") is not True:
        return False
    if (
        not is_valid_common_scalar("Sequence", status_journal_sequence)
        or not is_valid_common_scalar("Sequence", report_journal_sequence)
        or report_journal_sequence != status_journal_sequence
    ):
        return False

    for key, scalar_kind in (
        ("environment", "Environment"),
        ("currency", "CurrencyId"),
    ):
        status_value = status.get(key)
        report_value = report.get(key)
        if (
            not is_valid_common_scalar(scalar_kind, status_value)
            or not is_valid_common_scalar(scalar_kind, report_value)
            or report_value != status_value
        ):
            return False

    matched_values = {}
    for report_key, status_key in (
        ("initial_equity", "initial_cash"),
        ("cash", "cash"),
        ("ending_position", "position"),
    ):
        report_value = report.get(report_key)
        status_value = status.get(status_key)
        try:
            parsed_report = parse_canonical_decimal_text(report_value)
            parsed_status = parse_canonical_decimal_text(status_value)
        except ExactDecimalError:
            return False
        if report_value != status_value or parsed_report != parsed_status:
            return False
        matched_values[report_key] = parsed_report

    try:
        total_fees = parse_canonical_decimal_text(report.get("total_fees"))
        turnover = parse_canonical_decimal_text(report.get("turnover"))
        unvalued_drawdown = (
            report.get("valuation_status") == "MARK_UNAVAILABLE"
            and report.get("drawdown_status") == "MARK_UNAVAILABLE"
            and "max_drawdown" in report
            and report["max_drawdown"] is None
        )
        max_drawdown = (0 if unvalued_drawdown
                        else parse_canonical_decimal_text(report.get("max_drawdown")))
    except ExactDecimalError:
        return False
    if total_fees < 0 or turnover < 0 or max_drawdown < 0:
        return False

    fill_count = _canonical_fill_count(
        status.get("fills", {}),
        expected_instrument=status.get("symbol"),
    )
    trade_count = report.get("trade_count")
    if (
        fill_count is None
        or type(trade_count) is not int
        or trade_count < 0
        or trade_count != fill_count
    ):
        return False

    valuation_status = _safe_text(report.get("valuation_status"), "")
    ending_position = matched_values["ending_position"]
    if valuation_status == "CASH_ONLY":
        if ending_position != 0:
            return False
        try:
            final_equity = parse_canonical_decimal_text(report.get("final_equity"))
            net_pnl = parse_canonical_decimal_text(report.get("net_pnl"))
        except ExactDecimalError:
            return False
        try:
            expected_net_pnl = exact_subtract(
                final_equity,
                matched_values["initial_equity"],
            )
        except ExactDecimalError:
            return False
        if (
            final_equity != matched_values["cash"]
            or net_pnl != expected_net_pnl
        ):
            return False
    elif valuation_status == "MARK_UNAVAILABLE":
        if (
            ending_position == 0
            or report.get("final_equity") is not None
            or report.get("net_pnl") is not None
        ):
            return False
    else:
        return False
    return True


def _canonical_zero_economic_report_is_readable(
    report: dict[str, Any], *, status: dict[str, Any]
) -> bool:
    """Validate the current autonomous ZERO report against one journal cut."""
    environment = status.get("environment")
    report_environment = report.get("environment")
    edge = report.get("economic_edge_status")
    if (
        report.get("reconciled") is not True
        or report.get("financial_equality_verified") is not True
        or type(edge) is not str or edge != "INCONCLUSIVE"
        or status.get("replay_verified") is not True
        or type(environment) is not str or environment != "SIMULATION"
        or type(report_environment) is not str or report_environment != environment
        or type(status.get("journal_sequence")) is not int
        or status["journal_sequence"] < 0
        or type(report.get("journal_sequence")) is not int
        or report["journal_sequence"] != status["journal_sequence"]
    ):
        return False
    buckets = report.get("cash_buckets")
    if (not _has_exact_text_keys(buckets)
            or type(buckets.get("currency")) is not str
            or buckets["currency"] != "USD"):
        return False
    for report_key, status_key in (
        ("initial_equity", "initial_cash"),
        ("cash", "cash"),
        ("ending_position", "position"),
    ):
        report_value = report.get(report_key)
        status_value = status.get(status_key)
        if (type(report_value) is not str or type(status_value) is not str
                or report_value != status_value):
            return False
    try:
        values = {
            key: parse_canonical_decimal_text(report.get(key))
            for key in (
                "initial_equity", "cash", "ending_position", "final_equity",
                "net_pnl", "realized_pnl", "unrealized_pnl", "total_fees",
                "turnover", "open_cost_basis",
            )
        }
        cash = {
            key: parse_canonical_decimal_text(buckets.get(key))
            for key in (
                "account_cash", "settled_cash", "unsettled_receivable",
                "unsettled_payable", "reserved_cash", "available_cash",
            )
        }
        if (
            values["cash"] != cash["account_cash"]
            or values["net_pnl"] != exact_subtract(
                values["final_equity"], values["initial_equity"]
            )
            or values["net_pnl"] != exact_subtract(
                exact_sum((values["realized_pnl"], values["unrealized_pnl"])),
                values["total_fees"],
            )
            or cash["account_cash"] != exact_subtract(
                exact_sum((cash["settled_cash"], cash["unsettled_receivable"])),
                cash["unsettled_payable"],
            )
            or cash["available_cash"] != max(0, exact_subtract(
                exact_subtract(cash["settled_cash"], cash["unsettled_payable"]),
                cash["reserved_cash"],
            ))
        ):
            return False
    except ExactDecimalError:
        return False
    return (
        values["total_fees"] >= 0
        and values["turnover"] >= 0
        and values["open_cost_basis"] >= 0
        and all(cash[key] >= 0 for key in (
            "settled_cash", "unsettled_receivable", "unsettled_payable", "reserved_cash",
        ))
    )


def _canonical_evidence_count(status: dict[str, Any]) -> str:
    evidence_count = status.get("evidence_count")
    if _is_zero_mode(status):
        completed = status.get("completed_episodes")
        if type(evidence_count) is int and type(completed) is int and evidence_count == completed and completed >= 0:
            return str(evidence_count)
    if (
        type(evidence_count) is int
        and evidence_count >= 0
        and str(evidence_count)
        == _canonical_sequence_value(status, "journal_sequence", "")
    ):
        return str(evidence_count)
    return "Unavailable"


def _canonical_fill_count(
    value: Any,
    *,
    expected_instrument: Any,
) -> int | None:
    if (
        type(expected_instrument) is not str
        or not expected_instrument
        or expected_instrument != expected_instrument.strip()
        or not _has_exact_text_keys(value)
    ):
        return None
    for fill_id, payload in value.items():
        if type(fill_id) is not str or not fill_id or fill_id != fill_id.strip():
            return None
        if not _has_exact_text_keys(payload):
            return None
        instrument = payload.get("instrument")
        quantity = payload.get("quantity")
        if (
            type(instrument) is not str
            or instrument != expected_instrument
        ):
            return None
        try:
            parsed_quantity = parse_canonical_decimal_text(quantity)
        except ExactDecimalError:
            return None
        if parsed_quantity <= 0:
            return None
    return len(value)


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

    if not _has_exact_text_keys(status):
        status = {"status": "corrupt"}
    has_state_format = "state_format" in status
    state_format = (
        _safe_text(status.get("state_format"), "")
        if has_state_format
        else ""
    )
    state = _safe_text(status.get("status", "corrupt"), "corrupt")
    if (
        state not in STATE_TEXT
        or (has_state_format and state_format != "canonical_journal")
    ):
        # Legacy status has no state_format key. An explicitly present but
        # unknown/malformed format is therefore damaged canonical state, not
        # permission to downgrade financial fields to the legacy renderer.
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
    if state_format == "canonical_journal":
        canonical_fill_count = _canonical_fill_count(
            fills,
            expected_instrument=status.get("symbol"),
        )
        recorded_fills = (
            canonical_fill_count
            if canonical_fill_count is not None
            else "Unavailable"
        )
    else:
        recorded_fills = len(fills) if type(fills) is dict else "Unavailable"
    initial_capital = (
        _canonical_decimal_value(status, "initial_cash")
        if state_format == "canonical_journal"
        else _value(status, "initial_cash")
    )
    lines.extend(
        [
            f"Replay verification: {_replay_verification_text(replay_verified)}",
            f"Instrument: {_value(status, 'symbol')}",
            f"Initial capital: {initial_capital}",
            "Recorded evidence items: "
            + (
                _canonical_evidence_count(status)
                if state_format == "canonical_journal"
                else _value(status, "evidence_count", "0")
            ),
            f"Recorded fills: {recorded_fills}",
        ]
    )

    if "completed_episodes" in status:
        completed_episodes = status.get("completed_episodes")
        total_episodes = status.get("total_episodes")
        completed_text = (
            str(completed_episodes)
            if type(completed_episodes) is int and completed_episodes >= 0
            else "Unavailable"
        )
        total_text = (
            str(total_episodes)
            if type(total_episodes) is int and total_episodes >= 0
            else "Unavailable"
        )
        if (
            type(completed_episodes) is int
            and type(total_episodes) is int
            and (completed_episodes < 0 or total_episodes < 0 or completed_episodes > total_episodes)
        ):
            completed_text = "Unavailable"
            total_text = "Unavailable"
        mode = status.get("mode")
        mode_text = (
            mode.strip()
            if type(mode) is str and mode.strip()
            else "Unavailable"
        )
        lines.extend(
            [
                f"Autonomous episodes: {completed_text} of {total_text}",
                f"Model mode: {mode_text}",
            ]
        )

    if state == "needs_recovery":
        lines.append("Action required: recovery or reconciliation is needed before trusting current state")

    if state_format == "canonical_journal":
        lines.extend([
            f"Episode: {_value(status, 'episode_id')}",
            f"Session outcome: {_value(status, 'session_status')}",
            f"Cash (USD): {_canonical_decimal_value(status, 'cash')}",
            f"Position (shares): {_canonical_decimal_value(status, 'position')}",
            f"Journal sequence: {_canonical_sequence_value(status, 'journal_sequence')}",
            "Order submission during this read: none",
        ])
        if _safe_text(status.get("session_status"), "") == "BLOCKED":
            lines.append(f"Blocked reason: {_value(status, 'reason')}")
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
                if not _has_exact_text_keys(item):
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
        if not _has_exact_text_keys(economic_report):
            lines.append("Economic report: unavailable; malformed state")
        else:
            canonical_report_readable = (
                _canonical_economic_report_is_readable(
                    economic_report,
                    status=status,
                )
                if state_format == "canonical_journal"
                else True
            )
            if state_format == "canonical_journal":
                report_value = (
                    _canonical_decimal_value
                    if canonical_report_readable
                    else lambda _mapping, _key: "Unavailable"
                )
            else:
                report_value = _value
            reconciliation_passed = (
                economic_report.get("reconciled") is True
                and canonical_report_readable
                and (
                    state_format != "canonical_journal"
                    or status.get("reconciled") is True
                    or (_is_zero_mode(status) and status.get("replay_verified") is True)
                )
            )
            lines.extend(
                [
                    f"Final equity: {report_value(economic_report, 'final_equity')}",
                    f"Net profit or loss: {report_value(economic_report, 'net_pnl')}",
                    f"Total fees: {report_value(economic_report, 'total_fees')}",
                    f"Turnover: {report_value(economic_report, 'turnover')}",
                    f"Maximum drawdown: {report_value(economic_report, 'max_drawdown')}",
                    f"Economic reconciliation: {'passed' if reconciliation_passed else 'not confirmed'}",
                ]
            )
            if not canonical_report_readable:
                lines.append(
                    "Economic report validation: unavailable; malformed or incomplete canonical state"
                )
            valuation_status = _safe_text(
                economic_report.get("valuation_status"),
                "",
            )
            if canonical_report_readable and valuation_status == "MARK_UNAVAILABLE":
                lines.append("Portfolio valuation and profit or loss: unavailable; no retained current market mark")
            cash_buckets = economic_report.get("cash_buckets")
            if (
                canonical_report_readable
                and _has_exact_text_keys(cash_buckets)
                and is_valid_common_scalar("CurrencyId", cash_buckets.get("currency"))
            ):
                lines.extend(
                    [
                        f"Account cash ({cash_buckets['currency']}): {_canonical_decimal_value(cash_buckets, 'account_cash')}",
                        f"Settled cash: {_canonical_decimal_value(cash_buckets, 'settled_cash')}",
                        f"Unsettled receivable: {_canonical_decimal_value(cash_buckets, 'unsettled_receivable')}",
                        f"Unsettled payable: {_canonical_decimal_value(cash_buckets, 'unsettled_payable')}",
                        f"Reserved cash: {_canonical_decimal_value(cash_buckets, 'reserved_cash')}",
                        f"Available cash: {_canonical_decimal_value(cash_buckets, 'available_cash')}",
                        f"Realized profit or loss: {_canonical_decimal_value(economic_report, 'realized_pnl')}",
                        f"Unrealized profit or loss: {_canonical_decimal_value(economic_report, 'unrealized_pnl')}",
                    ]
                )
                if state_format == "canonical_journal" and _is_zero_mode(status):
                    lines.append(
                        f"Реально доступні кошти: {_canonical_decimal_value(cash_buckets, 'available_cash')}"
                    )

    lines.append("Economic edge: unproven")
    return "\n".join(lines)
