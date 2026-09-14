import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import * as Vue from 'vue';
import {parse} from '@vue/compiler-sfc';
import {parse as parseJs} from '@babel/parser';

const clone = value => JSON.parse(JSON.stringify(value));
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return {promise, resolve, reject}; };
const settle = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); await Vue.nextTick(); };

// Execute the real SFC setup and handlers with Vue's actual reactive/watch state.
// Only imports, network, notifications, mount hooks and timers are substituted;
// no browser, production service, or copy of the save algorithm is involved.
function editor(name, overrides = {}) {
  const descriptor = parse(fs.readFileSync(new URL(`${name}View.vue`, import.meta.url), 'utf8')).descriptor;
  const source = descriptor.scriptSetup.content;
  const ast = parseJs(source, {sourceType: 'module'});
  const code = ast.program.body.filter(n => n.type !== 'ImportDeclaration').map(n => source.slice(n.start, n.end)).join('\n');
  const notices = [], unmount = [], requests = [];
  let confirmation = async () => {}, confirms = 0;
  const api = new Proxy(overrides, {get(target, method) {
    return (...args) => {
      requests.push({method, args: clone(args)});
      if (method in target) return target[method](...args);
      return Promise.resolve({ok: true, items: []});
    };
  }});
  const timers = new Map(); let timerId = 0;
  const context = vm.createContext({...Vue,
    onMounted: () => {}, onBeforeUnmount: fn => unmount.push(fn),
    defineProps: () => ({activeType: 'memory'}), defineEmits: () => () => {},
    Api: api, apiError: error => error.message || String(error),
    ElMessage: Object.fromEntries(['success', 'warning', 'error', 'info'].map(kind => [kind, text => notices.push({kind, text})])),
    ElMessageBox: {confirm: (...args) => { confirms++; return confirmation(...args); }},
    encode: value => [...String(value)], pinyin: value => [value],
    MarkdownIt: class { constructor() { this.renderer = {rules: {}}; this.utils = {}; } render(value) { return value; } },
    hljs: {getLanguage: () => false},
    setTimeout(fn) { const id = ++timerId; timers.set(id, fn); return id; }, clearTimeout(id) { timers.delete(id); },
    performance: {now: () => 0},
  });
  vm.runInContext(code, context);
  return {
    run: code => vm.runInContext(code, context), get: name => vm.runInContext(name, context),
    set(name, value) { context.input = value; vm.runInContext(`${name} = input`, context); },
    notices, requests, unmount() { unmount.forEach(fn => fn()); },
    confirm(fn) { confirmation = fn; }, get confirms() { return confirms; },
  };
}
function template(overrides = {}) {
  const rows = [{id: 1, name: 'one', content: 'stored', is_active: 0, is_agent_active: 0}, {id: 2, name: 'two', content: 'other', is_active: 0, is_agent_active: 0}];
  const e = editor('Template', {templates: async () => ({items: clone(rows)}), ...overrides});
  e.set('templates.value', clone(rows));
  e.run('select(templates.value[0]); editing.value.content = "submitted"');
  return {e, rows};
}

for (const [action, flag] of [['save', null], ['activate', 'is_active'], ['activateAgent', 'is_agent_active']]) {
  test(`template ${action} preserves edits made during save and list refresh, and only commits its submitted baseline`, async () => {
    const write = deferred(), read = deferred();
    const {e} = template({updateTemplate: () => write.promise, templates: () => read.promise});
    const pending = e.run(`${action}()`);
    await e.run(`${action}()`);
    assert.equal(e.requests.filter(r => r.method === 'updateTemplate').length, 1);
    assert.equal(e.get('saving.value'), true);
    e.run('editing.value.content = "new draft during write"');
    write.resolve({ok: true}); await settle();
    e.run('editing.value.content = "new draft during refresh"');
    read.resolve({items: [{...e.requests[0].args[1], updated_at: 10}]}); await pending;
    assert.equal(e.get('editing.value.content'), 'new draft during refresh');
    assert.equal(JSON.parse(e.get('original.value')).content, 'submitted');
    assert.equal(e.run('dirty()'), true);
    assert.equal(e.get('saving.value'), false);
    if (flag) assert.equal(e.get(`editing.value.${flag}`), 1);
    assert.match(e.notices[0].text, /后续修改仍未保存/);
    e.unmount();
  });
}

