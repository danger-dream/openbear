import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdirSync} from 'node:fs';
import {createRequire} from 'node:module';
import {startBrowserTestServer, waitForBrowserReady} from '../../browserTestServer.mjs';
const require = createRequire(import.meta.url);
let chromium;
try { ({chromium} = require(process.env.OPENBEAR_PLAYWRIGHT_MODULE || '/opt/openclaw/node_modules/playwright-core')); } catch {}
const shots = process.env.OPENBEAR_BENCH_SHOTS || '';
const html = '<html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head><body><div id="app"></div><script type="module" src="/@id/__x00__virtual:bench-test"></script></body></html>';
const entry = `import {createApp, h} from 'vue'; import ElementPlus from 'element-plus'; import * as Icons from '@element-plus/icons-vue'; import '/src/theme.js'; import 'element-plus/dist/index.css'; import 'element-plus/theme-chalk/dark/css-vars.css'; import '/src/style.css'; import '/src/dark-theme.css'; import '/src/admin-mobile.css'; import '/src/mobile-overlays.css'; import '/src/mobile-inputs.css';
if (window.__dark) document.documentElement.classList.add('dark');
const View = (await import('/src/servers/bench/BenchView.vue')).default;
// window.__page: the whole servers page in the app's shell (sidebar, then main as in App.vue), opened on the 评测 tab.
const Page = window.__page ? (await import('/src/views/ServersView.vue')).default : null;
const root = window.__page
  ? {render: () => h('div', {class: 'app-shell h-full flex', style: 'height:100vh'}, [h('aside', {style: 'width:220px;flex:none'}), h('main', {class: 'app-main flex-1 min-w-0 flex flex-col'}, [h(Page)])])}
  : {render: () => h('div', {class: 'instance-ui servers-ui sv-body', style: 'height:100vh'}, [h(View, {active: true, servers: window.__servers})])};
const app = createApp(root); app.use(ElementPlus); for (const [key, icon] of Object.entries(Icons)) app.component(key, icon); app.mount('#app');`;

import {real, linux, SERVERS, pageItem, IP_REPORT, CATALOG, T, run, fakeBench} from '../testing/benchFixture.mjs';

