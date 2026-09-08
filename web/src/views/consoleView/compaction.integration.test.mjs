import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import {computed, effectScope, nextTick, reactive, ref, watch} from "vue";
import {isContextCompactionOperation} from "../../timelineProjection.js";
import {runGuardedConversationStateRefresh} from "./terminalStateRefresh.js";

// Execute the real component's compaction, navigation watcher, loading guards
// and Vue reactivity. Only dialogs, HTTP and display/application seams are fake.
const source = fs.readFileSync(new URL("./ConsoleView.vue", import.meta.url), "utf8");
function between(start, end) {
  const a = source.indexOf(start);
  const b = source.indexOf(end, a + start.length);
  assert.ok(a >= 0 && b > a, `${start}..${end}`);
  return source.slice(a, b);
}
const actual = [
  between("const INITIAL_TIMELINE_LIMIT =", "const LOAD_EARLIER_SCROLL_THRESHOLD ="),
  between("const loading = ref(false);", "const sendPending = ref(false);"),
  between("const activeConversationUuid = computed(", "let runConfigInteractionGeneration ="),
  between("const serverCompacting = computed(", "const modelCallRows = computed("),
  between("const canSend = computed(() => {", "const modelGroups = computed("),
  between("function resetOperationStore(", "function clearUiCaches()"),
  between("async function load(options = {})", "async function deleteTurnSuffix("),
  between("async function compactConversation()", "async function send()"),
  between("watch(() => props.conversationUuid,", "watch(() => draft.value,"),
].join("\n");
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => {resolve = yes; reject = no;});
  return {promise, resolve, reject};
}
const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
const snapshot = (uuid, tag = "loaded", operations = []) => ({conversationUuid: uuid, tag, operations, messages: [], running: false});
const result = (uuid, did = true) => ({state: snapshot(uuid, "compact-response"), outcome: {did}});

function harness(t, {manualConfirm = false} = {}) {
  const scope = effectScope();
  t.after(() => scope.stop());
  const props = reactive({conversationUuid: "A"});
  const requests = [], confirmations = [], notices = [], stateCalls = [], applications = [], connections = [];
  const statePlans = [], watchJobs = [], scrolls = [];
  const noop = () => {};
  let context;
  const globals = {
    ref, computed, props, nextTick, isContextCompactionOperation, runGuardedConversationStateRefresh,
    watch: (get, callback, options) => watch(get, (...args) => {
      const work = callback(...args);
      watchJobs.push(work);
      return work;
    }, options),
    sendPending: ref(false), draft: ref("ready to send"), pendingAttachments: ref([]),
    chatState: ref(snapshot("A", "initial")), operationsById: ref(new Map()),
    orderedOpIds: ref([]), revisionByOpId: ref(new Map()), stateStatsByOpId: new Map(), lastFrameSeq: ref(0),
    contextUsage: ref({known: true, authoritative: true, percent: 80, manualMinPercent: 50}),
    autoScrollLocked: ref(false), runConfigSaves: {appliedVersion: 0}, modelOptions: ref([{}]), localModel: ref("test"),
    runConfigOverride: ref(null), localToServerTransitionUuid: ref(""), activeTurnIndex: ref(0),
    messages: ref([]), runStartedAt: ref(0), lastStats: ref(null),
    Api: {
      conversationCompact: (uuid) => {
        const request = {uuid, ...deferred()};
        requests.push(request);
        return request.promise;
      },
      conversationState: async (uuid) => {
        stateCalls.push(uuid);
        return statePlans.length ? statePlans.shift().promise : snapshot(uuid);
      },
    },
    ElMessageBox: {confirm: (...args) => {
      const confirmation = {...deferred(), args};
      confirmations.push(confirmation);
      if (!manualConfirm) confirmation.resolve();
      return confirmation.promise;
    }},
    ElMessage: {
      success: (message) => notices.push({kind: "success", message}),
      error: (message) => notices.push({kind: "error", message}),
    },
    apiError: String,
    captureScrollAnchor: () => ({anchor: "retained"}),
    restoreScrollAnchor: async (anchor, options) => {if (options.isCurrent()) scrolls.push(anchor);},
    scrollBottom: async () => {}, updateScrollerOverflow: noop, scheduleActiveTurnFromScroll: noop,
    loadOptions: async () => {}, applyDefaultLocalModel: noop, loadLocalRunDefaults: async () => {},
    resetLocalConversationState: (uuid) => {
      context.chatState.value = snapshot(uuid, "local");
      context.operationsById.value = new Map();
      vm.runInContext("running.value = false", context);
    },
    applyLoadedConversationState: (data, uuid) => {
      applications.push({data, uuid});
      context.chatState.value = data;
      context.operationsById.value = new Map((data.operations || []).map((op, i) => [String(i), op]));
      context.messages.value = data.messages || [];
      vm.runInContext(`running.value = ${Boolean(data.running)}`, context);
    },
    connectWs: async (uuid) => {connections.push(uuid);}, closeWs: noop,
    restoreDraftForConversation: noop, resetTransientThinking: noop, setDraftForConversation: noop,
    resetAgentAutoOpenBoundary: noop, hasOptimisticLocalTurn: () => false, leavePendingSend: noop,
    clearUiCaches: noop, resetTimelinePagination: noop, focusComposer: async () => {},
  };
  context = vm.createContext(globals);
  scope.run(() => vm.runInContext(`
    let componentMounted = true;
    let loadRequestGeneration = 0;
    let runConfigInteractionGeneration = 0;
    let pinnedActiveTurnIndex = null;
    let agentAutoOpenBoundaryConversation = "";
    ${actual}
  `, context));
  const run = (code) => vm.runInContext(code, context);
  return {
    context, run, requests, confirmations, notices, stateCalls, applications, connections, scrolls,
    start: () => run("compactConversation()"),
    navigate: async (uuid) => {
      const start = watchJobs.length;
      props.conversationUuid = uuid;
      await nextTick();
      await Promise.all(watchJobs.slice(start));
    },
    nextState: () => {const pending = deferred(); statePlans.push(pending); return pending;},
    serverCompaction: (lifecycle = "active") => {
      context.operationsById.value = new Map([["compact", {opType: "context_compaction", lifecycle}]]);
    },
  };
}

