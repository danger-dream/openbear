function nonNegativeInteger(value) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? Math.floor(number) : 0;
}

function nonNegativeNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : 0;
}

export function normalizeLedgerUsageBaseline(usage = {}) {
  const base = usage && typeof usage === "object" && !Array.isArray(usage) ? usage : {};
  return {
    ...base,
    ledger_revision: nonNegativeInteger(base.ledger_revision),
    input_tokens: nonNegativeInteger(base.input_tokens),
    output_tokens: nonNegativeInteger(base.output_tokens),
    cache_read_tokens: nonNegativeInteger(base.cache_read_tokens),
    cache_write_tokens: nonNegativeInteger(base.cache_write_tokens),
    cost_usd: nonNegativeNumber(base.cost_usd),
  };
}

export function ledgerTokenParts(usage = {}) {
  const ledger = normalizeLedgerUsageBaseline(usage);
  const cache = ledger.cache_read_tokens + ledger.cache_write_tokens;
  return {
    input: ledger.input_tokens + cache,
    output: ledger.output_tokens,
    cache,
  };
}

/**
 * Preserve the baseline Math.max fallbacks without adding overlapping totals.
 * A positive timeline aggregate is the full turns[].stats.durationMs sum and
 * therefore replaces (rather than competes with) the model-call ledger proxy.
 * Zero/missing values keep old-backend and operation-less legacy fallbacks.
 */
export function totalSessionDurationMs({
  turns = [],
  modelCalls = [],
  liveMs = 0,
  timelineTotalDurationMs = 0,
  ledgerUsage = {},
} = {}) {
  const turnMs = turns.reduce((sum, turn) => sum + Number(turn?.stats?.durationMs || 0), 0);
  const rowMs = modelCalls.reduce((sum, row) => sum + Number(row?.total_time_ms || 0), 0);
  const timelineMs = nonNegativeInteger(timelineTotalDurationMs);
  const legacyLedgerMs = timelineMs > 0
    ? 0
    : nonNegativeInteger(ledgerUsage?.stat_total_time_ms_sum);
  return Math.max(turnMs, rowMs, Number(liveMs || 0), timelineMs, legacyLedgerMs);
}

export function applyLedgerUsageSnapshot(currentUsage = {}, snapshot = null) {
  const current = normalizeLedgerUsageBaseline(currentUsage);
  if (!snapshot || typeof snapshot !== "object" || Array.isArray(snapshot)) {
    return {usage: current, applied: false};
  }
  const rawRevision = Number(snapshot.ledgerRevision);
  if (!Number.isFinite(rawRevision) || rawRevision < 0) {
    return {usage: current, applied: false};
  }
  const ledgerRevision = Math.floor(rawRevision);
  if (ledgerRevision < current.ledger_revision) {
    return {usage: current, applied: false};
  }
  return {
    applied: true,
    usage: {
      ...current,
      ledger_revision: ledgerRevision,
      input_tokens: nonNegativeInteger(snapshot.inputTokens),
      output_tokens: nonNegativeInteger(snapshot.outputTokens),
      cache_read_tokens: nonNegativeInteger(snapshot.cacheReadTokens),
      cache_write_tokens: nonNegativeInteger(snapshot.cacheWriteTokens),
      cost_usd: nonNegativeNumber(snapshot.costUsd),
    },
  };
}
