using System.Net;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using AutoTrade.Desktop;

namespace DesktopClientContracts;

internal static class Program
{
    static readonly Uri HostOrigin = new("http://127.0.0.1:8765/");

    static EmergencyHostSession PairedSession(
        string token,
        string actor = "owner",
        Uri? origin = null) =>
        new(actor, token, origin ?? HostOrigin);

    static HttpResponseMessage Json(HttpStatusCode status, object value) =>
        new(status)
        {
            Content = new StringContent(
                JsonSerializer.Serialize(value),
                Encoding.UTF8,
                "application/json"),
        };
    
    static string NowUtc() =>
        DateTimeOffset.UtcNow.AddSeconds(-1).UtcDateTime.ToString(
            "yyyy-MM-dd'T'HH:mm:ss.fffffff'Z'",
            System.Globalization.CultureInfo.InvariantCulture);
    
    static object Snapshot(
        string token,
        string version = "0",
        string hostFreshness = "CURRENT",
        string? freshnessAsOf = null)
    {
        string serverTime = NowUtc();
        return new
        {
            state_version = version,
            event_cursor = version,
            server_time = serverTime,
            host_id = "host-local-1",
            account_id = "paper-account-1",
            environment = "PAPER",
            permission_summary = new
            {
                actor = "owner",
                role = "OWNER",
                session = AuthenticatedEmergencyHostClient.PublicSessionReference(token),
            },
            connection_freshness = new
            {
                host = hostFreshness,
                as_of = freshnessAsOf ?? serverTime,
            },
            portfolio = new { },
            risk = new { },
            strategy = new { },
            jobs = Array.Empty<object>(),
            reason_codes = Array.Empty<string>(),
        };
    }
    
    static void AssertAuth(HttpRequestMessage request, string token)
    {
        Check.True(
            request.Headers.Authorization?.Scheme == "AutoTrade-Session",
            "request must use AutoTrade-Session authentication scheme");
        Check.True(
            request.Headers.Authorization?.Parameter == token,
            "request session token changed");
        Check.True(
            request.Headers.TryGetValues("X-AutoTrade-Actor", out IEnumerable<string>? actors)
                && actors.Single() == "owner",
            "request actor header is missing or changed");
    }
    
    static void CredentialTargetIsOriginBoundTest()
    {
        string target =
            WindowsCredentialManagerSessionProvider.CredentialTargetForOrigin(
                HostOrigin);
        Check.True(
            target == "AutoTrade.HostSession:http://127.0.0.1:8765",
            "credential target is not a deterministic canonical origin binding");

        _ = new WindowsCredentialManagerSessionProvider(target, HostOrigin);
        Check.Throws<ArgumentException>(
            () => _ = new WindowsCredentialManagerSessionProvider(
                target,
                new Uri("http://127.0.0.1:8766/")),
            "credential target was reusable across a different host origin");
    }

    static async Task PairedOriginMismatchFailsBeforeTransportTest()
    {
        const string token = "origin-bound-session-token";
        int transportCalls = 0;
        MutableSessionProvider sessions =
            new(PairedSession(
                token,
                origin: new Uri("http://127.0.0.1:8766/")));
        MemoryPendingCommandStore pendingStore = new();
        DelegateHandler handler = new((request, _, _) =>
        {
            transportCalls++;
            Check.True(
                request.Headers.Authorization?.Parameter != token,
                "mismatched-origin bearer reached the HTTP transport");
            throw new InvalidOperationException(
                "origin mismatch must fail before HTTP transport");
        });
        AuthenticatedEmergencyHostClient client = new(
            new HttpClient(handler),
            HostOrigin,
            sessions,
            pendingStore);

        await Check.ThrowsAsync<InvalidOperationException>(
            () => client.GetStatusAsync(CancellationToken.None),
            "mismatched paired origin did not block authenticated status request");
        await Check.ThrowsAsync<InvalidOperationException>(
            () => client.BlockNewExposureAsync(CancellationToken.None),
            "mismatched paired origin did not block emergency command");
        Check.True(
            transportCalls == 0,
            "mismatched paired origin caused an HTTP call");
        Check.True(
            pendingStore.Payload is null,
            "origin mismatch persisted a command before authority was established");
    }

