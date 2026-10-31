// The mesh board (servers page, 组网 tab) without a browser: what it asks the controller, what it
// shows and how it switches, in happy-dom with the browser tests' fake controller
// (testing/meshBackend.mjs, mounted by testing/meshDom.mjs). Long timers run 10 times faster
// (scaleTimers), so "no request within 6 s" waits 0.6 s. The layout of the same scenarios stays in
// mesh.*.browser.test.mjs.
import test from 'node:test';
import assert from 'node:assert/strict';
import {setup, node, label, inspector, title, memberOf, dimmed, texts, until} from './testing/meshDom.mjs';
import {GB, server, member} from './testing/meshBackend.mjs';

test('canvas: the panel folds away and is remembered; a selection then shows a small card with the way back', async t => {
  const h = await setup(t, {lines: true});
  const {page} = h;
  const toolbar = page.locator('.mesh-toolbar');
  // The fold button sits right of "访问规则".
  const names = await toolbar.getByRole('button').evaluateAll(list => list.map(b => b.getAttribute('aria-label') || b.textContent.trim()));
  assert.deepEqual(names.slice(-2), ['访问规则', '收起属性面板']);
  await toolbar.getByRole('button', {name: '收起属性面板'}).click();
  assert.equal(await page.locator('.mesh-inspector-col').count(), 0);
  // A node: name, state, role and address; alerts if any; the way to the panel.
  await node(page, 'home').click();
  const peek = page.locator('.mesh-peek');
  await peek.waitFor();
  assert.equal(await peek.locator('h3').innerText(), 'Home');
  assert.match(await peek.innerText(), /成员 · 100\.96\.0\.3 · 延迟 152 ms[\s\S]*展开面板查看详情/);
  // A link: what it is and how the line is doing.
  await label(page, '2 · TCP').click();
  assert.match(await peek.innerText(), /连通 · 152 ms · 丢包 0\.4%/);
  await peek.getByRole('button', {name: '取消选择'}).click();
  assert.equal(await peek.count(), 0);
  // Remembered across a reload.
  await page.reload();
  await node(page, 'home').waitFor();
  assert.equal(await page.locator('.mesh-inspector-col').count(), 0);
  await label(page, '2 · TCP').click();
  await peek.getByRole('button', {name: '展开面板查看详情'}).click();
  assert.equal(await peek.count(), 0);
  await inspector(page).locator('.mesh-line-ends').waitFor();
  assert.equal(await toolbar.getByRole('button', {name: '收起属性面板'}).getAttribute('aria-expanded'), 'true');
  assert.equal(await page.evaluate(() => JSON.parse(localStorage.getItem('openbear.mesh.folded')).panel), false);
  await h.close();
});

test('canvas: the server list folds to a rail with the tools; its search stays on top while the list scrolls', async t => {
  const h = await setup(t, {height: 640});
  const {page} = h;
  // Many servers to scroll through.
  const palette = page.locator('.mesh-palette');
  const search = palette.getByRole('searchbox', {name: '搜索服务器'});
  const list = palette.locator('.mesh-servers-scroll');
  await page.evaluate(() => { const ul = document.querySelector('.mesh-servers-scroll ul'); for (let i = 0; i < 30; i++) ul.appendChild(ul.firstElementChild.cloneNode(true)); });
  // Fold: a rail with the way back and the tools; connecting still works from it.
  await palette.getByRole('button', {name: '收起服务器列表'}).click();
  const rail = page.locator('.mesh-rail');
  assert.equal(await rail.locator('.mesh-servers').count(), 0);
  await rail.getByRole('radio', {name: '访问规则'}).click();
  assert.equal(await rail.getByRole('radio', {name: '访问规则'}).getAttribute('aria-checked'), 'true');
  await rail.getByRole('radio', {name: '选择'}).click();
  // Both folded: the board has nearly the whole width.
  await page.locator('.mesh-toolbar').getByRole('button', {name: '收起属性面板'}).click();
  await page.reload();
  await node(page, 'home').waitFor();
  assert.equal(await page.locator('.mesh-rail').count(), 1, 'remembered');
  await page.locator('.mesh-rail').getByRole('button', {name: '展开服务器列表'}).click();
  await page.getByRole('searchbox', {name: '搜索服务器'}).waitFor();
  await h.close();
});

