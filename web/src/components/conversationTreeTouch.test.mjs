import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { compile, createSSRApp, h, nextTick, proxyRefs, ref } from 'vue';
import { renderToString } from 'vue/server-renderer';
import { parse } from '@vue/compiler-sfc';
import { baseParse } from '@vue/compiler-dom';
import postcss from 'postcss';
import { treeItemId as rowId } from './conversationTreeInteractions.js';
import { MOBILE_VIEWPORT_QUERY } from '../mobileViewport.js';
import { isActivityWaiting } from '../conversationActivity.js';
import { REFERENCE_MIME, referenceToken } from '../references/codec.js';
import * as conversationGroupsTree from './conversationGroupsTree.js';
const source = fs.readFileSync(new URL('./ConversationTree.vue', import.meta.url), 'utf8');
const between = (start, end) => { const a = source.indexOf(start), b = source.indexOf(end, a + start.length); assert.ok(a >= 0 && b > a); return source.slice(a, b); };
const walk = nodes => (nodes || []).flatMap(node => [node, ...walk(Array.isArray(node.children) ? node.children : [])]);
const ast = walk(baseParse(parse(source).descriptor.template.content).children);
const rowTemplate = ast.find(node => node.type === 1 && node.props.some(prop => prop.name === 'class' && prop.value?.content === 'tree-row-wrap'));
const menuTemplate = ast.find(node => node.type === 1 && node.props.some(prop => prop.name === 'data-conversation-tree-menu'));
const shieldTemplate = ast.find(node => node.type === 1 && node.props.some(prop => prop.name === 'class' && prop.value?.content === 'tree-menu-shield'));
const renderRow = compile(rowTemplate.loc.source), renderMenu = compile(menuTemplate.loc.source), renderShield = compile(shieldTemplate.loc.source);
const saved = { kind: 'conversation', id: 'A', conversationUuid: 'A', title: '会话A', folderId: 'F' };
const event = (extra = {}) => ({ prevented: 0, stopped: 0, preventDefault() { this.prevented++; }, stopPropagation() { this.stopped++; }, currentTarget: { getBoundingClientRect: () => ({ right: 280, top: 100, bottom: 144 }) }, ...extra });
function harness({ mobile = true, row = saved } = {}) {
  const opened = [], moved = [], emitted = [];
  const ctx = vm.createContext({...conversationGroupsTree, ref, nextTick, rowId, MOBILE_VIEWPORT_QUERY, REFERENCE_MIME, referenceToken, isActivityWaiting,
    ...Object.fromEntries(['Loading', 'MagicStick', 'ArrowDown', 'ArrowRight', 'Box', 'ChatLineRound', 'FolderOpened', 'Folder', 'Star', 'StarFilled', 'RefreshLeft'].map(name => [name, 'svg'])),
    menu: ref({ open: false, x: 0, y: 0, row: null }), drag: ref({ row: null, target: null, zone: '' }),
    isDirectoryView: ref(true), locatingConversation: ref(false), displayRows: ref([row]), moveInFlight: ref(false), query: ref(''), activeConversationRow: ref(null), selectedFolderId: ref(''),
    moveMode: ref(''), moveRow: ref(null), moveFolderId: ref(''), moveFolderQuery: ref(''), moveUnarchive: ref(false), moveUpdateSnapshots: ref(false), moveDialog: ref(false),
    window: { innerWidth: 1200, innerHeight: 900, matchMedia: () => ({ matches: mobile }), visualViewport: { width: 390, height: 430, offsetLeft: 0, offsetTop: 35 } },
    document: { querySelector: () => ({ getBoundingClientRect: () => ({ width: 194, height: 330 }) }) },
    closeOverview() {}, enterOverview() {}, leaveOverview() {}, running: row => Boolean(row?.running), activityLabel: () => '', rowLoading: () => false,
    liveConversationTitle: row => row.title || '新会话', conversationOrigin: () => ({icon: 'svg'}), hasConversationMessages: () => true, isTitleGenerating: () => false,
    nodePath: row => row.path || '目录 F', recentLabel: () => '刚刚', rowLabel: row => row.title || row.name, indentation: () => '0px', isExpanded: () => false, toggleRow() {},
    activateRow: row => opened.push(row.id), locateAndOpen: row => opened.push(row.id), dragOver() {}, drop() {}, clearDrag() {},
    rootDropTarget: {kind: 'root', id: '__root'},
    selecting: ref(false), isSelected: () => false, selectableRow: row => row?.kind === 'conversation' && !row.local, toggleSelected() {}, exitSelection() {}, enterSelection() {},
    clickConversation: (_event, row) => row.search ? ctx.locateAndOpen(row) : ctx.activateRow(row),
    selectFolder() {}, emit: (...args) => emitted.push(args), loadAllFolders: async () => moved.push('folders'),
    ElMessage: { error(error) { assert.fail(String(error)); } }, apiError: String,
  });
  vm.runInContext(between('async function openMenu(', 'async function promptFolder(') + between('async function showMove(', 'async function submitMove(')
    + between('async function runMenuAction(', 'function dropIntent(') + between('function rowKeydown(', 'function globalKeydown('), ctx);
  const bindings = proxyRefs(ctx);
  return { ctx, opened, moved, emitted, run: code => vm.runInContext(code, ctx), async render(menu = false) {
    let tree;
    const app = createSSRApp({ render() { tree = (menu === 'shield' ? renderShield : menu ? renderMenu : renderRow).call(this, bindings, []); return tree; } });
    app.component('ElIcon', { render() { return h('i', this.$slots.default?.()); } });
    app.component('AnimatedConversationTitle', { props: ['text'], render() { return h('span', this.text); } });
    for (const icon of ['Aim', 'Finished', 'MoreFilled', 'StarFilled', 'FolderOpened', 'EditPen', 'DocumentCopy', 'Refresh', 'Box', 'Delete', 'Star', 'Check', 'InfoFilled', 'FolderAdd', 'ChatLineRound', 'MagicStick']) app.component(icon, { render: () => h('svg') });
    const html = await renderToString(app);
    return { html, nodes: walk([tree]) };
  } };
}
const classHas = (node, name) => String(node.props?.class || '').split(/\s+/).includes(name);