for (const target of ["B", "local:new"]) {
  test(`compacting A does not lock ${target}; late success cannot apply state, reload or notify there`, async (t) => {
    const h = harness(t);
    const pending = h.start();
    await flush();
    assert.equal(h.run("compacting.value"), true);
    assert.equal(h.run("canSend.value"), false);
    // Navigation must clear both the local lock's visibility and A's server indicator.
    h.serverCompaction();
    await h.navigate(target);
    assert.equal(h.run("serverCompacting.value"), false);
    assert.equal(h.run("compactPending.value"), false);
    assert.equal(h.run("canSend.value"), true);
    const state = h.context.chatState.value;
    const reads = h.stateCalls.length;
    h.requests[0].resolve(result("A"));
    await pending;
    assert.equal(h.context.chatState.value, state);
    assert.equal(h.stateCalls.length, reads);
    assert.equal(h.notices.length, 0);
    assert.deepEqual(h.requests.map((request) => request.uuid), ["A"]);
  });

  test(`late failure from A is silent and does not disturb ${target}`, async (t) => {
    const h = harness(t);
    const pending = h.start();
    await flush();
    await h.navigate(target);
    const state = h.context.chatState.value;
    h.requests[0].reject(new Error("A failed"));
    await pending;
    assert.equal(h.context.chatState.value, state);
    assert.equal(h.run("canSend.value"), true);
    assert.equal(h.notices.length, 0);
  });
}

test("A -> B -> A keeps A's in-flight lock, but rejects the old visit's response", async (t) => {
  const h = harness(t);
  const pending = h.start();
  await flush();
  await h.navigate("B");
  await h.navigate("A");
  assert.equal(h.run("compactPending.value"), true);
  assert.equal(h.run("canSend.value"), false);
  await h.start();
  assert.equal(h.requests.length, 1);
  const state = h.context.chatState.value;
  const reads = h.stateCalls.length;
  // Even after the HTTP request ends, a real active operation still blocks A.
  h.serverCompaction();
  h.requests[0].resolve(result("A"));
  await pending;
  assert.equal(h.context.chatState.value, state);
  assert.equal(h.stateCalls.length, reads);
  assert.equal(h.notices.length, 0);
  assert.equal(h.run("compactPending.value"), false);
  assert.equal(h.run("compacting.value"), true);
  h.serverCompaction("terminal");
  assert.equal(h.run("canSend.value"), true);
});

for (const failure of [false, true]) {
  test(`A's late ${failure ? "failure" : "success"} cannot release B's own compaction`, async (t) => {
    const h = harness(t);
    const a = h.start();
    await flush();
    await h.navigate("B");
    const b = h.start();
    await flush();
    assert.deepEqual(h.requests.map((request) => request.uuid), ["A", "B"]);
    if (failure) h.requests[0].reject(new Error("A failed"));
    else h.requests[0].resolve(result("A"));
    await a;
    assert.equal(h.run("compactPending.value"), true);
    assert.equal(h.run("canSend.value"), false);
    assert.equal(h.notices.length, 0);
    h.requests[1].resolve(result("B"));
    await b;
    assert.equal(h.run("compactPending.value"), false);
    assert.equal(h.run("canSend.value"), true);
    assert.equal(h.notices.length, 1);
    assert.equal(h.context.chatState.value.conversationUuid, "B");
  });
}

