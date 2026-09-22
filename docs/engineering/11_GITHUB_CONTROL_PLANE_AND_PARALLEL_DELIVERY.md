# AutoTrade — optional GitHub control plane

Future design only. No repository, issues, PRs, automation or GitHub application was created in this architecture task.

## 1. Minimal canonical structure

`control/INDEX.json` is the stable entry point. It contains repository identity, schema version and paths/URLs for product baseline, engineering/contracts, roadmap/work-package bank, claim registry, qualification matrix and findings. The universal prompts depend on this path, not temporary issue numbers.

Keep four human-facing canonical issues when implementation is authorized: Whole product/qualification; Roadmap/dependencies; Dispatch/ownership; Audit/integration. They are views/entry links, not competing copies of state. Ordinary package/defect issues and PRs link to machine-readable records. Scientific and financial governance remain their canonical engineering documents, not hundreds of extra control issues.

Files on main: `control/INDEX.json`, `control/work-packages/*.json`, `control/qualification.json`, `control/findings/*.json`, `control/CONSTITUTION.md`. Operational ownership lives on a dedicated protected `control/registry` branch under `registry.json` with an append-only transition log. Never place trading/runtime secrets, live financial authority or broker credentials in this development registry.

## 2. Atomic registry protocol

Issue comments and labels are not atomic locks. Claim requests use a unique request ID and a narrow registry service/GitHub App. The service reads registry head H, validates package/dependencies/scope overlaps and owner permissions, creates a commit whose parent is H with the complete intended state transition, then updates the registry ref without force. A competing sibling commit is rejected as non-fast-forward; the service re-reads and re-evaluates the request. Reusing the same request ID returns the recorded result.

This is an optimistic compare-and-swap pattern over one short control transaction; it is not a long global worker lock. For multi-scope claims, all scopes are granted in one registry commit or none are. Worker tokens cannot force-update or delete the registry. Branch rules and service permissions must enforce that design. If the selected GitHub integration cannot enforce the expected-parent/non-force semantics, implement an equivalent transactional registry service before concurrent source mutation. Do not pretend a best-effort comment protocol has atomicity.

Transitions: CLAIM expects READY and unowned compatible scopes; RENEW expects owner/generation; SUBMIT_REVIEW binds PR/head/evidence; TRANSFER expects current ownership and nominated successor; EXPIRE/RECLAIM records reason and increments generation; RELEASE expects current generation and handoff state; COMPLETE requires merged compatible head and accepted evidence. Every response returns registry revision, claim generation and exact scopes.

The registry uses trusted service time for leases. A worker's local clock cannot expire another claim. Registry outage permits read-only work and local analysis; no new shared mutation ownership is assumed. Existing work can be preserved on its branch, but merge still requires a current verified generation.

## 3. Package record schema

Required record keys: `id`, `requirement_refs`, `semantic_responsibility`, `authority_family`, `mutation_scopes`, `exact_scope`, `required_inputs`, `contracts`, `dependencies`, `reuse_sources`, `tests`, `acceptance`, `integration_target`, `forbidden_scope`, `conflicts`, `state`, `owner_claim`, `branch`, `pr`, `accepted_artifacts`, `blocking_findings`, `supersedes`, `updated_at`.

READY is derived from accepted dependency artifacts and compatible contracts. A label is a cached display, not independent authority. A package in REVIEW remains protected from duplicate implementation; auditors can read it freely. If rework is required, ownership returns to the same PR or an explicit successor. Changes to work-package meaning require review and preserve history.

## 4. PR and merge protocol

Branches: `wp/<stable-id>/<semantic-slug>`. One canonical PR per semantic package unless an explicit dependency stack is recorded. PR body includes problem, changed behavior, contracts, reuse/provenance, scope/claim generation, exact tests/outcomes, risks, integration target and evidence. Use draft while dependencies/evidence are incomplete; READY_FOR_REVIEW is not DONE.

Merge checks: current ownership generation; scope matches; correct base/contract versions; exact-head CI; required independent specialist findings resolved; license/provenance complete; no untracked build dependencies; branch/PR conflicts addressed; release/qualification implications recorded. A new commit invalidates evidence whose scope it changes. Do not require rerunning unrelated expensive tests without a concrete risk or gate.

A merge queue validates each candidate against current main and relevant dependencies. Compatible disjoint changes merge continuously. Incompatible contract migrations use expand/transition/contract or a coordinated narrowly scoped wave. Resolve conflicts by preserving both intended behaviors and running the affected contracts, not by choosing one side mechanically. Main protections apply equally to bots and human contributors.

## 5. Findings, handoff and stale workers

Finding record: ID, dedupe fingerprint, family, severity, requirement/contract, target SHA, expected/observed behavior, evidence, uncertainty, affected scope, proposed acceptance and status. A controller creates/updates a meaningful defect package after dedupe. Severity can block the affected qualification without stopping unrelated work. False/obsolete findings are closed with evidence and retained history.

On stale ownership, inspect last progress, branch, PR and related claims. Preserve commits/tests. Reclaim increments generation; stale-worker commits cannot pass the merge ownership gate. The replacement worker checks whether the previous solution can be finished instead of creating a duplicate. A claim release never deletes the previous branch or evidence automatically.

## 6. Control-plane acceptance tests

Before launching concurrent mutation, prove: two simultaneous claims for overlapping scopes yield one winner; disjoint scopes both succeed; multi-scope claim is all-or-none; duplicated request is idempotent; stale generation cannot renew/release/merge; clock skew cannot steal ownership; registry outage grants no imaginary claim; expired work can be recovered without lost commits; labels/comments cannot override registry; an old static prompt resolves current INDEX and tasks; no global worker-count gate exists.

These controls are a delivery support component to implement later. They are not a reason to postpone initial authorized single-owner implementation or read-only research; concurrent source mutation begins when atomic ownership is actually available.
