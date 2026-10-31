// Mesh member settings and rule sides without a browser: tags, help tooltips, address and port
// checks, gateway networks, 所有网关网段 — what is saved to the draft and what the panel shows, in
// happy-dom with the browser test's fake controller (testing/meshFormsFixture.mjs). The layout
// (sizes, 16px inputs, phone sheets, dark theme) stays in mesh.forms.browser.test.mjs.
import test from 'node:test';
import assert from 'node:assert/strict';
import {mountPage} from './testing/pageDom.mjs';
import {sleep, settle} from '../testing/domPage.mjs';
import {GB, server, member, VIEW_KEYS, fakeMesh} from './testing/meshFormsFixture.mjs';

async function setup(t, {width = 1440, height = 900, dark = false} = {}) {
  const mesh = fakeMesh();
  const items = [server('berlin', 'Berlin'), server('lax', 'LAX', {region: 'US'}), server('home', 'Home', {region: 'CN'})];
  const h = await mountPage(t, {width, height, dark, load: n => import(`../views/ServersView.vue?fresh=${n}`),
    session: {'openbear.servers.tabs': JSON.stringify({tabs: [], active: 'mesh'})},
    handle: async (method, path, body) => {
      let status = 200, data = {ok: true};
      if (path.startsWith('/api/servers/mesh')) [status, data] = mesh.handle(method, path.slice('/api/servers/mesh'.length), body);
      else if (path === '/api/servers' && method === 'GET') data = {ok: true, items, settings: {accessPoints: [], defaultAccess: '', timezone: 'Asia/Shanghai'}, node: {available: true, version: '0.13.1'}};
      else if (path === '/api/servers/live') data = {ok: true, at: Date.now(), items: Object.fromEntries(items.map(i => [i.id, i.live]))};
      else if (path.endsWith('/metrics')) data = {ok: true, step: 3, points: {t: [], cpu: [], steal: [], mem: [], rxRate: [], txRate: [], ioRead: [], ioWrite: [], load: [], tcp: [], udp: []}};
      return [status, data];
    }});
  await h.page.locator('.vue-flow__node').first().waitFor();
  const puts = () => h.calls.filter(c => c.method === 'PUT' && c.path === '/api/servers/mesh/draft');
  return {...h, mesh, puts,
    saved: async (check, label = 'draft save') => { for (let i = 0; i < 100; i++) { const hit = puts().find(c => { try { return check(c.body); } catch { return false; } }); if (hit) return hit.body; await sleep(20); await settle(); } assert.fail(`no ${label}`); }};
}
const node = (page, id) => page.locator(`.vue-flow__node[data-id="${id}"]`);
const memberOf = (body, id) => body.members.find(m => m.serverId === id);
const tip = (page, text) => page.getByRole('tooltip').filter({hasText: text});

