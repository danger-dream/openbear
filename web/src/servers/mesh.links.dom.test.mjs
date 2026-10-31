// The mesh board (servers page, 组网 tab) without a browser: what it asks the controller, what it
// shows and how it switches, in happy-dom with the browser tests' fake controller
// (testing/meshBackend.mjs, mounted by testing/meshDom.mjs). Long timers run 10 times faster
// (scaleTimers), so "no request within 6 s" waits 0.6 s. The layout of the same scenarios stays in
// mesh.*.browser.test.mjs.
import test from 'node:test';
import assert from 'node:assert/strict';
import {setup, node, label, inspector, title, memberOf, dimmed, texts, until} from './testing/meshDom.mjs';
import {GB, server, member} from './testing/meshBackend.mjs';

test('punched direct links: live state on the board and in the panel, no outside address needed, the new checks in place', async t => {
  const h = await setup(t, {punch: true});
  const {page} = h;
  const direct = (state) => page.locator(`.mesh-edge[data-kind=direct][data-live=${state}]`);
  // Three links, three states; Office–Cafe takes the worse end (Cafe is still punching while Office reports direct).
  await label(page, '直连 · 局域网 · 3.2 ms').waitFor();
  assert.equal(await label(page, '直连 · 局域网 · 3.2 ms').getAttribute('data-live'), 'direct');
  assert.equal(await label(page, '打洞中').getAttribute('data-live'), 'punching');
  assert.equal(await label(page, '走中转').getAttribute('data-live'), 'relayed');
  for (const state of ['direct', 'punching', 'relayed']) assert.equal(await direct(state).count(), 1, state);
  // The state comes with GET /mesh: no member detail is read while nothing is selected.
  const details = () => h.calls.filter(c => c.method === 'GET' && /^\/api\/servers\/mesh\/members\/[\w-]+$/.test(c.path)).map(c => c.path.split('/').pop());
  await page.waitForTimeout(1200);
  assert.deepEqual(details(), []);
  // Hovering says why, as far as the summary knows.
  assert.equal(await label(page, '走中转').getAttribute('title'), '暂时没打通，先走中转，会定时重试');
  assert.equal(await label(page, '打洞中').getAttribute('title'), '正在打洞');

  // Selected relayed link: the label says why, the panel shows the live state and no dialer.
  await label(page, '走中转').click();
  assert.equal(await title(page).innerText(), '直连');
  // Selected: both ends' details are read for the reason, attempts and how long.
  await label(page, '走中转 · 打洞未成功，会定时重试').waitFor();
  assert.deepEqual([...new Set(details())].sort(), ['cafe', 'home']);
  const panel = inspector(page);
  await panel.getByText('自动打洞：两边都在防火墙后也能直连，打不通时走中转').waitFor();
  assert.equal(await panel.getByText('主动方').count(), 0);
  assert.equal(await panel.getByText(/对外地址/).count(), 0);
  const facts = await panel.locator('.mesh-direct-live .mesh-stats').innerText();
  for (const want of [/状态\s*走中转/, /经由\s*中转/, /持续\s*5 分钟/, /尝试次数\s*4 次/, /最近错误\s*打洞超时/]) assert.match(facts, want);
  await panel.getByText('打洞未成功，会定时重试。').waitFor();
  // The two ends disagree: the worse is shown, and both are named.
  await label(page, '打洞中').click();
  await label(page, '打洞中 · 第 3 次尝试').waitFor();
  assert.match(await panel.locator('.mesh-direct-live .mesh-stats').innerText(), /状态\s*正在打洞[\s\S]*尝试次数\s*3 次[\s\S]*最近错误\s*打洞超时/);
  await panel.getByText('正在打洞，第 3 次尝试。两端不一致，按较差的一端显示：Office 直连中，Cafe 正在打洞。').waitFor();
  await label(page, '直连 · 局域网 · 3.2 ms').click();
  await page.waitForFunction(() => /持续/.test(document.querySelector('.mesh-direct-live .mesh-stats')?.innerText || ''));
  assert.match(await panel.locator('.mesh-direct-live .mesh-stats').innerText(), /状态\s*直连中[\s\S]*经由\s*局域网[\s\S]*延迟\s*3.2 ms[\s\S]*持续\s*12 分钟[\s\S]*尝试次数\s*1 次[\s\S]*最近错误\s*无/);
  // Deselected: the details stop; the board keeps the summary's state.
  await page.keyboard.press('Escape');
  const read = details().length;
  await page.waitForTimeout(6000);
  assert.equal(details().length, read, 'no member detail while no link is selected');
  assert.equal(await label(page, '打洞中').getAttribute('title'), '正在打洞');

  // Peers and events of a member.
  await node(page, 'home').click();
  await panel.getByRole('tab', {name: '对端'}).click();
  await panel.locator('.mesh-peer-direct[data-state=direct]', {hasText: '直连中 · 经由局域网 · 已 12 分钟'}).waitFor();
  assert.match(await panel.locator('.mesh-peer-direct[data-state=relayed]').innerText(), /^走中转 · 已 5 分钟 · 最近错误：打洞超时$/);
  await panel.getByRole('tab', {name: '事件'}).click();
  const events = await panel.locator('.mesh-events > li').allInnerTexts();
  for (const want of [/打洞失败\s*和 Cafe 打洞没有成功：打洞超时，先走中转/, /直连断开\s*和 Cafe 的直连断开：直连线路中断，先走中转/, /直连打通\s*和 Office 直连，经由局域网/])
    assert.ok(events.some(e => want.test(e)), `${want} in ${JSON.stringify(events)}`);
  await page.keyboard.press('Escape');

  // A new direct link between two members without an outside address: UDP, no complaint; NAS gets UDP listening.
  await h.close();

  // An older controller without the summary: the board reads the ends' details itself.
  const legacy = await setup(t, {punch: true, legacyDirect: true});
  await label(legacy.page, '走中转').waitFor();
  assert.equal(await label(legacy.page, '走中转').getAttribute('title'), '打洞未成功，会定时重试');
  assert.equal(await label(legacy.page, '打洞中').getAttribute('title'), '正在打洞，第 3 次尝试');
  await label(legacy.page, '直连 · 局域网 · 3.2 ms').waitFor();
  assert.deepEqual([...new Set(legacy.calls.filter(c => /^\/api\/servers\/mesh\/members\/[\w-]+$/.test(c.path)).map(c => c.path.split('/').pop()))].sort(), ['cafe', 'home', 'office']);
  await legacy.close();

  // Phone, dark: the same three states.
  const phone = await setup(t, {punch: true, width: 390, height: 844, dark: true});
  await label(phone.page, '直连 · 局域网 · 3.2 ms').waitFor();
  await label(phone.page, '打洞中').waitFor();
  await label(phone.page, '走中转').waitFor();
  // Home and Office sit side by side: their link bows out so the label covers neither node.
  await phone.close();
});

