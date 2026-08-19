import test from "node:test";
import assert from "node:assert/strict";

import {createTaskMemoryRequestGate} from "./taskMemoryRequestGate.js";
import {
  createTaskMemoryBadgeState,
  createTaskMemoryChangedEventGate,
  normalizeTaskMemoryChangedEvent,
  taskMemoryChangedMatchesIdentity,
  taskMemoryChangedTransportEvent,
  taskMemoryMutationRecovery,
  taskMemorySourceLabel,
} from "./taskMemoryUiState.js";

function event(overrides = {}) {
  return {
    type: "task_memory.changed",
    conversationUuid: "conv-current",
    scopeType: "agent_task",
    taskUuid: "task-current",
    memoryUuid: "mem-current",
    action: "update",
    revision: 2,
    ...overrides,
  };
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return {promise, resolve};
}

test("transport accepts only the active conversation/socket and sanitizes the domain payload", () => {
  const transport = {
    activeConversationUuid: "conv-current",
    sourceConversationUuid: "conv-current",
    socketConversationUuid: "conv-current",
    sourceIsActive: true,
  };
  const accepted = taskMemoryChangedTransportEvent(event({body: "must-not-pass", chatId: 42}), transport);
  assert.deepEqual(accepted, event());
  assert.equal(Object.hasOwn(accepted, "body"), false);
  assert.equal(Object.hasOwn(accepted, "chatId"), false);

  assert.equal(taskMemoryChangedTransportEvent(event({conversationUuid: "conv-old"}), transport), null);
  assert.equal(taskMemoryChangedTransportEvent(event(), {...transport, sourceConversationUuid: "conv-old"}), null);
  assert.equal(taskMemoryChangedTransportEvent(event(), {...transport, socketConversationUuid: "conv-old"}), null);
  assert.equal(taskMemoryChangedTransportEvent(event(), {...transport, sourceIsActive: false}), null);
  assert.equal(normalizeTaskMemoryChangedEvent({...event(), revision: 0}), null);
});

test("event gate filters conversation/scope/task and ignores duplicate or older revisions", () => {
  const identity = {
    conversationUuid: "conv-current",
    scopeType: "agent_task",
    taskUuid: "task-current",
  };
  const gate = createTaskMemoryChangedEventGate(identity);
  assert.equal(taskMemoryChangedMatchesIdentity(event(), identity), true);
  assert.equal(gate.accept(event(), identity), true);
  assert.equal(gate.accept(event(), identity), false);
  assert.equal(gate.accept(event({revision: 1}), identity), false);
  assert.equal(gate.accept(event({revision: 3}), identity), true);
  assert.equal(gate.accept(event({taskUuid: "task-other", revision: 4}), identity), false);
  assert.equal(gate.accept(event({scopeType: "conversation", taskUuid: "", revision: 4}), identity), false);
  assert.equal(gate.accept(event({conversationUuid: "conv-other", revision: 4}), identity), false);
});

test("accepted realtime event invalidates an older pending request before the fresh refresh applies", async () => {
  const identity = {
    conversationUuid: "conv-current",
    scopeType: "agent_task",
    taskUuid: "task-current",
  };
  const requestGate = createTaskMemoryRequestGate();
  const eventGate = createTaskMemoryChangedEventGate(identity);
  const state = {items: []};
  const oldRequest = deferred();
  const oldToken = requestGate.capture(identity, "items");
  const oldPending = oldRequest.promise.then((items) => {
    if (requestGate.isCurrent(oldToken, identity)) state.items = items;
  });

  assert.equal(eventGate.accept(event(), identity), true);
  requestGate.invalidate();
  const freshRequest = deferred();
  const freshToken = requestGate.capture(identity, "items");
  const freshPending = freshRequest.promise.then((items) => {
    if (requestGate.isCurrent(freshToken, identity)) state.items = items;
  });
  freshRequest.resolve(["revision-2"]);
  await freshPending;
  oldRequest.resolve(["stale-revision-1"]);
  await oldPending;

  assert.deepEqual(state.items, ["revision-2"]);
});

test("stable badge survives close, updates after realtime refresh, and resets on conversation switch", () => {
  const badge = createTaskMemoryBadgeState();
  assert.equal(badge.switchConversation("conv-current").count, 0);
  const agentIdentity = {
    conversationUuid: "conv-current", scopeType: "agent_task", taskUuid: "task-current",
  };
  assert.equal(badge.set(agentIdentity, 3).count, 3);
  assert.equal(badge.snapshot().count, 3, "closing the drawer does not mutate stable count");
  assert.equal(badge.set(agentIdentity, 4).count, 4, "realtime count refresh updates stable entry");

  assert.deepEqual(badge.switchConversation("conv-next"), {
    conversationUuid: "conv-next", scopeType: "conversation", taskUuid: "", count: 0,
  });
  assert.equal(badge.set(agentIdentity, 99).count, 0, "late previous-conversation count is ignored");
});

test("409/404 recovery decisions clear stale editing and request refresh while other errors stay generic", () => {
  const conflict = taskMemoryMutationRecovery({response: {status: 409}});
  assert.deepEqual(conflict, {
    kind: "conflict", resetEditor: true, refresh: true,
    message: "任务记忆已被其他操作更新，已刷新最新版本，请重新编辑。",
  });
  const missing = taskMemoryMutationRecovery({response: {status: 404}});
  assert.deepEqual(missing, {
    kind: "not_found", resetEditor: true, refresh: true,
    message: "该任务记忆已不存在或当前不可访问，已清理陈旧状态。",
  });
  assert.deepEqual(taskMemoryMutationRecovery({response: {status: 500}}), {
    kind: "generic", resetEditor: false, refresh: false, message: "",
  });
});

test("source projection exposes only user, AI, or system labels plus short turn/run ids", () => {
  const rows = [
    [{createdBy: "web:998877", sourceTurnUuid: "turn-123456789", sourceRunUuid: "run-987654321"}, "用户"],
    [{createdBy: "agent:private-agent-key", sourceTurnUuid: "agent-turn", sourceRunUuid: "agent-run"}, "AI"],
    [{createdBy: "main-controller", sourceTurnUuid: "main-turn", sourceRunUuid: "main-run"}, "AI"],
    [{createdBy: "duplicate", sourceTurnUuid: "system-turn", sourceRunUuid: "system-run"}, "系统"],
  ];
  for (const [item, expectedActor] of rows) {
    const label = taskMemorySourceLabel(item);
    assert.equal(label.startsWith(`${expectedActor} · turn `), true);
    assert.equal(label.includes(String(item.createdBy)), false);
    assert.equal(label.includes(String(item.sourceTurnUuid).slice(0, 8)), true);
    assert.equal(label.includes(String(item.sourceRunUuid).slice(0, 8)), true);
  }
});
