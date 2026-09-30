import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { compile, createSSRApp, h, nextTick, reactive, ref, watch } from 'vue';
import { renderToString } from 'vue/server-renderer';
import { parse } from '@vue/compiler-sfc';
import { baseParse } from '@vue/compiler-dom';
import postcss from 'postcss';
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
  for (const name of ['Close', 'Document', 'Promotion', 'Layers', 'Coins', 'Clock3', 'ArrowDown']) app.component(name, { render: () => h('svg') });
  app.component('ElImage', { render: () => h('img') });
  app.component('ElTooltip', { inheritAttrs: false, render() { const nodes = this.$slots.default?.() || []; slotTrees.push(...nodes); return nodes; } });
  app.component('ReferenceEditor', { render: () => h('div') });
  app.component('ContextUsageMeter', { props: ['usage'], render() { return h('button', {class: 'context-usage-trigger'}, `${this.usage?.used ?? '待实测'}`); } });
  const html = await renderToString(app); return { html, nodes: walk([tree, ...slotTrees]) };
}

test('mobile footer keeps context, then icon-only Tokens/cache, cost and duration in order with accessible labels', async () => {
  const template = ast.find(node => node.type === 1 && node.props.some(prop => prop.name === 'class' && prop.value?.content === 'composer-usage-summary'));
  const props = {contextUsage:{used:125000},tokensText:'75.5M',cachePercentText:'96.0%',durationText:'12m34s',costText:'$3.45678'};
  for (const values of [{}, {durationText:'250m08s',costText:'$106.91340'}, {durationText:'—',costText:'—'}]) {
    Object.assign(props, values);
    const {html} = await render(template, {props});
    assert.ok(html.includes(`总耗时 ${props.durationText}`));
    assert.ok(html.includes(`总花费 ${props.costText}`));
    assert.match(html, /context-usage-trigger/);
    assert.match(html, /125000/);
    assert.match(html, /75\.5M（96\.0%）/);
    assert.match(html, /aria-label="总 Tokens 75\.5M；缓存命中率 96\.0%"/);
    assert.equal((html.match(/<svg/g)||[]).length,3);
    const visibleText=html.replace(/<[^>]*>/g,'');
    assert.doesNotMatch(visibleText,/Tokens|总耗时|总花费|压缩/);
    assert.ok(visibleText.indexOf('125000')<visibleText.indexOf('75.5M'));
    assert.ok(visibleText.indexOf('75.5M')<visibleText.indexOf(props.costText));
    if(props.costText!==props.durationText) assert.ok(visibleText.indexOf(props.costText)<visibleText.indexOf(props.durationText));
  }
});

test('model link retains compression strategy immediately after the model name for both strategies', async () => {
  const template=ast.find(node=>node.type===1 && node.tag==='button' && node.props.some(prop=>prop.name==='class' && prop.value?.content.includes('run-config-chip')));
  for(const [contextStrategy,label] of [['sliding_window','滑窗压缩'],['model_summary','摘要压缩']]) {
    const {html}=await render(template,{props:{contextStrategy},runConfigStrategyText:label,runConfigModelText:'GPT-6 Astra',runConfigMetaText:'',runConfigMetaParts:[],runConfigStatusLabel:'',runConfigThinkingBadge:''});
    assert.ok(html.indexOf('GPT-6 Astra')<html.indexOf(`>${label}</span>`));
  }
});

