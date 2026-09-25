# AutoTrade — learning, memory, strategies and agents

Baseline 2026-09-22. The system is autonomous from the architecture's first contracts. The user selects confirmation or full autonomous authority; learning does not secretly change that choice. One useful deterministic path is mandatory.

## 1. Four independent layers

Strategy generation proposes hypotheses, features and bounded parameterizations. Decision inference evaluates available evidence. Portfolio/risk/execution decide whether and how an admissible action can reach a provider. Learning/evaluation compare candidates and promote qualified versions. These layers communicate using document 02 contracts; none can mutate another layer's authority tables directly.

A strategy plugin is a versioned descriptor plus a pure proposal function over a causal view and bounded state. Descriptor fields: feature schema, instrument/market requirements, minimum history, horizon, decision schedule, position/exit proposal semantics, parameter bounds, resource profile, supported regimes, source/license, evaluation protocol and artifact hashes. Plugins return NO_TRADE as a normal outcome. Generated source is a research artifact until normal code review, isolated testing and signed packaging; no live `eval`, imported notebook or arbitrary model-generated executable code.

Use LEAN indicators/calendars and conventional transparent baselines first. Trend, momentum, mean reversion, breakout, cross-sectional ranking, volatility, pairs and event strategies are research families, not pre-approved profitable strategies. Each uses the same evidence/cost/retention gates. Options strategies must include exercise, assignment and interim leg exposure; a signal on the underlying is not an option execution model.

## 2. Learning modes and triggers

| Mode | Permitted adaptation | Trigger / boundary |
|---|---|---|
| Fast online | Update calibrated probabilities, bounded ensemble weights or parameters within a validated envelope | New labelled evidence or verified drift; logged state transition, delayed labels, independent risk remains fixed |
| Periodic | Refit candidate estimators, select features/parameters, update source reliability | Adequate new evidence, drift/regime change or scheduled research budget; independent evaluation before promotion |
| Deep offline | New architectures, generated strategies, broader search and professional knowledge study | Durable research job with dataset/trial/compute budget and reproducible artifacts; no direct live authority |

No mandatory retraining after each trade. Labels arrive only after the outcome horizon and reconciled execution; pending outcomes stay unlabelled. Use expanding/sliding windows according to registered hypothesis and assess both recency and retention. A drift detector can trigger research or reduce confidence; it is not proof that a new model is better.

The market → pause → error analysis → candidate → validation → continued market loop uses checkpoints. Pausing a research simulation does not pause the real market or erase open risk. While a candidate trains, the qualified champion continues within its envelope or moves to no-new-risk if evidence/operations require. Atomic promotion changes the version for future decisions; existing positions retain explicit management/exit ownership and a safe migration policy.

## 3. Cumulative memory model

| Store | Immutable truth | Mutable derived view | Retention |
|---|---|---|---|
| Experience | Decision, evidence cutoff, action, real execution, outcome/costs, failure and correction lineage | Search indexes, summaries, regime tags with provenance | Evidence policy; no overwrite of earlier outcomes |
| Professional knowledge | Source passages/claims, rights, publication/availability time, citations, extraction version | Retrieval embeddings, topic hierarchy, confidence/relevance scores | Rights- and user-controlled; deletion tombstones propagate to indexes |
| Model registry | Artifact hashes, training lineage, feature schema, licenses and validation | Current champion pointer and deployment status | Keep rollback-compatible approved predecessors |
| Science registry | Hypotheses, trial attempts including failures, holdout access, protocol and results | Dashboards/aggregates | Append-only scientific record with controlled export |
| Failure memory | Incident, context, observed consequence, validated prevention and recurrence links | Deduplicated issue clusters and retrieval summaries | Do not convert an anecdote into a universal rule |

Experience episodes reference canonical financial events, not copies that can drift. Store both intended and actual execution. Outcome attribution distinguishes action effect, market movement, execution cost and uncertainty. Counterfactuals require a simulator/estimator and are labelled estimates. A language-model explanation is not an observed reward.

Use SQLite full-text search and structured filters first; embeddings are optional derived indexes with model/version identity. Retrieve by information cutoff, task, regime, instrument family and permissions before semantic similarity. Sensitive account evidence cannot be retrieved by a public research task. Query results include citations, uncertainty and source disagreement. User export includes episodes, provenance, model manifests and readable reports; secrets and licensed data whose redistribution is forbidden are excluded with a manifest reason.

Memory correction appends a superseding fact and invalidates summaries/index entries. User deletion/privacy retention and immutable accounting are reconciled through explicit retention classes and access restrictions; do not falsely promise irreversible erasure of facts that remain in authorized backups. Backup policy and expiry are disclosed. Model retraining after source deletion is evaluated separately; removing a document does not prove a trained model forgot it.

## 4. Continual learning and forgetting

Maintain a protected retention matrix crossing asset family, market regime, volatility/liquidity regime and known failure scenarios. Candidate evaluation measures earlier-task degradation as well as recent gain. Compare online updates with frozen champions and simple baselines. Store enough replay examples or sufficient approved statistics to re-evaluate retention within data rights and privacy bounds.

