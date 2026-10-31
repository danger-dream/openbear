// The model menu of the run config without a browser, on the real ConsoleView: switching keeps
// the original conversation (no new one, its messages stay), sends one save per click, reports
// "next run" for an active run, and a failed save keeps the old model and lets the user retry.
// The menu's fit on narrow and dark phones: modelSwitch.browser.test.mjs.
import {window} from './domTestEnv.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import {h} from 'vue';
import {openPage} from './testing/appHarness.mjs';
import {createModelSwitchBackend} from './modelSwitchFixture.mjs';

const ConsoleView = (await import('./views/consoleView/ConsoleView.vue')).default;

// The switch menu itself runs at every width; the server-side branches
// (deleted source model, active run, failed save) do not depend on width.
const cases = [[1440, 'idle'], [390, 'idle'], [320, 'idle'], [390, 'source-deleted'], [390, 'running'], [390, 'failed-save']];
for (const [width, scenario] of cases) test(`${width}px ${scenario}`, async t => {
  const backend = createModelSwitchBackend(scenario), created = [];
  const root = {render: () => h(ConsoleView, {conversationUuid: 'original', onConversationCreated: id => created.push(id)})};
  const {page, unmount} = await openPage(t, root, {url: '/__model-test', width, height: 900, dark: width === 320, handler: backend.handle, onSocket: backend.socket});
  await page.getByText('原会话已经完成的结果', {exact: true}).waitFor();
  await page.getByRole('button', {name: '运行配置', exact: true}).click();
  await page.getByRole('button', {name: 'claude（target/claude）', exact: true}).click();
  if (scenario === 'failed-save') {
    await page.locator('.el-message--error').waitFor();
    assert.match(await page.locator('.run-config-chip-model').innerText(), /gpt/);
    assert.equal(await page.locator('.el-message--success').count(), 0);
    assert.equal(await page.locator('.el-message-box').count(), 0);
    // The menu may still be closing or already closed after the failed save; toggling it blindly
    // would shut it again.
    const retry = page.getByRole('button', {name: 'claude（target/claude）', exact: true});
    if (!await retry.count()) await page.getByRole('button', {name: '运行配置', exact: true}).click();
    await retry.click();
  }
  await page.waitForFunction(() => document.querySelector('.run-config-chip-model')?.textContent.includes('claude'));
  const message = page.locator('.el-message--success');
  await message.waitFor();
  assert.match(await message.innerText(), backend.running ? /下一轮运行生效/ : /已切换模型/);
  assert.equal(await page.getByText('原会话已经完成的结果', {exact: true}).count(), 1);
  assert.equal(await page.getByText('原会话的任务要求', {exact: true}).count(), 1);
  assert.deepEqual(backend.writes, Array.from({length: scenario === 'failed-save' ? 2 : 1}, () => ({path: '/api/conversations/original/model', body: {model: 'target/claude'}})));
  assert.deepEqual(created, []);
  assert.equal(await page.locator('.el-message-box').count(), 0);
  await unmount();
});
