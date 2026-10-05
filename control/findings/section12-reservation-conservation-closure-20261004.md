# Section 12 reservations and admission-to-fill conservation closure candidate

Date: 2026-10-04; current-main/public-surface reconvergence updated 2026-10-05
Current-main source base: `60e7c95b3b572810dcfb6c4ab34e0b338b0ace02`
Implementation readback before this finding update: `e7b054f0e626468e314bde1b551a880c0b2abc8d`

Current main already contains the canonical reservation authority, exact restart binding and atomic provider-free fill/financial consumption. This candidate carries the current-main reservation-bust lineage into the Section 12 closure lane without claiming provider/PAPER/LIVE qualification.

## Integrated authority

The selected lineage preserves:
- exact bounded reservation quantities and resource identities;
- canonical JournalStore generation/scope authority across restart;
- atomic OMS fill + reservation + economics composition;
- exact consumed/original capacity bounds;
- stale reservation snapshot fencing;
- idempotent replay;
- atomic OMS fill-bust + economic reversal + reservation restoration;
- fail-closed omission guard for reservation-bound busts;
- settled-source busts remain blocked without distinct settlement-compensation authority;
- current-main recovery sequence fencing;
- polymorphic text/snapshot/fill/plan/prepared-binding ingress fails before financial mutation.

## Public provider-fill mutation fences

Current-main reconvergence exposed a public-surface regression outside the core atomic writer: the compatibility facade again re-exported lower-level generic financial primitives that retain historical no-reservation shapes.

The candidate therefore restores the existing bounded public fences without replacing the atomic implementation:
- a caller cannot present a `PreparedProviderFillBinding` to the generic economic/reservation batch function and choose independent `usage` or transactions; provider-fill-bound publication must enter through the evidence-derived canonical provider-fill entrypoint;
- durable provider-fill financial-binding events are the authority for transaction ownership; no transaction-id naming heuristic is used;
- a fresh generic economics+settlement correction of a provider-fill-owned source transaction fails closed unless the reservation-aware correction path supplies the canonical correction binding;
- the legacy no-reservation correction shape remains available only when both the economic correction and replacement settlement are already durable, preserving exact upgrade/retry readback without permitting fresh publication;
- ownership resolution is bound to the current durable economic-book provider/account/environment scope and verifies event type, version, payload hash, request digest and transaction identity before granting provider-fill ownership meaning.

The regression sequence commits a canonical provider fill with 100 units of cash reservation consumption, constructs a 1.0→1.1 correction, proves the public generic correction fails with no economic/settlement/reservation mutation, then proves the canonical reservation-aware correction consumes exactly the additional 10 once. A separate facade test proves a caller-supplied provider-fill binding cannot dispatch through the generic batch barrier, while an unbound legacy generic batch retains its historical behavior.

These fences add no provider qualification or economic authority; they only prevent weaker compatibility surfaces from bypassing the already-selected WP-15 authority.

## Expected-fill identity and product-flow reachability

The admitted/expected provider-fill path now requires an exact non-null client-order identity on both the projected fill and independently observed provider fill before accounting derives any economic effect. Equality is unconditional once both identities exist. A provider execution without authenticated order linkage therefore cannot be relabeled as an admitted expected fill by supplying only intent/reservation-compatible economics; it remains on the separate unexpected/manual reconciliation authority. The negative regression exercises both provider-null and both-null client-order cases and requires zero economic mutation.

The credential-free product flows represented by this candidate also exercise the same WP-15 authority rather than only its isolated unit tests:
- the canonical single-session SIMULATION path constructs exact provider and projected fill evidence from the observed simulated execution, derives its settlement obligation from the canonical provider-fill financial plan, and commits through `commit_provider_fill_with_reservation_consumption()`; orchestration no longer supplies its own reservation usage or economic transaction to the atomic mutation barrier;
- its restart test requires one durable `provider_fill_financial_binding`, exact reservation/intent/provider-execution identities, exact `CASH:USD=103.103` derived usage, a canonical `provider-fill:` transaction identity, and a reopened reservation cut with consumed `103.103` and zero remaining;
- the whole-simulator qualification flow likewise constructs provider/projected evidence and calls the canonical provider-fill entrypoint instead of publishing caller-authored `usage={CASH:USD:200.2}` plus a caller-built equity transaction. It checks the durable binding, exact `200.2` consumption and restart projection.

These changes strengthen reachability and classification correctness in SIMULATION only. They do not qualify any external provider, enable PAPER/LIVE order authority, or establish economic edge.

## Production full-fill reservation terminalization

The production provider-fill writer now distinguishes partial and terminal OMS fills from the exact prepared `RECORD_FILL` post-snapshot.

For a partial fill, the durable reservation event remains `CONSUME`: exact evidence-derived usage is consumed and the unresolved remainder stays held.