test('site relays and relay links: relay mode and its recommendation, punched links between site relays, metrics, preference, the new checks', async t => {
  const h = await setup(t, {reach: true});
  const {page} = h;
  const panel = inspector(page);
  // Home↔TOY punch (the worse end's measure), the others dial; LAX flaps and is held back.
  await label(page, '打洞 · 2.6 ms').waitFor();
  assert.equal(await label(page, '打洞 · 2.6 ms').getAttribute('data-live'), 'direct');
  await label(page, '拨号 · 79 ms').waitFor();
  await label(page, '拨号 · 75 ms').waitFor();
  const held = label(page, '线路不稳，暂停使用');
  assert.equal(await held.getAttribute('data-look'), 'suppressed');
  assert.equal(await held.getAttribute('title'), '最近反复断开，稳定后自动恢复');
  assert.equal(await page.locator('.mesh-edge[data-kind=relay][data-look=suppressed]').count(), 1);

  // A dialled link: how it connects, what both ends measure, the preference.
  await held.click();
  assert.equal(await title(page).innerText(), '中转互联');
  await label(page, '线路不稳，暂停使用 · 最近反复断开，稳定后自动恢复').waitFor();
  assert.match(await panel.locator('.mesh-relay-facts').innerText(), /连接方式\s*拨号：Home 主动连接 LAX/);
  const metric = await panel.locator('.mesh-relay-metric').innerText();
  for (const want of [/线路\s*线路不稳，暂停使用/, /丢包\s*30%/, /代价\s*不可用/, /两端测得不同，按较差的一端显示：Home 正常，LAX 线路不稳，暂停使用。/, /这条线路最近反复断开，先不用于选路，稳定后自动恢复。/])
    assert.match(metric, want);
  assert.doesNotMatch(metric, /延迟/);
  await panel.getByText('由 Home 主动拨号：站点中转不对外宣告地址。').waitFor();
  assert.equal(await panel.getByRole('combobox', {name: '由谁主动连接'}).count(), 0);
  const bias = panel.getByRole('spinbutton', {name: '偏好（毫秒）'});
  assert.equal(await bias.inputValue(), '0');
  await bias.fill('-20');
  await bias.press('Tab');
  let body = await h.saved(d => d.relayLinks.pairs[1].bias === -20, 'preference');
  assert.equal(body.relayLinks.pairs.length, 4);
  await until(page, async () => /偏好 -20 ms/.test(await page.locator('.mesh-edge-label[data-kind=relay][data-selected]').getAttribute('title')), 'preference in the title');
  // What is typed survives a poll; past the range it is held at 10000.
  const polls = () => h.calls.filter(c => c.method === 'GET' && c.path === '/api/servers/mesh').length;
  const seen = polls();
  await bias.fill('20000');
  await until(page, async () => polls() > seen, 'a poll while typing', 7000);
  assert.equal(await bias.inputValue(), '20000');
  await bias.press('Tab');
  await h.saved(d => d.relayLinks.pairs[1].bias === 10000, 'preference held at 10000');
  await until(page, async () => await bias.inputValue() === '10000', 'the box shows what was kept');
  const before = h.puts().length;
  await bias.fill('0');
  await bias.press('Tab');
  body = await h.saved(d => h.puts().length > before && !('bias' in d.relayLinks.pairs[1]), 'preference back to 0');

  // A punched link between two site relays: no transport or dialer to choose, the punch state and the measures.
  await label(page, '打洞 · 2.6 ms').click();
  await panel.getByText('自动打洞：两台站点中转都在 NAT 后也能直接相连，打不通时经其他中转绕行').waitFor();
  assert.match(await panel.locator('.mesh-relay-facts').innerText(), /连接方式\s*打洞：两台都是站点中转/);
  assert.match(await panel.locator('.mesh-relay-metric').innerText(), /线路\s*正常[\s\S]*延迟\s*2\.6 ms[\s\S]*抖动\s*0\.2 ms[\s\S]*丢包\s*0%[\s\S]*代价\s*3/);
  await page.waitForFunction(() => /持续/.test(document.querySelector('.mesh-direct-live .mesh-stats')?.innerText || ''));
  const live = await panel.locator('.mesh-direct-live .mesh-stats').innerText();
  for (const want of [/打洞\s*直连中/, /经由\s*公网/, /持续\s*42 分钟/, /尝试次数\s*2 次/, /最近错误\s*无/]) assert.match(live, want);
  assert.equal(await panel.getByRole('radiogroup', {name: '传输方式'}).count(), 0);
  assert.equal(await panel.getByText('主动方').count(), 0);
  await panel.getByRole('spinbutton', {name: '偏好（毫秒）'}).waitFor();
  // Both ends symmetric, but the line is already through: no prediction that it will not be.
  for (const id of ['home', 'toy']) h.mesh.nodes[id].summary.reachability = {...h.mesh.nodes[id].summary.reachability, verdict: 'symmetric'};
  // Two fresh reads of GET /mesh: the board has seen the new verdicts.
  for (let i = 0; i < 2; i++) await page.waitForResponse(r => new URL(r.url()).pathname.endsWith('/servers/mesh'), {timeout: 9000});
  assert.equal(await panel.getByText('两端都是对称型 NAT，打洞基本不会成功，会一直经中转绕行').count(), 0, 'not said of a line that is through');
  assert.notEqual(await label(page, '打洞 · 2.6 ms').getAttribute('title'), '两端都是对称型 NAT，打洞基本不会成功，会一直经中转绕行');
  assert.equal(await label(page, '打洞 · 2.6 ms').getAttribute('data-look'), null);
  for (const id of ['home', 'toy']) h.mesh.nodes[id].summary.reachability = {...h.mesh.nodes[id].summary.reachability, verdict: 'punchable'};
  await page.keyboard.press('Escape');

  // Relay mode: TOY is a site relay, as the check recommends; public makes the link to Home a dialled one.
  await node(page, 'toy').click();
  await panel.getByRole('tab', {name: '配置'}).click();
  const modes = panel.getByRole('radiogroup', {name: '中转类型'});
  assert.equal(await modes.getByRole('radio', {name: /^站点中转/}).isChecked(), true);
  assert.equal(await modes.locator('label', {hasText: '站点中转'}).locator('.pb-tag').innerText(), '推荐');
  await modes.getByRole('radio', {name: /^公网中转/}).check();
  await h.saved(d => memberOf(d, 'toy').relayMode === 'public', 'public relay');
  await panel.getByText('可达性检测结果是“可打洞”，建议设为站点中转。').waitFor();
  await label(page, '拨号 · 2.6 ms').waitFor();
  await modes.getByRole('radio', {name: /^站点中转/}).check();
  await h.saved(d => memberOf(d, 'toy').relayMode === 'site', 'site relay again');
  await label(page, '打洞 · 2.6 ms').waitFor();

  // Lab is behind NAT: made a relay it starts as a site relay with UDP, and needs a public relay to link to.
  await node(page, 'lab').click();
  await panel.getByRole('tab', {name: '配置'}).click();
  await panel.getByRole('checkbox', {name: /作为中转/}).check();
  body = await h.saved(d => memberOf(d, 'lab').role === 'relay', 'lab relay');
  assert.equal(memberOf(body, 'lab').relayMode, 'site');
  assert.equal(memberOf(body, 'lab').listen.udp, 51999);
  await panel.getByText('站点中转至少要连一台公网中转，用来交换打洞地址').waitFor();
  assert.equal(await panel.getByText('站点中转至少要连一台公网中转，用来交换打洞地址').count(), 1, 'said once');
  await node(page, 'lab').locator('.pb-tag', {hasText: '需修改'}).waitFor();
  // Two site relays may be linked: by punching.

  // An entry through a site relay needs a public one too: Win without its SG entry.
  await node(page, 'win').click();
  await panel.getByRole('tab', {name: '入口'}).click();
  await panel.getByRole('button', {name: '删除第 2 个入口'}).click();
  await h.saved(d => memberOf(d, 'win').entry.candidates.length === 1, 'win without SG');
  await panel.getByText('入口里有站点中转时，还要有一个公网中转入口：先经它连上，再和站点中转打洞').waitFor();
  assert.equal(await panel.getByText('入口里有站点中转时，还要有一个公网中转入口：先经它连上，再和站点中转打洞').count(), 1, 'said once');
  await h.close();
});

