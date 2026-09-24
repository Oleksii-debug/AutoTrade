# WP-19 — single order projection authority — 2026-09-25

Base: `6fec062594f767e00039c127014c44f058387534`.

This convergence keeps the WP-19 module introduced by merged PR #87 as the
single provider-neutral order lifecycle projection and removes the parallel
`orders.py` authority after a runtime dependency scan of the exact base found
no runtime imports of either projection module.

Strengthened canonical invariants:
- acknowledgement never invents fills;
- provider execution identity is unique and cannot map to two fill identities;
- fill duplicates are idempotent only for exact immutable content;
- corrections and busts preserve append-like observation history instead of
  erasing the original provider fact;
- revisions preserve the original provider execution identity;
- repeated identical provider revisions are idempotent and conflicting reuse is
  rejected;
- late fills remain visible after rejection/cancel;
- overfill after cancel is surfaced explicitly;
- OCO double fills remain economic truth and surface a breach;
- instrument and side are bound into the order projection snapshot;
- authoritative quantities/prices reject binary float.

Boundary:
- this module is still an in-memory projection, not durable journal authority;
- provider adapters must supply qualified provider execution/revision evidence;
- accounting, reconciliation and dispatch remain separate authorities;
- no live trading authority is enabled.
