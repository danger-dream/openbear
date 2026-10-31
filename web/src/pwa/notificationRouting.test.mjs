import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import * as Vue from 'vue';
import {renderToString} from 'vue/server-renderer';
import {parse} from '@vue/compiler-sfc';
import {parse as parseJs} from '@babel/parser';
import {installPushNavigation} from './pushClient.js';

const target = '/settings?section=system-settings&domain=notifications';
const origin = 'https://bear.test';
function worker(client) {
  const handlers = {}, notices = [], opened = [];
  const self = {location: {origin}, addEventListener(type, callback) {handlers[type] = callback;},
    registration: {async showNotification(title, options) {notices.push({title, options});}},
    clients: {async matchAll() {return client ? [client] : [];}, async openWindow(url) {opened.push(url);}},
  };
  vm.runInNewContext(readFileSync(new URL('../../public/openbear-push-sw.js', import.meta.url), 'utf8'), {self, URL});
  async function event(type, data) {
    let pending;
    handlers[type]({...data, waitUntil(work) {pending = work;}});
    await pending;
  }
  return {notices, opened, event};
}
function settingsPage(win) {
  const descriptor = parse(readFileSync(new URL('../views/SettingsView.vue', import.meta.url), 'utf8')).descriptor;
  const source = descriptor.scriptSetup.content;
  const ast = parseJs(source, {sourceType: 'module'});
  const code = ast.program.body.filter(n => n.type !== 'ImportDeclaration').map(n => source.slice(n.start, n.end)).join('\n');
  const mounted = [], unmounted = [];
  const context = vm.createContext({...Vue, window: win, URLSearchParams, applyFeatureSettings: () => {},
    onMounted: callback => mounted.push(callback), onBeforeUnmount: callback => unmounted.push(callback),
    Api: {
      async settingsSpecs() {return {ok: true, domains: [
        {key: 'agent', sections: [{key: 'agent', paths: ['agent.foo']}]},
        {key: 'notifications', sections: [{key: 'telegram', paths: ['web.taskNotifications.enabled']}]},
      ], specs: {'web.taskNotifications.enabled': {path: 'web.taskNotifications.enabled', title: 'Telegram 通知', kind: 'bool'}}};},
      async settings() {return {ok: true, values: {}};}, async rathOptions() {return {models: []};},
    }, ElMessage: {error: message => assert.fail(message)}, apiError: error => error.message,
  });
  vm.runInContext(code, context);
  return {context, descriptor, mounted, unmounted, run: code => vm.runInContext(code, context)};
}
function browser(path) {
  const events = new Map(), messages = new Map();
  const win = {location: new URL(path, origin), Event,
    addEventListener: (type, fn) => events.set(type, fn), removeEventListener: type => events.delete(type),
    dispatchEvent: event => events.get(event.type)?.(event),
    navigator: {serviceWorker: {addEventListener: (type, fn) => messages.set(type, fn), removeEventListener: type => messages.delete(type)}},
  };
  return {win, messages};
}

test('no-conversation push opens device notification domain; initial App normalization and Settings mount retain it', async () => {
  const w = worker();
  await w.event('push', {data: {json: () => ({body: '测试通知'})}});
  assert.equal(w.notices[0].options.data.url, target);
  await w.event('notificationclick', {notification: {close() {}, data: w.notices[0].options.data}});
  assert.deepEqual(w.opened, [origin + target]);
  const {win} = browser(target);
  const app = parse(readFileSync(new URL('../App.vue', import.meta.url), 'utf8')).descriptor.scriptSetup.content;
  const ast = parseJs(app, {sourceType: 'module'});
  const route = ast.program.body.find(n => n.type === 'FunctionDeclaration' && n.id.name === 'routeForCurrentState');
  const routeContext = vm.createContext({URLSearchParams, window: win, active: Vue.ref('settings'), settingsSection: Vue.ref('system-settings'), pageToPath: {settings: '/settings'}});
  vm.runInContext(app.slice(route.start, route.end), routeContext);
  assert.equal(vm.runInContext('routeForCurrentState()', routeContext), target);
  const page = settingsPage(win);
  await page.run('load()');
  assert.equal(page.run('activeDomain.value'), 'notifications');
  assert.equal(page.run('query.value'), '');
  assert.equal(page.run('showDeviceNotifications.value'), true);
  const scope = page.run('({showDeviceNotifications})');
  // Render the actual conditional from the real Settings template, using a card
  // stub; DeviceNotifications itself has separate SFC rendering/style tests.
  const card = page.descriptor.template.content.match(/<DeviceNotifications[^>]*\/>/)[0];
  const appView = Vue.createSSRApp({setup: () => scope, render: Vue.compile(card)});
  appView.component('DeviceNotifications', {render: () => Vue.h('section', '本设备系统通知')});
  assert.match(await renderToString(appView), /本设备系统通知/);
});

for (const initial of ['/settings?section=system-settings&setting=web.taskNotifications.enabled', target]) {
  test(`an already-mounted settings card is revealed by SPA notification navigation from ${initial}`, async () => {
    const {win, messages} = browser(initial);
    const page = settingsPage(win);
    await page.run('load()');
    page.run("activeDomain.value = 'agent'; query.value = 'web.taskNotifications.enabled'; draft['agent.foo'] = 'unsaved draft'");
    // Install only the lifecycle listener; load() was already awaited above.
    page.mounted[0]();
    const routes = [];
    const stop = installPushNavigation(win, async url => {routes.push(url); win.location = new URL(url, origin);});
    let handled;
    const client = {url: win.location.href, async focus() {}, postMessage(data) {handled = messages.get('message')({data});}};
    const w = worker(client);
    await w.event('notificationclick', {notification: {close() {}, data: {url: target}}});
    await handled;
    assert.equal(page.run('activeDomain.value'), 'notifications');
    assert.equal(page.run('query.value'), '');
    assert.equal(page.run('showDeviceNotifications.value'), true);
    assert.equal(page.run("draft['agent.foo']"), 'unsaved draft');
    assert.deepEqual(w.opened, []);
    assert.deepEqual(routes, initial === target ? [] : [target]);
    stop(); page.unmounted.forEach(callback => callback());
  });
}

test('cancelled SPA navigation does not reveal notification settings or replace the page', async () => {
  const {win, messages} = browser('/cron');
  let navigated = false;
  win.addEventListener('openbear:notification-navigated', () => {navigated = true;});
  const stop = installPushNavigation(win, async () => false);
  await messages.get('message')({data: {type: 'openbear:notification-open', url: target}});
  assert.equal(navigated, false);
  assert.equal(win.location.pathname, '/cron');
  stop();
});

test('invalid notification URL falls back safely and login windows are not reused', async () => {
  for (const url of ['http://[', 'https://evil.test/chat', '/api/auth/session', 'https://evil.test/settings', 'https://user:pass@bear.test/chat', '/chat/', '/chat-extra', '/api/conversations', '/%63hat', '/recommendations']) {
    const client = {url: origin + '/login', postMessage() {assert.fail('must not navigate login SPA');}};
    const w = worker(client);
    await w.event('notificationclick', {notification: {close() {}, data: {url}}});
    assert.deepEqual(w.opened, [origin + '/chat']);
  }
});
