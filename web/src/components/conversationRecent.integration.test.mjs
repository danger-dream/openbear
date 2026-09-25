import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {compile, computed, createSSRApp, effectScope, h, nextTick, proxyRefs, reactive, ref, toRaw, watch} from 'vue';
import {renderToString} from 'vue/server-renderer';
import {parse} from '@vue/compiler-sfc';
import {baseParse} from '@vue/compiler-dom';
import {treeItemId as rowId, treeItemParent, compareTreeItems, resolveTreeDrop} from './conversationTreeInteractions.js';
import {activityLabel, activityState, activityReadRequests, applyActivityReadVersions, groupActivityItems} from '../conversationActivity.js';

const read = file => fs.readFileSync(new URL(file, import.meta.url), 'utf8');
const treeSource = parse(read('./ConversationTree.vue')).descriptor;
const folderSource = parse(read('./ConversationActivityFolder.vue')).descriptor;
const strip = descriptor => descriptor.scriptSetup.content.replace(/^import[\s\S]*?;\n/gm, '');
const walk = nodes => (nodes || []).flatMap(node => node && typeof node === 'object' ? [node, ...walk(Array.isArray(node.children) ? node.children : [])] : []);
const hasClass = (node, value) => String(node.props?.class || '').split(/\s+/).includes(value);
const activityTemplate = walk(baseParse(treeSource.template.content).children).find(node => node.tag === 'ConversationActivityFolder').loc.source;
const row = (id, at = 1000, extra = {}) => ({kind: 'conversation', id, conversationUuid: id, folderId: 'F', parentId: 'F', title: id, path: '目录 F', lastInteractionAtMs: at, activityVersion: 1, activityReadVersion: 1, activityUnread: false, activityState: 'completed', running: false, ...extra});

function treeHarness(t) {
  const scope = effectScope(); t.after(() => scope.stop());
  const props = reactive({activeConversationUuid: '', draftConversation: null});
  const catalog = reactive({connected: false, ready: false, activityReadVersions: new Map()});
  const emitted = [], timers = new Map(); let time = 0, serial = 0;
  const ctx = vm.createContext({computed, nextTick, reactive, ref, watch, rowId, treeItemParent, compareTreeItems, resolveTreeDrop,
    activityLabel, activityReadRequests, referenceCatalog: catalog,
    defineLazyView: () => ({}), defineProps: () => props, defineEmits: () => (...args) => emitted.push(args), defineExpose() {}, onMounted() {}, onBeforeUnmount() {},
    Api: {}, apiError: String, ElMessage: {error(value) {assert.fail(String(value));}},
    window: {matchMedia: () => ({matches: true})}, document: {querySelector: () => null}, CSS: {escape: value => value},
    setTimeout(fn, delay) {timers.set(++serial, {fn, at: time + delay}); return serial;}, clearTimeout(id) {timers.delete(id);},
  });
  scope.run(() => vm.runInContext(strip(treeSource) + '\nglobalThis.tree={props,activityItems,recentItems,activityReadBusy,titleGenerating,referenceCatalog,applyStatus,knownConversationRows,forgetConversation,withDraft,rootFolders,stateFor,expanded,selectedFolderId,openActivityConversation,markActivityRead,overview,enterOverview,leaveOverview,closeOverview,keepOverview,menu,openMoreMenu};', ctx));
  const tree = ctx.tree;
  return {tree, props, catalog, emitted, run: text => vm.runInContext(text, ctx),
    tick(ms) {time += ms; for (const [id, timer] of [...timers]) if (timer.at <= time) {timers.delete(id); timer.fn();}},
    async folderBindings() {
      let vnode;
      const bindings = proxyRefs({...tree, activeConversationUuid: props.activeConversationUuid});
      const renderer = compile(activityTemplate);
      const app = createSSRApp({render() {vnode = renderer.call(this, bindings, []); return vnode;}});
      let delivered;
      app.component('ConversationActivityFolder', {props: ['items', 'recentItems', 'activeConversationUuid', 'readVersions', 'busy', 'titleGenerating'], setup(props, {attrs}) {delivered = {...props, ...attrs}; return () => h('div');}});
      await renderToString(app);
      return delivered;
    },
  };
}

