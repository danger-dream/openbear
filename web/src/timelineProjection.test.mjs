import test from "node:test";
import assert from "node:assert/strict";

import {
  applyOperationFrame,
  convergeStoppedAcknowledgement,
  deriveOperationRunState,
  eventDisplayTimeMs,
  eventStartedAtMs,
  eventUpdatedAtMs,
  isContextCompactionOperation,
  isTerminalOperationFrame,
  projectOperationMessages,
  shouldApplyOperationFrame,
  withTransientIdleThinking,
} from "./timelineProjection.js";
import {
  createTerminalStateRefreshScheduler,
  runGuardedConversationStateRefresh,
} from "./views/consoleView/terminalStateRefresh.js";


test("context compaction operations project as visible ContextCompaction tool cards", () => {
  const projected = projectOperationMessages([{
    conversationUuid: "conv-compact",
    opId: "manual-compact:1",
    opType: "context_compaction",
    displaySeq: 10,
    revision: 2,
    status: "completed",
    lifecycle: "terminal",
    internal: true,
    payload: {
      compactionId: "context-compaction:119",
      summaryId: 119,
      scope: "root",
      source: "manual",
      status: "completed",
      beforeTokens: 212912,
      afterTokens: 3334,
      outputPreview: "compressed summary",
      internal: true,
    },
    createdAtMs: 1000,
    updatedAtMs: 2000,
  }]);

  const assistant = projected.find((message) => message.role === "assistant");
  assert.ok(assistant, "expected a visible assistant timeline row");
  const event = assistant.localTimeline.find((item) => item.kind === "tool");
  assert.ok(event, "expected a context compaction tool card");
  assert.equal(event.toolName, "ContextCompaction");
  assert.equal(event.calls[0].id, "context-compaction:119");
  assert.equal(event.calls[0].name, "ContextCompaction");
  assert.match(event.calls[0].preview, /来源：手动触发/);
  assert.match(event.calls[0].preview, /压缩前上下文：212,912 token/);
  assert.match(event.calls[0].preview, /压缩后上下文：3,334 token/);
  assert.equal(JSON.parse(event.calls[0].arguments).source, "manual");
  assert.equal(event.result.content, "compressed summary");
  assert.equal(event.operation.payload.summaryId, 119);
  assert.equal(event.operation.payload.name, "ContextCompaction");
  assert.equal(event.live, false);
});

test("operation-only turns mark their empty user anchor as a hidden synthetic placeholder", () => {
  const projected = projectOperationMessages([{
    conversationUuid: "conv-orphan-control",
    opId: "control:orphan",
    opType: "run_control",
    displaySeq: 10,
    revision: 1,
    status: "cancelled",
    lifecycle: "terminal",
    internal: false,
    payload: {reason: "已停止", status: "cancelled"},
  }]);

  const user = projected.find((message) => message.role === "user");
  assert.ok(user);
  assert.equal(user.content, "");
  assert.equal(user.syntheticPlaceholder, true);
});


test("historical generic compactions use the same classifier and keep their original turn", () => {
  const historical = {
    conversationUuid: "conv-compact",
    opId: "tool:context-compaction:77",
    opType: "tool",
    source: "context_compaction",
    turnUuid: "turn-original",
    runRootTurnId: "turn-original",
    displaySeq: 940,
    revision: 2,
    status: "completed",
    lifecycle: "terminal",
    internal: true,
    payload: {
      toolName: "ContextCompaction",
      compactionId: "context-compaction:77",
      summaryId: 77,
      scope: "root",
      status: "completed",
      outputPreview: "legacy summary",
      internal: true,
    },
    createdAtMs: 1000,
    updatedAtMs: 2000,
  };
  const ordinary = {
    opId: "tool:bash-1",
    opType: "tool",
    source: "tool",
    internal: true,
    payload: {toolName: "Bash"},
  };

  assert.equal(isContextCompactionOperation(historical), true);
  assert.equal(isContextCompactionOperation(ordinary), false);
  assert.equal(isContextCompactionOperation({
    ...ordinary,
    opId: "tool:context-compaction:not-valid",
    payload: {toolName: "ContextCompaction"},
  }), false);

  const projected = projectOperationMessages([historical]);
  const assistant = projected.find((message) => message.id === "op-assistant-turn-original");
  assert.ok(assistant);
  assert.equal(projected.some((message) => message.id !== "op-user-turn-original" && message.id !== "op-assistant-turn-original"), false);
  assert.equal(assistant.localTimeline.length, 1);
  assert.equal(assistant.localTimeline[0].operation.displaySeq, 940);
  assert.equal(assistant.localTimeline[0].operation.runRootTurnId, "turn-original");
  assert.equal(assistant.localTimeline[0].calls[0].name, "ContextCompaction");
});


test("operation projection keeps agent transcript result separate from native payload state", () => {
  const transcriptResult = JSON.stringify({
    ok: true,
    status: "running",
    detached: true,
    task: { status: "running", currentStatus: "旧 ACK，不应覆盖 native 状态" },
  });
  const operations = [
    {
      conversationUuid: "conv-1",
      internalChatId: -1,
      opId: "run:agent",
      opType: "run",
      turnId: "turn-agent",
      displaySeq: 10,
      revision: 1,
      lifecycle: "terminal",
      status: "completed",
      payload: { turnId: "turn-agent", status: "completed" },
      createdAtMs: 1000,
      updatedAtMs: 1000,
    },
    {
      conversationUuid: "conv-1",
      internalChatId: -1,
      opId: "msg:agent",
      opType: "user_message",
      turnId: "turn-agent",
      displaySeq: 20,
      revision: 1,
      lifecycle: "terminal",
      status: "completed",
      payload: { role: "user", text: "跑 Agent" },
      transcriptMessageIds: [42],
      createdAtMs: 1001,
      updatedAtMs: 1001,
    },
    {
      conversationUuid: "conv-1",
      internalChatId: -1,
      opId: "agent:call-1",
      opType: "agent",
      turnId: "turn-agent",
      displaySeq: 30,
      revision: 4,
      lifecycle: "terminal",
      status: "completed",
      payload: {
        toolCallId: "call-1",
        toolName: "Agent",
        name: "Agent",
        arguments: "{}",
        status: "completed",
        task: { status: "completed", currentStatus: "任务完成" },
        result: { summary: "Agent 完成输出" },
        resultText: transcriptResult,
        transcriptResult: true,
      },
      createdAtMs: 1002,
      updatedAtMs: 1010,
    },
  ];

  const projected = projectOperationMessages(operations);
  assert.equal(projected[0].turnUuid, "turn-agent");
  assert.equal(projected[0].opId, "msg:agent");
  assert.equal(projected[0].deleteTraceable, true);
  const tool = projected[1].localTimeline.find((item) => item.kind === "tool");
  assert.ok(tool, "expected agent operation tool card");
  assert.equal(tool.live, false);
  assert.equal(tool.livePayload.status, "completed");
  assert.equal(tool.livePayload.task.currentStatus, "任务完成");
  assert.equal(tool.result.content, transcriptResult);
});

test("operation projection marks pre-steering assistant turn as continued", () => {
  const operations = [
    { opId: "run:old", opType: "run", runId: "old", displaySeq: 10, status: "completed", lifecycle: "terminal", payload: { status: "completed" } },
    { opId: "msg:old", opType: "user_message", runId: "old", displaySeq: 20, status: "completed", lifecycle: "terminal", payload: { text: "先跑" } },
    { opId: "assistant:old:0", opType: "assistant_message", runId: "old", displaySeq: 30, status: "completed", lifecycle: "terminal", payload: { text: "前半段", complete: true } },
    { opId: "run:new", opType: "run", runId: "new", displaySeq: 40, status: "running", lifecycle: "active", payload: { status: "running" } },
    { opId: "msg:new", opType: "user_message", runId: "new", displaySeq: 50, status: "completed", lifecycle: "terminal", payload: { text: "插话" } },
    { opId: "msg:queued", opType: "user_message", runId: "new", displaySeq: 60, status: "completed", lifecycle: "terminal", payload: { queued: true, text: "插话", status: "已追加到当前运行" } },
    { opId: "assistant:new:0", opType: "assistant_message", runId: "new", displaySeq: 70, status: "completed", lifecycle: "terminal", payload: { text: "最终回答", complete: true } },
  ];

  const projected = projectOperationMessages(operations);
  const assistants = projected.filter((msg) => msg.role === "assistant");
  const users = projected.filter((msg) => msg.role === "user");
  assert.equal(assistants.length, 2);
  assert.equal(assistants[0].continuedBySteering, true);
  assert.equal(users[1].queuedSteering, true);
  assert.equal(assistants[1].continuedBySteering, false);
});

