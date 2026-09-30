using System.Text.RegularExpressions;

namespace AutoTrade.Contracts;

/// <summary>
/// AUTO-GENERATED strict admission for the language-neutral common scalar subset.
/// Run python tools/generate_common_scalar_bindings.py to regenerate.
/// </summary>
public static partial class CommonScalarContracts
{
    private static readonly HashSet<string> EnvironmentValues =
        new(StringComparer.Ordinal) { "REPLAY", "SIMULATION", "PAPER", "LIVE" };

    /// <summary>Validates a textual common scalar without numeric coercion.</summary>
    public static bool IsValid(string kind, string? value)
    {
        if (value is null)
        {
            return false;
        }

        return kind switch
        {
            "Decimal" => value.Length <= 259 && IsFullMatch(DecimalPattern(), value) && IsWithinDecimalEnvelope(value, 256, 256, 256),
            "Sequence" => IsFullMatch(SequencePattern(), value),
            "Digest" => IsFullMatch(DigestPattern(), value),
            "CurrencyId" => value.Length >= 1 && value.Length <= 32 && IsFullMatch(CurrencyIdPattern(), value),
            "UnitId" => value.Length >= 1 && value.Length <= 64 && IsFullMatch(UnitIdPattern(), value),
            "Environment" => EnvironmentValues.Contains(value),
            _ => throw new ArgumentOutOfRangeException(nameof(kind), kind, "Unsupported common scalar kind."),
        };
    }

    private static bool IsFullMatch(Regex regex, string value)
    {
        var match = regex.Match(value);
        return match.Success && match.Index == 0 && match.Length == value.Length;
    }

    private static bool IsWithinDecimalEnvelope(
        string value,
        int maxSignificantDigits,
        int maxScale,
        int maxIntegerDigits)
    {
        var start = value.StartsWith("-", StringComparison.Ordinal) ? 1 : 0;
        var dot = value.IndexOf('.', start);
        var integerEnd = dot >= 0 ? dot : value.Length;
        var integerDigits = integerEnd - start;
        var integerMagnitude =
            integerDigits == 1 && value[start] == '0' ? 0 : integerDigits;
        var scale = dot >= 0 ? value.Length - dot - 1 : 0;
        var coefficientDigits = value.Length - start - (dot >= 0 ? 1 : 0);
        var leadingCoefficientZeros = 0;
        for (var index = start; index < value.Length; index++)
        {
            if (value[index] == '.')
            {
                continue;
            }
            if (value[index] != '0')
            {
                break;
            }
            leadingCoefficientZeros++;
        }
        var significantDigits =
            leadingCoefficientZeros == coefficientDigits
                ? 1
                : coefficientDigits - leadingCoefficientZeros;
        return significantDigits <= maxSignificantDigits
            && scale <= maxScale
            && integerMagnitude <= maxIntegerDigits;
    }

    [GeneratedRegex(@"^(?:0|[1-9][0-9]*(?:\.[0-9]*[1-9])?|0\.[0-9]*[1-9]|-(?:[1-9][0-9]*(?:\.[0-9]*[1-9])?|0\.[0-9]*[1-9]))$(?![\s\S])", RegexOptions.CultureInvariant)]
    private static partial Regex DecimalPattern();

    [GeneratedRegex(@"^(0|[1-9][0-9]*)$(?![\s\S])", RegexOptions.CultureInvariant)]
    private static partial Regex SequencePattern();

    [GeneratedRegex(@"^sha256:[0-9a-f]{64}$(?![\s\S])", RegexOptions.CultureInvariant)]
    private static partial Regex DigestPattern();

    [GeneratedRegex(@"^[A-Za-z0-9._:-]+$(?![\s\S])", RegexOptions.CultureInvariant)]
    private static partial Regex CurrencyIdPattern();

    [GeneratedRegex(@"^[A-Za-z0-9._:/-]+$(?![\s\S])", RegexOptions.CultureInvariant)]
    private static partial Regex UnitIdPattern();

}
