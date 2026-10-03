# AGENTS.md

Objective: TIME_TO_WHOLE_FINISHED_AUTOTRADE.

Read `control/INDEX.json` before work and refresh live GitHub truth before mutating shared state.

Current mode: `SINGLE_PRIMARY_DEVELOPER`. One autonomous primary developer may inspect, modify, test, and commit bounded product changes directly on the currently designated canonical integration lineage. Do not create a second architecture or duplicate an already-active canonical authority. Re-read the live branch head before each write and treat exact-head verification as the integration authority.

Provider/live boundary for the current development phase:
- real provider integration and provider qualification are deferred unless the owner explicitly re-authorizes them;
- do not access, create, rotate, or expose real credentials/secrets;
- do not submit real or paper-provider financial orders and do not perform live trading;
- provider-free simulation and internal deterministic trading workflows may be implemented and qualified.

Hard rules:
- provider reconciliation + durable journal establish financial truth when provider-backed operation is later enabled;
- acknowledgement is not a fill;
- UNKNOWN outbound financial state is never blindly retried;
- authoritative money/quantity uses exact unit/currency semantics;
- models/learning cannot expand trading authority or hard risk;
- source/test/simulation/paper/real evidence classes stay distinct;
- no sports/bookmaker semantics are imported from Autosport;
- reusable first-party code must be neutralized, provenance-cleared and characterization-tested;
- Windows/NVDA keyboard usability is a release requirement.
