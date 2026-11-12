// 服务器设置 without a browser: its 数据 tab (how long the metrics file keeps each level, what it takes, cleaning it now)
// its 延迟 tab (where the three carriers are measured to, how often, how, what a card shows) and its 监测地址 tab
// (the carriers' directory and how it is kept, the addresses in and out of China, one's own, the three default lists).
import test from 'node:test';
import assert from 'node:assert/strict';
import {setup} from './testing/serversDom.mjs';

const MB = 1024 ** 2;
async function open(h) {
  await h.page.getByRole('button', {name: '设置'}).click();
  const dialog = h.page.getByRole('dialog', {name: '服务器设置'});
  await dialog.getByRole('tab', {name: '接入'}).waitFor();
  return dialog;
}

test('数据: the days kept, hours never shorter than minutes, the file\'s size and cleaning it now', async t => {
  const h = await setup(t);
  const dialog = await open(h);
  assert.equal(h.calls.filter(c => c.path === '/api/servers/storage').length, 0, 'read only when its tab opens');
  await dialog.getByRole('tab', {name: '数据'}).click();
  const list = dialog.locator('.sv-storage-list > li');
  await list.first().waitFor();
  assert.match(await dialog.locator('.sv-storage .sv-set-head').innerText(), /占用\s*共 182(\.0)? MB/);
  // Minute rows: metrics over 12.3 days and latency over 2.1, each at its own pace, for 31 days.
  const minutePerDay = 66 * MB / 12.3 + 18 * MB / 2.1;
  assert.match(await list.nth(0).innerText(), new RegExp(`分钟级\\s*84(\\.0)? MB\\s*保留 31 天 · 存满约 ${Math.round(minutePerDay * 31 / MB)}`));
  assert.match(await list.nth(1).innerText(), /小时级[\s\S]*保留 400 天 · 存满约/);
  assert.match(await list.nth(2).innerText(), /每日流量[\s\S]*永久保留/);
  // The hour choices follow the minutes: never shorter, and for ever when the minutes are.
  const minutes = dialog.getByLabel('分钟级数据保留'), hours = dialog.getByLabel('小时级数据保留');
  assert.deepEqual(await minutes.locator('option').allInnerTexts(), ['7 天', '14 天', '31 天（默认）', '60 天', '90 天', '180 天', '1 年', '永久']);
  await minutes.selectOption('365');
  assert.deepEqual(await hours.locator('option').allInnerTexts(), ['400 天（默认）', '2 年', '永久']);
  await minutes.selectOption('0');
  assert.deepEqual(await hours.locator('option').allInnerTexts(), ['永久']);
  assert.equal(await hours.inputValue(), '0');
  assert.match(await list.nth(0).innerText(), /保留 永久 · 每月约增加/);
  await minutes.selectOption('7');
  await hours.selectOption('90');
  // Cleaning now: the free pages go back and the figures follow.
  await dialog.getByRole('button', {name: '立即清理并压缩'}).click();
  await h.page.getByText('已清理，释放 6.0 MB').waitFor();
  assert.match(await dialog.locator('.sv-storage .sv-set-head').innerText(), /共 176(\.0)? MB/);
  await dialog.getByRole('button', {name: '保存'}).click();
  await dialog.waitFor({state: 'detached'});
  const put = h.calls.find(c => c.path === '/api/servers/settings' && c.method === 'PUT').body;
  assert.deepEqual(put.retention, {minuteDays: 7, hourDays: 90});
  await h.close();
});

test('延迟: province, interval with its floor, method, what a card shows', async t => {
  const h = await setup(t);
  const dialog = await open(h);
  await dialog.getByRole('tab', {name: '延迟'}).click();
  const province = dialog.getByLabel('观测地');
  assert.equal(await province.inputValue(), '云南');
  await province.selectOption('北京');
  await dialog.getByRole('radio', {name: /ICMP/}).check();
  await dialog.getByText('节点要有发 ICMP 的权限', {exact: false}).waitFor();
  assert.equal(await dialog.getByRole('radio', {name: '自动'}).isChecked(), true);
  await dialog.getByRole('radio', {name: '三网'}).check();
  await dialog.getByText('只算观测地电信、联通、移动', {exact: false}).waitFor();
  // Below the floor: said on its tab, nothing saved.
  await dialog.getByLabel('探测间隔（秒）').fill('2');
  await dialog.getByRole('tab', {name: '接入'}).click();
  await dialog.getByRole('button', {name: '保存'}).click();
  await dialog.getByText('探测间隔应为 3–600 秒的整数。').waitFor();
  assert.equal(await dialog.getByRole('tab', {name: '延迟'}).getAttribute('aria-selected'), 'true');
  assert.equal(h.calls.filter(c => c.path === '/api/servers/settings' && c.method === 'PUT').length, 0);
  await dialog.getByLabel('探测间隔（秒）').fill('3');
  // What is measured: one click to its own tab.
  await dialog.getByRole('button', {name: '在「监测地址」里设置'}).click();
  assert.equal(await dialog.getByRole('tab', {name: '监测地址'}).getAttribute('aria-selected'), 'true');
  await dialog.getByRole('button', {name: '保存'}).click();
  await dialog.waitFor({state: 'detached'});
  const put = h.calls.find(c => c.path === '/api/servers/settings' && c.method === 'PUT').body;
  assert.deepEqual(put.latency, {enabled: true, province: '北京', intervalS: 3, method: 'icmp', card: 'three',
    custom: [{id: 'u-0a1b2c3d', label: '家里 NAS', host: 'nas.example.com', port: 443, group: 'cn'}],
    defaults: {domestic: ['three', 'ali-dns', 'tencent-dns'], optimized: ['three', 'cloudflare', 'google', 'openai', 'claude', 'gemini', 'github', 'telegram'],
      plain: ['cloudflare', 'google', 'openai', 'claude', 'gemini', 'github', 'telegram']}});
  // Switched off: the rest of the tab goes.
  const again = await open(h);
  await again.getByRole('tab', {name: '延迟'}).click();
  await again.getByLabel(/开启延迟监测/).uncheck();
  assert.equal(await again.getByLabel('观测地').count(), 0);
  await h.close();
});

