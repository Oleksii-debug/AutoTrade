# Section 15 — portfolio allocation — CI blocker record (superseded as terminal status)

> **STATUS CORRECTION — 2026-10-07**  
> This record is no longer a terminal Section-15 blocker statement. A later exact-source audit found additional internally actionable Section-15 residuals: allocation-authority snapshot mapping ingress (PR #2317) and the stacked correlation / open-OMS concentration lineages (#1651 / #1666). Hosted exact-head CI is still queued and remains a real external qualification blocker, but Section 15 must stay **IN_PROGRESS**, not BLOCKED_EXTERNAL, until the internal stack is converged, qualified and integrated.

Date: 2026-10-07

## Ordered context

- Section 2 remains `BLOCKED_EXTERNAL` under its canonical dependency/provenance finding.
- Current-plan Section 15 means **Portfolio construction/allocation**. Historical Section-15 learning-wave evidence used an older numbering and is not closure evidence for this Section.
- Canonical implementation lineage: PR #2191, branch `wp32/sealed-payload-current-main-20261006-sol56-h7q2`.
- Exact head audited in this cut: `f327a3579c133fe04e8b62ad6948a45940b57c82`.
- Accepted donor: `9807afd3852cc0f531e1fee5417ceb14a039283b`.

## Internal work believed exhausted on the earlier cut

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

This remains an external execution blocker for terminal qualification of the audited #2191 cut, but it is no longer the only Section-15 blocker. Later internally actionable authority/correlation residuals must also converge before closure. Neither source work nor CI gates may be bypassed or weakened.

No provider/PAPER/LIVE, profitability, economic-edge or release authority follows.