test('routes: standby entries drawn faint, LAN or internet on punched entries, the routes list and its paths, who gets a gateway network', async t => {
  const h = await setup(t, {routes: true});
  const {page} = h;
  const standby = id => page.locator(`[data-id="${id}"] .mesh-edge`).getAttribute('data-quiet');
  // Standbys carry nothing: quiet (faint, thin, finely dashed and still). The entries in use stay solid;
  // so does a standby that fails (Mac's second): a problem is never quiet.
  assert.equal(await standby('entry:win:w2'), 'true', 'entry:win:w2 is a standby');
  assert.equal(await standby('entry:mac:m2'), null, 'a failing standby stays up');
  for (const id of ['entry:mac:m1', 'entry:win:w1', 'entry:lab:l1']) {
    assert.equal(await standby(id), null, `${id} is in use`);
  }
  // Punched entries say how they got through: both office machines reach TOY over the LAN.
  assert.equal(await label(page, '1 · 局域网').count(), 2);
  assert.equal(await page.locator('.mesh-edge-label[data-kind=entry]', {hasText: '打洞'}).count(), 0);
  assert.match(await label(page, '1 · 局域网').first().getAttribute('title'), /经局域网接入/);
  assert.match(await label(page, '2 · TCP · 超时').getAttribute('aria-label'), /热备，平时不走流量/);
  // Win's standby is quiet: its label stays away until Win is selected.
  assert.equal(await label(page, '2 · TCP').count(), 0);
  await node(page, 'win').click();
  await until(page, async () => await standby('entry:win:w2') === null, "Win's standby drawn normally once Win is selected");
  assert.match(await label(page, '2 · TCP').getAttribute('aria-label'), /热备，平时不走流量/);
  // A selected member shows its standbys as normal lines; the others stay faint.
  await node(page, 'mac').click();
  await until(page, async () => await standby('entry:win:w2') === 'true', "Win's standby faint again once Mac is selected");
  assert.equal(await standby('entry:mac:m2'), null);
  // Its routes: a few rows, the rest behind a tap.
  const panel = inspector(page);
  const routes = panel.locator('.mesh-routes');
  const rows = () => routes.locator('.mesh-route').evaluateAll(list => list.map(el => [...el.children].map(c => c.textContent.trim()).filter(Boolean).join(' | ')));
  await routes.locator('.mesh-route').first().waitFor();
  assert.deepEqual(await rows(), ['SG | 经 TOY · 数据不完整 | 延迟未知', 'LAX | 经 TOY → Home | 178 ms', 'Home | 经 TOY | 6.6 ms', 'TOY | 直达 · 局域网 | 4 ms']);
  assert.equal(await routes.locator('.mesh-route-sub').count(), 0, 'Mac gets no gateway network');
  await routes.getByRole('button', {name: '展开其余 2 条'}).click();
  assert.deepEqual((await rows()).slice(4), ['Win-Box | 经 TOY | 8.1 ms', 'Lab | 在 SG 之后断开']);
  assert.equal(await routes.locator('.mesh-route[data-broken]').count(), 1);
  await routes.getByRole('button', {name: '收起'}).click();
  assert.equal(await routes.locator('.mesh-route').count(), 4);
  // A row shows its path on the board, the way path viewing does; the row stays marked.
  await routes.locator('.mesh-route', {hasText: 'LAX'}).click();
  const box = page.locator('.mesh-path');
  await box.locator('.mesh-path-hops').waitFor();
  assert.equal(await box.locator('h3').innerText(), '公司-Mac 到 LAX 的路径');
  assert.match(await box.innerText(), /公司-Mac\s*局域网 4 ms →\s*TOY\s*中转链路 2\.6 ms →\s*Home\s*中转链路 171 ms →\s*LAX\s*总延迟约 178 ms/);
  assert.deepEqual(await dimmed(page), ['lab', 'sg', 'win']);
  assert.equal(await routes.locator('.mesh-route[aria-pressed=true] .mesh-route-name').innerText(), 'LAX');
  assert.equal(await page.locator('[data-id="entry:mac:m1"] .mesh-edge').getAttribute('data-path'), 'true');
  await box.getByRole('button', {name: '关闭路径'}).click();
  // Gateway networks are listed apart: LAX reaches Home's network through Home.
  await node(page, 'lax').click();
  await routes.locator('.mesh-route-sub', {hasText: '网段'}).waitFor();
  const net = routes.locator('.mesh-route', {hasText: '10.10.10.0/24'});
  assert.match(await net.innerText(), /10\.10\.10\.0\/24\s*经 Home\s*171 ms/);
  await net.click();
  await box.locator('.mesh-path-hops').waitFor();
  assert.equal(await box.locator('h3').innerText(), 'LAX 到 10.10.10.0/24 的路径');
  assert.deepEqual(await dimmed(page), ['lab', 'mac', 'sg', 'toy', 'win']);
  await box.getByRole('button', {name: '关闭路径'}).click();
  // The gateway: each network says who gets it, by the rules in the draft.
  await node(page, 'home').click();
  const exports = panel.locator('.mesh-exports');
  assert.deepEqual(await texts(exports.locator('.mesh-export')), ['10.10.10.0/24 下发给：LAX', '192.168.50.0/24 没有机器能访问（要在访问规则的目标里写上这个网段）']);
  assert.equal(await panel.locator('.mesh-member-role').innerText(), '站点中转 · 网关');
  // Editing the rule changes it at once.
  await page.keyboard.press('Escape');
  await panel.getByRole('button', {name: '编辑第 1 条规则'}).click();
  await panel.getByRole('combobox', {name: '来源：添加成员或标签'}).selectOption('members:mac');
  const edited = await h.saved(d => d.rules[0].src.members.includes('mac'), 'rule source');
  assert.equal(memberOf(edited, 'mac').entry.candidates[0].endpoint, 'udp://10.20.20.7:51998', 'the filled-in LAN address is kept');
  await node(page, 'home').click();
  await exports.getByText('下发给：LAX、公司-Mac').waitFor();
  await page.keyboard.press('Escape');
  await panel.getByRole('button', {name: '编辑第 1 条规则'}).click();
  await panel.getByRole('button', {name: '删除规则'}).click();
  await h.saved(d => d.rules.length === 0, 'rule removed');
  await node(page, 'home').click();
  assert.deepEqual(await texts(exports.locator('small')), ['没有机器能访问（要在访问规则的目标里写上这个网段）', '没有机器能访问（要在访问规则的目标里写上这个网段）']);
  // A member only in the draft has no routes yet.
  h.mesh.doc.members.push(member('lab2', 'Lab-2', 'member', {address: '100.96.0.9', entry: {mode: 'fixed', autoReturn: true, candidates: []}}));
  await page.getByRole('button', {name: '选中 Lab-2'}).click({timeout: 8000});
  await routes.getByText('应用到网格后，这里显示它去各处实际走的路线。').waitFor();
  await h.close();

  // An older controller: no via, no exports, no routes; nothing breaks and nothing is made up.
  const old = await setup(t, {routes: true, oldRoutes: true});
  assert.equal(await label(old.page, '1 · 打洞').count(), 2);
  assert.equal(await old.page.locator('[data-id="entry:win:w2"] .mesh-edge').getAttribute('data-quiet'), 'true');
  await node(old.page, 'home').click();
  await inspector(old.page).locator('.mesh-reach').waitFor();
  assert.equal(await inspector(old.page).locator('.mesh-routes, .mesh-exports').count(), 0);
  assert.equal(await inspector(old.page).locator('.mesh-member-role').innerText(), '站点中转 · 网关');
  assert.match(await inspector(old.page).locator('.mesh-plain-nets').innerText(), /网关网段\s*10\.10\.10\.0\/24、192\.168\.50\.0\/24/);
  assert.equal(old.calls.filter(c => c.path === '/api/servers/mesh/routes').length, 1, 'not asked again');
  await old.close();
});

