# AutoTrade — desktop, web, accessibility and runtime

Baseline 2026-09-22. Primary environment: Windows 11, keyboard and NVDA. Desktop and web are views of one authoritative host state, not separate implementations of financial logic.

## 1. Runtime topology

AutoTrade.Host runs the ASP.NET Core API, journal writer, risk/admission and guarded LEAN integration. Account execution is serialized per account/environment; different independent accounts can process concurrently while portfolio-wide reservations remain coordinated. Python research workers run in restricted processes with bounded CPU/RAM/disk/network access and no trading credentials. The UI can restart without terminating the host or losing order state.

Local mode uses a per-user background host with a clear tray/startup status and loopback-only API by default. Windows service mode is optional and must use a credential arrangement suited to its service identity; a user's DPAPI secret cannot simply be assumed decryptable by another identity. Remote mode uses an explicitly paired always-on host with authenticated encrypted transport. The UI always names the active host, account and environment.

One active sender is a safety invariant. An expiring database lease alone cannot fence an old host from a broker that does not validate the lease. Initial deployment therefore supports a single execution host and a controlled transfer: stop/fence old execution, reconcile in-flight actions, revoke/rotate its trading credential if needed, then admit the new owner. Automatic split-brain failover is unavailable until its external fencing mechanism is proved. Backup and read-only observers need no trade authority.

## 2. API and state synchronization

| Endpoint family | Purpose |
|---|---|
| `GET /api/v1/state`, `/events?after=cursor` | Versioned snapshot and resumable event stream |
| `GET /accounts`, `/capabilities`, `/instruments` | Account/environment and capability inspection |
| `GET /portfolio`, `/risk`, `/orders`, `/decisions/{id}` | Current state and reconstructable reasons |
| `POST /commands` | Typed idempotent command with expected state version |
| `GET /operations/{id}` | Command progress and actual completion/uncertainty |
| `GET /experiments`, `/learning`, `/models`, `/jobs` | Evidence, candidates, budgets and background work |
| `POST /exports`, `GET /exports/{id}` | Authorized redacted export with rights manifest |
| `/health/live`, `/health/ready` | Process life versus readiness; no secrets or account detail publicly exposed |

Commands include CONNECT_READ, ENABLE_TRADING, SET_AUTHORITY, CONFIRM_INTENT, REVOKE_AUTHORITY, BLOCK_NEW_EXPOSURE, CANCEL_SELECTION, FLATTEN_SELECTION, START/PAUSE/CANCEL_RESEARCH, PROMOTE_APPROVED_CANDIDATE and EXPORT. Capability and permission checks occur server-side. UI disabling a button is not a security control. Accepted command, pending provider action and completed financial outcome have separate accessible statuses.

The UI preserves user focus/selection while incoming prices change. Event cursor gaps trigger snapshot refresh with a concise announcement; stale data stays labelled. Optimistic display may show a requested change, but never invent a fill or “flat” position before provider evidence. Multiple open interfaces share state versions and receive conflicts instead of overwriting each other's authority changes.

## 3. Semantic navigation

Top-level regions: Overview; Accounts and host; Opportunities and decisions; Portfolio and orders; Risk and authority; Research and replay; Learning and memory; Models and costs; History and diagnostics; Settings. Use headings, landmarks, labelled forms, standard controls and tables with headers. Do not build a visually dense exchange clone or encode essential meaning only in charts/colors.

Critical fields are selectable/copyable text: account, environment, active host, capital, exposure, P&L, risk state, market freshness, order status, protection, reasons and error evidence IDs. Tables offer search/filter, stable sort descriptions, keyboard row actions and paged reading. Avoid inaccessible virtualization that removes the user's current row. Charts have equivalent data tables and concise trend descriptions.

Notifications are event-based: material risk/state changes, fills, authority requests, data/provider failures, learning completion and budget limits. Do not speak every tick. Polite announcements do not interrupt active reading; urgent alerts are used sparingly for material danger. Users can inspect an ordered notification history. Sound is optional and never the sole signal.