    static async Task CanonicalStatusAndOperationTest()
    {
        const string token = "session-token-a";
        MutableSessionProvider sessions =
            new(PairedSession(token));
        DelegateHandler handler = new(async (request, _, cancellationToken) =>
        {
            AssertAuth(request, token);
            if (request.Method == HttpMethod.Get
                && request.RequestUri!.AbsolutePath == "/api/v1/state")
            {
                return Json(HttpStatusCode.OK, Snapshot(token, "7"));
            }
    
            if (request.Method == HttpMethod.Get
                && request.RequestUri!.AbsolutePath.StartsWith(
                    "/api/v1/operations/",
                    StringComparison.Ordinal))
            {
                string operationId = request.RequestUri.AbsolutePath.Split('/').Last();
                return Json(
                    HttpStatusCode.OK,
                    new
                    {
                        operation_id = operationId,
                        phase = "SUCCEEDED",
                        started_at = NowUtc(),
                        updated_at = NowUtc(),
                        affected_refs = Array.Empty<string>(),
                        evidence = Array.Empty<object>(),
                        remaining_uncertainty = Array.Empty<string>(),
                    });
            }
    
            throw new InvalidOperationException("unexpected request " + request.RequestUri);
        });
        AuthenticatedEmergencyHostClient client = new(
            new HttpClient(handler),
            new Uri("http://127.0.0.1:8765/"),
            sessions);
    
        EmergencyHostStatus status = await client.GetStatusAsync(CancellationToken.None);
        Check.True(status.Connected, "authenticated snapshot must be connected");
        Check.True(status.IsCurrent, "CURRENT host freshness was not preserved");
        Check.True(status.HostId == "host-local-1", "host identity changed");
        Check.True(status.AccountId == "paper-account-1", "account identity changed");
        Check.True(status.Environment == "PAPER", "environment identity changed");
        Check.True(status.StateVersion == "7", "state version changed");
    
        const string op = "22222222-2222-2222-2222-222222222222";
        EmergencyOperationStatus operation =
            await client.GetOperationAsync(op, CancellationToken.None);
        Check.True(
            operation.State == EmergencyOperationState.Succeeded,
            "SUCCEEDED phase was not preserved");
        Check.True(operation.DurableBlockConfirmed, "succeeded block must be durable");
        Check.True(
            operation.InFlightActions == InFlightActionState.Unknown,
            "operation success must not fabricate provider in-flight absence");
    }
    
    static void StaleSuccessorMayCarryOlderEvidenceTimeTest()
    {
        EmergencyHostStatus current = new(
            Connected: true,
            HostId: "host-local-1",
            AccountId: "paper-account-1",
            Environment: "PAPER",
            StateVersion: "7",
            ObservedAtUtc: DateTimeOffset.Parse(
                "2026-09-25T09:30:00Z",
                System.Globalization.CultureInfo.InvariantCulture,
                System.Globalization.DateTimeStyles.AssumeUniversal
                    | System.Globalization.DateTimeStyles.AdjustToUniversal),
            Message: "current")
        {
            IsCurrent = true,
        };
        EmergencyHostStatus stale = new(
            Connected: true,
            HostId: "host-local-1",
            AccountId: "paper-account-1",
            Environment: "PAPER",
            StateVersion: "8",
            ObservedAtUtc: DateTimeOffset.Parse(
                "2026-09-25T09:29:00Z",
                System.Globalization.CultureInfo.InvariantCulture,
                System.Globalization.DateTimeStyles.AssumeUniversal
                    | System.Globalization.DateTimeStyles.AdjustToUniversal),
            Message: "stale")
        {
            IsCurrent = false,
        };

        EmergencyHostStatus accepted = stale.ValidateStaleSuccessorOf(current);
        Check.True(
            ReferenceEquals(accepted, stale),
            "stale successor should preserve the validated observation");

        bool normalSuccessorRejected = false;
        try
        {
            stale.ValidateSuccessorOf(current);
        }
        catch (InvalidOperationException)
        {
            normalSuccessorRejected = true;
        }
        Check.True(
            normalSuccessorRejected,
            "current-evidence successor validation must still reject evidence-time regression");
    }

