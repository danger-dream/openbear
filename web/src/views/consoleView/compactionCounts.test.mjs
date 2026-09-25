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
    assert.doesNotMatch(view.cardPreview, /移出|保留 \d+ 组/);
    if (status === "completed") assert.equal(view.cardPreview, "23.79k → 未记录");
    else {
      assert.match(view.cardPreview, /23,792/);
      assert.match(view.cardPreview, /估算/);
    }
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
  assert.doesNotMatch(valid.output, /移出|保留 \d+ 组/);
  assert.match(valid.cardPreview, /未记录 → 未记录/);
  assert.equal(valid.removedBatches, 0);
  assert.equal(valid.retainedBatches, 5);
  for (const value of [undefined, null, "", "not-a-count", -1, 1.5, false]) {
    const view = contextCompactionView({...base, status: "completed", removedBatches: value, retainedBatches: 5});
    assert.equal(view.removedBatches, null);
    assert.doesNotMatch(view.output, /移出|保留 \d+ 组/);
  }
});

test("window preview distinguishes known zero from missing tokens and never presents failed candidates as applied", () => {
  const complete = contextCompactionView({...base, status:"completed", beforeEstimateTokens:1000, afterEstimateTokens:0});
  assert.equal(complete.cardPreview, "1k → 0");
  const roundTrip = contextCompactionView({...complete, name:"ContextCompaction"});
  assert.equal(roundTrip.cardPreview,complete.cardPreview);
  const running = contextCompactionView({...base,status:"running",beforeEstimateTokens:1000});
  assert.match(running.cardPreview,/1,000 → 处理中/);
  const failed = contextCompactionView({...base,status:"failed",beforeEstimateTokens:1000,afterEstimateTokens:42});
  assert.match(failed.cardPreview,/原上下文估算：1,000 Tokens/);
  assert.doesNotMatch(failed.cardPreview,/→|42/);
});

test("completed window preview uses compact tokens while details retain exact estimates", () => {
  const view = contextCompactionView({...base, status:"completed", beforeEstimateTokens:270071, afterEstimateTokens:38056, durationMs:600});
  assert.equal(view.cardPreview, "270.07k → 38.06k · 0.6s");
  assert.doesNotMatch(view.cardPreview, /滑动窗口|Tokens|估算/);
  assert.match(view.output, /Tokens（估算）：270,071 → 38,056/);
  assert.equal(view.afterEstimateTokens, 38056);
  assert.equal(contextCompactionView({...base, beforeEstimateTokens:3800, afterEstimateTokens:999}).cardPreview, "3.8k → 999");
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
