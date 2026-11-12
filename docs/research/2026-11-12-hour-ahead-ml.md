# Hour-ahead model training on WhiteBIT M5 history

BTC_PERP, ETH_PERP and SOL_PERP each supplied 17,280 authenticated five-minute candles from the existing 60-day course acquisition. For each market we fitted separately on two disjoint 30-day windows. Within a window, training uses only earlier rows, with purged boundaries before DEVELOPMENT and FINAL. Calendar history was previously available. These runs give retrospective research evidence.

The existing OHLCV XGBoost screen trained six native models with a 12-bar horizon, virtual long and short actions, a 26 basis point cost envelope, at least eight trades per stage and a 10% maximum drawdown. All six had a positive training threshold. None qualified on DEVELOPMENT, so no FINAL was scored. At the common 26 basis point entry threshold the six DEVELOPMENT runs generated 129 simulated round trips. Their average gross return was approximately 0.49 basis points per trade versus the 26 basis point cost envelope. Each DEVELOPMENT run lost after costs.

A simpler comparison used standardized ridge regression, an unpenalized intercept and a fixed penalty of 0.1 times the training sample size. It used the same data, features, horizon and cost envelope with a fixed 26 basis point entry threshold. Six model checkpoints were persisted. DEVELOPMENT generated eight trades, and every active window lost after costs. No FINAL was scored.

Artifacts are under runtime/m5-xgboost-hourly-20261112 and runtime/m5-ridge-hourly-20261112. Each has a protocol, summary and verified model checkpoints. The ridge summary SHA-256 over its canonical content is 23a70eaf5b4853563d35dc4b7057f088bc89855b66480143891ca4b89ed7eab5. The XGBoost summary links the native weights and each individual result. Both runs use full virtual notional in the existing simulator, which differs from the frozen SOL forward shadow's 25% allocation. Neither model family qualified for promotion. The requested 1% hourly profit remains unverified.

The models ran locally without paid calls. The recorded market data and native weights remain on the local runtime disk.


## Volatility diagnostic and durable depth history

A fixed filter admitted decisions when completed two-hour volatility reached the training period's 75th percentile. With the same saved XGBoost weights and cost threshold, the share of DEVELOPMENT decision rows with enough gross movement to cover modeled costs rose from 3,771/10,194 to 1,157/2,065. These one-hour outcome rows overlap. Directional results remained weak. Five of six filtered runs lost and the only positive run returned 0.01924% over its full development period with five trades. The minimum was eight. FINAL stayed closed. Receipt 965d39d95ce85be768471751ccc41415174fe756b86b9cf33e9286b2a13e4d6d is in runtime/m5-hourly-volatility-gate-20261112.

The public depth collector now retains five-minute features beyond its three-day raw retention. Each archived window includes observed bucket count, first and last epochs, snapshot and generation counts, mid prices, spread, imbalance and its last source identity. The collector writes completed windows after a one-minute settling delay and resumes by market and window across restarts. Coverage fields allow later research to exclude sparse or disconnected windows. The archive uses the existing SQLite database and public collector.

A live-tape backup produced 144 archived windows across BTC, ETH and SOL. The repeated pass inserted zero duplicates. Twenty-five focused tests passed, including retention and restart regression coverage.
