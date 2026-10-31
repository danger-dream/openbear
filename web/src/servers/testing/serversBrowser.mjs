// The servers page in Chromium, for the layout smoke tests (servers.*.browser.test.mjs).
// The page talks to the same fake backend as the happy-dom tests (serversBackend.mjs).
import assert from 'node:assert/strict';
import {mkdirSync} from 'node:fs';
import {createRequire} from 'node:module';
import {startBrowserTestServer, waitForBrowserReady} from '../../browserTestServer.mjs';
import {createServersBackend} from './serversBackend.mjs';

const require = createRequire(import.meta.url);
export let chromium;
try { ({chromium} = require(process.env.OPENBEAR_PLAYWRIGHT_MODULE || '/opt/openclaw/node_modules/playwright-core')); } catch {}
export const shots = process.env.OPENBEAR_SERVER_SHOTS || '';
const html = '<html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head><body><div id="app"></div><script type="module" src="/@id/__x00__virtual:servers-test"></script></body></html>';
const entry = `import {createApp, h, KeepAlive, ref} from 'vue'; import ElementPlus from 'element-plus'; import * as Icons from '@element-plus/icons-vue'; import '/src/theme.js'; import 'element-plus/dist/index.css'; import '/src/style.css'; import '/src/admin-mobile.css'; import '/src/mobile-overlays.css'; import '/src/mobile-inputs.css';
if (window.__dark) document.documentElement.classList.add('dark');
const component = (await import('/src/views/ServersView.vue')).default;
const shown = ref(true); window.__showServers = value => { shown.value = value; };
const Other = {name: 'OtherPage', render: () => h('div', {id: 'other-page'}, '会话')};
const root = window.__keepAlive ? {render: () => h(KeepAlive, {include: ['ServersView']}, [shown.value ? h(component) : h(Other)])} : component;
const app=createApp(root); app.use(ElementPlus); for (const [key,icon] of Object.entries(Icons)) app.component(key,icon); app.mount('#app');`;

// Starts Vite and Chromium for one test file; closes both after it.
export async function startServersBrowser(t, {entries = ['/src/servers/TerminalWorkbench.vue']} = {}) {
  const serverFixture = await startBrowserTestServer(t, {name: 'servers', entries: ['virtual:servers-test', '/src/views/ServersView.vue', ...entries], plugins: [{name: 'servers-test-entry', resolveId(id) { if (id === 'virtual:servers-test') return '\0virtual:servers-test'; }, load(id) { if (id === '\0virtual:servers-test') return entry; }, configureServer(server) { server.middlewares.use((req, res, next) => { if (req.url?.split('?')[0] === '/__test') { res.setHeader('content-type', 'text/html'); res.end(html); } else next(); }); }}]});
  let browser;
  t.after(async () => { try { await browser?.close(); } finally { await serverFixture.close(); } });
  browser = await chromium.launch({executablePath: process.env.OPENBEAR_CHROME || '/usr/bin/google-chrome', headless: true, args: ['--no-sandbox']});
  if (shots) mkdirSync(shots, {recursive: true});

  async function setup(t, {width = 1440, height = 900, dark = false, view = 'cards', tweak = null, keepAlive = false, fx, release = null, recheck = null, mesh = null, refuseMesh = [], touch = false} = {}) {
    const context = await browser.newContext({viewport: {width, height}, deviceScaleFactor: 1, colorScheme: dark ? 'dark' : 'light', acceptDownloads: true, hasTouch: touch, isMobile: touch});
    await context.grantPermissions(['clipboard-read', 'clipboard-write'], {origin: serverFixture.base});
    t.after(() => context.close());
    const page = await context.newPage(), errors = [];
    const backend = createServersBackend({tweak, ...(fx === undefined ? {} : {fx}), release, recheck, mesh, refuseMesh});
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(([isDark, mode, alive]) => { window.__dark = isDark; window.__keepAlive = alive; sessionStorage.clear(); localStorage.setItem('openbear.servers.view', mode); }, [dark, view, keepAlive]);
    await page.routeWebSocket(/\/api\/servers\/[^/]+\/terminal/, ws => {
      const onMessage = backend.openTerminal(data => ws.send(data));
      ws.onMessage(onMessage);
    });
    const fulfill = (route, answer) => route.fulfill(answer.json !== undefined
      ? {status: answer.status ?? 200, contentType: 'application/json', headers: answer.headers, body: JSON.stringify(answer.json)}
      : {status: answer.status ?? 200, headers: answer.headers, body: answer.body ?? ''});
    await page.route(/^https:\/\/(open\.er-api\.com|api\.frankfurter\.dev)\//, route => fulfill(route, backend.external({url: new URL(route.request().url())})));
    await page.route('**/api/**', async route => {
      const req = route.request(), url = new URL(req.url()), raw = req.postDataBuffer();
      const body = raw && req.headers()['content-type']?.includes('json') ? JSON.parse(raw.toString()) : null;
      await fulfill(route, await backend.handle({url, path: url.pathname, method: req.method(), body, raw, query: Object.fromEntries(url.searchParams)}));
    });
    await page.goto(serverFixture.base + '/__test');
    await waitForBrowserReady(page, page.locator('#app > *').first(), errors);
    await page.locator(view === 'list' ? '.pl-row' : '.pb-card').first().waitFor();
    return {page, backend, calls: backend.calls, terminal: backend.terminal, uploads: backend.uploads, saved: backend.saved, items: backend.items,
      close: async () => { await context.close(); assert.deepEqual(errors, []); }};
  }
  return {setup, browser, base: serverFixture.base};
}

export const noOverflow = async page => assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'page scrolls horizontally');
// Screenshots (only with OPENBEAR_SERVER_SHOTS) are taken once the sheets and dialogs have finished moving.
export const settled = page => page.evaluate(() => Promise.all(document.getAnimations().map(a => a.finished.catch(() => {}))));
export const shot = async (page, name) => { if (shots) { await settled(page); await page.screenshot({path: `${shots}/${name}.png`}); } };
// Node version screenshots can go to a directory of their own.
const versionShots = process.env.OPENBEAR_NODE_VERSION_SHOTS || shots;
if (versionShots) mkdirSync(versionShots, {recursive: true});
export const versionShot = async (page, name) => { if (versionShots) { await settled(page); await page.screenshot({path: `${versionShots}/${name}.png`}); } };
export const names = page => page.locator('.pb-card .pb-name').allInnerTexts();
export const expectFocused = async locator => assert.ok(await locator.evaluate(el => el === document.activeElement), 'focused');
export const tab = (page, name) => page.locator('.sv-tabs [role=tab]').filter({hasText: name});
// Waits for a condition on the test's side (requests the fake backend has seen, terminal input).
export async function until(condition, {timeout = 5000} = {}) {
  const deadline = Date.now() + timeout;
  while (!(await condition())) {
    if (Date.now() > deadline) throw new Error(`until: timed out: ${condition}`);
    await new Promise(resolve => setTimeout(resolve, 20));
  }
}
// Two animation frames: the page has rendered what it was just given.
export const frames = page => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
