import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import * as Vue from 'vue';
import {renderToString} from 'vue/server-renderer';
import {parse, compileScript, compileTemplate, compileStyle} from '@vue/compiler-sfc';
import {baseParse} from '@vue/compiler-dom';
import postcss from 'postcss';
import {ElButton, ElCheckbox, ElSwitch} from 'element-plus';
import {Plus, Refresh, InfoFilled} from '@element-plus/icons-vue';

const read = path => fs.readFileSync(new URL(path, import.meta.url), 'utf8');
const header = parse(read('./AdminPageHeader.vue')).descriptor;
const app = parse(read('../App.vue')).descriptor;
const names = ['Memory', 'Secrets', 'Docs', 'Skills', 'Mcp'];
const pages = Object.fromEntries(names.map(n => [n, parse(read(`../views/${n}View.vue`)).descriptor]));
const walk = nodes => (nodes || []).flatMap(n => [n, ...walk(Array.isArray(n.children) ? n.children : []), ...walk(n.component?.subTree ? [n.component.subTree] : [])]);
const ast = sfc => walk(baseParse(sfc.template.content).children);
const hasClass = (n, cls) => n.props?.some?.(p => p.name === 'class' && p.value?.content.split(/\s+/).includes(cls));
function css(sfc, selector, width) {
  const output = {};
  postcss.parse(sfc.styles.map(s => s.content).join('\n')).walkRules(rule => {
    if (!rule.selectors.includes(selector)) return;
    for (let p = rule.parent; p; p = p.parent) if (p.type === 'atrule' && p.name === 'media') {
      const max = p.params.match(/max-width:\s*(\d+)px/), min = p.params.match(/min-width:\s*(\d+)px/);
      if ((max && width > +max[1]) || (min && width < +min[1])) return;
    }
    rule.walkDecls(d => { output[d.prop] = d.value; });
  });
  return output;
}
function runtime(phone = true) {
  const mounted = [], unmounted = [], events = [], listeners = new Map();
  const context = vm.createContext({...Vue,
    defineProps: () => ({title:'记忆管理', subtitle:'条目统计', description:'使用说明'}),
    defineEmits: () => (...args) => events.push(args),
    useId: () => 'test', useAdminPhone: () => Vue.ref(phone),
    onMounted: fn => mounted.push(fn), onBeforeUnmount: fn => unmounted.push(fn),
    document: {addEventListener: (name, fn) => listeners.set(name, fn), removeEventListener: (name, fn) => {assert.equal(listeners.get(name), fn); listeners.delete(name);}},
  });
  vm.runInContext(header.scriptSetup.content.replace(/^import .*;\n/gm, ''), context);
  return {mounted, unmounted, events, listeners, run: code => vm.runInContext(code, context)};
}
async function render(markup, scope, components = {}) {
  const compiled = Vue.compile(markup); let tree;
  const instance = Vue.createSSRApp({render() { tree = compiled.call(this, scope, []); return tree; }});
  for (const [name, component] of Object.entries({ElButton, ElCheckbox, ElSwitch, Plus, Refresh, InfoFilled, ...components})) instance.component(name, component);
  instance.config.warnHandler = message => assert.fail(message);
  const html = await renderToString(instance);
  return {html, tree, nodes: walk([tree])};
}

test('shared header compiles and keeps one bounded 66px/60px header with neutral tokens', () => {
  const script = compileScript(header, {id:'admin-header'});
  assert.deepEqual(compileTemplate({source:header.template.content, filename:'AdminPageHeader.vue', id:'admin-header', compilerOptions:{bindingMetadata:script.bindings}}).errors, []);
  for (const style of header.styles) assert.deepEqual(compileStyle({source:style.content, filename:'AdminPageHeader.vue', id:'admin-header', scoped:true}).errors, []);
  for (const width of [320, 360, 390, 430, 760, 761, 1024, 1440]) {
    const phone = width <= 760;
    assert.equal(css(header, '.admin-page-header', width).height, phone ? 'calc(60px + env(safe-area-inset-top, 0px))' : '66px');
    assert.equal(css(header, '.admin-page-header', width).background, 'var(--ob-chat-bg)');
    assert.equal(css(header, '.admin-page-header', width).flex, 'none');
    assert.equal(css(header, '.admin-header-title', width)['font-size'], '13px');
    assert.equal(css(header, '.admin-header-title', width)['font-weight'], '500');
    assert.equal(css(header, '.admin-header-navigation', width).display, phone ? 'flex' : 'none');
    assert.equal(css(header, '.admin-header-more', width).display, phone ? 'grid' : 'none');
    if (phone) {
      assert.equal(css(header, '.admin-header-actions', width).position, 'absolute');
      assert.equal(css(header, '.admin-header-actions', width)['overflow-y'], 'auto');
    }
  }
});

