# AutoTrade claim registry

Dedicated operational ownership branch.

Current state: **bootstrap only; concurrent mutation is not yet enabled**.

Target claim identity:
`AUTHORITY_FAMILY + SEMANTIC_KEY + MUTATION_SCOPE`.

Target protocol:
- compare-and-swap / non-force fast-forward updates;
- generation and lease;
- base source head and contract versions;
- all-or-none multi-scope claims;
- idempotent request IDs;
- stale generations cannot renew, release or merge;
- comments and labels are never ownership authority.
