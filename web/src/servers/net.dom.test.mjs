// Server network without a browser: addresses, ping / tcping / trace on the detail page and in the
// mesh member panel, 动态域名 — what the page asks the backend, what it shows and how it switches,
// in happy-dom with the browser tests' fake backend (testing/netBackend.mjs, mounted by
// testing/netDom.mjs). The layout of the same scenarios stays in net.*.browser.test.mjs.
import test from 'node:test';
import assert from 'node:assert/strict';
import {setup, clipboard} from './testing/netDom.mjs';
import {ADDRESSES, GB, GEO, MESH_ADDR, SERVERS, addresses, fakeMesh, fakeNet, press, probe, rowTexts, run, server, waitFor} from './testing/netBackend.mjs';

test('addresses on the detail page: grouped by interface, tags, the exit behind NAT, copy, refresh; offline and too old', async t => {
  const h = await setup(t);
  const box = addresses(h.page);
  await box.locator('.net-iface').first().waitFor();
  const groups = await box.locator('.net-iface').evaluateAll(list => list.map(el => el.innerText.replace(/\s+/g, ' ').trim()));
  assert.deepEqual(groups, ['eth0 10.10.10.251/24 私有 2001:db8:10::251/64 公网 IPv6', 'obmesh0 组网 100.96.0.3/16 100.96.255.3/24 控制', 'wg0 隧道 未启用 100.64.12.5/10 CGNAT', 'eth1 169.254.20.7/16 链路本地']);
  assert.match(await box.locator('.net-exit:not([data-proxy])').innerText(), /公网出口\s*192\.0\.2\.117\s*国内服务看到的地址，不在本机网卡上/);
  // Traffic abroad leaves from a proxy: that exit too, said so.
  assert.match(await box.locator('.net-exit[data-proxy]').innerText(), /出国出口\s*192\.0\.2\.92\s*经代理\s*国外服务看到的地址：去国外的流量经过代理/);
  // Addresses come after the system and the bill; the test card after the server's other cards, across the page.
  const order = await h.page.locator('.sv-detail:visible').locator('.sv-cards > section, section.net-probe').evaluateAll(list => list.map(el => el.querySelector('h3')?.textContent || el.getAttribute('aria-label')));
  assert.deepEqual(order.slice(0, 3), ['系统', '账单', '网络地址']);
  assert.ok(order.indexOf('网络测试') > order.indexOf('动态域名') && order.indexOf('网络测试') < order.indexOf('浏览器'), order.join(' '));
  // A tap copies the address (without its prefix length) and says so.
  await box.getByRole('button', {name: /10\.10\.10\.251\/24/}).click();
  await h.page.locator('.el-message', {hasText: '已复制 10.10.10.251'}).waitFor();
  assert.equal(await h.page.evaluate(() => navigator.clipboard.readText()), '10.10.10.251');
  const reads = h.of('GET', /\/home\/net\/addresses$/).length;
  assert.equal(reads, 1, 'read once when opened');
  await box.getByRole('button', {name: '刷新'}).click();
  await waitFor(() => h.of('GET', /\/home\/net\/addresses$/).length === 2, 'a refresh');
  await h.close();

  // Too old a node, and an offline one (not even asked).
  const old = await setup(t, {tabs: ['old', 'win'], active: 'old'});
  await addresses(old.page).getByText('节点版本过旧，升级节点后可用').waitFor();
  await old.page.locator('.sv-tab', {hasText: 'Win-Box'}).click();
  const win = old.page.locator('.sv-detail').nth(1);
  await win.locator('section.net-addresses').getByText('节点离线').waitFor();
  assert.equal(old.of('GET', /\/win\/net\/addresses$/).length, 0);
  assert.equal(await win.locator('section.net-probe .net-go').isDisabled(), true);
  await win.locator('section.net-probe').getByText('节点离线，上线后才能测试。').waitFor();
  // Starting a test on a node too old for it.
  await old.page.locator('.sv-tab', {hasText: 'Old-VPS'}).click();
  await probe(old.page).getByRole('combobox', {name: '目标'}).fill('example.com');
  await probe(old.page).locator('.net-go').click();
  await probe(old.page).getByText('节点版本过旧，升级节点后可用。').waitFor();
  await old.close();
});

