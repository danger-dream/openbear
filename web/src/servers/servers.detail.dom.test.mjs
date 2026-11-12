// The detail page's monitoring without a browser: its tabs, and what 流量统计, 延迟与丢包, 网络设备,
// 硬盘 and IP 质量 read and show, and how their controls change that. Every section counts as on screen
// here (serversStubs.mjs); the browser test checks what is read only once scrolled to.
import test from 'node:test';
import assert from 'node:assert/strict';
import {setup, showTab, tab} from './testing/serversDom.mjs';
import {window} from '../domTestEnv.mjs';
import {sleep} from '../testing/domPage.mjs';
import {DETAILS} from './testing/serversBackend.mjs';

const open = async (h, name, part = '') => {
  await h.page.locator('.pb-card').filter({hasText: name}).click();
  const detail = h.page.locator('.sv-detail:visible');
  await detail.locator('.dt-tabs').waitFor();
  if (part) await showTab(detail, part);
  return detail;
};
async function until(check, ms = 4000) {
  for (let waited = 0; waited < ms && !check(); waited += 25) await new Promise(r => setTimeout(r, 25));
  return check();
}

test('tabs: four, each with its parts; another server opens on the tab looked at; a server without a node has none', async t => {
  const h = await setup(t);
  const detail = await open(h, 'Delta-JP');
  assert.deepEqual(await detail.locator('.dt-tabs [role=tab]').allInnerTexts(), ['概览', '网络', '系统', '工具']);
  const parts = async name => {
    const panel = detail.locator(`[data-panel=${name}]`);
    // Each part by its marker, a card without one by its heading.
    return panel.evaluate(el => [...el.querySelectorAll('[data-jump], [data-part], .ix-card-head > h3')].filter(n => n.matches('[data-jump], [data-part]') || !n.closest('[data-jump], [data-part]'))
      .map(n => n.dataset.jump || n.dataset.part || n.textContent.trim()));
  };
  assert.deepEqual(await parts('overview'), ['overview', 'trends', 'traffic', 'disk']);
  const net = await parts('net');
  for (const part of ['latency', '三网回程', '网络地址', 'bgp', 'ip', 'net']) assert.ok(net.includes(part), `网络 has ${part}: ${net}`);
  assert.deepEqual(await parts('system'), ['system', 'billing']);
  assert.ok((await parts('tools')).includes('browser'));
  // Only the tab picked is shown.
  assert.equal(await detail.locator('[data-panel=overview]').isVisible(), true);
  await showTab(detail, '网络');
  assert.deepEqual(await Promise.all(['overview', 'net', 'system', 'tools'].map(n => detail.locator(`[data-panel=${n}]`).isVisible())), [false, true, false, false]);
  // Another server opens on the same tab.
  await tab(h.page, '探针').click();
  const other = await open(h, 'Papa-HK.T1');
  assert.equal(await other.locator('.dt-tabs [aria-selected=true]').innerText(), '网络');
  // A server without a node has its bill and no tabs.
  await tab(h.page, '探针').click();
  const bare = await (async () => { await h.page.locator('.pb-card').filter({hasText: 'Hotel-HK'}).click(); return h.page.locator('.sv-detail:visible'); })();
  await bare.getByRole('button', {name: '生成安装命令'}).waitFor();
  assert.equal(await bare.locator('.dt-tabs').count(), 0);
  assert.equal(await bare.locator('.sv-dash').count(), 0);
  assert.equal(await bare.locator('[data-part=billing]').count(), 1);
  await h.close();
});

test('系统: a transparent proxy the node routes around, and one it could not', async t => {
  const facts = async () => {
    const h = await setup(t);
    const detail = await open(h, 'Delta-JP', '系统');
    await detail.locator('.sv-system-facts').waitFor();
    const list = (await detail.locator('.sv-system-facts > div').allInnerTexts()).map(s => s.replace(/\s+/g, ' ').trim());
    await h.close();
    return list.filter(s => s.startsWith('透明代理'));
  };
  assert.deepEqual(await facts(), [], 'none without a proxy');
  t.after(() => { delete DETAILS.proxy; });
  DETAILS.proxy = {kinds: ['redirect', 'tun'], active: true};
  assert.deepEqual(await facts(), ['透明代理 已绕开（TCP 重定向、TUN 接管）']);
  DETAILS.proxy = {kinds: ['tun'], active: false, error: 'nftables: Error: Could not process rule'};
  assert.deepEqual(await facts(), ['透明代理 绕开失败：nftables: Error: Could not process rule']);
});

test('offline: the cards make way for when it was last seen; the history stays', async t => {
  const h = await setup(t);
  const detail = await open(h, 'GoldenCloud');
  assert.match(await detail.locator('.dt-offline').innerText(), /^离线 · 最后在线 .*，下面是之前记录的数据。$/);
  assert.equal(await detail.locator('.ov-card').count(), 0);
  await detail.locator('[data-jump=traffic] .tc-bar').first().waitFor();
  assert.equal(h.calls.filter(c => c.path === '/api/servers/srv-golf/metrics' && c.query.range === 'live').length <= 1, true, 'no live polling for an offline node');
  await h.close();
});

test('走势: each range read once and bucketed by the server; switching back to 实时 uses what is held', async t => {
  const h = await setup(t);
  const detail = await open(h, 'Delta-JP');
  const trends = detail.locator('[data-jump=trends]');
  await trends.locator('[data-chart=load] svg').waitFor();
  assert.deepEqual(await trends.locator('.dt-ranges button').allInnerTexts(), ['实时', '1 小时', '24 小时', '7 天', '30 天', '90 天', '1 年']);
  for (const [label, range] of [['24 小时', '24h'], ['90 天', '90d'], ['1 年', '1y']]) {
    await trends.getByRole('tab', {name: label}).click();
    assert.ok(await until(() => h.calls.some(c => c.path === '/api/servers/srv-delta/metrics' && c.query.range === range)), range);
    await trends.locator('[data-chart=bandwidth] .tc-line').first().waitFor();
  }
  // The scale counts in one unit, its quarters round.
  const scale = await trends.locator('[data-chart=bandwidth] .tc-y > span').allInnerTexts();
  assert.equal(new Set(scale.map(s => s.split(' ')[1])).size, 1, scale.join(' | '));
  // The history under 网络设备 and 硬盘 follows the range.
  await showTab(detail, '网络');
  assert.match(await detail.locator('[data-jump=net] .tc-head').innerText(), /连接数\s*1 年/);
  await showTab(detail, '概览');
  const live = h.calls.filter(c => c.path === '/api/servers/srv-delta/metrics' && c.query.range === 'live' && !c.query.after).length;
  await trends.getByRole('tab', {name: '实时'}).click();
  await trends.locator('[data-chart=load] .tc-line').first().waitFor();
  assert.equal(h.calls.filter(c => c.path === '/api/servers/srv-delta/metrics' && c.query.range === 'live' && !c.query.after).length, live);
  await h.close();
});

