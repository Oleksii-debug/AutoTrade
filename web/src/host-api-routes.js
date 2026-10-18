(() => {
  "use strict";

  // AUTO-GENERATED from contracts/openapi/host-api.yaml. DO NOT EDIT.
  // Run python tools/generate_host_api_routes.py to regenerate.
  const ROUTES = Object.freeze({
    getState: '/api/v1/state',
    submitCommand: '/api/v1/commands',
    getOperation: '/api/v1/operations/{operation_id}',
    streamEvents: '/api/v1/events',
    getHealth: '/api/v1/health',
    getHistoricalStatus: '/api/v1/historical/status',
    getHistoricalResults: '/api/v1/historical/results',
    getHistoricalTrades: '/api/v1/historical/trades',
    startHistorical: '/api/v1/historical/start',
    pauseHistorical: '/api/v1/historical/pause',
    stopHistorical: '/api/v1/historical/stop',
    resumeHistorical: '/api/v1/historical/resume',
    exportHistorical: '/api/v1/historical/export',
    downloadLocalDiagnostics: '/api/v1/local-diagnostics',
    downloadTechnicalDiagnostics: '/api/v1/technical-diagnostics',
    downloadAgentReport: '/api/v1/agent-report'
  });

  function encodePathSegment(raw) {
    return encodeURIComponent(raw).replace(
      /[!'()*]/g,
      (character) =>
        "%" + character.charCodeAt(0).toString(16).toUpperCase()
    );
  }

  function route(operationId, parameters = {}) {
    const template = ROUTES[operationId];
    if (typeof template !== "string") {
      throw new RangeError(
        "Unknown host API operation: " + String(operationId));
    }
    let value = template;
    const required = [
      ...template.matchAll(/\{([A-Za-z_][A-Za-z0-9_]*)\}/g)
    ].map((match) => match[1]);
    for (const name of required) {
      const raw = parameters[name];
      if (
        typeof raw !== "string" ||
        raw.trim() === "" ||
        raw !== raw.trim()
      ) {
        throw new TypeError(
          "Host API route parameter " + name +
          " is required and must be canonical");
      }
      value = value.replace("{" + name + "}", encodePathSegment(raw));
    }
    if (/\{|\}/.test(value)) {
      throw new Error("Host API route template was not fully resolved");
    }
    return value;
  }

  Object.defineProperty(window, "AutoTradeHostApi", {
    value: Object.freeze({routes: ROUTES, route}),
    writable: false,
    configurable: false,
    enumerable: false
  });
})();
