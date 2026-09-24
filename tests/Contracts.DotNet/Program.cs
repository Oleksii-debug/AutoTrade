using System.Text.Json;
using AutoTrade.Contracts;

if (args.Length != 1)
{
    Console.Error.WriteLine("Usage: Contracts.DotNet <common-scalars.corpus.json>");
    return 2;
}

using var document = JsonDocument.Parse(File.ReadAllText(args[0]));
var root = document.RootElement;
var corpusVersion = root.GetProperty("contract_version").GetString();
if (!string.Equals(corpusVersion, ContractBaseline.Version, StringComparison.Ordinal))
{
    Console.Error.WriteLine(
        $"Corpus contract version {corpusVersion} != binding {ContractBaseline.Version}"
    );
    return 1;
}

var failures = new List<string>();
var names = new HashSet<string>(StringComparer.Ordinal);
foreach (var item in root.GetProperty("cases").EnumerateArray())
{
    var name = item.GetProperty("name").GetString() ?? "";
    var kind = item.GetProperty("type").GetString() ?? "";
    var value = item.GetProperty("value").GetString();
    var expected = item.GetProperty("expected").GetBoolean();
    if (!names.Add(name))
    {
        failures.Add($"{name}: duplicate case name");
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

if (failures.Count != 0)
{
    foreach (var failure in failures)
    {
        Console.Error.WriteLine(failure);
    }
    return 1;
}

Console.WriteLine($"C# common scalar corpus passed: {names.Count} cases.");
return 0;
