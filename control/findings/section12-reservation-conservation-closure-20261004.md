# Section 12 reservations and admission-to-fill conservation closure candidate

Date: 2026-10-04
Current-main source base: `cc0b7fa5cee4597dc8039f0b0a5fd622e8b12a29`

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

## Terminal fill-bust conservation implemented in this candidate

Terminal `FILLED` and `CANCELED` cuts deliberately have different post-bust economics.

### FILLED

A late provider bust must not simply relabel a terminal `FILLED` reservation as ordinary `WORKING` or `UNKNOWN`. Terminalization has already set every `remaining` resource to zero, which releases both fill-consumed capacity and any unused worst-case safety buffer. Restoring only the busted fill usage would therefore under-hold capital.

The candidate uses a dedicated `BUSTED_PENDING_RECONCILIATION` reservation state. For a `FILLED` cut, fill-bust restoration:
- subtracts only the provider-binding-derived busted usage from `consumed`;
- rebuilds every resource's held balance as `original - still_consumed`;
- clears the old terminal evidence from the current snapshot while retaining that evidence in immutable journal history;
- includes the rebuilt balance in `total_reserved` and reservation admission;
- permits later evidence-derived consumption without converting the hold into ordinary order authority;
- rejects local `mark_unknown()` from the post-bust hold, so canonical reconciliation is still required for lifecycle resolution.

### CANCELED

A provider-confirmed cancellation remains terminal after a later fill bust. The busted fill is removed from historical `consumed`, but the canceled remainder cannot execute, so `remaining` stays zero and no capacity is re-held. The `CANCELED` state and its terminal evidence remain current.

`REJECTED` and `PROVEN_ABSENT` do not acquire fill-bust restoration authority.

These are not independent capital mutations. The existing atomic fill-bust integration binds reservation correction to the same durable command as:
- the exact OMS `BUST_FILL` event and its post-bust snapshot digest;
- the exact economic reversal;
- the initial provider-fill financial binding and reservation cut;
- one JournalStore sequence CAS and command idempotency authority.

The existing settlement guard continues to reject a bust after settlement completion until a separate provider settlement-compensation authority exists. Therefore the new state does not manufacture settled cash or provider authority.

Falsifiers now cover:
- full and partial terminal-FILLED restoration;
- unused safety-buffer reconstruction;
- post-bust held capacity remaining admission-visible;
- refusal to locally downgrade the post-bust hold to `UNKNOWN`;
- durable prepared restoration having no side effect before the shared atomic commit;
- atomic terminal-FILLED OMS/economic/reservation bust, restart and exact retry;
- late-CANCELED OMS/economic/reservation bust, restart and no-rehold semantics;
- rejection of restoration authority for REJECTED/PROVEN_ABSENT.

## Remaining production closure seam

Section 12 is not yet DONE.

The production provider-fill path currently prepares an OMS `RECORD_FILL` and atomically commits that fill with reservation `CONSUME`, economics, optional settlement and provider-fill binding. When the prepared OMS post-fill snapshot is `FILLED`, the durable reservation path still does **not** derive a journal-native terminal `FILLED` transition from that same OMS event. `DurableReservationBook.mark_terminal()` intentionally accepts reconciliation semantics only for `PROVEN_ABSENT`; its source explicitly keeps capacity held until a canonical terminal order/fill projection proves `FILLED`.

Therefore the late-terminal `FILLED` regressions in this candidate use bounded test-only legacy fixtures. They prove the bust-side repair, but they do not prove that a normal production full provider fill creates the terminal reservation cut that the repaired bust path can later reverse.

The correct continuation is not to weaken reconciliation verification. It is to add one journal-native full-fill reservation transition whose replay is bound to the exact atomic OMS `RECORD_FILL` event and its `FILLED` post-snapshot, while preserving exact retry of historical commands that committed only `CONSUME`. A fresh full fill must not release unused worst-case reservation capacity unless that canonical OMS evidence is committed in the same command. Legacy retry must not rewrite old journal history.

Before Section 12 can be marked DONE:
1. a normal fresh full provider fill must atomically commit OMS `FILLED`, economic effects, provider binding and the terminal reservation cut from one durable command;
2. partial fills must continue to consume without terminal release;
3. historical full-fill commands containing only reservation `CONSUME` must remain replayable/idempotent and conservatively held;
4. a late bust after a production-created terminal `FILLED` cut must reconstruct `original - still_consumed` in the same OMS/economic/reservation bust command;
5. restart, acknowledgement-loss retry and unrelated concurrent mutation fencing must remain exact;
6. settled-source bust must remain blocked without settlement compensation;
7. baseline and Verify AutoTrade must be terminal green on the exact final head;
8. review state must be clean;
9. merge and post-merge readback must confirm accepted source identity.

Until those gates are complete, keep Section 12 DRAFT and do not claim complete reservation conservation, provider qualification, economic edge, profitability, release readiness, or Windows/NVDA readiness.
