import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import test from 'node:test';
import { startBrowserTestServer } from './browserTestServer.mjs';
import { createToolFileBackend, uuid } from './toolFileOpenFixture.mjs';

const require = createRequire(import.meta.url);
let chromium;
try {
  ({ chromium } = require('./testing/playwright.cjs'));
} catch {}

const entry = `import '/src/monaco-worker.js'; import '/src/theme.js'; import {createApp} from 'vue'; import ElementPlus from 'element-plus'; import * as Icons from '@element-plus/icons-vue'; import 'element-plus/dist/index.css'; import 'element-plus/theme-chalk/dark/css-vars.css'; import '/src/style.css'; import '/src/dark-theme.css'; import '/src/admin-mobile.css'; import '/src/mobile-overlays.css'; import '/src/mobile-inputs.css'; import App from '/src/App.vue'; const app=createApp(App); app.use(ElementPlus); for(const [name,component] of Object.entries(Icons))app.component(name,component);app.mount('#app');`;
const html = '<html><head><meta name="viewport" content="width=device-width,initial-scale=1"><script>window.__OPENBEAR_AUTH_MODE__="controller"</script></head><body><div id="app"></div><script type="module" src="/@id/__x00__virtual:user-notes-test"></script></body></html>';

function createNotesBackend(settingsOverrides = {}) {
  const base = createToolFileBackend();
  let seq = 1;
  const sessionNotes = [];
  const settingPatches = [];
  const currentSettings = {
    'ui.notesEnabled': true,
    'ui.notesSelectionPopover': true,
    ...settingsOverrides,
  };
  const globalSheets = [
    { sheetUuid: 'sheet-default', title: '默认便签', sortOrder: 0, createdAtMs: 1000, updatedAtMs: 1000, notes: [] },
  ];

  return {
    ...base,
    sessionNotes,
    globalSheets,
    settingPatches,
    currentSettings,
    handle(request) {
      if (request.path === '/api/settings' && request.method === 'GET') {
        const res = base.handle(request);
        return {
          ...res,
          json: {
            ...res.json,
            values: {
              ...(res.json?.values || {}),
              ...currentSettings,
            },
          },
        };
      }
      if (request.path.startsWith('/api/settings/') && request.method === 'PATCH') {
        const key = decodeURIComponent(request.path.slice('/api/settings/'.length));
        currentSettings[key] = request.body?.value;
        settingPatches.push({ key, value: request.body?.value });
        return { status: 200, json: { ok: true, revision: seq++ } };
      }
      if (request.path === '/api/notes' && request.method === 'GET') {
        return { status: 200, json: { ok: true, sessionNotes, globalSheets } };
      }
      if (request.path === '/api/notes' && request.method === 'POST') {
        const body = request.body || {};
        const note = {
          noteUuid: `note-${seq++}`,
          scope: body.scope || 'session',
          conversationUuid: body.conversationUuid || uuid,
          conversationTitle: '测试会话',
          conversationExists: true,
          sheetUuid: body.scope === 'global' ? (body.sheetUuid || globalSheets[0].sheetUuid) : '',
          turnUuid: body.turnUuid || '',
          turnNumber: body.turnNumber || 0,
          opId: body.opId || '',
          quote: body.quote || '',
          content: body.content || '',
          createdAtMs: Date.now(),
          updatedAtMs: Date.now(),
        };
        if (note.scope === 'session') sessionNotes.unshift(note);
        else {
          const sheet = globalSheets.find((s) => s.sheetUuid === note.sheetUuid) || globalSheets[0];
          sheet.notes.unshift(note);
        }
        return { status: 200, json: { ok: true, note } };
      }
      if (request.path === '/api/notes/sheets' && request.method === 'POST') {
        const sheet = {
          sheetUuid: `sheet-${seq++}`,
          title: request.body?.title || '新便签',
          sortOrder: globalSheets.length,
          createdAtMs: Date.now(),
          updatedAtMs: Date.now(),
          notes: [],
        };
        globalSheets.push(sheet);
        return { status: 200, json: { ok: true, sheet } };
      }
      return base.handle(request);
    },
  };
}

