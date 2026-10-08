# AutoTrade — Critical Multi-Plan Audit — 2026-10-08

## Verdict

The old 48-Section global sequential execution model has been replaced by exactly 9 plans.

- Plans 1–7: independent engineering plans, no priority order. All seven are now actionable at source/offline level without owner provider credentials; Plan 6 uses public specs + existing code + fixtures only.
- Plan 8: provider-free M1 convergence; depends on terminal M1-required outputs from Plans 1,2,3,4,5,7. Plan 6 and all real provider input are not required.
- Plan 9: final dependency-aware release/NVDA/M2-PF DAG with a separately parked optional provider/PAPER/LIVE activation track.

## Completeness

- Legacy Sections checked: 48/48.
- Unmapped legacy Sections: 0.
- Detailed mapping: LEGACY_48_TO_MULTIPLAN_COVERAGE.md.
- New Drive plan Sections: 71 total, all numbered from 1; no new Section 0.

## Migrated closure truth

- Legacy 0–1 and 3–14 keep accepted terminal engineering evidence.
- Legacy 2 is split:
  - repository-controllable dependency/provenance engineering is internally complete in Plan 4 / Section 2;
  - external/final delivered-release rights/trust/SBOM facts remain honest WAITING_EXTERNAL in Plan 9 / Section 1.
- Legacy 15 -> Plan 1 / Section 10 QUALIFYING; canonical PR #2318 is preserved.
- Legacy 16 -> Plan 2 / Section 2 PARTIAL_EXISTING; PR #1631 preserved.
- Legacy 17 -> Plan 1 / Section 11 PARTIAL_EXISTING; PR #1628 preserved.
- Legacy 18–36 partial/merged/open source is preserved by its new owner plan rather than treated as greenfield work.
- Legacy 38–40 source/provider-adapter engineering is now allowed offline without real provider/account input. Real authenticated provider/PAPER/LIVE evidence remains parked in Plan 9.

## Parallelism rules

Plans 1–7 can start and finish in any order.
Plan 6 may also close its adapter engineering offline without credentials/accounts; real provider activation is a separate evidence class.
They may use frozen contracts/fixtures for peer outputs and reach component DONE before a peer plan is terminal.
Fixture evidence never counts as M1/PAPER/LIVE/physical NVDA/final-release evidence.

Mutation surfaces and conflict keys are defined in MULTI_PLAN_PARALLELISM_CONTRACT.md to reduce cross-plan PR collisions.

## M1 dependency graph

Plan 8 terminal provider-free convergence waits on:
- Plan 1 — financial/portfolio/economics;
- Plan 2 — agent/research/learning/models;
- Plan 3 — runtime/recovery/host;
- Plan 4 — trust/security/chronology/supply-chain engineering;
- Plan 5 — Web/Desktop/accessibility/packaging source;
- Plan 7 — qualification/science/performance.

Plan 6 providers is explicitly NOT an M1 dependency, even if its offline engineering is unfinished. M1 must run normally with providers NOT_CONFIGURED/UNAVAILABLE.

## Final dependency graph

Plan 9 is not a blanket "after everything" sequence.
Examples:
- final external provenance can close whenever the real release facts arrive;
- Sections 2–6 (provider binding, provider-backed truth, PAPER, forward economic claim, bounded-real) are PARKED_OPTIONAL_PROVIDER_TRACK and do not block provider-free release;
- signed Windows release consumes Plan 4 + Plan 5 + Plan 8 + release-rights/provenance evidence, but no provider credentials when provider capability is NOT_CLAIMED;
- physical NVDA waits for the exact signed provider-free artifact;
- final matrix may explicitly mark provider/PAPER/LIVE rows NOT_ACTIVATED/NOT_CLAIMED;
- M2-PF can close for the provider-free claimed scope; later provider activation creates a successor qualification without reopening unrelated engineering plans.

## Existing candidate topology

Multi-plan coordination commits advanced main while the old Section-15 PR stayed frozen.
At migration readback:
- PR #2318 head: 7662ddbc8226965f4676684f7309d40f882ddaed
- current main after authority switch: efed52b213039f88e03d33561c9d1429cb4f0371

Do not mutate the frozen Section-15 candidate merely because target main advanced through control-only commits. After functional qualification, perform one fresh target-topology/reconvergence step, integrate, and read back.

## Expected speedup

This architecture exposes seven immediately active independent engineering fronts (Plans 1–7). Plan 6 is offline/fixture engineering and requires no owner credentials.
The actual wall-clock gain depends on worker count, scope balance, CI/merge throughput and conflict avoidance.
It removes the artificial requirement that Plan 5 or Plan 7 wait for legacy Section 15.

## Canonical UI decision

The existing `web/src` interface is the canonical product UI. It already contains semantic headings/sections, primary navigation, skip-link, forms, tables, live regions and keyboard table tooling. Plan 5 now treats this browser-like Web UI as the single navigation model. Windows Desktop embeds the same Web artifact and adds only host integration/native emergency fallback, avoiding a second divergent UX.

## Count verification

Drive readback after the corrections: 9 plans, 71 Sections, 213 numbered Subsections, zero new Section 0.
