import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { computed, nextTick, reactive, ref } from 'vue';
import { treeItemId as rowId, treeItemParent, compareTreeItems, resolveTreeDrop } from './conversationTreeInteractions.js';

const source = fs.readFileSync(new URL('./ConversationTree.vue', import.meta.url), 'utf8');
const script = source.match(/<script setup>([\s\S]*?)<\/script>/)[1].replace(/^import[\s\S]*?;\n/gm, '');
const folder = (folderId, parentId = '') => ({ kind: 'folder', folderId, id: folderId, parentId, name: folderId });
const conversation = (id, folderId = 'a', extra = {}) => ({ kind: 'conversation', id, conversationUuid: id, folderId, title: id, ...extra });
function deferred() { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; }
function harness(Api = {}) {
  const errors = [];
  const context = vm.createContext({ computed, nextTick, reactive, ref, rowId, treeItemParent, compareTreeItems, resolveTreeDrop, Api, apiError: String, referenceCatalog: { connected: false, ready: false },
    defineProps: () => ({ activeConversationUuid: '', draftConversation: null }), defineEmits: () => () => {}, defineExpose() {},
    watch() {}, onMounted() {}, onBeforeUnmount() {}, ElMessage: { error(value) { errors.push(value); }, warning() {} }, document: { querySelector: () => null }, CSS: { escape: s => s },
  });
  vm.runInContext(script + '\nglobalThis.tree = { visibleRows, rootFolders, stateFor, setExpanded, rowLoading, loadChildren, invalidateBranch, loading, initialized, refreshTree, refreshStatus, forgetConversation, forgetFolder, moveTreeItem, movingRowId };', context);
  const tree = context.tree;
  tree.errors = errors;
  tree.rootFolders.value = [folder('a'), folder('b')];
  tree.setExpanded('a', true);
  tree.initialized.value = true;
  return tree;
}
const ids = tree => JSON.stringify(tree.visibleRows.value.map(row => [row.kind, row.id || rowId(row)]));

for (const [parent, system, owner, item] of [
  ['a', '', folder('a'), conversation('c')],
  ['', 'temporary', { kind: 'system', id: '__temporary', systemNode: 'temporary' }, conversation('t', '')],
  ['', 'archive', { kind: 'system', id: '__archive', systemNode: 'archive' }, conversation('old', 'a', { archived: true })],
]) {
  test(`${system || 'folder'} refresh keeps the same rows and indicates only its owner, not its children`, async () => {
    const gate = deferred(); const tree = harness({ conversationTreeChildren: () => gate.promise });
    tree.setExpanded(owner.id, true);
    const branch = tree.stateFor(parent, system);
    Object.assign(branch, { loaded: true, pages: 1, items: [item] });
    const before = ids(tree);
    const pending = tree.loadChildren(parent, system, { force: true });
    assert.equal(ids(tree), before);
    assert.equal(tree.rowLoading(owner), true);
    assert.equal(tree.rowLoading(item), false);
    assert.equal(tree.rowLoading(folder('b')), false);
    assert.ok(tree.visibleRows.value.every(row => row.kind !== 'loading'));
    gate.resolve({ items: [item] }); await pending;
    assert.equal(ids(tree), before);
    assert.equal(tree.rowLoading(owner), false); assert.equal(tree.rowLoading(item), false);
  });
  test(`${system || 'folder'} known empty row does not disappear while reloading`, async () => {
    const gate = deferred(); const tree = harness({ conversationTreeChildren: () => gate.promise });
    tree.setExpanded(owner.id, true);
    Object.assign(tree.stateFor(parent, system), { loaded: true, pages: 1 });
    const before = ids(tree);
    tree.invalidateBranch(parent, system);
    const pending = tree.loadChildren(parent, system, { force: true });
    assert.equal(ids(tree), before);
    gate.resolve({ items: [] }); await pending; assert.equal(ids(tree), before);
  });
}