test("consumed interruption remains visible in the same root turn", () => {
  const base = [
    { opId: "run:root", opType: "run", runId: "root", turnUuid: "root", displaySeq: 10, status: "running", lifecycle: "active", payload: { status: "running" } },
    { opId: "msg:root", opType: "user_message", runId: "root", turnUuid: "root", displaySeq: 20, status: "completed", lifecycle: "terminal", payload: { text: "原始问题" } },
    { opId: "assistant:root:0", opType: "assistant_message", runId: "root", turnUuid: "root", displaySeq: 30, status: "completed", lifecycle: "terminal", payload: { text: "处理中", complete: true } },
  ];
  const queued = {
    opId: "msg:interrupt", opType: "user_message", runId: "root", turnUuid: "root", displaySeq: 40,
    status: "completed", lifecycle: "terminal", revision: 1,
    payload: { text: "停止 Agent", queued: true, interruption: true, status: "已追加到当前轮" },
  };
  const injected = {
    ...queued,
    revision: 2,
    payload: { text: "停止 Agent", queued: false, interruption: true, status: "插话已交给主会话" },
  };

  const before = projectOperationMessages([...base, queued]);
  const after = projectOperationMessages([...base, injected]);
  assert.equal(before.filter((item) => item.role === "user").length, 1);
  assert.equal(after.filter((item) => item.role === "user").length, 1);
  assert.equal(after.find((item) => item.role === "user").content, "原始问题");
  const interruption = after.find((item) => item.role === "assistant").localTimeline.find((event) => event.interruption);
  assert.ok(interruption);
  assert.equal(interruption.preview, "停止 Agent");
  assert.equal(interruption.status, "插话已交给主会话");
  assert.equal(interruption.queued, false);
});


test("operation frames use revision freshness instead of frame seq for one op", () => {
  const store = { operationsById: new Map(), orderedOpIds: [], revisionByOpId: new Map(), lastFrameSeq: 100 };
  assert.equal(applyOperationFrame(store, {
    frameSeq: 1,
    conversationUuid: "conv-1",
    internalChatId: -1,
    opId: "agent:call-1",
    opType: "agent",
    action: "start",
    revision: 1,
    displaySeq: 20,
    runId: "run-1",
    payload: { toolCallId: "call-1", toolName: "Agent", name: "Agent", status: "running", task: { status: "running", currentStatus: "检索中" } },
  }), true);
  assert.equal(applyOperationFrame(store, {
    frameSeq: 2,
    conversationUuid: "conv-1",
    internalChatId: -1,
    opId: "agent:call-1",
    opType: "agent",
    action: "end",
    revision: 2,
    displaySeq: 20,
    runId: "run-1",
    payload: { status: "completed", task: { status: "completed", currentStatus: "任务完成" }, result: { summary: "done" } },
  }), true);
  assert.equal(applyOperationFrame(store, {
    frameSeq: 3,
    conversationUuid: "conv-1",
    internalChatId: -1,
    opId: "agent:call-1",
    opType: "agent",
    action: "patch",
    revision: 1,
    displaySeq: 20,
    runId: "run-1",
    payload: { status: "running", task: { status: "running", currentStatus: "旧 ACK" } },
  }), false);

  const op = store.operationsById.get("agent:call-1");
  assert.equal(op.revision, 2);
  assert.equal(op.payload.status, "completed");
  assert.equal(op.payload.task.currentStatus, "任务完成");
  assert.equal(store.lastFrameSeq, 100);
  assert.equal(deriveOperationRunState([...store.operationsById.values()]).running, false);
});

test("websocket frame gate accepts older frameSeq when op revision is newer", () => {
  const store = {
    operationsById: new Map([["run:notify", { opId: "run:notify", revision: 1 }]]),
    revisionByOpId: new Map([["run:notify", 1]]),
    lastFrameSeq: 200,
  };

  assert.equal(shouldApplyOperationFrame({ frameSeq: 199, opId: "run:notify", revision: 2 }, store), true);
  assert.equal(shouldApplyOperationFrame({ frameSeq: 198, opId: "run:notify", revision: 1 }, store), false);
});

test("waiting-control agent operations do not keep run state busy", () => {
  const operations = [
    {
      opId: "agent:paused",
      opType: "agent",
      runId: "run-1",
      displaySeq: 1,
      revision: 1,
      status: "needs_openbear_control",
      lifecycle: "waiting_control",
      payload: { status: "needs_openbear_control", toolName: "Agent" },
      createdAtMs: 1000,
      updatedAtMs: 1000,
    },
  ];

  const state = deriveOperationRunState(operations);
  assert.equal(state.running, false);
  assert.equal(state.backgroundRunning, false);
  assert.equal(state.statusLabel, "Agent 等待裁决");

  const tool = projectOperationMessages(operations).find((msg) => msg.role === "assistant")?.localTimeline.find((item) => item.kind === "tool");
  assert.ok(tool, "expected waiting-control agent card");
  assert.equal(tool.live, false);
});

test("waiting-control notices are informational and never keep a conversation busy", () => {
  const store = {operationsById: new Map(), orderedOpIds: [], revisionByOpId: new Map(), lastFrameSeq: 0};
  assert.equal(applyOperationFrame(store, {
    frameSeq: 1,
    opId: "notice:task:old",
    opType: "notice",
    action: "create",
    revision: 1,
    displaySeq: 1,
    status: "needs_openbear_control",
    payload: {status: "needs_openbear_control", taskUuid: "old", text: "等待裁决"},
  }), true);
  const notice = store.operationsById.get("notice:task:old");
  assert.equal(notice.lifecycle, "informational");
  assert.equal(deriveOperationRunState([notice]).running, false);
});

test("operation frame revision gap requests state resync instead of applying partial patch", () => {
  const store = { operationsById: new Map(), orderedOpIds: [], revisionByOpId: new Map(), lastFrameSeq: 0 };
  assert.equal(applyOperationFrame(store, {
    frameSeq: 1,
    conversationUuid: "conv-1",
    opId: "agent:gap",
    opType: "agent",
    action: "start",
    revision: 1,
    displaySeq: 10,
    turnId: "turn-gap",
    payload: { status: "running", task: { status: "running", currentStatus: "检索中" } },
  }), true);
  assert.equal(applyOperationFrame(store, {
    frameSeq: 3,
    conversationUuid: "conv-1",
    opId: "agent:gap",
    opType: "agent",
    action: "patch",
    revision: 3,
    displaySeq: 10,
    turnId: "turn-gap",
    payload: { result: { summary: "缺了 revision 2 的增量" } },
  }), false);

  const op = store.operationsById.get("agent:gap");
  assert.equal(store.needsResync, true);
  assert.deepEqual(store.revisionGap, { opId: "agent:gap", expectedRevision: 2, incomingRevision: 3, frameSeq: 3, resyncMode: "frames", requiresFullState: false });
  assert.equal(store.lastFrameSeq, 3);
  assert.equal(op.revision, 1);
  assert.equal(op.payload.task.currentStatus, "检索中");
  assert.equal(op.payload.result, undefined);
});

test("first terminal frame time remains immutable across duplicate terminal frames", () => {
  const store = { operationsById: new Map(), orderedOpIds: [], revisionByOpId: new Map(), lastFrameSeq: 0 };
  assert.equal(applyOperationFrame(store, {
    frameSeq: 1,
    opId: "reasoning:stable-terminal",
    opType: "reasoning",
    action: "append",
    revision: 1,
    displaySeq: 1,
    createdAtMs: 1_000,
    updatedAtMs: 1_000,
    payload: {delta: "思考", complete: false},
  }), true);
  assert.equal(applyOperationFrame(store, {
    frameSeq: 2,
    opId: "reasoning:stable-terminal",
    opType: "reasoning",
    action: "end",
    revision: 2,
    displaySeq: 1,
    createdAtMs: 2_000,
    updatedAtMs: 2_000,
    payload: {complete: true},
  }), true);
  assert.equal(applyOperationFrame(store, {
    frameSeq: 3,
    opId: "reasoning:stable-terminal",
    opType: "reasoning",
    action: "end",
    revision: 3,
    displaySeq: 1,
    createdAtMs: 9_000,
    updatedAtMs: 9_000,
    payload: {complete: true},
  }), true);

  const operation = store.operationsById.get("reasoning:stable-terminal");
  assert.equal(operation.createdAtMs, 1_000);
  assert.equal(operation.updatedAtMs, 9_000);
  assert.equal(operation.terminalAtMs, 2_000);
});

test("patch for a missing operation requests resync instead of inventing a new createdAt", () => {
  const store = { operationsById: new Map(), orderedOpIds: [], revisionByOpId: new Map(), lastFrameSeq: 100 };

  assert.equal(applyOperationFrame(store, {
    frameSeq: 103,
    conversationUuid: "conv-1",
    opId: "agent:missing-base",
    opType: "agent",
    action: "patch",
    revision: 7,
    displaySeq: 10,
    turnId: "turn-gap",
    createdAtMs: 999999,
    payload: { status: "running", currentStatus: "迟到的局部进度" },
  }), false);

  assert.equal(store.operationsById.has("agent:missing-base"), false);
  assert.equal(store.needsResync, true);
  assert.deepEqual(store.revisionGap, {
    opId: "agent:missing-base",
    expectedRevision: 1,
    incomingRevision: 7,
    frameSeq: 103,
    resyncMode: "frames",
    requiresFullState: false,
  });
  assert.equal(store.lastFrameSeq, 103);
});