    static void StaleSuccessorRejectsDurableRegressionAndIdentityChangeTest()
    {
        DateTimeOffset observed = DateTimeOffset.Parse(
            "2026-09-25T09:29:00Z",
            System.Globalization.CultureInfo.InvariantCulture,
            System.Globalization.DateTimeStyles.AssumeUniversal
                | System.Globalization.DateTimeStyles.AdjustToUniversal);
        EmergencyHostStatus staleNine = new(
            Connected: true,
            HostId: "host-local-1",
            AccountId: "paper-account-1",
            Environment: "PAPER",
            StateVersion: "9",
            ObservedAtUtc: observed,
            Message: "stale-nine")
        {
            IsCurrent = false,
        };

        EmergencyHostStatus regressed = staleNine with
        {
            StateVersion = "8",
            ObservedAtUtc = observed.AddMinutes(-1),
            Message = "stale-eight",
        };
        bool versionRejected = false;
        try
        {
            regressed.ValidateStaleSuccessorOf(staleNine);
        }
        catch (InvalidOperationException)
        {
            versionRejected = true;
        }
        Check.True(
            versionRejected,
            "stale-to-stale succession must reject durable state-version regression");

        EmergencyHostStatus changedIdentity = staleNine with
        {
            HostId = "host-other",
            StateVersion = "10",
            ObservedAtUtc = observed.AddMinutes(-2),
            Message = "stale-other-host",
        };
        bool identityRejected = false;
        try
        {
            changedIdentity.ValidateStaleSuccessorOf(staleNine);
        }
        catch (InvalidOperationException)
        {
            identityRejected = true;
        }
        Check.True(
            identityRejected,
            "stale-to-stale succession must reject silent host authority identity change");
    }

    static async Task NonCurrentFreshnessRemainsExplicitTest()
    {
        const string token = "session-token-stale";
        const string freshnessAsOf = "2026-09-25T09:29:00Z";
        MutableSessionProvider sessions =
            new(PairedSession(token));
        DelegateHandler handler = new((request, _, _) =>
        {
            AssertAuth(request, token);
            if (request.Method == HttpMethod.Get
                && request.RequestUri!.AbsolutePath == "/api/v1/state")
            {
                return Task.FromResult(
                    Json(
                        HttpStatusCode.OK,
                        Snapshot(
                            token,
                            "8",
                            hostFreshness: "STALE",
                            freshnessAsOf: freshnessAsOf)));
            }

            throw new InvalidOperationException(
                "stale-status test issued an unexpected request");
        });
        AuthenticatedEmergencyHostClient client = new(
            new HttpClient(handler),
            HostOrigin,
            sessions);

        EmergencyHostStatus status =
            await client.GetStatusAsync(CancellationToken.None);
        Check.True(status.Connected, "stale authenticated snapshot lost host reachability");
        Check.True(!status.IsCurrent, "STALE host freshness was fabricated as CURRENT");
        Check.True(
            status.StateVersion == "8",
            "stale snapshot lost its durable state version");
        Check.True(
            status.ObservedAtUtc
                == DateTimeOffset.Parse(
                    freshnessAsOf,
                    System.Globalization.CultureInfo.InvariantCulture,
                    System.Globalization.DateTimeStyles.AssumeUniversal
                        | System.Globalization.DateTimeStyles.AdjustToUniversal),
            "native last-evidence time did not use connection_freshness.as_of");
        Check.True(
            status.Message.Contains("STALE", StringComparison.Ordinal),
            "non-current host freshness was not surfaced in status text");
    }