test('routes in dark and on phones: the list in the bottom sheet, a row by tap shows the path', async t => {
  const dark = await setup(t, {routes: true, dark: true});
  await node(dark.page, 'mac').click();
  await inspector(dark.page).locator('.mesh-routes .mesh-route', {hasText: 'LAX'}).click();
  await dark.page.locator('.mesh-path .mesh-path-hops').waitFor();
  await node(dark.page, 'home').click();
  await inspector(dark.page).locator('.mesh-exports').waitFor();
  await dark.close();

  for (const theme of ['dark', 'light']) {
    const phone = await setup(t, {routes: true, width: 390, height: 844, dark: theme === 'dark'});
    const {page} = phone;
    const sheet = page.locator('.mesh-inspect-sheet');
    await node(page, 'mac').tap();
    const routes = sheet.locator('.mesh-routes');
    await routes.locator('.mesh-route').first().waitFor();
    await routes.scrollIntoViewIfNeeded();
    // Collapsed by default; rows and the toggle are easy to tap; nothing scrolls sideways.
    assert.equal(await routes.locator('.mesh-route').count(), 4);
    await routes.getByRole('button', {name: '展开其余 2 条'}).tap();
    await routes.locator('.mesh-route', {hasText: 'Lab'}).scrollIntoViewIfNeeded();
    // A row by tap: the sheet steps aside and the board shows the path.
    await routes.locator('.mesh-route', {hasText: 'LAX'}).tap();
    await sheet.waitFor({state: 'hidden'});
    const box = page.locator('.mesh-path');
    await box.locator('.mesh-path-hops').waitFor();
    assert.deepEqual(await dimmed(page), ['lab', 'sg', 'win']);
    await box.getByRole('button', {name: '关闭路径'}).tap();
    await box.waitFor({state: 'detached'});
    // The gateway's networks in the sheet, long lines wrapped.
    await node(page, 'home').tap();
    const exports = sheet.locator('.mesh-exports');
    await exports.waitFor();
    await exports.scrollIntoViewIfNeeded();
    await phone.close();
  }
});

