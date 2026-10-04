namespace AutoTrade.Engine.Lean;

public enum LeanAdoptionProbeStatus
{
    Pass,
    Fail,
    Inconclusive,
}

public sealed record LeanAdoptionEvidence(
    string Repository,
    string Commit,
    string Tree,
    string License,
    LeanAdoptionProbeStatus SourceComposition,
    LeanAdoptionProbeStatus Embedding,
    LeanAdoptionProbeStatus WindowsBuild,
    LeanAdoptionProbeStatus LinuxBuild,
    LeanAdoptionProbeStatus Packaging,
    LeanAdoptionProbeStatus DecimalBehavior,
    LeanAdoptionProbeStatus EventOrdering,
    LeanAdoptionProbeStatus RestartReconciliation,
    LeanAdoptionProbeStatus AdapterIsolation);

public sealed record LeanAdoptionDecision(
    string Status,
    IReadOnlyList<string> Reasons,
    bool LiveTradingAuthorityGranted);

public static class LeanAdoptionGate
{
    public const string Repository = "QuantConnect/Lean";
    public const string Commit = "985ef30ad3ac774218c5ac516b4cb0aa2655730f";
    public const string Tree = "4b163abf9fca60e731b76510b9ae6721ffff7e6c";
    public const string License = "Apache-2.0";

    public static LeanAdoptionDecision Evaluate(LeanAdoptionEvidence evidence)
    {
        ArgumentNullException.ThrowIfNull(evidence);

        var reasons = new List<string>();
        if (!string.Equals(evidence.Repository, Repository, StringComparison.Ordinal))
        {
            reasons.Add("repository_pin_mismatch");
        }

        if (!string.Equals(evidence.Commit, Commit, StringComparison.Ordinal))
        {
            reasons.Add("commit_pin_mismatch");
        }

        if (!string.Equals(evidence.Tree, Tree, StringComparison.Ordinal))
        {
            reasons.Add("tree_pin_mismatch");
        }

        if (!string.Equals(evidence.License, License, StringComparison.Ordinal))
        {
            reasons.Add("license_pin_mismatch");
        }

        var probes = new (string Name, LeanAdoptionProbeStatus Status)[]
        {
            ("source_composition", evidence.SourceComposition),
            ("embedding", evidence.Embedding),
            ("windows_build", evidence.WindowsBuild),
            ("linux_build", evidence.LinuxBuild),
            ("packaging", evidence.Packaging),
            ("decimal_behavior", evidence.DecimalBehavior),
            ("event_ordering", evidence.EventOrdering),
            ("restart_reconciliation", evidence.RestartReconciliation),
            ("adapter_isolation", evidence.AdapterIsolation),
        };

        foreach (var (name, status) in probes)
        {
            if (!Enum.IsDefined(status))
            {
                throw new ArgumentOutOfRangeException(
                    nameof(evidence),
                    $"unknown probe status for {name}");
            }

            if (status == LeanAdoptionProbeStatus.Fail)
            {
                reasons.Add($"probe_failed:{name}");
            }
        }

        if (reasons.Count > 0)
        {
            return new LeanAdoptionDecision(
                "FAIL",
                reasons.AsReadOnly(),
                LiveTradingAuthorityGranted: false);
        }

        var inconclusive = probes
            .Where(item => item.Status == LeanAdoptionProbeStatus.Inconclusive)
            .Select(item => $"probe_inconclusive:{item.Name}")
            .ToList();

        if (inconclusive.Count > 0)
        {
            return new LeanAdoptionDecision(
                "INCONCLUSIVE",
                inconclusive.AsReadOnly(),
                LiveTradingAuthorityGranted: false);
        }

        return new LeanAdoptionDecision(
            "PASS",
            Array.Empty<string>(),
            LiveTradingAuthorityGranted: false);
    }
}
