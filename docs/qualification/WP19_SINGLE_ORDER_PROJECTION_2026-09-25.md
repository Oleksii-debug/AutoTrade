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
- `OrderProjection` / `OrderBookProjection` remain the deterministic in-memory lifecycle model; durability is supplied by the separate `DurableOrderBookProjection` adapter over the canonical `JournalStore`, not by a second OMS or database;
- provider adapters must supply qualified provider execution/revision evidence;
- accounting, reservation authority, reconciliation decisions and guarded dispatch remain separate authorities; WP-19 records their order-lifecycle consequences but does not absorb their powers;
- durable order events do not by themselves authorize reservation release, book accounting, prove economic edge, or qualify a real provider;
- no live trading authority is enabled.


## Pending cancel is not terminal cancellation

The canonical projection now separates `request_cancel()` from provider-confirmed `confirm_cancel()`. A pending request keeps the unfilled remainder economically live and exposes `CANCEL_REQUESTED` / `PARTIALLY_FILLED_CANCEL_REQUESTED`; fills and overfills observed while cancellation is pending remain explicit. Only confirmation produces `CANCELLED` / `PARTIALLY_FILLED_CANCELLED`.

The legacy `cancel()` entry point remains only as a compatibility alias for confirmed cancellation evidence. Callers that merely sent a cancel request must use `request_cancel()`; treating send/acknowledgement as confirmed terminal cancellation would violate this contract.

This converges the useful late-fill/rejection semantics previously explored in PR #154 into the single `order_projection.py` authority. No order send, cancel transport, accounting or reconciliation authority is added here.


## Lifecycle identity hardening on whole-product convergence

Stacked continuation from exact whole-product integration head `24379f28298e287656d1044200cd2f447bcbb787`.

Additional fail-closed invariants:
- submission outcome is monotonic: UNKNOWN may resolve to ACCEPTED or REJECTED, while an accepted/rejected terminal submission outcome cannot later be relabelled as another submission outcome;
- provider-confirmed cancellation cannot overwrite an already rejected submission fact;
- linked amendment children must retain the parent's instrument, side and OCO group identity;
- every canonical order projection is explicitly bound to provider, account and environment identity;
- every OrderBookProjection accepts only orders in its exact provider/account/environment scope, so client/execution/OCO/amendment identities cannot collide across foreign financial domains.

These checks remain inside the existing provider-neutral OrderProjection/OrderBookProjection authority. They do not move dispatch, reconciliation, provider transport, accounting or live trading authority into WP-19.


## Pending replace and expiry lifecycle completion

The same canonical projection now materializes two lifecycle facts required by document 03:
- `request_replace()` records `REPLACE_REQUESTED` (or `PARTIALLY_FILLED_REPLACE_REQUESTED`) without creating a child order, a fill, or amendment completion;
- `confirm_expired()` records provider-evidenced expiry of the remaining quantity while preserving already-filled economics and any later fill evidence.

Cancel and replace cannot be pending simultaneously. Rejected/cancelled/expired orders cannot be silently relabelled into another terminal operational outcome, and pending cancel/replace must resolve before expiry is recorded. Late fills after expiry remain explicit as `FILLED_AFTER_EXPIRY` / `OVERFILLED_AFTER_EXPIRY`; expiry never erases economic truth.


## Provider/account/environment projection scope

Document 02 requires account/environment on the intent and environment-separated identities; document 03 requires provider/account/environment separation throughout provider execution. WP-19 now carries that scope directly:
- `OrderProjection` and `OrderSnapshot` require `provider_id`, `account_id`, and canonical `Environment`;
- `OrderBookProjection` is constructed for one exact provider/account/environment domain and rejects cross-scope registration;
- linked amendments inherit that book scope;
- legacy `OcoGroupProjection` rejects peers from a different scope.

A previous draft-only idea to enforce `provider_order_id` uniqueness globally was removed before integration because the canonical uniqueness rule is scope-dependent and adapters, not the provider-neutral projection, declare which provider keys are guaranteed unique.


## Optional provider order identity

