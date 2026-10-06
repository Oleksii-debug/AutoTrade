# Section 9 settled / actually available capital closure — 2026-10-06

## Canonical result

Section 9 provider-free settled/available-capital authority is closed on `main`.

- Current closure base: `ef86be4d0264925f70ce7cff0e15a56f1b9edbcd`
- Canonical current-main financial convergence: PR #2275
- Accepted #2275 head: `eb4988d30344b1c86a95cf64867f568a33f75f21`
- #2275 merge commit: `11da6a62888bb832546a0063ebcbbada0fed7042`
- Issue #723: CLOSED / completed
- Historical closure PR #1620 is superseded by the stronger current-main result.

## Closed authority

The accepted product state now preserves these distinctions and fail-closed rules:

1. Trade-date economic cash obligations are not silently equivalent to settled spendable cash.
2. Unsettled receivables do not increase available CASH before canonical settlement evidence.
3. Settlement completion is versioned/evidenced, durable and exactly-once across restart.
4. Provider availability is intersected with local settled/spendable capital at the actual admission cut.
5. Final dispatch revalidates current capital authority.
6. Typed `MARGIN_CREDIT:<currency>` remains a separate resource from `CASH:<currency>`; credit never inflates cash.
7. Provider/account/runtime/provider-domain scope and evidence identity remain bound to capital observations.
8. Missing, stale, late, contradictory or UNKNOWN settlement/capital evidence blocks unsafe reuse.
9. Corrections/busts retain explicit conservation and cannot double-release capital.
10. Provider-free partial-fill/restart continuation retains these semantics.

## Qualification boundary

This closure is provider-free financial source/integration authority. It does **not** claim:
- real PAPER/LIVE provider reconciliation or account qualification;
- universal reservation/hard-risk completion;
- provider qualification;
- profitability/economic edge;
- signed release or physical Windows/NVDA qualification.

Hosted queued/pending/cancelled checks are not represented as PASS. Those boundaries remain separately owned and do not reopen the closed Section-9 source/integration authority.