## 4. Keyboard/NVDA acceptance scripts

| Workflow | Keyboard actions / observable result |
|---|---|
| First launch | Tab through language, host and account setup; NVDA announces label, role, required state and errors. Secret fields are labelled and masked. No mouse or sighted step is required inside the application. |
| Account inspection | Open Accounts, choose account, navigate capability table. NVDA reads account, environment, permission, supported/unknown capability and refresh time. |
| Research/replay | Select dataset and protocol, start, inspect progress, pause/resume/cancel. Status does not steal focus. Result tables and evidence links are reachable. |
| Confirmation | Open pending intent; read intention, instrument, quantity, estimated/max cost, risk, alternatives and expiry. Confirming a stale/changed intent returns an explained conflict. |
| Autonomous policy | Inspect/edit bounded policy, review changes, activate; NVDA announces actual active version, scope and host. The user is not required to approve each permitted trade afterward. |
| Emergency | Reach “Block new exposure” with a documented shortcut and normal navigation. NVDA distinguishes accepted request, durable block and outstanding in-flight actions. Cancel/flatten are separate actions with clear scope. |
| Disconnect | Disconnect network or host; stale status and last evidence time are announced. The UI never says cancellation/flatten completed merely because the button was pressed. |
| Portfolio/history | Select and copy a value and a decision explanation; inspect partial fill, fees, protection and correction lineage without chart dependence. |
| Update/restore | Complete signed installer/update/rollback and restore wizard with keyboard; errors explain recovery without secret exposure. |

Shortcut map is user-configurable and must avoid NVDA/Windows conflicts. Use a discoverable shortcuts dialog; exact defaults are validated on the target NVDA configuration before shipping. Focus returns to the invoking control after dialogs; destructive scope choices are explicit. Browser zoom/high contrast/text scaling are tested in addition to screen-reader operation.

