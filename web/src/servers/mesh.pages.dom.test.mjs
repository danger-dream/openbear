// The mesh pages (third design) in happy-dom: the overview board, machines and a machine's page, joining
// with measurements, lines and line rules, exit groups and proxy links, shared LANs, revisions. The
// fake controller is testing/meshBackend.mjs with v3 on; helpers: testing/meshDom.mjs.
import test from 'node:test';
import assert from 'node:assert/strict';
import {memberOf, setup, texts, until} from './testing/meshDom.mjs';
import {linesAtWork} from './testing/meshLinesFixture.mjs';

const tab = (page, name) => page.locator('.m3-tabs').getByRole('tab', {name: new RegExp(`^${name}`)});
const box = async el => el.evaluate(e => ({x: parseFloat(e.style.left), y: parseFloat(e.style.top), w: parseFloat(e.style.width), h: parseFloat(e.style.height)}));
const plain = list => list.map(x => x.replace(/\p{Regional_Indicator}/gu, '').trim());
const overlap = (a, b) => a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;
// A node picked in a node picker (MeshNodeSelect): opened, then the option with that name (flag aside), in the given group if any.
async function pickNode(page, scope, name, node, group = '') {
  await scope.locator('.m3-node-select', {has: page.getByRole('combobox', {name})}).locator('.el-select__wrapper').click();
  const pop = page.locator('.m3-node-pop:visible');
  const within = group ? pop.locator('.el-select-group__wrap', {has: page.locator('.el-select-group__title', {hasText: group})}) : pop;
  await within.locator('.el-select-dropdown__item', {has: page.locator('.m3-ns', {hasText: new RegExp(`${node}$`)})}).click();
}

test('overview: relays by country, members under the relay they enter by, several open without overlap, any two machines\' route', async t => {
  const h = await setup(t, {v3: true});
  const {page} = h;
  // Berlin in Germany, LAX in the US, each a zone of its own; Home enters by Berlin.
  assert.deepEqual(plain(await texts(page.locator('.m3-card b'))).sort(), ['Berlin', 'LAX']);
  assert.equal(await page.locator('.m3-zone').count(), 2);
  await page.getByRole('button', {name: '全部展开'}).click();
  await page.locator('.m3-chip', {hasText: 'Home'}).waitFor();
  // The open relay's member sits under its card, and nothing overlaps anything.
  const boxes = await Promise.all((await page.locator('.m3-card, .m3-chip').all()).map(box));
  for (let i = 0; i < boxes.length; i++) for (let j = i + 1; j < boxes.length; j++) assert.ok(!overlap(boxes[i], boxes[j]), `boxes ${i} and ${j} overlap`);
  const relay = page.locator('.m3-card', {has: page.getByRole('button', {name: '收起 1 台成员'})});
  const [r, home] = [await box(relay), await box(page.locator('.m3-chip', {hasText: 'Home'}))];
  assert.ok(home.y >= r.y + r.h && Math.abs(home.x - r.x - 14) < 1, 'Home under the relay it enters by');
  // Any two machines: the route as the nodes route it, each hop and the total.
  h.mesh.setPath(() => ({hops: [{node: 'home', via: ''}, {node: 'berlin', via: 'entry', rttMs: 12}, {node: 'lax', via: 'relay', rttMs: 140}], rttMs: 152}));
  await pickNode(page, page, '起点', 'Home');
  await pickNode(page, page, '终点', 'LAX');
  await until(page, async () => (await page.locator('.m3-ppanel').innerText()).includes('共 152 ms'), 'the route');
  assert.ok(h.calls.some(c => c.path === '/api/servers/mesh/path' && c.query === '?src=home&dst=lax'));
  assert.deepEqual(plain(await texts(page.locator('.m3-hop'))), ['Home', 'Berlin', 'LAX']);
  assert.equal(await page.locator('.m3-edge.path').count(), 1);
  // Picking a machine: its card, with the way to its page.
  await page.getByRole('button', {name: '清除路线'}).click();
  await page.locator('.m3-card', {hasText: 'LAX'}).click();
  const card = page.locator('.m3-fcard');
  assert.ok((await card.innerText()).includes('中转'));
  await card.getByRole('button', {name: '打开详情'}).click();
  await page.locator('.m3-node h2', {hasText: 'LAX'}).waitFor();
  await h.close();
});

