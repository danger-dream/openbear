// 前置机 (fronts) on the servers page without a browser: adding one through its exits, and making a server one in its editor.
// The layout of the same forms stays in servers.front.browser.test.mjs.
import test from 'node:test';
import assert from 'node:assert/strict';
import {setup} from './testing/serversDom.mjs';

test('add server: a front installs through its exit, its backup optional', async t => {
  for (const [width, touch] of [[1440, false], [390, true]]) {
    const h = await setup(t, {width, height: 844, touch});
    await h.page.getByRole('button', {name: '添加服务器'}).click();
    const dialog = h.page.getByRole('dialog', {name: '添加服务器'});
    await dialog.getByRole('tab', {name: '前置机'}).click();
    // Exits are other servers with a node: not the controller's machine, not one without a node.
    const exit = dialog.getByLabel('出口', {exact: true});
    const names = await exit.locator('option').allInnerTexts();
    assert.ok(names.includes('Papa-HK.T1') && names.includes('Delta-JP'), names);
    assert.ok(!names.some(n => n.includes('Edge-SG') || n.includes('Hotel-HK')), names);
    // A front uses no access point.
    assert.equal(await dialog.getByLabel('首选接入点').count(), 0);
    assert.ok(await dialog.getByRole('button', {name: '保存并安装'}).isDisabled());
    // The address is one the exit reports, picked after the exit: its line and its public one behind NAT, not its mesh address.
    const address = dialog.getByLabel('出口地址', {exact: true});
    assert.ok(await address.isDisabled());
    await exit.selectOption('srv-papa');
    await address.locator('option[value="10.0.10.246"]').waitFor({state: 'attached'});
    assert.deepEqual(await address.locator('option:not([disabled])').allInnerTexts(), ['10.0.10.246（eth0 · 私有）', '203.0.113.38（公网出口）']);
    await address.selectOption('10.0.10.246');
    await dialog.getByRole('button', {name: '加一个备用出口'}).click();
    // With two exits either can go, so both rows keep the same columns.
    assert.equal(await dialog.locator('.sv-front-exit').filter({has: dialog.getByRole('button', {name: /^去掉/})}).count(), 2);
    // The primary is not offered as the backup too.
    const backup = dialog.getByLabel('备用出口', {exact: true});
    assert.equal(await backup.locator('option[value="srv-papa"]').evaluate(o => o.disabled), true);
    await backup.selectOption('srv-delta');
    const backupAddress = dialog.getByLabel('备用出口地址');
    await backupAddress.locator('option[value="10.77.17.46"]').waitFor({state: 'attached'});
    assert.ok(!(await backupAddress.locator('option').allInnerTexts()).some(t => t.startsWith('fe80')));
    await backupAddress.selectOption('10.77.17.46');
    await dialog.getByLabel('前置机地址').fill('172.16.10.203');
    await dialog.getByLabel('SSH 端口').fill('2222');
    await dialog.getByLabel('服务器名称').fill('广州');
    await dialog.getByRole('button', {name: '保存并安装'}).click();
    await dialog.locator('.ip-steps > li[data-state=running]').first().waitFor();
    const create = h.calls.find(c => c.path === '/api/servers' && c.method === 'POST').body;
    assert.deepEqual(create.profile, {front: {exits: [{serverId: 'srv-papa', address: '10.0.10.246'}, {serverId: 'srv-delta', address: '10.77.17.46'}]}});
    assert.deepEqual({name: create.name, host: create.ssh.host, port: create.ssh.port}, {name: '广州', host: '172.16.10.203', port: 2222});
    // The browser is not turned on for a front.
    assert.deepEqual(h.calls.find(c => c.path.endsWith('/install') && c.method === 'POST').body, {});
    await h.close();
  }
});

test('editor: a server made a front uses its exits instead of access points', async t => {
  const h = await setup(t);
  await h.page.locator('.pb-card').filter({hasText: 'Delta-JP'}).click();
  await h.page.locator('.sv-detail:visible').getByRole('button', {name: '编辑'}).first().click();
  const editor = h.page.getByRole('dialog', {name: '编辑「Delta-JP」'});
  assert.ok(await editor.getByLabel('首选接入点').isVisible());
  await editor.getByLabel(/这是前置机/).check();
  await editor.getByLabel('首选接入点').waitFor({state: 'detached'});
  // Itself is no exit of its own.
  assert.ok(!(await editor.getByLabel('出口', {exact: true}).locator('option').allInnerTexts()).includes('Delta-JP'));
  await editor.getByLabel('出口', {exact: true}).selectOption('srv-papa');
  const address = editor.getByLabel('出口地址', {exact: true});
  await address.locator('option[value="203.0.113.38"]').waitFor({state: 'attached'});
  await address.selectOption('203.0.113.38');
  // Another exit: the address picked for the first one goes, and an exit reporting none says so.
  await editor.getByLabel('出口', {exact: true}).selectOption('srv-bravo');
  await address.locator('option', {hasText: '出口没有可用地址'}).waitFor({state: 'attached'});
  assert.equal(await address.inputValue(), '');
  await editor.getByLabel('出口', {exact: true}).selectOption('srv-papa');
  await address.locator('option[value="10.0.10.246"]').waitFor({state: 'attached'});
  await address.selectOption('10.0.10.246');
  await editor.getByRole('button', {name: '保存'}).click();
  await editor.waitFor({state: 'detached'});
  const patch = h.calls.find(c => c.path === '/api/servers/srv-delta' && c.method === 'PATCH').body;
  assert.deepEqual(patch.profile.front, {exits: [{serverId: 'srv-papa', address: '10.0.10.246'}]});
  await h.close();
});
