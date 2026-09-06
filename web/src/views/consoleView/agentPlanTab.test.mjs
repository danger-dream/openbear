import test, {after} from "node:test";
import assert from "node:assert/strict";
import {readFile, unlink, writeFile} from "node:fs/promises";
import {compileScript, parse} from "@vue/compiler-sfc";
import {effectScope, nextTick, reactive} from "vue";

import {Api} from "../../api.js";

const componentUrl = new URL("./AgentEventCard.vue", import.meta.url);
const generatedUrl = new URL(`./.AgentEventCard.plan-tab-${process.pid}.mjs`, import.meta.url);
const displayStubUrl = new URL(`./.AgentEventCard.display-stub-${process.pid}.mjs`, import.meta.url);

const displayStubSource = `
export const agentOutputDisplay = (value) => String(value || "");
export const fmtNum = (value) => String(value || 0);
export const recentEventMessage = () => "";
export const toolPreview = () => "";
export const agentStatusMeta = (status) => ({label: String(status || ""), cls: String(status || "partial"), statusIcon: null});
export const agentRowMetricChips = () => [];
export const agentRowArgumentsDisplay = () => "";
export const agentRowOutputSection = () => null;
export function agentTasks(event) {
  const payload = event?.livePayload || {};
  if (Array.isArray(payload.results)) return payload.results.map((item) => item?.task || item?.result?.task).filter(Boolean);
  const task = payload.task || payload.result?.task;
  return task ? [task] : [];
}
export function agentDisplayState(event) {
  const task = agentTasks(event)[0] || {};
  const status = String(task.status || "running");
  const title = String(task.title || "");
  const label = status === "completed" ? "执行完成" : status;
  return {
    summary: {toolName: "Agent", label, cls: status, title: "Agent", preview: title, countText: "Agent", statusIcon: null},
    rows: task.taskUuid || task.task_uuid ? [{taskUuid: task.taskUuid || task.task_uuid, status, title, hasArguments: false, hasOutput: false, metrics: {}}] : [],
    metricChips: [],
    recentLines: [],
  };
}
`;

let AgentEventCard;
try {
  const source = await readFile(componentUrl, "utf8");
  const {descriptor, errors} = parse(source, {filename: "AgentEventCard.vue"});
  assert.deepEqual(errors, []);
  let compiled = compileScript(descriptor, {id: "agent-plan-tab-test"}).content;
  compiled = compiled
    .replace(/^import (\w+) from "(\.\/[^"\n]+\.vue)";$/gm, "const $1 = {};")
    .replace('from "./display.js"', `from "./${displayStubUrl.pathname.split("/").at(-1)}"`);
  await writeFile(displayStubUrl, displayStubSource);
  await writeFile(generatedUrl, compiled);
  ({default: AgentEventCard} = await import(`${generatedUrl.href}?test=${Date.now()}`));
} finally {
  await unlink(generatedUrl).catch(() => {});
  await unlink(displayStubUrl).catch(() => {});
}

const originalPlanRequest = Api.rathTaskPlan;
const originalEventsRequest = Api.rathTaskEvents;
const originalInstanceRequest = Api.rathAgentInstance;
after(() => {
  Api.rathTaskPlan = originalPlanRequest;
  Api.rathTaskEvents = originalEventsRequest;
  Api.rathAgentInstance = originalInstanceRequest;
});

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return {promise, resolve, reject};
}

function taskEvent(task, agentSession = null) {
  return {livePayload: {task, ...(agentSession ? {agentSession} : {})}, calls: [], result: {}};
}

function plannedSnapshot(version = 1) {
  const plan = {
    title: `Plan v${version}`,
    objective: "Keep the Plan tab visible",
    steps: [{id: "step-1", title: "Run", objective: "Run", method: "Test", required: true, criteria: []}],
    finalOutputs: [],
  };
  const current = {version, status: "approved", plan_type: "initial", plan};
  return {
    ok: true,
    task: {status: "running"},
    state: {phase: "executing", active_plan_version: version, pending_plan_version: 0, current_step_id: "step-1"},
    current,
    versions: [current],
    steps: [],
    evidence: [],
    decisions: [],
  };
}

function noPlanSnapshot() {
  return {
    ok: true,
    task: {status: "running"},
    state: {phase: "drafting", active_plan_version: 0, pending_plan_version: 0},
    current: null,
    // A non-empty versions array intentionally proves that visibility does not
    // use versions.length as a proxy for a real Plan.
    versions: [{version: 1, status: "pending", plan: null}],
    steps: [],
    evidence: [],
    decisions: [],
  };
}

