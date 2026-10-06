using System.IO;
using System.Security.Cryptography;
using System.Text.Json;

namespace AutoTrade.Desktop;

/// <summary>Exact installed-byte preflight. An unsigned inventory grants no release trust.</summary>
internal static class InstalledCandidateInventory
{
    private static readonly HashSet<string> ManifestFields = new(StringComparer.Ordinal)
    {
        "schema_version",
        "product",
        "version",
        "source_sha",
        "mode",
        "release_eligible",
        "trading_authority_granted_by_artifact",
        "provenance_sha256",
        "composition_sha256",
        "composition",
        "provenance_blockers",
        "files",
    };

    private static readonly HashSet<string> FileFields = new(StringComparer.Ordinal)
    {
        "path",
        "sha256",
        "size",
    };

    public static void Verify(string payloadDirectory)
    {
        string root = Path.GetFullPath(payloadDirectory).TrimEnd(Path.DirectorySeparatorChar);
        DirectoryInfo? parent = Directory.GetParent(root);
        if (parent is null)
            throw new InvalidOperationException("The installed package root is invalid.");

        string manifest = Path.Combine(parent.FullName, "bundle-manifest.json");
        using JsonDocument document = JsonDocument.Parse(
            File.ReadAllBytes(manifest),
            new JsonDocumentOptions
            {
                AllowTrailingCommas = false,
                CommentHandling = JsonCommentHandling.Disallow,
                MaxDepth = 64,
            });
        JsonElement value = document.RootElement;
        RejectDuplicateProperties(value);
        if (value.ValueKind != JsonValueKind.Object
            || !ManifestFields.SetEquals(value.EnumerateObject().Select(property => property.Name)))
            throw new InvalidOperationException("The installed bundle manifest schema is invalid.");

        if (value.GetProperty("schema_version").GetString() != "1.0.0"
            || value.GetProperty("product").GetString() != "AutoTrade"
            || value.GetProperty("trading_authority_granted_by_artifact").GetBoolean())
            throw new InvalidOperationException("The installed bundle identity is invalid.");

        string? mode = value.GetProperty("mode").GetString();
        bool releaseEligible = value.GetProperty("release_eligible").GetBoolean();
        if (mode is not ("diagnostics" or "release")
            || (mode == "diagnostics" && releaseEligible))
            throw new InvalidOperationException("The installed bundle mode is invalid.");

        string? revision = value.GetProperty("source_sha").GetString();
        if (!IsLowerHex(revision, 40))
            throw new InvalidOperationException("The installed source revision is invalid.");
        if (!string.Equals(
                revision,
                File.ReadAllText(Path.Combine(root, "product", "SOURCE_REVISION")).Trim(),
                StringComparison.Ordinal))
            throw new InvalidOperationException("The installed source differs from the bundle inventory.");

        JsonElement fileInventory = value.GetProperty("files");
        if (fileInventory.ValueKind != JsonValueKind.Array)
            throw new InvalidOperationException("The installed file inventory is invalid.");

        HashSet<string> paths = new(StringComparer.OrdinalIgnoreCase);
        foreach (JsonElement file in fileInventory.EnumerateArray())
        {
            if (file.ValueKind != JsonValueKind.Object
                || !FileFields.SetEquals(file.EnumerateObject().Select(property => property.Name)))
                throw new InvalidOperationException("An installed inventory entry is invalid.");

            string relative = file.GetProperty("path").GetString()
                ?? throw new InvalidOperationException("Inventory path is missing.");
            string? expectedDigest = file.GetProperty("sha256").GetString();
            long expectedSize = file.GetProperty("size").GetInt64();
            if (!IsSha256(expectedDigest) || expectedSize < 0)
                throw new InvalidOperationException("An installed inventory digest or size is invalid.");

            if (relative.Contains('\') || relative.Contains(':') || relative.StartsWith('/')
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
            string observedDigest = "sha256:" + Convert.ToHexString(SHA256.HashData(stream)).ToLowerInvariant();
            if (stream.Length != expectedSize
                || !string.Equals(observedDigest, expectedDigest, StringComparison.Ordinal))
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

    private static void RejectDuplicateProperties(JsonElement value)
    {
        if (value.ValueKind == JsonValueKind.Object)
        {
            HashSet<string> names = new(StringComparer.Ordinal);
            foreach (JsonProperty property in value.EnumerateObject())
            {
                if (!names.Add(property.Name))
                    throw new InvalidOperationException("The installed bundle manifest contains duplicate JSON properties.");
                RejectDuplicateProperties(property.Value);
            }
            return;
        }

        if (value.ValueKind == JsonValueKind.Array)
        {
            foreach (JsonElement item in value.EnumerateArray())
                RejectDuplicateProperties(item);
        }
    }

    private static bool IsSha256(string? value) =>
        value is not null
        && value.StartsWith("sha256:", StringComparison.Ordinal)
        && IsLowerHex(value[7..], 64);

    private static bool IsLowerHex(string? value, int length) =>
        value is not null
        && value.Length == length
        && value.All(character =>
            character is >= '0' and <= '9'
            or >= 'a' and <= 'f');
}