test('template save refresh preserves input started only after the write succeeded', async () => {
  const read = deferred();
  const {e} = template({updateTemplate: async () => ({ok: true}), templates: () => read.promise});
  const pending = e.run('save()'); await settle();
  assert.equal(e.run('dirty()'), false);
  e.run('editing.value.name = "new name during refresh"');
  read.resolve({items: [{id: 1, name: 'one', content: 'submitted'}]}); await pending;
  assert.equal(e.get('editing.value.name'), 'new name during refresh');
  assert.equal(e.run('dirty()'), true); e.unmount();
});

for (const stage of ['write', 'refresh']) {
  test(`template old ${stage} response cannot replace a newly selected editor`, async () => {
    const gate = deferred();
    const {e, rows} = template(stage === 'write' ? {updateTemplate: () => gate.promise} : {updateTemplate: async () => ({ok: true}), templates: () => gate.promise});
    const pending = e.run('save()'); await settle();
    await e.run('selectById(2)');
    const baseline = e.get('original.value');
    gate.resolve(stage === 'write' ? {ok: true} : {items: rows}); await pending;
    assert.equal(e.get('editing.value.id'), 2);
    assert.equal(e.get('original.value'), baseline); e.unmount();
  });
}

test('template unsuccessful write and successful write with failed refresh are distinguished', async () => {
  for (const committed of [false, true]) {
    const {e} = template({updateTemplate: async () => { if (!committed) throw Error('write failed'); return {ok: true}; }, templates: async () => { throw Error('refresh failed'); }});
    const before = e.get('original.value'); await e.run('save()');
    assert.equal(e.get('saving.value'), false);
    assert.equal(e.get('editing.value.content'), 'submitted');
    if (committed) { assert.equal(e.run('dirty()'), false); assert.ok(e.notices.some(n => n.kind === 'warning' && /已保存/.test(n.text))); }
    else { assert.equal(e.get('original.value'), before); assert.equal(e.requests.length, 1); assert.ok(e.notices.some(n => n.kind === 'error')); }
    e.unmount();
  }
});

test('new template dirty cancellation never creates or discards; confirmation is single-flight', async () => {
  const answer = deferred(); const {e} = template(); e.confirm(() => answer.promise);
  const before = clone(e.get('editing.value'));
  const pending = e.run('newTpl()'); await e.run('newTpl()');
  assert.equal(e.confirms, 1); assert.equal(e.requests.length, 0);
  answer.reject('cancel'); await pending;
  assert.deepEqual(clone(e.get('editing.value')), before); assert.equal(e.requests.length, 0);
  assert.equal(e.get('changingTemplate.value'), false); e.unmount();
});

for (const dirty of [false, true]) {
  test(`new template ${dirty ? 'confirmed dirty' : 'clean'} switches normally and creates once`, async () => {
    const created = {id: 3, name: 'new', content: 'new content'};
    const {e} = template({createTemplate: async () => ({ok: true, id: 3}), templates: async () => ({items: [created]})});
    if (!dirty) e.run('original.value = JSON.stringify(editing.value)');
    await e.run('newTpl()');
    assert.equal(e.get('editing.value.id'), 3); assert.equal(e.run('dirty()'), false);
    assert.equal(e.confirms, dirty ? 1 : 0);
    assert.equal(e.requests.filter(r => r.method === 'createTemplate').length, 1); e.unmount();
  });
}

for (const stage of ['confirmation', 'creation', 'refresh']) {
  test(`new template never discards input changed during ${stage}`, async () => {
    const gate = deferred();
    const {e} = template({createTemplate: () => stage === 'creation' ? gate.promise : Promise.resolve({ok: true, id: 3}), templates: () => stage === 'refresh' ? gate.promise : Promise.resolve({items: [{id: 3, content: 'new'}]})});
    if (stage === 'confirmation') e.confirm(() => gate.promise);
    const pending = e.run('newTpl()'); await settle();
    e.run('editing.value.content = "latest typing"');
    gate.resolve(stage === 'creation' ? {ok: true, id: 3} : stage === 'refresh' ? {items: [{id: 3, content: 'new'}]} : undefined);
    await pending;
    assert.equal(e.get('editing.value.id'), 1); assert.equal(e.get('editing.value.content'), 'latest typing'); assert.equal(e.run('dirty()'), true);
    if (stage === 'confirmation') assert.equal(e.requests.length, 0); e.unmount();
  });
}

