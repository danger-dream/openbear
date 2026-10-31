// The servers page in happy-dom, with the same fake backend as the browser tests.
// setup(t, options) takes the options of the browser tests' setup() (width, dark, view, tweak,
// keepAlive, fx, release, recheck, mesh, refuseMesh) and returns the same handles:
// {page, calls, terminal, uploads, saved, items, close}.
import {window} from '../../domTestEnv.mjs';
import {network} from '../../testing/fakeNetwork.mjs';
import './serversStubs.mjs';
import {mountApp} from '../../testing/mountApp.mjs';
import {scaleTimers} from '../../testing/domPage.mjs';
import {createServersBackend} from './serversBackend.mjs';

scaleTimers(10);
const {h, KeepAlive, ref} = await import('vue');
const ServersView = (await import('../../views/ServersView.vue')).default;

export async function setup(t, {width = 1440, height = 900, dark = false, view = 'cards', tweak = null, keepAlive = false, fx, release = null, recheck = null, mesh = null, refuseMesh = [], wait = true} = {}) {
  const backend = createServersBackend({tweak, ...(fx === undefined ? {} : {fx}), release, recheck, mesh, refuseMesh});
  network.reset();
  network.handler = backend.handle;
  network.external = backend.external;
  network.onSocket = socket => {
    if (!/\/api\/servers\/[^/]+\/terminal/.test(new URL(socket.url).pathname)) return;
    // What the page sent while the backend greeted it is handled once the handler exists.
    const onMessage = backend.openTerminal(data => socket.receive(data));
    const handle = message => onMessage(typeof message === 'string' ? message : Buffer.from(message));
    for (const message of socket.sent) handle(message);
    socket.onSend = handle;
  };
  window.sessionStorage.clear();
  window.localStorage.clear();
  window.localStorage.setItem('openbear.servers.view', view);
  const shown = ref(true);
  window.__showServers = value => { shown.value = value; };
  const Other = {name: 'OtherPage', render: () => h('div', {id: 'other-page'}, '会话')};
  const root = keepAlive ? {render: () => h(KeepAlive, {include: ['ServersView']}, [shown.value ? h(ServersView) : h(Other)])} : ServersView;
  const {page, unmount} = mountApp(t, root, {width, height, dark, network});
  if (wait) await page.locator(view === 'list' ? '.pl-row' : '.pb-card').first().waitFor();
  return {page, backend, calls: backend.calls, terminal: backend.terminal, uploads: backend.uploads, saved: backend.saved, items: backend.items, close: () => unmount()};
}

export const names = page => page.locator('.pb-card .pb-name').allInnerTexts();
export const expectFocused = async locator => { if (!(await locator.evaluate(el => el === window.document.activeElement))) throw new Error('not focused'); };
export const tab = (page, name) => page.locator('.sv-tabs [role=tab]').filter({hasText: name});
