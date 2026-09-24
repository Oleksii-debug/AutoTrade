using AutoTrade.Engine.Lean;

static void Expect(bool condition, string message)
{
    if (!condition)
    {
        throw new InvalidOperationException(message);
    }
}

static LeanAdoptionEvidence Evidence(
    LeanAdoptionProbeStatus status = LeanAdoptionProbeStatus.Pass,
    string? commit = null,
    string? tree = null)
{
    return new LeanAdoptionEvidence(
        LeanAdoptionGate.Repository,
        commit ?? LeanAdoptionGate.Commit,
        tree ?? LeanAdoptionGate.Tree,
        LeanAdoptionGate.License,
        status,
        status,
        status,
        status,
        status,
        status,
        status,
        status,
        status);
}

var complete = LeanAdoptionGate.Evaluate(Evidence());
Expect(complete.Status == "PASS", "complete exact evidence must pass the adoption gate");
Expect(!complete.LiveTradingAuthorityGranted, "LEAN adoption must never grant live trading authority");

var compositionMissing = Evidence() with
{
    SourceComposition = LeanAdoptionProbeStatus.Inconclusive,
};
var compositionDecision = LeanAdoptionGate.Evaluate(compositionMissing);
Expect(compositionDecision.Status == "INCONCLUSIVE", "unresolved source composition must block adoption");
Expect(
    compositionDecision.Reasons.Contains("probe_inconclusive:source_composition"),
    "source-composition blocker must remain explicit");

var wrongPin = LeanAdoptionGate.Evaluate(
    Evidence(commit: new string('a', 40)));
Expect(wrongPin.Status == "FAIL", "wrong LEAN commit must fail");
Expect(wrongPin.Reasons.Contains("commit_pin_mismatch"), "commit mismatch must be named");

var wrongTree = LeanAdoptionGate.Evaluate(
    Evidence(tree: new string('b', 40)));
Expect(wrongTree.Status == "FAIL", "wrong LEAN tree must fail");
Expect(wrongTree.Reasons.Contains("tree_pin_mismatch"), "tree mismatch must be named");

var restartFailure = Evidence() with
{
    RestartReconciliation = LeanAdoptionProbeStatus.Fail,
};
var restartDecision = LeanAdoptionGate.Evaluate(restartFailure);
Expect(restartDecision.Status == "FAIL", "restart/reconciliation failure must block adoption");
Expect(
    restartDecision.Reasons.Contains("probe_failed:restart_reconciliation"),
    "restart/reconciliation blocker must remain explicit");

var eventOrderingUnknown = Evidence() with
{
    EventOrdering = LeanAdoptionProbeStatus.Inconclusive,
};
Expect(
    LeanAdoptionGate.Evaluate(eventOrderingUnknown).Status == "INCONCLUSIVE",
    "unknown callback ordering must not pass");

Expect(0.1m + 0.2m == 0.3m, ".NET decimal boundary must remain exact");
Expect(
    LeanAdoptionGate.Evaluate(Evidence()).Status == "PASS",
    "deterministic gate evaluation must be stable");

Console.WriteLine("LEAN adoption gate harness passed; this is gate logic evidence, not LEAN runtime qualification.");
