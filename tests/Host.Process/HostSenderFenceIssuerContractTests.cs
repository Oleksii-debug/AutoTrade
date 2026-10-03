using System.Runtime.CompilerServices;
using System.Text.Json;
using AutoTrade.Host;

internal static class HostSenderFenceIssuerContractTests
{
    [ModuleInitializer]
    internal static void Run()
    {
        string root = Path.Combine(
            Path.GetTempPath(),
            "autotrade-host-sender-fence-" + Guid.NewGuid().ToString("N"));
        string alternateRoot = root + "-alternate";
        string invalidRoot = root + "-not-a-directory";
        Directory.CreateDirectory(root);
        Directory.CreateDirectory(alternateRoot);
        File.WriteAllText(invalidRoot, "not-a-directory");
        try
        {
            DateTimeOffset issuerStarted = DateTimeOffset.UtcNow.AddMinutes(-1);
            using ProviderIssuerAuthority issuer =
                ProviderIssuerAuthority.CreateProcessAuthority(issuerStarted);
            HostProcessOptions options = HostProcessOptions.Create(
                "http://127.0.0.1:8871/",
                "restored-owner",
                "paper-account",
                "PAPER",
                "BYBIT",
                "TESTNET");

            ExpectRejected(
                () => HostProcessOptions.Create(
                    "http://127.0.0.1:8872/",
                    "case-drift-provider",
                    "paper-account",
                    "PAPER",
                    "bybit",
                    "TESTNET"),
                "provider case drift must not create an alternate lifetime-fence scope");
            ExpectRejected(
                () => HostProcessOptions.Create(
                    "http://127.0.0.1:8873/",
                    "case-drift-environment",
                    "paper-account",
                    "PAPER",
                    "BYBIT",
                    "testnet"),
                "provider-environment case drift must not create an alternate lifetime-fence scope");

            if (OperatingSystem.IsWindows())
            {
                ExpectRejected(
                    () =>
                    {
                        using HostLifetimeExclusiveLease failed =
                            HostLifetimeExclusiveLease.Acquire(
                                options,
                                invalidRoot);
                    },
                    "failed lifetime-fence storage setup must release the kernel scope fence");
            }

            HostLifetimeExclusiveLease lease =
                HostLifetimeExclusiveLease.Acquire(options, root);
            HostSenderFenceReceipt receipt;
            try
            {
                if (OperatingSystem.IsWindows())
                {
                    ExpectRejected(
                        () =>
                        {
                            using HostLifetimeExclusiveLease alternate =
                                HostLifetimeExclusiveLease.Acquire(
                                    options,
                                    alternateRoot);
                        },
                        "alternate filesystem root must not bypass the provider-domain kernel fence");
                }
                HostSenderFenceChallenge challenge = Challenge(
                    newOwnerId: options.HostId);
                receipt = issuer.IssueHostSenderFence(
                    lease,
                    challenge,
                    DateTimeOffset.UtcNow.AddSeconds(1));

                ProviderIssuerVerifier.RequireValidHostSenderFenceReceipt(
                    issuer.Session,
                    receipt,
                    issuer.Session.SessionIdentity,
                    issuer.Session.PublicKeySha256);

                Check(
                    receipt.Schema == "autotrade-host-sender-fence:v1",
                    "sender-fence receipt schema drifted");
                Check(
                    receipt.FenceMethod
                        == "SAME_HOST_EXCLUSIVE_LEASE_HANDOFF",
                    "sender-fence method drifted");
                Check(
                    receipt.LeaseScopeId == lease.ScopeId,
                    "sender-fence receipt lost held lease identity");
                Check(
                    receipt.LeaseOwnerRecordSha256
                        == lease.OwnerRecordSha256,
                    "sender-fence receipt lost exact lease owner record");
                Check(
                    receipt.AccountId == options.AccountId
                        && receipt.RuntimeEnvironment == options.Environment,
                    "sender-fence receipt lost Host financial scope");
                Check(
                    receipt.NewOwnerId == options.HostId
                        && receipt.NewOwnerEpoch == 2,
                    "sender-fence receipt lost exact successor owner");
                Check(
                    receipt.CredentialHandleId == "credential-handle-7"
                        && receipt.CredentialGeneration == 7,
                    "sender-fence receipt lost credential generation binding");

                byte[] envelope =
                    HostSenderFenceAttestationEnvelope.Serialize(
                        issuer.Session,
                        receipt);
                using JsonDocument document = JsonDocument.Parse(envelope);
                Check(
                    document.RootElement.GetProperty("schema").GetString()
                        == "autotrade-host-sender-fence-envelope:v1",
                    "sender-fence envelope schema drifted");
                Check(
                    document.RootElement
                        .GetProperty("receipt")
                        .GetProperty("lease_scope_id")
                        .GetString()
                        == lease.ScopeId,
                    "serialized sender-fence envelope lost lease identity");

                ExpectRejected(
                    () => ProviderIssuerVerifier.RequireValidHostSenderFenceReceipt(
                        issuer.Session,
                        receipt with { ProviderEnvironment = "DEMO" },
                        issuer.Session.SessionIdentity,
                        issuer.Session.PublicKeySha256),
                    "signed sender fence must reject provider-domain relabeling");

                ExpectRejected(
                    () => ProviderIssuerVerifier.RequireValidHostSenderFenceReceipt(
                        issuer.Session,
                        receipt with
                        {
                            BackupManifestSha256 =
                                "sha256:" + new string('b', 64)
                        },
                        issuer.Session.SessionIdentity,
                        issuer.Session.PublicKeySha256),
                    "signed sender fence must reject restore-manifest relabeling");

                ExpectRejected(
                    () => issuer.IssueHostSenderFence(
                        lease,
                        Challenge(newOwnerId: "other-owner"),
                        DateTimeOffset.UtcNow.AddSeconds(2)),
                    "Host must not sign a transition to an owner that does not hold the lease");

                HostSenderFenceChallenge skippedEpoch =
                    Challenge(newOwnerId: options.HostId) with
                    {
                        NewOwnerEpoch = 3
                    };
                ExpectRejected(
                    () => issuer.IssueHostSenderFence(
                        lease,
                        skippedEpoch,
                        DateTimeOffset.UtcNow.AddSeconds(2)),
                    "Host must not sign a skipped recovery-owner epoch");
            }
            finally
            {
                lease.Dispose();
            }

            ExpectRejected(
                () => issuer.IssueHostSenderFence(
                    lease,
                    Challenge(newOwnerId: options.HostId),
                    DateTimeOffset.UtcNow.AddSeconds(2)),
                "released Host lifetime lease must issue no sender-fence proof");

            using ProviderIssuerAuthority attacker =
                ProviderIssuerAuthority.CreateProcessAuthority(
                    DateTimeOffset.UtcNow.AddMinutes(-1));
            ExpectRejected(
                () => ProviderIssuerVerifier.RequireValidHostSenderFenceReceipt(
                    attacker.Session,
                    receipt,
                    attacker.Session.SessionIdentity,
                    attacker.Session.PublicKeySha256),
                "another Host issuer session must not validate the fence receipt");
        }
        finally
        {
            try
            {
                Directory.Delete(root, recursive: true);
                Directory.Delete(alternateRoot, recursive: true);
                File.Delete(invalidRoot);
            }
            catch (IOException)
            {
            }
            catch (UnauthorizedAccessException)
            {
            }
        }
    }

    private static HostSenderFenceChallenge Challenge(string newOwnerId) =>
        new(
            BackupManifestSha256: "sha256:" + new string('a', 64),
            OwnerScope: "PAPER:paper-account",
            ProviderId: "BYBIT",
            ProviderEnvironment: "TESTNET",
            CredentialHandleId: "credential-handle-7",
            CredentialGeneration: 7,
            OldOwnerId: "source-owner",
            OldOwnerEpoch: 1,
            NewOwnerId: newOwnerId,
            NewOwnerEpoch: 2);

    private static void Check(bool condition, string message)
    {
        if (!condition)
        {
            throw new InvalidOperationException(message);
        }
    }

    private static void ExpectRejected(Action action, string message)
    {
        try
        {
            action();
        }
        catch (InvalidOperationException)
        {
            return;
        }
        throw new InvalidOperationException(message);
    }
}
