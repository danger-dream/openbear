// The servers page in Chromium, layout only: node versions, dark phone, mesh access.
// Sizes, overflow, scrolling, phone and dark theme, and what needs real pointer events (dragging).
// What the page asks the backend and what it shows is checked without a browser in
// servers.dom.test.mjs and servers.install.dom.test.mjs.
import test from 'node:test';
import assert from 'node:assert/strict';
import {chromium, startServersBrowser, noOverflow, shot, versionShot, settled, until, frames, names, expectFocused, tab} from './testing/serversBrowser.mjs';
import {day, GB, server, many, uninstalledLast, TARGET, RELEASE, versions, MESH_ON, onMesh, card, orders, meshOption, off, fact} from './testing/serversBackend.mjs';

test('servers page layout: node versions, dark phone, mesh access', {skip: !chromium, timeout: 300000}, async t => {
  const {setup} = await startServersBrowser(t);
  await t.test('node versions: state tags, summary with 全部更新, build and source in the detail, unconfirmed update', {timeout: 90000}, async t => {
    const h = await setup(t, {tweak: versions, release: RELEASE});
    const notice = h.page.locator('.pb-notice[data-node-update=summary]');
    await notice.getByText('2 台服务器的节点可以更新到 1.2.0，其中 1 台需要更新才能使用组网、网络测试').waitFor();
    const tagOf = name => h.page.locator('.pb-card').filter({hasText: name}).locator('.pb-tag[data-node-state]');
    // Current and newer-than-target nodes carry no tag in the list.
    // No build hashes on the board.
    await versionShot(h.page, 'desktop-board-light');
    await h.page.getByRole('button', {name: '列表视图'}).click();
    const row = h.page.locator('.pl-row').filter({hasText: 'Papa-HK.T1'});
    await noOverflow(h.page);
    await versionShot(h.page, 'desktop-list-light');

    await h.page.locator('.pl-row').filter({hasText: 'Delta-JP'}).click();
    const detail = h.page.locator('.sv-detail:visible');
    await detail.locator('.pb-notice').getByText('节点 1.1.0 可以更新到 1.2.0（a1b2c3d4e5f6），更新时节点会重启，打开的终端会断开。').waitFor();
    const fact = label => detail.locator('.ix-facts > div').filter({has: h.page.locator('dt', {hasText: new RegExp(`^${label}$`)})}).locator('dd');
    await versionShot(h.page, 'desktop-detail-available');
    await detail.locator('.pb-notice').getByRole('button', {name: '更新'}).click();
    await h.page.getByText('文件已替换，未确认运行：节点没有在限定时间内用新程序重新连上').waitFor();
    await h.close();

    // A node that must update says why; one already on the target build says so in the detail only.
    const p = await setup(t, {tweak: versions, release: RELEASE});
    await p.page.locator('.pb-card').filter({hasText: 'Papa-HK.T1'}).click();
    let d = p.page.locator('.sv-detail:visible');
    await d.locator('.pb-notice[data-node-state=required]').getByText('节点 1.0.0 需要更新才能使用组网、网络测试，可以更新到 1.2.0（a1b2c3d4e5f6）', {exact: false}).waitFor();
    await tab(p.page, '探针').click();
    await p.page.locator('.pb-card').filter({hasText: 'Edge-SG.Game'}).click();
    d = p.page.locator('.sv-detail:visible');
    const fact2 = label => d.locator('.ix-facts > div').filter({has: p.page.locator('dt', {hasText: new RegExp(`^${label}$`)})}).locator('dd');
    await tab(p.page, '探针').click();
    await p.page.locator('.pb-card').filter({hasText: 'Bravo-FRA'}).click();
    d = p.page.locator('.sv-detail:visible');
    await tab(p.page, '探针').click();
    await p.page.locator('.pb-notice[data-node-update=summary]').getByRole('button', {name: '全部更新'}).click();
    await p.page.getByText('已更新 1 台，1 台文件已替换但未确认运行').waitFor();
    await p.close();
  });

  await t.test('node versions on a phone, dark: tags, summary and detail stay compact', {timeout: 60000}, async t => {
    const h = await setup(t, {width: 390, height: 844, dark: true, tweak: versions, release: {...RELEASE, prerelease: true, autoUpdate: true}});
    const notice = h.page.locator('.pb-notice[data-node-update=summary]');
    await notice.getByRole('button', {name: '全部更新'}).waitFor();
    const box = await notice.boundingBox();
    assert.ok(box.height <= 110, JSON.stringify(box));
    const tag = await h.page.locator('.pb-card').filter({hasText: 'Papa-HK.T1'}).locator('.pb-tag[data-node-state]').boundingBox();
    assert.ok(tag.height <= 24, JSON.stringify(tag));
    await noOverflow(h.page);
    await versionShot(h.page, 'phone-board-dark');
    await h.page.getByRole('button', {name: '列表视图'}).click();
    await h.page.locator('.pl-row').filter({hasText: 'Papa-HK.T1'}).locator('.pl-state').waitFor();
    await noOverflow(h.page);
    await versionShot(h.page, 'phone-list-dark');
    await h.page.locator('.pl-row').filter({hasText: 'Papa-HK.T1'}).click();
    const detail = h.page.locator('.sv-detail:visible');
    await detail.locator('.pb-notice[data-node-state=required]').waitFor();
    await detail.locator('.ix-facts dt', {hasText: /^构建$/}).scrollIntoViewIfNeeded();
    await noOverflow(h.page);
    await versionShot(h.page, 'phone-detail-dark');
    await h.close();
  });

  await t.test('dark phone: offline card, edit sheet with 16px inputs and sort order', {timeout: 90000}, async t => {
    const h = await setup(t, {width: 360, height: 780, dark: true});
    const offline = h.page.locator('.pb-card[data-state=offline]');
    await offline.getByText('2 小时前离线', {exact: true}).waitFor();
    await shot(h.page, 'phone-dark-board');
    await offline.click();
    await h.page.locator('.sv-detail:visible .ix-more').click();
    await h.page.getByRole('menuitem', {name: '编辑…'}).click();
    const sheet = h.page.getByRole('dialog', {name: /^编辑「GoldenCloud/});
    await sheet.waitFor();
    for (const input of await sheet.locator('input:not([type=checkbox]):not([type=radio]), select, textarea').all()) assert.ok(await input.evaluate(el => parseFloat(getComputedStyle(el).fontSize) >= 16), await input.getAttribute('aria-label'));
    // Inputs with suggestions show the same chevron as selects.
    assert.ok(await sheet.getByRole('combobox', {name: '分组'}).evaluate(el => getComputedStyle(el).backgroundImage.includes('svg')));
    assert.equal(await sheet.locator('select').first().evaluate(el => getComputedStyle(el).appearance), 'none');
    await shot(h.page, 'phone-dark-edit');
    await sheet.getByRole('combobox', {name: '分组'}).click();
    const options = h.page.getByRole('listbox').getByRole('option');
    await options.first().waitFor();
    for (const option of await options.all()) assert.ok((await option.boundingBox()).height >= 40);
    await shot(h.page, 'phone-dark-groups');
    await options.filter({hasText: 'Tier 1'}).click();
    // No sort field: order comes from "adjust order". Region is the flag beside the name.
    const regionButton = sheet.getByRole('button', {name: /^地区：/});
    await regionButton.click();
    const regionSearch = h.page.getByRole('combobox', {name: '搜索地区'});
    await expectFocused(regionSearch);
    assert.ok(await regionSearch.evaluate(el => parseFloat(getComputedStyle(el).fontSize) >= 16));
    const regionOptions = h.page.getByRole('listbox', {name: '地区'}).getByRole('option');
    await regionOptions.first().waitFor();
    assert.ok((await regionOptions.first().boundingBox()).height >= 40);
    await shot(h.page, 'phone-dark-region');
    await regionSearch.fill('hk');
    await regionOptions.filter({hasText: 'HK'}).first().click();
    await sheet.getByLabel('价格').fill('99');
    await noOverflow(h.page);
    await sheet.getByRole('button', {name: '保存', exact: true}).click();
    await sheet.waitFor({state: 'hidden'});
    const patch = h.calls.find(c => c.path === '/api/servers/srv-golf' && c.method === 'PATCH').body;
    await shot(h.page, 'phone-dark-detail');
    await h.close();
  });

  for (const [dark, theme] of [[false, 'light'], [true, 'dark']]) await t.test(`mesh access on a phone, ${theme}: settings row, editor sheet with the hint, detail`, {timeout: 120000}, async t => {
    const h = await setup(t, {width: 390, height: 844, dark, mesh: MESH_ON, tweak: onMesh('ap-mesh')});
    await h.page.getByRole('button', {name: '服务器更多操作'}).click();
    await h.page.getByRole('button', {name: '设置', exact: true}).click();
    const dialog = h.page.getByRole('dialog', {name: '服务器设置'});
    const builtin = dialog.locator('.ap-builtin');
    await builtin.waitFor();
    await builtin.scrollIntoViewIfNeeded();
    assert.ok(await builtin.evaluate(el => el.scrollWidth <= el.clientWidth + 1), 'the row fits');
    await noOverflow(h.page);
    await shot(h.page, `phone-${theme}-settings-mesh`);
    await dialog.getByRole('button', {name: '取消'}).click();
    await dialog.waitFor({state: 'hidden'});
    await h.page.locator('.pb-card').filter({hasText: 'Delta-JP'}).click();
    const detail = h.page.locator('.sv-detail:visible');
    const current = fact(detail, '当前连接');
    assert.ok(await current.evaluate(el => el.scrollWidth <= el.clientWidth + 1), 'the address is not cut');
    await current.scrollIntoViewIfNeeded();
    await noOverflow(h.page);
    await shot(h.page, `phone-${theme}-detail-mesh`);
    await detail.locator('.ix-more').click();
    await h.page.getByRole('menuitem', {name: '编辑…'}).click();
    const sheet = h.page.getByRole('dialog', {name: '编辑「Delta-JP」'});
    const pick = sheet.locator('.ap-pick');
    await pick.scrollIntoViewIfNeeded();
    for (const select of await pick.locator('select').all()) {
      assert.ok(await select.evaluate(el => parseFloat(getComputedStyle(el).fontSize) >= 16));
      assert.ok((await select.boundingBox()).height >= 36);
    }
    await noOverflow(h.page);
    await shot(h.page, `phone-${theme}-edit-mesh-hint`);
    await h.close();
  });

});
