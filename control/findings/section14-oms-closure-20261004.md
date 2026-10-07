# Section 14 — internal order lifecycle closure candidate

Date: 2026-10-04
Original lineage base: `cc0b7fa5cee4597dc8039f0b0a5fd622e8b12a29`
Current-main reconvergence base: `884e573a6171a8c85fe2a3f606419d14d386f341`
Donor semantic head: `c4934d0200a1b6178214807f76a78e382f6734cf`

## Accepted product behavior already present in main

The canonical durable OMS/order projection already covers the provider-free lifecycle needed for M1:

- create intent/order identity;
- durable send-start / acknowledgement projection;
- ACK is not treated as a fill;
- UNKNOWN survives restart and resolves explicitly;
- exact partial fills;
- late fills racing cancel;
- cancel request and cancel rejection;
- replace request and replace rejection;
- fill/cancel and fill/replace race conservation;
- terminal filled/cancelled/rejected/expired states;
- exact correction and bust history;
- OCO breach history;
- duplicate durable events are idempotent;
- conflicting event-key reuse fails closed;
- restart reconstructs the same authoritative order state;
- financial/reservation effects are handled through the existing atomic canonical paths already integrated on main.

## Residual repaired on this closure lineage

PR #1629 hardens the remaining demonstrated projection trust boundaries:

- text authority rejects str subclasses before virtual string dispatch;
- OrderBookProjection accepts only exact OrderProjection objects at authority-bearing registration;
- OCO group composition accepts only exact OrderProjection objects;
- snapshot override accepts only exact bool;
- dedicated hostile-subclass regressions prove those boundaries fail closed;
- amendment ownership is now sealed to the retained registration cut: the caller-held
  `_amend_children` table is a derived index only and must exactly reconstruct
  from each sealed order identity's `parent_intent_id`;
- clearing, retargeting, adding forged entries, or replacing that index with a
  dict subclass fails closed before amendment lookup or new registration can
  influence OMS authority.

The amendment regression that previously demonstrated a bypass is no longer an
accepted failing gate on this lineage. Exact-head GitHub qualification is still
required; queued or pending workflow state is not PASS. This execution could
not perform an independent local checkout because the sandbox could not resolve
`github.com`, so no local test PASS is asserted for the repaired head.

## DONE requirements

Section 14 is DONE only when:

1. exact-head focused OMS/order tests pass;
2. baseline and Verify AutoTrade are terminal green on the exact candidate head;
3. review state is clean;
4. candidate is integrated to current main;
5. post-merge readback confirms the accepted source and the provider-free OMS lifecycle still passes.

No provider/PAPER/LIVE, real-order, profitability, signed-release or NVDA qualification is claimed.


## Current-main reconvergence — 2026-10-06

The Section 14 source patch was re-applied to exact `main@884e573a6171a8c85fe2a3f606419d14d386f341`. All 12 source hunks applied without conflict, and the reconstructed `order_projection.py` is byte-identical to donor blob `319a66b8d991b7b90f13f5328d31ccc402636aa2`. The five focused falsifier suites are carried unchanged from the canonical lineage. The historical Verify failure on `c4934d0...` was an unrelated missing `mvp.tests.capability_test_support` import in contract tests; that support module exists on this current-main base. Fresh exact-head qualification remains required and only terminal results count.


## Weakref authority-erasure hardening — 2026-10-06

The current-main review found that the donor's `WeakKeyDictionary` aggregate seal registries inherited the same callback-erasure class already demonstrated elsewhere in financial authority code. Section 14 therefore no longer uses `WeakKeyDictionary` for book/OCO seals. Closure-owned id-keyed registries retain callback-free weakrefs, seal entries retain callback-free order weakrefs, and live aggregate reinitialization is rejected before caller-visible scope or registration state can be reset. Focused regressions require zero callable weakref removal callbacks and prove book/OCO `__init__` cannot reset a live seal. This is fail-closed OMS authority hardening; it adds no provider or economic-edge claim.


## Final integration closure — 2026-10-06

Section 14 provider-free internal OMS/projection authority is closed on `main`.

- Final PR: #1629
- Accepted head: `345608e4edd3d06fc52e58af3752e73b128b70c0`
- Exact base: `9544ca69d1593c8a880956504591122c42771f16`
- Merge commit: `f899a085d79e970a39cb92350f1b623050d571da`
- Candidate tree: `73237e9ae28fb5da36d466ad55767984fb368514`
- Post-merge main tree: `73237e9ae28fb5da36d466ad55767984fb368514`
- Post-merge tree equality: PASS.

The accepted source closes the remaining demonstrated provider-free projection trust seams:
- exact built-in scalar/type admission at OMS authority boundaries;
- retained order registration identity outside caller-mutable order state;
- aggregate scope/registration seals outside caller-mutable aggregate state;
- callback-free weakref registries;
- live book/OCO reinitialization cannot reset a seal;
- hostile equality / executable-subclass inputs fail before authority-bearing comparisons;
- amendment child ownership is reconstructed from retained registration identity.

Hosted exact-head provider-free/baseline/Verify runs were queued without runner assignment at the closure cut and are not represented as PASS. This is a provider-free source/integration closure only. Provider-normalized event qualification, PAPER/LIVE transport, release, economic edge and NVDA remain separate gates and do not reopen the internal OMS conservation/trust invariant.
