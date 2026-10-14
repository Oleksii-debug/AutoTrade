# Провайдерський AutoTrade — канонічний зовнішній підпроєкт

**Спільний репозиторій:** `Oleksii-debug/AutoTrade`; shared code not copied.
**Google Drive:** https://drive.google.com/drive/folders/1Mi2zwLjp9KT-WB5pxM9OrZ4grxRyiSk5
**Канонічний план:** https://docs.google.com/document/d/1sbCFL7NeCK5AfUvZFkGJ2FbOGBAAfHqPcj5-99j9qFw/edit

## Own scope

Plan 9 / **30 atomic sections**: external rights and security provenance for signed release, provider/account/capability mapping, per-venue account feeds, broker instruction signing/transport, reconciliation to actual settlement, optional PAPER/LIVE campaigns, external controls, delivered immutable signed package and physical Windows/NVDA assessment.

Plan 9 includes owner-controlled final external release qualification that is *not* inherently a broker account feature. Keep these final gates isolated from simulation development and internal unsigned Windows builds; do not falsify them or silently remove them.

## Parked until facts and authorization

Owner has **not** supplied provider credentials, broker account or real-money mandate. All external provider/PAPER/LIVE rows remain `PARKED/NOT_ACTIVATED/NOT_CLAIMED` where applicable; source-engineering and contract work that requires no external fact remains actionable. No real order may be sent without explicit additional owner authorization, risk/kill/reconcile proof and the required exact evidence.

## Same agent, different environment

Provider mode uses *the same agent code, versioned strategy/model checkpoints, learned memory, financial contracts, host/UI, canonical ledger, OMS and risk controls* as Simulation AutoTrade. Provider mode adds exchange/feed/execution adapters and verified broker truth; it does not create a competing learner, financial owner, routing sender or strategy engine. Frozen artifact promotion requires [MODEL_PROMOTION_CONTRACT.md](../MODEL_PROMOTION_CONTRACT.md). The same code and weights do not guarantee matching fills or profitability.

Each terminal claim must be supported by the correct real external evidence class, exact-source/delivered artifact identity, CI, authorized `main` merge, readback and updated plan state. Do not use simulations to substitute for physical/real-provider evidence.
