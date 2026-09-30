import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {createRequire} from 'node:module';
import * as Vue from 'vue';
import {renderToString} from 'vue/server-renderer';
import {parse, compileStyle} from '@vue/compiler-sfc';
import {baseParse} from '@vue/compiler-dom';
import {parse as parseScript} from '@babel/parser';
import postcss from 'postcss';
import draggable from 'vuedraggable';
import {ElButton} from 'element-plus';
import {scrollModelListAbove} from '../components/modelDragAutoScroll.js';

const files = ['SettingsHubView.vue', 'ChannelsView.vue'];
const source = Object.fromEntries(files.map(f => [f, fs.readFileSync(new URL(f, import.meta.url), 'utf8')]));
const sfc = Object.fromEntries(files.map(f => [f, parse(source[f]).descriptor]));
const styles = Object.fromEntries(files.map(f => [f, postcss.parse(sfc[f].styles.map(s => s.content).join('\n'))]));
const walk = nodes => (nodes || []).flatMap(n => [n, ...walk(Array.isArray(n.children) ? n.children : []), ...walk(n.component?.subTree ? [n.component.subTree] : [])]);
const ast = f => walk(baseParse(sfc[f].template.content).children);
const hasClass = (n, c) => n.props?.some?.(p => p.name === 'class' && p.value?.content.split(/\s+/).includes(c));
const node = (f, c) => ast(f).find(n => hasClass(n, c));
const classHas = (n, c) => String(n.props?.class || '').split(/\s+/).includes(c);
function css(f, selector, width = 390) {
  const out = {}, priorities = {};
  styles[f].walkRules(rule => {
    if (!rule.selectors.includes(selector)) return;
    for (let p = rule.parent; p; p = p.parent) {
      if (p.type === 'atrule' && p.name === 'media') {
        const max = p.params.match(/max-width:\s*(\d+)px/), min = p.params.match(/min-width:\s*(\d+)px/);
        if ((max && width > +max[1]) || (min && width < +min[1])) return;
      }
    }
    rule.walkDecls(d => { if (!priorities[d.prop] || d.important) { out[d.prop] = d.value; priorities[d.prop] = d.important; } });
  });
  return out;
}
const forbidden = () => assert.fail('Business API/confirmation is forbidden in this UI harness');
function runtime(f, overrides = {}) {
  const script = sfc[f].scriptSetup.content;
  const names = parseScript(script, {sourceType: 'module'}).program.body.flatMap(n => n.type === 'VariableDeclaration' ? n.declarations.map(d => d.id.name) : n.type === 'FunctionDeclaration' ? [n.id.name] : []);
  const props = Vue.reactive({section: 'channels'}), events = [], mounted = [], unmounted = [];
  const ctx = vm.createContext({ ...Vue, draggable, scrollModelListAbove,
    defineProps: () => props, defineEmits: () => (...args) => events.push(args),
    defineLazyView: (_loader, label) => ({name: label, render: () => Vue.h('div', label)}),
    onMounted: fn => mounted.push(fn), onBeforeUnmount: fn => unmounted.push(fn), mobileSlots: {},
    Api: new Proxy({}, {get: () => forbidden}), ElMessageBox: {confirm: forbidden}, ElMessage: {error: forbidden, success: forbidden}, apiError: String,
    copyTextToClipboard: forbidden, ...overrides,
  });
  vm.runInContext(script.replace(/^import .*;\n/gm, ''), ctx);
  const bindings = () => Vue.proxyRefs(vm.runInContext(`({${names.join(',')}, draggable, $slots: mobileSlots})`, ctx));
  return {ctx, props, events, mounted, unmounted, run: code => vm.runInContext(code, ctx), bindings};
}
async function render(f, r, markup = sfc[f].template.content) {
  const compiled = Vue.compile(markup); let tree;
  const app = Vue.createSSRApp({render() { tree = compiled.call(this, r.bindings(), []); return tree; }});
  app.config.warnHandler = message => assert.fail(message);
  app.component('draggable', draggable);
  app.component('ElDialog', {render: () => null}); // closed dialogs; do not evaluate their slots
  app.component('ElIcon', {render() { return Vue.h('i', this.$slots.default?.()); }});
  app.component('ElButton', ElButton);
  for (const name of ['ElSelect', 'ElOption']) app.component(name, {render: () => null}); // dialog-only controls
  app.directive('loading', {getSSRProps: () => ({})});
  const html = await renderToString(app);
  return {html, nodes: walk([tree])};
}
const settings = 'SettingsHubView.vue', channels = 'ChannelsView.vue';

test('settings merges navigation only after its lazy header is ready, preserving loading/failure escape', async () => {
  const appSource = fs.readFileSync(new URL('../App.vue', import.meta.url), 'utf8');
  const appSfc = parse(appSource).descriptor;
  styles['App.vue'] = postcss.parse(appSfc.styles.map(s => s.content).join('\n'));
  const appNodes = walk(baseParse(appSfc.template.content).children);
  const shell = appNodes.find(n => hasClass(n, 'app-shell'));
  const expression = shell.props.find(p => p.name === 'bind' && p.arg?.content === 'class').exp.content;
  const classes = new Function('active', 'pageHeaderReady', `return (${expression})`);
  assert.equal(classes('settings', false)['is-settings'], false, 'navigation stays available while loading or failed');
  assert.equal(classes('settings', true)['is-settings'], true);
  assert.equal(classes('logs', true)['is-settings'], false);
  const outlet = appNodes.find(n => n.tag === 'component' && n.props.some(p => p.name === 'bind' && p.arg?.content === 'is' && p.exp?.content === 'activeView'));
  const Forwarder = {render() {return Vue.h('div', this.$slots['mobile-navigation']?.());}};
  const scope = Vue.reactive({active:'settings', activeView:Vue.markRaw(Forwarder), sidebarOpen:false, settingsSection:'channels', pageHeaderReady:false, handleSettingsSectionChanged(){}});
  const compiled = Vue.compile(outlet.loc.source.replace(/\s+v-else(?=\s)/, ''));
  let tree;
  const rerender = async () => {
    const app = Vue.createSSRApp({render() {tree = compiled.call(this, scope, []); return tree;}});
    await renderToString(app); return walk([tree]);
  };
  let rendered = await rerender();
  const navigation = rendered.find(n => n.type === 'button' && classHas(n, 'mobile-sidebar-toggle'));
  assert.ok(navigation); navigation.props.onClick(); assert.equal(scope.sidebarOpen, true);
  tree.props.onMobileHeaderReady(true); assert.equal(scope.pageHeaderReady, true);
  rendered = await rerender();
  assert.equal(rendered.find(n => n.type === 'button').props['aria-expanded'], true);
  const r = runtime(settings, {mobileSlots:{'mobile-navigation':() => [Vue.h('button', {'aria-label':'导航测试'}, '导航')]}});
  const settingsView = await render(settings, r);
  assert.ok(settingsView.nodes.find(n => n.type === 'button' && n.props['aria-label'] === '导航测试'));
  r.mounted.forEach(fn => fn()); assert.deepEqual(r.events.at(-1), ['mobile-header-ready', true]);
  r.unmounted.forEach(fn => fn()); assert.deepEqual(r.events.at(-1), ['mobile-header-ready', false]);
  for (const width of [320, 390, 760]) {
    assert.equal(css('App.vue', '.app-shell.is-settings .mobile-app-bar', width).display, 'none');
    assert.equal(css('App.vue', '.app-shell.is-settings .app-main', width)['padding-top'], '0');
    assert.equal(css(settings, '.settings-mobile-navigation', width).display, 'flex');
    assert.equal(css(settings, '.settings-heading', width).display, 'none');
  }
  assert.equal(css('App.vue', '.app-shell.is-settings .mobile-app-bar', 1440).display, undefined);
  assert.equal(css(settings, '.settings-mobile-navigation', 1440).display, 'none');
});

