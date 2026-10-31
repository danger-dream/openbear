// Multi-select in the conversation tree on the real App without a browser: taps toggle instead
// of opening, "only the automatic ones of this folder" picks all 60 across pages, one can be
// unticked, and the batch delete sends exactly the rest and removes them. The selection bar's fit
// and the tree's overflow in light and dark stay in conversationSelection.browser.test.mjs.
import {window} from './domTestEnv.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import {openPage} from './testing/appHarness.mjs';
import {createSelectionBackend} from './conversationSelectionFixture.mjs';

const App = (await import('./App.vue')).default;

for (const width of [1440, 320]) test(`${width}px: a whole folder across pages, one unticked, the rest batch-deleted`, async t => {
  const phone = width < 761, backend = createSelectionBackend(), {batches} = backend;
  const {page, unmount} = await openPage(t, App, {url: '/chat?id=manual', width, height: 844, handler: backend.handle, onSocket: backend.socket,
    storage: {'openbear:conversation-sidebar-view': 'folders', 'openbear.notifications.prompt.v1': 'dismissed'}});
  if (phone) await page.getByRole('button', {name: '打开导航', exact: true}).filter({visible: true}).click();
  await page.locator('[data-tree-id="event-0"]').waitFor({state: 'visible'});
  await page.getByRole('button', {name: '多选会话', exact: true}).click();
  const bar = page.locator('.selection-bar');
  await bar.waitFor({state: 'visible'});
  assert.equal(await page.locator('[data-tree-id="manual"] .tree-check').count(), 1);
  // Ordinary taps toggle instead of opening the conversation.
  await page.locator('[data-tree-id="manual"] .tree-node-main').click();
  await page.waitForFunction(() => document.querySelector('.selection-count')?.textContent === '已选 1 个');
  await page.locator('[data-tree-id="manual"] .tree-node-main').click();
  await bar.getByRole('button', {name: '更多选择方式'}).click();
  await page.getByRole('menuitem', {name: '只选本目录中自动创建的'}).click();
  await page.waitForFunction(() => document.querySelector('.selection-count')?.textContent === '已选 60 个');
  // Rows beyond the first page are loaded so each can still be unticked.
  await page.locator('[data-tree-id="event-59"]').waitFor({state: 'attached'});
  assert.equal(await page.locator('[data-tree-id="manual"] .tree-check').getAttribute('aria-checked'), 'false');
  await page.locator('[data-tree-id="event-3"] .tree-check').click();
  await page.waitForFunction(() => document.querySelector('.selection-count')?.textContent === '已选 59 个');
  await bar.getByRole('button', {name: '删除'}).click();
  await page.locator('.el-message-box').getByRole('button', {name: '删除'}).click();
  await page.waitForFunction(() => document.querySelector('.selection-count')?.textContent === '已选 0 个');
  assert.equal(batches.length, 1);
  assert.equal(batches[0].action, 'delete');
  assert.equal(batches[0].conversationIds.length, 59);
  assert.ok(!batches[0].conversationIds.includes('event-3') && !batches[0].conversationIds.includes('manual'));
  await page.waitForFunction(() => !document.querySelector('[data-tree-id="event-0"]'));
  assert.equal(await page.locator('[data-tree-id="event-3"]').count(), 1);
  assert.equal(await page.locator('[data-tree-id="manual"]').count(), 1);
  await bar.getByRole('button', {name: '完成'}).click();
  await bar.waitFor({state: 'detached'});
  assert.equal(await page.locator('.tree-check').count(), 0);
  await unmount();
});
