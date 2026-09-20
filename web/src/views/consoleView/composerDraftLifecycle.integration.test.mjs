import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {ref, nextTick} from 'vue';
import {createAttachmentDraftStorage} from './attachmentDraftStorage.js';
import {draftEnvironment, mountDraftConsole, settleDraftConsole, deferred, draftFile, draftNames} from './consoleDraftLifecycleHarness.mjs';

const TEXT_KEY = 'openbear.console.drafts.v1';
const options = {models: [{key: 'test-model'}], primaryModel: 'test-model'};

for (const uuid of ['A', 'local:new']) {
  test(`RC01: ${uuid} restores text before editing, and late initialization keeps later edits`, async () => {
    const env = draftEnvironment(), optionGate = deferred(), stateGate = deferred();
    env.window.localStorage.setItem(TEXT_KEY, JSON.stringify({[uuid]: '原草稿', other: '另一会话'}));
    env.api.rathOptions = () => optionGate.promise;
    env.api.conversationState = () => stateGate.promise;
    env.api.conversationDefaults = () => stateGate.promise;
    const h = await mountDraftConsole(env, uuid);
    try {
      assert.equal(h.state.draft, '原草稿', 'the first editable render must not be blank');
      h.state.draft += '，继续输入';
      await settleDraftConsole();
      optionGate.resolve(options);
      await settleDraftConsole();
      h.state.draft += '，等待期间再编辑';
      await settleDraftConsole();
      stateGate.resolve(uuid === 'A' ? env.snapshot(uuid) : {defaults: {mainModel: 'test-model'}});
      await settleDraftConsole();
      assert.equal(h.state.draft, '原草稿，继续输入，等待期间再编辑');
      assert.deepEqual(JSON.parse(env.window.localStorage.getItem(TEXT_KEY)), {
        [uuid]: '原草稿，继续输入，等待期间再编辑', other: '另一会话',
      });
    } finally {
      optionGate.resolve(options); stateGate.resolve(env.snapshot(uuid));
      await settleDraftConsole(); h.unmount(); env.cleanup();
    }
  });
}

test('RC01: clearing a restored draft while initialization waits does not resurrect it', async () => {
  const env = draftEnvironment(), loading = deferred();
  env.window.localStorage.setItem(TEXT_KEY, JSON.stringify({A: '清空这段文字'}));
  env.api.conversationState = () => loading.promise;
  const h = await mountDraftConsole(env, 'A');
  try {
    assert.equal(h.state.draft, '清空这段文字');
    // ConsoleComposer emits update:draft before clear-draft.
    h.state.draft = '';
    h.state.clearDraftAndAttachments();
    await settleDraftConsole();
    loading.resolve(env.snapshot('A'));
    await settleDraftConsole();
    assert.equal(h.state.draft, '');
    assert.deepEqual(JSON.parse(env.window.localStorage.getItem(TEXT_KEY)), {});
  } finally {
    loading.resolve(env.snapshot('A')); await settleDraftConsole(); h.unmount(); env.cleanup();
  }
});

const app = fs.readFileSync(new URL('../../App.vue', import.meta.url), 'utf8');
const start = app.indexOf('function discardConversationDraft(');
const end = app.indexOf('function closeConversationMenu(', start);
assert.ok(start >= 0 && end > start);
const actualAppDeletion = app.slice(start, end);
function appDeletion(env, {mounted = null, confirm = async () => true} = {}) {
  const row = {conversationUuid: 'local:new', local: true, folderId: '', title: '新会话'};
  const focused = [], notices = [], deletes = [];
  const attachmentDrafts = createAttachmentDraftStorage({driver: env.driver});
  const context = vm.createContext({
    window: env.window, consoleViewRef: ref(mounted), attachmentDrafts,
    conversations: ref([row]), activeConversationUuid: ref('local:new'), selectedFolderId: ref(''),
    deletingConversations: new Set(), conversationTreeRef: ref({forgetConversation() {}}),
    isLocalConversation: row => Boolean(row?.local), isRunning: row => Boolean(row?.running),
    conversationTitle: row => row.title, ElMessageBox: {confirm},
    ElMessage: Object.fromEntries(['error', 'warning', 'success'].map(level => [level, value => notices.push([level, value])])),
    Api: {deleteConversation: async uuid => deletes.push(uuid), conversationTreeChildren: async () => ({items: []})},
    apiError: String, nextTick, syncRoute() {}, setDraftFolderId() {}, loadConversations: async () => {},
    focusLocalConversation: () => focused.push('local:new'),
    setConversationsIfChanged: rows => {context.conversations.value = rows;},
  });
  vm.runInContext(actualAppDeletion, context);
  return {attachmentDrafts, focused, notices, deletes, remove: () => context.deleteConversation(row)};
}

test('RC02: confirmed deletion after Console unmount clears only its files before a new draft opens', async () => {
  const env = draftEnvironment();
  const h = await mountDraftConsole(env, 'local:new');
  await settleDraftConsole();
  h.state.draft = '删除这份草稿';
  h.state.addAttachment(draftFile('delete-one.txt'));
  h.state.addAttachment(draftFile('delete-two.txt'));
  await settleDraftConsole();
  await h.state.attachmentDrafts.save('other', [{id: 'kept', file: draftFile('keep.txt')}]);
  assert.deepEqual(draftNames(h), ['delete-one.txt', 'delete-two.txt']);
  h.unmount();
  const deletion = appDeletion(env), deleting = deferred(), originalDelete = env.driver.delete;
  const removedKeys = [];
  env.driver.delete = async key => {removedKeys.push(key); await deleting.promise; return originalDelete(key);};
  const job = deletion.remove();
  let reopened;
  try {
    await settleDraftConsole();
    assert.deepEqual(removedKeys, ['local:new'], 'the unmounted App fallback must clear attachments too');
    assert.deepEqual(deletion.focused, [], 'do not reuse local:new before its deletion commits');
    deleting.resolve(); await job;
    assert.deepEqual(deletion.deletes, [], 'local draft deletion is still not a server DELETE');
    assert.deepEqual(deletion.focused, ['local:new']);
    assert.equal(JSON.parse(env.window.localStorage.getItem(TEXT_KEY))['local:new'], undefined);
    assert.deepEqual((await deletion.attachmentDrafts.load('other')).items.map(item => item.file.name), ['keep.txt']);
    reopened = await mountDraftConsole(env, 'local:new'); await settleDraftConsole();
    assert.equal(reopened.state.draft, '');
    assert.deepEqual(draftNames(reopened), []);
  } finally {
    deleting.resolve(); await job; reopened?.unmount(); env.cleanup();
  }
});

test('RC02: cancelling deletion retains the unmounted draft files and text', async () => {
  const env = draftEnvironment();
  env.window.localStorage.setItem(TEXT_KEY, JSON.stringify({'local:new': '保留'}));
  const deletion = appDeletion(env, {confirm: async () => {throw 'cancel';}});
  try {
    await deletion.attachmentDrafts.save('local:new', [{id: 'kept', file: draftFile('keep.txt')}]);
    await deletion.remove();
    assert.deepEqual((await deletion.attachmentDrafts.load('local:new')).items.map(item => item.file.name), ['keep.txt']);
    assert.equal(JSON.parse(env.window.localStorage.getItem(TEXT_KEY))['local:new'], '保留');
    assert.deepEqual(deletion.focused, []);
  } finally {env.cleanup();}
});
