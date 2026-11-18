// 前置机 (fronts) on the servers page without a browser: adding one through its exits, and making a server one in its editor.
// The layout of the same forms stays in servers.front.browser.test.mjs.
import test from 'node:test';
import assert from 'node:assert/strict';
import {setup} from './testing/serversDom.mjs';

// The server picker (ddns/NodePicker.vue) the exit and the SSH relay use: its button, and the names (with flags) its list offers.
const picker = (scope, label) => scope.getByRole('button', {name: new RegExp(`^${label}：`)});
async function offered(page, scope, label) {
  await picker(scope, label).click();
  const panel = page.locator('.dd-node-panel');
  await panel.waitFor();
  const rows = {names: await panel.locator('.ss-option .ss-name').allInnerTexts(), flags: await panel.locator('.ss-option .ss-flag').allInnerTexts()};
  await picker(scope, label).click();
  await panel.waitFor({state: 'detached'});
  return rows;
}
async function pick(page, scope, label, name) {
  await picker(scope, label).click();
  const panel = page.locator('.dd-node-panel');
  await panel.waitFor();
  await panel.locator('.ss-option').filter({has: page.locator('.ss-name', {hasText: new RegExp(`^${name}$`)})}).dispatchEvent('pointerdown');
  await panel.waitFor({state: 'detached'});
}

test('add server: a front installs through its exit, its backup optional', async t => {
  for (const [width, touch] of [[1440, false], [390, true]]) {
    const h = await setup(t, {width, height: 844, touch});
    await h.page.getByRole('button', {name: '添加服务器'}).click();
    const dialog = h.page.getByRole('dialog', {name: '添加服务器'});
    await dialog.getByRole('tab', {name: '前置机'}).click();
    // Exits are other servers with a node: not the controller's machine, not one without a node.
    assert.match(await picker(dialog, '出口').innerText(), /选择出口/);
    const {names, flags} = await offered(h.page, dialog, '出口');
    assert.ok(names.includes('Papa-HK.T1') && names.includes('Delta-JP'), names);
    assert.ok(!names.some(n => n.includes('Edge-SG') || n.includes('Hotel-HK')), names);
    assert.equal(flags[names.indexOf('Delta-JP')], '🇯🇵');
    // A front uses no access point.
    assert.equal(await dialog.getByLabel('首选接入点').count(), 0);
    assert.ok(await dialog.getByRole('button', {name: '保存并安装'}).isDisabled());
    // The address is one the exit reports, picked after the exit: its line and its public one behind NAT, not its mesh address.
    const address = dialog.getByLabel('出口地址', {exact: true});
    assert.ok(await address.isDisabled());
    await pick(h.page, dialog, '出口', 'Papa-HK.T1');
    assert.match(await picker(dialog, '出口').innerText(), /Papa-HK\.T1/);
    await address.locator('option[value="10.0.10.246"]').waitFor({state: 'attached'});
    assert.deepEqual(await address.locator('option:not([disabled])').allInnerTexts(), ['10.0.10.246（eth0 · 私有）', '203.0.113.38（公网出口）']);
    await address.selectOption('10.0.10.246');
    await dialog.getByRole('button', {name: '加一个备用出口'}).click();
    // With two exits either can go, so both rows keep the same columns.
    assert.equal(await dialog.locator('.sv-front-exit').filter({has: dialog.getByRole('button', {name: /^去掉/})}).count(), 2);
    // The primary is not offered as the backup too.
    assert.ok(!(await offered(h.page, dialog, '备用出口')).names.includes('Papa-HK.T1'));
    await pick(h.page, dialog, '备用出口', 'Delta-JP');
    const backupAddress = dialog.getByLabel('备用出口地址');
    await backupAddress.locator('option[value="10.77.17.46"]').waitFor({state: 'attached'});
    assert.ok(!(await backupAddress.locator('option').allInnerTexts()).some(t => t.startsWith('fe80')));
    await backupAddress.selectOption('10.77.17.46');
    // Its SSH goes through its exits unless another server's node is chosen; a front is never one.
    assert.match(await picker(dialog, 'SSH 经由').innerText(), /经出口（默认）/);
    const relays = (await offered(h.page, dialog, 'SSH 经由')).names;
    assert.ok(relays[0] === '经出口（默认）' && relays.includes('Delta-JP') && !relays.some(n => n.includes('Edge-SG')), relays);
    await pick(h.page, dialog, 'SSH 经由', 'Delta-JP');
    assert.equal(await dialog.getByLabel('前置机地址').getAttribute('placeholder'), 'Delta-JP 登录前置机用的地址');
    await dialog.getByLabel('前置机地址').fill('172.16.10.203');
    await dialog.getByLabel('SSH 端口').fill('2222');
    await dialog.getByLabel('服务器名称').fill('广州');
    await dialog.getByRole('button', {name: '保存并安装'}).click();
    await dialog.locator('.ip-steps > li[data-state=running]').first().waitFor();
    const create = h.calls.find(c => c.path === '/api/servers' && c.method === 'POST').body;
    assert.deepEqual(create.profile, {front: {exits: [{serverId: 'srv-papa', address: '10.0.10.246'}, {serverId: 'srv-delta', address: '10.77.17.46'}]}});
    assert.deepEqual({name: create.name, host: create.ssh.host, port: create.ssh.port, relay: create.ssh.relay}, {name: '广州', host: '172.16.10.203', port: 2222, relay: 'srv-delta'});
    // The browser is not turned on for a front.
    assert.deepEqual(h.calls.find(c => c.path.endsWith('/install') && c.method === 'POST').body, {});
    await h.close();
  }
});