test('superseded template list requests and unmounted save responses cannot mutate current editor', async () => {
  const first = deferred(), second = deferred(); let count = 0;
  const {e} = template({templates: () => (++count === 1 ? first.promise : second.promise)});
  const a = e.run('load()'), b = e.run('load()');
  second.resolve({items: [{id: 1, name: 'latest', content: 'latest'}]}); await b;
  first.resolve({items: [{id: 1, name: 'stale', content: 'stale'}]}); await a;
  assert.equal(e.get('editing.value.content'), 'latest'); assert.equal(e.get('templates.value[0].name'), 'latest'); e.unmount();
  const write = deferred(); const {e: removed} = template({updateTemplate: () => write.promise});
  const before = removed.get('original.value'); const pending = removed.run('save()'); removed.unmount(); write.resolve({ok: true}); await pending;
  assert.equal(removed.get('original.value'), before); assert.equal(removed.requests.length, 1); assert.equal(removed.notices.length, 0);
});

const assets = [
  {name: 'Docs', create: 'createDoc', update: 'updateDoc', field: 'content', mutate: 'editing.value.content = "new draft"', initialize: 'editing.value.name = "doc"; editing.value.content = "submitted"'},
  {name: 'Memory', create: 'createEntry', update: 'updateEntry', field: 'body', mutate: 'editing.value.body = "new draft"', initialize: 'editing.value.title = "note"; editing.value.body = "submitted"'},
  {name: 'Secrets', create: 'createSecret', update: 'updateSecret', field: 'kvJson', mutate: 'editing.value.kv[0].value = "new draft"', initialize: 'editing.value.name = "secret"; editing.value.kv = [{key:"value", value:"submitted"}]'},
];
for (const asset of assets) {
  async function setup(api = {}) { const e = editor(asset.name, api); await e.run('openEdit()'); e.run(asset.initialize); return e; }
  test(`${asset.name}: new asset preserves later edits, records returned ID, and next save updates rather than creates again`, async () => {
    const gate = deferred(); const e = await setup({[asset.create]: () => gate.promise, [asset.update]: async () => ({ok: true, item: {id: 99}})});
    const pending = e.run('save()'); await e.run('save()');
    assert.equal(e.requests.filter(r => r.method === asset.create).length, 1);
    e.run(asset.mutate); gate.resolve({ok: true, item: {id: 99}}); await pending;
    assert.equal(e.get('dialogOpen.value'), true); assert.equal(e.get('editing.value.id'), 99);
    const submitted = e.requests.find(r => r.method === asset.create).args[0];
    assert.match(submitted[asset.field], /submitted/); assert.doesNotMatch(submitted[asset.field], /new draft/);
    assert.notEqual(e.run('editSnapshot(editing.value)'), e.get('original.value'));
    assert.equal(JSON.parse(e.get('original.value')).id, 99);
    await e.run('save()');
    assert.equal(e.requests.filter(r => r.method === asset.create).length, 1);
    const update = e.requests.find(r => r.method === asset.update); assert.equal(update.args[0], 99); assert.match(update.args[1][asset.field], /new draft/);
    assert.equal(e.get('dialogOpen.value'), false); assert.equal(e.run('editSnapshot(editing.value)'), e.get('original.value')); e.unmount();
  });
  test(`${asset.name}: save never closes or changes a subsequently opened editor`, async () => {
    const gate = deferred(); const e = await setup({[asset.create]: () => gate.promise});
    const pending = e.run('save()'); e.run('dialogOpen.value = false'); await e.run('openEdit()'); e.run(asset.initialize); e.run(asset.mutate);
    const before = clone(e.get('editing.value')), baseline = e.get('original.value');
    gate.resolve({ok: true, item: {id: 99}}); await pending;
    assert.deepEqual(clone(e.get('editing.value')), before); assert.equal(e.get('original.value'), baseline); assert.equal(e.get('dialogOpen.value'), true); e.unmount();
  });
  test(`${asset.name}: write failure leaves draft recoverable and allows explicit retry`, async () => {
    let fail = true; const e = await setup({[asset.create]: async () => { if (fail) throw Error('not saved'); return {ok: true, item: {id: 99}}; }});
    const baseline = e.get('original.value'); await e.run('save()');
    assert.equal(e.get('dialogOpen.value'), true); assert.equal(e.get('original.value'), baseline); assert.equal(e.get('saving.value'), false);
    fail = false; await e.run('save()'); assert.equal(e.get('dialogOpen.value'), false); e.unmount();
  });
  test(`${asset.name}: unmounted editor ignores successful write response`, async () => {
    const gate = deferred(); const e = await setup({[asset.create]: () => gate.promise});
    const baseline = e.get('original.value'), pending = e.run('save()'); e.unmount(); gate.resolve({ok: true, item: {id: 99}}); await pending;
    assert.equal(e.get('original.value'), baseline); assert.equal(e.get('editing.value.id'), null); assert.equal(e.requests.length, 1); assert.equal(e.notices.length, 0);
  });
}