test('compiled touch More is a sibling button: click/Enter/pointer/drag never activate the conversation; same menu moves the same row', async () => {
  const h = harness(), { nodes, html } = await h.render();
  const more = nodes.find(node => classHas(node, 'tree-touch-more'));
  assert.ok(more); assert.match(html, /会话A：更多操作/); assert.equal(more.props['aria-haspopup'], 'menu');
  const key = event({ key: 'Enter' }); more.props.onKeydown(key); assert.equal(key.stopped, 1); assert.deepEqual(h.opened, []);
  for (const key of ['Escape', 'F10', 'n']) { const shortcut = event({ key, ctrlKey: key === 'n' }); more.props.onKeydown(shortcut); assert.equal(shortcut.stopped, 0, 'application/menu shortcuts must still bubble'); }
  const pointer = event({ pointerType: 'touch' }); more.props.onPointerdown(pointer); assert.equal(pointer.stopped, 1);
  const drag = event(); more.props.onDragstart(drag); assert.equal(drag.prevented, 1); assert.equal(h.ctx.drag.value.row, null);
  const click = event(); await more.props.onClick(click); assert.ok(click.stopped); assert.deepEqual(h.opened, []);
  assert.equal(h.ctx.menu.value.row.conversationUuid, 'A'); assert.equal(h.ctx.menu.value.x, 188); assert.equal(h.ctx.menu.value.y, 127);
  const rendered = await h.render(true); const move = rendered.nodes.find(node => node.type === 'button' && node.props.onClick?.toString().includes("runMenuAction('move')"));
  assert.ok(move); await move.props.onClick(); assert.equal(h.ctx.menu.value.open, false);
  assert.equal(h.ctx.moveDialog.value, true); assert.equal(h.ctx.moveRow.value.conversationUuid, 'A'); assert.equal(h.ctx.moveFolderId.value, 'F'); assert.deepEqual(h.moved, ['folders']);
});