test("older frame with newer revision gap requests full state resync", () => {
  const store = {
    operationsById: new Map([["agent:gap-old", {
      opId: "agent:gap-old",
      opType: "agent",
      revision: 1,
      payload: { status: "running" },
    }]]),
    orderedOpIds: ["agent:gap-old"],
    revisionByOpId: new Map([["agent:gap-old", 1]]),
    lastFrameSeq: 200,
  };

  assert.equal(applyOperationFrame(store, {
    frameSeq: 199,
    conversationUuid: "conv-1",
    opId: "agent:gap-old",
    opType: "agent",
    action: "patch",
    revision: 3,
    displaySeq: 10,
    turnId: "turn-gap",
    payload: { status: "completed" },
  }), false);

  assert.equal(store.needsResync, true);
  assert.deepEqual(store.revisionGap, {
    opId: "agent:gap-old",
    expectedRevision: 2,
    incomingRevision: 3,
    frameSeq: 199,
    resyncMode: "full_state",
    requiresFullState: true,
  });
  assert.equal(store.lastFrameSeq, 200);
  assert.equal(store.operationsById.get("agent:gap-old").payload.status, "running");
});

test("projected event display time stays at creation while duration uses the latest revision", () => {
  const operations = [
    {
      opId: "msg:time",
      opType: "user_message",
      turnId: "turn-time",
      runRootTurnId: "turn-time",
      displaySeq: 1,
      revision: 1,
      lifecycle: "terminal",
      status: "completed",
      payload: { text: "检查时间" },
      createdAtMs: 1_000,
      updatedAtMs: 1_000,
    },
    {
      opId: "reasoning:turn-time:0",
      opType: "reasoning",
      turnId: "turn-time",
      runRootTurnId: "turn-time",
      displaySeq: 2,
      revision: 9,
      lifecycle: "terminal",
      status: "completed",
      payload: { text: "稳定的思考过程", complete: true },
      createdAtMs: 2_000,
      updatedAtMs: 62_000,
      terminalAtMs: 12_000,
    },
    {
      opId: "agent:task-time",
      opType: "agent",
      turnId: "turn-time",
      runRootTurnId: "turn-time",
      displaySeq: 3,
      revision: 20,
      lifecycle: "active",
      status: "running",
      payload: { taskUuid: "task-time", task: { taskUuid: "task-time", status: "running" } },
      createdAtMs: 3_000,
      updatedAtMs: 123_000,
    },
  ];

  const timeline = projectOperationMessages(operations)
    .find((item) => item.role === "assistant")
    .localTimeline;
  const reasoning = timeline.find((item) => item.id === "reasoning:turn-time:0");
  const agent = timeline.find((item) => item.id === "agent:task-time");

  assert.equal(reasoning.startedAt, 2_000);
  assert.equal(reasoning.ts, 62_000);
  assert.equal(eventStartedAtMs(reasoning), 2_000);
  assert.equal(eventDisplayTimeMs(reasoning), 2_000);
  assert.equal(eventUpdatedAtMs(reasoning), 12_000);
  assert.equal(agent.startedAt, 3_000);
  assert.equal(eventDisplayTimeMs(agent), 3_000);
  assert.equal(eventUpdatedAtMs(agent), 123_000);
});

test("operation projection hides terminal status rows after turn completion", () => {
  const operations = [
    {
      conversationUuid: "conv-1",
      internalChatId: -1,
      opId: "run:done",
      opType: "run",
      turnId: "done",
      displaySeq: 10,
      revision: 2,
      lifecycle: "terminal",
      status: "completed",
      payload: { turnId: "done", source: "user", status: "completed" },
      createdAtMs: 1000,
      updatedAtMs: 2000,
    },
    {
      conversationUuid: "conv-1",
      internalChatId: -1,
      opId: "msg:done",
      opType: "user_message",
      turnId: "done",
      displaySeq: 20,
      revision: 1,
      lifecycle: "terminal",
      status: "completed",
      payload: { role: "user", text: "问题" },
      createdAtMs: 1001,
      updatedAtMs: 1001,
    },
    {
      conversationUuid: "conv-1",
      internalChatId: -1,
      opId: "status:done",
      opType: "status",
      turnId: "done",
      displaySeq: 30,
      revision: 4,
      lifecycle: "active",
      status: "running",
      payload: { statusText: "正在思考 …", active: true },
      createdAtMs: 1002,
      updatedAtMs: 1002,
    },
    {
      conversationUuid: "conv-1",
      internalChatId: -1,
      opId: "assistant:done:0",
      opType: "assistant_message",
      turnId: "done",
      displaySeq: 40,
      revision: 2,
      lifecycle: "terminal",
      status: "completed",
      payload: { text: "答案", complete: true },
      createdAtMs: 1003,
      updatedAtMs: 1003,
    },
  ];

  const projected = projectOperationMessages(operations);
  const timeline = projected.find((msg) => msg.role === "assistant")?.localTimeline || [];
  assert.equal(timeline.some((item) => item.kind === "live_status" && /正在思考/.test(item.status)), false);
  assert.equal(timeline.find((item) => item.kind === "answer")?.message.content, "答案");
});

test("operation projection renders task notification as internal assistant turn", () => {
  const operations = [
    {
      conversationUuid: "conv-1",
      internalChatId: -1,
      opId: "run:notify",
      opType: "run",
      turnId: "notify",
      displaySeq: 10,
      revision: 1,
      lifecycle: "terminal",
      status: "completed",
      internal: true,
      payload: { turnId: "notify", source: "task_notification", status: "completed", internal: true },
      createdAtMs: 1000,
      updatedAtMs: 1000,
    },
    {
      conversationUuid: "conv-1",
      internalChatId: -1,
      opId: "msg:internal",
      opType: "user_message",
      turnId: "notify",
      displaySeq: 20,
      revision: 1,
      lifecycle: "terminal",
      status: "completed",
      internal: true,
      payload: { role: "user", text: "<task-notification>raw</task-notification>", internal: true },
      createdAtMs: 1001,
      updatedAtMs: 1001,
    },
    {
      conversationUuid: "conv-1",
      internalChatId: -1,
      opId: "notice:task:task-1",
      opType: "notice",
      turnId: "notify",
      displaySeq: 30,
      revision: 1,
      lifecycle: "informational",
      status: "",
      internal: true,
      payload: { text: "Agent 结果已回传", taskUuid: "task-1", internal: true },
      createdAtMs: 1002,
      updatedAtMs: 1002,
    },
    {
      conversationUuid: "conv-1",
      internalChatId: -1,
      opId: "assistant:notify:0",
      opType: "assistant_message",
      turnId: "notify",
      displaySeq: 40,
      revision: 1,
      lifecycle: "terminal",
      status: "completed",
      internal: false,
      payload: { text: "汇总完成", complete: true },
      createdAtMs: 1003,
      updatedAtMs: 1003,
    },
  ];

  const projected = projectOperationMessages(operations);
  assert.deepEqual(projected.map((msg) => msg.role), ["assistant"]);
  assert.equal(projected[0].localTimeline.find((item) => item.kind === "live_status").status, "Agent 结果已回传");
  assert.equal(projected[0].localTimeline.find((item) => item.kind === "live_status").preview, "");
  assert.equal(projected[0].localTimeline.find((item) => item.kind === "answer").message.content, "汇总完成");
});

test("operation projection does not render active status as transcript event", () => {
  const operations = [
    { opId: "run:1", opType: "run", runId: "run-1", displaySeq: 1, revision: 1, lifecycle: "active", status: "running", payload: { status: "running" }, createdAtMs: 1000, updatedAtMs: 1000 },
    { opId: "msg:1", opType: "user_message", runId: "run-1", displaySeq: 2, revision: 1, lifecycle: "terminal", status: "completed", payload: { text: "问题" }, createdAtMs: 1001, updatedAtMs: 1001 },
    { opId: "status:1", opType: "status", runId: "run-1", displaySeq: 3, revision: 1, lifecycle: "active", status: "running", payload: { statusText: "正在思考 …", active: true }, createdAtMs: 1002, updatedAtMs: 1002 },
  ];
  const assistant = projectOperationMessages(operations).find((msg) => msg.role === "assistant");
  assert.equal(assistant, undefined);
});

