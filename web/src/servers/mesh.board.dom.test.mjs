// The mesh board (servers page, 组网 tab) without a browser: what it asks the controller, what it
// shows and how it switches, in happy-dom with the browser tests' fake controller
// (testing/meshBackend.mjs, mounted by testing/meshDom.mjs). Long timers run 10 times faster
// (scaleTimers), so "no request within 6 s" waits 0.6 s. The layout of the same scenarios stays in
// mesh.*.browser.test.mjs.
import test from 'node:test';
import assert from 'node:assert/strict';
import {setup, node, label, inspector, title, memberOf, dimmed, texts, until} from './testing/meshDom.mjs';
import {GB, server, member} from './testing/meshBackend.mjs';

test('desktop board: servers on the left, links on the board, join by dragging a server in', async t => {
  const h = await setup(t);
  const {page} = h;
  assert.equal(await page.locator('.sv-tabs [role=tab]').nth(2).innerText(), '组网');
  assert.match(await page.locator('.mesh-summary').innerText(), /100\.96\.0\.0\/16\s*3 台成员\s*2 台中转\s*全部就绪/);
  assert.equal(await page.locator('.vue-flow__node').count(), 3);
  // Relays above, the member below, as saved.
  assert.match(await node(page, 'lax').innerText(), /LAX\s*100\.96\.0\.2\s*中转/);
  assert.match(await node(page, 'home').innerText(), /Home\s*100\.96\.0\.3\s*152 ms/);
  // Entry candidates: order and transport; the one in use is highlighted, the other a quiet standby
  // without its label until Home is pointed at.
  assert.equal(await label(page, '2 · TCP').getAttribute('data-look'), 'active');
  assert.equal(await label(page, '1 · UDP').count(), 0);
  assert.equal(await page.locator('.mesh-edge[data-kind=entry][data-look=standby][data-quiet]').count(), 1);
  await node(page, 'home').hover();
  assert.equal(await label(page, '1 · UDP').getAttribute('data-look'), 'standby');
  await node(page, 'home').dispatchEvent('mouseleave'); await node(page, 'home').dispatchEvent('pointerleave');
  await label(page, '1 · UDP').waitFor({state: 'detached'});
  assert.equal(await page.locator('.mesh-edge[data-kind=relay][data-look=auto]').count(), 1, 'all relays linked: one faint pair');
  // Left: who can join and why not.
  const palette = page.locator('.mesh-palette');
  assert.deepEqual(await palette.locator('.mesh-servers-group > h3').allInnerTexts().then(l => l.map(s => s.split(/\s/)[0])), ['可加入', '网格中']);
  assert.equal(await palette.getByRole('button', {name: 'Win-Box：节点离线'}).isDisabled(), true);
  assert.equal(await palette.getByRole('button', {name: 'Old-VPS：节点版本过旧，需要先更新'}).isDisabled(), true);
  await palette.getByRole('searchbox', {name: '搜索服务器'}).fill('mac');
  assert.equal(await palette.locator('.mesh-server').count(), 1);
  await palette.getByRole('searchbox', {name: '搜索服务器'}).fill('');
  // Right, nothing selected: changes, rules and settings.
  assert.equal(await title(page).innerText(), '网格');
  await inspector(page).getByText('没有未应用的更改，已应用第 12 版。').waitFor();
  assert.equal(await inspector(page).getByRole('textbox', {name: '地址段'}).inputValue(), '100.96.0.0/16');
  assert.equal(await page.locator('.mesh-tool[aria-checked=true]').innerText(), '选择');

  // Drag 公司-Mac onto the board: it installs where it was dropped (the browser test measures the point).
  await palette.getByRole('button', {name: '公司-Mac，拖到画板上加入组网'}).dragTo(page.locator('.mesh-canvas'), {targetPosition: {x: 780, y: 500}});
  const mac = node(page, 'mac');
  await mac.locator('.mesh-node[data-installing]').waitFor();
  assert.match(await mac.innerText(), /公司-Mac[\s\S]*安装中/);
  assert.match(await palette.getByRole('button', {name: /^公司-Mac/}).innerText(), /安装中…/);
  assert.deepEqual(h.calls.find(c => c.path === '/api/servers/mesh/members' && c.method === 'POST').body, {serverIds: ['mac']});
  await mac.locator('.mesh-node:not([data-installing])').waitFor({timeout: 8000});
  assert.ok(h.layoutPuts().some(c => c.body.positions.mac), 'the drop position is saved');
  assert.match(await mac.innerText(), /公司-Mac[\s\S]*已安装，未就绪/);
  assert.equal(await title(page).innerText(), '公司-Mac');
  assert.equal(await page.locator('.mesh-stage .mesh-bar strong').innerText(), '有 1 项更改未应用');
  await h.close();
});

