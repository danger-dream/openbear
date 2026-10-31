// The 动态域名 page without a browser: lists, filters, details, forms, the node picker, the
// server card and links on the real DdnsView / DdnsCard in happy-dom — the requests they send,
// the texts, counts and states. Sizes, sheets, dragging, 16px inputs, sideways scroll, dark
// colours and alignment stay in ddns.browser.test.mjs.
import {window} from '../domTestEnv.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import {h} from 'vue';
import {openPage} from '../testing/appHarness.mjs';
import {settle, sleep} from '../testing/domPage.mjs';
import {fakeDdns, handleApi} from './ddnsFixture.mjs';

const View = (await import('./DdnsView.vue')).default, Card = (await import('./DdnsCard.vue')).default;
await import('../instances/instances.css'); await import('../servers/servers.css');

async function setup(t, {width = 1440, height = 900, card = '', focus = null} = {}) {
  const ddns = fakeDdns(), calls = [];
  const handler = request => {
    calls.push({path: request.path, query: request.url.search, method: request.method, body: request.body});
    const [status, json] = handleApi(ddns, request.method, request.path, request.body, request.url.searchParams);
    return {status, json};
  };
  const root = card ? {render: () => h('div', {class: 'instance-ui servers-ui'}, [h(Card, {serverId: card, active: true})])}
    : {render: () => h('div', {class: 'instance-ui servers-ui sv-body'}, [h(View, {active: true, focus})])};
  const {page, unmount} = await openPage(t, root, {url: '/__test', width, height, handler});
  if (!card) await page.locator('.dd-table tbody tr').first().waitFor();
  // The first request that matches, waiting for it as long as the browser test did.
  const called = async (check, label) => {
    for (let i = 0; i < 300; i++) {
      const hit = calls.find(c => { try { return check(c); } catch { return false; } });
      if (hit) return hit;
      await sleep(10); await settle();
    }
    assert.fail(`no ${label}`);
  };
  return {page, calls, ddns, called, close: () => unmount()};
}
const rows = page => page.locator('.dd-table tbody tr');
const names = async page => (await rows(page).locator('.dd-name').allInnerTexts()).map(s => s.trim());
const dialog = page => page.locator('.dd-dialog');

test('desktop: providers, filters, search, the account bar', async t => {
  const {page, called, close} = await setup(t);
  assert.equal(await rows(page).count(), 6);
  const all = page.locator('.dd-prov').first();
  assert.match(await all.innerText(), /全部解析[\s\S]*3 条需要关注[\s\S]*6/);
  assert.equal(await page.locator('.dd-table thead th').count(), 6, 'five columns and the chevron');
  // Colors by state group.
  const tone = async name => rows(page).filter({hasText: name}).locator('.dd-c-state .ix-pill').getAttribute('data-tone');
  assert.deepEqual([await tone('hk.example.net'), await tone('la.example.com'), await tone('old.example.org'), await tone('nas.example.cn')], ['warn', 'idle', 'bad', 'idle']);
  assert.equal(await rows(page).filter({hasText: 'home.example.com'}).nth(1).locator('.ix-pill').getAttribute('data-tone'), 'busy');
  assert.equal(await rows(page).first().locator('.ix-pill').innerText(), '正常', 'the controller\'s own words');
  await page.getByRole('tab', {name: '需要关注'}).click();
  assert.deepEqual(await names(page), ['hk.example.net', 'la.example.com', 'old.example.org']);
  await page.getByRole('tab', {name: '全部'}).click();
  await page.getByRole('searchbox', {name: '搜索'}).fill('香港');
  assert.deepEqual(await names(page), ['hk.example.net']);
  await page.getByRole('searchbox', {name: '搜索'}).fill('198.51.100.76');
  assert.deepEqual(await names(page), ['la.example.com']);
  await page.getByRole('searchbox', {name: '搜索'}).fill('');
  // An account: its records, the bar with 测试连接 and the settings.
  await page.locator('.dd-prov[data-account="a_dp"]').click();
  assert.deepEqual(await names(page), ['old.example.org']);
  const bar = page.locator('.dd-acct-bar');
  assert.match(await bar.innerText(), /DNSPod[\s\S]*认证失败/);
  await page.locator('.dd-prov[data-account="a_cf"]').click();
  assert.equal(await rows(page).count(), 4);
  assert.match(await bar.innerText(), /可管理：example\.com、example\.net/);
  await bar.getByRole('button', {name: '测试连接'}).click();
  await called(c => c.path === '/api/ddns/accounts/a_cf/check' && c.method === 'POST', 'check');
  await page.locator('.el-message').filter({hasText: '无法提前确认写权限'}).waitFor();
  await close();
});

