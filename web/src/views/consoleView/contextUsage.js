function record(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : null;
}

function nonNegative(value) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : 0;
}

function metadata(currentUsage, defaults = {}) {
  const current = record(currentUsage) || {};
  return {
    rolloverTriggerTokens: nonNegative(current.rolloverTriggerTokens || defaults.rolloverTriggerTokens),
  };
}

function older(current, incoming) {
  if (current.ownerId && incoming.ownerId && current.ownerId !== incoming.ownerId) return true;
  const version = nonNegative(incoming.windowVersion);
  const previous = nonNegative(current.windowVersion);
  if (version !== previous) return version < previous;
  return nonNegative(incoming.requestSequence) < nonNegative(current.requestSequence);
}

export function resolveContextUsage(serverUsage, legacyTokens = 0) {
  const server = record(serverUsage);
  if (server && typeof server.known === "boolean") {
    const meta = metadata(server);
    const tokens = server.known ? nonNegative(server.tokens) : 0;
    return {...server, ...meta, tokens,
      percent: server.known && meta.rolloverTriggerTokens > 0 ? tokens * 100 / meta.rolloverTriggerTokens : null,
      authoritative: true};
  }
  const tokens = nonNegative(legacyTokens);
  return {known: tokens > 0, tokens, rolloverTriggerTokens: 0, percent: null, authoritative: false};
}

export function mergeStatsContextUsage(currentUsage, statsUsage, defaults = {}) {
  const stats = record(statsUsage);
  const current = record(currentUsage) || {};
  if (!stats || stats.available !== true || typeof stats.known !== "boolean" || older(current, stats)) return currentUsage;
  const meta = metadata(stats, {...current, ...defaults});
  const tokens = stats.known ? nonNegative(stats.tokens) : 0;
  return {...current, ...stats, ...meta, tokens,
    percent: stats.known && meta.rolloverTriggerTokens > 0 ? tokens * 100 / meta.rolloverTriggerTokens : null};
}

export function invalidateContextUsage(currentUsage, defaults = {}) {
  const current = record(currentUsage) || {};
  // New window telemetry cannot invalidate a newer completed request.
  if (defaults.windowVersion != null && older(current, defaults)) return currentUsage;
  return {...current, ...defaults, ...metadata(current, defaults), known: false, tokens: 0, percent: null};
}
