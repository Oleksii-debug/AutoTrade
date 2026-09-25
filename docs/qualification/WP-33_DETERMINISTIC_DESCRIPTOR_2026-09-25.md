# WP-33 deterministic strategy descriptor evidence

Exact base: `39ef7063bc17c96eb0eb51d6dccbfb24bc1e563a`.

This increment extends the existing first-party deterministic zero-model strategy path. It does not create a second strategy engine, execution authority, or any economic-edge claim.

Implemented evidence:

- versioned immutable strategy descriptor with feature schema, market/instrument requirements, minimum history, horizon, decision schedule, proposal/exit semantics, bounded parameters, resource profile, supported regimes, source/rights status, evaluation protocol and artifact hashes;
- canonical JSON plus SHA-256 descriptor digest for experiment lineage;
- exact Decimal parameter values with float rejection;
- pure proposal-from-snapshot function that verifies the descriptor parameters match the serialized strategy state before producing a proposal;
- fail-closed handling for malformed hashes, duplicate metadata and descriptor/state mismatches;
- zero-model and `UNPROVEN` economic-edge semantics remain unchanged.

Qualification boundary: this is software-contract evidence for WP-33. It is not proof of profitability, not WP-36 scientific gate completion, not forward-paper evidence, and grants no provider or real-money authority.
