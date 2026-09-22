# AutoTrade — additional ready-code research

Addendum dated 2026-09-22. Existing documents 00–13, the product DOCX and the original ZIP are preserved. This document adds source-backed findings and narrowly supersedes earlier uncertainty where explicitly stated. At the time this addendum was written, no AutoTrade production code had been created. The repository has since been bootstrapped and neutral research primitives have begun migrating; this addendum remains evidence for the reuse decisions.

## 1. Main findings already verified

1. WhiteBIT does not require starting a C# client from scratch: `JKorf/WhiteBit.Net` is an actively maintained MIT-licensed REST/WebSocket client with spot/collateral trading methods and test fixtures. It is the first SDK to qualify for the currently missing WhiteBIT adapter. It remains transport/model reuse, not a replacement for AutoTrade authority, accounting or reconciliation.
2. The `CryptoExchange.Net` family provides common transport, rate limiting, response models and test infrastructure. Its inspected .NET 10 target aligns with the proposed financial host. Shared transport can reduce duplicated adapter work without forcing all venue semantics into one generic order model.
3. Alpaca has an official Apache-2.0 C# SDK. The latest stable release observed was 7.2.2, while the inspected main-branch project is 8.0.0-beta6. These are different adoption candidates; never call the main-branch API a verified stable package interface.
4. Existing SDK behavior must be qualified at the actual send boundary. The inspected CryptoExchange.Net base client can retry a rate-limited request, and debug logging receives request/header information. AutoTrade must prevent hidden trading retries, enforce deadlines/authority for every actual outbound attempt, and redact/disable sensitive transport logs. Reuse is valuable when these boundaries are explicitly controlled.
5. The previous LEAN hash uncertainty is now resolved: `985ef30ad3ac774218c5ac516b4cb0aa2655730f` IS a commit, dated 2026-09-18, with tree `4b163abf9fca60e731b76510b9ae6721ffff7e6c`. The earlier wording calling it a source-tree object is superseded by this verified mapping. Latest observed main is `b2a01cc15b09c1d448920f4af81c73f8b07ec7d4` with tree `cec48e907afd55523d50a6fe00c6c9d11962cb91`; the two later commits concern data-monitor report storage and future ticker year parsing. Keep the original reviewed commit as reproducible baseline, then review those deltas before updating.

## 2. Source identity and release evidence

