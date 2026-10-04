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

## Follow-up source review: terminal fill-bust blocker

Section 12 is not yet honestly closed for the full fill lifecycle.

The current restoration authority deliberately rejects restore_consumption on terminal reservations. The regression test for a provider bust arriving after a reservation was durably marked FILLED confirms that the atomic bust fails closed without partial OMS/economic mutation. That behavior is safer than silently reopening capital, but it does not prove reservation conservation after a late provider correction:

- FILLED is a TERMINAL_STATE;
- mark_terminal zeros remaining while retaining consumed history;
- total_reserved excludes terminal reservations;
- a later provider bust cannot currently move the consumed amount back into held capacity.

Therefore the candidate proves active-reservation bust restoration, restart/idempotency and fail-closed terminal behavior, but not terminal FILLED -> corrected/busted capital re-reservation.

Do not implement this by simply changing FILLED back to ACTIVE/UNKNOWN. A correct continuation must first bind one authoritative post-bust OMS order state, settlement/clearing compensation state where applicable, and the exact capital reservation that must again be held. That transition must be one atomic/restart-safe authority decision so an unrelated or already-settled fill cannot mint/free capacity.

Until that contract is implemented and qualified, keep Section 12 DRAFT and do not claim complete reservation conservation across terminal provider corrections.