test('a node\'s page: sections, what it is joined to, auto values with the way back, the address picked, a backup gateway', async t => {
  const h = await setup(t, {v3: true});
  const {page} = h;
  await tab(page, '节点').click();
  // By mesh address.
  assert.deepEqual((await texts(page.locator('.m3-row-name strong'))), ['Berlin', 'LAX', 'Home']);
  await page.locator('.m3-row', {hasText: 'Home'}).click();
  const nav = await texts(page.locator('.m3-node-nav button'));
  assert.deepEqual(nav, ['概况', '角色', '怎么连进组网', '共享局域网', '出口和代理端口', '访问', '网络参数', '诊断', '移除']);
  // The relays it enters by, in order, each with its protocol.
  assert.deepEqual(plain(await texts(page.locator('#m3-sec-connect .m3-nitems-main b'))), ['Berlin', 'LAX']);
  // Listen ports set by hand: what the mesh would set and why, and the way back.
  const network = page.locator('#m3-sec-net');
  assert.ok((await network.locator('.m3-autoline.is-changed').first().innerText()).includes('已改 · 自动值是 UDP 51999（成员只开 UDP）'));
  await network.getByRole('button', {name: '恢复自动'}).first().click();
  await h.saved(d => memberOf(d, 'home').auto.includes('listen'), 'listen back to auto');
  // Opening a transport by hand takes it out of auto.
  await network.getByRole('switch', {name: '监听 TCP'}).click();
  await h.saved(d => !memberOf(d, 'home').auto.includes('listen') && memberOf(d, 'home').listen.tcp === 52000, 'listen set by hand');
  // Not announced; then an address typed by hand with the port a mapping forwards.
  await network.getByRole('radio', {name: /^不对外宣告/}).click();
  await h.saved(d => memberOf(d, 'home').announce === false, 'not announced');
  await network.getByRole('radio', {name: /^手填/}).click();
  await network.getByRole('textbox', {name: '手填对外地址'}).fill('home.example.net');
  await network.getByRole('textbox', {name: '手填对外地址'}).dispatchEvent('change');
  await h.saved(d => memberOf(d, 'home').publicEndpoint === 'home.example.net' && !('announce' in memberOf(d, 'home')), 'typed address');
  // Only TCP is listened on now: its mapping.
  await network.getByRole('textbox', {name: 'TCP 对外端口'}).fill('41999');
  await network.getByRole('textbox', {name: 'TCP 对外端口'}).dispatchEvent('change');
  await h.saved(d => memberOf(d, 'home').publicPorts?.tcp === 41999, 'a port mapping');
  // Berlin shares a LAN under a name; LAX stands by for it.
  await page.getByRole('button', {name: '返回节点列表'}).click();
  await page.locator('.m3-row', {hasText: 'Berlin'}).click();
  const lan = page.locator('#m3-sec-lan');
  await lan.getByRole('switch', {name: '把局域网网段接入网格'}).click();
  await lan.getByRole('textbox', {name: '局域网名称'}).fill('家里');
  await lan.getByRole('textbox', {name: '局域网名称'}).dispatchEvent('change');
  await h.saved(d => memberOf(d, 'berlin').gateway?.name === '家里', 'a LAN name');
  await lan.getByRole('textbox', {name: '添加网关网段'}).fill('192.168.10.0/24');
  await lan.getByRole('textbox', {name: '添加网关网段'}).dispatchEvent('change');
  await h.saved(d => memberOf(d, 'berlin').gateway.subnets.join() === '192.168.10.0/24', 'Berlin\'s network');
  // A new LAN masquerades both ways, nothing marked as changed.
  assert.equal(await lan.locator('.m3-autoline.is-changed').count(), 0);
  assert.deepEqual(memberOf(await h.saved(d => memberOf(d, 'berlin').gateway?.snat === true, 'both ways'), 'berlin').gateway.masquerade, true);
  // LAX, sharing nothing, stands by for Berlin: picked from the gateways there are (with flags), its networks taken over.
  await page.getByRole('button', {name: '返回节点列表'}).click();
  await page.locator('.m3-row', {has: page.locator('.m3-row-name strong', {hasText: /LAX$/})}).click();
  const laxLan = page.locator('#m3-sec-lan');
  await laxLan.locator('.m3-node-select .el-select__wrapper').click();
  assert.ok((await page.locator('.m3-node-pop:visible .el-select-dropdown__item', {hasText: 'Berlin'}).innerText()).includes('🇩🇪'));
  await page.locator('.m3-node-pop:visible .el-select-dropdown__item', {hasText: 'Berlin'}).click();
  const lax = memberOf(await h.saved(d => memberOf(d, 'lax').gateway?.backup === true, 'LAX standing by'), 'lax');
  assert.deepEqual(lax.gateway.subnets, ['192.168.10.0/24']);
  assert.ok((await laxLan.innerText()).includes('给 🇩🇪Berlin 当备用'));
  // Berlin's page lists it among its backups.
  await page.getByRole('button', {name: '返回节点列表'}).click();
  await page.locator('.m3-row', {hasText: 'Berlin'}).click();
  assert.deepEqual(plain(await texts(page.locator('#m3-sec-lan .m3-backup > button:first-child'))), ['LAX']);
  await h.close();
});

test('joining: the node measures every relay, the role and the relays recommended with reasons, then the draft gets them', async t => {
  const h = await setup(t, {v3: true});
  const {page} = h;
  await tab(page, '节点').click();
  await page.getByRole('button', {name: '＋ 加入服务器'}).click();
  const dialog = page.locator('.mesh3-dialog');
  await dialog.getByRole('option', {name: /公司-Mac/}).click();
  await until(page, async () => (await dialog.innerText()).includes('测完了：到 2 台中转，2 台能连上；最近的是 Berlin（13 ms）'), 'the measurements');
  assert.ok(h.calls.some(c => c.path === '/api/servers/mesh/probe' && c.body.serverId === 'mac'));
  await dialog.getByRole('button', {name: '下一步'}).click();
  // macOS: a member, and why.
  assert.ok((await dialog.innerText()).includes('它是 macOS，只能当成员。'));
  assert.ok(await dialog.locator('input[value="relay"]').isDisabled());
  await dialog.getByRole('button', {name: '下一步'}).click();
  // Berlin first (lowest), LAX not second: 160 ms, more than four times Berlin's and loss 2%.
  const picked = plain(await texts(dialog.locator('.m3-items-main b')));
  assert.deepEqual(picked, ['Berlin']);
  assert.ok((await dialog.innerText()).includes('实测 160 ms、丢包 2%，比 Berlin 多 148 ms'));
  // LAX added by hand: UDP timed out there, so TCP, set (not left to the mesh).
  await dialog.locator('tr', {hasText: 'LAX'}).getByRole('button', {name: '加上'}).click();
  assert.ok((await dialog.innerText()).includes('UDP超时，用 TCP'));
  for (const step of [3, 4]) { void step; await dialog.getByRole('button', {name: '下一步'}).click(); }
  assert.ok((await dialog.innerText()).includes('经 🇺🇸LAX 接入（TCP，备用）'));
  await dialog.getByRole('button', {name: '加入草稿'}).click();
  const draft = await h.saved(d => memberOf(d, 'mac')?.entry?.candidates?.length === 2, 'the newcomer\'s entries');
  assert.deepEqual(memberOf(draft, 'mac').entry.candidates.map(c => [c.relay, c.transport, c.auto || []]), [['berlin', 'udp', ['transport']], ['lax', 'tcp', []]]);
  await page.locator('.m3-joined', {hasText: '公司-Mac 已加入草稿'}).waitFor();
  await h.close();
});

