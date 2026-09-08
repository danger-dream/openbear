import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import {createOutboundSendTracker, probeSocket, restoreOutboundDraft, waitForSocketOpen} from "./outboundSend.js";

// Run the actual ConsoleView submission/recovery functions, not a second model
// of the implementation. Vue rendering and HTTP are isolated side-effect seams.
const source = fs.readFileSync(new URL("./ConsoleView.vue", import.meta.url), "utf8");
const composer = fs.readFileSync(new URL("./ConsoleComposer.vue", import.meta.url), "utf8");
function between(start, end, text = source) {
  const a = text.indexOf(start);
  const b = text.indexOf(end, a + start.length);
  assert.ok(a >= 0 && b > a, `${start}..${end}`);
  return text.slice(a, b);
}
const actual = [
  between("const canSend = computed(() => {", "const modelGroups = computed("),
  between("function closeWs() {", "function normalizePendingSteering("),
  between("function finishPendingOutboundSend(", "function applyLoadedConversationState("),
  between("async function send() {", "async function stop() {"),
].join("\n");
const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };

function harness({local = false, halfOpenFirst = false, createConversation} = {}) {
  let time = 1000;
  let timerSeq = 0;
  let requestSeq = 0;
  const timers = new Map();
  const sockets = [];
  const warnings = [];
  const refreshes = [];
  const revoked = [];
  const timer = {
    now: () => time,
    scheduleTimeout: (fn, ms) => { timers.set(++timerSeq, {fn, due: time + ms}); return timerSeq; },
    clearScheduledTimeout: (id) => timers.delete(id),
  };
  class Socket {
    static OPEN = 1; static CONNECTING = 0; static CLOSED = 3;
    constructor(url) { this.url = url; this.readyState = 1; this.listeners = new Map(); this.sent = []; this.halfOpen = halfOpenFirst && !sockets.length; sockets.push(this); }
    addEventListener(type, fn) { if (!this.listeners.has(type)) this.listeners.set(type, new Set()); this.listeners.get(type).add(fn); }
    removeEventListener(type, fn) { this.listeners.get(type)?.delete(fn); }
    emit(type, data) { this[`on${type}`]?.({type, data}); for (const fn of [...(this.listeners.get(type) || [])]) fn({type, data}); }
    send(body) {
      if (this.readyState !== 1) throw new Error("socket_not_open");
      const data = JSON.parse(body); this.sent.push(data);
      if (data.type === "ping" && !this.halfOpen) queueMicrotask(() => this.emit("message", JSON.stringify({type: "pong"})));
    }
    close() { this.readyState = 3; this.emit("close"); }
  }
  const noop = () => {};
  const props = {conversationUuid: local ? "local:new" : "conv-a"};
  let context;
  const globals = {
    WebSocket: Socket,
    window: {setTimeout: timer.scheduleTimeout, clearTimeout: timer.clearScheduledTimeout, dispatchEvent: noop},
    document: {visibilityState: "visible"},
    URL: {createObjectURL: (file) => `blob:${file.name}`, revokeObjectURL: (url) => revoked.push(url)},
    CustomEvent: class {constructor(type) {this.type = type;}},
    globalThis: {crypto: {randomUUID: () => `request-${++requestSeq}`}},
    computed: (get) => ({get value() { return get(); }}),
    createOutboundSendTracker: (options) => createOutboundSendTracker({...options, ...timer}),
    probeSocket: (socket) => probeSocket(socket, timer),
    waitForSocketOpen: (socket) => waitForSocketOpen(socket, timer),
    restoreOutboundDraft,
    props,
    compacting: {value: false}, sendPending: {value: false}, running: {value: false},
    draft: {value: "original message"}, pendingAttachments: {value: []}, attachmentPreviews: {value: {}},
    messages: {value: []}, lastStats: {value: null}, foregroundRunning: {value: false}, rootTurnRunning: {value: false},
    runStartedAt: {value: 0}, status: {value: "就绪"}, lastFrameSeq: {value: 10},
    autoScrollLocked: {value: true}, chatState: {value: {running: false}}, draftByConversation: {value: {}},
    activeConversationUuid: {get value() {return props.conversationUuid;}},
    isLocalConversation: {get value() {return props.conversationUuid.startsWith("local:");}},
    localToServerTransitionUuid: {value: ""}, terminalStateRefreshScheduler: {invalidate: noop}, cancelScheduledUiWork: noop,
    conversationWsUrl: (uuid) => `ws://test.invalid/${uuid}`,
    localAttachmentPayload: () => [], clearDraftForConversation: noop, adjustComposerHeight: noop, closeComposerMenus: noop,
    noteVisibleOutput: noop, clearActiveRun: noop, lockAutoScroll: noop, scrollBottom: async () => {},
    filesToWsPayload: async (files) => files.map((file) => ({name: file.name})),
    emit: (event, uuid) => {if (event === "conversation-created") props.conversationUuid = uuid;},
    nextTick: async () => {}, completeLocalRunConfig: () => ({}),
    Api: {createConversation: createConversation || (async () => ({conversation: {conversationUuid: "conv-created"}}))},
    load: async (options) => {refreshes.push(options);}, apiError: String,
    queueSentAttachmentPreviewRevokes: (urls) => revoked.push(...urls),
    draftKey: (uuid) => uuid,
    setDraftForConversation: (uuid, text) => {context.draftByConversation.value[uuid] = text;},
    orderedOperationsList: () => [],
    syncRunStateFromOperations: () => {context.running.value = Boolean(context.serverRunning); context.foregroundRunning.value = Boolean(context.serverRunning);},
    ElMessage: {warning: (message) => warnings.push(message), error: (message) => warnings.push(message)},
    updatePendingConfirmations: noop, updatePendingSteering: noop, debugFrames: noop,
    visibleEventSignatureForMessages: () => "", loadOperationsFromState: () => [], shouldPreserveOptimisticMessages: () => false,
    normalizeLedgerUsageBaseline: (value) => value,
  };
  context = vm.createContext(globals);
  vm.runInContext(`let ws=null; let wsConversationUuid=""; let reconnectTimer=null;
    let componentMounted=true; let sendAttemptGeneration=0; let connectionResumePromise=null;
    const outboundSends=createOutboundSendTracker({onTimeout:(pending)=>recoverUnconfirmedSend(pending)});
    ${actual}`, context);
  return {
    context, sockets, warnings, refreshes, revoked, timers,
    run: (code) => vm.runInContext(code, context),
    sends: () => sockets.flatMap((socket) => socket.sent).filter((item) => item.type === "send"),
    advance: async (ms, {run = true} = {}) => {
      time += ms;
      if (run) for (const [id, item] of [...timers]) if (item.due <= time) {timers.delete(id); item.fn();}
      await flush();
    },
  };
}

