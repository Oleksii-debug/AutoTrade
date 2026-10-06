using AutoTrade.Desktop;
using System.Text;
using System.Text.Json;

// Compile the exact Desktop preflight into a portable test host. No second
// inventory authority is implemented here; every decision calls Desktop code.
if (args.Length is < 1 or > 2 || (args.Length == 2 && args[1] != "--exercise"))
{
    Console.Error.WriteLine("Usage: InventoryProbe PAYLOAD [--exercise]");
    return 2;
}

string payload = Path.GetFullPath(args[0]);
try
{
    InstalledCandidateInventory.Verify(payload);
    bool exercised = args.Length == 2;
    if (exercised)
    {
        string asset = Path.Combine(payload, "product", "web", "src", "app.js");
        string retained = Path.Combine(Path.GetDirectoryName(payload)!, "inventory-probe-retained.js");
        string extra = Path.Combine(payload, "inventory-probe-undeclared.txt");
        if (File.Exists(retained) || File.Exists(extra))
            throw new InvalidOperationException("Probe scratch already exists.");

        ExpectRejection(() => File.Move(asset, retained),
            () => File.Move(retained, asset), "missing installed file");

        byte[] original = File.ReadAllBytes(asset);
        byte[] changed = (byte[])original.Clone();
        changed[0] ^= 1;
        ExpectRejection(() => File.WriteAllBytes(asset, changed),
            () => File.WriteAllBytes(asset, original), "changed installed file");

        ExpectRejection(() => File.WriteAllText(extra, "probe"),
            () => File.Delete(extra), "undeclared installed file");

        ExpectManifestRejection(text =>
        {
            const string marker = "\"product\": \"AutoTrade\",";
            int index = text.IndexOf(marker, StringComparison.Ordinal);
            if (index < 0) throw new InvalidOperationException("Probe could not locate product identity.");
            return text.Insert(index, "\"product\": \"forged-first-value\",\n  ");
        }, "duplicate root manifest property");

        ExpectManifestRejection(text =>
        {
            int files = text.IndexOf("\"files\": [", StringComparison.Ordinal);
            int path = files < 0 ? -1 : text.IndexOf("\"path\": ", files, StringComparison.Ordinal);
            if (path < 0) throw new InvalidOperationException("Probe could not locate file inventory path.");
            return text.Insert(path, "\"path\": \"forged-first-path\",\n      ");
        }, "duplicate file inventory property");

        ExpectManifestRejection(text =>
        {
            int firstLine = text.IndexOf('\n');
            if (firstLine < 0) throw new InvalidOperationException("Probe manifest is not multiline JSON.");
            return text.Insert(firstLine + 1, "  \"unexpected_inventory_authority\": true,\n");
        }, "unknown root manifest property");
    }
    InstalledCandidateInventory.Verify(payload);
    Console.WriteLine(JsonSerializer.Serialize(new
    {
        inventory = "PASS",
        exercised_missing_changed_extra = exercised,
        exercised_manifest_schema = exercised,
        windows_executed = OperatingSystem.IsWindows(),
        nvda_verified = false
    }));
    return 0;
}
catch (Exception error)
{
    Console.Error.WriteLine("Inventory probe failed: " + error.Message);
    return 1;
}

void ExpectRejection(Action mutate, Action restore, string scenario)
{
    mutate();
    try
    {
        bool rejected = false;
        try { InstalledCandidateInventory.Verify(payload); }
        catch (Exception) { rejected = true; }
        if (!rejected) throw new InvalidOperationException("Desktop accepted " + scenario);
    }
    finally { restore(); }
    InstalledCandidateInventory.Verify(payload);
}

void ExpectManifestRejection(Func<string, string> mutate, string scenario)
{
    string manifest = Path.Combine(Path.GetDirectoryName(payload)!, "bundle-manifest.json");
    byte[] original = File.ReadAllBytes(manifest);
    string originalText = Encoding.UTF8.GetString(original);
    string changedText = mutate(originalText);
    byte[] changed = Encoding.UTF8.GetBytes(changedText);
    ExpectRejection(
        () => File.WriteAllBytes(manifest, changed),
        () => File.WriteAllBytes(manifest, original),
        scenario);
}
