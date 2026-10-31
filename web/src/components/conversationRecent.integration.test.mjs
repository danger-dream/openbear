import * as promptPolicy from './promptPolicy.js';
import test from 'node:test';
import {useRecentConversationRows} from './conversationRecentRows.js';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {compile, computed, createSSRApp, effectScope, h, nextTick, proxyRefs, reactive, ref, toRaw, watch} from 'vue';
import {renderToString} from 'vue/server-renderer';
import {parse} from '@vue/compiler-sfc';
import {baseParse} from '@vue/compiler-dom';
import * as icons from '@element-plus/icons-vue';
import {treeItemId as rowId, treeItemParent, compareTreeItems, resolveTreeDrop} from './conversationTreeInteractions.js';
import {activityLabel, activityState, activityReadRequests, applyActivityReadVersions, isActivityWaiting, pendingConversationRows, pendingFolderCounts, folderActivityCounts} from '../conversationActivity.js';
import * as conversationGroupsTree from './conversationGroupsTree.js';
import * as conversationTreeSelection from './conversationTreeSelection.js';

const read = file => fs.readFileSync(new URL(file, import.meta.url), 'utf8');
const treeSource = parse(read('./ConversationTree.vue')).descriptor;
const strip = descriptor => descriptor.scriptSetup.content.replace(/^import[\s\S]*?;\n/gm, '');
const walk = nodes => (nodes || []).flatMap(node => node && typeof node === 'object' ? [node, ...walk(Array.isArray(node.children) ? node.children : [])] : []);
const hasClass = (node, value) => String(node.props?.class || '').split(/\s+/).includes(value);
const treeTemplate = walk(baseParse(treeSource.template.content).children).find(node => node.props?.some(prop => prop.name === 'ref' && prop.value?.content === 'listRef')).loc.source;
const row = (id, at = 1000, extra = {}) => ({kind: 'conversation', id, conversationUuid: id, folderId: 'F', parentId: 'F', title: id, path: '目录 F', lastInteractionAtMs: at, activityVersion: 1, activityReadVersion: 1, activityUnread: false, activityState: 'completed', running: false, ...extra});

function treeHarness(t) {
  const scope = effectScope(); t.after(() => scope.stop());
  const props = reactive({activeConversationUuid: '', draftConversation: null});
  const catalog = reactive({connected: false, ready: false, activityReadVersions: new Map()});
  const emitted = [], timers = new Map(); let time = 0, serial = 0;
  const ctx = vm.createContext({...conversationGroupsTree,...conversationTreeSelection,...promptPolicy,...icons, referenceItem: () => null, useRecentConversationRows, computed, nextTick, reactive, ref, watch, rowId, treeItemParent, compareTreeItems, resolveTreeDrop,
    activityLabel, activityState, activityReadRequests, isActivityWaiting, pendingConversationRows, pendingFolderCounts, folderActivityCounts, referenceCatalog: catalog,
    defineLazyView: () => ({}), defineProps: () => props, defineEmits: () => (...args) => emitted.push(args), defineExpose() {}, onMounted() {}, onBeforeUnmount() {},
    Api: {}, apiError: String, ElMessage: {error(value) {assert.fail(String(value));}},
    window: {matchMedia: query => ({matches: query === '(hover: hover)'})}, document: {querySelector: () => null}, CSS: {escape: value => value},
    setTimeout(fn, delay) {timers.set(++serial, {fn, at: time + delay}); return serial;}, clearTimeout(id) {timers.delete(id);},
  });
  scope.run(() => vm.runInContext(strip(treeSource) + `
    globalThis.tree={selecting,isSelected,selectableRow,toggleSelected,clickConversation,props,activityItems,recentItems,activityReadBusy,titleGenerating,referenceCatalog,applyStatus,knownConversationRows,forgetConversation,withDraft,rootFolders,stateFor,expanded,selectedFolderId,openActivityConversation,markActivityRead,overview,enterOverview,leaveOverview,closeOverview,keepOverview,menu,openMoreMenu,
      activeConversationRow,sidebarView,isDirectoryView,switchSidebarView,recentClock,recentConversationRows,recentUnreadCount,recentWaitingCount,recentLabel,displayRows,query,initialized,loading,listRef,
      activateRow,toggleRow,dragStart,dragOver,drop,clearDrag,rootDropTarget,drag,moveInFlight,rowId,rowLabel,rowLoading,nodePath,indentation,isExpanded,running,isTitleGenerating,liveConversationTitle,conversationOrigin,
      loadChildren,searchHasMore,searchRows,searchLoading,runSearch,locateAndOpen,openMenu,openRootMenu,rowKeydown,moreMenuKeydown,clearDropTarget};
    initialized.value=true;`, ctx));
  const tree = ctx.tree;
  return {tree, props, catalog, emitted, run: text => vm.runInContext(text, ctx),
    tick(ms) {time += ms; for (const [id, timer] of [...timers]) if (timer.at <= time) {timers.delete(id); timer.fn();}},
    async render() {
      let vnode;
      const bindings = proxyRefs({...icons, ...tree, activityLabel, isActivityWaiting, activeConversationUuid: props.activeConversationUuid});
      const renderer = compile(treeTemplate);
      const app = createSSRApp({render() {vnode = renderer.call(this, bindings, []); return vnode;}});
      for (const [name, icon] of Object.entries(icons)) app.component(name, icon);
      app.component('AnimatedConversationTitle', {props: ['text'], render() {return h('span', this.text);}});
      app.component('el-icon', {render() {return h('i', this.$slots.default?.());}});
      const html = await renderToString(app);
      return {html, vnode, nodes: walk([vnode])};
    },
  };
}

