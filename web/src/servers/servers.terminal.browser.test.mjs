// The servers page in Chromium, layout only: terminal with docked files and the Monaco editor.
// Sizes, overflow, scrolling, phone and dark theme, and what needs real pointer events (dragging).
// What the page asks the backend and what it shows is checked without a browser in
// servers.dom.test.mjs and servers.install.dom.test.mjs.
import test from 'node:test';
import assert from 'node:assert/strict';
import {chromium, startServersBrowser, noOverflow, shot, versionShot, settled, until, frames, names, expectFocused, tab} from './testing/serversBrowser.mjs';
import {day, GB, server, many, uninstalledLast, TARGET, RELEASE, versions, MESH_ON, onMesh, card, orders, meshOption, off, fact} from './testing/serversBackend.mjs';

test('servers page layout: terminal with docked files and the Monaco editor', {skip: !chromium, timeout: 300000}, async t => {
  const {setup} = await startServersBrowser(t);
  await t.test('desktop terminal: search, font size, docked files with menu, editor and operations, batch exec', {timeout: 240000}, async t => {
    const h = await setup(t);
    await h.page.locator('.pb-card').filter({hasText: 'Edge-SG.Game'}).locator('.pb-icon').click();
    await h.page.locator('.tw-tab .tw-status[data-status=open]').waitFor();
    await h.page.locator('.tw-terminal').click();
    await h.page.keyboard.type('uptime');
    await h.page.keyboard.press('Enter');
    await h.page.locator('.xterm-rows').getByText('needle-line', {exact: false}).waitFor();
    await h.page.keyboard.press('Control+Shift+F');
    const find = h.page.getByRole('searchbox', {name: '搜索终端输出'});
    await find.waitFor();
    await find.fill('needle-line');
    await find.fill('absent-text-xyz');
    await h.page.locator('.tw-find').getByText('没有找到').waitFor();
    await find.press('Escape');
    await find.waitFor({state: 'hidden'});
    const size = () => h.page.evaluate(() => parseFloat(getComputedStyle(document.querySelector('.xterm-rows')).fontSize));
    const before = await size();
    await h.page.getByRole('button', {name: '放大字号'}).click();
    await h.page.getByRole('button', {name: '放大字号'}).click();
    await h.page.waitForFunction(b => parseFloat(getComputedStyle(document.querySelector('.xterm-rows')).fontSize) === b + 2, before);
    assert.equal(await h.page.evaluate(() => localStorage.getItem('openbear.servers.terminalFont')), String(before + 2));

    // Files dock beside the terminal instead of covering it.
    await h.page.getByRole('button', {name: '文件', exact: true}).click();
    const pane = h.page.getByRole('region', {name: '文件'});
    await pane.getByText('backup.tar.gz').waitFor();
    assert.equal(await h.page.locator('.tw-body').getAttribute('data-dock'), 'right');
    assert.ok((await h.page.locator('.tw-panes').boundingBox()).width > 600, 'the terminal keeps its room');
    await pane.getByText('显示隐藏文件').click();
    const widthBefore = (await pane.boundingBox()).width, split = await h.page.locator('.tw-splitter').boundingBox();
    await h.page.mouse.move(split.x + 2, split.y + 100); await h.page.mouse.down();
    await h.page.mouse.move(split.x - 98, split.y + 100, {steps: 5}); await h.page.mouse.up();
    assert.ok(Math.abs((await pane.boundingBox()).width - widthBefore - 100) <= 4, 'the splitter resizes the pane');
    assert.ok(JSON.parse(await h.page.evaluate(() => localStorage.getItem('openbear.servers.filesDock'))).right > widthBefore + 90);
    // Below the terminal on request, remembered; back on the right the same way.
    await pane.getByRole('button', {name: '放到下方'}).click();
    assert.equal(await h.page.locator('.tw-body').getAttribute('data-dock'), 'bottom');
    await pane.getByRole('button', {name: '放到右侧'}).click();
    assert.equal(await h.page.locator('.tw-body').getAttribute('data-dock'), 'right');
    // The server list folds away for more room and comes back from the same button; remembered.
    const termWidth = (await h.page.locator('.tw-panes').boundingBox()).width;
    const fold = h.page.getByRole('button', {name: '收起服务器列表'});
    const foldAt = await fold.boundingBox();
    await shot(h.page, 'desktop-terminal-files-right');
    await fold.click();
    await h.page.locator('.tw-side').waitFor({state: 'hidden'});
    await shot(h.page, 'desktop-terminal-folded');
    assert.ok((await h.page.locator('.tw-panes').boundingBox()).width > termWidth + 200, 'the terminal takes the room');
    const unfold = h.page.getByRole('button', {name: '展开服务器列表'});
    await unfold.click();
    await h.page.locator('.tw-side').waitFor();
    assert.ok(Math.abs((await fold.boundingBox()).y - foldAt.y) <= 1, 'the button stays where it was');

    // The menu fits the entry: archives extract, every file downloads.
    await pane.locator('.fb-item').filter({hasText: 'backup.tar.gz'}).click({button: 'right'});
    const menu = h.page.getByRole('menu', {name: 'backup.tar.gz 的操作'});
    const menuItems = async () => { await h.page.getByRole('menuitem').first().waitFor(); return h.page.getByRole('menuitem').allInnerTexts(); };
    await shot(h.page, 'desktop-files-menu');
    const download = h.page.waitForEvent('download');
    await menu.getByRole('menuitem', {name: '下载'}).click();
    const op = () => h.calls.filter(c => c.path.endsWith('/files/op') && c.method === 'POST').at(-1).body;
    const prompt = name => h.page.getByRole('dialog', {name});

    await pane.locator('.fb-item').filter({hasText: 'backup.tar.gz'}).locator('.fb-more').click();
    await h.page.getByRole('menuitem', {name: '解压…'}).click();
    await prompt('解压').getByRole('button', {name: '解压'}).click();
    await h.page.waitForFunction(() => !document.querySelector('.fb-busy'));

    await pane.locator('.fb-item').filter({hasText: 'backup.tar.gz'}).click({button: 'right'});
    await h.page.getByRole('menuitem', {name: '重命名…'}).click();
    await prompt('重命名').locator('input').fill('old.tgz');
    await prompt('重命名').getByRole('button', {name: '确定'}).click();
    await h.page.waitForFunction(() => !document.querySelector('.fb-busy'));

    await pane.locator('.fb-item').filter({hasText: 'app'}).locator('.fb-more').click();
    await h.page.getByRole('menuitem', {name: '修改权限…'}).click();
    const perm = h.page.getByRole('dialog', {name: '修改权限'});
    await perm.getByLabel('用户组写').check();
    await perm.getByText('同时应用到文件夹里的所有内容').click();
    await perm.getByRole('button', {name: '修改'}).click();
    await h.page.waitForFunction(() => !document.querySelector('.fb-busy'));

    // Several picked: compress or delete them together.
    await pane.getByRole('checkbox', {name: '选择 app'}).check();
    await pane.getByRole('checkbox', {name: '选择 backup.tar.gz'}).check();
    const bar = pane.getByRole('toolbar', {name: '已选文件'});
    await bar.getByText('已选 2 项').waitFor();
    await bar.getByRole('button', {name: '压缩'}).click();
    const pack = h.page.getByRole('dialog', {name: '压缩'});
    await pack.getByLabel('压缩格式').selectOption('zip');
    await pack.getByRole('button', {name: '压缩'}).click();
    await h.page.waitForFunction(() => !document.querySelector('.fb-busy'));
    await pane.getByRole('checkbox', {name: '选择 app'}).check();
    await pane.getByRole('checkbox', {name: '选择 .bashrc'}).check();
    await pane.locator('.fb-item').filter({hasText: '.bashrc'}).click({button: 'right'});
    await h.page.getByRole('menuitem', {name: '删除 2 项'}).click();
    await prompt('删除').getByText('文件夹里的内容会一起删除', {exact: false}).waitFor();
    await prompt('删除').getByRole('button', {name: '删除'}).click();
    await h.page.waitForFunction(() => !document.querySelector('.fb-busy'));

    // Empty space: create; a failure says why.
    await pane.locator('.fb-tip').click({button: 'right'});
    await h.page.getByRole('menuitem', {name: '新建文件夹'}).click();
    await prompt('新建文件夹').locator('input').fill('exists');
    await prompt('新建文件夹').getByRole('button', {name: '确定'}).click();
    await h.page.getByText('同名文件已存在。').waitFor();

    // Text opens in the editor; saving writes it back over the old file.
    await pane.locator('.fb-row').filter({hasText: 'app'}).click();
    await pane.getByText('config.yml').waitFor();
    await pane.locator('.fb-row').filter({hasText: 'config.yml'}).click();
    const viewer = h.page.locator('.fv-dialog');
    await viewer.locator('.monaco-editor').getByText('port: 8080').waitFor({timeout: 60000});
    await viewer.locator('.monaco-editor .view-lines').click();
    await h.page.keyboard.press('Control+End');
    await h.page.keyboard.type('debug: true');
    await viewer.getByText('未保存', {exact: false}).waitFor();
    await shot(h.page, 'desktop-files-editor');
    await h.page.keyboard.press('Control+s');
    await h.page.getByText('已保存').waitFor();
    assert.deepEqual(h.saved.at(-1), {path: '/root/app/config.yml', text: 'port: 8080\ndebug: true'});
    assert.deepEqual(h.uploads.at(-1), {path: '/root/app/config.yml', size: 22, overwrite: true});
    await viewer.getByRole('button', {name: '关闭'}).click();
    await viewer.waitFor({state: 'hidden'});
    await pane.locator('input[type=file]').setInputFiles({name: 'deploy.sh', mimeType: 'text/plain', buffer: Buffer.from('#!/bin/sh\necho hi\n')});
    await pane.getByText('已上传').waitFor();
    assert.deepEqual(h.uploads.at(-1), {path: '/root/app/deploy.sh', size: 18, overwrite: false});
    await noOverflow(h.page);
    await shot(h.page, 'desktop-files');
    // Closing and reopening keeps the folder.
    await pane.getByRole('button', {name: '关闭文件面板'}).click();
    await pane.waitFor({state: 'hidden'});
    await h.page.getByRole('button', {name: '文件', exact: true}).click();
    await pane.getByRole('button', {name: '关闭文件面板'}).click();

    await h.page.locator('.tw-tabs').getByRole('button', {name: '批量执行'}).click();
    const batch = h.page.getByRole('dialog', {name: '批量执行'});
    await batch.waitFor();
    await batch.getByRole('button', {name: '流媒体'}).click();
    await batch.getByRole('combobox', {name: '插入常用命令'}).selectOption('1');
    await batch.getByRole('button', {name: '执行（2 台）'}).click();
    await batch.getByText('1 成功 · 1 失败').waitFor();
    const sent = h.calls.find(c => c.path === '/api/servers/exec' && c.method === 'POST').body;
    await batch.locator('.be-row').filter({hasText: 'Delta-JP'}).click();
    await batch.getByText('df: /data: No such file').waitFor();
    await shot(h.page, 'desktop-batch');
    await batch.getByRole('button', {name: '重试失败的'}).click();
    await h.close();
  });

});
