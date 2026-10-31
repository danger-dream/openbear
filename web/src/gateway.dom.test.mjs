// The page binding of a gateway login without a browser: which requests carry the page's
// context, which URLs get it and which never do, what a changed login clears and never replays,
// how the push client keeps its owner, and how a notification for another instance is refused.
// The same rules over real cookies, HTTP streams, WebSocket upgrades, IndexedDB, other tabs and
// the service worker stay in gateway.browser.test.mjs.
import {window} from './domTestEnv.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import './testing/appHarnessStubs.mjs';
import {network} from './testing/fakeNetwork.mjs';

const uuid = '11111111-1111-4111-8111-111111111111', fileId = '22222222-2222-4222-8222-222222222222';
const artifact = `/api/conversations/${uuid}/artifacts/${fileId}/content`;
const users = {'family-a': {instanceId: 'family-a', username: 'family-a', contextToken: 'page-1'}, 'family-b': {instanceId: 'family-b', username: 'family-b', contextToken: 'page-2'}};
// The fixture server: the current login (a cookie in the browser) decides who the request is.
let current = 'family-a';
const calls = [], forwarded = [];
network.handler = request => {
  const login = users[current], context = request.headers['x-openbear-context'] || request.query._ob_ctx;
  calls.push({path: request.path, method: request.method, context, body: request.body});
  if (request.path === '/api/auth/session' && !context) return {json: {ok: true, mode: 'gateway', ...login}};
  if (context !== login.contextToken) return {status: 409, json: {ok: false, error: 'session_context_changed'}};
  if (request.path === '/api/auth/session') return {json: {ok: true, mode: 'gateway', ...login}};
  forwarded.push({instance: login.instanceId, path: request.path, body: request.body});
  if (request.path === artifact) return {headers: {'content-type': 'text/plain'}, body: 'file-content'};
  if (request.path === '/api/push/key') return {json: {ok: true, publicKey: 'AQID'}};
  if (request.path === '/api/push/status') return {json: {ok: true, enabled: true}};
  return {json: {ok: true, items: []}};
};
window.__OPENBEAR_AUTH_MODE__ = 'gateway';
localStorage.setItem('openbear.gateway.family-a.openbear.notifications.prompt.v1', 'dismissed');
const boundary = await import('./sessionBoundary.js');
await boundary.initializePageSession(window);
const {Api, conversationWsUrl} = await import('./api.js');

test('bootstrap reads the session once, without a context; every later call carries the page binding', async () => {
  assert.equal(calls[0].path, '/api/auth/session');
  assert.equal(calls[0].context, undefined);
  assert.ok(!calls.some(c => c.path === '/api/auth/mode'));
  assert.equal(boundary.pageSession.instanceId, 'family-a');
  await Api.conversations();
  const response = await boundary.contextFetch(artifact + '?text=1');
  assert.equal(await response.text(), 'file-content');
  assert.ok(calls.slice(1).every(c => c.context === 'page-1'), JSON.stringify(calls));
  assert.ok(calls.some(c => c.path === artifact && c.context === 'page-1'));
});

test('Markdown images, download links, attachment URLs and sockets get the binding; other origins and blobs never do', async () => {
  const {artifactFromUrl} = await import('./artifacts/artifactFiles.js');
  assert.ok(artifactFromUrl(artifact).downloadUrl.includes('_ob_ctx=page-1'));
  const {renderMarkdown} = await import('./views/consoleView/markdown.js');
  const markup = renderMarkdown(`![test](${artifact})\n\n[download](${artifact}?download=1)`);
  assert.ok(markup.includes('_ob_ctx=page-1'), markup);
  assert.equal(boundary.contextResourceUrl('https://external.example/api/private'), 'https://external.example/api/private');
  assert.equal(boundary.contextResourceUrl('blob:local'), 'blob:local');
  const ws = boundary.contextWebSocket(conversationWsUrl(uuid));
  assert.equal(new URL(ws.url).pathname, `/api/conversations/${uuid}/ws`);
  assert.equal(new URL(ws.url).searchParams.get('_ob_ctx'), 'page-1');
  ws.close();
});