test('desktop: details, diagnosis, events, pause, sync, delete', async t => {
  const {page, called, calls, close} = await setup(t);
  await rows(page).filter({hasText: 'hk.example.net'}).click();
  const drawer = page.locator('.dd-drawer');
  await drawer.waitFor();
  assert.match(await drawer.locator('.dd-note').first().innerText(), /服务商上的值被改成了 203\.0\.113\.99[\s\S]*已告警并改回 203\.0\.113\.42/);
  assert.match(await drawer.locator('.dd-kv').innerText(), /期望值（来自来源）\s*203\.0\.113\.42[\s\S]*服务商当前值\s*203\.0\.113\.99[\s\S]*香港 HK1 · 网卡/);
  assert.equal(await drawer.locator('.dd-v.is-diff').count(), 1);
  await drawer.locator('.dd-events li').first().waitFor();
  assert.equal(await drawer.locator('.dd-events li').count(), 3);
  assert.equal(await drawer.locator('.dd-events [data-tone="warn"]').count(), 1);
  await drawer.getByRole('button', {name: '诊断'}).click();
  const diag = await called(c => c.path === '/api/ddns/diagnose', 'diagnose');
  assert.deepEqual(diag.body, {serverId: 'hk', family: 'v4'});
  await drawer.locator('.dd-diag li').first().waitFor();
  assert.match(await drawer.locator('.dd-diag').innerText(), /eth0 · 默认[\s\S]*2001:db8:1f0::abcd（临时）/);
  // Actions.
  await drawer.getByRole('button', {name: '停用'}).click();
  assert.deepEqual((await called(c => c.method === 'PATCH' && c.path === '/api/ddns/records/r3', 'pause')).body, {enabled: false});
  await drawer.getByRole('button', {name: '启用'}).waitFor();
  await drawer.getByRole('button', {name: '启用'}).click();
  await drawer.getByRole('button', {name: '立即同步'}).waitFor();
  await page.waitForFunction(() => !document.querySelector('.dd-drawer .is-loading'));
  await drawer.getByRole('button', {name: '立即同步'}).click();
  await called(c => c.path === '/api/ddns/records/r3/sync', 'sync');
  // Delete: the provider's record stays unless asked.
  await drawer.getByRole('button', {name: '删除', exact: true}).click();
  const remote = drawer.getByRole('checkbox', {name: /同时删除服务商上的记录/});
  assert.equal(await remote.isChecked(), false);
  await remote.check();
  await drawer.getByRole('button', {name: '删除解析和服务商记录'}).waitFor();
  await remote.uncheck();
  await drawer.getByRole('button', {name: '删除解析', exact: true}).click();
  const del = await called(c => c.method === 'DELETE', 'delete');
  assert.equal(del.path + del.query, '/api/ddns/records/r3');
  await page.locator('.el-message').filter({hasText: '服务商上的记录保持原样'}).waitFor();
  await drawer.waitFor({state: 'hidden'});
  assert.equal(await rows(page).count(), 5);
  // With the remote record.
  await rows(page).filter({hasText: 'la.example.com'}).click();
  await drawer.getByRole('button', {name: '删除', exact: true}).click();
  await drawer.getByRole('checkbox', {name: /同时删除服务商上的记录/}).check();
  await drawer.getByRole('button', {name: '删除解析和服务商记录'}).click();
  await called(c => c.method === 'DELETE' && c.query === '?deleteRemote=1' && c.path.endsWith('/r4'), 'delete remote');
  assert.equal(calls.filter(c => c.method === 'DELETE').length, 2);
  await close();
});

