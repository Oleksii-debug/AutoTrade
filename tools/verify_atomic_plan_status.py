"""Read the canonical Plan 9/10 atomic registry without ambiguous zero counts.

The registry is Markdown because it is also an owner-readable control record.
This helper gives automation a small fail-closed projection of that record:
``total_sections`` and ``completed_sections`` are always emitted alongside
``unfinished_sections``.  A zero can therefore never be mistaken for an empty
plan.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
import re
import sys
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = ROOT / "ATOMIC_OPEN_SECTIONS_2026-10-13.md"

_PLAN_HEADING = re.compile(r"^##\s+План\s+(9|10):", re.IGNORECASE)
_ROW = re.compile(r"^\|\s*([^|]+?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*$")
_SECTION_ID = re.compile(r"^(?:[1-9]|10)[АБВГ]$")
_TERMINAL_MARKERS = (
    "DONE",
    "TERMINALLY NOT_CLAIMED",
    "TERMINALLY NOT_ACTIVATED",
)


class PlanRegistryError(ValueError):
    """The canonical atomic registry is malformed or internally inconsistent."""


@dataclass(frozen=True)
class AtomicSection:
    section_id: str
    description: str
    state: str

    @property
    def terminal(self) -> bool:
        normalized = self.state.upper()
        return any(marker in normalized for marker in _TERMINAL_MARKERS)


def _parse_rows(lines: Iterable[str]) -> dict[str, tuple[AtomicSection, ...]]:
    sections: dict[str, list[AtomicSection]] = {"9": [], "10": []}
    current_plan: str | None = None
    seen: dict[str, set[str]] = {"9": set(), "10": set()}
    for raw_line in lines:
        heading = _PLAN_HEADING.match(raw_line.strip())
        if heading:
            current_plan = heading.group(1)
            continue
        if raw_line.startswith("## ") and current_plan is not None:
            current_plan = None
            continue
        if current_plan is None:
            continue
        row = _ROW.match(raw_line.strip())
        if row is None:
            continue
        section_id, description, state = (value.strip() for value in row.groups())
        if not _SECTION_ID.fullmatch(section_id):
            continue
        if section_id in seen[current_plan]:
            raise PlanRegistryError(
                f"Plan {current_plan} contains duplicate section {section_id}"
            )
        if not description or not state:
            raise PlanRegistryError(
                f"Plan {current_plan} section {section_id} is incomplete"
            )
        seen[current_plan].add(section_id)
        sections[current_plan].append(AtomicSection(section_id, description, state))
    return {plan: tuple(rows) for plan, rows in sections.items()}


def atomic_plan_status(registry: Path = DEFAULT_REGISTRY) -> dict[str, object]:
    try:
        text = registry.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PlanRegistryError(f"cannot read atomic plan registry: {exc}") from exc

    parsed = _parse_rows(text.splitlines())
    expected = {"9": 30, "10": 23}
    plans: dict[str, object] = {}
    all_unfinished: list[str] = []
    for plan, expected_total in expected.items():
        rows = parsed[plan]
        if len(rows) != expected_total:
            raise PlanRegistryError(
                f"Plan {plan} has {len(rows)} atomic sections; expected {expected_total}"
            )
        terminal = tuple(row for row in rows if row.terminal)
        unfinished = tuple(row for row in rows if not row.terminal)
        unfinished_ids = [row.section_id for row in unfinished]
        all_unfinished.extend(f"{plan}/{section_id}" for section_id in unfinished_ids)
        plans[plan] = {
            "total_sections": len(rows),
            "completed_sections": len(terminal),
            "unfinished_sections": len(unfinished),
            "unfinished_section_ids": unfinished_ids,
        }

    total = sum(item["total_sections"] for item in plans.values())
    completed = sum(item["completed_sections"] for item in plans.values())
    return {
        "schema_version": "1.0.0",
        "source": registry.name,
        "plans": plans,
        "total_sections": total,
        "completed_sections": completed,
        "unfinished_sections": total - completed,
        "unfinished_section_ids": all_unfinished,
        "terminal_for_approved_private_zero_scope": not all_unfinished,
        "scope_note": (
            "Terminal applies to the approved PRIVATE/ZERO scope only; it does not "
            "claim provider account binding, PAPER/LIVE trading, or profitability."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Project unambiguous Plan 9/10 atomic closure counts as JSON."
    )
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument(
        "--check-terminal",
        action="store_true",
        help="return nonzero unless every approved PRIVATE/ZERO section is terminal",
    )
    args = parser.parse_args(argv)
    try:
        status = atomic_plan_status(args.registry)
    except PlanRegistryError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(status, ensure_ascii=False, sort_keys=True, indent=2))
    if args.check_terminal and not status["terminal_for_approved_private_zero_scope"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