test('lines: a line rule saved as it is edited, switched off, relay links made automatically, the matrix', async t => {
  const h = await setup(t, {v3: true});
  const {page} = h;
  await tab(page, '线路').click();
  await page.getByRole('button', {name: '＋ 新建规则'}).click();
  const dialog = page.locator('.mesh3-dialog');
  await dialog.getByRole('textbox', {name: '规则名称'}).fill('跨境走 TLS');
  await dialog.getByRole('textbox', {name: '规则名称'}).dispatchEvent('change');
  await dialog.getByRole('radio', {name: '中转线路'}).click();
  await dialog.getByRole('radiogroup', {name: /^一端的范围$/}).getByRole('radio', {name: /^节点$/}).click();
  await pickNode(page, dialog, '一端：添加节点', 'Berlin');
  // TLS only: UDP and TCP taken out.
  await dialog.getByRole('button', {name: '去掉 UDP'}).click();
  await dialog.getByRole('button', {name: '去掉 TCP'}).click();
  const rule = (await h.saved(d => d.lineRules[0]?.order?.join() === 'tls', 'the rule')).lineRules[0];
  assert.deepEqual([rule.name, rule.applies, rule.a, rule.b], ['跨境走 TLS', 'lines', {kind: 'machines', ids: ['berlin']}, {kind: 'any'}]);
  // It reaches the one relay link, Berlin ⇄ LAX.
  assert.ok((await dialog.innerText()).includes('现在作用于 1 条中转线路'));
  await dialog.getByRole('button', {name: '完成'}).click();
  await page.getByRole('switch', {name: '启用规则 跨境走 TLS'}).click();
  await h.saved(d => d.lineRules[0].on === false, 'the rule off');
  await page.getByRole('radio', {name: /^自动生成/}).click();
  await h.saved(d => d.relayLinks.mode === 'auto', 'links made automatically');
  assert.equal(await page.locator('.m3-mx tbody tr').count(), 2);
  await h.close();
});

test('exits, groups and proxy ports: exits added at once with links to copy, a group by region, a port to it; revisions come back as new ones', async t => {
  const h = await setup(t, {v3: true});
  const {page} = h;
  await tab(page, '出口和代理').click();
  // Two exits at once: each its own proxy on its mesh address, named after the machine.
  await page.getByRole('button', {name: '＋ 添加出口'}).click();
  const add = page.locator('.mesh3-dialog');
  await add.getByRole('checkbox', {name: 'LAX'}).check();
  await add.getByRole('checkbox', {name: 'Berlin'}).check();
  await add.getByRole('button', {name: '添加 2 个出口'}).click();
  const saved = await h.saved(d => ['lax', 'berlin'].every(id => memberOf(d, id).proxy?.exit), 'two exits');
  assert.deepEqual(['lax', 'berlin'].map(id => [memberOf(saved, id).proxy.exit.user, memberOf(saved, id).proxy.exit.port]), [['lax', 1081], ['berlin', 1081]]);
  assert.equal(saved.exitGroups.length, 0);
  await until(page, async () => (await texts(page.locator('.m3-rows-exits .m3-row-addr'))).every(a => /^100\.96\.0\.\d+:1081$/.test(a)), 'the exits at their mesh addresses');
  // A group of the US exits, by region.
  await page.getByRole('button', {name: '按地区建组'}).click();
  await page.getByRole('menuitem', {name: /US · 1 台/}).click();
  await h.saved(d => d.exitGroups.length === 1 && d.exitGroups[0].name === 'US' && d.exitGroups[0].members.join() === 'lax', 'the US group');
  const dialog = page.locator('.mesh3-dialog');
  await dialog.getByRole('checkbox', {name: 'Berlin'}).click();
  await h.saved(d => d.exitGroups[0].members.join() === 'lax,berlin', 'Berlin in the group too');
  await dialog.getByRole('button', {name: '完成'}).click();
  // A port on Home to the group, the lowest round trip first; on loopback, no username in its link.
  await page.getByRole('button', {name: '＋ 新建代理端口'}).click();
  await pickNode(page, dialog, '开在哪台节点', 'Home');
  await dialog.getByRole('radio', {name: '一个出口组'}).click();
  await dialog.getByRole('radio', {name: /^延迟最低/}).click();
  await dialog.getByRole('button', {name: '添加'}).click();
  const port = (await h.saved(d => memberOf(d, 'home').proxy?.ports?.length === 1, 'a proxy port')).members.find(m => m.serverId === 'home').proxy.ports[0];
  assert.deepEqual([port.port, Boolean(port.group), port.select, port.listen], [1080, true, 'latency', 'local']);
  assert.ok((await page.locator('.m3-rows-ports').innerText()).includes('US · 延迟最低'));
  // Revisions: any one back, as a new revision.
  await tab(page, '版本记录').click();
  await page.locator('.m3-revisions li').nth(2).getByRole('button', {name: '回到这一版'}).click();
  await page.locator('.el-message-box').getByRole('button', {name: '回到这一版'}).click();
  await until(page, async () => h.mesh.rollbacks.length === 1, 'the rollback');
  assert.equal(typeof h.mesh.rollbacks[0].revision, 'number');
  await h.close();
});

