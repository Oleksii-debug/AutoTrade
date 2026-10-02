using System.Net;
using System.Net.Http;
using System.Net.WebSockets;
using System.Security.Cryptography;

namespace AutoTrade.Host;

internal sealed class ProviderOriginPrivateFrame : IDisposable
{
    private byte[] _frameBytes;
    private bool _disposed;

    internal ProviderOriginPrivateFrame(
        byte[] frameBytes,
        ProviderPrivateFrameReceipt receipt)
    {
        ArgumentNullException.ThrowIfNull(frameBytes);
        ArgumentNullException.ThrowIfNull(receipt);
        if (frameBytes.Length == 0)
        {
            throw new ProviderIssuerAuthorityException(
                "provider-origin frame must retain exact non-empty bytes");
        }
        _frameBytes = frameBytes;
        Receipt = receipt;
    }

    internal ReadOnlySpan<byte> FrameBytes
    {
        get
        {
            if (_disposed)
            {
                throw new ObjectDisposedException(nameof(ProviderOriginPrivateFrame));
            }
            return _frameBytes;
        }
    }

    public ProviderPrivateFrameReceipt Receipt { get; }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }
        _disposed = true;
        CryptographicOperations.ZeroMemory(_frameBytes);
        _frameBytes = Array.Empty<byte>();
        GC.SuppressFinalize(this);
    }
}

/// <summary>
/// Concrete no-retry Bybit private WebSocket composition. It owns the actual
/// ClientWebSocket instance, disables ambient proxy routing, resolves the exact
/// DPAPI credential generation only after current C+Q admission, authenticates,
/// subscribes, rechecks current authority, then asks the process-private issuer
/// to bind the exact handshake transcript. It never places orders.
/// </summary>
internal sealed class BybitPrivateWebSocketClient : IAsyncDisposable
{
    private const int ControlTimeoutSeconds = 15;
    private const int MaxFrameBytes = 1024 * 1024;
    private static readonly TimeSpan HeartbeatInterval = TimeSpan.FromSeconds(20);

    private readonly ClientWebSocket _socket;
    private readonly HttpMessageInvoker _httpInvoker;
    private readonly ProviderIssuerAuthority _issuer;
    private readonly IProviderCurrentAuthorityBoundary _currentAuthority;
    private readonly ProviderCurrentRouteAuthority _routeAuthority;
    private readonly ProviderAuthenticatedConnectionBinding _connection;
    private DateTimeOffset _lastOutboundUtc;
    private bool _disposed;

    private BybitPrivateWebSocketClient(
        ClientWebSocket socket,
        HttpMessageInvoker httpInvoker,
        ProviderIssuerAuthority issuer,
        IProviderCurrentAuthorityBoundary currentAuthority,
        ProviderCurrentRouteAuthority routeAuthority,
        ProviderAuthenticatedConnectionBinding connection,
        DateTimeOffset lastOutboundUtc)
    {
        _socket = socket;
        _httpInvoker = httpInvoker;
        _issuer = issuer;
        _currentAuthority = currentAuthority;
        _routeAuthority = routeAuthority;
        _connection = connection;
        _lastOutboundUtc = lastOutboundUtc;
    }

    public ProviderAuthenticatedConnectionBinding ConnectionBinding => _connection;

