// The servers page without a browser (board, detail, terminal, files, node versions, mesh access): what the page asks the backend, what it shows and how it
// switches, checked in happy-dom with the browser tests' fake backend (testing/serversBackend.mjs).
// The layout of the same scenarios (sizes, overflow, scrolling, phone and dark) stays in
// servers.*.browser.test.mjs.
import test from 'node:test';
import assert from 'node:assert/strict';
import {setup, names, tab, expectFocused} from './testing/serversDom.mjs';
import {sleep} from '../testing/domPage.mjs';
import {server, GB, day, many, uninstalledLast, versions, RELEASE, MESH_ON, onMesh, card, meshOption, off, fact} from './testing/serversBackend.mjs';

test('desktop board: summary filters, update notice, sparklines, list view', async t => {
  const h = await setup(t);
  const stats = h.page.locator('.pb-stats');
  assert.match(await stats.locator('.pb-stat').first().innerText(), /4 \/ 5\s*1 离线/);
  // Bills in dollars and euros add up in CNY; the breakdown is in the title.
  const spend = stats.locator('.pb-stat').filter({hasText: '每月费用'});
  assert.match(await spend.locator('b').innerText(), /^¥\d{3}\.\d{2}$/);
  const breakdown = await spend.getAttribute('title');
  assert.match(breakdown, /^\$43\.44 ≈ ¥308\.\d\d（1 USD = 7\.1000 CNY）\n€9\.33 ≈ ¥7\d\.\d\d（1 EUR = 8\.2500 CNY）\n¥54\.29\n汇率日期 2026-10-25$/);
  const parts = breakdown.match(/¥(\d+\.\d\d)/g).slice(0, 3).map(v => Number(v.slice(1)));
  assert.ok(Math.abs(parts.reduce((a, b) => a + b, 0) - Number((await spend.locator('b').innerText()).slice(1))) < 0.03);
  assert.equal(await h.page.locator('.pb-card').count(), 6);
  await stats.getByRole('button', {name: /在线/}).click();
  assert.deepEqual(await names(h.page), ['GoldenCloud-1212 JP 测试 测试机 超长的名称用来检查卡片里的截断']);
  await stats.getByRole('button', {name: /在线/}).click();
  await stats.getByRole('button', {name: /30 天内到期/}).click();
  assert.deepEqual(await names(h.page), ['Hotel-HK', 'Papa-HK.T1']);
  await h.page.getByRole('button', {name: '显示全部'}).click();
  const papa = h.page.locator('.pb-card').filter({hasText: 'Papa-HK.T1'});
  assert.deepEqual(await papa.locator('.pb-tag').allInnerTexts(), ['¥37.79/月', '余 8 天', 'Tier 1', 'steal 23%', '可更新']);
  assert.equal(await papa.locator('.pb-tag').filter({hasText: 'steal'}).getAttribute('data-tone'), 'bad');
  await h.page.waitForFunction(() => document.querySelectorAll('.pb-spark path.is-rx').length >= 4, null, {timeout: 15000});
  await h.page.locator('.pb-notice').getByText('2 台服务器的节点可以更新到本地构建 0.13.1').waitFor();
  await h.page.locator('.pb-notice').getByRole('button', {name: '全部更新'}).click();
  await h.page.locator('.pb-notice').waitFor({state: 'detached'});
  assert.ok(h.calls.some(c => c.path === '/api/servers/node-update'));
  await h.page.getByRole('button', {name: '列表视图'}).click();
  assert.equal(await h.page.locator('.pl-row').count(), 6);
  const row = h.page.locator('.pl-row').filter({hasText: 'Papa-HK.T1'});
  assert.match(await row.innerText(), /64%[\s\S]*13%[\s\S]*39%[\s\S]*500 GB\s*100%[\s\S]*8 天[\s\S]*¥37\.79\/月/);
  assert.equal(await h.page.locator('.pl-row[data-state=offline] .pl-off').innerText(), '离线');
  assert.equal(await h.page.evaluate(() => localStorage.getItem('openbear.servers.view')), 'list');
  await row.click();
  assert.equal(await tab(h.page, 'Papa-HK.T1').getAttribute('aria-selected'), 'true');
  await h.close();
});

test('desktop detail: gauges, live charts, renew, logs and removing with uninstall', async t => {
  const h = await setup(t);
  await h.page.locator('.pb-card').filter({hasText: 'Edge-SG.Game'}).click();
  const detail = h.page.locator('.sv-detail:visible');
  assert.deepEqual(await detail.locator('.sv-gauge strong').allInnerTexts(), ['13%', '41%', '82%', '0%']);
  assert.equal(await detail.locator('.sv-gauge').nth(2).getAttribute('data-tone'), 'warn');
  assert.match(await detail.locator('.sv-tiles').innerText(), /写 1\.2 MB\/s[\s\S]*TCP 120 · UDP 8[\s\S]*steal 0\.4%/);
  await detail.locator('.sv-chart svg').first().waitFor();
  assert.equal(await detail.locator('.sv-ranges [aria-selected=true]').first().innerText(), '实时');
  assert.deepEqual(await detail.locator('.sv-chart figcaption > span:first-child').allInnerTexts(), ['CPU', '内存', '网络', '磁盘读写', '负载', '连接数']);
  assert.ok(h.calls.some(c => c.path.endsWith('/metrics') && c.query.range === 'live'));
  assert.match(await detail.locator('.sv-system').innerText(), /节点 [\d.]+ · 占用 11\.8 MB 内存[\s\S]*CPU\s*2 核[\s\S]*IPv4\s*198\.51\.100\.5/);
  // The cards in their order.
  const cards = await detail.locator('.sv-cards > section, section.net-probe').evaluateAll(list => list.map(el => el.querySelector('h3')?.textContent || el.getAttribute('aria-label')));
  assert.deepEqual(cards, ['系统', '账单', '网络地址', '本期流量', '网络测试', '浏览器', '命令记录']);
  assert.match(await detail.locator('.sv-price').innerText(), /\$375\.91 \/ 年\s*约 \$31\.33\/月\s*年付\s*手动续费/);
  assert.match(await detail.locator('.sv-traffic').innerText(), /2\.5 TB\s*\/ 10\.0 TB\s*取大（上下行较大的一方）[\s\S]*下行 2\.5 TB\s*上行 853 GB/);
  assert.match(await detail.locator('.sv-traffic').innerText(), /按目前速度，本期约/);
  const logs = detail.locator('.sv-logs');
  assert.match(await logs.innerText(), /AI\s+systemctl status nginx --no-pager\s+成功/);
  await logs.getByRole('tab', {name: '终端记录'}).click();
  await logs.getByText('192.0.2.1 · 会话已结束').waitFor();
  await detail.locator('.ix-card').filter({hasText: '账单'}).getByRole('button', {name: '续费'}).click();
  assert.ok(h.calls.some(c => c.path === '/api/servers/local/renew' && c.method === 'POST'));
  await detail.locator('.ix-more').click();
  await h.page.getByRole('menuitem', {name: '移除节点…'}).click();
  const dialog = h.page.getByRole('dialog', {name: '移除节点'});
  assert.equal(await dialog.getByRole('checkbox').isChecked(), true);
  await dialog.getByRole('button', {name: '移除'}).click();
  await dialog.waitFor({state: 'hidden'});
  assert.equal(h.calls.find(c => c.path === '/api/servers/local/node' && c.method === 'DELETE').query.uninstall, '1');
  await h.close();
});

test('background jobs: list, output panel with earlier output, stop; phone sheet and no overflow', async t => {
  const withJobs = items => { items.find(i => i.id === 'local').live.features = ['fs.edit', 'jobs', 'exec.shell']; };
  const h = await setup(t, {tweak: withJobs});
  await h.page.locator('.pb-card').filter({hasText: 'Edge-SG.Game'}).click();
  const detail = h.page.locator('.sv-detail:visible');
  const card = detail.locator('.sv-jobs');
  await card.locator('.sv-job-list > li').first().waitFor();
  assert.deepEqual(await card.locator('.sv-job-state').allInnerTexts(), ['运行中', '成功', '已中断']);
  assert.match(await card.locator('.sv-job-list > li').first().innerText(), /12 分 3\d 秒/);
  assert.equal(await card.locator('.sv-job-stop').count(), 1);
  await card.getByRole('button', {name: '查看任务 mh2k3j9a1b2c 的日志'}).click();
  const panel = h.page.locator('.sv-job-drawer');
  const output = panel.locator('.sv-job-log');
  await output.waitFor();
  assert.match(await output.innerText(), /\[2400\] building chunk 2400 of 2400/);
  assert.doesNotMatch(await output.innerText(), /\[0001\]/);
  assert.match(await panel.locator('.ix-facts').innerText(), /Shell\s+\/bin\/bash/);
  await panel.getByRole('button', {name: '加载更早的输出'}).click();
  await h.page.waitForFunction(() => document.querySelector('.sv-job-log')?.textContent.length > 66000);
  assert.ok(h.calls.some(c => /\/jobs\/mh2k3j9a1b2c$/.test(c.path) && c.query.offset === '0'));
  assert.match(await output.innerText(), /^\[0001\] building chunk 1 of 2400/);
  await panel.getByRole('button', {name: '停止任务'}).click();
  await h.page.getByRole('dialog', {name: '停止后台任务'}).getByRole('button', {name: '停止'}).click();
  await card.locator('.sv-job-state').first().filter({hasText: '已停止'}).waitFor();
  assert.ok(h.calls.some(c => c.path === '/api/servers/local/jobs/mh2k3j9a1b2c/stop' && c.method === 'POST'));
  await h.page.keyboard.press('Escape');
  await panel.waitFor({state: 'hidden'});
  // A node build without background jobs gets no such card.
  await tab(h.page, '探针').click();
  await h.page.locator('.pb-card').filter({hasText: 'Delta-JP'}).click();
  await h.page.locator('.sv-detail:visible .sv-gauge').first().waitFor();
  assert.equal(await h.page.locator('.sv-detail:visible .sv-jobs').count(), 0);
  await h.close();
});