test('node properties: settings saved to the draft, live status with hints, trend and events, upgrades', async t => {
  const h = await setup(t, {upgrades: true});
  const {page} = h;
  assert.match(await node(page, 'lax').locator('.mesh-node-badge').innerText(), /^升级中$/);
  assert.match(await node(page, 'berlin').locator('.mesh-node-badge').innerText(), /^排队升级$/);
  await node(page, 'home').click();
  assert.equal(await title(page).innerText(), 'Home');
  assert.equal(await node(page, 'home').locator('.mesh-node[data-selected]').count(), 1);
  const panel = inspector(page);
  await panel.locator('.mesh-sparks > li').nth(3).waitFor();
  const text = await panel.innerText();
  assert.match(text, /实际\s*LAX · TCP · 198\.51\.100\.3:52000/);
  assert.match(await panel.locator('.mesh-member-summary').innerText(), /就绪[\s\S]*成员[\s\S]*经 LAX · TCP[\s\S]*3 项需注意/);
  // One line per hint, memory first; the explanation opens on a click.
  const hintRows = panel.locator('.mesh-hints summary');
  assert.deepEqual(await hintRows.allInnerTexts(), ['内存接近上限（230 MB / 256 MB）', '1 条网格路由没有加上', 'macOS 防火墙已开启']);
  await hintRows.nth(1).click();
  await panel.getByText('本机没有加上：10.8.0.0/16。去往这些网段的流量不经过网格。').waitFor();
  await hintRows.nth(1).click();
  assert.match(text, /内存\s*230 MB \/ 256 MB/);
  assert.match(text, /CPU\s*3\.4%/);
  assert.match(text, /升级失败，已回退到原版本：组网服务升级后没有就绪/);
  assert.deepEqual(await panel.locator('.mesh-spark-label').allInnerTexts(), ['延迟', '探测超时', '内存', 'CPU']);
  assert.match(await panel.locator('.mesh-trend-head').innerText(), /切换 1 次 · 重连 1 次/);
  await panel.getByRole('tab', {name: '24 小时'}).click();
  assert.ok(h.calls.some(c => c.path === '/api/servers/mesh/members/home/trend' && c.query === '?range=24h'));
  await panel.getByRole('tab', {name: '事件'}).click();
  await panel.getByText('第 1 个入口（Berlin · UDP）：探测超时').waitFor();
  assert.equal(await panel.locator('.mesh-events > li').filter({hasText: '线路不通'}).getAttribute('data-tone'), 'bad');
  await panel.getByRole('tab', {name: '对端'}).click();
  await panel.getByText(/重连 2 次/).waitFor();
  await panel.getByRole('tab', {name: '配置'}).click();
  await panel.getByRole('spinbutton', {name: '发送速率'}).fill('90');
  await panel.getByRole('spinbutton', {name: '发送速率'}).press('Tab');
  await h.saved(d => memberOf(d, 'home').uploadLimitMbps === 90, 'upload limit');
  await panel.getByRole('tab', {name: '入口'}).click();
  await panel.getByRole('radio', {name: '固定'}).check();
  await h.saved(d => memberOf(d, 'home').entry.mode === 'fixed', 'entry mode');
  // In fixed mode only the first entry is used; the second stays a standby line.
  assert.equal(await page.locator('.mesh-stage .mesh-bar strong').innerText(), '有 1 项更改未应用');
  await page.keyboard.press('Escape');
  assert.equal(await title(page).innerText(), '网格');
  await h.close();
});

