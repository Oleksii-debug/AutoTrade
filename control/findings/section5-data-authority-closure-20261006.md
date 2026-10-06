# Section 5 data-authority closure — 2026-10-06

## Canonical result

Section 5 — instrument, market-data and causal internal-data authority — is integrated on `main`.

- Final closure PR: #2155
- Accepted exact head: `a2a03e3eded025426a18b66d48b11ee8080777fb`
- Accepted base: `8495a66cf7d8979c5e8b3811c2088523ed147bf1`
- Merge commit: `3e28de127dea011a73c6000e46b0513ed2ba8ea9`
- Candidate tree: `9ff1b5dc2957bbfc18968664c47c652ac8adf2ff`
- Post-merge main tree: `9ff1b5dc2957bbfc18968664c47c652ac8adf2ff`
- Post-merge tree equality: PASS

## Historical Section-5 convergence

The older #1615 source convergence is absorbed by main:
- all of its production implementation paths are present on current main;
- production semantics are byte-identical to the accepted lineage;
- remaining differing paths are newer canonical contract fixtures/tests;
- the old private finding file was not required as executable authority.

The stale #1615 PR is closed as superseded.

## WP-07 final hardening convergence

PR #2192 owned hostile executable metadata-ingress hardening in exactly:
- `mvp/autotrade_mvp/instruments.py`
- `mvp/tests/test_instruments.py`

Its exact base instrument blob was identical to the #2155/current-main preimage.

A deterministic three-way line merge found:
- 18 Section-5 instrument hunks;
- 26 WP-07 ingress-hardening hunks;
- zero overlapping base ranges.

The final #2155 tree therefore retains both:
- adjusted-option / settlement-convention Section-5 semantics;
- exact built-in text/decimal/datetime/container/calendar/evidence ingress hardening.

The exact #2192 `test_instruments.py` blob was carried into #2155. #2192 is closed as superseded.

## Instrument authority

The final InstrumentRegistry authority includes:
- immutable version identity and effective-dated provider-symbol resolution;
- causal knowledge lookup using metadata evidence;
- exact calendars and explicit DST transition authority;
- exact price/quantity units and precision bounds;
- derivative version identity;
- explicit adjusted-option exercise cash authority;
- immutable inverse-futures settlement convention authority;
- exact built-in metadata ingress that rejects executable subclasses before callbacks.

## Market and internal-data authority

Main retains the earlier Section-5 convergence for:
- normalized MarketEvent/raw-evidence chronology and adapter-build identity;
- causal information-claim contradiction/supersession semantics;
- immutable historical vintages and causal revision visibility;
- authenticated rights-bound source-population resolution;
- deterministic authoritative population fingerprints and fold fitting.

## Replay/common-cut bridge

The last #974 dependency seam is closed:
- product code detaches `CompositeReplayCheckpoint`;
- exact `RuntimeStateVerifier` verification happens before research fit/transform;
- research receives the fingerprint of the verified detached checkpoint rather than caller-selected text;
- checkpoint/verifier subclasses are rejected before callbacks;
- later caller mutation cannot rewrite the frozen fold's common-cut identity.

Issue #974 is closed as completed.

## Qualification boundary

This closes Section 5 data/instrument authority.

It does not close the broader WP-12 whole-runtime checkpoint/recovery portability work tracked by #699. It also does not claim provider/PAPER/LIVE authority, economic edge, profitability, signed release or NVDA qualification.

Fresh exact-head hosted workflows for the final #2155 head were queued/pending and one provider-free run was cancelled before runner completion. Those states are not represented as PASS. Source/integration closure is established by exact-head merge, post-merge tree equality, absorbed prior production lineage and checked-in regression coverage.
