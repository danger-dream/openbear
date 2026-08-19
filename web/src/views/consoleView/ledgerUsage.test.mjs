import test from "node:test";
import assert from "node:assert/strict";

import {
  applyLedgerUsageSnapshot,
  ledgerTokenParts,
  normalizeLedgerUsageBaseline,
  totalSessionDurationMs,
} from "./ledgerUsage.js";

const snapshot = (ledgerRevision, inputTokens, outputTokens, cacheReadTokens, cacheWriteTokens, costUsd) => ({
  ledgerRevision,
  inputTokens,
  outputTokens,
  cacheReadTokens,
  cacheWriteTokens,
  costUsd,
});

test("header token parts use only the absolute sessions ledger", () => {
  const usage = normalizeLedgerUsageBaseline({
    ledger_revision: 7,
    input_tokens: 100,
    output_tokens: 20,
    cache_read_tokens: 30,
    cache_write_tokens: 5,
    cost_usd: 0.25,
  });

  assert.deepEqual(ledgerTokenParts(usage), {input: 135, output: 20, cache: 35});
  assert.equal(usage.cost_usd, 0.25);
});

test("ledger snapshots reject old revisions and apply equal revisions idempotently", () => {
  const baseline = normalizeLedgerUsageBaseline({ledger_revision: 9, input_tokens: 90, cost_usd: 0.9});
  const old = applyLedgerUsageSnapshot(baseline, snapshot(8, 800, 80, 8, 8, 8));
  assert.equal(old.applied, false);
  assert.deepEqual(old.usage, baseline);

  const equal = applyLedgerUsageSnapshot(baseline, snapshot(9, 90, 9, 4, 1, 0.9));
  assert.equal(equal.applied, true);
  const replay = applyLedgerUsageSnapshot(equal.usage, snapshot(9, 90, 9, 4, 1, 0.9));
  assert.equal(replay.applied, true);
  assert.deepEqual(replay.usage, equal.usage);
});

test("concurrent Agent snapshots converge on the highest ledger revision without addition", () => {
  let usage = normalizeLedgerUsageBaseline({ledger_revision: 0});
  usage = applyLedgerUsageSnapshot(usage, snapshot(12, 120, 12, 20, 2, 1.2)).usage;
  usage = applyLedgerUsageSnapshot(usage, snapshot(11, 110, 11, 10, 1, 1.1)).usage;
  assert.deepEqual(
    {
      revision: usage.ledger_revision,
      input: usage.input_tokens,
      output: usage.output_tokens,
      cacheRead: usage.cache_read_tokens,
      cacheWrite: usage.cache_write_tokens,
      cost: usage.cost_usd,
    },
    {revision: 12, input: 120, output: 12, cacheRead: 20, cacheWrite: 2, cost: 1.2},
  );
});

test("a complete state replaces the baseline and may lower totals after reset", () => {
  const live = applyLedgerUsageSnapshot(
    normalizeLedgerUsageBaseline({ledger_revision: 20}),
    snapshot(21, 210, 21, 20, 1, 2.1),
  ).usage;
  assert.equal(live.input_tokens, 210);

  const resetState = normalizeLedgerUsageBaseline({
    ledger_revision: 2,
    input_tokens: 10,
    output_tokens: 1,
    cache_read_tokens: 0,
    cache_write_tokens: 0,
    cost_usd: 0.1,
  });
  assert.equal(resetState.ledger_revision, 2);
  assert.equal(resetState.input_tokens, 10);
  assert.equal(resetState.cost_usd, 0.1);
});

test("missing provider usage and missing pricing never fabricate totals", () => {
  const noUsage = applyLedgerUsageSnapshot(
    normalizeLedgerUsageBaseline({ledger_revision: 3, input_tokens: 4, cost_usd: 0}),
    snapshot(4, 4, 0, 0, 0, 0),
  );
  assert.equal(noUsage.usage.input_tokens, 4);
  assert.equal(noUsage.usage.output_tokens, 0);
  assert.equal(noUsage.usage.cost_usd, 0);

  const unpriced = applyLedgerUsageSnapshot(noUsage.usage, snapshot(5, 14, 2, 1, 0, 0));
  assert.equal(unpriced.usage.input_tokens, 14);
  assert.equal(unpriced.usage.output_tokens, 2);
  assert.equal(unpriced.usage.cost_usd, 0);
});

test("full-session timeline duration is unchanged as earlier pages are prepended", () => {
  const timelineTotalDurationMs = 120_000;
  const ledgerUsage = normalizeLedgerUsageBaseline({stat_total_time_ms_sum: 900_000});
  const initialPage = Array.from({length: 2}, (_, index) => ({stats: {durationMs: 1000 + index}}));
  const afterPrepend = [
    ...Array.from({length: 200}, () => ({stats: {durationMs: 100}})),
    ...initialPage,
  ];

  assert.equal(totalSessionDurationMs({turns: initialPage, timelineTotalDurationMs, ledgerUsage}), 120_000);
  assert.equal(totalSessionDurationMs({turns: afterPrepend, timelineTotalDurationMs, ledgerUsage}), 120_000);
});

test("timeline aggregate wins over partial turns, 500 model rows, and the different ledger metric", () => {
  const cappedRows = Array.from({length: 500}, () => ({total_time_ms: 20}));
  assert.equal(totalSessionDurationMs({
    turns: [{stats: {durationMs: 30_000}}],
    modelCalls: cappedRows,
    timelineTotalDurationMs: 29_532_876,
    ledgerUsage: {stat_total_time_ms_sum: 66_639_090},
  }), 29_532_876);
});

test("live duration can exceed the timeline aggregate without reviving the ledger proxy", () => {
  assert.equal(totalSessionDurationMs({
    liveMs: 6000,
    timelineTotalDurationMs: 5000,
    ledgerUsage: {stat_total_time_ms_sum: 8000},
  }), 6000);
});

test("missing or zero timeline fields retain old-backend and legacy fallbacks", () => {
  assert.equal(totalSessionDurationMs(), 0);
  assert.equal(totalSessionDurationMs({liveMs: 2345}), 2345);
  assert.equal(totalSessionDurationMs({turns: [{stats: {durationMs: 3456}}]}), 3456);
  assert.equal(totalSessionDurationMs({modelCalls: [{total_time_ms: 4567}]}), 4567);
  assert.equal(totalSessionDurationMs({ledgerUsage: {stat_total_time_ms_sum: 5000}}), 5000);
  assert.equal(totalSessionDurationMs({
    timelineTotalDurationMs: 0,
    ledgerUsage: {stat_total_time_ms_sum: 5000},
  }), 5000);
});