test('detail: a local rebuild of the same version is offered as a new build', async t => {
  const h = await setup(t, {tweak: items => {
    Object.assign(items.find(i => i.id === 'srv-delta').node, {version: '0.13.1', latest: '0.13.1', updateAvailable: true, newBuild: true});
  }});
  await h.page.locator('.pb-card').filter({hasText: 'Delta-JP'}).click();
  const detail = h.page.locator('.sv-detail:visible');
  await detail.locator('.pb-notice').getByText('节点 0.13.1 可以更新到本地的新构建，更新时节点会重启，打开的终端会断开。').waitFor();
  await Promise.all([h.page.waitForResponse(r => r.url().endsWith('/api/servers/srv-delta/node/update')),
    detail.locator('.pb-notice').getByRole('button', {name: '更新'}).click()]);
  await h.close();
});

test('detail: a node past the target is not offered an update; a downgrade is asked for and named', async t => {
  for (const [width, touch] of [[1440, false], [390, true]]) {
    const h = await setup(t, {width, height: 844, touch, tweak: items => {
      const delta = items.find(i => i.id === 'srv-delta');
      Object.assign(delta.node, {version: '0.14.0-dev.3', updateAvailable: false,
        update: {state: 'ahead', updatable: false, downgrade: true, missing: [], protocolOk: true, withdrawn: false, target: {version: '0.13.1', source: 'github', build: 'a1b2c3d4e5f6'}}});
      delta.updateResult = {ok: true, changed: true, confirmed: true, state: 'updated', from: '0.14.0-dev.3', to: '0.13.1', build: 'a1b2c3d4e5f6', downgrade: true};
    }});
    await h.page.locator('.pb-card').filter({hasText: 'Delta-JP'}).click();
    const detail = h.page.locator('.sv-detail:visible');
    await detail.getByText('比目标 0.13.1 新，不会自动降级').waitFor();
    assert.equal(await detail.locator('.pb-notice[data-node-state]').count(), 0);
    await detail.locator('.ix-more').click();
    assert.equal(await h.page.getByRole('menuitem', {name: /^更新节点/}).count(), 0);
    await h.page.getByRole('menuitem', {name: '降级节点到 0.13.1…'}).click();
    const dialog = h.page.locator('.el-message-box');
    await dialog.getByText('这是降级：「Delta-JP」的节点会从 0.14.0-dev.3 换成更旧的 0.13.1').waitFor();
    await Promise.all([h.page.waitForResponse(r => r.url().endsWith('/api/servers/srv-delta/node/update')), dialog.getByRole('button', {name: '降级'}).click()]);
    assert.deepEqual(h.calls.find(c => c.path === '/api/servers/srv-delta/node/update').body, {downgrade: true});
    await h.page.getByText('已降级到 0.13.1，节点重新连上并报告运行新构建').waitFor();
    await h.close();
  }
});

test('update notice and error wording: nodes without command execution, backend codes, unknown codes', async t => {
  const h = await setup(t, {tweak: items => {
    const delta = items.find(i => i.id === 'srv-delta');
    delta.node.caps.exec = delta.live.caps.exec = false;
  }});
  await h.page.locator('.pb-notice').getByText('1 台服务器的节点可以更新到本地构建 0.13.1；另有 1 台关闭了命令执行，需要在服务器上重新运行安装命令').waitFor();
  const texts = await h.page.evaluate(async () => {
    const {safeError, errorText} = await import('./serverHelpers.js');
    const failed = (status, error) => safeError({response: {status, data: {ok: false, error}}});
    return [errorText('public_key_in_use'), errorText('io_error'), errorText('config_write_failed'), errorText('invalid_params'),
      failed(413, 'body_too_large'), failed(500, 'some_new_code'), failed(404, 'no_such_route_code'), failed(502, undefined)];
  });
  assert.deepEqual(texts, ['这个节点的密钥已被另一台服务器使用，不能重复注册。', '服务器读写文件时出错，请检查磁盘后重试。', '节点没能写入配置文件。',
    '节点不接受这个请求，可能需要先更新节点。', '内容太大：上传的文件最大 4 GB。', '操作没有完成（some_new_code）。', '资源不存在，请刷新。', '无法连接目标，请稍后重试。']);
  await h.close();
});

test('node browser: status card, turning it on with an existing address, restart, install option', async t => {
  const browserCard = detail => detail.locator('.ix-card').filter({has: detail.page().getByRole('heading', {name: '浏览器', exact: true})});
  let h = await setup(t, {tweak: items => {
    const delta = items.find(i => i.id === 'srv-delta');
    delta.node.caps.browser = delta.live.caps.browser = true;
    delta.browserState = {enabled: true, online: true, state: 'ready', source: 'download', product: 'Chrome/156.0.8078.47', headless: true, endpoint: '', epoch: 'e1'};
  }});
  await h.page.locator('.pb-card').filter({hasText: 'Delta-JP'}).click();
  let card = browserCard(h.page.locator('.sv-detail:visible'));
  await card.getByText('运行中').waitFor();
  assert.match(await card.innerText(), /Chrome\/156\.0\.8078\.47[\s\S]*自动下载的测试版 Chrome[\s\S]*无头模式/);
  await card.getByRole('button', {name: '重启'}).click();
  await Promise.all([h.page.waitForResponse(r => r.url().endsWith('/browser/restart')),
    h.page.locator('.el-message-box').getByRole('button', {name: '重启'}).click()]);
  await card.getByText('运行中').waitFor();
  assert.ok(h.calls.some(c => c.path === '/api/servers/srv-delta/browser/restart' && c.method === 'POST'));
  await h.close();

  for (const width of [1440, 390]) {
    h = await setup(t, {width, height: width < 500 ? 844 : 900});
    await h.page.locator('.pb-card').filter({hasText: 'Papa-HK.T1'}).click();
    const detail = h.page.locator('.sv-detail:visible');
    card = browserCard(detail);
    await card.getByText('未开启', {exact: false}).waitFor();
    await card.getByRole('button', {name: '开启'}).click();
    const dialog = h.page.getByRole('dialog', {name: '开启浏览器'});
    await dialog.getByText('优先用这台机器上的 Chrome', {exact: false}).waitFor();
    await dialog.getByRole('radio', {name: '连接已有的浏览器'}).check();
    await dialog.getByLabel('浏览器调试地址').fill('http://127.0.0.1:9222');
    await dialog.getByRole('button', {name: '开启', exact: true}).click();
    await dialog.waitFor({state: 'hidden'});
    assert.deepEqual(h.calls.find(c => c.path === '/api/servers/srv-papa/browser' && c.method === 'PUT').body, {enabled: true, endpoint: 'http://127.0.0.1:9222', download: false});
    await card.getByText('未启动，第一次使用时启动').waitFor();
    assert.match(await card.innerText(), /已有的浏览器（http:\/\/127\.0\.0\.1:9222）/);
    await h.close();
  }

  // An installed browser the node cannot run is explained; switching to Chrome for Testing is a choice.
  h = await setup(t, {tweak: items => {
    const delta = items.find(i => i.id === 'srv-delta');
    delta.node.caps.browser = delta.live.caps.browser = true;
    delta.browserState = {enabled: true, online: true, state: 'failed', source: '', product: '', headless: false, endpoint: '', epoch: '',
      error: 'browser_not_runnable: /opt/google/chrome/chrome'};
  }});
  await h.page.locator('.pb-card').filter({hasText: 'Delta-JP'}).click();
  card = browserCard(h.page.locator('.sv-detail:visible'));
  await card.getByText('启动失败').waitFor();
  assert.match(await card.innerText(), /节点没法运行这台机器上的 \/opt\/google\/chrome\/chrome[\s\S]*改用下载的测试版 Chrome/);
  await card.getByRole('button', {name: '设置'}).click();
  const settingsDialog = h.page.getByRole('dialog', {name: '浏览器设置'});
  await settingsDialog.getByRole('radio', {name: '使用下载的测试版 Chrome'}).check();
  await settingsDialog.getByText('适合已装的浏览器节点运行不了的情况', {exact: false}).waitFor();
  await settingsDialog.getByRole('button', {name: '保存'}).click();
  await settingsDialog.waitFor({state: 'hidden'});
  assert.deepEqual(h.calls.find(c => c.path === '/api/servers/srv-delta/browser' && c.method === 'PUT').body, {enabled: true, endpoint: '', download: true});
  await card.getByRole('button', {name: '设置'}).click();
  assert.ok(await settingsDialog.getByRole('radio', {name: '使用下载的测试版 Chrome'}).isChecked());
  await h.close();

  h = await setup(t, {tweak: items => {
    const delta = items.find(i => i.id === 'srv-delta');
    delta.node.caps.browser = delta.live.caps.browser = true;
    delta.browserState = {enabled: true, online: true, state: 'failed', error: 'browser_account_unavailable'};
  }});
  await h.page.locator('.pb-card').filter({hasText: 'Delta-JP'}).click();
  card = browserCard(h.page.locator('.sv-detail:visible'));
  await card.getByText('启动失败').waitFor();
  assert.match(await card.innerText(), /不会降级为 root 或关闭沙箱[\s\S]*连接已有的浏览器[\s\S]*改用下载的测试版不能解决账户问题/);
  await h.close();

  // The local server's Browser tool already uses the configured default browser.
  h = await setup(t);
  await h.page.locator('.pb-card').filter({hasText: 'Edge-SG.Game'}).click();
  card = browserCard(h.page.locator('.sv-detail:visible'));
  await card.getByText('本机不需要开启', {exact: false}).waitFor();
  assert.equal(await card.getByRole('button').count(), 0);
  assert.equal(await h.page.locator('.sv-detail:visible .sv-install').getByRole('checkbox', {name: /同时开启浏览器/}).count(), 0);
  await h.close();

  h = await setup(t);
  // A node that refuses commands is switched on where it runs.
  await h.page.locator('.pb-card').filter({hasText: 'Bravo-FRA'}).click();
  card = browserCard(h.page.locator('.sv-detail:visible'));
  await card.getByText('关闭了命令执行', {exact: false}).waitFor();
  assert.equal(await card.getByRole('button').count(), 0);
  await h.close();

  // Installing can turn the browser on at the same time.
  h = await setup(t);
  await h.page.locator('.pb-card').filter({hasText: 'Hotel-HK'}).click();
  const install = h.page.locator('.sv-detail:visible .sv-install');
  await install.getByRole('checkbox', {name: /同时开启浏览器/}).check();
  await Promise.all([h.page.waitForResponse(r => r.url().endsWith('/api/servers/srv-hotel/install')),
    install.getByRole('button', {name: '通过 SSH 安装'}).click()]);
  assert.deepEqual(h.calls.find(c => c.path === '/api/servers/srv-hotel/install' && c.method === 'POST').body, {browser: true});
  await h.close();
});