function folderHarness(t, props, dispatch = () => {}) {
  const scope = effectScope(); t.after(() => scope.stop());
  const emitted = [], mounted = [], unmounted = [], intervals = new Map(); let serial = 0;
  const ctx = vm.createContext({computed, ref, watch, activityLabel, activityState, groupActivityItems,
    referenceItem: () => null,
    defineProps: () => props, defineEmits: () => (...args) => {emitted.push(args); dispatch(...args);},
    onMounted: fn => mounted.push(fn), onBeforeUnmount: fn => unmounted.push(fn),
    setInterval: fn => {intervals.set(++serial, fn); return serial;}, clearInterval: id => intervals.delete(id),
  });
  scope.run(() => vm.runInContext(strip(folderSource), ctx));
  for (const icon of ['ArrowRight', 'Folder', 'FolderOpened', 'Check', 'MoreFilled']) ctx[icon] = {render: () => h('svg')};
  ctx.AnimatedConversationTitle = {props: ['text'], render() {return h('span', this.text);}};
  t.after(() => unmounted.forEach(fn => fn()));
  const renderer = compile(folderSource.template.content);
  return {emitted, mounted, unmounted, intervals, run: text => vm.runInContext(text, ctx), async render() {
    let vnode;
    const bindings = proxyRefs(vm.runInContext('({...props,props,emit,expanded,rows,unreadCount,waitingCount,rowState,rowLabel,rowTitle,liveTitle,isTitleGenerating,ArrowRight,Folder,FolderOpened,Check,MoreFilled,AnimatedConversationTitle})', ctx));
    const app = createSSRApp({components: {ArrowRight: ctx.ArrowRight, Check: ctx.Check, MoreFilled: ctx.MoreFilled, AnimatedConversationTitle: ctx.AnimatedConversationTitle}, render() {vnode = renderer.call(this, bindings, []); return vnode;}});
    const html = await renderToString(app);
    return {html, vnode, nodes: walk([vnode])};
  }};
}

const recentGroup = view => view.nodes.find(node => Object.hasOwn(node.props || {}, 'data-recent-conversations'));
const groupRows = group => walk([group]).filter(node => hasClass(node, 'activity-row'));

test('one recent list keeps newest five plus outstanding work, excludes archives/drafts, and remains after switching', async t => {
  const items = Array.from({length: 6}, (_, i) => row(`c${i}`, (i + 1) * 1000));
  const props = reactive({items: [row('working', 9000, {running: true}), row('unread', 8000, {activityUnread: true})], recentItems: [...items, {...items[0]}, row('archived', 99000, {archived: true}), row('draft', 99001, {local: true}), row('empty', 0)], activeConversationUuid: '', readVersions: new Map(), busy: false});
  const folder = folderHarness(t, props);
  let view = await folder.render();
  assert.match(view.html, /最近会话/);
  assert.deepEqual(groupRows(recentGroup(view)).map(node => node.props['data-activity-id']), ['working', 'unread', 'c5', 'c4', 'c3', 'c2', 'c1']);
  assert.equal(walk([recentGroup(view)]).filter(node => hasClass(node, 'activity-row-read')).length, 1);
  assert.doesNotMatch(view.html, /运行与未读|<h5|data-activity-group/);
  props.items = []; props.activeConversationUuid = 'different'; await nextTick();
  view = await folder.render(); assert.equal(groupRows(recentGroup(view)).length, 5);
  props.recentItems = items.filter(item => item.conversationUuid !== 'c5'); await nextTick();
  view = await folder.render();
  assert.deepEqual(groupRows(recentGroup(view)).map(node => node.props['data-activity-id']), ['c4', 'c3', 'c2', 'c1', 'c0']);
  assert.doesNotMatch(view.html, /暂无运行/);
});

