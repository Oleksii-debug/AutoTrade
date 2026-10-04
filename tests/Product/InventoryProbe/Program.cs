using AutoTrade.Desktop;
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
    }
    InstalledCandidateInventory.Verify(payload);
    Console.WriteLine(JsonSerializer.Serialize(new
    {
        inventory = "PASS",
        exercised_missing_changed_extra = exercised,
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