test("operation assistant text deactivates previous reasoning immediately", () => {
  const operations = [
    { opId: "run:1", opType: "run", runId: "run-1", displaySeq: 1, revision: 1, lifecycle: "active", status: "running", payload: { status: "running" }, createdAtMs: 1000, updatedAtMs: 1000 },
    { opId: "msg:1", opType: "user_message", runId: "run-1", displaySeq: 2, revision: 1, lifecycle: "terminal", status: "completed", payload: { text: "问题" }, createdAtMs: 1001, updatedAtMs: 1001 },
    { opId: "reasoning:1", opType: "reasoning", runId: "run-1", displaySeq: 3, revision: 2, lifecycle: "active", status: "running", payload: { text: "最后一段思考", complete: false }, createdAtMs: 1002, updatedAtMs: 1003 },
    { opId: "assistant:1", opType: "assistant_message", runId: "run-1", displaySeq: 4, revision: 5, lifecycle: "active", status: "running", payload: { text: "开始输出结论", complete: false }, createdAtMs: 1004, updatedAtMs: 1005 },
  ];
  const assistant = projectOperationMessages(operations).find((msg) => msg.role === "assistant");
  assert.ok(assistant, "expected assistant message");
  const reasoning = assistant.localTimeline.find((item) => item.kind === "answer" && item.message.reasoning);
  assert.ok(reasoning, "expected reasoning block");
  assert.equal(reasoning.reasoningActive, false);
  const answer = assistant.localTimeline.find((item) => item.kind === "answer" && item.message.content);
  assert.equal(answer.message.content, "开始输出结论");
});

test("operation projection groups distinct execution runs into one root turn", () => {
  const operations = [
    { opId: "run:exec-1", opType: "run", runId: "exec-1", runRootTurnId: "root-1", turnUuid: "root-1", displaySeq: 10, status: "completed", lifecycle: "terminal", payload: { runId: "exec-1", status: "completed" } },
    { opId: "msg:root", opType: "user_message", runId: "exec-1", runRootTurnId: "root-1", turnUuid: "root-1", displaySeq: 20, status: "completed", lifecycle: "terminal", payload: { text: "原始问题" } },
    { opId: "assistant:root-1:0", opType: "assistant_message", runId: "exec-1", runRootTurnId: "root-1", turnUuid: "root-1", displaySeq: 30, status: "completed", lifecycle: "terminal", payload: { text: "先等待 Agent", complete: true } },
    { opId: "run:exec-2", opType: "run", runId: "exec-2", runRootTurnId: "root-1", turnUuid: "root-1", displaySeq: 40, status: "completed", lifecycle: "terminal", payload: { runId: "exec-2", source: "task_notification", internal: true, status: "completed" } },
    { opId: "notice:task:t1", opType: "notice", runId: "exec-2", runRootTurnId: "root-1", turnUuid: "root-1", displaySeq: 50, status: "completed", lifecycle: "informational", internal: true, payload: { taskUuid: "t1", text: "Agent 完成", internal: true } },
    { opId: "assistant:root-1:1", opType: "assistant_message", runId: "exec-2", runRootTurnId: "root-1", turnUuid: "root-1", displaySeq: 60, status: "completed", lifecycle: "terminal", payload: { text: "最终汇总", complete: true } },
  ];

  const projected = projectOperationMessages(operations);
  const users = projected.filter((msg) => msg.role === "user");
  const assistants = projected.filter((msg) => msg.role === "assistant");
  assert.equal(users.length, 1);
  assert.equal(users[0].content, "原始问题");
  assert.equal(assistants.length, 1);
  assert.equal(assistants[0].id, "op-assistant-root-1");
  assert.ok(assistants[0].localTimeline.some((item) => item.kind === "answer" && item.message.content === "最终汇总"));
});

test("operation projection does not create empty user row for internal task-notification answer", () => {
  const operations = [
    { opId: "status:notify-1", opType: "status", runId: "notify-1", displaySeq: 5, revision: 1, lifecycle: "terminal", status: "completed", internal: false, payload: { status: "completed" }, createdAtMs: 999, updatedAtMs: 999 },
    { opId: "notice:task:t1", opType: "notice", runId: "notify-1", displaySeq: 10, revision: 1, lifecycle: "informational", status: "", internal: true, payload: { internal: true, taskUuid: "task-1", text: "Agent 结果已回传" }, createdAtMs: 1000, updatedAtMs: 1000 },
    { opId: "assistant:notify-1:0", opType: "assistant_message", runId: "notify-1", displaySeq: 20, revision: 1, lifecycle: "terminal", status: "completed", internal: false, payload: { text: "统一总结", complete: true }, createdAtMs: 1001, updatedAtMs: 1001 },
  ];

  const projected = projectOperationMessages(operations);
  assert.equal(projected.some((msg) => msg.role === "user" && !String(msg.content || "").trim()), false);
  const assistant = projected.find((msg) => msg.role === "assistant");
  assert.ok(assistant, "expected task-notification assistant output to remain visible");
  assert.equal(assistant.localTimeline.some((item) => item.kind === "answer" && item.message.content === "统一总结"), true);
});

test("operation projection hides silent internal task-notification turns", () => {
  const operations = [
    { opId: "notice:task:t1", opType: "notice", runId: "notify-hidden", displaySeq: 10, revision: 1, lifecycle: "informational", status: "", internal: true, payload: { internal: true, hidden: true, taskUuid: "task-1", text: "Agent 结果已回传" }, createdAtMs: 1000, updatedAtMs: 1000 },
    { opId: "assistant:notify-hidden:0", opType: "assistant_message", runId: "notify-hidden", displaySeq: 20, revision: 1, lifecycle: "terminal", status: "completed", internal: true, payload: { internal: true, hidden: true, text: "等待其它 Agent", complete: true }, createdAtMs: 1001, updatedAtMs: 1001 },
  ];

  const projected = projectOperationMessages(operations);
  assert.equal(projected.some((msg) => msg.role === "user" && !String(msg.content || "").trim()), false);
  assert.equal(projected.some((msg) => msg.role === "assistant" && msg.localTimeline?.some((item) => item.message?.content === "等待其它 Agent")), false);
});

test("orphan agent_control never falls back into the main model timeline", () => {
  const operations = [
    { opId: "msg:1", opType: "user_message", runId: "bg-1", displaySeq: 10, revision: 1, lifecycle: "terminal", status: "completed", payload: { text: "补充一下" }, createdAtMs: 1000, updatedAtMs: 1000 },
    { opId: "agent-control:c1", opType: "agent_control", runId: "bg-1", displaySeq: 20, revision: 1, lifecycle: "terminal", status: "completed", payload: { controlAction: "steer", taskUuid: "task-1", controlUuid: "c1", statusText: "已追加给后台 Agent：general-purpose-task", text: "补充一下" }, createdAtMs: 1001, updatedAtMs: 1001 },
    { opId: "assistant:bg-1:0", opType: "assistant_message", runId: "bg-1", displaySeq: 30, revision: 1, lifecycle: "terminal", status: "completed", payload: { text: "已追加给后台 Agent", complete: true }, createdAtMs: 1002, updatedAtMs: 1002 },
  ];

  const projected = projectOperationMessages(operations);
  const assistant = projected.find((msg) => msg.role === "assistant");
  assert.ok(assistant, "expected assistant timeline");
  const control = assistant.localTimeline.find((item) => item.agentControl);
  assert.equal(control, undefined);
});

test("agent_control resolves a unique short task id and attaches to the Agent monitor stream", () => {
  const operations = [
    { opId: "msg:start", opType: "user_message", runId: "run-1", displaySeq: 10, revision: 1, lifecycle: "terminal", status: "completed", payload: { text: "启动 Agent" }, createdAtMs: 1000, updatedAtMs: 1000 },
    { opId: "agent:task-1", opType: "agent", runId: "run-1", displaySeq: 20, revision: 1, lifecycle: "active", status: "running", payload: { status: "running", toolName: "Agent", taskUuid: "task-1", task: { taskUuid: "task-1", status: "running", currentStatus: "执行中" } }, createdAtMs: 1001, updatedAtMs: 1001 },
    { opId: "msg:control", opType: "user_message", runId: "control-1", displaySeq: 30, revision: 1, lifecycle: "terminal", status: "completed", payload: { text: "补充一下" }, createdAtMs: 1002, updatedAtMs: 1002 },
    { opId: "agent-control:c1", opType: "agent_control", runId: "control-1", displaySeq: 40, revision: 1, lifecycle: "terminal", status: "completed", payload: { controlAction: "steer", taskUuid: "task", controlUuid: "c1", statusText: "已追加给后台 Agent：general-purpose-task", text: "补充一下" }, createdAtMs: 1003, updatedAtMs: 1003 },
    { opId: "assistant:control", opType: "assistant_message", runId: "control-1", displaySeq: 50, revision: 1, lifecycle: "terminal", status: "completed", payload: { text: "已追加给后台 Agent", complete: true }, createdAtMs: 1004, updatedAtMs: 1004 },
  ];

  const projected = projectOperationMessages(operations);
  const firstAssistant = projected.find((msg) => msg.role === "assistant" && msg.localTimeline?.some((item) => item.kind === "tool"));
  assert.ok(firstAssistant, "expected original assistant with agent card");
  const agentCard = firstAssistant.localTimeline.find((item) => item.kind === "tool");
  assert.equal(agentCard.livePayload.recentEvents.at(-1).kind, "agent_control");
  assert.equal(agentCard.livePayload.recentEvents.at(-1).summary, "已追加给后台 Agent：general-purpose-task");
  const fallbackStatusCount = projected.flatMap((msg) => msg.localTimeline || []).filter((item) => item.agentControl).length;
  assert.equal(fallbackStatusCount, 0);
});