test('running and unread recents merge into one row with one read action, live status and one count', async t => {
  const working = row('working', 3000, {running: true, activityState: 'running'});
  const unread = row('done', 2000, {activityReadVersion: 0, activityUnread: true});
  const recent = [row('working', 3000), unread, row('idle', 1000)];
  const props = reactive({items: [working, unread, working], recentItems: recent, activeConversationUuid: '', readVersions: new Map(), busy: false});
  const folder = folderHarness(t, props);
  const view = await folder.render();
  assert.deepEqual(groupRows(recentGroup(view)).map(node => node.props['data-activity-id']), ['working', 'done', 'idle']);
  assert.equal(view.nodes.filter(node => hasClass(node, 'activity-row-read')).length, 1);
  assert.equal(view.nodes.find(node => hasClass(node, 'activity-total')).children, '3');
  for (const node of groupRows(recentGroup(view))) {
    assert.ok(walk([node]).every(child => !Object.hasOwn(child.props || {}, 'title')), 'row descendants must not trigger native tooltips over the overview');
  }
  const workingRow = groupRows(recentGroup(view))[0];
  assert.equal(walk([workingRow]).find(node => hasClass(node, 'activity-row-open')).props['aria-label'], 'working · 目录 F · 运行中');
  assert.equal(walk([workingRow]).find(node => hasClass(node, 'activity-row-status-desktop')).children, '运行中');
  view.nodes.find(node => hasClass(node, 'activity-row-read')).props.onClick();
  assert.equal(folder.emitted.at(-1)[0], 'read');
  assert.equal(folder.emitted.at(-1)[1].conversationUuid, 'done');
  view.nodes.find(node => hasClass(node, 'activity-read-all')).props.onClick();
  assert.equal(folder.emitted.at(-1)[0], 'read-all');
});

test('pending, running, unread and failure labels take priority over time on both desktop and touch', async t => {
  const cases = [
    [{running: true, activityState: 'waiting', activityPending: [{action: 'confirm'}]}, '待确认'],
    [{running: true, activityState: 'waiting', activityPending: [{action: 'select'}]}, '待选择'],
    [{running: true, activityState: 'waiting', activityPending: [{action: 'prompt'}]}, '待填写'],
    [{running: true, activityState: 'waiting', activityPending: [{action: 'questionnaire'}]}, '待填写问卷'],
    [{activityPending: [{action: 'confirm'}]}, '待确认'],
    [{running: true, activityState: 'running', activityResult: {status: 'failed'}}, '运行中'],
    [{activityUnread: true, activityReadVersion: 0}, '完成待查看'],
    [{activityState: 'failed', activityUnread: true, activityReadVersion: 0}, '失败待查看'],
    [{activityState: 'failed'}, '失败'],
    [{activityState: 'error', readWhileSelected: true}, '失败'],
    [{activityState: 'partial'}, '部分完成'],
    [{activityState: 'interrupted'}, '已中断'],
    [{activityState: 'cancelled'}, '已取消'],
    [{}, '3 分钟前'],
  ];
  for (const [state, label] of cases) {
    const item = row('one', Date.now() - 180000, state);
    const folder = folderHarness(t, reactive({items: [], recentItems: [item], activeConversationUuid: '', readVersions: new Map(), busy: false}));
    const view = await folder.render();
    const statuses = view.nodes.filter(node => hasClass(node, 'activity-row-status'));
    assert.deepEqual(statuses.map(node => node.children), [label, label], JSON.stringify(state));
    assert.ok(statuses.every(node => node.props.title === undefined));
    if (label !== '3 分钟前') assert.ok(statuses.every(node => node.props['aria-label'] === label));
  }
});

