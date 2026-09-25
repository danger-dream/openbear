import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import {createOutboundSendTracker, probeSocket, restoreOutboundDraft, waitForSocketOpen} from "./outboundSend.js";
import {referenceDisplayText, referenceErrorText, referenceToken, referenceKey, referencesInText} from "../../references/codec.js";
import {createOperationFrameBuffer} from './operationFrameBuffer.js';
import {createAttachmentDraftStorage} from './attachmentDraftStorage.js';
import {initialConversationTitle} from '../../conversationTitle.js';
import {createMemoryAttachmentDraftDriver} from './attachmentDraftMemoryDriver.mjs';

// Run the actual ConsoleView submission/recovery functions, not a second model
// of the implementation. Vue rendering and HTTP are isolated side-effect seams.
const source = fs.readFileSync(process.env.CONSOLE_TEST_SOURCE || new URL("./ConsoleView.vue", import.meta.url), "utf8");
const composer = fs.readFileSync(new URL("./ConsoleComposer.vue", import.meta.url), "utf8");
function between(start, end, text = source) {
  const a = text.indexOf(start);
  const b = text.indexOf(end, a + start.length);
  assert.ok(a >= 0 && b > a, `${start}..${end}`);
  return text.slice(a, b);
}
const actual = [
  between("const canSend = computed(() => {", "const modelGroups = computed("),
  between("function activeAttachmentKey()", "function queueSentAttachmentPreviewRevokes("),
  between("function closeWs() {", "function normalizePendingSteering("),
  between("function finishPendingOutboundSend(", "function applyLoadedConversationState("),
  between("async function send() {", "async function stop() {"),
  between("async function stop() {", "async function newSession() {"),
].join("\n");
const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };

function harness({local = false, halfOpenFirst = false, createConversation} = {}) {
  let time = 1000;
  let timerSeq = 0;
  let requestSeq = 0;
  const timers = new Map();
  const attachmentRecords = new Map();
  const attachmentDriver = createMemoryAttachmentDraftDriver({records: attachmentRecords});
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
    WebSocket: Socket, AbortController,
    operationFrameBuffer: createOperationFrameBuffer(), conversationStateRequests: {pending:false,invalidate(){}},
    window: {setTimeout: timer.scheduleTimeout, clearTimeout: timer.clearScheduledTimeout, dispatchEvent: noop},
    document: {visibilityState: "visible"},
    URL: {createObjectURL: (file) => `blob:${file.name}`, revokeObjectURL: (url) => revoked.push(url)},
    CustomEvent: class {constructor(type) {this.type = type;}},
    globalThis: {crypto: {randomUUID: () => `request-${++requestSeq}`}},
    computed: (get) => ({get value() { return get(); }}),
    createOutboundSendTracker: (options) => createOutboundSendTracker({...options, ...timer}),
    probeSocket: (socket) => probeSocket(socket, timer),
    waitForSocketOpen: (socket) => waitForSocketOpen(socket, timer),
    restoreOutboundDraft, referenceDisplayText, referenceErrorText, referencesInText, initialConversationTitle, referenceCatalog: {ready:true,connected:true},
    props, composer: {value: null},
    attachmentRestoring: {value: false},
    compacting: {value: false}, modelMutating: {value: false}, sendPending: {value: false}, running: {value: false},
    draft: {value: "original message"}, pendingAttachments: {value: []}, attachmentPreviews: {value: {}},
    messages: {value: []}, lastStats: {value: null}, foregroundRunning: {value: false}, rootTurnRunning: {value: false},
    runStartedAt: {value: 0}, status: {value: "就绪"}, lastFrameSeq: {value: 10},
    autoScrollLocked: {value: true}, chatState: {value: {running: false}}, draftByConversation: {value: {}},
    activeConversationUuid: {get value() {return props.conversationUuid;}},
    isLocalConversation: {get value() {return props.conversationUuid.startsWith("local:");}},
    localToServerTransitionUuid: {value: ""}, terminalStateRefreshScheduler: {invalidate: noop}, cancelScheduledUiWork: noop,
    conversationWsUrl: (uuid) => `ws://test.invalid/${uuid}`,
    localAttachmentPayload: () => [], clearDraftForConversation: noop, adjustComposerHeight: noop, closeComposerMenus: noop,
    attachmentDrafts: createAttachmentDraftStorage({driver: attachmentDriver}),
    noteVisibleOutput: noop, clearActiveRun: noop, lockAutoScroll: noop, scrollBottom: async () => {},
    emit: (event, uuid) => {if (event === "conversation-created") props.conversationUuid = uuid;},
    nextTick: async () => {}, completeLocalRunConfig: () => ({}), ensureLocalRunDefaults: async () => {},
    Api: {
      createConversation: createConversation || (async () => ({conversation: {conversationUuid: "conv-created"}})),
      uploadConversationFiles: async (_uuid, files) => files.map((file) => ({uploadId: `uploaded-${file.name}`})),
    },
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
  vm.runInContext(`let attachmentsLoadedKey=props.conversationUuid; let attachmentRestoreGeneration=0;
    const attachmentsByConversation=new Map(); const attachmentHydrations=new Map();
    let ws=null; let wsConversationUuid=""; let reconnectTimer=null;
    let componentMounted=true; let sendAttemptGeneration=0; let connectionResumePromise=null;
    let timelinePageInitialized=!props.conversationUuid.startsWith('local:');
    let timelinePageConversationUuid=props.conversationUuid;
    const outboundSends=createOutboundSendTracker({onTimeout:(pending)=>recoverUnconfirmedSend(pending)});
    ${actual}`, context);
  return {
    context, sockets, warnings, refreshes, revoked, timers, attachmentDriver,
    stored: (key) => Array.from(attachmentRecords.get(key)?.items || [], (item) => item.fileName),
    run: (code) => vm.runInContext(code, context),
    sends: () => sockets.flatMap((socket) => socket.sent).filter((item) => item.type === "send"),
    advance: async (ms, {run = true} = {}) => {
      time += ms;
      if (run) for (const [id, item] of [...timers]) if (item.due <= time) {timers.delete(id); item.fn();}
      await flush();
    },
  };
}