test('流量统计: by hour, day or month, as bars or lines, the current one still counting', async t => {
  const h = await setup(t);
  const detail = await open(h, 'Delta-JP');
  const section = detail.locator('[data-jump=traffic]');
  await section.locator('.tc-bar').first().waitFor();
  assert.ok(h.calls.some(c => c.path === '/api/servers/srv-delta/traffic' && c.query.unit === 'day'));
  assert.equal(await section.locator('.tc-bar').count(), 62);
  assert.equal(await section.locator('.tc-bar.is-partial').count(), 2, 'the day so far, down and up');
  assert.match(await section.locator('.dt-total').innerText(), /下行合计 \S+ GB\s*上行合计 \S+ GB\s*共 \S+ (GB|TB)/);
  assert.match(await section.locator('.dt-period').innerText(), /本期\s*自 .* 起 · 重置于每月 1 号[\s\S]*已用\s*4\.0 GB \/ 750 GB/);
  await section.getByRole('tab', {name: '按月'}).click();
  assert.ok(await until(() => h.calls.some(c => c.path === '/api/servers/srv-delta/traffic' && c.query.unit === 'month')));
  await section.locator('.tc-bar').nth(23).waitFor();
  assert.equal(await section.locator('.tc-bar').count(), 24);
  assert.equal(await section.getByRole('tablist', {name: '统计单位'}).locator('[aria-selected=true]').innerText(), '按月');
  const scale = await section.locator('.tc-y > span').allInnerTexts();
  assert.equal(new Set(scale.map(s => s.split(' ')[1])).size, 1, scale.join(' | '));
  await section.getByRole('tab', {name: '折线'}).click();
  await section.locator('.tc-line').first().waitFor();
  assert.equal(await section.locator('.tc-bar').count(), 0);
  // From this period: calibrating it, and the server's traffic settings.
  const period = section.locator('.dt-period');
  await period.getByRole('button', {name: '校准'}).click();
  const calib = h.page.getByRole('dialog', {name: '校准本期流量'});
  assert.equal(await calib.getByLabel('本期已用').inputValue(), '4');
  await calib.getByRole('button', {name: '取消'}).click();
  await calib.waitFor({state: 'hidden'});
  await period.getByRole('button', {name: '流量设置'}).click();
  const editor = h.page.getByRole('dialog', {name: '编辑「Delta-JP」'});
  await editor.getByRole('radiogroup', {name: '流量方式'}).waitFor();
  await h.close();
});

test('延迟与丢包: a tile per target picks its line and its lane of loss; cuts and gaps are marked', async t => {
  const h = await setup(t);
  const detail = await open(h, 'Delta-JP', '网络');
  const section = detail.locator('[data-jump=latency]');
  await section.locator('.lt-target').first().waitFor();
  assert.ok(h.calls.some(c => c.path === '/api/servers/srv-delta/latency' && c.query.range === '24h'));
  assert.match(await section.locator('.ix-card-head').innerText(), /TCPing · 每 10 秒/);
  assert.deepEqual(await section.locator('.lt-name').allInnerTexts(), ['云南电信', '云南联通', '云南移动', 'Cloudflare', 'Google DNS']);
  const tile = name => section.locator('.lt-target').filter({hasText: name});
  assert.equal(await tile('云南电信').locator('strong').getAttribute('data-tone'), 'bad');
  assert.equal(await tile('云南联通').locator('strong').getAttribute('data-tone'), 'warn');
  assert.equal(await tile('Cloudflare').locator('strong').getAttribute('data-tone'), 'ok');
  assert.match(await tile('云南移动').innerText(), /平均 \d+ ms · 丢包 \d/);
  // At first only the card's group (三网) is drawn.
  assert.deepEqual(await section.locator('.lt-target[aria-pressed=true] .lt-name').allInnerTexts(), ['云南电信', '云南联通', '云南移动']);
  assert.equal(await section.locator('.tc-line').count(), 3);
  // A lane per target, named as its line; 云南移动 often loses much, all of them were cut for a while.
  assert.deepEqual(await section.locator('.tc-lane-label').allInnerTexts(), ['云南电信', '云南联通', '云南移动']);
  const lane = name => section.locator('.ll').filter({has: h.page.locator('rect')}).and(section.locator(`.ll[aria-label="${name}的丢包"]`));
  assert.ok(await lane('云南移动').locator('rect[data-loss=high]').count() > 0);
  assert.equal(await lane('云南电信').locator('rect[data-loss=down]').count(), 1);
  // The rest a tap away: its line and its lane.
  await tile('Cloudflare').click();
  assert.equal(await tile('Cloudflare').getAttribute('aria-pressed'), 'true');
  assert.equal(await section.locator('.tc-line').count(), 4);
  assert.equal(await lane('Cloudflare').locator('rect[data-loss=high]').count(), 0);
  assert.equal(await lane('Cloudflare').locator('rect[data-loss=down]').count(), 1);
  // Behind the lines: the stretch where every target was cut, and the one nothing was measured.
  assert.deepEqual(await section.locator('.tc-band').evaluateAll(l => l.map(b => b.dataset.band)), ['down', 'gap']);
  // The time axis is the range asked for.
  const ticks = await section.locator('.tc-x span').allInnerTexts();
  assert.equal(ticks.length, 5);
  assert.match(await section.locator('.lt-legend').innerText(), /丢包\s*无丢包\s*< 2%\s*2%～20%\s*20% 以上\s*全部丢失（断连）\s*没有数据\s*背景\s*所选目标全部断连\s*没有数据/);
  // Picking: a tile off takes its line and lane away; the last one shown stays.
  await tile('Cloudflare').click();
  assert.equal(await tile('Cloudflare').getAttribute('aria-pressed'), 'false');
  assert.equal(await section.locator('.tc-line').count(), 3);
  assert.equal(await section.locator('.tc-lane').count(), 3);
  for (const name of ['云南电信', '云南联通']) await tile(name).click();
  await tile('云南移动').click();
  assert.equal(await section.locator('.lt-target[aria-pressed=true]').count(), 1);
  assert.equal(await section.locator('.tc-line').count(), 1);
  // Another range keeps what is picked.
  await section.getByRole('tab', {name: '1 小时'}).click();
  assert.ok(await until(() => h.calls.some(c => c.path === '/api/servers/srv-delta/latency' && c.query.range === '1h')));
  for (let i = 0; i < 80 && (await section.locator('.tc-x span').first().innerText()).includes('/'); i++) await new Promise(r => setTimeout(r, 25));
  await section.locator('.tc-line').first().waitFor();
  assert.deepEqual(await section.locator('.lt-target[aria-pressed=true] .lt-name').allInnerTexts(), ['云南移动']);
  assert.deepEqual(await section.locator('.tc-lane-label').allInnerTexts(), ['云南移动']);
  await h.close();
});