for (const recentAlias of [false, true]) test(`${recentAlias ? 'recent' : 'directory'} More toggles on the same trigger without opening the chat`, async () => {
  const row = {...saved, recentAlias}, h = harness({row});
  const anchor = event().currentTarget;
  const click = () => event({type: 'click', pointerType: 'touch', currentTarget: anchor});
  const more = () => h.render().then(view => view.nodes.find(node => classHas(node, 'tree-touch-more')));
  await (await more()).props.onClick(click());
  assert.equal(h.ctx.menu.value.open, true); assert.equal((await more()).props['aria-expanded'], true);
  const second = click(); await (await more()).props.onClick(second);
  assert.equal(h.ctx.menu.value.open, false); assert.equal((await more()).props['aria-expanded'], false); assert.ok(second.prevented); assert.ok(second.stopped);
  await (await more()).props.onClick(click()); assert.equal(h.ctx.menu.value.open, true);
  // Native keyboard activation reaches the same button click path.
  await (await more()).props.onClick(event({type:'click', detail:0, currentTarget:anchor}));
  assert.equal(h.ctx.menu.value.open, false); assert.deepEqual(h.opened, []);
});

test('More on a different row retargets instead of closing the existing menu', async () => {
  const h = harness(); await h.ctx.openMoreMenu(event(), saved);
  const other = {...saved, id:'B', conversationUuid:'B'};
  await h.ctx.openMoreMenu(event(), other);
  assert.equal(h.ctx.menu.value.open, true); assert.equal(h.ctx.menu.value.row.conversationUuid, 'B'); assert.deepEqual(h.opened, []);
});

test('outside touch keeps the shield through pointerdown and consumes the closing click, rather than reopening the trigger', async () => {
  const h = harness(), anchor = event().currentTarget;
  const more = (await h.render()).nodes.find(node => classHas(node, 'tree-touch-more'));
  await more.props.onClick(event({currentTarget:anchor}));
  const shield = (await h.render('shield')).nodes.find(node => classHas(node, 'tree-menu-shield'));
  const surface = {}, pointer = event({type:'pointerdown',pointerType:'touch',target:surface,currentTarget:surface});
  shield.props.onPointerdown(pointer); await nextTick();
  assert.equal(h.ctx.menu.value.open, true, 'shield must still own the subsequent pointerup/click'); assert.ok(pointer.stopped);
  const inside = event({target:{},currentTarget:surface});shield.props.onClick(inside);assert.equal(h.ctx.menu.value.open,true);
  const click = event({type:'click',target:surface,currentTarget:surface});shield.props.onClick(click);await nextTick();
  assert.equal(h.ctx.menu.value.open,false);assert.ok(click.prevented);assert.ok(click.stopped);assert.deepEqual(h.opened,[]);
  await more.props.onClick(event({currentTarget:anchor}));assert.equal(h.ctx.menu.value.open,true,'the next deliberate tap can open it again');
});

for (const recentAlias of [false,true]) test(`${recentAlias ? 'recent' : 'directory'} active touch row includes both title and More in its selected container`, async () => {
  const h = harness({row:{...saved,recentAlias}});h.ctx.activeConversationRow.value=h.ctx.displayRows.value[0];
  const view=await h.render(), row=view.nodes.find(node=>classHas(node,'tree-row-wrap'));
  assert.ok(classHas(row,'is-chat-active-row'));
  const children=walk([row]);assert.ok(children.some(node=>classHas(node,'conversation')));assert.ok(children.some(node=>classHas(node,'tree-touch-more')));
  const main=children.find(node=>classHas(node,'conversation'));assert.ok(!walk([main]).some(node=>classHas(node,'tree-touch-more')),'buttons remain siblings, not nested');
  h.ctx.activeConversationRow.value=null;assert.ok(!(await h.render()).nodes.some(node=>classHas(node,'is-chat-active-row')));
});

test('full-row selection applies only to touch layout and does not paint a second title-only background', () => {
  const css=postcss.parse(parse(source).descriptor.styles[0].content);
  const selector='.tree-row-wrap.is-chat-active-row';let found=0;
  css.walkRules(selector, rule=>{
    found++;assert.equal(rule.parent.name,'media');assert.equal(rule.parent.params,'(max-width:760px), (hover:none) and (pointer:coarse)');
    assert.ok(rule.nodes.some(d=>d.prop==='background'&&d.value==='var(--ob-chat-selected)'));
  });assert.equal(found,1);
  css.walkRules(selector+' .tree-node-main', rule=>assert.ok(rule.nodes.some(d=>d.prop==='background'&&d.value==='transparent')));
});

