# WP-52 inert export boundary foundation — 2026-09-24

Status: **implementation foundation only; WP-52 remains unqualified**.

This slice adds a host-facing safety primitive for exporting structured
evidence without creating another authority or artifact store. It deliberately
does not modify the active credential-vault, host-state or model-routing
lineages.

## Guarantees implemented

- output format is fixed to inert UTF-8 JSON; arbitrary bytes, pickle/model
  objects, HTML and executable extensions are rejected;
- export requires explicit `rights.export=true` and a non-empty rights ID;
- nested credential/authorization/token/signature fields are redacted before
  serialized bytes are produced;
- binary floating-point is rejected; exact Decimal values are rendered as
  strings;
- Windows-reserved names, traversal and path separators are rejected;
- depth, item-count and byte budgets fail closed;
- payload strings are treated only as data, including prompt-injection text;
- each prepared export carries SHA-256, byte count, rights identity and source
  references, and is reverified before atomic publication.

## Deliberate limits

This is not full WP-52 qualification. It does not claim that arbitrary natural
language can be inspected to discover every possible secret. Upstream host
projections must not place raw credentials into generic fields. Production
authorization still belongs to the authenticated host/security authority;
this helper does not grant EXPORT permission by itself.

The remaining package requires end-to-end role enforcement after the canonical
host-command and persistent credential-vault lineages converge, isolated
research/model execution tests, cross-role retrieval tests, and exact release
evidence. No trading authority or network capability is added here.