test('desktop: tags, tooltips, address and ports checked as typed, gateway networks', async t => {
  const h = await setup(t);
  const {page} = h;
  await node(page, 'home').click();
  const panel = page.locator('.mesh-inspector-col');
  await panel.getByRole('tab', {name: '配置'}).click();

  // Help: a tooltip on hover; the form below does not move.
  const address = panel.getByRole('textbox', {name: '网格地址'});
  await panel.getByRole('button', {name: '标签：说明'}).hover();
  await tip(page, '用于画板展示和访问规则里的分组').waitFor();
  await panel.getByRole('button', {name: '网格地址：说明'}).hover();
  await tip(page, '留空').waitFor();
  await tip(page, '用于画板展示').waitFor({state: 'hidden'});
  // From the keyboard too.
  await panel.getByRole('button', {name: '发送速率：说明'}).focus();
  await tip(page, '发往网格的速率').waitFor();

  // Tags: suggestions from the other members, Enter, comma, leaving the field; Backspace and ✕ remove.
  const tags = panel.getByRole('combobox', {name: '添加标签'});
  await tags.click();
  const list = page.getByRole('listbox', {name: '添加标签：候选'});
  await list.getByRole('option').first().waitFor();
  assert.deepEqual(await list.getByRole('option').allInnerTexts(), ['公司', '欧洲', '美国']);
  await tags.pressSequentially('公');
  assert.deepEqual(await list.getByRole('option').allInnerTexts(), ['公司']);
  await tags.press('ArrowDown');
  await tags.press('Enter');
  await h.saved(d => memberOf(d, 'home').tags.join() === '家,公司', 'tag from a suggestion');
  await tags.pressSequentially('测试,');
  await h.saved(d => memberOf(d, 'home').tags.join() === '家,公司,测试', 'tag by comma');
  assert.equal(await tags.inputValue(), '');
  await tags.pressSequentially('临时');
  await address.click();
  await h.saved(d => memberOf(d, 'home').tags.join() === '家,公司,测试,临时', 'tag on blur');
  await tags.click();
  await tags.press('Backspace');
  await h.saved(d => memberOf(d, 'home').tags.join() === '家,公司,测试', 'backspace removes the last');
  await panel.getByRole('button', {name: '删除 家'}).click();
  await h.saved(d => memberOf(d, 'home').tags.join() === '公司,测试', '✕ removes one');
  assert.equal(await tags.getAttribute('maxlength'), '40');

  // Address: said as typed; only good ones reach the draft.
  const putsBefore = h.puts().length;
  const addressError = panel.locator('.mesh-field', {has: page.getByRole('textbox', {name: '网格地址'})}).locator('.ob-bad');
  for (const [value, text] of [['100.96.0.1', '和 Berlin 的地址重复'], ['100.97.0.8', '不在组网网段 100.96.0.0/16 内'], ['100.96.0.0', '100.96.0.0 是组网网段的网络地址，不能分给成员'],
    ['100.96.255.255', '100.96.255.255 是组网网段的广播地址，不能分给成员'], ['100.96.255.7', '100.96.255.0/24 留作控制地址，请换一个'], ['100.96.0.x', '写成 IPv4 地址，例如 100.96.0.10；留空自动分配']]) {
    await address.fill(value);
    assert.equal(await addressError.innerText(), text, value);
  }
  await address.press('Tab');
  assert.equal(h.puts().length, putsBefore, 'nothing invalid is saved');
  await address.focus();
  await address.fill('100.96.');
  assert.equal(await addressError.count(), 0, 'not yet an address: nothing said');
  assert.equal(await address.getAttribute('placeholder'), '留空自动分配');
  await address.fill('100.96.0.20');
  await address.press('Tab');
  await h.saved(d => memberOf(d, 'home').address === '100.96.0.20', 'a good address');
  // What only the controller knows comes back by path, in its words, under the field.
  await address.fill('100.96.0.99');
  await address.press('Tab');
  await addressError.filter({hasText: '这个地址给主控的网关留着，请换一个'}).waitFor();
  await address.fill('');
  await address.press('Tab');
  await h.saved(d => memberOf(d, 'home').address === '', 'empty: assigned on apply');
  await addressError.waitFor({state: 'detached'});

  // Ports: 1–65535, not another transport's; the port another program holds says so, and that apply will refuse it.
  const udp = panel.getByRole('textbox', {name: 'UDP 端口'});
  const udpNotes = panel.locator('.mesh-listen-note[data-port=udp]');
  assert.match(await udpNotes.filter({hasText: 'dnsmasq'}).innerText(), /UDP 端口 51999 已被 dnsmasq（PID 812）占用[\s\S]*应用时会被拒绝/);
  assert.equal(await udpNotes.filter({hasText: 'dnsmasq'}).getAttribute('class'), 'ob-warn mesh-listen-note');
  const before = h.puts().length;
  await udp.fill('70000');
  await udpNotes.filter({hasText: '端口在 1–65535 之间'}).waitFor();
  await udp.fill('0');
  await udpNotes.filter({hasText: '端口在 1–65535 之间'}).waitFor();
  await udp.fill('52000');
  await udpNotes.filter({hasText: '和 TCP 端口相同'}).waitFor();
  await udp.press('Tab');
  assert.equal(h.puts().length, before);
  await udp.fill('51888');
  await udp.press('Tab');
  await h.saved(d => memberOf(d, 'home').listen.udp === 51888, 'a good port');
  // The controller checks the new port when the draft is saved: the warning about 51999 goes.
  await udpNotes.filter({hasText: 'dnsmasq'}).waitFor({state: 'detached'});
  assert.deepEqual(await panel.locator('.mesh-listen-note.ob-bad').allInnerTexts(), []);

  // Gateway: networks typed like tags, checked as typed, the machine's LAN networks offered.
  await panel.getByRole('checkbox', {name: '把局域网网段接入网格'}).check();
  const nets = panel.getByRole('combobox', {name: '添加网关网段'});
  await panel.locator('.mesh-field', {has: page.getByRole('combobox', {name: '添加网关网段'})}).getByText('网关至少要有一个网段').waitFor();
  await nets.click();
  const offered = page.getByRole('listbox', {name: '添加网关网段：候选'});
  await offered.getByRole('option').first().waitFor();
  // 10.20.5.0/24 is inside LAX's 10.20.0.0/16: not offered.
  assert.deepEqual(await offered.getByRole('option').allInnerTexts(), ['192.168.50.0/24\n本机所在的局域网']);
  const netError = panel.locator('.mesh-chips', {has: page.getByRole('combobox', {name: '添加网关网段'})}).locator('.ob-bad');
  await nets.pressSequentially('192.168.');
  assert.equal(await netError.count(), 0, 'nothing said while it may still become right');
  for (const [value, text] of [['192.168.1.5/24', '192.168.1.5/24 不是网段的起始地址，应写成 192.168.1.0/24'], ['100.96.8.0/24', '和组网网段 100.96.0.0/16 重叠'],
    ['10.20.8.0/24', '和 LAX 的网段 10.20.0.0/16 重叠'], ['192.168.1.0/33', '写成网段，例如 192.168.1.0/24'], ['lan', '写成网段，例如 192.168.1.0/24']]) {
    await nets.fill(value);
    assert.equal(await netError.innerText(), text, value);
  }
  await nets.fill('');
  await offered.getByRole('option').first().click();
  await h.saved(d => memberOf(d, 'home').gateway?.subnets.join() === '192.168.50.0/24', 'a suggested network');
  await nets.pressSequentially('10.30.0.0/16,');
  await h.saved(d => memberOf(d, 'home').gateway.subnets.join() === '192.168.50.0/24,10.30.0.0/16', 'a network by comma');
  await nets.pressSequentially('172.16.5.1/24');
  await nets.press('Enter');
  assert.equal(await netError.innerText(), '172.16.5.1/24 不是网段的起始地址，应写成 172.16.5.0/24');
  assert.equal(await nets.inputValue(), '172.16.5.1/24', 'a bad value stays to be fixed');
  await nets.fill('172.16.5.0/24');
  await address.click();
  await h.saved(d => memberOf(d, 'home').gateway.subnets.length === 3, 'a network on blur');
  // IP masquerade, explained in a tooltip; off, it says what the router needs.
  const masq = panel.getByRole('checkbox', {name: '地址转换（IP 伪装）'});
  assert.equal(await masq.isChecked(), true);
  await panel.getByRole('button', {name: '地址转换：说明'}).hover();
  await tip(page, '局域网里的设备看到的来源是网关这台机器的地址，所以不用在路由器上加回程路由').waitFor();
  await masq.uncheck();
  await h.saved(d => memberOf(d, 'home').gateway.masquerade === false, 'masquerade off');
  await panel.getByText('需要在局域网路由器上把组网网段 100.96.0.0/16 指向这台网关').waitFor();
  await h.close();
});