test('desktop: the node picker, warnings, preview with takeovers, save, edit', async t => {
  const {page, called, calls, close} = await setup(t);
  await page.getByRole('button', {name: '添加解析'}).click();
  const d = dialog(page);
  await d.waitFor();
  const picker = d.getByRole('button', {name: /^来源节点：/});
  assert.match(await picker.innerText(), /家里 NAS[\s\S]*198\.51\.100\.17/);
  // Search and keyboard; Esc closes only the list.
  await picker.click();
  const search = page.getByRole('combobox', {name: '搜索节点'});
  await search.waitFor();
  await page.waitForFunction(() => document.activeElement?.getAttribute('aria-label') === '搜索节点');
  assert.ok(await search.evaluate(el => el === document.activeElement), 'the dialog does not take the focus back');
  const options = page.locator('.dd-node-panel [role="option"]');
  assert.equal(await options.count(), 4);
  assert.match(await options.filter({hasText: 'Los Angeles'}).innerText(), /离线/);
  assert.match(await options.filter({hasText: '法兰克福旧机'}).innerText(), /需升级/);
  assert.ok((await options.first().locator('.ss-flag').innerText()).length > 0, 'flags');
  await search.fill('hk');
  assert.equal(await options.count(), 1);
  await search.fill('');
  await search.press('ArrowDown');
  await search.press('ArrowDown');
  assert.equal(await page.locator('.dd-node-panel .ss-option.is-active').innerText().then(s => /Los Angeles/.test(s)), true);
  await search.press('Escape');
  await search.waitFor({state: 'hidden'});
  assert.ok(await d.isVisible(), 'Esc closed only the picker');
  assert.ok(await picker.evaluate(el => el === document.activeElement), 'focus back on the picker');
  await picker.press('ArrowDown');
  await search.waitFor();
  await search.fill('香港');
  await search.press('Enter');
  await search.waitFor({state: 'hidden'});
  assert.match(await picker.innerText(), /香港 HK1/);
  // Command needs command execution on the node; the old node needs an update.
  await d.locator('.dd-adv > summary').click();
  await d.getByRole('radio', {name: '命令'}).check();
  await d.getByText('这台节点关闭了命令执行').waitFor();
  assert.equal(await d.getByTestId('dd-save').isDisabled(), true);
  await d.getByRole('radio', {name: '自动（推荐）'}).check();
  await picker.click(); await search.fill('法兰克福'); await search.press('Enter');
  await d.getByText('节点版本过旧，需要升级').waitFor();
  await picker.click(); await search.fill('家里'); await search.press('Enter');
  // Domains → preview.
  await d.getByRole('textbox', {name: /域名/}).fill('www.example.com\ntake.example.com\ntake2.example.cn\nsame.example.com\nhome.example.com');
  await d.getByRole('checkbox', {name: /IPv6（AAAA）/}).uncheck();
  const prev = await called(c => c.path === '/api/ddns/preview' && c.body.names.length === 5 && c.body.types.length === 1, 'preview');
  assert.deepEqual(prev.body.types, ['A']);
  assert.equal(prev.body.serverId, 'home');
  const pr = d.locator('.dd-pr[data-action]');
  await pr.nth(4).waitFor();
  assert.deepEqual(await pr.locator('.ix-pill').allInnerTexts(), ['新建', '需要接管确认', '需要接管确认', '无需修改', '已由 OpenBear 管理']);
  assert.match(await pr.nth(2).innerText(), /同名共 2 条/);
  const note = d.locator('.dd-save-note');
  assert.equal(await note.innerText(), '2 条未确认接管，不会保存');
  assert.equal(await d.getByTestId('dd-save').innerText(), '保存 2 条');
  await pr.nth(1).getByRole('checkbox').check();
  assert.equal(await note.innerText(), '1 条未确认接管，不会保存');
  assert.equal(await d.getByTestId('dd-save').innerText(), '保存 3 条');
  // Provider options of the accounts written to: Cloudflare's proxy, Aliyun's line.
  await d.getByRole('combobox', {name: /Cloudflare 代理/}).selectOption('on');
  await d.getByRole('combobox', {name: /解析线路/}).selectOption('telecom');
  await d.getByRole('combobox', {name: 'TTL'}).selectOption({label: '5 分钟'});
  await d.getByRole('checkbox', {name: /允许写入内网地址/}).check();
  await d.getByRole('combobox', {name: '被外部改动时'}).selectOption('alert');
  await d.getByRole('button', {name: /^备用来源：/}).click();
  await search.fill('hk'); await search.press('Enter');
  await d.getByRole('combobox', {name: '主来源离线多久后切换'}).selectOption({label: '15 分钟'});
  // A different provider line needs a fresh preview and takeover confirmation.
  await pr.nth(4).waitFor();
  assert.equal(await pr.nth(1).getByRole('checkbox').isChecked(), false);
  await pr.nth(1).getByRole('checkbox').check();
  await d.getByTestId('dd-save').click();
  const saved = await called(c => c.method === 'POST' && c.path === '/api/ddns/records', 'save');
  assert.deepEqual(saved.body.rows, [{name: 'www.example.com', type: 'A', accountId: 'a_cf', confirmTakeover: false}, {name: 'take.example.com', type: 'A', accountId: 'a_cf', confirmTakeover: true},
    {name: 'same.example.com', type: 'A', accountId: 'a_cf', confirmTakeover: true}], 'a record already holding the value is adopted without a question');
  assert.deepEqual({backup: saved.body.backup, ttl: saved.body.ttl, options: saved.body.options, policy: saved.body.policy},
    {backup: {serverId: 'hk', afterS: 900}, ttl: 300, options: {proxied: true, line: 'telecom'}, policy: {multi: 'one', allowPrivate: true, onDrift: 'alert'}});
  await d.waitFor({state: 'hidden'});
  await page.locator('.el-message').filter({hasText: '已保存 3 条'}).waitFor();
  await rows(page).filter({hasText: 'take.example.com'}).waitFor();
  // Edit uses the same form, filled from the record.
  await rows(page).filter({hasText: 'home.example.com'}).first().click();
  await page.locator('.dd-drawer').getByRole('button', {name: '编辑'}).click();
  await d.waitFor();
  assert.equal(await d.locator('.el-dialog__title').innerText(), '编辑解析');
  assert.equal(await d.getByRole('textbox', {name: /域名/}).isDisabled(), true);
  assert.equal(await d.getByRole('combobox', {name: /Cloudflare 代理/}).inputValue(), 'off');
  assert.match(await d.getByRole('button', {name: /^备用来源：/}).innerText(), /香港 HK1/);
  await d.getByRole('radio', {name: '接口'}).check();
  await d.getByTestId('dd-save').click();
  const patch = await called(c => c.method === 'PATCH' && c.path === '/api/ddns/records/r1', 'edit');
  assert.equal(patch.body.source.method, 'url');
  assert.deepEqual(patch.body.source.params.urls, ['https://api.ipify.org', 'https://4.icanhazip.com']);
  assert.deepEqual(patch.body.options, {proxied: false});
  assert.ok(!calls.some(c => c.method === 'POST' && c.path === '/api/ddns/records' && c !== saved));
  await close();
});

