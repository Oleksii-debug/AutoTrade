"""Strict decoder for canonical durable target-host financial bindings.

Encoding/digest authority stays in ``runtime_target_host_durable_financial``.
This module only provides an independent fail-closed bytes admission boundary for
retained/signed artifacts; it does not collect measurements or evaluate budgets.
"""

from __future__ import annotations

import json

from .runtime_target_host_durable_financial import (
    DurableFinancialIdentityBinding,
    DurableTargetHostFinancialBinding,
    RuntimeTargetHostDurableFinancialError,
)


_BINDING_FIELDS = frozenset(
    {
        "bindings",
        "clock_contract_id",
        "declared_plan_digest",
        "declared_plan_id",
        "schema_version",
        "source_sha",
        "spec_digest",
        "target_host_measurement_digest",
    }
)
_IDENTITY_FIELDS = frozenset(
    {
        "durable_latency_sample_digest",
        "event_id",
        "event_journal_sequence",
        "event_payload_hash",
        "latency_end_monotonic_ns",
        "latency_measurement_event_id",
        "latency_measurement_journal_sequence",
        "latency_start_monotonic_ns",
        "latency_us",
        "target_sample_id",
    }
)


def _reject_duplicate_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeTargetHostDurableFinancialError(
                "durable financial binding contains duplicate JSON object key"
            )
        result[key] = value
    return result


def parse_durable_target_host_financial_binding(
    raw: bytes,
) -> DurableTargetHostFinancialBinding:
    """Parse exact canonical JSON bytes into one immutable durable binding."""

    if type(raw) is not bytes or not raw:
        raise RuntimeTargetHostDurableFinancialError(
            "durable financial binding must be non-empty bytes"
        )
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_object,
            parse_constant=lambda token: (_ for _ in ()).throw(
                RuntimeTargetHostDurableFinancialError(
                    f"durable financial binding contains invalid JSON constant {token}"
                )
            ),
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeTargetHostDurableFinancialError(
            "durable financial binding is not valid UTF-8 JSON"
        ) from error
    if type(value) is not dict or frozenset(value) != _BINDING_FIELDS:
        raise RuntimeTargetHostDurableFinancialError(
            "durable financial binding fields are non-canonical"
        )
    nested = value["bindings"]
    if type(nested) is not list:
        raise RuntimeTargetHostDurableFinancialError(
            "durable financial binding entries must be an array"
        )
    bindings: list[DurableFinancialIdentityBinding] = []
    for item in nested:
        if type(item) is not dict or frozenset(item) != _IDENTITY_FIELDS:
            raise RuntimeTargetHostDurableFinancialError(
                "durable financial identity binding fields are non-canonical"
            )
        bindings.append(DurableFinancialIdentityBinding(**item))

    binding = DurableTargetHostFinancialBinding(
        target_host_measurement_digest=value["target_host_measurement_digest"],
        source_sha=value["source_sha"],
        spec_digest=value["spec_digest"],
        declared_plan_id=value["declared_plan_id"],
        declared_plan_digest=value["declared_plan_digest"],
        clock_contract_id=value["clock_contract_id"],
        bindings=tuple(bindings),
        schema_version=value["schema_version"],
    )
    if binding.canonical_bytes() != raw:
        raise RuntimeTargetHostDurableFinancialError(
            "durable financial binding bytes are not canonical JSON"
        )
    return binding
