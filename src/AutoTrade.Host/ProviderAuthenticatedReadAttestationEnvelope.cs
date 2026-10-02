using System.Buffers;
using System.Collections.Generic;
using System.Text.Json;

namespace AutoTrade.Host;

internal static class ProviderAuthenticatedReadAttestationEnvelope
{
    internal const string PreparedSchema =
        "autotrade-host-authenticated-read-prepared:v1";
    internal const string ObservedSchema =
        "autotrade-host-authenticated-read-observed:v1";

    internal static byte[] SerializePrepared(
        ProviderAuthenticatedReadPreparedEvidence evidence)
    {
        ArgumentNullException.ThrowIfNull(evidence);
        ProviderIssuerVerifier.RequireValidReadAttempt(
            evidence.IssuerSession,
            evidence.Attempt,
            evidence.IssuerSession.SessionIdentity,
            evidence.IssuerSession.PublicKeySha256);

        ArrayBufferWriter<byte> buffer = new();
        using (Utf8JsonWriter writer = new(buffer))
        {
            writer.WriteStartObject();
            writer.WriteString("schema", PreparedSchema);
            writer.WritePropertyName("issuer_session");
            WriteSession(writer, evidence.IssuerSession);
            writer.WritePropertyName("attempt");
            WriteAttempt(writer, evidence.Attempt);
            writer.WritePropertyName("query");
            WriteQuery(writer, evidence.Query);
            writer.WriteEndObject();
        }
        return buffer.WrittenSpan.ToArray();
    }

    internal static byte[] SerializeObserved(
        ProviderAuthenticatedReadEvidence evidence)
    {
        ArgumentNullException.ThrowIfNull(evidence);
        byte[] response = evidence.CopyResponseBytes();
        ProviderIssuerVerifier.RequireValidReadReceipt(
            evidence.IssuerSession,
            evidence.Attempt,
            evidence.Receipt,
            response,
            evidence.IssuerSession.SessionIdentity,
            evidence.IssuerSession.PublicKeySha256);

        ArrayBufferWriter<byte> buffer = new();
        using (Utf8JsonWriter writer = new(buffer))
        {
            writer.WriteStartObject();
            writer.WriteString("schema", ObservedSchema);
            writer.WritePropertyName("issuer_session");
            WriteSession(writer, evidence.IssuerSession);
            writer.WritePropertyName("attempt");
            WriteAttempt(writer, evidence.Attempt);
            writer.WritePropertyName("receipt");
            WriteReceipt(writer, evidence.Receipt);
            writer.WritePropertyName("query");
            WriteQuery(writer, evidence.PreparedEvidence.Query);
            writer.WriteBase64String("response_base64", response);
            writer.WriteEndObject();
        }
        return buffer.WrittenSpan.ToArray();
    }

    private static void WriteSession(
        Utf8JsonWriter writer,
        ProviderIssuerSession session)
    {
        writer.WriteStartObject();
        writer.WriteString("schema", session.Schema);
        writer.WriteString("issuer_instance_id", session.IssuerInstanceId);
        writer.WriteString("started_at_utc", session.StartedAtUtc);
        writer.WriteString(
            "public_key_spki_base64",
            session.PublicKeySpkiBase64);
        writer.WriteString("public_key_sha256", session.PublicKeySha256);
        writer.WriteString("session_identity", session.SessionIdentity);
        writer.WriteEndObject();
    }

    private static void WriteSubject(
        Utf8JsonWriter writer,
        ProviderAuthenticatedReadSubject subject)
    {
        writer.WriteStartObject();
        writer.WriteString("provider_id", subject.ProviderId);
        writer.WriteString("account_id", subject.AccountId);
        writer.WriteString("entity_id", subject.EntityId);
        writer.WriteString("runtime_environment", subject.RuntimeEnvironment);
        writer.WriteString("provider_environment", subject.ProviderEnvironment);
        writer.WriteString("endpoint", subject.Endpoint);
        writer.WriteString("surface", subject.Surface);
        writer.WriteString("permission_scope", subject.PermissionScope);
        writer.WriteString("data_entitlement", subject.DataEntitlement);
        writer.WriteString("instrument_version", subject.InstrumentVersion);
        writer.WriteString("query_digest", subject.QueryDigest);
        writer.WriteString(
            "endpoint_rule_identity",
            subject.EndpointRuleIdentity);
        writer.WriteString("credential_handle_id", subject.CredentialHandleId);
        writer.WriteNumber(
            "credential_generation",
            subject.CredentialGeneration);
        writer.WriteString("capability_id", subject.CapabilityId);
        writer.WriteString("qualification_id", subject.QualificationId);
        writer.WriteString(
            "qualification_build_id",
            subject.QualificationBuildId);
        writer.WriteString(
            "adapter_build_identity",
            subject.AdapterBuildIdentity);
        writer.WriteString(
            "network_policy_identity",
            subject.NetworkPolicyIdentity);
        writer.WriteString("transport_identity", subject.TransportIdentity);
        writer.WriteEndObject();
    }

    private static void WriteAttempt(
        Utf8JsonWriter writer,
        ProviderAuthenticatedReadAttemptBinding attempt)
    {
        writer.WriteStartObject();
        writer.WriteString("schema", attempt.Schema);
        writer.WriteString(
            "issuer_session_identity",
            attempt.IssuerSessionIdentity);
        writer.WritePropertyName("subject");
        WriteSubject(writer, attempt.Subject);
        writer.WriteNumber("read_generation", attempt.ReadGeneration);
        writer.WriteString("read_attempt_id", attempt.ReadAttemptId);
        writer.WriteString("prepared_at_utc", attempt.PreparedAtUtc);
        writer.WriteString("binding_sha256", attempt.BindingSha256);
        writer.WriteString("signature_base64", attempt.SignatureBase64);
        writer.WriteEndObject();
    }

    private static void WriteReceipt(
        Utf8JsonWriter writer,
        ProviderAuthenticatedReadReceipt receipt)
    {
        writer.WriteStartObject();
        writer.WriteString("schema", receipt.Schema);
        writer.WriteString(
            "issuer_session_identity",
            receipt.IssuerSessionIdentity);
        writer.WriteString(
            "read_attempt_binding_sha256",
            receipt.ReadAttemptBindingSha256);
        writer.WriteString("read_attempt_id", receipt.ReadAttemptId);
        writer.WriteNumber("read_generation", receipt.ReadGeneration);
        writer.WriteNumber("http_status", receipt.HttpStatus);
        writer.WriteString("response_sha256", receipt.ResponseSha256);
        writer.WriteNumber("response_length", receipt.ResponseLength);
        writer.WriteString("observed_at_utc", receipt.ObservedAtUtc);
        writer.WriteString("receipt_sha256", receipt.ReceiptSha256);
        writer.WriteString("signature_base64", receipt.SignatureBase64);
        writer.WriteEndObject();
    }

    private static void WriteQuery(
        Utf8JsonWriter writer,
        IReadOnlyDictionary<string, string> query)
    {
        ArgumentNullException.ThrowIfNull(query);
        List<string> keys = new(query.Keys);
        keys.Sort(StringComparer.Ordinal);
        writer.WriteStartObject();
        foreach (string key in keys)
        {
            writer.WriteString(key, query[key]);
        }
        writer.WriteEndObject();
    }
}