test('desktop dark: the same form, its suggestions, errors and tooltips', async t => {
  const h = await setup(t, {dark: true});
  const {page} = h;
  await node(page, 'home').click();
  const panel = page.locator('.mesh-inspector-col');
  await panel.getByRole('tab', {name: '配置'}).click();
  const tags = panel.getByRole('combobox', {name: '添加标签'});
  await tags.click();
  await page.getByRole('listbox', {name: '添加标签：候选'}).getByRole('option').first().waitFor();
  await page.keyboard.press('Escape');
  const address = panel.getByRole('textbox', {name: '网格地址'});
  await address.fill('100.96.0.1');
  await panel.getByText('和 Berlin 的地址重复').waitFor();
  await panel.getByRole('checkbox', {name: '把局域网网段接入网格'}).check();
  const nets = panel.getByRole('combobox', {name: '添加网关网段'});
  await nets.pressSequentially('10.20.8.0/24');
  await panel.getByText('和 LAX 的网段 10.20.0.0/16 重叠').waitFor();
  await nets.fill('');
  await page.getByRole('listbox', {name: '添加网关网段：候选'}).getByRole('option').first().waitFor();
  await page.keyboard.press('Escape');
  await panel.getByRole('button', {name: '地址转换：说明'}).hover();
  await tip(page, '不用在路由器上加回程路由').waitFor();
  await h.close();
});

