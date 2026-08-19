import test from "node:test";
import assert from "node:assert/strict";

import {
  invalidateContextUsage,
  mergeStatsContextUsage,
  resolveContextUsage,
} from "./contextUsage.js";

test("authoritative unknown context never revives stale legacy tokens", () => {
  const resolved = resolveContextUsage({
    known: false,
    tokens: 0,
    compactTriggerTokens: 1000,
    manualMinPercent: 50,
  }, 750);

  assert.equal(resolved.known, false);
  assert.equal(resolved.tokens, 0);
  assert.equal(resolved.authoritative, true);
});

test("legacy exact usage remains visible during rolling frontend/backend upgrades", () => {
  const resolved = resolveContextUsage(null, 625);

  assert.equal(resolved.known, true);
  assert.equal(resolved.tokens, 625);
  assert.equal(resolved.authoritative, false);
});

test("live provider stats replace state and a successful manual compaction invalidates it", () => {
  const initial = {
    known: false,
    tokens: 0,
    compactTriggerTokens: 1000,
    manualMinPercent: 50,
  };
  const live = mergeStatsContextUsage(initial, {
    available: true,
    known: true,
    tokens: 600,
  });

  assert.equal(live.known, true);
  assert.equal(live.tokens, 600);
  assert.equal(live.percent, 60);

  const compacted = invalidateContextUsage(live);
  assert.equal(compacted.known, false);
  assert.equal(compacted.tokens, 0);
  assert.equal(compacted.percent, null);
});

test("stats without a completed provider call cannot overwrite current context", () => {
  const current = {known: true, tokens: 400, compactTriggerTokens: 1000};
  assert.equal(
    mergeStatsContextUsage(current, {available: false, known: false, tokens: 0}),
    current,
  );
});