test('监测地址: the carriers\' directory and how it is kept, a refresh that fails, the watched province and all of them', async t => {
  const h = await setup(t);
  const dialog = await open(h);
  await dialog.getByRole('tab', {name: '监测地址'}).click();
  const lib = dialog.locator('[data-latency-library]');
  assert.equal(await lib.getByRole('tab', {name: '全国三网'}).getAttribute('aria-selected'), 'true');
  const state = await lib.locator('.sv-lib-state').innerText();
  assert.match(state, /每天自动取一次全国 31 个省的节点地址；没取到时每小时重试/);
  assert.match(state, /当前地址\s*5 小时前取到/);
  assert.match(state, /上次检查\s*2 小时前 · 成功/);
  // The watched province alone, then all of them.
  const rows = () => lib.locator('.sv-lib-table tbody tr').allInnerTexts();
  assert.deepEqual((await rows()).map(r => r.replace(/\s+/g, ' ').trim()), ['云南观测地 yn-ct-v4.ip.zstaticcdn.com:80 yn-cu-v4.ip.zstaticcdn.com:80 yn-cm-v4.ip.zstaticcdn.com:80']);
  await lib.getByRole('button', {name: '查看全部 4 个省'}).click();
  assert.equal((await rows()).length, 4);
  // A refresh that fails: said, with why, and the addresses stay.
  h.backend.settings.latencyCatalog = {...h.backend.settings.latencyCatalog, unreachable: true};
  await lib.getByRole('button', {name: '立即更新'}).click();
  await h.page.getByText('没能取到节点目录，继续用原来的地址').waitFor();
  const failed = lib.locator('.sv-lib-state dd[data-tone=bad]');
  assert.match(await failed.innerText(), /刚刚 · 失败：ClientConnectorError: Cannot connect to host www\.zstaticcdn\.com:443/);
  assert.match(await lib.locator('.sv-lib-state').innerText(), /当前地址\s*5 小时前取到/);
  h.backend.settings.latencyCatalog = {...h.backend.settings.latencyCatalog, unreachable: false};
  await lib.getByRole('button', {name: '立即更新'}).click();
  await h.page.getByText('节点目录已是最新').waitFor();
  assert.match(await lib.locator('.sv-lib-state').innerText(), /当前地址\s*刚刚取到[\s\S]*上次检查\s*刚刚 · 成功/);
  await h.close();
});