test('desktop terminal: search, font size, docked files with menu, editor and operations, batch exec', async t => {
  const h = await setup(t);
  await h.page.locator('.pb-card').filter({hasText: 'Edge-SG.Game'}).locator('.pb-icon').click();
  await h.page.locator('.tw-tab .tw-status[data-status=open]').waitFor();
  await h.page.locator('.tw-terminal').click();
  await h.page.keyboard.type('uptime');
  await h.page.keyboard.press('Enter');
  await h.page.locator('.xterm-rows').getByText('needle-line', {exact: false}).waitFor();
  await h.page.keyboard.press('Control+Shift+F');
  const find = h.page.getByRole('searchbox', {name: '搜索终端输出'});
  await find.waitFor();
  await find.fill('needle-line');
  assert.equal(await h.page.locator('.tw-find .instance-muted').count(), 0);
  await find.fill('absent-text-xyz');
  await h.page.locator('.tw-find').getByText('没有找到').waitFor();
  await find.press('Escape');
  await find.waitFor({state: 'hidden'});
  // The size the terminal is given (the rendered size is checked in the browser test).
  const size = () => h.page.evaluate(() => parseFloat(document.querySelector('.xterm-rows').style.fontSize));
  const before = await size();
  await h.page.getByRole('button', {name: '放大字号'}).click();
  await h.page.getByRole('button', {name: '放大字号'}).click();
  await h.page.waitForFunction(b => parseFloat(document.querySelector('.xterm-rows').style.fontSize) === b + 2, before);
  assert.equal(await h.page.evaluate(() => localStorage.getItem('openbear.servers.terminalFont')), String(before + 2));

  // Files open docked beside the terminal (where it docks depends on the width: browser test).
  await h.page.getByRole('button', {name: '文件', exact: true}).click();
  const pane = h.page.getByRole('region', {name: '文件'});
  await pane.getByText('backup.tar.gz').waitFor();
  assert.deepEqual(await pane.locator('.fb-name').allInnerTexts(), ['app', 'backup.tar.gz']);
  await pane.getByText('显示隐藏文件').click();
  assert.deepEqual(await pane.locator('.fb-name').allInnerTexts(), ['app', '.bashrc', 'backup.tar.gz']);
  assert.match(await pane.locator('.fb-row').filter({hasText: 'backup'}).innerText(), /50\.0 MB/);
  // Below the terminal on request: remembered. (The dock moving, and back, follow the measured width: browser test.)
  await pane.getByRole('button', {name: '放到下方'}).click();
  assert.equal(JSON.parse(await h.page.evaluate(() => localStorage.getItem('openbear.servers.filesDock'))).side, 'bottom');
  // The server list folds away for more room and comes back from the same button; remembered.
  const fold = h.page.getByRole('button', {name: '收起服务器列表'});
  await fold.click();
  await h.page.locator('.tw-side').waitFor({state: 'hidden'});
  const unfold = h.page.getByRole('button', {name: '展开服务器列表'});
  assert.equal(await h.page.evaluate(() => localStorage.getItem('openbear.servers.terminalSide')), 'folded');
  await unfold.click();
  await h.page.locator('.tw-side').waitFor();

  // The menu fits the entry: archives extract, every file downloads.
  await pane.locator('.fb-item').filter({hasText: 'backup.tar.gz'}).click({button: 'right'});
  const menu = h.page.getByRole('menu', {name: 'backup.tar.gz 的操作'});
  const menuItems = async () => { await h.page.getByRole('menuitem').first().waitFor(); return h.page.getByRole('menuitem').allInnerTexts(); };
  assert.deepEqual(await menuItems(), ['解压…', '下载', '重命名…', '修改权限…', '压缩…', '复制路径', '删除']);
  const download = h.page.waitForEvent('download');
  await menu.getByRole('menuitem', {name: '下载'}).click();
  assert.equal((await download).suggestedFilename(), 'backup.tar.gz');
  const op = () => h.calls.filter(c => c.path.endsWith('/files/op') && c.method === 'POST').at(-1).body;
  const prompt = name => h.page.getByRole('dialog', {name});

  await pane.locator('.fb-item').filter({hasText: 'backup.tar.gz'}).locator('.fb-more').click();
  await h.page.getByRole('menuitem', {name: '解压…'}).click();
  assert.equal(await prompt('解压').locator('input').inputValue(), '/root/backup');
  await prompt('解压').getByRole('button', {name: '解压'}).click();
  await h.page.waitForFunction(() => !document.querySelector('.fb-busy'));
  assert.deepEqual(op(), {op: 'extract', path: '/root/backup.tar.gz', dest: '/root/backup'});

  await pane.locator('.fb-item').filter({hasText: 'backup.tar.gz'}).click({button: 'right'});
  await h.page.getByRole('menuitem', {name: '重命名…'}).click();
  await prompt('重命名').locator('input').fill('old.tgz');
  await prompt('重命名').getByRole('button', {name: '确定'}).click();
  await h.page.waitForFunction(() => !document.querySelector('.fb-busy'));
  assert.deepEqual(op(), {op: 'rename', path: '/root/backup.tar.gz', name: 'old.tgz'});

  await pane.locator('.fb-item').filter({hasText: 'app'}).locator('.fb-more').click();
  await h.page.getByRole('menuitem', {name: '修改权限…'}).click();
  const perm = h.page.getByRole('dialog', {name: '修改权限'});
  assert.equal(await perm.getByLabel('权限数字').inputValue(), '755');
  await perm.getByLabel('用户组写').check();
  assert.equal(await perm.getByLabel('权限数字').inputValue(), '775');
  await perm.getByText('同时应用到文件夹里的所有内容').click();
  await perm.getByRole('button', {name: '修改'}).click();
  await h.page.waitForFunction(() => !document.querySelector('.fb-busy'));
  assert.deepEqual(op(), {op: 'chmod', paths: ['/root/app'], mode: '775', recursive: true});

  // Several picked: compress or delete them together.
  await pane.getByRole('checkbox', {name: '选择 app'}).check();
  await pane.getByRole('checkbox', {name: '选择 backup.tar.gz'}).check();
  const bar = pane.getByRole('toolbar', {name: '已选文件'});
  await bar.getByText('已选 2 项').waitFor();
  await bar.getByRole('button', {name: '压缩'}).click();
  const pack = h.page.getByRole('dialog', {name: '压缩'});
  assert.equal(await pack.getByLabel('压缩包名称').inputValue(), 'root');
  await pack.getByLabel('压缩格式').selectOption('zip');
  await pack.getByRole('button', {name: '压缩'}).click();
  await h.page.waitForFunction(() => !document.querySelector('.fb-busy'));
  assert.deepEqual(op(), {op: 'compress', paths: ['/root/app', '/root/backup.tar.gz'], name: 'root', format: 'zip'});
  await pane.getByRole('checkbox', {name: '选择 app'}).check();
  await pane.getByRole('checkbox', {name: '选择 .bashrc'}).check();
  await pane.locator('.fb-item').filter({hasText: '.bashrc'}).click({button: 'right'});
  assert.deepEqual(await menuItems(), ['压缩 2 项', '修改权限', '删除 2 项']);
  await h.page.getByRole('menuitem', {name: '删除 2 项'}).click();
  await prompt('删除').getByText('文件夹里的内容会一起删除', {exact: false}).waitFor();
  await prompt('删除').getByRole('button', {name: '删除'}).click();
  await h.page.waitForFunction(() => !document.querySelector('.fb-busy'));
  assert.deepEqual(op(), {op: 'delete', paths: ['/root/app', '/root/.bashrc']});

  // Empty space: create; a failure says why.
  await pane.locator('.fb-tip').click({button: 'right'});
  assert.deepEqual(await menuItems(), ['新建文件夹', '新建文件', '上传文件', '刷新']);
  await h.page.getByRole('menuitem', {name: '新建文件夹'}).click();
  await prompt('新建文件夹').locator('input').fill('exists');
  await prompt('新建文件夹').getByRole('button', {name: '确定'}).click();
  await h.page.getByText('同名文件已存在。').waitFor();
  assert.deepEqual(op(), {op: 'mkdir', path: '/root', name: 'exists'});

  // Into a folder. (Opening a text file in the Monaco editor and saving it is checked in the browser test.)
  await pane.locator('.fb-row').filter({hasText: 'app'}).click();
  await pane.getByText('config.yml').waitFor();
  assert.equal(await pane.getByRole('textbox', {name: '路径'}).inputValue(), '/root/app');
  await pane.locator('input[type=file]').setInputFiles({name: 'deploy.sh', mimeType: 'text/plain', buffer: Buffer.from('#!/bin/sh\necho hi\n')});
  await pane.getByText('已上传').waitFor();
  assert.deepEqual(h.uploads.at(-1), {path: '/root/app/deploy.sh', size: 18, overwrite: false});
  // Closing and reopening keeps the folder.
  await pane.getByRole('button', {name: '关闭文件面板'}).click();
  await pane.waitFor({state: 'hidden'});
  await h.page.getByRole('button', {name: '文件', exact: true}).click();
  assert.equal(await pane.getByRole('textbox', {name: '路径'}).inputValue(), '/root/app');
  await pane.getByRole('button', {name: '关闭文件面板'}).click();

  await h.page.locator('.tw-tabs').getByRole('button', {name: '批量执行'}).click();
  const batch = h.page.getByRole('dialog', {name: '批量执行'});
  await batch.waitFor();
  assert.equal(await batch.locator('.be-chip[aria-pressed=true]').count(), 1);
  assert.equal(await batch.locator('.be-chip').filter({hasText: 'Bravo-FRA'}).isDisabled(), true);
  await batch.getByRole('button', {name: '流媒体'}).click();
  await batch.getByRole('combobox', {name: '插入常用命令'}).selectOption('1');
  assert.equal(await batch.getByRole('textbox', {name: '要执行的命令'}).inputValue(), 'df -h /');
  await batch.getByRole('button', {name: '执行（2 台）'}).click();
  await batch.getByText('1 成功 · 1 失败').waitFor();
  const sent = h.calls.find(c => c.path === '/api/servers/exec' && c.method === 'POST').body;
  assert.deepEqual(sent, {serverIds: ['local', 'srv-delta'], command: 'df -h /', timeout: 120});
  await batch.locator('.be-row').filter({hasText: 'Delta-JP'}).click();
  await batch.getByText('df: /data: No such file').waitFor();
  assert.equal(await batch.locator('li[data-state=bad] .be-status').innerText(), '退出码 1');
  await batch.getByRole('button', {name: '重试失败的'}).click();
  assert.deepEqual(h.calls.filter(c => c.path === '/api/servers/exec' && c.method === 'POST').at(-1).body.serverIds, ['srv-delta']);
  await h.close();
});