test('the form: TCPing needs a port, targets checked, ranges per mode, the controller naming a field, busy', async t => {
  const h = await setup(t);
  const root = probe(h.page);
  await root.locator('.net-form').waitFor();
  // Defaults: Ping. The detail page is wide: mode, target and start on one line, the options out under it, nothing to unfold.
  assert.equal(await root.getByRole('radio', {name: 'Ping', exact: true}).getAttribute('aria-checked'), 'true');
  assert.equal(await root.locator('details.net-advanced').evaluate(el => el.open), true);
  assert.equal(await root.locator('details.net-advanced > summary').isVisible(), false);
  await root.locator('.net-go').click();
  await root.getByText('请填写目标').waitFor();
  await root.getByRole('radio', {name: 'TCPing'}).click();
  const port = root.getByRole('textbox', {name: '端口'});
  await port.waitFor();
  await root.getByRole('combobox', {name: '目标'}).fill('example.com');
  await root.locator('.net-go').click();
  await root.getByText('请填写端口').waitFor();
  await port.fill('99999');
  await root.locator('.net-go').click();
  await root.getByText('端口应为 1–65535').waitFor();
  await root.getByRole('combobox', {name: '目标'}).fill('http://exa mple.com');
  await root.locator('.net-go').click();
  await root.getByText('只填主机名或 IP，例如 example.com 或 1.1.1.1').waitFor();
  await root.getByRole('combobox', {name: '目标'}).fill('例子.中国');
  await root.locator('.net-go').click();
  await root.getByText('中文域名请换成 xn-- 开头的 Punycode 形式，或直接填 IP').waitFor();
  // The port field goes with TCPing; trace counts rounds, up to 10.
  await root.getByRole('radio', {name: '路由追踪'}).click();
  assert.equal(await port.count(), 0);
  await root.getByRole('combobox', {name: '目标'}).fill('example.com');
  await root.getByRole('textbox', {name: '轮数'}).fill('11');
  await root.getByRole('textbox', {name: '间隔（毫秒）'}).fill('50');
  await root.locator('.net-go').click();
  await root.getByText('轮数应为 1–10').waitFor();
  await root.getByText('应为 200–10000 毫秒').waitFor();
  assert.equal(h.of('POST', /\/net\/probes$/).length, 0, 'nothing is sent while the form is wrong');
  // The suggestions: the other servers' public addresses, then (home is a member) the mesh addresses, named.
  await root.getByRole('combobox', {name: '目标'}).fill('');
  if (!(await h.page.locator('.ob-suggest-list').isVisible())) await root.getByRole('combobox', {name: '目标'}).press('ArrowDown');
  const list = h.page.locator('.ob-suggest-list');
  await list.waitFor();
  const opts = await list.locator('li').evaluateAll(l => l.map(li => li.innerText.replace(/\s+/g, ' ').trim()));
  assert.deepEqual(opts.slice(0, 2), ['192.0.2.34 TOY', '203.0.113.7 VLINE']);
  assert.ok(opts.includes('2001:db8:7::7 VLINE') && opts.includes('100.96.0.2 LAX02 · 组网') && !opts.some(o => o.includes('192.0.2.117')), opts.join(' | '));
  await root.getByRole('combobox', {name: '目标'}).fill('lax');
  assert.deepEqual(await list.locator('li').evaluateAll(l => l.map(li => li.innerText.replace(/\s+/g, ' ').trim())), ['45.76.0.2 LAX02', '100.96.0.2 LAX02 · 组网'], 'matched by name too');
  await list.locator('li', {hasText: '45.76.0.2'}).click();
  assert.equal(await root.getByRole('combobox', {name: '目标'}).inputValue(), '45.76.0.2');
  // The controller turns a field down, or the node is busy.
  await root.getByRole('textbox', {name: '轮数'}).fill('');
  await root.getByRole('textbox', {name: '间隔（毫秒）'}).fill('');
  await root.getByRole('radio', {name: 'Ping', exact: true}).click();
  await root.getByRole('combobox', {name: '目标'}).fill('busy.example');
  await root.locator('.net-go').click();
  await root.getByText('这台服务器上已经有 4 个测试在运行，请等其中一个结束后再试。').waitFor();
  // Family against the address: caught here for a literal, by the controller for a name; shown under the choice, the options opened.
  const details = root.locator('details.net-advanced');
  if (!(await details.evaluate(el => el.open))) await details.locator('summary').click();
  await root.getByRole('radio', {name: 'IPv4'}).click();
  const famError = root.locator('.net-family-field .ob-bad');
  await root.getByRole('combobox', {name: '目标'}).fill('2001:db8::5');
  const posts = h.of('POST', /\/net\/probes$/).length;
  await root.locator('.net-go').click();
  assert.equal(await details.evaluate(el => el.open), true, 'opened to show the message');
  assert.equal(await root.getByRole('radio', {name: 'IPv4'}).getAttribute('aria-checked'), 'true');
  assert.match(await famError.innerText(), /协议和目标地址不符/);
  assert.equal(h.of('POST', /\/net\/probes$/).length, posts, 'not sent');
  await root.getByRole('radio', {name: '自动'}).click();
  assert.equal(await famError.count(), 0, 'gone once the choice changes');
  await root.getByRole('radio', {name: 'IPv4'}).click();
  await root.getByRole('combobox', {name: '目标'}).fill('v6only.example');
  await root.locator('.net-go').click();
  await famError.waitFor();
  assert.equal(await details.evaluate(el => el.open), true);
  assert.equal(h.of('POST', /\/net\/probes$/).at(-1).body.family, 4);
  await h.close();
});

