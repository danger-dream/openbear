import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import {projectOperationMessages, isContextCompactionOperation} from "../../timelineProjection.js";
import {compactAgentStepActivityLines, agentCompactionActivityView, isAgentMonitorEventKind} from "./agentPlanPresentation.js";
import {invalidateContextUsage, mergeStatsContextUsage} from "./contextUsage.js";

const read = path => fs.readFileSync(new URL(path, import.meta.url), "utf8");
test("summary controls are strategy-gated and candidates belong in system settings", () => {
  const composer = read("./ConsoleComposer.vue");
  const consoleView = read("./ConsoleView.vue");
  const channels = read("../ChannelsView.vue");
  const api = read("../../api.js");
  assert.match(composer, /v-if="props.contextStrategy === 'model_summary'"/);
  assert.match(composer, /手动压缩上下文/);
  assert.doesNotMatch(consoleView, /compactConversation|serverCompacting|compactRequests|manualMinPercent/);
  assert.doesNotMatch(channels, /compressionCandidates|compressionOrder|compressionModels/);
  assert.match(channels, /vuedraggable/);
  assert.match(api, /conversationCompact:/);
  assert.doesNotMatch(api, /setCompression/);
  assert.match(api, /conversationCompaction:/);
});

test("controller rotation is a local notice, never a tool or model summary", () => {
  const op = {conversationUuid: "c", opId: "window:2", opType: "context_window", displaySeq: 2,
    lifecycle: "terminal", revision: 1, payload: {statusText: "上下文窗口已轮换", windowVersion: 2,
      ownerId: "controller:c", estimateOnly: true, afterEstimateTokens: 12345}};
  assert.equal(isContextCompactionOperation(op), false);
  const messages = projectOperationMessages([op]);
  const events = messages.flatMap(m => m.localTimeline || []);
  assert.equal(events.length, 1);
  assert.equal(events[0].kind, "live_status");
  assert.equal(events[0].status, "上下文窗口已轮换");
});

test("Agent rotation is distinct from legacy summary output and business tools", () => {
  const event = {seq: 1, kind: "model_context_window_rotated", detail: {windowVersion: 3, afterEstimateTokens: 42}};
  assert.equal(isAgentMonitorEventKind(event.kind), true);
  assert.equal(agentCompactionActivityView(event).isCompaction, false);
  const lines = compactAgentStepActivityLines([event]);
  assert.equal(lines.length, 1);
  assert.equal(lines[0].kind, "context_window");
  assert.match(lines[0].message, /v3.*待下一次请求实测/);
  assert.equal(lines[0].compactedOutput, undefined);
  assert.equal(lines[0].processStatus, undefined);
});

test("old window, old owner, late request and late rotation cannot overwrite newer actual usage", () => {
  const current = {ownerId: "a", windowVersion: 3, requestSequence: 8, known: true, tokens: 600, rolloverTriggerTokens: 1000};
  for (const stale of [{ownerId: "b", windowVersion: 5, requestSequence: 99},
    {ownerId: "a", windowVersion: 2, requestSequence: 99},
    {ownerId: "a", windowVersion: 3, requestSequence: 7}]) {
    assert.equal(mergeStatsContextUsage(current, {...stale, available: true, known: true, tokens: 999}), current);
    assert.equal(invalidateContextUsage(current, stale), current);
  }
  assert.equal(invalidateContextUsage(current, {ownerId: "a", windowVersion: 3}), current);
  const rotated = invalidateContextUsage(current, {ownerId: "a", windowVersion: 4, afterEstimateTokens: 35, estimateOnly: true});
  assert.equal(rotated.known, false);
  assert.equal(rotated.tokens, 0);
  const actual = mergeStatsContextUsage(rotated, {ownerId: "a", windowVersion: 4, requestSequence: 9, available: true, known: true, tokens: 500});
  assert.equal(actual.percent, 50);
  assert.equal(actual.tokens, 500);
});