test('marking a failed recent read keeps failure, while newer live and completed states replace the retained snapshot', async t => {
  const failed = row('failed', Date.now() - 180000, {activityState: 'failed', activityReadVersion: 0, activityUnread: true});
  const props = reactive({items: [failed], recentItems: [failed], activeConversationUuid: 'failed', readVersions: new Map(), busy: false});
  const folder = folderHarness(t, props);
  const label = async () => (await folder.render()).nodes.find(node => hasClass(node, 'activity-row-status-desktop')).children;
  assert.equal(await label(), '失败待查看');
  props.readVersions = new Map([['failed', 1]]);
  props.items = []; props.recentItems = [{...failed, activityUnread: false, activityReadVersion: 1}]; await nextTick();
  assert.equal(await label(), '失败');
  assert.equal((await folder.render()).nodes.filter(node => hasClass(node, 'activity-row')).length, 1);
  props.recentItems = [{...props.recentItems[0], running: true, activityState: 'running'}]; await nextTick();
  assert.equal(await label(), '运行中');
  props.recentItems = [{...props.recentItems[0], running: false, activityState: 'completed', activityResult: {status: 'completed'}, activityUnread: true, activityVersion: 2}]; await nextTick();
  assert.equal(await label(), '完成待查看');
  props.recentItems = [{...props.recentItems[0], activityUnread: false, activityReadVersion: 2}]; await nextTick();
  assert.equal(await label(), '3 分钟前');
});

test('read receipts clear badges without removing recent aliases or changing their order', () => {
  const unread = row('done', 2000, {activityReadVersion: 0, activityUnread: true});
  const status = {items: [], activityItems: [unread], recentItems: [unread, row('old', 1000)]};
  const adjusted = applyActivityReadVersions(status, new Map([['done', 1]]));
  assert.equal(adjusted.activityItems.length, 0);
  assert.deepEqual(adjusted.recentItems.map(item => item.conversationUuid), ['done', 'old']);
  assert.equal(adjusted.recentItems[0].activityUnread, false);
  assert.equal(status.recentItems[0].activityUnread, true);
});

test('actual tree props carry recent aliases; opening and deleting reuse existing navigation without moving folders', async t => {
  const h = treeHarness(t), item = row('recent', Date.now() - 180000);
  h.tree.applyStatus({items: [], activityItems: [], recentItems: [item]});
  const bindings = await h.folderBindings();
  assert.equal(bindings.recentItems[0].conversationUuid, 'recent');
  assert.ok(h.tree.knownConversationRows().some(item => item.conversationUuid === 'recent'));
  const folder = folderHarness(t, reactive({items: [], recentItems: bindings.recentItems, activeConversationUuid: '', readVersions: new Map(), busy: false}), (type, ...args) => {if (type === 'open') bindings.onOpen(...args);});
  const view = await folder.render();
  assert.match(view.html, /3 分钟前/); assert.match(view.html, /最后交互/);
  h.tree.selectedFolderId.value = 'do-not-change';
  walk([recentGroup(view)]).find(node => hasClass(node, 'activity-row-open')).props.onClick();
  assert.equal(h.emitted.find(event => event[0] === 'open')[1].conversationUuid, 'recent');
  assert.equal(h.tree.selectedFolderId.value, 'do-not-change'); assert.equal(h.tree.expanded.value.size, 0);
  h.tree.forgetConversation('recent');
  assert.equal(h.tree.recentItems.value.length, 0);
  assert.equal(h.tree.knownConversationRows().some(item => item.conversationUuid === 'recent'), false);
});