test('ping and tcping: polled every second, rows with timeouts, refusals and unreachables, the summary, stop', async t => {
  const h = await setup(t);
  const root = probe(h.page);
  await root.locator('.net-form').waitFor();
  const pid = await run(h, {target: 'example.com', count: '30'});
  await root.locator('.net-results > li').nth(3).waitFor();
  assert.equal(await root.locator('.net-go').innerText(), '停止');
  assert.equal(await root.getByRole('combobox', {name: '目标'}).isDisabled(), true, 'the form is locked while it runs');
  // About one read a second.
  const a = h.polls(pid); await h.page.waitForTimeout(3100); const b = h.polls(pid);
  assert.ok(b - a >= 2 && b - a <= 4, `${b - a} reads in 3 seconds`);
  const rows = await rowTexts(root);
  assert.equal(rows[0], '1 4.6 ms TTL 54', 'below 10 ms with one decimal');
  assert.equal(rows[2], '3 超时');
  assert.match(await root.locator('.net-result-head').innerText(), /example\.com（93\.184\.216\.34）\s*·\s*IPv4\s*测试中/);
  assert.match(await root.locator('.net-summary').innerText(), /发送\s*\d+\s*收到\s*\d+\s*丢包\s*[\d.]+%\s*最小\s*4\.6 ms\s*平均\s*\d+ ms\s*最大\s*\d+ ms\s*抖动\s*[\d.]+ ms/);
  // Stop: one DELETE, the last state read back, then no more reads.
  await root.locator('.net-go').click();
  await root.locator('.net-result-head .pb-tag', {hasText: '已停止'}).waitFor();
  assert.equal(h.stops(pid), 1);
  const c = h.polls(pid); await h.page.waitForTimeout(2200);
  assert.equal(h.polls(pid), c, 'no reads after it stopped');
  assert.equal(await root.locator('.net-go').innerText(), '开始');

  // Over 10 results: the latest 10, all on a tap. Done by itself at the count.
  const done = await run(h, {target: '1.1.1.1', count: '12'});
  await root.locator('.net-result-head .pb-tag', {hasText: '已完成'}).waitFor({timeout: 20000});
  assert.equal(h.stops(done), 0, 'a finished test is not stopped');
  assert.equal(await root.locator('.net-results > li').count(), 10);
  assert.equal((await rowTexts(root))[0], '3 超时');
  await root.getByRole('button', {name: '显示全部 12 次'}).click();
  assert.equal(await root.locator('.net-results > li').count(), 12);

  // TCPing: refused and unreachable, no TTL.
  await run(h, {mode: 'TCPing', target: 'example.com', port: '443', count: '5'});
  await root.locator('.net-result-head .pb-tag', {hasText: '已完成'}).waitFor({timeout: 15000});
  assert.deepEqual(await rowTexts(root), ['1 4.6 ms', '2 拒绝', '3 13 ms', '4 不可达', '5 14 ms']);
  // On the detail page: the summary as tiles, the loss coloured, each try a bar (a failed one red, full height).
  assert.equal(await root.locator('.net-summary.is-tiles > div').count(), 7);
  assert.equal(await root.locator('.net-summary > div', {hasText: '丢包'}).getAttribute('data-tone'), 'bad');
  assert.equal(h.of('POST', /\/net\/probes$/).at(-1).body.port, 443);
  assert.match(await root.locator('.net-result-head').innerText(), /端口 443/);
  assert.match(await root.locator('.net-summary').innerText(), /发送\s*5\s*收到\s*3\s*丢包\s*40%/);
  // A name that does not resolve.
  await run(h, {mode: 'Ping', target: 'nxdomain.invalid', count: ''});
  const failed = root.locator('.net-failed');
  await failed.getByText('解析域名失败').waitFor();
  assert.equal(await failed.locator('small').innerText(), 'lookup nxdomain.invalid: no such host', 'the reason in small print');
  await root.locator('.net-result-head .pb-tag', {hasText: '出错'}).waitFor();
  await run(h, {mode: 'Ping', target: 'noicmp.example'});
  await failed.getByText('这台机器上无法发送 ping').waitFor();
  assert.equal(await failed.locator('small').innerText(), 'socket: operation not permitted');
  // A router on the way answering "time exceeded"; another error as the system says it.
  await run(h, {mode: 'Ping', target: 'far.example', count: '3'});
  await root.locator('.net-result-head .pb-tag', {hasText: '已完成'}).waitFor({timeout: 15000});
  assert.deepEqual(await rowTexts(root), ['1 4.6 ms TTL 54', '2 中途超时', '3 no route to host']);
  assert.equal(await root.locator('.net-results > li[data-fail]').count(), 2);
  await h.close();
});

