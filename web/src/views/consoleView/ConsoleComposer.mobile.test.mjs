import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { compile, createSSRApp, h, nextTick, reactive, ref, watch } from 'vue';
import { renderToString } from 'vue/server-renderer';
import { parse } from '@vue/compiler-sfc';
import { baseParse } from '@vue/compiler-dom';
const source = fs.readFileSync(new URL('./ConsoleComposer.vue', import.meta.url), 'utf8');
const editor = fs.readFileSync(new URL('../../references/ReferenceEditor.vue', import.meta.url), 'utf8');
const between = (source, start, end) => { const a = source.indexOf(start), b = source.indexOf(end, a + start.length); assert.ok(a >= 0 && b > a); return source.slice(a, b); };
const walk = nodes => (nodes || []).flatMap(node => [node, ...walk(Array.isArray(node.children) ? node.children : [])]);
const ast = walk(baseParse(parse(source).descriptor.template.content).children);
const attachmentTemplate = ast.find(node => node.type === 1 && node.props.some(prop => prop.name === 'class' && prop.value?.content === 'attachment-strip'));
const editorTemplate = ast.find(node => node.type === 1 && node.tag === 'ReferenceEditor');
const sendTemplate = ast.find(node => node.type === 1 && node.props.some(prop => prop.name === 'class' && prop.value?.content === 'send-button'));
async function render(template, bindings) {
  let tree; const slotTrees = [];
  const compiled = compile(template.loc.source), app = createSSRApp({ render() { tree = compiled.call(this, bindings, []); return tree; } });
  for (const name of ['Close', 'Document', 'Promotion']) app.component(name, { render: () => h('svg') });
  app.component('ElImage', { render: () => h('img') });
  app.component('ElTooltip', { inheritAttrs: false, render() { const nodes = this.$slots.default?.() || []; slotTrees.push(...nodes); return nodes; } });
  app.component('ReferenceEditor', { render: () => h('div') });
  const html = await renderToString(app); return { html, nodes: walk([tree, ...slotTrees]) };
}

test('actual attachment remove button has a filename, stops preview bubbling and emits only the selected attachment', async () => {
  const calls = [], props = { pendingAttachments: [{ id: 'one', file: { name: 'photo.png', size: 123, type: 'image/png' } }, { id: 'two', file: { name: 'notes.txt', size: 24, type: 'text/plain' } }] };
  const { html, nodes } = await render(attachmentTemplate, { props, emit: (...args) => calls.push(args), attachmentPreviewUrl: () => '', fmtBytes: value => `${value} B` });
  assert.match(html, /移除附件：photo.png/); assert.match(html, /移除附件：notes.txt/);
  const buttons = nodes.filter(node => node.type === 'button'); let stopped = 0;
  buttons[1].props.onClick({ stopPropagation() { stopped++; } });
  assert.equal(stopped, 1); assert.deepEqual(calls, [['remove-attachment', 'two']]); assert.equal(props.pendingAttachments.length, 2, 'the composer still delegates ownership to ConsoleView');
});

test('actual send entry retains disabled state and ReferenceEditor canSend guard, without altering input content', async () => {
  const calls = [], props = { canSend: false, draft: '未提交引用和输入', conversationUuid: 'A' };
  const bindings = { props, emit: (...args) => calls.push(args), composerTextarea: null, onPaste() {}, onComposerKeydownCapture() {}, scheduleRunConfigPosition() {} };
  let rendered = await render(sendTemplate, bindings); assert.equal(rendered.nodes.find(node => node.type === 'button').props.disabled, true);
  rendered = await render(editorTemplate, bindings); rendered.nodes[0].props.onSend(); assert.deepEqual(calls, []);
  props.canSend = true; rendered.nodes[0].props.onSend(); assert.deepEqual(calls, [['send']]); assert.equal(props.draft, '未提交引用和输入');
  rendered = await render(sendTemplate, bindings); assert.equal(rendered.nodes[0].props.disabled, false);
});