test('dark desktop and phone: node alert, relay link and path, in a bottom sheet and by taps on the phone', async t => {
  const dark = await setup(t, {reach: true, dark: true});
  let {page} = dark;
  await node(page, 'mac').click();
  await inspector(page).locator('.mesh-hints summary', {hasText: '备用入口不可用'}).click();
  await inspector(page).getByText('LAX · TCP：连不上：超时 · 从没连上过').waitFor();
  await label(page, '线路不稳，暂停使用').click();
  await inspector(page).locator('.mesh-relay-metric').waitFor();
  await node(page, 'home').click();
  await inspector(page).getByRole('button', {name: '查看到…的路径'}).click();
  await node(page, 'mac').click();
  await page.locator('.mesh-path-hops').waitFor();
  await dark.close();

  const phone = await setup(t, {reach: true, width: 390, height: 844, dark: true});
  ({page} = phone);
  // Badges stay readable and the taller nodes do not run into each other.
  // Every link label can be tapped: none covers another or a node.
  // Mac's alert in the bottom sheet; the failing candidate is named, long lines wrap.
  await node(page, 'mac').tap();
  const sheet = page.locator('.mesh-inspect-sheet');
  await sheet.locator('.mesh-hints summary', {hasText: '备用入口不可用：LAX 备用：超时'}).tap();
  await sheet.getByText('LAX · TCP：连不上：超时 · 从没连上过').waitFor();
  await sheet.getByRole('button', {name: '关闭'}).tap();
  await sheet.waitFor({state: 'hidden'});
  // With Mac selected only its lines are up: the selection cleared, every line again.
  await page.locator('.mesh-dock').getByRole('button', {name: '取消选择'}).tap();
  // A relay link by tapping its label; the preference input does not zoom the page.
  await label(page, '线路不稳，暂停使用').tap();
  await sheet.locator('.mesh-sheet-head h2', {hasText: '中转互联'}).waitFor();
  await sheet.getByText('两端测得不同，按较差的一端显示：Home 正常，LAX 线路不稳，暂停使用。').waitFor();
  await sheet.getByRole('button', {name: '关闭'}).tap();
  await sheet.waitFor({state: 'hidden'});
  // The path by taps: select Home, "路径…" in the dock, tap Mac.
  await node(page, 'home').tap();
  await sheet.locator('.mesh-sheet-head h2', {hasText: 'Home'}).waitFor();
  await sheet.getByRole('button', {name: '关闭'}).tap();
  await sheet.waitFor({state: 'hidden'});
  await page.locator('.mesh-dock').getByRole('button', {name: '路径…'}).tap();
  await page.locator('.mesh-banner', {hasText: '看 Home 怎么到它'}).waitFor();
  await node(page, 'mac').tap();
  const box = page.locator('.mesh-path');
  await box.locator('.mesh-path-hops').waitFor();
  assert.deepEqual(await dimmed(page), ['lab', 'lax', 'sg', 'win']);
  await box.getByRole('button', {name: '关闭路径'}).tap();
  await box.waitFor({state: 'detached'});
  await phone.close();
});