test('an in-flight model source save blocks the next send until the saved source is authoritative',async()=>{
  const h=harness();
  h.context.modelMutating.value=true;
  assert.equal(h.run('canSend.value'),false);
  await h.run('send()');
  assert.equal(h.sends().length,0);
  assert.equal(h.context.draft.value,'original message');
  h.context.modelMutating.value=false;
  assert.equal(h.run('canSend.value'),true);
  await h.run('send()');
  assert.equal(h.sends().length,1);
});

test('one accepted send uses only one parent directory-refresh path, never component plus global notification',async()=>{
  const h=harness(),componentEvents=[],globalEvents=[];
  h.context.emit=(name)=>componentEvents.push(name);
  h.context.window.dispatchEvent=(event)=>globalEvents.push(event.type);
  await h.run('send()');
  assert.equal(h.sends().length,1);
  assert.equal(componentEvents.filter(x=>x==='conversations-refresh').length,1);
  assert.equal(globalEvents.filter(x=>x==='openbear:conversations-refresh').length,0);
  h.run('leavePendingSend()');
});

test('send snapshots selection order before clearing the editor; newly inserted front item stays last in priority',async()=>{
  const h=harness();
  const old={kind:'chat',id:'old',label:'先选'},added={kind:'chat',id:'added',label:'后选'};
  const keys=referencesInText(referenceToken(old)+' '+referenceToken(added)).map(referenceKey);
  const child={getReferenceOrder:()=>[...keys]},context=vm.createContext({composerTextarea:{value:child}});
  vm.runInContext(between('function getReferenceOrder()', 'defineExpose(', composer),context);
  h.context.composer.value={getReferenceOrder:()=>vm.runInContext('getReferenceOrder()',context)};
  h.context.draft.value=referenceToken(added)+' 原问题 '+referenceToken(old);
  await h.run('send()');keys.splice(0);
  assert.deepEqual(h.sends()[0].referenceOrder,[referenceKey(old),referenceKey(added)]);
  assert.equal(referencesInText(h.sends()[0].text)[0].id,'added');
});