test('actual composer paste and file chooser retain files, draft text and focus semantics', async () => {
  const emitted = [], inserts = [], focuses = [], props = { draft: 'draft', running: false, pendingAttachments: [], attachmentPreviews: {} };
  let picked = 0;
  const c = vm.createContext({ props, nextTick, File, emit: (...args) => emitted.push(args), fileInput: ref({ click() { picked++; } }), composerTextarea: ref({ insertText: text => inserts.push(text), adjustHeight() {}, focus: options => focuses.push(options), value: 'draft', setSelectionRange() {} }) });
  vm.runInContext(between(source, 'function openFilePicker()', 'function interactionAction('), c);
  vm.runInContext('openFilePicker()', c); assert.equal(picked, 1);
  const file = new File(['image'], 'image.png', { type: 'image/png' }); let prevented = 0;
  c.event = { clipboardData: { items: [{ kind: 'file', getAsFile: () => file }], getData: () => 'pasted text' }, preventDefault() { prevented++; } };
  vm.runInContext('onPaste(event)', c); assert.equal(prevented, 1); assert.equal(emitted[0][0], 'attachment-change'); assert.equal(emitted[0][1][0], file); assert.deepEqual(inserts, ['pasted text']);
  props.running = true; vm.runInContext('onPaste(event)', c); assert.equal(emitted.length, 1, 'running still prohibits new attachment paste');
  await vm.runInContext('focus()', c); assert.equal(focuses[0].preventScroll, true);
  const input = { files: [file], value: 'old-file' }; c.change = { target: input }; vm.runInContext('onAttachmentChange(change)', c); assert.equal(input.value, '');
});

test('touch keyboard Enter inserts a hard break before ReferenceEditor sends; desktop, modifiers, IME and suggestions keep their handlers', async () => {
  const breaks = [], calls = [], props = { canSend: true, draft: 'draft', conversationUuid: 'A' };
  let touch = true, suggestion = false;
  const composerTextarea = ref({ editor: { view: { composing: false }, commands: { insertContent: node => breaks.push(node) } } });
  const c = vm.createContext({ composerTextarea, window: { matchMedia: () => ({ matches: touch }) }, document: { querySelector: () => suggestion ? {} : null } });
  vm.runInContext(between(source, 'function onComposerKeydownCapture(', 'function scheduleRunConfigPosition('), c);
  const { nodes } = await render(editorTemplate, { props, composerTextarea, emit: (...args) => calls.push(args), onPaste() {}, onComposerKeydownCapture: c.onComposerKeydownCapture, scheduleRunConfigPosition() {} });
  const node = nodes[0];
  assert.equal(typeof node.props.onKeydownCapture, 'function');
  const key = (overrides = {}) => {
    const event = { key: 'Enter', target: { closest: selector => selector === '.reference-editor-content' ? {} : null }, preventDefault() { this.prevented = true; }, stopPropagation() { this.stopped = true; }, ...overrides };
    node.props.onKeydownCapture(event); return event;
  };
  let event = key(); assert.equal(event.prevented, true); assert.equal(event.stopped, true);
  assert.equal(breaks[0].type, 'hardBreak'); assert.deepEqual(calls, []);
  for (const variant of [{ shiftKey: true }, { ctrlKey: true }, { metaKey: true }, { altKey: true }, { isComposing: true }, { keyCode: 229 }, { key: 'Tab' }, { target: { closest: () => null } }]) {
    event = key(variant); assert.equal(event.prevented, undefined); assert.equal(event.stopped, undefined);
  }
  composerTextarea.value.editor.view.composing = true; assert.equal(key().prevented, undefined);
  composerTextarea.value.editor.view.composing = false;
  suggestion = true; assert.equal(key().prevented, undefined);
  suggestion = false; touch = false; assert.equal(key().prevented, undefined);
  assert.equal(breaks.length, 1, 'only plain mobile Enter inserts a break');
  nodes[0].props.onSend(); assert.deepEqual(calls, [['send']], 'explicit send is still available on phones');
});