test('desktop: add a provider: tiers, search, picked collapses, dynamic fields, secret store, settings', async t => {
  const {page, called, close} = await setup(t);
  await page.getByRole('button', {name: '添加服务商'}).click();
  const d = dialog(page);
  await d.waitFor();
  assert.deepEqual(await d.locator('.dd-pgroup > h5').allInnerTexts(), ['A 档 · 令牌即可接入', 'B 档 · 需要签名（AccessKey）', 'C 档 · 标准协议 / 通用回调']);
  await d.getByRole('searchbox', {name: '搜索服务商'}).fill('rfc');
  assert.equal(await d.locator('.dd-pcard').count(), 1);
  await d.locator('.dd-pcard[data-type="rfc2136"]').click();
  assert.equal(await d.locator('.dd-pgrid').count(), 0, 'the grid collapsed');
  assert.match(await d.locator('.dd-picked').innerText(), /RFC 2136[\s\S]*更换/);
  // Every kind of field.
  assert.equal(await d.getByLabel('TSIG 算法').evaluate(el => el.tagName), 'SELECT');
  assert.equal(await d.getByLabel('端口（可选）').getAttribute('type'), 'number');
  assert.equal(await d.getByLabel('TSIG 密钥').getAttribute('type'), 'password');
  assert.equal(await d.getByLabel('备注（可选）').evaluate(el => el.tagName), 'TEXTAREA');
  await d.getByRole('checkbox', {name: '使用 TCP'}).waitFor();
  assert.match(await d.locator('.dd-save-note').innerText(), /DNS 服务器、TSIG 密钥/);
  // No 备用 DNS among the advanced options.
  await d.locator('.dd-adv > summary').click();
  assert.deepEqual(await d.locator('.dd-adv-grid > .dd-field > span').allInnerTexts(), ['API 端点', '代理', '默认 TTL']);
  await d.getByRole('button', {name: '更换'}).click();
  await d.getByRole('searchbox', {name: '搜索服务商'}).fill('');
  await d.locator('.dd-pcard[data-type="cloudflare"]').click();
  await d.getByLabel('API Token').fill('bad');
  await d.getByRole('button', {name: '测试连接'}).click();
  await d.locator('.dd-note[data-tone="bad"]').filter({hasText: '认证失败'}).waitFor();
  await d.getByLabel('API Token').fill('tok-1');
  await d.getByRole('button', {name: '测试连接'}).click();
  await d.locator('.dd-note[data-tone="ok"]').filter({hasText: '可管理 2 个域'}).waitFor();
  // From the secret store, keys mapped by name.
  await d.getByRole('checkbox', {name: /使用已保存的动态域名凭据/}).check();
  // Only entries the DDNS page saved are offered.
  assert.equal(await d.getByRole('combobox', {name: '凭据条目'}).locator('option[value="40"]').count(), 0);
  await d.getByRole('combobox', {name: '凭据条目'}).selectOption('31');
  await d.locator('.dd-map').waitFor();
  await page.waitForFunction(() => document.querySelector('.dd-map select')?.value === 'API_TOKEN');
  await d.getByRole('textbox', {name: '名称'}).fill('备用账号');
  await d.getByTestId('dd-account-save').click();
  const add = await called(c => c.method === 'POST' && c.path === '/api/ddns/accounts', 'add account');
  assert.deepEqual(add.body, {type: 'cloudflare', alias: '备用账号', settings: {endpoint: '', proxy: '', ttl: null}, secretRef: {secretId: 31, map: {api_token: 'API_TOKEN'}}});
  await d.waitFor({state: 'hidden'});
  // The new account is selected; its settings: rename, then delete (no records).
  await page.locator('.dd-acct-bar').filter({hasText: '备用账号'}).waitFor();
  // Settings of an account with records: delete needs force.
  await page.locator('.dd-prov[data-account="a_ali"]').click();
  await page.getByRole('button', {name: '服务商设置'}).click();
  await d.waitFor();
  assert.equal(await d.getByRole('radio', {name: /保持现有/}).isChecked(), true);
  await d.getByRole('button', {name: '删除账户'}).click();
  const confirm = d.getByRole('button', {name: '确认删除'});
  assert.equal(await confirm.isDisabled(), true);
  await d.getByRole('checkbox', {name: /同时停止管理这 1 条解析/}).check();
  await confirm.click();
  const del = await called(c => c.method === 'DELETE' && c.path === '/api/ddns/accounts/a_ali', 'delete account');
  assert.equal(del.query, '?force=1');
  await d.waitFor({state: 'hidden'});
  assert.equal(await page.locator('.dd-prov[data-account="a_ali"]').count(), 0);
  await close();
});