| Repository | Inspected commit | Latest release observed | License evidence |
|---|---|---|---|
| [JKorf/CryptoExchange.Net](https://github.com/JKorf/CryptoExchange.Net) | `cecfbcba48b84ff56109c7d1c67f548ba0eabf22` | CryptoExchange.Net.12.5.1, 2026-09-01 | MIT LICENSE and csproj inspected |
| [JKorf/WhiteBit.Net](https://github.com/JKorf/WhiteBit.Net) | `5ef49daa517abe86a8e35a85256db184cffdfdb2` | WhiteBit.Net.4.4.0, 2026-08-21 | MIT LICENSE and csproj inspected |
| [alpacahq/alpaca-trade-api-csharp](https://github.com/alpacahq/alpaca-trade-api-csharp) | `42043eafdefa4fc2314ff24be8c08d533dffeeb8` | sdk7.2.2, 2026-07-09 | Apache-2.0 LICENSE and csproj inspected; main is 8.0.0-beta6 |

All three repositories showed September 2026 activity and were not archived. Maintenance is not correctness certification. Package release identity, source commit and dependency graph are recorded separately. No SDK tests or live calls have been executed in this documentation task.

## 3. Exact WhiteBIT reuse boundary

Source files inspected:

- `WhiteBit.Net/Clients/V4Api/WhiteBitRestClientV4ApiTrading.cs`: `PlaceSpotOrderAsync`, `CancelOrderAsync`, `GetOpenOrdersAsync`, `GetClosedOrdersAsync`, `GetUserTradesAsync`, `GetOrderTradesAsync`, `EditOrderAsync`, `SetKillSwitchAsync`, `GetKillSwitchStatusAsync`.
- `WhiteBit.Net/Clients/V4Api/WhiteBitRestClientV4ApiCollateralTrading.cs`: `PlaceOrderAsync`, `GetOpenPositionsAsync`, `GetPositionHistoryAsync`, `GetOpenConditionalOrdersAsync`, OCO and conditional cancellation methods.
- `WhiteBit.Net.UnitTests/RestRequestTests.cs`: request/response fixture validation for account, exchange data, trading and collateral operations.
- `WhiteBit.Net/WhiteBit.Net.csproj`: dependency on CryptoExchange.Net 12.5.0 observed. The later base release 12.5.1 reports fixes relevant to authenticated caching and asynchronous subscription/book startup. Qualify a coherent resolved dependency set; do not assume a direct SDK version pins the safest transitive version automatically.

Destination remains `src/AutoTrade.Providers.WhiteBIT/`. Map typed SDK responses into document-02 contracts. Supply the durable AutoTrade client order ID; never allow a per-retry generated ID to replace it. Explicitly separate request acknowledgement, unique executions and final remainder state. Preserve raw evidence only through redacted, rights-aware capture. Restrict the adapter-facing interface to approved reads/trading; the SDK also contains account-management/withdrawal methods that must not be exposed to strategy/model workers.

Required qualification: spot/collateral units and account mode; request serialization; client-ID query/reconciliation; partial fill and cancelled remainder; reduce-only behavior; pagination/history lag; stream gaps; deadlines; cancellation; retry policy; credential-safe logs; environment separation. Existing upstream fixtures shorten this work but do not replace AutoTrade financial invariants or official-account capability checks.

## 4. CryptoExchange.Net source implications

`CryptoExchange.Net/Clients/RestApiClient.cs` contains `SendAsync<T>` and `ShouldRetryRequestAsync<T>`. The inspected implementation retries at most once under specific server-rate-limit/limiter conditions. This is not evidence that it blindly retries every timeout. Nevertheless, automatic retry is a separate actual outbound send: AutoTrade must either prevent it for financial mutations or re-authorize/record every attempt using a qualified transport seam. A rate-limit response does not grant a permanent authority exemption.

`CryptoExchange.Net.UnitTests/RateLimitTests.cs` includes per-key, endpoint, host, connection, cancellation, reset and group scenarios. Reuse the test ideas and harness where the license permits, then add AutoTrade's reserved quota for reconciliation/protection and cross-instance/account cases. SDK-local/static rate gates are not automatically a distributed account-wide quota service.

The project explicitly targets netstandard2.0/2.1 and net8/9/10. Inspected dependencies include Microsoft.Extensions logging/HTTP/configuration, System.Text.Json, System.Threading.Channels and NSec.Cryptography for applicable targets. Resolve one compatible .NET graph with LEAN; do not let different SDKs float to incompatible base versions. Do not select the aggregate all-exchange package merely for convenience when four individual clients give a smaller dependency/support surface.

## 5. Official Alpaca SDK reuse boundary

Inspected `Alpaca.Markets/AlpacaTradingClient.Orders.cs` supplies order listing, query by client/server identifier and cancellations. `Alpaca.Markets.Tests/AlpacaTradingClientTest.Orders.cs` has creation/query/patch/cancel and validation tests. The project includes options-data, trading, activity and streaming tests; their presence is an inventory observation, not a claim that all bodies were reviewed or tests passed.

Prefer the official SDK behind the existing LEAN brokerage route when its qualified implementation already uses it; otherwise compare a thin canonical adapter built on the SDK against extending that route. There must still be one order submission/reconciliation owner. Stable 7.2.2 release notes include fixes to non-marginable buying-power mapping and account/config deserialization for changing provider fields; these are concrete reasons to test financial field completeness, not only successful HTTP status.

Before adopting 8.x beta APIs, establish a required capability absent from the stable package, pin the exact revision, read the migration/dependency diff and run the applicable contract suite. If stable 7.2.2 meets the requirement, start that qualification path. Do not choose a beta solely because its main-branch target says .NET 10; compatible .NET Standard libraries can run on the newer host.

## 6. Closed first-party research gaps

The full Autosport `tests/test_replay_jsonl_integrity.py` was now read at the original audit commit. It explicitly tests duplicate top-level/nested keys, nonstandard constants, overflowing numbers, more-than-640-digit integers, invalid UTF-8, lone surrogates, non-object records, physical line reporting and unreadable paths. This strengthens A1's characterization basis without claiming those tests were executed or importing the sports ReplayEngine.

The full Nika `src/nika_core/model_gateway/contracts.py` was now read at `2f7be3389109d7dd6fb3bae40540fe0cf2eba695`. Useful portable semantics include `ModelFailureEffect.UNKNOWN/NO_EFFECT`, `ProviderCapabilities.supports_hard_cancellation` defaulting false, `ModelDownloadAuthorization` separate from inference, immutable/canonical `ModelRequest` validation and `ModelResourcePolicy`. These inform AutoTrade's existing model contracts. The Python representations are not a reason to create a second .NET/Python authority model; import only a narrow research-side subset if rights and integration cost justify it. The previous missing-license qualification remains unchanged.

## 7. Evidence scope

Primary evidence is the exact repository source, license, project file and release record named above. This follow-up has read source and tests selectively; it has not compiled dependencies, run upstream suites, contacted live brokers, imported code into AutoTrade or validated profitability. Further component dossiers and the concrete adoption sequence are added as this follow-up completes.
