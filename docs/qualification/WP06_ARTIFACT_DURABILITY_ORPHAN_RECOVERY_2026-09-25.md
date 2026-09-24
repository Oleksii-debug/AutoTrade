# WP-06 — artifact publication durability and orphan-recovery hardening — 2026-09-25

Status: **IMPLEMENTATION_INCREMENT_AWAITING_EXACT_HEAD_CI**

This is a narrow hardening increment for the existing canonical research/evidence ArtifactStore. It does not create a second artifact store, financial ledger, persistence authority, or trading authority.

## Exact lineage

- Source main at branch creation: `6fec062594f767e00039c127014c44f058387534`.
- Public durable parent-directory sync: `8c2c51a99393cbd84348e64356b9481c099e6949`.
- Object/export directory durability: `bf29dec21f139d41baddbb3f09ff430cf4a95303`.
- Publication durability regression: `7bea2d6030bb17bd566d78554bcd351297c1f9e2`.
- Malformed/misplaced object audit hardening: `c8e0558c97113cf41ddf99a4396aa1ad667df495`.
- Recovery-isolation regression: `ea6f57815a6e0a81dfaf488825a956280464ac06`.
- Focused ArtifactStore test surface at this head: 9 tests.

## Implemented boundary

- durable JSON publication and ArtifactStore object/export publication use an explicit parent-directory sync primitive where supported;
- a completed object rename is followed by directory durability before manifest publication can rely on that object path;
- export rename receives the same directory durability treatment;
- artifact audit accepts only canonical lowercase SHA-256 object paths;
- malformed names, misplaced valid digests, and symlink object entries are reported as corruption evidence rather than entering the unreferenced-object deletion set;
- orphan recovery can still remove valid canonical unreferenced objects and staging leftovers without aborting on malformed object entries;
- suspicious object entries are preserved for inspection rather than blindly deleted.

## Exact-head qualification

The final PR head must pass the repository's existing verification and baseline workflows. No PASS is claimed until exact-head GitHub evidence exists.

## Deliberate unresolved limits

WP-06 is not DONE. Product-level completion still requires:

- convergence between the two currently existing artifact-store surfaces where architecture still permits both, or an explicit canonical-boundary decision;
- full rights-policy semantics for derived/redistributed artifacts across every research/export path;
- sustained multiprocess and forced-crash qualification across Windows and POSIX filesystems;
- backup/restore qualification of artifact manifests, objects and orphan-recovery evidence;
- integration of immutable artifact references into all required science, capability and release evidence;
- release-candidate proof that packaging preserves artifact integrity and rights metadata.

This change grants no live trading authority and does not establish economic edge.