test('windows and macOS nodes: metrics per platform, drives and backslash paths, install commands, mixed batch', async t => {
  const h = await setup(t, {tweak: items => {
    const w = items.find(i => i.id === 'srv-delta');
    Object.assign(w.node.info, {platform: 'windows', os: 'Windows 10 Enterprise LTSC 2019 (1809)', kernel: '10.0.17763.9376', cpu: 'Common KVM processor'});
    w.live.m.load = [0, 0, 0];
    Object.assign(items.find(i => i.id === 'srv-papa').node.info, {platform: 'darwin', os: 'macOS 26.2', kernel: '25.2.0', arch: 'arm64', cpu: 'Apple M4', virtualization: ''});
  }});
  // Windows has no load average: its card shows the process count.
  const win = h.page.locator('.pb-card').filter({hasText: 'Delta-JP'});
  assert.match(await win.locator('.pb-load').innerText(), /进程\s*310/);
  assert.match(await h.page.locator('.pb-card').filter({hasText: 'Papa-HK.T1'}).locator('.pb-load').innerText(), /负载\s*0\.5 \| 0\.4 \| 0\.3/);
  // Windows and macOS carry a mark right after the name; Linux, the usual case, has none.
  const mark = win.locator('.pb-head .sv-platform');
  assert.equal(await mark.getAttribute('data-platform'), 'windows');
  assert.equal(await mark.getAttribute('title'), 'Windows 10 Enterprise LTSC 2019 (1809)');
  assert.equal(await h.page.locator('.pb-card').filter({hasText: 'Papa-HK.T1'}).locator('.sv-platform').getAttribute('data-platform'), 'darwin');
  assert.equal(await h.page.locator('.pb-card').filter({hasText: 'Edge-SG.Game'}).locator('.sv-platform').count(), 0);
  await h.page.getByRole('button', {name: '列表视图'}).click();
  assert.equal(await h.page.locator('.pl-row').filter({hasText: 'Delta-JP'}).locator('.sv-platform').getAttribute('data-platform'), 'windows');
  assert.equal(await h.page.locator('.pl-row').filter({hasText: 'Papa-HK.T1'}).locator('.sv-platform').getAttribute('data-platform'), 'darwin');
  assert.equal(await h.page.locator('.pl-row .sv-platform').count(), 2);
  await h.page.getByRole('button', {name: '卡片视图'}).click();

  await win.click();
  let detail = h.page.locator('.sv-detail:visible');
  await detail.locator('.sv-chart svg').first().waitFor();
  assert.deepEqual(await detail.locator('.sv-tile > span').allInnerTexts(), ['网络', '磁盘读写', '连接']);
  assert.deepEqual(await detail.locator('.sv-chart figcaption > span:first-child').allInnerTexts(), ['CPU', '内存', '网络', '磁盘读写', '连接数']);
  assert.equal(await detail.locator('h2 .sv-platform').getAttribute('data-platform'), 'windows');
  assert.match(await detail.locator('.sv-system').innerText(), /Windows 10 Enterprise LTSC 2019 \(1809\)/);

  // Files on Windows: the drives, then backslash paths; no permissions.
  await detail.getByRole('button', {name: '文件', exact: true}).click();
  const sheet = h.page.locator('.sv-files-drawer');
  await sheet.getByText('E:', {exact: true}).waitFor();
  const box = sheet.getByRole('textbox', {name: '路径'});
  assert.equal(await box.inputValue(), '');
  assert.equal(await box.getAttribute('placeholder'), '此电脑');
  assert.deepEqual(await sheet.locator('.fb-crumbs > button').allInnerTexts(), ['此电脑']);
  assert.equal(await sheet.getByRole('button', {name: '新建文件夹'}).isDisabled(), true);
  await sheet.locator('.fb-row').filter({hasText: 'C:'}).click();
  await sheet.getByText('backup.zip').waitFor();
  assert.equal(await box.inputValue(), 'C:\\');
  assert.equal(h.calls.filter(c => c.path.endsWith('/files')).at(-1).query.path, 'C:/');
  await sheet.locator('.fb-row').filter({hasText: 'Users'}).click();
  await sheet.getByText('admin', {exact: true}).waitFor();
  assert.equal(await box.inputValue(), 'C:\\Users');
  assert.deepEqual(await sheet.locator('.fb-crumbs > button').allInnerTexts(), ['此电脑', 'C:', 'Users']);
  await sheet.getByRole('button', {name: '上一级'}).click();
  await sheet.getByText('backup.zip').waitFor();
  assert.equal(h.calls.filter(c => c.path.endsWith('/files')).at(-1).query.path, 'C:/');
  assert.equal(await sheet.locator('.fb-mode').first().innerText(), '');
  await sheet.locator('.fb-item').filter({hasText: 'backup.zip'}).click({button: 'right'});
  await h.page.getByRole('menuitem').first().waitFor();
  const items = await h.page.getByRole('menuitem').allInnerTexts();
  assert.ok(items.includes('解压…') && !items.some(i => i.startsWith('修改权限')), items.join());
  await h.page.getByRole('menuitem', {name: '复制路径'}).click();
  assert.equal(await h.page.evaluate(() => navigator.clipboard.readText()), 'C:\\backup.zip');
  await sheet.locator('.fb-item').filter({hasText: 'Users'}).click({button: 'right'});
  await h.page.getByRole('menuitem', {name: '压缩…'}).click();
  const pack = h.page.getByRole('dialog', {name: '压缩'});
  assert.deepEqual(await pack.getByLabel('压缩格式').locator('option').allInnerTexts(), ['zip', 'tar.gz']);
  assert.equal(await pack.getByLabel('压缩格式').inputValue(), 'zip');
  assert.match(await pack.innerText(), /Users → C:\\/);
  await pack.getByRole('button', {name: '压缩'}).click();
  await h.page.waitForFunction(() => !document.querySelector('.fb-busy'));
  assert.deepEqual(h.calls.filter(c => c.path.endsWith('/files/op')).at(-1).body, {op: 'compress', paths: ['C:/Users'], name: 'Users', format: 'zip'});
  await sheet.locator('.fb-item').filter({hasText: 'backup.zip'}).click({button: 'right'});
  await h.page.getByRole('menuitem', {name: '解压…'}).click();
  const unpack = h.page.getByRole('dialog', {name: '解压'});
  assert.equal(await unpack.locator('input').inputValue(), 'C:\\backup');
  await unpack.locator('input').fill('D:\\restore');
  await unpack.getByRole('button', {name: '解压'}).click();
  await h.page.waitForFunction(() => !document.querySelector('.fb-busy'));
  assert.deepEqual(h.calls.filter(c => c.path.endsWith('/files/op')).at(-1).body, {op: 'extract', path: 'C:/backup.zip', dest: 'D:/restore'});
  await h.page.keyboard.press('Escape');
  await sheet.waitFor({state: 'hidden'});

  // macOS: no disk I/O and no steal; a privacy block explains itself.
  await tab(h.page, '探针').click();
  await h.page.locator('.pb-card').filter({hasText: 'Papa-HK.T1'}).click();
  detail = h.page.locator('.sv-detail:visible');
  await detail.getByText('macOS 26.2').waitFor();
  assert.deepEqual(await detail.locator('.sv-tile > span').allInnerTexts(), ['网络', '负载', '连接']);
  await detail.locator('.sv-chart svg').first().waitFor();
  assert.deepEqual(await detail.locator('.sv-chart figcaption > span:first-child').allInnerTexts(), ['CPU', '内存', '网络', '负载', '连接数']);
  await detail.getByRole('button', {name: '文件', exact: true}).click();
  await sheet.getByText('完全磁盘访问权限', {exact: false}).waitFor();
  assert.equal(h.calls.filter(c => c.path.endsWith('/files')).at(-1).query.path, '/Users');
  await h.page.keyboard.press('Escape');
  await sheet.waitFor({state: 'hidden'});

  // Mixed batch: a warning, and Windows chips say PowerShell.
  await detail.getByRole('button', {name: '执行命令'}).click();
  const batch = h.page.getByRole('dialog', {name: '批量执行'});
  await batch.locator('.be-chip').filter({hasText: 'Delta-JP'}).click();
  assert.match(await batch.locator('.be-chip').filter({hasText: 'Delta-JP'}).innerText(), /PowerShell/);
  await batch.getByText('既有 Windows 也有 Linux / macOS', {exact: false}).waitFor();
  await batch.locator('.be-chip').filter({hasText: 'Papa-HK.T1'}).click();
  assert.equal(await batch.getByText('既有 Windows', {exact: false}).count(), 0);
  assert.equal(await batch.getByRole('textbox', {name: '要执行的命令'}).getAttribute('placeholder'), '例如：Get-PSDrive C; Get-Service sshd');
  await batch.getByRole('button', {name: '关闭'}).click();
  await batch.waitFor({state: 'hidden'});
  await tab(h.page, '终端').click();
  const sidebar = h.page.locator('.tw-server').filter({hasText: 'Delta-JP'}).first();
  assert.equal(await sidebar.locator('.sv-platform').getAttribute('data-platform'), 'windows');
  assert.equal(await h.page.locator('.tw-server').filter({hasText: 'Edge-SG.Game'}).first().locator('.sv-platform').count(), 0);
  await h.close();
});

test('summary without exchange rates keeps a total per currency', async t => {
  const h = await setup(t, {fx: null});
  const spend = h.page.locator('.pb-stats .pb-stat').filter({hasText: '每月费用'});
  assert.deepEqual(await spend.locator('em').allInnerTexts(), ['¥54.29', '$43.44', '€9.33']);
  assert.equal(await spend.getAttribute('title'), null);
  await h.close();
});