test('监测地址: the addresses in and out of China by kind, one\'s own added, changed and taken out, the default lists', async t => {
  const h = await setup(t);
  const dialog = await open(h);
  await dialog.getByRole('tab', {name: '监测地址'}).click();
  const lib = dialog.locator('[data-latency-library]');
  await lib.getByRole('tab', {name: '国外'}).click();
  const group = kind => lib.locator(`.sv-lib-group[data-kind=${kind}]`);
  assert.deepEqual(await group('ai').locator('.sv-lib-name').allInnerTexts(), ['OpenAI', 'Claude', 'Gemini', 'Grok']);
  assert.deepEqual(await group('service').locator('.sv-lib-name').allInnerTexts(), ['GitHub', 'Telegram', 'Netflix']);
  // Each says which default lists measure it.
  const tags = id => lib.locator(`li[data-id="${id}"] .sv-lib-tags > i`).allInnerTexts();
  assert.deepEqual(await tags('openai'), ['海外优化', '海外普通']);
  assert.deepEqual(await tags('grok'), []);
  // One's own abroad: host:port pasted, a bad host said, then added.
  await group('custom').getByRole('button', {name: '添加地址'}).click();
  const form = lib.getByRole('group', {name: '添加地址'});
  await form.getByLabel('名称').fill('东京 VPS');
  await form.getByLabel('主机').fill('bad host:22');
  await form.getByLabel('主机').blur();
  assert.equal(await form.getByLabel('端口').inputValue(), '22');
  await form.getByRole('button', {name: '添加'}).click();
  await form.getByText('主机填域名或 IP 地址', {exact: false}).waitFor();
  await form.getByLabel('主机').fill('203.0.113.9');
  await form.getByRole('button', {name: '添加'}).click();
  assert.deepEqual(await group('custom').locator('.sv-lib-name').allInnerTexts(), ['东京 VPS']);
  assert.match(await group('custom').locator('.sv-lib-host').innerText(), /^203\.0\.113\.9:22$/);
  // One timed by HTTP: its row says so, with its URL.
  await group('custom').getByRole('button', {name: '添加地址'}).click();
  const web = lib.getByRole('group', {name: '添加地址'});
  await web.getByLabel('名称').fill('状态页');
  await web.getByLabel('协议').selectOption('http');
  await web.getByLabel('网址').fill('https://status.example.com/');
  await web.getByRole('button', {name: '添加'}).click();
  assert.deepEqual(await group('custom').locator('.sv-lib-host').allInnerTexts(), ['203.0.113.9:22', 'HTTPhttps://status.example.com/']);
  // In China: the one already there changed.
  await lib.getByRole('tab', {name: '国内'}).click();
  await group('custom').getByRole('button', {name: '修改 家里 NAS'}).click();
  const edit = lib.getByRole('group', {name: '修改 家里 NAS'});
  await edit.getByLabel('端口').fill('8443');
  await edit.getByRole('button', {name: '确定'}).click();
  assert.match(await group('custom').locator('.sv-lib-host').innerText(), /^nas\.example\.com:8443$/);
  // 默认: the plain list without Telegram, with 东京 VPS; the domestic one with 家里 NAS, which is then taken out.
  await lib.getByRole('tab', {name: '默认'}).click();
  const list = name => lib.locator(`.sv-lib-group[data-group=${name}]`);
  assert.match(await list('optimized').locator('.sv-lib-chosen').innerText(), /^测 云南三网、Cloudflare、Google DNS、OpenAI、Claude、Gemini、GitHub、Telegram$/);
  await list('plain').getByRole('button', {name: '修改'}).click();
  await list('plain').getByRole('group', {name: '海外 · 普通线路：国外'}).getByRole('button', {name: 'Telegram'}).click();
  await list('plain').getByRole('group', {name: '海外 · 普通线路：国外'}).getByRole('button', {name: '东京 VPS'}).click();
  assert.match(await list('plain').locator('.sv-lib-chosen').innerText(), /^测 Cloudflare、Google DNS、OpenAI、Claude、Gemini、GitHub、东京 VPS$/);
  await list('domestic').getByRole('button', {name: '修改'}).click();
  await list('domestic').getByRole('group', {name: '国内服务器：国内'}).getByRole('button', {name: '家里 NAS'}).click();
  await lib.getByRole('tab', {name: '国内'}).click();
  assert.deepEqual(await tags('u-0a1b2c3d'), ['国内']);
  await group('custom').getByRole('button', {name: '删除 家里 NAS'}).click();
  await lib.getByText('还没有自己加的地址。').waitFor();
  await lib.getByRole('tab', {name: '默认'}).click();
  assert.match(await list('domestic').locator('.sv-lib-chosen').innerText(), /^测 云南三网、阿里 DNS、腾讯 DNS$/);
  // 恢复内置默认 puts a list back as it came.
  await list('plain').getByRole('button', {name: '恢复内置默认'}).click();
  assert.match(await list('plain').locator('.sv-lib-chosen').innerText(), /Telegram$/);
  await list('plain').getByRole('group', {name: '海外 · 普通线路：国外'}).getByRole('button', {name: 'Telegram'}).click();
  await dialog.getByRole('button', {name: '保存'}).click();
  await dialog.waitFor({state: 'detached'});
  const put = h.calls.find(c => c.path === '/api/servers/settings' && c.method === 'PUT').body.latency;
  assert.equal(put.custom.length, 2);
  const [tokyo, status] = put.custom;
  assert.match(tokyo.id, /^u-[0-9a-f]{8}$/);
  assert.deepEqual({...tokyo, id: ''}, {id: '', label: '东京 VPS', host: '203.0.113.9', port: 22, group: 'intl'});
  assert.deepEqual({...status, id: ''}, {id: '', label: '状态页', host: 'status.example.com', port: 443, group: 'intl', mode: 'http', url: 'https://status.example.com/'});
  assert.deepEqual(put.defaults, {domestic: ['three', 'ali-dns', 'tencent-dns'], optimized: ['three', 'cloudflare', 'google', 'openai', 'claude', 'gemini', 'github', 'telegram'],
    plain: ['cloudflare', 'google', 'openai', 'claude', 'gemini', 'github']});
  await h.close();
});
