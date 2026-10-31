import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdirSync} from 'node:fs';
import {createRequire} from 'node:module';
import {startBrowserTestServer, waitForBrowserReady} from '../browserTestServer.mjs';
import {createGatewayUpdateBackend, NEW, OLD} from './gatewayUpdateFixture.mjs';
const require = createRequire(import.meta.url);
let chromium;
try { ({chromium} = require(process.env.OPENBEAR_PLAYWRIGHT_MODULE || '/opt/openclaw/node_modules/playwright-core')); } catch {}

// Screenshots go here when OPENBEAR_GATEWAY_UPDATE_SHOTS names a directory.
const shots = process.env.OPENBEAR_GATEWAY_UPDATE_SHOTS || '';
if (shots) mkdirSync(shots, {recursive: true});
const html = '<html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head><body><div id="app"></div><script type="module" src="/@id/__x00__virtual:gateway-update-test"></script></body></html>';
const entry = `import {createApp} from 'vue'; import ElementPlus from 'element-plus'; import * as Icons from '@element-plus/icons-vue'; import '/src/theme.js'; import 'element-plus/dist/index.css'; import 'element-plus/theme-chalk/dark/css-vars.css'; import '/src/style.css'; import '/src/dark-theme.css'; import '/src/admin-mobile.css'; import '/src/mobile-overlays.css'; import '/src/mobile-inputs.css';
const component = (await import('/src/views/InstancesView.vue')).default;
const app = createApp(component); app.use(ElementPlus); for (const [key, icon] of Object.entries(Icons)) app.component(key, icon); app.mount('#app');`;