test('compiled original desktop contextmenu, activation, keyboard menu and drag path remain available', async () => {
  const h = harness({ mobile: false }), { nodes } = await h.render();
  const main = nodes.find(node => classHas(node, 'conversation')), row = nodes.find(node => classHas(node, 'tree-row-wrap'));
  const context = event({ clientX: 1100, clientY: 850 }); await main.props.onContextmenu(context);
  assert.equal(context.prevented, 1); assert.equal(context.stopped, 1); assert.equal(h.ctx.menu.value.x, 998); assert.equal(h.ctx.menu.value.y, 562);
  assert.deepEqual(h.opened, []); main.props.onClick(); assert.deepEqual(h.opened, ['A']); assert.equal(row.props.draggable, true);
  const keyboard = event({ key: 'F10', shiftKey: true }); row.props.onKeydown(keyboard); await nextTick();
  assert.equal(h.ctx.menu.value.row.conversationUuid, 'A'); assert.ok(keyboard.prevented);
  const data = new Map(), drag = event({ dataTransfer: { setData: (key, value) => data.set(key, value) } });
  row.props.onDragstart(drag); assert.equal(data.get('application/x-openbear-tree'), 'conversation'); assert.equal(h.ctx.drag.value.row.conversationUuid, 'A');
  assert.equal(JSON.parse(data.get(REFERENCE_MIME)).id, 'A'); assert.equal(drag.dataTransfer.effectAllowed, 'copyMove');
});

for (const mobile of [false, true]) for (const folderId of ['F', '']) test(`${mobile ? 'touch' : 'desktop'} recent menu creates a sibling in ${folderId || 'temporary'} and groups locate with move`, async () => {
  const alias = {...saved, folderId, parentId: folderId, recentAlias: true};
  const h = harness({mobile, row: alias});
  h.ctx.selectedFolderId.value = 'unrelated-selected-folder';
  await h.ctx.openMoreMenu(event({type: mobile ? 'click' : 'contextmenu', clientX: 110, clientY: 90}), alias);
  assert.equal(h.ctx.menu.value.recent, true);
  assert.equal('recentAlias' in h.ctx.menu.value.row, false);
  const view = await h.render(true);
  assert.doesNotMatch(view.html, /会话概览/);
  assert.ok(view.html.indexOf('新建同级会话') < view.html.indexOf('生成会话名称'));
  assert.ok(view.html.indexOf('在目录中定位') > view.html.indexOf('置顶'));
  assert.ok(view.html.indexOf('在目录中定位') < view.html.indexOf('移动到…'));
  const create = view.nodes.find(node => node.type === 'button' && node.props.onClick?.toString().includes("runMenuAction('new-sibling')"));
  assert.ok(create); assert.ok(!create.props.disabled);
  await create.props.onClick();
  assert.equal(h.ctx.menu.value.open, false);
  const emitted = h.emitted.find(args => args[0] === 'new-conversation');
  assert.equal(emitted[1], folderId);
  assert.equal(emitted[2].revealInFolders, false);
  assert.deepEqual(h.opened, []); assert.deepEqual(h.moved, []);
});

for (const mobile of [false, true]) test(`${mobile ? 'touch' : 'desktop'} blank-area menu creates only a temporary conversation, even after a recent row menu`, async () => {
  const h = harness({mobile});
  h.ctx.selectedFolderId.value = 'previous-directory';
  await h.ctx.openMenu(event(), {...saved, recentAlias: true});
  await h.ctx.openRootMenu(event({type: 'contextmenu'}));
  assert.equal(h.ctx.menu.value.row.kind, 'root');
  assert.equal(h.ctx.menu.value.recent, false);
  const view = await h.render(true);
  assert.match(view.html, /新建临时会话/);
  assert.doesNotMatch(view.html, /新建同级会话|会话概览|在目录中定位/);
  const create = view.nodes.find(node => node.type === 'button' && node.props.onClick?.toString().includes("runMenuAction('new-conversation')"));
  await create.props.onClick();
  assert.deepEqual(h.emitted.find(args => args[0] === 'new-conversation'), ['new-conversation', '']);
  assert.equal(h.ctx.menu.value.open, false);
});

