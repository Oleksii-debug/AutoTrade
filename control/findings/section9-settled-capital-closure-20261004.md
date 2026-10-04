# Section 9 settlement and actually available capital closure candidate

Date: 2026-10-04
Base main: `cc0b7fa5cee4597dc8039f0b0a5fd622e8b12a29`

Current main already contains the accepted provider-free settlement/capital composition used by the autonomous ZERO loop.

## Integrated provider-free authority

Accepted main includes:
- durable settlement obligations and application;
- settled/spendable cash projection;
- active trading cash fenced from being treated as settled opening capital without the required settlement state;
- reservation-aware capital admission;
- partial-fill settlement/capital continuation;
- exact cash buckets exposed by the accepted ZERO flow;
- crash-safe retained-fill recovery bound to the settlement/capital cut;
- provider-free autonomous execution using the resulting available-capital authority.

The accepted ZERO convergence explicitly consumed the current settlement-capital lineage and reran the integrated scenario after merge.

## Scope boundary

Provider reconciliation versus local spendable capital for real PAPER/LIVE provider accounts remains a later real-provider qualification concern. It is not required to prove the provider-free M1 capital model and must not block this Section 9 closure.

## Closure requirements

Section 9 is DONE only if exact-head qualification confirms:
1. pending/unsettled cash is distinct from settled cash;
2. settled cash is reduced by active reservations before new admission;
3. unsettled proceeds cannot be reused as spendable capital;
4. partial fills consume only their causal share of reserved/settlement resources;
5. settlement/funding/financing changes are restart-stable;
6. crash/recovery cannot release or reuse capital twice;
7. baseline and Verify are terminal green;
8. review state is clean;
9. merge and post-merge readback confirm accepted source identity.

No provider/PAPER/LIVE, profitability, release or NVDA qualification is implied.