test('native pickers have one inset icon; channel switching and disclosure align without twin chevrons', () => {
  for (const width of [320, 390, 760]) {
    assert.equal(css(settings, '.settings-section-select', width).appearance, 'none');
    assert.equal(css(settings, '.settings-section-select', width).padding, '0 40px 0 12px');
    assert.equal(css(settings, '.settings-section-chevron', width).right, '14px');
    assert.equal(css(settings, '.settings-section-chevron', width)['pointer-events'], 'none');
    assert.equal(css(channels, '.channel-mobile-picker select', width).appearance, 'none');
    assert.match(css(channels, '.channel-mobile-picker select', width).border, /^1px solid/);
    assert.equal(css(channels, '.channel-provider-toggle', width)['align-self'], 'end');
    assert.equal(css(channels, '.channel-provider-toggle', width).height, css(channels, '.channel-mobile-picker select', width)['min-height']);
    assert.equal(css(channels, '.channel-switch-icon', width)['pointer-events'], 'none');
  }
  assert.equal(walk(node(settings, 'settings-section-control').children).filter(n => n.tag === 'svg').length, 1);
  assert.equal(walk(node(channels, 'channel-mobile-picker').children).filter(n => hasClass(n, 'channel-chevron')).length, 0);
  assert.ok(walk(node(channels, 'channel-mobile-picker').children).some(n => hasClass(n, 'channel-switch-icon')));
});

