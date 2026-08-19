import test from "node:test";
import assert from "node:assert/strict";

import {
  agentPanelDetailKey,
  decideAgentAutoOpen,
  normalizeAgentPanelIntents,
  selectLatestActiveAgentOperation,
} from "./agentPanelState.js";

const operations = [
  {opId: "agent:old", opType: "agent", taskUuid: "task-old", displaySeq: 20, createdAtMs: 3000},
  {opId: "agent:newer-time", opType: "agent", taskUuid: "task-newer-time", displaySeq: 30, createdAtMs: 4000},
  {opId: "agent:newest", opType: "agent", taskUuid: "task-newest", displaySeq: 30, createdAtMs: 5000},
  {opId: "agent:terminal", opType: "agent", taskUuid: "task-terminal", displaySeq: 40, createdAtMs: 6000},
];
const runState = {
  activeAgentOperationIds: ["agent:old", "agent:newer-time", "agent:newest"],
};

test("latest active Agent selection is stable by displaySeq then createdAt", () => {
  const selected = selectLatestActiveAgentOperation(operations, runState);
  assert.equal(selected.opId, "agent:newest");
  assert.equal(agentPanelDetailKey("conversation-1", "task-newest"), "agent-panel:conversation-1:task-newest");
});

test("auto-open uses stable task identity and respects an explicit closed latest task", () => {
  const latestKey = agentPanelDetailKey("conversation-1", "task-newest");
  const decision = decideAgentAutoOpen({
    conversationUuid: "conversation-1",
    operations,
    runState,
    intents: {[latestKey]: "closed"},
  });
  assert.equal(decision.action, "respect_closed");
  assert.equal(decision.key, latestKey);
  assert.equal(decision.operation.opId, "agent:newest");
  assert.equal(decision.fallbackOperation, null);
});

test("a new Agent task does not inherit an older task closed intent", () => {
  const oldKey = agentPanelDetailKey("conversation-1", "task-old");
  const decision = decideAgentAutoOpen({
    conversationUuid: "conversation-1",
    operations,
    runState,
    intents: {[oldKey]: "closed"},
  });
  assert.equal(decision.action, "open");
  assert.equal(decision.intent, "auto");
  assert.equal(decision.key, agentPanelDetailKey("conversation-1", "task-newest"));
});

test("missing active Agent requests one pending hydration consumption", () => {
  const decision = decideAgentAutoOpen({
    conversationUuid: "conversation-1",
    operations,
    runState: {activeAgentOperationIds: []},
    intents: {},
  });
  assert.equal(decision.action, "pending");
  assert.equal(decision.key, "");
});

test("session intent normalization keeps identifiers and tri-state UI intent only", () => {
  assert.deepEqual(normalizeAgentPanelIntents({
    "agent-panel:conversation-1:task-1": "closed",
    "agent-panel:conversation-1:task-2": "open",
    "agent-panel:conversation-1:task-3": "auto",
    "agent-panel:conversation-1:task-4": "running business payload",
    other: {status: "running", output: "secret"},
  }), {
    "agent-panel:conversation-1:task-1": "closed",
    "agent-panel:conversation-1:task-2": "open",
    "agent-panel:conversation-1:task-3": "auto",
  });
});