test('phone 390px: rows read name, type, state, then source and value; the add sheet previews', async t => {
  const {page, called, close} = await setup(t, {width: 390, height: 844});
  assert.match(await rows(page).first().innerText(), /home\.example\.com\s*A[\s\S]*家里 NAS · STUN · 198\.51\.100\.17/);
  await rows(page).filter({hasText: 'hk.example.net'}).click();
  const sheet = page.locator('.mesh-sheet.dd-sheet:visible');
  await sheet.waitFor();
  await sheet.locator('.dd-events li').first().waitFor();
  assert.ok(await sheet.getByRole('button', {name: '关闭'}).isVisible(), 'a close button');
  await sheet.getByRole('button', {name: '关闭'}).click();
  await sheet.waitFor({state: 'hidden'});
  await page.getByRole('button', {name: '添加解析'}).click();
  await sheet.waitFor();
  await sheet.getByRole('textbox', {name: /域名/}).fill('take.example.com\nnew-very-long-subdomain-name-for-layout.example.com');
  await sheet.locator('.dd-pr[data-action]').first().waitFor();
  await called(c => c.path === '/api/ddns/preview', 'preview');
  await close();
});

test('a server\'s detail card: its records, the way to /ddns; not a node: no card', async t => {
  const {page, close} = await setup(t, {card: 'home'});
  const card = page.locator('.dd-card');
  await card.waitFor();
  assert.deepEqual(await card.locator('.dd-card-name').allInnerTexts(), ['home.example.com', 'home.example.com', 'nas.example.cn']);
  const opened = [];
  window.addEventListener('openbear:open-ddns', e => opened.push(e.detail));
  await card.locator('.dd-card-row').first().click();
  await card.getByRole('button', {name: '全部查看'}).click();
  assert.deepEqual(opened, [{serverId: 'home', recordId: 'r1'}, {serverId: 'home'}]);
  await close();
  // hk is the backup of r1 and the source of r3: both show; a node without records shows 添加解析.
  const other = await setup(t, {card: 'old2'});
  // Wait for the card's request to be answered instead of a fixed pause.
  await other.called(c => c.path === '/api/ddns', 'load');
  await settle(10);
  assert.equal(await other.page.locator('.dd-card').count(), 0, 'not a node: no card');
  await other.close();
});

test('a link: to a machine narrows the list, to a record opens it, to add opens the form for that machine', async t => {
  const opened = await setup(t, {focus: {serverId: 'hk', recordId: 'r3', add: false, nonce: 1}});
  await opened.page.locator('.dd-drawer').waitFor();
  assert.match(await opened.page.locator('.dd-drawer').innerText(), /hk\.example\.net/);
  await opened.close();
  const adding = await setup(t, {focus: {serverId: 'hk', recordId: '', add: true, nonce: 1}});
  await dialog(adding.page).waitFor();
  assert.match(await dialog(adding.page).getByRole('button', {name: /^来源节点：/}).innerText(), /香港 HK1/);
  await adding.close();
  const {page, close} = await setup(t, {focus: {serverId: 'hk', recordId: '', add: false, nonce: 1}});
  assert.deepEqual(await names(page), ['home.example.com', 'hk.example.net'], 'source or backup');
  await page.locator('.dd-chip').filter({hasText: '香港 HK1'}).waitFor();
  await page.getByRole('button', {name: '不再按这台机器筛选'}).click();
  assert.equal(await rows(page).count(), 6);
  await close();
});