For an OMS post-fill snapshot whose state is `FILLED`, the same atomic provider-fill command uses journal-native `CONSUME_AND_MARK_FILLED`. That transition:
- consumes the exact fill usage;
- zeros every remaining reservation resource only because the exact OMS cut is terminal `FILLED`;
- derives reservation terminal evidence from the OMS fill event id, OMS payload hash, OMS post-snapshot digest and OMS mutation hash;
- binds the exact fill identity (provider, client order id, fill id and provider execution id) into the durable reservation request;
- commits the OMS event, reservation event, economics, optional settlement and provider-fill binding in the same JournalStore command/event batch;
- preserves historical exact retry for older commands that durably recorded only reservation `CONSUME` rather than rewriting legacy journal history; the fallback is accepted only through the canonical legacy `prepare_consume_mutation()` path resolving `already_committed=True` with its exact historical request/snapshot authority.

Durable reservation replay does not trust the stored terminal evidence string alone. For `CONSUME_AND_MARK_FILLED`, replay resolves the referenced immutable OMS event from JournalStore and verifies:
- exact event id/type/aggregate type;
- stored OMS payload hash against both the event and reservation binding;
- provider/account/environment scope;
- `RECORD_FILL` operation;
- exact client order, fill and provider-execution identity;
- terminal OMS snapshot state `FILLED` with exact zero `open_quantity`;
- OMS request hash;
- the exact OMS post-snapshot digest named by the reservation event; and
- the exact OMS mutation hash named by the reservation event.

A negative durable-boundary falsifier also proves that a terminal reservation envelope with only fabricated OMS identifiers/hashes cannot survive reservation replay when the referenced OMS event is absent. A separate compatibility falsifier proves a historical `CONSUME` component remains historical `CONSUME` under the new terminal prepare path.

The end-to-end falsifiers cover both a one-fill terminal order and a two-fill order. The first partial fill must keep the reservation `WORKING` and admission-visible; only the second fill that makes the OMS terminal may atomically mark the reservation `FILLED`. Restart and exact retry preserve the same terminal reservation cut without duplicate economic or reservation mutation.

## Terminal fill-bust conservation

Terminal `FILLED` and `CANCELED` cuts deliberately have different post-bust economics.

### FILLED

A late provider bust must not simply relabel a terminal `FILLED` reservation as ordinary `WORKING` or `UNKNOWN`. Terminalization has already set every `remaining` resource to zero, which releases both fill-consumed capacity and any unused worst-case safety buffer. Restoring only the busted fill usage would therefore under-hold capital.

The candidate uses a dedicated `BUSTED_PENDING_RECONCILIATION` reservation state. For a `FILLED` cut, fill-bust restoration:
- subtracts only the provider-binding-derived busted usage from `consumed`;
- rebuilds every resource's held balance as `original - still_consumed`;
- clears the old terminal evidence from the current snapshot while retaining that evidence in immutable journal history;
- includes the rebuilt balance in `total_reserved` and reservation admission;
- permits later evidence-derived consumption without converting the hold into ordinary order authority; and
- rejects local `mark_unknown()` from the post-bust hold, so canonical reconciliation is still required for lifecycle resolution.

A late bust after a production-created terminal `FILLED` cut is exercised without a synthetic terminal fixture: OMS fill removal, economic reversal and reservation re-hold commit under the existing atomic bust authority, and restart/retry preserve the resulting cut.

### CANCELED

A provider-confirmed cancellation remains terminal after a later fill bust. The busted fill is removed from historical `consumed`, but the canceled remainder cannot execute, so `remaining` stays zero and no capacity is re-held. The `CANCELED` state and its terminal evidence remain current.

`REJECTED` and `PROVEN_ABSENT` do not acquire fill-bust restoration authority.

These are not independent capital mutations. The existing atomic fill-bust integration binds reservation correction to the same durable command as:
- the exact OMS `BUST_FILL` event and its post-bust snapshot digest;
- the exact economic reversal;
- the initial provider-fill financial binding and reservation cut; and
- one JournalStore sequence CAS and command idempotency authority.

The existing settlement guard continues to reject a bust after settlement completion until a separate provider settlement-compensation authority exists. Therefore the new state does not manufacture settled cash or provider authority.

## Qualification state

The known source-level Section 12 conservation seams represented by this candidate are implemented and have executable falsifiers. Section 12 is still not DONE until integration qualification completes.

Before this candidate can be integrated:
1. baseline and Verify AutoTrade must be terminal green on the exact final head;
2. any focused reservation/OMS/financial tests selected by those gates must be terminal green on that same head;
3. review state must remain clean and the PR must remain mergeable against accepted main;
4. merge must use the reviewed exact head; and
5. post-merge readback must confirm the accepted source identity and required checks.

No green software result establishes economic edge. This candidate adds no provider/PAPER/LIVE/real-money/profitability/release/NVDA authority. Until the gates above complete, keep Section 12 DRAFT and do not claim Section closure.
