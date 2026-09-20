import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {computed, nextTick, reactive, ref} from 'vue';
import {treeItemId as rowId, treeItemParent, compareTreeItems, resolveTreeDrop} from './conversationTreeInteractions.js';

const source = fs.readFileSync(new URL('./ConversationTree.vue', import.meta.url), 'utf8');
const script = source.match(/<script setup>([\s\S]*?)<\/script>/)[1].replace(/^import[\s\S]*?;\n/gm, '');
function harness() {
  const context = vm.createContext({
    computed, nextTick, reactive, ref, rowId, treeItemParent, compareTreeItems, resolveTreeDrop,
    Api: {}, apiError: String, referenceCatalog: {connected: false, ready: false},
    defineLazyView: () => ({}), defineProps: () => ({activeConversationUuid: '', draftConversation: null}),
    defineEmits: () => () => {}, defineExpose() {}, watch() {}, onMounted() {}, onBeforeUnmount() {},
    ElMessage: {error() {}, warning() {}}, document: {querySelector: () => null}, CSS: {escape: s => s},
  });
  vm.runInContext(script + '\nglobalThis.tree={rootFolders,stateFor,searchRows,applyStatus,sameRowShape};', context);
  return context.tree;
}
const conversation = id => ({kind: 'conversation', conversationUuid: id, archived: false});
const packet = target => ({
  items: [{conversationUuid: 'active', running: true, activityVersion: 1,
    activityReadVersion: 0, activityPending: [{kind: 'confirmation', target: {id: target}}]}],
  folderConversationCounts: {root: 2}, folderRunningCounts: {root: 1},
});

test('identical status packets preserve every idle conversation and branch reference', () => {
  const t = harness();
  const branch = t.stateFor('root');
  branch.items = Array.from({length: 1000}, (_, i) => conversation(`idle-${i}`));
  t.searchRows.value = [conversation('search')];
  t.applyStatus({items: []});
  const rows = branch.items;
  const search = t.searchRows.value;
  const identities = [...rows];
  t.applyStatus(JSON.parse('{"items":[]}'));
  assert.equal(branch.items, rows);
  assert.equal(t.searchRows.value, search);
  assert.ok(branch.items.every((row, i) => row === identities[i]));
});

test('new JSON identities with equal nested activity preserve active rows and folders', () => {
  const t = harness();
  t.rootFolders.value = [{kind: 'folder', folderId: 'root', conversationCount: 2}];
  const branch = t.stateFor('root');
  branch.items = [conversation('active'), conversation('idle')];
  t.applyStatus(packet('request-a'));
  const rows = branch.items;
  const folders = t.rootFolders.value;
  const activity = rows[0].activityPending;
  t.applyStatus(JSON.parse(JSON.stringify(packet('request-a'))));
  assert.equal(branch.items, rows);
  assert.equal(t.rootFolders.value, folders);
  assert.equal(branch.items[0].activityPending, activity);
});

test('real nested activity changes update only the affected row', () => {
  const t = harness();
  const branch = t.stateFor('root');
  branch.items = [conversation('active'), conversation('idle')];
  t.applyStatus(packet('request-a'));
  const rows = branch.items;
  t.applyStatus(packet('request-b'));
  assert.notEqual(branch.items, rows);
  assert.notEqual(branch.items[0], rows[0]);
  assert.equal(branch.items[1], rows[1]);
  assert.equal(branch.items[0].activityPending[0].target.id, 'request-b');
  assert.equal(rows[0].activityPending[0].target.id, 'request-a');
});

test('completion clears activity and stabilizes on the following idle packet', () => {
  const t = harness();
  const branch = t.stateFor('root');
  branch.items = [conversation('active')];
  t.applyStatus(packet('request-a'));
  const running = branch.items[0];
  t.applyStatus({items: []});
  assert.notEqual(branch.items[0], running);
  assert.equal(branch.items[0].running, false);
  assert.equal(branch.items[0].activityUnread, false);
  assert.equal(branch.items[0].activityPending.length, 0);
  const idle = branch.items;
  t.applyStatus({items: []});
  assert.equal(branch.items, idle);
});

test('comparison distinguishes missing keys and array/object shapes', () => {
  const t = harness();
  assert.equal(t.sameRowShape({a: undefined}, {b: undefined}), false);
  assert.equal(t.sameRowShape({items: []}, {items: {}}), false);
  assert.equal(t.sameRowShape({items: [{value: null}]}, {items: [{value: 0}]}), false);
});
