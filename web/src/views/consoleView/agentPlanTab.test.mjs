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
after(() => {
  Api.rathTaskPlan = originalPlanRequest;
  Api.rathTaskEvents = originalEventsRequest;
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

function taskEvent(task) {
  return {livePayload: {task}, calls: [], result: {}};
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

function createCard(task, {previewOnly = false, open = true} = {}) {
  const props = reactive({
    event: taskEvent(task),
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
  assert.equal(running.bindings.summarySubtitle.value, "· 分析会话首屏加载链路 · 运行中");
  assert.equal(running.bindings.panelTitle.value, "Agent 运行中", "expanded panel heading remains unchanged");
  assert.equal(completed.bindings.summaryTitle.value, "Agent");
  assert.equal(completed.bindings.summarySubtitle.value, "· 分析会话首屏加载链路 · 执行完成");
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
