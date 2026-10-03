# AutoTrade — optional parallel development operating model

Prepared after the core engineering package was published. The AutoTrade repository now exists and its bootstrap control plane has started; this document remains the operating design for future high-concurrency development. The external launcher is assumed available and is not redesigned here.

## 1. Goal and concurrency rule

Optimize TIME_TO_WHOLE_FINISHED_AUTOTRADE. Useful progress is an accepted capability or a resolved uncertainty on the dependency path. Worker count, comments, claims, PR count and document volume are not progress metrics.

There is no global worker, source-worker, auditor or open-PR cap; no RED/YELLOW/GREEN admission scheme or numeric capacity gate. Exclusive mutation applies only to overlapping semantic responsibility. Independent providers, asset models, UI workflows, science, tests and read-only research proceed concurrently. Infrastructure quotas are respected through queue/backoff, not recast as a product-wide worker limit.

`(AUTHORITY_FAMILY, SEMANTIC_KEY, MUTATION_SCOPE)` is advisory collision metadata only. It helps workers notice overlapping semantic responsibility but never locks a scope or prevents useful safe work. If overlap is detected, workers should prefer non-conflicting work or reconcile/rebase; they do not terminate merely because another worker is active.

## 2. Roles and useful work

Implementation workers own exact packages. Integration workers own identified cross-package joins. Contract guardians review version/meaning changes without serially owning every implementation. Financial, scientific, provider, security, accessibility, recovery, reuse/license and integration auditors inspect independent evidence. Research workers reduce named uncertainty and stop when their question is answered; they do not repeatedly summarize the same source.

Read-only work needs no claim. An auditor who fixes code may create an isolated branch/commit/PR without obtaining an exclusive mutation claim; use advisory ownership metadata only when it helps collision awareness. Independent review remains required only where product/risk/release policy calls for it. Auditor findings are tracked as defects with evidence, affected contract/build, severity, expected behavior, reproducible non-live scenario and a dedupe fingerprint.

## 3. Self-dispatch loop

1. Load `control/INDEX.json` from the live default branch, then the referenced contract versions, dependency graph, open findings, PRs and claim registry.
2. Resume useful owned work first; check whether another PR already solves the package.
3. Select a READY package maximizing critical-path impact, risk reduction and integration value while respecting dependencies and available evidence/tools.
4. Record advisory scope/ownership metadata when useful for coordination, but never wait for a claim token, lease, coordinator, or registry service before beginning useful isolated work.
5. Work in an isolated branch/worktree based on the recorded source/contract revision. Keep changes within scope.
6. Run meaningful tests, collect exact-head evidence, publish/update the canonical PR and its package result.
7. Update coordination metadata while useful, but registry/lease state is not an execution or integration gate. Preserve progress in the branch/PR and continue useful work.
8. Choose the next compatible READY responsibility. If no useful mutation is ready, perform a distinct bounded audit/research task or end the pulse without noise.

A static prompt is only the entry instruction. Live repository state supplies tasks, claims, gates and priorities. Stale snapshots, old chats and Drive copies cannot override newer reviewed repository contracts once implementation has started.

## 4. Claim and package states

Package states: PROPOSED → BLOCKED/READY → CLAIMED → IN_PROGRESS → REVIEW → INTEGRATING → DONE. REWORK returns to the same responsibility with an explicit finding. CANCELLED/SUPERSEDED preserve lineage. Dependencies unlock from accepted artifacts and evidence, not elapsed time or a merged unrelated PR.

Coordination records may contain package ID, authority family, semantic key, mutation scope, worker identity, base commit, contract versions, timestamps, branch/PR, progress artifact and status. These records are advisory and never authorize or forbid work. Expiry/lease concepts may be used for stale-metadata cleanup only, not as mutation, commit, PR, or merge gates.

When coordination metadata becomes stale, inspect branch/PR/artifacts and preserve useful work. Any authorized worker may resume, supersede, reconcile, or integrate that work using current repository truth. Stale claim/lease/generation metadata never invalidates otherwise correct commits and is never a merge gate.

## 5. Independent audit and integration

Financial audit checks money, units, fills, fees, FX, funding, margin and corrections against independent oracles. Science audit checks availability, split construction, trial accounting, holdout consumption, uncertainty and retention. Provider audit compares real official contracts/fixtures and actual account capabilities. Security audit checks privileges, secrets, model/data boundaries and distribution. Accessibility audit exercises real keyboard/NVDA workflows. Recovery audit targets uncertainty windows and restoration. Reuse audit checks provenance and license obligations. Integration audit checks shared meaning and actual cross-component behavior.

A finding becomes a package when it names a falsifiable defect and concrete acceptance. Fingerprint by affected contract/component, root cause and evidence so ten auditors do not open ten equivalent tasks. Critical authority/conservation/leakage defects block affected merges/qualification; unrelated work continues. A report without actionable evidence is a research note, not an automatic blocker or a completion claim.

Use continuous integration for backward-compatible work. A coordinated wave is warranted for an incompatible schema/engine migration, release freeze or qualification baseline. It names participating packages, migration order and rollback; only dependent scopes wait. Integration is not reserved to an exclusive owner. Any authorized worker may perform the join when dependencies and product/release evidence make it safe.

## 6. Launch recommendations without caps

During foundation work, source concurrency follows the number of genuinely independent contract/engine/persistence/provenance/UI-fixture scopes. Additional workers can audit sources, design independent oracles or resolve named questions. Once interfaces stabilize, six providers, asset families, science, memory/routing, UI, packaging and recovery expose many more independent scopes.

Ten, fifty, one hundred or more workers can be useful when that many distinct ready responsibilities or bounded research questions exist. Launch volume follows live ready-work breadth, review backlog, test resources and integration latency. More workers stop helping when they duplicate semantic work, create speculative incompatible contracts, or produce changes faster than meaningful verification can accept them. The response is to invest work in tests/integration/defects and unblock dependencies, not impose an arbitrary numeric ceiling.

Auditor allocation follows risk and evidence backlog rather than a fixed auditor:implementer ratio. Authority, accounting, science and release changes need independent specialist review; low-risk isolated UI text does not need every auditor family. Never count repeated review of the same unchanged head as new evidence.

Track: accepted capability completion, critical-path blocked time, ready-scope breadth, oldest useful PR, integration latency, reopened defects, escaped financial/scientific faults, audit signal, and qualification coverage. Do not rank workers by lines changed or number of comments.

## 7. Renewable package supply

Seed the bank with WP-01–65. Generate additional packages only from uncovered requirements, concrete audit/bug findings, dependency splits, integration failures or evidence gaps. Each has the same required fields as the original bank and a parent requirement/finding. Automated READY computation validates dependencies and conflicts; it never invents acceptance evidence. Complete discovery work can close a question without generating code. Once no meaningful unfinished scope remains, the swarm performs final qualification rather than manufacturing tasks.
