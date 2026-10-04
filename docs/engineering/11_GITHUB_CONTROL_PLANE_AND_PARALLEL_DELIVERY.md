# AutoTrade — optional GitHub control plane

The repository and four human-facing canonical control issues now exist, and a dedicated `control/registry` branch has been bootstrapped. The registry is optional coordination infrastructure. Its availability or qualification never disables concurrent source mutation, branch creation, commits, pull requests, testing, integration work, or other useful isolated work.

## 1. Minimal canonical structure

`control/INDEX.json` is the stable entry point. It contains repository identity, schema version and paths/URLs for product baseline, engineering/contracts, roadmap/work-package bank, claim registry, qualification matrix and findings. The universal prompts depend on this path, not temporary issue numbers.

Keep four human-facing canonical issues when implementation is authorized: Whole product/qualification; Roadmap/dependencies; Dispatch/ownership; Audit/integration. They are views/entry links, not competing copies of state. Ordinary package/defect issues and PRs link to machine-readable records. Scientific and financial governance remain their canonical engineering documents, not hundreds of extra control issues.

Files on main: `control/INDEX.json`, `control/work-packages/*.json`, `control/qualification.json`, `control/findings/*.json`, `control/CONSTITUTION.md`. Operational ownership lives on a dedicated protected `control/registry` branch under `registry.json` with an append-only transition log. Never place trading/runtime secrets, live financial authority or broker credentials in this development registry.

## 2. Atomic registry protocol

Issue comments, labels, claims and registry records are advisory coordination metadata, not locks. A registry service may record package/dependency/scope overlap information transactionally when available, but workers do not need an accepted claim before useful isolated mutation.

If used, the registry may use optimistic compare-and-swap to keep its own metadata consistent. It is never a prerequisite for concurrent source mutation and never grants exclusive authority over code. If registry semantics are unavailable, continue on isolated branches and reconcile by live Git state.

Registry transitions, when used, record coordination history only. They do not gate branch creation, source mutation, commits, PR creation, review, integration, or merge eligibility. Merge eligibility is determined by current code, dependencies, required product/release evidence and GitHub permissions.

Registry outage does not stop useful work. Workers may continue isolated mutation, testing, branches and PRs; stale lease/generation metadata has no authority to invalidate otherwise correct work. Reconcile overlap using current Git state.

## 3. Package record schema

Required record keys: `id`, `requirement_refs`, `semantic_responsibility`, `authority_family`, `mutation_scopes`, `exact_scope`, `required_inputs`, `contracts`, `dependencies`, `reuse_sources`, `tests`, `acceptance`, `integration_target`, `forbidden_scope`, `conflicts`, `state`, `owner_claim`, `branch`, `pr`, `accepted_artifacts`, `blocking_findings`, `supersedes`, `updated_at`.

READY is derived from accepted dependency artifacts and compatible contracts. A label is a cached display, not independent authority. A package in REVIEW should normally be reused rather than duplicated, but REVIEW state is not a hard lock. If another worker can safely advance a non-conflicting or corrective path, it may do so and reconcile through the existing PR or an explicit successor.

## 4. PR and merge protocol

Branches: `wp/<stable-id>/<semantic-slug>`. One canonical PR per semantic package unless an explicit dependency stack is recorded. PR body includes problem, changed behavior, contracts, reuse/provenance, scope/claim generation, exact tests/outcomes, risks, integration target and evidence. Use draft while dependencies/evidence are incomplete; READY_FOR_REVIEW is not DONE.

Merge checks: scope and behavior are correct; correct base/contract versions; applicable exact-head verification; required independent specialist findings resolved where product policy requires them; license/provenance complete; no untracked build dependencies; branch/PR conflicts addressed; release/qualification implications recorded. Claim/lease/generation state is not a merge gate. A new commit invalidates evidence whose scope it changes. Do not require rerunning unrelated expensive tests without a concrete risk or gate.

A merge queue validates each candidate against current main and relevant dependencies. Compatible disjoint changes merge continuously. Incompatible contract migrations use expand/transition/contract or a coordinated narrowly scoped wave. Resolve conflicts by preserving both intended behaviors and running the affected contracts, not by choosing one side mechanically. Main protections apply equally to bots and human contributors.

## 5. Findings, handoff and stale workers

Finding record: ID, dedupe fingerprint, family, severity, requirement/contract, target SHA, expected/observed behavior, evidence, uncertainty, affected scope, proposed acceptance and status. A controller creates/updates a meaningful defect package after dedupe. Severity can block the affected qualification without stopping unrelated work. False/obsolete findings are closed with evidence and retained history.

On stale coordination metadata, inspect last progress, branch and PR. Preserve commits/tests. Stale metadata does not invalidate commits or block merge. Another authorized worker may continue or reconcile the same lineage instead of rebuilding it.

## 6. Control-plane acceptance tests

If the optional registry is maintained, test its own metadata consistency and recovery. These tests are not prerequisites for concurrent source mutation. No worker-count, WIP, claim, lease, coordinator or registry-availability gate may block otherwise useful isolated work.

These controls are optional delivery-support infrastructure. Concurrent source mutation is allowed now; registry/claim infrastructure may improve collision visibility but never determines whether autonomous workers are allowed to work.