test('phone: the overview\'s route at the bottom, machines in two lines, a machine\'s sections across the top', async t => {
  const h = await setup(t, {v3: true, width: 390, height: 844});
  const {page} = h;
  await page.locator('.m3-canvas.is-phone .m3-pathbar').waitFor();
  // No classic board switch on a phone.
  assert.equal(await page.getByRole('radio', {name: '经典画板'}).count(), 0);
  await tab(page, '节点').click();
  assert.equal(await page.locator('.m3-row-head').count(), 0);
  await page.locator('.m3-row', {hasText: 'Home'}).click();
  await page.locator('.m3-node.is-phone .m3-node-nav').waitFor();
  assert.equal(await page.locator('.m3-node-side').count(), 0);
  await h.close();
});

test('the shell and the list pages: summary, 我想… tasks, the access preview and table, revisions as a table', async t => {
  // The rule's source names Home: a node with its flag.
  const h = await setup(t, {v3: true, allSubnets: true, init: mesh => { Object.assign(mesh.doc.rules[0].src, {all: false, members: ['home']}); }});
  const {page} = h;
  assert.ok((await page.locator('.m3-top').innerText()).includes('3 个节点 · 2 台中转'));
  // 我想… → 新建出口组: the exits page with a new group open.
  await page.locator('.m3-top').getByRole('button', {name: '我想…'}).click();
  await page.getByRole('menuitem', {name: /新建出口组/}).click();
  await until(page, async () => (await page.locator('.m3-tabs [aria-selected="true"]').innerText()).startsWith('出口和代理'), 'the exits page');
  // Access rules: the preview of one node, then every rule as a row.
  await tab(page, '访问规则').click();
  await pickNode(page, page, '预览哪个节点或局域网', 'Home', '节点');
  assert.ok((await page.locator('.m3-preview').innerText()).includes('它能访问'));
  // Nodes named in rules carry their flags.
  assert.deepEqual(await texts(page.locator('.m3-rows-rules .m3-side-names .sv-flag')), ['🇨🇳']);
  assert.equal(await page.locator('.m3-rows-rules > li:not([aria-hidden])').count(), 1);
  // Revisions: a table, the current one without the way back.
  await tab(page, '版本记录').click();
  await page.locator('.m3-rows-revs li[data-current]').waitFor();
  assert.equal(await page.locator('.m3-rows-revs li[data-current]').getByRole('button', {name: '回到这一版'}).count(), 0);
  await h.close();
});

test('shared LANs: a network another gateway shares is greyed and not picked, and a node with nothing else to share is left out', async t => {
  const h = await setup(t, {v3: true, init: mesh => {
    const by = id => mesh.doc.members.find(m => m.serverId === id);
    by('berlin').gateway = {subnets: ['192.168.50.0/24'], masquerade: true};
    by('home').subnetSuggestions = ['192.168.50.0/24', '10.30.0.0/24'];
    by('lax').subnetSuggestions = ['192.168.50.0/24'];
  }});
  const {page} = h;
  await tab(page, '共享局域网').click();
  const found = page.locator('.m3-rows-found');
  assert.deepEqual(plain(await texts(found.locator('.m3-row-name strong'))), ['Home']);
  assert.deepEqual(await texts(found.locator('.m3-taken')), ['192.168.50.0/24（已由 🇩🇪Berlin 共享）']);
  await found.getByRole('button', {name: '共享 Home 所在的局域网'}).click();
  const dialog = page.locator('.mesh3-dialog');
  assert.deepEqual(await texts(dialog.locator('.sv-chip')), ['10.30.0.0/24 ✕']);
  assert.ok(await dialog.getByRole('button', {name: /192\.168\.50\.0\/24 · 已由 Berlin 共享/}).isDisabled());
  await h.close();
});

test('a node\'s page: the route from it or to it, live; an idle line; events in pages; the tools laid out with their options', async t => {
  const h = await setup(t, {v3: true, init: mesh => {
    const now = Date.now();
    mesh.extra.peers.push({id: 'berlin', kind: 'relay', active: true, transport: 'tcp', endpoint: '198.51.100.6:52000', lastHandshakeUnixNano: String((now - 2 * 3600e3) * 1e6),
      rxBytes: 364, txBytes: 404, rttMs: 160, timeoutRate: 0});
    for (let i = 0; i < 45; i++) mesh.extra.events.push({seq: 100 + i, at: now - (i + 3) * 61000, kind: i % 2 ? 'peer.reconnect' : 'path.failed', detail: i % 2 ? 'lax' : `c${i}: probe timeout`});
  }});
  const {page} = h;
  await tab(page, '节点').click();
  await page.locator('.m3-row', {hasText: 'Home'}).click();
  // The route goes from this node by default, or to it; live keeps it fresh.
  await until(page, async () => h.calls.some(c => c.path === '/api/servers/mesh/path' && c.query.startsWith('?src=home&')), 'the route from Home');
  const route = page.getByRole('radiogroup', {name: '路线方向'});
  await route.getByRole('radio', {name: '到这台'}).click();
  await until(page, async () => h.calls.some(c => c.path === '/api/servers/mesh/path' && /&dst=home$/.test(c.query)), 'the route to Home');
  const before = h.calls.filter(c => c.path === '/api/servers/mesh/path').length;
  await page.getByRole('switch', {name: '实时刷新'}).click();
  await until(page, async () => h.calls.filter(c => c.path === '/api/servers/mesh/path').length > before, 'refreshed at once');
  // An old handshake on a line the node still measures: idle, not down.
  await page.locator('.m3-node-nav button', {hasText: '诊断'}).click();
  const diag = page.locator('#m3-sec-diag');
  const idle = diag.locator('tr', {has: page.locator('td', {hasText: '中转'})});
  await idle.waitFor();
  assert.ok((await idle.innerText()).includes('空闲 · 2 小时前握手'));
  assert.equal(await idle.locator('.ob-dot').getAttribute('data-tone'), 'ok');
  // Events: twenty to a page.
  await diag.getByRole('tab', {name: '事件'}).click();
  await diag.locator('.m3-pager').waitFor();
  assert.equal(await diag.locator('.m3-nitems > li').count(), 20);
  await diag.locator('.m3-pager .btn-next').click();
  await until(page, async () => (await diag.locator('.m3-nitems > li').count()) === 20 && (await diag.locator('.m3-pager .is-active').innerText()) === '2', 'page 2');
  // Tools: the options out, nothing to unfold.
  await diag.getByRole('tab', {name: '工具'}).click();
  await diag.locator('.net-target input').waitFor();
  assert.ok(await diag.locator('.net-advanced').evaluate(el => el.open && el.classList.contains('is-inline')));
  await h.close();
});