    public static async Task<BybitPrivateWebSocketClient> ConnectAsync(
        ProviderConnectionRequest request,
        IProviderCurrentAuthorityBoundary currentAuthority,
        IProviderCredentialMaterialSource credentialSource,
        ProviderIssuerAuthority issuer,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(request);
        ArgumentNullException.ThrowIfNull(currentAuthority);
        ArgumentNullException.ThrowIfNull(credentialSource);
        ArgumentNullException.ThrowIfNull(issuer);

        ProviderCurrentRouteAuthority route = currentAuthority.RequireCurrent(request);
        RequireRequestMatchesRoute(request, route);
        BybitPrivateWebSocketProtocol.RequireRoute(route);
        currentAuthority.RequireStillCurrent(route);

        using ResolvedProviderCredential resolved = credentialSource.Resolve(
            route,
            request.CredentialHandleId,
            BybitPrivateWebSocketProtocol.CredentialPurpose);
        using BybitCredentialMaterial credential =
            BybitCredentialMaterial.Parse(resolved.SecretBytes);

        ProviderConnectionSubject subject = new(
            ProviderId: route.ProviderId,
            AccountId: route.AccountId,
            RuntimeEnvironment: route.RuntimeEnvironment,
            ProviderEnvironment: route.ProviderEnvironment,
            Endpoint: route.Endpoint,
            TopicId: route.TopicId,
            SubscriptionIdentity: route.SubscriptionIdentity,
            CredentialHandleId: resolved.HandleId,
            CredentialGeneration: resolved.Generation,
            CredentialPurpose: resolved.Purpose,
            CapabilityId: route.CapabilityId,
            QualificationId: route.QualificationId,
            QualificationBuildId: route.QualificationBuildId,
            NetworkPolicyIdentity: route.NetworkPolicyIdentity,
            TransportIdentity: route.TransportIdentity);
        ProviderIssuerAuthority.RequireSubject(subject);

        SocketsHttpHandler handler = new()
        {
            UseProxy = false,
            UseCookies = false,
            AllowAutoRedirect = false,
            AutomaticDecompression = DecompressionMethods.None,
        };
        HttpMessageInvoker httpInvoker = new(handler, disposeHandler: true);
        ClientWebSocket socket = new();
        try
        {
            using CancellationTokenSource controlTimeout =
                CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            controlTimeout.CancelAfter(TimeSpan.FromSeconds(ControlTimeoutSeconds));
            await socket.ConnectAsync(
                new Uri(route.Endpoint, UriKind.Absolute),
                httpInvoker,
                controlTimeout.Token).ConfigureAwait(false);

            currentAuthority.RequireStillCurrent(route);
            DateTimeOffset authTime = DateTimeOffset.UtcNow;
            byte[] authRequest =
                BybitPrivateWebSocketProtocol.BuildAuthenticationRequest(
                    credential,
                    authTime);
            byte[] authResponse = Array.Empty<byte>();
            byte[] subscriptionRequest = Array.Empty<byte>();
            byte[] subscriptionResponse = Array.Empty<byte>();
            byte[] authenticationTranscript = Array.Empty<byte>();
            byte[] subscriptionTranscript = Array.Empty<byte>();
            try
            {
                await SendTextAsync(socket, authRequest, controlTimeout.Token)
                    .ConfigureAwait(false);
                authResponse = await ReceiveTextMessageAsync(
                    socket,
                    maxBytes: 32 * 1024,
                    controlTimeout.Token).ConfigureAwait(false);
                BybitAuthenticationAcknowledgement auth =
                    BybitPrivateWebSocketProtocol.ParseAuthenticationAcknowledgement(
                        authResponse);

                currentAuthority.RequireStillCurrent(route);
                subscriptionRequest =
                    BybitPrivateWebSocketProtocol.BuildSubscriptionRequest();
                await SendTextAsync(
                    socket,
                    subscriptionRequest,
                    controlTimeout.Token).ConfigureAwait(false);
                subscriptionResponse = await ReceiveTextMessageAsync(
                    socket,
                    maxBytes: 32 * 1024,
                    controlTimeout.Token).ConfigureAwait(false);
                _ = BybitPrivateWebSocketProtocol.ParseSubscriptionAcknowledgement(
                    subscriptionResponse,
                    auth.ConnectionId);

                currentAuthority.RequireStillCurrent(route);
                authenticationTranscript =
                    BybitPrivateWebSocketProtocol.CanonicalTranscript(
                        authRequest,
                        authResponse);
                subscriptionTranscript =
                    BybitPrivateWebSocketProtocol.CanonicalTranscript(
                        subscriptionRequest,
                        subscriptionResponse);
                ProviderAuthenticatedConnectionBinding binding =
                    issuer.IssueAuthenticatedConnection(
                        subject,
                        authenticationTranscript,
                        subscriptionTranscript,
                        DateTimeOffset.UtcNow);
                return new BybitPrivateWebSocketClient(
                    socket,
                    httpInvoker,
                    issuer,
                    currentAuthority,
                    route,
                    binding,
                    DateTimeOffset.UtcNow);
            }
            finally
            {
                CryptographicOperations.ZeroMemory(authRequest);
                if (authResponse.Length != 0)
                {
                    CryptographicOperations.ZeroMemory(authResponse);
                }
                if (subscriptionRequest.Length != 0)
                {
                    CryptographicOperations.ZeroMemory(subscriptionRequest);
                }
                if (subscriptionResponse.Length != 0)
                {
                    CryptographicOperations.ZeroMemory(subscriptionResponse);
                }
                if (authenticationTranscript.Length != 0)
                {
                    CryptographicOperations.ZeroMemory(authenticationTranscript);
                }
                if (subscriptionTranscript.Length != 0)
                {
                    CryptographicOperations.ZeroMemory(subscriptionTranscript);
                }
            }
        }
        catch
        {
            socket.Abort();
            socket.Dispose();
            httpInvoker.Dispose();
            throw;
        }
    }