    static async Task StaleFreshnessDoesNotDisableEmergencyBlockTest()
    {
        const string token = "session-token-stale-emergency";
        const string freshnessAsOf = "2026-09-25T09:29:00Z";
        const string operationId = "23232323-2323-2323-2323-232323232323";
        MutableSessionProvider sessions =
            new(PairedSession(token));
        int posts = 0;
        DelegateHandler handler = new(async (request, _, cancellationToken) =>
        {
            AssertAuth(request, token);
            if (request.Method == HttpMethod.Get
                && request.RequestUri!.AbsolutePath == "/api/v1/state")
            {
                return Json(
                    HttpStatusCode.OK,
                    Snapshot(
                        token,
                        "0",
                        hostFreshness: "STALE",
                        freshnessAsOf: freshnessAsOf));
            }

            if (request.Method == HttpMethod.Post
                && request.RequestUri!.AbsolutePath == "/api/v1/commands")
            {
                posts++;
                string body =
                    await request.Content!.ReadAsStringAsync(cancellationToken);
                using JsonDocument parsed = JsonDocument.Parse(body);
                return Json(
                    HttpStatusCode.OK,
                    new
                    {
                        command_id =
                            parsed.RootElement.GetProperty("command_id").GetString(),
                        status = "ACCEPTED",
                        state_version = "1",
                        reason_codes = Array.Empty<string>(),
                        field_errors = Array.Empty<object>(),
                        operation_id = operationId,
                    });
            }

            if (request.Method == HttpMethod.Get
                && request.RequestUri!.AbsolutePath
                    == "/api/v1/operations/" + operationId)
            {
                return Json(
                    HttpStatusCode.OK,
                    new
                    {
                        operation_id = operationId,
                        phase = "SUCCEEDED",
                        started_at = NowUtc(),
                        updated_at = NowUtc(),
                        affected_refs = Array.Empty<string>(),
                        evidence = Array.Empty<object>(),
                        remaining_uncertainty = Array.Empty<string>(),
                    });
            }

            throw new InvalidOperationException(
                "stale-emergency test issued an unexpected request");
        });
        AuthenticatedEmergencyHostClient client = new(
            new HttpClient(handler),
            HostOrigin,
            sessions);

        EmergencyCommandResult result =
            await client.BlockNewExposureAsync(CancellationToken.None);
        Check.True(
            result.Accepted && result.DurableBlockConfirmed,
            "stale display freshness incorrectly disabled the risk-reducing emergency block");
        Check.True(posts == 1, "emergency block was not submitted exactly once");
    }

    static async Task AmbiguousPostExactRetryTest()
    {
        const string token = "session-token-b";
        MutableSessionProvider sessions =
            new(PairedSession(token));
        List<string> commandBodies = [];
        int postCount = 0;
        const string operationId = "33333333-3333-3333-3333-333333333333";
    
        DelegateHandler handler = new(async (request, _, cancellationToken) =>
        {
            AssertAuth(request, token);
            if (request.Method == HttpMethod.Get
                && request.RequestUri!.AbsolutePath == "/api/v1/state")
            {
                return Json(HttpStatusCode.OK, Snapshot(token));
            }
    
            if (request.Method == HttpMethod.Post
                && request.RequestUri!.AbsolutePath == "/api/v1/commands")
            {
                postCount++;
                string body = await request.Content!.ReadAsStringAsync(cancellationToken);
                commandBodies.Add(body);
                if (postCount == 1)
                {
                    throw new HttpRequestException("response lost after durable commit");
                }
    
                using JsonDocument parsed = JsonDocument.Parse(body);
                string commandId =
                    parsed.RootElement.GetProperty("command_id").GetString()!;
                return Json(
                    HttpStatusCode.OK,
                    new
                    {
                        command_id = commandId,
                        status = "ACCEPTED",
                        state_version = "1",
                        reason_codes = Array.Empty<string>(),
                        field_errors = Array.Empty<object>(),
                        operation_id = operationId,
                    });
            }
    
            if (request.Method == HttpMethod.Get
                && request.RequestUri!.AbsolutePath
                    == "/api/v1/operations/" + operationId)
            {
                return Json(
                    HttpStatusCode.OK,
                    new
                    {
                        operation_id = operationId,
                        phase = "QUEUED",
                        started_at = NowUtc(),
                        updated_at = NowUtc(),
                        affected_refs = Array.Empty<string>(),
                        evidence = Array.Empty<object>(),
                        remaining_uncertainty = new[]
                        {
                            "financial_outcome_not_completed",
                        },
                    });
            }
    
            throw new InvalidOperationException("unexpected request " + request.RequestUri);
        });
    
        AuthenticatedEmergencyHostClient client = new(
            new HttpClient(handler),
            new Uri("http://127.0.0.1:8765/"),
            sessions);
    
        string? uncertainCommandId = null;
        try
        {
            await client.BlockNewExposureAsync(CancellationToken.None);
            throw new InvalidOperationException("first lost response must remain uncertain");
        }
        catch (EmergencyCommandUncertainException error)
        {
            uncertainCommandId = error.CommandId;
        }
    
        EmergencyCommandResult recovered =
            await client.BlockNewExposureAsync(CancellationToken.None);
        Check.True(recovered.Accepted, "exact retry must recover durable acceptance");
        Check.True(
            recovered.OperationId == operationId,
            "exact retry recovered the wrong operation");
        Check.True(postCount == 2, "expected exactly one original send and one exact recovery send");
        Check.True(
            commandBodies.Count == 2 && commandBodies[0] == commandBodies[1],
            "recovery changed command/idempotency/scope/session payload");
    
        using JsonDocument body = JsonDocument.Parse(commandBodies[0]);
        Check.True(
            body.RootElement.GetProperty("command_id").GetString() == uncertainCommandId,
            "surfaced uncertain command identity differs from retried command");
        string? publicSession =
            body.RootElement.GetProperty("session").GetString();
        Check.True(
            publicSession == AuthenticatedEmergencyHostClient.PublicSessionReference(token),
            "command did not bind the canonical public session reference");
        Check.True(
            publicSession != token
                && !commandBodies[0].Contains(token, StringComparison.Ordinal),
            "command payload leaked the reusable bearer credential");
    }
    
