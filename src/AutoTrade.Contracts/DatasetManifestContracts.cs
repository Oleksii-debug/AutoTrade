using System.Text.Json;

namespace AutoTrade.Contracts;

/// <summary>
/// Provides semantic validation rules for canonical DatasetManifest contract payloads.
/// </summary>
public static class DatasetManifestContracts
{
    /// <summary>
    /// Gets the stable identifier of the DatasetManifest semantic validator.
    /// </summary>
    public const string SemanticValidatorId = "dataset-manifest-content-authority-v1";

    /// <summary>
    /// Determines whether a DatasetManifest payload satisfies the cross-field semantic
    /// requirements that cannot be expressed by JSON Schema alone.
    /// </summary>
    /// <param name="value">The DatasetManifest JSON value to validate.</param>
    /// <returns>
    /// <see langword="true"/> when every declared content hash is uniquely declared and
    /// is backed by at least one source-evidence SHA-256 reference; otherwise,
    /// <see langword="false"/>.
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