for (const target of ["B", "local:new", "A"]) {
  test(`confirmation from an old visit cannot start compaction after navigating to ${target}`, async (t) => {
    const h = harness(t, {manualConfirm: true});
    const pending = h.start();
    await h.navigate("B");
    if (target !== "B") await h.navigate(target);
    h.confirmations[0].resolve();
    await pending;
    assert.equal(h.requests.length, 0);
    assert.equal(h.run("compactPending.value"), false);
    assert.equal(h.notices.length, 0);
  });
}

test("confirmation cancellation, changed eligibility and duplicate confirmations remain safe", async (t) => {
  const h = harness(t, {manualConfirm: true});
  const cancelled = h.start();
  h.confirmations[0].reject("cancel");
  await cancelled;
  assert.equal(h.requests.length, 0);
  const noLongerEligible = h.start();
  h.run("running.value = true");
  h.confirmations[1].resolve();
  await noLongerEligible;
  assert.equal(h.requests.length, 0);
  h.run("running.value = false");
  const first = h.start(), duplicate = h.start();
  h.confirmations[2].resolve();
  h.confirmations[3].resolve();
  await flush();
  await duplicate;
  assert.equal(h.requests.length, 1);
  h.requests[0].resolve(result("A"));
  await first;
  assert.equal(h.run("compactPending.value"), false);
});

for (const did of [true, false]) {
  test(`same-visit completion preserves normal ${did ? "success" : "no-content"} feedback and scroll refresh`, async (t) => {
    const h = harness(t);
    const pending = h.start();
    await flush();
    h.requests[0].resolve(result("A", did));
    await pending;
    assert.deepEqual(h.stateCalls, ["A"]);
    assert.equal(h.context.chatState.value.tag, "loaded");
    assert.equal(h.run("canSend.value"), true);
    assert.deepEqual(h.scrolls, [{anchor: "retained"}]);
    assert.deepEqual(h.notices, [{kind: "success", message: did ? "上下文压缩完成" : "当前历史没有可压缩内容"}]);
  });
}

test("same-visit HTTP failure reports the error and releases only local pending", async (t) => {
  const h = harness(t);
  const pending = h.start();
  await flush();
  h.requests[0].reject(new Error("offline"));
  await pending;
  assert.equal(h.run("compactPending.value"), false);
  assert.equal(h.run("canSend.value"), true);
  assert.deepEqual(h.notices, [{kind: "error", message: "Error: offline"}]);
  assert.equal(h.stateCalls.length, 0);
});

for (const failure of [false, true]) {
  test(`navigation during post-compaction reload rejects late state ${failure ? "failure" : "success"} and feedback`, async (t) => {
    const h = harness(t);
    const pending = h.start();
    await flush();
    const reload = h.nextState();
    h.requests[0].resolve(result("A"));
    await flush();
    assert.deepEqual(h.stateCalls, ["A"]);
    await h.navigate("B");
    await h.navigate("A");
    const state = h.context.chatState.value;
    if (failure) reload.reject(new Error("old state failed"));
    else reload.resolve(snapshot("A", "stale reload"));
    await pending;
    assert.equal(h.context.chatState.value, state);
    assert.equal(h.notices.length, 0);
    assert.equal(h.run("compactPending.value"), false);
    assert.equal(h.run("loading.value"), false);
    assert.deepEqual(h.connections, ["B", "A"]);
  });
}

test("unmount blocks outstanding confirmation and late request side effects", async (t) => {
  const beforeConfirm = harness(t, {manualConfirm: true});
  const confirmation = beforeConfirm.start();
  // onBeforeUnmount already sets this flag in the production component.
  beforeConfirm.run("componentMounted = false");
  beforeConfirm.confirmations[0].resolve();
  await confirmation;
  assert.equal(beforeConfirm.requests.length, 0);

  const afterRequest = harness(t);
  const pending = afterRequest.start();
  await flush();
  const state = afterRequest.context.chatState.value;
  afterRequest.run("componentMounted = false");
  afterRequest.requests[0].resolve(result("A"));
  await pending;
  assert.equal(afterRequest.context.chatState.value, state);
  assert.equal(afterRequest.stateCalls.length, 0);
  assert.equal(afterRequest.notices.length, 0);
  assert.equal(afterRequest.run("compactPending.value"), false);
});

test("server compaction remains authoritative without a local HTTP request", async (t) => {
  const h = harness(t);
  for (const lifecycle of ["active", "paused", "waiting_control"]) {
    h.serverCompaction(lifecycle);
    assert.equal(h.run("compactPending.value"), false);
    assert.equal(h.run("canSend.value"), false);
    assert.equal(h.run("canCompact.value"), false);
    await h.start();
  }
  assert.equal(h.requests.length, 0);
  await h.navigate("B");
  assert.equal(h.run("canSend.value"), true);
  assert.equal(h.run("canCompact.value"), true);
});
