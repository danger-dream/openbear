// The 评测 tab without a browser: the tree, reports, comparison, deletion, the start form and its
// warnings, a running run and its polling — the requests sent and what is shown, in happy-dom with
// the browser test's fake controller (../testing/benchFixture.mjs). Sizes, scrolling inside the
// report boxes, phone sheets and the dark theme stay in bench.browser.test.mjs.
import test from 'node:test';
import assert from 'node:assert/strict';
import {mountPage} from '../testing/pageDom.mjs';
import {linux, SERVERS, pageItem, IP_REPORT, CATALOG, T, run, fakeBench} from '../testing/benchFixture.mjs';

// The browser test runs in Asia/Shanghai (timezoneId): times are shown in it.
process.env.TZ = 'Asia/Shanghai';
// What a running run's job log answers (as in the browser test).
const JOB = {ok: true, job: {id: 'x', state: 'running'}, log: {data: '正在安装依赖…\r依赖已就绪   \n三网 TCP 大包延迟 \x1b[32m进行中\x1b[0m 12/31\r三网 TCP 大包延迟 \x1b[32m进行中\x1b[0m 29/31\n', offset: 0, next: 1, size: 1, first: 0}};

async function setup(t, {width = 1440, height = 900, dark = false, page: whole = false, local = {}} = {}) {
  const bench = fakeBench();
  const h = await mountPage(t, {width, height, dark, local,
    load: n => whole ? import(`../../views/ServersView.vue?fresh=${n}`) : import(`./BenchView.vue?fresh=${n}`),
    root: (View, h) => whole
      ? {render: () => h('div', {class: 'app-shell h-full flex', style: 'height:100vh'}, [h('aside', {style: 'width:220px;flex:none'}), h('main', {class: 'app-main flex-1 min-w-0 flex flex-col'}, [h(View)])])}
      : {render: () => h('div', {class: 'instance-ui servers-ui sv-body', style: 'height:100vh'}, [h(View, {active: true, servers: SERVERS})])},
    session: whole ? {'openbear.servers.tabs': JSON.stringify({tabs: [], active: 'bench'})} : {},
    handle: async (method, path, body) => {
      let status = 200, data = {ok: true};
      if (path.startsWith('/api/servers/bench')) [status, data] = bench.handle(method, path.slice('/api/servers/bench'.length), body);
      else if (path === '/api/servers') data = {ok: true, items: SERVERS.map(pageItem), settings: {accessPoints: [], defaultAccess: '', timezone: 'Asia/Shanghai'}, node: {available: true, version: '0.14.0'}};
      else if (path === '/api/servers/live') data = {ok: true, at: Date.now(), items: Object.fromEntries(SERVERS.map(x => [x.id, pageItem(x).live]))};
      else if (/^\/api\/servers\/[^/]+\/jobs\//.test(path)) data = JOB;
      return [status, data];
    }});
  await h.page.locator('.bn-nodes').waitFor();
  // The runs are in (the browser test waited 200 ms for them).
  await h.page.locator('.bn-nodes li[data-server]').first().waitFor({state: 'attached'});
  return {...h, bench, close: async () => { assert.deepEqual(h.errors, []); await h.close(); }};
}
const server = (page, id) => page.locator(`li[data-server="${id}"]`);
const runRow = (page, id) => page.locator(`li[data-run="${id}"] .bn-row-main`);
// Servers open folded, a test's earlier runs too; the time leaves out this year.
const expand = (page, id) => server(page, id).locator('.bn-row-server .bn-row-main').click();
const earlier = (page, id, test) => server(page, id).locator(`li[data-test="${test}"] .bn-older`).click();

test('terminal detail failures retry while summaries stay unchanged', async t => {
  const {page, bench, close} = await setup(t);
  await runRow(page, 'r5').click();
  await page.locator('.bn-main .bn-live').waitFor();
  const original = bench.handle;
  let failures = 0;
  bench.handle = (method, path, body) => {
    if (path === '/runs/r5' && failures++ === 0) return [503, {error: 'temporary'}];
    return original(method, path, body);
  };
  bench.state.runs.find(r => r.id === 'r5').state = 'done';
  await page.locator('.bn-main .bn-term').filter({hasText: '网络质量体检报告'}).waitFor({timeout: 15000});
  assert.ok(failures >= 2, 'unchanged terminal summary still retries the failed detail');
  await close();
});

test('a timed-out report shows its limit and interruption reason', async t => {
  const {page, bench, calls, close} = await setup(t);
  await runRow(page, 'r5').click();
  await page.locator('.bn-main .bn-live').waitFor();
  Object.assign(bench.state.runs.find(r => r.id === 'r5'), {
    state: 'interrupted', error: 'bench_timeout', detail: '超过 1 小时未完成，已自动停止', endedMs: T,
  });
  const note = page.locator('.bn-main .bn-note');
  await note.waitFor({timeout: 15000});
  assert.equal(await note.innerText(), '超过 1 小时未完成，已自动停止');
  assert.equal(await page.locator('.bn-main .bn-rep-head .ix-pill').innerText(), '中断');
  if (!await runRow(page, 'r5').count()) await expand(page, 'jp');
  assert.equal(await runRow(page, 'r5').locator('.ix-pill').innerText(), '中断');
  const count = calls.filter(c => /\/jobs\//.test(c.path)).length;
  await page.waitForTimeout(3500);
  assert.equal(calls.filter(c => /\/jobs\//.test(c.path)).length, count);
  await close();
});

test('desktop: a running report stops asking the node once the tab is left', async t => {
  const {page, calls, close} = await setup(t, {page: true});
  await runRow(page, 'r5').click();
  await page.locator('.bn-main .bn-live').waitFor();
  await page.waitForTimeout(3500);
  const asked = () => calls.filter(c => /\/api\/servers\/jp\/jobs\//.test(c.path)).length;
  const before = asked();
  assert.ok(before >= 2, 'it follows the job while shown');
  await page.locator('.sv-tabs [role="tab"]', {hasText: '探针'}).click();
  await page.waitForTimeout(3500);
  assert.equal(asked(), before, 'nothing asked while another tab is shown');
  await page.locator('.sv-tabs [role="tab"]', {hasText: '评测'}).click();
  assert.ok(asked() > before, 'and it picks up again on return');
  await close();
});

test('desktop: the tree, a report, comparison and deletion', async t => {
  const {page, calls, close} = await setup(t);
  // Servers with runs, folded but the one running a test; Windows cannot run tests and has none, so it is not listed.
  assert.deepEqual(await page.locator('.bn-row-server .bn-name').allInnerTexts(), ['OVH.SG', 'RFCHost-JP2.T1', 'Misaka-Los Angeles LAX02 一个很长很长的服务器名字']);
  assert.match(await server(page, 'jp').locator('.bn-row-server .bn-count').innerText(), /1 项进行中/);
  assert.equal(await runRow(page, 'r5').count(), 1, 'open while it runs a test');
  assert.match(await server(page, 'sg').locator('.bn-row-server .bn-count').innerText(), /^4 条 · 最近 (2026\.)?10\.30 22:30$/);
  assert.equal(await server(page, 'sg').locator('li[data-run]').count(), 0, 'folded');
  // A row a test, in the categories' order, with its newest run; the earlier ones fold under it.
  await expand(page, 'sg');
  assert.deepEqual(await server(page, 'sg').locator('.bn-row-test .bn-name').allInnerTexts(), ['IP.Check.Place', 'Net.Check.Place', 'Geekbench 5']);
  assert.equal(await page.locator('.bn-row-cat').count(), 0);
  // The run's label is the row's title; the earlier runs' rows show theirs.
  assert.match(await runRow(page, 'r2').innerText(), /^IP\.Check\.Place\s*(2026\.)?10\.30 22:00$/);
  assert.equal(await runRow(page, 'r2').getAttribute('title'), 'IP.Check.Place · 2026.10.30 22:00 · IPv4 + IPv6');
  assert.equal(await runRow(page, 'r1').count(), 0);
  assert.match(await server(page, 'sg').locator('li[data-test="ipcheck"] .bn-older').innerText(), /更早 1 次/);
  assert.equal(await server(page, 'sg').locator('li[data-test="netcheck"] .bn-older').count(), 0);
  await earlier(page, 'sg', 'ipcheck');
  assert.match(await runRow(page, 'r1').innerText(), /(2026\.)?10\.18 23:00/);
  await expand(page, 'us');
  assert.equal(await runRow(page, 'r6').locator('.ix-pill').innerText(), '失败');
  const opened = window.localStorage.getItem('openbear.servers.bench.expanded');
  assert.match(await page.locator('.bn-intro').innerText(), /服务器评测/);

  await runRow(page, 'r2').click();
  const report = page.locator('.bn-main .bn-report');
  await report.locator('.bn-section').first().waitFor();
  // Two reports: one at a time, by tabs.
  assert.deepEqual(await report.locator('.bn-section-tabs [role="tab"]').allInnerTexts(), ['IPv4', 'IPv6']);
  assert.equal(await report.locator('.bn-term:visible').count(), 1);
  assert.match(await report.locator('.bn-term:visible').innerText(), /IP质量体检报告|IP QUALITY CHECK REPORT/);
  await report.getByRole('tab', {name: 'IPv6'}).click();
  assert.match(await report.locator('.bn-term:visible').innerText(), /IP质量体检报告：2402/);
  assert.ok(await report.locator('.bn-term:visible .bn-w2').count() > 5, 'CJK sits in two-cell boxes');
  await report.getByRole('tab', {name: 'IPv4'}).click();
  assert.equal(await report.locator('.bn-link').getAttribute('href'), 'https://Report.Check.Place/ip/abc');
  assert.match(await report.locator('.bn-rep-meta').innerText(), /IPv4 \+ IPv6 · ip\.check\.place v0\.1\.0 · 1 分 33 秒/);
  assert.doesNotMatch(await report.innerText(), /运行日志|已安装依赖/, 'a report already says what the log ends with');

  // A failed run says why.
  await runRow(page, 'r6').click();
  await page.locator('.bn-main .bn-note').waitFor();
  assert.match(await page.locator('.bn-main .bn-note').innerText(), /没有产生报告/);
  // Without a report the log is all there is.
  await page.locator('.bn-main .bn-section h4', {hasText: '运行日志'}).waitFor();
  assert.match(await page.locator('.bn-main .bn-term').innerText(), /Could not resolve host/);

  // Two runs side by side, scrolling together.
  await page.locator('li[data-run="r1"] .bn-pick input').check();
  await page.locator('li[data-run="r2"] .bn-pick input').check();
  assert.match(await page.locator('.bn-pickbar').innerText(), /已选 2 条/);
  await page.getByTestId('bn-compare').click();
  const cols = page.locator('.bn-compare-col');
  await cols.nth(1).locator('.bn-section').first().waitFor();
  assert.equal(await cols.count(), 2);
  await page.getByRole('button', {name: '退出对比'}).click();

  // Deleting asks first.
  await runRow(page, 'r1').click();
  await page.locator('.bn-main .bn-report .dd-danger').click();
  await page.locator('.el-message-box').getByRole('button', {name: '删除'}).click();
  await page.locator('li[data-run="r1"]').waitFor({state: 'detached'});
  assert.ok(calls.some(c => c.method === 'DELETE' && c.path === '/api/servers/bench/runs/r1'));
  await close();

  // What was open is open on the next visit, the earlier runs too; what was folded stays so.
  const again = await setup(t, {local: {'openbear.servers.bench.expanded': opened}});
  await runRow(again.page, 'r1').waitFor();
  await runRow(again.page, 'r6').waitFor();
  await expand(again.page, 'sg');
  assert.equal(await runRow(again.page, 'r2').count(), 0);
  await again.close();
});

test('desktop: starting a test, warnings, a running run', async t => {
  const {page, calls, close} = await setup(t);
  await server(page, 'jp').locator('.bn-row-server .bn-add').click();
  const form = page.locator('.dd-dialog .bn-form');
  await form.waitFor();
  // The server picker shows flags (with the page's flag font, which a native select cannot use on Windows).
  const picker = form.locator('.dd-node-trigger');
  assert.match(await picker.getAttribute('aria-label'), /服务器：RFCHost-JP2\.T1/);
  assert.equal(await picker.locator('.ss-flag').innerText(), '🇯🇵');
  await picker.click();
  const panel = page.locator('.dd-node-panel');
  await panel.waitFor();
  assert.deepEqual(await panel.locator('.ss-option .ss-name').allInnerTexts(), ['OVH.SG', 'RFCHost-JP2.T1', 'Misaka-Los Angeles LAX02 一个很长很长的服务器名字', 'Idle-DE'],
    'a server without runs is not in the tree but can be picked here; Windows, macOS and a node without command execution cannot');
  assert.deepEqual(await panel.locator('.ss-option .ss-flag').allInnerTexts(), ['🇸🇬', '🇯🇵', '🇺🇸', '🇩🇪']);
  assert.equal(await panel.locator('.dd-tag-warn').count(), 0, 'nothing marked 需升级');
  assert.deepEqual(await panel.locator('.ss-option .ss-via').allInnerTexts(), ['198.51.100.6', '198.51.100.14', '198.51.100.37', '离线']);
  await panel.locator('.ss-option', {hasText: 'Idle-DE'}).dispatchEvent('pointerdown');
  await panel.waitFor({state: 'hidden'});
  assert.match(await picker.getAttribute('aria-label'), /服务器：Idle-DE/);
  await picker.click();
  await panel.locator('.ss-option', {hasText: 'RFCHost-JP2.T1'}).dispatchEvent('pointerdown');
  await panel.waitFor({state: 'hidden'});
  const check = name => form.getByRole('checkbox', {name, exact: true});
  assert.equal(await check('综合评测').isChecked(), true, 'the suite is chosen by default');
  assert.equal(await check('IP.Check.Place').isDisabled(), true, 'a test inside the suite is shown as included');
  await check('综合评测').uncheck();
  await check('Net.Check.Place').check();
  assert.deepEqual(await form.locator('.bn-warnings li').allInnerTexts(), ['会安装依赖。']);
  await form.locator('.dd-choices label', {hasText: '完整模式'}).click();
  assert.match(await form.locator('.bn-warnings').innerText(), /消耗大量流量/);
  await page.getByTestId('bn-start').click();
  await form.waitFor({state: 'detached'});
  const posted = calls.find(c => c.method === 'POST' && c.path === '/api/servers/bench/runs');
  assert.deepEqual(posted.body, {serverId: 'jp', test: 'netcheck', options: {family: '4', mode: 'full'}});
  // The new run opens, with the node's latest output while it runs.
  const live = page.locator('.bn-main .bn-live');
  await live.waitFor();
  assert.match(await live.innerText(), /依赖已就绪[\s\S]*29\/31/);
  assert.doesNotMatch(await live.innerText(), /12\/31/, 'a redrawn line shows its last state');
  assert.equal(await page.locator('.bn-main .bn-rep-actions .el-button').innerText(), '停止');

  // The suite queues several runs.
  await page.getByTestId('bn-new').click();
  await form.waitFor();
  assert.deepEqual(await form.locator('.bn-warnings li').allInnerTexts(), ['会用系统包管理器安装缺少的依赖（合并）。', '压力测试会占满资源。'], 'the suite warns once of each thing');
  assert.match(await form.locator('.bn-item', {hasText: '综合评测'}).innerText(), /耗时：约 20～30 分钟/);
  await page.getByTestId('bn-start').click();
  await form.waitFor({state: 'detached'});
  const suite = calls.filter(c => c.method === 'POST' && c.path === '/api/servers/bench/runs').at(-1);
  assert.deepEqual(suite.body, {serverId: 'sg', suite: true, options: {family: '4'}});

  // Several tests at once: the suite and more beside it, each its own run in the server's queue.
  await page.getByTestId('bn-new').click();
  await form.waitFor();
  await check('Geekbench 5').check();
  await check('Service Unlock').check();
  assert.match(await form.getByTestId('bn-count').innerText(), /共 5 个评测/);
  assert.deepEqual(await form.locator('.bn-warnings li').allInnerTexts(), ['会用系统包管理器安装缺少的依赖（合并）。', '压力测试会占满资源。', '成绩会上传到 Geekbench 官网。']);
  await page.getByTestId('bn-start').click();
  await form.waitFor({state: 'detached'});
  const more = calls.filter(c => c.method === 'POST' && c.path === '/api/servers/bench/runs').at(-1);
  assert.deepEqual(more.body, {serverId: 'sg', suite: true, options: {family: '4'}, tests: [{test: 'geekbench5', options: {}}, {test: 'unlock', options: {region: 'auto'}}]});

  // Two tests with the same option set it once.
  await page.getByTestId('bn-new').click();
  await form.waitFor();
  await check('综合评测').uncheck();
  await check('IP.Check.Place').check();
  await check('Net.Check.Place').check();
  const common = form.locator('.bn-item', {hasText: '共用选项'});
  assert.match(await common.innerText(), /对IP\.Check\.Place、Net\.Check\.Place都生效/);
  await common.locator('.dd-choices label', {hasText: 'IPv4 + IPv6'}).click();
  assert.equal(await form.locator('.bn-item', {hasText: '三网延迟与回程'}).getByText('协议').count(), 0, 'the shared option is not asked again per test');
  await page.getByTestId('bn-start').click();
  await form.waitFor({state: 'detached'});
  const pair = calls.filter(c => c.method === 'POST' && c.path === '/api/servers/bench/runs').at(-1);
  assert.deepEqual(pair.body, {serverId: 'sg', tests: [{test: 'ipcheck', options: {family: '46', report: false}}, {test: 'netcheck', options: {family: '46', mode: 'low'}}]});
  await close();
});

test('phone: the form as a sheet', async t => {
  const {page, close} = await setup(t, {width: 390, height: 844});
  await page.getByTestId('bn-new').click();
  const sheet = page.getByRole('dialog', {name: '运行评测'});
  await sheet.locator('.bn-form').waitFor();
  await sheet.getByRole('checkbox', {name: '综合评测', exact: true}).uncheck();
  await sheet.getByRole('checkbox', {name: 'Net.Check.Place 回程路由', exact: true}).check();
  assert.equal(await sheet.getByLabel('省份').evaluate(el => el.tagName), 'SELECT', 'long choices are a list');
  await close();
});

for (const [width, height, name] of [[1440, 900, 'desktop'], [1180, 760, 'laptop']]) {
  test(`${name}: a report inside the servers page fits and scrolls`, async t => {
    const {page, close} = await setup(t, {width, height, page: true});
    await page.locator('.bn-nodes').waitFor();
    await expand(page, 'sg');
    await runRow(page, 'r7').click();
    const term = page.locator('.bn-main .bn-term').first();
    await term.waitFor();
    // A real mouse wheel over the box scrolls it.
    // The tree lists servers with runs only.
    assert.deepEqual(await page.locator('.bn-row-server .bn-name').allInnerTexts(), ['OVH.SG', 'RFCHost-JP2.T1', 'Misaka-Los Angeles LAX02 一个很长很长的服务器名字']);
    await close();
  });
}

for (const dark of [false, true]) {
  test(`phone${dark ? ', dark' : ''}: the tree alone, sheets for reports and the comparison`, async t => {
    const {page, close} = await setup(t, {width: 390, height: 844, dark});
    assert.equal(await page.locator('.bn-main').count(), 0, 'no right pane on a phone');
    await expand(page, 'sg');
    await runRow(page, 'r2').click();
    const sheet = page.getByRole('dialog', {name: '评测报告'});
    await sheet.locator('.bn-section').first().waitFor();
    const term = sheet.locator('.bn-term').first();
    await sheet.getByRole('button', {name: '关闭'}).click();
    await sheet.waitFor({state: 'hidden'});
    await earlier(page, 'sg', 'ipcheck');
    await page.locator('li[data-run="r1"] .bn-pick input').check();
    await page.locator('li[data-run="r2"] .bn-pick input').check();
    await page.locator('li[data-run="r3"] .bn-pick input').check();
    assert.match(await page.locator('.bn-pickbar').innerText(), /已选 2 条/, 'two at most on a phone');
    await page.getByTestId('bn-compare').click();
    const pair = page.getByRole('dialog', {name: '对比'});
    await pair.locator('.bn-compare-tabs').waitFor();
    assert.equal(await pair.locator('.bn-compare-tabs [role="tab"]').count(), 2);
    await pair.locator('.bn-compare-tabs [role="tab"]').nth(1).click();
    assert.equal(await pair.locator('.bn-compare-col:visible').count(), 1);
    await close();
  });
}

test('an OpenWrt router: only the tests it can run, with the reason', async t => {
  const wrt = {...linux('wrt', 'Home-Kwrt', 'CN', 'arm64'), node: {exec: true, online: true, info: {platform: 'linux', arch: 'arm64', openwrt: true, ipv4: '198.51.100.20'}}};
  const {page, calls, errors, close} = await mountPage(t, {
    load: n => import(`./BenchRunForm.vue?fresh=${n}`),
    root: (View, h) => ({render: () => h('div', {class: 'instance-ui servers-ui'}, [h(View, {catalog: CATALOG, servers: [wrt, SERVERS[0]], preset: {serverId: 'wrt'}})])}),
    handle: async () => [200, {ok: true, runs: []}]});
  await page.getByTestId('bn-openwrt').waitFor();
  assert.equal(await page.getByTestId('bn-openwrt').innerText(), 'OpenWrt 上只能运行 Service Unlock。');
  const box = name => page.getByRole('checkbox', {name});
  assert.equal(await box('综合评测').isChecked(), false, 'the suite is not picked for it');
  assert.equal(await box('综合评测').isDisabled(), true);
  for (const name of ['IP.Check.Place', 'Net.Check.Place 回程路由', 'Geekbench 5']) assert.equal(await box(name).isDisabled(), true, name);
  assert.equal(await box('Service Unlock').isDisabled(), false);
  await box('Service Unlock').check();
  await page.getByTestId('bn-start').click();
  await page.waitForTimeout(50);
  const posted = calls.find(c => c.method === 'POST' && c.path === '/api/servers/bench/runs');
  assert.deepEqual(posted.body, {serverId: 'wrt', test: 'unlock', options: {region: 'auto'}});
  assert.deepEqual(errors, []);
  await close();
});
