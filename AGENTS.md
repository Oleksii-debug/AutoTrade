# AGENTS.md

Objective: TIME_TO_WHOLE_FINISHED_AUTOTRADE.

Read `control/INDEX.json` before work.

Current mode: `PARALLEL_ISOLATED_WORKTREES`. Five autonomous developers may change the full product code surface concurrently only in their own isolated worktrees. No worker commits, merges, touches secrets, or performs live trading; integration remains a separate verified action.

Hard rules:
- provider reconciliation + durable journal establish financial truth;
- acknowledgement is not a fill;
- UNKNOWN outbound financial state is never blindly retried;
- authoritative money/quantity uses exact unit/currency semantics;
- models/learning cannot expand trading authority or hard risk;
- source/test/simulation/paper/real evidence classes stay distinct;
- no sports/bookmaker semantics are imported from Autosport;
- reusable first-party code must be neutralized, provenance-cleared and characterization-tested;
- Windows/NVDA keyboard usability is a release requirement.
