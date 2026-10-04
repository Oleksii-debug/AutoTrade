# First-party reuse: Nika Core → AutoTrade contract semantics

Source snapshot: `Oleksii-debug/Nika-Core@2f7be3389109d7dd6fb3bae40540fe0cf2eba695`.
Inspected source: `src/nika_core/model_gateway/contracts.py` (blob `bafcf6cbb07d5b511979b09d1d920972a00da2c2`).

AutoTrade does **not** import Nika Core as a runtime dependency. Portable semantics incorporated into the canonical language-neutral contract tranche are:

- explicit `UNKNOWN` versus positive `NO_EFFECT` model failure effect;
- provider hard-cancellation capability fails closed;
- model-download authorization is separate from ordinary inference;
- model resource policy is explicit;
- model requests/responses carry exact identity, privacy/budget/deadline/evidence semantics.

The AutoTrade schema follows the approved AutoTrade document-02 contract rather than copying Nika's Python dataclasses verbatim.

Rights/distribution remain subject to WP-03 exact first-party provenance clearance. This record proves source identity and the adaptation boundary; it is not a blanket license conclusion.
