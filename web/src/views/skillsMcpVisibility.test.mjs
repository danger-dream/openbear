import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import * as Vue from 'vue';
import { parse } from '@vue/compiler-sfc';
import { parse as parseJs } from '@babel/parser';

function page(name, api = {}, search = '') {
  const descriptor = parse(fs.readFileSync(new URL(`${name}View.vue`, import.meta.url), 'utf8')).descriptor;
  const source = descriptor.scriptSetup.content;
  const ast = parseJs(source, { sourceType: 'module' });
  const code = ast.program.body.filter(n => n.type !== 'ImportDeclaration').map(n => source.slice(n.start, n.end)).join('\n');
  const context = vm.createContext({usePageSubject:()=>{}, usePageAction:()=>{}, useResourceChanges:()=>{},  ...Vue, authMode: Vue.ref('controller'), onMounted: () => {}, defineEmits: () => () => {}, Api: api, apiError: e => e.message,
    ElMessage: { error: () => {}, success: () => {} }, ElMessageBox: {},
    window: { location: { search } }, URLSearchParams });
  vm.runInContext(code, context);
  return { template: descriptor.template.content, run: code => vm.runInContext(code, context) };
}

test('Skills direct navigation keeps its page instead of auto-opening a conversation', () => {
  const app = parse(fs.readFileSync(new URL('../App.vue', import.meta.url), 'utf8')).descriptor;
  const binding = app.template.content.match(/<ConversationTree\b[\s\S]*?:auto-open-selected="([^"]+)"/);
  assert.ok(binding);
  for (const active of ['skills', 'instances']) {
    assert.equal(vm.runInNewContext(binding[1], {active}), false, active);
  }
  assert.equal(vm.runInNewContext(binding[1], {active: 'console'}), true);
});

test('Skills hides only disabled by default and permits explicit display and status filtering', () => {
  const p = page('Skills');
  p.run('items.value = [{name:"active",status:"enabled"},{name:"paused",status:"disabled"},{name:"missing",status:"dependency_missing"}]');
  assert.deepEqual(Array.from(p.run('filteredItems.value.map(row => row.name)')), ['active', 'missing']);
  p.run('showDisabled.value = true; statusFilter.value = "disabled"');
  assert.deepEqual(Array.from(p.run('filteredItems.value.map(row => row.name)')), ['paused']);
  assert.match(p.template, /v-model="showDisabled">显示停用/);
});

test('MCP hides disabled cards, exposes them on request and links to an existing settings item', async () => {
  const p = page('Mcp', { settingsSpecs: async () => ({ok: true, groups: [{key: 'mcp', paths: ['mcp.installDir']}]}) });
  p.run('status.value = {ok:true, settingsAvailable:true, servers:[{key:"on",enabled:true},{key:"off",enabled:false}], tools:[], prompts:[]}');
  assert.deepEqual(Array.from(p.run('filteredServerCards.value.map(card => card.key)')), ['on']);
  p.run('showDisabled.value = true');
  assert.deepEqual(Array.from(p.run('filteredServerCards.value.map(card => card.key)')), ['on', 'off']);
  assert.match(p.template, /v-model="showDisabled">显示停用/);
  assert.equal(p.run('settingsEntryAvailable.value'), false);
  await p.run('loadSettingsSpecHint()');
  assert.equal(p.run('settingsHref.value'), '/settings?section=system-settings&setting=mcp.installDir');
  assert.match(p.template, /:href="settingsHref"/);
  assert.doesNotMatch(p.template, /href="\/settings"/);
});

