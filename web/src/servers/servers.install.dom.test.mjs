// The servers page without a browser (adding and installing servers, registration codes, settings): what the page asks the backend, what it shows and how it
// switches, checked in happy-dom with the browser tests' fake backend (testing/serversBackend.mjs).
// The layout of the same scenarios (sizes, overflow, scrolling, phone and dark) stays in
// servers.*.browser.test.mjs.
import test from 'node:test';
import assert from 'node:assert/strict';
import {setup, tab} from './testing/serversDom.mjs';
import {server, card} from './testing/serversBackend.mjs';

test('registration code join pending: card tag, notice, keep / take over / reject, phone without overflow', async t => {
  const pending = (items, id, name) => {
    const clone = JSON.parse(JSON.stringify(items.find(i => i.id === 'srv-delta')));
    Object.assign(clone, {id, name});
    clone.node.joinPending = {serverId: 'srv-delta', name: 'Delta-JP', code: '新开的 VPS', atMs: Date.now()};
    items.push(clone);
  };
  for (const [width, touch] of [[1440, false], [390, true]]) {
    const h = await setup(t, {width, height: 844, touch, tweak: items => {
      pending(items, 'srv-new1', 'delta (2)'); pending(items, 'srv-new2', 'delta (3)'); pending(items, 'srv-new3', 'delta (4) 一个很长很长的主机名用来检查通知按钮的换行');
    }});
    const card = h.page.locator('.pb-card').filter({hasText: 'delta (2)'});
    assert.equal(await card.locator('[data-join-pending]').innerText(), '待确认接入');
    assert.equal(await h.page.locator('.pb-card').filter({hasText: 'Delta-JP'}).locator('[data-join-pending]').count(), 0);

    // Keep: a server of its own.
    await card.click();
    let notice = h.page.locator('.sv-detail:visible [data-join-pending]');
    await notice.getByText('机器 ID 和主机名与「Delta-JP」相同').waitFor();
    await Promise.all([h.page.waitForResponse(r => r.url().endsWith('/api/servers/srv-new1/join')),
      notice.getByRole('button', {name: '作为新服务器保留'}).click()]);
    await notice.waitFor({state: 'detached'});
    assert.deepEqual(h.calls.find(c => c.path === '/api/servers/srv-new1/join').body, {action: 'keep'});

    // Take over: confirmed first, then the server it looked like opens and this one goes.
    await tab(h.page, '探针').click();
    await h.page.locator('.pb-card').filter({hasText: 'delta (3)'}).click();
    notice = h.page.locator('.sv-detail:visible [data-join-pending]');
    await notice.getByRole('button', {name: '接管「Delta-JP」'}).click();
    const dialog = h.page.locator('.el-message-box');
    await dialog.getByText('「Delta-JP」改由这台机器的节点连接').waitFor();
    await Promise.all([h.page.waitForResponse(r => r.url().endsWith('/api/servers/srv-new2/join')), dialog.getByRole('button', {name: '接管'}).click()]);
    await h.page.locator('.sv-detail:visible').getByRole('heading', {name: /Delta-JP/}).waitFor();
    assert.deepEqual(h.calls.find(c => c.path === '/api/servers/srv-new2/join').body, {action: 'takeover'});

    // Reject, with the long name.
    await tab(h.page, '探针').click();
    await h.page.locator('.pb-card').filter({hasText: '一个很长很长的主机名'}).click();
    notice = h.page.locator('.sv-detail:visible [data-join-pending]');
    await notice.getByRole('button', {name: '拒绝'}).click();
    await Promise.all([h.page.waitForResponse(r => r.url().endsWith('/api/servers/srv-new3/join')),
      h.page.locator('.el-message-box').getByRole('button', {name: '拒绝'}).click()]);
    assert.deepEqual(h.calls.find(c => c.path === '/api/servers/srv-new3/join').body, {action: 'reject'});
    await h.close();
  }
});