    public async Task<ProviderOriginPrivateFrame> ReceiveFrameAsync(
        CancellationToken cancellationToken)
    {
        ThrowIfDisposed();
        _currentAuthority.RequireStillCurrent(_routeAuthority);
        byte[] frame = await ReceiveWithHeartbeatAsync(cancellationToken)
            .ConfigureAwait(false);
        try
        {
            _currentAuthority.RequireStillCurrent(_routeAuthority);
            BybitPrivateWebSocketProtocol.RequirePrivateExecutionFrame(frame);
            _currentAuthority.RequireStillCurrent(_routeAuthority);
            ProviderPrivateFrameReceipt receipt = _issuer.IssuePrivateFrameReceipt(
                _connection,
                frame,
                DateTimeOffset.UtcNow);
            return new ProviderOriginPrivateFrame(frame, receipt);
        }
        catch
        {
            CryptographicOperations.ZeroMemory(frame);
            throw;
        }
    }

    public async ValueTask DisposeAsync()
    {
        if (_disposed)
        {
            return;
        }
        _disposed = true;
        try
        {
            if (_socket.State == WebSocketState.Open
                || _socket.State == WebSocketState.CloseReceived)
            {
                using CancellationTokenSource closeTimeout = new(
                    TimeSpan.FromSeconds(2));
                await _socket.CloseOutputAsync(
                    WebSocketCloseStatus.NormalClosure,
                    string.Empty,
                    closeTimeout.Token).ConfigureAwait(false);
            }
        }
        catch
        {
            _socket.Abort();
        }
        finally
        {
            _socket.Dispose();
            _httpInvoker.Dispose();
        }
        GC.SuppressFinalize(this);
    }

    private async Task<byte[]> ReceiveWithHeartbeatAsync(
        CancellationToken cancellationToken)
    {
        Task<byte[]> receiveTask = ReceiveTextMessageAsync(
            _socket,
            MaxFrameBytes,
            cancellationToken);
        while (true)
        {
            TimeSpan untilHeartbeat =
                (_lastOutboundUtc + HeartbeatInterval) - DateTimeOffset.UtcNow;
            if (untilHeartbeat <= TimeSpan.Zero)
            {
                byte[] heartbeat = BybitPrivateWebSocketProtocol.BuildHeartbeatRequest();
                try
                {
                    await SendTextAsync(_socket, heartbeat, cancellationToken)
                        .ConfigureAwait(false);
                    _lastOutboundUtc = DateTimeOffset.UtcNow;
                }
                finally
                {
                    CryptographicOperations.ZeroMemory(heartbeat);
                }
                continue;
            }

            Task heartbeatDeadline = Task.Delay(untilHeartbeat, cancellationToken);
            Task completed = await Task.WhenAny(receiveTask, heartbeatDeadline)
                .ConfigureAwait(false);
            if (completed == receiveTask)
            {
                byte[] frame = await receiveTask.ConfigureAwait(false);
                if (BybitPrivateWebSocketProtocol.IsHeartbeatAcknowledgement(frame))
                {
                    CryptographicOperations.ZeroMemory(frame);
                    receiveTask = ReceiveTextMessageAsync(
                        _socket,
                        MaxFrameBytes,
                        cancellationToken);
                    continue;
                }
                return frame;
            }

            await heartbeatDeadline.ConfigureAwait(false);
            byte[] ping = BybitPrivateWebSocketProtocol.BuildHeartbeatRequest();
            try
            {
                await SendTextAsync(_socket, ping, cancellationToken)
                    .ConfigureAwait(false);
                _lastOutboundUtc = DateTimeOffset.UtcNow;
            }
            finally
            {
                CryptographicOperations.ZeroMemory(ping);
            }
        }
    }

