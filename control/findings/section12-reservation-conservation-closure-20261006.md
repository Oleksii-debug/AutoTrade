# Section 12 closure — reservation conservation

## Canonical result

Section 12 / WP-15 reservation conservation is represented on current main before this control-only closure.

The canonical source ancestry includes:
- the Section-12 conservation lineage #1624 / integrated #1872;
- the later current integration successor #2035, which combines strict reservation ingress/replay scope with the newer post-bust and atomic-fill reservation state;
- previously integrated atomic admission/reservation authority tracked by #501;
- provider-fill accounting plus reservation-consumption atomicity tracked by #649;
- terminal release through one authenticated ArtifactStore snapshot tracked by #1008.

## Conservation properties present on main

Current tests and source retain:
- two concurrent intents cannot double-spend one resource;
- partial fills consume only justified reservation usage;
- the fill that makes OMS terminal atomically terminalizes reservation state;
- full-fill restart preserves the same durable cut;
- cancel releases only remaining capacity;
- UNKNOWN retains remaining exposure until evidenced terminal resolution;
- overlapping replacement continues to count the older reservation until terminal;
- duplicate/retry is idempotent and changed content conflicts;
- provider fill economics and reservation consumption commit through one durable command;
- fill correction preserves reservation high-water authority;
- FILLED bust restores conservative unresolved hold atomically;
- CANCELED bust reverses historical consumed economics without reholding a terminal cancelled order;
- late terminal bust/cancel paths remain restart-safe and fail closed;
- durable reservation replay rejects cross-environment/account rehash;
- hostile text/mapping/snapshot inputs fail before financial journal mutation;
- closure-owned reservation authority releases selected store/reader resources when the owner dies and remains callback-free.

## Acceptance evidence

Representative current-main tests include:
- test_two_concurrent_intents_cannot_double_spend_cash
- test_partial_fill_reduces_reservation_and_cancel_releases_remainder
- test_unknown_send_keeps_remaining_exposure_reserved
- test_unknown_can_release_only_after_evidenced_terminal_resolution
- test_overlapping_replacement_counts_old_reservation_until_terminal
- test_fill_economics_and_reservation_consumption_restart_together
- test_two_partial_fills_accumulate_exact_reservation_consumption
- test_exact_retry_after_restart_is_idempotent_across_both_aggregates
- test_atomic_full_fill_terminalizes_reservation_and_restart_preserves_cut
- test_only_the_fill_that_makes_oms_terminal_marks_reservation_filled
- test_fresh_bust_is_atomic_restart_safe_and_idempotent
- test_bust_restores_only_one_partial_fill_usage
- test_terminal_filled_bust_restores_full_unresolved_hold_without_relabelling_active
- test_terminal_cancelled_bust_reverses_consumed_history_without_reholding_capacity
- test_destroyed_book_releases_selected_authorities_without_next_bind
- test_replay_rejects_rehashed_cross_environment_scope
- test_replay_rejects_rehashed_cross_account_scope

Historical issue #649 records exact-head green integration evidence for the core atomic fill/reservation transaction boundary. Historical issue #501 records exact-head dual-OS/full Verify evidence for atomic financial admission with risk + reservation.

## Qualification boundary

This closes Section 12 reservation-conservation source/integration authority.

It does not grant provider/PAPER/LIVE qualification, production risk authority, release readiness, profitability or economic-edge evidence. Those remain separately owned gates and do not reopen the reservation conservation invariant.
