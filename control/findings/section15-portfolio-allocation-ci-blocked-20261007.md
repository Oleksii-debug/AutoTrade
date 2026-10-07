# Section 15 — portfolio allocation — external CI blocker

Date: 2026-10-07

## Ordered context

- Section 2 remains `BLOCKED_EXTERNAL` under its canonical dependency/provenance finding.
- Current-plan Section 15 means **Portfolio construction/allocation**. Historical Section-15 learning-wave evidence used an older numbering and is not closure evidence for this Section.
- Canonical implementation lineage: PR #2191, branch `wp32/sealed-payload-current-main-20261006-sol56-h7q2`.
- Exact head audited in this cut: `f327a3579c133fe04e8b62ad6948a45940b57c82`.
- Accepted donor: `9807afd3852cc0f531e1fee5417ceb14a039283b`.

## Internal work exhausted on this cut

The current five-path WP-32 restoration was read back against the live canonical plan and current repository contracts. The evidence-bound allocation path covers the Section-15 acceptance semantics that are internally implementable on this lineage:

- horizon-bound after-cost/holding-cost accounting;
- exact and conservative FX normalization, including inverse/rational identity;
- executable-capacity / lot constraints;
- cash, gross/net/symbol, turnover, cost and stress limits;
- fresh adverse stress evidence;
- authoritative account/reservation/capability/instrument currentness;
- sealed immutable evidence provenance and use-time revalidation.

The branch is a restoration/convergence of the accepted canonical allocator; no second allocator, risk engine, provider authority or profitability claim is introduced.

## External terminal blocker

Fresh exact-head hosted qualification exists for `f327a3579c133fe04e8b62ad6948a45940b57c82`, but the required jobs are still **queued without runner execution**:

- baseline run `37554459638`;
- Verify AutoTrade run `37554459637`;
- provider-free-product run `37554459651`.

Queued/no-run is **not PASS**. Section 15 therefore cannot honestly be marked DONE or merged as closure evidence until the applicable exact-head jobs execute terminal green, final topology/source readback is repeated, integration occurs, and post-merge main is verified.

This is an external execution blocker after the currently identifiable source/integration residuals were exhausted. It must not be bypassed by weakening gates or by treating earlier/superseded CI as exact-head evidence.

No provider/PAPER/LIVE, profitability, economic-edge or release authority follows.