test('MCP setting deep link reaches its actual visible item within system settings', async () => {
  const app = fs.readFileSync(new URL('../App.vue', import.meta.url), 'utf8');
  const hub = fs.readFileSync(new URL('SettingsHubView.vue', import.meta.url), 'utf8');
  assert.match(app, /settings: "\/settings"/);
  assert.match(hub, /key: "system-settings"[^\n]*component: SettingsView/);
  const appSetup = parse(app).descriptor.scriptSetup.content;
  const appAst = parseJs(appSetup, {sourceType:'module'});
  const routeNode = appAst.program.body.find(node => node.type === 'FunctionDeclaration' && node.id.name === 'routeForCurrentState');
  const routeContext = vm.createContext({ URLSearchParams, active: Vue.ref('settings'), settingsSection: Vue.ref('system-settings'),
    pageToPath: {settings:'/settings'}, window: {location: {pathname:'/settings', search:'?section=system-settings&setting=mcp.installDir'}} });
  vm.runInContext(appSetup.slice(routeNode.start, routeNode.end), routeContext);
  assert.equal(vm.runInContext('routeForCurrentState()', routeContext), '/settings?section=system-settings&setting=mcp.installDir');
  const p = page('Settings', {
    settingsSpecs: async () => ({ok:true, domains:[{key:'agent', sections:[{paths:['agent.foo']}]},{key:'tools', sections:[{key:'mcp', paths:['mcp.installDir']}]}], specs:{'mcp.installDir':{path:'mcp.installDir', title:'MCP 安装目录', kind:'str'}}}),
    settings: async () => ({ok:true, values:{'mcp.installDir':'./mcp-servers'}}),
    rathOptions: async () => ({models:[]}),
  }, '?section=system-settings&setting=mcp.installDir');
  await p.run('load()');
  assert.equal(p.run('activeDomain.value'), 'tools');
  assert.equal(p.run('query.value'), 'mcp.installDir');
  assert.deepEqual(Array.from(p.run('visibleSections.value.flatMap(section => section.specs.map(spec => spec.path))')), ['mcp.installDir']);
});

test('notifications have a dedicated domain; local device settings remain discoverable without Telegram search matches', async () => {
  const p = page('Settings', {
    settingsSpecs: async () => ({ok: true, domains: [
      {key: 'agent', sections: [{key: 'agent', paths: ['agent.foo']}]},
      {key: 'notifications', title: '通知', sections: [{key: 'web_notifications', title: 'Telegram 通知', paths: ['web.taskNotifications.enabled']}]},
      {key: 'web', sections: [{key: 'web', paths: ['web.host']}]},
    ], specs: {
      'agent.foo': {path: 'agent.foo', title: 'Agent', kind: 'str'},
      'web.taskNotifications.enabled': {path: 'web.taskNotifications.enabled', title: 'Telegram 长任务通知', kind: 'bool'},
      'web.host': {path: 'web.host', title: '监听地址', kind: 'str'},
    }}),
    settings: async () => ({ok: true, values: {}}), rathOptions: async () => ({models: []}),
  });
  await p.run('load()');
  assert.equal(p.run('showDeviceNotifications.value'), false);
  p.run("activeDomain.value = 'notifications'");
  assert.equal(p.run('showDeviceNotifications.value'), true);
  assert.equal(p.run('domainSettingCount(activeDomainInfo.value)'), 2);
  assert.equal(p.run('resultCount.value'), 2);
  assert.equal(p.run('totalSettings.value'), 4);
  assert.deepEqual(Array.from(p.run('visibleSections.value.map(section => section.key)')), ['web_notifications']);
  for (const query of ['PWA', '系统通知', 'Chrome', '权限']) {
    p.run(`query.value = ${JSON.stringify(query)}`);
    assert.equal(p.run('showDeviceNotifications.value'), true);
    assert.equal(p.run('resultCount.value'), 1);
  }
  p.run("query.value = 'Telegram'");
  assert.equal(p.run('showDeviceNotifications.value'), false);
  assert.equal(p.run('resultCount.value'), 1);
  p.run("query.value = ''; activeDomain.value = 'web'");
  assert.equal(p.run('showDeviceNotifications.value'), false);
  assert.match(p.template, /<DeviceNotifications v-if="showDeviceNotifications"/);
  assert.doesNotMatch(p.template, /<DeviceNotifications v-if="section.key/);
});