test('open run-config popover follows the composer through keyboard blur and visual viewport changes, then detaches', async () => {
  const popover = ast.find(node => node.type === 1 && node.tag === 'el-popover');
  assert.ok(popover.props.some(prop => prop.name === 'ref' && prop.value?.content === 'runConfigPopover'));
  const editorNode = ast.find(node => node.type === 1 && node.tag === 'ReferenceEditor');
  assert.ok(editorNode.props.some(prop => prop.name === 'on' && prop.arg?.content === 'focusout'));
  const createTarget = () => {
    const listeners = new Map();
    return { listeners, addEventListener(name, listener) { listeners.set(name, listener); }, removeEventListener(name, listener) { if (listeners.get(name) === listener) listeners.delete(name); }, fire(name) { listeners.get(name)?.(); } };
  };
  const win = createTarget(), viewport = createTarget(), pending = new Map();
  let frameId = 0, anchorY = 80;
  win.visualViewport = viewport;
  win.requestAnimationFrame = callback => { pending.set(++frameId, callback); return frameId; };
  win.cancelAnimationFrame = id => pending.delete(id);
  const flushFrame = () => { const frames = [...pending.values()]; pending.clear(); frames.forEach(callback => callback()); };
  const positions = [], props = reactive({modelMenuOpen: false});
  const runConfigPopover = ref({ popperRef: { popperInstanceRef: { update: () => positions.push(anchorY) } } });
  let stopWatch;
  const c = vm.createContext({ props, runConfigPopover, window: win, nextTick, watch: (...args) => { stopWatch = watch(...args); } });
  vm.runInContext('let runConfigPositionFrame = 0;\n' + between(source, 'function scheduleRunConfigPosition()', 'onMounted(() => {'), c);
  props.modelMenuOpen = true; await nextTick(); await nextTick(); flushFrame();
  assert.deepEqual(positions, [80]);
  anchorY = 320; c.scheduleRunConfigPosition(); viewport.fire('resize'); flushFrame();
  assert.deepEqual(positions, [80, 320], 'blur and viewport resize coalesce into one reposition at the new anchor');
  anchorY = 360; viewport.fire('scroll'); flushFrame(); assert.deepEqual(positions, [80, 320, 360]);
  anchorY = 400; win.fire('resize'); flushFrame(); assert.deepEqual(positions, [80, 320, 360, 400]);
  viewport.fire('resize'); props.modelMenuOpen = false; await nextTick(); flushFrame();
  assert.deepEqual(positions, [80, 320, 360, 400], 'closed popup does not update');
  assert.equal(viewport.listeners.size, 0); assert.equal(win.listeners.size, 0);
  stopWatch();
});

test('actual ReferenceEditor IME flags, candidate selection, Enter and Shift+Enter behavior remain unchanged', () => {
  const emitted = [], inserted = [], chosen = [], focus = [];
  const c = vm.createContext({ emit: (...args) => emitted.push(args), editor: ref({ commands: { insertContent: content => inserted.push(content), focus: (...args) => focus.push(args) } }), mention: ref(null), picker: ref({ key: event => { if (['Enter','Tab'].includes(event.key)) chosen.push('choose'); else if (event.key === 'ArrowDown') chosen.push(1); else return false; event.preventDefault(); return true; } }), closeMention() {} });
  vm.runInContext(between(editor, 'function onKey(', 'function onPaste(') + between(editor, 'function focus()', 'function adjustHeight('), c);
  const key = (event = {}, view = {}) => { c.event = { key: 'Enter', preventDefault() { this.prevented = true; }, ...event }; c.view = view; return vm.runInContext('onKey(view,event)', c); };
  for (const [event, view] of [[{ isComposing: true }, {}], [{ keyCode: 229 }, {}], [{}, { composing: true }]]) {
    assert.equal(key(event, view), false); assert.equal(c.event.prevented, undefined);
  }
  assert.deepEqual(emitted, []); assert.equal(key({ shiftKey: true }), true); assert.equal(inserted[0].type, 'hardBreak'); assert.deepEqual(emitted, []);
  assert.equal(key(), true); assert.deepEqual(emitted, [['send']]);
  c.mention.value = { query: 'doc' }; key(); key({ key: 'Tab' }); key({ key: 'ArrowDown' }); assert.deepEqual(chosen, ['choose', 'choose', 1]); assert.equal(emitted.length, 1);
  vm.runInContext('focus()', c); assert.equal(focus[0][0], 'end'); assert.equal(focus[0][1].scrollIntoView, false);
});