    static async Task UncertainCommandCannotRetargetSessionTest()
    {
        const string originalToken = "session-token-c";
        MutableSessionProvider sessions =
            new(PairedSession(originalToken));
        int posts = 0;
    
        DelegateHandler handler = new(async (request, _, cancellationToken) =>
        {
            if (request.Method == HttpMethod.Get)
            {
                AssertAuth(request, originalToken);
                return Json(HttpStatusCode.OK, Snapshot(originalToken));
            }
    
            posts++;
            await request.Content!.ReadAsStringAsync(cancellationToken);
            throw new HttpRequestException("response unknown");
        });
    
        AuthenticatedEmergencyHostClient client = new(
            new HttpClient(handler),
            new Uri("http://127.0.0.1:8765/"),
            sessions);
    
        await Check.ThrowsAsync<EmergencyCommandUncertainException>(
            () => client.BlockNewExposureAsync(CancellationToken.None),
            "first ambiguous send must be uncertain");
        Check.True(posts == 1, "first command was not sent exactly once");
    
        sessions.Session = PairedSession("different-session-token");
        await Check.ThrowsAsync<EmergencyCommandUncertainException>(
            () => client.BlockNewExposureAsync(CancellationToken.None),
            "changed session must not retarget unresolved command");
        Check.True(
            posts == 1,
            "client sent unresolved command with a different session instead of failing closed");
    }
    