test('canvas: a link shows both ends and the line\'s long-term stability; entries open their line; help in plain words', async t => {
  const h = await setup(t, {lines: true});
  const {page} = h;
  const panel = inspector(page);
  // The entry in use: each end from where, over what, to where, and who dials.
  await label(page, '2 · TCP').click();
  const ends = panel.locator('.mesh-line-ends');
  await ends.waitFor();
  assert.match(await ends.locator('.mesh-group-title').innerText(), /两端\s*连通/);
  const rows = await ends.locator('li').allInnerTexts();
  assert.match(rows[0], /Home\s*主动拨号\s*从 10\.0\.0\.5:40112 经 TCP 连到 198\.51\.100\.3:52000/);
  assert.match(rows[1], /LAX\s*等待对方连接\s*从 198\.51\.100\.3:52000 经 TCP 连到 203\.0\.113\.7:40112/);
  // Stability over 24 hours by default, six ranges; summary and the chart.
  const trend = panel.locator('.mesh-line-trend');
  await trend.locator('.mesh-sparks').waitFor();
  assert.deepEqual(await trend.getByRole('tab').allInnerTexts(), ['1 小时', '24 小时', '3 天', '7 天', '15 天', '30 天']);
  assert.ok(h.calls.some(c => c.path === '/api/servers/mesh/lines/entry%3Ahome%7Clax%7Ctcp/trend' && c.query === '?range=24h'));
  assert.match(await trend.locator('.mesh-sparks').innerText(), /可用\s*95\.4%\s*平均延迟\s*152 ms\s*丢包\s*0\.6%/);
  const facts = await trend.locator('.mesh-line-facts').innerText();
  for (const want of [/P95 延迟\s*171 ms/, /最小 \/ 最大\s*138 ms \/ 213 ms/, /断开\s*2 次，共 7 分钟/]) assert.match(facts, want);
  assert.equal(await trend.locator('rect[data-tone=bad]').count(), 1);
  assert.equal(await trend.locator('rect[data-tone=warn]').count(), 1);
  await trend.getByRole('tab', {name: '30 天'}).click();
  assert.ok(h.calls.some(c => c.path.endsWith('/trend') && c.query === '?range=30d'));
  // A cold standby: one end has not reported; its public mapping; tried now and then, gaps are not downtime.
  // Quiet without its label: Home selected brings it up.
  await node(page, 'home').click();
  await label(page, '1 · UDP').click();
  await panel.locator('.mesh-line-end-list').waitFor();
  const cold = await panel.locator('.mesh-line-ends').innerText();
  assert.match(cold, /两端\s*冷备/);
  assert.match(cold, /Home\s*主动拨号\s*经 UDP 连到 198\.51\.100\.6:51999\s*公网映射 203\.0\.113\.7:61022/);
  assert.doesNotMatch(cold, /未知地址/);
  assert.match(cold, /Berlin\s*这端没有上报/);
  await trend.locator('.mesh-sparks').waitFor();
  assert.match(await trend.locator('.mesh-trend-head').innerText(), /冷备，抽测/);
  assert.equal(await trend.locator('rect[data-tone=none]').count(), 24, 'no sample: a gap');
  assert.equal(await trend.locator('rect[data-tone=bad]').count(), 1, 'one failed try');
  assert.match(await trend.innerText(), /试连\s*83\.3%[\s\S]*试连失败\s*1 次[\s\S]*有抽测的时间\s*6 分钟[\s\S]*图上的空白是那段时间没有抽测，不是断开/);
  // Lone tries are dots, not a line drawn through the gaps.
  assert.equal(await trend.locator('.mesh-sparks > li').nth(1).locator('path.mesh-spark-dots').evaluate(p => (p.getAttribute('d').match(/M/g) || []).length), 6);
  assert.equal(await trend.locator('.mesh-sparks > li').nth(1).locator('path:not(.mesh-spark-dots)').count(), 0);
  // A punched relay link: both public mappings; the help in plain words with an example.
  // The relay link has no tag in full mesh: click the line between the two relays.
  await page.locator('.mesh-edge[data-kind=relay]').first().click();
  await panel.locator('.mesh-line-end-list').waitFor();
  assert.match(await panel.locator('.mesh-line-ends .mesh-group-title').innerText(), /两端\s*连通 · 142 ms · 打洞/);
  // Recorded for only part of the day: availability over that part, and said so.
  await trend.locator('.mesh-sparks').waitFor();
  assert.match(await trend.locator('.mesh-sparks').innerText(), /可用\s*98\.7%\s*平均延迟\s*142 ms/);
  assert.match(await trend.locator('.mesh-line-facts').innerText(), /断开\s*1 次，共 4 分钟[\s\S]*有记录的时间\s*6 小时 \/ 1 天/);
  assert.equal(await trend.locator('rect[data-tone=none]').count(), 22);
  // Nothing recorded over the hour (knownSeconds 0): no data, not "never down".
  await trend.getByRole('tab', {name: '1 小时'}).click();
  await trend.getByText('这段时间还没有数据。').waitFor();
  assert.equal(await trend.locator('.mesh-line-facts').count(), 0);
  await trend.getByRole('tab', {name: '24 小时'}).click();
  await trend.locator('.mesh-sparks').waitFor();
  assert.match(await panel.locator('.mesh-line-ends').innerText(), /Berlin\s*主动拨号\s*从 192\.168\.10\.2:51999 经 UDP 连到 203\.0\.113\.30:40002\s*公网映射 203\.0\.113\.20:40001[\s\S]*LAX\s*等待对方连接[\s\S]*公网映射 203\.0\.113\.30:40002/);
  await panel.getByRole('button', {name: '偏好：说明'}).hover();
  const preferHelp = page.getByRole('tooltip').filter({hasText: '偏好'});
  await preferHelp.waitFor();
  assert.match(await preferHelp.innerText(), /偏好 -20：把这条线当成比实测快 20 ms，更容易走它/);
  assert.match(await panel.locator('label.ob-check', {hasText: '所有中转两两相连'}).innerText(), /例如 3 台中转就是 3 条线/);
  // A member's entries open their line; its trend says whom the latency is to.
  await node(page, 'home').click();
  await panel.locator('.mesh-cand-list li').first().waitFor();
  await panel.getByRole('button', {name: /第 2 个入口（LAX · .*）：查看这条线路/}).click();
  await panel.locator('.mesh-line-ends').waitFor();
  assert.match(await panel.locator('.mesh-line-end-list').innerText(), /从 10\.0\.0\.5:40112 经 TCP/);
  await node(page, 'home').click();
  const note = panel.locator('.mesh-trend-note');
  await note.waitFor();
  assert.equal(await note.innerText(), '上面是到 LAX（入口）的延迟；单条线路请点连线查看稳定性。');
  // A relay's latency is to the other relays only.
  await node(page, 'lax').click();
  await panel.getByText('上面是到 Berlin 的中转链路平均延迟；单条线路请点连线查看稳定性。').waitFor();
  await h.close();
});