test('a node\'s name, wherever it is shown: its flag, and a click opens its page, not the row it sits in', async t => {
  const h = await setup(t, {v3: true, allSubnets: true, init: mesh => { Object.assign(mesh.doc.rules[0].src, {all: false, members: ['home']}); }});
  const {page} = h;
  await tab(page, '线路').click();
  const row = page.locator('.m3-rows-entries .m3-row', {hasText: 'Home'});
  assert.deepEqual(await texts(row.locator('.m3-row-conn .m3-node-name')), ['🇩🇪Berlin', '🇺🇸LAX']);
  // The relay's name opens the relay.
  await row.getByRole('button', {name: /Berlin$/}).click();
  await page.locator('.m3-node h2', {hasText: 'Berlin'}).waitFor();
  // Elsewhere on the row, the member; and by the keyboard too.
  await page.getByRole('button', {name: '返回节点列表'}).click();
  await tab(page, '线路').click();
  await row.locator('.m3-row-addr').click({position: {x: 2, y: 2}});
  await page.locator('.m3-node h2', {hasText: 'Home'}).waitFor();
  await page.getByRole('button', {name: '返回节点列表'}).click();
  await tab(page, '线路').click();
  await row.focus();
  await page.keyboard.press('Enter');
  await page.locator('.m3-node h2', {hasText: 'Home'}).waitFor();
  // An access rule's row opens the rule; Home's name in it opens Home.
  await page.getByRole('button', {name: '返回节点列表'}).click();
  await tab(page, '访问规则').click();
  await page.locator('.m3-rows-rules .m3-node-name', {hasText: 'Home'}).click();
  await page.locator('.m3-node h2', {hasText: 'Home'}).waitFor();
  await h.close();
});

test('auto-routing: connection limits and violation errors, upstreams & direct switch, auto links filter/switch/lastChange, auto revision badge, overview +N', async t => {
  const h = await setup(t, {v3: true, lines: true, init: mesh => {
    mesh.doc.relayLinks = {mode: 'custom', pairs: [
      {a: 'berlin', b: 'lax', transport: 'udp', cost: 1, dialer: 'a', origin: 'auto', reason: '探测发现 Berlin 与 LAX 直连比绕行快 55 ms（实测 142 ms），已自动建立跨区中转线路', since: Date.now() - 120000},
    ]};
  }});
  const {page} = h;

  // Overview: Home is in upstreams mode with 2 upstreams (Berlin, LAX); drawn under first upstream Berlin with "+1".
  await page.getByRole('button', {name: '全部展开'}).click();
  const homeChip = page.locator('.m3-chip', {hasText: 'Home'});
  await homeChip.waitFor();
  assert.ok((await homeChip.innerText()).includes('+1'));

  // Node detail (Home): its entry relays used all at once (同时连上), without order numbers; the switch letting the mesh add one, and an auto one with its reason & 转成手动.
  await tab(page, '节点').click();
  await page.locator('.m3-row', {hasText: 'Home'}).click();
  const connectSec = page.locator('#m3-sec-connect');
  assert.ok((await connectSec.innerText()).includes('入口中转'));
  assert.equal(await connectSec.getByRole('radiogroup', {name: '怎么用这些入口'}).getByRole('radio', {name: '同时连上'}).getAttribute('aria-checked'), 'true');
  assert.equal(await connectSec.getByRole('button', {name: '上移'}).count(), 0);
  assert.ok((await connectSec.innerText()).includes('实测 152 ms'));
  assert.ok((await connectSec.innerText()).includes('去 LAX 比经 Berlin 快 42 ms'));
  // Convert auto upstream c2 to manual: removes origin/reason/since.
  await connectSec.getByRole('button', {name: '转成手动'}).click();
  const afterManual = await h.saved(d => !memberOf(d, 'home').entry.candidates[1].origin, 'upstream converted to manual');
  assert.equal('reason' in memberOf(afterManual, 'home').entry.candidates[1], false);
  // Toggle the switch letting the mesh add entries (member.direct).
  await connectSec.getByRole('switch', {name: '允许自动加入口'}).click();
  await h.saved(d => memberOf(d, 'home').direct === false, 'member direct off');

  // Lines page: autoLinks switch & lastChange, origin tags (自动/手动) & 自动建立 filter, connection limits & violation errors.
  await tab(page, '线路').click();
  const lastLog = page.locator('.m3-auto-log', {hasText: '最近一次调整'});
  assert.ok((await lastLog.innerText()).includes('为 Home 新增上游 LAX'));
  assert.ok((await page.locator('.m3-auto-log', {hasText: '正在观察'}).innerText()).includes('暂时没有更快的候选'));
  await page.getByRole('switch', {name: '自动调整线路'}).click();
  await h.saved(d => d.settings.autoLinks?.enabled === false, 'autoLinks disabled');
  assert.ok((await page.locator('.m3-autolinks-bar').innerText()).includes('不测速，也不自动改线路'));
  assert.equal(await page.locator('.m3-auto-log', {hasText: '正在观察'}).count(), 0);

  // Filter by 自动建立: 1 relay link (Berlin ⇄ LAX) has origin: 'auto'; without the optimizer's measurements it says why it was made.
  await page.getByRole('button', {name: /^自动建立/}).click();
  const lineRow = page.locator('.m3-rows-lines .m3-row').first();
  assert.ok((await lineRow.innerText()).includes('自动'));
  assert.ok((await lineRow.innerText()).includes('建立时：探测发现 Berlin 与 LAX 直连'));

  // Connection limits: create a limit, test limit_without_condition and limit_violation on entry & relay link.
  await page.getByRole('button', {name: '＋ 新建限制'}).click();
  const dialog = page.locator('.mesh3-dialog');
  await dialog.getByRole('textbox', {name: '限制名称'}).fill('跨境走 TLS');
  await dialog.getByRole('textbox', {name: '限制名称'}).dispatchEvent('change');
  await h.saved(d => d.limits?.[0]?.name === '跨境走 TLS', 'limit created');
  assert.ok((await dialog.innerText()).includes('现在涉及 3 条连接'));
  // Uncheck relayEnd without other conditions -> backend rejects with limit_without_condition on limits[0].
  await dialog.getByRole('checkbox', {name: /至少一端是中转/}).uncheck();
  await until(page, async () => (await dialog.innerText()).includes('至少要勾一项跨界条件'), 'limit_without_condition shown');
  // Check 只允许这些协议 (defaults to TCP, TLS) and 只允许手动建立 -> Berlin ⇄ LAX (UDP, auto) and Home → Berlin (UDP) violate it!
  await dialog.getByRole('checkbox', {name: '只允许这些协议'}).check();
  await dialog.getByRole('checkbox', {name: /只允许手动建立/}).check();
  await until(page, async () => (await page.locator('.m3-rows-lines').innerText()).includes('违反连接限制「跨境走 TLS」'), 'violation shown on relay link');
  assert.ok((await page.locator('.m3-rows-entries').innerText()).includes('违反连接限制「跨境走 TLS」'));
  await dialog.getByRole('button', {name: '完成'}).click();
  // Switching the limit off allows saving cleanly, and draft changes list 修改连接限制.
  await page.getByRole('switch', {name: '启用限制 跨境走 TLS'}).click();
  await h.saved(d => d.limits?.[0]?.on === false, 'limit turned off');
  await page.locator('.mesh-bar').getByRole('button', {name: '查看全部'}).click();
  assert.ok((await page.locator('.mesh-bar').innerText()).includes('修改连接限制'));

  // Revisions: reason with 自动 shows the 自动 tag.
  await tab(page, '版本记录').click();
  await page.locator('.m3-revisions li .m3-tag', {hasText: '自动'}).waitFor();
  await h.close();
});