test("AgentPlanDecision is projected into the matching Agent monitor stream, not as a main Tool", () => {
  const operations = [
    { opId: "msg:start", opType: "user_message", runId: "run-1", runRootTurnId: "root-1", displaySeq: 10, revision: 1, lifecycle: "terminal", status: "completed", payload: { text: "启动两个 Agent" }, createdAtMs: 1000, updatedAtMs: 1000 },
    { opId: "agent:task-1111", opType: "agent", runId: "run-1", runRootTurnId: "root-1", displaySeq: 20, revision: 1, lifecycle: "active", status: "running", payload: { taskUuid: "task-1111", task: { taskUuid: "task-1111", title: "安全审查", status: "running" } }, createdAtMs: 1001, updatedAtMs: 1001 },
    { opId: "agent:task-2222", opType: "agent", runId: "run-1", runRootTurnId: "root-1", displaySeq: 30, revision: 1, lifecycle: "active", status: "running", payload: { taskUuid: "task-2222", task: { taskUuid: "task-2222", title: "性能审查", status: "running" } }, createdAtMs: 1002, updatedAtMs: 1002 },
    { opId: "tool:decision", opType: "tool", runId: "run-1", runRootTurnId: "root-1", displaySeq: 40, revision: 1, lifecycle: "terminal", status: "completed", payload: { name: "AgentPlanDecision", arguments: JSON.stringify({ taskUuid: "task-1111", action: "approve", reason: "范围清晰", grantedTools: ["Read"] }), result: { ok: true, taskUuid: "task-1111", action: "approve" } }, createdAtMs: 1003, updatedAtMs: 1003 },
    { opId: "agent-control:title", opType: "agent_control", runId: "run-1", runRootTurnId: "root-1", displaySeq: 50, revision: 1, lifecycle: "terminal", status: "completed", payload: { controlAction: "steer", title: "性能审查", controlUuid: "control-title", statusText: "补充性能边界", text: "只测生产路径" }, createdAtMs: 1004, updatedAtMs: 1004 },
  ];

  const assistant = projectOperationMessages(operations).find((item) => item.role === "assistant");
  assert.ok(assistant);
  const cards = assistant.localTimeline.filter((item) => item.kind === "tool");
  assert.equal(cards.length, 2, "Plan decision must not render as a third Tool card");
  assert.equal(cards[0].livePayload.recentEvents.at(-1).kind, "plan_decision");
  assert.equal(cards[0].livePayload.recentEvents.at(-1).detail.action, "approve");
  assert.equal(cards[1].livePayload.recentEvents.at(-1).kind, "agent_control");
  assert.equal(assistant.localTimeline.some((item) => item.agentControl || item.agentSupervision), false);
});

test("agent operation display name is not polluted by nested tool payload name", () => {
  const operations = [
    { opId: "msg:start", opType: "user_message", runId: "run-1", displaySeq: 10, revision: 1, lifecycle: "terminal", status: "completed", payload: { text: "启动 Agent" }, createdAtMs: 1000, updatedAtMs: 1000 },
    {
      opId: "agent:task-1",
      opType: "agent",
      runId: "run-1",
      displaySeq: 20,
      revision: 2,
      lifecycle: "terminal",
      status: "completed",
      payload: {
        toolName: "Agent",
        rootToolName: "Agent",
        name: "Memory",
        toolCallId: "call_memory",
        status: "completed",
        task: { taskUuid: "task-1", status: "completed", title: "只读调查" },
        resultText: "Agent 完成",
      },
      createdAtMs: 1001,
      updatedAtMs: 1002,
    },
  ];

  const projected = projectOperationMessages(operations);
  const agentCard = projected.find((msg) => msg.role === "assistant")?.localTimeline?.find((item) => item.kind === "tool");
  assert.ok(agentCard, "expected agent card");
  assert.equal(agentCard.toolName, "Agent");
  assert.equal(agentCard.calls[0].name, "Agent");
  assert.equal(agentCard.result.name, "Agent");
  assert.equal(agentCard.calls[0].id, "agent:task-1");
});

test("agent projection recovers original launch arguments from merged placeholder", () => {
  const originalArguments = JSON.stringify({ prompt: "最初的 Agent 任务", tools: ["Read"] });
  const operations = [
    { opId: "msg:start", opType: "user_message", runId: "run-1", displaySeq: 10, revision: 1, lifecycle: "terminal", status: "completed", payload: { text: "启动 Agent" }, createdAtMs: 1000, updatedAtMs: 1000 },
    {
      opId: "agent:call-root",
      opType: "agent",
      runId: "run-1",
      displaySeq: 20,
      revision: 2,
      lifecycle: "terminal",
      status: "completed",
      payload: {
        toolCallId: "call-root",
        toolName: "Agent",
        name: "Agent",
        arguments: originalArguments,
        args: originalArguments,
        merged: true,
        mergedTo: "agent:task-1",
      },
      createdAtMs: 1001,
      updatedAtMs: 1002,
    },
    {
      opId: "agent:task-1",
      opType: "agent",
      runId: "run-1",
      displaySeq: 30,
      revision: 9,
      lifecycle: "terminal",
      status: "completed",
      payload: {
        toolName: "Agent",
        rootToolName: "Agent",
        name: "AgentWait",
        toolCallId: "call-wait",
        arguments: '{"mode":"event_only","reason":"等待 Agent"}',
        args: '{"mode":"event_only","reason":"等待 Agent"}',
        task: { taskUuid: "task-1", status: "completed", title: "只读调查" },
        resultText: "Agent 完成",
      },
      createdAtMs: 1003,
      updatedAtMs: 1004,
    },
  ];

  const projected = projectOperationMessages(operations);
  const agentCard = projected.find((msg) => msg.role === "assistant")?.localTimeline?.find((item) => item.kind === "tool");
  assert.ok(agentCard, "expected agent card");
  assert.equal(agentCard.calls[0].name, "Agent");
  assert.equal(agentCard.calls[0].id, "call-root");
  assert.equal(agentCard.calls[0].arguments, originalArguments);
});

test("task notice for generic Bash background job is not labeled as Agent", () => {
  const operations = [
    { opId: "msg:start", opType: "user_message", runId: "run-1", displaySeq: 10, revision: 1, lifecycle: "terminal", status: "completed", payload: { text: "跑 Bash" }, createdAtMs: 1000, updatedAtMs: 1000 },
    {
      opId: "notice:task:bash-1",
      opType: "notice",
      runId: "run-1",
      displaySeq: 20,
      revision: 1,
      lifecycle: "terminal",
      status: "completed",
      taskUuid: "bash-1",
      payload: { text: "Bash 后台任务已完成", status: "completed", taskUuid: "bash-1", internal: true },
      createdAtMs: 1001,
      updatedAtMs: 1001,
    },
  ];

  const projected = projectOperationMessages(operations);
  const notice = projected.find((msg) => msg.role === "assistant")?.localTimeline?.find((item) => item.kind === "live_status");
  assert.ok(notice, "expected generic task notice");
  assert.equal(notice.status, "Bash 后台任务已完成");
  assert.equal(notice.preview, "");
  assert.equal(notice.notification, true);
  assert.equal(notice.agentNotice, false);
});

test("task notice renders agent result with title instead of raw uuid preview", () => {
  const operations = [
    { opId: "msg:start", opType: "user_message", runId: "run-1", displaySeq: 10, revision: 1, lifecycle: "terminal", status: "completed", payload: { text: "启动 Agent" }, createdAtMs: 1000, updatedAtMs: 1000 },
    {
      opId: "agent:task-1",
      opType: "agent",
      runId: "run-1",
      displaySeq: 20,
      revision: 1,
      lifecycle: "terminal",
      status: "completed",
      taskUuid: "task-1",
      payload: { toolName: "Agent", status: "completed", task: { taskUuid: "task-1", title: "只读计算 2+2", status: "completed" } },
      createdAtMs: 1001,
      updatedAtMs: 1001,
    },
    {
      opId: "notice:task:task-1",
      opType: "notice",
      runId: "run-1",
      displaySeq: 30,
      revision: 1,
      lifecycle: "terminal",
      status: "completed",
      taskUuid: "task-1",
      payload: { text: "4", status: "completed", taskUuid: "task-1", internal: true },
      createdAtMs: 1002,
      updatedAtMs: 1002,
    },
  ];

  const projected = projectOperationMessages(operations);
  const notice = projected.find((msg) => msg.role === "assistant")?.localTimeline?.find((item) => item.agentNotice);
  assert.ok(notice, "expected formatted agent notice");
  assert.equal(notice.status, "Agent 只读计算 2+2 完成");
  assert.equal(notice.preview, "结果：4");
  assert.equal(notice.notification, true);
  assert.equal(notice.taskUuid, "task-1");
});

