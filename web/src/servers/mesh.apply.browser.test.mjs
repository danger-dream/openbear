// The mesh board in Chromium, layout only: apply and rollback.
// Sizes, overflow, scrolling, phone and dark theme, and what needs real pointer input (dragging
// nodes, ports and sheets). What the board asks the controller and what it shows is checked without
// a browser in mesh.board / mesh.canvas / mesh.links.dom.test.mjs. Page, fake controller and helpers:
// testing/meshBrowser.mjs.
import test from 'node:test';
import assert from 'node:assert/strict';
import {GB, SERVERS, centre, chromium, clearOfBoard, dimmed, dragBody, dragPort, fakeMesh, guideCount, inspector, label, member, memberOf, noOverflow, node, server, shot, shots, startMeshBrowser, texts, title, touchDrag, until} from './testing/meshBrowser.mjs';

test('mesh board layout: apply and rollback', {skip: !chromium, timeout: 300000}, async t => {
  const {setup} = await startMeshBrowser(t);
  await t.test('apply: progress on the nodes, a failure counted in the summary and on the node, rollback', {timeout: 90000}, async t => {
    const h = await setup(t, {failing: 'lax'});
    const {page} = h;
    await node(page, 'berlin').click();
    await inspector(page).getByRole('tab', {name: '配置'}).click();
    await inspector(page).getByRole('checkbox', {name: '监听 TCP'}).check();
    await h.saved(d => memberOf(d, 'berlin').listen.tcp === 52000, 'listen');
    const bar = page.locator('.mesh-stage .mesh-bar');
    await bar.getByRole('button', {name: '应用'}).click();
    await node(page, 'berlin').locator('.mesh-node-badge', {hasText: /下发中|已应用/}).waitFor();
    await bar.locator('strong', {hasText: /正在应用/}).waitFor();
    await node(page, 'lax').locator('.mesh-node-badge[data-tone=bad]', {hasText: '失败'}).waitFor({timeout: 15000});
    await bar.locator('strong', {hasText: '1 台失败'}).waitFor({timeout: 15000});
    assert.notEqual(await label(page, '2 · TCP').evaluate(el => getComputedStyle(el).backgroundImage), 'none', 'labels hide the line under them');
    await shot(page, 'desktop-apply-failed');
    await bar.getByRole('button', {name: '回退到上一版'}).click();
    await page.locator('.el-message-box').getByRole('button', {name: '回退'}).click();
    await page.waitForFunction(() => !document.querySelector('.mesh-stage .mesh-bar'), null, {timeout: 20000});
    await h.close();
  });

  await t.test('apply: shows what it prepares at once, takes one click, then the nodes under way and the time it took', {timeout: 90000}, async t => {
    const h = await setup(t, {prepareMs: 2400});
    const {page} = h;
    await node(page, 'berlin').click();
    await inspector(page).getByRole('tab', {name: '配置'}).click();
    await inspector(page).getByRole('checkbox', {name: '监听 TCP'}).check();
    await h.saved(d => memberOf(d, 'berlin').listen.tcp === 52000, 'listen');
    const bar = page.locator('.mesh-stage .mesh-bar');
    const apply = bar.getByRole('button', {name: '应用'});
    await apply.click();
    // At once, before the controller answers: the bar says so and nothing can be applied again.
    await bar.locator('strong', {hasText: '正在准备应用'}).waitFor({timeout: 500});
    assert.equal(await bar.getByRole('button', {name: /应用中/}).isDisabled(), true);
    await bar.locator('.mesh-bar-summary', {hasText: /正在(读取各节点的最新状态|检查中继的 TLS 证书)/}).waitFor({timeout: 3000});
    await bar.locator('.mesh-bar-summary', {hasText: /已用 \d+ 秒/}).waitFor({timeout: 3000});
    await shot(page, 'desktop-apply-preparing');
    await bar.getByRole('button', {name: /应用中/}).click({force: true}).catch(() => {});
    await bar.locator('strong', {hasText: /正在应用/}).waitFor({timeout: 6000});
    assert.equal(h.calls.filter(c => c.method === 'POST' && c.path === '/api/servers/mesh/apply').length, 1, 'one apply');
    await bar.getByRole('button', {name: '查看进度'}).click();
    await bar.locator('.mesh-bar-list li').first().waitFor();
    await shot(page, 'desktop-apply-progress');
    await page.locator('.el-message', {hasText: /已应用到 \d+ 台，用时 \d+ 秒/}).waitFor({timeout: 20000});
    await h.close();
  });
});