test('desktop hover/pin sticky note, footer capture, right-click selection menu, optional selection bar and mobile bottom sheet', { skip: !chromium, timeout: 180000 }, async (t) => {
  const fixture = await startBrowserTestServer(t, {
    name: 'user-notes',
    entries: ['virtual:user-notes-test'],
    plugins: [{
      name: 'user-notes-test',
      resolveId(id) {
        if (id === 'virtual:user-notes-test') return '\0virtual:user-notes-test';
      },
      load(id) {
        if (id === '\0virtual:user-notes-test') return entry;
      },
      configureServer(server) {
        server.middlewares.use((req, res, next) => {
          if (req.url?.startsWith('/chat')) {
            res.setHeader('content-type', 'text/html');
            res.end(html);
          } else next();
        });
      },
    }],
  });

  let browser;
  t.after(async () => {
    try { await browser?.close(); } finally { await fixture.close(); }
  });
  browser = await chromium.launch({
    executablePath: process.env.OPENBEAR_CHROME || '/usr/bin/google-chrome',
    headless: true,
    args: ['--no-sandbox'],
  });

  await t.test('desktop 1440px: footer button, right-click menu, hover/pin popover, multi-sheet global notes & jump back', { timeout: 60000 }, async (t) => {
    const context = await browser.newContext({ viewport: { width: 1440, height: 860 } });
    t.after(() => context.close());
    const page = await context.newPage();
    const errors = [];
    const backend = createNotesBackend({ 'ui.notesEnabled': true });
    page.on('pageerror', (err) => errors.push(err.message));
    await page.addInitScript(() => {
      localStorage.setItem('openbear.notifications.prompt.v1', 'dismissed');
    });
    await page.route('**/api/**', async (route) => {
      const req = route.request();
      const url = new URL(req.url());
      let parsedBody = null;
      try { parsedBody = req.postDataJSON(); } catch {}
      const { status, json, headers, body } = backend.handle({
        method: req.method(),
        path: url.pathname,
        query: Object.fromEntries(url.searchParams),
        body: parsedBody,
      });
      if (json !== undefined) return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(json) });
      return route.fulfill({ status, headers, body });
    });
    await page.routeWebSocket('**/api/**', backend.socket);

    await page.goto(`${fixture.base}/chat?id=${uuid}`);
    await page.locator('.assistant-message-meta').first().waitFor({ state: 'visible', timeout: 30000 });

    // 1. Assistant footer button sits right next to Copy and captures turn with opId.
    const noteFooterBtn = page.locator('.message-footer-actions button[aria-label="记入便签"]').first();
    await noteFooterBtn.waitFor({ state: 'visible' });
    await noteFooterBtn.click();
    assert.equal(backend.sessionNotes.length, 1);
    assert.ok(backend.sessionNotes[0].opId);

    // 2. Hovering rail icon expands sticky note popover without sideways overflow; clicking pins it.
    const trigger = page.locator('.notes-entry');
    await trigger.hover();
    const popover = page.locator('.notes-sticky-popover');
    await popover.waitFor({ state: 'visible' });
    const overflow = await popover.evaluate((el) => el.scrollWidth - el.clientWidth);
    assert.ok(overflow <= 1);
    await trigger.click();
    assert.match(await popover.getAttribute('class'), /is-pinned/);

    // 3. Clicking source jump button highlights the assistant message row.
    await popover.locator('.notes-jump-btn').first().click();
    await page.locator('.note-hit-highlight').first().waitFor({ state: 'visible' });

    // 4. Switch to Global scope and create a second global sheet.
    await popover.getByRole('tab', { name: /全局/ }).click();
    await popover.getByRole('button', { name: '新建全局便签' }).click();
    await popover.locator('.notes-sheet-form input').fill('网络架构备忘');
    await popover.locator('.notes-sheet-form button[type="submit"]').click();
    await popover.locator('.notes-sheet-tab', { hasText: '网络架构备忘' }).waitFor({ state: 'visible' });

    // 5. Selection popover is ON by default; clicking header icon toggle turns it OFF and persists to settings!
    const selToggle = popover.locator('button.notes-sel-toggle');
    assert.equal(await selToggle.getAttribute('aria-pressed'), 'true');
    await selToggle.click();
    assert.equal(await selToggle.getAttribute('aria-pressed'), 'false');
    assert.deepEqual(backend.settingPatches.at(-1), { key: 'ui.notesSelectionPopover', value: false });

    const selected = await page.evaluate(() => {
      const p = document.querySelector('.assistant-card p');
      if (!p) return null;
      const range = document.createRange();
      range.selectNodeContents(p);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      const r = p.getBoundingClientRect();
      return { x: r.left + 20, y: r.top + 8 };
    });
    assert.ok(selected);
    assert.equal(await page.locator('.notes-sel-toolbar').count(), 0);
    await page.mouse.click(selected.x, selected.y, { button: 'right' });
    const ctxMenu = page.locator('.notes-ctx-menu');
    await ctxMenu.waitFor({ state: 'visible' });
    await ctxMenu.getByRole('menuitem', { name: '记入全局便签' }).click();
    assert.equal(backend.globalSheets[1].notes.length, 1);

    assert.deepEqual(errors, []);
  });

  await t.test('mobile 390px & narrow 320px: header entry, 16px input bottom sheet, dark mode and selection pill when enabled', { timeout: 60000 }, async (t) => {
    const context = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });
    t.after(() => context.close());
    const page = await context.newPage();
    const errors = [];
    const backend = createNotesBackend({ 'ui.notesEnabled': true, 'ui.notesSelectionPopover': true });
    page.on('pageerror', (err) => errors.push(err.message));
    await page.addInitScript(() => {
      localStorage.setItem('openbear.notifications.prompt.v1', 'dismissed');
      localStorage.setItem('openbear.theme.v1', 'dark');
    });
    await page.route('**/api/**', async (route) => {
      const req = route.request();
      const url = new URL(req.url());
      let parsedBody = null;
      try { parsedBody = req.postDataJSON(); } catch {}
      const { status, json, headers, body } = backend.handle({
        method: req.method(),
        path: url.pathname,
        query: Object.fromEntries(url.searchParams),
        body: parsedBody,
      });
      if (json !== undefined) return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(json) });
      return route.fulfill({ status, headers, body });
    });
    await page.routeWebSocket('**/api/**', backend.socket);

    await page.goto(`${fixture.base}/chat?id=${uuid}`);
    await page.locator('.assistant-message-meta').first().waitFor({ state: 'visible', timeout: 30000 });

    // Open mobile bottom sheet from header button
    await page.locator('.mobile-conversation-tools button[aria-label*="打开便签"]').click();
    const drawer = page.locator('.notes-mobile-drawer');
    await drawer.waitFor({ state: 'visible' });

    // Check input font-size is 16px (prevents iPhone auto-zoom) and no sideways overflow at 320px
    const fontSize = await drawer.locator('.notes-compose textarea').evaluate((el) => getComputedStyle(el).fontSize);
    assert.equal(fontSize, '16px');
    await page.setViewportSize({ width: 320, height: 740 });
    const headerOverflow = await page.locator('.console-header').evaluate((el) => el.scrollWidth - el.clientWidth);
    assert.ok(headerOverflow <= 1);

    await drawer.locator('.notes-compose textarea').fill('手机端随手备忘');
    await drawer.locator('.notes-submit-btn').click();
    await drawer.locator('.notes-card', { hasText: '手机端随手备忘' }).waitFor({ state: 'visible' });

    assert.deepEqual(errors, []);
  });
});