test("actual lost-ACK path unlocks click-send and preserves old/new drafts without resending", async () => {
  const h = harness();
  await h.run("send()");
  assert.equal(h.sends().length, 1);
  h.context.draft.value = "next draft";
  assert.equal(h.run("canSend.value"), false);
  await h.advance(15000);
  assert.equal(h.run("sendPending.value"), false);
  assert.equal(h.run("canSend.value"), true);
  assert.equal(h.context.draft.value, "original message\n\nnext draft");
  assert.equal(h.sends().length, 1);
  assert.match(h.warnings[0].message, /发送结果未确认/);
  assert.equal(h.refreshes.length, 1);
});

test("actual socket close/error recovers immediately; old ACK cannot clear a new request or draft", async () => {
  for (const event of ["close", "error"]) {
    const h = harness();
    await h.run("send()");
    const first = h.sends()[0];
    const oldSocket = h.sockets[0];
    oldSocket.emit(event);
    await flush();
    assert.equal(h.run("sendPending.value"), false);
    assert.equal(h.context.draft.value, "original message");
    assert.equal(h.sends().length, 1);
    h.context.status.value = "new connection status";
    oldSocket.emit("open");
    assert.equal(h.context.status.value, "new connection status");
    await h.run("send()");
    assert.equal(h.sends().length, 2);
    h.context.draft.value = "newer draft";
    h.run(`handleWsMessage(JSON.stringify({type:"ack",requestId:${JSON.stringify(first.requestId)}}));`);
    assert.equal(h.run("sendPending.value"), true);
    assert.equal(h.context.draft.value, "newer draft");
    h.run("leavePendingSend()");
  }
});