test("terminal agent placeholder does not keep run state active when stale payload status was queued", () => {
  const store = { operationsById: new Map(), orderedOpIds: [], revisionByOpId: new Map(), lastFrameSeq: 0 };
  applyOperationFrame(store, {
    frameSeq: 1,
    opId: "agent:call-1",
    opType: "agent",
    action: "create",
    revision: 1,
    displaySeq: 10,
    payload: { toolName: "Agent", status: "queued" },
    createdAtMs: 1000,
    updatedAtMs: 1000,
  });
  applyOperationFrame(store, {
    frameSeq: 2,
    opId: "agent:call-1",
    opType: "agent",
    action: "cancel",
    revision: 2,
    displaySeq: 10,
    payload: { toolName: "Agent", merged: true },
    createdAtMs: 1000,
    updatedAtMs: 1001,
  });

  const op = store.operationsById.get("agent:call-1");
  assert.equal(op.lifecycle, "terminal");
  assert.equal(op.status, "cancelled");
  assert.equal(op.payload.status, "cancelled");
  const derived = deriveOperationRunState([op]);
  assert.equal(derived.running, false);
  assert.deepEqual(derived.activeOperationIds, []);
});

test("agent supervision uses one mutable status operation and drives background state", () => {
  const store = { operationsById: new Map(), orderedOpIds: [], revisionByOpId: new Map(), lastFrameSeq: 0 };
  applyOperationFrame(store, {
    frameSeq: 1,
    opId: "agent-supervision:root-1",
    opType: "agent_supervision",
    action: "patch",
    revision: 1,
    displaySeq: 20,
    turnUuid: "root-1",
    runRootTurnUuid: "root-1",
    status: "running",
    lifecycle: "active",
    payload: { statusText: "计划 180 秒后统一复查 Agent", mode: "review_after", active: true, reviewAfterSeconds: 180 },
    createdAtMs: 1000,
    updatedAtMs: 1000,
  });
  applyOperationFrame(store, {
    frameSeq: 2,
    opId: "agent-supervision:root-1",
    opType: "agent_supervision",
    action: "patch",
    revision: 2,
    displaySeq: 20,
    turnUuid: "root-1",
    runRootTurnUuid: "root-1",
    status: "running",
    lifecycle: "active",
    payload: { statusText: "计划 420 秒后统一复查 Agent", mode: "review_after", active: true, reviewAfterSeconds: 420 },
    createdAtMs: 1000,
    updatedAtMs: 2000,
  });

  assert.equal(store.operationsById.size, 1);
  const op = store.operationsById.get("agent-supervision:root-1");
  assert.equal(op.payload.reviewAfterSeconds, 420);
  const derived = deriveOperationRunState([{
    opId: "run:root-1",
    opType: "run",
    turnUuid: "root-1",
    status: "running",
    lifecycle: "active",
    payload: { status: "running" },
    createdAtMs: 900,
    updatedAtMs: 900,
  }, op]);
  assert.equal(derived.foregroundRunning, false);
  assert.equal(derived.backgroundRunning, true);
  assert.equal(derived.statusLabel, "计划 420 秒后统一复查 Agent");
  const turn = projectOperationMessages([op]).find((item) => item.role === "assistant");
  assert.equal(turn, undefined, "supervision without an Agent card must not pollute the main timeline");

  applyOperationFrame(store, {
    frameSeq: 3,
    opId: "agent-supervision:root-1",
    opType: "agent_supervision",
    action: "end",
    revision: 3,
    displaySeq: 20,
    turnUuid: "root-1",
    runRootTurnUuid: "root-1",
    status: "completed",
    lifecycle: "terminal",
    payload: { statusText: "全部 Agent 已完成", active: false, wakeReason: "all_terminal" },
    createdAtMs: 1000,
    updatedAtMs: 3000,
  });
  assert.equal(deriveOperationRunState([...store.operationsById.values()]).running, false);
});

test("operation projection folds repeated all-terminal waits until a new Agent generation", () => {
  const base = {
    turnUuid: "root-terminal-fold",
    runRootTurnId: "root-terminal-fold",
    status: "completed",
    lifecycle: "terminal",
  };
  const operations = [
    {
      ...base,
      opId: "run:root-terminal-fold",
      opType: "run",
      displaySeq: 10,
      payload: { status: "completed" },
      createdAtMs: 1000,
      updatedAtMs: 9000,
    },
    {
      ...base,
      opId: "msg:root-terminal-fold",
      opType: "user_message",
      displaySeq: 20,
      payload: { text: "run agents" },
      createdAtMs: 1100,
      updatedAtMs: 1100,
    },
    {
      ...base,
      opId: "agent:task-one",
      opType: "agent",
      displaySeq: 30,
      taskUuid: "task-one",
      payload: { taskUuid: "task-one", status: "completed", result: { summary: "one" } },
      createdAtMs: 1200,
      updatedAtMs: 2000,
    },
    {
      ...base,
      opId: "agent-supervision:first-terminal",
      opType: "agent_supervision",
      displaySeq: 40,
      payload: { statusText: "全部 Agent 已完成", preview: "真实完成", active: false, wakeReason: "task_notification" },
      createdAtMs: 2100,
      updatedAtMs: 2100,
    },
    {
      ...base,
      opId: "agent:merged-placeholder-after-terminal",
      opType: "agent",
      displaySeq: 45,
      payload: { merged: true, mergedTo: "agent:task-one", status: "cancelled" },
      createdAtMs: 2150,
      updatedAtMs: 2150,
    },
    {
      ...base,
      opId: "agent-supervision:repeat-one",
      opType: "agent_supervision",
      displaySeq: 50,
      payload: { statusText: "全部 Agent 已完成", preview: "正在统一汇总", active: false, wakeReason: "all_terminal" },
      createdAtMs: 2200,
      updatedAtMs: 2200,
    },
    {
      ...base,
      opId: "agent-supervision:repeat-two",
      opType: "agent_supervision",
      displaySeq: 60,
      payload: { statusText: "全部 Agent 已完成", preview: "正在统一汇总", active: false, wakeReason: "all_terminal" },
      createdAtMs: 2300,
      updatedAtMs: 2300,
    },
    {
      ...base,
      opId: "agent:task-two",
      opType: "agent",
      displaySeq: 70,
      taskUuid: "task-two",
      payload: { taskUuid: "task-two", status: "completed", result: { summary: "two" } },
      createdAtMs: 2400,
      updatedAtMs: 3000,
    },
    {
      ...base,
      opId: "agent-supervision:second-terminal",
      opType: "agent_supervision",
      displaySeq: 80,
      payload: { statusText: "全部 Agent 已完成", preview: "第二轮真实完成", active: false, wakeReason: "task_notification" },
      createdAtMs: 3100,
      updatedAtMs: 3100,
    },
  ];

  const assistant = projectOperationMessages(operations).find((item) => item.role === "assistant");
  assert.ok(assistant);
  const mainTimelineStatuses = assistant.localTimeline.filter((item) => item.agentSupervision);
  assert.equal(mainTimelineStatuses.length, 0);
  const agentCards = assistant.localTimeline.filter((item) => item.kind === "tool");
  assert.equal(agentCards.length, 2);
  const firstMonitorEvents = agentCards[0].livePayload.recentEvents.filter((item) => item.kind === "agent_supervision");
  const secondMonitorEvents = agentCards[1].livePayload.recentEvents.filter((item) => item.kind === "agent_supervision");
  assert.deepEqual(firstMonitorEvents.map((item) => item.summary), ["全部 Agent 已完成", "全部 Agent 已完成"]);
  assert.deepEqual(secondMonitorEvents.map((item) => item.summary), ["全部 Agent 已完成"]);
});

test("merged agent placeholder cancel is not a task terminal refresh boundary", () => {
  assert.equal(isTerminalOperationFrame({
    action: "cancel",
    payload: {merged: true, mergedTo: "agent:task-1", status: "completed"},
  }), false);
  assert.equal(isTerminalOperationFrame({
    action: "cancel",
    payload: {status: "cancelled"},
  }), true);
  assert.equal(isTerminalOperationFrame({
    action: "end",
    payload: {status: "completed"},
  }), true);
});


