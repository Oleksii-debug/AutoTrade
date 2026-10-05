namespace AutoTrade.Host;

/// <summary>
/// Fail-closed admission verdict for the packaged Host process. Listener binding is
/// a privileged transition: a process that cannot observe the selected canonical
/// financial/provider authority must terminate before Kestrel binds any socket.
/// </summary>
public sealed record HostStartupAdmission(
    bool ListenerBindingAuthorized,
    int ExitCode,
    string Status,
    IReadOnlyList<string> ReasonCodes)
{
    public const int SuccessExitCode = 0;
    public const int AuthorityUnavailableExitCode = 78;

    public static HostStartupAdmission Evaluate(IHostAuthorityBoundary authority)
    {
        ArgumentNullException.ThrowIfNull(authority);

        HostAuthorityReadiness? readiness;
        try
        {
            readiness = authority.Readiness;
        }
        catch
        {
            return Blocked("host_authority_probe_failed");
        }

        if (readiness is null)
        {
            return Blocked("host_authority_readiness_missing");
        }

        string status = readiness.Status ?? string.Empty;
        if (string.IsNullOrWhiteSpace(status)
            || !string.Equals(status, status.Trim(), StringComparison.Ordinal))
        {
            return Blocked("host_authority_status_invalid");
        }

        string[] reasonCodes;
        try
        {
            reasonCodes = readiness.ReasonCodes?.ToArray() ?? [];
        }
        catch
        {
            return Blocked("host_authority_reason_codes_invalid");
        }

        if (!ReasonCodesAreCanonical(reasonCodes))
        {
            return Blocked("host_authority_reason_codes_invalid");
        }

        bool ready = string.Equals(status, "READY", StringComparison.Ordinal);
        if (ready && reasonCodes.Length == 0)
        {
            return new HostStartupAdmission(
                ListenerBindingAuthorized: true,
                ExitCode: SuccessExitCode,
                Status: "READY",
                ReasonCodes: Array.Empty<string>());
        }

        if (reasonCodes.Length == 0)
        {
            reasonCodes = ["host_authority_not_ready"];
        }
        else if (ready)
        {
            reasonCodes = [.. reasonCodes, "host_authority_ready_with_blockers"];
        }

        return new HostStartupAdmission(
            ListenerBindingAuthorized: false,
            ExitCode: AuthorityUnavailableExitCode,
            Status: status,
            ReasonCodes: Array.AsReadOnly(reasonCodes));
    }

    private static HostStartupAdmission Blocked(string reasonCode) =>
        new(
            ListenerBindingAuthorized: false,
            ExitCode: AuthorityUnavailableExitCode,
            Status: "BLOCKED",
            ReasonCodes: Array.AsReadOnly(new[] { reasonCode }));

    private static bool ReasonCodesAreCanonical(IEnumerable<string> reasonCodes)
    {
        HashSet<string> seen = new(StringComparer.Ordinal);
        foreach (string reasonCode in reasonCodes)
        {
            if (string.IsNullOrWhiteSpace(reasonCode)
                || !string.Equals(reasonCode, reasonCode.Trim(), StringComparison.Ordinal)
                || reasonCode.Any(character =>
                    !(character is >= 'a' and <= 'z'
                        or >= '0' and <= '9'
                        or '_'))
                || !seen.Add(reasonCode))
            {
                return false;
            }
        }
        return true;
    }
}