test('延迟与丢包: a target no longer measured is hidden until the switch shows it, on every server after', async t => {
  const h = await setup(t, {tweak: items => { items.find(i => i.id === 'srv-delta').latencyStopped = true; }});
  const detail = await open(h, 'Delta-JP', '网络');
  const section = detail.locator('[data-jump=latency]');
  await section.locator('.lt-target').first().waitFor();
  assert.deepEqual(await section.locator('.lt-name').allInnerTexts(), ['云南电信', '云南联通', '云南移动', 'Cloudflare', 'Google DNS', 'Papa-HK.T1 · HTTP 80']);
  // An HTTP target the node is too old for says what it waits for.
  assert.match(await section.locator('.lt-target').filter({hasText: 'HTTP 80'}).innerText(), /—\s*更新节点后开始测 HTTP/);
  const toggle = section.getByRole('switch', {name: '显示已停测的目标'});
  assert.equal(await toggle.isChecked(), false);
  assert.match(await section.locator('.ix-card-head').innerText(), /已停测 1/);
  await section.locator('.lt-stopped .el-switch__label').click();
  assert.equal(await toggle.isChecked(), true);
  assert.deepEqual(await section.locator('.lt-name').allInnerTexts(), ['云南电信', '云南联通', '云南移动', 'Cloudflare', 'Google DNS', 'Papa-HK.T1 · HTTP 80', '广东电信\n已停测']);
  // Picked, it is drawn; hidden again, its line goes with it.
  await section.locator('.lt-target').filter({hasText: '广东电信'}).click();
  assert.equal(await section.locator('.tc-line').count(), 4);
  await toggle.click();
  assert.equal(await toggle.isChecked(), false);
  assert.equal(await section.locator('.lt-target').count(), 6);
  assert.equal(await section.locator('.tc-line').count(), 3);
  // A server with none has no switch.
  await tab(h.page, '探针').click();
  const papa = await open(h, 'Papa-HK.T1', '网络');
  await papa.locator('[data-jump=latency] .lt-target').first().waitFor();
  assert.equal(await papa.locator('[data-jump=latency] .lt-stopped').count(), 0);
  await h.close();
});

test('延迟与丢包: the tooltip names each target\'s latency and loss, a cut and no data', async t => {
  const h = await setup(t, {width: 1440});
  const detail = await open(h, 'Delta-JP', '网络');
  const section = detail.locator('[data-jump=latency]');
  await section.locator('.tc-plot').waitFor();
  await section.locator('.lt-target').filter({hasText: 'Cloudflare'}).click();
  const plot = section.locator('.tc-plot');
  // happy-dom lays nothing out: the plot is given a width, and the pointer goes where a sample is.
  await plot.evaluate(el => { el.getBoundingClientRect = () => ({left: 0, top: 0, width: 1000, height: 180, right: 1000, bottom: 180, x: 0, y: 0}); });
  const at = async fraction => {
    await plot.dispatchEvent('pointermove', {clientX: fraction * 1000, clientY: 20});
    return section.locator('.tc-tip').innerText();
  };
  const cut = await at((288 - 50 + 0.5) / 288);
  assert.match(cut, /云南电信\s*断连（全部丢失）[\s\S]*Cloudflare\s*断连（全部丢失）/);
  // Around sample 10 from the start, where 云南移动 lost a third (where the samples fall moves with the clock).
  const near = [];
  for (const i of [9, 10, 11, 12]) near.push(await at((i + 0.5) / 288));
  assert.ok(near.some(text => /云南移动\s*\d+(\.\d)? ms · 丢 33%/.test(text)), near.join(' | '));
  assert.ok(near.some(text => /云南移动\s*\d+(\.\d)? ms\s*Cloudflare/.test(text)), 'no loss, no loss shown');
  await h.close();
});

test('延迟与丢包: a node too old for it, and monitoring switched off', async t => {
  const h = await setup(t, {tweak: items => { items.find(i => i.id === 'srv-bravo').node.version = 'old'; }});
  let detail = await open(h, 'Bravo-FRA', '网络');
  await detail.getByText('这台服务器的节点版本还不支持延迟监测，更新节点后开始记录。').waitFor();
  await h.page.getByRole('button', {name: '设置', exact: true}).click();
  const panel = h.page.getByRole('dialog', {name: '服务器设置'});
  await panel.getByRole('tab', {name: '延迟'}).click();
  await panel.getByText('开启延迟监测').click();
  await panel.getByRole('button', {name: '保存'}).click();
  await panel.waitFor({state: 'hidden'});
  await tab(h.page, '探针').click();
  detail = await open(h, 'Delta-JP');
  await detail.locator('[data-jump=latency]').getByText('延迟监测已关闭。').waitFor();
  assert.equal(h.calls.filter(c => c.path === '/api/servers/srv-delta/latency').length, 0, 'nothing read while it is off');
  await h.close();
});

test('网络设备: connections now and over the range, each interface since boot', async t => {
  const h = await setup(t);
  const detail = await open(h, 'Delta-JP', '网络');
  const section = detail.locator('[data-jump=net]');
  await section.locator('.dt-nic-list li').first().waitFor();
  assert.match(await section.locator('[data-counts]').innerText(), /TCP\s*120\s*UDP\s*8\s*进程\s*310/);
  assert.match(await section.locator('.dt-nic-list').innerText(), /eth0\s*10 Gbps\s*↓\s*8\.4 TB\s*↑\s*15\.5 TB[\s\S]*docker0\s*↓\s*0 B\s*↑\s*0 B/);
  assert.equal(await section.locator('.dt-nic-list li[data-up=down] .dt-nic-bar').count(), 0, 'nothing drawn for an interface that moved nothing');
  assert.equal(await section.locator('.dt-more').count(), 0);
  assert.ok(h.calls.some(c => c.path === '/api/servers/srv-delta/details'));
  await h.close();
});

