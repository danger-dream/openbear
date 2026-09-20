import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import * as Vue from 'vue';
import {parse} from '@vue/compiler-sfc';
import {parse as parseJs} from '@babel/parser';

const clone = value => JSON.parse(JSON.stringify(value));
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return {promise, resolve, reject}; };
const snapshot = (mode = 'disabled', tools = []) => ({ok: true, enabled: true, settingsAvailable: true, agentAccessAvailable: true,
  servers: [{key: 'service/name', status: 'connected', enabled: true, agentAccess: {mode, tools}}],
  tools: [{serverKey: 'service/name', originalToolName: 'search', publicName: 'mcp__service_name__search', description: 'v1'}]});

// Run the actual SFC setup; substitute only IO/notifications/mount, not its logic.
function page(name, overrides = {}) {
  const descriptor = parse(fs.readFileSync(new URL(`${name}View.vue`, import.meta.url), 'utf8')).descriptor;
  const source = descriptor.scriptSetup.content;
  const ast = parseJs(source, {sourceType: 'module'});
  const code = ast.program.body.filter(n => n.type !== 'ImportDeclaration').map(n => source.slice(n.start, n.end)).join('\n');
  const requests = [], notices = [];
  const api = new Proxy(overrides, {get(target, method) { return (...args) => {
    requests.push({method, args: clone(args)});
    return method in target ? target[method](...args) : Promise.resolve({ok: true, items: [], groups: []});
  }; }});
  const context = vm.createContext({...Vue, onMounted: () => {}, Api: api,
    apiError: e => e.message, ElMessage: Object.fromEntries(['success', 'warning', 'error'].map(kind => [kind, text => notices.push({kind, text})])),
    ElMessageBox: {confirm: async () => {}, prompt: async () => ({})}});
  vm.runInContext(code, context);
  return {requests, notices, template: descriptor.template.content,
    run: code => vm.runInContext(code, context),
    get: code => clone(vm.runInContext(code, context)),
    set(code, value) { context.input = value; vm.runInContext(`${code} = input`, context); }};
}

async function mcp(overrides = {}) {
  const p = page('Mcp', {mcpStatus: async () => snapshot(), ...overrides});
  p.set('status.value', snapshot()); await p.run('openServer(serverCards.value[0])');
  return p;
}

test('MCP saves exact three-state policy and baseline, never connection configuration; cancel writes nothing', async () => {
  const p = await mcp({setMcpServerAgentAccess: async (_key, access) => ({ok: true, saved: true, applied: true, agentAccess: access}),
    mcpStatus: async () => snapshot('selected', ['search', 'gone'])});
  p.set('accessDraft.value', {mode: 'selected', tools: ['search', 'gone']});
  await p.run('saveAgentAccess()');
  const write = p.requests.find(r => r.method === 'setMcpServerAgentAccess');
  assert.deepEqual(write.args, ['service/name', {mode: 'selected', tools: ['search', 'gone']}, {mode: 'disabled', tools: []}]);
  assert.equal(p.get('accessDirty.value'), false);
  assert.equal(p.get('accessChoices.value.find(x => x.name === "gone").name'), 'gone');
  p.set('accessDraft.value.mode', 'all'); p.run('resetAccessDraft()');
  assert.equal(p.requests.filter(r => r.method === 'setMcpServerAgentAccess').length, 1);
  assert.equal(p.get('accessDraft.value.mode'), 'selected');
});

test('state refresh preserves dirty selections and a conflict never retries or adopts a new baseline', async () => {
  const p = await mcp({mcpStatus: async () => snapshot('all'), setMcpServerAgentAccess: async () => { throw {response: {data: {error: 'mcp_agent_access_conflict'}}}; }});
  p.set('accessDraft.value', {mode: 'selected', tools: ['missing']});
  await p.run('load()');
  assert.deepEqual(p.get('accessDraft.value'), {mode: 'selected', tools: ['missing']});
  await p.run('saveAgentAccess()');
  assert.deepEqual(p.get('accessBaseline.value'), {mode: 'disabled', tools: []});
  assert.deepEqual(p.get('accessDraft.value'), {mode: 'selected', tools: ['missing']});
  assert.equal(p.requests.filter(r => r.method === 'setMcpServerAgentAccess').length, 1);
  assert.ok(p.notices.some(n => n.text.includes('草稿已保留')));
});

test('saved-but-not-applied never reports success', async () => {
  const p = await mcp({setMcpServerAgentAccess: async () => { throw {response: {data: {saved: true, applied: false}}}; }});
  p.set('accessDraft.value.mode', 'selected');
  await p.run('saveAgentAccess()');
  assert.ok(p.notices.some(n => n.text.includes('尚未应用')));
  assert.ok(!p.notices.some(n => n.kind === 'success'));
});