for (const mobile of [false, true]) for (const folderId of ['F', '']) test(`${mobile ? 'touch' : 'desktop'} directory menu creates a sibling in the clicked row's ${folderId || 'temporary'} location`, async () => {
  const h = harness({mobile});
  h.ctx.selectedFolderId.value = 'unrelated-selected-folder';
  await h.ctx.openMenu(event(), {...saved, recentAlias: true});
  await h.ctx.openMenu(event(), {...saved, folderId});
  assert.equal(h.ctx.menu.value.recent, false);
  const view = await h.render(true);
  assert.doesNotMatch(view.html, /会话概览|在目录中定位/);
  assert.ok(view.html.indexOf('新建同级会话') < view.html.indexOf('生成会话名称'));
  const create = view.nodes.find(node => node.type === 'button' && node.props.onClick?.toString().includes("runMenuAction('new-sibling')"));
  await create.props.onClick();
  assert.equal(h.ctx.menu.value.open, false);
  const emitted = h.emitted.find(args => args[0] === 'new-conversation');
  assert.equal(emitted[1], folderId);assert.equal(emitted[2].revealInFolders, true);
  assert.deepEqual(h.opened, []);
});

const menuShape = view => view.nodes.filter(node => node.type === 'button' || node.type === 'hr').map(node => node.type === 'hr'
  ? {label: 'separator'} : {label: walk([node]).find(child => child.type === 'span').children, disabled: Boolean(node.props.disabled)});
for (const mobile of [false, true]) for (const [state, extra] of Object.entries({saved:{}, running:{running:true}, local:{local:true}, archived:{archived:true}, unread:{activityUnread:true}, pinned:{pinned:true}})) {
  test(`${mobile ? 'touch' : 'desktop'} ${state} conversation menus share order, separators and disabled actions except recent-only locate`, async () => {
    const h = harness({mobile}), row = {...saved, ...extra};h.ctx.activityReadBusy = false;
    await h.ctx.openMenu(event(), {...row, recentAlias:true});const recent = menuShape(await h.render(true));
    await h.ctx.openMenu(event(), row);const directory = menuShape(await h.render(true));
    assert.deepEqual(directory, recent.filter(item => item.label !== '在目录中定位'));
    assert.deepEqual(directory.map(item => item.label), [
      '新建同级会话', ...(extra.activityUnread ? ['标为已读'] : []), '生成会话名称', '重命名', '复制会话', extra.pinned ? '取消置顶' : '置顶',
      'separator', '多选', '移动到…', '更新系统提示词…', extra.archived ? '取消归档' : '归档', '会话属性', 'separator', '删除会话',
    ]);
    const disabled = label => directory.find(item => item.label === label).disabled;
    assert.equal(disabled('会话属性'), false, 'properties can show the explicit local-draft persistence gate');
    assert.equal(disabled('重命名'), Boolean(extra.local));
    assert.equal(disabled('多选'), Boolean(extra.local));
    assert.equal(disabled('复制会话'), Boolean(extra.local || extra.running));
    assert.equal(disabled('更新系统提示词…'), Boolean(extra.local || extra.running));
    assert.equal(disabled('删除会话'), Boolean(extra.running));
  });
}

test('local draft keeps original disabled move/rename protections; no touch capability bypass', async () => {
  const h = harness({ row: { ...saved, id: 'local:new', conversationUuid: 'local:new', local: true } });
  const { nodes } = await h.render(); assert.equal(nodes.find(node => classHas(node, 'tree-row-wrap')).props.draggable, false);
  await nodes.find(node => classHas(node, 'tree-touch-more')).props.onClick(event());
  const menu = await h.render(true); const move = menu.nodes.find(node => node.type === 'button' && node.props.onClick?.toString().includes("runMenuAction('move')"));
  assert.equal(move.props.disabled, true); assert.deepEqual(h.opened, []);
});
