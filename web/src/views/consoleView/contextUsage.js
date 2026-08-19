function record(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : null;
}

function nonNegative(value) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : 0;
}

function metadata(currentUsage, defaults = {}) {
  const current = record(currentUsage) || {};
  const fallback = record(defaults) || {};
  return {
    compactTriggerTokens: nonNegative(current.compactTriggerTokens || fallback.compactTriggerTokens),
    manualMinPercent: nonNegative(current.manualMinPercent ?? fallback.manualMinPercent ?? 50),
  };
}

export function resolveContextUsage(serverUsage, legacyTokens = 0) {
  const server = record(serverUsage);
  if (server && typeof server.known === "boolean") {
    const meta = metadata(server);
    const tokens = server.known ? nonNegative(server.tokens) : 0;
    return {
      ...server,
      ...meta,
      known: server.known,
      tokens,
      percent: server.known && meta.compactTriggerTokens > 0
        ? tokens * 100 / meta.compactTriggerTokens
        : null,
      authoritative: true,
    };
  }
  const tokens = nonNegative(legacyTokens);
  return {
    known: tokens > 0,
    tokens,
    compactTriggerTokens: 0,
    percent: null,
    manualMinPercent: 50,
    authoritative: false,
  };
}

export function mergeStatsContextUsage(currentUsage, statsUsage, defaults = {}) {
  const stats = record(statsUsage);
  if (!stats || stats.available !== true || typeof stats.known !== "boolean") {
    return currentUsage;
  }
  const current = record(currentUsage) || {};
  const meta = metadata(current, defaults);
  const tokens = stats.known ? nonNegative(stats.tokens) : 0;
  return {
    ...current,
    ...meta,
    known: stats.known,
    tokens,
    percent: stats.known && meta.compactTriggerTokens > 0
      ? tokens * 100 / meta.compactTriggerTokens
      : null,
  };
}

export function invalidateContextUsage(currentUsage, defaults = {}) {
  const current = record(currentUsage) || {};
  return {
    ...current,
    ...metadata(current, defaults),
    known: false,
    tokens: 0,
    percent: null,
  };
}
