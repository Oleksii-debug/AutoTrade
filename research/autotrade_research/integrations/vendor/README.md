# Unchanged WhiteBIT signing component

`whitebit_hmac_transport.py` is copied byte-for-byte from WhiteBIT's official Python SDK at commit `19017a280b6d6b313a2f2b59ccdc2613818e77d2`, path `src/whitebit/hmac_transport.py`. Git blob `7c46f60fded504d06ae1f58ebfea081fb5e69f3a`; SHA-256 `9824e4fdb6edbec6be27dd12a2b57dae9a8850e4322d3b1b150aee69b83076e0`; 4985 bytes. This is actual third-party implementation reuse, not a mocked or reimplemented signer.

Original MIT notice: WHITEBIT_LICENSE, copied from the same revision's root LICENSE. The repository's pyproject also states MIT; its README additionally points at Apache LICENSE.md. Keep that upstream record during broader SDK adoption and preserve component-specific notices.

Upstream transport signs any matching POST path; it is not a security boundary. AutoTrade wraps it with exact read-only host/path/body gates. Never hand it directly to an LLM or a generic arbitrary-request tool. It also does not close its wrapped transport; our reader explicitly owns cleanup. The current reader only exposes main-balance bytes, not orders, not withdrawals, not account-binding proof.
