# WP-37 — causal episode availability integrity

Date: 2026-09-25

Original exact lineage base: `main@618c0422afaf71774f9af7f77889594d7e82b218`.

## Scope

This change hardens the existing canonical `ExperienceMemory`; it does not create a second memory store, model registry, science authority, promotion authority, or trading authority.

The qualification-facing population already excludes an episode that was not yet appended at the frozen causal cutoff. The remaining trust-boundary defect was that persisted episode `created_at` was not integrity-bound, so direct durable-state tampering could backdate availability and make a later episode appear inside an earlier frozen qualification population.

Normal `retrieve()` and `source_episode()` retain their existing evidence-time semantics: their `information_cutoff` constrains decision/evidence truth, not the physical SQLite append time. This hardening deliberately does not redefine those public retrieval semantics.

## Invariants

- New episodes persist an `availability_hash` binding exact `episode_id`, `episode_hash`, and canonical append `created_at`.
- The availability identity is verified at the same immutable episode trust boundary used by retrieval and qualification.
- Qualification population uses the verified append timestamp and therefore cannot be backdated by changing `created_at` alone.
- Existing-id and duplicate-content rows are integrity-verified before idempotent reuse.
- Legacy episode rows without availability identity fail closed and require explicit recovery; historical timestamps are not silently upgraded into trusted causal evidence.
- Evidence-time retrieval behavior, episode content identity, correction authority, tombstone semantics, permissions, negative/NULL/NO_TRADE retention, and trading authority are otherwise unchanged.

## Adversarial regression coverage

`research/tests/test_experience_memory.py` proves:

1. an episode appended now with historical decision/evidence timestamps does not enter a qualification population frozen before its append time;
2. direct DB backdating of `created_at` without the bound availability identity fails closed;
3. a legacy/null availability identity fails closed after restart rather than being silently blessed.

## Required exact-head evidence

Before merge, the exact PR head must pass:
- `baseline`;
- `research-primitives`;
- `science-qualification`;
- full `Verify AutoTrade`;
- `reconvergence-integrity`.

This is software-correctness/causality evidence only. It does not establish economic edge, provider qualification, release readiness, or live trading authority.