test('网络设备: established and listening, where the node tells them', async t => {
  const h = await setup(t, {tweak: items => { Object.assign(items.find(i => i.id === 'srv-delta').live.m, {tcpEstab: 97, tcpListen: 12}); }});
  const detail = await open(h, 'Delta-JP', '网络');
  assert.match(await detail.locator('[data-jump=net] [data-counts]').innerText(), /TCP\s*120\s*已建立\s*97\s*监听\s*12\s*UDP\s*8/);
  await h.close();
});

test('硬盘: partitions now, their use over a range, reads and writes', async t => {
  const h = await setup(t);
  const detail = await open(h, 'Delta-JP', '概览');
  const section = detail.locator('[data-jump=disk]');
  await section.locator('.dt-parts li').first().waitFor();
  assert.ok(h.calls.some(c => c.path === '/api/servers/srv-delta/disks' && c.query.range === '30d'));
  assert.deepEqual(await section.locator('.dt-part-name b').allInnerTexts(), ['/', '/data', '/boot']);
  assert.match(await section.locator('.dt-parts li').first().innerText(), /ext4\s*31\.0 GB \/ 40\.0 GB\s*78%/);
  assert.equal(await section.locator('.dt-part-use b[data-tone=warn]').innerText(), '78%');
  assert.deepEqual(await section.locator('.tc-head h4').evaluateAll(l => l.map(h => h.firstChild.textContent)), ['使用率', '读写']);
  assert.equal(await section.locator('[data-chart=io] h4 small').innerText(), '实时');
  await section.getByRole('tab', {name: '全部'}).click();
  assert.ok(await until(() => h.calls.some(c => c.path === '/api/servers/srv-delta/disks' && c.query.range === 'all')));
  await h.close();
});

const rows = (card, part) => card.locator(`[aria-label="${part}"] dl > div`).allInnerTexts().then(l => l.map(t => t.replace(/\s+/g, ' ').trim()));

test('IP 质量: the address and its 纯净分, each panel of facts and verdicts, open ports when probed, the map, what changed, and checking again', async t => {
  const h = await setup(t);
  const detail = await open(h, 'Delta-JP', '网络');
  const card = detail.locator('[data-jump=ip]');
  await card.locator('.ipq-hero').waitFor();
  assert.match(await card.locator('.ipq-who').innerText(), /^198\.51\.100\.9\s*🇯🇵东京 · Tokyo · 日本 · NTT America, Inc\.$/);
  assert.match(await card.locator('.ipq-score').innerText(), /^68\s*一般$/);
  assert.equal(await card.locator('.ipq-hero').getAttribute('data-tone'), 'warn');
  assert.equal(await card.locator('.ipq-scale > i').evaluate(i => i.style.left), '68%');
  await card.getByText('7,340,032').waitFor();
  assert.deepEqual(await rows(card, '使用场景 / 类型'), ['IP 原生性 🇯🇵 日本 原生 IP 注册地 美国', '标记 机房 IP 4/5 家', '运营商类型 机房 2/2 家',
    '人机流量 人类 67.3% 机器 32.7% 人类偏多', '场景评估 6 TikTok 一般 3 社媒运营 较差 9 AI 应用 极佳', '服务商 NTT America, Inc.']);
  assert.deepEqual(await card.locator('.ipq-scenes li').evaluateAll(l => l.map(li => li.dataset.tone)), ['warn', 'bad', 'ok']);
  assert.deepEqual(await rows(card, 'ASN / 运营商'), ['ASN AS2914', 'ASN 归属 NTT America, Inc.', 'ASN 自报类型 NSP 网络服务商', 'ASN IPv4 总量 7,340,032 1205 个前缀',
    '预估带宽 100+Tbps PeeringDB 自报', 'ASN 注册日期 1994-07-01', '企业信息 NTT America, Inc.']);
  assert.deepEqual(await rows(card, 'IP 情报（威胁指标）'), ['风险标记 代理', '滥用等级 极度纯净 AbuseIPDB', '综合风险 75 · 高 5 家中最高', 'HTTP 蜜罐黑名单 纯净',
    '邮件黑名单 未上榜 共查 423 个 · 12 个标记']);
  assert.deepEqual(await rows(card, '风险深度检测'), ['代理（Proxy） 1/7 家判定', 'VPN 未检测到 6 家', 'Tor 未检测到 7 家', '机房（Hosting） 5/6 家判定', '滥用者 未检测到 6 家', '机器人 未检测到 4 家']);
  assert.deepEqual(await card.locator('[aria-label="风险深度检测"] .ipq-tag').evaluateAll(l => l.map(b => b.dataset.tone)), ['warn', 'ok', 'ok', 'bad', 'ok', 'ok']);
  // Open ports: probed from the controller when asked; the ones better closed marked.
  assert.deepEqual(await rows(card, '技术指标'), ['Bogon 否（公网可达）', '反向 DNS v198-51-100-9.example.net', '开放端口 检测常见端口', '时区 Asia/Tokyo', '邮局 25 端口 不通']);
  await card.getByRole('button', {name: '检测常见端口'}).click();
  await card.locator('.ipq-ports').waitFor();
  assert.ok(h.calls.some(c => c.path === '/api/servers/srv-delta/ip-intel/ports' && c.method === 'POST'));
  assert.deepEqual(await card.locator('.ipq-ports li').allInnerTexts(), ['22 SSH', '80 HTTP', '443 HTTPS', '6379 Redis']);
  assert.equal(await card.locator('.ipq-ports li').filter({hasText: 'Redis'}).getAttribute('data-tone'), 'warn');
  assert.match(await card.locator('.ipq-ports-row').innerText(), /重新探测[\s\S]*从主控探测 31 个/);
  assert.deepEqual(await card.locator('.ipq-scores span').allInnerTexts(), ['IP2Location', 'Scamalytics', 'ipapi', 'AbuseIPDB', 'IPQS', 'DB-IP']);
  assert.deepEqual(await card.locator('.ipq-scores li').evaluateAll(l => l.map(li => li.dataset.tone)), ['ok', 'warn', 'ok', 'ok', 'bad', 'off']);
  assert.match(await card.locator('[data-ip-changes]').innerText(), /^与上次（\d+\/\d+）相比\s*代理 0 家 → 1 家；Prime Video 屏蔽 → 解锁 JP$/);
  assert.match(await card.locator('.ipq-map figcaption').innerText(), /原生IP\s*东京 · Tokyo · 日本/);
  // OpenStreetMap's map: in to the city at its coordinates and pinned there; zoomed with its buttons, with the wheel
  // only once the map is clicked.
  const map = card.locator('.ipm-map');
  await map.locator('.ipm-pin').waitFor();
  assert.deepEqual([await map.getAttribute('data-center'), await map.getAttribute('data-zoom'), await map.locator('.ipm-pin').getAttribute('data-at')],
    ['35.69,139.69', '10', '35.69,139.69']);
  assert.equal(await map.getAttribute('data-tiles'), 'https://tile.openstreetmap.org/{z}/{x}/{y}.png');
  assert.equal(await map.locator('.leaflet-control-attribution').innerText(), '© OpenStreetMap');
  await map.locator('[title=放大]').click();
  assert.equal(await map.getAttribute('data-zoom'), '11');
  await map.dispatchEvent('wheel');
  await card.getByText('先点一下地图，再用滚轮缩放').waitFor();
  await card.locator('.ipm-hint').waitFor({state: 'detached'});
  await map.click();
  await map.dispatchEvent('wheel');
  assert.equal(await card.locator('.ipm-hint').count(), 0);
  assert.equal(await card.locator('.ipq-map svg.wm').count(), 0);
  // Checking again: said what it runs, started as a 评测 run, followed until its report arrives.
  await card.getByRole('button', {name: '重新检测'}).click();
  const ask = h.page.getByRole('dialog', {name: '检测 IP 质量'});
  await ask.getByText('会用系统包管理器安装缺少的依赖', {exact: false}).waitFor();
  await ask.getByRole('button', {name: '开始检测'}).click();
  assert.ok(await until(() => h.calls.some(c => c.path === '/api/servers/bench/runs' && c.method === 'POST')));
  assert.deepEqual(h.calls.find(c => c.path === '/api/servers/bench/runs').body, {serverId: 'srv-delta', test: 'ipcheck', options: {family: '4'}});
  await card.getByRole('button', {name: /^(排队中|检测中)…$/}).waitFor();
  assert.ok(await card.getByRole('button', {name: /^(排队中|检测中)…$/}).isDisabled());
  await h.page.getByText('IP 质量检测完成').waitFor({timeout: 8000});
  await card.getByRole('button', {name: '重新检测'}).waitFor();
  assert.match(await card.locator('.ix-card-head').innerText(), /刚刚检测|\d+ 秒前检测/);
  await h.close();
});