test('phone actions open, focus the first control, retain toggles, close on Escape/outside/resize, and clean listeners', async () => {
  const r = runtime();
  r.run('let focused = 0; let returned = 0; root.value = {contains: target => target === "inside"}; actions.value = {querySelector: () => ({focus: () => focused++})}; moreButton.value = {focus: () => returned++};');
  r.mounted.forEach(fn => fn());
  assert.deepEqual(r.events, [['mobile-header-ready', true]]);
  await r.run('toggleMore()'); assert.equal(r.run('moreOpen.value'), true); assert.equal(r.run('focused'), 1);
  r.run('closeAfterAction({target:{closest: () => null}})'); assert.equal(r.run('moreOpen.value'), true);
  r.listeners.get('pointerdown')({target:'inside'}); assert.equal(r.run('moreOpen.value'), true);
  r.run('closeMore(true)'); assert.equal(r.run('returned'), 1); assert.equal(r.run('moreOpen.value'), false);
  await r.run('toggleMore()'); r.listeners.get('pointerdown')({target:'outside'}); assert.equal(r.run('moreOpen.value'), false);
  await r.run('toggleMore()'); r.run('isPhone.value = false'); await Vue.nextTick(); assert.equal(r.run('moreOpen.value'), false);
  await r.run('toggleMore()'); r.run('closeAfterAction({target:{closest: () => ({})}})'); assert.equal(r.run('moreOpen.value'), false);
  r.unmounted.forEach(fn => fn()); assert.equal(r.listeners.size, 0); assert.deepEqual(r.events.at(-1), ['mobile-header-ready', false]);
});

test('rendered phone disclosure has explicit state and does not duplicate controls across layouts', async () => {
  for (const phone of [true, false]) {
    const r = runtime(phone);
    const scope = Vue.proxyRefs(r.run('({props,isPhone,root,moreButton,actions,moreOpen,actionsId,toggleMore,closeMore,closeAfterAction,closeOnFocusLeave})'));
    scope.$slots = {'mobile-navigation': () => [Vue.h('button', '打开导航')], primary: () => [Vue.h('button', '新建条目')], actions: () => [Vue.h('button', '刷新')]};
    let view = await render(header.template.content, scope);
    assert.equal((view.html.match(/>刷新</g) || []).length, 1);
    assert.equal((view.html.match(/>新建条目</g) || []).length, 1);
    assert.match(view.html, /aria-expanded="false"/);
    const extra = () => view.nodes.find(n => n.props?.class?.includes?.('admin-header-actions'));
    assert.equal(extra().dirs[0].value, !phone);
    await r.run('toggleMore()'); view = await render(header.template.content, scope);
    assert.match(view.html, /aria-expanded="true"/); assert.equal(extra().dirs[0].value, true);
  }
});

