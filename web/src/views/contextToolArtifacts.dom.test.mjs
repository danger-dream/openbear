// The tool-result setting row without a browser, on the real SettingsView: editing it sends one
// PATCH for that setting and shows 0 as "关闭（0 token）"; an older server without the setting
// keeps saving the compaction ratio and shows no tool-result row. The row's fit and contrast in
// light/dark stay in contextToolArtifacts.browser.test.mjs.
import {window} from '../domTestEnv.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import {openPage} from '../testing/appHarness.mjs';
import {createSettingsBackend, TOOL_RESULT_PATH} from './contextToolArtifactsFixture.mjs';

window.__OPENBEAR_AUTH_MODE__ = 'controller';
const SettingsView = (await import('./SettingsView.vue')).default;

// The save path does not depend on width or theme: the rows of the browser test run once each.
for (const newBackend of [true, false]) test(`${newBackend ? 'new' : 'old'} backend saves the setting it knows`, async t => {
  const backend = createSettingsBackend({newBackend}), {calls} = backend;
  const {page, unmount} = await openPage(t, SettingsView, {url: '/__test', width: 390, height: 844, handler: backend.handle});
  const title = newBackend ? '工具结果落盘阈值' : '压缩触发比例';
  const row = page.locator('.settings-row').filter({has: page.locator('h4', {hasText: title})});
  await row.waitFor();
  await row.locator('.value-pill').click();
  await row.locator('input').fill(newBackend ? '0' : '0.6');
  await row.locator('input').press('Enter');
  await row.locator('.value-pill').filter({hasText: newBackend ? '关闭（0 token）' : '0.6'}).waitFor();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].path, newBackend ? TOOL_RESULT_PATH : 'agent.compactRatio');
  if (!newBackend) assert.equal(await page.getByText('工具结果落盘阈值', {exact: true}).count(), 0);
  await unmount();
});