test('IP 质量: none yet, and no such card for a front', async t => {
  const h = await setup(t, {tweak: items => {
    items.find(i => i.id === 'srv-delta').ipQuality = null;
    items.find(i => i.id === 'srv-bravo').profile.front = {exits: [{serverId: 'srv-papa', address: '10.0.10.246'}]};
  }});
  let detail = await open(h, 'Delta-JP', '网络');
  const card = detail.locator('[data-jump=ip]');
  // A Linux server that runs commands, abroad: in the queue of 自动检测.
  await card.getByText('还没有检测过，已在自动检测的队列里', {exact: false}).waitFor();
  assert.equal(await card.locator('> .ix-card-head').getByRole('button', {name: '检测', exact: true}).count(), 1);
  // What the controller reads needs no run: the network and the address's technical facts.
  await card.getByText('7,340,032').waitFor();
  assert.deepEqual(await card.locator('.ipq-panel > h4').allInnerTexts(), ['ASN / 运营商', '技术指标']);
  assert.equal(await card.locator('.ipq-hero').count(), 0);
  await tab(h.page, '探针').click();
  detail = await open(h, 'Bravo-FRA');
  assert.equal(await detail.locator('[data-jump=ip]').count(), 0);
  assert.match(await detail.locator('[data-jump=latency] .ix-card-head').innerText(), /前置机只测到出口/);
  await h.close();
});

test('IP 质量: a report with only the country, as IP.Check.Place gives for a small one: no city or time zone, pinned at the country', async t => {
  const h = await setup(t, {tweak: items => {
    items.find(i => i.id === 'srv-delta').ipQuality = {location: {lat: null, lon: null, city: '', subdivision: '', country: 'SG', countryName: '新加坡',
      registered: 'SG', registeredName: '新加坡', timezone: ''}};
  }});
  const detail = await open(h, 'Delta-JP', '网络');
  const card = detail.locator('[data-jump=ip]');
  await card.locator('.ipq-hero').waitFor();
  assert.match(await card.locator('.ipq-who').innerText(), /^198\.51\.100\.9\s*🇸🇬新加坡 · NTT America, Inc\.$/);
  assert.deepEqual(await rows(card, '技术指标'), ['Bogon 否（公网可达）', '反向 DNS v198-51-100-9.example.net', '开放端口 检测常见端口', '邮局 25 端口 不通']);
  assert.match(await card.locator('.ipq-map figcaption').innerText(), /原生IP\s*新加坡\s*按国家定位$/);
  // Pinned at the country's label point; Singapore has no outline to fit, so in to the city's level there.
  const map = card.locator('.ipm-map');
  await map.locator('.ipm-pin').waitFor();
  assert.deepEqual([await map.getAttribute('data-center'), await map.getAttribute('data-zoom'), await map.locator('.ipm-pin').getAttribute('data-at')],
    ['1.37,103.82', '10', '1.37,103.82']);
  await h.close();
});

test('IP 质量: the map of a country with an outline shows all of it; where OpenStreetMap cannot be reached, the built-in map', async t => {
  let h = await setup(t, {tweak: items => {
    items.find(i => i.id === 'srv-delta').ipQuality = {location: {lat: null, lon: null, city: '', subdivision: '', country: 'JP', countryName: '日本',
      registered: 'JP', registeredName: '日本', timezone: ''}};
  }});
  let card = (await open(h, 'Delta-JP', '网络')).locator('[data-jump=ip]');
  const map = card.locator('.ipm-map');
  await map.locator('.ipm-pin').waitFor();
  const [s, w, n, e] = (await map.getAttribute('data-bounds')).split(',').map(Number);
  assert.ok(s > 24 && s < 35 && n > 40 && n < 47 && w > 120 && w < 135 && e > 140 && e < 150, `Japan: ${[s, w, n, e]}`);
  await h.close();
  globalThis.leafletTiles = 'fail';
  t.after(() => { delete globalThis.leafletTiles; });
  h = await setup(t);
  card = (await open(h, 'Delta-JP', '网络')).locator('[data-jump=ip]');
  await card.locator('.ipq-map svg.wm').waitFor();
  assert.equal(await card.locator('.wm-dot').count(), 1);
  assert.equal(await card.locator('.ipm-map').count(), 0);
  await h.close();
});

const ROUTE = tiers => ({at: Date.now(), ip: '198.51.100.9', results: Object.fromEntries(['bj', 'sh', 'gd'].flatMap(city => Object.entries(tiers)
  .map(([carrier, tier]) => [`${city}-${carrier}`, {line: {gold: 'CN2 GIA', silver: 'CMI', plain: '163'}[tier], tier, rttMs: 40, reached: true, error: '', hops: []}])))});

