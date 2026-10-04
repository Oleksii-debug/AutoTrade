# AGENTS.md

Objective: TIME_TO_WHOLE_FINISHED_AUTOTRADE.

Read `control/INDEX.json` before work.

Current mode: `UNBOUNDED_AUTONOMOUS_PARALLEL_DELIVERY`.

Canonical worker-coordination override:
- No repository-defined worker, coordinator, WIP, work-package, branch, or PR cap.
- Any older fixed worker count, WIP limit, serialized lane, ownership/claim lock, exclusive integration owner, mandatory PR-order waiting rule, or CI-wait stop rule is non-binding if it conflicts with this section.
- Claims, leases, ownership, assignments, queues, and coordinator labels are advisory coordination metadata only; they never block useful safe work.
- Workers may create branches, commits, pull requests, tests, fixes, integration commits, and merges when GitHub permissions allow and the change is honestly verified.
- Dependencies constrain final integration order only. They must not stop independent implementation, testing, hardening, research, documentation, accessibility, packaging, fixtures, adapters, or other non-conflicting work.
- Queued, pending, slow, or unavailable CI is never by itself a reason to terminate. Record the pending state and immediately continue another valuable independent task.
- A blocked first workline is never by itself a reason to terminate. STATUS: BLOCKED is allowed only after all reasonably available safe independent work is exhausted.
- Do not idle because another PR, branch, worker, check, review, claim, or queue is active. Switch to non-conflicting work or reconcile/rebase instead of abandoning the run.
- No repository-defined exclusive integration owner is required.
- Use the full execution window while useful safe work remains.

This removes orchestration throttles only. The product/domain hard rules below remain mandatory.

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