test('trace: hops in a table, no answer as *, several addresses on a hop, places filled in without moving rows', async t => {
  const h = await setup(t);
  const root = probe(h.page);
  await root.locator('.net-form').waitFor();
  await run(h, {mode: '路由追踪', target: 'example.com'});
  const table = root.locator('table.net-trace');
  await table.locator('tbody tr').nth(2).waitFor();
  // While places are looked up: a placeholder of the same height.
  await table.locator('tbody tr').nth(3).waitFor();
  const waiting = table.locator('.net-geo-wait');
  await waiting.first().waitFor();
  const row4 = table.locator('tbody tr').nth(3);
  await table.locator('tbody tr').nth(5).locator('.net-trace-geo', {hasText: 'AS15133'}).waitFor({timeout: 10000});
  assert.equal(await waiting.count(), 0);
  const cells = await table.locator('tbody tr').evaluateAll(list => list.map(tr => [...tr.cells].map(td => td.innerText.replace(/\s+/g, ' ').trim()).join(' | ')));
  assert.deepEqual(cells, [
    '1 | 10.10.10.1 gateway.lan | 私有地址 | 0% | 0.4 ms | 0.3 ms | 0.6 ms',
    '2 | * | * | 100% | * | * | *',
    '3 | 100.64.0.1 | CGNAT | 0% | 3 ms | 2.9 ms | 3.1 ms',
    '4 | 203.0.113.1 ae-1.r20.tokyjp05.jp.bb.gin.ntt.net 203.0.113.9 | 🇯🇵 AS2914 NTT America, Inc. 🇯🇵 AS2914 NTT America, Inc. | 0% | 21 ms | 21 ms | 21 ms',
    '5 | 198.51.100.77 core.example.jp | 🇯🇵 AS2516 KDDI CORPORATION | 0% | 48 ms | 48 ms | 48 ms',
    '6 | 93.184.216.34 | 🇺🇸 AS15133 Edgecast Inc. | 0% | 151 ms | 150 ms | 151 ms']);
  // Two addresses on hop 4: each on its own line, in step with its place.
  await root.locator('.net-result-head .pb-tag', {hasText: '已完成'}).waitFor({timeout: 15000});

  // A hop not tried yet while it runs is a placeholder, not "0%"; a router answering "unreachable" is listed but is not the end.
  await run(h, {mode: '路由追踪', target: 'gap.example'});
  await table.locator('tr[data-pending]').waitFor();
  const pending = await table.locator('tr[data-pending]').innerText();
  assert.ok(!pending.includes('%'), pending);
  assert.equal(await table.locator('tr[data-pending] .net-geo-wait').count(), 1);
  await root.locator('.net-result-head .pb-tag', {hasText: '已完成'}).waitFor({timeout: 15000});
  assert.equal(await table.locator('tr[data-pending]').count(), 0);
  assert.equal(await table.locator('tbody tr').count(), 4);
  assert.equal(await table.locator('tr[data-reached]').count(), 0, 'the router that said unreachable is not the target');
  await root.getByText('目标没有回应，表里是途经的路由器；目标可能不回应 ping。').waitFor();
  await h.close();
});

