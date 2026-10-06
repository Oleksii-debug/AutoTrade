using System.IO;
using System.Security.Cryptography;
using System.Text.Json;

namespace AutoTrade.Desktop;

/// <summary>Exact installed-byte preflight. An unsigned inventory grants no release trust.</summary>
internal static class InstalledCandidateInventory
{
    public static void Verify(string payloadDirectory)
    {
        string root = Path.GetFullPath(payloadDirectory).TrimEnd(Path.DirectorySeparatorChar);
        string manifest = Path.Combine(Directory.GetParent(root)!.FullName, "bundle-manifest.json");
        using JsonDocument document = JsonDocument.Parse(File.ReadAllBytes(manifest));
        JsonElement value = document.RootElement;
        if (value.GetProperty("product").GetString() != "AutoTrade"
            || value.GetProperty("trading_authority_granted_by_artifact").GetBoolean())
            throw new InvalidOperationException("The installed bundle identity is invalid.");
        string? revision = value.GetProperty("source_sha").GetString();
        if (revision != File.ReadAllText(Path.Combine(root, "product", "SOURCE_REVISION")).Trim())
            throw new InvalidOperationException("The installed source differs from the bundle inventory.");
        HashSet<string> paths = new(StringComparer.OrdinalIgnoreCase);
        foreach (JsonElement file in value.GetProperty("files").EnumerateArray())
        {
            string relative = file.GetProperty("path").GetString()
                ?? throw new InvalidOperationException("Inventory path is missing.");
            if (relative.Contains('\\') || relative.Contains(':') || relative.StartsWith('/')
                || relative.Split('/').Any(part => part is "" or "." or "..") || !paths.Add(relative))
                throw new InvalidOperationException("The installed inventory contains unsafe or duplicated paths.");
            string target = Path.GetFullPath(Path.Combine(root, relative.Replace('/', Path.DirectorySeparatorChar)));
            if (!target.StartsWith(root + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("An installed inventory path escaped the package.");
            for (string? current = target; current is not null; current = Path.GetDirectoryName(current))
            {
                if ((File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0)
                    throw new InvalidOperationException("Package reparse aliases are unsupported. Extract a fresh package to a regular local directory.");
                if (string.Equals(current, root, StringComparison.OrdinalIgnoreCase)) break;
            }
            using FileStream stream = new(target, FileMode.Open, FileAccess.Read, FileShare.Read);
            if (stream.Length != file.GetProperty("size").GetInt64()
                || "sha256:" + Convert.ToHexString(SHA256.HashData(stream)).ToLowerInvariant()
                    != file.GetProperty("sha256").GetString())
                throw new InvalidOperationException("An installed file is missing or changed: " + relative + ". Repair the package.");
        }
        Stack<string> directories = new();
        directories.Push(root);
        while (directories.TryPop(out string? directory))
        {
            foreach (string entry in Directory.EnumerateFileSystemEntries(directory))
            {
                FileAttributes attributes = File.GetAttributes(entry);
                if ((attributes & FileAttributes.ReparsePoint) != 0)
                    throw new InvalidOperationException("The installed package contains a reparse alias.");
                if ((attributes & FileAttributes.Directory) != 0) directories.Push(entry);
                else if (!paths.Contains(Path.GetRelativePath(root, entry).Replace(Path.DirectorySeparatorChar, '/')))
                    throw new InvalidOperationException("The installed package contains an undeclared file. Extract a fresh package.");
            }
        }
    }
}
