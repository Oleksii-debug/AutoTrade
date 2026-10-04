# Section 12 reservations and admission-to-fill conservation closure candidate

Date: 2026-10-04
Current-main source base: `cc0b7fa5cee4597dc8039f0b0a5fd622e8b12a29`
Current candidate head before this finding refresh: `0621956b5f6178fe4e16ff5c9d481308fe0c34e1`

Current main already contains the canonical reservation authority, exact restart binding and atomic provider-free fill/financial consumption. This candidate carries the current-main reservation-bust lineage into the Section 12 closure lane without claiming provider/PAPER/LIVE qualification.

## Integrated authority

The selected lineage preserves:
- exact bounded reservation quantities and resource identities;
- canonical JournalStore generation/scope authority across restart;
- atomic fill + reservation consumption;
- exact consumed/original capacity bounds;
- stale reservation snapshot fencing;
- idempotent replay;
- atomic OMS fill-bust + economic reversal + reservation restoration;
- fail-closed omission guard for reservation-bound busts;
- settled-source busts remain blocked without distinct settlement-compensation authority;
- current-main recovery sequence fencing;
- polymorphic text/snapshot/fill/plan/prepared-binding ingress fails before financial mutation.

## Terminal FILLED bust continuation implemented in this candidate

The former terminal-correction blocker was narrowed and a conservative projection contract is now implemented.

A late provider bust must not simply relabel a terminal `FILLED` reservation as ordinary `WORKING` or `UNKNOWN`. Terminalization has already set every `remaining` resource to zero, which releases both fill-consumed capacity and any unused worst-case safety buffer. Restoring only the busted fill usage would therefore under-hold capital.

The candidate now uses a dedicated `BUSTED_PENDING_RECONCILIATION` reservation state. For a `FILLED` cut, fill-bust restoration:
- subtracts only the provider-binding-derived busted usage from `consumed`;
- rebuilds every resource's held balance as `original - still_consumed`;
- clears the old terminal evidence from the current snapshot while retaining that evidence in immutable journal history;
- includes the rebuilt balance in `total_reserved` and reservation admission;
- permits later evidence-derived consumption without converting the hold into ordinary order authority;
- rejects local `mark_unknown()` from the post-bust hold, so canonical reconciliation is still required for lifecycle resolution.

This is not an independent capital mutation. The existing atomic fill-bust integration already binds the reservation restoration plan to the same durable command as:
- the exact OMS `BUST_FILL` event and its post-bust snapshot digest;
- the exact economic reversal;
- the initial provider-fill financial binding and reservation cut;
- one JournalStore sequence CAS and command idempotency authority.

The existing settlement guard continues to reject a bust after settlement completion until a separate provider settlement-compensation authority exists. Therefore the new state does not manufacture settled cash or provider authority.

Focused pure-projection falsifiers now prove:
- terminal FILLED bust restoration reconstitutes the full released worst-case buffer, not merely the busted fill amount;
- partial bust restoration holds exactly `original - still_consumed`;
- the post-bust hold remains reserved and consumable by later evidence-derived fills;
- it cannot be locally downgraded to `UNKNOWN`.

## Remaining closure requirements

Section 12 is not yet DONE. Before merge, exact-head qualification must still prove the durable cross-owner path, including legacy/terminal replay construction rather than only pure projection semantics:
1. terminal FILLED -> post-bust hold is committed in the same OMS/economic/reservation command;
2. restart reconstructs the same hold and exact reservation totals;
3. acknowledgement-loss retry is exactly-once;
4. unrelated concurrent reservation/OMS mutation trips the existing journal-sequence fence;
5. settled-source bust remains blocked without settlement compensation;
6. the existing late-terminal regression is replaced by a positive atomic conservation regression without weakening durable terminal-evidence verification;
7. baseline and Verify AutoTrade are terminal green on the exact final head;
8. review state is clean;
9. merge and post-merge readback confirm accepted source identity.

Until those gates are complete, keep Section 12 DRAFT and do not claim complete reservation conservation, provider qualification, economic edge, profitability, release readiness, or Windows/NVDA readiness.
