# Causal fold feature qualification foundation — 2026-09-24

Status: **WP-34 causal feature/fold hardening; not complete model-research qualification**.

This change extends the existing causal feature authority rather than adding a
second feature pipeline.

Implemented controls:

- explicit immutable walk-forward fold identity with train/validation windows
  and purge interval;
- training-information cutoff derived from the registered fold;
- fold normalizer fits only matching feature points inside the purged training
  window even when the caller passes the complete dataset;
- future validation extremes therefore cannot alter fitted mean, scale or
  provenance hash;
- a fitted normalizer is bound to exact fold fingerprint and feature name and
  refuses use on another fold or outside the frozen validation window;
- fold training rows require feature/label symbol and anchor alignment and
  exclude labels whose availability crosses the training-information cutoff;
- cross-market features require every configured component to be causally
  available, within an explicit freshness limit, and produce a provenance hash
  over observation IDs, revisions, availability times and values.

Focused tests cover full-dataset-fit leakage, purge-tail exclusion, exact fold
binding, out-of-window transform rejection, delayed labels, overlapping fold
rejection, future/stale cross-market components and revision-sensitive
provenance.

Remaining WP-34 work includes asset/regime-specific feature libraries,
purge/embargo derivation from each label dependency horizon rather than a
caller-provided interval, process/filesystem isolation for untrusted generated
strategies, and exact integration with the registered experiment protocol and
locked evaluation runner.