    static async Task UncertainCommandSurvivesDesktopRestartTest()
    {
        const string token = "session-token-restart";
        MutableSessionProvider sessions =
            new(PairedSession(token));
        MemoryPendingCommandStore pendingStore = new();
        List<string> commandBodies = [];
        int posts = 0;
        int stateReads = 0;
        int operationReads = 0;
        const string operationId = "44444444-4444-4444-4444-444444444444";

        DelegateHandler handler = new(async (request, _, cancellationToken) =>
        {
            AssertAuth(request, token);
            if (request.Method == HttpMethod.Get
                && request.RequestUri!.AbsolutePath == "/api/v1/state")
            {
                stateReads++;
                return Json(HttpStatusCode.OK, Snapshot(token, "11"));
            }

            if (request.Method == HttpMethod.Post
                && request.RequestUri!.AbsolutePath == "/api/v1/commands")
            {
                posts++;
                string body = await request.Content!.ReadAsStringAsync(cancellationToken);
                commandBodies.Add(body);
                if (posts == 1)
                {
                    throw new HttpRequestException(
                        "response lost after durable host acceptance");
                }

                using JsonDocument parsed = JsonDocument.Parse(body);
                return Json(
                    HttpStatusCode.OK,
                    new
                    {
                        command_id =
                            parsed.RootElement.GetProperty("command_id").GetString(),
                        status = "ACCEPTED",
                        state_version = "12",
                        reason_codes = Array.Empty<string>(),
                        field_errors = Array.Empty<object>(),
                        operation_id = operationId,
                    });
            }

            if (request.Method == HttpMethod.Get
                && request.RequestUri!.AbsolutePath
                    == "/api/v1/operations/" + operationId)
            {
                operationReads++;
                bool succeeded = operationReads >= 2;
                return Json(
                    HttpStatusCode.OK,
                    new
                    {
                        operation_id = operationId,
                        phase = succeeded ? "SUCCEEDED" : "QUEUED",
                        started_at = NowUtc(),
                        updated_at = NowUtc(),
                        affected_refs = Array.Empty<string>(),
                        evidence = Array.Empty<object>(),
                        remaining_uncertainty = succeeded
                            ? Array.Empty<string>()
                            : new[]
                            {
                                "provider_in_flight_state_unknown",
                            },
                    });
            }

            throw new InvalidOperationException(
                "unexpected request " + request.RequestUri);
        });

        AuthenticatedEmergencyHostClient firstProcess = new(
            new HttpClient(handler),
            new Uri("http://127.0.0.1:8765/"),
            sessions,
            pendingStore);
        await Check.ThrowsAsync<EmergencyCommandUncertainException>(
            () => firstProcess.BlockNewExposureAsync(CancellationToken.None),
            "lost first response must persist an unresolved command");
        Check.True(
            pendingStore.Payload is not null,
            "uncertain command was not persisted before restart");
        Check.True(
            !pendingStore.Payload!.Contains(token, StringComparison.Ordinal),
            "durable recovery record persisted the reusable bearer credential");
        Check.True(
            pendingStore.Payload!.Contains(
                AuthenticatedEmergencyHostClient.PublicSessionReference(token),
                StringComparison.Ordinal),
            "durable recovery record did not persist the canonical public session reference");
        Check.True(
            pendingStore.Payload!.Contains("\"schema_version\":\"2\"", StringComparison.Ordinal),
            "durable recovery record was not upgraded to the bearer-free v2 schema");

        AuthenticatedEmergencyHostClient restartedProcess = new(
            new HttpClient(handler),
            new Uri("http://127.0.0.1:8765/"),
            sessions,
            pendingStore);
        EmergencyCommandResult recovered =
            await restartedProcess.BlockNewExposureAsync(CancellationToken.None);

        Check.True(recovered.Accepted, "restart did not recover durable acceptance");
        Check.True(
            !recovered.DurableBlockConfirmed,
            "queued operation must not fabricate durable block confirmation");
        Check.True(posts == 2, "restart recovery did not perform one exact retry");
        Check.True(
            stateReads == 1,
            "restart recovery fetched a fresh snapshot and risked retargeting the unresolved command");
        Check.True(
            commandBodies.Count == 2
                && commandBodies[0] == commandBodies[1],
            "restart changed the persisted command bytes");
        Check.True(
            pendingStore.Payload is not null,
            "non-terminal accepted operation lost its exact restart recovery record");

        AuthenticatedEmergencyHostClient secondRestart = new(
            new HttpClient(handler),
            new Uri("http://127.0.0.1:8765/"),
            sessions,
            pendingStore);
        EmergencyCommandResult terminal =
            await secondRestart.BlockNewExposureAsync(CancellationToken.None);
        Check.True(
            terminal.DurableBlockConfirmed,
            "succeeded recovered operation did not confirm the durable block");
        Check.True(posts == 3, "second restart did not recover the same command exactly once");
        Check.True(
            stateReads == 1,
            "accepted-operation recovery minted a fresh snapshot instead of reusing the exact command");
        Check.True(
            commandBodies.Count == 3
                && commandBodies.All(body => body == commandBodies[0]),
            "accepted-operation restart changed command/idempotency/scope/session bytes");
        Check.True(
            pendingStore.Payload is null,
            "terminal recovered operation did not clear the secure recovery record");
    }

    static async Task RestartedCommandCannotRetargetSessionTest()
    {
        const string originalToken = "session-token-restart-original";
        MutableSessionProvider sessions =
            new(PairedSession(originalToken));
        MemoryPendingCommandStore pendingStore = new();
        int posts = 0;

        DelegateHandler handler = new(async (request, _, cancellationToken) =>
        {
            if (request.Method == HttpMethod.Get)
            {
                AssertAuth(request, originalToken);
                return Json(HttpStatusCode.OK, Snapshot(originalToken));
            }

            posts++;
            await request.Content!.ReadAsStringAsync(cancellationToken);
            throw new HttpRequestException("response unknown");
        });

        AuthenticatedEmergencyHostClient firstProcess = new(
            new HttpClient(handler),
            new Uri("http://127.0.0.1:8765/"),
            sessions,
            pendingStore);
        await Check.ThrowsAsync<EmergencyCommandUncertainException>(
            () => firstProcess.BlockNewExposureAsync(CancellationToken.None),
            "first process must retain ambiguous send");

        sessions.Session =
            PairedSession("replacement-session-token");
        AuthenticatedEmergencyHostClient restartedProcess = new(
            new HttpClient(handler),
            new Uri("http://127.0.0.1:8765/"),
            sessions,
            pendingStore);
        await Check.ThrowsAsync<EmergencyCommandUncertainException>(
            () => restartedProcess.BlockNewExposureAsync(CancellationToken.None),
            "restart must not retarget persisted command to a new session");
        Check.True(
            posts == 1,
            "restart sent a persisted command under a replacement session");
    }