test('compiled activity/recent row pointer events open the same desktop overview and ignore touch; scroll/collapse close it', async t => {
  const h = treeHarness(t), running = row('working', 3000, {running: true}), recent = row('recent', 2000);
  h.tree.applyStatus({items: [running], activityItems: [running], recentItems: [recent]});
  const bindings = await h.folderBindings();
  const handlers = {'overview-enter': bindings.onOverviewEnter, 'overview-leave': bindings.onOverviewLeave, 'overview-close': bindings.onOverviewClose};
  const folder = folderHarness(t, reactive({items: bindings.items, recentItems: bindings.recentItems, activeConversationUuid: '', readVersions: new Map(), busy: false}), (type, ...args) => handlers[type]?.(...args));
  const view = await folder.render();
  const rows = view.nodes.filter(node => hasClass(node, 'activity-row'));
  const titleButton = {isConnected: true, getBoundingClientRect: () => ({left: 20, right: 230})};
  const anchor = {isConnected: true, getBoundingClientRect: () => ({left: 20, right: 320}),
    querySelector(selector) {return selector.includes('activity-row-open') ? titleButton : null;}};
  const event = {pointerType: 'mouse', currentTarget: anchor};
  for (const node of rows) {
    node.props.onPointerenter(event); h.tick(279); assert.equal(h.tree.overview.value.open, false);
    h.tick(1); assert.equal(h.tree.overview.value.row.conversationUuid, node.props['data-activity-id']);
    assert.equal(toRaw(h.tree.overview.value.anchor), anchor);
    assert.equal(h.tree.overview.value.anchor.getBoundingClientRect().right, 320, 'overview starts outside the complete row, not inside the trailing status/read controls');
    node.props.onPointerleave(); h.tick(100); h.tree.keepOverview(); h.tick(100); assert.equal(h.tree.overview.value.open, true);
    view.nodes.find(node => hasClass(node, 'activity-folder-content')).props.onScrollPassive();
    assert.equal(h.tree.overview.value.open, false);
    node.props.onPointerenter({...event, pointerType: 'touch'}); h.tick(300); assert.equal(h.tree.overview.value.open, false);
  }
  rows[0].props.onPointerenter(event); h.tick(280);
  view.nodes.find(node => hasClass(node, 'activity-folder-toggle')).props.onClick();
  assert.equal(h.tree.overview.value.open, false);
});

test('right-clicking a recent row opens the ordinary conversation menu at the pointer', async t => {
  const h = treeHarness(t), item = row('recent-menu', 2000);
  h.tree.applyStatus({items: [], recentItems: [item]});
  const bindings = await h.folderBindings();
  const folder = folderHarness(t, reactive({items: [], recentItems: bindings.recentItems, activeConversationUuid: '', readVersions: new Map(), busy: false}),
    (type, ...args) => {if (type === 'more') bindings.onMore(...args);});
  const view = await folder.render();
  const recentRow = groupRows(recentGroup(view))[0];
  let prevented = 0, stopped = 0;
  recentRow.props.onContextmenu({
    type: 'contextmenu', clientX: 123, clientY: 77,
    preventDefault() {prevented++;}, stopPropagation() {stopped++;},
  });
  await nextTick();
  assert.ok(prevented >= 1); assert.ok(stopped >= 1);
  assert.equal(h.tree.menu.value.open, true);
  assert.equal(h.tree.menu.value.row.conversationUuid, 'recent-menu');
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

test('local draft uses normal folder/pin/manual ordering without mutating loaded rows or duplicating after send', t => {
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
  assert.deepEqual(Array.from(merged, rowId), ['child', 'pinned', 'manual', 'local:new', 'older']);
  assert.equal(JSON.stringify(rows), snapshot);
  assert.equal(h.tree.withDraft(rows, 'other').length, rows.length);
  assert.equal(h.tree.withDraft(merged, 'F').length, merged.length);
  const persisted = merged.map(item => item.local ? {...item, local: false, id: 'sent', conversationUuid: 'sent'} : item).sort(compareTreeItems);
  h.props.draftConversation = null;
  assert.deepEqual(Array.from(h.tree.withDraft(persisted, 'F'), rowId), ['child', 'pinned', 'manual', 'sent', 'older']);
});

test('relative interaction labels update locally and their clock is disposed with the component', t => {
  const folder = folderHarness(t, reactive({items: [], recentItems: [], activeConversationUuid: '', readVersions: new Map()}));
  folder.mounted.forEach(fn => fn()); assert.equal(folder.intervals.size, 1);
  assert.equal(folder.run('recentInteractionTime(1000, 301000)'), '5 分钟前');
  assert.equal(folder.run('recentInteractionTime(1000, 7201000)'), '2 小时前');
  assert.equal(folder.run('recentInteractionTime(1000, 172801000)'), '2 天前');
  folder.unmounted.forEach(fn => fn()); assert.equal(folder.intervals.size, 0);
});
