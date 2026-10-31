// Page loads of real components in happy-dom for the *.dom.test.mjs files: each open() is a
// fresh "page" at a URL (previous app unmounted, storage cleared, the fake network reset),
// like the browser tests' page.goto. Import after '../domTestEnv.mjs'. monaco-editor is
// replaced by testing/monacoStub.mjs (see appHarnessStubs.mjs).
import {window} from '../domTestEnv.mjs';
import './appHarnessStubs.mjs';
import {network} from './fakeNetwork.mjs';
import {mountApp} from './mountApp.mjs';

// Viewport globals the app reads directly (innerWidth, scrollY...): live, so resize() applies.
for (const name of ['innerWidth', 'innerHeight', 'outerWidth', 'outerHeight', 'scrollX', 'scrollY', 'pageXOffset', 'pageYOffset', 'devicePixelRatio', 'visualViewport', 'screen']) {
  if (!(name in globalThis)) Object.defineProperty(globalThis, name, {get: () => window[name], configurable: true});
}
if (!('getSelection' in globalThis)) Object.defineProperty(globalThis, 'getSelection', {value: window.getSelection.bind(window), configurable: true, writable: true});

let current = null;
// handler(request) as for testing/fakeNetwork.mjs; onSocket(socket) for sockets the page opens.
// storage: localStorage entries to set before the app starts.
// keepStorage: a reload of the same browser (localStorage survives, sessionStorage of the tab too).
export async function openPage(t, component, {url = '/', width = 1440, height = 900, dark = false, handler, onSocket = null, storage = {}, keepStorage = false, before = null} = {}) {
  await current?.unmount({check: false});
  current = null;
  if (!keepStorage) { window.localStorage.clear(); window.sessionStorage.clear(); }
  for (const [key, value] of Object.entries(storage)) window.localStorage.setItem(key, value);
  window.happyDOM.setURL(new URL(url, 'http://localhost/').href);
  network.reset();
  network.handler = handler;
  network.onSocket = onSocket;
  await before?.();
  const mounted = mountApp(t, component, {width, height, dark, network});
  current = mounted;
  return {...mounted, calls: network.calls, phone: width < 761};
}
// Everything a page left in localStorage and sessionStorage.
export function storageDump() {
  const out = {};
  for (const storage of [window.localStorage, window.sessionStorage]) for (let i = 0; i < storage.length; i++) out[storage.key(i)] = storage.getItem(storage.key(i));
  return out;
}
export {network, window};