test('rules: 所有网关网段 on both sides, with the rest, in the summary; the bare gateway hint', async t => {
  for (const dark of [false, true]) {
    const h = await setup(t, {dark});
    const {page} = h;
    const panel = page.locator('.mesh-inspector-col');
    await panel.getByRole('button', {name: '＋ 添加规则'}).click();
    await panel.getByRole('combobox', {name: '来源：添加成员或标签'}).selectOption('members:berlin');
    await panel.getByRole('combobox', {name: '目标：添加成员或标签'}).selectOption('members:lax');
    // LAX is a gateway chosen as a machine: its networks are not in.
    const hint = panel.getByText('LAX 只包括这台机器，不包括它的网段；要开放网段请在这里添加。');
    await hint.waitFor();
    const dst = panel.locator('.mesh-side').nth(1);
    assert.match(await dst.locator('.mesh-side-every').innerText(), /所有成员\s*各成员自己的组网地址，以后加入的也算；不含网关后面的局域网\s*所有网关网段\s*各网关后面的局域网，比如家里的 10\.10\.10\.0\/24；以后新加的网段也算/);
    assert.match(await panel.locator('.mesh-side').first().locator('.mesh-side-every').innerText(), /所有网关网段\s*各网关后面局域网里的设备，比如家里 10\.10\.10\.0\/24 的电脑，可以主动访问目标/);
    await panel.getByRole('checkbox', {name: '目标：所有网关网段'}).check();
    await h.saved(d => d.rules[0].dst.allSubnets === true && d.rules[0].dst.members.join() === 'lax', 'dst all gateway networks, with LAX');
    await hint.waitFor({state: 'detached'});
    // A network written as well stays.
    await panel.getByRole('textbox', {name: '目标：添加网段'}).fill('10.20.0.0/16');
    await panel.getByRole('textbox', {name: '目标：添加网段'}).press('Enter');
    await h.saved(d => d.rules[0].dst.subnets.join() === '10.20.0.0/16' && d.rules[0].dst.allSubnets, 'a network with all gateway networks');
    assert.equal(await dst.locator('.sv-chip').count(), 2, 'LAX and the network as chips; all gateway networks is the box');
    await panel.getByRole('checkbox', {name: '来源：所有成员'}).check();
    await panel.getByRole('checkbox', {name: '来源：所有网关网段'}).check();
    const body = await h.saved(d => d.rules[0].src.all && d.rules[0].src.allSubnets, 'src all members and all gateway networks');
    assert.deepEqual(body.rules[0].src.members, []);
    await page.keyboard.press('Escape');
    await page.locator('.mesh-inspector-head > h2', {hasText: '网格'}).waitFor();
    assert.equal(await panel.getByRole('button', {name: '编辑第 1 条规则'}).innerText(), '允许 所有成员、所有网关网段 访问 所有网关网段、LAX、10.20.0.0/16 的 所有端口');
    // A side with only all gateway networks counts as chosen.
    await panel.getByRole('button', {name: '＋ 添加规则'}).click();
    await panel.getByText('还没有选，这条规则不会生效').first().waitFor();
    await panel.getByRole('checkbox', {name: '来源：所有网关网段'}).check();
    assert.equal(await panel.locator('.mesh-side').first().getByText('还没有选，这条规则不会生效').count(), 0);
    await h.close();
  }
});

