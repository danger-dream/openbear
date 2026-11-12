// The detail page in Chromium: a tab's sections are read once the tab is shown and scrolled near, the map loads
// with the IP card (OpenStreetMap's, its tiles served here; the built-in one where they fail), the wheel zooms it
// only once it is clicked, the tab bar stays on top and another tab starts at its own top, the live samples stop
// while no monitoring tab is shown, and on a phone nothing is wider than the screen, in either theme.
import test from 'node:test';
import assert from 'node:assert/strict';
import {chromium, startServersBrowser, noOverflow, showTab, until} from './testing/serversBrowser.mjs';

// Scrolling as a reader does, with the wheel over the page.
async function wheel(page, detail, dy) {
  const box = await detail.boundingBox();
  await page.mouse.move(box.x + box.width / 2, 450);
  await page.mouse.wheel(0, dy);
}

test('detail page: tabs, lazy sections, phone and themes', {skip: !chromium, timeout: 240000}, async t => {
  const {setup} = await startServersBrowser(t);

  await t.test('desktop: read when shown and near, the bar on top, live samples only while monitoring is shown', {timeout: 90000}, async t => {
    const h = await setup(t, {width: 1440, height: 900});
    await h.page.locator('.pb-card').filter({hasText: 'Delta-JP'}).click();
    const detail = h.page.locator('.sv-detail:visible');
    await detail.locator('.ov-card').first().waitFor();
    await detail.locator('[data-chart=load] .tc-line').first().waitFor();
    const asked = what => h.calls.filter(c => c.path === `/api/servers/srv-delta/${what}`).length;
    const mapLoaded = () => h.page.evaluate(() => performance.getEntriesByType('resource').some(e => /worldMap\.js/.test(e.name)));
    // 概览 reads its own; what the other tabs hold waits for them.
    assert.equal(await detail.locator('.dt-tabs [aria-selected=true]').innerText(), '概览');
    assert.ok(asked('traffic') >= 1);
    for (const key of ['latency', 'ip-quality', 'bgp']) assert.equal(asked(key), 0, `${key} not read yet`);
    assert.equal(await mapLoaded(), false);

    // 网络: read when shown; the IP card and its map once scrolled to.
    await showTab(detail, '网络');
    await until(() => asked('latency') >= 1);
    await detail.locator('[data-jump=ip]').scrollIntoViewIfNeeded();
    await detail.locator('.ipq-map .leaflet-tile-loaded').first().waitFor();
    assert.equal(asked('ip-quality'), 1);
    assert.equal(await mapLoaded(), true);
    // The wheel over the map scrolls the page past it, and says how to zoom; once the map is clicked it zooms.
    const map = detail.locator('.ipq-map');
    const zooms = () => map.evaluate(el => [...new Set([...el.querySelectorAll('img.leaflet-tile')].map(img => img.src.split('/').at(-3)))]);
    await until(async () => (await zooms()).join() === '10');
    await map.evaluate(el => el.scrollIntoView({block: 'center', behavior: 'instant'}));
    const box = await map.boundingBox(), scrolled = () => detail.evaluate(el => el.closest('.sv-scroll').scrollTop);
    const top = await scrolled();
    // Less than the map's height: the page is scrolled before the wheel event is handed to it, and the event's
    // target is what is under the pointer then.
    await h.page.mouse.move(box.x + box.width / 3, box.y + box.height / 2);
    await h.page.mouse.wheel(0, 60);
    await until(async () => await scrolled() > top);
    await map.getByText('先点一下地图，再用滚轮缩放').waitFor();
    assert.deepEqual(await zooms(), ['10']);
    await h.page.mouse.click(box.x + box.width / 3, box.y + box.height / 2);
    await h.page.mouse.wheel(0, -200);
    await until(async () => { const z = await zooms(); return z.length === 1 && Number(z[0]) > 10; });
    const zoomed = Number((await zooms())[0]);
    await map.getByRole('button', {name: '放大'}).click();
    await until(async () => (await zooms()).join() === String(zoomed + 1));
    // Scrolled down, the bar is on top.
    const bar = () => detail.evaluate(el => {
      const tabs = el.querySelector('.dt-tabs').getBoundingClientRect(), pane = el.closest('.sv-scroll').getBoundingClientRect();
      return {top: Math.round(tabs.top - pane.top), bottom: tabs.bottom, stuck: el.querySelector('.dt-tabs').classList.contains('is-stuck')};
    });
    await until(async () => (await bar()).stuck);
    assert.equal((await bar()).top, 0);
    // Another tab picked down there starts at its own top, under the bar.
    await showTab(detail, '系统');
    const start = await detail.evaluate(el => el.querySelector('[data-panel=system]').getBoundingClientRect().top);
    const under = (await bar()).bottom;
    assert.ok(start >= under - 1 && start <= under + 16, `系统 starts under the bar: ${start} / ${under}`);

    // On 工具 the live samples stop; back on 概览 they go on.
    await showTab(detail, '工具');
    const live = () => h.calls.filter(c => c.path === '/api/servers/srv-delta/metrics' && c.query.after).length;
    await h.page.waitForTimeout(600);
    const before = live();
    await h.page.waitForTimeout(3600);
    assert.equal(live(), before, 'no live samples asked for while 工具 is shown');
    await showTab(detail, '概览');
    await wheel(h.page, detail, -1e5);
    await until(() => live() > before, {timeout: 6000});
    await h.close();
  });

  await t.test('desktop: where OpenStreetMap cannot be reached, the built-in map', {timeout: 60000}, async t => {
    const h = await setup(t, {width: 1440, height: 900, tiles: 'fail'});
    await h.page.locator('.pb-card').filter({hasText: 'Delta-JP'}).click();
    const detail = h.page.locator('.sv-detail:visible');
    await showTab(detail, '网络');
    await detail.locator('[data-jump=ip]').scrollIntoViewIfNeeded();
    await detail.locator('.ipq-map svg.wm .wm-dot').waitFor();
    assert.equal(await detail.locator('.ipm-map').count(), 0);
    await h.close();
  });

  for (const dark of [false, true]) {
    await t.test(`phone${dark ? ', dark' : ''}: two cards a row, four tabs a row, nothing wider than the screen`, {timeout: 90000}, async t => {
      const h = await setup(t, {width: 390, height: 844, dark, touch: true});
      await h.page.locator('.pb-card').filter({hasText: 'Delta-JP'}).click();
      const detail = h.page.locator('.sv-detail:visible');
      await detail.locator('.ov-card').first().waitFor();
      const boxes = await detail.locator('.ov-card').evaluateAll(cards => cards.map(c => { const r = c.getBoundingClientRect(); return [Math.round(r.left), Math.round(r.top)]; }));
      assert.equal(boxes[0][1], boxes[1][1], 'CPU and memory side by side');
      assert.equal(boxes[2][1], boxes[3][1], 'disk and network side by side');
      assert.ok(boxes[2][1] > boxes[0][1]);
      // The four tabs share a row, each its words on one line.
      const tabs = await detail.locator('.dt-tabs [role=tab]').evaluateAll(list => list.map(b => { const r = b.getBoundingClientRect(); return [Math.round(r.top), Math.round(r.height), Math.round(r.right)]; }));
      assert.equal(new Set(tabs.map(([top]) => top)).size, 1, 'one row');
      assert.ok(tabs.every(([, height]) => height <= 40) && tabs.at(-1)[2] <= 390, JSON.stringify(tabs));
      // Every range fits its own strip; the strip scrolls sideways rather than the page.
      const ranges = await detail.locator('.dt-ranges').first().evaluate(el => ({fits: el.getBoundingClientRect().right <= innerWidth, buttons: el.children.length}));
      assert.deepEqual(ranges, {fits: true, buttons: 7});
      for (const [name, panel, ready] of [['概览', 'overview', '[data-jump=traffic] .ix-card-head'], ['网络', 'net', '[data-jump=latency] .ix-card-head'],
        ['系统', 'system', '[data-part=billing] .ix-card-head'], ['工具', 'tools', 'section.net-probe .net-form']]) {
        await showTab(detail, name);
        await detail.locator(ready).first().waitFor();
        if (name === '网络') {
          await detail.locator('[data-jump=ip]').scrollIntoViewIfNeeded();
          await detail.locator('.ipq-map .leaflet-tile-loaded').first().waitFor();
          // One finger scrolls the page past the map; two move and zoom it.
          assert.deepEqual(await detail.locator('.ipm-map').evaluate(el => [getComputedStyle(el).touchAction, el.classList.contains('leaflet-touch-drag')]), ['pan-x pan-y', false]);
        }
        await h.page.waitForTimeout(500);
        await noOverflow(h.page);
        const wide = await detail.locator(`[data-panel=${panel}]`).evaluate(el => [...el.querySelectorAll('*')].filter(n => n.getBoundingClientRect().right > innerWidth + 1
          && !n.closest('.dt-ranges, .sv-ranges, .bg-graph, .leaflet-container, svg')).map(n => n.className?.baseVal ?? n.className).slice(0, 5));
        assert.deepEqual(wide, [], `${name}: nothing past the screen's edge`);
      }
      await h.close();
    });
  }
});