test('cards: spec icons, tags without billing, no empty expiry text, long-term plans, centred summary', async t => {
  const h = await setup(t, {tweak: items => {
    const bero = items.find(i => i.id === 'srv-bravo');
    bero.profile.group = '欧洲';
    bero.profile.billing = {price: 0, currency: '', cycle: 30, startedAt: '', expiresAt: '', autoRenew: false};
    items.find(i => i.id === 'srv-delta').profile.billing.expiresAt = day(73000);
    Object.assign(items.find(i => i.id === 'local').profile, {group: '', billing: {price: 0, currency: '', cycle: 30, startedAt: '', expiresAt: '', autoRenew: false}});
  }});
  const card = name => h.page.locator('.pb-card').filter({hasText: name});
  const bero = card('Bravo-FRA');
  assert.deepEqual(await bero.locator('.pb-tag').allInnerTexts(), ['欧洲']);
  assert.equal(await bero.locator('.pb-foot > span').first().innerText(), '');
  assert.deepEqual(await bero.locator('.pb-spec > span').allInnerTexts(), ['2 核', '2.0 GB', '20.0 GB']);
  assert.equal(await bero.locator('.pb-spec svg').count(), 3);
  const delta = card('Delta-JP');
  assert.ok((await delta.locator('.pb-tag').allInnerTexts()).includes('长期'));
  assert.equal(await delta.locator('.pb-foot > span').first().innerText(), '长期');
  // A card without tags.
  const row = ['Edge-SG.Game', 'Papa-HK.T1', 'GoldenCloud-1212'];
  assert.equal(await card(row[0]).locator('.pb-tag').count(), 0);
  await h.close();
});

test('profile: tags beside the group, the merchant\'s renewal page, a start date from the expiry', async t => {
  const h = await setup(t);
  await h.page.locator('.pb-card').filter({hasText: 'Papa-HK.T1'}).click();
  const detail = h.page.locator('.sv-detail:visible');
  await detail.getByRole('button', {name: '编辑'}).first().click();
  const editor = h.page.getByRole('dialog', {name: '编辑「Papa-HK.T1」'});
  // No start date: one cycle (a month) before the expiry, following the expiry until one is typed.
  const expires = await editor.getByLabel('到期日期').inputValue(), [y, m, d] = expires.split('-').map(Number);
  const back = new Date(Date.UTC(y, m - 2, 1)), last = new Date(Date.UTC(y, m - 1, 0)).getUTCDate();
  const month = `${back.getUTCFullYear()}-${String(back.getUTCMonth() + 1).padStart(2, '0')}-${String(Math.min(d, last)).padStart(2, '0')}`;
  assert.equal(await editor.getByLabel('开始日期').inputValue(), month);
  await editor.getByText('开始日期按到期日往前推一个周期自动填写').waitFor();
  await editor.getByLabel('到期日期').fill('2027-03-31');
  assert.equal(await editor.getByLabel('开始日期').inputValue(), '2027-02-28');
  await editor.getByLabel('开始日期').fill('2027-01-15');
  await editor.getByText('开始日期按到期日往前推一个周期自动填写').waitFor({state: 'hidden'});
  await editor.getByLabel('到期日期').fill('2027-04-30');
  assert.equal(await editor.getByLabel('开始日期').inputValue(), '2027-01-15', 'typed: kept');
  // Tags: Enter, a comma and leaving the field each add one; the same one twice stays once.
  const tags = editor.getByLabel('添加标签');
  await tags.fill('独服'); await tags.press('Enter');
  await tags.fill('建站专用'); await tags.press(',');
  await tags.fill('独服'); await tags.press('Enter');
  await tags.fill('备用'); await editor.getByLabel('商家', {exact: true}).click();
  assert.deepEqual(await editor.locator('.sv-tags-edit .sv-chip').allInnerTexts(), ['独服', '建站专用', '备用']);
  await editor.getByRole('button', {name: '删除标签 备用'}).click();
  await editor.getByLabel('商家', {exact: true}).fill('Papa Cloud');
  await editor.getByLabel('商家网址').fill('https://example.com/renew');
  await editor.getByRole('button', {name: '保存', exact: true}).click();
  await editor.waitFor({state: 'hidden'});
  const patch = h.calls.find(c => c.path === '/api/servers/srv-papa' && c.method === 'PATCH').body.profile;
  assert.deepEqual([patch.tags, patch.provider, patch.providerUrl, patch.billing.startedAt, patch.billing.expiresAt], [['独服', '建站专用'], 'Papa Cloud', 'https://example.com/renew', '2027-01-15', '2027-04-30']);
  // Shown under the name with the group and on the card; the link opens the merchant's page.
  assert.deepEqual(await detail.locator('.sv-hero-tags .pb-tag').allInnerTexts(), ['Tier 1', '独服', '建站专用']);
  const renew = detail.getByRole('link', {name: '去续费 ↗'});
  assert.deepEqual([await renew.getAttribute('href'), await renew.getAttribute('target'), await renew.getAttribute('rel')], ['https://example.com/renew', '_blank', 'noopener noreferrer']);
  assert.match(await detail.locator('.sv-billing').innerText(), /开始\s*2027-01-15[\s\S]*商家\s*Papa Cloud/);
  await tab(h.page, '探针').click();
  assert.match(await h.page.locator('.pb-card').filter({hasText: 'Papa-HK.T1'}).locator('.pb-tags').innerText(), /Tier 1\s*独服\s*建站专用/);
  await h.close();
});

test('traffic modes: editor fields per mode and how cards show them', async t => {
  const h = await setup(t, {tweak: items => {
    Object.assign(items.find(i => i.id === 'srv-delta').traffic, {mode: 'metered', limit: 0, price: 0.075, cost: 1.88});
    Object.assign(items.find(i => i.id === 'srv-bravo').traffic, {mode: 'unlimited', limit: 0});
  }});
  const card = name => h.page.locator('.pb-card').filter({hasText: name});
  assert.equal(await card('Delta-JP').locator('.pb-traffic small').innerText(), '本期约 ¥1.88 · ¥0.075/GB');
  assert.equal(await card('Bravo-FRA').locator('.pb-traffic small').innerText(), '不限流量');
  await card('Papa-HK.T1').click();
  const detail = h.page.locator('.sv-detail:visible');
  await detail.getByRole('button', {name: '编辑'}).first().click();
  const editor = h.page.getByRole('dialog', {name: '编辑「Papa-HK.T1」'});
  const modes = editor.getByRole('radiogroup', {name: '流量方式'}), mode = {selectOption: name => modes.getByRole('radio', {name: {fixed: '每月固定日期', metered: '按流量计费', unlimited: '不限流量'}[name]}).check()};
  assert.equal(await modes.getByRole('radio', {name: '按账单日重置'}).isChecked(), true);
  // How it is counted: one set of radios for every mode; a profile saved before keeps the larger direction.
  assert.deepEqual((await editor.getByRole('radiogroup', {name: '计算方式'}).innerText()).split(/\n+/).map(v => v.trim()).filter(Boolean),
    ['双向计费', '单向：上行', '单向：下行', '取大', '上下行较大的一方', '取小', '上下行较小的一方']);
  assert.equal(await editor.getByRole('radio', {name: /^取大/}).isChecked(), true);
  assert.match(await editor.getByText(/每月 \d+ 号重置，按到期日推算/).innerText(), /年付、季付等也是每月重置一次/);
  assert.equal(await editor.getByLabel('每月重置日').count(), 0);
  await mode.selectOption('fixed');
  await editor.getByLabel('每月重置日').fill('1');
  await mode.selectOption('metered');
  assert.equal(await editor.getByLabel('流量额度').count(), 0);
  await editor.getByLabel('每 GB 单价').fill('0.075');
  assert.ok(await editor.getByText('¥/GB').isVisible());
  await mode.selectOption('unlimited');
  assert.equal(await editor.getByLabel('每 GB 单价').count(), 0);
  assert.equal(await editor.getByRole('radiogroup', {name: '计算方式'}).count(), 0);
  assert.ok(await editor.getByText('不发流量提醒', {exact: false}).isVisible());
  await mode.selectOption('metered');
  await editor.getByRole('radio', {name: '双向计费'}).check();
  for (const input of await editor.locator('input:not([type=checkbox]):not([type=radio])').all()) assert.equal(await input.getAttribute('data-1p-ignore'), '', await input.getAttribute('aria-label'));
  await editor.getByRole('button', {name: '保存', exact: true}).click();
  await editor.waitFor({state: 'hidden'});
  const saved = h.calls.find(c => c.path === '/api/servers/srv-papa' && c.method === 'PATCH').body.profile.traffic;
  assert.deepEqual(saved, {mode: 'metered', type: 'sum', limit: 0, resetDay: 0, price: 0.075});
  await h.close();
});

test('many servers: search filters the board', async t => {
  const h = await setup(t, {height: 760, tweak: many});
  const search = h.page.getByRole('searchbox', {name: '搜索服务器'});
  await search.fill('Extra-1');
  assert.ok((await names(h.page)).every(n => n.startsWith('Extra-1')));
  await search.fill('');
  await h.close();
});

