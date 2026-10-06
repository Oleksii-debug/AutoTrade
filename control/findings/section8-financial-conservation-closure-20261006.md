# Section 8 closure — exact financial conservation and P&L

Date: 2026-10-06

## Canonical integration

Current-main settled-capital successor #2275 was merged with exact accepted head
`eb4988d30344b1c86a95cf64867f568a33f75f21`.

Merge commit:
`11da6a62888bb832546a0063ebcbbada0fed7042`

Candidate tree and post-merge tree are byte-identical:
`93f27dc04797a5351a2d65d2e9c9126c70720705`.

## Result

Section 8 core economic-ledger / conservation semantics are closed on main.

Canonical accounting and settlement now provide:
- bounded context-independent exact arithmetic for authoritative ledger values;
- balanced multi-asset/currency postings;
- exact cash and position projection;
- exact quantity x price and fee accounting;
- deterministic FIFO basis plus realized/unrealized P&L;
- exact reversal/correction and duplicate/double-reversal protection;
- trade-date settlement obligations separate from spendable settled CASH;
- SELL receivables unavailable until authenticated settlement evidence;
- BUY payables represented explicitly;
- provider settlement release exactly once across restart;
- typed MARGIN_CREDIT separate from legal settled cash;
- actual admission/dispatch-time intersection of provider and local capital;
- versioned settlement-rule authority;
- overdue/missing settlement evidence remains UNKNOWN and blocks unsafe capital reuse;
- FX cash legs use the same settlement-availability semantics;
- bust/correction before or after settlement cannot double-release capital.

The accepted #2275 regressions include 59 settlement tests, 31 authority/capital-availability tests and 47 reconciliation-journal tests, including explicit hostile/polymorphic ingress and ambient-Decimal-context falsifiers.

## Boundary

Reservation-capacity lifecycle belongs to WP-15/Section 12.
Asset-specific futures/perpetual/options/corporate lifecycle economics remain their own sections/WPs.
Hard-risk arithmetic and provider qualification remain separate authorities.

Issue #1076 remains an umbrella for those broader financial/risk consumers; its original core accounting and FX ambient-context defects are already materially integrated and therefore do not keep Section 8 open.

Hosted exact-head jobs for #2275 were queued when merged and are not represented as PASS. Source integration and post-merge tree equality are recorded exactly.

No provider/PAPER/LIVE, release, profitability, economic-edge or NVDA qualification follows from this closure.