test('rules for all members: the ping button, the "all members" choice on either side, summaries and the controller\'s change text', async t => {
  const h = await setup(t);
  const {page} = h;
  const panel = inspector(page);
  await panel.getByRole('button', {name: '允许所有成员互相 ping'}).click();
  await h.saved(d => d.rules.length === 1 && d.rules[0].src.all && d.rules[0].dst.all, 'ping for all');
  // The new rule opens in the editor: both sides say all members; no member picker, networks can still be added.
  assert.equal(await title(page).innerText(), '访问规则 1');
  for (const side of ['来源', '目标']) {
    assert.equal(await panel.getByRole('checkbox', {name: `${side}：所有成员`}).isChecked(), true);
    assert.equal(await panel.getByRole('combobox', {name: `${side}：添加成员或标签`}).count(), 0);
    assert.equal(await panel.getByRole('textbox', {name: `${side}：添加网段`}).count(), 1);
  }
  await panel.getByText('各成员自己的组网地址，以后加入的也算；不含网关后面的局域网').waitFor();
  // Back in the list: the sentence, the board legend, and the change as the controller words it.
  await page.keyboard.press('Escape');
  await panel.getByText('允许 所有成员 访问 所有成员 的 ICMP（ping）').waitFor();
  assert.equal(await panel.getByRole('button', {name: '允许所有成员互相 ping'}).isDisabled(), true);
  await panel.locator('.mesh-changes li', {hasText: '新增访问规则：所有成员 → 所有成员 · ICMP'}).waitFor();
  const layer = page.locator('.mesh-toolbar').getByRole('button', {name: '访问规则'});
  if (await layer.getAttribute('aria-pressed') === 'false') await layer.click();
  assert.deepEqual(await page.locator('.mesh-legend').getByRole('button').evaluateAll(list => list.map(b => b.getAttribute('aria-label'))), ['访问规则 1：所有成员 → 所有成员 · ping']);
  await panel.getByRole('button', {name: '编辑第 1 条规则'}).click();
  assert.equal(await title(page).innerText(), '访问规则 1');
  // Untick the source: nothing chosen covers nobody, and the editor says so; then pick one member.
  await panel.getByRole('checkbox', {name: '来源：所有成员'}).uncheck();
  await h.saved(d => d.rules[0].src.all === false && d.rules[0].dst.all === true, 'source not all');
  await panel.getByText('还没有选，这条规则不会生效').waitFor();
  await panel.getByRole('combobox', {name: '来源：添加成员或标签'}).selectOption({label: 'Home'});
  await h.saved(d => d.rules[0].src.members.join() === 'home' && d.rules[0].dst.all, 'source home');
  assert.equal(await panel.getByText('还没有选，这条规则不会生效').count(), 0);
  await label(page, 'ping').count().then(n => assert.equal(n, 0, 'one member to all members stays in the legend'));
  assert.deepEqual(await page.locator('.mesh-legend').getByRole('button').evaluateAll(list => list.map(b => b.getAttribute('aria-label'))), ['访问规则 1：Home → 所有成员 · ping']);
  // Ticking all members again drops the chosen members.
  await panel.getByRole('checkbox', {name: '来源：所有成员'}).check();
  await h.saved(d => d.rules[0].src.all && !d.rules[0].src.members.length, 'source all again');
  await h.close();
  // Phone, dark: the same editor in the bottom sheet, wide enough, nothing sideways.
  for (const dark of [true, false]) {
    const p = await setup(t, {width: 390, height: 844, dark});
    await p.page.locator('.mesh-dock').getByRole('button', {name: '网格设置与访问规则'}).tap();
    const overview = p.page.locator('.mesh-overview-sheet');
    await overview.getByRole('button', {name: '允许所有成员互相 ping'}).tap();
    await p.saved(d => d.rules.length === 1 && d.rules[0].src.all, 'phone ping for all');
    const box = p.page.getByRole('checkbox', {name: '目标：所有成员'});
    await box.waitFor();
    const sheet = p.page.locator('.mesh-sheet', {has: box});
    assert.equal(await box.isChecked(), true);
    await p.close();
  }
});