const INTL = ['Cloudflare', 'Google DNS', 'OpenAI', 'Claude', 'Gemini', 'GitHub', 'Telegram'];
async function editLatency(h, detail) {
  await showTab(detail, '网络');
  const section = detail.locator('[data-jump=latency]');
  await section.locator('.lt-target').first().waitFor();
  await section.getByRole('button', {name: '编辑', exact: true}).click();
  const dialog = h.page.getByRole('dialog', {name: '编辑延迟目标'});
  await dialog.locator('.lte').waitFor();
  return dialog;
}
const chosen = dialog => dialog.locator('.lte-chosen .lte-name > span').allInnerTexts();
const count = dialog => dialog.locator('.lte-chosen .lte-head > b').innerText();

test('延迟目标: following the default list, what it measures listed; abroad without the carriers found ordinary; all three ordinary, the plain list', async t => {
  const routes = {'srv-delta': ROUTE({ct: 'plain', cu: 'plain', cm: 'silver'}), 'srv-papa': ROUTE({ct: 'plain', cu: 'plain', cm: 'plain'})};
  const tweak = items => { for (const [id, route] of Object.entries(routes)) items.find(i => i.id === id).node.route = route; };
  let h = await setup(t, {tweak});
  let dialog = await editLatency(h, await open(h, 'Delta-JP'));
  // Only the list it measures is named, with the way to the other.
  assert.match(await dialog.locator('.lte-mode').innerText(), /^跟随默认列表\s*「海外 · 优化线路」，在「设置 → 延迟」里改；电信、联通回程是普通线路，不测\s*自己选目标$/);
  assert.deepEqual(await chosen(dialog), ['云南移动', ...INTL]);
  assert.equal(await count(dialog), '8 / 16');
  assert.match(await dialog.locator('[data-target=yn-cm] .lte-name small').innerText(), /^TCPing · yn-cm-v4\.ip\.zstaticcdn\.com$/);
  // Following it, nothing is taken off or added; the card's stars still are.
  assert.equal(await dialog.getByRole('button', {name: /^去掉 /}).count(), 0);
  assert.match(await dialog.locator('.lte-locked').innerText(), /现在跟随默认列表[\s\S]*自己选目标/);
  assert.match(await dialog.locator('.lte-note').innerText(), /都不标，按设置里的规则/);
  await h.close();
  h = await setup(t, {tweak});
  dialog = await editLatency(h, await open(h, 'Papa-HK.T1'));
  assert.match(await dialog.locator('.lte-mode small').innerText(), /^「海外 · 普通线路」，在「设置 → 延迟」里改$/);
  assert.deepEqual(await chosen(dialog), INTL);
  await h.close();
});

test('延迟目标: a list of its own; servers each their way, a group at once; found by name; a new address into the library; the card\'s stars; back to the default', async t => {
  const h = await setup(t);
  const detail = await open(h, 'Delta-JP');
  let dialog = await editLatency(h, detail);
  await dialog.locator('.lte-mode').getByRole('button', {name: '自己选目标'}).click();
  assert.match(await dialog.locator('.lte-mode').innerText(), /^这台自己选的目标\s*默认列表是「海外 · 优化线路」的 10 个\s*改为跟随默认列表$/);
  await dialog.getByRole('button', {name: '去掉 云南联通'}).click();
  assert.equal(await count(dialog), '9 / 16');
  // Another server, by TCP to a port of its own: picked, then added.
  await dialog.getByRole('tab', {name: '服务器'}).click();
  const papa = dialog.locator('.lte-pick [data-server=srv-papa]');
  assert.match(await papa.innerText(), /Papa-HK\.T1\s*198\.51\.100\.8/);
  assert.equal(await papa.locator('.sv-flag').innerText(), '🇭🇰');
  await papa.getByLabel('Papa-HK.T1 的协议').selectOption('tcp');
  await papa.getByLabel('Papa-HK.T1 的端口').fill('2222');
  await papa.getByLabel('Papa-HK.T1 的端口').dispatchEvent('change');
  await papa.getByRole('button', {name: '添加 Papa-HK.T1'}).click();
  const added = papa.getByRole('button', {name: '添加 Papa-HK.T1'});
  assert.deepEqual([await added.innerText(), await added.getAttribute('aria-pressed')], ['已添加', 'true']);
  assert.match(await dialog.locator('[data-target="s:srv-papa:tcp:2222"]').innerText(), /^🇭🇰Papa-HK\.T1\s*TCP · 198\.51\.100\.8:2222/);
  // A group at once, by ICMP.
  const group = dialog.locator('.lte-group').filter({hasText: '测试机'});
  await group.getByRole('button', {name: '全部添加（2）'}).click();
  assert.equal(await group.locator('.lte-add[aria-pressed=true]').count(), 2);
  assert.equal(await count(dialog), '12 / 16');
  // Found by name across every tab: another province's carrier.
  await dialog.getByLabel('搜索目标').fill('北京');
  assert.equal(await dialog.locator('.lte-tabs').count(), 0);
  await dialog.getByRole('button', {name: '添加 北京电信'}).click();
  await dialog.getByLabel('搜索目标').fill('');
  // One's own address by HTTP: checked as the settings take it, then added.
  await dialog.getByRole('tab', {name: '我的地址'}).click();
  await dialog.getByRole('button', {name: '新增地址'}).click();
  const form = dialog.locator('.sv-addr-form');
  await form.getByLabel('名称').fill('博客');
  await form.getByLabel('协议', {exact: true}).selectOption('http');
  await form.getByLabel('网址').fill('ftp://blog.example.com/');
  await form.getByRole('button', {name: '添加'}).click();
  await form.getByText('网址以 http:// 或 https:// 开头', {exact: false}).waitFor();
  await form.getByLabel('网址').fill('https://blog.example.com/health');
  await form.getByRole('button', {name: '添加'}).click();
  assert.deepEqual((await chosen(dialog)).slice(-1), ['博客']);
  assert.equal(await count(dialog), '14 / 16');
  // The card: Papa starred.
  await dialog.getByRole('button', {name: 'Papa-HK.T1：在服务器卡片上显示'}).click();
  await dialog.getByRole('button', {name: '保存'}).click();
  await dialog.waitFor({state: 'hidden'});
  const custom = h.calls.find(c => c.path === '/api/servers/settings' && c.method === 'PUT').body.latency.custom;
  const blog = custom.at(-1);
  assert.deepEqual({...blog, id: ''}, {id: '', label: '博客', host: 'blog.example.com', port: 443, group: 'intl', mode: 'http', url: 'https://blog.example.com/health'});
  assert.equal(custom[0].id, 'u-0a1b2c3d', 'the library kept');
  const patches = () => h.calls.filter(c => c.path === '/api/servers/srv-delta' && c.method === 'PATCH');
  assert.deepEqual(patches().at(-1).body.profile.latency, {targets: ['yn-ct', 'yn-cm', 'cloudflare', 'google', 'openai', 'claude', 'gemini', 'github', 'telegram',
    's:srv-papa:tcp:2222', 's:local', 's:srv-golf', 'bj-ct', blog.id], card: ['s:srv-papa:tcp:2222']});
  // Back to the default list: a star of what it no longer measures goes.
  dialog = await editLatency(h, detail);
  assert.equal(await dialog.locator('.lte-mode b').innerText(), '这台自己选的目标');
  await dialog.getByRole('button', {name: '改为跟随默认列表'}).click();
  assert.equal(await dialog.locator('.lte-mode b').innerText(), '跟随默认列表');
  assert.equal(await count(dialog), '10 / 16');
  await dialog.getByRole('button', {name: '云南电信：在服务器卡片上显示'}).click();
  await dialog.getByRole('button', {name: '保存'}).click();
  await dialog.waitFor({state: 'hidden'});
  assert.deepEqual(patches().at(-1).body.profile.latency, {card: ['yn-ct']});
  await h.close();
});