test("operation frame reducer preserves target fields from live frames", () => {
  const store = { operationsById: new Map(), orderedOpIds: [], revisionByOpId: new Map(), lastFrameSeq: 0 };
  applyOperationFrame(store, {
    frameSeq: 1,
    opId: "agent-control:c1",
    opType: "agent_control",
    action: "create",
    revision: 1,
    displaySeq: 10,
    targetType: "task",
    targetId: "task-1",
    taskUuid: "task-1",
    runId: "run-1",
    payload: { taskUuid: "task-1", controlAction: "steer" },
    createdAtMs: 1000,
    updatedAtMs: 1000,
  });
  const op = store.operationsById.get("agent-control:c1");
  assert.equal(op.targetType, "task");
  assert.equal(op.targetId, "task-1");
  assert.equal(op.taskUuid, "task-1");
  assert.equal(op.runId, "run-1");
});

test("model_retry projects at its first display sequence between partial assistant segments and excludes child tasks", () => {
  const operations = [
    {opId: "user:root", opType: "user_message", turnId: "turn-root", runRootTurnId: "turn-root", displaySeq: 10, revision: 1, lifecycle: "terminal", status: "completed", payload: {text: "开始"}, createdAtMs: 1000, updatedAtMs: 1000},
    {opId: "assistant:partial", opType: "assistant_message", turnId: "turn-root", runRootTurnId: "turn-root", displaySeq: 20, revision: 1, lifecycle: "active", status: "running", payload: {text: "第一段", complete: false}, createdAtMs: 2000, updatedAtMs: 2000},
    {opId: "model-retry:run-1:2", opType: "model_retry", turnId: "turn-root", runRootTurnId: "turn-root", displaySeq: 30, revision: 1, lifecycle: "active", status: "running", source: "model_retry", payload: {retry: {attempt: 2, max_retries: 5, wait_ms: 1200, reason: "rate_limit", summary: "当前账户请求过于频繁，请稍后再试", transportStatus: 503, upstreamStatus: 429, rootCause: {status: 429, classification: "rate_limit"}, attempts: [{status: 429}], details: {summary: "当前账户请求过于频繁，请稍后再试"}, active: true}, cancel_supported: true}, createdAtMs: 3000, updatedAtMs: 3000},
    {opId: "model-retry:child:1", opType: "model_retry", turnId: "turn-root", runRootTurnId: "turn-root", taskUuid: "task-child", displaySeq: 35, revision: 1, lifecycle: "active", status: "running", payload: {attempt: 1, maxAttempts: 3, active: true}, createdAtMs: 3500, updatedAtMs: 3500},
    {opId: "assistant:final", opType: "assistant_message", turnId: "turn-root", runRootTurnId: "turn-root", displaySeq: 40, revision: 1, lifecycle: "terminal", status: "completed", payload: {text: "第二段", complete: true}, createdAtMs: 4000, updatedAtMs: 4000},
  ];

  const assistant = projectOperationMessages(operations).find((message) => message.role === "assistant");
  assert.ok(assistant);
  assert.deepEqual(assistant.localTimeline.map((event) => event.kind), ["answer", "model_retry", "answer"]);
  const retry = assistant.localTimeline[1];
  assert.equal(retry.id, "model-retry:run-1:2");
  assert.equal(retry.startedAt, 3000);
  assert.deepEqual(retry.retry, {
    attempt: 2,
    maxAttempts: 5,
    waitMs: 1200,
    retryAtMs: 0,
    reason: "rate_limit",
    summary: "当前账户请求过于频繁，请稍后再试",
    error: "",
    transportStatus: 503,
    upstreamStatus: 429,
    rootCause: {status: 429, classification: "rate_limit"},
    attempts: [{status: 429}],
    details: {summary: "当前账户请求过于频繁，请稍后再试"},
    active: true,
    status: "running",
    cancellable: true,
    taskUuid: "",
  });
  assert.equal(assistant.localTimeline.some((event) => event.id === "model-retry:child:1"), false);
});

test("model_retry active to resumed patch keeps the original timeline position and identity", () => {
  const store = {operationsById: new Map(), orderedOpIds: [], revisionByOpId: new Map(), lastFrameSeq: 0};
  applyOperationFrame(store, {
    frameSeq: 1,
    opId: "model-retry:run-2:1",
    opType: "model_retry",
    action: "create",
    revision: 1,
    turnId: "turn-2",
    runRootTurnId: "turn-2",
    displaySeq: 25,
    source: "model_retry",
    status: "running",
    payload: {attempt: 1, maxAttempts: 4, waitMs: 500, reason: "timeout", active: true},
    createdAtMs: 2500,
    updatedAtMs: 2500,
  });
  applyOperationFrame(store, {
    frameSeq: 2,
    opId: "model-retry:run-2:1",
    opType: "model_retry",
    action: "patch",
    revision: 2,
    displaySeq: 99,
    source: "model_retry",
    status: "resumed",
    payload: {active: false, status: "resumed"},
    updatedAtMs: 3500,
  });

  const operation = store.operationsById.get("model-retry:run-2:1");
  assert.equal(operation.displaySeq, 25);
  assert.equal(operation.createdAtMs, 2500);
  const assistant = projectOperationMessages([...store.operationsById.values()]).find((message) => message.role === "assistant");
  const retry = assistant.localTimeline.find((event) => event.kind === "model_retry");
  assert.equal(retry.id, "model-retry:run-2:1");
  assert.equal(retry.retry.active, false);
  assert.equal(retry.retry.status, "resumed");
  assert.equal(retry.startedAt, 2500);
});

test("root run remains the sole source for the transient thinking row across supervision", () => {
  const activeRun = {opId: "run:root", opType: "run", turnId: "turn-root", displaySeq: 10, lifecycle: "active", status: "running", createdAtMs: 1000, payload: {status: "running"}};
  const activeAgent = {opId: "agent:task", opType: "agent", turnId: "turn-root", taskUuid: "task-1", displaySeq: 20, lifecycle: "active", status: "running", createdAtMs: 2000, payload: {status: "running"}};
  const supervision = {opId: "agent-supervision:root", opType: "agent_supervision", turnId: "turn-root", displaySeq: 30, lifecycle: "active", status: "running", createdAtMs: 3000, payload: {active: true}};

  const whileSupervising = deriveOperationRunState([activeRun, activeAgent, supervision]);
  assert.equal(whileSupervising.rootTurnRunning, true);
  assert.equal(whileSupervising.activeRootTurnId, "turn-root");
  assert.equal(whileSupervising.foregroundRunning, false);
  assert.equal(whileSupervising.backgroundRunning, true);
  const turns = [{turnUuid: "turn-root", events: [{kind: "tool", id: "agent:task"}]}];
  const projected = withTransientIdleThinking(turns, whileSupervising, {startedAtMs: 1000});
  assert.equal(projected[0].events.at(-1).id, "transient-run-thinking");

  const afterRootTerminal = deriveOperationRunState([{...activeRun, lifecycle: "terminal", status: "completed"}, activeAgent, supervision]);
  assert.equal(afterRootTerminal.rootTurnRunning, false);
  assert.equal(afterRootTerminal.activeRootTurnId, "");
  assert.equal(afterRootTerminal.backgroundRunning, true);
  assert.strictEqual(withTransientIdleThinking(turns, afterRootTerminal, {startedAtMs: 1000}), turns);
});

test("transient thinking row matches the real buildTurns user identity shape", () => {
  const runState = {
    rootTurnRunning: true,
    activeRootTurnId: "turn-root",
  };
  const turns = [{
    id: "message-1",
    user: {turnUuid: "turn-root", content: "hello"},
    events: [],
  }];

  const projected = withTransientIdleThinking(turns, runState, {startedAtMs: 1000});

  assert.notStrictEqual(projected, turns);
  assert.equal(projected[0].events.length, 1);
  assert.equal(projected[0].events[0].id, "transient-run-thinking");
});

test("stopped acknowledgement converges state and list without directly clearing a real active projection", () => {
  const socket = {};
  let running = true;
  let status = "运行中";
  const stateRefreshes = [];
  let listRefreshes = 0;
  const converge = (message = {type: "stopped"}) => convergeStoppedAcknowledgement({
    message,
    sourceSocket: socket,
    activeSocket: socket,
    sourceConversationUuid: "conversation-current",
    socketConversationUuid: "conversation-current",
    activeConversationUuid: "conversation-current",
    setStatus: (value) => { status = value; },
    refreshCurrentState: (reason) => { stateRefreshes.push(reason); },
    refreshConversationList: () => { listRefreshes += 1; },
  });

  assert.equal(converge(), true);
  assert.equal(status, "停止已确认，正在同步状态");
  assert.equal(running, true);
  assert.deepEqual(stateRefreshes, [{source: "stopped_ack", stopAtMs: 0}]);
  assert.equal(listRefreshes, 1);

  // A duplicate ACK before an authoritative refresh remains a convergence hint;
  // it never forces the locally projected running bit to false.
  assert.equal(converge(), true);
  assert.equal(running, true);
  assert.equal(stateRefreshes.length, 2);
  assert.equal(listRefreshes, 2);

  // A terminal frame may race before the ACK. Reapplying the same terminal fact
  // is harmless and retains the server/frame-owned running result.
  running = false;
  status = "已停止";
  assert.equal(converge({type: "stopped", reason: "已停止", stopAtMs: 1234}), true);
  assert.equal(running, false);
  assert.equal(status, "已停止");
  assert.deepEqual(stateRefreshes.at(-1), {source: "stopped_ack", stopAtMs: 1234});
  assert.equal(listRefreshes, 3);
});

