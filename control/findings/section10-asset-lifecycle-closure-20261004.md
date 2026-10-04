# Section 10 asset lifecycle closure candidate

Date: 2026-10-04
Base main: `cc0b7fa5cee4597dc8039f0b0a5fd622e8b12a29`

Current main already contains the provider-free lifecycle foundations for equities/short-borrow, futures, perpetual margin/funding and options, including canonical InstrumentVersion quantity grids, exact option economics and authenticated borrow evidence.

## Residual defect closed here

The generic provider-neutral futures lifecycle still accepted a caller-authored `physical_delivery_authorized=True` scalar. That could reopen exposure after the physical-delivery cutoff without any separately qualified delivery authority.

This candidate removes that bypass:
- provider-neutral PHYSICAL futures are unconditionally DELIVERY_BLOCKED at/after delivery_cutoff;
- the public new-exposure gate no longer accepts a delivery-authorization boolean;
- construction-time lifecycle authority remains protected against post-construction object/instrument mutation;
- future physical-delivery support must arrive through a separately reviewed provider/authority composition.

## Already integrated and not duplicated

- canonical instrument registry and causal lookup;
- exact option quantity grid and lifecycle arithmetic;
- one EconomicBookCut for option position/economic decisions;
- perpetual-margin exact arithmetic;
- authenticated securities-borrow evidence;
- funding/financing/settlement authorities already present in current main.

## Closure requirements

Section 10 is DONE only if exact-head qualification confirms:
1. supported equity/short/futures/perpetual/options lifecycle transitions are deterministic and exact;
2. expiry/exercise/assignment/settlement cannot create duplicate economics;
3. no caller scalar can reopen blocked physical-delivery exposure;
4. lifecycle state survives restart consistently;
5. baseline, futures qualification where applicable, and Verify are terminal green;
6. review state is clean;
7. merge and post-merge readback confirm accepted source identity.

No real physical delivery, provider/PAPER/LIVE, profitability, release or NVDA qualification is implied.

## Follow-up source review: canonical lifecycle binding

Exact source review after the initial boolean-bypass repair found a second provider-neutral self-authorization path: lifecycle_gate accepted an exact FuturesContract whose canonical_instrument was absent. A caller could therefore supply its own lifecycle dates/settlement method without any InstrumentVersion binding and receive OPEN.

The canonical Section 10 candidate now also:
- requires an exact canonical InstrumentVersion for lifecycle_gate and require_open_for_new_exposure;
- fails closed if canonical_instrument is removed after FuturesContract construction;
- retains the construction-time lifecycle snapshot and exact InstrumentVersion consistency checks;
- includes regressions for both an initially unbound self-authored contract and post-construction removal of the canonical binding.

This is still provider-neutral safety only. It does not prove that a provider-origin InstrumentVersion was issued, does not enable physical delivery, and does not grant PAPER/LIVE authority.