test('phone: add from the + list, tap to select, connect to…, bottom sheet closes by button and by drag', async t => {
  const h = await setup(t, {width: 390, height: 844, dark: true});
  const {page} = h;
  assert.equal(await page.locator('.mesh-palette, .mesh-inspector-col').count(), 0);
  const tools = page.locator('.mesh-fab-row');
  // Readable from the start: zoom at least 0.85, laid out top-down within the width, text 12px or more.
  // + opens the server list; one tap adds, placed automatically in the members' row.
  await page.locator('.mesh-board').getByRole('button', {name: '添加服务器'}).tap();
  const add = page.locator('.mesh-add-sheet');
  await add.getByRole('button', {name: '公司-Mac，加入组网'}).waitFor();
  await add.getByRole('button', {name: '公司-Mac，加入组网'}).tap();
  await add.waitFor({state: 'hidden'});
  await node(page, 'mac').locator('.mesh-node[data-installing]').waitFor();
  await node(page, 'mac').locator('.mesh-node:not([data-installing])').waitFor({timeout: 8000});
  // Placed in the phone's own layout, next to Home; the desktop layout is left alone.
  assert.equal(h.layoutPuts().length, 0);
  // Tap a node: its properties open in the bottom sheet; inputs are 16px.
  // The new server is selected after joining; clear it from the dock.
  await page.locator('.mesh-dock').getByRole('button', {name: '取消选择'}).tap();
  await node(page, 'home').tap();
  const sheet = page.locator('.mesh-inspect-sheet');
  await sheet.locator('.mesh-sheet-head h2', {hasText: 'Home'}).waitFor();
  await sheet.getByRole('tab', {name: '配置'}).tap();
  // A setting's explanation opens in a tooltip by a tap on ⓘ, and closes by another; the names stay on one line.
  await sheet.getByRole('button', {name: '发送速率：说明'}).tap();
  await page.getByRole('tooltip').filter({hasText: /建议按这条线路上行带宽的九成设置/}).waitFor();
  await sheet.getByRole('button', {name: '发送速率：说明'}).tap();
  await page.getByRole('tooltip').filter({hasText: /建议按这条线路上行带宽的九成设置/}).waitFor({state: 'hidden'});
  // Entries: the relay has the first line (with delete) to itself, the order buttons sit on the second.
  await sheet.getByRole('tab', {name: '入口'}).tap();
  const first = sheet.locator('.mesh-cand').first();
  await sheet.getByRole('button', {name: '关闭'}).tap();
  await sheet.waitFor({state: 'hidden'});
  // Still selected: the dock offers the panel again and "connect to…".
  await page.locator('.mesh-dock', {hasText: '已选中，可拖动'}).waitFor();
  assert.equal(await node(page, 'home').getAttribute('class').then(c => /draggable/.test(c)), true, 'a selected node can be dragged');
  assert.equal(await node(page, 'lax').getAttribute('class').then(c => /draggable/.test(c)), false, 'others pan the board');
  await page.locator('.mesh-dock').getByRole('button', {name: '连接到…'}).tap();
  await page.locator('.mesh-banner', {hasText: '把 Home 连过去'}).waitFor();
  await node(page, 'mac').tap();
  await page.locator('.mesh-notice', {hasText: '直连'}).waitFor();
  await h.saved(d => d.directLinks.length === 1 && d.directLinks[0].a === 'home' && d.directLinks[0].b === 'mac', 'direct link');
  await sheet.locator('.mesh-sheet-head h2', {hasText: '直连'}).waitFor();
  await page.locator('.mesh-dock').getByRole('button', {name: '取消选择'}).tap();
  await page.locator('.mesh-dock').getByRole('button', {name: '网格设置与访问规则'}).tap();
  const overview = page.locator('.mesh-overview-sheet');
  await overview.getByRole('textbox', {name: '地址段'}).waitFor();
  await overview.getByText('允许所有成员互相 ping').waitFor();
  await overview.getByRole('button', {name: '关闭'}).tap();
  await overview.waitFor({state: 'hidden'});
  await h.close();
  const light = await setup(t, {width: 390, height: 844});
  await node(light.page, 'home').tap();
  await light.page.locator('.mesh-inspect-sheet .mesh-sparks > li').first().waitFor();
  await light.close();
});

