// Background conversations waiting for an answer, on the real App without a browser: the waiting
// counts in recents, the tree and the phone entry, the pending panel, opening the exact form,
// keeping an unsent answer, and clearing every marker when the work stops waiting. Composer
// position, panel overflow, dark, the toast's pointer events, the desktop hiding of the phone entry
// and the touch drag stay in pendingConversations.browser.test.mjs.
import {window} from './domTestEnv.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import {openPage} from './testing/appHarness.mjs';
import {createPendingBackend} from './pendingConversationsFixture.mjs';

const App = (await import('./App.vue')).default;

for (const width of [1440, 320]) test(`${width}px: waiting work is listed, opens its exact form and clears in real time`, async t => {
  const backend = createPendingBackend();
  const {page, calls, unmount} = await openPage(t, App, {url: '/chat?id=current', width, height: 844, handler: backend.handle, onSocket: backend.socket,
    storage: {'openbear:conversation-sidebar-view': 'folders', 'openbear.notifications.prompt.v1': 'dismissed'}});
  const phone = width < 761;
  const here = () => new URL(window.location.href);
  await page.waitForFunction(() => document.querySelector('.recent-waiting-count')?.textContent.includes('待处理 1'));
  await page.locator('[data-tree-id="root"] .tree-waiting-state').waitFor({state: 'attached'});
  if (phone) {
    assert.equal(await page.locator('#openbear-sidebar').evaluate(el => el.classList.contains('is-open')), false);
    await page.locator('.mobile-pending-entry').click();
  } else await page.locator('.recent-waiting-count').click();
  const panel = page.locator('.pending-conversations');
  await panel.waitFor({state: 'visible'});
  assert.equal(await panel.locator('[data-pending-conversation]').count(), 1);
  assert.equal(await panel.locator('[data-interaction-id]').count(), 3);
  await panel.locator('[data-interaction-id="choose-two"]').click();
  await page.waitForFunction(() => here().searchParams.get('id') === 'background');
  await page.waitForFunction(() => document.activeElement?.classList.contains('web-confirm-card') && document.activeElement.dataset.interactionId === 'choose-two');
  assert.equal(await page.locator('.web-confirm-card').count(), 3);
  const questionnaire = page.locator('[data-questionnaire-id="questionnaire-three"]');
  await questionnaire.locator('textarea').fill('未提交的问卷回答');
  if (phone) await page.getByRole('button', {name: '打开导航', exact: true}).filter({visible: true}).click();
  if (phone) await page.locator('.sidebar-new-session').click();
  else {
    await page.getByRole('button', {name: '新建会话或目录', exact: true}).click();
    await page.getByRole('menuitem', {name: '新建临时会话', exact: true}).click();
  }
  await page.waitForFunction(() => here().searchParams.get('id') === 'local:new');
  await page.waitForFunction(() => document.querySelectorAll('.web-confirm-card').length === 0);
  assert.equal(await page.locator('.recent-waiting-count').textContent(), '待处理 1');
  await (phone ? page.locator('.mobile-pending-entry') : page.locator('.recent-waiting-count')).click();
  await panel.locator('[data-interaction-id="questionnaire-three"]').click();
  await page.waitForFunction(() => here().searchParams.get('id') === 'background');
  await page.waitForFunction(() => document.activeElement?.dataset.interactionId === 'questionnaire-three');
  assert.equal(await questionnaire.locator('textarea').inputValue(), '未提交的问卷回答');
  assert.equal(await page.locator('.web-confirm-card').count(), 3);
  assert.equal(await page.locator('.recent-waiting-count').textContent(), '待处理 1');
  if (phone) await page.getByRole('button', {name: '打开导航', exact: true}).filter({visible: true}).click();
  assert.equal(await page.locator('[data-tree-id="background"] .tree-waiting-state').textContent(), '待确认 · 3项');
  assert.equal(await page.locator('[data-tree-id="background"] .running-leaf').count(), 0);
  await page.locator('[data-tree-id="root"] .tree-chevron').click();
  assert.equal(await page.locator('[data-tree-id="root"] .tree-waiting-state').textContent(), '待处理 1');
  if (phone) await page.locator('.app-sidebar-backdrop').click();
  await (phone ? page.locator('.mobile-pending-entry') : page.locator('.recent-waiting-count')).click();
  await panel.waitFor({state: 'visible'});
  await page.keyboard.press('Escape');
  await panel.waitFor({state: 'hidden'});
  backend.stopWaiting();
  await page.waitForFunction(() => document.querySelector('.recent-waiting-count')?.textContent === '待处理 0');
  assert.equal(await page.locator('.mobile-pending-entry').count(), 0);
  assert.equal(await page.locator('.tree-waiting-state').count(), 0);
  assert.ok(!calls.some(c => c.method !== 'GET' && (c.path.endsWith('/answer') || c.path.endsWith('/send'))));
  await unmount();
});