function installApi(planRequest) {
  const planCalls = [];
  const eventCalls = [];
  Api.rathTaskPlan = (...args) => {
    planCalls.push(args);
    return planRequest(...args);
  };
  Api.rathTaskEvents = async (...args) => {
    eventCalls.push(args);
    return {
      ok: true,
      events: [{seq: 1, ts: 1, kind: "task_started", summary: "started", detail: {}}],
      total: 1,
      monitorTotal: 1,
      hasMore: false,
      nextBeforeSeq: 0,
    };
  };
  return {planCalls, eventCalls};
}

function createCard(task, {previewOnly = false, open = true, agentSession = null} = {}) {
  const props = reactive({
    event: taskEvent(task, agentSession),
    conversationUuid: "conversation-1",
    turnId: "turn-1",
    index: 0,
    detailKey: () => "legacy-agent-detail",
    isDetailOpen: () => open,
    onDetailsToggle: () => {},
    previewOnly,
  });
  const scope = effectScope();
  const bindings = scope.run(() => AgentEventCard.setup(props, {expose: () => {}}));
  return {props, bindings, stop: () => scope.stop()};
}

function tabIds(bindings) {
  return bindings.panelTabs.value.map((item) => item.id);
}

async function flushAsync() {
  await Promise.resolve();
  await Promise.resolve();
  await new Promise((resolve) => setImmediate(resolve));
  await nextTick();
}

test("single Agent summary uses its task title for running and completed states", (t) => {
  installApi(async () => noPlanSnapshot());
  const running = createCard({taskUuid: "task-running", title: "分析会话首屏加载链路", status: "running"}, {previewOnly: true});
  const completed = createCard({taskUuid: "task-completed", title: "分析会话首屏加载链路", status: "completed"}, {previewOnly: true});
  t.after(running.stop);
  t.after(completed.stop);

  assert.equal(running.bindings.summaryTitle.value, "Agent");
  assert.equal(running.bindings.summarySubtitle.value, "· 分析会话首屏加载链路 · 本轮执行中");
  assert.equal(running.bindings.panelTitle.value, "Agent 运行中", "expanded panel heading remains unchanged");
  assert.equal(completed.bindings.summaryTitle.value, "Agent");
  assert.equal(completed.bindings.summarySubtitle.value, "· 分析会话首屏加载链路 · 本轮已结束");
});

test("planMode=direct hides the Plan tab on first open without blocking workspace loading", async (t) => {
  const pendingPlan = deferred();
  const requests = installApi(() => pendingPlan.promise);
  const card = createCard({taskUuid: "task-direct", planMode: "direct", status: "running"});
  t.after(card.stop);

  assert.equal(card.bindings.planCapability.value, false);
  assert.equal(card.bindings.planResolved.value, false);
  assert.equal(card.bindings.panelTab.value, "activity");
  assert.equal(tabIds(card.bindings).includes("plan"), false);
  assert.equal(requests.planCalls.length, 1, "direct tasks still load workspace data for launch information");

  pendingPlan.resolve(noPlanSnapshot());
  await flushAsync();
});

test("managed tasks with a real Plan keep the tab, default selection, recent activity, and refresh behavior", async (t) => {
  let snapshot = plannedSnapshot(1);
  const requests = installApi(async () => snapshot);
  const card = createCard({taskUuid: "task-managed", planMode: "managed", status: "running"});
  t.after(card.stop);

  assert.equal(card.bindings.panelTab.value, "plan");
  assert.equal(tabIds(card.bindings).includes("plan"), true);
  await flushAsync();

  assert.equal(card.bindings.planResolved.value, true);
  assert.equal(card.bindings.hasPlan.value, true);
  assert.equal(card.bindings.panelTab.value, "plan");
  assert.equal(tabIds(card.bindings).includes("plan"), true);
  assert.equal(card.bindings.activityLoaded.value, true, "Plan workspace keeps its recent activity feed");
  assert.ok(requests.eventCalls.length >= 1);

  snapshot = plannedSnapshot(2);
  await card.bindings.loadWorkspace(true);
  await flushAsync();
  assert.equal(requests.planCalls.length, 2);
  assert.equal(card.bindings.workspaceData.value.current.version, 2);
  assert.equal(card.bindings.hasPlan.value, true);
  assert.equal(card.bindings.panelTab.value, "plan");
  assert.equal(tabIds(card.bindings).includes("plan"), true);
});

