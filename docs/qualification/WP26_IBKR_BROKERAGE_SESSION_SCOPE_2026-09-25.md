# WP-26 — IBKR brokerage session scope binding

Date: 2026-09-25  
Base: `main@7b8bd88aa09d32d3295b1f38465ec7fa7c73920b`.

## Defect

The Web API foundation checked freshness and four brokerage-session readiness booleans, but that evidence carried no account or environment identity. Order preparation also checked the capability account and instrument without checking that the capability environment matched the brokerage session. A fresh trade-ready observation from a different account/environment could therefore satisfy the local readiness gate.

## Increment

`IbkrBrokerageSessionStatus` is now explicitly scoped by:
- `account_id`;
- `environment = PAPER | LIVE`;
- existing connected/authenticated/established/competing state;
- observation timestamp.

Before normalized order preparation:
- the session account must equal the intent account;
- capability account must still equal the intent account;
- capability environment must equal the exact brokerage-session environment;
- session freshness and trade-ready checks still apply;
- exact capability/instrument/order/TIF permission remains mandatory.

## Regression

Focused tests reject a ready session from another account, reject PAPER capability against a LIVE session, and reject non-brokerage pseudo-environments such as SIMULATION.

This does not initialize or authenticate an IBKR session, qualify provider numeric serialization, bypass reply handling, or perform a live order.