test('add server: a front given an install command instead, no SSH asked', async t => {
  const h = await setup(t);
  await h.page.getByRole('button', {name: '添加服务器'}).click();
  const dialog = h.page.getByRole('dialog', {name: '添加服务器'});
  await dialog.getByRole('tab', {name: '前置机'}).click();
  await pick(h.page, dialog, '出口', 'Papa-HK.T1');
  const address = dialog.getByLabel('出口地址', {exact: true});
  await address.locator('option[value="10.0.10.246"]').waitFor({state: 'attached'});
  await address.selectOption('10.0.10.246');
  await dialog.getByLabel('安装命令').check();
  // No login, no relay: the command runs on the front itself.
  for (const label of ['前置机地址', 'SSH 用户']) assert.equal(await dialog.getByLabel(label).count(), 0, label);
  assert.equal(await picker(dialog, 'SSH 经由').count(), 0);
  const make = dialog.getByRole('button', {name: '生成安装命令'});
  assert.ok(await make.isDisabled());
  await dialog.getByLabel('服务器名称').fill('广州');
  await make.click();
  await dialog.getByText('30 分钟内有效，只能用一次').waitFor();
  const create = h.calls.find(c => c.path === '/api/servers' && c.method === 'POST').body;
  assert.deepEqual(create, {name: '广州', profile: {front: {exits: [{serverId: 'srv-papa', address: '10.0.10.246'}]}}});
  assert.deepEqual(h.calls.find(c => c.path.endsWith('/enrollment')).body, {browser: false});
  assert.ok(!h.calls.some(c => c.path.endsWith('/install')));
  await h.close();
});

test('add server: an ordinary login can go through another server too', async t => {
  const h = await setup(t);
  await h.page.getByRole('button', {name: '添加服务器'}).click();
  const dialog = h.page.getByRole('dialog', {name: '添加服务器'});
  assert.equal((await offered(h.page, dialog, 'SSH 经由')).names[0], '直接连接（默认）');
  await dialog.getByLabel('服务器地址').fill('10.89.0.222');
  await dialog.getByLabel('SSH 端口').fill('22');
  await pick(h.page, dialog, 'SSH 经由', 'Delta-JP');
  await dialog.getByLabel('服务器名称').fill('机房');
  await dialog.getByRole('button', {name: '保存并安装'}).click();
  await dialog.locator('.ip-steps > li[data-state=running]').first().waitFor();
  assert.equal(h.calls.find(c => c.path === '/api/servers' && c.method === 'POST').body.ssh.relay, 'srv-delta');
  await h.close();
});

test('detail: an offline front whose exit already serves it points at the exit\'s firewall; an online one says nothing', async t => {
  // GoldenCloud is offline, Delta-JP online; each a front of Papa, which serves it.
  for (const [name, id, offline] of [['GoldenCloud', 'srv-golf', true], ['Delta-JP', 'srv-delta', false]]) {
    const h = await setup(t, {tweak: items => {
      const item = items.find(i => i.id === id);
      item.profile = {...item.profile, front: {exits: [{serverId: 'srv-papa', address: '10.0.10.246'}]}};
    }});
    await h.page.locator('.pb-card').filter({hasText: name}).click();
    const detail = h.page.locator('.sv-detail:visible');
    await detail.locator('.dt-tabs').waitFor();
    const waiting = detail.locator('[data-front-waiting]');
    if (offline) assert.match(await waiting.innerText(), /出口已在 https:\/\/10\.0\.10\.246:52002 等待这台前置机连接[\s\S]*防火墙放行这个 TCP 端口[\s\S]*专线内网地址/);
    else assert.equal(await waiting.count(), 0);
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
  assert.ok(!(await offered(h.page, editor, '出口')).names.includes('Delta-JP'));
  await pick(h.page, editor, '出口', 'Papa-HK.T1');
  const address = editor.getByLabel('出口地址', {exact: true});
  await address.locator('option[value="203.0.113.38"]').waitFor({state: 'attached'});
  await address.selectOption('203.0.113.38');
  // Another exit: the address picked for the first one goes, and an exit reporting none says so.
  await pick(h.page, editor, '出口', 'Bravo-FRA');
  await address.locator('option', {hasText: '出口没有可用地址'}).waitFor({state: 'attached'});
  assert.equal(await address.inputValue(), '');
  await pick(h.page, editor, '出口', 'Papa-HK.T1');
  await address.locator('option[value="10.0.10.246"]').waitFor({state: 'attached'});
  await address.selectOption('10.0.10.246');
  await editor.getByRole('button', {name: '保存'}).click();
  await editor.waitFor({state: 'detached'});
  const patch = h.calls.find(c => c.path === '/api/servers/srv-delta' && c.method === 'PATCH').body;
  assert.deepEqual(patch.profile.front, {exits: [{serverId: 'srv-papa', address: '10.0.10.246'}]});
  await h.close();
});
