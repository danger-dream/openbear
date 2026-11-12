import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdirSync} from 'node:fs';
import {startBrowserTestServer} from '../browserTestServer.mjs';

const require = createRequire(import.meta.url);
let chromium;
try {({chromium} = require('../testing/playwright.cjs'));} catch {}
// The add-server dialog as the servers page opens it.
const entry = `
import {createApp,h} from 'vue';
import ElementPlus,{ElDialog} from 'element-plus';
import 'element-plus/dist/index.css';
import 'element-plus/theme-chalk/dark/css-vars.css';
import '/src/style.css'; import '/src/dark-theme.css'; import '/src/mobile-overlays.css';
import '/src/instances/instances.css'; import '/src/servers/servers.css';
import ServerEditor from '/src/servers/ServerEditor.vue';
createApp({render:()=>h(ElDialog,{modelValue:true,title:'添加服务器',width:'560px',class:'admin-dialog instance-dialog instance-ui servers-ui',appendToBody:true},()=>
 h(ServerEditor,{server:null,groups:[],tags:[],regions:[],settings:{accessPoints:[]}}))}).use(ElementPlus).mount('#app');
`;
const html = '<html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div id="app"></div><script type="module" src="/@id/__x00__virtual:server-editor"></script></body></html>';
const shots = process.env.OPENBEAR_SERVER_EDITOR_SHOTS;

test('a one-line check in the add-server dialog sits on the same middle line as its text', {skip: !chromium, timeout: 120000}, async t => {
  const fixture = await startBrowserTestServer(t, {name: 'server-editor', entries: ['virtual:server-editor'], plugins: [{
    name: 'server-editor',
    resolveId(id) {if (id === 'virtual:server-editor') return '\0virtual:server-editor';},
    load(id) {if (id === '\0virtual:server-editor') return entry;},
    configureServer(server) {server.middlewares.use((req, res, next) => {if (req.url?.startsWith('/fixture')) {res.setHeader('content-type', 'text/html'); res.end(html);} else next();});},
  }]});
  let browser;
  t.after(async () => {try {await browser?.close();} finally {await fixture.close();}});
  browser = await chromium.launch({executablePath: process.env.OPENBEAR_CHROME || '/usr/bin/google-chrome', headless: true, args: ['--no-sandbox']});
  if (shots) mkdirSync(shots, {recursive: true});
  for (const width of [1440, 390]) await t.test(`${width}px`, {timeout: 45000}, async t => {
    const context = await browser.newContext({viewport: {width, height: 900}, isMobile: width < 761, hasTouch: width < 761});
    t.after(() => context.close());
    const page = await context.newPage();
    await page.goto(`${fixture.base}/fixture`);
    const label = page.locator('.instance-form label.ob-check', {hasText: '自动续费'});
    await label.waitFor();
    const offset = await label.evaluate(el => {
      const box = el.querySelector('input').getBoundingClientRect();
      const range = document.createRange();
      range.selectNodeContents([...el.childNodes].find(node => node.nodeType === 3 && node.textContent.trim()));
      const lines = [...range.getClientRects()];
      const first = lines[0];
      return {checkbox: box.top + box.height / 2, text: first.top + first.height / 2, lines: lines.length};
    });
    assert.ok(Math.abs(offset.checkbox - offset.text) <= 1, `checkbox middle ${offset.checkbox} vs text middle ${offset.text}`);
    if (shots) await label.screenshot({path: `${shots}/auto-renew-${width}.png`});
  });
});
