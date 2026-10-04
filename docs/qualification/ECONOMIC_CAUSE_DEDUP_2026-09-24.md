# Economic cause deduplication — 2026-09-24

Status: WP-14 accounting hardening.

The canonical `EconomicBook` already deduplicated immutable
`transaction_id` values, but a caller could previously submit the same
provider/economic event again under a new transaction ID and double-book cash,
position and fees.

The journal now also owns a unique `cause_event_id -> JournalTransaction`
projection. A second transaction that claims an already-booked economic cause
fails closed. Exact retry of the original transaction remains idempotent.
Reversals must carry their own correction/cause event and therefore cannot
reuse the original provider event identity.

Focused tests cover duplicate external cash causes, duplicate fill causes
(position and fee remain single-counted), reversal cause reuse, and replay/
constructor detection of duplicate historical causes.

The invariant assumes one atomic journal transaction per canonical economic
cause. Provider adapters and reconciliation must therefore preserve stable
execution/activity identities when creating `cause_event_id`.
