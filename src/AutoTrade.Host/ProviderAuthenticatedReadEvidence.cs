using System;
using System.Buffers;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Net;
using System.Net.Http;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;

namespace AutoTrade.Host;

internal sealed class ProviderAuthenticatedReadEvidence
{
    private readonly byte[] _responseBytes;

    internal ProviderAuthenticatedReadEvidence(
        ProviderAuthenticatedReadPreparedEvidence preparedEvidence,
        ProviderAuthenticatedReadReceipt receipt,
        ProviderAuthenticatedReadDurabilityReceipt durabilityReceipt,
        ProviderAuthenticatedReadObservedDurabilityReceipt observedDurabilityReceipt,
        byte[] responseBytes)
    {
        ArgumentNullException.ThrowIfNull(preparedEvidence);
        ArgumentNullException.ThrowIfNull(receipt);
        ArgumentNullException.ThrowIfNull(durabilityReceipt);
        ArgumentNullException.ThrowIfNull(observedDurabilityReceipt);
        ArgumentNullException.ThrowIfNull(responseBytes);
        if (responseBytes.Length == 0)
        {
            throw new ProviderIssuerAuthorityException(
                "authenticated read evidence requires exact non-empty response bytes");
        }

        ProviderIssuerVerifier.RequireValidReadReceipt(
            preparedEvidence.IssuerSession,
            preparedEvidence.Attempt,
            receipt,
            responseBytes,
            preparedEvidence.IssuerSession.SessionIdentity,
            preparedEvidence.IssuerSession.PublicKeySha256);
        ProviderAuthenticatedReadDurabilityVerifier.RequireObservedMatches(
            preparedEvidence,
            receipt,
            durabilityReceipt,
            observedDurabilityReceipt,
            responseBytes);

        PreparedEvidence = preparedEvidence;
        IssuerSession = preparedEvidence.IssuerSession;
        Attempt = preparedEvidence.Attempt;
        Receipt = receipt;
        DurabilityReceipt = durabilityReceipt;
        ObservedDurabilityReceipt = observedDurabilityReceipt;
        _responseBytes = responseBytes.ToArray();
    }

    internal ProviderAuthenticatedReadPreparedEvidence PreparedEvidence { get; }
    internal ProviderIssuerSession IssuerSession { get; }
    internal ProviderAuthenticatedReadAttemptBinding Attempt { get; }
    internal ProviderAuthenticatedReadReceipt Receipt { get; }
    internal ProviderAuthenticatedReadDurabilityReceipt DurabilityReceipt { get; }
    internal ProviderAuthenticatedReadObservedDurabilityReceipt ObservedDurabilityReceipt { get; }
    internal ReadOnlyMemory<byte> ResponseBytes => _responseBytes;
    internal byte[] CopyResponseBytes() => _responseBytes.ToArray();
}