test('mesh targets: the path in place of the trace table, the end-to-end ping, stale, a gateway network; "经过" under ping', async t => {
  const h = await setup(t);
  const root = probe(h.page);
  await root.locator('.net-form').waitFor();
  await h.page.locator('.mesh-card').waitFor();
  const pid = await run(h, {mode: '路由追踪', target: '100.96.0.2', count: '4'});
  const path = root.locator('.net-mesh-path');
  await path.waitFor();
  assert.equal(h.of('POST', /\/net\/probes$/).at(-1).body.mode, 'trace', 'the controller turns it into a ping');
  assert.equal(await path.locator('.net-path-line').innerText(), '家 → TOY（打洞 2.4 ms）→ VLINE（TCP 167 ms）→ LAX02（UDP 0.6 ms）');
  assert.match(await path.locator('.net-path-total').innerText(), /总延迟约 170 ms/);
  assert.equal(await path.getByText('数据不完整：有的线路没有上报，路径和延迟可能不准').count(), 0);
  assert.equal(await root.locator('table.net-trace').count(), 0);
  assert.equal(await path.getByRole('button', {name: '在画板上查看'}).count(), 0, 'no board on the detail page');
  await root.getByText('端到端 Ping').waitFor();
  await root.locator('.net-summary').waitFor();
  assert.equal(await root.locator('.net-results').count(), 0, 'only the summary under a path');
  await root.locator('.net-result-head .pb-tag', {hasText: '已完成'}).waitFor({timeout: 15000});
  assert.equal(h.stops(pid), 0);
  // After a controller restart the same test reads as a plain ping.
  const again = await run(h, {mode: '路由追踪', target: '100.96.0.2', count: '6'});
  await root.locator('.net-mesh-path').waitFor();
  h.net.restart();
  await root.locator('.net-results > li').first().waitFor();
  assert.equal(await root.locator('.net-mesh-path').count(), 0);
  assert.equal(await root.locator('.net-through').count(), 0);
  assert.ok(h.polls(again) > 0);
  // A ping to a mesh address: the machines on the way under the summary.
  await run(h, {mode: 'Ping', target: '100.96.0.2', count: '3'});
  await root.locator('.net-through').waitFor();
  assert.equal(await root.locator('.net-through').innerText(), '经过：家 → TOY → VLINE → LAX02');
  assert.equal(await root.locator('.net-mesh-path').count(), 0);
  await root.locator('.net-results > li').first().waitFor();
  await h.close();

  // From LAX02 to a host behind home's gateway: the tail after the gateway, and a stale report.
  const g = await setup(t, {tabs: ['lax'], active: 'lax'});
  const gr = probe(g.page);
  await gr.locator('.net-form').waitFor();
  await g.page.locator('.mesh-card').waitFor();
  await run(g, {mode: '路由追踪', target: '10.10.10.1', count: '3', root: gr});
  await gr.locator('.net-mesh-path').waitFor();
  assert.equal(await gr.locator('.net-path-line').innerText(), 'LAX02 → VLINE（UDP 0.6 ms）→ TOY（TCP 167 ms）→ 家（打洞 2.4 ms）→ 10.10.10.1（经网关，局域网）');
  await gr.getByText('数据不完整：有的线路没有上报，路径和延迟可能不准').waitFor();
  await g.close();
});

test('mesh member panel behind a proxy: the exit abroad under 本机地址, then 动态域名; desktop and phone', async t => {
  const h = await setup(t, {tabs: [], active: 'mesh', ddns: true});
  const {page} = h;
  await page.locator('.vue-flow__node[data-id="home"]').click();
  const panel = page.locator('.mesh-inspector-col');
  const local = panel.locator('section.net-addresses');
  await local.locator('.net-exit[data-proxy]').waitFor();
  assert.match(await local.locator('.net-exit[data-proxy]').innerText(), /出国出口\s*192\.0\.2\.92\s*经代理/);
  const dd = panel.locator('section.dd-card-panel');
  await dd.waitFor();
  assert.equal(await dd.locator('h4').innerText(), '动态域名');
  assert.deepEqual(await dd.locator('.dd-card-name').allInnerTexts(), ['home.example.com']);
  await page.evaluate(() => { window.__opened = []; window.addEventListener('openbear:open-ddns', e => window.__opened.push(e.detail)); });
  await dd.locator('.dd-card-row').click();
  assert.deepEqual(await page.evaluate(() => window.__opened), [{serverId: 'home', recordId: 'r1'}]);
  await dd.scrollIntoViewIfNeeded();
  await h.close();
  const m = await setup(t, {width: 390, height: 844, tabs: [], active: 'mesh', ddns: true});
  const sheet = m.page.locator('.mesh-inspect-sheet');
  await m.page.locator('.vue-flow__node[data-id="home"]').tap();
  const card = sheet.locator('section.dd-card-panel');
  await card.waitFor();
  await card.scrollIntoViewIfNeeded();
  await m.close();
});