test("stopped acknowledgement rejects delayed old socket and old conversation messages", () => {
  const oldSocket = {};
  const activeSocket = {};
  let sideEffects = 0;
  const options = {
    message: {type: "stopped"},
    sourceSocket: oldSocket,
    activeSocket,
    sourceConversationUuid: "conversation-old",
    socketConversationUuid: "conversation-new",
    activeConversationUuid: "conversation-new",
    setStatus: () => { sideEffects += 1; },
    refreshCurrentState: () => { sideEffects += 1; },
    refreshConversationList: () => { sideEffects += 1; },
  };

  assert.equal(convergeStoppedAcknowledgement(options), false);
  assert.equal(convergeStoppedAcknowledgement({
    ...options,
    sourceSocket: activeSocket,
    socketConversationUuid: "conversation-old",
    activeConversationUuid: "conversation-new",
  }), false);
  assert.equal(sideEffects, 0);
});

function terminalRefreshHarness(refresh) {
  let conversationUuid = "conversation-a";
  let socketConversationUuid = "conversation-a";
  let socket = {id: "socket-a", active: true};
  let componentActive = true;
  let nextTimer = 1;
  const timers = new Map();
  const scheduler = createTerminalStateRefreshScheduler({
    delayMs: 650,
    getConversationUuid: () => conversationUuid,
    getSocket: () => socket,
    getSocketConversationUuid: () => socketConversationUuid,
    isComponentActive: () => componentActive,
    isSocketActive: (candidate) => Boolean(candidate?.active),
    scheduleTimeout: (callback) => {
      const handle = nextTimer++;
      timers.set(handle, callback);
      return handle;
    },
    clearScheduledTimeout: (handle) => timers.delete(handle),
    refresh,
  });
  return {
    scheduler,
    timers,
    latestTimer: () => timers.get(Math.max(...timers.keys())),
    setConversation: (value) => { conversationUuid = value; },
    setSocketConversation: (value) => { socketConversationUuid = value; },
    replaceSocket: (value) => { socket = value; },
    setComponentActive: (value) => { componentActive = value; },
  };
}

test("terminal state refresh rejects a scheduled ACK after conversation switch", async () => {
  let requests = 0;
  const harness = terminalRefreshHarness(async () => { requests += 1; });
  assert.equal(harness.scheduler.schedule({source: "stopped_ack"}), true);
  const scheduled = harness.latestTimer();

  harness.setConversation("conversation-b");
  harness.setSocketConversation("conversation-b");
  await scheduled();

  assert.equal(requests, 0);
});

test("terminal state refresh rejects a scheduled ACK after socket replacement", async () => {
  let requests = 0;
  const harness = terminalRefreshHarness(async () => { requests += 1; });
  assert.equal(harness.scheduler.schedule({source: "stopped_ack"}), true);
  const scheduled = harness.latestTimer();

  harness.replaceSocket({id: "socket-b", active: true});
  await scheduled();

  assert.equal(requests, 0);
});

test("terminal state refresh invalidation and disposal reject an already queued timer", async () => {
  let requests = 0;
  const closeHarness = terminalRefreshHarness(async () => { requests += 1; });
  assert.equal(closeHarness.scheduler.schedule({source: "stopped_ack"}), true);
  const afterClose = closeHarness.latestTimer();
  closeHarness.scheduler.invalidate();
  await afterClose();

  const unmountHarness = terminalRefreshHarness(async () => { requests += 1; });
  assert.equal(unmountHarness.scheduler.schedule({source: "stopped_ack"}), true);
  const afterUnmount = unmountHarness.latestTimer();
  unmountHarness.setComponentActive(false);
  unmountHarness.scheduler.dispose();
  await afterUnmount();

  assert.equal(requests, 0);
});

test("terminal state refresh drops a late state response before apply and reconnect", async () => {
  let resolveRequest;
  const requested = [];
  const applied = [];
  const connected = [];
  const requestPromise = new Promise((resolve) => { resolveRequest = resolve; });
  const harness = terminalRefreshHarness(({conversationUuid, isCurrent}) => runGuardedConversationStateRefresh({
    conversationUuid,
    isCurrent,
    requestState: (uuid) => {
      requested.push(uuid);
      return requestPromise;
    },
    applyState: (state) => applied.push(state),
    connectState: async (uuid) => { connected.push(uuid); },
  }));
  assert.equal(harness.scheduler.schedule({source: "stopped_ack"}), true);
  const refresh = harness.latestTimer()();
  await Promise.resolve();
  assert.deepEqual(requested, ["conversation-a"]);

  harness.setConversation("conversation-b");
  harness.setSocketConversation("conversation-b");
  resolveRequest({conversationUuid: "conversation-a", running: false});
  await refresh;

  assert.deepEqual(applied, []);
  assert.deepEqual(connected, []);
});

test("duplicate terminal ACK scheduling is debounced and only the latest token refreshes", async () => {
  const reasons = [];
  const harness = terminalRefreshHarness(async ({reason}) => { reasons.push(reason.sequence); });
  assert.equal(harness.scheduler.schedule({sequence: 1}), true);
  const first = harness.latestTimer();
  assert.equal(harness.scheduler.schedule({sequence: 2}), true);
  const second = harness.latestTimer();

  await first();
  await second();

  assert.deepEqual(reasons, [2]);
});

test("tool summary projects a collapsed card without materializing lazy result detail", () => {
  const projected = projectOperationMessages([
    {
      opId: "msg:summary-tool",
      opType: "user_message",
      turnId: "turn-summary-tool",
      displaySeq: 1,
      status: "completed",
      lifecycle: "terminal",
      payload: {role: "user", text: "执行命令"},
    },
    {
      opId: "tool:summary-tool",
      opType: "tool",
      turnId: "turn-summary-tool",
      displaySeq: 2,
      status: "completed",
      lifecycle: "terminal",
      detailAvailable: true,
      detailLoaded: false,
      payload: {
        toolCallId: "summary-tool",
        name: "Bash",
        toolName: "Bash",
        previewArguments: '{"description":"检查服务状态"}',
        preview: "💻 Bash: command status",
        resultState: "ok",
      },
    },
  ]);

  const assistant = projected.find((message) => Array.isArray(message.localTimeline));
  const tool = assistant?.localTimeline.find((event) => event.kind === "tool");
  assert.ok(tool);
  assert.equal(tool.calls[0].name, "Bash");
  assert.equal(tool.calls[0].previewArguments, '{"description":"检查服务状态"}');
  assert.equal(tool.calls[0].preview, "💻 Bash: command status");
  // The bounded source occupies the established arguments slot for the
  // collapsed-card renderer; detailLoaded still remains false.
  assert.equal(tool.calls[0].arguments, '{"description":"检查服务状态"}');
  assert.equal(tool.result, null);
  assert.equal(tool.operation.detailAvailable, true);
  assert.equal(tool.operation.detailLoaded, false);
});

test("TaskMemory summary carries action and target facts through lazy projection", () => {
  const projected = projectOperationMessages([
    {
      opId: "msg:task-memory-summary",
      opType: "user_message",
      turnId: "turn-task-memory-summary",
      displaySeq: 1,
      status: "completed",
      lifecycle: "terminal",
      payload: {role: "user", text: "记录要点"},
    },
    {
      opId: "tool:task-memory-summary",
      opType: "tool",
      turnId: "turn-task-memory-summary",
      displaySeq: 2,
      status: "completed",
      lifecycle: "terminal",
      detailAvailable: true,
      detailLoaded: false,
      payload: {
        toolCallId: "task-memory-summary",
        name: "TaskMemory",
        resultState: "ok",
        previewArguments: '{"action":"delete","memoryLabel":"记录案件要点","memoryUuid":"memory-123"}',
      },
    },
  ]);

  const assistant = projected.find((message) => Array.isArray(message.localTimeline));
  const tool = assistant?.localTimeline.find((event) => event.kind === "tool");
  assert.ok(tool);
  assert.equal(tool.calls[0].arguments, '{"action":"delete","memoryLabel":"记录案件要点","memoryUuid":"memory-123"}');
  assert.equal(tool.calls[0].previewArguments, '{"action":"delete","memoryLabel":"记录案件要点","memoryUuid":"memory-123"}');
  assert.equal("taskMemorySummary" in tool.calls[0], false);
});