test('own routes: from a route seen on the board, laid out with the links it lacks, the best ten, order, off and delete, states', async t => {
  const h = await setup(t, {v3: true, lines: true, init: mesh => {
    mesh.doc.relayLinks = {mode: 'custom', pairs: []};
    // Home enters by LAX alone, one entry at a time; SG is a third relay linked to nothing.
    mesh.doc.members[2].entry = {mode: 'failover', autoReturn: true, candidates: [{id: 'c2', relay: 'lax', transport: 'tcp'}]};
    mesh.doc.members.push({...JSON.parse(JSON.stringify(mesh.doc.members[0])), serverId: 'sg', name: 'SG', address: '100.96.0.9'});
  }});
  const {page} = h;

  // 1. A route on the board through a relay becomes an own route: its source's page, the editor laid out as it goes.
  h.mesh.setPath((src, dst) => ({hops: [{node: src, via: ''}, {node: 'berlin', via: 'entry', rttMs: 12}, {node: dst, via: 'relay', rttMs: 140}], rttMs: 152, stale: false}));
  await pickNode(page, page, '起点', 'Home');
  await pickNode(page, page, '终点', 'LAX');
  await until(page, async () => (await page.locator('.m3-ppanel').innerText()).includes('加为自建路线'), 'add button shown on the board route');
  await page.locator('.m3-ppanel').getByRole('button', {name: '加为自建路线'}).click();
  const dialog = page.locator('.mesh3-dialog', {hasText: '自己搭一条路线'});
  await dialog.waitFor();
  // Each link measured as it is laid out: not linked yet, so measured now from one end; the sum below.
  await until(page, async () => (await dialog.locator('.m3-rt-total').innerText()).includes('合计'), 'the route measured');
  assert.deepEqual(plain(await texts(dialog.locator('.m3-rt-link'))), ['↓ 保存后：Berlin 加为 Home 的入口中转，Home 改为「同时连上」 · 直测 38 ms', '↓ 保存后：新建 Berlin 和 LAX 之间的中转线路 · 直测 38 ms']);
  assert.match((await dialog.locator('.m3-rt-total').innerText()).replace(/\s+/g, ''), /^合计76ms（其中2段是刚刚直测的，应用后以实测为准）/);
  // Through SG instead: measured again as the route changes.
  await dialog.getByRole('button', {name: '去掉 Berlin'}).click();
  await pickNode(page, dialog, '插入一台中转', 'SG');
  await until(page, async () => (await texts(dialog.locator('.m3-rt-link'))).join().includes('SG 和 LAX 之间的中转线路 · 直测'), 'measured again through SG');
  assert.deepEqual(plain(await texts(dialog.locator('.m3-rt-link'))), ['↓ 保存后：SG 加为 Home 的入口中转，Home 改为「同时连上」 · 直测 38 ms', '↓ 保存后：新建 SG 和 LAX 之间的中转线路 · 直测 38 ms']);
  await dialog.getByRole('button', {name: '去掉 SG'}).click();
  await pickNode(page, dialog, '插入一台中转', 'Berlin');
  await dialog.getByRole('button', {name: '存入草稿'}).click();
  const first = await h.saved(d => d.paths?.length === 1, 'route saved with its links');
  assert.deepEqual({from: first.paths[0].from, to: first.paths[0].to, via: first.paths[0].via, on: first.paths[0].on}, {from: 'home', to: 'lax', via: ['berlin'], on: true});
  const home = memberOf(first, 'home');
  assert.equal(home.entry.mode, 'upstreams');
  assert.deepEqual(home.entry.candidates.map(c => [c.relay, c.auto || null]), [['lax', null], ['berlin', ['transport']]]);
  assert.deepEqual(first.relayLinks.pairs.map(p => [p.a, p.b, p.auto]), [['berlin', 'lax', ['transport', 'dialer', 'bias']]]);

  // 2. On Home's page: the pair with its route, not applied yet.
  const pair = page.locator('.m3-rt-pair', {hasText: 'LAX'});
  await pair.waitFor();
  assert.match(plain([await pair.locator('.m3-rt-list > li').first().innerText()])[0].replace(/\s+/g, ' '), /^1 Home → ?Berlin → ?LAX 未应用/);

  // 3. The best ten from Home to LAX: the own route marked, the one taken now, the loss on a link; one more becomes the pair's second.
  h.mesh.setBest(() => ({
    routes: [{nodes: ['home', 'berlin', 'lax'], segments: [{a: 'home', b: 'berlin', exists: true, rttMs: 12}, {a: 'berlin', b: 'lax', exists: true, rttMs: 140}], rttMs: 152, own: 1},
      {nodes: ['home', 'lax'], segments: [{a: 'home', b: 'lax', exists: true, rttMs: 150}], rttMs: 150, own: null},
      {nodes: ['home', 'sg', 'lax'], segments: [{a: 'home', b: 'sg', exists: true, rttMs: 20}, {a: 'sg', b: 'lax', exists: true, rttMs: 140, loss: 0.02}], rttMs: 160, own: null}],
    own: [{id: first.paths[0].id, rank: 1, on: true, nodes: ['home', 'berlin', 'lax'], segments: [], rttMs: 152, place: 1}]}));
  await pickNode(page, page, '路线另一端', 'LAX中转');
  await page.getByRole('button', {name: '计算前 10 条'}).click();
  const best = page.locator('.m3-rt-best > li');
  await until(page, async () => await best.count() === 3, 'the best routes listed');
  assert.match(await best.nth(0).innerText(), /自建第 1 条/);
  assert.equal(await best.nth(1).getByRole('button', {name: '加为自建'}).count(), 0);
  assert.match(await best.nth(2).locator('[data-loss]').innerText(), /丢包 2%/);
  await best.nth(2).getByRole('button', {name: '加为自建'}).click();
  const box = page.locator('.el-message-box');
  assert.match(await box.innerText(), /SG 加为 Home 的入口中转；新建 SG 和 LAX 之间的中转线路/);
  await box.getByRole('button', {name: '加为自建路线'}).click();
  await h.saved(d => d.paths.length === 2 && d.paths[1].via.join() === 'sg' && d.relayLinks.pairs.length === 2, 'the second route of the pair');

  // 4. Edit the pair: the second goes first, the first goes off, then away.
  await pair.getByRole('button', {name: '编辑'}).click();
  await pair.getByRole('button', {name: '第 2 条往前'}).click();
  await h.saved(d => d.paths[0].via.join() === 'sg' && d.paths[1].via.join() === 'berlin', 'the pair reordered');
  await pair.locator('.m3-rt-list > li').first().getByRole('button', {name: '停用'}).click();
  await h.saved(d => d.paths[0].on === false, 'the first route off');
  assert.match(await pair.locator('.m3-rt-list > li').first().innerText(), /已停用/);
  await pair.locator('.m3-rt-list > li').first().getByRole('button', {name: '删除'}).click();
  await page.locator('.el-message-box').getByRole('button', {name: '删除'}).click();
  await h.saved(d => d.paths.length === 1 && d.paths[0].via.join() === 'berlin', 'the route deleted');
  await page.locator('.mesh-bar').getByRole('button', {name: '查看全部'}).click();
  assert.ok((await page.locator('.mesh-bar').innerText()).includes('修改自建路线'));

  // 5. On the board, a pair on its own route says which one, and offers no more.
  h.mesh.setPath((src, dst) => ({hops: [{node: src, via: ''}, {node: 'berlin', via: 'entry', rttMs: 12}, {node: dst, via: 'relay', rttMs: 140}], rttMs: 152, stale: false,
    fixed: {id: 'p1', pathId: 'p1', back: false, rank: 1, nodes: ['home', 'berlin', 'lax'], state: 'pinned', since: Date.now() - 60000}}));
  await tab(page, '概览').click();
  await pickNode(page, page, '起点', 'Home');
  await pickNode(page, page, '终点', 'LAX');
  await until(page, async () => (await page.locator('.m3-ppanel').innerText()).includes('按自建路线第 1 条'), 'own route state on the board');
  assert.equal(await page.locator('.m3-ppanel').getByRole('button', {name: '加为自建路线'}).count(), 0);
  await h.close();
});