// Relevant matched selectors, with both specificity and !important respected.
// This catches the old desktop display:flex !important defeating phone groups.
function matchedProperty(f, matchedSelectors, property, width) {
  let best;
  styles[f].walkRules(rule => {
    const matching = rule.selectors.filter(s => matchedSelectors.includes(s)); if (!matching.length) return;
    for (let p = rule.parent; p; p = p.parent) if (p.type === 'atrule' && p.name === 'media') {
      const max = p.params.match(/max-width:\s*(\d+)px/); if (max && width > +max[1]) return;
    }
    const specificity = Math.max(...matching.map(s => (s.match(/[.#][\w-]+/g) || []).length));
    rule.walkDecls(property, d => {
      const important = Number(Boolean(d.important));
      if (!best || important > best.important || (important === best.important && specificity >= best.specificity)) best = {important, specificity, value:d.value};
    });
  }); return best;
}

test('everyday model actions and explicit delete align on one row without a one-item overflow menu', async () => {
  for (const width of [320, 390, 760]) {
    assert.equal(matchedProperty(channels, ['.model-action-row','.channels-view .model-action-row'], 'display', width).value, 'grid');
    assert.equal(matchedProperty(channels, ['.model-action-group','.model-action-group.is-meta','.channels-view .model-action-row .model-action-group'], 'display', width).value, 'contents');
    assert.equal(css(channels, '.channels-view .model-action-row.has-sync', width)['grid-template-columns'].match(/minmax/g).length, 4);
    assert.equal(css(channels, '.channels-view .model-action-row', width)['grid-template-columns'].includes('repeat(3,'), true);
    for (const [selector, order] of [['.channels-view .model-action.is-primary-action',0],['.model-edit-action',1],['.model-sync-action',2],['.model-copy-action',3],['.model-action-row .is-danger',4]]) assert.equal(css(channels, selector, width).order, String(order));
    assert.equal(css(channels, '.model-action-row .is-danger', width)['grid-column'], '-2 / -1');
    assert.equal(css(channels, '.channels-view .model-action', width).display, 'inline-flex');
    assert.equal(css(channels, '.channels-view .model-action', width)['align-items'], 'center');
    assert.equal(css(channels, '.model-paste-action', width)['grid-column'], '1 / -1');
  }
  assert.equal(matchedProperty(channels, ['.model-action-row','.channels-view .model-action-row'], 'display', 1440).value, 'flex');
  assert.equal(matchedProperty(channels, ['.model-action-group','.channels-view .model-action-row .model-action-group'], 'display', 1440).value, 'flex');
  const r = channelRuntime(1);
  let rendered = await render(channels, r);
  let row = rendered.nodes.find(n => classHas(n, 'model-action-row'));
  const actions = () => walk(row.children).filter(n => n.type === 'button');
  for (const name of ['is-primary-action','model-edit-action','model-sync-action','model-copy-action']) assert.ok(actions().some(n => classHas(n, name)));
  assert.equal(actions().some(n => classHas(n, 'model-more-toggle')), false);
  assert.ok(actions().some(n => classHas(n, 'is-danger')));
  assert.equal(actions().some(n => classHas(n, 'model-paste-action')), false);
  r.run('canPasteMetadataTo = () => true');
  rendered = await render(channels, r); row = rendered.nodes.find(n => classHas(n, 'model-action-row'));
  assert.ok(actions().some(n => classHas(n, 'model-paste-action')));
  for (const name of ['is-primary-action','model-edit-action','model-sync-action','model-copy-action','is-danger']) assert.ok(actions().some(n => classHas(n, name)));
});

test('drag handle owns a nonoverlapping touch cell and real Sortable registers touch movement, not native HTML drag', () => {
  for (const width of [320, 390, 760]) {
    assert.equal(css(channels, '.model-head', width)['grid-template-columns'], 'minmax(0, 1fr) 44px');
    assert.equal(css(channels, '.model-title-block', width)['grid-area'], '1 / 1');
    const handle = css(channels, '.channels-view .model-drag', width);
    assert.equal(handle['grid-area'], '1 / 2'); assert.equal(handle.position, 'relative');
    assert.equal(handle['z-index'], '1'); assert.equal(handle['touch-action'], 'none');
    assert.equal(css(channels, '.channels-view .model-drag-grip', width)['pointer-events'], 'none');
  }
  const require = createRequire(import.meta.url);
  const Sortable = createRequire(require.resolve('vuedraggable'))('sortablejs');
  const oldDocument = Object.getOwnPropertyDescriptor(globalThis, 'document');
  const registered = [];
  Object.defineProperty(globalThis, 'document', {configurable:true, value:{addEventListener:(event,handler) => registered.push({event,handler})}});
  try {
    const move = () => {};
    Sortable.prototype._triggerDragStart.call({nativeDraggable:true,options:{supportPointer:true},_onTouchMove:move}, {pointerType:'touch'}, null);
    Sortable.prototype._triggerDragStart.call({nativeDraggable:true,options:{supportPointer:false},_onTouchMove:move}, {type:'touchstart'}, {clientX:100,clientY:100});
    assert.deepEqual(registered.map(v => v.event), ['pointermove','touchmove']);
    assert.ok(registered.every(v => v.handler === move));
  } finally { if (oldDocument) Object.defineProperty(globalThis, 'document', oldDocument); else delete globalThis.document; }
});

test('compiled native section picker exposes every settings section and uses existing route synchronization', async () => {
  const r = runtime(settings);
  let view = await render(settings, r);
  const select = view.nodes.find(n => n.type === 'select');
  assert.equal(select.props['aria-label'], '切换设置分区');
  const options = view.nodes.filter(n => n.type === 'option');
  assert.deepEqual(options.map(n => n.props.value), ['channels', 'templates', 'agents', 'system-settings', 'sessions', 'logs', 'install-app']);
  assert.ok(view.html.includes('安装应用 (PWA)'));
  for (const option of options) {
    select.props.onChange({target: {value: option.props.value}}); await Vue.nextTick();
    assert.equal(r.run('activeSection.value'), option.props.value);
    assert.equal(r.run('activeComponent.value'), r.run('activeInfo.value.component'));
  }
  assert.deepEqual(r.events.at(-1), ['section-changed', 'install-app']);
  r.props.section = 'logs'; await Vue.nextTick();
  view = await render(settings, r);
  assert.equal(view.nodes.find(n => n.type === 'select').props.value, 'logs');
  select.props.onChange({target: {value: 'unknown'}}); await Vue.nextTick();
  assert.equal(r.run('activeSection.value'), 'channels');
  const before = r.events.length;
  select.props.onChange({target: {value: 'channels'}}); await Vue.nextTick();
  assert.equal(r.events.length, before);
});

test('restart remains one accessible, busy-guarded existing handler; never invoke it or its API', async () => {
  const r = runtime(settings);
  for (const busy of [false, true]) {
    r.run(`restarting.value = ${busy}`);
    const {nodes} = await render(settings, r);
    const button = nodes.find(n => n.type === 'button' && classHas(n, 'settings-restart'));
    assert.equal(button.props['aria-label'], '重启 OpenBear');
    assert.equal(button.props['aria-busy'], busy); assert.equal(button.props.disabled, busy);
    const component = nodes.find(n => n.type === ElButton && classHas(n, 'settings-restart'));
    assert.equal(component.props.loading, busy);
    assert.equal(component.props.onClick, r.run('confirmRestart'));
  }
  const script = sfc[settings].scriptSetup.content;
  assert.match(script, /systemRestart\(\{ confirm: true, force, reason: "web settings page" \}\)/);
  assert.match(script, /resp\?\.status === 409 && resp\.data\?\.running/);
  assert.match(script, /confirmButtonText: "强制重启"/);
  assert.match(script, /catch \{ return; \}\s*return requestRestart\(true\)/);
  assert.match(script, /finally \{\s*restarting.value = false/);
  assert.match(script, /catch \{ return; \}\s*await requestRestart\(false\)/);
});

test('320–760px navigation is a 44px picker and quiet restart, never a horizontal tab scroller', () => {
  for (const width of [320, 360, 390, 430, 640, 760]) {
    assert.equal(css(settings, '.settings-desktop-tabs', width).display, 'none');
    assert.equal(css(settings, '.settings-subtitle', width).display, 'none');
    assert.equal(css(settings, '.settings-section-select', width).display, 'block');
    assert.equal(css(settings, '.settings-section-select', width)['min-width'], '0');
    assert.equal(css(settings, '.settings-section-select', width)['min-height'], '44px');
    const restart = css(settings, '.settings-header .settings-restart', width);
    assert.equal(restart.border, '0'); assert.equal(restart.background, 'transparent');
    assert.equal(restart['min-height'], '44px'); assert.equal(restart['min-width'], '44px');
  }
});

function searchRuntime({mobile = true, scale = 1, noViewport = false} = {}) {
  const timers = new Map(); let nextTimer = 0;
  const stack = {scrollTop: 200}, list = {scrollTop: 450};
  const document = {activeElement: null};
  const input = {blur() {document.activeElement = null;}};
  document.activeElement = input;
  const window = {
    innerHeight: 874,
    visualViewport: noViewport ? null : {height: 874, scale, removeEventListener() {}},
    removeEventListener() {}, matchMedia: () => ({matches: mobile}),
    setTimeout(callback, delay) {const id = ++nextTimer; timers.set(id, {callback, delay}); return id;},
    clearTimeout(id) {timers.delete(id);},
  };
  const r = runtime(channels, {window, document});
  Object.assign(r.ctx, {input, stack, list});
  r.run('modelSearchInput.value=markRaw(input);channelDetailStack.value=markRaw(stack);modelScrollList.value=markRaw({$el:list});modelSearchQuery.value="astra";providerDetailsOpen.value=true;channelOverviewOpen.value=true;channelToolsOpen.value=true');
  return {...r, stack, list, document, window, timers, setHeight(height) {if (window.visualViewport) window.visualViewport.height = height; else window.innerHeight = height;}};
}

test('channel search uses a local workspace, retains query/disclosures, and restores both positions on done', async () => {
  const r = searchRuntime();
  const inputTemplate = node(channels, 'channel-model-search');
  const {nodes} = await render(channels, r, inputTemplate.loc.source);
  const input = nodes.find(n => n.type === 'input');
  input.props.onFocus(); assert.equal(r.run('modelSearchMode.value'), true);
  r.stack.scrollTop = 0; r.list.scrollTop = 30;
  r.setHeight(320);
  r.run('scheduleModelSearchViewport();scheduleModelSearchViewport();scheduleModelSearchViewport()');
  assert.equal(r.timers.size, 1, 'keyboard animation events settle once');
  const pending = [...r.timers.values()][0]; assert.equal(pending.delay, 120);
  r.timers.clear(); pending.callback();
  assert.equal(r.stack.scrollTop, 0, 'keyboard changes do not scroll the outer stack');
  assert.equal(r.run('modelSearchMode.value'), true);
  r.document.activeElement = null;
  r.run('syncModelSearchViewport()');
  assert.equal(r.run('modelSearchMode.value'), true, 'blur precedes row clicks and cannot tear down the workspace');
  r.run('finishModelSearch()'); await Vue.nextTick();
  assert.equal(r.run('modelSearchMode.value'), false);
  assert.equal(r.stack.scrollTop, 200); assert.equal(r.list.scrollTop, 450);
  assert.equal(r.run('modelSearchQuery.value'), 'astra');
  assert.equal(r.run('providerDetailsOpen.value && channelOverviewOpen.value && channelToolsOpen.value'), true);
  assert.deepEqual(r.events, [], 'layout changes invoke no business action');
  r.run('scheduleModelSearchViewport()'); r.unmounted.forEach(fn => fn());
  assert.equal(r.timers.size, 0, 'unmount cancels the pending callback');
});

test('keyboard dismissal restores channel layout even while focused, and a second keyboard opening works without refocus', async () => {
  for (const noViewport of [false, true]) {
    const r = searchRuntime({noViewport});
    r.run('beginModelSearch()'); r.setHeight(300); r.run('syncModelSearchViewport()');
    r.setHeight(340); r.run('syncModelSearchViewport()');
    assert.equal(r.run('modelSearchMode.value'), true, 'small toolbar changes do not end search');
    r.setHeight(874); r.run('syncModelSearchViewport()'); await Vue.nextTick();
    assert.equal(r.run('modelSearchMode.value'), false, 'keyboard can close without an input blur');
    r.setHeight(300); r.run('syncModelSearchViewport()');
    assert.equal(r.run('modelSearchMode.value'), true, 'already-focused field can summon the keyboard again');
    assert.equal(r.run('modelSearchQuery.value'), 'astra');
  }
  for (const scenario of [{mobile:false}, {scale:2}]) {
    const r = searchRuntime(scenario); r.run('beginModelSearch();syncModelSearchViewport()');
    assert.equal(r.run('modelSearchMode.value'), false, 'desktop and manual pinch zoom stay native');
    assert.equal(r.stack.scrollTop, 200); assert.equal(r.list.scrollTop, 450);
  }
});

test('mobile search removes the outer scroll and minimum panel height, with a fixed channel/search header and 16px input', () => {
  for (const width of [320, 390, 640, 760]) {
    assert.equal(css(channels, '.channels-view.is-model-search .channel-detail-stack', width)['overflow-y'], 'hidden');
    const panel = css(channels, '.channels-view.is-model-search .channel-models-panel', width);
    assert.equal(panel.flex, '1 1 0%'); assert.equal(panel['min-height'], '0');
    for (const name of ['channels-header','channels-overview','channel-overview-card','channel-models-title','channel-models-actions']) {
      assert.equal(css(channels, `.channels-view.is-model-search .${name}`, width).display, 'none');
    }
    assert.equal(css(channels, '.channels-view.is-model-search .channel-search-context', width).flex, '0 0 auto');
    assert.equal(css(channels, '.channel-model-search input', width)['font-size'], '16px');
    assert.equal(css(channels, '.channels-view.is-model-search .model-list-scroll', width)['overscroll-behavior'], 'contain');
  }
  assert.equal(css(channels, '.channels-view.is-model-search .channel-detail-stack', 1280)['overflow-y'], undefined);
  assert.equal(css(channels, '.channel-model-search input', 1280)['font-size'], undefined, 'desktop input size is unchanged');
  assert.equal(css(channels, '.channel-model-search .mac-input', 1280)['padding-left'], '1.75rem', 'desktop icon gutter is not overridden by generic input padding');
  assert.equal(css(channels, '.channel-model-search .mac-input', 1280)['padding-right'], '1.5rem');
  for (const width of [320, 390, 760]) assert.deepEqual(css(channels, '.channel-model-search .mac-input', width), {}, 'phone search keeps its existing padding rule');
});

test('mobile model list scrolls normally and short viewports scroll the stack without squeezing the channel card', () => {
  const chain = ['channels-view', 'channels-workspace', 'channel-detail', 'channel-detail-stack', 'channel-models-panel', 'model-list-scroll'];
  let parent = node(channels, chain[0]);
  assert.ok(hasClass(parent, 'h-full'));
  for (const c of chain.slice(1)) {
    const next = walk(parent.children).find(n => hasClass(n, c));
    assert.ok(next, `${c} is inside the actual scroll ancestry`); parent = next;
  }
  assert.ok(hasClass(node(settings, 'settings-content'), 'min-h-0'));
  assert.ok(hasClass(node(settings, 'settings-content'), 'flex-1'));
  for (const width of [320, 360, 390, 430, 640, 760]) {
    const root = css(channels, '.channels-view', width);
    assert.equal(root.display, 'flex'); assert.equal(root['min-height'], '0'); assert.equal(root.overflow, 'hidden');
    assert.equal(root['touch-action'], 'pan-y pinch-zoom');
    for (const c of chain.slice(1, -1)) {
      const style = css(channels, `.${c}`, width);
      assert.equal(style['min-height'], c === 'channel-models-panel' ? '240px' : '0', c);
      assert.equal(style.flex, c === 'channel-models-panel' ? '1 0 240px' : '1 1 0%', `${c}: model content must not set the outer flex basis`);
      assert.equal(style.overflow, 'hidden', c);
      if (c === 'channel-detail-stack') assert.equal(style['overflow-y'], 'auto');
    }
    assert.equal(css(channels, '.channels-view .channel-overview-card', width)['max-height'], 'none');
    assert.equal(css(channels, '.channels-view .channel-overview-card', width).overflow, 'visible');
    assert.equal(css(channels, '.channel-models-header', width).flex, 'none');
    const list = css(channels, '.model-list-scroll', width);
    assert.equal(list['overflow-y'], 'auto');
    assert.equal(list.flex, '1 1 0%');
    assert.equal(list['grid-template-columns'], 'minmax(0, 1fr)');
    assert.equal(list['grid-auto-rows'], 'max-content');
    assert.equal(list['overscroll-behavior'], 'auto');
    assert.equal(css(channels, '.channels-view .model-drag', width)['touch-action'], 'none');
    assert.equal(css(channels, '.channels-view .model-drag:disabled', width)['touch-action'], 'pan-y pinch-zoom');
  }
});

test('mobile overview/metrics wrap rather than crop, header/actions have explicit hierarchy with unchanged typography', () => {
  for (const width of [320, 390, 640, 760]) {
    assert.equal(css(channels, '.channels-toolbar', width).display, 'none');
    assert.equal(css(channels, '.channels-toolbar.is-open', width).display, 'flex');
    assert.equal(css(channels, '.channels-overview', width).display, 'none');
    assert.equal(css(channels, '.channels-overview.is-open', width).display, 'block');
    assert.equal(css(channels, '.channel-models-title', width)['grid-area'], '1 / 1');
    assert.equal(css(channels, '.channel-models-actions .mac-primary-button', width)['grid-area'], '1 / 2');
    assert.equal(css(channels, '.channel-model-search', width)['grid-area'], '2 / 1');
    assert.equal(css(channels, '.channel-batch-sync', width)['grid-area'], '2 / 2');
    assert.equal(css(channels, '.channels-overview-grid', width)['grid-template-columns'], 'repeat(2, minmax(0, 1fr))');
    assert.equal(css(channels, '.channels-view .channels-overview-grid > div', width)['flex-wrap'], 'wrap');
    assert.equal(css(channels, '.channels-view .channels-overview-panel', width).overflow, 'visible');
    assert.equal(css(channels, '.provider-metrics', width)['grid-template-columns'], 'repeat(2, minmax(0, 1fr))');
    assert.equal(css(channels, '.provider-metrics .metric-card--unified', width).height, 'auto');
    assert.equal(css(channels, '.provider-metrics .metric-card strong', width)['white-space'], 'normal');
    assert.equal(css(channels, '.channels-switcher', width).display, 'none');
    assert.equal(css(channels, '.channel-mobile-picker select', width)['min-height'], '44px');
    assert.equal(css(channels, '.channels-view .model-title', width)['white-space'], 'normal');
    for (const selector of ['.channel-overview-card:not(.is-open) .channel-provider-details', '.channel-overview-card:not(.is-open) .channel-actions', '.model-group-block:not(.is-open) .model-section-body']) assert.equal(css(channels, selector, width).display, 'none');
    assert.equal(css(channels, '.channels-view .model-action-row', width).display, 'grid');
    assert.equal(css(channels, '.channels-view .model-action-row .model-action-group', width).display, 'contents');
    assert.equal(css(channels, '.channels-view .stat-v', width)['overflow-wrap'], 'anywhere');
  }
  for (const f of files) styles[f].walkAtRules('media', media => {
    if (media.params !== '(max-width: 760px)') return;
    const searchSizes = {'.channel-model-search input': '16px', '.channel-search-context strong': '13px', '.channel-search-context span': '12px'};
    media.walkDecls(d => {
      if (f === channels && d.prop === 'font-size' && d.parent.selector in searchSizes) {
        assert.equal(d.value, searchSizes[d.parent.selector]);
      } else assert.ok(!['font-size', 'font-family', 'zoom'].includes(d.prop), `unrelated mobile typography/zoom is unchanged: ${d.toString()}`);
    });
  });
});

test('761px+ keeps desktop tabs, restart style and nested model scroller/grid rules', () => {
  for (const width of [761, 1024, 1440]) {
    assert.equal(css(settings, '.settings-section-select', width).display, 'none');
    for (const c of ['.settings-header', '.settings-desktop-tabs', '.settings-header .settings-restart']) assert.deepEqual(css(settings, c, width), {});
    for (const c of ['.channels-view', '.channels-workspace', '.channel-detail-stack', '.channels-header', '.channels-view .model-card']) assert.deepEqual(css(channels, c, width), {});
    assert.equal(css(channels, '.model-list-scroll', width)['overflow-y'], 'auto');
    assert.equal(css(channels, '.model-list-scroll', width).flex, '1 1 auto');
    assert.equal(css(channels, '.model-grid-responsive', width)['grid-template-columns'], 'repeat(auto-fill, minmax(360px, 1fr))');
    assert.equal(css(channels, '.model-action-row', width)['flex-wrap'], 'nowrap');
  }
  for (const f of files) for (const style of sfc[f].styles) {
    const result = compileStyle({source: style.content, filename: f, id: 'data-v-mobile-test', scoped: style.scoped});
    assert.deepEqual(result.errors, []);
  }
});

function channelRuntime(count = 40) {
  const r = runtime(channels);
  r.run(`providers.value = [{name:'fixture-channel', modelCount:${count}, enabled:true}];
    selectedName.value = 'fixture-channel';
    detail.value = {provider:{name:'fixture-channel', enabled:true, protocol:'chat', baseUrl:'https://fixture.invalid/api', apiKeyMasked:'fixture-masked', stats:{calls:12345678,input_tokens:100,cache_read_tokens:900,cost_usd:12345.67},
      models:Array.from({length:${count}}, (_,i) => ({id:'model-'+i, fullname:'fixture-channel/model-'+i, name:'模型 '+i, stats:{calls:i,input_tokens:100,cache_read_tokens:900}, thinkingLevels:['low','high'], cost:{input:1,output:2}, modelsDev:{bound:true,providerId:'fixture'}}))}};
    modelsDev.value = {available:true};`);
  return r;
}

test('mobile disclosure controls reveal existing full data independently without business calls, and remain inert to desktop layout', async () => {
  const r = channelRuntime(2);
  r.run(`selectedProvider.value.models[0].fullname = 'fixture-channel/model with space/α';`);
  let view = await render(channels, r);
  const expandable = view.nodes.filter(n => n.type === 'button' && n.props['aria-expanded'] !== undefined);
  assert.equal(expandable.length, 7); // statistics, management, provider + 2 per model
  for (const original of expandable) {
    const targetIds = original.props['aria-controls'];
    assert.equal(original.props['aria-expanded'], false);
    for (const id of targetIds.split(' ')) assert.ok(view.nodes.some(n => n.props?.id === id), id);
    original.props.onClick(); await Vue.nextTick();
    view = await render(channels, r);
    let current = view.nodes.find(n => n.type === 'button' && n.props['aria-controls'] === targetIds);
    assert.equal(current.props['aria-expanded'], true);
    for (const id of targetIds.split(' ')) {
      const target = view.nodes.find(n => n.props?.id === id);
      assert.ok(target);
      const owner = view.nodes.find(n => classHas(n, 'is-open') && (n === target || walk(n.children).includes(target)));
      assert.ok(owner, `target ${id} sits in its open-state owner`);
    }
    assert.ok(view.html.includes('fixture-masked'));
    assert.ok(view.html.includes('压缩触发'));
    assert.ok(view.html.includes('吞吐速度'));
    current.props.onClick(); await Vue.nextTick();
    view = await render(channels, r);
    current = view.nodes.find(n => n.type === 'button' && n.props['aria-controls'] === targetIds);
    assert.equal(current.props['aria-expanded'], false);
  }
  const ids = view.nodes.map(n => n.props?.id).filter(Boolean);
  assert.equal(new Set(ids).size, ids.length);
  for (const width of [761, 1024, 1440]) {
    assert.equal(css(channels, '.channels-view .mobile-channel-only', width).display, 'none');
    for (const selector of ['.channel-provider-details', '.model-section-body', '.channel-models-title', '.channel-identity-badges']) assert.equal(css(channels, selector, width).display, 'contents');
    for (const selector of ['.channels-overview', '.model-group-block:not(.is-open) .model-section-body', '.model-action-row:not(.is-open) .model-paste-action', '.channel-overview-card:not(.is-open) .channel-actions']) assert.equal(css(channels, selector, width).display, undefined);
  }
});

test('phone channel picker lists all providers and reuses selected-channel handler, including an unavailable detail', async () => {
  const r = channelRuntime(1);
  r.run(`providers.value.push({name:'second provider',enabled:false}); globalThis.picked=[]; loadProvider = name => picked.push(name);`);
  for (const missing of [false, true]) {
    if (missing) r.run('detail.value = null');
    const view = await render(channels, r);
    const select = view.nodes.find(n => n.type === 'select' && n.props['aria-label'] === '切换渠道');
    assert.ok(select);
    const options = walk(select.children).filter(n => n.type === 'option' && n.props.value);
    assert.deepEqual(options.map(n => n.props.value), ['fixture-channel', 'second provider']);
    select.props.onChange({target: {value: 'second provider'}});
    assert.equal(r.run('picked.at(-1)'), 'second provider');
  }
  r.run('providers.value = []');
  const empty = await render(channels, r);
  assert.ok(empty.nodes.find(n => classHas(n, 'channel-empty-actions')));
});

test('actual Vue + vuedraggable SSR renders every model/card/action in original order without any mounted API calls', async () => {
  const r = channelRuntime();
  const {html, nodes} = await render(channels, r);
  const list = nodes.find(n => n.type === draggable && classHas(n, 'model-list-scroll'));
  assert.ok(list); assert.equal(list.props.handle, '.model-drag'); assert.equal(list.props.disabled, false);
  assert.equal(typeof list.props.onStart, 'function'); assert.equal(typeof list.props.onEnd, 'function');
  assert.equal(list.props.scroll, true, 'use ancestor discovery until the DOM scroller mounts');
  assert.equal(list.props['force-auto-scroll-fallback'], true, 'desktop native DnD must use Sortable auto-scroll');
  assert.equal(list.props['bubble-scroll'], true, 'short screens may scroll the model stack');
  assert.equal(list.props['scroll-fn'], r.run('scrollModelsWithinStack'), 'each scroll layer is guarded against page scrolling');
  assert.ok(list.props['scroll-sensitivity'] >= 60);
  assert.ok(list.props['scroll-speed'] > 10);
  assert.equal(list.props.list, r.run('selectedProvider.value.models'));
  const scrollTarget = {$el: {scrollTop: 620, scrollHeight: 1800, clientHeight: 360}};
  r.ctx.scrollTarget = scrollTarget;
  r.run('modelScrollList.value = scrollTarget');
  const withScroller = await render(channels, r);
  assert.equal(withScroller.nodes.find(n => n.type === draggable && classHas(n, 'model-list-scroll')).props.scroll, true,
    'discover the nearest overflow layer even when the mounted list does not overflow');
  assert.equal(css(channels, '.model-list-scroll', 1440)['overflow-y'], 'auto');
  const cards = nodes.filter(n => n.type === 'article' && classHas(n, 'model-card'));
  assert.equal(cards.length, 40);
  assert.deepEqual(cards.map(n => n.key), Array.from({length: 40}, (_, i) => `model-${i}`));
  assert.ok(html.includes('模型 0')); assert.ok(html.includes('模型 39'));
  assert.ok(html.includes('https://fixture.invalid/api')); assert.ok(html.includes('fixture-masked'));
  assert.equal(r.mounted.length, 1, 'business mounted hook is collected, never executed');
  r.run('modelSearchQuery.value = "model-39"');
  const searched = await render(channels, r);
  const searchedList = searched.nodes.find(n => n.type === draggable && classHas(n, 'model-list-scroll'));
  assert.equal(searchedList.props.disabled, true);
  assert.deepEqual(Array.from(searchedList.props.list, model => model.id), ['model-39']);
  const searchedCards = searched.nodes.filter(n => n.type === 'article' && classHas(n, 'model-card'));
  assert.deepEqual(searchedCards.map(n => n.key), ['model-39'], 'search renders only matching models');
  assert.ok(searched.nodes.filter(n => n.type === 'button' && classHas(n, 'model-drag')).every(n => n.props.disabled));
  r.run('modelSearchQuery.value = ""');
  const cleared = await render(channels, r);
  assert.deepEqual(cleared.nodes.filter(n => n.type === 'article' && classHas(n, 'model-card')).map(n => n.key),
    Array.from({length: 40}, (_, i) => `model-${i}`), 'clearing search restores original order');
});

test('real Sortable touch/pointer startup ignores ordinary card content; only configured handle prepares a drag', () => {
  const require = createRequire(import.meta.url);
  const Sortable = createRequire(require.resolve('vuedraggable'))('sortablejs');
  const list = {nodeType:1, tagName:'DIV', getElementsByTagName: () => []};
  const card = {nodeType:1, tagName:'ARTICLE', parentNode:list, matches: s => s === '*' || s === '[data-draggable]'};
  const content = {nodeType:1, tagName:'DIV', parentNode:card, matches: () => false};
  const handle = {nodeType:1, tagName:'BUTTON', parentNode:card, matches: s => s === '.model-drag'};
  const handleAttribute = node(channels, 'model-list-scroll').props.find(p => p.name === 'handle').value.content;
  let prepared = 0;
  const sortable = {el:list, options:{draggable:'>[data-draggable]',handle:handleAttribute,disabled:false}, _prepareDragStart() { prepared++; }};
  for (const type of ['touchstart', 'pointerdown']) {
    for (const [target, expected] of [[content, 0], [handle, 1]]) {
      prepared = 0; let prevented = 0;
      const event = {type, target, cancelable:true, button:0, pointerType:'touch', preventDefault() { prevented++; }};
      if (type === 'touchstart') event.touches = [{target}];
      Sortable.prototype._onTapStart.call(sortable, event);
      assert.equal(prepared, expected); assert.equal(prevented, 0);
    }
  }
  sortable.options.disabled = true; prepared = 0;
  Sortable.prototype._onTapStart.call(sortable, {type:'pointerdown',target:handle,pointerType:'touch',button:0,cancelable:true});
  assert.equal(prepared, 0);
});

// Load the installed Sortable library in an isolated DOM/timer realm.
// Rectangles are deterministic inputs, not browser layout or iOS keyboard claims.
function sortableScrollRealm({listTop = 398, listBottom = 540, listScrollHeight = 3000, desktop = false} = {}) {
  const intervals = new Map(), timeouts = new Map(); let nextTimer = 0;
  function element(name, top, bottom, scrollHeight = bottom - top) {
    return {
      nodeType: 1, nodeName: name, tagName: name, parentNode: null, style: {}, children: [], draggable: true,
      scrollTop: 0, scrollLeft: 0, scrollHeight, scrollWidth: 320, clientHeight: bottom - top, clientWidth: 320,
      currentStyle: {overflowY: 'auto', overflowX: 'hidden', position: 'static', transform: 'none'},
      getBoundingClientRect: () => ({top, bottom, left: 0, right: 320, width: 320, height: bottom - top}),
      addEventListener() {}, removeEventListener() {}, getElementsByTagName: () => [], querySelectorAll: () => [],
    };
  }
  const page = element('HTML', 0, 874, 2000), settingsContent = element('DIV', 80, 874, 1900);
  const stack = element('DIV', 100, 430, 900), list = element('DIV', listTop, listBottom, listScrollHeight);
  const row = element('ARTICLE', listTop, listTop + 44);
  const providers = element('DIV', 144, 700, 1800);
  let hit = row;
  const document = {
    documentElement: page, scrollingElement: page,
    addEventListener() {}, removeEventListener() {}, elementFromPoint: () => hit,
    createElement: name => element(name.toUpperCase(), 0, 0),
  };
  const navigator = {userAgent: desktop ? 'Mozilla/5.0 (Macintosh) AppleWebKit/605.1.15 Safari/605.1.15' : 'Mozilla/5.0 (iPhone) AppleWebKit/605.1.15 Safari/604.1'};
  const window = {document, navigator, innerHeight: 874, innerWidth: 320, devicePixelRatio: 1,
    addEventListener() {}, removeEventListener() {}, getComputedStyle: el => el.currentStyle};
  document.defaultView = window;
  document.body = element('BODY', 0, 874); document.body.parentNode = page;
  settingsContent.parentNode = document.body; stack.parentNode = settingsContent; list.parentNode = stack; row.parentNode = list;
  providers.parentNode = document.body;
  const module = {exports: {}};
  const context = vm.createContext({window, document, navigator, module, exports: module.exports,
    setInterval(callback, delay) { const id = ++nextTimer; intervals.set(id, {callback, delay}); return id; },
    clearInterval(id) { intervals.delete(id); },
    setTimeout(callback, delay) { const id = ++nextTimer; timeouts.set(id, {callback, delay}); return id; },
    clearTimeout(id) { timeouts.delete(id); },
  });
  const require = createRequire(import.meta.url);
  const library = createRequire(require.resolve('vuedraggable')).resolve('sortablejs');
  vm.runInContext(fs.readFileSync(library, 'utf8'), context, {filename: library});
  return {Sortable: module.exports, list, row, stack, settingsContent, page, providers,
    setHit(element) { hit = element; },
    tickThrottle() { for (const [id, timer] of [...timeouts]) if (timer.delay === 30) { timeouts.delete(id); timer.callback(); } },
    tickScroll() { for (const timer of [...intervals.values()]) if (timer.delay === 24) timer.callback(); },
    assertClean() { assert.equal(intervals.size, 0); assert.equal(timeouts.size, 0); },
  };
}

test('controlled drag scrolling accepts only the actual fifth-argument list or stack layer', () => {
  const r = channelRuntime(2), list = {}, stack = {}, page = {};
  r.ctx.target = {$el: list}; r.ctx.stack = stack;
  r.run('modelScrollList.value=markRaw(target);channelDetailStack.value=markRaw(stack)');
  const scroll = r.run('scrollModelsWithinStack');
  for (const container of [list, stack]) assert.equal(scroll(0, 18, {}, {}, container), 'continue');
  for (const container of [page, {}, null, undefined]) assert.equal(scroll(list, stack, {}, {}, container), undefined);
});

test('real Sortable autoScroll reaches the short-screen stack, retains list scrolling and never moves page ancestors', async () => {
  const scenarios = [
    {name: 'short stack with clipped inner bottom', y: 418, expectedList: 0, expectedStack: 18},
    {name: 'content-expanded list with no inner overflow', listTop: 220, listBottom: 1500, listScrollHeight: 1280, y: 418, expectedList: 0, expectedStack: 18},
    {name: 'ordinary inner-list edge', listTop: 200, listBottom: 430, y: 418, expectedList: 18, expectedStack: 18},
    {name: 'other scrollable ancestors remain blocked', y: 868, expectedList: 0, expectedStack: 0, forbiddenLayers: true},
  ];
  for (const scenario of scenarios) for (const bubble of [false, true]) {
    const realm = sortableScrollRealm(scenario), r = channelRuntime(2), calls = [];
    r.ctx.target = {$el: realm.list}; r.ctx.stack = realm.stack;
    r.run('modelScrollList.value=markRaw(target);channelDetailStack.value=markRaw(stack)');
    const view = await render(channels, r);
    const props = view.nodes.find(n => n.type === draggable && classHas(n, 'model-list-scroll')).props;
    const sortable = new realm.Sortable(realm.list, {
      scroll: bubble ? props.scroll : realm.list, bubbleScroll: bubble && props['bubble-scroll'], forceAutoScrollFallback: props['force-auto-scroll-fallback'],
      scrollSensitivity: props['scroll-sensitivity'], scrollSpeed: props['scroll-speed'],
      scrollFn(...args) { calls.push(args[4]); return props['scroll-fn'](...args); },
      forceFallback: true,
    });
    // Ghost motion is outside this scroll regression; execute the real plugin.
    sortable._onTouchMove = () => {};
    realm.Sortable.active = sortable; realm.Sortable.dragged = realm.row;
    const event = {type: 'touchmove', touches: [{clientX: 160, clientY: scenario.y}]};
    try {
      sortable.scroll._handleAutoScroll(event, true);
      realm.tickScroll();
      assert.equal(realm.list.scrollTop, scenario.expectedList, `${scenario.name}: list`);
      assert.equal(realm.stack.scrollTop, bubble ? scenario.expectedStack : 0, `${scenario.name}: stack`);
      assert.equal(realm.settingsContent.scrollTop, 0, `${scenario.name}: settings wrapper`);
      assert.equal(realm.page.scrollTop, 0, `${scenario.name}: page`);
      if (bubble && scenario.forbiddenLayers) {
        assert.ok(calls.includes(realm.settingsContent), 'the real library attempts the settings ancestor');
        assert.ok(calls.includes(realm.page), 'the real library attempts the page layer, and the component rejects it');
      }
    } finally {
      sortable.scroll.drop(); sortable.scroll.nulling();
      realm.assertClean();
    }
  }
});

test('continuous native/fallback drag events retain the scroll guard across edge and direction changes', async () => {
  for (const fallback of [false, true]) for (const forbidden of [false, true]) {
    const realm = sortableScrollRealm({desktop: true, listTop: 200, listBottom: 430});
    const r = channelRuntime(2), calls = [];
    r.ctx.target = {$el: realm.list}; r.ctx.stack = realm.stack;
    r.run('modelScrollList.value=markRaw(target);channelDetailStack.value=markRaw(stack)');
    const view = await render(channels, r);
    const props = view.nodes.find(n => n.type === draggable && classHas(n, 'model-list-scroll')).props;
    const sortable = new realm.Sortable(realm.list, {
      scroll: props.scroll, bubbleScroll: props['bubble-scroll'], forceAutoScrollFallback: props['force-auto-scroll-fallback'],
      scrollSensitivity: props['scroll-sensitivity'], scrollSpeed: props['scroll-speed'], forceFallback: fallback,
      scrollFn(...args) { calls.push(args[4]); return props['scroll-fn'](...args); },
    });
    sortable._onTouchMove = () => {};
    realm.Sortable.active = sortable; realm.Sortable.dragged = realm.row;
    assert.equal(sortable.nativeDraggable, !fallback);
    if (forbidden) realm.setHit(realm.providers);
    const move = y => {
      realm.tickThrottle();
      const point = {clientX: forbidden ? 4 : 160, clientY: y};
      sortable.scroll._handleAutoScroll(fallback ? {type: 'touchmove', touches: [point]} : {type: 'dragover', ...point}, fallback);
      realm.tickScroll();
    };
    try {
      move(forbidden ? 400 : 300); // no edge velocity, but fixes the same hit root
      move(forbidden ? 686 : 418); // new timer must still ask the guard
      if (forbidden) {
        assert.ok(calls.includes(realm.providers));
        assert.equal(realm.providers.scrollTop, 0, 'sibling channel list remains still');
        assert.equal(realm.list.scrollTop, 0); assert.equal(realm.stack.scrollTop, 0);
      } else {
        assert.equal(realm.list.scrollTop, 18); assert.equal(realm.stack.scrollTop, 18);
        move(214); // reverse direction without changing the hit root
        assert.equal(realm.list.scrollTop, 0, 'allowed list still scrolls upward');
      }
      assert.equal(realm.settingsContent.scrollTop, 0); assert.equal(realm.page.scrollTop, 0);
    } finally {
      sortable.scroll.drop(); sortable.scroll.nulling(); realm.assertClean();
    }
  }
});

test('vuedraggable reordering persists the same selected channel and full ID order through the original end handler (mock API only)', async () => {
  const calls = [];
  const r = channelRuntime(3);
  r.ctx.Api = {reorderChannelModels: async (...args) => { calls.push(args); return {ok:true}; }};
  r.ctx.loaded = [];
  r.run('loadList = async name => loaded.push(name)');
  const {nodes} = await render(channels, r);
  const list = nodes.find(n => n.type === draggable && classHas(n, 'model-list-scroll'));
  const owner = {list:list.props.list};
  owner.alterList = draggable.methods.alterList.bind(owner);
  draggable.methods.updatePosition.call(owner, 0, 2);
  await list.props.onEnd();
  assert.equal(calls[0][0], 'fixture-channel');
  assert.deepEqual(Array.from(calls[0][1]), ['model-1', 'model-2', 'model-0']);
  assert.deepEqual(r.ctx.loaded, ['fixture-channel']);
});

test('compiled toolbar and model actions preserve target row and busy bindings (event spies only)', async () => {
  const r = channelRuntime(1), calls = [];
  const handlers = ['refreshModelsDev','loadList','openCreateProvider','testChannel','openEditProvider','removeProvider','openModelsDevBatch','openCreateModel','testModel','openEditModel','removeModel','syncModelFromModelsDev','copyModelMetadata','pasteCopiedMetadataToModel','confirmSetPrimary','confirmSetDefaultThinkingLevel'];
  r.ctx.record = (name, ...args) => calls.push([name, ...args]);
  for (const name of handlers) r.run(`${name} = (...args) => record('${name}', ...args)`);
  r.run('canPasteMetadataTo = () => true');
  const {nodes} = await render(channels, r);
  for (const n of nodes.filter(n => n.type === 'button' && n.props.onClick)) {
    if (classHas(n, 'mobile-channel-pill') || classHas(n, 'provider-main')) continue;
    n.props.onClick();
  }
  for (const name of handlers) assert.ok(calls.some(c => c[0] === name), name);
  const row = r.run('selectedProvider.value.models[0]');
  for (const name of handlers.slice(8)) assert.ok(calls.filter(c => c[0] === name).every(c => c[1] === row), name);
  r.run('loading.value = true; modelsDevRefreshing.value = true; modelsDevSyncing.value = true; batchModelsDevLoading.value = true; testing["channel:fixture-channel"] = true; testing["model:fixture-channel/model-0"] = true');
  const busy = await render(channels, r);
  for (const c of ['channels-refresh', 'channels-metadata']) assert.equal(busy.nodes.find(n => n.type === 'button' && classHas(n, c)).props.disabled, true);
  assert.match(busy.html, /测试中…/); assert.match(busy.html, /匹配中…/);
});