test('动态域名 is a tab of the servers page: after 组网, it fills the body, a link opens it narrowed to a machine; phone too', async t => {
  for (const [width, height, name] of [[1440, 900, 'desktop'], [390, 844, 'phone']]) {
    const h = await setup(t, {width, height, tabs: ['home'], active: 'probe', ddns: true});
    const {page} = h;
    const tabs = page.locator('.sv-tabs > [role="tab"]');
    assert.deepEqual((await tabs.allInnerTexts()).slice(0, 4).map(s => s.trim()), ['探针', '终端', '组网', '动态域名']);
    assert.equal(await page.locator('section.dd-page').count(), 0, 'not loaded before it is opened');
    await press(h, page.getByRole('tab', {name: '动态域名'}));
    const dd = page.locator('section.dd-page');
    await dd.locator('.dd-table tbody tr').first().waitFor();
    assert.deepEqual(await dd.locator('.dd-name').allInnerTexts(), ['home.example.com']);
    await dd.getByRole('button', {name: '添加解析'}).waitFor();
    assert.equal(await dd.getByRole('button', {name: '从 ddns-go 导入'}).count(), 0, 'no import entry');
    // The tab body, not the page, holds it: nothing scrolls the page.
    // A link from elsewhere (a card, a reminder, /ddns): back to the tab, narrowed to the machine.
    await press(h, page.getByRole('tab', {name: '探针'}));
    await page.evaluate(() => import(`../ddns/ddnsLink.js?fresh=${h.fresh}`).then(m => m.requestDdns({serverId: 'home'})));
    await dd.locator('.dd-chip').filter({hasText: '家'}).waitFor();
    assert.equal(await page.getByRole('tab', {name: '动态域名'}).getAttribute('aria-selected'), 'true');
    await h.close();
  }
});

for (const theme of ['light', 'dark']) {
  test(`phone ${theme}: cards on the detail page, inputs that do not zoom, the trace as two-line hops with no sideways scrolling, the member sheet`, async t => {
    const h = await setup(t, {width: 390, height: 844, dark: theme === 'dark'});
    const {page} = h;
    const root = probe(page);
    const box = addresses(page);
    await box.locator('.net-iface').first().waitFor();
    await box.scrollIntoViewIfNeeded();
    // Inputs at 16px (no zoom on focus); the buttons easy to tap.
    await root.scrollIntoViewIfNeeded();
    const target = root.getByRole('combobox', {name: '目标'});
    await press(h, root.getByRole('radio', {name: 'TCPing'}));
    await press(h, root.locator('details.net-advanced > summary'));
    await press(h, root.locator('details.net-advanced > summary'));
    // Focus and blur leave the page where it was (no zoom, nothing shifted sideways).
    await target.tap();
    await target.blur();
    await run(h, {mode: 'TCPing', target: 'example.com', port: '443', count: '5'});
    await root.locator('.net-result-head .pb-tag', {hasText: '已完成'}).waitFor({timeout: 15000});
    await root.locator('.net-result').scrollIntoViewIfNeeded();
    // The trace on a phone: two lines an address instead of the table, nothing to scroll sideways, the average in sight.
    await run(h, {mode: '路由追踪', target: 'example.com'});
    const list = root.locator('ol.net-hops');
    await list.locator(':scope > li').nth(3).waitFor();
    assert.equal(await root.locator('.net-trace-wrap, table.net-trace').count(), 0, 'no table on a phone');
    await list.locator('.net-geo-wait').first().waitFor();
    await root.locator('.net-result-head .pb-tag', {hasText: '已完成'}).waitFor({timeout: 15000});
    await list.locator(':scope > li').nth(5).getByText('AS15133 Edgecast Inc.').waitFor({timeout: 10000});
    const hopTexts = () => list.locator(':scope > li').evaluateAll(l => l.map(li => [...li.querySelectorAll('.net-hop-line')].map(d => d.innerText.replace(/\s+/g, ' ').trim()).filter(Boolean).join(' / ')));
    assert.deepEqual(await hopTexts(), [
      '1 10.10.10.1 gateway.lan 0.4 ms / 私有地址',
      '2 * *',
      '3 100.64.0.1 3 ms / CGNAT',
      '4 203.0.113.1 ae-1.r20.tokyjp05.jp.bb.gin.ntt.net 21 ms / 🇯🇵 AS2914 NTT America, Inc. / 203.0.113.9 / 🇯🇵 AS2914 NTT America, Inc.',
      '5 198.51.100.77 core.example.jp 48 ms / 🇯🇵 AS2516 KDDI CORPORATION',
      '6 93.184.216.34 151 ms / 🇺🇸 AS15133 Edgecast Inc.']);
    const fits = async () => {
    };
    await list.scrollIntoViewIfNeeded();
    // Two addresses on a hop, loss, places known by operator only or by country only, a long name cut short.
    await run(h, {mode: '路由追踪', target: 'cn2.example'});
    await root.locator('.net-result-head .pb-tag', {hasText: '已完成'}).waitFor({timeout: 15000});
    await list.locator(':scope > li').nth(3).getByText('United States').waitFor({timeout: 10000});
    const cn2 = await hopTexts();
    assert.deepEqual(cn2.slice(0, 3), [
      '1 192.168.1.1 router.lan 0.6 ms / 私有地址',
      '2 59.43.246.85 38 ms / 🇨🇳 Chinatelecom Next Carrying Network backbone 丢包 33.3% / 59.43.247.9 / 🇨🇳 China',
      '3 * *']);
    assert.match(cn2[3], /^4 69\.194\.171\.42 ae12-0\.r01\.losangeles\.ctgnet\.backbone\.example\.net 153 ms \/ 🇺🇸 United States$/);
    assert.doesNotMatch(cn2.join(' '), /AS0|ASnull|Unknown|0%/);
    const two = list.locator(':scope > li').nth(1);
    assert.equal(await two.locator('.net-hop-rtt').count(), 1, 'the average once a hop');
    assert.equal(await two.locator('.net-hop-loss').count(), 1);
    assert.equal(await two.locator('.sv-flag').count(), 2);
    const name = list.locator(':scope > li').nth(3).locator('.net-hop-addr small');
    await list.scrollIntoViewIfNeeded();
    // A hop not tried yet while it runs: the waiting placeholder, no "0%".
    await run(h, {mode: '路由追踪', target: 'gap.example'});
    await list.locator('li[data-pending] .net-geo-wait').waitFor();
    assert.doesNotMatch(await list.locator('li[data-pending]').innerText(), /%/);
    await root.locator('.net-result-head .pb-tag', {hasText: '已完成'}).waitFor({timeout: 15000});
    assert.equal(await list.locator('li[data-reached]').count(), 0);
    await run(h, {mode: '路由追踪', target: '100.96.0.2'});
    await root.locator('.net-mesh-path').waitFor();
    await root.locator('.net-summary').waitFor();
    await root.locator('.net-result').scrollIntoViewIfNeeded();
    await h.close();

    // The member panel in the bottom sheet.
    const m = await setup(t, {width: 390, height: 844, dark: theme === 'dark', tabs: [], active: 'mesh'});
    const sheet = m.page.locator('.mesh-inspect-sheet');
    await m.page.locator('.vue-flow__node[data-id="lax"]').tap();
    const local = sheet.locator('section.net-addresses');
    await local.locator('.net-iface').first().waitFor();
    await local.scrollIntoViewIfNeeded();
    await sheet.getByRole('tab', {name: '测试'}).tap();
    const pr = sheet.locator('section.net-probe');
    const pid = await run(m, {mode: '路由追踪', target: '10.10.10.1', count: '10', root: pr});
    await pr.locator('.net-mesh-path').waitFor();
    await pr.locator('.net-summary').waitFor();
    // On the board: the sheet steps aside (and closes, so the test stops) and the path shows.
    await pr.getByRole('button', {name: '在画板上查看'}).tap();
    await sheet.waitFor({state: 'hidden'});
    await m.page.locator('.mesh-path .mesh-path-hops').waitFor();
    await waitFor(() => m.stops(pid) === 1, 'a stop when the sheet closes');
    await m.close();
  });
}