test('lines page: the last automatic change a line each with its node pairs, what it watches, lines grouped with the rest folded, a line set by hand only labelled', async t => {
  const h = await setup(t, {v3: true, lines: true, init: linesAtWork});
  const {page} = h;
  await tab(page, '线路').click();

  // The last change: one line per link with both ends' flags, how many pairs got faster and the most of them, all of them on asking.
  const [log, watch] = [page.locator('.m3-auto-log', {hasText: '最近一次调整'}), page.locator('.m3-auto-log', {hasText: '正在观察'})];
  assert.match(plain([await log.locator('.m3-auto-h').innerText()])[0], /新建 1 条 · 拆除 1 条/);
  const [added, dropped] = plain(await texts(log.locator('.m3-auto-item')));
  assert.match(added, /^新建 Berlin ⇄ ?LAX 中转线路 让 2 对节点变快，最多： ?Home → ?R2 300 → 200 ms 查看 2 对$/);
  assert.match(dropped, /^拆除 LAX ⇄ ?R5 中转线路 连续无改善$/);
  assert.equal(await log.locator('.m3-auto-item').first().locator('.sv-flag').count(), 4);
  await log.getByRole('button', {name: '查看 2 对'}).click();
  assert.deepEqual(plain(await texts(log.locator('.m3-gains li'))).map(s => s.replace(/\s+/g, ' ')), ['Home → R2 300 → 200 ms −100 ms', 'Home → R3 280 → 230 ms −50 ms']);
  // What it watches: an upstream reads member → relay, with its rounds.
  assert.match(plain([await watch.innerText()])[0].replace(/\s+/g, ' '), /2\/3 轮 Home → ?R1 入口中转 预计让 1 对节点变快/);

  // Lines by what they want: the automatic one open, the 20 others folded under their count, the whole head the switch.
  assert.deepEqual((await texts(page.locator('.m3-rows-group b'))), ['自动建立', '其余线路']);
  assert.equal(await page.locator('.m3-rows-lines .m3-row').count(), 1);
  const rest = page.locator('.m3-rows-group-btn');
  assert.equal(await rest.getAttribute('aria-expanded'), 'false');
  assert.match((await rest.innerText()).replace(/\s+/g, ' '), /其余线路 20 条 · 运行正常 ?展开/);
  await rest.click();
  assert.equal(await page.locator('.m3-rows-lines .m3-row').count(), 21);
  assert.equal(await rest.getAttribute('aria-expanded'), 'true');
  // The automatic one: its node pairs below it, and why it was made.
  const autoLine = page.locator('.m3-rows-lines > li[data-group="auto"]:not(.m3-rows-group)');
  await autoLine.getByRole('button', {name: '查看 2 对', exact: true}).click();
  assert.equal(await autoLine.locator('.m3-line-more .m3-gains li').count(), 2);
  assert.ok((await autoLine.locator('.m3-line-more').innerText()).includes('建立时：让 Home → R2'));
  // A line set by hand is only labelled so, nothing urged: no warning, no going back to the automatic values.
  const byHand = page.locator('.m3-rows-lines .m3-row', {hasText: 'R1'}).filter({hasText: '手动设置'});
  assert.equal(await byHand.locator('.m3-tag', {hasText: '手动设置 2 项'}).getAttribute('title'), '手动设置：协议 TCP、偏好 少走 +100 ms');
  assert.equal(await page.locator('.m3-rows-lines', {hasText: '可优化'}).count(), 0);
  assert.equal(await page.getByRole('button', {name: '改回自动'}).count(), 0);
  await page.getByRole('button', {name: /^手动设置/}).click();
  assert.equal(await page.locator('.m3-rows-lines .m3-row').count(), 1);
  await h.close();
});

