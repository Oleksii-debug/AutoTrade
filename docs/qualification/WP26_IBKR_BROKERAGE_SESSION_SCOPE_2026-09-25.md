# WP-26 — IBKR brokerage session and provider account binding

Date: 2026-09-26  
Branch lineage: `wp26/ibkr-session-scope-binding-20260925-sol`.

## Provider-truth defect

Brokerage-session readiness is username/session evidence. It does not prove that
one caller-supplied account owns the session: one authenticated IB username may
have trading permission for multiple accounts. The authenticated
`GET /iserver/accounts` surface is the provider authority for accessible
account membership/current selection and also reports paper/live context.

The earlier branch model placed `account_id` directly on
`IbkrBrokerageSessionStatus`. That could manufacture account scope locally and
incorrectly model a legitimate multi-account username as a single-account
session.

## Increment

`IbkrBrokerageSessionStatus` now carries only readiness plus environment and
observation time; it no longer contains an account id.

`IbkrBrokerageAccountsObservation.from_iserver_accounts_payload()` parses and
provenance-binds the authenticated provider response:
- exact provider `accounts` membership;
- `selectedAccount`;
- provider `sessionId`;
- `isPaper` -> PAPER/LIVE environment;
- observation time and SHA-256 of the complete provider payload.

Direct construction is rejected, account lists must be non-empty/unique, the
selected account must belong to the provider list, and `isPaper` must be a real
boolean.

Order preparation now requires both independent evidence families. It checks
session readiness/freshness, provider account-evidence freshness, the intent
account's membership in the exact provider list, capability account equality,
and agreement among capability/session/provider environments. The prepared
order retains both the combined session fingerprint and exact account-observation
digest/session/selection provenance.

## Regression

Focused tests cover:
- one brokerage username exposing multiple accounts, including ordering for an
  accessible account that is not the currently selected account;
- intent account absent from `/iserver/accounts` -> fail closed;
- changed account membership after a session restart -> no stale membership
  reuse;
- stale account observation -> fail closed;
- forged direct construction, duplicate membership, invalid selection and
  non-boolean paper/live evidence;
- PAPER/LIVE mismatch remains fail closed.

This is still non-live foundation work. It does not authenticate/init a
brokerage session, serialize provider doubles, auto-confirm replies, send live
orders, or claim terminal IBKR qualification. Execution/reconciliation identity
(`permId`/`execId`) and history-retention qualification remain separate
acceptance work.