test('events: repeats with when they began, a relay\'s flapping route as one row, raw tunnel errors in Chinese with the original aside; desktop and phone', async t => {
  const h = await setup(t, {noisy: true});
  const {page} = h;
  const texts = async locator => (await locator.allInnerTexts()).map(x => x.replace(/\s+/g, ' ').trim());
  const panel = inspector(page);
  await node(page, 'home').click();
  await panel.getByRole('tab', {name: '事件'}).click();
  const rows = panel.locator('.mesh-events > li');
  await rows.first().waitFor();
  let list = await texts(rows);
  // The tunnel error in Chinese, with the server's name; the original is folded under "原文".
  const tunnel = rows.filter({hasText: '隧道报错'});
  assert.match(await tunnel.locator('.mesh-event-detail').innerText(), /^向 Berlin 发起握手失败：地址 192\.0\.2\.203 被写成了 IPv6 形式/);
  assert.equal(await tunnel.locator('.mesh-event-raw > code').isVisible(), false);
  await tunnel.locator('.mesh-event-raw > summary').click();
  assert.match(await tunnel.locator('.mesh-event-raw > code').innerText(), /non-IPv4 address/);
  // A repeat merged by the controller: how many times and since when.
  assert.match(await rows.filter({hasText: '线路不通'}).innerText(), /第 1 个入口（Berlin · UDP）：探测超时\s*重复 12 次，最早 3 小时前/);
  // The standby's trial and the relay's confirmation are routine: folded, counted with their repeats.
  assert.ok(!list.some(e => /线路可用|接入确认/.test(e)), JSON.stringify(list));
  const note = panel.locator('.mesh-routine-note');
  assert.match(await note.innerText(), /已隐藏 38 条例行记录/);
  await note.getByRole('button', {name: '显示'}).click();
  assert.match(await rows.filter({hasText: '线路可用'}).innerText(), /重复 37 次，最早 2 小时前/);
  // Ids, keys and sequence numbers stay out of what is read; only the opened original has them.
  for (const e of await texts(rows.locator('.mesh-event-source, .sv-log-source, .mesh-event-detail, .mesh-event-repeat'))) assert.doesNotMatch(e, /\bc1\b|seq=|peer\(|E0pT|[0-9a-f]{8}-[0-9a-f]{4}/, e);
  // On a relay: four flips to Berlin read as one row; it opens to the changes.
  await node(page, 'lax').click();
  await panel.getByRole('tab', {name: '事件'}).click();
  const run = rows.filter({hasText: '换路'});
  await run.waitFor();
  assert.equal(await run.count(), 1);
  assert.match(await run.locator('.mesh-event-detail').innerText(), /^到 Berlin：直达 ⇄ 经 Home 来回 4 次，现在直达$/);
  assert.match(await run.locator('.mesh-event-more').innerText(), /^最早 4 分钟前/);
  await run.getByRole('button', {name: '展开 4 条'}).click();
  assert.equal(await run.locator('.mesh-event-items > li').count(), 4);
  assert.match(await run.locator('.mesh-event-items > li').first().innerText(), /^到 Berlin：经 Home → 直达（新线路更快）/);
  await rows.filter({hasText: '网段没走组网'}).filter({hasText: '网关网段 10.10.10.0/24 和本机所在的网络重叠，本机不经组网访问它们'}).waitFor();
  await run.getByRole('button', {name: '收起'}).click();
  assert.equal(await run.locator('.mesh-event-items').count(), 0);
  await h.close();
  // Phone, dark and light: the events tab in the bottom sheet; the original and the run open by tap.
  for (const dark of [true, false]) {
    const p = await setup(t, {width: 390, height: 844, dark, noisy: true});
    await node(p.page, 'home').tap();
    const sheet = p.page.locator('.mesh-inspect-sheet');
    await sheet.getByRole('tab', {name: '事件'}).tap();
    const raw = sheet.locator('.mesh-events > li', {hasText: '隧道报错'}).locator('.mesh-event-raw');
    await raw.locator('summary').tap();
    await raw.locator('code').waitFor();
    await sheet.getByRole('button', {name: '关闭'}).tap();
    await sheet.waitFor({state: 'hidden'});
    await p.page.locator('.mesh-dock').getByRole('button', {name: '取消选择'}).tap();
    await node(p.page, 'lax').tap();
    await sheet.getByRole('tab', {name: '事件'}).tap();
    const run = sheet.locator('.mesh-events > li', {hasText: '换路'});
    await run.getByRole('button', {name: '展开 4 条'}).tap();
    assert.equal(await run.locator('.mesh-event-items > li').count(), 4);
    await p.close();
  }
});

test('security: a member’s end-to-end sessions, rejected signatures and latest security event as status rows; phone light and dark', async t => {
  const rowsOf = scope => label => scope.locator('.mesh-stats > div', {has: scope.page().locator('dt', {hasText: new RegExp(`^${label}$`)})});
  const check = async row => {
    assert.match(await row('端到端会话').innerText(), /64 \/ 64\s*丢弃 7 个包：会话已满 4、无法解密 2、未加密 1/);
    assert.match(await row('验签拒绝').innerText(), /5 次\s*来自 Berlin 5 次/);
    // The newest of them, in Chinese, with what to do and when.
    assert.match(await row('最近安全事件').innerText(), /入口疑似丢包\s*入口 LAX 回应探测，但经它发往 Berlin 的端到端探测连续 3 次没有回应，可能只丢业务流量。固定入口模式只提醒、不切换；请检查这台中转。（刚刚）/);
    assert.equal(await row('端到端会话').locator('dd > span').first().getAttribute('class'), 'ob-warn');
  };
  const h = await setup(t, {secure: true});
  await node(h.page, 'home').click();
  await inspector(h.page).locator('.mesh-stats dt', {hasText: '端到端会话'}).waitFor();
  await check(rowsOf(inspector(h.page)));
  // The events tab reads them in Chinese too.
  await inspector(h.page).getByRole('tab', {name: '事件'}).click();
  assert.match(await inspector(h.page).locator('.mesh-events > li', {hasText: '签名校验失败'}).innerText(), /Berlin 转来的以 LAX 名义的打洞地址签名不对，已丢弃/);
  await inspector(h.page).getByRole('tab', {name: '状态'}).click();
  await h.close();
  // A member whose node reports none of it: no such rows.
  const plain = await setup(t);
  await node(plain.page, 'home').click();
  await inspector(plain.page).locator('.mesh-stats dt', {hasText: 'CPU'}).waitFor();
  assert.equal(await inspector(plain.page).locator('.mesh-stats dt', {hasText: /端到端会话|验签拒绝|最近安全事件/}).count(), 0);
  await plain.close();
  for (const dark of [true, false]) {
    const p = await setup(t, {width: 390, height: 844, dark, secure: true});
    await node(p.page, 'home').tap();
    const sheet = p.page.locator('.mesh-inspect-sheet');
    await sheet.locator('.mesh-stats dt', {hasText: '最近安全事件'}).waitFor();
    await check(rowsOf(sheet));
    await sheet.locator('.mesh-stats').scrollIntoViewIfNeeded();
    // Sized as the rows above them: values at least 12px, labels and notes as theirs.
    // The warning reads against the sheet in either theme.
    await p.close();
  }
});

test('instances on a mesh address and host firewalls: read-only automatic rules after the user\'s; a Linux member\'s firewalls and what they may not let through; phone light and dark', async t => {
  const h = await setup(t, {firewall: true});
  const panel = inspector(h.page);
  const auto = panel.getByRole('list', {name: '自动放行'});
  await auto.waitFor();
  assert.deepEqual(await auto.locator('li').allInnerTexts(), ['允许 Berlin 访问 Home 的 TCP 18970\n实例 Edge-Fra\n自动']);
  // Read-only: no editing, no deleting, not dragged; the user's rules and their buttons as before.
  assert.equal(await auto.getByRole('button').count(), 0);
  assert.equal(await auto.locator('.so-grip').count(), 0);
  await panel.getByText('还没有规则：除了下面的自动放行，成员之间互相不能访问。').waitFor();
  await panel.getByRole('button', {name: '允许所有成员互相 ping'}).click();
  await h.saved(d => d.rules.length === 1, 'a rule of the user\'s');
  await h.page.keyboard.press('Escape');
  await panel.getByRole('button', {name: '编辑第 1 条规则'}).waitFor();
  assert.equal(await auto.locator('li').count(), 1);
  // The automatic row lines up with the user's rule text.
  // The Linux member: its firewalls as a status row, one hint per thing they may not let through.
  await node(h.page, 'home').click();
  const row = panel.locator('.mesh-stats > div', {has: h.page.locator('dt', {hasText: /^主机防火墙$/})});
  await row.waitFor();
  assert.match(await row.innerText(), /ufw · Docker · firewalld/);
  const hints = panel.locator('.mesh-hints summary');
  assert.deepEqual((await hints.allInnerTexts()).filter(x => !/内存|路由/.test(x)), ['组网网卡在 firewalld 的 work 区域', '没能放行发往容器的组网连接']);
  await hints.filter({hasText: 'work 区域'}).click();
  await panel.getByText(/obmesh0 是在这台机器上手动指定到 work 区域的，组网没有改动它/).waitFor();
  assert.equal(await panel.getByText('macOS 防火墙已开启').count(), 0, 'no macOS hint for a Linux member');
  await h.close();
  for (const dark of [true, false]) {
    const p = await setup(t, {width: 390, height: 844, dark, firewall: true});
    await p.page.locator('.mesh-dock').getByRole('button', {name: '网格设置与访问规则'}).tap();
    const overview = p.page.locator('.mesh-overview-sheet');
    const list = overview.getByRole('list', {name: '自动放行'});
    await list.waitFor();
    await list.scrollIntoViewIfNeeded();
    await p.close();
    const q = await setup(t, {width: 390, height: 844, dark, firewall: true});
    await node(q.page, 'home').tap();
    const sheet = q.page.locator('.mesh-inspect-sheet');
    await sheet.locator('.mesh-stats dt', {hasText: '主机防火墙'}).waitFor();
    await sheet.locator('.mesh-hints summary', {hasText: 'work 区域'}).tap();
    await sheet.getByText(/obmesh0 是在这台机器上手动指定到 work 区域的/).waitFor();
    await q.close();
  }
});

test('apply: progress on the nodes, a failure counted in the summary and on the node, rollback', async t => {
  const h = await setup(t, {failing: 'lax'});
  const {page} = h;
  await node(page, 'berlin').click();
  await inspector(page).getByRole('tab', {name: '配置'}).click();
  await inspector(page).getByRole('checkbox', {name: '监听 TCP'}).check();
  await h.saved(d => memberOf(d, 'berlin').listen.tcp === 52000, 'listen');
  const bar = page.locator('.mesh-stage .mesh-bar');
  await bar.getByRole('button', {name: '应用'}).click();
  await node(page, 'berlin').locator('.mesh-node-badge', {hasText: /下发中|已应用/}).waitFor();
  await bar.locator('strong', {hasText: /正在应用/}).waitFor();
  await node(page, 'lax').locator('.mesh-node-badge[data-tone=bad]', {hasText: '失败'}).waitFor({timeout: 15000});
  assert.equal(await node(page, 'lax').locator('.mesh-node-badge').getAttribute('title'), '失败：节点拒绝了配置：self.address outside network.prefix');
  await bar.locator('strong', {hasText: '1 台失败'}).waitFor({timeout: 15000});
  assert.match(await page.locator('.mesh-summary').innerText(), /1 台失败/);
  assert.equal(await page.locator('.mesh-summary .ob-dot').getAttribute('data-tone'), 'bad');
  assert.equal(await node(page, 'berlin').locator('.mesh-node-badge').count(), 0, 'applied nodes go back to plain');
  assert.equal(await bar.getByRole('button', {name: '重试'}).isDisabled(), false);
  await bar.getByRole('button', {name: '回退到上一版'}).click();
  await page.locator('.el-message-box').getByRole('button', {name: '回退'}).click();
  await page.waitForFunction(() => !document.querySelector('.mesh-stage .mesh-bar'), null, {timeout: 20000});
  assert.ok(h.calls.some(c => c.path === '/api/servers/mesh/rollback'));
  assert.match(await page.locator('.mesh-summary').innerText(), /全部就绪/);
  await h.close();
});

test('missing mesh service: the state on the board, the bar and the panel; reinstall asks, runs, says why it could not; skipped by a rollout; phone light and dark', async t => {
  const h = await setup(t, {gone: 'home'});
  const {page, calls} = h;
  const panel = inspector(page);
  const reinstalls = () => calls.filter(c => c.method === 'POST' && c.path.endsWith('/reinstall'));
  // The board: the node and its badge in red, the summary counts it, the bar names it.
  assert.equal(await node(page, 'home').locator('.mesh-node').getAttribute('data-tone'), 'bad');
  const badge = node(page, 'home').locator('.mesh-node-badge');
  assert.equal(await badge.innerText(), '组网服务不见了');
  assert.equal(await badge.getAttribute('data-tone'), 'bad');
  assert.equal(await badge.getAttribute('title'), '组网服务不见了（可能被卸载或重装过）');
  assert.match(await page.locator('.mesh-summary').innerText(), /1 台组网服务不见了/);
  assert.equal(await page.locator('.mesh-summary .ob-dot').getAttribute('data-tone'), 'bad');
  const bar = page.locator('.mesh-stage .mesh-bar[data-kind=missing]');
  assert.equal(await bar.getAttribute('data-tone'), 'bad');
  assert.match(await bar.innerText(), /1 台的组网服务不见了\s*Home：下发时跳过，重新安装组网服务后恢复/);
  // An entry through a relay that lost it says so (Berlin here, for the check).
  await bar.getByRole('button', {name: '查看'}).click();
  assert.equal(await title(page).innerText(), 'Home');
  assert.match(await panel.locator('.mesh-member-summary').innerText(), /组网服务不见了/);
  const notice = panel.locator('.mesh-missing');
  assert.match(await notice.innerText(), /^组网服务不见了（可能被卸载或重装过）。组网暂停给它下发配置，其他成员也连不上它。/);
  const button = notice.getByRole('button', {name: '重新安装组网服务'});
  // Asks first; cancelling sends nothing.
  await button.click();
  const box = page.locator('.el-message-box');
  await box.waitFor();
  assert.match(await box.innerText(), /重新安装组网服务[\s\S]*会在「Home」上重新安装组网服务，并签发一个新版本让其他成员更新它的密钥；你的草稿不受影响。/);
  await box.getByRole('button', {name: '取消'}).click();
  await box.waitFor({state: 'hidden'});
  assert.equal(reinstalls().length, 0);
  // Refused: the reason in Chinese under the button; the member stays missing.
  for (const [code, text] of [['node_offline', '节点离线，连上后才能重新安装'], ['capability_disabled', '这台服务器的节点关闭了命令执行，没法远程安装组网服务'],
    ['node_update_required', '节点版本过旧，先更新节点再重新安装'], ['mesh_apply_running', '正在应用别的更改，等它完成后再重新安装'], ['mesh_install_invalid', '节点返回的组网密钥不正确，安装没有完成']]) {
    h.mesh.setReinstall(code);
    await button.click();
    await box.getByRole('button', {name: '重新安装'}).click();
    await notice.locator('.mesh-missing-error', {hasText: text}).waitFor();
    assert.equal(await notice.locator('.mesh-missing-error').innerText(), `没有重新安装：${text}`);
  }
  assert.equal(reinstalls().length, 5);
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
  assert.equal(await running.isDisabled(), true);
  assert.match(await notice.innerText(), /正在机器上安装，最多约 3 分钟/);
  assert.equal(await notice.locator('.mesh-missing-error').count(), 0, 'the earlier reason goes once it is tried again');
  await page.locator('.el-message', {hasText: '已在「Home」上重新安装组网服务，正在把新密钥下发给其他成员'}).waitFor({timeout: 10000});
  await notice.waitFor({state: 'detached', timeout: 10000});
  await page.waitForFunction(() => !document.querySelector('.mesh-stage .mesh-bar[data-kind=running]'), null, {timeout: 20000});
  assert.match(await page.locator('.mesh-stage .mesh-bar').innerText(), /有 1 项更改未应用\s*修改网格设置/);
  assert.equal(h.mesh.doc.settings.mtu, 1380);
  assert.equal(h.puts().length, drafts, 'the draft is not written');
  assert.ok(!calls.some(c => /\/(discard|apply)$/.test(c.path)));
  // Its events: the controller's two, in Chinese.
  await panel.getByRole('tab', {name: '事件'}).click();
  const rows = panel.locator('.mesh-events > li');
  const gone = rows.filter({has: page.locator('.sv-log-source', {hasText: /^组网服务不见了$/})});
  await gone.waitFor();
  assert.equal(await gone.getAttribute('data-tone'), 'bad');
  assert.match(await gone.locator('.mesh-event-detail').innerText(), /^可能被卸载或重装过，暂停给它下发配置；点「重新安装组网服务」恢复$/);
  assert.match(await rows.filter({has: page.locator('.sv-log-source', {hasText: /^重新安装组网服务$/})}).locator('.mesh-event-detail').innerText(), /^已经装回，其他成员会收到它的新密钥$/);
  await h.close();

  // A rollout skips it: "组网服务不见了，已跳过", not a failure; the toast says so.
  const r = await setup(t, {gone: 'home'});
  r.mesh.doc.settings.mtu = 1380;
  const rbar = r.page.locator('.mesh-stage .mesh-bar');
  await rbar.getByRole('button', {name: '应用'}).click({timeout: 10000});
  await rbar.locator('strong', {hasText: /正在应用/}).waitFor();
  await r.page.locator('.el-message', {hasText: /已应用到 2 台；1 台的组网服务不见了，已跳过/}).waitFor({timeout: 20000});
  assert.equal(await r.page.locator('.mesh-stage .mesh-bar').getAttribute('data-kind'), 'missing');
  assert.doesNotMatch(await r.page.locator('.mesh-summary').innerText(), /失败/);
  // Nothing selected: the panel lists it.
  assert.match(await r.page.locator('.mesh-changes').innerText(), /1 台的组网服务不见了\s*Home：组网服务不见了，已跳过/);
  await r.close();

  // An entry through a relay whose service is gone.
  const e = await setup(t, {gone: 'berlin'});
  await label(e.page, '1 · UDP').click();
  await inspector(e.page).locator('.mesh-link-text', {hasText: 'Berlin 的组网服务不见了'}).waitFor();
  assert.equal(await label(e.page, '1 · UDP').getAttribute('data-look'), 'bad');
  await e.close();

  // Phone, dark and light: the bottom sheet's notice, the button within reach, nothing wider than the screen.
  for (const dark of [true, false]) {
    const p = await setup(t, {width: 390, height: 844, dark, gone: 'home'});
    assert.equal(await node(p.page, 'home').locator('.mesh-node-badge').innerText(), '组网服务不见了');
    await node(p.page, 'home').tap();
    const sheet = p.page.locator('.mesh-inspect-sheet');
    const n = sheet.locator('.mesh-missing');
    await n.waitFor();
    const b = n.getByRole('button', {name: '重新安装组网服务'});
    p.mesh.setReinstall('node_offline');
    await b.tap();
    await p.page.locator('.el-message-box').getByRole('button', {name: '重新安装'}).tap();
    await n.locator('.mesh-missing-error', {hasText: '节点离线，连上后才能重新安装'}).waitFor();
    await p.close();
  }
});

test('a poll answered before a save but arriving after it leaves the edit alone; the next save keeps both', async t => {
  const h = await setup(t);
  const {page} = h;
  await node(page, 'home').click();
  const panel = inspector(page);
  await panel.getByRole('tab', {name: '配置'}).click();
  const rate = panel.getByRole('spinbutton', {name: '发送速率'});
  await rate.waitFor();
  // The next poll is slow: it reads the draft before the edit below and lands after its save.
  h.slowGets(2500);
  const isPoll = r => r.request().method() === 'GET' && new URL(r.url()).pathname === '/api/servers/mesh';
  await page.waitForRequest(r => r.method() === 'GET' && new URL(r.url()).pathname === '/api/servers/mesh', {timeout: 9000});
  const late = page.waitForResponse(isPoll, {timeout: 9000});
  await rate.fill('90');
  await rate.press('Tab');
  await h.saved(d => memberOf(d, 'home').uploadLimitMbps === 90, 'upload limit');
  await late;
  h.slowGets(0);
  assert.equal(await rate.inputValue(), '90', 'the late poll did not put the old value back');
  assert.equal(await page.locator('.mesh-stage .mesh-bar strong').innerText(), '有 1 项更改未应用');
  // Another edit: its save carries the first one too.
  await panel.getByRole('tab', {name: '入口'}).click();
  await panel.getByRole('radio', {name: '固定'}).check();
  const body = await h.saved(d => memberOf(d, 'home').entry.mode === 'fixed', 'entry mode');
  assert.equal(memberOf(body, 'home').uploadLimitMbps, 90, 'the second save keeps the first edit');
  assert.equal(h.mesh.doc.members.find(m => m.serverId === 'home').uploadLimitMbps, 90);
  await h.close();
});

test('leaving the mesh tab or the servers page stops every reading; coming back reads again at once', async t => {
  const meshCalls = (h, since) => h.calls.filter(c => c.at >= since && c.path.startsWith('/api/servers/mesh')).map(c => `${c.method} ${c.path.slice('/api/servers/mesh'.length) || '/'}${c.query}`);
  const settle = async (page, ms) => { await page.waitForTimeout(ms); };
  // The mesh tab left with a member selected: its detail (every 5 s), routes (10 s) and trend stop with the board's own poll.
  let h = await setup(t, {routes: true});
  let {page} = h;
  await node(page, 'mac').click();
  await inspector(page).locator('.mesh-sparks > li').first().waitFor();
  await inspector(page).locator('.mesh-routes').waitFor();
  await settle(page, 300);
  await page.locator('.sv-tabs [role=tab]', {hasText: '探针'}).click();
  let since = Date.now();
  await settle(page, 11000);
  assert.deepEqual(meshCalls(h, since), [], 'nothing asked while the mesh tab is not shown');
  since = Date.now();
  await page.locator('.sv-tabs [role=tab]', {hasText: '组网'}).click();
  await settle(page, 1500);
  let back = meshCalls(h, since);
  for (const want of ['GET /', 'GET /members/mac', 'GET /routes?src=mac', 'GET /members/mac/trend?range=1h']) assert.ok(back.includes(want), `${want} at once on return: ${back}`);
  await h.close();

  // The whole servers page put away (another page of the app): the same, a link's line record included.
  h = await setup(t, {lines: true, keepAlive: true});
  ({page} = h);
  await node(page, 'home').click();
  await inspector(page).locator('.mesh-sparks > li').first().waitFor();
  await settle(page, 300);
  await page.evaluate(() => window.__showServers(false));
  await page.locator('#other-page').waitFor();
  since = Date.now();
  await settle(page, 11000);
  assert.deepEqual(meshCalls(h, since), [], 'nothing asked while the servers page is put away');
  since = Date.now();
  await page.evaluate(() => window.__showServers(true));
  await settle(page, 1500);
  back = meshCalls(h, since);
  for (const want of ['GET /', 'GET /members/home', 'GET /members/home/trend?range=1h']) assert.ok(back.includes(want), `${want} at once on return: ${back}`);
  // A line's record: read when the link is selected, again at once when the page comes back.
  await label(page, '2 · TCP').click();
  await inspector(page).locator('.mesh-line-ranges').waitFor();
  await settle(page, 300);
  const lineTrend = 'GET /lines/entry%3Ahome%7Clax%7Ctcp/trend?range=24h';
  await page.evaluate(() => window.__showServers(false));
  since = Date.now();
  await settle(page, 6000);
  assert.deepEqual(meshCalls(h, since), []);
  since = Date.now();
  await page.evaluate(() => window.__showServers(true));
  await settle(page, 1500);
  back = meshCalls(h, since);
  assert.ok(back.includes(lineTrend) && back.includes('GET /'), `the line's record and the mesh at once on return: ${back}`);
  await h.close();
});