    static async Task LegacyBearerRecoveryRecordMigratesFailClosedTest()
    {
        const string token = "legacy-session-token";
        const string commandId = "55555555-5555-5555-5555-555555555555";
        MutableSessionProvider sessions =
            new(PairedSession(token));
        MemoryPendingCommandStore pendingStore = new()
        {
            Payload = JsonSerializer.Serialize(
                new
                {
                    schema_version = "1",
                    command_id = commandId,
                    idempotency_key = "66666666-6666-6666-6666-666666666666",
                    actor = "owner",
                    session = token,
                    account_id = "paper-account-1",
                    environment = "PAPER",
                    expected_state_version = "11",
                }),
        };
        int transportCalls = 0;
        DelegateHandler handler = new((_, _, _) =>
        {
            transportCalls++;
            throw new InvalidOperationException(
                "mismatched recovered session must fail before transport");
        });

        AuthenticatedEmergencyHostClient client = new(
            new HttpClient(handler),
            new Uri("http://127.0.0.1:8765/"),
            sessions,
            pendingStore);

        Check.True(
            pendingStore.Payload is not null
                && !pendingStore.Payload.Contains(token, StringComparison.Ordinal),
            "legacy durable recovery record retained the reusable bearer after migration");
        Check.True(
            pendingStore.Payload!.Contains(
                AuthenticatedEmergencyHostClient.PublicSessionReference(token),
                StringComparison.Ordinal)
                && pendingStore.Payload.Contains(
                    "\"schema_version\":\"2\"",
                    StringComparison.Ordinal),
            "legacy recovery record did not migrate to the canonical public-reference schema");

        sessions.Session =
            PairedSession("replacement-session-token");
        await Check.ThrowsAsync<EmergencyCommandUncertainException>(
            () => client.BlockNewExposureAsync(CancellationToken.None),
            "migrated unresolved command must not retarget to a replacement session");
        Check.True(
            transportCalls == 0,
            "migrated unresolved command reached transport under a replacement session");
    }

    static void CorruptPersistedCommandFailsClosedTest()
    {
        MemoryPendingCommandStore pendingStore = new()
        {
            Payload = "{\"schema_version\":\"1\",\"command_id\":\"not-a-uuid\"}",
        };
        MutableSessionProvider sessions =
            new(PairedSession("session-token-corrupt"));

        Check.Throws<InvalidOperationException>(
            () => _ = new AuthenticatedEmergencyHostClient(
                new HttpClient(new DelegateHandler(
                    (_, _, _) => throw new InvalidOperationException(
                        "corrupt persisted state must fail before transport"))),
                new Uri("http://127.0.0.1:8765/"),
                sessions,
                pendingStore),
            "corrupt persisted emergency command must fail closed at startup");
    }

    static async Task SnapshotBearerEchoFailsClosedTest()
    {
        const string token = "session-token-secret-must-never-echo";
        MutableSessionProvider sessions =
            new(PairedSession(token));
        DelegateHandler handler = new((request, _, _) =>
        {
            AssertAuth(request, token);
            return Task.FromResult(
                Json(
                    HttpStatusCode.OK,
                    new
                    {
                        state_version = "0",
                        event_cursor = "0",
                        server_time = NowUtc(),
                        host_id = "host-local-1",
                        account_id = "paper-account-1",
                        environment = "PAPER",
                        permission_summary = new
                        {
                            actor = "owner",
                            role = "OWNER",
                            session = token,
                        },
                        connection_freshness = new { host = "CURRENT" },
                        portfolio = new { },
                        risk = new { },
                        strategy = new { },
                        jobs = Array.Empty<object>(),
                        reason_codes = Array.Empty<string>(),
                    }));
        });
        AuthenticatedEmergencyHostClient client = new(
            new HttpClient(handler),
            new Uri("http://127.0.0.1:8765/"),
            sessions);

        await Check.ThrowsAsync<InvalidOperationException>(
            () => client.GetStatusAsync(CancellationToken.None),
            "snapshot that echoes the bearer credential must fail closed");

        string safeState = JsonSerializer.Serialize(Snapshot(token));
        Check.True(
            !safeState.Contains(token, StringComparison.Ordinal),
            "canonical snapshot fixture leaked the bearer credential");
        Check.True(
            safeState.Contains(
                "\"session\":\""
                    + AuthenticatedEmergencyHostClient.PublicSessionReference(token)
                    + "\"",
                StringComparison.Ordinal),
            "canonical permission metadata must expose only the public session reference");
        Check.True(
            !safeState.Contains("session_id", StringComparison.Ordinal),
            "legacy session_id alias must not survive contract v3");
    }

