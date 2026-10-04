# WP-27 Alpaca option entitlement/lifecycle evidence

Exact base: `39ef7063bc17c96eb0eb51d6dccbfb24bc1e563a`.

This increment extends the existing Alpaca adapter foundation without creating a second sender, capability authority, ledger or reconciliation engine.

It adds:

- explicit fresh account evidence for Alpaca option trading level;
- fail-closed rejection for stale, blocked or insufficiently entitled option intents;
- separation between order-stream acknowledgement and separately polled option exercise/assignment/expiration activities;
- deterministic source hashing for polled lifecycle evidence.

The implementation intentionally does not infer required option level from strategy intent. The caller must bind a provider-qualified required level to the exact intended operation. This avoids silently treating all single-leg option actions as economically or legally equivalent.

This is non-live software evidence only. It does not prove paper/live equivalence, option assignment timing guarantees, economic edge, or real-account qualification.


Hardening: option entitlement admission is bound to the exact target account ID and PAPER/LIVE environment. Evidence from another account or from paper trading cannot qualify a live option intent.
