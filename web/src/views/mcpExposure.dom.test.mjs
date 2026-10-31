// MCP tool selection without a browser, on the real McpView: search, select none / all, the
// budget confirmation (cancel sends nothing, confirm sends the server's confirmations), and an
// older backend keeping its server switch without the new endpoints. The page's sideways scroll
// and the 44px switch rows stay in mcpExposure.browser.test.mjs.
import {window} from '../domTestEnv.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import {openPage} from '../testing/appHarness.mjs';
import {settle} from '../testing/domPage.mjs';
import {createMcpBackend} from './mcpExposureFixture.mjs';

const McpView = (await import('./McpView.vue')).default;
// The drawer animates 170 tool rows; for each, Vue's <Transition> asks getComputedStyle for the
// transition timings, and happy-dom answers that by scanning the whole document for stylesheets
// (there are none: CSS goes to testing/cssIndex.mjs). Here computed style is the inline style,
// which is all happy-dom would find anyway; it takes the test from ~30 s to ~1 s.
const inlineStyle = el => new Proxy({}, {get: (_, key) => key === 'getPropertyValue' ? prop => el.style.getPropertyValue(prop) : typeof key === 'string' ? el.style[key] ?? '' : undefined});
window.getComputedStyle = globalThis.getComputedStyle = inlineStyle;

for (const width of [320, 1280]) test(`${width}px tool selection and cancellation`, async t => {
  const backend = createMcpBackend(), {writes} = backend;
  const {page, unmount} = await openPage(t, McpView, {url: '/__test', width, height: 844, handler: backend.handle});
  await page.locator('.mcp-card').waitFor();
  await page.getByRole('button', {name: '查看详情 →', exact: true}).click();
  const section = page.getByRole('region', {name: '工具启停'});
  await section.waitFor();
  assert.equal(await section.locator('.mcp-selection-row').count(), 170);
  await section.getByLabel('搜索工具', {exact: true}).fill('read_169_');
  assert.equal(await section.locator('.mcp-selection-row').count(), 1);
  await section.getByLabel('搜索工具', {exact: true}).fill('');
  await section.getByRole('button', {name: '全不选', exact: true}).click();
  assert.ok((await section.innerText()).includes('已启用 0 / 170'));
  await section.getByRole('button', {name: '保存工具启停', exact: true}).click();
  await page.getByText('工具启停已保存，新回合生效', {exact: true}).last().waitFor();
  await section.getByRole('button', {name: '全选', exact: true}).click();
  await section.getByRole('button', {name: '保存工具启停', exact: true}).click();
  await page.locator('.mcp-exposure-confirmation').waitFor();
  assert.ok((await page.locator('.mcp-exposure-confirmation').innerText()).includes('149,988 tokens'));
  const before = writes.length;
  await page.getByRole('button', {name: '先调整工具', exact: true}).click();
  // Let the click's handlers and any request they started finish instead of a fixed pause.
  await page.locator('.mcp-exposure-confirmation').waitFor({state: 'hidden'}); await settle(10);
  assert.equal(writes.length, before);
  await section.getByRole('button', {name: '保存工具启停', exact: true}).click();
  await page.getByRole('button', {name: '确认启用', exact: true}).click();
  await page.getByText('工具启停已保存，新回合生效', {exact: true}).last().waitFor();
  assert.deepEqual(writes.at(-1).body.exposureConfirmations, [{server: 'rea', fingerprint: 'fixture'}]);
  await unmount();
});

test('older backend still supports its original MCP switches without calling new APIs', async t => {
  const backend = createMcpBackend({legacy: true}), {calls} = backend;
  const {page, unmount} = await openPage(t, McpView, {url: '/__test', width: 390, height: 844, handler: backend.handle});
  await page.locator('.mcp-card').waitFor();
  await page.getByRole('button', {name: '查看详情 →', exact: true}).click();
  await page.locator('.mcp-drawer .el-switch').first().waitFor();
  assert.equal(await page.getByRole('region', {name: '工具启停'}).count(), 0);
  await page.locator('.mcp-drawer .el-switch').first().click();
  await page.waitForFunction(() => calls.includes('/api/mcp/servers/rea/enabled'));
  assert.ok(calls.includes('/api/mcp/servers/rea/enabled'));
  assert.ok(!calls.some(p => p.endsWith('/tools') || p.endsWith('/confirm-exposure')));
  await unmount();
});