test('older controller: no layout, no installs list, layout and trend 404, and no mesh at all', async t => {
  const h = await setup(t, {old: true});
  const {page} = h;
  // Laid out automatically: relays in a row on top.
  await node(page, 'home').click();
  await inspector(page).getByText('主控还不提供趋势数据。').waitFor();
  assert.doesNotMatch(await inspector(page).innerText(), /内存|CPU/);
  await h.close();
  const none = await setup(t, {missing: true});
  await none.page.getByText('主控还不支持组网，更新 OpenBear 后再试。').waitFor();
  await none.close();
});

test('desktop widths and themes: three columns at 1440 and 1024, light and dark', async t => {
  for (const [width, dark] of [[1440, false], [1440, true], [1024, false], [1024, true]]) {
    const h = await setup(t, {width, height: width > 1200 ? 900 : 768, dark});
    const {page} = h;
    await node(page, 'home').click();
    await inspector(page).locator('.mesh-sparks > li').first().waitFor();
    // Home runs Linux: no macOS firewall hint even though the status mentions one.
    assert.deepEqual(await inspector(page).locator('.mesh-hints summary').allInnerTexts(), ['内存接近上限（230 MB / 256 MB）', '1 条网格路由没有加上']);
    await h.close();
  }
});

test('canvas: without lines from the controller the link says so; rules for every gateway network go to the legend', async t => {
  const h = await setup(t, {allSubnets: true});
  const {page} = h;
  await label(page, '2 · TCP').click();
  await inspector(page).getByText('主控还不提供线路稳定性数据，更新主控后可用。').waitFor();
  assert.equal(await inspector(page).locator('.mesh-line-ends').count(), 0);
  await page.keyboard.press('Escape');
  const layer = page.locator('.mesh-toolbar').getByRole('button', {name: '访问规则'});
  if (await layer.getAttribute('aria-pressed') === 'false') await layer.click();
  assert.deepEqual(await page.locator('.mesh-legend').getByRole('button').evaluateAll(list => list.map(b => b.getAttribute('aria-label'))), ['访问规则 1：所有成员、所有网关网段 → 所有成员 · ping']);
  assert.equal(await page.locator('.mesh-edge[data-kind=rule]').count(), 0, 'not an arrow');
  await h.close();
});

