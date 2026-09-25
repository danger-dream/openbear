import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import {initialConversationTitle} from "../../conversationTitle.js";

const source = fs.readFileSync(new URL("./ConsoleView.vue", import.meta.url), "utf8");
const display = fs.readFileSync(new URL("./display.js", import.meta.url), "utf8");
function between(start, end, text = source) {
  const from = text.indexOf(start), to = text.indexOf(end, from + start.length);
  assert.ok(from >= 0 && to > from, `${start}..${end}`);
  return text.slice(from, to);
}
const actual = [
  between("let defaultsRequestSeq =", "let optionsLoadPromise"),
  between("function primaryModelInfo()", "function statsUsageSnapshot("),
  between("async function ensureServerConversationForSend(", "function applyLoadedConversationState("),
  between("watch(() => props.folderId,", "watch(() => draft.value,"),
  between("export function modelThinkingLevels(", "export function thinkingLabel(", display).replaceAll("export function", "function"),
].join("\n");
const cheap = {contextStrategy: "sliding_window", mainModel: "cheap", mainThinkingLevel: "low", mainFastMode: false, agentModel: "cheap", agentThinkLevel: "low", agentFastMode: false};
const paid = {contextStrategy: "sliding_window", mainModel: "paid", mainThinkingLevel: "high", mainFastMode: true, agentModel: "paid", agentThinkLevel: "high", agentFastMode: true};
const flush = async () => {for (let i = 0; i < 20; i++) await Promise.resolve();};
function deferred() {let resolve, reject; const promise = new Promise((yes, no) => {resolve = yes; reject = no;}); return {promise, resolve, reject};}

function harness({folderId = "project", getDefaults} = {}) {
  const props = {conversationUuid: "local:new", folderId};
  const requests = [], patches = [], creates = [], resets = [], migrations = [];
  const ref = value => ({value});
  const state = {
    props, DEFAULT_NEW_CONVERSATION_THINKING: "",
    primaryModelKey: ref("cheap"), currentPrimaryModelKey: ref("cheap"),
    modelOptions: ref([
      {key: "cheap", thinkingLevels: ["low", "medium"], defaultThinkingLevel: "low", supportsFast: false},
      {key: "paid", thinkingLevels: ["low", "medium", "high"], defaultThinkingLevel: "high", supportsFast: true},
    ]),
    localContextStrategy: ref("sliding_window"),
    localModel: ref("cheap"), localThinking: ref("low"), localFast: ref(false),
    localAgentModel: ref(""), localAgentThinking: ref(""), localAgentFast: ref(null),
    isLocalConversation: {get value() {return props.conversationUuid.startsWith("local:");}},
    activeConversationUuid: {get value() {return props.conversationUuid;}},
    chatState: ref({}), localToServerTransitionUuid: ref(""),
    migrateAttachmentDraft: (from, to) => migrations.push([from, to]),
    loadOptions: async () => {}, nextTick: async () => {},
    resetLocalConversationState: uuid => resets.push(uuid),
    referenceDisplayText: text => text, initialConversationTitle,
    outboundSends: {current: null, isCurrent: pending => !pending.cancelled},
    leavePendingSend() {if (state.outboundSends.current) state.outboundSends.current.cancelled = true; state.outboundSends.current = null;},
    watch(_get, callback) {state.folderChanged = callback;},
    emit(name, value) {if (name === "conversation-created") props.conversationUuid = value;},
    Api: {
      async conversationDefaults(params = {}) {
        requests.push(params);
        return getDefaults ? getDefaults(params) : {defaults: params.folderId ? paid : cheap, folderDefaults: params.folderId ? paid : {}};
      },
      async updateConversationDefaults(patch) {patches.push(patch); return {defaults: {...cheap, ...patch, revision: patches.length}};},
      async createConversation(data) {creates.push(data); return {conversation: {conversationUuid: "created"}};},
    },
  };
  const context = vm.createContext(state);
  vm.runInContext(actual, context);
  const run = code => vm.runInContext(code, context);
  return {state, requests, patches, creates, resets, migrations, run, config: () => JSON.parse(run("JSON.stringify(completeLocalRunConfig())"))};
}

test("folder defaults populate all six controls and are the exact create payload", async () => {
  const h = harness();
  await h.run("loadLocalRunDefaults()");
  assert.deepEqual(h.config(), paid);
  await h.run("ensureServerConversationForSend('hello', {})");
  assert.equal(h.requests.length, 1);
  assert.equal(h.requests[0].folderId, "project");
  assert.deepEqual(JSON.parse(JSON.stringify(h.creates[0].runConfig)), paid);
  assert.equal(h.creates[0].folderId, "project");
  // The composer still holds the files of this send, so their draft follows the new id.
  assert.deepEqual(h.migrations, [["local:new", "created"]]);
});

test("manual draft overrides retain other folder values without updating global preferences", async () => {
  const h = harness();
  await h.run("loadLocalRunDefaults()");
  await h.run("patchLocalRunDefaults({mainThinkingLevel:'medium'})");
  await h.run("patchLocalRunDefaults({agentFastMode:false})");
  assert.deepEqual(h.config(), {...paid, mainThinkingLevel: "medium", agentFastMode: false});
  assert.deepEqual(h.patches, []);
  await h.run("patchLocalRunDefaults({mainModel:'cheap'})");
  assert.deepEqual(h.config(), {...paid, mainModel: "cheap", mainThinkingLevel: "medium", mainFastMode: false, agentFastMode: false});
});