test('region picker: search by name or code, keyboard, back to auto detection', async t => {
  const h = await setup(t, {tweak: items => { const d = items.find(i => i.id === 'srv-delta'); d.profile.region = ''; d.region = 'JP'; d.node.geo = {country: 'JP'}; }});
  await h.page.locator('.pb-card').filter({hasText: 'Delta-JP'}).click();
  await h.page.locator('.sv-detail:visible').getByRole('button', {name: '编辑'}).first().click();
  const editor = h.page.getByRole('dialog', {name: '编辑「Delta-JP」'});
  const button = editor.getByRole('button', {name: /^地区：/});
  assert.equal(await button.getAttribute('aria-label'), '地区：日本（自动识别）');
  assert.equal(await button.getAttribute('data-auto'), 'true');
  await button.click();
  const search = h.page.getByRole('combobox', {name: '搜索地区'});
  await expectFocused(search);
  const options = h.page.getByRole('listbox', {name: '地区'}).getByRole('option');
  await options.first().waitFor();
  assert.match(await options.first().innerText(), /自动识别[\s\S]*日本/);
  assert.ok((await h.page.locator('.rp-caption').allInnerTexts()).includes('你的服务器'));
  await search.fill('de');
  assert.match(await options.first().innerText(), /德国[\s\S]*DE/);
  await search.press('Enter');
  assert.equal(await button.getAttribute('aria-label'), '地区：德国');
  await button.click();
  await search.fill('japan');
  assert.match(await options.first().innerText(), /日本[\s\S]*JP/);
  await search.press('Escape');
  assert.ok(await editor.isVisible(), 'Escape closes only the picker');
  assert.equal(await button.getAttribute('aria-label'), '地区：德国');
  await button.click();
  await options.filter({hasText: '自动识别'}).click();
  assert.equal(await button.getAttribute('aria-label'), '地区：日本（自动识别）');
  await editor.getByRole('button', {name: '保存', exact: true}).click();
  await editor.waitFor({state: 'hidden'});
  const patch = h.calls.find(c => c.path === '/api/servers/srv-delta' && c.method === 'PATCH').body.profile;
  assert.equal(patch.region, '');
  assert.equal('sort' in patch, false);
  await h.close();
});

test('leaving the page and coming back keeps open terminals connected', async t => {
  const h = await setup(t, {keepAlive: true});
  await tab(h.page, '终端').click();
  await h.page.locator('.tw-server').filter({hasText: 'Edge-SG.Game'}).click();
  await h.page.locator('.tw-status[data-status=open]').waitFor();
  assert.equal(h.terminal.sockets, 1);
  await h.page.evaluate(() => window.__showServers(false));
  await h.page.locator('#other-page').waitFor();
  const polls = () => h.calls.filter(c => c.path === '/api/servers/live').length;
  const before = polls();
  // Two poll intervals (3 s each, run 10 times faster here) with the page away.
  await sleep(600);
  assert.equal(polls(), before, 'no polling while another page is shown');
  await h.page.evaluate(() => window.__showServers(true));
  await h.page.locator('.tw-status[data-status=open]').waitFor();
  assert.equal(h.terminal.sockets, 1, 'the same connection, not a new one');
  await h.page.locator('.xterm-helper-textarea').focus();
  await h.page.keyboard.type('uptime\r');
  await h.page.waitForFunction(() => h.terminal.input.join('').includes('uptime'));
  await h.page.waitForFunction(() => polls() > before, null, {timeout: 3000}).catch(() => {});
  assert.ok(polls() > before, 'polling resumes');
  await h.close();
});

test('reorder: installed and uninstalled sections, arrows and save', async t => {
  const h = await setup(t);
  await h.page.getByRole('button', {name: '调整顺序'}).click();
  const dialog = h.page.getByRole('dialog', {name: '调整顺序'});
  const installed = dialog.getByRole('region', {name: '已安装节点'}), pending = dialog.getByRole('region', {name: '未安装节点'});
  assert.deepEqual(await pending.locator('.so-name').allInnerTexts(), ['Hotel-HK']);
  const order = () => installed.locator('.so-name').allInnerTexts();
  assert.equal((await order())[0], 'Edge-SG.Game');
  await installed.getByRole('button', {name: '下移 Edge-SG.Game'}).click();
  assert.deepEqual((await order()).slice(0, 2), ['Papa-HK.T1', 'Edge-SG.Game']);
  assert.ok(await installed.getByRole('button', {name: '上移 Papa-HK.T1'}).isDisabled());
  // (Dragging a row by its handle is checked in the browser test.)
  await dialog.getByRole('button', {name: '保存'}).click();
  await dialog.waitFor({state: 'hidden'});
  const saved = h.calls.find(c => c.path === '/api/servers/order').body.ids;
  assert.deepEqual(saved.slice(0, 2), ['srv-papa', 'local']);
  assert.equal(saved.at(-1), 'srv-hotel');
  assert.equal((await names(h.page))[0], 'Papa-HK.T1');
  assert.equal((await names(h.page)).at(-1), 'Hotel-HK');
  await h.close();
});

test('phone: reorder sheet from the header menu', async t => {
  const h = await setup(t, {width: 390, height: 844});
  await h.page.getByRole('button', {name: '服务器更多操作'}).click();
  await h.page.getByRole('button', {name: '调整顺序'}).click();
  const dialog = h.page.getByRole('dialog', {name: '调整顺序'});
  await dialog.locator('.so-item').first().waitFor();
  await dialog.getByRole('button', {name: '上移 Papa-HK.T1'}).click();
  await dialog.getByRole('button', {name: '保存'}).click();
  await dialog.waitFor({state: 'hidden'});
  assert.equal(h.calls.find(c => c.path === '/api/servers/order').body.ids[0], 'srv-papa');
  await h.close();
});

test('board: the flag is the drag handle only in the saved order', async t => {
  const h = await setup(t, {tweak: uninstalledLast});
  const {page} = h;
  assert.deepEqual(await names(page), ['Edge-SG.Game', 'Papa-HK.T1', 'GoldenCloud-1212 JP 测试 测试机 超长的名称用来检查卡片里的截断', 'Delta-JP', 'Bravo-FRA', 'Hotel-HK']);
  const flag = name => card(page, name).locator('.pb-flag');
  assert.equal(await flag('Bravo-FRA').getAttribute('title'), '德国 · 拖动调整顺序');
  // Another sort is not the saved order: nothing to drag by. (The dragging itself: browser test.)
  await page.getByRole('combobox', {name: '排序方式'}).selectOption('name');
  assert.equal(await page.locator('.pb-card .pb-grip').count(), 0);
  assert.equal(await flag('Bravo-FRA').getAttribute('title'), '德国');
  await page.getByRole('combobox', {name: '排序方式'}).selectOption('default');
  await page.getByRole('button', {name: '列表视图'}).click();
  assert.equal(await page.locator('.pl-head').count(), 1);
  assert.equal(await page.locator('.pl > .pl-head + .pl-row').count(), 1, 'the header stays first');
  await page.getByRole('button', {name: '调整顺序'}).click();
  await page.getByRole('dialog', {name: '调整顺序'}).getByRole('button', {name: '下移 Bravo-FRA'}).waitFor();
  await h.close();
});

test('node versions: state tags, summary with 全部更新, build and source in the detail, unconfirmed update', async t => {
  const h = await setup(t, {tweak: versions, release: RELEASE});
  const notice = h.page.locator('.pb-notice[data-node-update=summary]');
  await notice.getByText('2 台服务器的节点可以更新到 1.2.0，其中 1 台需要更新才能使用组网、网络测试').waitFor();
  const tagOf = name => h.page.locator('.pb-card').filter({hasText: name}).locator('.pb-tag[data-node-state]');
  assert.equal(await tagOf('Papa-HK.T1').innerText(), '需更新');
  assert.equal(await tagOf('Papa-HK.T1').getAttribute('data-tone'), 'warn');
  assert.equal(await tagOf('Papa-HK.T1').getAttribute('title'), '更新后才能使用组网、网络测试');
  assert.equal(await tagOf('Delta-JP').innerText(), '可更新');
  assert.equal(await tagOf('GoldenCloud').innerText(), '构建不一致');
  // Current and newer-than-target nodes carry no tag in the list.
  assert.equal(await tagOf('Edge-SG.Game').count(), 0);
  assert.equal(await tagOf('Bravo-FRA').count(), 0);
  // No build hashes on the board.
  assert.doesNotMatch(await h.page.locator('.pb').innerText(), /a1b2c3d4e5f6|9f8e7d6c5b4a/);
  await h.page.getByRole('button', {name: '列表视图'}).click();
  const row = h.page.locator('.pl-row').filter({hasText: 'Papa-HK.T1'});
  assert.equal(await row.locator('.pl-state').innerText(), '需更新');
  assert.equal(await h.page.locator('.pl-row').filter({hasText: 'Edge-SG.Game'}).locator('.pl-state').count(), 0);

  await h.page.locator('.pl-row').filter({hasText: 'Delta-JP'}).click();
  const detail = h.page.locator('.sv-detail:visible');
  await detail.locator('.pb-notice').getByText('节点 1.1.0 可以更新到 1.2.0（a1b2c3d4e5f6），更新时节点会重启，打开的终端会断开。').waitFor();
  const fact = label => detail.locator('.ix-facts > div').filter({has: h.page.locator('dt', {hasText: new RegExp(`^${label}$`)})}).locator('dd');
  assert.equal(await fact('构建').innerText(), '0123456789ab · 正式版');
  await detail.locator('.pb-notice').getByRole('button', {name: '更新'}).click();
  await h.page.getByText('文件已替换，未确认运行：节点没有在限定时间内用新程序重新连上').waitFor();
  await h.close();

  // A node that must update says why; one already on the target build says so in the detail only.
  const p = await setup(t, {tweak: versions, release: RELEASE});
  await p.page.locator('.pb-card').filter({hasText: 'Papa-HK.T1'}).click();
  let d = p.page.locator('.sv-detail:visible');
  await d.locator('.pb-notice[data-node-state=required]').getByText('节点 1.0.0 需要更新才能使用组网、网络测试，可以更新到 1.2.0（a1b2c3d4e5f6）', {exact: false}).waitFor();
  await tab(p.page, '探针').click();
  await p.page.locator('.pb-card').filter({hasText: 'Edge-SG.Game'}).click();
  d = p.page.locator('.sv-detail:visible');
  const fact2 = label => d.locator('.ix-facts > div').filter({has: p.page.locator('dt', {hasText: new RegExp(`^${label}$`)})}).locator('dd');
  assert.equal(await fact2('构建').innerText(), 'a1b2c3d4e5f6 · 正式版 · 已是目标构建');
  assert.equal(await d.locator('.pb-notice[data-node-state]').count(), 0);
  await tab(p.page, '探针').click();
  await p.page.locator('.pb-card').filter({hasText: 'Bravo-FRA'}).click();
  d = p.page.locator('.sv-detail:visible');
  assert.equal(await d.locator('.ix-facts > div').filter({has: p.page.locator('dt', {hasText: /^构建$/})}).locator('dd').innerText(), '5a5a5a5a5a5a · 测试版 · 比目标 1.2.0 新，不会自动降级');
  await tab(p.page, '探针').click();
  await p.page.locator('.pb-notice[data-node-update=summary]').getByRole('button', {name: '全部更新'}).click();
  await p.page.getByText('已更新 1 台，1 台文件已替换但未确认运行').waitFor();
  assert.ok(p.calls.some(c => c.path === '/api/servers/node-update'));
  await p.close();
});