test('a member on one entry keeps it until moved: on its page any way and back, or all at once on the lines page after a confirmation', async t => {
  const h = await setup(t, {v3: true, lines: true, init: mesh => { mesh.doc.members[2].entry.mode = 'failover'; }});
  const {page} = h;
  const homeEntry = d => memberOf(d, 'home').entry;
  const before = homeEntry(h.mesh.doc);

  // The lines page counts it and asks before moving every such member.
  await tab(page, '线路').click();
  const bar = page.locator('.m3-autolinks-bar', {hasText: '一次只用一个入口'});
  await until(page, async () => (await bar.innerText()).includes('1 台成员一次只用一个入口中转'), 'single-entry members counted');
  await bar.getByRole('button', {name: '全部改为同时连上'}).click();
  await page.locator('.el-message-box').getByRole('button', {name: '取消'}).click();
  await page.locator('.el-message-box').waitFor({state: 'hidden'});
  assert.equal(homeEntry(h.mesh.doc).mode, 'failover');

  // The node's page: one of the three ways picked there, its entry relays as they were.
  await tab(page, '节点').click();
  await page.locator('.m3-row', {hasText: 'Home'}).click();
  const connectSec = page.locator('#m3-sec-connect');
  const modes = connectSec.getByRole('radiogroup', {name: '怎么用这些入口'});
  assert.equal(await modes.getByRole('radio', {name: '主备'}).getAttribute('aria-checked'), 'true');
  await modes.getByRole('radio', {name: '同时连上'}).click();
  const saved = await h.saved(d => homeEntry(d).mode === 'upstreams', 'home moved to upstreams');
  assert.deepEqual(homeEntry(saved).candidates, before.candidates);
  assert.equal(await connectSec.getByRole('button', {name: '上移'}).count(), 0);
  // And back: one at a time again, the order back in sight.
  await modes.getByRole('radio', {name: '主备'}).click();
  assert.equal((await h.saved(d => homeEntry(d).mode === 'failover', 'home back to failover')).members.find(x => x.serverId === 'home').entry.autoReturn, true);
  assert.ok(await connectSec.getByRole('button', {name: '上移'}).count() > 0);
  await modes.getByRole('radio', {name: '同时连上'}).click();
  await until(page, async () => homeEntry(h.mesh.doc).mode === 'upstreams', 'home on all at once again');

  // Nothing left on one entry: the lines page says nothing more.
  await tab(page, '线路').click();
  await page.locator('.m3-autolinks-bar').first().waitFor();
  assert.equal(await page.locator('.m3-autolinks-bar', {hasText: '一次只用一个入口'}).count(), 0);
  await h.close();
});