test('reachability and entry health: verdict badges, the standby alert naming the failing candidate, the real exit and observers', async t => {
  const h = await setup(t, {reach: true});
  const {page} = h;
  const reachTag = id => node(page, id).locator('.mesh-node-reach');
  await reachTag('sg').waitFor();
  assert.deepEqual(await Promise.all(['sg', 'lax', 'home', 'toy', 'mac', 'win'].map(id => reachTag(id).innerText())), ['公网可达', '公网可达', '可打洞', '可打洞', '可打洞', '未知']);
  assert.deepEqual(await Promise.all(['sg', 'home', 'win'].map(id => reachTag(id).getAttribute('data-tone'))), ['ok', 'info', 'off']);
  assert.match(await reachTag('home').getAttribute('title'), /^可达性：可打洞。在 NAT 后/);
  // Mac's only standby (LAX) never connected: an alert on the node that says which and why.
  const alert = node(page, 'mac').locator('.mesh-node-alert');
  assert.equal(await alert.innerText(), '备用入口不可用');
  assert.equal(await alert.getAttribute('data-tone'), 'bad');
  assert.equal(await alert.getAttribute('title'), 'LAX 备用：超时');
  assert.equal(await node(page, 'win').locator('.mesh-node-alert').innerText(), '出口被代理改写');
  assert.equal(await page.locator('.mesh-node-alert').count(), 2);
  // The entry lines say so too: the failing standby in red with the error, a punching entry flowing.
  assert.equal(await label(page, '2 · TCP · 超时').getAttribute('data-look'), 'failing');
  assert.equal(await label(page, '1 · 打洞').getAttribute('data-look'), 'active');
  assert.equal(await label(page, '1 · 打洞中').getAttribute('data-look'), 'punching');
  assert.equal(await label(page, '2 · TCP · 超时').getAttribute('title'), 'LAX 备用：超时');

  await node(page, 'mac').click();
  const panel = inspector(page);
  const hint = panel.locator('.mesh-hints summary').first();
  await hint.waitFor();
  assert.equal(await hint.innerText(), '备用入口不可用：LAX 备用：超时');
  assert.equal(await panel.locator('.mesh-hints > li').first().getAttribute('data-tone'), 'bad');
  await hint.click();
  await panel.getByText('LAX · TCP：连不上：超时 · 从没连上过').waitFor();
  await panel.getByText('备用入口都连不上：在用的入口断开时，没有可以切换过去的入口').waitFor();
  await panel.locator('.mesh-cand-health').waitFor();
  const candRows = () => panel.locator('.mesh-cand-list > li').evaluateAll(list => list.map(li => [...li.querySelectorAll('.mesh-cand-no, strong, .mesh-cand-name > small, .mesh-cand-now > *')].map(e => e.textContent.trim()).filter(Boolean).join(' | ')));
  assert.deepEqual(await candRows(), ['1 | TOY | 打洞 | 在用 | 已 30 分钟', '2 | LAX | TCP | 连不上 | 超时 · 从没连上过']);
  assert.equal(await panel.locator('.mesh-cand-list > li').nth(1).getAttribute('data-tone'), 'bad');
  assert.match(await panel.locator('.mesh-reach').innerText(), /可打洞\s*出口 192\.0\.2\.34:51999/);
  await panel.getByRole('tab', {name: '入口'}).click();
  assert.deepEqual(await panel.locator('.mesh-cand-state').evaluateAll(list => list.map(el => [...el.children].map(c => c.textContent.trim()).join(' | '))), ['在用 | 已 30 分钟', '连不上 | 超时 · 从没连上过']);
  assert.equal(await panel.locator('.mesh-cand-punch').innerText(), '打洞');

  // Win: the observers disagree about the exit, so it is not to be trusted.
  await node(page, 'win').click();
  await panel.locator('.mesh-hints summary', {hasText: '出口被代理改写'}).waitFor();
  const reach = panel.locator('.mesh-reach');
  await reach.getByText('出口被代理改写：各观测点看到的地址不一致').waitFor();
  assert.match(await reach.innerText(), /未知\s*出口 192\.0\.2\.42:51999/);
  await reach.locator('summary').click();
  assert.match(await reach.locator('summary').innerText(), /^观测点 2\/3 个有回应 · 检测于 3 分钟前$/);
  const seen = await texts(reach.locator('.mesh-observers li'));
  assert.deepEqual(seen, ['SG 中转 192.0.2.34:51999', 'stun.miwifi.com:3478 STUN 192.0.2.42:51999', 'stun.l.google.com:19302 STUN 没有回应']);
  await panel.locator('.mesh-hints summary', {hasText: '出口被代理改写'}).click();
  await panel.getByText(/各观测点看到的出口地址不一致（SG 看到 192\.0\.2\.34:51999；stun\.miwifi\.com:3478 看到 192\.0\.2\.42:51999）/).waitFor();
  const winCands = await candRows();
  assert.match(winCands[0], /^1 \| TOY \| 打洞 \| 打洞中 \| 已 \d+ 秒$/);
  assert.equal(winCands[1], '2 | SG | TCP | 在用 | 已 1 小时');

  // New events of a relay read in Chinese.
  await node(page, 'home').click();
  await panel.getByRole('tab', {name: '事件'}).click();
  await panel.locator('.mesh-events > li').nth(2).waitFor();
  const events = await texts(panel.locator('.mesh-events > li'));
  for (const want of [/换路 到 TOY：经 SG → 直达（新线路更快）/, /线路暂停使用 到 LAX 的中转链路反复断开，暂停用于选路/, /可达性变化 未知 → 可打洞/])
    assert.ok(events.some(e => want.test(e)), `${want} in ${JSON.stringify(events)}`);

  // The other verdicts arrive with the next GET /mesh.
  h.mesh.nodes.lab.summary.reachability = {...h.mesh.nodes.lab.summary.reachability, verdict: 'symmetric'};
  await until(page, async () => await reachTag('lab').innerText() === '对称型 NAT', 'symmetric badge');
  h.mesh.nodes.lab.summary.reachability = {...h.mesh.nodes.lab.summary.reachability, verdict: 'no_udp'};
  await until(page, async () => await reachTag('lab').innerText() === 'UDP 不通', 'no_udp badge');
  assert.equal(await reachTag('lab').getAttribute('data-tone'), 'bad');
  // Not reported (null): the badge goes, nothing breaks.
  h.mesh.nodes.lab.summary.reachability = null;
  await until(page, async () => await reachTag('lab').count() === 0, 'no badge without a report');
  await h.close();

  // An older controller without these fields: no badges, no alerts, no new rows.
  const plain = await setup(t);
  assert.equal(await plain.page.locator('.mesh-node-reach, .mesh-node-alert').count(), 0);
  await node(plain.page, 'home').click();
  await inspector(plain.page).locator('.mesh-sparks > li').first().waitFor();
  assert.equal(await inspector(plain.page).locator('.mesh-reach').count(), 0);
  // Its entries are listed as configured, with no state made up.
  assert.equal(await inspector(plain.page).locator('.mesh-cand-list > li').count(), 2);
  assert.equal(await inspector(plain.page).locator('.mesh-cand-now .pb-tag').count(), 0);
  assert.equal(await label(plain.page, '2 · TCP').getAttribute('data-look'), 'active');
  await plain.close();
});

