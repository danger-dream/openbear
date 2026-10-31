// The mesh board in Chromium, layout only: link ends, phone touch, older controller, widths and themes.
// Sizes, overflow, scrolling, phone and dark theme, and what needs real pointer input (dragging
// nodes, ports and sheets). What the board asks the controller and what it shows is checked without
// a browser in mesh.board / mesh.canvas / mesh.links.dom.test.mjs. Page, fake controller and helpers:
// testing/meshBrowser.mjs.
import test from 'node:test';
import assert from 'node:assert/strict';
import {GB, SERVERS, centre, chromium, clearOfBoard, dimmed, dragBody, dragPort, fakeMesh, guideCount, inspector, label, member, memberOf, noOverflow, node, server, shot, shots, startMeshBrowser, texts, title, touchDrag, until, frames, settled} from './testing/meshBrowser.mjs';

test('mesh board layout: link ends, phone touch, older controller, widths and themes', {skip: !chromium, timeout: 300000}, async t => {
  const {setup} = await startMeshBrowser(t);
  for (const dark of [false, true]) {
    await t.test(`canvas, phone ${dark ? 'dark' : 'light'}: touch drag snaps; a link's ends and stability in the sheet; nothing folds`, {timeout: 120000}, async t => {
      const h = await setup(t, {width: 390, height: 844, dark, lines: true});
      const {page} = h;
      assert.equal(await page.getByRole('button', {name: '收起属性面板'}).count(), 0);
      assert.equal(await page.getByRole('button', {name: '收起服务器列表'}).count(), 0);
      // On the phone a node moves once it is selected (so a swipe still pans): select Home, close its sheet,
      // then drag it by touch until its left edge is 3 px from Berlin's: it snaps, with a guide.
      await node(page, 'home').tap();
      const sheet = page.locator('.mesh-inspect-sheet');
      await sheet.getByRole('button', {name: '关闭'}).tap();
      await sheet.waitFor({state: 'hidden'});
      const b = await node(page, 'berlin').boundingBox(), m = await node(page, 'home').boundingBox();
      const from = {x: m.x + m.width / 2, y: m.y + m.height / 2};
      const drag = await touchDrag(page, from, {x: from.x + (b.x - m.x) + 3, y: from.y + 30});
      await frames(page);
      assert.ok(await guideCount(page) >= 1, 'a guide under the finger');
      // Snapped: one of Home's edges or its centre sits exactly on another node's.
      const boxes = await page.locator('.vue-flow__node').evaluateAll(list => list.map(el => { const r = el.getBoundingClientRect(); return {id: el.dataset.id, x: r.x, y: r.y, w: r.width, h: r.height}; }));
      const me = boxes.find(x => x.id === 'home'), lines = (o, k, d) => [o[k], o[k] + o[d] / 2, o[k] + o[d]];
      assert.ok(boxes.filter(o => o.id !== 'home').some(o => [['x', 'w'], ['y', 'h']].some(([k, d]) => lines(me, k, d).some(v => lines(o, k, d).some(at => Math.abs(at - v) < 1)))), 'snapped');
      await shot(page, `canvas-phone-guides-${dark ? 'dark' : 'light'}`);
      await drag.end();
      await frames(page);
      assert.equal(await guideCount(page), 0);
      // The entry line in the sheet: ends wrap, the range buttons wrap, nothing scrolls sideways.
      await label(page, '2 · TCP').tap();
      await sheet.locator('.mesh-line-ends').waitFor();
      await sheet.locator('.mesh-line-trend .mesh-sparks').waitFor();
      assert.equal(await page.locator('.mesh-peek').count(), 0, 'the phone keeps its sheet');
      await sheet.locator('.mesh-line-ends').scrollIntoViewIfNeeded();
      await noOverflow(page);
      for (const el of await sheet.locator('.mesh-line-ends, .mesh-line-trend').all()) assert.ok(await el.evaluate(x => x.scrollWidth <= x.clientWidth + 1), 'no sideways scroll inside');
      const tabs = await sheet.locator('.mesh-line-ranges [role=tab]').evaluateAll(list => list.map(b => b.getBoundingClientRect().height));
      assert.ok(tabs.every(hh => hh >= 24), 'range buttons stay tappable');
      await shot(page, `canvas-phone-line-${dark ? 'dark' : 'light'}`);
      await sheet.locator('.mesh-line-trend').scrollIntoViewIfNeeded();
      await shot(page, `canvas-phone-stability-${dark ? 'dark' : 'light'}`);
      // A member scrolled down: the sticky summary and tabs meet the sheet's header with no strip
      // of the content scrolling by between them (the same block stays on top on every tab).
      await sheet.getByRole('button', {name: '关闭'}).tap();
      await sheet.waitFor({state: 'hidden'});
      await node(page, 'home').tap();
      await sheet.locator('.mesh-trend .mesh-sparks').waitFor();
      const body = sheet.locator('.mesh-sheet-body');
      await body.evaluate(el => { el.scrollTop = 400; });
      await frames(page);
      assert.ok(await body.evaluate(el => el.scrollTop) > 100, 'scrolled');
      const seam = await sheet.evaluate(el => {
        const head = el.querySelector('.mesh-sheet-head').getBoundingClientRect(), tabs = el.querySelector('.mesh-member-tabs').getBoundingClientRect();
        const hits = [0.5, 1.5, 2.5, 3.5, 4.5].map(d => document.elementFromPoint(tabs.left + tabs.width / 2, head.bottom + d));
        return {gap: tabs.top - head.bottom, covered: hits.every(x => x && (x.closest('.mesh-member-tabs') || x.closest('.mesh-sheet-head')))};
      });
      assert.ok(seam.gap <= 0.5, `no gap under the sheet header (${seam.gap}px)`);
      assert.ok(seam.covered, 'nothing scrolls by between the header and the summary');
      await shot(page, `canvas-phone-sticky-${dark ? 'dark' : 'light'}`);
      await h.close();
    });
  }

  await t.test('older controller: no layout, no installs list, layout and trend 404, and no mesh at all', {timeout: 90000}, async t => {
    const h = await setup(t, {old: true});
    const {page} = h;
    // Laid out automatically: relays in a row on top.
    const [b, l, m] = await Promise.all(['berlin', 'lax', 'home'].map(id => node(page, id).boundingBox()));
    assert.ok(Math.abs(b.y - l.y) < 1 && m.y > b.y + 100, 'relays on top');
    const start = await centre(node(page, 'home'));
    await page.mouse.move(start.x, start.y);
    await page.mouse.down();
    await page.mouse.move(start.x + 100, start.y + 40, {steps: 6});
    await page.mouse.up();
    await page.locator('.mesh-layout-note', {hasText: '位置只在本页保留'}).waitFor();
    const movedTo = await centre(node(page, 'home'));
    await page.waitForResponse(r => new URL(r.url()).pathname === '/api/servers/mesh', {timeout: 9000});
    await frames(page);
    assert.deepEqual(await centre(node(page, 'home')), movedTo, 'a poll keeps the local position');
    await node(page, 'home').click();
    await inspector(page).getByText('主控还不提供趋势数据。').waitFor();
    await h.close();
    const none = await setup(t, {missing: true});
    await none.page.getByText('主控还不支持组网，更新 OpenBear 后再试。').waitFor();
    await none.close();
  });

  await t.test('desktop widths and themes: three columns at 1440 and 1024, light and dark', {timeout: 120000}, async t => {
    for (const [width, dark] of [[1440, false], [1440, true], [1024, false], [1024, true]]) {
      const h = await setup(t, {width, height: width > 1200 ? 900 : 768, dark});
      const {page} = h;
      await noOverflow(page);
      const [left, mid, right] = await Promise.all(['.mesh-palette', '.mesh-canvas', '.mesh-inspector-col'].map(s => page.locator(s).boundingBox()));
      assert.ok(left.x + left.width <= mid.x && mid.x + mid.width <= right.x + 1 && mid.width > 380, `${width}: ${JSON.stringify([left, mid, right])}`);
      if (dark) assert.notEqual(await page.locator('.mesh-node').first().evaluate(el => getComputedStyle(el).backgroundColor), 'rgb(255, 255, 255)');
      await node(page, 'home').click();
      await inspector(page).locator('.mesh-sparks > li').first().waitFor();
      // Home runs Linux: no macOS firewall hint even though the status mentions one.
      await shot(page, `desktop-${width}-${dark ? 'dark' : 'light'}`);
      await h.close();
    }
  });
});