test("temporary drafts and folders without overrides preserve the existing remembered-preference path", async () => {
  for (const folderId of ["", "unconfigured"]) {
    const h = harness({folderId, getDefaults: () => ({defaults: cheap, folderDefaults: {}})});
    await h.run("loadLocalRunDefaults()");
    await h.run("patchLocalRunDefaults({mainThinkingLevel:'medium'})");
    assert.equal(h.patches.length, 1);
    assert.deepEqual(h.config(), {...cheap, mainThinkingLevel: "medium"});
  }
});

test("changing folders while still local:new refreshes all six fields", async () => {
  const h = harness();
  await h.run("loadLocalRunDefaults()");
  h.state.props.folderId = "";
  h.state.folderChanged("", "project");
  await flush();
  assert.deepEqual(h.config(), cheap);
  assert.equal(h.requests.length, 2);
  assert.equal(h.patches.length, 0);
});

test("late defaults from another folder cannot overwrite the new target", async () => {
  const old = deferred();
  const h = harness({getDefaults: ({folderId}) => folderId === "project" ? old.promise : {defaults: cheap, folderDefaults: {}}});
  const loadingOld = h.run("loadLocalRunDefaults()");
  h.state.props.folderId = "other";
  await h.run("loadLocalRunDefaults()");
  old.resolve({defaults: paid, folderDefaults: paid});
  assert.equal(await loadingOld, false);
  assert.deepEqual(h.config(), cheap);
});

test("late refresh cannot overwrite a newer explicit choice", async () => {
  const old = deferred();
  let calls = 0;
  const h = harness({getDefaults: () => ++calls === 1 ? old.promise : {defaults: paid, folderDefaults: paid}});
  const oldLoad = h.run("loadLocalRunDefaults()");
  await h.run("loadLocalRunDefaults()");
  await h.run("patchLocalRunDefaults({mainThinkingLevel:'low',agentFastMode:false})");
  old.resolve({defaults: paid, folderDefaults: paid});
  await oldLoad;
  assert.equal(h.config().mainThinkingLevel, "low");
  assert.equal(h.config().agentFastMode, false);
});

test("folder property refresh preserves explicit draft choices and updates untouched inherited fields", async () => {
  let current = paid;
  const h = harness({getDefaults: () => ({defaults: current, folderDefaults: current})});
  await h.run("loadLocalRunDefaults()");
  await h.run("patchLocalRunDefaults({agentFastMode:false})");
  current = {...paid, mainThinkingLevel: "medium"};
  await h.run("refreshFolderRunDefaults({preserveManual:true})");
  assert.deepEqual(h.config(), {...current, agentFastMode: false});
});

test("create waits for folder defaults and refuses to send using stale values on failure", async () => {
  const response = deferred();
  const h = harness({getDefaults: () => response.promise});
  const loading = h.run("loadLocalRunDefaults()");
  const creating = h.run("ensureServerConversationForSend('hello', {})");
  await flush();
  assert.equal(h.creates.length, 0);
  assert.equal(h.requests.length, 1);
  response.resolve({defaults: paid, folderDefaults: paid});
  await loading;
  await creating;
  assert.equal(h.creates[0].runConfig.mainModel, "paid");
  const bad = harness({getDefaults: () => {throw new Error("network unavailable");}});
  await assert.rejects(bad.run("ensureServerConversationForSend('hello', {})"), /无法读取会话默认配置/);
  assert.equal(bad.creates.length, 0);
});

test("temporary overrides populate creation, remain draft-only, and refresh after their own property save", async () => {
  let current = cheap;
  const h = harness({folderId: "", getDefaults: () => ({defaults: current, folderDefaults: current})});
  await h.run("loadLocalRunDefaults()");
  await h.run("patchLocalRunDefaults({mainThinkingLevel:'medium'})");
  assert.equal(h.patches.length, 0);
  current = {...paid, agentFastMode: false};
  h.run("handleFolderPropertiesChanged({detail:{folderId:'__temporary'}})");
  await flush();
  assert.deepEqual(h.config(), {...current, mainThinkingLevel: 'medium'});
  const count = h.requests.length;
  h.run("handleFolderPropertiesChanged({detail:{folderId:'project'}})");
  await flush();
  assert.equal(h.requests.length, count);
  await h.run("ensureServerConversationForSend('temporary', {})");
  assert.deepEqual(JSON.parse(JSON.stringify(h.creates[0].runConfig)), {...current, mainThinkingLevel: 'medium'});
  assert.equal(h.creates[0].folderId, '');
});

test("temporary defaults failure blocks creation instead of falling back to an unrelated model", async () => {
  const h = harness({folderId: '', getDefaults: () => {throw new Error('offline');}});
  await assert.rejects(h.run("ensureServerConversationForSend('hello', {})"), /无法读取会话默认配置/);
  assert.equal(h.creates.length, 0);
});

test("temporary property changes do not refresh a project draft", async () => {
  const h = harness();
  await h.run("loadLocalRunDefaults()");
  h.run("handleFolderPropertiesChanged({detail:{folderId:'__temporary'}})");
  await flush();
  assert.equal(h.requests.length, 1);
  assert.deepEqual(h.config(), paid);
});

test("existing conversations ignore directory defaults refresh events", async () => {
  const h = harness();
  h.state.props.conversationUuid = "existing-conversation";
  h.state.folderChanged("other", "project");
  h.run("handleFolderPropertiesChanged()");
  await flush();
  assert.equal(h.requests.length, 0);
  assert.equal(h.resets.length, 0);
});