test('延迟目标: another server at the address picked, its public IPv6 or, from a server on the mesh, its mesh address', async t => {
  const tweak = items => {
    const by = id => items.find(i => i.id === id);
    by('srv-delta').meshAddress = '100.96.0.5';
    by('srv-papa').meshAddress = '100.96.0.8';
    by('srv-papa').node.info.ipv6 = '2001:db8::8';
  };
  let h = await setup(t, {tweak});
  let dialog = await editLatency(h, await open(h, 'Delta-JP'));
  await dialog.locator('.lte-mode').getByRole('button', {name: '自己选目标'}).click();
  await dialog.getByRole('tab', {name: '服务器'}).click();
  const papa = dialog.locator('.lte-pick [data-server=srv-papa]');
  // Which address: its name and an arrow, its addresses in a menu (Papa's the only one here with more than one).
  const at = papa.getByRole('button', {name: /^Papa-HK\.T1 的地址/});
  assert.equal((await at.innerText()).trim(), '公网 IPv4 · 198.51.100.8');
  const offered = async () => (await h.page.getByRole('menuitem').allInnerTexts()).map(t => t.trim()).filter(t => t.includes(' · '));
  await at.click();
  await h.page.getByRole('menuitem', {name: '组网 · 100.96.0.8'}).waitFor();
  assert.deepEqual(await offered(), ['公网 IPv4 · 198.51.100.8', '公网 IPv6 · 2001:db8::8', '组网 · 100.96.0.8']);
  // One address: shown, nothing to pick.
  assert.equal(await dialog.locator('.lte-pick [data-server=srv-bravo] .lte-at').count(), 0);
  assert.equal(await dialog.locator('.lte-pick [data-server=srv-bravo] .lte-name small').innerText(), '198.51.100.9');
  await h.page.getByRole('menuitem', {name: '组网 · 100.96.0.8'}).click();
  assert.equal((await at.innerText()).trim(), '组网 · 100.96.0.8');
  await papa.getByLabel('Papa-HK.T1 的协议').selectOption('tcp');
  await papa.getByRole('button', {name: '添加 Papa-HK.T1'}).click();
  assert.match(await dialog.locator('[data-target="s:srv-papa@mesh:tcp:22"]').innerText(), /Papa-HK\.T1\s*TCP · 组网 100\.96\.0\.8:22$/);
  // Another address for it once added: its entry follows.
  await at.click();
  await h.page.getByRole('menuitem', {name: '公网 IPv6 · 2001:db8::8'}).click();
  assert.match(await dialog.locator('[data-target="s:srv-papa@v6:tcp:22"]').innerText(), /TCP · IPv6 \[2001:db8::8\]:22$/);
  await dialog.getByRole('button', {name: '保存'}).click();
  await dialog.waitFor({state: 'hidden'});
  assert.equal(h.calls.filter(c => c.path === '/api/servers/srv-delta' && c.method === 'PATCH').at(-1).body.profile.latency.targets.at(-1), 's:srv-papa@v6:tcp:22');
  await h.close();
  // This one off the mesh: no mesh address to pick, and why; one picked before is said not to be measured.
  h = await setup(t, {tweak: items => {
    tweak(items);
    const delta = items.find(i => i.id === 'srv-delta');
    delete delta.meshAddress;
    delta.profile.latency = {targets: ['s:srv-papa@mesh']};
  }});
  dialog = await editLatency(h, await open(h, 'Delta-JP'));
  assert.equal(await dialog.locator('[data-target="s:srv-papa@mesh"] .lte-name small').innerText(), 'ICMP · 组网 · 这台不在组网里，测不了');
  await dialog.getByRole('tab', {name: '服务器'}).click();
  await dialog.getByText('这台不在组网里，只能测别的服务器的公网地址。').waitFor();
  const at2 = dialog.getByRole('button', {name: /^Papa-HK\.T1 的地址/});
  assert.equal((await at2.innerText()).trim(), '组网 · 没有这个地址');
  await at2.click();
  await h.page.getByRole('menuitem', {name: '组网 · 没有这个地址'}).waitFor();
  assert.deepEqual((await h.page.getByRole('menuitem').allInnerTexts()).map(t => t.trim()).filter(t => t.includes(' · ')),
    ['公网 IPv4 · 198.51.100.8', '公网 IPv6 · 2001:db8::8', '组网 · 没有这个地址']);
  await h.close();
});

test('延迟与丢包: a target just added says it waits for its first minute, and is drawn at once', async t => {
  const h = await setup(t);
  const detail = await open(h, 'Delta-JP', '网络');
  const section = detail.locator('[data-jump=latency]');
  await section.locator('.lt-target').first().waitFor();
  const dialog = await editLatency(h, detail);
  await dialog.locator('.lte-mode').getByRole('button', {name: '自己选目标'}).click();
  await dialog.getByRole('tab', {name: '服务器'}).click();
  await dialog.getByRole('button', {name: '添加 Bravo-FRA'}).click();
  await dialog.getByRole('button', {name: '保存'}).click();
  await dialog.waitFor({state: 'hidden'});
  const bravo = section.locator('.lt-target').filter({hasText: 'Bravo-FRA'});
  await bravo.waitFor();
  assert.match(await bravo.innerText(), /—\s*等待第一组数据，约 1 分钟/);
  assert.equal(await bravo.getAttribute('aria-pressed'), 'true');
  // Those measured before keep what was drawn: the card's group, the three carriers.
  assert.equal(await section.locator('.lt-target').filter({hasText: 'Cloudflare'}).getAttribute('aria-pressed'), 'false');
  await h.close();
});

