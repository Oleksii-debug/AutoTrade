using System.Text.Json;
using AutoTrade.Contracts;

if (args.Length != 2)
{
    Console.Error.WriteLine(
        "usage: AutoTrade.Contracts.Conformance <corpus> <manifest>");
    return 2;
}

using var corpusDocument = JsonDocument.Parse(File.ReadAllText(args[0]));
using var manifestDocument = JsonDocument.Parse(File.ReadAllText(args[1]));

var corpus = corpusDocument.RootElement;
var manifest = manifestDocument.RootElement;
var manifestVersion = manifest.GetProperty("contract_version").GetString()
    ?? throw new InvalidOperationException("manifest contract_version is missing");
var corpusVersion = corpus.GetProperty("contract_version").GetString()
    ?? throw new InvalidOperationException("corpus contract_version is missing");

if (!StringComparer.Ordinal.Equals(ContractBaseline.Version, manifestVersion))
{
    throw new InvalidOperationException(
        $"C# binding version {ContractBaseline.Version} != manifest {manifestVersion}");
}
if (!StringComparer.Ordinal.Equals(corpusVersion, manifestVersion))
{
    throw new InvalidOperationException(
        $"corpus contract version {corpusVersion} != manifest {manifestVersion}");
}

var names = new HashSet<string>(StringComparer.Ordinal);
var count = 0;
foreach (var testCase in corpus.GetProperty("cases").EnumerateArray())
{
    count++;
    var name = testCase.GetProperty("name").GetString()
        ?? throw new InvalidOperationException("corpus case name is missing");
    if (!names.Add(name))
    {
        throw new InvalidOperationException($"duplicate corpus case: {name}");
    }

    var kind = testCase.GetProperty("type").GetString()
        ?? throw new InvalidOperationException($"{name}: type is missing");
    var valueElement = testCase.GetProperty("value");
    string? value = valueElement.ValueKind == JsonValueKind.String
        ? valueElement.GetString()
        : null;
    var expected = testCase.GetProperty("expected").GetBoolean();
    var actual = CommonScalarContracts.IsValid(kind, value);

    if (actual != expected)
    {
        throw new InvalidOperationException(
            $"C# verdict mismatch for {name}: expected {expected}, got {actual}");
    }
}

Console.WriteLine($"C# common-scalar conformance passed ({count} cases).");
return 0;
