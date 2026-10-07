# AGENTS.md

## Terminal Section Closure Protocol v2 — owner directive 2026-10-07

**This section overrides every older coordination rule in this repository, including any instruction to use the full execution window, keep creating residual work, prepare FRONT-2, avoid idling, satisfy a depth/work-unit floor, or keep mutating while CI is pending. Product correctness/safety requirements remain binding.**

### Objective

Optimize for **honestly closed Sections**, not commits, PR count, execution units, branch activity, or the amount of hardening performed.

A worker must prefer the shortest evidence-correct path from the current repository state to terminal closure of the earliest actionable Section.

### Mandatory lifecycle

For the earliest actionable Section, execute this lifecycle in order:

1. **AUDIT EXISTING** — map the fixed acceptance contract to live code, tests, evidence, and already integrated capabilities. Existing correct implementation is an asset, not a reason to reimplement.
2. **IMPLEMENT ONLY MISSING** — change only acceptance-critical gaps. Reuse/repair/converge existing canonical work before creating anything new.
3. **CANDIDATE** — as soon as all internally controllable acceptance requirements appear satisfied, designate one canonical finisher lineage and one candidate SHA.
4. **FREEZE** — freeze that candidate. After freeze, unrelated hardening, polishing, speculative edge-case hunting, refactors, extra features, and “while we are here” changes are forbidden.
5. **QUALIFY EXACT SHA** — run the required tests/gates against that exact candidate. Pending/queued CI does not authorize changing the SHA.
6. **REPAIR ONLY PROVEN GATING FAILURE** — if qualification fails, make the smallest acceptance-relevant repair on the same canonical finisher, refreeze a new SHA, and rerun affected gates. A discovered non-gating improvement goes to backlog/later QA scope.
7. **INTEGRATE** — merge/converge the qualified candidate into the canonical integration authority required by the plan.
8. **POST-MERGE READBACK** — verify accepted tree/SHA and any required downstream evidence after integration.
9. **DONE** — update `SEQUENTIAL_CLOSURE_STATE.md` and applicable canonical control record in the same closure run, then immediately select the next Section.

### One-front law

- There is exactly **one mutation front**: the earliest actionable not-closed Section.
- Creating or mutating FRONT-2/Section N+1 is **forbidden** while Section N still has internally controllable acceptance work.
- Work on a later Section is allowed only when it is a **named direct dependency required to close the primary Section**, and only the minimum dependency slice may be changed.
- Do not create speculative prequalification, next-section integration, or reconvergence branches merely because the primary candidate is waiting for CI.
- Repeatedly propagating a moving Section-N parent into Section N+1 is forbidden. Section N+1 waits for a frozen/accepted predecessor, except for the explicit external-block rule below.

### External-block escape without false DONE

If all internally controllable acceptance work is complete and the only remaining requirements are genuinely external (for example human NVDA/physical-device evidence, vendor/certificate/account approval, externally unavailable infrastructure, or an external fact that cannot be manufactured):

- freeze the exact internal candidate;
- record **INTERNAL_DONE_BLOCKED_EXTERNAL** with exact SHA, completed evidence, missing external evidence, and the precise unblock condition;
- treat that Section as **immutable for autonomous sequencing** while the external condition is unchanged;
- move to the next earliest Section whose implementation does not depend on the missing external fact;
- do **not** call the blocked Section DONE;
- do **not** reopen or mutate it just to consume worker time;
- when the external fact arrives, requalify only the dependency surface it can invalidate, then complete terminal closure.

Queued CI is not automatically an external blocker. If the frozen exact-SHA CI is merely pending, keep the candidate frozen and check its result; do not move the SHA or invent new work. If CI is demonstrably unavailable for an extended period and no autonomous action can restore it, record the exact infrastructure blocker before using this escape.

### One canonical finisher

- Each active Section has one canonical finisher branch/PR/lineage, recorded in durable state.
- Before creating a branch or PR, inspect active lineages. Reuse the canonical finisher whenever possible.
- Parallel workers may contribute non-overlapping acceptance-critical fixes, tests, or evidence, but those contributions must converge into the same finisher.
- Alternate integration trees, competing “whole Section” PRs, and reconvergence carousels are forbidden.
- A merged PR into an intermediate feature/integration/prequal branch is **not Section closure**.
- If duplicate lineages already exist, preserve unique required changes, converge once, supersede the rest, and stop propagating duplicates.

### Acceptance-contract boundary

- The canonical Section plan defines the acceptance boundary. Workers may not silently enlarge it.
- After CANDIDATE/FREEZE, a newly imagined edge case does not block closure unless it demonstrates violation of an existing acceptance requirement, regression, security/correctness invariant, or required negative/recovery case.
- Non-gating improvements must be recorded for later scope instead of extending the current Section indefinitely.
- A work-unit/depth floor, token budget, run duration, “do not stop after one PR”, or “use the full execution window” rule can **never** force extra mutation after the closure candidate is ready.

### Already-implemented Section rule

If the Section’s required capability already exists in current canonical code:

- do not rebuild it;
- perform a closure audit against the acceptance contract;
- reuse current implementation/evidence;
- add only missing tests/evidence/integration;
- create/freeze the closure candidate;
- qualify and close it.

The correct action for an already-implemented Section is **qualification and closure**, not invention of more implementation.

### CI and SHA discipline

- One qualification cycle = one frozen SHA.
- Never invalidate green/pending exact-head evidence with unrelated commits.
- Never claim PASS from queued, cancelled, skipped, stale-base, different-SHA, or intermediate-branch CI.
- On failure, repair the proven failure only, then refreeze.
- On success, integrate promptly; do not continue polishing the candidate.

### Reopen discipline

A DONE Section may be REOPENED only for a concrete demonstrated reason: regression, invalid closure evidence, materially changed acceptance contract, or later integration that breaks the closed scope.

Before mutation, record the exact reopen reason and affected evidence. Reopen only the invalidated surface; do not restart the entire Section by default.

### Required worker decision at every run

Before writing code, answer from live state:

1. What is the earliest actionable Section?
2. Does its required implementation already exist?
3. What exact acceptance-critical gap remains?
4. What is the one canonical finisher?
5. Is there already a frozen candidate SHA?
6. If frozen, am I permitted to mutate it? Only a proven gating failure permits that.
7. Can this run close the Section now? If yes, closure takes priority over every depth/work-unit target.

**Closure beats activity. Frozen candidates beat moving targets. Existing implementation beats reimplementation. Exact evidence beats PR count.**


Objective: TIME_TO_WHOLE_FINISHED_AUTOTRADE.

Read `control/INDEX.json` before work.

Current mode: `TERMINAL_SECTION_CLOSURE_V2`.

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
- If another worker closes the current Section/Subsection while you are working, refresh the live registry immediately. Do not keep mutating already-DONE scope merely because your branch or PR is still open. Preserve and converge only genuinely unique required changes; otherwise close/supersede/retarget the duplicate lineage and move to the earliest unfinished Section.
- External blockers do not justify false DONE. Finish all internally controllable work, record the blocker precisely, and proceed only to dependency-safe work.

Chat history is not closure authority. Durable GitHub state is.