test('saved edits show at once: a server\'s own targets and the latency settings read its latency again', async t => {
  const h = await setup(t);
  const detail = await open(h, 'Delta-JP', '网络');
  await detail.locator('[data-jump=latency] .lt-target').first().waitFor();
  const reads = () => h.calls.filter(c => c.path === '/api/servers/srv-delta/latency').length;
  const before = reads();
  const editor = await editLatency(h, detail);
  await editor.locator('.lte-mode').getByRole('button', {name: '自己选目标'}).click();
  await editor.getByRole('button', {name: '去掉 云南联通'}).click();
  await editor.getByRole('button', {name: '保存', exact: true}).click();
  await editor.waitFor({state: 'hidden'});
  assert.ok(await until(() => reads() > before), 'read again after saving the targets');
  assert.deepEqual(h.calls.find(c => c.path === '/api/servers/srv-delta' && c.method === 'PATCH').body.profile.latency.targets,
    ['yn-ct', 'yn-cm', 'cloudflare', 'google', 'openai', 'claude', 'gemini', 'github', 'telegram']);
  // The settings: another interval reads it again, and the cards' half hour too.
  const after = reads(), summaries = h.calls.filter(c => c.path === '/api/servers/latency').length;
  await h.page.getByRole('button', {name: '设置', exact: true}).click();
  const panel = h.page.getByRole('dialog', {name: '服务器设置'});
  await panel.getByRole('tab', {name: '延迟'}).click();
  await panel.getByLabel('探测间隔（秒）').fill('20');
  await panel.getByRole('button', {name: '保存'}).click();
  await panel.waitFor({state: 'hidden'});
  assert.ok(await until(() => reads() > after), 'read again after saving the settings');
  assert.ok(await until(() => h.calls.filter(c => c.path === '/api/servers/latency').length > summaries));
  assert.match(await detail.locator('[data-jump=latency] .ix-card-head').innerText(), /每 20 秒/);
  await h.close();
});

// From the 11-09 review: a detail page kept reading its live samples every 3 seconds, and an open processes
// popover kept asking the node, with another page of the app shown (this one kept alive) or the browser tab hidden.
test('away: the detail and its processes stop asking with another page shown or the tab hidden, and pick up again', async t => {
  const h = await setup(t, {keepAlive: true});
  const detail = await open(h, 'Delta-JP');
  await detail.getByRole('button', {name: /^处理器/}).click();
  await h.page.locator('.sv-top-popper .pt-row').first().waitFor();
  const live = () => h.calls.filter(c => c.path === '/api/servers/srv-delta/metrics' && c.query.range === 'live').length;
  const top = () => h.calls.filter(c => c.path === '/api/servers/srv-delta/processes').length;
  assert.ok(await until(() => live() > 1 && top() > 1), 'both refresh while shown');
  const quiet = async label => {
    await sleep(100);
    const before = [live(), top()];
    // More than two rounds of each (3 seconds, run ten times faster here).
    await sleep(700);
    assert.deepEqual([live(), top()], before, label);
    return before;
  };
  await h.page.evaluate(() => window.__showServers(false));
  await h.page.locator('#other-page').waitFor();
  let [lives, tops] = await quiet('nothing asked with another page shown');
  await h.page.evaluate(() => window.__showServers(true));
  assert.ok(await until(() => live() > lives), 'the live samples pick up again');
  // The popover closed on leaving (on a phone the sheet stays, and its rounds pick up again): opened again, they do.
  if (!await h.page.locator('.sv-top-popper .pt-row').count()) {
    assert.equal(top(), tops);
    await detail.getByRole('button', {name: /^处理器/}).click();
  }
  assert.ok(await until(() => top() > tops), 'the processes are read again');
  await h.page.locator('.sv-top-popper .pt-row').first().waitFor();
  const {document} = window;
  const hide = hidden => { Object.defineProperty(document, 'hidden', {configurable: true, get: () => hidden}); document.dispatchEvent(new window.Event('visibilitychange')); };
  t.after(() => { delete document.hidden; });
  hide(true);
  [lives, tops] = await quiet('nothing asked with the tab hidden');
  hide(false);
  assert.ok(await until(() => live() > lives && top() > tops), 'both pick up again in front');
  // One round of the processes at a time, not one more per return.
  tops = top();
  await sleep(450);
  assert.ok(top() - tops <= 2, `${top() - tops} reads in 450 ms`);
  await h.close();
});

// On a phone the processes are a bottom sheet, which stays open behind another page: its rounds stop and pick up again.
test('away on a phone: the processes sheet stops asking behind another page, one round again on return', async t => {
  const h = await setup(t, {keepAlive: true, width: 390, height: 844});
  const detail = await open(h, 'Delta-JP');
  await detail.getByRole('button', {name: /^处理器/}).click();
  await h.page.locator('.pt .pt-row').first().waitFor();
  const top = () => h.calls.filter(c => c.path === '/api/servers/srv-delta/processes').length;
  assert.ok(await until(() => top() > 1), 'it refreshes while shown');
  await h.page.evaluate(() => window.__showServers(false));
  await h.page.locator('#other-page').waitFor();
  await sleep(100);
  const tops = top();
  await sleep(700);
  assert.equal(top(), tops, 'nothing asked with another page shown');
  await h.page.evaluate(() => window.__showServers(true));
  assert.ok(await until(() => top() > tops), 'read again on return');
  const back = top();
  await sleep(450);
  assert.ok(top() - back <= 2, `${top() - back} reads in 450 ms`);
  await h.close();
});

// A node that reads no interface's counters (macOS before it did): not read, rather than 0 B each.
test('网络设备: interfaces without their counters show none, not 0 B', async t => {
  const saved = DETAILS.interfaces;
  t.after(() => { DETAILS.interfaces = saved; });
  DETAILS.interfaces = saved.map(({rx, tx, ...rest}) => rest);
  const h = await setup(t);
  const detail = await open(h, 'Delta-JP', '网络');
  const section = detail.locator('[data-jump=net]');
  await section.locator('.dt-nic-list li').first().waitFor();
  assert.match(await section.locator('.dt-nics h4').innerText(), /节点没有提供各网卡的流量/);
  assert.equal(await section.locator('.dt-nic-bytes').count(), 0);
  assert.doesNotMatch(await section.locator('.dt-nic-list').innerText(), /0 B/);
  await h.close();
});