// Layout smoke in real Chromium: where the update notice sits beside the gateway strip, its
// height and button on phones, light and dark, no sideways scroll. The texts, button states
// and requests of the same flows: gatewayUpdate.dom.test.mjs.
test('gateway update notice layout on desktop and phone, light and dark', {skip: !chromium, timeout: 180000}, async t => {
  const serverFixture = await startBrowserTestServer(t, {name: 'gateway-update', entries: ['virtual:gateway-update-test', '/src/views/InstancesView.vue'], plugins: [{name: 'gateway-update-entry',
    resolveId(id) { if (id === 'virtual:gateway-update-test') return '\0virtual:gateway-update-test'; },
    load(id) { if (id === '\0virtual:gateway-update-test') return entry; },
    configureServer(server) { server.middlewares.use((req, res, next) => { if (req.url?.split('?')[0] === '/__test') { res.setHeader('content-type', 'text/html'); res.end(html); } else next(); }); }}]});
  let browser;
  t.after(async () => { try { await browser?.close(); } finally { await serverFixture.close(); } });
  browser = await chromium.launch({executablePath: process.env.OPENBEAR_CHROME || '/usr/bin/google-chrome', headless: true, args: ['--no-sandbox']});

  async function setup(t, {width = 1280, height = 844, dark = false, touch = false, ...options} = {}) {
    const context = await browser.newContext({viewport: {width, height}, deviceScaleFactor: 1, colorScheme: dark ? 'dark' : 'light', hasTouch: touch, isMobile: touch});
    t.after(() => context.close());
    const page = await context.newPage(), errors = [], backend = createGatewayUpdateBackend(options);
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/api/**', async route => {
      const req = route.request();
      const {status, json} = await backend.handle({path: new URL(req.url()).pathname, method: req.method(), body: req.postDataJSON()});
      await route.fulfill({status, contentType: 'application/json', body: JSON.stringify(json)});
    });
    await page.goto(serverFixture.base + '/__test');
    await waitForBrowserReady(page, page.locator('#app > *').first(), errors);
    await page.locator('.ob-strip').waitFor();
    if (dark) await page.evaluate(() => document.documentElement.classList.add('dark'));
    const close = async () => { await context.close(); assert.deepEqual(errors, []); };
    return {page, close, finishUpdate: backend.finishUpdate};
  }
  const noOverflow = async page => assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
  const shot = async (page, name, options = {}) => { if (shots) { await page.mouse.move(1, 1); await page.waitForTimeout(700); await page.screenshot({path: `${shots}/${name}.png`, ...options}); } };
  const openSettings = async page => {
    await page.locator('.ob-strip').getByRole('button', {name: '设置', exact: true}).click();
    const dialog = page.getByRole('dialog', {name: '访问网关'}); await dialog.waitFor(); return dialog;
  };

  await t.test('desktop: the notice sits beside the gateway strip without changing the bar', {timeout: 60000}, async t => {
    const plain = await setup(t, {gatewayPatch: {build: NEW, updateAvailable: false, updateReasons: []}});
    const plainBar = await plain.page.locator('.ix-bar').boundingBox(), plainStrip = await plain.page.locator('.ob-strip').boundingBox();
    await plain.close();
    const h = await setup(t);
    const notice = h.page.locator('.ix-update');
    await notice.getByText('主控会自动更新', {exact: true}).waitFor();
    // Desktop layout stays one line: same bar height and strip position as without the notice.
    const bar = await h.page.locator('.ix-bar').boundingBox(), strip = await h.page.locator('.ob-strip').boundingBox(), box = await notice.boundingBox();
    assert.equal(Math.round(bar.height), Math.round(plainBar.height));
    assert.deepEqual([strip.x, strip.y].map(Math.round), [plainStrip.x, plainStrip.y].map(Math.round));
    assert.ok(Math.abs((box.y + box.height / 2) - (strip.y + strip.height / 2)) < 4 && box.x > strip.x + strip.width);
    await shot(h.page, 'desktop-1280-notice', {clip: {x: 0, y: 0, width: 1280, height: 220}});
    const dialog = await openSettings(h.page);
    await dialog.getByRole('button', {name: '更新网关', exact: true}).waitFor();
    await noOverflow(h.page);
    await shot(h.page, 'desktop-1280-settings');
    await h.close();
  });

  for (const dark of [false, true]) {
    await t.test(`phone ${dark ? 'dark' : 'light'} 390px: the notice gets its own compact line; the button does not move while updating`, {timeout: 60000}, async t => {
      const h = await setup(t, {width: 390, dark, touch: true});
      const notice = h.page.locator('.ix-update'), button = notice.getByRole('button', {name: '更新网关', exact: true});
      await notice.getByText('主控会自动更新', {exact: true}).waitFor();
      const strip = await h.page.locator('.ob-strip').boundingBox(), box = await notice.boundingBox(), btn = await button.boundingBox();
      assert.ok(box.y >= strip.y + strip.height - 1 && box.height <= 34, JSON.stringify({strip, box}));
      assert.ok(btn.height >= 32 && btn.x + btn.width <= 390 - 11);
      await noOverflow(h.page);
      await shot(h.page, `phone-390-${dark ? 'dark' : 'light'}-notice`);
      await button.click();
      await notice.getByText('正在更新…', {exact: true}).waitFor();
      // Pending and idle keep the button where it was: nothing shifts under the finger.
      assert.deepEqual(Object.values(await button.boundingBox()).map(Math.round), Object.values(btn).map(Math.round));
      await h.finishUpdate();
      await notice.waitFor({state: 'hidden'});
      const dialog = await openSettings(h.page);
      await dialog.getByText('上次更新成功（刚刚）', {exact: true}).waitFor();
      await noOverflow(h.page);
      await shot(h.page, `phone-390-${dark ? 'dark' : 'light'}-settings-current`);
      await h.close();
    });
  }

  await t.test('phone 320px: the failed-update notice stays one compact line; settings fit', {timeout: 60000}, async t => {
    const failed = {atMs: Date.now() - 180000, ok: false, auto: true, error: 'gateway_start_failed', build: OLD};
    const h = await setup(t, {width: 320, touch: true, gatewayPatch: {lastUpdate: failed, updateReasons: ['build_outdated', 'control_not_loopback']}});
    const notice = h.page.locator('.ix-update');
    await notice.getByText('自动更新失败', {exact: true}).waitFor();
    assert.ok((await notice.boundingBox()).height <= 34);
    await noOverflow(h.page);
    await shot(h.page, 'phone-320-failed-notice');
    const dialog = await openSettings(h.page);
    await dialog.getByText('更新后，网关的管理接口改为只在服务器本机监听。', {exact: false}).waitFor();
    await noOverflow(h.page);
    await shot(h.page, 'phone-320-failed-settings', {fullPage: true});
    await h.close();
  });
});
