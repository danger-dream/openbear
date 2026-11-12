import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {startBrowserTestServer} from '../browserTestServer.mjs';

const require = createRequire(import.meta.url);
let chromium;
try {({chromium} = require('../testing/playwright.cjs'));} catch {}
const conversation = '11111111-2222-3333-4444-555555555555', artifact = '66666666-7777-8888-9999-000000000000';
const entry = `
import {createApp,h} from 'vue';
import 'element-plus/dist/index.css';
import '/src/style.css'; import '/src/dark-theme.css'; import '/src/mobile-overlays.css';
import ArtifactPreview from '/src/artifacts/ArtifactPreview.vue';
createApp({render:()=>h('main',[h(ArtifactPreview)])}).mount('#app');
window.__open=()=>window.dispatchEvent(new CustomEvent('openbear:preview-artifact',{detail:{href:'/api/conversations/${conversation}/artifacts/${artifact}/content',label:'chart.png'}}));
`;
const html = '<html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div id="app"></div><script type="module" src="/@id/__x00__virtual:artifact-preview-close"></script></body></html>';

// A read-only preview closes from its backdrop, as the image viewer does; the content area keeps it open.
test('the artifact preview closes when its backdrop is clicked, on desktop and phone', {skip: !chromium, timeout: 120000}, async t => {
  const fixture = await startBrowserTestServer(t, {name: 'artifact-preview-close', entries: ['virtual:artifact-preview-close'], plugins: [{
    name: 'artifact-preview-close',
    resolveId(id) {if (id === 'virtual:artifact-preview-close') return '\0virtual:artifact-preview-close';},
    load(id) {if (id === '\0virtual:artifact-preview-close') return entry;},
    configureServer(server) {server.middlewares.use((req, res, next) => {if (req.url?.startsWith('/fixture')) {res.setHeader('content-type', 'text/html'); res.end(html);} else next();});},
  }]});
  let browser;
  t.after(async () => {try {await browser?.close();} finally {await fixture.close();}});
  browser = await chromium.launch({executablePath: process.env.OPENBEAR_CHROME || '/usr/bin/google-chrome', headless: true, args: ['--no-sandbox']});
  for (const width of [1440, 390]) await t.test(`${width}px`, {timeout: 45000}, async () => {
    const phone = width < 761;
    const context = await browser.newContext({viewport: {width, height: 844}, hasTouch: phone, isMobile: phone});
    const page = await context.newPage();
    await page.route('**/api/**', route => route.fulfill({status: 404, contentType: 'application/json', body: '{"ok":false,"error":"not_found"}'}));
    await page.goto(`${fixture.base}/fixture`);
    const dialog = page.locator('.artifact-preview-dialog');
    await page.evaluate(() => window.__open());
    await dialog.waitFor({state: 'visible'});
    const box = await dialog.boundingBox();
    // Inside the dialog nothing closes it.
    await page.mouse.click(box.x + box.width / 2, box.y + Math.min(box.height - 4, 60));
    await page.waitForTimeout(300);
    assert.ok(await dialog.isVisible(), 'a click inside keeps the preview open');
    // The backdrop around it closes it: below it on a phone sheet, beside it on a desktop.
    const outside = phone ? {x: width / 2, y: Math.max(4, box.y - 8)} : {x: Math.max(4, box.x - 12), y: box.y + box.height / 2};
    if (phone && box.y < 8) {
      // A fullscreen phone preview leaves no backdrop to click: it keeps its close button.
      assert.ok(await page.getByRole('button', {name: /关闭/}).first().isVisible());
    } else {
      await page.mouse.click(outside.x, outside.y);
      await dialog.waitFor({state: 'hidden'});
    }
    await context.close();
  });
});
