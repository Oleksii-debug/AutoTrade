using System.Globalization;

namespace AutoTrade.Host;

public static class HostContractTime
{
    public static string FormatUtcInstant(DateTimeOffset value) =>
        value.ToUniversalTime().ToString(
            "yyyy-MM-dd'T'HH:mm:ss.fffffff'Z'",
            CultureInfo.InvariantCulture);
}