test("managed and legacy tasks hide a resolved no-Plan tab and fall back to prepared activity", async (t) => {
  for (const [label, planMode] of [["managed", "managed"], ["legacy", undefined]]) {
    const requests = installApi(async () => noPlanSnapshot());
    const task = {taskUuid: `task-${label}`, status: "running"};
    if (planMode) task.planMode = planMode;
    const card = createCard(task);
    t.after(card.stop);

    assert.equal(card.bindings.planCapability.value, true, `${label} remains compatible before resolution`);
    assert.equal(card.bindings.planResolved.value, false);
    assert.equal(card.bindings.panelTab.value, "plan");
    assert.equal(tabIds(card.bindings).includes("plan"), true);
    await flushAsync();

    assert.equal(card.bindings.workspaceData.value.versions.length, 1);
    assert.equal(card.bindings.workspaceData.value.task.status, "running");
    assert.equal(card.bindings.planResolved.value, true);
    assert.equal(card.bindings.hasPlan.value, false);
    assert.equal(tabIds(card.bindings).includes("plan"), false);
    assert.equal(card.bindings.panelTab.value, "activity");
    assert.equal(card.bindings.activityLoaded.value, true);
    assert.ok(requests.eventCalls.length >= 1, `${label} no-Plan fallback prepares activity data`);
  }
});

test("managed loading and load errors remain unknown instead of hiding the Plan tab", async (t) => {
  const pendingPlan = deferred();
  installApi(() => pendingPlan.promise);
  const card = createCard({taskUuid: "task-error", planMode: "managed", status: "running"});
  t.after(card.stop);

  assert.equal(card.bindings.workspaceLoading.value, true);
  assert.equal(card.bindings.planResolved.value, false);
  assert.equal(tabIds(card.bindings).includes("plan"), true);
  assert.equal(card.bindings.panelTab.value, "plan");

  pendingPlan.reject(new Error("workspace unavailable"));
  await flushAsync();
  assert.equal(card.bindings.workspaceLoading.value, false);
  assert.equal(card.bindings.workspaceData.value, null);
  assert.equal(card.bindings.planResolved.value, false);
  assert.match(card.bindings.workspaceError.value, /workspace unavailable/);
  assert.equal(tabIds(card.bindings).includes("plan"), true);
  assert.equal(card.bindings.panelTab.value, "plan");
});

test("task switching clears old hasPlan state and rejects old in-flight workspace data", async (t) => {
  const oldRefresh = deferred();
  const newLoad = deferred();
  let oldCalls = 0;
  const requests = installApi((_conversationUuid, taskUuid) => {
    if (taskUuid === "task-old") {
      oldCalls += 1;
      return oldCalls === 1 ? Promise.resolve(plannedSnapshot(1)) : oldRefresh.promise;
    }
    assert.equal(taskUuid, "task-new");
    return newLoad.promise;
  });
  const card = createCard({taskUuid: "task-old", planMode: "managed", status: "running"});
  t.after(card.stop);
  await flushAsync();

  assert.equal(card.bindings.workspaceTaskUuid.value, "task-old");
  assert.equal(card.bindings.hasPlan.value, true);
  const refreshPromise = card.bindings.loadWorkspace(true);
  await Promise.resolve();
  card.props.event = taskEvent({taskUuid: "task-new", planMode: "managed", status: "running"});
  await nextTick();

  assert.equal(card.bindings.taskUuid.value, "task-new");
  assert.equal(card.bindings.workspaceData.value, null);
  assert.equal(card.bindings.workspaceTaskUuid.value, "");
  assert.equal(card.bindings.planResolved.value, false);
  assert.equal(card.bindings.hasPlan.value, false);
  assert.equal(tabIds(card.bindings).includes("plan"), true, "new managed task starts in compatible unknown state");

  oldRefresh.resolve(plannedSnapshot(2));
  await refreshPromise;
  await flushAsync();
  assert.equal(requests.planCalls.some(([, taskUuid]) => taskUuid === "task-new"), true);
  assert.equal(card.bindings.workspaceData.value, null, "old task response cannot populate the new task");
  assert.equal(card.bindings.hasPlan.value, false);

  newLoad.resolve(noPlanSnapshot());
  await flushAsync();
  assert.equal(card.bindings.workspaceTaskUuid.value, "task-new");
  assert.equal(card.bindings.hasPlan.value, false);
  assert.equal(tabIds(card.bindings).includes("plan"), false);
  assert.equal(card.bindings.panelTab.value, "activity");
});

