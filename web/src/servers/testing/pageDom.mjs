// Mounting a page of the servers area in happy-dom for the *.dom.test.mjs files whose browser
// tests build their own page (mesh.forms, mesh.clarity, bench, terminalLinks): one call does what
// their browser setup() does before the checks — a fresh copy of the app's modules (vueLoader's
// ?fresh, as a new page), the fake API, session / local storage, theme and viewport.
//   const {page, fresh, close} = await mountPage(t, {
//     load: n => import(`../../views/ServersView.vue?fresh=${n}`),  // the module to mount (n: copy)
//     root: (View, h) => View,              // optional: wrap it (props, a shell)
//     handle: async (method, path, body, url) => [status, data],  // the API, like page.route
//     session: {...}, local: {...}, width, height, dark});
// page.mouse.move(x, y) leaves the hovered element (no layout: only a move to an empty spot).
// Long timers run 10 times faster (scaleTimers); page.waitForTimeout(ms) waits the same page time.
import {window} from '../../domTestEnv.mjs';
import {network} from '../../testing/fakeNetwork.mjs';
import './serversStubs.mjs';
import {mountApp} from '../../testing/mountApp.mjs';
import {sleep, settle, scaleTimers, leaveHover} from '../../testing/domPage.mjs';
import './flowLayout.mjs';

const SCALE = 10;
scaleTimers(SCALE);
const {h} = await import('vue');
let copies = 0;

export async function mountPage(t, {load, root = View => View, handle, session = {}, local = {}, width = 1440, height = 900, dark = false, before = null} = {}) {
  const calls = [];
  network.reset();
  network.external = async () => ({status: 503, body: ''});
  network.handler = async ({path, method, body, url}) => {
    calls.push({path, method, body, at: Date.now(), query: url.search});
    const [status, data] = await handle(method, path, body, url);
    return {status, json: JSON.parse(JSON.stringify(data))};
  };
  window.sessionStorage.clear();
  window.localStorage.clear();
  for (const [key, value] of Object.entries(session)) window.sessionStorage.setItem(key, value);
  for (const [key, value] of Object.entries(local)) window.localStorage.setItem(key, value);
  before?.();
  const fresh = ++copies;
  const View = (await load(fresh)).default;
  const {page, unmount, errors} = mountApp(t, root(View, h), {width, height, dark, network, touch: width < 760});
  page.waitForTimeout = async ms => { await sleep(Number(ms) >= 200 ? Math.max(20, ms / SCALE) : ms); await settle(); };
  // Only moves off to an empty spot: a move to a point needs layout (the browser tests).
  page.mouse = {move: async () => { leaveHover(); await settle(); }};
  await page.locator('#app > *').first().waitFor({state: 'attached'});
  return {page, calls, errors, fresh, close: () => unmount()};
}
