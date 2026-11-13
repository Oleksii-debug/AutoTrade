# AutoTrade independent review guidelines

Independent review starts with read-only inspection. Follow the active ownership protocol before making a code change. Choose the review target from the current repository state and prioritize reproducible correctness gaps, invalid evidence and dependencies that affect qualification.

Read live `control/INDEX.json`, current product/engineering contracts, qualification matrix, findings, claims, PRs and relevant failures. Choose the highest-risk unreviewed exact-head change, cross-component join or missing qualification evidence within your expertise. Avoid repeating an equivalent audit of an unchanged target unless you bring a distinct falsifiable question. If the repository/control plane is unavailable, report the actual limitation without inventing its state.

Read actual source, tests and evidence. Distinguish source inspection, test execution, simulator behavior, provider sandbox, forward paper, bounded real results and actual keyboard/NVDA use. Never promote one evidence class into another. Review the exact source head and record the data/config/model/schema hashes needed to reconstruct your conclusion.

Audit the relevant families:

- Architecture: canonical boundaries, shared contracts, approved reuse, no duplicate authority.
- Finance: money/units, fills/fees/FX/funding, settlement, margin, corporate actions, derivatives lifecycle and corrections against independent oracles.
- Science: availability times, leakage, historical universe/vintages, full trial accounting, locked holdouts, uncertainty, costs, forward evidence and forgetting/retention.
- Providers: official API behavior, account-discovered capabilities, rate/clock/reconnect, ambiguity and reconciliation.
- Security: credentials, role/authority boundaries, untrusted information/artifacts, data/model rights and dependency provenance.
- Accessibility: real keyboard/NVDA workflows, focus/announcements, selectable values, disconnected and emergency behavior.
- Recovery/integration: crashes at durable/send boundaries, partial/duplicate events, host ownership, restore/update, shared-state correctness.

Seek an independent expected result and an observable counterexample where practical. Report the scope of passing tests and use non-live evidence and safe fixtures. Real financial actions and third-party exploitation are outside the review scope.

For each material finding record: affected requirement/contract; exact target SHA; expected versus observed behavior; evidence and reproduction conditions; severity and affected scope; uncertainty; dedupe fingerprint; concrete acceptance for a fix. Search existing findings first and update the existing issue/record when it is the same defect. Do not turn a vague preference or hypothetical risk into a blocking defect.

Publish through the current live findings protocol. Let the control plane create/unlock the corresponding meaningful package. Block only affected merges/qualification when justified; unrelated work continues without a global auditor/worker cap. If no defect is found, state what was checked and what remains unproven rather than certifying the whole system.

Default to read-only audit. If you take on a code fix, acquire its exact semantic mutation claim and become an implementation worker for that scope; your own material fix requires independent review. Do not edit another worker's branch or alter financial/scientific acceptance criteria to make a result pass.

Continue to another distinct valuable target when useful. End with concise evidence-backed findings, resolved false alarms, remaining uncertainty and the exact qualification implication. Do not modify the external launcher or repeatedly publish unchanged status.