test("preview-only Agent cards do not enter workspace or event request paths", async (t) => {
  const requests = installApi(async () => plannedSnapshot());
  const card = createCard({taskUuid: "task-preview", planMode: "managed", status: "running"}, {previewOnly: true});
  t.after(card.stop);
  await flushAsync();

  assert.equal(requests.planCalls.length, 0);
  assert.equal(requests.eventCalls.length, 0);
  assert.equal(card.bindings.isOpen.value, false);
});

test("instance history is requested only when its tab opens and keeps all turns of one agent", async (t) => {
  installApi(async () => noPlanSnapshot());
  const instanceCalls = [];
  const session = {
    agentId: "agent-a", sessionKind: "independent", status: "active", activeTaskUuid: "",
    lastTaskUuid: "task-2", contextRevision: 5, turnCount: 2, canContinue: true,
  };
  Api.rathAgentInstance = async (...args) => {
    instanceCalls.push(args);
    return {
      ok: true,
      agentSession: session,
      legacy: false,
      tasks: [
        {taskUuid: "task-1", agentId: "agent-a", sessionKind: "independent", sessionTurn: 1, title: "调查", status: "completed", grantedTools: ["Read"], contextSource: {state: "fresh", revision: 1}},
        {taskUuid: "task-2", agentId: "agent-a", sessionKind: "independent", sessionTurn: 2, title: "修复", status: "completed", grantedTools: ["Read", "Edit"], continuedFromTaskUuid: "task-1", contextSource: {state: "restored", taskUuid: "task-1", revision: 4}},
      ],
    };
  };
  const card = createCard({taskUuid: "task-2", agentId: "agent-a", sessionKind: "independent", sessionTurn: 2, title: "修复", status: "completed"}, {agentSession: session});
  t.after(card.stop);
  await flushAsync();

  assert.equal(instanceCalls.length, 0, "opening the Agent panel does not eagerly request instance history");
  card.bindings.selectTab("instance");
  await flushAsync();
  assert.equal(instanceCalls.length, 1);
  assert.deepEqual(card.bindings.visibleInstanceData.value.tasks.map((task) => task.taskUuid), ["task-1", "task-2"]);
  assert.equal(card.bindings.visibleInstanceData.value.tasks[1].statusView.label, "本轮已结束，可继续指派");
  assert.equal(card.bindings.instanceView.value.turnLabel, "第 2 次指派");
});

test("late instance response for an old task cannot replace the new task state", async (t) => {
  installApi(async () => noPlanSnapshot());
  const oldRequest = deferred();
  const instanceCalls = [];
  Api.rathAgentInstance = async (_conversationUuid, requestedTaskUuid) => {
    instanceCalls.push(requestedTaskUuid);
    if (requestedTaskUuid === "task-old") return oldRequest.promise;
    return {
      ok: true,
      legacy: false,
      agentSession: {agentId: "agent-new", sessionKind: "independent", activeTaskUuid: "task-new", lastTaskUuid: "task-new", turnCount: 1, canContinue: false},
      tasks: [{taskUuid: "task-new", agentId: "agent-new", sessionKind: "independent", sessionTurn: 1, title: "新轮", status: "running", grantedTools: ["Read"], contextSource: {state: "fresh", revision: 1}}],
    };
  };
  const card = createCard({taskUuid: "task-old", agentId: "agent-old", sessionKind: "independent", sessionTurn: 1, title: "旧轮", status: "completed"}, {
    agentSession: {agentId: "agent-old", sessionKind: "independent", lastTaskUuid: "task-old", canContinue: true},
  });
  t.after(card.stop);
  card.bindings.selectTab("instance");
  await Promise.resolve();

  card.props.event = taskEvent(
    {taskUuid: "task-new", agentId: "agent-new", sessionKind: "independent", sessionTurn: 1, title: "新轮", status: "running"},
    {agentId: "agent-new", sessionKind: "independent", activeTaskUuid: "task-new", lastTaskUuid: "task-new", canContinue: false},
  );
  await nextTick();
  oldRequest.resolve({
    ok: true,
    legacy: false,
    agentSession: {agentId: "agent-old", sessionKind: "independent", lastTaskUuid: "task-old", canContinue: true},
    tasks: [{taskUuid: "task-old", agentId: "agent-old", sessionKind: "independent", sessionTurn: 1, title: "旧轮", status: "completed"}],
  });
  await flushAsync();
  await flushAsync();

  assert.deepEqual(instanceCalls, ["task-old", "task-new"]);
  assert.equal(card.bindings.instanceTaskUuid.value, "task-new");
  assert.deepEqual(card.bindings.visibleInstanceData.value.tasks.map((task) => task.taskUuid), ["task-new"]);
  assert.equal(card.bindings.instanceView.value.statusView.label, "本轮执行中");
});

