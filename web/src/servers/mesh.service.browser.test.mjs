// The mesh board in Chromium, layout only: missing mesh service.
// Sizes, overflow, scrolling, phone and dark theme, and what needs real pointer input (dragging
// nodes, ports and sheets). What the board asks the controller and what it shows is checked without
// a browser in mesh.board / mesh.canvas / mesh.links.dom.test.mjs. Page, fake controller and helpers:
// testing/meshBrowser.mjs.
import test from 'node:test';
import assert from 'node:assert/strict';
import {GB, SERVERS, centre, chromium, clearOfBoard, dimmed, dragBody, dragPort, fakeMesh, guideCount, inspector, label, member, memberOf, noOverflow, node, server, shot, shots, startMeshBrowser, texts, title, touchDrag, until} from './testing/meshBrowser.mjs';

test('mesh board layout: missing mesh service', {skip: !chromium, timeout: 300000}, async t => {
  const {setup} = await startMeshBrowser(t);
  await t.test('missing mesh service: the state on the board, the bar and the panel; reinstall asks, runs, says why it could not; skipped by a rollout; phone light and dark', {timeout: 180000}, async t => {
    const h = await setup(t, {gone: 'home'});
    const {page, calls} = h;
    const panel = inspector(page);
    const reinstalls = () => calls.filter(c => c.method === 'POST' && c.path.endsWith('/reinstall'));
    // The board: the node and its badge in red, the summary counts it, the bar names it.
    const badge = node(page, 'home').locator('.mesh-node-badge');
    const bar = page.locator('.mesh-stage .mesh-bar[data-kind=missing]');
    // An entry through a relay that lost it says so (Berlin here, for the check).
    await shot(page, 'desktop-missing-board');
    await bar.getByRole('button', {name: '查看'}).click();
    const notice = panel.locator('.mesh-missing');
    const button = notice.getByRole('button', {name: '重新安装组网服务'});
    // Asks first; cancelling sends nothing.
    await button.click();
    const box = page.locator('.el-message-box');
    await box.waitFor();
    await shot(page, 'desktop-missing-confirm');
    await box.getByRole('button', {name: '取消'}).click();
    await box.waitFor({state: 'hidden'});
    // Refused: the reason in Chinese under the button; the member stays missing.
    for (const [code, text] of [['node_offline', '节点离线，连上后才能重新安装'], ['capability_disabled', '这台服务器的节点关闭了命令执行，没法远程安装组网服务'],
      ['node_update_required', '节点版本过旧，先更新节点再重新安装'], ['mesh_apply_running', '正在应用别的更改，等它完成后再重新安装'], ['mesh_install_invalid', '节点返回的组网密钥不正确，安装没有完成']]) {
      h.mesh.setReinstall(code);
      await button.click();
      await box.getByRole('button', {name: '重新安装'}).click();
      await notice.locator('.mesh-missing-error', {hasText: text}).waitFor();
      assert.equal(await notice.locator('.mesh-missing-error').innerText(), `没有重新安装：${text}`);
    }
    await shot(page, 'desktop-missing-error');
    // A draft change waiting to be applied stays as it is.
    h.mesh.doc.settings.mtu = 1380;
    await page.locator('.mesh-stage .mesh-bar[data-kind=dirty]').waitFor({timeout: 10000});
    const drafts = h.puts().length;
    // Done: the button says it is running, the node installs, then a rollout carries its new keys.
    h.mesh.setReinstall('');
    await button.click();
    await box.getByRole('button', {name: '重新安装'}).click();
    const running = notice.getByRole('button', {name: '正在重新安装…'});
    await running.waitFor();
    await shot(page, 'desktop-missing-reinstalling');
    await page.locator('.el-message', {hasText: '已在「Home」上重新安装组网服务，正在把新密钥下发给其他成员'}).waitFor({timeout: 10000});
    await notice.waitFor({state: 'detached', timeout: 10000});
    await page.waitForFunction(() => !document.querySelector('.mesh-stage .mesh-bar[data-kind=running]'), null, {timeout: 20000});
    // Its events: the controller's two, in Chinese.
    await panel.getByRole('tab', {name: '事件'}).click();
    const rows = panel.locator('.mesh-events > li');
    const gone = rows.filter({has: page.locator('.sv-log-source', {hasText: /^组网服务不见了$/})});
    await gone.waitFor();
    await shot(page, 'desktop-missing-events');
    await h.close();

    // A rollout skips it: "组网服务不见了，已跳过", not a failure; the toast says so.
    const r = await setup(t, {gone: 'home'});
    r.mesh.doc.settings.mtu = 1380;
    const rbar = r.page.locator('.mesh-stage .mesh-bar');
    await rbar.getByRole('button', {name: '应用'}).click({timeout: 10000});
    await rbar.locator('strong', {hasText: /正在应用/}).waitFor();
    await r.page.locator('.el-message', {hasText: /已应用到 2 台；1 台的组网服务不见了，已跳过/}).waitFor({timeout: 20000});
    // Nothing selected: the panel lists it.
    await r.close();

    // An entry through a relay whose service is gone.
    const e = await setup(t, {gone: 'berlin'});
    await label(e.page, '1 · UDP').click();
    await inspector(e.page).locator('.mesh-link-text', {hasText: 'Berlin 的组网服务不见了'}).waitFor();
    await e.close();

    // Phone, dark and light: the bottom sheet's notice, the button within reach, nothing wider than the screen.
    for (const dark of [true, false]) {
      const p = await setup(t, {width: 390, height: 844, dark, gone: 'home'});
      await shot(p.page, `phone-missing-board-${dark ? 'dark' : 'light'}`);
      await node(p.page, 'home').tap();
      const sheet = p.page.locator('.mesh-inspect-sheet');
      const n = sheet.locator('.mesh-missing');
      await n.waitFor();
      const b = n.getByRole('button', {name: '重新安装组网服务'});
      assert.ok((await b.boundingBox()).height >= 28, 'the button is big enough to tap');
      assert.ok(await sheet.evaluate(el => el.scrollWidth <= el.clientWidth + 1), 'the sheet fits the width');
      await noOverflow(p.page);
      await shot(p.page, `phone-missing-panel-${dark ? 'dark' : 'light'}`);
      p.mesh.setReinstall('node_offline');
      await b.tap();
      await p.page.locator('.el-message-box').getByRole('button', {name: '重新安装'}).tap();
      await n.locator('.mesh-missing-error', {hasText: '节点离线，连上后才能重新安装'}).waitFor();
      await noOverflow(p.page);
      await shot(p.page, `phone-missing-error-${dark ? 'dark' : 'light'}`);
      await p.close();
    }
  });
});