test("references wait for backend capability without clearing a draft or sending raw locators", async () => {
  const h=harness();h.context.referenceCatalog.ready=false;
  const text=referenceToken({kind:'doc',id:'17',label:'等待后端'});h.context.draft.value=text;
  await h.run('send()');assert.equal(h.sends().length,0);assert.equal(h.context.draft.value,text);
});

test("reference nodes survive lost acknowledgement alongside a newly typed draft", async () => {
  const h = harness();
  const first = referenceToken({kind:'doc',id:'17',label:'原文档'});
  const next = referenceToken({kind:'chat',id:'target-chat',label:'新引用',scope:'recent',turns:3});
  h.context.draft.value = '请看 '+first;
  await h.run('send()');
  h.context.draft.value = '再看 '+next;
  await h.advance(15000);
  assert.deepEqual(referencesInText(h.context.draft.value).map(ref=>ref.id), ['17','target-chat']);
  assert.equal(h.sends().length,1);
});

test("a new conversation title uses capsule labels rather than reference URLs", async () => {
  let title;
  const h = harness({local:true,createConversation:async data=>{title=data.title;return {conversation:{conversationUuid:'created-with-ref'}};}});
  h.context.draft.value = '请看 '+referenceToken({kind:'doc',id:'17',label:'部署文档'});
  await h.run('send()');
  assert.equal(title,'请看 部署文档');
  assert.equal(referencesInText(h.sends()[0].text)[0].id,'17');
});

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
  vm.runInContext(between("function handleKeydown(", "function onComposerKeydownCapture(", composer), enterContext);
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

