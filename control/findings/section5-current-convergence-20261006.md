# Section 5 current convergence — 2026-10-06

## Scope

This is the current convergence record for Section 5 — Instrument, market-data, causal internal data.

Integration base: canonical whole-product convergence #2077 at `dc62227c423edcdcd36a74124156b15f2ff214bc`.
Semantic donor: Section 5 closure #1615 at `af83832dac6364464d9b32cab0202df7befaed38`.
Historical donor parent: Section 1 v6 `a58f8b8b0d633aa04724cf72dd2addcc68d20e04`.

The current integration base already contains contract v6 and newer market-book, provider, recovery, risk and host hardening. This convergence therefore preserves the current tree and imports only Section 5 semantics that remain missing.

## Non-loss composition

Thirteen donor paths were proven byte-identical to the donor parent on the current integration base, or absent on both the donor parent and current base, and are therefore safe exact-blob overlays.

Four market-data paths had newer independent current changes:
- `mvp/autotrade_mvp/market_data.py`
- `mvp/tests/test_market_data.py`
- `mvp/tests/test_market_data_causal_snapshot.py`
- `mvp/tests/test_binance_spot.py`

For these four paths, the #1615 diff was applied semantically onto the newer current files. All fourteen production market-data hunks applied without conflict. Test-only insertion conflicts were caused by newer hostile-ingress tests moving the insertion anchors; the Section 5 tests were inserted without removing or replacing those newer tests.

## Section 5 semantics retained

1. Every normalized MarketEvent binds an exact bounded canonical `adapter_version`; adapter build participates in normalized identity.
2. One active stream generation cannot silently change adapter build; a build change requires a newer generation before book mutation.
3. Corrections/replay cannot cross adapter-build identity silently.
4. Information-claim history remains append-only while the effective causal view applies supersession and exposes current contradictions instead of silently choosing a winner.
5. Historical/fold populations revalidate exact dataset/version/manifest/content against authenticated rights-bound artifact evidence.
6. Frozen feature/fold derivation uses only causally available authenticated source population and rejects caller-forged population assertions.
7. The public MarketEvent contract and fixture expose the adapter-build identity.

## Qualification boundary

This convergence is source composition, not terminal qualification. Exact-head contract, research, MVP/baseline and full Verify runs are required on the resulting head. Queued, pending, cancelled or inherited donor checks are not PASS.

No provider/PAPER/LIVE authority, real-money readiness, profitability/economic-edge claim, signed-release claim or NVDA/HUMAN_TESTED claim follows from Section 5 source convergence.
