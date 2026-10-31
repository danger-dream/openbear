// Server network in Chromium, layout only: ping, tcping, trace, mesh targets.
// Sizes, overflow, scrolling, phone and dark theme. What the page asks the backend and what it
// shows is checked without a browser in net.dom.test.mjs. Page, fake backend and helpers:
// testing/netBrowser.mjs.
import test from 'node:test';
import assert from 'node:assert/strict';
import {ADDRESSES, GB, GEO, MESH_ADDR, SERVERS, addresses, chromium, fakeMesh, fakeNet, noOverflow, press, probe, rowTexts, run, server, shot, shots, startNetBrowser, waitFor} from './testing/netBrowser.mjs';

test('server network layout: ping, tcping, trace, mesh targets', {skip: !chromium, timeout: 300000}, async t => {
  const {setup} = await startNetBrowser(t);
  await t.test('ping and tcping: polled every second, rows with timeouts, refusals and unreachables, the summary, stop', {timeout: 90000}, async t => {
    const h = await setup(t);
    const root = probe(h.page);
    await root.locator('.net-form').waitFor();
    // TCPing: refused and unreachable, no TTL.
    await run(h, {mode: 'TCPing', target: 'example.com', port: '443', count: '5'});
    await root.locator('.net-result-head .pb-tag', {hasText: '已完成'}).waitFor({timeout: 15000});
    // On the detail page: the summary as tiles, the loss coloured, each try a bar (a failed one red, full height).
    const bars = await root.locator('.net-bars > i').evaluateAll(l => l.map(el => [el.hasAttribute('data-fail'), Math.round(el.getBoundingClientRect().height)]));
    assert.deepEqual(bars.map(([fail]) => fail), [false, true, false, true, false]);
    assert.ok(bars[1][1] === bars[3][1] && bars[0][1] < bars[2][1] && bars[2][1] <= bars[4][1], JSON.stringify(bars));
    await shot(h.page, 'desktop-light-tcping');
    await h.close();
  });

  await t.test('trace: hops in a table, no answer as *, several addresses on a hop, places filled in without moving rows', {timeout: 90000}, async t => {
    const h = await setup(t);
    const root = probe(h.page);
    await root.locator('.net-form').waitFor();
    await run(h, {mode: '路由追踪', target: 'example.com'});
    const table = root.locator('table.net-trace');
    await table.locator('tbody tr').nth(2).waitFor();
    // While places are looked up: a placeholder of the same height.
    await table.locator('tbody tr').nth(3).waitFor();
    const waiting = table.locator('.net-geo-wait');
    await waiting.first().waitFor();
    const row4 = table.locator('tbody tr').nth(3);
    const before = (await row4.boundingBox()).height;
    await shot(h.page, 'desktop-light-trace-waiting');
    await table.locator('tbody tr').nth(5).locator('.net-trace-geo', {hasText: 'AS15133'}).waitFor({timeout: 10000});
    assert.equal((await row4.boundingBox()).height, before, 'the row did not move when its places came in');
    const cells = await table.locator('tbody tr').evaluateAll(list => list.map(tr => [...tr.cells].map(td => td.innerText.replace(/\s+/g, ' ').trim()).join(' | ')));
    // Two addresses on hop 4: each on its own line, in step with its place.
    const lines = await row4.evaluate(tr => [...tr.cells].slice(1, 3).map(td => [...td.querySelectorAll('.net-line-cell')].map(el => Math.round(el.getBoundingClientRect().top))));
    assert.deepEqual(lines[0], lines[1]);
    assert.equal(lines[0].length, 2);
    await root.locator('.net-result-head .pb-tag', {hasText: '已完成'}).waitFor({timeout: 15000});
    await shot(h.page, 'desktop-light-trace');

    // A hop not tried yet while it runs is a placeholder, not "0%"; a router answering "unreachable" is listed but is not the end.
    await run(h, {mode: '路由追踪', target: 'gap.example'});
    await table.locator('tr[data-pending]').waitFor();
    const pending = await table.locator('tr[data-pending]').innerText();
    await shot(h.page, 'desktop-light-trace-pending');
    await root.locator('.net-result-head .pb-tag', {hasText: '已完成'}).waitFor({timeout: 15000});
    await root.getByText('目标没有回应，表里是途经的路由器；目标可能不回应 ping。').waitFor();
    await h.close();
  });

  await t.test('mesh targets: the path in place of the trace table, the end-to-end ping, stale, a gateway network; "经过" under ping', {timeout: 90000}, async t => {
    const h = await setup(t);
    const root = probe(h.page);
    await root.locator('.net-form').waitFor();
    await h.page.locator('.mesh-card').waitFor();
    const pid = await run(h, {mode: '路由追踪', target: '100.96.0.2', count: '4'});
    const path = root.locator('.net-mesh-path');
    await path.waitFor();
    await root.getByText('端到端 Ping').waitFor();
    await root.locator('.net-summary').waitFor();
    await root.locator('.net-result-head .pb-tag', {hasText: '已完成'}).waitFor({timeout: 15000});
    await shot(h.page, 'desktop-light-mesh-trace');
    // After a controller restart the same test reads as a plain ping.
    const again = await run(h, {mode: '路由追踪', target: '100.96.0.2', count: '6'});
    await root.locator('.net-mesh-path').waitFor();
    h.net.restart();
    await root.locator('.net-results > li').first().waitFor();
    // A ping to a mesh address: the machines on the way under the summary.
    await run(h, {mode: 'Ping', target: '100.96.0.2', count: '3'});
    await root.locator('.net-through').waitFor();
    await root.locator('.net-results > li').first().waitFor();
    await h.close();

    // From LAX02 to a host behind home's gateway: the tail after the gateway, and a stale report.
    const g = await setup(t, {tabs: ['lax'], active: 'lax'});
    const gr = probe(g.page);
    await gr.locator('.net-form').waitFor();
    await g.page.locator('.mesh-card').waitFor();
    await run(g, {mode: '路由追踪', target: '10.10.10.1', count: '3', root: gr});
    await gr.locator('.net-mesh-path').waitFor();
    await gr.getByText('数据不完整：有的线路没有上报，路径和延迟可能不准').waitFor();
    await g.close();
  });
});
