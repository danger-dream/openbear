import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import * as Vue from 'vue';
import { parse } from '@vue/compiler-sfc';

// Compile App's actual page switch with stub pages, rendered by Vue's real
// KeepAlive lifecycle on a minimal in-memory host.
const template = parse(fs.readFileSync(new URL('./App.vue', import.meta.url), 'utf8')).descriptor.template.content;
const start = template.indexOf('<KeepAlive'), end = template.indexOf('</KeepAlive>') + '</KeepAlive>'.length;
const pageSwitch = template.slice(start, end);

function mount(component) {
  const node = type => ({ type, children: [], props: {}, parent: null, text: '' });
  const renderer = Vue.createRenderer({
    createElement: node, createText: text => ({ ...node('#text'), text }), createComment: text => ({ ...node('#comment'), text }),
    setText: (n, text) => { n.text = text; }, setElementText: (n, text) => { n.children = []; n.text = text; },
    patchProp: (n, key, old, value) => { n.props[key] = value; },
    parentNode: n => n.parent,
    nextSibling: n => n.parent?.children[n.parent.children.indexOf(n) + 1] || null,
    insert(n, parent, anchor) { if (n.parent) n.parent.children.splice(n.parent.children.indexOf(n), 1); const at = anchor ? parent.children.indexOf(anchor) : -1; parent.children.splice(at < 0 ? parent.children.length : at, 0, n); n.parent = parent; },
    remove(n) { n.parent?.children.splice(n.parent.children.indexOf(n), 1); n.parent = null; },
  });
  const app = renderer.createApp(component);
  app.config.warnHandler = () => {};
  app.mount(node('root'));
  return app;
}

test('the servers page survives a visit to a conversation or memory, other pages do not', async () => {
  assert.ok(start >= 0 && end > start, 'App keeps a KeepAlive around its pages');
  for (const tag of ['<ConsoleView', '<MemoryView', ':is="activeView"']) assert.ok(pageSwitch.includes(tag), `${tag} is inside the KeepAlive`);
  const counts = {};
  const page = name => Vue.defineComponent({ name, setup() {
    counts[name] = counts[name] || { mounts: 0, unmounts: 0 };
    counts[name].mounts++;
    Vue.onUnmounted(() => { counts[name].unmounts++; });
    return () => Vue.h('section', name);
  } });
  const views = { servers: page('ServersPage'), settings: page('LazyView') };
  const active = Vue.ref('servers');
  const app = mount({
    components: { ConsoleView: page('ConsoleView'), MemoryView: page('MemoryView') },
    setup: () => ({ active, activeView: Vue.computed(() => views[active.value]), activeFeatureState: 'on', isLocalConversation: () => false }),
    render: Vue.compile(pageSwitch),
  });
  for (const next of ['console', 'servers', 'memory', 'servers', 'settings', 'servers']) { active.value = next; await Vue.nextTick(); }
  assert.deepEqual(counts.ServersPage, { mounts: 1, unmounts: 0 });
  assert.deepEqual(counts.ConsoleView, { mounts: 1, unmounts: 1 });
  assert.deepEqual(counts.MemoryView, { mounts: 1, unmounts: 1 });
  assert.deepEqual(counts.LazyView, { mounts: 1, unmounts: 1 });
  app.unmount();
});
