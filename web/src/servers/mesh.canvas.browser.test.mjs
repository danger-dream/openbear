// The mesh board in Chromium, layout only: layout saving, alignment, folding columns.
// Sizes, overflow, scrolling, phone and dark theme, and what needs real pointer input (dragging
// nodes, ports and sheets). What the board asks the controller and what it shows is checked without
// a browser in mesh.board / mesh.canvas / mesh.links.dom.test.mjs. Page, fake controller and helpers:
// testing/meshBrowser.mjs.
import test from 'node:test';
import assert from 'node:assert/strict';
import {GB, SERVERS, centre, chromium, clearOfBoard, dimmed, dragBody, dragPort, fakeMesh, guideCount, inspector, label, member, memberOf, noOverflow, node, server, shot, shots, startMeshBrowser, texts, title, touchDrag, until, frames, settled} from './testing/meshBrowser.mjs';

test('mesh board layout: layout saving, alignment, folding columns', {skip: !chromium, timeout: 300000}, async t => {
  const {setup} = await startMeshBrowser(t);
  await t.test('layout: a moved node is saved after the drag ends; no auto layout; not part of the draft', {timeout: 90000}, async t => {
    const h = await setup(t);
    const {page} = h;
    const start = await centre(node(page, 'home'));
    await page.mouse.move(start.x - 40, start.y);
    await page.mouse.down();
    await page.mouse.move(start.x + 80, start.y + 60, {steps: 8});
    await page.mouse.up();
    for (let i = 0; i < 30 && !h.layoutPuts().length; i++) await page.waitForTimeout(100);
    const moved = h.layoutPuts().at(-1).body.positions;
    assert.deepEqual(Object.keys(moved), ['home']);
    assert.ok(moved.home.x > 150 + 60 && moved.home.y > 220 + 30, JSON.stringify(moved));
    assert.equal(h.layoutPuts().length, 1, 'one save after the drag');
    // Auto layout is gone: no button, and the saved positions are what the board shows.
    assert.equal(await page.getByRole('button', {name: '自动排布'}).count(), 0);
    assert.equal(h.puts().length, 0, 'moving nodes changes no draft');
    assert.equal(await page.locator('.mesh-stage .mesh-bar').count(), 0);
    await page.locator('.mesh-toolbar').getByRole('button', {name: '放大'}).click();
    await page.waitForFunction(() => document.querySelector('.mesh-tb-zoom')?.innerText.trim() !== '100%');
    assert.notEqual(await page.locator('.mesh-tb-zoom').innerText(), '100%');
    await h.close();
  });

  await t.test('canvas: dragging lines a node up with the others, guides while it moves; pan and zoom unchanged', {timeout: 90000}, async t => {
    const h = await setup(t);
    const {page} = h;
    // Home's left edge 4 px from Berlin's: it snaps there, with a guide, while the pointer is still down.
    const b = await node(page, 'berlin').boundingBox(), m = await node(page, 'home').boundingBox();
    const start = {x: m.x + 30, y: m.y + m.height / 2};
    await page.mouse.move(start.x, start.y);
    await page.mouse.down();
    // The board starts the drag once the pointer has moved past its threshold (2 px); from there the node follows.
    await page.mouse.move(start.x, start.y + 3);
    await page.mouse.move(start.x + (b.x - m.x) + 4, start.y + 23, {steps: 10});
    await frames(page);
    assert.ok(await guideCount(page) >= 1, 'a guide while aligned');
    assert.ok(Math.abs((await node(page, 'home').boundingBox()).x - b.x) < 1, 'snapped to the edge');
    await shot(page, 'canvas-desktop-guides');
    await page.mouse.up();
    assert.equal(await guideCount(page), 0, 'guides go with the drag');
    for (let i = 0; i < 30 && !h.layoutPuts().length; i++) await page.waitForTimeout(100);
    assert.equal(h.layoutPuts().at(-1).body.positions.home.x, 0, 'the snapped position is saved');
    // Centres: LAX's centre near Home's centre lines up on that axis.
    const l = await node(page, 'lax').boundingBox(), m2 = await node(page, 'home').boundingBox();
    const from = {x: l.x + l.width / 2, y: l.y + l.height / 2};
    await page.mouse.move(from.x, from.y);
    await page.mouse.down();
    await page.mouse.move(from.x, from.y + 3);
    await page.mouse.move(from.x + (m2.x + m2.width / 2 - from.x) + 3, from.y + 63, {steps: 10});
    await frames(page);
    const l2 = await node(page, 'lax').boundingBox();
    assert.ok(Math.abs(l2.x + l2.width / 2 - (m2.x + m2.width / 2)) < 1, 'centres line up');
    // Far from everything: no guide, the node follows the pointer.
    await page.mouse.move(from.x + 260, from.y + 140, {steps: 6});
    await frames(page);
    assert.equal(await guideCount(page), 0);
    await page.mouse.up();
    // Panning the empty board and zooming with the wheel still work, with no guide.
    const before = await node(page, 'berlin').boundingBox(), stage = await page.locator('.mesh-canvas').boundingBox();
    await page.mouse.move(stage.x + 30, stage.y + stage.height - 60);
    await page.mouse.down();
    await page.mouse.move(stage.x + 110, stage.y + stage.height - 20, {steps: 6});
    assert.equal(await guideCount(page), 0);
    await page.mouse.up();
    const after = await node(page, 'berlin').boundingBox();
    assert.ok(Math.abs(after.x - before.x - 80) < 2 && Math.abs(after.y - before.y - 40) < 2, 'panned');
    const zoom = await page.locator('.mesh-tb-zoom').innerText();
    await page.mouse.move(stage.x + stage.width / 2, stage.y + stage.height / 2);
    await page.mouse.wheel(0, -300);
    await page.waitForFunction(before => document.querySelector('.mesh-tb-zoom')?.innerText.trim() !== before, zoom.trim());
    assert.notEqual(await page.locator('.mesh-tb-zoom').innerText(), zoom, 'wheel zoom');
    assert.equal(h.puts().length, 0, 'moving nodes changes no draft');
    await h.close();
  });

  await t.test('canvas: the panel folds away and is remembered; a selection then shows a small card with the way back', {timeout: 90000}, async t => {
    const h = await setup(t, {lines: true});
    const {page} = h;
    const toolbar = page.locator('.mesh-toolbar');
    const wide = (await page.locator('.mesh-canvas').boundingBox()).width;
    // The fold button sits right of "访问规则".
    const names = await toolbar.getByRole('button').evaluateAll(list => list.map(b => b.getAttribute('aria-label') || b.textContent.trim()));
    await toolbar.getByRole('button', {name: '收起属性面板'}).click();
    assert.ok((await page.locator('.mesh-canvas').boundingBox()).width > wide + 300, 'the board takes the room');
    // A node: name, state, role and address; alerts if any; the way to the panel.
    await node(page, 'home').click();
    const peek = page.locator('.mesh-peek');
    await peek.waitFor();
    await clearOfBoard(page, peek);
    await shot(page, 'canvas-desktop-folded-node');
    // A link: what it is and how the line is doing.
    await label(page, '2 · TCP').click();
    await shot(page, 'canvas-desktop-folded-link');
    await peek.getByRole('button', {name: '取消选择'}).click();
    // Remembered across a reload.
    await page.reload();
    await node(page, 'home').waitFor();
    await label(page, '2 · TCP').click();
    await peek.getByRole('button', {name: '展开面板查看详情'}).click();
    await inspector(page).locator('.mesh-line-ends').waitFor();
    await h.close();
  });

  await t.test('canvas: the server list folds to a rail with the tools; its search stays on top while the list scrolls', {timeout: 90000}, async t => {
    const h = await setup(t, {height: 640});
    const {page} = h;
    // Many servers to scroll through.
    const palette = page.locator('.mesh-palette');
    const search = palette.getByRole('searchbox', {name: '搜索服务器'});
    const list = palette.locator('.mesh-servers-scroll');
    await page.evaluate(() => { const ul = document.querySelector('.mesh-servers-scroll ul'); for (let i = 0; i < 30; i++) ul.appendChild(ul.firstElementChild.cloneNode(true)); });
    const top = (await search.boundingBox()).y;
    await list.evaluate(el => { el.scrollTop = 400; });
    await frames(page);
    assert.ok(await list.evaluate(el => el.scrollTop) > 100, 'the list scrolls');
    assert.equal((await search.boundingBox()).y, top, 'the search stays');
    assert.equal(await palette.locator('.mesh-servers').evaluate(el => el.scrollTop), 0);
    await shot(page, 'canvas-desktop-list-scrolled');
    // Fold: a rail with the way back and the tools; connecting still works from it.
    await palette.getByRole('button', {name: '收起服务器列表'}).click();
    const rail = page.locator('.mesh-rail');
    assert.ok((await rail.boundingBox()).width <= 46);
    await rail.getByRole('radio', {name: '访问规则'}).click();
    await rail.getByRole('radio', {name: '选择'}).click();
    // Both folded: the board has nearly the whole width.
    await page.locator('.mesh-toolbar').getByRole('button', {name: '收起属性面板'}).click();
    assert.ok((await page.locator('.mesh-canvas').boundingBox()).width > 1440 - 100, 'the board has nearly the whole width');
    await shot(page, 'canvas-desktop-both-folded');
    await page.reload();
    await node(page, 'home').waitFor();
    await page.locator('.mesh-rail').getByRole('button', {name: '展开服务器列表'}).click();
    await page.getByRole('searchbox', {name: '搜索服务器'}).waitFor();
    await h.close();
  });
});
