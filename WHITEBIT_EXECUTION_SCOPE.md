# AutoTrade — WhiteBIT-first private-local execution scope

**Updated:** 2026-10-18. This file is the active scope for the private AutoTrade
installation. It records authority and priorities; it is not evidence that an API
request, simulated fill, model result, or financial profit occurred.

## Operating mode

AutoTrade is private local software for its owner. Public distribution, marketing,
release signing, and social/demo presentation are not product requirements. GitHub
remains the source/checkpoint system and may later become private. Local tests are
the fallback when hosted Actions quota is unavailable.

The functional priority order is:

1. real WhiteBIT public market data and durable collection;
2. causal agent decisions, simulation, exact accounting, risk, memory, learning,
   and independent evaluation;
3. recovery, diagnostics, update safety, and a verified Windows package;
4. standard semantic/keyboard accessibility;
5. optional interface polish after the functional loop is complete.

## WhiteBIT authority

WhiteBIT is the current primary exchange. The protected private credential has the
logical name `whitebit-primary`; its value must never be committed, logged, placed
in diagnostics, or copied into an ordinary settings file.

Allowed without private credentials:

- official public REST/WebSocket market data;
- bounded historical data acquisition with provenance and checksums;
- ZERO/local simulation, replay, agent learning, and evaluation.

Allowed when the protected credential is actually available to the process:

- authenticated account reads only, initially, with secret-redacted evidence.

Real BUY/SELL, transfer, withdrawal, leverage, or another irreversible exchange
effect is not part of the current running campaign. If an official WhiteBIT demo or
sandbox becomes documented and technically available, it may be connected as an
explicit DEMO environment. Production endpoints must never be disguised as demo.

## Reuse and scope control

Reuse existing canonical provider, OMS, authority, ledger, risk, ArtifactStore,
agent, model-gateway, Web/Desktop, diagnostics, backup, and update components.
There must be one financial state authority and no duplicate sender.

Existing multi-exchange code may remain when it supplies useful contracts, tests,
or reusable adapters, but new non-WhiteBIT provider development is not a current
priority. Integrate third-party components only when they close a concrete gap and
do not add more runtime cost, latency, maintenance, or duplicate authority than
they remove. Preserve upstream attribution/license files in the private checkout.

## Learning and claims

Local mathematical/ML learning is authorized now and may run independently of the
UI. It must use CLOSED simulated outcomes, causal train/validation/holdout splits,
durable checkpoints, reproducible identities, costs, drawdown, and an independent
comparison. LLM calls remain bounded and opt-in; cheaper deterministic/local paths
are preferred.

Simulation is never labelled as provider PAPER/LIVE truth. A profitable backtest
does not prove future profit. Missing evidence is `INCOMPLETE` or `NOT_CLAIMED`,
never silently upgraded to PASS.

## Accessibility

Use the same standard accessible Windows/Web conventions as the owner's other
working applications: semantic controls, stable names/roles/states, keyboard-only
navigation, managed focus, live status/error messages, selectable text, and usable
export/emergency controls. A manual run with one specific screen-reader version is
optional compatibility QA and does not block functional core completion.