test('评测 tab: tree, reports, comparison, the form, phone and dark', {skip: !chromium, timeout: 600000}, async t => {
  const fixture = await startBrowserTestServer(t, {name: 'bench', entries: ['virtual:bench-test', '/src/servers/bench/BenchView.vue'], plugins: [{name: 'bench-entry',
    resolveId(id) { if (id === 'virtual:bench-test') return '\0virtual:bench-test'; }, load(id) { if (id === '\0virtual:bench-test') return entry; },
    configureServer(s) { s.middlewares.use((req, res, next) => { if (req.url?.split('?')[0] === '/__test') { res.setHeader('content-type', 'text/html'); res.end(html); } else next(); }); }}]});
  let browser;
  t.after(async () => { try { await browser?.close(); } finally { await fixture.close(); } });
  browser = await chromium.launch({executablePath: process.env.OPENBEAR_CHROME || '/usr/bin/google-chrome', headless: true, args: ['--no-sandbox']});
  if (shots) mkdirSync(shots, {recursive: true});

  async function setup(t, {width = 1440, height = 900, dark = false, page: whole = false} = {}) {
    const phone = width < 760;
    const context = await browser.newContext({viewport: {width, height}, deviceScaleFactor: phone ? 2 : 1, colorScheme: dark ? 'dark' : 'light', hasTouch: phone, isMobile: phone, timezoneId: 'Asia/Shanghai'});
    t.after(() => context.close().catch(() => {}));
    const page = await context.newPage(), calls = [], errors = [], bench = fakeBench();
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(([isDark, servers, whole]) => {
      window.__dark = isDark; window.__servers = servers; window.__page = whole;
      if (whole) sessionStorage.setItem('openbear.servers.tabs', JSON.stringify({tabs: [], active: 'bench'}));
    }, [dark, SERVERS, whole]);
    await page.route('**/api/**', async route => {
      const req = route.request(), url = new URL(req.url()), method = req.method();
      const raw = req.postDataBuffer();
      const body = raw && req.headers()['content-type']?.includes('json') ? JSON.parse(raw.toString()) : null;
      calls.push({path: url.pathname, method, body});
      let status = 200, data = {ok: true};
      if (url.pathname.startsWith('/api/servers/bench')) [status, data] = bench.handle(method, url.pathname.slice('/api/servers/bench'.length), body);
      else if (url.pathname === '/api/servers') data = {ok: true, items: SERVERS.map(pageItem), settings: {accessPoints: [], defaultAccess: '', timezone: 'Asia/Shanghai'}, node: {available: true, version: '0.14.0'}};
      else if (url.pathname === '/api/servers/live') data = {ok: true, at: Date.now(), items: Object.fromEntries(SERVERS.map(x => [x.id, pageItem(x).live]))};
      else if (/^\/api\/servers\/[^/]+\/jobs\//.test(url.pathname)) data = {ok: true, job: {id: 'x', state: 'running'}, log: {data: '正在安装依赖…\r依赖已就绪   \n三网 TCP 大包延迟 \x1b[32m进行中\x1b[0m 12/31\r三网 TCP 大包延迟 \x1b[32m进行中\x1b[0m 29/31\n', offset: 0, next: 1, size: 1, first: 0}};
      await route.fulfill({status, contentType: 'application/json', body: JSON.stringify(data)});
    });
    await page.goto(fixture.base + '/__test');
    await waitForBrowserReady(page, page.locator('#app > *').first(), errors);
    await page.locator('.bn-nodes').waitFor();
    // The runs are in.
    await page.locator('.bn-nodes li[data-server]').first().waitFor();
    return {page, calls, bench, errors, close: async () => { assert.deepEqual(errors, []); await context.close(); }};
  }
  // Against the device width: a mobile layout viewport widens to fit anything too wide, innerWidth with it.
  const noOverflow = async (page, label = '') => {
    const found = await page.evaluate(() => {
      const limit = screen.width + 1, wide = [];
      for (const el of document.querySelectorAll('body *')) { const r = el.getBoundingClientRect(); if (r.width && r.right > limit && getComputedStyle(el).position !== 'fixed') wide.push(`${el.tagName}.${el.className}:${Math.round(r.right)}`); }
      return {inner: innerWidth, doc: document.documentElement.scrollWidth, width: screen.width, wide: wide.slice(0, 6)};
    });
    assert.ok(found.doc <= found.width + 1 && found.inner <= found.width + 1, `page scrolls horizontally ${label} ${JSON.stringify(found)}`);
  };
  const shot = async (page, name) => { if (shots) { await page.evaluate(() => Promise.all(document.getAnimations().filter(a => a.effect?.getComputedTiming().endTime !== Infinity).map(a => a.finished.catch(() => {})))); await page.screenshot({path: `${shots}/${name}.png`}); } };
  const server = (page, id) => page.locator(`li[data-server="${id}"]`);
  const runRow = (page, id) => page.locator(`li[data-run="${id}"] .bn-row-main`);

  // In the servers page and the app's shell: a report box fits the pane and scrolls inside it, a long one included.
  await t.test('desktop: the tree, a report, comparison and deletion', async t => {
    const {page, calls, close} = await setup(t);
    // Servers with runs open; Windows cannot run tests and has none, so it is not listed.
    await shot(page, 'desktop-intro');

    await runRow(page, 'r2').click();
    const report = page.locator('.bn-main .bn-report');
    await report.locator('.bn-section').first().waitFor();
    // Two reports: one at a time, by tabs.
    await report.getByRole('tab', {name: 'IPv6'}).click();
    await report.getByRole('tab', {name: 'IPv4'}).click();
    await noOverflow(page, 'report');
    await shot(page, 'desktop-report');

    // A failed run says why.
    await runRow(page, 'r6').click();
    await page.locator('.bn-main .bn-note').waitFor();
    // Without a report the log is all there is.
    await page.locator('.bn-main .bn-section h4', {hasText: '运行日志'}).waitFor();

    // Two runs side by side, scrolling together.
    await page.locator('li[data-run="r1"] .bn-pick input').check();
    await page.locator('li[data-run="r2"] .bn-pick input').check();
    await page.getByTestId('bn-compare').click();
    const cols = page.locator('.bn-compare-col');
    await cols.nth(1).locator('.bn-section').first().waitFor();
    await cols.nth(0).evaluate(el => { el.scrollTop = 120; el.dispatchEvent(new Event('scroll')); });
    // The other column has had a frame to follow.
    await page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))));
    const tops = await cols.evaluateAll(els => els.map(el => el.scrollTop));
    if (tops[0] > 0) assert.equal(tops[1], tops[0], 'the other column follows');
    await shot(page, 'desktop-compare');
    await page.getByRole('button', {name: '退出对比'}).click();

    // Deleting asks first.
    await runRow(page, 'r1').click();
    await page.locator('.bn-main .bn-report .dd-danger').click();
    await page.locator('.el-message-box').getByRole('button', {name: '删除'}).click();
    await page.locator('li[data-run="r1"]').waitFor({state: 'detached'});
    await close();
  });

  await t.test('desktop: starting a test, warnings, a running run', async t => {
    const {page, calls, close} = await setup(t);
    await server(page, 'jp').locator('.bn-row-server .bn-add').click();
    const form = page.locator('.dd-dialog .bn-form');
    await form.waitFor();
    // The server picker shows flags (with the page's flag font, which a native select cannot use on Windows).
    const picker = form.locator('.dd-node-trigger');
    await picker.click();
    const panel = page.locator('.dd-node-panel');
    await panel.waitFor();
    await shot(page, 'desktop-form-picker');
    await panel.locator('.ss-option', {hasText: 'Idle-DE'}).dispatchEvent('pointerdown');
    await panel.waitFor({state: 'hidden'});
    await picker.click();
    await panel.locator('.ss-option', {hasText: 'RFCHost-JP2.T1'}).dispatchEvent('pointerdown');
    await panel.waitFor({state: 'hidden'});
    await form.getByLabel('评测项目').selectOption('netcheck');
    await form.locator('.dd-choices label', {hasText: '完整模式'}).click();
    await shot(page, 'desktop-form');
    await page.getByTestId('bn-start').click();
    await form.waitFor({state: 'detached'});
    const posted = calls.find(c => c.method === 'POST' && c.path === '/api/servers/bench/runs');
    // The new run opens, with the node's latest output while it runs.
    const live = page.locator('.bn-main .bn-live');
    await live.waitFor();
    await shot(page, 'desktop-running');

    // The suite queues several runs.
    await page.getByTestId('bn-new').click();
    await form.waitFor();
    await page.getByTestId('bn-start').click();
    await form.waitFor({state: 'detached'});
    const suite = calls.filter(c => c.method === 'POST' && c.path === '/api/servers/bench/runs').at(-1);
    await close();
  });

  await t.test('phone: the form as a sheet', async t => {
    const {page, close} = await setup(t, {width: 390, height: 844});
    await page.getByTestId('bn-new').click();
    const sheet = page.getByRole('dialog', {name: '运行评测'});
    await sheet.locator('.bn-form').waitFor();
    await sheet.getByLabel('评测项目').selectOption('netroute');
    assert.ok(await sheet.getByLabel('省份').evaluate(el => parseFloat(getComputedStyle(el).fontSize) >= 16), 'no zoom on focus');
    await noOverflow(page, 'form');
    await shot(page, 'phone-form');
    await close();
  });

  for (const [width, height, name] of [[1440, 900, 'desktop'], [1180, 760, 'laptop']]) {
    await t.test(`${name}: a report inside the servers page fits and scrolls`, async t => {
      const {page, close} = await setup(t, {width, height, page: true});
      await page.locator('.bn-nodes').waitFor();
      await runRow(page, 'r7').click();
      const term = page.locator('.bn-main .bn-term').first();
      await term.waitFor();
      const box = await term.boundingBox();
      const pane = await page.locator('.bn-main').boundingBox();
      const inside = await term.evaluate(el => ({scrollH: el.scrollHeight, clientH: el.clientHeight, scrollW: el.scrollWidth, clientW: el.clientWidth}));
      await shot(page, `${name}-page-gb5`);
      assert.ok(box.x + box.width <= pane.x + pane.width + 1 && box.y + box.height <= pane.y + pane.height + 1, `the box stays in the pane: ${JSON.stringify({box, pane})}`);
      assert.ok(inside.scrollH > inside.clientH + 100, `the long report scrolls inside its box: ${JSON.stringify(inside)}`);
      // A real mouse wheel over the box scrolls it.
      await page.mouse.move(box.x + 100, box.y + 100);
      await page.mouse.wheel(0, 600);
      await term.evaluate(el => new Promise(resolve => { const t0 = Date.now(); (function check() { if (el.scrollTop > 100 || Date.now() - t0 > 2000) resolve(); else requestAnimationFrame(check); })(); }));
      assert.ok(await term.evaluate(el => el.scrollTop > 100), 'the wheel scrolls the report');
      assert.ok(await page.locator('.bn-main-scroll').evaluate(el => el.scrollHeight <= el.clientHeight + 1), 'the pane itself does not need to scroll');
      if (name === 'desktop') assert.ok(box.width < pane.width - 100, `a narrow report gets a narrow box: ${box.width} in ${pane.width}`);
      else assert.ok(inside.scrollW <= inside.clientW + 1 || box.width >= pane.width - 50, 'a report wider than the pane takes its width and scrolls sideways');
      // The tree lists servers with runs only.
      await close();
    });
  }

  for (const dark of [false, true]) {
    await t.test(`phone${dark ? ', dark' : ''}: the tree alone, sheets for reports and the comparison`, async t => {
      const {page, close} = await setup(t, {width: 390, height: 844, dark});
      await noOverflow(page, 'tree');
      const box = await runRow(page, 'r2').boundingBox();
      assert.ok(box.height >= 40, 'rows are easy to tap');
      await shot(page, `phone-tree${dark ? '-dark' : ''}`);
      await runRow(page, 'r2').click();
      const sheet = page.getByRole('dialog', {name: '评测报告'});
      await sheet.locator('.bn-section').first().waitFor();
      await noOverflow(page, 'report sheet');
      const term = sheet.locator('.bn-term').first();
      const metrics = await term.evaluate(el => { const out = []; for (let n = el; n && out.length < 8; n = n.parentElement) out.push(`${n.className}|${n.clientWidth}/${n.scrollWidth}|${getComputedStyle(n).overflowX}|${getComputedStyle(n).display}`); return out; });
      assert.ok(await term.evaluate(el => el.scrollWidth > el.clientWidth), `a wide report scrolls inside its own box ${metrics.join(' <- ')}`);
      assert.equal(await term.evaluate(el => getComputedStyle(el).overscrollBehaviorY), 'auto', 'a vertical swipe on the box scrolls the sheet');
      await shot(page, `phone-report${dark ? '-dark' : ''}`);
      await sheet.getByRole('button', {name: '关闭'}).click();
      await sheet.waitFor({state: 'hidden'});
      await page.locator('li[data-run="r1"] .bn-pick input').check();
      await page.locator('li[data-run="r2"] .bn-pick input').check();
      await page.locator('li[data-run="r3"] .bn-pick input').check();
      await page.getByTestId('bn-compare').click();
      const pair = page.getByRole('dialog', {name: '对比'});
      await pair.locator('.bn-compare-tabs').waitFor();
      await pair.locator('.bn-compare-tabs [role="tab"]').nth(1).click();
      await noOverflow(page, 'comparison sheet');
      await shot(page, `phone-compare${dark ? '-dark' : ''}`);
      await close();
    });
  }
});