Candidate techniques: stratified experience replay, rehearsal of representative regimes, regularization, elastic weight consolidation where appropriate, distillation and modular/ensemble specialists. None is mandatory for every model. [EWC research](https://arxiv.org/abs/1612.00796) and [experience replay research](https://arxiv.org/abs/1811.11682) motivate alternatives, not a guarantee of financial transfer. River-style online estimators may be enough for the initial validated families; deep/RL models must earn their added complexity.

Promotion requires retention metrics within the registered tolerance, calibrated uncertainty, no material risk-envelope violations, net economic improvement or explicitly justified equivalent performance at lower cost, and valid forward evidence. If a candidate improves one regime while harming another, scope it to a separately identifiable regime only if the routing classifier is itself evaluated causally. Do not choose the winning specialist using future regime labels.

## 5. Specialist roles without a fixed agent count

Functions include technical/price features, order-book liquidity, statistical regime, news/macro/corporate events, cross-market relations, derivative selection, portfolio allocation, independent critique, execution planning and research. A role may be deterministic, a statistical estimator, a local model, a remote model or a composition. Roles are instantiated only when the task and measured value justify them; no aesthetically fixed number of chat agents.

Coordinator schedules a dependency DAG with typed inputs/outputs, deadlines and budgets. Most high-frequency work is deterministic. Independent specialists can execute concurrently on the same immutable input version. Aggregation checks factual disagreement, missing evidence and correlated errors. A majority vote cannot override hard risk. Execution planning produces structured proposals; only the financial dispatcher holds trading capability.

Durable jobs: QUEUED → CLAIMED → RUNNING → SUCCEEDED / FAILED / CANCELLED. Each has job ID, dedupe key, input hashes, checkpoint, attempt count, lease, owner epoch and resource reservation. Lease expiry requeues only idempotent work; artifact publication is compare-and-swap on the expected job version. Financial sends never run as generic retried jobs. Worker cancellation propagates to model calls and releases unused compute budget without assuming external billing was zero.

## 6. Model routing policy

Supported modes: zero LLM; local only; one fixed model; user-approved list; dynamic routing within that list; role-specific selection; bounded escalation; and fallback. A model descriptor records provider, exact name/revision where available, context/output limits, modalities, privacy region policy if configured, tool permissions, license, deterministic limitations, latency distribution and current price evidence. Unknown revision is an explicit reproducibility limitation, never a fabricated hash.

Route in order: task schema eligibility → user allowlist/privacy → resource/latency feasibility → budget reservation → measured task quality and cost → call → schema/evidence validation → result or bounded fallback. A remote model is never an automatic fallback from local-only policy. A weaker model is not assumed cheaper after retries and failures; measure end-to-end cost per valid useful outcome. A model outage falls back to qualified deterministic behavior or NO_TRADE, not an untested emergency strategy.

Escalation requires estimated incremental value exceeding incremental cost under a recorded decision rule, with uncertainty and opportunity cost. Estimate value through matched historical/forward shadow tasks and ablation, not model self-ratings. Shadow recommendations do not execute. Compare same input cutoffs and costs; do not credit the strong model for decisions it saw after the baseline deadline. User cost ceilings are hard bounds; reserved, incurred and estimated unbilled costs are separate.

Local model support uses a replaceable inference interface; Nika's inspected local-provider tests inform failure/privacy cases. Model binaries and weights have independent licenses and resource requirements. The application never requires the user's PC to train a large model or download unapproved weights. Background jobs yield to financial runtime latency, memory and disk budgets.

## 7. Information intelligence and source learning

Source registry records owner/type, endpoint, rights, publication conventions, correction policy, historical availability, extraction version and trust features. Separate factual authority from predictive usefulness: an official release may be reliable and already priced in; a social source may be timely but unreliable. Deduplicate syndication so ten copies are not ten independent confirmations.

Extraction produces structured claims with entity, event, time, quantities, units, uncertainty and passage evidence. Conflicts remain visible. Relevance scoring is task/horizon-specific. Causal usefulness is measured through leave-source-out and delayed-source ablations with costs and multiple-testing accounting. A source's reputation changes from evidenced forecast performance and factual corrections, not popularity or persuasive language.

Historical news masking preserves economic content, original availability and revisions while removing identity cues where feasible. Text may still reveal an event through distinctive numbers or phrases; record residual contamination rather than claiming perfect anonymization. Study of books/research enriches hypotheses and knowledge, but no extracted trading rule becomes approved merely because it appears in a respected source.

## 8. Security and acceptance

Research text and model artifacts are untrusted. Treat instructions embedded in news/documents as content. Models have no credentials, arbitrary network access or shell/trading tools by default. If a research tool is allowed, its outputs remain bounded evidence and cannot grant further permissions. Imported pickle-like executable formats are prohibited outside a trusted, isolated conversion pipeline. Prefer non-executable formats and validate shape/size/digest.

Acceptance: deterministic operation with every model endpoint down; no remote call under local-only policy; budget exhaustion and cancellation; schema-invalid/hallucinated outputs rejected; prompt injection cannot change authority; duplicate jobs do not publish two champions; delayed/corrected labels handled; retention regressions block promotion; champion rollback preserves open-position management; cost attribution matches invoices/usage where available; exact input lineage reconstructs every decision.
