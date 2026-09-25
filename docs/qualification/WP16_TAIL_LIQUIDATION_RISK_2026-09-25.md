# WP-16 — tail-risk and liquidation-headroom admission — 2026-09-25

Status: **IMPLEMENTATION_INCREMENT_AWAITING_EXACT_HEAD_CI**

This record covers one continuation of the existing independent risk authority. It does not create another risk engine, authorize provider networking, or establish economic edge.

## Exact lineage

- Base main: `92ca4e0cfd8949645663eeaf092ac2db156ec75a`.
- Implementation/test head before this evidence record: `4360536e790ca53fae530483043d86c8032a0da9`.
- Canonical pull request: #547.
- Changed authority: existing `mvp/autotrade_mvp/risk.py` only, plus focused risk tests.
- Exact final PR-head evidence must come from repository CI after this document commit; this document does not self-certify PASS.

## Implemented boundaries

1. Expected shortfall is optional policy, never an invented default. The loss limit and tail fraction must be configured together.
2. Tail scenarios are exact-Decimal inputs. When expected shortfall is enabled, every non-zero projected position must be represented in every tail scenario; missing evidence fails closed.
3. The admitted observation is the equal-weight mean of the worst `ceil(N * tail_fraction)` non-negative portfolio losses under the supplied frozen scenario distribution.
4. The calculation uses the whole projected marked portfolio, including durable reserved position deltas already present in the canonical risk context.
5. A reduce-only exception cannot use the ordinary over-limit escape if configured stress/tail risk worsens. When the intent removes a symbol while other projected risk remains, the comparison requires scenario coverage for the removed base symbol too; missing base-side hedge evidence cannot be silently treated as zero return.
6. Liquidation headroom is optional policy but, when configured, UNKNOWN evidence is not permission. A known breached floor is bypassable only by the existing strict protective-reduction predicate.
7. Binary floating-point inputs remain rejected for financial/tail values.

## Focused regression surface

`mvp/tests/test_risk.py` adds cases for:
- exact expected-shortfall boundary and a one-cent breach;
- absent and incomplete projected tail scenario coverage, plus missing base-side hedge coverage for reduce-only comparison;
- paired policy configuration and invalid tail fraction;
- absent and exact liquidation-headroom evidence;
- rejection of binary float inputs.

At the time this record is written, GitHub exact-head Ubuntu/Windows `baseline` and `Verify AutoTrade` checks are required and must be inspected before merge.

## Remaining WP-16/product work

This increment is not WP-16 DONE. Remaining blockers include binding final admission inside the canonical writer transaction, sourcing all risk evidence from qualified market/instrument/provider state, richer correlation-regime qualification, derivative/provider margin and liquidation semantics, real account/provider qualification, and product-level release evidence. Software risk correctness remains separate from evidence of economic profitability.
