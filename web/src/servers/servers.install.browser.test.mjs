// The servers page in Chromium, layout only: node browser, adding servers on a phone, install clashes.
// Sizes, overflow, scrolling, phone and dark theme, and what needs real pointer events (dragging).
// What the page asks the backend and what it shows is checked without a browser in
// servers.dom.test.mjs and servers.install.dom.test.mjs.
import test from 'node:test';
import assert from 'node:assert/strict';
import {chromium, startServersBrowser, noOverflow, shot, versionShot, settled, until, frames, names, expectFocused, tab} from './testing/serversBrowser.mjs';
import {day, GB, server, many, uninstalledLast, TARGET, RELEASE, versions, MESH_ON, onMesh, card, orders, meshOption, off, fact} from './testing/serversBackend.mjs';

test('servers page layout: node browser, adding servers on a phone, install clashes', {skip: !chromium, timeout: 300000}, async t => {
  const {setup} = await startServersBrowser(t);
  await t.test('node browser: status card, turning it on with an existing address, restart, install option', {timeout: 120000}, async t => {
    const browserCard = detail => detail.locator('.ix-card').filter({has: detail.page().getByRole('heading', {name: '浏览器', exact: true})});
    let h = await setup(t, {tweak: items => {
      const delta = items.find(i => i.id === 'srv-delta');
      delta.node.caps.browser = delta.live.caps.browser = true;
      delta.browserState = {enabled: true, online: true, state: 'ready', source: 'download', product: 'Chrome/156.0.8078.47', headless: true, endpoint: '', epoch: 'e1'};
    }});
    await h.page.locator('.pb-card').filter({hasText: 'Delta-JP'}).click();
    let card = browserCard(h.page.locator('.sv-detail:visible'));
    await card.getByText('运行中').waitFor();
    await card.getByRole('button', {name: '重启'}).click();
    await Promise.all([h.page.waitForResponse(r => r.url().endsWith('/browser/restart')),
      h.page.locator('.el-message-box').getByRole('button', {name: '重启'}).click()]);
    await card.getByText('运行中').waitFor();
    await h.close();

    for (const width of [1440, 390]) {
      h = await setup(t, {width, height: width < 500 ? 844 : 900});
      await h.page.locator('.pb-card').filter({hasText: 'Papa-HK.T1'}).click();
      const detail = h.page.locator('.sv-detail:visible');
      card = browserCard(detail);
      await card.getByText('未开启', {exact: false}).waitFor();
      await card.getByRole('button', {name: '开启'}).click();
      const dialog = h.page.getByRole('dialog', {name: '开启浏览器'});
      await dialog.getByText('优先用这台机器上的 Chrome', {exact: false}).waitFor();
      await dialog.getByRole('radio', {name: '连接已有的浏览器'}).check();
      await dialog.getByLabel('浏览器调试地址').fill('http://127.0.0.1:9222');
      const box = await dialog.evaluate(el => el.getBoundingClientRect().toJSON());
      assert.ok(box.left >= 0 && box.right <= width + 1, JSON.stringify(box));
      if (width < 500) assert.ok(await dialog.getByLabel('浏览器调试地址').evaluate(el => parseFloat(getComputedStyle(el).fontSize) >= 16));
      await shot(h.page, `browser-enable-${width}`);
      await dialog.getByRole('button', {name: '开启', exact: true}).click();
      await dialog.waitFor({state: 'hidden'});
      await card.getByText('未启动，第一次使用时启动').waitFor();
      await noOverflow(h.page);
      await h.close();
    }

    // An installed browser the node cannot run is explained; switching to Chrome for Testing is a choice.
    h = await setup(t, {tweak: items => {
      const delta = items.find(i => i.id === 'srv-delta');
      delta.node.caps.browser = delta.live.caps.browser = true;
      delta.browserState = {enabled: true, online: true, state: 'failed', source: '', product: '', headless: false, endpoint: '', epoch: '',
        error: 'browser_not_runnable: /opt/google/chrome/chrome'};
    }});
    await h.page.locator('.pb-card').filter({hasText: 'Delta-JP'}).click();
    card = browserCard(h.page.locator('.sv-detail:visible'));
    await card.getByText('启动失败').waitFor();
    await card.getByRole('button', {name: '设置'}).click();
    const settingsDialog = h.page.getByRole('dialog', {name: '浏览器设置'});
    await settingsDialog.getByRole('radio', {name: '使用下载的测试版 Chrome'}).check();
    await settingsDialog.getByText('适合已装的浏览器节点运行不了的情况', {exact: false}).waitFor();
    await settingsDialog.getByRole('button', {name: '保存'}).click();
    await settingsDialog.waitFor({state: 'hidden'});
    await card.getByRole('button', {name: '设置'}).click();
    await h.close();

    // The local server's Browser tool already uses the configured default browser.
    h = await setup(t);
    await h.page.locator('.pb-card').filter({hasText: 'Edge-SG.Game'}).click();
    card = browserCard(h.page.locator('.sv-detail:visible'));
    await card.getByText('本机不需要开启', {exact: false}).waitFor();
    await h.close();

    h = await setup(t);
    // A node that refuses commands is switched on where it runs.
    await h.page.locator('.pb-card').filter({hasText: 'Bravo-FRA'}).click();
    card = browserCard(h.page.locator('.sv-detail:visible'));
    await card.getByText('关闭了命令执行', {exact: false}).waitFor();
    await h.close();

    // Installing can turn the browser on at the same time.
    h = await setup(t);
    await h.page.locator('.pb-card').filter({hasText: 'Hotel-HK'}).click();
    const install = h.page.locator('.sv-detail:visible .sv-install');
    await install.getByRole('checkbox', {name: /同时开启浏览器/}).check();
    await Promise.all([h.page.waitForResponse(r => r.url().endsWith('/api/servers/srv-hotel/install')),
      install.getByRole('button', {name: '通过 SSH 安装'}).click()]);
    await h.close();
  });

  await t.test('installs on a machine that already runs a node: phone light and dark', {timeout: 180000}, async t => {
    // Phone, dark and light: the add sheet with a clash, nothing wider than the screen, buttons within reach.
    for (const dark of [true, false]) {
      const p = await setup(t, {width: 390, height: 844, dark});
      await p.page.getByRole('button', {name: '添加服务器'}).click();
      const d = p.page.getByRole('dialog', {name: '添加服务器'});
      await d.getByLabel('服务器地址').fill('203.0.113.77');
      await d.getByRole('button', {name: '保存并安装'}).click();
      const c = d.locator('.sv-conflict');
      await c.waitFor({timeout: 15000});
      assert.ok((await c.getByRole('button', {name: '仍然覆盖安装'}).boundingBox()).height >= 28);
      await noOverflow(p.page);
      await shot(p.page, `phone-install-other-${dark ? 'dark' : 'light'}`);
      await d.getByRole('button', {name: '继续添加'}).click();
      await d.getByRole('tab', {name: '批量 SSH'}).click();
      await d.getByLabel('服务器列表').fill('甲 203.0.113.66\n乙 203.0.113.77\n东京 203.0.113.10 27920');
      await d.getByRole('button', {name: '开始安装（3 台）'}).click();
      await d.getByText('1 台已上线 · 2 台失败 · 共 3 台').waitFor({timeout: 15000});
      await noOverflow(p.page);
      await shot(p.page, `phone-install-batch-${dark ? 'dark' : 'light'}`);
      await p.close();
    }
  });

  await t.test('phone: add server sheet, batch lines, access picker and guide', {timeout: 90000}, async t => {
    const h = await setup(t, {width: 390, height: 844});
    await h.page.getByRole('button', {name: '添加服务器'}).click();
    const dialog = h.page.getByRole('dialog', {name: '添加服务器'});
    await dialog.getByLabel('服务器地址').waitFor();
    for (const input of await dialog.locator('input:not([type=checkbox]):not([type=radio]):not([type=file]), select, textarea').all()) assert.ok(await input.evaluate(el => parseFloat(getComputedStyle(el).fontSize) >= 16));
    await noOverflow(h.page);
    await shot(h.page, 'phone-add-ssh');
    await dialog.getByRole('tab', {name: '批量 SSH'}).click();
    await dialog.getByLabel('服务器列表').fill('东京 203.0.113.10 27920\n198.51.100.7\n大阪 192.0.2.99');
    await dialog.getByRole('button', {name: '开始安装（3 台）'}).click();
    await dialog.getByText('2 台已上线 · 1 台失败 · 共 3 台').waitFor({timeout: 15000});
    await noOverflow(h.page);
    await shot(h.page, 'phone-add-batch');
    await dialog.getByRole('button', {name: '继续添加'}).click();
    await dialog.getByRole('tab', {name: '注册码'}).click();
    await dialog.getByText('已用 2 次', {exact: false}).waitFor();
    await noOverflow(h.page);
    await shot(h.page, 'phone-add-codes');
    await dialog.getByRole('button', {name: '接入说明'}).click();
    const guide = h.page.locator('.sv-guide-drawer');
    await guide.getByRole('heading', {name: '第二步：添加服务器'}).scrollIntoViewIfNeeded();
    assert.ok(await guide.evaluate(el => el.getBoundingClientRect().width <= innerWidth));
    await shot(h.page, 'phone-guide');
    await h.close();
  });

});
