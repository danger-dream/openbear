import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {startBrowserTestServer} from './browserTestServer.mjs';
const require = createRequire(import.meta.url);
let chromium;
try { ({chromium} = require('./testing/playwright.cjs')); } catch {}
const entry = `import '/src/monaco-worker.js'; import {createApp} from 'vue'; import ElementPlus from 'element-plus'; import * as Icons from '@element-plus/icons-vue';
import 'element-plus/dist/index.css'; import 'element-plus/theme-chalk/dark/css-vars.css'; import '/src/style.css'; import '/src/dark-theme.css'; import '/src/admin-mobile.css'; import '/src/mobile-overlays.css'; import '/src/mobile-inputs.css';
import TemplateView from '/src/views/TemplateView.vue';
const app=createApp(TemplateView);app.use(ElementPlus);for(const [name,component] of Object.entries(Icons))app.component(name,component);app.mount('#app');`;
const html = '<html><head><meta name="viewport" content="width=device-width,initial-scale=1"><style>html,body,#app{height:100%;margin:0}</style></head><body><div id="app"></div><script type="module" src="/@id/__x00__virtual:local-persona-sync"></script></body></html>';

test('local persona sync: real template page, preserved draft, retries and responsive dialogs', {skip: !chromium, timeout: 240000}, async t => {
  const fixture = await startBrowserTestServer(t, {name: 'local-persona-sync', entries: ['virtual:local-persona-sync'], plugins: [{
    name: 'local-persona-sync', resolveId: id => id === 'virtual:local-persona-sync' ? '\0virtual:local-persona-sync' : undefined,
    load: id => id === '\0virtual:local-persona-sync' ? entry : undefined,
    configureServer(server) { server.middlewares.use((req, res, next) => { if (req.url === '/fixture') { res.setHeader('content-type', 'text/html'); res.end(html); } else next(); }); },
  }]});
  let browser;
  t.after(async () => { try { await browser?.close(); } finally { await fixture.close(); } });
  browser = await chromium.launch({executablePath: process.env.OPENBEAR_CHROME || '/usr/bin/google-chrome', headless: true, args: ['--no-sandbox']});
  for (const width of [1440, 900, 390, 320]) await t.test(`${width}px light/dark`, async t => {
    const phone = width < 761, context = await browser.newContext({viewport: {width, height: 844}, isMobile: phone, hasTouch: phone});
    t.after(() => context.close());
    const page = await context.newPage(), errors = [], writes = [];
    page.setDefaultTimeout(15000);page.on('pageerror', e => errors.push(e.message));
    let failedRead = false;
    const items = [{id: 'one', name: '小码酱', status: 'available'}, {id: 'two', name: 'CTF 沙箱', status: 'modified'}, {id: 'three', name: '大肥鱼', status: 'existing'}, {id: 'four', name: 'gemini-argon-4', status: 'update'}];
    const templates = [{id: 1, name: '当前主模板', content: 'system', is_active: 1, is_persona: 0, is_agent_active: 0}];
    await page.route('**/api/**', async route => {
      const req = route.request(), path = new URL(req.url()).pathname;
      if (path === '/api/memory/templates/personas') {
        if (!failedRead) { failedRead = true; return route.fulfill({status: 500, json: {error: 'temporary'}}); }
        return route.fulfill({json: {ok: true, items}});
      }
      if (path === '/api/memory/templates/sync-personas') {
        const body = req.postDataJSON();writes.push(body);
        for (const id of body.ids) { const item = items.find(x => x.id === id); item.status = 'existing'; templates.push({id: templates.length + 1, name: item.name, content: 'persona', is_persona: 1}); }
        return route.fulfill({json: {ok: true, created: body.ids.length, reused: 0}});
      }
      return route.fulfill({json: path === '/api/memory/templates' ? {items: templates} : {ok: true, output: 'preview', items: []}});
    });
    await page.goto(`${fixture.base}/fixture`);
    const name = page.getByRole('textbox', {name: '模板名称', exact: true});
    await name.fill('未保存的编辑');
    const open = async () => {
      if (phone) await page.locator('.template-more summary').click();
      await page.getByRole('button', {name: '同步本地人设', exact: true}).filter({visible: true}).click();
    };
    await open();
    const dialog = page.locator('.local-personas-dialog');
    await dialog.getByRole('alert').waitFor();
    await dialog.getByRole('button', {name: '重新读取', exact: true}).click();
    await dialog.getByText('小码酱', {exact: true}).waitFor();
    assert.deepEqual(writes, [], 'reading never imports');
    assert.equal(await dialog.locator('input[type=checkbox]:checked').count(), 2);
    assert.equal(await dialog.locator('input[type=checkbox]:disabled').count(), 2);
    for (const dark of [false, true]) {
      await page.evaluate(value => document.documentElement.classList.toggle('dark', value), dark);
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
      assert.ok(await dialog.evaluate(el => el.scrollWidth <= el.clientWidth + 1));
      const rect = await dialog.boundingBox();assert.ok(rect.x >= 0 && rect.x + rect.width <= width + 1);
      if (phone) assert.ok(rect.height < 620 && rect.y > 150, 'four entries stay in a compact bottom sheet');
      assert.ok(await dialog.getByRole('button', {name: '同步所选人设', exact: true}).isVisible());
      if (process.env.OPENBEAR_SCREENSHOT_DIR) await page.screenshot({path: `${process.env.OPENBEAR_SCREENSHOT_DIR}/personas-${width}-${dark ? 'dark' : 'light'}.png`});
    }
    await dialog.getByRole('button', {name: '同步所选人设', exact: true}).click();
    await dialog.waitFor({state: 'hidden'});
    assert.deepEqual(writes, [{ids: ['one', 'four']}]);
    assert.equal(await name.inputValue(), '未保存的编辑', 'list reload keeps unsaved draft');
    await open();await dialog.getByText('小码酱', {exact: true}).waitFor();
    assert.ok(await dialog.getByRole('button', {name: '同步所选人设', exact: true}).isDisabled());
    if (phone) {
      const settled = () => page.waitForFunction(() => {
        const el = document.querySelector('.local-personas-sheet');
        return el && !el.getAnimations().length && Math.abs(new DOMMatrixReadOnly(getComputedStyle(el).transform).m42) < 0.1;
      });
      await settled();
      const head = dialog.locator('.local-personas-heading'), rect = await head.boundingBox();
      await page.mouse.move(rect.x + 90, rect.y + 30);await page.mouse.down();
      await page.mouse.move(rect.x + 90, rect.y + 70, {steps: 5});
      assert.equal(await dialog.evaluate(el => new DOMMatrixReadOnly(getComputedStyle(el).transform).m42), 40, 'sheet follows the pointer without a delayed transition');
      await page.mouse.move(rect.x + 90, rect.y + 160, {steps: 6});await page.mouse.up();
      await dialog.waitFor({state: 'hidden'});
      await open();await dialog.getByText('小码酱', {exact: true}).waitFor();await settled();
      assert.equal(await dialog.evaluate(el => new DOMMatrixReadOnly(getComputedStyle(el).transform).m42), 0, 'reopened sheet clears drag state');
    }
    await dialog.getByRole('button', {name: '关闭', exact: true}).click();
    await dialog.waitFor({state: 'hidden'});
    assert.deepEqual(errors, []);
  });
});
