// The servers page in Chromium, layout only: Windows and macOS nodes, phone detail, file sheet and terminal keys.
// Sizes, overflow, scrolling, phone and dark theme, and what needs real pointer events (dragging).
// What the page asks the backend and what it shows is checked without a browser in
// servers.dom.test.mjs and servers.install.dom.test.mjs.
import test from 'node:test';
import assert from 'node:assert/strict';
import {chromium, startServersBrowser, noOverflow, shot, versionShot, settled, until, frames, names, expectFocused, tab} from './testing/serversBrowser.mjs';
import {day, GB, server, many, uninstalledLast, TARGET, RELEASE, versions, MESH_ON, onMesh, card, orders, meshOption, off, fact} from './testing/serversBackend.mjs';

test('servers page layout: Windows and macOS nodes, phone detail, file sheet and terminal keys', {skip: !chromium, timeout: 300000}, async t => {
  const {setup} = await startServersBrowser(t);
  await t.test('windows and macOS nodes: metrics per platform, drives and backslash paths, install commands, mixed batch', {timeout: 120000}, async t => {
    const h = await setup(t, {tweak: items => {
      const w = items.find(i => i.id === 'srv-delta');
      Object.assign(w.node.info, {platform: 'windows', os: 'Windows 10 Enterprise LTSC 2019 (1809)', kernel: '10.0.17763.9376', cpu: 'Common KVM processor'});
      w.live.m.load = [0, 0, 0];
      Object.assign(items.find(i => i.id === 'srv-papa').node.info, {platform: 'darwin', os: 'macOS 26.2', kernel: '25.2.0', arch: 'arm64', cpu: 'Apple M4', virtualization: ''});
    }});
    // Windows has no load average: its card shows the process count.
    const win = h.page.locator('.pb-card').filter({hasText: 'Delta-JP'});
    // Windows and macOS carry a mark right after the name; Linux, the usual case, has none.
    const mark = win.locator('.pb-head .sv-platform');
    const nameBox = await win.locator('.pb-name').boundingBox(), markBox = await mark.boundingBox();
    assert.ok(markBox.x - (nameBox.x + nameBox.width) <= 8 && markBox.width >= 12, JSON.stringify([nameBox, markBox]));
    await shot(h.page, 'desktop-platform-cards');
    await h.page.getByRole('button', {name: '列表视图'}).click();
    await shot(h.page, 'desktop-platform-list');
    await h.page.getByRole('button', {name: '卡片视图'}).click();

    await win.click();
    let detail = h.page.locator('.sv-detail:visible');
    await detail.locator('.sv-chart svg').first().waitFor();

    // Files on Windows: the drives, then backslash paths; no permissions.
    await detail.getByRole('button', {name: '文件', exact: true}).click();
    const sheet = h.page.locator('.sv-files-drawer');
    await sheet.getByText('E:', {exact: true}).waitFor();
    const box = sheet.getByRole('textbox', {name: '路径'});
    await sheet.locator('.fb-row').filter({hasText: 'C:'}).click();
    await sheet.getByText('backup.zip').waitFor();
    await sheet.locator('.fb-row').filter({hasText: 'Users'}).click();
    await sheet.getByText('admin', {exact: true}).waitFor();
    await sheet.getByRole('button', {name: '上一级'}).click();
    await sheet.getByText('backup.zip').waitFor();
    await sheet.locator('.fb-item').filter({hasText: 'backup.zip'}).click({button: 'right'});
    await h.page.getByRole('menuitem').first().waitFor();
    const items = await h.page.getByRole('menuitem').allInnerTexts();
    await h.page.getByRole('menuitem', {name: '复制路径'}).click();
    await sheet.locator('.fb-item').filter({hasText: 'Users'}).click({button: 'right'});
    await h.page.getByRole('menuitem', {name: '压缩…'}).click();
    const pack = h.page.getByRole('dialog', {name: '压缩'});
    await pack.getByRole('button', {name: '压缩'}).click();
    await h.page.waitForFunction(() => !document.querySelector('.fb-busy'));
    await sheet.locator('.fb-item').filter({hasText: 'backup.zip'}).click({button: 'right'});
    await h.page.getByRole('menuitem', {name: '解压…'}).click();
    const unpack = h.page.getByRole('dialog', {name: '解压'});
    await unpack.locator('input').fill('D:\\restore');
    await unpack.getByRole('button', {name: '解压'}).click();
    await h.page.waitForFunction(() => !document.querySelector('.fb-busy'));
    await shot(h.page, 'desktop-windows-files');
    await h.page.keyboard.press('Escape');
    await sheet.waitFor({state: 'hidden'});

    // macOS: no disk I/O and no steal; a privacy block explains itself.
    await tab(h.page, '探针').click();
    await h.page.locator('.pb-card').filter({hasText: 'Papa-HK.T1'}).click();
    detail = h.page.locator('.sv-detail:visible');
    await detail.getByText('macOS 26.2').waitFor();
    await detail.locator('.sv-chart svg').first().waitFor();
    await detail.getByRole('button', {name: '文件', exact: true}).click();
    await sheet.getByText('完全磁盘访问权限', {exact: false}).waitFor();
    await h.page.keyboard.press('Escape');
    await sheet.waitFor({state: 'hidden'});

    // Mixed batch: a warning, and Windows chips say PowerShell.
    await detail.getByRole('button', {name: '执行命令'}).click();
    const batch = h.page.getByRole('dialog', {name: '批量执行'});
    await batch.locator('.be-chip').filter({hasText: 'Delta-JP'}).click();
    await batch.getByText('既有 Windows 也有 Linux / macOS', {exact: false}).waitFor();
    await batch.locator('.be-chip').filter({hasText: 'Papa-HK.T1'}).click();
    await shot(h.page, 'desktop-windows-batch');
    await batch.getByRole('button', {name: '关闭'}).click();
    await batch.waitFor({state: 'hidden'});
    await tab(h.page, '终端').click();
    const sidebar = h.page.locator('.tw-server').filter({hasText: 'Delta-JP'}).first();
    await shot(h.page, 'desktop-platform-terminal');
    await h.close();
  });

  await t.test('phone: Windows detail, drives and paths, install command switch, PowerShell tag', {timeout: 120000}, async t => {
    const h = await setup(t, {width: 390, height: 844, tweak: items => {
      const w = items.find(i => i.id === 'srv-delta');
      Object.assign(w.node.info, {platform: 'windows', os: 'Windows 10 Enterprise LTSC 2019 (1809)', kernel: '10.0.17763.9376'});
    }});
    await h.page.locator('.pb-card').filter({hasText: 'Delta-JP'}).click();
    let detail = h.page.locator('.sv-detail:visible');
    await detail.locator('.sv-gauge').first().waitFor();
    await noOverflow(h.page);
    await detail.getByRole('button', {name: '文件'}).click();
    const sheet = h.page.locator('.sv-files-drawer');
    await sheet.getByText('E:', {exact: true}).waitFor();
    await sheet.locator('.fb-row').filter({hasText: 'C:'}).click();
    await sheet.getByText('backup.zip').waitFor();
    for (const b of await sheet.locator('.fb-more').all()) assert.ok((await b.boundingBox()).height >= 38);
    const row = await sheet.locator('.fb-row').filter({hasText: 'backup.zip'}).boundingBox();
    assert.ok(row.width > 200 && row.height <= 48, JSON.stringify(row));
    await noOverflow(h.page);
    await shot(h.page, 'phone-windows-files');
    await sheet.getByRole('button', {name: 'backup.zip 的操作'}).click();
    await h.page.getByRole('menuitem').first().waitFor();
    await h.page.keyboard.press('Escape');
    await h.page.getByRole('menuitem').first().waitFor({state: 'hidden'});
    await h.page.keyboard.press('Escape');
    await sheet.waitFor({state: 'hidden'});

    await detail.locator('.ix-more').click();
    await h.page.getByRole('menuitem', {name: '执行命令…'}).click();
    const batch = h.page.getByRole('dialog', {name: '批量执行'});
    await batch.locator('.be-chip').filter({hasText: 'Delta-JP'}).locator('.be-os').waitFor();
    await batch.locator('.be-chip').filter({hasText: 'Edge-SG'}).click();
    await batch.getByText('既有 Windows', {exact: false}).waitFor();
    await noOverflow(h.page);
    await shot(h.page, 'phone-windows-batch');
    await batch.getByRole('button', {name: '关闭'}).click();
    await batch.waitFor({state: 'hidden'});

    await tab(h.page, '探针').click();
    await h.page.locator('.pb-card').filter({hasText: 'Hotel-HK'}).click();
    detail = h.page.locator('.sv-detail:visible');
    await detail.getByRole('button', {name: '生成安装命令'}).click();
    const block = detail.locator('.sv-install-command');
    await block.getByRole('button', {name: 'Windows'}).click();
    await block.locator('.sv-command > code').filter({hasText: 'install.ps1'}).waitFor();
    for (const b of await block.locator('.sv-os > button').all()) assert.ok((await b.boundingBox()).height >= 28);
    await noOverflow(h.page);
    await shot(h.page, 'phone-install-windows');
    await h.close();
  });

  await t.test('phone: compact cards and list, detail actions, file sheet, terminal keys', {timeout: 120000}, async t => {
    const h = await setup(t, {width: 390, height: 844});
    await noOverflow(h.page);
    const heights = await h.page.locator('.pb-card').evaluateAll(cards => cards.map(c => Math.round(c.getBoundingClientRect().height)));
    assert.ok(heights.every(height => height <= 240), JSON.stringify(heights));
    await shot(h.page, 'phone-board');
    await h.page.getByRole('button', {name: '列表视图'}).click();
    const rows = await h.page.locator('.pl-row').evaluateAll(list => list.map(r => Math.round(r.getBoundingClientRect().height)));
    assert.ok(rows.every(height => height <= 80), JSON.stringify(rows));
    await noOverflow(h.page);
    await shot(h.page, 'phone-list');
    await h.page.locator('.pl-row').filter({hasText: 'Edge-SG.Game'}).click();
    const detail = h.page.locator('.sv-detail:visible');
    await detail.locator('.sv-gauge').first().waitFor();
    await noOverflow(h.page);
    await shot(h.page, 'phone-detail');
    await detail.locator('.ix-more').click();
    await h.page.getByRole('menuitem', {name: '执行命令…'}).click();
    const batch = h.page.getByRole('dialog', {name: '批量执行'});
    await batch.waitFor();
    for (const input of await batch.locator('textarea, select').all()) assert.ok(await input.evaluate(el => parseFloat(getComputedStyle(el).fontSize) >= 16));
    await noOverflow(h.page);
    await shot(h.page, 'phone-batch');
    await batch.getByRole('button', {name: '关闭'}).click();
    await batch.waitFor({state: 'hidden'});
    await detail.getByRole('button', {name: '文件'}).click();
    const sheet = h.page.locator('.sv-files-drawer');
    await sheet.getByText('backup.tar.gz').waitFor();
    assert.ok(await sheet.evaluate(el => el.getBoundingClientRect().bottom >= innerHeight - 1 && el.getBoundingClientRect().top > 40));
    assert.ok(await sheet.getByRole('textbox', {name: '路径'}).evaluate(el => parseFloat(getComputedStyle(el).fontSize) >= 16));
    await sheet.getByText('点 ⋯ 查看更多操作', {exact: false}).waitFor();
    await sheet.getByRole('button', {name: 'backup.tar.gz 的操作'}).click();
    const items = h.page.getByRole('menuitem');
    await items.first().waitFor();
    for (const item of await items.all()) assert.ok((await item.boundingBox()).height >= 40);
    await shot(h.page, 'phone-files-menu');
    await expectFocused(items.first());
    await h.page.keyboard.press('ArrowDown');
    await expectFocused(items.nth(1));
    await h.page.keyboard.press('Escape');
    await items.first().waitFor({state: 'hidden'});
    await shot(h.page, 'phone-files');
    await h.page.keyboard.press('Escape');
    await sheet.waitFor({state: 'hidden'});
    await tab(h.page, '终端').click();
    await h.page.locator('.tw-server').filter({hasText: 'Edge-SG.Game'}).click();
    await h.page.locator('.tw-status[data-status=open]').waitFor();
    await h.page.getByRole('button', {name: '更多终端操作'}).click();
    await h.page.getByRole('menuitem', {name: '搜索输出'}).click();
    await h.page.getByRole('searchbox', {name: '搜索终端输出'}).waitFor();
    await h.page.locator('.tw-keys').getByRole('button', {name: 'Ctrl'}).click();
    await h.page.locator('.xterm-helper-textarea').focus();
    await h.page.keyboard.type('c');
    await until(() => h.terminal.input.length > 0);
    await noOverflow(h.page);
    await shot(h.page, 'phone-terminal');
    await h.close();
  });

});
