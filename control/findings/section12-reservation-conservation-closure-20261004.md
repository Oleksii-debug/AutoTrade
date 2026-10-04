# Section 12 reservations and admission-to-fill conservation closure candidate

Date: 2026-10-04
Current-main source parent: `5afc5602d3a54cb5e35bd5231bbb0619dcb2596e`

Current main already contains the canonical reservation authority, exact restart binding and atomic provider-free fill/financial consumption. This candidate promotes the current-main reservation-bust successor into the Section 12 closure lane.

## Integrated/residual authority

The selected lineage preserves:
- exact bounded reservation quantities and resource identities;
- canonical JournalStore generation/scope authority across restart;
- atomic fill + reservation consumption;
- exact consumed/original capacity bounds;
- stale reservation snapshot fencing;
- idempotent replay;
- atomic economic reversal + reservation restoration for provider-free fill bust;
- fail-closed omission guard for reservation-bound busts;
- terminal FILLED reservations are not blindly reactivated;
- current-main recovery sequence fencing;
- polymorphic text/snapshot/fill/plan/prepared-binding ingress fails before financial mutation.

## Closure requirements

Section 12 is DONE only if exact-head qualification confirms:
1. admission reserves the exact required resources before execution;
2. partial fills consume only their causal resource share;
3. cancel/replace/UNKNOWN cannot expose free duplicate capacity;
4. terminal release occurs exactly once;
5. correction/bust restoration is bounded, idempotent and restart-safe;
6. terminal reservations are not reopened without separate canonical authority;
7. baseline and Verify are terminal green;
8. review state is clean;
9. merge and post-merge readback confirm accepted source identity.

No provider/PAPER/LIVE, profitability, release or NVDA qualification is implied.
