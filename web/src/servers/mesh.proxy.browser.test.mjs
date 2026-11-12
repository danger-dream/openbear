// The exit proxy in Chromium: the 出口和代理 page (exits with links to copy, a group, a proxy port), a member's
// 代理 tab on the board, and both on a phone, light and dark. Page, fake controller and helpers: testing/meshBrowser.mjs.
import test from 'node:test';
import assert from 'node:assert/strict';
import {chromium, inspector, memberOf, node, noOverflow, shot, startMeshBrowser} from './testing/meshBrowser.mjs';

const tab = (page, name) => page.locator('.m3-tabs').getByRole('tab', {name: new RegExp(`^${name}`)});

test('mesh exit proxy: exits, groups, proxy ports, member settings, phone', {skip: !chromium, timeout: 300000}, async t => {
  const {setup} = await startMeshBrowser(t);

  await t.test('desktop: exits added at once, a link to copy, a group, a port to it', {timeout: 120000}, async t => {
    const h = await setup(t, {v3: true});
    const {page} = h;
    await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);
    await tab(page, '出口和代理').click();
    await page.getByRole('button', {name: '＋ 添加出口'}).click();
    const dialog = page.locator('.mesh3-dialog');
    await dialog.getByRole('checkbox', {name: 'LAX'}).check();
    await dialog.getByRole('checkbox', {name: 'Berlin'}).check();
    await dialog.getByRole('button', {name: '添加 2 个出口'}).click();
    await h.saved(d => memberOf(d, 'lax').proxy?.exit && memberOf(d, 'berlin').proxy?.exit, 'two exits');
    // Copied: the exit's own proxy at its mesh address, with its username and password.
    const lax = page.locator('.m3-rows-exits li', {hasText: 'LAX'});
    await lax.getByRole('button', {name: '复制 LAX 的出口链接'}).click();
    const copied = await page.evaluate(() => navigator.clipboard.readText());
    assert.match(copied, /^socks5:\/\/lax:lax-pw@100\.96\.0\.\d+:1081$/);
    // One line per exit.
    for (const row of await page.locator('.m3-rows-exits .m3-row:not(.m3-row-head)').all()) assert.ok((await row.boundingBox()).height <= 48, 'an exit on one line');
    await page.getByRole('button', {name: '＋ 新建出口组'}).click();
    await dialog.getByRole('textbox', {name: '出口组名称'}).fill('家宽');
    await dialog.getByRole('textbox', {name: '出口组名称'}).press('Tab');
    await dialog.locator('.m3-picks').getByRole('checkbox', {name: 'LAX'}).click();
    await h.saved(d => d.exitGroups?.[0]?.name === '家宽' && d.exitGroups[0].members.join() === 'lax', 'a group');
    await dialog.getByRole('button', {name: '完成'}).click();
    await page.getByRole('button', {name: '＋ 新建代理端口'}).click();
    await dialog.locator('.m3-node-select').first().click();
    await page.locator('.m3-node-pop:visible .el-select-dropdown__item', {hasText: 'Home'}).click();
    await dialog.getByRole('radio', {name: '一个出口组'}).click();
    await dialog.getByRole('radio', {name: '本机和局域网'}).click();
    await dialog.getByRole('textbox', {name: '局域网地址'}).fill('192.168.1.10');
    await dialog.getByRole('textbox', {name: '局域网地址'}).press('Tab');
    await shot(page, 'desktop-proxy-port-new');
    await dialog.getByRole('button', {name: '添加'}).click();
    await h.saved(d => memberOf(d, 'home').proxy?.ports?.[0]?.listen === 'lan', 'a LAN port');
    await page.locator('.m3-rows-ports li', {hasText: 'Home'}).getByText('家宽 · 按顺序').waitFor();
    await shot(page, 'desktop-proxy-page');
    await noOverflow(page);
    await h.close();
  });

  await t.test('desktop board: a member\'s 代理 tab turns its exit on and opens a port', {timeout: 120000}, async t => {
    const h = await setup(t);
    const {page} = h;
    await node(page, 'lax').click();
    const panel = inspector(page);
    await panel.getByRole('tab', {name: '代理'}).click();
    await panel.getByRole('switch', {name: '当出口'}).click();
    await h.saved(d => memberOf(d, 'lax').proxy?.exit?.user === 'lax', 'LAX as an exit');
    await panel.getByText(/^socks5:\/\/lax:••••@100\.96\.0\.\d+:1081$/).waitFor();
    await panel.getByRole('radio', {name: 'http://'}).click();
    await panel.getByText(/^http:\/\/lax:••••@/).waitFor();
    await panel.getByRole('switch', {name: '也允许共享局域网里的设备'}).click();
    await h.saved(d => memberOf(d, 'lax').proxy.exit.subnets, 'LAN devices too');
    await shot(page, 'desktop-proxy-member');
    await noOverflow(page);
    await h.close();
  });

  await t.test('phone, light and dark: the exits page and a node\'s 代理 tab with 16px inputs', {timeout: 150000}, async t => {
    for (const dark of [false, true]) {
      const h = await setup(t, {width: 390, height: 844, dark, v3: true, init: () => {}});
      const {page} = h;
      await tab(page, '出口和代理').tap();
      await page.getByRole('button', {name: '＋ 添加出口'}).tap();
      const sheet = page.locator('.m3-dialog-body:visible');
      await sheet.getByRole('checkbox', {name: 'LAX'}).tap();
      await sheet.getByRole('button', {name: '添加 1 个出口'}).tap();
      await h.saved(d => memberOf(d, 'lax').proxy?.exit, 'phone: LAX as an exit');
      await page.locator('.m3-rows-exits li', {hasText: 'LAX'}).waitFor();
      const copy = page.getByRole('button', {name: '复制 LAX 的出口链接'});
      const b = await copy.boundingBox();
      assert.ok(b.height >= 24, `the copy button ${b.height}px high`);
      await noOverflow(page);
      await shot(page, `phone-proxy-page${dark ? '-dark' : ''}`);
      await h.close();

      const p = await setup(t, {width: 390, height: 844, dark});
      await node(p.page, 'home').tap();
      const member = p.page.locator('.mesh-inspect-sheet');
      await member.getByRole('tab', {name: '代理'}).tap();
      await member.getByRole('switch', {name: '当出口'}).tap();
      await p.saved(d => memberOf(d, 'home').proxy?.exit, 'phone: Home as an exit');
      await member.getByRole('textbox', {name: '出口用户名'}).waitFor();
      for (const input of await member.locator('#m3-sec-proxy input:not([type=checkbox]):not([type=radio])').all()) {
        assert.ok(await input.evaluate(el => parseFloat(getComputedStyle(el).fontSize) >= 16), 'inputs at 16px');
      }
      await noOverflow(p.page);
      await shot(p.page, `phone-proxy-member${dark ? '-dark' : ''}`);
      await p.close();
    }
  });
});