    static async Task ScopeAndCanonicalResponseFailureTest()
    {
        const string token = "session-token-d";
        MutableSessionProvider sessions =
            new(PairedSession(token));
        DelegateHandler handler = new((request, _, _) =>
        {
            AssertAuth(request, token);
            return Task.FromResult(
                Json(
                    HttpStatusCode.OK,
                    new
                    {
                        state_version = "01",
                        event_cursor = "0",
                        server_time = NowUtc(),
                        host_id = "host-local-1",
                        account_id = "paper-account-1",
                        environment = "PAPER",
                        permission_summary = new { actor = "owner", role = "OWNER" },
                        connection_freshness = new { host = "CURRENT" },
                        portfolio = new { },
                        risk = new { },
                        strategy = new { },
                        jobs = Array.Empty<object>(),
                        reason_codes = Array.Empty<string>(),
                    }));
        });
        AuthenticatedEmergencyHostClient client = new(
            new HttpClient(handler),
            new Uri("http://127.0.0.1:8765/"),
            sessions);
    
        await Check.ThrowsAsync<InvalidOperationException>(
            () => client.GetStatusAsync(CancellationToken.None),
            "non-canonical sequence must fail closed");
    }

    public static async Task Main()
    {
        CredentialTargetIsOriginBoundTest();
        await PairedOriginMismatchFailsBeforeTransportTest();
        await CanonicalStatusAndOperationTest();
        StaleSuccessorMayCarryOlderEvidenceTimeTest();
        StaleSuccessorRejectsDurableRegressionAndIdentityChangeTest();
        await NonCurrentFreshnessRemainsExplicitTest();
        await StaleFreshnessDoesNotDisableEmergencyBlockTest();
        await AmbiguousPostExactRetryTest();
        await UncertainCommandCannotRetargetSessionTest();
        await UncertainCommandSurvivesDesktopRestartTest();
        await RestartedCommandCannotRetargetSessionTest();
        await LegacyBearerRecoveryRecordMigratesFailClosedTest();
        CorruptPersistedCommandFailsClosedTest();
        await ScopeAndCanonicalResponseFailureTest();
        await SnapshotBearerEchoFailsClosedTest();
        Console.WriteLine("Desktop authenticated host-client contract tests passed.");
    }
}

internal static class Check
{
    public static void True(bool condition, string message)
    {
        if (!condition) throw new InvalidOperationException(message);
    }

    public static void Throws<T>(Action action, string message)
        where T : Exception
    {
        try
        {
            action();
        }
        catch (T)
        {
            return;
        }

        throw new InvalidOperationException(message);
    }

    public static async Task ThrowsAsync<T>(Func<Task> action, string message)
        where T : Exception
    {
        try
        {
            await action();
        }
        catch (T)
        {
            return;
        }

        throw new InvalidOperationException(message);
    }
}

internal sealed class MutableSessionProvider : IEmergencyHostSessionProvider
{
    public EmergencyHostSession Session { get; set; }

    public MutableSessionProvider(EmergencyHostSession session)
    {
        Session = session;
    }

    public EmergencyHostSession GetSession() => Session;
}

internal sealed class DelegateHandler : HttpMessageHandler
{
    private readonly Func<HttpRequestMessage, int, CancellationToken, Task<HttpResponseMessage>> _handler;
    private int _count;

    public DelegateHandler(
        Func<HttpRequestMessage, int, CancellationToken, Task<HttpResponseMessage>> handler)
    {
        _handler = handler;
    }

    protected override Task<HttpResponseMessage> SendAsync(
        HttpRequestMessage request,
        CancellationToken cancellationToken)
    {
        int call = Interlocked.Increment(ref _count);
        return _handler(request, call, cancellationToken);
    }
}

internal sealed class MemoryPendingCommandStore : IEmergencyPendingCommandStore
{
    public string? Payload { get; set; }

    public string? Load() => Payload;

    public void Save(string payload)
    {
        Payload = payload;
    }

    public void Clear()
    {
        Payload = null;
    }
}
