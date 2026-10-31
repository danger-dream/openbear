import test from 'node:test';
import assert from 'node:assert/strict';
import {startBrowserTestServer, waitForBrowserReady} from '../browserTestServer.mjs';
import {createRequire} from 'node:module';
const require = createRequire(import.meta.url);
let chromium;
const shots = process.env.OPENBEAR_SERVER_SHOTS || '';
const shot = async (page, name) => { if (shots) { await page.evaluate(() => Promise.all(document.getAnimations().filter(a => a.effect?.getComputedTiming().endTime !== Infinity).map(a => a.finished.catch(() => {})))); await page.screenshot({path: `${shots}/${name}.png`}); } };
try { ({chromium} = require(process.env.OPENBEAR_PLAYWRIGHT_MODULE || '/opt/openclaw/node_modules/playwright-core')); } catch {}

const html = '<html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head><body><div id="app"></div><script type="module" src="/@id/__x00__virtual:terminal-links-test"></script></body></html>';
const entry = `import {createApp, h} from 'vue'; import ElementPlus from 'element-plus'; import '/src/theme.js'; import 'element-plus/dist/index.css'; import 'element-plus/theme-chalk/dark/css-vars.css'; import '/src/style.css'; import '/src/dark-theme.css'; import '/src/mobile-overlays.css'; import '/src/servers/servers.css';
if (window.__dark) document.documentElement.classList.add('dark');
const Pane = (await import('/src/servers/TerminalPane.vue')).default;
const app = createApp({render: () => h('div', {class: 'servers-ui instance-ui', style: 'position: fixed; inset: 0'}, [h('div', {class: 'tw-panes', style: 'position: absolute; inset: 0'}, [h(Pane, {serverId: 'srv', active: true})])])});
app.use(ElementPlus); app.mount('#app');`;

import {OUTPUT} from './testing/terminalLinksFixture.mjs';

test('terminal OSC 8 links: real address on hover, host mismatch called out, click and tap confirm first', {skip: !chromium, timeout: 180000}, async t => {
  const fixture = await startBrowserTestServer(t, {name: 'terminal-links', entries: ['virtual:terminal-links-test', '/src/servers/TerminalPane.vue'], plugins: [{name: 'terminal-links-entry',
    resolveId(id) { if (id === 'virtual:terminal-links-test') return '\0virtual:terminal-links-test'; },
    load(id) { if (id === '\0virtual:terminal-links-test') return entry; },
    configureServer(server) { server.middlewares.use((req, res, next) => { if (req.url?.split('?')[0] === '/__test') { res.setHeader('content-type', 'text/html'); res.end(html); } else next(); }); }}]});
  const browser = await chromium.launch({executablePath: process.env.OPENBEAR_CHROME || '/usr/bin/google-chrome', headless: true, args: ['--no-sandbox']});
  t.after(async () => { try { await browser.close(); } finally { await fixture.close(); } });

  async function open({width, height, touch = false, dark = false}) {
    const context = await browser.newContext({viewport: {width, height}, hasTouch: touch, isMobile: touch, colorScheme: dark ? 'dark' : 'light'});
    const page = await context.newPage(), errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(isDark => { window.__dark = isDark; window.__opened = []; window.open = (...args) => { window.__opened.push(args); return null; }; }, dark);
    await page.routeWebSocket(/\/api\/servers\/srv\/terminal/, ws => { ws.send(JSON.stringify({type: 'status', state: 'open'})); ws.send(Buffer.from(OUTPUT)); ws.onMessage(() => {}); });
    await page.goto(`${fixture.base}/__test`);
    await waitForBrowserReady(page, page.locator('.xterm-rows'), errors);
    await page.waitForFunction(() => document.querySelector('.xterm-rows')?.textContent.includes('使用说明'));
    // The middle of a piece of shown text, found in the rendered rows.
    const at = text => page.evaluate(needle => {
      const walker = document.createTreeWalker(document.querySelector('.xterm-rows'), NodeFilter.SHOW_TEXT);
      for (let node; (node = walker.nextNode());) {
        const i = node.textContent.indexOf(needle);
        if (i < 0) continue;
        const range = document.createRange();
        range.setStart(node, i); range.setEnd(node, i + needle.length);
        const r = range.getBoundingClientRect();
        return {x: r.left + r.width / 2, y: r.top + r.height / 2};
      }
      return null;
    }, text);
    return {page, errors, at, close: () => context.close()};
  }

  // The texts of the tip and the confirmation, what opens and when are in terminalLinks.dom.test.mjs;
  // here the real terminal: pointing at and clicking the rendered link, where the tip and the box go.
  await t.test('desktop: hover shows the real address and the mismatch; leaving hides it; the click asks first', async () => {
    const {page, errors, at, close} = await open({width: 1280, height: 800});
    const tip = page.locator('.tw-link-tip');
    const fake = await at('paypal.com');
    assert.ok(fake, 'the link text is on screen');
    await page.mouse.move(fake.x, fake.y);
    await tip.waitFor();
    await shot(page, 'terminal-link-hover');
    const box = await tip.boundingBox();
    assert.ok(box.x >= 0 && box.x + box.width <= 1280 && box.y + box.height <= 800, 'the tip stays inside the terminal');
    await page.mouse.move(5, 790);
    await tip.waitFor({state: 'detached'});

    // The other link (its texts: the dom test), then back to the first: xterm finds each under the pointer.
    const plain = await at('使用说明');
    await page.mouse.move(plain.x, plain.y);
    await tip.waitFor();

    // The click asks; nothing opens until 打开.
    await page.mouse.move(fake.x, fake.y);
    await page.mouse.click(fake.x, fake.y);
    const dialog = page.locator('.tw-link-box');
    await dialog.waitFor();
    assert.ok((await dialog.boundingBox()).width <= 480, 'a dialog, not a banner across the page');
    await shot(page, 'terminal-link-confirm');
    await dialog.getByRole('button', {name: '打开'}).click();
    await dialog.waitFor({state: 'detached'});
    assert.deepEqual(errors, []);
    await close();
  });

  await t.test('phone (dark): a tap shows the real address and the warning before anything opens', async () => {
    const {page, errors, at, close} = await open({width: 390, height: 760, touch: true, dark: true});
    const fake = await at('paypal.com');
    await page.touchscreen.tap(fake.x, fake.y);
    const dialog = page.locator('.tw-link-box');
    await dialog.waitFor();
    await shot(page, 'terminal-link-phone');
    const box = await dialog.boundingBox();
    assert.ok(box.x >= 0 && box.x + box.width <= 390, `the confirmation fits the phone: ${JSON.stringify(box)}`);
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), 'no sideways scroll');
    const cancel = await dialog.getByRole('button', {name: '取消'}).boundingBox();
    assert.ok(cancel.height >= 30, 'the buttons are big enough to tap');
    await dialog.getByRole('button', {name: '取消'}).tap();
    await dialog.waitFor({state: 'detached'});
    assert.deepEqual(errors, []);
    await close();
  });
});
