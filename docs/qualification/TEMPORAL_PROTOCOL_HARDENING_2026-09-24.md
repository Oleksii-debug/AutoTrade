# Temporal scientific protocol hardening — 2026-09-24

Status: WP-35 protocol-registry hardening.

The scientific registry now rejects free-text temporal placeholders for
train/validation/test/forward periods. Each period must contain exact UTC
`start` and `end` instants, start must precede end, and the four registered
windows must be ordered without overlap.

`purge_embargo` is machine-readable in seconds. Registered label horizons are
positive integer seconds and the purge must cover the longest registered
horizon. This makes a protocol incapable of claiming a purged split while its
declared label dependency is longer than the purge.

Focused tests cover legacy free text, overlapping windows, reversed windows,
naive timestamps, insufficient purge, booleans and negative temporal controls.

This does not itself prove independence of a dataset. The evaluation runner
must still enforce the registered periods, purge and embargo against actual
sample provenance and record every holdout access.
