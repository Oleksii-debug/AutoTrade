# AGENTS.md

Objective: TIME_TO_WHOLE_FINISHED_AUTOTRADE.

Read `control/INDEX.json` before work.

Current mode: `SEQUENTIAL_CLOSURE_WITH_PARALLEL_RESIDUALS`.

Canonical worker-coordination override:
- No repository-defined worker, coordinator, WIP, work-package, branch, or PR cap.
- Any older fixed worker count, WIP limit, serialized lane, ownership/claim lock, exclusive integration owner, mandatory PR-order waiting rule, or CI-wait stop rule is non-binding if it conflicts with this section.
- Claims, leases, ownership, assignments, queues, and coordinator labels are advisory coordination metadata only; they do not override the ordered Sequential closure authority below.
- Workers may create branches, commits, pull requests, tests, fixes, integration commits, and merges when GitHub permissions allow and the change is honestly verified.
- Parallel implementation, testing, hardening, research, documentation, accessibility, packaging, fixtures and adapters are allowed only when consistent with the ordered Sequential closure authority below: prioritize the earliest unfinished Section and use later work only when dependency-safe or directly supportive.
- Queued, pending, slow, or unavailable CI is never by itself a reason to terminate. Record the pending state and continue only work permitted by the current ordered closure front.
- A blocked primary closure front may be left only after all safe internally controllable residual work is exhausted and the blocker is durably recorded; later work must remain dependency-safe.
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

## Sequential closure authority — owner directive 2026-10-07

This section is the controlling coordination rule if any older repository text, worker prompt, issue, roadmap note, swarm rule, claim/ownership rule, or “parallel lane” instruction conflicts with it.

- Read the current canonical ordered Section plan and the live repository state before choosing work.
- The numerically earliest Section that is not honestly closed is the primary closure front. The next unfinished Section may be prepared only when this is dependency-safe, directly removes a dependency, or the primary front is genuinely non-actionable after all safe internal residual work is exhausted.
- Parallel workers are allowed, but parallelism does not authorize skipping the ordered closure front. Workers should take non-overlapping residuals of the same current front or dependency-safe preparation for the next front.
- A Section or Subsection may be marked DONE only after its required implementation/integration and applicable tests, negative/failure/recovery evidence, accessibility/security/performance/packaging evidence, and exact durable source state are satisfied.
- Every DONE Section/Subsection must be recorded durably in GitHub before the worker moves on. Use `SEQUENTIAL_CLOSURE_STATE.md` plus the repository’s existing canonical issue/status/control records when applicable.
- Once a Section/Subsection is durably recorded DONE, workers MUST NOT routinely re-enter it for reimplementation, polishing, re-auditing, or repeat verification. Skip it and work on the earliest unfinished Section.
- A closed Section/Subsection may be reopened only for a demonstrated regression, invalidated closure evidence, changed acceptance contract, or a later integration change that demonstrably broke it. Record `REOPENED` and the exact reason before new work begins.
- If two workers close the same scope concurrently, keep one canonical closure lineage/evidence set. Converge any unique necessary changes, then close/supersede the duplicate PR/branch/issue; delete the duplicate branch when safe. Never count one Section twice.
- Before creating a new PR/branch for the current Section, inspect existing active lineages and reuse/repair/converge them when possible.
- External blockers do not justify false DONE. Finish all internally controllable work, record the blocker precisely, and proceed only to dependency-safe work.

Chat history is not closure authority. Durable GitHub state is.
