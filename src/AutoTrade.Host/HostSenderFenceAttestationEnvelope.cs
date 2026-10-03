using System.Buffers;
using System.Text.Json;

namespace AutoTrade.Host;

/// <summary>
/// Canonical transport envelope for one Host-issued same-machine sender fence proof.
///
/// The contained signature proves that the process-private Host issuer observed the
/// exact challenge only while the successor process held the OS lifetime lease for
/// the account/runtime scope. It is deliberately not a clean-machine/remote-host
/// credential-revocation proof.
/// </summary>
internal static class HostSenderFenceAttestationEnvelope
{
    internal const string Schema =
        "autotrade-host-sender-fence-envelope:v1";

    internal static byte[] Serialize(
        ProviderIssuerSession issuerSession,
        HostSenderFenceReceipt receipt)
    {
        ArgumentNullException.ThrowIfNull(issuerSession);
        ArgumentNullException.ThrowIfNull(receipt);
        ProviderIssuerVerifier.RequireValidHostSenderFenceReceipt(
            issuerSession,
            receipt,
            issuerSession.SessionIdentity,
            issuerSession.PublicKeySha256);

        ArrayBufferWriter<byte> buffer = new();
        using (Utf8JsonWriter writer = new(buffer))
        {
            writer.WriteStartObject();
            writer.WriteString("schema", Schema);
            writer.WritePropertyName("issuer_session");
            WriteSession(writer, issuerSession);
            writer.WritePropertyName("receipt");
            WriteReceipt(writer, receipt);
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

    private static void WriteReceipt(
        Utf8JsonWriter writer,
        HostSenderFenceReceipt receipt)
    {
        writer.WriteStartObject();
        writer.WriteString("schema", receipt.Schema);
        writer.WriteString(
            "issuer_session_identity",
            receipt.IssuerSessionIdentity);
        writer.WriteString("fence_method", receipt.FenceMethod);
        writer.WriteString("lease_scope_id", receipt.LeaseScopeId);
        writer.WriteString(
            "lease_owner_record_sha256",
            receipt.LeaseOwnerRecordSha256);
        writer.WriteString(
            "lease_acquired_at_utc",
            receipt.LeaseAcquiredAtUtc);
        writer.WriteString("owner_scope", receipt.OwnerScope);
        writer.WriteString("provider_id", receipt.ProviderId);
        writer.WriteString("account_id", receipt.AccountId);
        writer.WriteString(
            "runtime_environment",
            receipt.RuntimeEnvironment);
        writer.WriteString(
            "provider_environment",
            receipt.ProviderEnvironment);
        writer.WriteString(
            "credential_handle_id",
            receipt.CredentialHandleId);
        writer.WriteNumber(
            "credential_generation",
            receipt.CredentialGeneration);
        writer.WriteString(
            "backup_manifest_sha256",
            receipt.BackupManifestSha256);
        writer.WriteString("old_owner_id", receipt.OldOwnerId);
        writer.WriteNumber("old_owner_epoch", receipt.OldOwnerEpoch);
        writer.WriteString("new_owner_id", receipt.NewOwnerId);
        writer.WriteNumber("new_owner_epoch", receipt.NewOwnerEpoch);
        writer.WriteString("fenced_at_utc", receipt.FencedAtUtc);
        writer.WriteString("receipt_sha256", receipt.ReceiptSha256);
        writer.WriteString("signature_base64", receipt.SignatureBase64);
        writer.WriteEndObject();
    }
}