test('node versions: no release available, retry, and the update switches', async t => {
  const none = {...RELEASE, available: false, version: '', source: '', latest: '', error: 'no_compatible_node_release'};
  const h = await setup(t, {tweak: versions, release: none, recheck: {...none, checkedAtMs: Date.now()}});
  const notice = h.page.locator('.pb-notice[data-node-release=none]');
  await notice.getByText('暂时没有可用的节点版本，稍后重试').waitFor();
  // No update summary while there is nothing to update to.
  assert.equal(await h.page.locator('.pb-notice[data-node-update]').count(), 0);
  await notice.getByRole('button', {name: '重试'}).click();
  await h.page.getByText('暂时还是没有可用的节点版本，稍后再试').waitFor();
  assert.equal(h.calls.filter(c => c.path === '/api/servers/node-release/check').length, 1);
  await h.page.locator('.pb-card').filter({hasText: 'Hotel-HK'}).click();
  const detail = h.page.locator('.sv-detail:visible');
  await detail.locator('[data-node-release=none]').getByText('暂时没有可用的节点版本，稍后重试。').waitFor();
  await detail.locator('[data-node-release=none]').getByRole('button', {name: '重试'}).click();
  await h.page.getByText('暂时还是没有可用的节点版本，稍后再试').first().waitFor();
  await tab(h.page, '探针').click();
  await h.page.getByRole('button', {name: '设置'}).click();
  const dialog = h.page.getByRole('dialog', {name: '服务器设置'});
  await dialog.getByText('暂时没有可用的节点版本', {exact: true}).waitFor();
  await dialog.getByText('GitHub 上暂时没有适合这个主控的节点版本，稍后重试', {exact: false}).waitFor();
  await dialog.locator('[data-node-switches]').getByText('只用正式版 · 自动更新节点已关闭，在「系统设置 → 版本更新」中修改').waitFor();
  await dialog.locator('.sv-release').getByRole('button', {name: '重试'}).waitFor();
  await h.close();
});

test('node versions on a phone, dark: tags, summary and detail stay compact', async t => {
  const h = await setup(t, {width: 390, height: 844, dark: true, tweak: versions, release: {...RELEASE, prerelease: true, autoUpdate: true}});
  const notice = h.page.locator('.pb-notice[data-node-update=summary]');
  await notice.getByRole('button', {name: '全部更新'}).waitFor();
  await h.page.getByRole('button', {name: '列表视图'}).click();
  await h.page.locator('.pl-row').filter({hasText: 'Papa-HK.T1'}).locator('.pl-state').waitFor();
  await h.page.locator('.pl-row').filter({hasText: 'Papa-HK.T1'}).click();
  const detail = h.page.locator('.sv-detail:visible');
  await detail.locator('.pb-notice[data-node-state=required]').waitFor();
  await detail.locator('.ix-facts dt', {hasText: /^构建$/}).scrollIntoViewIfNeeded();
  await h.close();
});

test('phone: Windows detail, drives and paths, install command switch, PowerShell tag', async t => {
  const h = await setup(t, {width: 390, height: 844, tweak: items => {
    const w = items.find(i => i.id === 'srv-delta');
    Object.assign(w.node.info, {platform: 'windows', os: 'Windows 10 Enterprise LTSC 2019 (1809)', kernel: '10.0.17763.9376'});
  }});
  assert.match(await h.page.locator('.pb-card').filter({hasText: 'Delta-JP'}).locator('.pb-load').innerText(), /进程/);
  await h.page.locator('.pb-card').filter({hasText: 'Delta-JP'}).click();
  let detail = h.page.locator('.sv-detail:visible');
  await detail.locator('.sv-gauge').first().waitFor();
  assert.deepEqual(await detail.locator('.sv-tile > span').allInnerTexts(), ['网络', '磁盘读写', '连接']);
  await detail.getByRole('button', {name: '文件'}).click();
  const sheet = h.page.locator('.sv-files-drawer');
  await sheet.getByText('E:', {exact: true}).waitFor();
  await sheet.locator('.fb-row').filter({hasText: 'C:'}).click();
  await sheet.getByText('backup.zip').waitFor();
  assert.equal(await sheet.getByRole('textbox', {name: '路径'}).inputValue(), 'C:\\');
  await sheet.getByRole('button', {name: 'backup.zip 的操作'}).click();
  await h.page.getByRole('menuitem').first().waitFor();
  assert.ok(!(await h.page.getByRole('menuitem').allInnerTexts()).some(i => i.startsWith('修改权限')));
  await h.page.keyboard.press('Escape');
  await h.page.getByRole('menuitem').first().waitFor({state: 'hidden'});
  await h.page.keyboard.press('Escape');
  await sheet.waitFor({state: 'hidden'});

  await detail.locator('.ix-more').click();
  await h.page.getByRole('menuitem', {name: '执行命令…'}).click();
  const batch = h.page.getByRole('dialog', {name: '批量执行'});
  await batch.locator('.be-chip').filter({hasText: 'Delta-JP'}).locator('.be-os').waitFor();
  await batch.locator('.be-chip').filter({hasText: 'Edge-SG'}).click();
  await batch.getByText('既有 Windows', {exact: false}).waitFor();
  await batch.getByRole('button', {name: '关闭'}).click();
  await batch.waitFor({state: 'hidden'});

  await tab(h.page, '探针').click();
  await h.page.locator('.pb-card').filter({hasText: 'Hotel-HK'}).click();
  detail = h.page.locator('.sv-detail:visible');
  await detail.getByRole('button', {name: '生成安装命令'}).click();
  const block = detail.locator('.sv-install-command');
  await block.getByRole('button', {name: 'Windows'}).click();
  await block.locator('.sv-command > code').filter({hasText: 'install.ps1'}).waitFor();
  await h.close();
});

test('phone: compact cards and list, detail actions, file sheet, terminal keys', async t => {
  const h = await setup(t, {width: 390, height: 844});
  await h.page.getByRole('button', {name: '列表视图'}).click();
  await h.page.locator('.pl-row').filter({hasText: 'Edge-SG.Game'}).click();
  const detail = h.page.locator('.sv-detail:visible');
  await detail.locator('.sv-gauge').first().waitFor();
  assert.equal(await detail.getByRole('button', {name: '执行命令'}).isVisible(), false);
  await detail.locator('.ix-more').click();
  await h.page.getByRole('menuitem', {name: '执行命令…'}).click();
  const batch = h.page.getByRole('dialog', {name: '批量执行'});
  await batch.waitFor();
  await batch.getByRole('button', {name: '关闭'}).click();
  await batch.waitFor({state: 'hidden'});
  await detail.getByRole('button', {name: '文件'}).click();
  const sheet = h.page.locator('.sv-files-drawer');
  await sheet.getByText('backup.tar.gz').waitFor();
  await sheet.getByText('点 ⋯ 查看更多操作', {exact: false}).waitFor();
  await sheet.getByRole('button', {name: 'backup.tar.gz 的操作'}).click();
  const items = h.page.getByRole('menuitem');
  await items.first().waitFor();
  await expectFocused(items.first());
  await h.page.keyboard.press('ArrowDown');
  await expectFocused(items.nth(1));
  await h.page.keyboard.press('Escape');
  await items.first().waitFor({state: 'hidden'});
  assert.ok(await sheet.isVisible(), 'Escape closes only the menu');
  await h.page.keyboard.press('Escape');
  await sheet.waitFor({state: 'hidden'});
  await tab(h.page, '终端').click();
  await h.page.locator('.tw-server').filter({hasText: 'Edge-SG.Game'}).click();
  await h.page.locator('.tw-status[data-status=open]').waitFor();
  assert.equal(await h.page.getByRole('button', {name: '放大字号'}).count(), 0);
  await h.page.getByRole('button', {name: '更多终端操作'}).click();
  await h.page.getByRole('menuitem', {name: '搜索输出'}).click();
  await h.page.getByRole('searchbox', {name: '搜索终端输出'}).waitFor();
  await h.page.locator('.tw-keys').getByRole('button', {name: 'Ctrl'}).click();
  await h.page.locator('.xterm-helper-textarea').focus();
  await h.page.keyboard.type('c');
  await h.page.waitForFunction(() => h.terminal.input.length > 0);
  assert.deepEqual(h.terminal.input, ['\x03']);
  await h.close();
});

test('dark phone: offline card, edit sheet with 16px inputs and sort order', async t => {
  const h = await setup(t, {width: 360, height: 780, dark: true});
  const offline = h.page.locator('.pb-card[data-state=offline]');
  await offline.getByText('2 小时前离线', {exact: true}).waitFor();
  await offline.click();
  await h.page.locator('.sv-detail:visible .ix-more').click();
  await h.page.getByRole('menuitem', {name: '编辑…'}).click();
  const sheet = h.page.getByRole('dialog', {name: /^编辑「GoldenCloud/});
  await sheet.waitFor();
  // Inputs with suggestions show the same chevron as selects.
  await sheet.getByRole('combobox', {name: '分组'}).click();
  const options = h.page.getByRole('listbox').getByRole('option');
  await options.first().waitFor();
  await options.filter({hasText: 'Tier 1'}).click();
  assert.equal(await sheet.getByRole('combobox', {name: '分组'}).inputValue(), 'Tier 1');
  // No sort field: order comes from "adjust order". Region is the flag beside the name.
  assert.equal(await sheet.getByLabel('排序').count(), 0);
  const regionButton = sheet.getByRole('button', {name: /^地区：/});
  assert.equal(await regionButton.getAttribute('aria-label'), '地区：日本');
  await regionButton.click();
  const regionSearch = h.page.getByRole('combobox', {name: '搜索地区'});
  await expectFocused(regionSearch);
  const regionOptions = h.page.getByRole('listbox', {name: '地区'}).getByRole('option');
  await regionOptions.first().waitFor();
  await regionSearch.fill('hk');
  await regionOptions.filter({hasText: 'HK'}).first().click();
  assert.match(await regionButton.getAttribute('aria-label'), /^地区：[^（]+$/);
  await sheet.getByLabel('价格').fill('99');
  await sheet.getByRole('button', {name: '保存', exact: true}).click();
  await sheet.waitFor({state: 'hidden'});
  const patch = h.calls.find(c => c.path === '/api/servers/srv-golf' && c.method === 'PATCH').body;
  assert.equal('sort' in patch.profile, false, 'saving keeps the order');
  assert.equal(patch.profile.region, 'HK');
  assert.equal(patch.profile.billing.price, 99);
  await h.close();
});

