import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { compile, createSSRApp, h, nextTick, proxyRefs, ref } from 'vue';
import { renderToString } from 'vue/server-renderer';
import { parse } from '@vue/compiler-sfc';
import { baseParse } from '@vue/compiler-dom';
import { treeItemId as rowId } from './conversationTreeInteractions.js';
import { MOBILE_VIEWPORT_QUERY } from '../mobileViewport.js';
import { REFERENCE_MIME, referenceToken } from '../references/codec.js';
const source = fs.readFileSync(new URL('./ConversationTree.vue', import.meta.url), 'utf8');
const between = (start, end) => { const a = source.indexOf(start), b = source.indexOf(end, a + start.length); assert.ok(a >= 0 && b > a); return source.slice(a, b); };
const walk = nodes => (nodes || []).flatMap(node => [node, ...walk(Array.isArray(node.children) ? node.children : [])]);
const ast = walk(baseParse(parse(source).descriptor.template.content).children);
const rowTemplate = ast.find(node => node.type === 1 && node.props.some(prop => prop.name === 'class' && prop.value?.content === 'tree-row-wrap'));
const menuTemplate = ast.find(node => node.type === 1 && node.props.some(prop => prop.name === 'data-conversation-tree-menu'));
const renderRow = compile(rowTemplate.loc.source), renderMenu = compile(menuTemplate.loc.source);
const saved = { kind: 'conversation', id: 'A', conversationUuid: 'A', title: '会话A', folderId: 'F' };
const event = (extra = {}) => ({ prevented: 0, stopped: 0, preventDefault() { this.prevented++; }, stopPropagation() { this.stopped++; }, currentTarget: { getBoundingClientRect: () => ({ right: 280, top: 100, bottom: 144 }) }, ...extra });
function harness({ mobile = true, row = saved } = {}) {
  const opened = [], moved = [], emitted = [];
  const ctx = vm.createContext({ ref, nextTick, rowId, MOBILE_VIEWPORT_QUERY, REFERENCE_MIME, referenceToken,
    ...Object.fromEntries(['Loading', 'ArrowDown', 'ArrowRight', 'Box', 'ChatLineRound', 'FolderOpened', 'Folder', 'Star', 'StarFilled', 'RefreshLeft'].map(name => [name, 'svg'])),
    menu: ref({ open: false, x: 0, y: 0, row: null }), drag: ref({ row: null, target: null, zone: '' }),
    displayRows: ref([row]), moveInFlight: ref(false), query: ref(''), activeConversationUuid: ref(''), selectedFolderId: ref(''),
    moveMode: ref(''), moveRow: ref(null), moveFolderId: ref(''), moveFolderQuery: ref(''), moveUnarchive: ref(false), moveUpdateSnapshots: ref(false), moveDialog: ref(false),
    window: { innerWidth: 1200, innerHeight: 900, matchMedia: () => ({ matches: mobile }), visualViewport: { width: 390, height: 430, offsetLeft: 0, offsetTop: 35 } },
    document: { querySelector: () => ({ getBoundingClientRect: () => ({ width: 194, height: 330 }) }) },
    closeOverview() {}, enterOverview() {}, leaveOverview() {}, running: row => Boolean(row?.running), activityLabel: () => '', rowLoading: () => false,
    rowLabel: row => row.title || row.name, indentation: () => '0px', isExpanded: () => false, toggleRow() {},
    activateRow: row => opened.push(row.id), locateAndOpen: row => opened.push(row.id), dragOver() {}, drop() {}, clearDrag() {},
    selectFolder() {}, emit: (...args) => emitted.push(args), loadAllFolders: async () => moved.push('folders'),
    ElMessage: { error(error) { assert.fail(String(error)); } }, apiError: String,
  });
  vm.runInContext(between('async function openMenu(', 'async function promptFolder(') + between('async function showMove(', 'async function submitMove(')
    + between('async function runMenuAction(', 'function dropIntent(') + between('function rowKeydown(', 'function globalKeydown('), ctx);
  const bindings = proxyRefs(ctx);
  return { ctx, opened, moved, emitted, run: code => vm.runInContext(code, ctx), async render(menu = false) {
    let tree;
    const app = createSSRApp({ render() { tree = (menu ? renderMenu : renderRow).call(this, bindings, []); return tree; } });
    app.component('ElIcon', { render() { return h('i', this.$slots.default?.()); } });
    for (const icon of ['MoreFilled', 'StarFilled', 'FolderOpened', 'EditPen', 'DocumentCopy', 'Refresh', 'Box', 'Delete', 'Star', 'Check', 'InfoFilled', 'FolderAdd', 'ChatLineRound']) app.component(icon, { render: () => h('svg') });
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

test('local draft keeps original disabled move/rename protections; no touch capability bypass', async () => {
  const h = harness({ row: { ...saved, id: 'local:new', conversationUuid: 'local:new', local: true } });
  const { nodes } = await h.render(); assert.equal(nodes.find(node => classHas(node, 'tree-row-wrap')).props.draggable, false);
  await nodes.find(node => classHas(node, 'tree-touch-more')).props.onClick(event());
  const menu = await h.render(true); const move = menu.nodes.find(node => node.type === 'button' && node.props.onClick?.toString().includes("runMenuAction('move')"));
  assert.equal(move.props.disabled, true); assert.deepEqual(h.opened, []);
});