test('keyboard and the server card: Delete on a node asks first, Esc clears, the card opens the node on the board', async t => {
  const h = await setup(t, {tabs: ['home'], active: 'home'});
  const {page} = h;
  const card = page.locator('.sv-detail:visible .mesh-card');
  await card.getByText('100.96.0.3').waitFor();
  await card.getByRole('button', {name: '在组网中查看'}).click();
  await node(page, 'home').locator('.mesh-node[data-selected]').waitFor();
  assert.equal(await title(page).innerText(), 'Home');
  assert.equal(await page.locator('.sv-tabs [role=tab][aria-selected=true]').innerText(), '组网');
  await page.keyboard.press('Delete');
  const box = page.locator('.el-message-box');
  await box.getByText('应用后「Home」将无法访问网格').waitFor();
  await box.getByRole('button', {name: '移除'}).click();
  await node(page, 'home').getByText('待移除').waitFor();
  assert.ok(h.calls.some(c => c.method === 'DELETE' && c.path === '/api/servers/mesh/members/home'));
  await page.keyboard.press('Escape');
  assert.equal(await title(page).innerText(), '网格');
  await h.close();
});

test('the server card off the mesh: why it cannot join under the title, the join at the head\'s right like every card\'s actions', async t => {
  const h = await setup(t, {tabs: ['old', 'mac'], active: 'old'});
  const {page} = h;
  const old = page.locator('.sv-detail:visible .mesh-card');
  await old.getByText('未加入组网：节点版本过旧，需要先更新').waitFor();
  assert.equal(await old.locator('.ix-card-head').getByRole('button', {name: '加入组网'}).isDisabled(), true);
  await page.locator('.sv-tab', {hasText: '公司-Mac'}).click();
  const card = page.locator('.sv-detail:visible .mesh-card');
  const join = card.locator('.ix-card-head').getByRole('button', {name: '加入组网'});
  await join.click();
  await card.locator('.ix-card-head').getByRole('button', {name: '正在加入…'}).waitFor();
  await page.locator('.el-message', {hasText: '已加入草稿'}).waitFor({timeout: 8000});
  assert.deepEqual(h.calls.find(c => c.path === '/api/servers/mesh/members' && c.method === 'POST').body, {serverIds: ['mac']});
  await h.close();
});
