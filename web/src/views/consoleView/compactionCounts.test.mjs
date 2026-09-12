import assert from "node:assert/strict";
import test from "node:test";
import {contextCompactionView, compactAgentStepActivityLines} from "./agentPlanPresentation.js";

const base = {name: "ContextCompaction", scope: "root", strategy: "sliding_window", compactionId: "window-1"};

for (const status of ["running", "failed", "completed"]) {
  test(`window ${status} without selection counts does not invent zero groups`, () => {
    const view = contextCompactionView({...base, status, beforeEstimateTokens: 23792});
    assert.equal(view.removedBatches, null);
    assert.equal(view.retainedBatches, null);
    assert.doesNotMatch(view.output, /移出|保留 \d+ 组/);
    assert.doesNotMatch(view.cardPreview, /移出|上下文估算/);
    assert.match(view.output, /23,792/);
  });
}

test("failed selection never describes candidate counts as committed removal", () => {
  const view = contextCompactionView({...base, status: "failed", removedBatches: 9, retainedBatches: 4,
    error: "Required input exceeds configured compression threshold"});
  assert.doesNotMatch(view.output, /移出|保留 \d+ 组/);
  assert.match(view.output, /Required input exceeds configured compression threshold/);
  assert.equal(view.failed, true);
});

test("completed selection distinguishes measured zero from missing or malformed counts", () => {
  const valid = contextCompactionView({...base, status: "completed", removedBatches: 0, retainedBatches: 5});
  assert.match(valid.output, /移出 0 组 · 保留 5 组完整记录/);
  assert.equal(valid.removedBatches, 0);
  assert.equal(valid.retainedBatches, 5);
  for (const value of [undefined, null, "", "not-a-count", -1, 1.5, false]) {
    const view = contextCompactionView({...base, status: "completed", removedBatches: value, retainedBatches: 5});
    assert.equal(view.removedBatches, null);
    assert.doesNotMatch(view.output, /移出|保留 \d+ 组/);
  }
});

test("Agent compression failure replaces running card without fake zero counts", () => {
  const lines = compactAgentStepActivityLines([
    {seq: 1, kind: "model_context_compaction_started", detail: {...base, scope: "agent", status: "running", beforeEstimateTokens: 23792}},
    {seq: 2, kind: "model_context_compaction_failed", detail: {...base, scope: "agent", status: "failed", error: "Budget failure; original context preserved"}},
  ]);
  assert.equal(lines.length, 1);
  assert.equal(lines[0].compaction.failed, true);
  assert.doesNotMatch(lines[0].compaction.output, /移出|保留 \d+ 组/);
  assert.match(lines[0].compaction.output, /original context preserved/);
});