for (const origin of ['webhook', 'cron', 'recommendation', 'deployment', 'maintenance']) {
  test(`automatic ${origin} remains available in pending work without appearing in recent rows`, async t => {
    const h = treeHarness(t);
    const pending = row(`automatic-${origin}`, 0, {origin, running: true, activityState: 'waiting',
      activityPending: [{interactionId: 'pending-1', action: 'confirm'}]});
    h.tree.applyStatus({items: [pending], activityItems: [], recentItems: []});
    await nextTick();
    assert.equal(h.tree.recentConversationRows.value.length, 0);
    assert.equal(h.tree.recentWaitingCount.value, 1);
    assert.equal(h.tree.recentUnreadCount.value, 0);
    assert.doesNotMatch((await h.render()).html, /automatic-/);
  });
}

test('read receipts clear badges without removing recent aliases or changing their order', () => {
  const unread = row('done', 2000, {activityReadVersion: 0, activityUnread: true});
  const status = {items: [], activityItems: [unread], recentItems: [unread, row('old', 1000)]};
  const adjusted = applyActivityReadVersions(status, new Map([['done', 1]]));
  assert.equal(adjusted.activityItems.length, 0);
  assert.deepEqual(adjusted.recentItems.map(item => item.conversationUuid), ['done', 'old']);
  assert.equal(adjusted.recentItems[0].activityUnread, false);
  assert.equal(status.recentItems[0].activityUnread, true);
});