test("unsupported instance endpoint falls back to the current task without losing the panel", async (t) => {
  installApi(async () => noPlanSnapshot());
  Api.rathAgentInstance = async () => {
    const error = new Error("Request failed with status code 404");
    error.response = {status: 404, data: {error: "not_found"}};
    throw error;
  };
  const card = createCard({taskUuid: "legacy-task", title: "历史任务", status: "completed"});
  t.after(card.stop);
  card.bindings.selectTab("instance");
  await flushAsync();

  assert.match(card.bindings.instanceError.value, /not_found/);
  assert.equal(card.bindings.visibleInstanceData.value.legacy, true);
  assert.deepEqual(card.bindings.visibleInstanceData.value.tasks.map((task) => task.taskUuid), ["legacy-task"]);
});


test("reopening a collapsed panel backfills activity missed while pushes were dropped", async (t) => {
  // Pushes are discarded while the details element is closed; once the task is
  // terminal no further push can trigger gap recovery, so reopening must fetch
  // the newer tail over HTTP (paginating past a single page) instead of leaving
  // the process log frozen at the moment the panel was collapsed.
  const eventCalls = [];
  Api.rathTaskPlan = async () => noPlanSnapshot();
  Api.rathTaskEvents = async (conversationUuid, taskUuid, opts = {}) => {
    eventCalls.push(opts);
    const afterSeq = Number(opts.afterSeq || 0);
    if (afterSeq >= 4) return {ok: true, events: [], total: 5, monitorTotal: 0, hasMore: false, nextBeforeSeq: 0};
    if (afterSeq === 3) {
      return {
        ok: true,
        events: [
          {seq: 4, ts: 4, kind: "model_call_finished", summary: "模型调用完成", detail: {}},
          {seq: 5, ts: 5, kind: "task_completed", summary: "任务完成", detail: {}},
        ],
        total: 5, monitorTotal: 0, hasMore: false, nextBeforeSeq: 0,
      };
    }
    if (afterSeq === 2) {
      return {
        ok: true,
        events: [{seq: 3, ts: 3, kind: "model_stream_progress", summary: "模型流式输出中", detail: {}}],
        total: 5, monitorTotal: 0, hasMore: true, nextBeforeSeq: 0,
      };
    }
    return {
      ok: true,
      events: [
        {seq: 1, ts: 1, kind: "task_started", summary: "任务开始", detail: {}},
        {seq: 2, ts: 2, kind: "model_call_started", summary: "模型调用开始", detail: {}},
      ],
      total: 2, monitorTotal: 0, hasMore: false, nextBeforeSeq: 0,
    };
  };
  const openState = reactive({open: true});
  const props = reactive({
    event: taskEvent({taskUuid: "task-reopen", status: "running", planMode: "direct"}),
    conversationUuid: "conversation-1",
    turnId: "turn-1",
    index: 0,
    detailKey: () => "legacy-agent-detail",
    isDetailOpen: () => openState.open,
    onDetailsToggle: () => {},
    previewOnly: false,
  });
  const scope = effectScope();
  const bindings = scope.run(() => AgentEventCard.setup(props, {expose: () => {}}));
  t.after(() => scope.stop());
  await flushAsync();
  await flushAsync();
  assert.deepEqual(bindings.activityLines.value.map((item) => item.seq), [1, 2]);

  openState.open = false;
  await flushAsync();
  // Tail events 3-5 happen while the panel is collapsed; the task completes.
  props.event.livePayload.task.status = "completed";
  await flushAsync();
  assert.deepEqual(bindings.activityLines.value.map((item) => item.seq), [1, 2]);

  openState.open = true;
  await flushAsync();
  await flushAsync();
  assert.deepEqual(bindings.activityLines.value.map((item) => item.seq), [1, 2, 3, 4, 5]);
  const afterSeqs = eventCalls.map((opts) => Number(opts?.afterSeq || 0)).filter(Boolean);
  assert.deepEqual(afterSeqs, [2, 3]);
});