The canonical SubmissionResult contract makes `provider_order_id` optional. WP-19 therefore does not fabricate or require one for UNKNOWN/REJECTED (or an acknowledgement where the provider has not supplied it). An UNKNOWN submission can be persisted with only its stable client-order identity and later resolve to ACCEPTED with an evidenced provider-order ID. Once a provider-order ID is observed, a different later value remains a fail-closed conflict. This preserves AMBIGUOUS_WRITE → reconciliation-first semantics without blind retry.


## Cancel/replace command lineage

Canonical contract 02 requires cancellation/replacement to use separate command identities and retain lineage. Order snapshots now surface `parent_intent_id`, `cancel_command_id`, and `replace_command_id`. Pending cancel/replace requests require a command ID; an exact retry of that ID is idempotent, while a different command ID attempting to alias the same pending action fails closed. The durable adapter persists these identities through restart.


## Canonical provider submission outcome compatibility

The provider contract emits `SubmissionResult.outcome = ACKNOWLEDGED | REJECTED | UNKNOWN`. WP-19 accepts `ACKNOWLEDGED` directly and canonicalizes it to its internal accepted-submission state; provider adapters or integration code do not need to invent a second translation enum. ACKNOWLEDGED still does not create a fill.


## Durable journal-backed projection and restart proof

`DurableOrderBookProjection` is the persistence adapter for the single canonical WP-19 model. It:
- uses the existing SQLite `JournalStore` and one scope aggregate per `(provider_id, account_id, environment)`; it creates no parallel persistence authority;
- commits immutable, hashed order-mutation events before publishing the rebuilt process projection;
- replays every event through the same `OrderBookProjection` logic and verifies the persisted snapshot against the replayed result;
- provides deterministic event-key idempotency, aggregate-version fencing and exact-retry behavior;
- persists create/acknowledge/unknown/reject/fill/correction/bust/cancel/replace/expiry facts, including action command lineage;
- fails closed on payload tampering, unknown but well-hashed order operations, conflicting event-key reuse and cross-scope state;
- has restart coverage for fills, corrections/busts, historical OCO breach, UNKNOWN resolution, pending lifecycle state and action-command identity.

This is durable order-lifecycle evidence, not an assertion that accounting, reservations and reconciliation are atomically committed with every order mutation. That broader transaction boundary remains integration work outside WP-19.

## Guarded-dispatch / simulated-provider / reconciliation integration proof

A focused network-free integration test uses the repository's real `GuardedDispatcher`, `stable_client_order_id`, `SimulatedProvider`, durable WP-19 projection and `reconcile_account` together. It proves:
- the pre-send durable order and dispatcher derive the same scoped client-order identity;
- canonical provider `ACKNOWLEDGED` is recorded as working order state and still does not fabricate a fill;
- the separately observed provider execution moves the durable projection to `FILLED` and survives restart;
- an `AFTER_ACCEPT_RESPONSE_LOST` send remains durable `UNKNOWN` without a fabricated provider-order ID;
- reconciliation can resolve that ambiguity from execution evidence;
- retrying the same UNKNOWN dispatch does not produce a second outbound send.

The test does not enable live transport and does not bypass the existing WP-17 reservation terminal-release gate.


## Durable WP-18 submission-attempt projection

WP-19 now carries `submission_attempt_id` and the explicit `SEND_STARTED` state required by document 03. `DurableOrderBookProjection.sync_submission_attempt()` reads the already committed WP-18 `submission_attempt` aggregate and projects only evidenced facts:
- `SubmissionSending` binds the exact attempt and produces `SEND_STARTED` without fabricating provider acknowledgement;
- `SubmissionSent` validates response attempt/client identity and projects the canonical ACKNOWLEDGED/REJECTED/UNKNOWN result;
- `SubmissionUnknown` binds UNKNOWN even when no provider-order ID was returned;
- a pre-send `SubmissionBlocked` produces no external-order send fact and leaves the order PENDING;
- repeated sync is idempotent because event keys are derived from immutable source event IDs.

The adapter is read-only with respect to WP-18: it never sends, retries, edits or replaces dispatch events. A different outbound attempt trying to bind the same external order after send-start fails closed.
