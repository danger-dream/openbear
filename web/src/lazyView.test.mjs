import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import * as Vue from 'vue';
import { parse } from '@vue/compiler-sfc';
const source = fs.readFileSync(new URL('./lazyView.js', import.meta.url), 'utf8');
const stateSource = parse(fs.readFileSync(new URL('./components/LazyViewState.vue', import.meta.url), 'utf8')).descriptor;
let stateProps, stateEmits;
vm.runInNewContext(stateSource.scriptSetup.content, { defineProps: value => { stateProps = value; }, defineEmits: value => { stateEmits = value; }, String, Boolean });
const LazyViewState = Vue.defineComponent({ props: stateProps, emits: stateEmits, render: Vue.compile(stateSource.template.content) });
const context = vm.createContext({ ...Vue, LazyViewState });
vm.runInContext(source.replace(/^import .*;\n/gm, '').replace('export function ', 'function '), context);
const { defineLazyView } = context;

// Vue's real patch/setup/unmount lifecycle, using a minimal in-memory host (not a
// browser). This detects component identity resets and invokes compiled buttons.
function mount(component) {
  const node = type => ({ type, children: [], props: {}, parent: null, text: '' });
  const renderer = Vue.createRenderer({
    createElement: node, createText: text => ({ ...node('#text'), text }), createComment: text => ({ ...node('#comment'), text }),
    setText: (node, text) => { node.text = text; }, setElementText: (node, text) => { node.children = []; node.text = text; },
    patchProp: (node, key, old, value) => { node.props[key] = value; },
    parentNode: node => node.parent,
    nextSibling: node => node.parent?.children[node.parent.children.indexOf(node) + 1] || null,
    insert(node, parent, anchor) { if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1); const at = anchor ? parent.children.indexOf(anchor) : -1; parent.children.splice(at < 0 ? parent.children.length : at, 0, node); node.parent = parent; },
    remove(node) { node.parent?.children.splice(node.parent.children.indexOf(node), 1); node.parent = null; },
  });
  const root = node('root'), app = renderer.createApp(component); app.mount(root);
  const all = parent => [parent, ...parent.children.flatMap(all)];
  return { root, app, nodes: () => all(root), text: () => all(root).map(node => node.text).join(' '), buttons: () => all(root).filter(node => node.type === 'button') };
}
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); await Vue.nextTick(); };
function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; }

test('lazy definitions do not load on chat; first navigation shows actual mac placeholder then preserves child identity, attrs and events', async () => {
  const request = deferred(), page = Vue.ref('chat'), section = Vue.ref('channels'), calls = [];
  let requests = 0, mounts = 0, unmounts = 0;
  const Settings = defineLazyView(() => { requests++; return request.promise; }, '设置');
  const Child = { props: ['section'], emits: ['section-changed'], setup(props, { emit }) {
    mounts++; Vue.onBeforeUnmount(() => unmounts++); const unsaved = Vue.ref('未保存');
    return () => Vue.h('button', { onClick: () => emit('section-changed', 'models') }, `${props.section}:${unsaved.value}`);
  } };
  const h = mount({ setup: () => () => page.value === 'chat' ? Vue.h('p', 'chat draft') : Vue.h(Settings, { section: section.value, onSectionChanged: value => calls.push(value) }) });
  await flush(); assert.equal(requests, 0); assert.match(h.text(), /chat draft/);
  page.value = 'settings'; await flush(); assert.equal(requests, 1); assert.match(h.text(), /正在加载设置/);
  request.resolve({ default: Child }); await flush(); assert.equal(mounts, 1); assert.match(h.text(), /channels:未保存/);
  section.value = 'models'; await flush(); assert.match(h.text(), /models:未保存/); assert.equal(mounts, 1); assert.equal(unmounts, 0);
  h.buttons()[0].props.onClick(); assert.deepEqual(calls, ['models']);
  page.value = 'chat'; await flush(); assert.equal(unmounts, 1);
  page.value = 'settings'; await flush(); assert.equal(requests, 1, 'module cached, not the old page instance');
  assert.equal(mounts, 2, 'normal navigation still unmounts/remounts as before; no new KeepAlive semantics'); h.app.unmount();
});

