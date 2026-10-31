// The servers page in Chromium, layout only: dragging cards on the board, with a mouse and a finger.
// Sizes, overflow, scrolling, phone and dark theme, and what needs real pointer events (dragging).
// What the page asks the backend and what it shows is checked without a browser in
// servers.dom.test.mjs and servers.install.dom.test.mjs.
import test from 'node:test';
import assert from 'node:assert/strict';
import {chromium, startServersBrowser, noOverflow, shot, versionShot, settled, until, frames, names, expectFocused, tab} from './testing/serversBrowser.mjs';
import {day, GB, server, many, uninstalledLast, TARGET, RELEASE, versions, MESH_ON, onMesh, card, orders, meshOption, off, fact} from './testing/serversBackend.mjs';

const centreOf = async locator => { const b = await locator.boundingBox(); return {x: b.x + b.width / 2, y: b.y + b.height / 2}; };
// A card (or a list row) dragged by its flag: let go once Sortable has taken the drag (its ghost
// is placed) and finished moving the cards, then wait until the drop has settled.
async function dragFlag(page, from, to, {after = false} = {}) {
  const a = await centreOf(from), box = await to.boundingBox();
  const b = {x: box.x + (after ? box.width - 12 : 12), y: box.y + box.height / 2};
  await page.mouse.move(a.x, a.y);
  await page.mouse.down();
  for (let i = 1; i <= 12; i++) await page.mouse.move(a.x + (b.x - a.x) * i / 12, a.y + (b.y - a.y) * i / 12);
  // A press outside the flag starts no drag: no ghost appears.
  const dragging = await page.waitForFunction(() => document.querySelector('.so-ghost'), null, {timeout: 1000}).then(() => true, () => false);
  if (dragging) await settled(page);
  await page.mouse.up();
  await page.waitForFunction(() => !document.querySelector('.so-ghost'));
  await settled(page);
}