test('actual attachment remove button has a filename, stops preview bubbling and emits only the selected attachment', async () => {
  const calls = [], props = { uploadProgress: {}, pendingAttachments: [{ id: 'one', file: { name: 'photo.png', size: 123, type: 'image/png' } }, { id: 'two', file: { name: 'notes.txt', size: 24, type: 'text/plain' } }] };
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

test('actual composer paste and file chooser retain files, mixed text and focus semantics; rejected files warn', async () => {
  const emitted = [], inserts = [], focuses = [], warnings = [], props = { draft: 'draft', running: false, pendingAttachments: [], attachmentPreviews: {} };
  let picked = 0;
  const c = vm.createContext({ props, nextTick, File, ElMessage: {warning: text => warnings.push(text)}, emit: (...args) => emitted.push(args), fileInput: ref({ click() { picked++; } }), composerTextarea: ref({ insertText: text => inserts.push(text), adjustHeight() {}, focus: options => focuses.push(options), value: 'draft', setSelectionRange() {} }) });
  vm.runInContext(between(source, 'function openFilePicker()', 'function interactionAction('), c);
  vm.runInContext('openFilePicker()', c); assert.equal(picked, 1);
  const file = new File(['image'], 'image.png', { type: 'image/png' }); let prevented = 0;
  c.event = { clipboardData: { items: [{ kind: 'file', getAsFile: () => file }], getData: () => 'pasted text' }, preventDefault() { prevented++; } };
  vm.runInContext('onPaste(event)', c); assert.equal(prevented, 1); assert.equal(emitted[0][0], 'attachment-change'); assert.equal(emitted[0][1][0], file); assert.deepEqual(inserts, ['pasted text']);
  props.running = true; vm.runInContext('onPaste(event)', c);
  assert.equal(emitted.length, 1, 'running still prohibits new attachment paste');
  assert.deepEqual(inserts, ['pasted text', 'pasted text'], 'text survives a rejected mixed file/text paste');
  assert.deepEqual(warnings, ['运行中暂不能添加附件']);
  c.event = {clipboardData:{items:[{kind:'file',getAsFile:()=>file}],getData:()=>''},preventDefault(){this.prevented=true;}};
  vm.runInContext('onPaste(event)',c);
  assert.equal(c.event.prevented,true,'file-only paste is intercepted');
  assert.equal(emitted.length,1);assert.equal(inserts.length,2);
  assert.deepEqual(warnings,['运行中暂不能添加附件','运行中暂不能添加附件']);
  c.event = {clipboardData:{items:[],getData:()=> 'native text'},preventDefault(){this.prevented=true;}};
  vm.runInContext('onPaste(event)',c);assert.equal(c.event.prevented,undefined,'ordinary text retains native paste');
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
  const reference = vm.createContext({emit:(name)=>{if(name==='send'&&props.canSend)calls.push(['send']);},editor:ref(null),mention:ref(null),picker:ref(null)});
  vm.runInContext(between(editor,'function onKey(','function onPaste('),reference);
  for (const modifier of ['ctrlKey','metaKey']) {
    event = key({[modifier]:true});
    assert.equal(event.prevented,undefined,'hardware shortcut reaches ReferenceEditor');
    reference.event=event;reference.view={composing:false};
    assert.equal(vm.runInContext('onKey(view,event)',reference),true);
  }
  assert.deepEqual(calls,[['send'],['send']],'both hardware shortcuts send on a touch device');
  props.canSend=false;event=key({metaKey:true});reference.event=event;vm.runInContext('onKey(view,event)',reference);
  assert.equal(calls.length,2,'hardware shortcut still respects canSend');props.canSend=true;
  for(const composing of [{isComposing:true},{keyCode:229}]) {
    event=key({ctrlKey:true,...composing});reference.event=event;
    assert.equal(vm.runInContext('onKey(view,event)',reference),false);
  }
  assert.equal(calls.length,2,'IME confirmation cannot send via the shortcut');
  composerTextarea.value.editor.view.composing = true; assert.equal(key().prevented, undefined);
  composerTextarea.value.editor.view.composing = false;
  suggestion = true; assert.equal(key().prevented, undefined);
  suggestion = false; touch = false; assert.equal(key().prevented, undefined);
  assert.equal(breaks.length, 1, 'only plain mobile Enter inserts a break');
  touch=true;event=key();reference.event=event;
  assert.equal(event.prevented,true,'plain touch Enter stays a newline');
  touch=false;event=key();reference.event=event;
  assert.equal(vm.runInContext('onKey(view,event)',reference),true,'desktop Enter still sends');
  nodes[0].props.onSend(); assert.deepEqual(calls, [['send'], ['send'], ['send'], ['send']], 'explicit send is still available on phones');
});

test('mobile composer hides the bottom hints and leaves the shell owning safe-area padding', () => {
  assert.match(source, /@media \(max-width: 760px\), \(hover: none\) and \(pointer: coarse\) \{\s*\.composer-hints \{ display: none; \}/);
  assert.match(source, /class="composer-desktop-shortcut">Enter 发送 · Ctrl\/⌘\+Enter 也可发送/);
  assert.match(source, /\.composer-shell \{ padding: \.5rem \.75rem 8px; \}/);
  assert.doesNotMatch(source, /\.composer-shell \{ padding:[^}]*safe-area-inset-bottom/);
});

test('open run-config popover follows the composer through keyboard blur and visual viewport changes, then detaches', async () => {
  const popover = ast.find(node => node.type === 1 && node.tag === 'el-popover');
  assert.ok(popover.props.some(prop => prop.name === 'ref' && prop.value?.content === 'runConfigPopover'));
  assert.ok(popover.props.some(prop => prop.name === 'bind' && prop.arg?.content === 'popper-options' && prop.exp?.content === 'runConfigPopperOptions'));
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
  const runConfigPopperOptions = ref({});
  let mobile = true;
  win.matchMedia = () => ({matches: mobile});
  const c = vm.createContext({ props, runConfigPopover, runConfigPopperOptions, runConfigContent: ref(null), runConfigCompact: ref(false), runConfigSettingsOpen: ref(false), window: win, nextTick, watch: (...args) => { stopWatch = watch(...args); } });
  vm.runInContext('let runConfigPositionFrame = 0; let runConfigFullChromeHeight = 0;\n' + between(source, 'function syncRunConfigLayout(', 'onMounted(() => {'), c);
  props.modelMenuOpen = true; await nextTick(); await nextTick(); flushFrame(); await nextTick();
  assert.deepEqual(positions, [80]);
  assert.equal(runConfigPopperOptions.value.strategy, 'fixed');
  const overflow = runConfigPopperOptions.value.modifiers.find(item => item.name === 'preventOverflow');
  assert.equal(overflow.options.altAxis, true, 'vertical escape must be clamped as well as horizontal overflow');
  assert.equal(overflow.options.tether, false, 'an offscreen anchor must not drag the search out of view');
  anchorY = 320; c.scheduleRunConfigPosition(); viewport.fire('resize'); flushFrame(); await nextTick();
  assert.deepEqual(positions, [80, 320], 'blur and viewport resize coalesce into one reposition at the new anchor');
  anchorY = 360; viewport.fire('scroll'); flushFrame(); await nextTick(); assert.deepEqual(positions, [80, 320, 360]);
  mobile = false;
  anchorY = 400; win.fire('resize'); flushFrame(); await nextTick(); assert.deepEqual(positions, [80, 320, 360, 400]);
  assert.equal(runConfigPopperOptions.value.strategy, 'absolute');
  assert.equal(runConfigPopperOptions.value.modifiers, undefined, 'desktop returns to default Popper behavior');
  viewport.fire('resize'); props.modelMenuOpen = false; await nextTick(); flushFrame();
  assert.deepEqual(positions, [80, 320, 360, 400], 'closed popup does not update');
  assert.equal(viewport.listeners.size, 0); assert.equal(win.listeners.size, 0);
  stopWatch();
});

test('mobile model picker fixes its search and footer, with only the active middle pane scrolling', () => {
  const styles = postcss.parse(parse(source).descriptor.styles.map(style => style.content).join('\n'));
  const mobileRules = {};
  styles.walkAtRules('media', media => {
    if (media.params !== '(max-width: 760px), (hover: none) and (pointer: coarse)') return;
    media.walkRules(rule => {
      const declarations = mobileRules[rule.selector] ||= {};
      rule.walkDecls(decl => { declarations[decl.prop] = decl.value; });
    });
  });
  assert.match(mobileRules['.run-config-popover']['max-height'], /var\(--mobile-viewport-height, 100dvh\)/);
  assert.match(mobileRules['.run-config-popover']['max-height'], /safe-area-inset-top.*safe-area-inset-bottom/);
  assert.equal(mobileRules['.run-config-popover']['overflow-y'], 'hidden');
  assert.equal(mobileRules['.run-config-model-section'].flex, '1 1 auto');
  assert.equal(mobileRules['.run-config-model-section']['min-height'], '0');
  assert.equal(mobileRules['.run-config-model-section'].overflow, 'hidden');
  assert.equal(mobileRules['.run-config-model-list'].flex, '1 1 auto');
  assert.equal(mobileRules['.run-config-model-list']['max-height'], '320px');
  assert.equal(mobileRules['.run-config-search'].position, 'static');
  assert.equal(mobileRules['.run-config-search input']['font-size'], '16px');
  assert.equal(mobileRules['.run-config-compact-footer'].flex, '0 0 auto');
  assert.equal(mobileRules['.run-config-popover.is-compact .run-config-controls']['overflow-y'], 'auto');
});

test('measured fixed chrome selects compact layout without resize oscillation and restores after keyboard dismissal', () => {
  let capacity = 600;
  const child = (height, model = false) => ({height, display: 'block', classList: {contains: name => model && name === 'run-config-model-section'}, getBoundingClientRect() {return {height: this.height};}});
  const children = [child(36), child(48), child(38), child(320, true), child(180)];
  const content = {clientHeight: 600, children};
  const runConfigCompact = ref(false), runConfigSettingsOpen = ref(false);
  const c = vm.createContext({runConfigContent: ref(content), runConfigCompact, runConfigSettingsOpen,
    window: {getComputedStyle: node => node.children ? {maxHeight: `${capacity}px`, rowGap: '10px'} : {display: node.display}, visualViewport: {height: 800}, innerHeight: 800}});
  vm.runInContext('let runConfigFullChromeHeight = 0;\n' + between(source, 'function syncRunConfigLayout(', 'function scheduleRunConfigPosition('), c);
  c.syncRunConfigLayout(true); assert.equal(runConfigCompact.value, false);
  assert.equal(vm.runInContext('runConfigFullChromeHeight', c), 342);
  capacity = 320; c.syncRunConfigLayout(true); assert.equal(runConfigCompact.value, true);
  children[1].display = children[4].display = 'none';
  c.syncRunConfigLayout(true); assert.equal(runConfigCompact.value, true, 'collapsed chrome cannot make the next resize reopen the full controls');
  assert.equal(vm.runInContext('runConfigFullChromeHeight', c), 342);
  runConfigSettingsOpen.value = true;
  capacity = 600; c.syncRunConfigLayout(true);
  assert.equal(runConfigCompact.value, false); assert.equal(runConfigSettingsOpen.value, false);
  children[1].display = children[4].display = 'block'; children[3].height = 24;
  c.syncRunConfigLayout(true); assert.equal(runConfigCompact.value, false, 'few search results do not trigger compact mode');
  capacity = 320; c.syncRunConfigLayout(true); runConfigSettingsOpen.value = true;
  c.syncRunConfigLayout(false);
  assert.equal(runConfigCompact.value, false); assert.equal(runConfigSettingsOpen.value, false, 'desktop retains its existing presentation');
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