test('card and drawer refresh invoke discovery API, block duplicates, then reload same-name descriptions', async () => {
  const pending = deferred();
  const p = await mcp({refreshMcpServerTools: () => pending.promise,
    mcpStatus: async () => ({...snapshot(), tools: [{...snapshot().tools[0], description: 'v2', inputSchema: {properties: {source: {enum: ['one', 'two']}}}}]})});
  assert.match(p.template, /@click="refreshServerTools\(card\)"/);
  assert.match(p.template, /@click="refreshServerTools\(drawerItem\)"/);
  p.set('accessDraft.value', {mode: 'selected', tools: ['gone']});
  const task = p.run('refreshServerTools(drawerItem.value)');
  await p.run('refreshServerTools(drawerItem.value)');
  assert.equal(p.requests.filter(r => r.method === 'refreshMcpServerTools').length, 1);
  assert.equal(p.requests.filter(r => r.method === 'mcpStatus').length, 0);
  pending.resolve({ok: true, changed: true}); await task;
  assert.equal(p.get('drawerItem.value.tools[0].description'), 'v2');
  assert.deepEqual(p.get('drawerItem.value.tools[0].inputSchema.properties.source.enum'), ['one', 'two']);
  assert.deepEqual(p.get('accessDraft.value.tools'), ['gone']);
  assert.equal(p.requests.filter(r => r.method === 'reloadMcp').length, 0);
});

test('failed discovery retains old metadata and offline services do not issue requests', async () => {
  const p = await mcp({refreshMcpServerTools: async () => { throw Error('discovery failed'); }});
  await p.run('refreshServerTools(drawerItem.value)');
  assert.equal(p.get('drawerItem.value.tools[0].description'), 'v1');
  assert.equal(p.requests.filter(r => r.method === 'mcpStatus').length, 0);
  p.set('drawerItem.value.status', 'failed');
  await p.run('refreshServerTools(drawerItem.value)');
  assert.equal(p.requests.filter(r => r.method === 'refreshMcpServerTools').length, 1);
});

test('preset directory is dynamic; invalid stored selections survive refresh and unrelated edits omit allowlist', async () => {
  const p = page('RathAgents', {rathOptions: async () => ({ok: true, tools: [{name: 'Read'}, {name: 'mcp__s__search', kind: 'mcp', serverKey: 's', originalToolName: 'search'}]})});
  p.set('agents.value', [{id: 1, name: 'native', tool_allowlist: ['mcp__old__gone'], enabled: true}]);
  p.run('openEdit(agents.value[0])'); await p.run('loadOptions()');
  assert.deepEqual(p.get('unavailableSelected.value'), ['mcp__old__gone']);
  assert.deepEqual(p.get('toolGroups.value.map(x => x.label)'), ['内置工具', 'MCP · s']);
  p.set('editing.value.name', 'changed');
  assert.deepEqual(p.get('payload()'), {name: 'changed'});
  p.set('editing.value.toolAllowlist', ['mcp__old__gone', 'mcp__s__search']);
  assert.deepEqual(p.get('payload().expectedToolAllowlist'), ['mcp__old__gone']);
  assert.match(p.template, /留空不附加预设限制/);
});

test('out-of-order and failed candidate refresh cannot clear selections or roll back newer directory', async () => {
  const old = deferred(); let calls = 0;
  const p = page('RathAgents', {rathOptions: () => ++calls === 1 ? old.promise : calls === 2 ? Promise.resolve({ok: true, tools: [{name: 'new'}]}) : Promise.reject(Error('offline'))});
  p.set('editing.value', {toolAllowlist: ['gone']});
  const first = p.run('loadOptions()'); await p.run('loadOptions()');
  old.resolve({ok: true, tools: [{name: 'old'}]}); await first;
  await p.run('loadOptions()');
  assert.deepEqual(p.get('options.value.tools'), [{name: 'new'}]);
  assert.deepEqual(p.get('editing.value.toolAllowlist'), ['gone']);
});

test('actual API wrappers use scoped PATCH and POST, not GET status or global reload', async () => {
  const source = fs.readFileSync(new URL('../api.js', import.meta.url), 'utf8');
  const ast = parseJs(source, {sourceType: 'module'});
  const declaration = ast.program.body.find(n => n.type === 'ExportNamedDeclaration' && n.declaration?.declarations?.[0]?.id.name === 'Api').declaration;
  const requests = [];
  const api = new Proxy({}, {get: (_, method) => (...args) => { requests.push({method, args}); return Promise.resolve({data: {ok: true}}); }});
  const ctx = vm.createContext({api, unwrap: response => response.data});
  vm.runInContext(source.slice(declaration.start, declaration.end), ctx);
  await vm.runInContext('Api.refreshMcpServerTools("service/name")', ctx);
  await vm.runInContext('Api.setMcpServerAgentAccess("service/name", {mode:"disabled",tools:[]}, {mode:"all",tools:[]})', ctx);
  assert.equal(requests[0].method, 'post');
  assert.equal(requests[0].args[0], '/mcp/servers/service%2Fname/refresh-tools');
  assert.equal(requests[1].method, 'patch');
  assert.equal(requests[1].args[0], '/mcp/servers/service%2Fname/agent-access');
});
