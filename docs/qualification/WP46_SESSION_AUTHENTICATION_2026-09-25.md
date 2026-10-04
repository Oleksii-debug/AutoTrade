# WP-46 — authenticated session and host-action hardening — 2026-09-25

Status: **IMPLEMENTATION_INCREMENT_AWAITING_EXACT_HEAD_CI**

Exact whole-product parent: `33269ae22c5fdf5d7af2c6fe3b695a736173c3e1`.

This increment reuses the existing `SecurityBoundary`, protected credential vault, durable host store and canonical `mvp/autotrade_mvp/host_actions.py`. It does not add a second authentication engine, credential store or host-action policy.

## Current invariant

Session minting already requires an injected independent `session_authorizer`; paired HTTPS/loopback origin alone is not identity. Host mutations are additionally constrained by the canonical enumerated action policy. Unknown/future/non-canonical actions are rejected at the command canonicalization boundary before session authorization, durable state mutation or event append. Owner-only authority actions and Owner/Operator exposure-blocking semantics remain unchanged.

## Focused regression

`mvp/tests/test_security.py` proves that both OWNER and OPERATOR submissions of an unknown future privileged action fail with the canonical unsupported-action error while host state remains version 0 and no events are appended.

## Remaining WP-46 release work

Concrete Windows per-user pairing credentials, remote TLS termination, strong remote identity-provider integration and release-environment qualification remain open. No provider networking, live credentials, transfer/withdrawal path or unrestricted real-money authority is enabled.
