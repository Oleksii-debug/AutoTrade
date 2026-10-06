using System.Text.Json;
using AutoTrade.Contracts;

if (args.Length != 3)
{
    Console.Error.WriteLine(
        "Usage: Contracts.DotNet <common-scalars.corpus.json> "
        + "<dataset-manifest.semantic.corpus.json> <contract-shapes.corpus.json>"
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


using (var document = JsonDocument.Parse(File.ReadAllText(args[2])))
{
    var root = document.RootElement;
    var corpusVersion = root.GetProperty("contract_version").GetString();
    if (!string.Equals(corpusVersion, ContractShapeContracts.Version, StringComparison.Ordinal))
    {
        Console.Error.WriteLine(
            $"Shape corpus contract version {corpusVersion} != binding {ContractShapeContracts.Version}"
        );
        return 1;
    }
    if (!string.Equals(
            ContractShapeContracts.Version,
            ContractBaseline.Version,
            StringComparison.Ordinal
        ))
    {
        Console.Error.WriteLine(
            $"Shape binding version {ContractShapeContracts.Version} != baseline {ContractBaseline.Version}"
        );
        return 1;
    }
    var scope = root.GetProperty("scope").GetString();
    if (!string.Equals(scope, "closed-object-shape-subset", StringComparison.Ordinal))
    {
        failures.Add($"unexpected shape corpus scope: {scope}");
    }

    var names = new HashSet<string>(StringComparer.Ordinal);
    var contracts = new HashSet<string>(StringComparer.Ordinal);
    var dimensions = new HashSet<string>(StringComparer.Ordinal);
    var cases = root.GetProperty("cases");
    foreach (var item in cases.EnumerateArray())
    {
        var name = item.GetProperty("name").GetString() ?? "";
        var contractName = item.GetProperty("contract").GetString() ?? "";
        var dimension = item.GetProperty("dimension").GetString() ?? "";
        var expected = item.GetProperty("expected").GetBoolean();
        if (!names.Add(name))
        {
            failures.Add($"{name}: duplicate contract-shape case name");
            continue;
        }
        contracts.Add(contractName);
        dimensions.Add(dimension);
        bool actual;
        try
        {
            actual = ContractShapeContracts.IsValid(
                contractName,
                item.GetProperty("value")
            );
        }
        catch (Exception error)
        {
            failures.Add($"{name}: unexpected exception {error.GetType().Name}");
            continue;
        }
        if (actual != expected)
        {
            failures.Add($"{name}: expected {expected}, got {actual}");
        }
    }

    if (cases.GetArrayLength() != root.GetProperty("case_count").GetInt32())
    {
        failures.Add("shape corpus case_count does not match cases length");
    }
    if (contracts.Count != root.GetProperty("definition_count").GetInt32())
    {
        failures.Add("shape corpus definition_count does not match distinct contracts");
    }
    foreach (var dimension in new[] { "shape", "unknown-field", "required-field", "enum" })
    {
        if (!dimensions.Contains(dimension))
        {
            failures.Add($"shape corpus missing dimension: {dimension}");
        }
    }

    try
    {
        using var unknown = JsonDocument.Parse("{}");
        _ = ContractShapeContracts.IsValid(
            "missing.schema.json#/$defs/Missing",
            unknown.RootElement
        );
        failures.Add("unknown contract identity was not rejected");
    }
    catch (ArgumentOutOfRangeException)
    {
        // Expected.
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