test('install command: Linux / macOS or Windows', async t => {
  const h = await setup(t);
  await h.page.locator('.pb-card').filter({hasText: 'Hotel-HK'}).click();
  const detail = h.page.locator('.sv-detail:visible');
  await detail.getByRole('button', {name: '生成安装命令'}).click();
  const block = detail.locator('.sv-install-command');
  await block.locator('.sv-command > code').filter({hasText: 'install.sh'}).waitFor();
  assert.match(await block.locator('.ix-card-note').innerText(), /以 root 执行/);
  await block.getByRole('button', {name: 'Windows'}).click();
  assert.match(await block.locator('.sv-command > code').innerText(), /^\[Net\.ServicePointManager\]::SecurityProtocol=3072; & \(\[scriptblock\]::Create/);
  assert.match(await block.locator('.ix-card-note').innerText(), /以管理员身份运行.*PowerShell/);
  await block.getByRole('button', {name: '复制安装命令'}).click();
  assert.match(await h.page.evaluate(() => navigator.clipboard.readText()), /install\.ps1/);
  await h.close();
});

test('settings: reminder rules, test reminder and recent list', async t => {
  const h = await setup(t);
  await h.page.getByRole('button', {name: '设置'}).click();
  const dialog = h.page.getByRole('dialog', {name: '服务器设置'});
  await dialog.waitFor();
  assert.deepEqual(await dialog.locator('.sv-chip').allInnerTexts(), ['7 天', '3 天', '1 天', '当天', '80%', '90%', '100%']);
  await dialog.getByRole('button', {name: '删除 3 天'}).click();
  await dialog.getByRole('spinbutton', {name: '添加到期提醒天数'}).fill('14');
  await dialog.getByRole('spinbutton', {name: '添加到期提醒天数'}).press('Enter');
  await dialog.getByRole('spinbutton', {name: '添加流量提醒百分比'}).fill('95');
  await dialog.getByRole('spinbutton', {name: '添加流量提醒百分比'}).press('Enter');
  await dialog.getByLabel('每天提醒时间').fill('08:30');
  await dialog.getByRole('button', {name: '发一条测试提醒'}).click();
  await h.page.getByText('测试提醒已发出').waitFor();
  await dialog.getByText('最近的提醒（1）').click();
  await dialog.getByText('本期流量已用 100%', {exact: false}).waitFor();
  await dialog.getByText('0.13.1（本地构建）').waitFor();
  await dialog.getByText('每天自动检查一次 GitHub 上的新版本').waitFor();
  await dialog.getByRole('button', {name: '检查新版本'}).click();
  await dialog.getByText('1.0.2（GitHub）').waitFor();
  assert.match(await dialog.locator('.sv-release').locator('..').innerText(), /检查过/);
  await dialog.getByRole('button', {name: '保存'}).click();
  await dialog.waitFor({state: 'hidden'});
  const saved = h.calls.find(c => c.path === '/api/servers/settings' && c.method === 'PUT').body;
  assert.deepEqual(saved.reminders, {enabled: true, time: '08:30', expiryDays: [14, 7, 1, 0], trafficPercents: [80, 90, 95, 100], autoRenew: true});
  await h.close();
});

test('add server: SSH install with steps, batch, one-time command and registration codes', async t => {
  const h = await setup(t);
  await h.page.getByRole('button', {name: '添加服务器'}).click();
  const dialog = h.page.getByRole('dialog', {name: '添加服务器'});
  // Host and user follow the controller's rules before anything is sent; surrounding spaces are trimmed.
  const valid = label => dialog.getByLabel(label).evaluate(el => el.checkValidity());
  await dialog.getByLabel('服务器地址').fill('bad_host.example.com');
  assert.equal(await valid('服务器地址'), false);
  await dialog.getByLabel('SSH 用户').fill('ro ot');
  assert.equal(await valid('SSH 用户'), false);
  await dialog.getByLabel('SSH 用户').fill(' root ');
  assert.equal(await valid('SSH 用户'), true);
  await dialog.getByLabel('服务器地址').fill(' 203.0.113.10');
  assert.equal(await valid('服务器地址'), true);
  await dialog.getByLabel('服务器地址').fill('203.0.113.10');
  await dialog.getByLabel('SSH 端口').fill('27920');
  await dialog.getByLabel('服务器名称').fill('东京');
  // Password managers must not take server fields for identities.
  for (const label of ['服务器名称', '服务器地址', 'SSH 用户', '分组']) assert.equal(await dialog.getByLabel(label).getAttribute('data-1p-ignore'), '');
  // Existing groups come from our own list, filtered as you type.
  const group = dialog.getByRole('combobox', {name: '分组'});
  await group.click();
  const list = h.page.getByRole('listbox');
  await list.getByRole('option').first().waitFor();
  assert.deepEqual(await list.getByRole('option').allInnerTexts(), ['测试机', 'Tier 1', '流媒体']);
  await group.fill('t');
  assert.deepEqual(await list.getByRole('option').allInnerTexts(), ['Tier 1']);
  await group.press('ArrowDown');
  await group.press('Enter');
  assert.equal(await group.inputValue(), 'Tier 1');
  await list.waitFor({state: 'hidden'});
  await group.press('ArrowDown');
  await list.getByRole('option', {name: '流媒体'}).click();
  assert.equal(await group.inputValue(), '流媒体');
  await group.press('ArrowDown');
  await group.press('Escape');
  await list.waitFor({state: 'hidden'});
  assert.ok(await dialog.isVisible(), 'Escape closes only the list');
  await group.fill('');
  await dialog.getByLabel('首选接入点').focus();
  await list.waitFor({state: 'hidden'});
  await dialog.getByLabel('首选接入点').selectOption('ap-cn');
  assert.deepEqual(await dialog.getByLabel('备用接入点').locator('option').allInnerTexts(), ['不使用', '海外直连']);
  await dialog.getByLabel('备用接入点').selectOption('ap-global');
  await dialog.getByRole('button', {name: '保存并安装'}).click();
  await dialog.locator('.ip-steps > li[data-state=running]').first().waitFor();
  await dialog.locator('.ip-steps > li[data-state=done]').nth(4).waitFor({timeout: 20000});
  assert.ok(await dialog.getByText('Debian 12 · amd64 · systemd').isVisible());
  const create = h.calls.find(c => c.path === '/api/servers' && c.method === 'POST').body;
  assert.deepEqual(create.profile, {access: {primary: 'ap-cn', backup: 'ap-global'}});
  assert.deepEqual({host: create.ssh.host, port: create.ssh.port, keyRef: create.ssh.keyRef}, {host: '203.0.113.10', port: 27920, keyRef: {secret: 'ssh-key', field: '私钥'}});

  await dialog.getByRole('button', {name: '继续添加'}).click();
  await dialog.getByRole('tab', {name: '批量 SSH'}).click();
  await dialog.getByLabel('服务器列表').fill('东京 203.0.113.10 27920\n198.51.100.7\n坏的 999.1 x');
  await dialog.getByText('第 3 行格式不对').waitFor();
  assert.ok(await dialog.getByRole('button', {name: /开始安装/}).isDisabled());
  // The controller accepts letters, digits, dots, colons and dashes in a host, and at most 50 lines.
  await dialog.getByLabel('服务器列表').fill('东京 bad_host.example.com 22');
  await dialog.getByText('第 1 行格式不对').waitFor();
  await dialog.getByLabel('服务器列表').fill(Array.from({length: 51}, (_, i) => `s${i} 192.0.2.${i + 1}`).join('\n'));
  await dialog.getByText('一次最多安装 50 台，现在有 51 台，请分批添加。').waitFor();
  assert.ok(await dialog.getByRole('button', {name: /开始安装/}).isDisabled());
  await dialog.getByLabel('服务器列表').fill('东京 203.0.113.10 27920\n198.51.100.7\n大阪 192.0.2.99');
  await dialog.getByText('将安装 3 台：东京、198.51.100.7、大阪').waitFor();
  await dialog.getByRole('button', {name: '开始安装（3 台）'}).click();
  await dialog.getByText('2 台已上线 · 1 台失败 · 共 3 台').waitFor({timeout: 15000});
  assert.ok(await dialog.locator('.as-batch > li[data-state=failed]').getByText('SSH 连接失败').isVisible());
  const batch = h.calls.find(c => c.path === '/api/servers/batch-install').body;
  assert.deepEqual(batch.servers, [{name: '东京', host: '203.0.113.10', port: 27920}, {name: '198.51.100.7', host: '198.51.100.7', port: 22}, {name: '大阪', host: '192.0.2.99', port: 22}]);

  await dialog.getByRole('button', {name: '继续添加'}).click();
  await dialog.getByRole('tab', {name: '安装命令'}).click();
  await dialog.getByLabel('服务器名称').fill('家里的 NAS');
  await dialog.getByRole('button', {name: '生成安装命令'}).click();
  await dialog.getByText('--key mc4CAQ', {exact: false}).waitFor();
  await dialog.locator('.ip-steps > li[data-state=done]').nth(2).waitFor({timeout: 15000});

  await dialog.getByRole('button', {name: '继续添加'}).click();
  await dialog.getByRole('tab', {name: '注册码'}).click();
  await dialog.getByText('已用 2 次', {exact: false}).waitFor();
  await dialog.getByLabel('注册码名称').fill('批量开机');
  await dialog.getByLabel('有效期').selectOption('7');
  await dialog.getByLabel('可用次数').selectOption('20');
  await dialog.getByRole('button', {name: '新建注册码'}).click();
  const fresh = dialog.locator('.as-codes > li.is-fresh');
  await fresh.getByText('--code new1', {exact: false}).waitFor();
  assert.ok(await fresh.getByText('已用 0/20 次', {exact: false}).isVisible());
  await dialog.locator('.as-codes > li').filter({hasText: '新开的 VPS'}).getByRole('button', {name: '作废'}).click();
  await h.page.getByRole('button', {name: '作废', exact: true}).last().click();
  await dialog.locator('.as-codes > li').filter({hasText: '新开的 VPS'}).getByText('已作废').waitFor();
  await h.close();
});

test('installs on a machine that already runs a node: open that server, or override after asking; added, in a batch and from the detail; phone light and dark', async t => {
  const installs = h => h.calls.filter(c => c.method === 'POST' && /^\/api\/servers\/[\w-]+\/install$/.test(c.path));
  const confirmBox = page => page.locator('.el-message-box');
  let h = await setup(t);
  await h.page.getByRole('button', {name: '添加服务器'}).click();
  let dialog = h.page.getByRole('dialog', {name: '添加服务器'});
  // Connected here as another server: its message once, under the steps, and a way to that server; nothing to override.
  await dialog.getByLabel('服务器地址').fill('203.0.113.66');
  await dialog.getByLabel('服务器名称').fill('重复的 Papa');
  await dialog.getByRole('button', {name: '保存并安装'}).click();
  let conflict = dialog.locator('.sv-conflict');
  await conflict.waitFor({timeout: 15000});
  assert.equal(await conflict.locator('span').first().innerText(), '这台机器已经作为「Papa-HK.T1」接入，先删除那台或直接在它上面重新安装');
  assert.equal(await dialog.locator('.ip-steps > li[data-state=failed] .ip-label').innerText(), '连接并检查系统');
  assert.equal(await dialog.locator('.ip-detail').count(), 0, 'the message is not repeated on the step');
  assert.equal(await dialog.locator('.ip-steps > li[data-state=pending]').count(), 4);
  assert.equal(await dialog.locator('.ip-error').count(), 0);
  assert.equal(await conflict.getByRole('button', {name: '仍然覆盖安装'}).count(), 0);
  await conflict.getByRole('button', {name: '打开「Papa-HK.T1」'}).click();
  await dialog.waitFor({state: 'hidden'});
  assert.equal(await h.page.locator('.sv-tab.is-server[aria-selected=true] .sv-tab-name').innerText(), 'Papa-HK.T1');

  // A node this controller does not know: override, asked first; cancelling sends nothing, confirming sends force.
  await h.page.getByRole('button', {name: '添加服务器'}).click();
  dialog = h.page.getByRole('dialog', {name: '添加服务器'});
  await dialog.getByLabel('服务器地址').fill('203.0.113.77');
  await dialog.getByLabel('服务器名称').fill('别人的机器');
  await dialog.getByRole('button', {name: '保存并安装'}).click();
  conflict = dialog.locator('.sv-conflict');
  await conflict.waitFor({timeout: 15000});
  assert.match(await conflict.innerText(), /这台机器上已经装有其他节点（7f3c9a2e-1b4d-4c6e-9f00-2a7b5c1d8e43，可能属于别的主控）/);
  assert.equal(await conflict.getByRole('button', {name: /^打开/}).count(), 0);
  const before = installs(h).length;
  await conflict.getByRole('button', {name: '仍然覆盖安装'}).click();
  await confirmBox(h.page).waitFor();
  assert.match(await confirmBox(h.page).innerText(), /仍然覆盖安装[\s\S]*这台机器上已经装有其他节点（7f3c9a2e-1b4d-4c6e-9f00-2a7b5c1d8e43），可能属于别的主控。覆盖安装会替换掉它，原来管理它的主控将失去这台机器。确定要覆盖吗？/);
  await confirmBox(h.page).getByRole('button', {name: '取消'}).click();
  await confirmBox(h.page).waitFor({state: 'hidden'});
  assert.equal(installs(h).length, before);
  await conflict.getByRole('button', {name: '仍然覆盖安装'}).click();
  await confirmBox(h.page).getByRole('button', {name: '覆盖安装'}).click();
  await dialog.locator('.ip-steps > li[data-state=done]').nth(4).waitFor({timeout: 20000});
  assert.deepEqual(installs(h).at(-1).body, {browser: false, force: true});
  assert.equal(await dialog.locator('.sv-conflict').count(), 0);
  // The first try carried no force.
  assert.deepEqual(installs(h)[before - 1].body, {browser: false});

  // A batch: each clash on its own line, with its way out; one overridden goes on to online.
  await dialog.getByRole('button', {name: '继续添加'}).click();
  await dialog.getByRole('tab', {name: '批量 SSH'}).click();
  await dialog.getByLabel('服务器列表').fill('甲 203.0.113.66\n乙 203.0.113.77\n东京 203.0.113.10 27920');
  await dialog.getByRole('button', {name: '开始安装（3 台）'}).click();
  await dialog.getByText('1 台已上线 · 2 台失败 · 共 3 台').waitFor({timeout: 15000});
  const row = name => dialog.locator('.as-batch > li').filter({has: h.page.locator('strong', {hasText: new RegExp(`^${name}$`)})});
  assert.equal(await row('甲').locator('.as-batch-state').innerText(), '已作为另一台接入');
  assert.match(await row('甲').locator('.sv-conflict').innerText(), /已经作为「Papa-HK.T1」接入[\s\S]*打开「Papa-HK.T1」/);
  assert.equal(await row('乙').locator('.as-batch-state').innerText(), '已装有其他节点');
  assert.equal(await row('东京').locator('.sv-conflict').count(), 0);
  await row('乙').getByRole('button', {name: '仍然覆盖安装'}).click();
  await confirmBox(h.page).getByRole('button', {name: '覆盖安装'}).click();
  await dialog.getByText('2 台已上线 · 1 台失败 · 共 3 台').waitFor({timeout: 20000});
  const forced = installs(h).at(-1);
  assert.match(forced.path, /^\/api\/servers\/srv-b\d+\/install$/);
  assert.deepEqual(forced.body, {browser: false, force: true});
  assert.equal(await row('乙').locator('.as-batch-state').innerText(), '已上线');
  assert.equal(await row('乙').locator('.sv-conflict').count(), 0);
  await row('甲').getByRole('button', {name: '打开「Papa-HK.T1」'}).click();
  await dialog.waitFor({state: 'hidden'});
  assert.equal(await h.page.locator('.sv-tab.is-server[aria-selected=true] .sv-tab-name').innerText(), 'Papa-HK.T1');
  await h.close();

  // From a server's detail: the same, with the install card's own button.
  h = await setup(t, {tweak: items => { items.find(i => i.id === 'srv-hotel').ssh.host = '203.0.113.77'; }});
  await h.page.locator('.pb-card').filter({hasText: 'Hotel-HK'}).click();
  const card = h.page.locator('.sv-detail:visible .sv-install');
  // The manual command's note says how to override there.
  await card.getByRole('button', {name: '生成安装命令'}).click();
  assert.match(await card.locator('.ix-card-note').innerText(), /机器上已有其他节点时命令会停下，确认要覆盖就在末尾加 --force（Windows 加 -Force）。/);
  await card.getByRole('button', {name: 'Windows'}).click();
  assert.match(await card.locator('.ix-card-note').innerText(), /机器上已有其他节点时命令会停下，确认要覆盖就在末尾加 -Force。$/);
  await card.getByRole('button', {name: '通过 SSH 安装'}).click();
  conflict = card.locator('.sv-conflict');
  await conflict.waitFor({timeout: 15000});
  await conflict.getByRole('button', {name: '仍然覆盖安装'}).click();
  await confirmBox(h.page).getByRole('button', {name: '覆盖安装'}).click();
  await h.page.waitForFunction(() => installs(h).length >= 2);
  assert.deepEqual(installs(h).map(c => c.body), [{browser: false}, {browser: false, force: true}]);
  await h.close();
  h = await setup(t, {tweak: items => { items.find(i => i.id === 'srv-hotel').ssh.host = '203.0.113.66'; }});
  await h.page.locator('.pb-card').filter({hasText: 'Hotel-HK'}).click();
  await h.page.locator('.sv-detail:visible .sv-install').getByRole('button', {name: '通过 SSH 安装'}).click();
  await h.page.locator('.sv-detail:visible .sv-conflict').getByRole('button', {name: '打开「Papa-HK.T1」'}).click();
  await h.page.locator('.sv-tab.is-server[aria-selected=true] .sv-tab-name', {hasText: 'Papa-HK.T1'}).waitFor();
  await h.close();

  // Phone, dark and light: the add sheet with a clash, nothing wider than the screen, buttons within reach.
  for (const dark of [true, false]) {
    const p = await setup(t, {width: 390, height: 844, dark});
    await p.page.getByRole('button', {name: '添加服务器'}).click();
    const d = p.page.getByRole('dialog', {name: '添加服务器'});
    await d.getByLabel('服务器地址').fill('203.0.113.77');
    await d.getByRole('button', {name: '保存并安装'}).click();
    const c = d.locator('.sv-conflict');
    await c.waitFor({timeout: 15000});
    await d.getByRole('button', {name: '继续添加'}).click();
    await d.getByRole('tab', {name: '批量 SSH'}).click();
    await d.getByLabel('服务器列表').fill('甲 203.0.113.66\n乙 203.0.113.77\n东京 203.0.113.10 27920');
    await d.getByRole('button', {name: '开始安装（3 台）'}).click();
    await d.getByText('1 台已上线 · 2 台失败 · 共 3 台').waitFor({timeout: 15000});
    await p.close();
  }
});

test('settings: access points with test, default and guide', async t => {
  const h = await setup(t);
  await h.page.getByRole('button', {name: '设置'}).click();
  const dialog = h.page.getByRole('dialog', {name: '服务器设置'});
  await dialog.locator('.ap-row').nth(1).waitFor();
  assert.ok(await dialog.locator('.ap-row').nth(1).getByText('2 台在用').isVisible());
  assert.ok(await dialog.locator('.ap-row').nth(1).getByRole('button', {name: '删除'}).isDisabled());
  await dialog.locator('.ap-row').nth(1).getByRole('button', {name: '测试'}).click();
  await dialog.getByText('✓ 可用 · 48 ms').waitFor();
  await dialog.getByRole('button', {name: '＋ 添加接入点'}).click();
  await dialog.getByLabel('第 3 个接入点名称').fill('WG 内网');
  await dialog.getByLabel('第 3 个接入点地址').fill('https://broken.example.com');
  await dialog.locator('.ap-row').nth(2).getByRole('button', {name: '测试'}).click();
  await dialog.getByText('没有转发到 OpenBear', {exact: false}).waitFor();
  await dialog.getByLabel('第 3 个接入点地址').fill('http://192.0.2.2:18961');
  await dialog.getByLabel('WG 内网设为默认').check();
  await dialog.getByLabel('第 2 个接入点地址').fill('https://node-cn.example.com:30443');
  await dialog.getByRole('button', {name: '保存'}).click();
  await dialog.getByText('接入点地址已修改').waitFor();
  const saved = h.calls.find(c => c.path === '/api/servers/settings' && c.method === 'PUT').body;
  assert.equal(saved.accessPoints.length, 3);
  assert.deepEqual(saved.accessPoints.slice(0, 2).map(p => p.id), ['ap-global', 'ap-cn']);
  assert.equal(saved.accessPoints[1].url, 'https://node-cn.example.com:30443');
  assert.equal(saved.defaultAccess, saved.accessPoints[2].id);
  assert.match(saved.defaultAccess, /^ap-[0-9a-f]{6}$/);
  await dialog.getByRole('button', {name: '让在线节点改用新地址'}).click();
  await dialog.waitFor({state: 'hidden'});
  await h.page.getByText('2 台已改用新地址，1 台连不上新地址，暂时保持原地址，下次连上主控时再切换').waitFor();
  assert.ok(h.calls.some(c => c.path === '/api/servers/node-url'));

  await h.page.getByRole('button', {name: '添加服务器'}).click();
  await h.page.getByRole('dialog', {name: '添加服务器'}).getByRole('button', {name: '接入说明'}).click();
  const guide = h.page.locator('.sv-guide-drawer');
  await guide.getByRole('heading', {name: '情况三：国内服务器连海外的 OpenBear 很慢或不稳'}).waitFor();
  await h.page.keyboard.press('Escape');
  await guide.waitFor({state: 'hidden'});

  // Editing a server moves its node first; when the node refuses, nothing is saved
  // unless the edit is kept for its next connection, which the detail then shows.
  await h.page.getByRole('dialog', {name: '添加服务器'}).getByRole('button', {name: '取消'}).click();
  await h.page.locator('.pb-card').filter({hasText: 'Delta-JP'}).click();
  const detail = h.page.locator('.sv-detail:visible');
  await detail.getByText('海外直连（首选）', {exact: false}).waitFor();
  await detail.getByRole('button', {name: '编辑'}).first().click();
  const editor = h.page.getByRole('dialog', {name: '编辑「Delta-JP」'});
  await editor.getByLabel('首选接入点').selectOption('ap-cn');
  await editor.getByRole('button', {name: '保存', exact: true}).click();
  await editor.getByText('节点连不上新地址，已保持原地址。这次的修改都没有保存。').waitFor();
  await editor.getByRole('button', {name: '只保存，节点下次连上时再切换'}).click();
  await editor.waitFor({state: 'hidden'});
  const patches = h.calls.filter(c => c.path === '/api/servers/srv-delta' && c.method === 'PATCH').map(c => [c.body.profile.access, c.body.accessApply]);
  assert.deepEqual(patches, [[{primary: 'ap-cn', backup: ''}, undefined], [{primary: 'ap-cn', backup: ''}, 'later']]);
  await detail.getByText('国内中转（首选） · 待生效，节点下次连上时切换').waitFor();
  await h.close();
});

test('phone: add server sheet, batch lines, access picker and guide', async t => {
  const h = await setup(t, {width: 390, height: 844});
  await h.page.getByRole('button', {name: '添加服务器'}).click();
  const dialog = h.page.getByRole('dialog', {name: '添加服务器'});
  await dialog.getByLabel('服务器地址').waitFor();
  await dialog.getByRole('tab', {name: '批量 SSH'}).click();
  await dialog.getByLabel('服务器列表').fill('东京 203.0.113.10 27920\n198.51.100.7\n大阪 192.0.2.99');
  await dialog.getByRole('button', {name: '开始安装（3 台）'}).click();
  await dialog.getByText('2 台已上线 · 1 台失败 · 共 3 台').waitFor({timeout: 15000});
  await dialog.getByRole('button', {name: '继续添加'}).click();
  await dialog.getByRole('tab', {name: '注册码'}).click();
  await dialog.getByText('已用 2 次', {exact: false}).waitFor();
  await dialog.getByRole('button', {name: '接入说明'}).click();
  const guide = h.page.locator('.sv-guide-drawer');
  await guide.getByRole('heading', {name: '第二步：添加服务器'}).scrollIntoViewIfNeeded();
  await h.close();
});