test("freshness probe replaces a half-open connection before sending message content exactly once", async () => {
  const h = harness({halfOpenFirst: true});
  const sending = h.run("send()");
  await flush();
  assert.equal(h.sockets.length, 1);
  assert.equal(h.sends().length, 0);
  await h.advance(4000);
  await sending;
  assert.equal(h.sockets.length, 2);
  assert.equal(h.sockets[0].sent.some((item) => item.type === "send"), false);
  assert.equal(h.sends().length, 1);
  h.run("leavePendingSend()");
});

test("actual duplicate Enter/click is blocked while a request awaits ACK", async () => {
  const h = harness();
  await h.run("send()");
  h.context.draft.value = "second";
  await h.run("send()");
  assert.equal(h.sends().length, 1);
  assert.equal(h.context.draft.value, "second");
  const events = [];
  const enterContext = vm.createContext({props: {canSend: false}, emit: (event) => events.push(event)});
  vm.runInContext(between("function handleKeydown(", "onMounted(", composer), enterContext);
  vm.runInContext("handleKeydown({key:'Enter',preventDefault(){}})", enterContext);
  assert.deepEqual(events, []);
  enterContext.props.canSend = true;
  vm.runInContext("handleKeydown({key:'Enter',preventDefault(){}})", enterContext);
  assert.deepEqual(events, ["send"]);
  h.run("leavePendingSend()");
});

test("ACK removes only submitted attachments; recovery retains original plus newly added files", async () => {
  for (const accepted of [true, false]) {
    const h = harness();
    const first = {id: "first", file: {name: "first.png", type: "image/png"}};
    const next = {id: "next", file: {name: "next.txt", type: "text/plain"}};
    h.context.pendingAttachments.value = [first];
    h.context.attachmentPreviews.value = {first: "blob:first"};
    await h.run("send()");
    h.context.pendingAttachments.value.push(next);
    if (accepted) {
      h.run(`handleWsMessage(JSON.stringify({type:"ack",requestId:${JSON.stringify(h.sends()[0].requestId)}}));`);
      assert.deepEqual(Array.from(h.context.pendingAttachments.value, (item) => item.id), ["next"]);
    } else {
      await h.advance(15000);
      assert.deepEqual(Array.from(h.context.pendingAttachments.value, (item) => item.id), ["first", "next"]);
      assert.equal(h.context.attachmentPreviews.value.first, "blob:first");
    }
  }
});

test("failed ACK does not falsely stop a real run that already produced frames", async () => {
  const h = harness();
  await h.run("send()");
  h.context.serverRunning = true;
  await h.advance(15000);
  assert.equal(h.context.running.value, true);
  assert.equal(h.context.sendPending.value, false);
  assert.equal(h.sends().length, 1);
});

test("resume expires suspended ACK timer and recovers input without a user refresh", async () => {
  const h = harness();
  await h.run("send()");
  await h.advance(600000, {run: false});
  h.run("checkConnectionOnResume()");
  await flush();
  assert.equal(h.context.sendPending.value, false);
  assert.equal(h.sends().length, 1);
  assert.equal(h.context.draft.value, "original message");
});

test("conversation change cancels delayed preparation and restores only the source conversation draft", async () => {
  let resolveCreate;
  const h = harness({local: true, createConversation: () => new Promise((resolve) => {resolveCreate = resolve;})});
  const sending = h.run("send()");
  await flush();
  h.context.props.conversationUuid = "other";
  h.context.draft.value = "other draft";
  h.run("leavePendingSend()");
  resolveCreate({conversation: {conversationUuid: "late-created"}});
  await sending;
  assert.equal(h.context.props.conversationUuid, "other");
  assert.equal(h.context.draft.value, "other draft");
  assert.equal(h.context.draftByConversation.value["local:new"], "original message");
  assert.equal(h.sends().length, 0);
  assert.equal(h.timers.size, 0);
});

test("resume before a conversation is selected does not open a socket or fetch state", async () => {
  const h = harness();
  h.context.props.conversationUuid = "";
  h.run("checkConnectionOnResume()");
  await flush();
  assert.equal(h.sockets.length, 0);
  assert.equal(h.refreshes.length, 0);
});

test("first local conversation binds its pending receipt to the created server conversation", async () => {
  const h = harness({local: true});
  await h.run("send()");
  assert.equal(h.context.props.conversationUuid, "conv-created");
  assert.equal(h.run("outboundSends.current.conversationUuid"), "conv-created");
  await h.advance(15000);
  assert.equal(h.context.draftByConversation.value["conv-created"], "original message");
  assert.equal(h.context.sendPending.value, false);
});