test('actual tree renders recent aliases; opening and deleting reuse existing navigation without moving folders', async t => {
  const h = treeHarness(t), item = row('recent', h.tree.recentClock.value - 180000);
  h.tree.applyStatus({items: [], activityItems: [], recentItems: [item]});
  assert.equal(h.tree.recentConversationRows.value[0].conversationUuid, 'recent');
  assert.ok(h.tree.knownConversationRows().some(item => item.conversationUuid === 'recent'));
  const view = await h.render();
  assert.match(view.html, /3 分钟前/);
  const alias = view.nodes.find(node => node.props?.['data-tree-id'] === 'recent:recent');
  h.tree.selectedFolderId.value = 'do-not-change';
  await walk([alias]).find(node => hasClass(node, 'conversation')).props.onClick();
  const opened = h.emitted.find(event => event[0] === 'open')[1];
  assert.equal(opened.conversationUuid, 'recent'); assert.equal(opened.folderId, 'F'); assert.equal('recentAlias' in opened, false);
  assert.equal(h.tree.selectedFolderId.value, 'do-not-change'); assert.equal(h.tree.expanded.value.size, 0);
  h.tree.forgetConversation('recent');
  assert.equal(h.tree.recentItems.value.length, 0);
  assert.equal(h.tree.knownConversationRows().some(item => item.conversationUuid === 'recent'), false);
  assert.ok(!(await h.render()).nodes.some(node => node.props?.['data-tree-id'] === 'recent:recent'));
});

test('compiled recent rows open the desktop overview and ignore touch; scroll/view switches close it', async t => {
  const h = treeHarness(t), running = row('working', 3000, {running: true}), recent = row('recent', 2000);
  h.tree.applyStatus({items: [running], activityItems: [running], recentItems: [recent]});
  const view = await h.render();
  const rows = view.nodes.filter(node => node.props?.['data-kind'] === 'conversation');
  assert.equal(rows.length, 2);
  const anchor = {isConnected: true, getBoundingClientRect: () => ({left: 20, right: 320})};
  const event = {pointerType: 'mouse', currentTarget: {querySelector: selector => selector === 'button.conversation' ? anchor : null}};
  for (const node of rows) {
    node.props.onPointerenter(event); h.tick(279); assert.equal(h.tree.overview.value.open, false);
    h.tick(1); assert.equal(`recent:${h.tree.overview.value.row.conversationUuid}`, node.props['data-tree-id']);
    assert.equal(toRaw(h.tree.overview.value.anchor), anchor);
    assert.equal(h.tree.overview.value.anchor.getBoundingClientRect().right, 320, 'overview uses the shared conversation button, including its trailing status');
    node.props.onPointerleave(); h.tick(100); h.tree.keepOverview(); h.tick(100); assert.equal(h.tree.overview.value.open, true);
    view.nodes.find(node => hasClass(node, 'tree-list')).props.onScrollPassive();
    assert.equal(h.tree.overview.value.open, false);
    node.props.onPointerenter({...event, pointerType: 'touch'}); h.tick(300); assert.equal(h.tree.overview.value.open, false);
  }
  rows[0].props.onPointerenter(event); h.tick(280);
  await h.tree.switchSidebarView('folders');
  assert.equal(h.tree.overview.value.open, false);
});

test('right-clicking a recent row opens the ordinary conversation menu at the pointer', async t => {
  const h = treeHarness(t), item = row('recent-menu', 2000);
  h.tree.applyStatus({items: [], recentItems: [item]});
  const view = await h.render();
  const recentRow = view.nodes.find(node => node.props?.['data-tree-id'] === 'recent:recent-menu');
  let prevented = 0, stopped = 0;
  recentRow.props.onContextmenu({
    type: 'contextmenu', clientX: 123, clientY: 77,
    preventDefault() {prevented++;}, stopPropagation() {stopped++;},
  });
  await nextTick();
  assert.ok(prevented >= 1); assert.ok(stopped >= 1);
  assert.equal(h.tree.menu.value.open, true);
  assert.equal(h.tree.menu.value.row.conversationUuid, 'recent-menu');
  assert.equal('recentAlias' in h.tree.menu.value.row, false);
  assert.equal(h.tree.menu.value.x, 123); assert.equal(h.tree.menu.value.y, 77);
});