test("submitting clears the stored files of that draft, and a released send stores them again", async () => {
  const h = harness();
  const only = {id: "only", file: {name: "only.txt", type: "text/plain", size: 4}};
  h.context.pendingAttachments.value = [only];
  await h.context.attachmentDrafts.save("conv-a", [only]);
  await h.run("send()");
  // A reload during the send must not offer a file whose message may be accepted.
  assert.deepEqual(h.stored("conv-a"), []);
  await h.advance(15000);
  assert.deepEqual(Array.from(h.context.pendingAttachments.value, (item) => item.id), ["only"]);
  assert.deepEqual(h.stored("conv-a"), ["only.txt"]);
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

test("slow HTTP upload outlives preparation/ACK timers and only sends opaque references", async () => {
  const h = harness();
  let finishUpload, progress;
  h.context.Api.uploadConversationFiles = (uuid, files, options) => {
    assert.equal(uuid, "conv-a");
    assert.equal(files[0].size, 70 * 1024 * 1024);
    progress = options.onProgress;
    return new Promise(resolve => { finishUpload = resolve; });
  };
  h.context.pendingAttachments.value = [{id: "large", file: {name: "large.zip", size: 70 * 1024 * 1024}}];
  const sending = h.run("send()");
  await flush();
  progress({loaded: 35, total: 70, fileIndex: 0, fileCount: 1});
  assert.match(h.context.status.value, /50%/);
  await h.advance(600000);
  assert.equal(h.context.sendPending.value, true);
  assert.equal(h.run("outboundSends.current.phase"), "uploading");
  assert.equal(h.sends().length, 0);
  assert.equal(h.warnings.length, 0);
  finishUpload([{uploadId: "opaque-upload-id"}]);
  await sending;
  assert.deepEqual(h.sends()[0].files, [{uploadId: "opaque-upload-id"}]);
  assert.equal(h.run("outboundSends.current.phase"), "sent");
  await h.advance(15000);
  assert.equal(h.context.sendPending.value, false);
  assert.equal(h.context.pendingAttachments.value.length, 1);
});

test("upload rejection restores draft and all attachments without sending a message", async () => {
  const h = harness();
  const file = {id: "first", file: {name: "failed.zip"}};
  h.context.pendingAttachments.value = [file];
  h.context.Api.uploadConversationFiles = async () => {throw new Error("disk_full");};
  await h.run("send()");
  assert.equal(h.context.sendPending.value, false);
  assert.equal(h.context.draft.value, "original message");
  assert.equal(h.context.pendingAttachments.value[0], file);
  assert.equal(h.sends().length, 0);
  assert.match(h.warnings[0], /disk_full/);
});

test("leaving during upload aborts HTTP and ignores late completion/progress", async () => {
  const h = harness();
  let finishUpload, options;
  h.context.pendingAttachments.value = [{id: "first", file: {name: "file.zip"}}];
  h.context.Api.uploadConversationFiles = (_uuid, _files, opts) => {
    options = opts;
    return new Promise(resolve => {finishUpload = resolve;});
  };
  const sending = h.run("send()");
  await flush();
  h.context.props.conversationUuid = "other";
  h.context.draft.value = "other draft";
  h.run("leavePendingSend()");
  assert.equal(options.signal.aborted, true);
  h.context.status.value = "other status";
  options.onProgress({loaded: 1, total: 1, fileIndex: 0, fileCount: 1});
  finishUpload([{uploadId: "late-upload"}]);
  await sending;
  assert.equal(h.context.status.value, "other status");
  assert.equal(h.context.draft.value, "other draft");
  assert.equal(h.sends().length, 0);
  assert.equal(h.timers.size, 0);
});

test("stop during HTTP upload cancels locally and restores draft/attachments without sending stop", async () => {
  const h = harness();
  let resolveUpload, signal;
  h.context.pendingAttachments.value = [{id: "first", file: {name: "file.zip"}}];
  h.context.Api.uploadConversationFiles = (_uuid, _files, options) => {
    signal = options.signal;
    return new Promise(resolve => {resolveUpload = resolve;});
  };
  const sending = h.run("send()");
  await flush();
  await h.run("stop()");
  assert.equal(signal.aborted, true);
  assert.equal(h.context.draft.value, "original message");
  assert.equal(h.context.pendingAttachments.value.length, 1);
  assert.equal(h.context.sendPending.value, false);
  assert.match(h.context.status.value, /上传已取消/);
  resolveUpload([{uploadId: "late"}]);
  await sending;
  assert.equal(h.sends().length, 0);
  assert.equal(h.timers.size, 0);
});

test("unavailable folder defaults restore the draft without creating or sending a conversation", async () => {
  const h = harness({local: true});
  let created = false;
  h.context.Api.createConversation = async () => {created = true;};
  h.context.ensureLocalRunDefaults = async () => {throw new Error("无法读取目录默认配置，请检查连接后重试");};
  await h.run("send()");
  assert.equal(created, false);
  assert.equal(h.sends().length, 0);
  assert.equal(h.context.draft.value, "original message");
  assert.equal(h.context.sendPending.value, false);
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

function deferred() {let resolve; const promise = new Promise(r => {resolve = r;}); return {promise, resolve};}
const draftFile = id => ({id, file: new File([id], `${id}.txt`, {type: 'text/plain'})});
const reloadNames = async (h, key = 'conv-a') => (await h.context.attachmentDrafts.load(key)).items.map(item => item.file.name);

test('C05: preparing/uploading files survive reload, but sent-unacknowledged files do not', async () => {
  for (const local of [false, true]) {
    const creation = deferred();
    const h = harness({local, createConversation: () => creation.promise});
    const only = draftFile('file'); const uploading = deferred();
    h.context.pendingAttachments.value = [only];
    await h.context.attachmentDrafts.save(local ? 'local:new' : 'conv-a', [only]);
    h.context.Api.uploadConversationFiles = () => uploading.promise;
    const sending = h.run('send()'); await flush();
    if (local) {
      assert.deepEqual(await reloadNames(h, 'local:new'), ['file.txt'], 'creation is definitely unsent');
      creation.resolve({conversation: {conversationUuid: 'conv-created'}}); await flush();
    }
    const key = local ? 'conv-created' : 'conv-a';
    assert.equal(h.run('outboundSends.current.phase'), 'uploading');
    assert.equal(h.sends().length, 0);
    assert.deepEqual(await reloadNames(h, key), ['file.txt']);
    assert.equal(h.context.draftByConversation.value[key], 'original message', 'preparing text is reloadable with its files');
    uploading.resolve([{uploadId: 'uploaded'}]); await sending;
    assert.equal(h.run('outboundSends.current.phase'), 'sent');
    assert.equal(h.sends().length, 1);
    assert.deepEqual(await reloadNames(h, key), [], 'a missing ACK must not make sent files reloadable');
    assert.equal(h.context.draftByConversation.value[key] || '', '', 'submitted text does not reappear awaiting ACK');
    // A draft edit while awaiting ACK must not re-save the submitted files.
    h.context.pendingAttachments.value.push(draftFile('next'));
    h.run('persistActiveAttachments()');
    assert.deepEqual(await reloadNames(h, key), ['next.txt']);
    h.run(`finishPendingOutboundSend(${JSON.stringify(h.sends()[0].requestId)})`);
    assert.deepEqual(await reloadNames(h, key), ['next.txt']);
  }
});

test('C05: cancelled preparation while the final storage fence is pending cannot send or lose restored files', async () => {
  const h = harness(); const only = draftFile('only');
  h.context.pendingAttachments.value = [only];
  await h.context.attachmentDrafts.save('conv-a', [only]);
  const entered = deferred(), gate = deferred(), get = h.attachmentDriver.get;
  h.attachmentDriver.get = async key => {h.attachmentDriver.get = get; const record = await get(key); entered.resolve(); await gate.promise; return record;};
  const sending = h.run('send()'); await entered.promise;
  assert.equal(h.sends().length, 0, 'persistence fence must finish before actual WS send');
  h.run('leavePendingSend()');
  gate.resolve(); await sending;
  assert.equal(h.sends().length, 0);
  assert.deepEqual(await reloadNames(h), ['only.txt']);
  assert.equal(h.context.sendPending.value, false);
});

test('C05: a failed persistence fence prevents send and leaves a recoverable draft', async () => {
  const h = harness(); const only = draftFile('only');
  h.context.pendingAttachments.value = [only];
  await h.context.attachmentDrafts.save('conv-a', [only]);
  h.attachmentDriver.delete = async () => {throw new Error('denied');};
  await h.run('send()');
  assert.equal(h.sends().length, 0);
  assert.equal(h.context.sendPending.value, false);
  assert.deepEqual(await reloadNames(h), ['only.txt']);
  assert.match(h.context.draft.value, /original message/);
});

test('storage disabled from the start still sends newly selected memory-only attachments', async () => {
  for (const local of [false, true]) {
    const h = harness({local}); const only = draftFile('memory-only');
    const key = h.context.props.conversationUuid;
    for (const method of ['get', 'list', 'put', 'delete']) {
      h.attachmentDriver[method] = async () => {throw new Error('storage_disabled');};
    }
    h.context.pendingAttachments.value = [only];
    await h.context.attachmentDrafts.save(key, [only]);
    await h.run('send()');
    assert.equal(h.sends().length, 1, local ? 'new conversation' : 'existing conversation');
    assert.equal(h.sends()[0].files[0].uploadId, 'uploaded-memory-only.txt');
    assert.deepEqual(h.stored(h.context.props.conversationUuid), [], 'does not claim reload persistence');
    h.run(`finishPendingOutboundSend(${JSON.stringify(h.sends()[0].requestId)})`);
  }
});

test('C03: both send entry points wait for hydration instead of sending a partial attachment list', async () => {
  const h = harness(); h.context.attachmentRestoring.value = true;
  assert.equal(h.run('canSend.value'), false);
  await h.run('send()'); assert.equal(h.sends().length, 0);
  assert.equal(h.context.draft.value, 'original message');
});
