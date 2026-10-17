# Optional pinned source donors

These Git links retain exact complete upstream revisions for reproducible inspection/adoption. They are **not automatically initialized**, not included in the current runtime, and not proof of full component integration.

- WhiteBIT official Python SDK: `19017a280b6d6b313a2f2b59ccdc2613818e77d2`. Root LICENSE and pyproject state MIT; README also points at Apache LICENSE.md. Retain upstream notices and audit the exact adopted subset/dependencies. Its generated client names differ from some README examples; use pinned code, not copied untested snippets.
- Accessible Trading Terminal: `d05529f3e4de9dd5b11565a9c945a74d878aee48`, GPL-3.0 source donor. Target is the manual keyboard/feedback/order-ticket subsystem, potentially replacing a complete custom module after a working compatibility trial. Preserve GPL source/notice obligations for distributed derivatives; external proprietary SDKs and binaries are not automatically covered.

Initialize only the required donor in an isolated worktree when useful. Do not run upstream trading examples, connect real credentials, download model weights or enable every upstream provider during inspection.

A separately vendored, unchanged WhiteBIT `src/whitebit/hmac_transport.py` is used by the new read-only bridge. Its exact Git blob is `7c46f60fded504d06ae1f58ebfea081fb5e69f3a`; SHA-256 `9824e4fdb6edbec6be27dd12a2b57dae9a8850e4322d3b1b150aee69b83076e0`. Source MIT notice travels beside it. Tests exercise this real signer through httpx MockTransport, not a mocked signer. Network/account binding and final Host integration remain separate tests.