test('all five pages forward navigation/readiness and retain existing primary, refresh and toggle handlers', async () => {
  const HeaderSlots = {props:['title','subtitle','description'], emits:['mobile-header-ready'], render() {return Vue.h('header', [this.title, this.subtitle, ...Object.values(this.$slots).flatMap(slot => slot())]);}};
  for (const name of names) {
    const node = ast(pages[name]).find(n => n.tag === 'AdminPageHeader'); assert.ok(node, name);
    const called = [], scope = Vue.reactive({
      isAdminPhone:false, showArchived:false, showSecretValues:false, archivedCount:7, enabledCount:12, expandedEntries:[{},{}], expandedTokens:107, totalTokens:4096,
      summary:{enabled:true}, mcpToggling:false, settingsEntryAvailable:true, settingsHref:'/settings?section=system-settings&setting=mcp.installDir', mcpSettingPaths:['mcp.installDir'], loading:false, reloading:false, installOpen:false,
      emit:(...args) => called.push(args), openEdit:row => called.push(['edit', row]), refresh:() => called.push(['refresh']), load:() => called.push(['load']), clearSelection:() => called.push(['clear']), reloadSkills:() => called.push(['reloadSkills']), reloadMcp:() => called.push(['reloadMcp']), setMcpEnabled:value => called.push(['setMcpEnabled', value]),
      $slots:{'mobile-navigation':() => [Vue.h('button', {'aria-label':'打开导航'}, '导航')]},
    });
    const view = await render(node.loc.source, scope, {AdminPageHeader:HeaderSlots});
    assert.match(view.html, /aria-label="打开导航"/);
    view.tree.props.onMobileHeaderReady(true); assert.deepEqual(called.shift(), ['mobile-header-ready', true]);
    for (const button of view.nodes.filter(n => n.type === ElButton)) button.props.onClick();
    if (['Memory','Secrets','Docs'].includes(name)) {
      assert.deepEqual(called, [['refresh'],['edit',null]]);
      const checkbox = view.nodes.find(n => n.type === ElCheckbox);
      checkbox.props['onUpdate:modelValue'](true); assert.equal(scope.showArchived,true);
      if (checkbox.props.onChange) {checkbox.props.onChange(); assert.deepEqual(called.slice(-2), [['load'],['clear']]);}
    }
    if (name === 'Secrets') {view.nodes.find(n => n.type === ElSwitch).props['onUpdate:modelValue'](true); assert.equal(scope.showSecretValues,true);}
    if (name === 'Skills') {assert.equal(scope.installOpen,true); assert.deepEqual(called,[['reloadSkills']]);}
    if (name === 'Mcp') {
      assert.deepEqual(called,[['load'],['reloadMcp']]);
      view.nodes.find(n => n.type === ElSwitch).props.onChange(false); assert.deepEqual(called.at(-1),['setMcpEnabled',false]);
      assert.match(view.html, /href="\/settings\?section=system-settings&amp;setting=mcp.installDir"/);
      assert.doesNotMatch(node.loc.source, /MCP 是 OpenBear 接入/);
      assert.match(pages.Mcp.template.content, /<details class="mcp-help admin-inline-help">/);
    }
    if (name === 'Memory') for (const text of ['启用 12 条','展开 2 条','107 tk','4,096 tk']) assert.ok(view.html.includes(text),text);
  }
});

test('app replaces the fallback only for mounted combined headers and resets readiness before lazy navigation', async () => {
  const shell = ast(app).find(n => hasClass(n,'app-shell'));
  const expression = shell.props.find(p => p.name === 'bind' && p.arg?.content === 'class').exp.content;
  const classes = new Function('active','pageHeaderReady',`return (${expression})`);
  for (const key of ['memory','secrets','docs','skills','mcp']) {
    assert.equal(classes(key,false)['is-admin'],false);
    assert.equal(classes(key,true)['is-admin'],true);
  }
  for (const key of ['console','settings','statistics','logs']) assert.equal(classes(key,true)['is-admin'],false);
  for (const width of [320,390,760]) {
    assert.equal(css(app,'.app-shell.is-admin .mobile-app-bar',width).display,'none');
    assert.equal(css(app,'.app-shell.is-admin .app-main',width)['padding-top'],'0');
  }
  assert.equal(css(app,'.app-shell.is-admin .mobile-app-bar',1440).display,undefined);
  const reset = app.scriptSetup.content.match(/watch\(active, \(\) => \{ pageHeaderReady.value = false; \}\);/)?.[0]; assert.ok(reset);
  const context = vm.createContext({watch:Vue.watch,active:Vue.ref('memory'),pageHeaderReady:Vue.ref(true)});
  vm.runInContext(reset,context); context.active.value='mcp'; await Vue.nextTick(); assert.equal(context.pageHeaderReady.value,false);
  for (const node of ast(app).filter(n => n.tag === 'MemoryView' || (n.tag === 'component' && n.loc.source.includes(':is="activeView"')))) {
    assert.match(node.loc.source, /#mobile-navigation/); assert.match(node.loc.source, /@mobile-header-ready="pageHeaderReady = \$event"/);
  }
});