Use [WCAG 2.2](https://www.w3.org/TR/WCAG22/) AA as the web acceptance baseline. Automated checks and UI Automation trees are useful but cannot certify actual NVDA experience. Test a real signed Windows build with supported NVDA versions, speech/braille-relevant names and keyboard only. [WebView2](https://learn.microsoft.com/en-us/microsoft-edge/webview2/) embedding is an implementation mechanism, not proof of accessibility. A native WPF emergency/status surface remains usable if the web view fails, and reports inability to reach the host honestly.

## 5. Security and authority

Threat boundaries: external market/news/model data; imported datasets/models/plugins; local research worker; UI session; remote network; financial host; provider credentials; update chain. Least privilege separates read-only data, trading and administrative actions. Withdrawal/external-transfer operations are absent from agent authority and never implemented as hidden adapter tools.

Loopback does not mean unauthenticated: pair desktop with the local host using a per-user protected credential/session; validate origins and protect browser-origin commands. Remote access requires TLS, strong authentication, short-lived sessions and explicit role checks. Default roles: Owner, Operator within policy, Researcher and Observer. Only Owner can grant/revoke authority or credentials. Logs store stable redacted account references, not API keys, signatures, tokens or complete sensitive prompts.

Protect local secrets under the correct Windows identity; remote deployments use a secret store and scoped retrieval. Export/backup excludes live credentials by default. Key rotation validates read identity first, re-establishes trade permissions and reconciles open obligations. Disconnecting a key does not imply positions disappeared. Data retention and model privacy preferences apply before routing/retrieval.

Dependencies and model artifacts are pinned, hashed and scanned. Signed update metadata binds version, hashes, supported schema range and minimum host compatibility. Do not execute arbitrary serialized Python objects in the financial host. Imported plugin code passes code review, isolation and normal release gates. Prompt injection in information sources cannot call tools, change policy or access secrets.

## 6. Failure response matrix

| Failure | Immediate state | Recovery / truthful user result |
|---|---|---|
| Host crash/restart | RECOVERING, block new exposure | Replay journal, reconcile orders/activities/positions and protection, then explicit readiness |
| Timeout after send | UNKNOWN, reserve risk | Query complete evidence; no blind resend |
| Market stale/book gap | Disable affected new risk | Rebuild stream/snapshot; preserve qualified native protection |
| Provider outage/auth expiry | Scope DEGRADED | Backoff/session recovery; state actual limitations and in-flight uncertainty |
| Partial fill/cancel race | Keep filled exposure and plausible remainder | Deduplicate executions and reconcile terminal remainder |
| Model outage | Qualified deterministic fallback or NO_TRADE | Preserve authority/risk; never switch privacy policy or unqualified model |
| Full disk/write failure | Reject new admission; signal urgent degraded state | Protect existing positions only through explicitly engineered emergency path; do not pretend unjournaled action is durable |
| Corrupt DB | Fail closed for new risk | Preserve evidence, restore verified snapshot plus events, reconcile provider before readiness |
| Clock jump/drift | Freeze time-sensitive admission | Use monotonic deadlines, resynchronize and invalidate expired approvals |
| Manual account action | Import external-origin event | Recompute risk; pause affected scope if policy requires |
| UI/webview crash | Host continues policy | Native status/stop or another authorized interface; reconnect snapshot |
| Two hosts claim account | Fence/reject new owner | Prove old sender stopped/credential revoked; do not rely on lease timeout alone |

Disk-full emergency design: maintain a preallocated emergency journal segment and space reserve, tested under failure. If durable logging cannot be guaranteed, default to existing provider-native protection and clear urgent notification. Any separately authorized emergency action with incomplete local logging must be reconciled from provider evidence and visibly marked; never silently fabricate a complete audit trail.

## 7. Observability, backup and support

Every financial decision links proposal, input hashes/cutoff, strategy/model versions, portfolio/risk checks, authority, admission, provider attempt, actual fills/costs and outcome. Trace IDs connect services; financial truth lives in the durable journal, not a sampled trace. Metrics cover data age/gaps, clock offset, queue age, outbox/UNKNOWN count, reconciliation lag, reservation totals, margin headroom, budget use, model deadlines and failed promotions.

Set service objectives per deployment/strategy horizon before qualification. Measure p50/p95/p99 ingest-to-decision/admission latency, recovery time and reconciliation completeness on declared hardware/load. Alert thresholds follow allowed staleness and risk; no universal millisecond promise. Recovery point objective for committed local financial records is zero under the qualified durability/storage assumptions, not a promise against total unbacked disk loss. Provider reconciliation repairs external facts but cannot recreate every lost model explanation.

Backups capture SQLite consistently using its backup mechanism, verified artifact manifests and compatible application/schema versions. Encrypt sensitive backups and test restore on a clean machine. Do not copy only the live SQLite main file while ignoring WAL consistency. Retain a pre-upgrade backup and a migration journal. Restore cannot enable trading until account/provider reconciliation and owner fencing succeed.

## 8. CI, packaging and release

PR gates: formatting/types; schemas and generated-binding drift; unit/property tests; financial and causality fixtures; component/adapter contracts; SQLite crash tests; dependency/license/secret checks; web semantics; applicable Windows tests. Nightly/qualification gates: recorded provider integration, long-running replay/paper soak, load, backup/restore, account/session failures and real NVDA scripts. Live probes are never run by untrusted PR code or exposed PR secrets.

Build Windows installer and portable diagnostics package from a clean pinned source; sign release artifacts; include SBOM, notices, hashes and compatibility manifest. Installer covers runtime/WebView2 prerequisites, per-user startup, upgrade, uninstall and data preservation choices. Web/host artifacts share contract compatibility checks. Dependency versions and model/data rights are reviewed on the exact release, not just on this architecture date.

Update flow: download/verify → quiesce new admissions → track/reconcile in-flight sends → backup → migrate → restart/reconcile → verify host/UI compatibility → restore allowed authority. Failed migration returns to a compatible binary+database backup; do not run an older binary on a newer incompatible schema. Existing provider protection remains visible throughout. A release is done only after installation, keyboard/NVDA workflows, recovery and evidence export work on the delivered artifacts.
