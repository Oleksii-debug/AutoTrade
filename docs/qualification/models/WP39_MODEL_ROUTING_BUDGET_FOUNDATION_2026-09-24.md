# WP-39 model routing and budget foundation — 2026-09-24

Status: **policy/budget foundation only; no model provider call and no trading authority**.

## Implemented boundary

The foundation adds one provider-neutral routing policy and one exact in-memory
budget projection. It does not add a second financial dispatcher, authority
service, journal or research scheduler.

Routing is fail-closed over:

- ZERO_LLM, LOCAL_ONLY, FIXED, ALLOWLIST and DYNAMIC modes;
- an explicit user-approved model-id set;
- explicit privacy-region permission for every remote model;
- task schema, modality and tool-permission compatibility;
- model context/output limits and a policy deadline;
- exact Decimal per-call limits and the remaining ledger ceiling;
- measured quality and current-price evidence identified by immutable SHA-256;
- stable deterministic tie-breaking.

A descriptor records provider, exact model name, revision when known, local or
remote location, task schemas, modalities, tool permissions, privacy region,
license identity, deterministic limitations, context/output limits, expected
latency, exact prices and measured-quality evidence. Unknown model revision is
reported as UNKNOWN_MODEL_REVISION; no fabricated revision hash is created.

## Budget invariant

Reserved, estimated-unbilled and incurred costs are separate. A planned call
must reserve its worst-case estimated cost before execution. Cancellation may
release only a pre-call reservation. After the call boundary a reservation can
move to estimated-unbilled and then to actual incurred billing. Observed provider
billing is never discarded merely because it exceeded an estimate: the overrun
remains visible and future reservations are blocked by zero remaining budget.

Binary floating-point monetary input is rejected.

## Authority invariant

ModelRoutingDecision.authorizes_trading is always false. This module neither
calls a model nor submits an order. If no model satisfies policy, privacy,
deadline, schema/tool/modality and budget constraints, the only outcomes are the
configured qualified deterministic fallback or NO_TRADE.

A remote model is never an automatic fallback from LOCAL_ONLY or ZERO_LLM.

## Evidence

Implementation:
- mvp/autotrade_mvp/model_routing.py

Regression coverage:
- mvp/tests/test_model_routing.py

Tests cover zero-model behavior, local-only isolation, allowlists, remote privacy
regions, exact Decimal reservation, deterministic quality/cost selection,
deadline/context/tool constraints, unknown revisions, hard pre-call budget
gates, reservation idempotency, unbilled/incurred separation and billing
overrun visibility.

## Still required for complete WP-39

This is not completion evidence for WP-39. Durable reservations/call records must
reuse the canonical WP-05/WP-06 journal/provenance authority after those
contracts are integrated. A provider-specific model client must record actual
model identity/revision returned by the provider where available, actual
latency, final billing evidence and schema validation. Fallback qualification
and matched shadow/ablation evidence remain separate scientific dependencies.
No release or economic-edge claim is made by this change.
