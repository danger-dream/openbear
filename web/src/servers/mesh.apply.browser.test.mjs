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
});