test('phone, light and dark: tooltips by tap, tag suggestions, 16px inputs, rule sides in the sheet', async t => {
  for (const dark of [false, true]) {
    const h = await setup(t, {width: 390, height: 844, dark});
    const {page} = h;
    await node(page, 'home').tap();
    const sheet = page.locator('.mesh-inspect-sheet');
    await sheet.locator('.mesh-sheet-head h2', {hasText: 'Home'}).waitFor();
    await sheet.getByRole('tab', {name: '配置'}).tap();
    const tags = sheet.getByRole('combobox', {name: '添加标签'});
    await tags.waitFor();
    // A tap opens the explanation, a tap elsewhere closes it; it does not push the form.
    const address = sheet.getByRole('textbox', {name: '网格地址'});
    const toggle = sheet.getByRole('button', {name: '标签：说明'});
    await toggle.tap();
    await tip(page, '用于画板展示和访问规则里的分组').waitFor();
    await sheet.locator('.mesh-group-title', {hasText: '网络'}).tap();
    await tip(page, '用于画板展示和访问规则里的分组').waitFor({state: 'hidden'});
    await toggle.tap();
    await tip(page, '用于画板展示和访问规则里的分组').waitFor();
    await toggle.tap();
    await tip(page, '用于画板展示和访问规则里的分组').waitFor({state: 'hidden'});
    // Tag suggestions by tap.
    await tags.tap();
    const list = page.getByRole('listbox', {name: '添加标签：候选'});
    await list.getByRole('option', {name: '欧洲'}).waitFor();
    await list.getByRole('option', {name: '欧洲'}).tap();
    await h.saved(d => memberOf(d, 'home').tags.join() === '家,欧洲', 'phone tag');
    // Gateway networks and the masquerade tooltip.
    await sheet.getByRole('checkbox', {name: '把局域网网段接入网格'}).tap();
    const nets = sheet.getByRole('combobox', {name: '添加网关网段'});
    await nets.tap();
    await page.getByRole('listbox', {name: '添加网关网段：候选'}).getByRole('option').first().tap();
    await h.saved(d => memberOf(d, 'home').gateway?.subnets.join() === '192.168.50.0/24', 'phone gateway network');
    await sheet.getByRole('button', {name: '地址转换：说明'}).tap();
    await tip(page, '不用在路由器上加回程路由').waitFor();
    await nets.scrollIntoViewIfNeeded();
    await sheet.getByRole('button', {name: '地址转换：说明'}).tap();
    // The port in use is said under the port, and nothing goes sideways.
    await sheet.locator('.mesh-listen-note[data-port=udp]', {hasText: 'dnsmasq'}).scrollIntoViewIfNeeded();
    await h.close();
    // Rules in the overview sheet: both boxes on each side, the hint gone with all gateway networks.
    const p = await setup(t, {width: 390, height: 844, dark});
    await p.page.locator('.mesh-dock').getByRole('button', {name: '网格设置与访问规则'}).tap();
    await p.page.locator('.mesh-overview-sheet').getByRole('button', {name: '＋ 添加规则'}).tap();
    const box2 = p.page.getByRole('checkbox', {name: '目标：所有网关网段'});
    await box2.waitFor();
    const ruleSheet = p.page.locator('.mesh-sheet', {has: box2});
    await ruleSheet.getByRole('combobox', {name: '目标：添加成员或标签'}).selectOption('members:lax');
    await ruleSheet.getByText('LAX 只包括这台机器，不包括它的网段').waitFor();
    await box2.tap();
    await p.saved(d => d.rules[0]?.dst.allSubnets, 'phone dst all gateway networks');
    await ruleSheet.getByText('LAX 只包括这台机器，不包括它的网段').waitFor({state: 'detached'});
    await p.close();
  }
});