test('path view: pick the target, hops and total, the path stands out, follows route changes, stale and missing data, older controller', async t => {
  const h = await setup(t, {reach: true});
  const {page} = h;
  const panel = inspector(page);
  const box = page.locator('.mesh-path');
  const pathCalls = () => h.calls.filter(c => c.path === '/api/servers/mesh/path');
  const onPath = async () => (await texts(page.locator('.mesh-edge-label[data-path]'))).sort();
  const hops = async () => ({nodes: await texts(box.locator('.mesh-path-node')), via: (await texts(box.locator('.mesh-path-via'))).map(s => s.replace(/\s*→$/, ''))});
  await node(page, 'home').click();
  await panel.getByRole('button', {name: '查看到…的路径'}).click();
  await page.locator('.mesh-banner', {hasText: '查看路径：点目标服务器，看 Home 怎么到它'}).waitFor();
  await node(page, 'mac').click();
  await box.locator('.mesh-path-hops').waitFor();
  assert.equal(pathCalls()[0].query, '?src=home&dst=mac');
  assert.equal(await box.locator('h3').innerText(), 'Home 到 公司-Mac 的路径');
  assert.deepEqual(await hops(), {nodes: ['Home', 'TOY', '公司-Mac'], via: ['中转链路 2.6 ms', '局域网 6.1 ms']});
  assert.equal(await box.locator('.mesh-path-total').innerText(), '总延迟约 8.7 ms');
  // Like the rule layer: the path's nodes and links stay, the rest steps back.
  assert.deepEqual(await dimmed(page), ['lab', 'lax', 'sg', 'win']);
  assert.equal(await page.locator('.mesh-node[data-path]').count(), 3);
  assert.deepEqual(await onPath(), ['1 · 打洞', '打洞 · 2.6 ms']);
  assert.equal(await page.locator('.mesh-edge-label:not([data-path])').count(), await page.locator('.mesh-edge-label[data-dim]').count());

  // Home↔TOY goes down: the route over SG shows by itself.
  const overSg = [{node: 'home', via: '', rttMs: null}, {node: 'sg', via: 'relay', rttMs: 79.4}, {node: 'toy', via: 'relay', rttMs: 75.2}, {node: 'mac', via: 'lan', rttMs: 8.8}];
  h.mesh.setPath(() => ({hops: overSg, rttMs: 163.4, stale: false}));
  await box.locator('.mesh-path-node', {hasText: 'SG'}).waitFor({timeout: 9000});
  assert.deepEqual(await hops(), {nodes: ['Home', 'SG', 'TOY', '公司-Mac'], via: ['中转链路 79 ms', '中转链路 75 ms', '局域网 8.8 ms']});
  assert.equal(await box.locator('.mesh-path-total').innerText(), '总延迟约 163 ms');
  assert.deepEqual(await onPath(), ['1 · 打洞', '拨号 · 75 ms', '拨号 · 79 ms']);
  assert.deepEqual(await dimmed(page), ['lab', 'lax', 'win']);
  // A hop without data: the total is what is known, and it says so.
  h.mesh.setPath(() => ({hops: overSg.map(x => x.node === 'mac' ? {...x, rttMs: null} : x), rttMs: null, stale: true}));
  await box.getByText('数据不完整：有的线路没有上报，路径和延迟可能不准').waitFor({timeout: 9000});
  assert.match(await box.locator('.mesh-path-total').innerText(), /^总延迟未知/);
  assert.deepEqual((await hops()).via, ['中转链路 79 ms', '中转链路 75 ms', '局域网']);
  // Broken on the way: the hops up to the break, and where it stops.
  h.mesh.setPath(() => ({hops: overSg.slice(0, 2), rttMs: null, stale: true}));
  await box.getByText('在 SG 之后断开，现在到不了 公司-Mac').waitFor({timeout: 9000});
  assert.equal(await box.locator('.mesh-path-total').count(), 0);
  assert.deepEqual(await onPath(), ['拨号 · 79 ms']);
  // An end not in the applied mesh (only in the draft): 404 mesh_member_not_found.
  h.mesh.setPath(() => ({status: 404, error: 'mesh_member_not_found'}));
  await box.getByText('读不到路径：起点或终点还没有应用到网格，应用后才能查看路径').waitFor({timeout: 9000});
  assert.deepEqual(await dimmed(page), []);
  // No path at all: said, and nothing is dimmed.
  h.mesh.setPath(() => ({hops: [], rttMs: null, stale: true}));
  await box.getByText('现在没有可用的路径。').waitFor({timeout: 9000});
  assert.deepEqual(await dimmed(page), []);
  // Esc closes it and the refresh stops.
  await page.keyboard.press('Escape');
  await box.waitFor({state: 'detached'});
  const asked = pathCalls().length;
  await page.waitForTimeout(6000);
  assert.equal(pathCalls().length, asked, 'no path requests once closed');
  // Clicking the board while picking cancels the pick.
  await node(page, 'home').click();
  await panel.getByRole('button', {name: '查看到…的路径'}).click();
  await page.locator('.mesh-banner', {hasText: '查看路径'}).waitFor();
  await page.locator('.vue-flow__pane').click({position: {x: 20, y: 600}});
  await page.locator('.mesh-banner', {hasText: '查看路径'}).waitFor({state: 'detached'});
  assert.equal(pathCalls().length, asked);
  await h.close();

  // An older controller without GET /mesh/path: said once, nothing dimmed, not asked again.
  const old = await setup(t, {reach: true, noPath: true});
  await node(old.page, 'home').click();
  await inspector(old.page).getByRole('button', {name: '查看到…的路径'}).click();
  await node(old.page, 'mac').click();
  await old.page.locator('.mesh-path').getByText('主控还不支持查看路径，升级主控后可用。').waitFor();
  assert.deepEqual(await dimmed(old.page), []);
  await old.page.waitForTimeout(6000);
  assert.equal(old.calls.filter(c => c.path === '/api/servers/mesh/path').length, 1);
  await old.close();
});

test('grid settings: extra STUN observers, checked, saved; left out for an older controller', async t => {
  const h = await setup(t, {reach: true});
  const {page} = h;
  const panel = inspector(page);
  // What a setting does is in a tooltip on its ⓘ, shown on hover.
  assert.equal(await page.getByText('这些是第三方服务', {exact: false}).count(), 0);
  await panel.getByRole('button', {name: 'STUN：说明'}).hover();
  await page.getByRole('tooltip').filter({hasText: '这些是第三方服务，会看到你的出口地址；留空就只用自己的中转。'}).waitFor();
  await panel.getByRole('textbox', {name: '添加 STUN 服务器'}).hover();
  const input = panel.getByRole('textbox', {name: '添加 STUN 服务器'});
  await input.fill('bad host');
  await input.press('Enter');
  await panel.getByText('写成 主机:端口，例如 stun.example.com:3478').waitFor();
  for (const v of ['stun.miwifi.com:3478', 'stun.l.google.com:19302', '[2001:db8::1]:3478', 'stun.miwifi.com:3478']) { await input.fill(v); await input.press('Enter'); }
  assert.equal(await panel.locator('.sv-chip').count(), 3, 'no duplicates');
  await panel.getByRole('button', {name: '保存到草稿'}).click();
  let body = await h.saved(d => d.settings.reachability?.stun?.length === 3, 'stun list');
  assert.deepEqual(body.settings.reachability, {stun: ['stun.miwifi.com:3478', 'stun.l.google.com:19302', '[2001:db8::1]:3478']});
  await panel.getByRole('button', {name: '删除 [2001:db8::1]:3478'}).click();
  await panel.getByRole('button', {name: '删除 stun.l.google.com:19302'}).click();
  await panel.getByRole('button', {name: '保存到草稿'}).click();
  body = await h.saved(d => d.settings.reachability?.stun?.length === 1, 'one stun');
  assert.deepEqual(body.settings.reachability.stun, ['stun.miwifi.com:3478']);
  await h.close();

  const plain = await setup(t);
  const settings = inspector(plain.page);
  await settings.getByRole('spinbutton', {name: 'MTU'}).fill('1380');
  await settings.getByRole('button', {name: '保存到草稿'}).click();
  body = await plain.saved(d => d.settings.mtu === 1380, 'mtu');
  assert.equal('reachability' in body.settings, false);
  await plain.close();
});
