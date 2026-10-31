// The servers page in Chromium, layout only: many servers, region picker, the reorder dialog.
// Sizes, overflow, scrolling, phone and dark theme, and what needs real pointer events (dragging).
// What the page asks the backend and what it shows is checked without a browser in
// servers.dom.test.mjs and servers.install.dom.test.mjs.
import test from 'node:test';
import assert from 'node:assert/strict';
import {chromium, startServersBrowser, noOverflow, shot, versionShot, settled, until, frames, names, expectFocused, tab} from './testing/serversBrowser.mjs';
import {day, GB, server, many, uninstalledLast, TARGET, RELEASE, versions, MESH_ON, onMesh, card, orders, meshOption, off, fact} from './testing/serversBackend.mjs';

test('servers page layout: many servers, region picker, the reorder dialog', {skip: !chromium, timeout: 300000}, async t => {
  const {setup} = await startServersBrowser(t);
  await t.test('many servers: search, sort and view stay together; reorder scrolls while dragging', {timeout: 120000}, async t => {
    const h = await setup(t, {height: 760, tweak: many});
    const tools = h.page.locator('.pb-tools');
    const search = h.page.getByRole('searchbox', {name: '搜索服务器'});
    assert.equal(Math.round((await search.boundingBox()).width), 200);
    const height = async name => Math.round((await h.page.getByRole(name === 'search' ? 'searchbox' : 'combobox', {name: name === 'search' ? '搜索服务器' : '排序方式'}).boundingBox()).height);
    assert.ok(Math.abs(await height('search') - await height('sort')) <= 2, `search ${await height('search')}px, sort ${await height('sort')}px`);
    const tops = await tools.locator(':scope > *').evaluateAll(els => els.map(el => Math.round(el.getBoundingClientRect().top + el.getBoundingClientRect().height / 2)));
    assert.ok(Math.max(...tops) - Math.min(...tops) <= 2, JSON.stringify(tops));
    const right = await h.page.locator('.pb-filters').evaluate(el => el.getBoundingClientRect().right);
    assert.ok(Math.abs((await tools.boundingBox()).x + (await tools.boundingBox()).width - right) <= 1);
    await search.fill('Extra-1');
    assert.ok((await names(h.page)).every(n => n.startsWith('Extra-1')));
    await search.fill('');
    await shot(h.page, 'desktop-many-filters');

    await h.page.getByRole('button', {name: '调整顺序'}).click();
    const dialog = h.page.getByRole('dialog', {name: '调整顺序'});
    const list = dialog.getByRole('region', {name: '已安装节点'}).locator('.so-list');
    await list.locator('.so-item').first().waitFor();
    assert.ok(await list.evaluate(el => el.scrollHeight > el.clientHeight + 100), 'list must overflow for this check');
    const grip = await list.locator('.so-item').first().locator('.so-grip').boundingBox(), box = await list.boundingBox();
    await h.page.mouse.move(grip.x + grip.width / 2, grip.y + grip.height / 2);
    await h.page.mouse.down();
    // Past the bottom edge, holding still, as a person dragging "to the bottom" does.
    for (let i = 1; i <= 10; i++) await h.page.mouse.move(grip.x + grip.width / 2, grip.y + (box.y + box.height + 40 - grip.y) * i / 10);
    // Held still past the edge, the list keeps scrolling.
    await h.page.waitForFunction(() => document.querySelector('[aria-label=已安装节点] .so-list').scrollTop > 100).catch(() => {});
    const scrolled = await list.evaluate(el => el.scrollTop);
    assert.ok(scrolled > 100, `list scrolled ${scrolled}px while dragging past its bottom edge`);
    await h.page.waitForFunction(() => { const el = document.querySelector('[aria-label=已安装节点] .so-list'); return el.scrollTop + el.clientHeight >= el.scrollHeight - 2; }).catch(() => {});
    assert.ok(await list.evaluate(el => el.scrollTop + el.clientHeight >= el.scrollHeight - 2), 'reaches the end');
    await h.page.mouse.move(grip.x + grip.width / 2, box.y + box.height - 8);
    await h.page.mouse.up();
    await h.page.waitForFunction(() => !document.querySelector('.so-ghost'));
    await settled(h.page);
    const order = await list.locator('.so-name').allInnerTexts();
    assert.ok(order.indexOf('Edge-SG.Game') >= order.length - 3, `dragged to ${order.indexOf('Edge-SG.Game')} of ${order.length}`);
    await shot(h.page, 'desktop-order-scrolled');
    await h.close();
  });

  await t.test('phone with many servers: search row, region flags row, no overflow', {timeout: 60000}, async t => {
    const h = await setup(t, {width: 390, height: 844, tweak: many});
    const middles = await h.page.locator('.pb-tools > *').evaluateAll(els => els.map(el => Math.round(el.getBoundingClientRect().top + el.getBoundingClientRect().height / 2)));
    assert.ok(Math.max(...middles) - Math.min(...middles) <= 2, JSON.stringify(middles));
    const tools = await h.page.locator('.pb-tools').boundingBox(), regions = await h.page.locator('.pb-regions').boundingBox();
    assert.ok(tools.y >= regions.y + regions.height - 1, 'tools row sits below the region flags');
    const filters = await h.page.locator('.pb-filters').boundingBox(), view = await h.page.locator('.pb-view').boundingBox();
    assert.ok(view.x + view.width <= filters.x + filters.width + 1, 'view toggle stays inside the row');
    const sizes = await h.page.locator('.pb-tools > input, .pb-tools > select').evaluateAll(els => els.map(el => Math.round(el.getBoundingClientRect().height)));
    assert.ok(Math.abs(sizes[0] - sizes[1]) <= 2, JSON.stringify(sizes));
    assert.ok(await h.page.getByRole('searchbox', {name: '搜索服务器'}).evaluate(el => parseFloat(getComputedStyle(el).fontSize) >= 16));
    await noOverflow(h.page);
    await shot(h.page, 'phone-many-filters');
    await h.close();
  });

  await t.test('region picker: search by name or code, keyboard, back to auto detection', {timeout: 60000}, async t => {
    const h = await setup(t, {tweak: items => { const d = items.find(i => i.id === 'srv-delta'); d.profile.region = ''; d.region = 'JP'; d.node.geo = {country: 'JP'}; }});
    await h.page.locator('.pb-card').filter({hasText: 'Delta-JP'}).click();
    await h.page.locator('.sv-detail:visible').getByRole('button', {name: '编辑'}).first().click();
    const editor = h.page.getByRole('dialog', {name: '编辑「Delta-JP」'});
    const button = editor.getByRole('button', {name: /^地区：/});
    assert.equal(await button.getAttribute('aria-label'), '地区：日本（自动识别）');
    assert.equal(await button.getAttribute('data-auto'), 'true');
    // Both rects in one frame: the dialog may still be animating in.
    const [name, flagBox] = await editor.evaluate(el => [el.querySelector('[aria-label="服务器名称"]'), el.querySelector('.rp-button')].map(n => n.getBoundingClientRect().toJSON()));
    assert.ok(Math.abs(name.y - flagBox.y) <= 1 && flagBox.x > name.x + name.width - 1, 'flag sits right of the name');
    await button.click();
    const search = h.page.getByRole('combobox', {name: '搜索地区'});
    await expectFocused(search);
    const options = h.page.getByRole('listbox', {name: '地区'}).getByRole('option');
    await options.first().waitFor();
    assert.match(await options.first().innerText(), /自动识别[\s\S]*日本/);
    await shot(h.page, 'desktop-region');
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
    await h.close();
  });

  await t.test('reorder: installed and uninstalled sections, arrows, drag and save', {timeout: 90000}, async t => {
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
    // Drag Bravo-FRA (last) by its handle onto the first row.
    await installed.locator('.so-item').last().locator('.so-grip').dragTo(installed.locator('.so-item').first(), {targetPosition: {x: 40, y: 4}});
    await h.page.waitForFunction(() => !document.querySelector('.so-ghost'));
    await settled(h.page);
    assert.equal((await order())[0], 'Bravo-FRA');
    await shot(h.page, 'desktop-order');
    await dialog.getByRole('button', {name: '保存'}).click();
    await dialog.waitFor({state: 'hidden'});
    const saved = h.calls.find(c => c.path === '/api/servers/order').body.ids;
    assert.deepEqual(saved.slice(0, 3), ['srv-bravo', 'srv-papa', 'local']);
    assert.equal(saved.at(-1), 'srv-hotel');
    assert.equal((await names(h.page))[0], 'Bravo-FRA');
    assert.equal((await names(h.page)).at(-1), 'Hotel-HK');
    await h.close();
  });

  await t.test('phone: reorder sheet from the header menu', {timeout: 60000}, async t => {
    const h = await setup(t, {width: 390, height: 844});
    await h.page.getByRole('button', {name: '服务器更多操作'}).click();
    await h.page.getByRole('button', {name: '调整顺序'}).click();
    const dialog = h.page.getByRole('dialog', {name: '调整顺序'});
    await dialog.locator('.so-item').first().waitFor();
    for (const button of await dialog.locator('.so-actions > button').all()) {
      const size = await button.evaluate(el => [parseFloat(getComputedStyle(el).width), parseFloat(getComputedStyle(el).height)]);
      assert.ok(size[0] >= 36 && size[1] >= 36, String(size));
    }
    await noOverflow(h.page);
    await shot(h.page, 'phone-order');
    await dialog.getByRole('button', {name: '上移 Papa-HK.T1'}).click();
    await dialog.getByRole('button', {name: '保存'}).click();
    await dialog.waitFor({state: 'hidden'});
    await h.close();
  });

});