test('failed chunk displays actual old-resource warning; retry is explicit, deduplicated and never reloads or edits the route', async () => {
  let attempts = 0; const request = deferred();
  const View = defineLazyView(() => ++attempts === 1 ? Promise.reject(Error('old chunk')) : request.promise, '文档库');
  const h = mount({ render: () => Vue.h(View) }); await flush();
  assert.match(h.text(), /文档库加载失败/); assert.match(h.text(), /旧资源/); assert.match(h.text(), /保存草稿和未提交设置/);
  assert.equal(h.nodes().some(node => node.props.role === 'alert'), true); assert.equal(h.buttons().length, 1);
  const retry = h.buttons()[0].props.onClick; retry(); retry(); await flush();
  assert.equal(attempts, 2); assert.match(h.text(), /正在加载文档库/);
  request.resolve({ default: { render: () => Vue.h('p', 'loaded') } }); await flush(); assert.match(h.text(), /loaded/);
  h.app.unmount();
  // The VM intentionally has no window/location/history; failed loads and retry
  // above would throw if any force-refresh/navigation path were introduced.
});

test('late success or failure after navigation does not replace the active chat; shared request is only fetched once', async () => {
  for (const fail of [false, true]) {
    const request = deferred(), visible = Vue.ref(true); let requests = 0;
    const View = defineLazyView(() => { requests++; return request.promise; }, '设置');
    const h = mount({ setup: () => () => visible.value ? Vue.h('div', [Vue.h(View), Vue.h(View)]) : Vue.h('p', 'chat draft remains') });
    await flush(); assert.equal(requests, 1); visible.value = false; await flush();
    if (fail) request.reject(Error('offline')); else request.resolve({ default: { render: () => Vue.h('p', 'stale settings') } });
    await flush(); assert.match(h.text(), /chat draft remains/); assert.doesNotMatch(h.text(), /stale settings|失败/); h.app.unmount();
  }
});

test('App retains route behavior across management navigation/popstate and frontend preload cancellation preserves the edit', async () => {
  const appSource = fs.readFileSync(new URL('./App.vue', import.meta.url), 'utf8');
  const between = (start, end) => { const a = appSource.indexOf(start), b = appSource.indexOf(end, a + start.length); assert.ok(a >= 0 && b > a); return appSource.slice(a, b); };
  const pushes = [], location = { pathname: '/chat', search: '?id=A', href: 'https://example.invalid/chat?id=A', reload() { pushes.push('reload'); } };
  let confirmation = 'cancel'; const c = vm.createContext({ ...Vue, URL, URLSearchParams, location,
    window: { location, history: { pushState(_, __, path) { pushes.push(path); }, replaceState(_, __, path) { pushes.push(path); } } },
    ElMessageBox: { confirm: async () => { if (confirmation === 'cancel') throw 'cancel'; } },
    isLoginPath: false, sidebarOpen: Vue.ref(true), active: Vue.ref('console'), activeConversationUuid: Vue.ref('A'), memoryType: Vue.ref('identity'), settingsSection: Vue.ref('channels'), frontendRefreshRequired: Vue.ref(false),
  });
  vm.runInContext(between('const pageToPath =', 'const active =') + between('function currentRouteConversationUuid()', 'function fmtTime(')
    + 'let refreshPromptOpen = false;\n' + between('async function requestFrontendRefresh()', 'function observeFrontendVersion(')
    + between('function handlePreloadError(', 'let lastNotifiedVersionResult'), c);
  vm.runInContext("selectNav('settings')", c); assert.equal(pushes.at(-1), '/settings?section=channels'); assert.equal(c.sidebarOpen.value, false);
  location.href = 'https://example.invalid/memory?type=knowledge'; vm.runInContext('applyRouteFromLocation()', c);
  assert.equal(c.active.value, 'memory'); assert.equal(c.memoryType.value, 'knowledge');
  location.href = 'https://example.invalid/chat?id=B'; vm.runInContext('applyRouteFromLocation()', c); assert.equal(c.activeConversationUuid.value, 'B');
  let prevented = 0; c.event = { preventDefault() { prevented++; } }; vm.runInContext('handlePreloadError(event)', c); await flush();
  assert.equal(prevented, 1); assert.equal(c.frontendRefreshRequired.value, true); assert.ok(!pushes.includes('reload'));
  assert.equal(c.active.value, 'console'); assert.equal(c.activeConversationUuid.value, 'B');
  confirmation = 'accept'; await vm.runInContext('requestFrontendRefresh()', c); assert.equal(pushes.at(-1), 'reload', 'only existing explicit confirmation refreshes');
});
