using System.Text.Json;

namespace AutoTrade.Contracts;

/// <summary>
/// Semantic validation authority for the canonical DatasetManifest contract.
/// </summary>
public static class DatasetManifestContracts
{
    /// <summary>
    /// Stable identifier for the DatasetManifest content/source-evidence validator.
    /// </summary>
    public const string SemanticValidatorId = "dataset-manifest-content-authority-v1";

    /// <summary>
    /// Validate that every declared content hash is covered by source evidence.
    /// </summary>
    /// <param name="value">The DatasetManifest JSON value to validate.</param>
    /// <returns>
    /// <see langword="true"/> when the semantic evidence relation is valid;
    /// otherwise <see langword="false"/>.
    /// </returns>
    public static bool IsSemanticallyValid(JsonElement value)
    {
        if (value.ValueKind != JsonValueKind.Object)
        {
            return false;
        }

        if (
            !value.TryGetProperty("content_hashes", out var contentHashes)
            || contentHashes.ValueKind != JsonValueKind.Array
            || contentHashes.GetArrayLength() == 0
        )
        {
            return false;
        }

        var declared = new HashSet<string>(StringComparer.Ordinal);
        foreach (var item in contentHashes.EnumerateArray())
        {
            if (item.ValueKind != JsonValueKind.String)
            {
                return false;
            }

            var digest = item.GetString();
            if (string.IsNullOrEmpty(digest) || !declared.Add(digest))
            {
                return false;
            }
        }

        if (
            !value.TryGetProperty("source_evidence", out var sourceEvidence)
            || sourceEvidence.ValueKind != JsonValueKind.Array
            || sourceEvidence.GetArrayLength() == 0
        )
        {
            return false;
        }

        var referenced = new HashSet<string>(StringComparer.Ordinal);
        foreach (var evidence in sourceEvidence.EnumerateArray())
        {
            if (
                evidence.ValueKind != JsonValueKind.Object
                || !evidence.TryGetProperty("sha256", out var sha)
                || sha.ValueKind != JsonValueKind.String
            )
            {
                return false;
            }

            var digest = sha.GetString();
            if (string.IsNullOrEmpty(digest))
            {
                return false;
            }
            referenced.Add(digest);
        }

        return declared.IsSubsetOf(referenced);
    }
}