test('mesh access: settings row, picker per member, no-backup hint, refused choice, current connection', async t => {
  const h = await setup(t, {mesh: MESH_ON, tweak: onMesh('ap-mesh'), refuseMesh: ['srv-bravo']});
  // Settings: the built-in row shows its address and users; nothing to edit, remove or make default.
  await h.page.getByRole('button', {name: '设置'}).click();
  const dialog = h.page.getByRole('dialog', {name: '服务器设置'});
  const builtin = dialog.locator('.ap-builtin');
  await builtin.waitFor();
  assert.equal(await builtin.getByLabel('组网接入地址').innerText(), 'http://100.96.0.1:18961');
  await builtin.getByText('内置', {exact: true}).waitFor();
  await builtin.getByText('1 台在用').waitFor();
  assert.equal(await builtin.locator('input, button').count(), 0, 'read-only: no inputs, no default radio, no delete');
  assert.equal(await dialog.locator('.ap-row:not(.ap-builtin)').count(), 2);
  await dialog.getByRole('button', {name: '保存'}).click();
  await dialog.waitFor({state: 'hidden'});
  const put = h.calls.find(c => c.path === '/api/servers/settings' && c.method === 'PUT').body;
  assert.equal('meshAccess' in put, false, 'the built-in point is not sent back');
  assert.deepEqual(put.accessPoints.map(p => p.id), ['ap-global', 'ap-cn']);
  assert.equal(put.defaultAccess, 'ap-global');

  // The detail of a member reaching the controller over the mesh.
  await h.page.locator('.pb-card').filter({hasText: 'Delta-JP'}).click();
  const detail = h.page.locator('.sv-detail:visible');
  assert.equal(await fact(detail, '接入').innerText(), '组网（首选）');
  assert.equal(await fact(detail, '当前连接').innerText(), '组网 · http://100.96.0.1:18961');
  await fact(detail, '当前连接').scrollIntoViewIfNeeded();

  // Its editor: the mesh offered as primary (chosen) but not again as backup; the hint until a backup is set.
  await detail.getByRole('button', {name: '编辑'}).first().click();
  const editor = h.page.getByRole('dialog', {name: '编辑「Delta-JP」'});
  const primary = editor.getByLabel('首选接入点'), backup = editor.getByLabel('备用接入点');
  assert.equal(await primary.inputValue(), 'ap-mesh');
  assert.equal(await meshOption(primary).innerText(), '组网');
  assert.equal(await off(meshOption(primary)), false);
  assert.equal(await meshOption(backup).count(), 0, 'not the backup of itself');
  const hint = editor.locator('.ap-note');
  assert.equal(await hint.innerText(), '组网断开时这台会失联，建议设一个备用。');
  await editor.locator('.ap-pick').scrollIntoViewIfNeeded();
  await backup.selectOption('ap-global');
  assert.equal(await hint.count(), 0);
  await primary.selectOption('ap-cn');
  assert.deepEqual(await backup.locator('option').allInnerTexts(), ['不使用', '海外直连', '组网']);
  await backup.selectOption('ap-mesh');
  assert.equal(await hint.count(), 0, 'the mesh as backup needs no hint');
  await primary.selectOption('ap-mesh');
  assert.equal(await backup.inputValue(), '', 'the backup cleared when it became the primary');
  await hint.waitFor();
  // The hint does not stop saving.
  await editor.getByRole('button', {name: '保存', exact: true}).click();
  await editor.waitFor({state: 'hidden'});
  const patch = h.calls.filter(c => c.path === '/api/servers/srv-delta' && c.method === 'PATCH').at(-1).body;
  assert.deepEqual(patch.profile.access, {primary: 'ap-mesh', backup: ''});

  // A member whose node is too old for it: the mesh listed but not selectable, with the reason in the option itself.
  await tab(h.page, '探针').click();
  await h.page.locator('.pb-card').filter({hasText: 'Papa-HK.T1'}).click();
  await h.page.locator('.sv-detail:visible').getByRole('button', {name: '编辑'}).first().click();
  const papa = h.page.getByRole('dialog', {name: '编辑「Papa-HK.T1」'});
  for (const label of ['首选接入点', '备用接入点']) {
    const option = meshOption(papa.getByLabel(label));
    assert.equal(await option.innerText(), '组网（这台的节点版本太旧，先更新节点）');
    assert.equal(await off(option), true);
  }
  assert.equal(await papa.locator('.ap-note').count(), 0);
  await papa.getByRole('button', {name: '取消'}).click();
  await papa.waitFor({state: 'hidden'});
  await tab(h.page, '探针').click();

  // Picked for a member the controller then refuses: the error in words.
  await h.page.locator('.pb-card').filter({hasText: 'Bravo-FRA'}).click();
  await h.page.locator('.sv-detail:visible').getByRole('button', {name: '编辑'}).first().click();
  const bravo = h.page.getByRole('dialog', {name: '编辑「Bravo-FRA」'});
  await bravo.getByLabel('备用接入点').selectOption('ap-mesh');
  await bravo.getByRole('button', {name: '保存', exact: true}).click();
  await bravo.getByRole('alert').getByText('这台服务器不在组网里，不能选组网接入。', {exact: false}).waitFor();
  await bravo.getByRole('button', {name: '取消'}).click();
  await bravo.waitFor({state: 'hidden'});
  await tab(h.page, '探针').click();

  // Adding a server: no member yet, so no mesh.
  await h.page.getByRole('button', {name: '添加服务器'}).click();
  const add = h.page.getByRole('dialog', {name: '添加服务器'});
  await add.getByLabel('首选接入点').waitFor();
  for (const select of await add.locator('.ap-pick select').all()) assert.equal(await meshOption(select).count(), 0);
  await h.close();
});

test('mesh access unavailable: the reason in settings and in the picker, a chosen mesh flagged', async t => {
  for (const [reason, text] of [['controller_not_member', '主控本机还没加入组网'], ['controller_no_address', '主控本机还没分到组网地址']]) {
    const h = await setup(t, {mesh: {...MESH_ON, url: '', available: false, reason}, tweak: onMesh('ap-cn', 'ap-mesh')});
    await h.page.getByRole('button', {name: '设置'}).click();
    const dialog = h.page.getByRole('dialog', {name: '服务器设置'});
    const builtin = dialog.locator('.ap-builtin');
    await builtin.getByText('暂不可用').waitFor();
    assert.equal(await builtin.getByLabel('组网接入地址').count(), 0);
    assert.equal(await builtin.locator('small').innerText(), `${text}，组网成员暂时不能选它。`);
    await dialog.getByRole('button', {name: '取消'}).click();
    await h.page.locator('.pb-card').filter({hasText: 'Delta-JP'}).click();
    const detail = h.page.locator('.sv-detail:visible');
    assert.equal(await fact(detail, '当前连接').innerText(), '国内中转 · https://node-cn.example.com:20443');
    await detail.getByRole('button', {name: '编辑'}).first().click();
    const editor = h.page.getByRole('dialog', {name: '编辑「Delta-JP」'});
    const primary = editor.getByLabel('首选接入点'), backup = editor.getByLabel('备用接入点');
    assert.equal(await meshOption(primary).innerText(), `组网（${text}）`);
    assert.equal(await off(meshOption(primary)), true);
    // Kept as chosen (not silently dropped), flagged under the choice.
    assert.equal(await backup.inputValue(), 'ap-mesh');
    assert.equal(await meshOption(backup).innerText(), '组网', 'chosen: the name only, the note says why');
    assert.equal(await editor.locator('.ap-note').innerText(), `已选的组网现在用不了：${text}。`);
    await backup.selectOption('');
    assert.equal(await off(meshOption(backup)), true, 'once moved away it cannot be picked again');
    assert.equal(await meshOption(backup).innerText(), `组网（${text}）`);
    assert.equal(await editor.locator('.ap-note').count(), 0);
    await h.close();
  }
});

for (const [dark, theme] of [[false, 'light'], [true, 'dark']]) test(`mesh access on a phone, ${theme}: settings row, editor sheet with the hint, detail`, async t => {
  const h = await setup(t, {width: 390, height: 844, dark, mesh: MESH_ON, tweak: onMesh('ap-mesh')});
  await h.page.getByRole('button', {name: '服务器更多操作'}).click();
  await h.page.getByRole('button', {name: '设置', exact: true}).click();
  const dialog = h.page.getByRole('dialog', {name: '服务器设置'});
  const builtin = dialog.locator('.ap-builtin');
  await builtin.waitFor();
  await builtin.scrollIntoViewIfNeeded();
  await dialog.getByRole('button', {name: '取消'}).click();
  await dialog.waitFor({state: 'hidden'});
  await h.page.locator('.pb-card').filter({hasText: 'Delta-JP'}).click();
  const detail = h.page.locator('.sv-detail:visible');
  const current = fact(detail, '当前连接');
  assert.equal(await current.innerText(), '组网 · http://100.96.0.1:18961');
  await current.scrollIntoViewIfNeeded();
  await detail.locator('.ix-more').click();
  await h.page.getByRole('menuitem', {name: '编辑…'}).click();
  const sheet = h.page.getByRole('dialog', {name: '编辑「Delta-JP」'});
  const pick = sheet.locator('.ap-pick');
  await pick.scrollIntoViewIfNeeded();
  for (const select of await pick.locator('select').all()) {
  }
  assert.equal(await pick.locator('.ap-note').innerText(), '组网断开时这台会失联，建议设一个备用。');
  await h.close();
});

test('desktop list view loads as remembered', async t => {
  const h = await setup(t, {view: 'list'});
  assert.equal(await h.page.locator('.pb-card').count(), 0);
  assert.equal(await h.page.getByRole('button', {name: '列表视图'}).getAttribute('aria-pressed'), 'true');
  await h.close();
});
