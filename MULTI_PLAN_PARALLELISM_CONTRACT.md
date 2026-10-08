# AutoTrade — Multi-Plan Parallelism Contract

## Purpose

This file makes Plans 1–7 safely parallel. It is coordination authority, not an extra plan.

## Shared baseline

Accepted legacy Sections 0–1 and 3–14 provide stable provider-free contracts and financial/runtime authorities.
Plans 1–7 may use frozen fixtures/mocks for peer outputs and may reach component DONE without a peer implementation being terminal, provided:
1. the shared contract is not broken;
2. fixture evidence is not claimed as M1/PAPER/LIVE/physical evidence;
3. compatible arrival of the real peer implementation does not require reopening a terminal plan.

A breaking shared-contract change needs a new version, migration/compatibility evidence and explicit impact analysis.

## Default mutation ownership

### Plan 1 — financial / portfolio / risk / economics
Primary: accounting, allocation, valuation, reservations, risk, order projection/OMS economics, settlement/capital, corporate actions/financing, cost/capacity/economics.
Representative mvp surfaces: accounting.py, allocation*.py, risk*.py, reservations.py, settlement*.py, financing.py, funding.py, securities_borrow.py, order_projection.py, economics.py.
Conflict keys: financial-core, portfolio-allocation, economics.

### Plan 2 — agent / research / learning / models
Primary: product_agent.py, product_research.py, research/**, learning/experience modules, model_gateway/model billing/budget/call, champion policy/rollback, zero-model qualification.
Conflict keys: agent-loop, research-engine, model-gateway, champion-lifecycle.

### Plan 3 — runtime / recovery / backup / host / observability
Primary: persistence/runtime stores, recovery*.py, backup.py, product_runtime.py, production_host*.py, host API/network/actions, diagnostics.py, C# AutoTrade.Host.
Conflict keys: persistence-runtime, recovery, host-runtime, observability.

### Plan 4 — trust / security / chronology / supply chain
Primary: security.py, credential/session/vault boundaries, trusted_chronology*.py, provenance/**, supply_chain_qualification.py, qualification_attestation/trust/signature policy.
Conflict keys: security, chronology, supply-chain-trust.

### Plan 5 — Web / Desktop / accessibility / packaging
Primary: web/**, src/AutoTrade.Desktop/**, accessibility.py, embedded_web.py, packaging/windows/**, windows_update.py, product-shell composition.
Conflict keys: web-ui, desktop, accessibility-source, packaging-update.

### Plan 6 — providers
Primary: provider_*.py, bybit*, kraken*, whitebit*, binance*, alpaca*, ibkr*, provider transport/read/dispatch/reconciliation adapters.
Conflict keys: provider-core, provider-families, provider-qualification.
This plan is OWNER-DEFERRED; no new mutations until explicit reauthorization.

### Plan 7 — scientific qualification / evidence / performance
Primary: science_qualification.py, qualification/strategy_economics/**, performance_qualification.py, runtime_load_*.py, target-host qualification/evidence mapping.
Conflict keys: scientific-evidence, qualification-trust, performance-load.

### Plan 8 — M1 integration
Owns only integration bindings/scenarios/freeze required to assemble terminal Plans 1–5,7 into one provider-free product.
A generic component defect must be repaired in its owning plan rather than forked inside Plan 8.
Conflict key: m1-integration.

### Plan 9 — external/final
Owns real account/provider binding evidence, PAPER/forward/bounded-real campaigns, exact delivered release evidence, physical NVDA, final matrix and M2 freeze.
It does not fork generic source subsystems from Plans 1–7.
Conflict keys: external-qualification, final-release, m2-freeze.

## Shared files

Root project files, broad CI workflows, shared contracts, control registries and product composition files may be touched by multiple plans.
Rules:
- minimize shared-file edits;
- declare conflict key in handoff/PR;
- refresh main before integration;
- prefer additive/versioned adapters;
- if two plans need the same shared mutation, converge one minimal contract/integration change;
- never use a shared-file collision as a reason to resurrect global serialization.

## Existing work

Existing branches/PRs from the old sequential model are assets, not automatic authorities.
On activation:
1. refresh live PR/head/base;
2. identify the new owning plan/Section;
3. preserve unique required changes;
4. rebase/reconverge only when needed;
5. supersede duplicates;
6. never call an old PR DONE solely because it exists.

## Financial hard rules

Always preserve:
- reconciliation + durable journal establish financial truth;
- acknowledgement is not fill;
- UNKNOWN outbound financial state is never blindly retried;
- authoritative money/quantity uses exact unit/currency semantics;
- models/learning cannot expand trading authority or hard risk;
- source/simulation/replay/PAPER/LIVE evidence classes remain distinct;
- no sports/bookmaker semantics from Autosport;
- secrets do not enter journal/log/model/DOM;
- Windows/NVDA is final release evidence, not an intermediate source blocker.