test('first lazy expansion adds no temporary row; parent shows busy until results arrive', async () => {
  const gate = deferred(); const tree = harness({ conversationTreeChildren: () => gate.promise });
  const before = ids(tree); const pending = tree.loadChildren('a');
  assert.equal(ids(tree), before); assert.equal(tree.rowLoading(folder('a')), true);
  gate.resolve({ items: [conversation('c')] }); await pending;
  assert.equal(tree.rowLoading(folder('a')), false); assert.ok(tree.visibleRows.value.some(row => row.id === 'c'));
});
test('pagination keeps the existing more row; only actual returned children add height', async () => {
  const gate = deferred(); const tree = harness({ conversationTreeChildren: () => gate.promise });
  Object.assign(tree.stateFor('a'), { loaded: true, pages: 1, items: [conversation('c')], hasMore: true, nextCursor: 'next' });
  const before = ids(tree); const pending = tree.loadChildren('a', '', { append: true });
  assert.equal(ids(tree), before);
  gate.resolve({ items: [conversation('d')] }); await pending;
  assert.equal(tree.stateFor('a').items.length, 2); assert.equal(tree.rowLoading(folder('a')), false);
});
test('stale branch completion cannot clear a newer request spinner; failure clears busy and retains children', async () => {
  const first = deferred(), second = deferred(); let count = 0;
  const tree = harness({ conversationTreeChildren: () => (++count === 1 ? first.promise : second.promise) });
  Object.assign(tree.stateFor('a'), { loaded: true, pages: 1, items: [conversation('c')] });
  const old = tree.loadChildren('a', '', { force: true }); const newer = tree.loadChildren('a', '', { force: true });
  first.resolve({ items: [] }); await old; assert.equal(tree.rowLoading(folder('a')), true);
  second.reject(Error('network failure')); await newer;
  assert.equal(tree.rowLoading(folder('a')), false); assert.equal(tree.stateFor('a').items.length, 1);
  assert.ok(tree.visibleRows.value.some(row => row.kind === 'error'));
});
test('global bootstrap uses toolbar loading without painting every row as loading', async () => {
  const gate = deferred(); const tree = harness({ conversationTreeBootstrap: () => gate.promise });
  const before = ids(tree); const pending = tree.refreshTree({ refreshLoaded: false });
  assert.equal(ids(tree), before); assert.equal(tree.rowLoading(folder('a')), false); assert.equal(tree.rowLoading(folder('b')), false); assert.equal(tree.loading.value, true);
  gate.resolve({ rootFolders: [folder('a'), folder('b')], running: {} }); await pending;
  assert.equal(tree.rowLoading(folder('a')), false); assert.equal(tree.loading.value, false);
});
test('invalidating bootstrap during removal cannot strand refresh loading', async () => {
  for (const removal of ['forgetConversation', 'forgetFolder']) {
    const gate = deferred(); const tree = harness({ conversationTreeBootstrap: () => gate.promise });
    const pending = tree.refreshTree({ refreshLoaded: false }); tree[removal]('missing');
    assert.equal(tree.loading.value, false);
    gate.resolve({ rootFolders: [] }); await pending; assert.equal(tree.loading.value, false);
  }
});
test('move request marks only the moved row busy and clears on error; status polling does not animate icons', async () => {
  const gate = deferred(); const tree = harness({ moveConversationTreeItem: () => gate.promise, conversationTreeStatus: async () => ({ items: [] }) });
  const moved = conversation('c'); const pending = tree.moveTreeItem(moved, 'a', 'd');
  assert.equal(tree.rowLoading(moved), true); assert.equal(tree.rowLoading(conversation('d')), false);
  gate.reject(Error('move failed')); await assert.rejects(pending);
  assert.equal(tree.rowLoading(moved), false); await tree.refreshStatus(); assert.equal(tree.rowLoading(folder('a')), false);
});
test('background refresh of loaded branches does not animate their owners or children', async () => {
  const gate = deferred(),started=deferred(); const tree = harness({conversationTreeBootstrap:async()=>({rootFolders:[folder('a'),folder('b')],running:{}}),conversationTreeChildren:()=>{started.resolve();return gate.promise;}});
  Object.assign(tree.stateFor('a'),{loaded:true,pages:1,items:[conversation('c'),folder('nested','a')]});
  const before=ids(tree);const pending=tree.refreshTree();await started.promise;
  assert.deepEqual(tree.errors,[]);
  assert.equal(tree.stateFor('a').loading,true);assert.equal(tree.loading.value,true);
  for(const row of [folder('a'),folder('b'),folder('nested','a'),conversation('c')])assert.equal(tree.rowLoading(row),false);
  assert.equal(ids(tree),before);gate.resolve({items:[conversation('c'),folder('nested','a')]});await pending;
});
test('simultaneous explicit branch loads show only the two requested owners', async () => {
  const a=deferred(),b=deferred();const tree=harness({conversationTreeChildren:({parentId})=>parentId==='a'?a.promise:b.promise});
  const pa=tree.loadChildren('a'),pb=tree.loadChildren('b');
  assert.equal(tree.rowLoading(folder('a')),true);assert.equal(tree.rowLoading(folder('b')),true);
  assert.equal(tree.rowLoading(folder('nested','a')),false);assert.equal(tree.rowLoading(conversation('c')),false);
  a.resolve({items:[]});await pa;assert.equal(tree.rowLoading(folder('a')),false);assert.equal(tree.rowLoading(folder('b')),true);
  b.resolve({items:[]});await pb;assert.equal(tree.rowLoading(folder('b')),false);
});
test('explicit expansion promotes an in-flight background request without issuing another request', async () => {
  const gate=deferred();let calls=0;const tree=harness({conversationTreeChildren:()=>{calls++;return gate.promise;}});
  const pending=tree.loadChildren('a','',{force:true,indicate:false});assert.equal(tree.rowLoading(folder('a')),false);
  await tree.loadChildren('a');assert.equal(calls,1);assert.equal(tree.rowLoading(folder('a')),true);
  gate.resolve({items:[]});await pending;assert.equal(tree.rowLoading(folder('a')),false);
});
test('background supersession preserves explicit owner indication until latest completion', async () => {
  const first=deferred(),second=deferred();let calls=0;const tree=harness({conversationTreeChildren:()=>++calls===1?first.promise:second.promise});
  const p1=tree.loadChildren('a');const p2=tree.loadChildren('a','',{force:true,indicate:false});
  first.resolve({items:[]});await p1;assert.equal(tree.rowLoading(folder('a')),true);
  second.resolve({items:[]});await p2;assert.equal(tree.rowLoading(folder('a')),false);
});
test('cancelling a pending branch clears its indicator and late completion cannot restore it', async () => {
  const gate=deferred();const tree=harness({conversationTreeChildren:()=>gate.promise});const pending=tree.loadChildren('a');
  tree.invalidateBranch('a');assert.equal(tree.rowLoading(folder('a')),false);
  gate.resolve({items:[]});await pending;assert.equal(tree.rowLoading(folder('a')),false);
});
test('search loading belongs to search control rather than all result row icons', () => {
  const body=source.slice(source.indexOf('function rowLoading('),source.indexOf('function withDraft('));
  assert.doesNotMatch(body,/searchLoading|loading\.value|treeItemParent/);
  assert.match(source,/:is="searchLoading \? Loading : Search"/);
});
test('both node icon types use the same fixed icon slot; loading rows are absent and pagination remains guarded', () => {
  assert.equal((source.match(/class="node-icon" :class="\{ 'is-spinning': rowLoading\(row\)(?:, 'is-working': running\(row\) && !rowLoading\(row\))? \}"/g) || []).length, 2);
  assert.match(source, /'is-working': running\(row\) && !rowLoading\(row\)/);
  assert.doesNotMatch(source, /kind: "loading"|row\.kind === 'loading'/);
  assert.match(source, /:aria-busy="rowLoading\(row\)"/);
  assert.match(source, /:disabled="stateFor\(row\.parentId \|\| '', row\.systemNode \|\| ''\)\.loading"/);
});
