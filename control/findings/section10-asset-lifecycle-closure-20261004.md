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

## Follow-up source review: registry selection authority

Exact Python type, construction snapshots and the merged `InstrumentRegistry` publication authority prove integrity of one registry's retained contents. They do **not** prove that a particular publicly constructible registry was selected by AutoTrade product composition. A caller can construct an exact `InstrumentVersion`, construct `InstrumentRegistry(versions=(version,))`, resolve that version, and therefore satisfy an integrity-only lifecycle check.

The candidate now makes the boundary explicit instead of manufacturing a second caller-mintable token:
- `lifecycle_gate(...)` remains a provider-neutral diagnostic resolver for an exact registry/version cut and still reports `OPEN`, `TRADING_ENDED`, `EXPIRED`, `DELIVERY_BLOCKED`, or instrument status;
- `require_open_for_new_exposure(...)` preserves those hard negative lifecycle reasons, but an `OPEN` diagnostic result fails closed until a real product/composition-owned registry authority is supplied by the actual order-admission boundary;
- a caller-constructed exact registry can no longer authorize new futures exposure;
- wrong registry, stale version, forged economics, inactive instruments, subclasses and hostile nested time values remain fail-closed;
- the existing canonical `InstrumentRegistry` continues to be reused for content integrity; no parallel registry or caller-mintable authority object is introduced.

This deliberately demotes the provider-neutral helper from financial authorization where provenance cannot currently be proven. It is a safety closure, not a claim that the product already has a complete positive new-exposure composition path. Section 10 therefore remains not-DONE until the real admission composition owns/re-resolves the selected instrument authority and exact-head qualification is terminal green.

Exact-head CI remains required. Queued or pending workflow state is not PASS.

Nested time authority is also fail-closed on this lineage. Lifecycle datetimes on both the contract and its bound version must retain exact UTC `datetime` values with `timezone.utc`; an exact `datetime` carrying caller-controlled `tzinfo` is rejected before equality/conversion can dispatch a timezone callback. Dedicated regressions cover both contract and bound-version mutation.
