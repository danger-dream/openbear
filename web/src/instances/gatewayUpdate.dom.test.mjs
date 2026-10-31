// Gateway update notice, one-click update and automatic update setting without a browser: the
// texts, button states and requests of gatewayUpdate.browser.test.mjs on the real InstancesView /
// GatewaySettings in happy-dom. Where the notice sits and how big it is stays in the browser test.
import {window} from '../domTestEnv.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import {openPage} from '../testing/appHarness.mjs';
import {createGatewayUpdateBackend, OLD, NEW} from './gatewayUpdateFixture.mjs';

const InstancesView = (await import('../views/InstancesView.vue')).default;

async function setup(t, {width = 1280, ...options} = {}) {
  const backend = createGatewayUpdateBackend(options);
  const {page, unmount} = await openPage(t, InstancesView, {url: '/__test', width, height: 844, handler: backend.handle});
  await page.locator('.ob-strip').waitFor();
  return {page, calls: backend.calls, finishUpdate: backend.finishUpdate, close: () => unmount()};
}
const openSettings = async page => {
  await page.locator('.ob-strip').getByRole('button', {name: '设置', exact: true}).click();
  const dialog = page.getByRole('dialog', {name: '访问网关'}); await dialog.waitFor(); return dialog;
};

test('desktop: the dialog updates in one click and switches automatic updates', async t => {
  const plain = await setup(t, {gatewayPatch: {build: NEW, updateAvailable: false, updateReasons: []}});
  assert.equal(await plain.page.locator('.ix-update').count(), 0);
  await plain.close();

  const h = await setup(t);
  const notice = h.page.locator('.ix-update');
  await notice.getByText('访问网关有新版本', {exact: true}).waitFor();
  await notice.getByText('主控会自动更新', {exact: true}).waitFor();
  const dialog = await openSettings(h.page);
  await dialog.getByText('访问网关有新版本', {exact: true}).waitFor();
  await dialog.getByText('构建 cccccccccccc', {exact: true}).waitFor();
  await dialog.getByText('主控带来了新版本的网关程序。', {exact: false}).waitFor();
  const auto = dialog.getByRole('checkbox', {name: /自动更新/});
  assert.equal(await auto.isChecked(), true);
  await dialog.getByText('发现新版本时自动更新', {exact: false}).waitFor();
  await dialog.getByRole('button', {name: '更新网关', exact: true}).click();
  // While it runs: one request, the button says so and the page notice cannot start another.
  await dialog.getByRole('button', {name: /正在更新/}).waitFor();
  await notice.getByText('正在更新…', {exact: true}).waitFor();
  assert.equal(await notice.getByRole('button', {name: '更新网关', exact: true}).isDisabled(), true);
  await h.finishUpdate();
  await dialog.getByText('已是最新', {exact: true}).waitFor();
  await dialog.getByText('构建 5beda9c64bd1', {exact: true}).waitFor();
  await dialog.getByText('上次更新成功（刚刚）', {exact: true}).waitFor();
  assert.equal(await dialog.getByRole('button', {name: '更新网关', exact: true}).count(), 0);
  await h.page.getByText('访问网关已更新', {exact: true}).waitFor();
  assert.equal(h.calls.filter(c => c.path === '/api/instance-gateway/update' && c.method === 'POST').length, 1);
  await auto.uncheck();
  await dialog.getByText('已关闭：有新版本时只提示', {exact: false}).waitFor();
  assert.deepEqual(h.calls.find(c => c.path === '/api/instance-gateway/auto-update').body, {autoUpdate: false});
  await dialog.getByRole('button', {name: '关闭', exact: true}).last().click(); await dialog.waitFor({state: 'hidden'});
  assert.equal(await notice.count(), 0);
  await h.close();
});

test('phone 390px: the notice updates from the page', async t => {
  const h = await setup(t, {width: 390});
  const notice = h.page.locator('.ix-update'), button = notice.getByRole('button', {name: '更新网关', exact: true});
  await notice.getByText('主控会自动更新', {exact: true}).waitFor();
  await button.click();
  await notice.getByText('正在更新…', {exact: true}).waitFor();
  assert.equal(await button.isDisabled(), true);
  await h.finishUpdate();
  await notice.waitFor({state: 'hidden'});
  const dialog = await openSettings(h.page);
  await dialog.getByText('上次更新成功（刚刚）', {exact: true}).waitFor();
  await h.close();
});

test('phone 320px: a failed automatic update says so on the page and why in settings; a failed click reports its error', async t => {
  const failed = {atMs: Date.now() - 180000, ok: false, auto: true, error: 'gateway_start_failed', build: OLD};
  const h = await setup(t, {width: 320, failUpdate: 'gateway_install_failed', gatewayPatch: {lastUpdate: failed, updateReasons: ['build_outdated', 'control_not_loopback']}});
  const notice = h.page.locator('.ix-update');
  await notice.getByText('自动更新失败', {exact: true}).waitFor();
  const dialog = await openSettings(h.page);
  await dialog.getByText('上次自动更新失败（3 分钟前）：网关已安装但服务启动失败，请查看服务器上 openbear-gateway 服务的日志。', {exact: true}).waitFor();
  await dialog.getByText('更新后，网关的管理接口改为只在服务器本机监听。', {exact: false}).waitFor();
  await dialog.getByRole('button', {name: '更新网关', exact: true}).click();
  await h.finishUpdate();
  await h.page.getByText('网关安装失败，请检查服务器后重试。', {exact: true}).first().waitFor();
  await dialog.getByText('上次更新失败（刚刚）：网关安装失败，请检查服务器后重试。', {exact: true}).waitFor();
  await dialog.getByRole('button', {name: '更新网关', exact: true}).waitFor();
  await h.close();
});

test('automatic updates off: only the notice and the button; an automatic update in progress disables it', async t => {
  const off = await setup(t, {width: 390, gatewayPatch: {autoUpdate: false}});
  const notice = off.page.locator('.ix-update');
  await notice.getByText('访问网关有新版本', {exact: true}).waitFor();
  assert.equal(await notice.locator('.instance-muted').count(), 0);
  const dialog = await openSettings(off.page);
  assert.equal(await dialog.getByRole('checkbox', {name: /自动更新/}).isChecked(), false);
  await off.close();
  const running = await setup(t, {gatewayPatch: {updating: true}});
  await running.page.locator('.ix-update').getByText('正在更新…', {exact: true}).waitFor();
  assert.equal(await running.page.locator('.ix-update').getByRole('button', {name: '更新网关', exact: true}).isDisabled(), true);
  await running.close();
});