test('push APIs use the binding; enabling takes ownership from another instance and disabling gives it back', async () => {
  const {createPushClient} = await import('./pwa/pushClient.js');
  const actions = [];
  let subscription = {endpoint: 'https://push.fixture/old', async unsubscribe() { actions.push('unsubscribe-old'); subscription = null; }};
  const registration = {active: {state: 'activated', postMessage: data => actions.push(data)}, pushManager: {async getSubscription() { return subscription; }, async subscribe() {
    subscription = {endpoint: 'https://push.fixture/new', toJSON: () => ({endpoint: 'https://push.fixture/new', keys: {p256dh: 'fixture', auth: 'fixture'}}), async unsubscribe() { actions.push('unsubscribe-new'); subscription = null; }};
    return subscription;
  }}};
  localStorage.setItem('openbear.gateway.push-owner', 'family-b');
  const before = calls.length;
  const native = {isSecureContext: true, location: window.location, localStorage, Event: window.Event, crypto: globalThis.crypto, dispatchEvent: window.dispatchEvent.bind(window), BroadcastChannel: window.BroadcastChannel,
    Notification: {permission: 'granted', requestPermission: async () => 'granted'}, PushManager: {}, navigator: {userAgent: 'Mozilla/5.0 (X11; Linux x86_64) Chrome/130', serviceWorker: {getRegistration: async () => registration, register: async () => registration}}};
  const client = createPushClient(native);
  const initial = await client.status();
  await client.enable();
  const owner = localStorage.getItem('openbear.gateway.push-owner');
  await client.test();
  await client.disable();
  assert.equal(initial.enabled, false);
  assert.equal(owner, 'family-a');
  assert.equal(localStorage.getItem('openbear.gateway.push-owner'), null);
  assert.ok(actions.includes('unsubscribe-old'));
  assert.ok(actions.includes('unsubscribe-new'));
  const pushCalls = calls.slice(before).filter(c => c.path.startsWith('/api/push/'));
  assert.ok(pushCalls.length >= 4, JSON.stringify(pushCalls));
  assert.ok(pushCalls.every(c => c.context === 'page-1'));
});

// Which storage is erased is checked once, in the real browser (gateway.browser.test.mjs).
test('another login cannot rebind this page: refused before upstream, session ended, nothing replayed', async () => {
  let ended = 0;
  window.addEventListener('openbear:session-ended', () => ended++);
  current = 'family-b';
  const before = forwarded.length;
  await Api.conversations().catch(() => {});
  assert.equal(forwarded.slice(before).filter(x => x.path === '/api/conversations').length, 0);
  assert.equal(boundary.pageSession.blocked, true);
  assert.equal(ended, 1);
  await Api.createConversation({title: 'NEVER REPLAY'}).catch(() => {});
  await boundary.initializePageSession(window);
  assert.equal(boundary.pageSession.blocked, true);
  assert.equal(boundary.pageSession.instanceId, 'family-a');
  assert.equal(calls.filter(c => c.body?.title === 'NEVER REPLAY').length, 0);
});

test('a page or notification link for another instance is blocked at bootstrap', async () => {
  // A fresh copy of the module is a fresh page.
  const fresh = await import('./sessionBoundary.js?page=notification');
  const browser = {__OPENBEAR_AUTH_MODE__: 'gateway', location: new URL(`http://localhost/chat?id=${uuid}&_ob_instance=family-a`), localStorage, document: window.document, navigator: {},
    CustomEvent: window.CustomEvent, dispatchEvent: () => true, addEventListener() {}, BroadcastChannel: window.BroadcastChannel,
    fetch: async () => new Response(JSON.stringify({ok: true, mode: 'gateway', ...users['family-b']}), {status: 200, headers: {'content-type': 'application/json'}})};
  await fresh.initializePageSession(browser);
  assert.equal(fresh.pageSession.instanceId, 'family-b');
  assert.equal(fresh.pageSession.blocked, true);
  assert.equal(fresh.notificationInstanceMatches('/chat?id=a&_ob_instance=family-b'), false);
});
