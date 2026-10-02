using System;
using System.Collections.Generic;
using System.Linq;
using System.Runtime.CompilerServices;
using System.Text;
using System.Text.Json;
using AutoTrade.Host;

internal static class ProviderAuthenticatedReadEnvelopeContractTests
{
    [ModuleInitializer]
    internal static void Run()
    {
        using ProviderIssuerAuthority issuer =
            ProviderIssuerAuthority.CreateProcessAuthority(
                new DateTimeOffset(2026, 10, 2, 12, 0, 0, TimeSpan.Zero));
        ProviderAuthenticatedReadSubject subject = new(
            ProviderId: "BYBIT",
            AccountId: "acct-1",
            EntityId: "entity-1",
            RuntimeEnvironment: "PAPER",
            ProviderEnvironment: "TESTNET",
            Endpoint: "/v5/account/wallet-balance",
            Surface: "AUTHENTICATED_READ",
            PermissionScope: "ACCOUNT_READ",
            DataEntitlement: "ACCOUNT_BALANCES",
            InstrumentVersion: "instrument-version-1",
            QueryDigest: "sha256:" + new string('1', 64),
            EndpointRuleIdentity: "sha256:" + new string('2', 64),
            CredentialHandleId: "credential-handle-1",
            CredentialGeneration: 7,
            CapabilityId: "capability-1",
            QualificationId: "qualification-1",
            QualificationBuildId: "qualification-build-1",
            AdapterBuildIdentity: "adapter-build-1",
            NetworkPolicyIdentity: "sha256:" + new string('3', 64),
            TransportIdentity: "provider-transport:https-v1");
        ProviderAuthenticatedReadAttemptBinding attempt =
            issuer.IssueAuthenticatedReadAttempt(
                subject,
                new DateTimeOffset(
                    2026, 10, 2, 12, 0, 0, 100, TimeSpan.Zero));
        ProviderAuthenticatedReadPreparedEvidence prepared = new(
            issuer.Session,
            attempt,
            new Dictionary<string, string>(StringComparer.Ordinal)
            {
                ["symbol"] = "BTCUSDT",
                ["category"] = "linear",
            });

        byte[] preparedJson =
            ProviderAuthenticatedReadAttestationEnvelope.SerializePrepared(prepared);
        using JsonDocument preparedDocument = JsonDocument.Parse(preparedJson);
        JsonElement preparedRoot = preparedDocument.RootElement;
        Check(
            PropertyNames(preparedRoot).SetEquals(
                new[] { "schema", "issuer_session", "attempt", "query" }),
            "Prepared envelope shape drifted");
        Check(
            preparedRoot.GetProperty("schema").GetString()
                == ProviderAuthenticatedReadAttestationEnvelope.PreparedSchema,
            "Prepared envelope schema drifted");
        Check(
            preparedRoot.GetProperty("attempt")
                .GetProperty("subject")
                .GetProperty("credential_generation")
                .GetInt64() == 7,
            "Prepared envelope lost credential generation");
        Check(
            preparedRoot.GetProperty("query").GetProperty("symbol").GetString()
                == "BTCUSDT",
            "Prepared envelope lost exact query");

        ProviderAuthenticatedReadDurabilityReceipt durable = new(
            issuer.Session.SessionIdentity,
            attempt.ReadAttemptId,
            attempt.BindingSha256,
            attempt.Subject.QueryDigest,
            "sha256:" + new string('4', 64),
            attempt.ReadAttemptId + ":prepared",
            1,
            "2026-10-02T12:00:00.2000000Z");
        ProviderAuthenticatedReadDurabilityVerifier.RequireMatches(
            prepared,
            durable);

        byte[] response = Encoding.UTF8.GetBytes(
            "{\"retCode\":0,\"result\":{\"coin\":[]}}");
        ProviderAuthenticatedReadReceipt receipt =
            issuer.IssueAuthenticatedReadReceipt(
                attempt,
                200,
                response,
                new DateTimeOffset(
                    2026, 10, 2, 12, 0, 0, 300, TimeSpan.Zero));
        ProviderAuthenticatedReadReceipt tamperedReceipt = receipt with
        {
            HttpStatus = 201,
        };
        ExpectFailure(
            () => _ = new ProviderAuthenticatedReadEvidence(
                prepared,
                tamperedReceipt,
                durable,
                response),
            "authenticated-read evidence accepted a tampered signed receipt");

        ProviderAuthenticatedReadDurabilityReceipt wrongDurable = new(
            issuer.Session.SessionIdentity,
            attempt.ReadAttemptId,
            attempt.BindingSha256,
            attempt.Subject.QueryDigest,
            "sha256:" + new string('4', 64),
            "different-prepared-event",
            1,
            "2026-10-02T12:00:00.2000000Z");
        ExpectFailure(
            () => _ = new ProviderAuthenticatedReadEvidence(
                prepared,
                receipt,
                wrongDurable,
                response),
            "authenticated-read evidence accepted a mismatched durability receipt");

        ProviderAuthenticatedReadEvidence observed = new(
            prepared,
            receipt,
            durable,
            response);

        byte[] observedJson =
            ProviderAuthenticatedReadAttestationEnvelope.SerializeObserved(observed);
        using JsonDocument observedDocument = JsonDocument.Parse(observedJson);
        JsonElement observedRoot = observedDocument.RootElement;
        Check(
            PropertyNames(observedRoot).SetEquals(
                new[]
                {
                    "schema",
                    "issuer_session",
                    "attempt",
                    "receipt",
                    "query",
                    "response_base64",
                }),
            "Observed envelope shape drifted");
        Check(
            observedRoot.GetProperty("schema").GetString()
                == ProviderAuthenticatedReadAttestationEnvelope.ObservedSchema,
            "Observed envelope schema drifted");
        Check(
            Convert.FromBase64String(
                observedRoot.GetProperty("response_base64").GetString()
                    ?? throw new InvalidOperationException(
                        "response_base64 is missing"))
                .SequenceEqual(response),
            "Observed envelope did not preserve exact response bytes");
        Check(
            observedRoot.GetProperty("receipt")
                .GetProperty("response_sha256")
                .GetString() == receipt.ResponseSha256,
            "Observed envelope response digest drifted");

        string observedText = Encoding.UTF8.GetString(observedJson);
        Check(
            !observedText.Contains("api_secret", StringComparison.OrdinalIgnoreCase)
                && !observedText.Contains("secret-456", StringComparison.Ordinal),
            "Host attestation envelope leaked credential material");
    }

    private static HashSet<string> PropertyNames(JsonElement value) =>
        value.EnumerateObject()
            .Select(property => property.Name)
            .ToHashSet(StringComparer.Ordinal);

    private static void ExpectFailure(Action action, string message)
    {
        try
        {
            action();
        }
        catch (ProviderIssuerAuthorityException)
        {
            return;
        }
        throw new InvalidOperationException(message);
    }

    private static void Check(bool condition, string message)
    {
        if (!condition)
        {
            throw new InvalidOperationException(message);
        }
    }
}
