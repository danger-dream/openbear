// The mesh board in Chromium, layout only: routes, dark and phone sheets, adding on a phone.
// Sizes, overflow, scrolling, phone and dark theme, and what needs real pointer input (dragging
// nodes, ports and sheets). What the board asks the controller and what it shows is checked without
// a browser in mesh.board / mesh.canvas / mesh.links.dom.test.mjs. Page, fake controller and helpers:
// testing/meshBrowser.mjs.
import test from 'node:test';
import assert from 'node:assert/strict';
import {GB, SERVERS, centre, chromium, clearOfBoard, dimmed, dragBody, dragPort, fakeMesh, guideCount, inspector, label, member, memberOf, noOverflow, node, server, shot, shots, startMeshBrowser, texts, title, touchDrag, until, frames, settled} from './testing/meshBrowser.mjs';

test('mesh board layout: routes, dark and phone sheets, adding on a phone', {skip: !chromium, timeout: 300000}, async t => {
  const {setup} = await startMeshBrowser(t);
  await t.test('routes: standby entries drawn faint, LAN or internet on punched entries, the routes list and its paths, who gets a gateway network', {timeout: 120000}, async t => {
    const h = await setup(t, {routes: true});
    const {page} = h;
    const style = id => page.locator(`[data-id="${id}"] .mesh-edge-line`).evaluate(el => { const c = getComputedStyle(el); return {opacity: c.strokeOpacity, dash: c.strokeDasharray, width: c.strokeWidth, animation: c.animationName}; });
    const standby = id => page.locator(`[data-id="${id}"] .mesh-edge`).getAttribute('data-quiet');
    // Standbys carry nothing: quiet (faint, thin, finely dashed and still). The entries in use stay solid;
    // so does a standby that fails (Mac's second): a problem is never quiet.
    assert.deepEqual(await style('entry:win:w2'), {opacity: '0.4', dash: '2px, 4px', width: '1px', animation: 'none'});
    for (const id of ['entry:mac:m1', 'entry:win:w1', 'entry:lab:l1']) {
      assert.equal((await style(id)).dash, 'none');
    }
    // Punched entries say how they got through: both office machines reach TOY over the LAN.
    // Win's standby is quiet: its label stays away until Win is selected.
    await node(page, 'win').click();
    // A selected member shows its standbys as normal lines; the others stay faint.
    await node(page, 'mac').click();
    // Its routes: a few rows, the rest behind a tap.
    const panel = inspector(page);
    const routes = panel.locator('.mesh-routes');
    const rows = () => routes.locator('.mesh-route').evaluateAll(list => list.map(el => [...el.children].map(c => c.textContent.trim()).filter(Boolean).join(' | ')));
    await routes.locator('.mesh-route').first().waitFor();
    await routes.getByRole('button', {name: '展开其余 2 条'}).click();
    await routes.getByRole('button', {name: '收起'}).click();
    // A row shows its path on the board, the way path viewing does; the row stays marked.
    await routes.locator('.mesh-route', {hasText: 'LAX'}).click();
    const box = page.locator('.mesh-path');
    await box.locator('.mesh-path-hops').waitFor();
    await shot(page, 'desktop-routes');
    await box.getByRole('button', {name: '关闭路径'}).click();
    // Gateway networks are listed apart: LAX reaches Home's network through Home.
    await node(page, 'lax').click();
    await routes.locator('.mesh-route-sub', {hasText: '网段'}).waitFor();
    const net = routes.locator('.mesh-route', {hasText: '10.10.10.0/24'});
    await net.click();
    await box.locator('.mesh-path-hops').waitFor();
    await box.getByRole('button', {name: '关闭路径'}).click();
    // The gateway: each network says who gets it, by the rules in the draft.
    await node(page, 'home').click();
    const exports = panel.locator('.mesh-exports');
    await shot(page, 'desktop-exports');
    // Editing the rule changes it at once.
    await page.keyboard.press('Escape');
    await panel.getByRole('button', {name: '编辑第 1 条规则'}).click();
    await panel.getByRole('combobox', {name: '来源：添加成员或标签'}).selectOption('members:mac');
    const edited = await h.saved(d => d.rules[0].src.members.includes('mac'), 'rule source');
    await node(page, 'home').click();
    await exports.getByText('下发给：LAX、公司-Mac').waitFor();
    await page.keyboard.press('Escape');
    await panel.getByRole('button', {name: '编辑第 1 条规则'}).click();
    await panel.getByRole('button', {name: '删除规则'}).click();
    await h.saved(d => d.rules.length === 0, 'rule removed');
    await node(page, 'home').click();
    // A member only in the draft has no routes yet.
    h.mesh.doc.members.push(member('lab2', 'Lab-2', 'member', {address: '100.96.0.9', entry: {mode: 'fixed', autoReturn: true, candidates: []}}));
    await page.getByRole('button', {name: '选中 Lab-2'}).click({timeout: 8000});
    await routes.getByText('应用到网格后，这里显示它去各处实际走的路线。').waitFor();
    await h.close();

    // An older controller: no via, no exports, no routes; nothing breaks and nothing is made up.
    const old = await setup(t, {routes: true, oldRoutes: true});
    await node(old.page, 'home').click();
    await inspector(old.page).locator('.mesh-reach').waitFor();
    await old.close();
  });

  await t.test('routes in dark and on phones: the list in the bottom sheet, a row by tap shows the path', {timeout: 150000}, async t => {
    const dark = await setup(t, {routes: true, dark: true});
    await node(dark.page, 'mac').click();
    await inspector(dark.page).locator('.mesh-routes .mesh-route', {hasText: 'LAX'}).click();
    await dark.page.locator('.mesh-path .mesh-path-hops').waitFor();
    await shot(dark.page, 'desktop-dark-routes');
    await node(dark.page, 'home').click();
    await inspector(dark.page).locator('.mesh-exports').waitFor();
    await shot(dark.page, 'desktop-dark-exports');
    await dark.close();

    for (const theme of ['dark', 'light']) {
      const phone = await setup(t, {routes: true, width: 390, height: 844, dark: theme === 'dark'});
      const {page} = phone;
      const sheet = page.locator('.mesh-inspect-sheet');
      await noOverflow(page);
      await shot(page, `phone-routes-board-${theme}`);
      await node(page, 'mac').tap();
      const routes = sheet.locator('.mesh-routes');
      await routes.locator('.mesh-route').first().waitFor();
      await routes.scrollIntoViewIfNeeded();
      // Collapsed by default; rows and the toggle are easy to tap; nothing scrolls sideways.
      for (const b of await routes.locator('.mesh-route, .mesh-fold-toggle').evaluateAll(list => list.map(el => el.getBoundingClientRect().height))) assert.ok(b >= 40, `row ${b}px`);
      assert.ok(await sheet.locator('.mesh-sheet-body').evaluate(el => el.scrollWidth <= el.clientWidth + 1), 'sheet scrolls horizontally');
      await routes.getByRole('button', {name: '展开其余 2 条'}).tap();
      await routes.locator('.mesh-route', {hasText: 'Lab'}).scrollIntoViewIfNeeded();
      await shot(page, `phone-routes-${theme}`);
      // A row by tap: the sheet steps aside and the board shows the path.
      await routes.locator('.mesh-route', {hasText: 'LAX'}).tap();
      await sheet.waitFor({state: 'hidden'});
      const box = page.locator('.mesh-path');
      await box.locator('.mesh-path-hops').waitFor();
      assert.ok(await box.evaluate(el => el.scrollWidth <= el.clientWidth + 1));
      await noOverflow(page);
      await shot(page, `phone-route-path-${theme}`);
      await box.getByRole('button', {name: '关闭路径'}).tap();
      await box.waitFor({state: 'detached'});
      // The gateway's networks in the sheet, long lines wrapped.
      await node(page, 'home').tap();
      const exports = sheet.locator('.mesh-exports');
      await exports.waitFor();
      await exports.scrollIntoViewIfNeeded();
      assert.ok(await sheet.locator('.mesh-sheet-body').evaluate(el => el.scrollWidth <= el.clientWidth + 1));
      await shot(page, `phone-exports-${theme}`);
      await phone.close();
    }
  });

  await t.test('dark desktop and phone: node alert, relay link and path, in a bottom sheet and by taps on the phone', {timeout: 150000}, async t => {
    const dark = await setup(t, {reach: true, dark: true});
    let {page} = dark;
    await node(page, 'mac').click();
    await inspector(page).locator('.mesh-hints summary', {hasText: '备用入口不可用'}).click();
    await inspector(page).getByText('LAX · TCP：连不上：超时 · 从没连上过').waitFor();
    await shot(page, 'desktop-dark-node-alert');
    await label(page, '线路不稳，暂停使用').click();
    await inspector(page).locator('.mesh-relay-metric').waitFor();
    await shot(page, 'desktop-dark-relay-link');
    await node(page, 'home').click();
    await inspector(page).getByRole('button', {name: '查看到…的路径'}).click();
    await node(page, 'mac').click();
    await page.locator('.mesh-path-hops').waitFor();
    await shot(page, 'desktop-dark-path');
    await dark.close();

    const phone = await setup(t, {reach: true, width: 390, height: 844, dark: true});
    ({page} = phone);
    await noOverflow(page);
    // Badges stay readable and the taller nodes do not run into each other.
    const sizes = await page.locator('.mesh-node .pb-tag').evaluateAll(list => list.map(el => parseFloat(getComputedStyle(el).fontSize)));
    assert.ok(Math.min(...sizes) >= 12, `smallest badge ${Math.min(...sizes)}px`);
    const boxes = await page.locator('.vue-flow__node').evaluateAll(list => list.map(el => { const r = el.getBoundingClientRect(); return {id: el.dataset.id, x: r.x, y: r.y, w: r.width, h: r.height}; }));
    for (const a of boxes) for (const b of boxes) if (a.id < b.id) assert.ok(a.x + a.w <= b.x || b.x + b.w <= a.x || a.y + a.h <= b.y || b.y + b.h <= a.y, `${a.id} overlaps ${b.id}`);
    await shot(page, 'phone-board-reach');
    // Every link label can be tapped: none covers another or a node.
    const tags = await page.locator('.mesh-edge-label').evaluateAll(list => list.map(el => { const r = el.getBoundingClientRect(); return {id: el.textContent, x: r.x, y: r.y, w: r.width, h: r.height}; }));
    for (const a of tags) for (const b of [...tags, ...boxes]) if (a !== b) assert.ok(a.x + a.w <= b.x + 1 || b.x + b.w <= a.x + 1 || a.y + a.h <= b.y + 1 || b.y + b.h <= a.y + 1, `label ${a.id} covers ${b.id}`);
    // Mac's alert in the bottom sheet; the failing candidate is named, long lines wrap.
    await node(page, 'mac').tap();
    const sheet = page.locator('.mesh-inspect-sheet');
    await sheet.locator('.mesh-hints summary', {hasText: '备用入口不可用：LAX 备用：超时'}).tap();
    await sheet.getByText('LAX · TCP：连不上：超时 · 从没连上过').waitFor();
    assert.ok((await sheet.locator('.mesh-hints summary').first().boundingBox()).height >= 40);
    assert.ok(await sheet.locator('.mesh-sheet-body').evaluate(el => el.scrollWidth <= el.clientWidth + 1), 'sheet scrolls horizontally');
    await shot(page, 'phone-node-alert');
    await sheet.getByRole('button', {name: '关闭'}).tap();
    await sheet.waitFor({state: 'hidden'});
    // With Mac selected only its lines are up: the selection cleared, every line again.
    await page.locator('.mesh-dock').getByRole('button', {name: '取消选择'}).tap();
    // A relay link by tapping its label; the preference input does not zoom the page.
    await label(page, '线路不稳，暂停使用').tap();
    await sheet.locator('.mesh-sheet-head h2', {hasText: '中转互联'}).waitFor();
    await sheet.getByText('两端测得不同，按较差的一端显示：Home 正常，LAX 线路不稳，暂停使用。').waitFor();
    assert.ok(await sheet.getByRole('spinbutton', {name: '偏好（毫秒）'}).evaluate(el => parseFloat(getComputedStyle(el).fontSize) >= 16));
    assert.ok(await sheet.locator('.mesh-sheet-body').evaluate(el => el.scrollWidth <= el.clientWidth + 1));
    await shot(page, 'phone-relay-link');
    await sheet.getByRole('button', {name: '关闭'}).tap();
    await sheet.waitFor({state: 'hidden'});
    // The path by taps: select Home, "路径…" in the dock, tap Mac.
    await node(page, 'home').tap();
    await sheet.locator('.mesh-sheet-head h2', {hasText: 'Home'}).waitFor();
    await sheet.getByRole('button', {name: '关闭'}).tap();
    await sheet.waitFor({state: 'hidden'});
    await page.locator('.mesh-dock').getByRole('button', {name: '路径…'}).tap();
    await page.locator('.mesh-banner', {hasText: '看 Home 怎么到它'}).waitFor();
    await node(page, 'mac').tap();
    const box = page.locator('.mesh-path');
    await box.locator('.mesh-path-hops').waitFor();
    const canvas = await page.locator('.mesh-canvas').boundingBox();
    const b = await box.boundingBox();
    assert.ok(b.x >= canvas.x && b.x + b.width <= canvas.x + canvas.width, 'the path fits the board');
    assert.ok(await box.evaluate(el => el.scrollWidth <= el.clientWidth + 1));
    assert.ok((await box.getByRole('button', {name: '关闭路径'}).boundingBox()).height >= 40);
    await noOverflow(page);
    const along = await page.locator('.mesh-edge-label').evaluateAll(list => list.map(el => { const r = el.getBoundingClientRect(); return {id: el.textContent, x: r.x, y: r.y, w: r.width, h: r.height}; }));
    const nodesNow = await page.locator('.vue-flow__node').evaluateAll(list => list.map(el => { const r = el.getBoundingClientRect(); return {id: el.dataset.id, x: r.x, y: r.y, w: r.width, h: r.height}; }));
    for (const a of along) for (const o of [...along, ...nodesNow]) if (a !== o) assert.ok(a.x + a.w <= o.x + 1 || o.x + o.w <= a.x + 1 || a.y + a.h <= o.y + 1 || o.y + o.h <= a.y + 1, `on the path, label ${a.id} covers ${o.id}`);
    await shot(page, 'phone-path');
    await box.getByRole('button', {name: '关闭路径'}).tap();
    await box.waitFor({state: 'detached'});
    await phone.close();
  });

  await t.test('phone: add from the + list, tap to select, connect to…, bottom sheet closes by button and by drag', {timeout: 120000}, async t => {
    const h = await setup(t, {width: 390, height: 844, dark: true});
    const {page} = h;
    await noOverflow(page);
    const tools = page.locator('.mesh-fab-row');
    assert.deepEqual(await tools.getByRole('button').evaluateAll(list => list.map(b => b.getAttribute('aria-label'))), ['缩小', '放大', '适应画面', '访问规则图层', '连接']);
    for (const b of await page.locator('.mesh-fab-row .mesh-tb, .mesh-fab, .mesh-dock-entry').all()) assert.ok((await b.boundingBox()).height >= 38);
    // Readable from the start: zoom at least 0.85, laid out top-down within the width, text 12px or more.
    const scale = await page.locator('.vue-flow__viewport').evaluate(el => new DOMMatrix(getComputedStyle(el).transform).a);
    assert.ok(scale >= 0.85, `zoom ${scale}`);
    const board = await page.locator('.mesh-canvas').boundingBox();
    for (const id of ['berlin', 'lax', 'home']) { const b = await node(page, id).boundingBox(); assert.ok(b.x >= board.x && b.x + b.width <= board.x + board.width, `${id} is off to the side`); }
    assert.ok((await node(page, 'home').boundingBox()).y > (await node(page, 'lax').boundingBox()).y + 60, 'members below the relays');
    const sizes = await page.locator('.mesh-node').evaluateAll(list => list.flatMap(n => [...n.querySelectorAll('strong, .mono, .mesh-node-role, .mesh-node-rtt, .pb-tag')].map(el => parseFloat(getComputedStyle(el).fontSize))));
    assert.ok(Math.min(...sizes) >= 12, `smallest text ${Math.min(...sizes)}px`);
    await shot(page, 'phone-board-dark');
    // + opens the server list; one tap adds, placed automatically in the members' row.
    await page.locator('.mesh-board').getByRole('button', {name: '添加服务器'}).tap();
    const add = page.locator('.mesh-add-sheet');
    await add.getByRole('button', {name: '公司-Mac，加入组网'}).waitFor();
    assert.ok((await add.getByRole('button', {name: '公司-Mac，加入组网'}).boundingBox()).height >= 44);
    assert.ok(await add.getByRole('searchbox').evaluate(el => parseFloat(getComputedStyle(el).fontSize) >= 16));
    await shot(page, 'phone-add-dark');
    await add.getByRole('button', {name: '公司-Mac，加入组网'}).tap();
    await add.waitFor({state: 'hidden'});
    await node(page, 'mac').locator('.mesh-node[data-installing]').waitFor();
    await node(page, 'mac').locator('.mesh-node:not([data-installing])').waitFor({timeout: 8000});
    // Placed in the phone's own layout, next to Home; the desktop layout is left alone.
    assert.ok(Math.abs((await node(page, 'mac').boundingBox()).y - (await node(page, 'home').boundingBox()).y) < 2, 'mac joins the members row');
    // Tap a node: its properties open in the bottom sheet; inputs are 16px.
    // The new server is selected after joining; clear it from the dock.
    await page.locator('.mesh-dock').getByRole('button', {name: '取消选择'}).tap();
    await node(page, 'home').tap();
    const sheet = page.locator('.mesh-inspect-sheet');
    await sheet.locator('.mesh-sheet-head h2', {hasText: 'Home'}).waitFor();
    await sheet.getByRole('tab', {name: '配置'}).tap();
    for (const input of await sheet.locator('input:not([type=checkbox]):not([type=radio])').all()) assert.ok(await input.evaluate(el => parseFloat(getComputedStyle(el).fontSize) >= 16));
    assert.ok(await sheet.evaluate(el => el.scrollWidth <= el.clientWidth + 1), 'sheet scrolls horizontally');
    await shot(page, 'phone-sheet-dark');
    // A setting's explanation opens in a tooltip by a tap on ⓘ, and closes by another; the names stay on one line.
    await sheet.getByRole('button', {name: '发送速率：说明'}).tap();
    await page.getByRole('tooltip').filter({hasText: /建议按这条线路上行带宽的九成设置/}).waitFor();
    await sheet.getByRole('button', {name: '发送速率：说明'}).tap();
    await page.getByRole('tooltip').filter({hasText: /建议按这条线路上行带宽的九成设置/}).waitFor({state: 'hidden'});
    for (const l of await sheet.locator('.mesh-field-label > span').all()) assert.ok((await l.boundingBox()).height < 26, `${await l.innerText()} on one line`);
    // Entries: the relay has the first line (with delete) to itself, the order buttons sit on the second.
    await sheet.getByRole('tab', {name: '入口'}).tap();
    const first = sheet.locator('.mesh-cand').first();
    const relayBox = await first.getByRole('combobox', {name: '第 1 个入口的中转'}).boundingBox();
    assert.ok(relayBox.width >= 240, `relay picker ${relayBox.width}px wide`);
    const down = await first.getByRole('button', {name: '下移第 1 个入口'}).boundingBox();
    assert.ok(down.y > relayBox.y + relayBox.height - 2, 'order buttons below the relay');
    assert.ok(down.height >= 32 && down.width >= 32, `order buttons ${down.width}x${down.height}`);
    assert.ok(await sheet.evaluate(el => el.scrollWidth <= el.clientWidth + 1), 'entries fit the width');
    await sheet.getByRole('button', {name: '关闭'}).tap();
    await sheet.waitFor({state: 'hidden'});
    // Still selected: the dock offers the panel again and "connect to…".
    await page.locator('.mesh-dock', {hasText: '已选中，可拖动'}).waitFor();
    await page.locator('.mesh-dock').getByRole('button', {name: '连接到…'}).tap();
    await page.locator('.mesh-banner', {hasText: '把 Home 连过去'}).waitFor();
    await node(page, 'mac').tap();
    await page.locator('.mesh-notice', {hasText: '直连'}).waitFor();
    await h.saved(d => d.directLinks.length === 1 && d.directLinks[0].a === 'home' && d.directLinks[0].b === 'mac', 'direct link');
    await sheet.locator('.mesh-sheet-head h2', {hasText: '直连'}).waitFor();
    // Drag the header down to close (once it has finished sliding in).
    await settled(page);
    const head = await sheet.locator('.mesh-sheet-head').boundingBox();
    await page.mouse.move(head.x + head.width / 2, head.y + 10);
    await page.mouse.down();
    await page.mouse.move(head.x + head.width / 2, head.y + 220, {steps: 6});
    await page.mouse.up();
    await sheet.waitFor({state: 'hidden'});
    await page.locator('.mesh-dock').getByRole('button', {name: '取消选择'}).tap();
    await page.locator('.mesh-dock').getByRole('button', {name: '网格设置与访问规则'}).tap();
    const overview = page.locator('.mesh-overview-sheet');
    await overview.getByRole('textbox', {name: '地址段'}).waitFor();
    await overview.getByText('允许所有成员互相 ping').waitFor();
    await overview.getByRole('button', {name: '关闭'}).tap();
    await overview.waitFor({state: 'hidden'});
    await noOverflow(page);
    await h.close();
    const light = await setup(t, {width: 390, height: 844});
    await shot(light.page, 'phone-board-light');
    await node(light.page, 'home').tap();
    await light.page.locator('.mesh-inspect-sheet .mesh-sparks > li').first().waitFor();
    await shot(light.page, 'phone-sheet-light');
    await light.close();
  });
});
