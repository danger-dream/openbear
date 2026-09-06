import assert from "node:assert/strict";
import test from "node:test";

import {
  agentInfoResultView,
  assignmentStatusView,
  currentAgentInstanceView,
  localAgentInstanceEnvelope,
  normalizeAgentInstanceEnvelope,
} from "./agentInstancePresentation.js";

const sessionA = {
  agentId: "agent-aaaaaaaa-1111",
  sessionUuid: "agent-aaaaaaaa-1111",
  sessionKind: "independent",
  status: "active",
  activeTaskUuid: "",
  lastTaskUuid: "task-22222222-bbbb",
  contextTaskUuid: "task-22222222-bbbb",
  contextRevision: 7,
  revision: 9,
  turnCount: 2,
  canContinue: true,
  continuationBlocker: "",
};

const task1 = {
  agentId: sessionA.agentId,
  taskUuid: "task-11111111-aaaa",
  sessionKind: "independent",
  sessionTurn: 1,
  continuedFromTaskUuid: "",
  title: "调查问题",
  status: "completed",
  grantedTools: ["Read", "Edit"],
  contextSource: {taskUuid: "", revision: 1, state: "fresh"},
};

const task2 = {
  agentId: sessionA.agentId,
  taskUuid: "task-22222222-bbbb",
  sessionKind: "independent",
  sessionTurn: 2,
  continuedFromTaskUuid: task1.taskUuid,
  title: "修复并自测",
  status: "completed",
  grantedTools: ["Read", "Edit", "EditBatch", "Bash"],
  contextSource: {taskUuid: task1.taskUuid, revision: 6, state: "restored"},
};

test("independent instance keeps turns separate, sorts them, and never merges another same-role agent", () => {
  const anotherAgentTask = {
    ...task2,
    agentId: "agent-bbbbbbbb-2222",
    taskUuid: "task-other-role-peer",
    sessionTurn: 3,
    title: "同角色的另一实例",
  };
  const view = normalizeAgentInstanceEnvelope({
    ok: true,
    agentSession: sessionA,
    tasks: [anotherAgentTask, task2, task1],
    legacy: false,
  }, {currentTask: task2, currentSession: sessionA});

  assert.equal(view.legacy, false);
  assert.deepEqual(view.tasks.map((task) => task.taskUuid), [task1.taskUuid, task2.taskUuid]);
  assert.deepEqual(view.tasks.map((task) => task.sessionTurn), [1, 2]);
  assert.equal(view.tasks[0].statusView.label, "本轮已结束");
  assert.equal(view.tasks[1].statusView.label, "本轮已结束，可继续指派");
  assert.equal(view.tasks[1].contextSource.label, "已恢复上下文 · 任务 task-111 · r6");
});

test("granted tools are rendered exactly as returned and Edit does not imply EditBatch", () => {
  const view = normalizeAgentInstanceEnvelope({agentSession: sessionA, tasks: [task1], legacy: false}, {
    currentTask: task1,
    currentSession: sessionA,
  });
  assert.deepEqual(view.tasks[0].grantedTools, ["Read", "Edit"]);
  assert.equal(view.tasks[0].grantedTools.includes("EditBatch"), false);
});

test("missing sessionKind is legacy and legacy history is limited to the requested task", () => {
  const currentTask = {taskUuid: "legacy-current", title: "旧任务", status: "failed", grantedTools: ["Read"]};
  const legacy = normalizeAgentInstanceEnvelope({
    ok: true,
    agentSession: {sessionUuid: "historical-role-session", canContinue: true},
    tasks: [
      currentTask,
      {taskUuid: "legacy-unrelated", title: "同角色但无连续性", status: "completed"},
    ],
    legacy: true,
  }, {currentTask});

  assert.equal(legacy.legacy, true);
  assert.deepEqual(legacy.tasks.map((task) => task.taskUuid), ["legacy-current"]);
  assert.equal(legacy.tasks[0].statusView.label, "本轮失败");
  assert.equal(currentAgentInstanceView({currentTask, currentSession: {canContinue: true}}).turnLabel, "单次记录");
});

test("completed only advertises continuation when the session explicitly allows the current head", () => {
  assert.equal(assignmentStatusView("completed", {canContinue: false}).label, "本轮已结束");
  assert.equal(assignmentStatusView("completed", {canContinue: true}).label, "本轮已结束，可继续指派");
  assert.equal(currentAgentInstanceView({
    currentTask: task2,
    currentSession: {...sessionA, canContinue: false, continuationBlocker: "实例已关闭"},
  }).statusView.label, "本轮已结束");
  assert.equal(currentAgentInstanceView({
    currentTask: task2,
    currentSession: sessionA,
  }).statusView.label, "本轮已结束，可继续指派");
});

test("local fallback preserves exactly the current task when instance API is unavailable", () => {
  const fallback = localAgentInstanceEnvelope({currentTask: task2, currentSession: sessionA});
  assert.equal(fallback.legacy, false);
  assert.deepEqual(fallback.tasks.map((task) => task.taskUuid), [task2.taskUuid]);
});

test("AgentInfo is a read-only structured snapshot, not an execution activity", () => {
  const view = agentInfoResultView("AgentInfo", JSON.stringify({
    ok: true,
    agentSession: sessionA,
    tasks: [task1, task2],
    legacy: false,
  }));
  assert.equal(view.isAgentInfo, true);
  assert.equal(view.readOnly, true);
  assert.equal(view.hasData, true);
  assert.equal(view.availabilityLabel, "空闲，可继续指派");
  assert.deepEqual(view.tasks.map((task) => task.title), ["调查问题", "修复并自测"]);
  assert.equal(agentInfoResultView("Read", "{}").isAgentInfo, false);
});