test('leaving stops a running test: another tab, the page kept alive, the server tab closed', async t => {
  const h = await setup(t, {tabs: ['home', 'vm'], active: 'home'});
  const root = probe(h.page);
  await root.locator('.net-form').waitFor();
  let pid = await run(h, {target: 'example.com', count: '100'});
  await root.locator('.net-results > li').first().waitFor();
  await h.page.locator('.sv-tab', {hasText: '探针'}).click();
  await waitFor(() => h.stops(pid) === 1, 'a stop when another tab is shown');
  const reads = h.polls(pid); await h.page.waitForTimeout(1500);
  assert.equal(h.polls(pid), reads, 'and no more reads');
  // The results stay; coming back shows them stopped.
  await h.page.locator('.sv-tab', {hasText: '家'}).click();
  await root.locator('.net-result-head .pb-tag', {hasText: '已停止'}).waitFor();
  // The servers page kept alive while another page shows.
  pid = await run(h, {target: 'example.com', count: '100'});
  await root.locator('.net-results > li').first().waitFor();
  await h.page.evaluate(() => window.__showServers(false));
  await waitFor(() => h.stops(pid) === 1, 'a stop when the page is left');
  await h.page.evaluate(() => window.__showServers(true));
  // The server's tab closed.
  await h.page.locator('.sv-tab', {hasText: '家'}).click();
  pid = await run(h, {target: 'example.com', count: '100'});
  await root.locator('.net-results > li').first().waitFor();
  await h.page.getByRole('button', {name: '关闭 家'}).click();
  await waitFor(() => h.stops(pid) === 1, 'a stop when the tab is closed');
  await h.close();
});