test('the same conversation reanchors when moving between the tree and its recent alias', t => {
  const h = treeHarness(t), item = row('same-conversation', 1000);
  const original = {isConnected: true, getBoundingClientRect: () => ({right: 300, top: 400})};
  const recent = {isConnected: true, querySelector: () => null, getBoundingClientRect: () => ({right: 320, top: 70})};
  h.tree.enterOverview({pointerType: 'mouse', currentTarget: {querySelector: () => original}}, item);
  h.tick(280); assert.equal(toRaw(h.tree.overview.value.anchor), original);
  h.tree.leaveOverview(); h.tick(80);
  h.tree.enterOverview({pointerType: 'mouse', currentTarget: recent}, item);
  h.tick(280); assert.equal(toRaw(h.tree.overview.value.anchor), recent);
  assert.equal(h.tree.overview.value.anchor.getBoundingClientRect().top, 70);
  h.tree.enterOverview({pointerType: 'mouse', currentTarget: {querySelector: () => original}}, item);
  h.tick(280); assert.equal(toRaw(h.tree.overview.value.anchor), original);
});

test('status refresh does not close an alias overview merely because its original folder is unloaded', async t => {
  const h = treeHarness(t), item = row('alias', 1000);
  h.tree.applyStatus({items: [], recentItems: [item]});
  const anchor = {isConnected: true};
  h.tree.enterOverview({pointerType: 'mouse', currentTarget: {querySelector: () => anchor}}, h.tree.recentItems.value[0]);
  h.tick(280); assert.equal(h.tree.overview.value.open, true);
  h.tree.applyStatus({items: [], recentItems: [{...item}]}); await nextTick();
  assert.equal(h.tree.overview.value.open, true);
  h.tree.applyStatus({items: [], recentItems: []}); await nextTick();
  assert.equal(h.tree.overview.value.open, false);
});

test('local draft heads the unpinned conversations without mutating loaded rows or moving after send', t => {
  const h = treeHarness(t);
  const rows = [
    {kind: 'folder', folderId: 'child', parentId: 'F', name: '子文件夹', createdAt: 1},
    row('pinned', 1, {pinned: true, createdAt: 5}),
    row('manual', 1, {displayOrder: 1, createdAt: 10}),
    row('older', 1, {createdAt: 20}),
  ];
  const snapshot = JSON.stringify(rows);
  h.props.draftConversation = {local: true, conversationUuid: 'local:new', folderId: 'F', createdAt: 100};
  let merged = h.tree.withDraft(rows, 'F');
  assert.deepEqual(Array.from(merged, rowId), ['child', 'pinned', 'local:new', 'manual', 'older']);
  assert.equal(JSON.stringify(rows), snapshot);
  assert.equal(h.tree.withDraft(rows, 'other').length, rows.length);
  assert.equal(h.tree.withDraft(merged, 'F').length, merged.length);
  // First send persists the draft below the group's smallest displayOrder.
  const persisted = merged.map(item => item.local ? {...item, local: false, id: 'sent', conversationUuid: 'sent', displayOrder: 1 - 1024} : item).sort(compareTreeItems);
  h.props.draftConversation = null;
  assert.deepEqual(Array.from(h.tree.withDraft(persisted, 'F'), rowId), ['child', 'pinned', 'sent', 'manual', 'older']);
});

test('hidden activity and selected retention stay out of recent without losing status', async () => {
  const {reactive, nextTick} = await import('vue');
  const props = reactive({items: [{conversationUuid:'a', activityVersion:1, activityUnread:true, running:true}], recentItems:[], activeConversationUuid:'a', readVersions:new Map(), hiddenConversationIds:[]});
  const view = useRecentConversationRows(props);
  assert.equal(view.rows.value.length, 1);
  props.hiddenConversationIds = ['a'];
  props.items = [{...props.items[0], hiddenFromRecent:true}];
  await nextTick();
  assert.equal(view.rows.value.length, 0);
  assert.equal(props.items[0].running, true);
  props.items = [];
  await nextTick();
  assert.equal(view.rows.value.length, 0);
  props.hiddenConversationIds = [];
  props.items = [{conversationUuid:'a', activityVersion:1, activityUnread:true, running:true}];
  await nextTick();
  assert.equal(view.rows.value.length, 1);
});
