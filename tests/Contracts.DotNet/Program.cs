using System.Text.Json;
using AutoTrade.Contracts;

if (args.Length != 2)
{
    Console.Error.WriteLine(
        "Usage: Contracts.DotNet <common-scalars.corpus.json> <dataset-manifest.semantic.corpus.json>"
    );
    return 2;
}

var failures = new List<string>();

using (var document = JsonDocument.Parse(File.ReadAllText(args[0])))
{
    var root = document.RootElement;
    var corpusVersion = root.GetProperty("contract_version").GetString();
    if (!string.Equals(corpusVersion, ContractBaseline.Version, StringComparison.Ordinal))
    {
        Console.Error.WriteLine(
            $"Corpus contract version {corpusVersion} != binding {ContractBaseline.Version}"
        );
        return 1;
    }

    var names = new HashSet<string>(StringComparer.Ordinal);
    foreach (var item in root.GetProperty("cases").EnumerateArray())
    {
        var name = item.GetProperty("name").GetString() ?? "";
        var kind = item.GetProperty("type").GetString() ?? "";
        var valueElement = item.GetProperty("value");
        var value = valueElement.ValueKind == JsonValueKind.String
            ? valueElement.GetString()
            : null;
        var expected = item.GetProperty("expected").GetBoolean();
        if (!names.Add(name))
        {
            failures.Add($"{name}: duplicate common-scalar case name");
            continue;
        }

        bool actual;
        try
        {
            actual = CommonScalarContracts.IsValid(kind, value);
        }
        catch (ArgumentOutOfRangeException)
        {
            failures.Add($"{name}: unsupported scalar kind {kind}");
            continue;
        }

        if (actual != expected)
        {
            failures.Add($"{name}: expected {expected}, got {actual}");
        }
    }
}

using (var document = JsonDocument.Parse(File.ReadAllText(args[1])))
{
    var root = document.RootElement;
    var corpusVersion = root.GetProperty("contract_version").GetString();
    if (!string.Equals(corpusVersion, ContractBaseline.Version, StringComparison.Ordinal))
    {
        Console.Error.WriteLine(
            $"Dataset corpus contract version {corpusVersion} != binding {ContractBaseline.Version}"
        );
        return 1;
    }
    var validatorId = root.GetProperty("validator_id").GetString();
    if (!string.Equals(
            validatorId,
            DatasetManifestContracts.SemanticValidatorId,
            StringComparison.Ordinal
        ))
    {
        Console.Error.WriteLine(
            $"Dataset corpus validator {validatorId} != binding {DatasetManifestContracts.SemanticValidatorId}"
        );
        return 1;
    }

    var names = new HashSet<string>(StringComparer.Ordinal);
    foreach (var item in root.GetProperty("cases").EnumerateArray())
    {
        var name = item.GetProperty("name").GetString() ?? "";
        var expected = item.GetProperty("expected").GetBoolean();
        if (!names.Add(name))
        {
            failures.Add($"{name}: duplicate DatasetManifest case name");
            continue;
        }
        var actual = DatasetManifestContracts.IsSemanticallyValid(
            item.GetProperty("value")
        );
        if (actual != expected)
        {
            failures.Add($"{name}: expected {expected}, got {actual}");
        }
    }
}

if (failures.Count != 0)
{
    foreach (var failure in failures)
    {
        Console.Error.WriteLine(failure);
    }
    return 1;
}

Console.WriteLine("C# contract corpora passed.");
return 0;