    private static async Task SendTextAsync(
        ClientWebSocket socket,
        byte[] payload,
        CancellationToken cancellationToken)
    {
        if (socket.State != WebSocketState.Open)
        {
            throw new ProviderIssuerAuthorityException(
                "provider WebSocket is not open");
        }
        await socket.SendAsync(
            payload,
            WebSocketMessageType.Text,
            endOfMessage: true,
            cancellationToken).ConfigureAwait(false);
    }

    private static async Task<byte[]> ReceiveTextMessageAsync(
        ClientWebSocket socket,
        int maxBytes,
        CancellationToken cancellationToken)
    {
        if (maxBytes <= 0)
        {
            throw new ArgumentOutOfRangeException(nameof(maxBytes));
        }
        using MemoryStream stream = new();
        byte[] buffer = new byte[16 * 1024];
        try
        {
            while (true)
            {
                ValueWebSocketReceiveResult result = await socket.ReceiveAsync(
                    buffer.AsMemory(),
                    cancellationToken).ConfigureAwait(false);
                if (result.MessageType == WebSocketMessageType.Close)
                {
                    throw new ProviderIssuerAuthorityException(
                        "provider WebSocket closed before a complete message");
                }
                if (result.MessageType != WebSocketMessageType.Text)
                {
                    throw new ProviderIssuerAuthorityException(
                        "provider WebSocket returned a non-text message");
                }
                if (result.Count > 0)
                {
                    if (stream.Length + result.Count > maxBytes)
                    {
                        throw new ProviderIssuerAuthorityException(
                            "provider WebSocket message exceeds configured bound");
                    }
                    stream.Write(buffer, 0, result.Count);
                }
                if (result.EndOfMessage)
                {
                    byte[] value = stream.ToArray();
                    if (value.Length == 0)
                    {
                        throw new ProviderIssuerAuthorityException(
                            "provider WebSocket returned an empty text message");
                    }
                    return value;
                }
            }
        }
        finally
        {
            CryptographicOperations.ZeroMemory(buffer);
        }
    }

    private static void RequireRequestMatchesRoute(
        ProviderConnectionRequest request,
        ProviderCurrentRouteAuthority route)
    {
        ProviderIssuerAuthority.ExactText(request.ProviderId, nameof(request.ProviderId));
        ProviderIssuerAuthority.ExactText(request.AccountId, nameof(request.AccountId));
        ProviderIssuerAuthority.ExactText(
            request.RuntimeEnvironment,
            nameof(request.RuntimeEnvironment));
        ProviderIssuerAuthority.ExactText(
            request.ProviderEnvironment,
            nameof(request.ProviderEnvironment));
        ProviderIssuerAuthority.ExactText(request.TopicId, nameof(request.TopicId));
        ProviderIssuerAuthority.ExactText(
            request.CredentialHandleId,
            nameof(request.CredentialHandleId));
        if (!string.Equals(request.ProviderId, route.ProviderId, StringComparison.Ordinal)
            || !string.Equals(request.AccountId, route.AccountId, StringComparison.Ordinal)
            || !string.Equals(
                request.RuntimeEnvironment,
                route.RuntimeEnvironment,
                StringComparison.Ordinal)
            || !string.Equals(
                request.ProviderEnvironment,
                route.ProviderEnvironment,
                StringComparison.Ordinal)
            || !string.Equals(request.TopicId, route.TopicId, StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                "current provider authority does not match connection request scope");
        }
    }

    private void ThrowIfDisposed()
    {
        if (_disposed)
        {
            throw new ObjectDisposedException(nameof(BybitPrivateWebSocketClient));
        }
        if (_socket.State != WebSocketState.Open)
        {
            throw new ProviderIssuerAuthorityException(
                "provider WebSocket is not open");
        }
    }
}
