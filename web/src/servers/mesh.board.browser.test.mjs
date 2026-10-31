// The mesh board in Chromium, layout only: joining, links, node properties.
// Sizes, overflow, scrolling, phone and dark theme, and what needs real pointer input (dragging
// nodes, ports and sheets). What the board asks the controller and what it shows is checked without
// a browser in mesh.board / mesh.canvas / mesh.links.dom.test.mjs. Page, fake controller and helpers:
// testing/meshBrowser.mjs.
import test from 'node:test';
import assert from 'node:assert/strict';
import {GB, SERVERS, centre, chromium, clearOfBoard, dimmed, dragBody, dragPort, fakeMesh, guideCount, inspector, label, member, memberOf, noOverflow, node, server, shot, shots, startMeshBrowser, texts, title, touchDrag, until, frames, settled} from './testing/meshBrowser.mjs';

test('mesh board layout: joining, links, node properties', {skip: !chromium, timeout: 300000}, async t => {
  const {setup} = await startMeshBrowser(t);
  await t.test('desktop board: servers on the left, links on the board, join by dragging a server in', {timeout: 90000}, async t => {
    const h = await setup(t);
    const {page} = h;
    // Relays above, the member below, as saved.
    assert.ok((await node(page, 'berlin').boundingBox()).y < (await node(page, 'home').boundingBox()).y);
    // Entry candidates: order and transport; the one in use is highlighted, the other a quiet standby
    // without its label until Home is pointed at.
    await node(page, 'home').hover();
    await page.mouse.move(5, 5);
    await label(page, '1 · UDP').waitFor({state: 'detached'});
    // Left: who can join and why not.
    const palette = page.locator('.mesh-palette');
    await palette.getByRole('searchbox', {name: '搜索服务器'}).fill('mac');
    await palette.getByRole('searchbox', {name: '搜索服务器'}).fill('');
    // Right, nothing selected: changes, rules and settings.
    await inspector(page).getByText('没有未应用的更改，已应用第 12 版。').waitFor();
    await noOverflow(page);

    // Drag 公司-Mac onto the board: it installs where it was dropped.
    const canvas = await page.locator('.mesh-canvas').boundingBox();
    const drop = {x: Math.round(canvas.width * 0.78), y: Math.round(canvas.height * 0.72)};
    await palette.getByRole('button', {name: '公司-Mac，拖到画板上加入组网'}).dragTo(page.locator('.mesh-canvas'), {targetPosition: drop});
    const mac = node(page, 'mac');
    await mac.locator('.mesh-node[data-installing]').waitFor();
    const at = await centre(mac);
    const box = await mac.boundingBox();
    assert.ok(Math.abs(at.x - (canvas.x + drop.x)) < 4 && Math.abs(at.y - (canvas.y + drop.y)) < 12, `dropped at ${JSON.stringify(at)}`);
    await shot(page, 'desktop-installing');
    await mac.locator('.mesh-node:not([data-installing])').waitFor({timeout: 8000});
    const after = await mac.boundingBox();
    assert.ok(Math.abs(after.x - box.x) < 2 && Math.abs(after.y - box.y) < 2, `stays where it was dropped: ${JSON.stringify([box, after])}`);
    await h.close();
  });

  await t.test('links: drag between nodes makes an entry and saves it, transport and listen fix, delete, reasons for invalid links', {timeout: 90000}, async t => {
    const h = await setup(t);
    const {page} = h;
    // Home → LAX again: LAX is already its TCP entry, so the new one takes LAX's next open transport.
    await dragPort(page, 'home', 'lax', 'top');
    let body = await h.saved(d => memberOf(d, 'home').entry.candidates.length === 3, 'new entry');
    assert.deepEqual(memberOf(body, 'home').entry.candidates.slice(-1).map(({relay, transport}) => ({relay, transport})), [{relay: 'lax', transport: 'udp'}]);
    assert.equal(await title(page).innerText(), '入口');
    // The result shows at the top of the board, clear of nodes and labels, and the new label flashes.
    const added = page.locator('.mesh-notice', {hasText: '已添加入口'});
    await added.waitFor();
    await settled(page, added);
    await clearOfBoard(page, added);
    assert.equal(await label(page, '3 · UDP').getAttribute('data-flash'), 'true');
    const panel = inspector(page);
    assert.match(await panel.locator('.mesh-link-ends').innerText(), /Home\s*→\s*LAX/);
    assert.match(await panel.locator('.mesh-link-settings').innerText(), /顺序\s*第 3 个，共 3 个/);
    await label(page, '3 · UDP').waitFor();
    // TLS: LAX does not listen on it; the panel says so and turns it on in one go.
    await panel.getByRole('radio', {name: 'TLS'}).click();
    await panel.getByText('LAX 没有开启 TLS 监听', {exact: true}).waitFor();
    assert.equal(await label(page, '3 · TLS').getAttribute('data-look'), 'bad');
    await panel.getByRole('button', {name: '为 LAX 开启 TLS 监听'}).click();
    body = await h.saved(d => memberOf(d, 'lax').listen.tls === 52001 && memberOf(d, 'home').entry.candidates[2].transport === 'tls', 'listen fix');
    await panel.getByRole('button', {name: '提前'}).click();
    body = await h.saved(d => memberOf(d, 'home').entry.candidates[1].transport === 'tls', 'reorder');
    await label(page, '2 · TLS').waitFor();
    await shot(page, 'desktop-entry-selected');
    // Delete removes the selected link.
    await page.keyboard.press('Delete');
    body = await h.saved(d => memberOf(d, 'home').entry.candidates.length === 2, 'delete');
    assert.deepEqual(memberOf(body, 'home').entry.candidates.map(c => c.id), ['c1', 'c2']);
    assert.equal(await title(page).innerText(), '网格');
    // Relay to relay while all relays are linked: explained where it was dropped, nothing saved.
    const before = h.puts().length;
    await dragPort(page, 'berlin', 'lax');
    await page.locator('.mesh-notice', {hasText: '所有中转已经两两相连'}).waitFor();
    await settled(page, page.locator('.mesh-notice'));
    await clearOfBoard(page, page.locator('.mesh-notice'));
    // The direct tool between a member and a relay: explained too.
    await page.locator('.mesh-tool', {hasText: '直连'}).click();
    await page.locator('.mesh-banner', {hasText: '两台普通成员之间不经过中转'}).waitFor();
    await dragBody(page, 'home', 'berlin');
    await page.locator('.mesh-notice', {hasText: '直连只用于两台普通成员，Berlin 是中转'}).waitFor();
    await settled(page, page.locator('.mesh-notice'));
    await clearOfBoard(page, page.locator('.mesh-notice'));
    await page.waitForTimeout(300);
    assert.equal(h.puts().length, before, 'invalid links are not saved');
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('.mesh-tool[aria-checked=true]').innerText(), '选择');
    // The automatic relay pair: selecting shows where it comes from; turning it off keeps the pair.
    await page.locator('.mesh-edge[data-kind=relay] .mesh-edge-hit').dispatchEvent('click');
    assert.equal(await title(page).innerText(), '中转互联');
    await panel.getByRole('checkbox', {name: /所有中转两两相连/}).uncheck();
    body = await h.saved(d => d.relayLinks.mode === 'custom', 'custom relay links');
    assert.deepEqual(body.relayLinks.pairs.map(p => [p.a, p.b, p.transport]), [['berlin', 'lax', 'udp']]);
    await label(page, '拨号 · UDP').waitFor();
    await h.close();
  });

  await t.test('node properties: settings saved to the draft, live status with hints, trend and events, upgrades', {timeout: 90000}, async t => {
    const h = await setup(t, {upgrades: true});
    const {page} = h;
    await node(page, 'home').click();
    const panel = inspector(page);
    await panel.locator('.mesh-sparks > li').nth(3).waitFor();
    const text = await panel.innerText();
    // One line per hint, memory first; the explanation opens on a click.
    const hintRows = panel.locator('.mesh-hints summary');
    for (const row of await hintRows.all()) { const b = await row.boundingBox(); assert.ok(b.height <= 30, `a hint is one line: ${b.height}`); }
    const rows = await panel.locator('.mesh-hints > li').evaluateAll(list => list.map(li => li.getBoundingClientRect().height));
    assert.ok(Math.max(...rows) <= 32, `hints stay compact: ${rows}`);
    await hintRows.nth(1).click();
    await panel.getByText('本机没有加上：10.8.0.0/16。去往这些网段的流量不经过网格。').waitFor();
    await hintRows.nth(1).click();
    await panel.getByRole('tab', {name: '24 小时'}).click();
    await settled(page);
    await shot(page, 'desktop-node-status');
    await panel.getByRole('tab', {name: '事件'}).click();
    await panel.getByText('第 1 个入口（Berlin · UDP）：探测超时').waitFor();
    await panel.getByRole('tab', {name: '对端'}).click();
    await panel.getByText(/重连 2 次/).waitFor();
    await panel.getByRole('tab', {name: '配置'}).click();
    await panel.getByRole('spinbutton', {name: '发送速率'}).fill('90');
    await panel.getByRole('spinbutton', {name: '发送速率'}).press('Tab');
    await h.saved(d => memberOf(d, 'home').uploadLimitMbps === 90, 'upload limit');
    await panel.getByRole('tab', {name: '入口'}).click();
    await panel.getByRole('radio', {name: '固定'}).check();
    await h.saved(d => memberOf(d, 'home').entry.mode === 'fixed', 'entry mode');
    // In fixed mode only the first entry is used; the second stays a standby line.
    await page.keyboard.press('Escape');
    await h.close();
  });
});