test('mesh member panel: local addresses on the status page, the test tab, a path on the board, stopped when the panel closes', async t => {
  const h = await setup(t, {tabs: [], active: 'mesh'});
  const {page} = h;
  await page.locator('.vue-flow__node').first().waitFor();
  await page.locator('.vue-flow__node[data-id="lax"]').click();
  const panel = page.locator('.mesh-inspector-col');
  const local = panel.locator('section.net-addresses');
  await local.locator('.net-iface').first().waitFor();
  assert.equal(await local.locator('h4').innerText(), '本机地址');
  assert.equal(await local.locator('.net-addr').count(), 4, 'the first four');
  assert.equal(await local.locator('.net-addr[data-control]').count(), 0, 'the control address is not among them');
  await local.getByRole('button', {name: '展开其余 3 个地址'}).click();
  assert.equal(await local.locator('.net-addr').count(), 7);
  // The test tab: mesh members first in the suggestions.
  await panel.getByRole('tab', {name: '测试'}).click();
  const root = panel.locator('section.net-probe');
  await root.getByRole('combobox', {name: '目标'}).click();
  await page.locator('.ob-suggest-list li').first().waitFor();
  const opts = await page.locator('.ob-suggest-list li').evaluateAll(l => l.map(li => li.innerText.replace(/\s+/g, ' ').trim()));
  assert.deepEqual(opts.slice(0, 3), ['100.96.0.3 家 · 组网', '100.96.0.6 TOY · 组网', '100.96.0.7 VLINE · 组网']);
  await root.getByRole('combobox', {name: '目标'}).press('Escape');
  let pid = await run(h, {mode: '路由追踪', target: '10.10.10.1', count: '10', root});
  await root.locator('.net-mesh-path').waitFor();
  // The other tabs can be looked at while it runs.
  await panel.getByRole('tab', {name: '状态'}).click();
  await page.waitForTimeout(1200);
  assert.equal(h.stops(pid), 0, 'not stopped by another tab of the panel');
  await panel.getByRole('tab', {name: '测试'}).click();
  // On the board: the path stands out, the path box names it.
  await root.getByRole('button', {name: '在画板上查看'}).click();
  const box = page.locator('.mesh-path');
  await box.locator('.mesh-path-hops').waitFor();
  assert.equal(await box.locator('h3').innerText(), 'LAX02 到 10.10.10.0/24 的路径');
  assert.equal(await root.getByRole('button', {name: '在画板上查看'}).getAttribute('aria-pressed'), 'true');
  assert.equal(await page.locator('.mesh-node[data-dim]').count(), 0, 'every member is on this path');
  // Closing the panel stops it.
  await panel.getByRole('button', {name: '取消选择'}).click();
  await waitFor(() => h.stops(pid) === 1, 'a stop when the panel closes');
  // Leaving the mesh page stops it too.
  await page.locator('.vue-flow__node[data-id="home"]').click();
  await panel.getByRole('tab', {name: '测试'}).click();
  pid = await run(h, {target: '100.96.0.2', count: '100', root: panel.locator('section.net-probe')});
  await panel.locator('section.net-probe .net-results > li').first().waitFor();
  await page.locator('.sv-tab', {hasText: '探针'}).click();
  await waitFor(() => h.stops(pid) === 1, 'a stop when the mesh page is left');
  await h.close();
});

test('dark desktop: the same views', async t => {
  const h = await setup(t, {dark: true});
  const root = probe(h.page);
  await addresses(h.page).locator('.net-iface').first().waitFor();
  await addresses(h.page).scrollIntoViewIfNeeded();
  await run(h, {mode: '路由追踪', target: 'example.com'});
  await root.locator('table.net-trace tbody tr').nth(3).waitFor();
  await root.locator('.net-geo-wait').first().waitFor();
  await root.scrollIntoViewIfNeeded();
  await root.locator('.net-result-head .pb-tag', {hasText: '已完成'}).waitFor({timeout: 15000});
  await run(h, {mode: 'TCPing', target: 'example.com', port: '443', count: '5'});
  await root.locator('.net-result-head .pb-tag', {hasText: '已完成'}).waitFor({timeout: 15000});
  await h.close();
  const m = await setup(t, {dark: true, tabs: [], active: 'mesh'});
  const panel = m.page.locator('.mesh-inspector-col');
  await m.page.locator('.vue-flow__node[data-id="home"]').click();
  await panel.locator('section.net-addresses .net-iface').first().waitFor();
  await panel.getByRole('tab', {name: '测试'}).click();
  await run(m, {mode: '路由追踪', target: '100.96.0.2', root: panel.locator('section.net-probe')});
  await panel.locator('.net-mesh-path').waitFor();
  await panel.getByRole('button', {name: '在画板上查看'}).click();
  await m.page.locator('.mesh-path .mesh-path-hops').waitFor();
  await m.close();
});