test('servers page layout: dragging cards on the board, with a mouse and a finger', {skip: !chromium, timeout: 300000}, async t => {
  const {setup} = await startServersBrowser(t);
  await t.test('board: drag a card by its flag, filtered, list rows, failure goes back, other sorts and sections', {timeout: 120000}, async t => {
    const h = await setup(t, {tweak: uninstalledLast});
    const {page} = h;
    assert.deepEqual(await names(page), ['Edge-SG.Game', 'Papa-HK.T1', 'GoldenCloud-1212 JP 测试 测试机 超长的名称用来检查卡片里的截断', 'Delta-JP', 'Bravo-FRA', 'Hotel-HK']);
    const flag = name => card(page, name).locator('.pb-flag');
    assert.equal(await flag('Bravo-FRA').getAttribute('title'), '德国 · 拖动调整顺序');
    assert.equal(await flag('Bravo-FRA').evaluate(el => getComputedStyle(el).cursor), 'grab');
    // Dragging the card body does nothing; the flag moves it, and the order is saved at once.
    await dragFlag(page, card(page, 'Bravo-FRA').locator('.pb-spec'), card(page, 'Edge-SG.Game'));
    assert.equal(orders(h).length, 0, 'only the flag drags');
    await dragFlag(page, flag('Bravo-FRA'), card(page, 'Edge-SG.Game'));
    await page.waitForResponse(r => r.url().endsWith('/api/servers/order')).catch(() => {});
    assert.equal(orders(h).length, 1);
    assert.deepEqual(orders(h)[0].body.ids, ['srv-bravo', 'local', 'srv-papa', 'srv-golf', 'srv-delta', 'srv-hotel']);
    assert.deepEqual((await names(page)).slice(0, 2), ['Bravo-FRA', 'Edge-SG.Game']);
    assert.equal(await page.getByRole('dialog', {name: '调整顺序'}).count(), 0);
    await shot(page, 'desktop-board-dragged');
    // An uninstalled server stays after the installed ones.
    await dragFlag(page, flag('Hotel-HK'), card(page, 'Bravo-FRA'));
    assert.equal(orders(h).length, 1, 'no move across installed and uninstalled');
    assert.equal((await names(page)).at(-1), 'Hotel-HK');
    // Filtered by group: the two shown swap places; everything else keeps its place.
    await page.locator('.pb-chips[aria-label=按分组筛选]').getByRole('button', {name: '测试机'}).click();
    assert.deepEqual(await names(page), ['Edge-SG.Game', 'GoldenCloud-1212 JP 测试 测试机 超长的名称用来检查卡片里的截断']);
    await dragFlag(page, flag('GoldenCloud'), card(page, 'Edge-SG.Game'));
    await until(() => orders(h).length >= 2);
    assert.deepEqual(orders(h)[1].body.ids, ['srv-bravo', 'srv-golf', 'srv-papa', 'local', 'srv-delta', 'srv-hotel']);
    assert.equal((await names(page))[0], 'GoldenCloud-1212 JP 测试 测试机 超长的名称用来检查卡片里的截断');
    await page.locator('.pb-chips[aria-label=按分组筛选]').getByRole('button', {name: '全部'}).click();
    // Saving fails: the cards go back and say why.
    await page.route('**/api/servers/order', route => route.fulfill({status: 503, contentType: 'application/json', body: JSON.stringify({ok: false, error: 'storage_unavailable'})}));
    const before = await names(page);
    await dragFlag(page, flag('Delta-JP'), card(page, 'Bravo-FRA'));
    await page.locator('.el-message--error').filter({hasText: '顺序没有保存'}).waitFor();
    assert.deepEqual(await names(page), before);
    await page.unroute('**/api/servers/order');
    // Another sort is not the saved order: nothing to drag by.
    await page.getByRole('combobox', {name: '排序方式'}).selectOption('name');
    assert.equal(await page.locator('.pb-card .pb-grip').count(), 0);
    assert.equal(await flag('Bravo-FRA').getAttribute('title'), '德国');
    const sorted = await names(page);
    await dragFlag(page, flag('Papa-HK.T1'), card(page, sorted[0]));
    assert.deepEqual(await names(page), sorted);
    await page.getByRole('combobox', {name: '排序方式'}).selectOption('default');
    // List view: the row's flag, the same way.
    await page.getByRole('button', {name: '列表视图'}).click();
    const row = name => page.locator('.pl-row').filter({hasText: name});
    const rowNames = () => page.locator('.pl-row .pl-name > strong').allInnerTexts();
    assert.equal(await page.locator('.pl-head').count(), 1);
    await dragFlag(page, row('Delta-JP').locator('.pb-grip'), row('GoldenCloud'));
    await until(() => orders(h).length >= 3);
    assert.equal(orders(h).length, 3);
    assert.deepEqual((await rowNames()).slice(0, 3), ['Bravo-FRA', 'Delta-JP', 'GoldenCloud-1212 JP 测试 测试机 超长的名称用来检查卡片里的截断']);
    assert.equal(await page.locator('.pl > .pl-head + .pl-row').count(), 1, 'the header stays first');
    await shot(page, 'desktop-list-dragged');
    // The dialog is still there, as the keyboard way to reorder.
    await page.getByRole('button', {name: '调整顺序'}).click();
    await page.getByRole('dialog', {name: '调整顺序'}).getByRole('button', {name: '下移 Bravo-FRA'}).waitFor();
    await h.close();
  });

  await t.test('board: dragging to the edge scrolls the page, and only then; dark theme', {timeout: 90000}, async t => {
    const h = await setup(t, {height: 700, dark: true, tweak: items => { many(items); uninstalledLast(items); }});
    const {page} = h;
    const scroller = await page.evaluateHandle(() => { let el = document.querySelector('.pb-grid'); while (el && !(/(auto|scroll)/.test(getComputedStyle(el).overflowY) && el.scrollHeight > el.clientHeight)) el = el.parentElement; return el || document.scrollingElement; });
    const top = () => scroller.evaluate(el => el.scrollTop);
    assert.equal(await top(), 0);
    const first = card(page, 'Edge-SG.Game').locator('.pb-flag');
    const a = await centreOf(first);
    const view = await scroller.evaluate(el => { const r = el === document.scrollingElement ? {top: 0, bottom: innerHeight} : el.getBoundingClientRect(); return {top: r.top, bottom: r.bottom}; });
    await page.mouse.move(a.x, a.y);
    await page.mouse.down();
    // In the middle of the page nothing scrolls.
    for (let i = 1; i <= 6; i++) await page.mouse.move(a.x + 20, a.y + (view.top + (view.bottom - view.top) / 2 - a.y) * i / 6);
    await page.waitForTimeout(500);
    assert.equal(await top(), 0, 'no scrolling away from the edge');
    // At the bottom edge it does.
    for (let i = 1; i <= 6; i++) await page.mouse.move(a.x + 20, view.bottom - 10);
    await page.waitForTimeout(900);
    const scrolled = await top();
    assert.ok(scrolled > 100, `scrolled ${scrolled}px at the edge`);
    await shot(page, 'desktop-dark-board-edge-scroll');
    await page.mouse.move(a.x + 20, view.top + (view.bottom - view.top) / 2);
    await page.waitForTimeout(300);
    const held = await top();
    await page.waitForTimeout(400);
    assert.equal(await top(), held, 'stops when back from the edge');
    await page.mouse.up();
    await page.waitForTimeout(400);
    assert.equal(orders(h).length, 1);
    assert.notEqual(orders(h)[0].body.ids[0], 'local');
    await h.close();
  });

  await t.test('board: a poll that empties the filtered list during a drag ends the drag; the list follows again', {timeout: 60000}, async t => {
    const h = await setup(t);
    const {page} = h;
    await page.locator('button.pb-stat', {hasText: '离线'}).click();
    assert.deepEqual(await names(page), ['GoldenCloud-1212 JP 测试 测试机 超长的名称用来检查卡片里的截断']);
    const a = await centreOf(card(page, 'GoldenCloud').locator('.pb-flag'));
    await page.mouse.move(a.x, a.y);
    await page.mouse.down();
    for (let i = 1; i <= 6; i++) await page.mouse.move(a.x + i * 6, a.y + i * 4);
    // It comes online while held: the live poll (every 3 s) takes it out of the offline filter.
    const golf = h.items.find(i => i.id === 'srv-golf');
    golf.live = JSON.parse(JSON.stringify(h.items.find(i => i.live && !i.local).live));
    const polls = () => h.calls.filter(c => c.path === '/api/servers/live').length, seen = polls();
    await until(() => polls() > seen);
    await page.waitForResponse(r => r.url().endsWith('/api/servers/live'));
    await frames(page);
    // Held still: the card stays under the pointer until it is let go.
    assert.equal(await page.locator('.pb-card').count(), 1, 'the list holds still during the drag');
    await page.mouse.up();
    await page.locator('.instance-empty', {hasText: '没有符合条件的服务器'}).waitFor({timeout: 3000});
    assert.equal(orders(h).length, 0);
    // Not stuck on the frozen list: without the filter every server shows again.
    await page.locator('.pb-quick').getByRole('button', {name: '显示全部'}).click();
    assert.equal((await names(page)).length, 6);
    await h.close();
  });

  await t.test('phone: drag a card by its flag with a finger; a swipe elsewhere scrolls; light and dark', {timeout: 120000}, async t => {
    for (const dark of [false, true]) {
      const h = await setup(t, {width: 390, height: 844, dark, touch: true, tweak: items => { many(items); uninstalledLast(items); }});
      const {page} = h;
      const cdp = await page.context().newCDPSession(page);
      const touch = async (type, x, y) => cdp.send('Input.dispatchTouchEvent', {type, touchPoints: type === 'touchEnd' ? [] : [{x: Math.round(x), y: Math.round(y)}]});
      async function swipe(from, to, steps = 14, hold = 0) {
        await touch('touchStart', from.x, from.y);
        for (let i = 1; i <= steps; i++) { await touch('touchMove', from.x + (to.x - from.x) * i / steps, from.y + (to.y - from.y) * i / steps); await page.waitForTimeout(16); }
        if (hold) await page.waitForTimeout(hold);
        await touch('touchEnd');
        await page.waitForTimeout(400);
      }
      const scroller = await page.evaluateHandle(() => { let el = document.querySelector('.pb-grid'); while (el && !(/(auto|scroll)/.test(getComputedStyle(el).overflowY) && el.scrollHeight > el.clientHeight)) el = el.parentElement; return el || document.scrollingElement; });
      const top = () => scroller.evaluate(el => el.scrollTop);
      // The flag takes the touch: a bigger target than the emoji, and no page scroll from it.
      const flag = card(page, 'Papa-HK.T1').locator('.pb-flag');
      assert.equal(await flag.evaluate(el => getComputedStyle(el).touchAction), 'none');
      const size = await flag.boundingBox();
      assert.ok(size.width >= 30 && size.height >= 30, `flag target ${size.width}x${size.height}`);
      await shot(page, `phone-board-${dark ? 'dark' : 'light'}`);
      // A swipe on the card body is an ordinary scroll and moves nothing.
      const body = await centreOf(card(page, 'Papa-HK.T1'));
      await swipe(body, {x: body.x, y: body.y - 300});
      assert.ok(await top() > 100, `page scrolled ${await top()}px`);
      assert.equal(orders(h).length, 0);
      await scroller.evaluate(el => { el.scrollTop = 0; });
      await frames(page);
      // Papa (second) dragged by its flag onto Edge (first): the page holds still and the order is saved.
      const a = await centreOf(flag), b = await centreOf(card(page, 'Edge-SG.Game').locator('.pb-head'));
      await swipe(a, {x: a.x, y: b.y - 10}, 14, 250);
      assert.equal(await top(), 0, 'the page did not scroll with the finger');
      await until(() => orders(h).length >= 1);
      assert.equal(orders(h).length, 1);
      assert.deepEqual(orders(h)[0].body.ids.slice(0, 2), ['srv-papa', 'local']);
      assert.deepEqual((await names(page)).slice(0, 2), ['Papa-HK.T1', 'Edge-SG.Game']);
      await noOverflow(page);
      await shot(page, `phone-board-dragged-${dark ? 'dark' : 'light'}`);
      await h.close();
    }
  });

});
