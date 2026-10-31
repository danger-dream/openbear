// The mesh board in Chromium, layout only: site relays.
// Sizes, overflow, scrolling, phone and dark theme, and what needs real pointer input (dragging
// nodes, ports and sheets). What the board asks the controller and what it shows is checked without
// a browser in mesh.board / mesh.canvas / mesh.links.dom.test.mjs. Page, fake controller and helpers:
// testing/meshBrowser.mjs.
import test from 'node:test';
import assert from 'node:assert/strict';
import {GB, SERVERS, centre, chromium, clearOfBoard, dimmed, dragBody, dragPort, fakeMesh, guideCount, inspector, label, member, memberOf, noOverflow, node, server, shot, shots, startMeshBrowser, texts, title, touchDrag, until, frames, settled} from './testing/meshBrowser.mjs';

test('mesh board layout: site relays', {skip: !chromium, timeout: 300000}, async t => {
  const {setup} = await startMeshBrowser(t);
  await t.test('site relays and relay links: relay mode and its recommendation, punched links between site relays, metrics, preference, the new checks', {timeout: 150000}, async t => {
    const h = await setup(t, {reach: true});
    const {page} = h;
    const panel = inspector(page);
    // Home↔TOY punch (the worse end's measure), the others dial; LAX flaps and is held back.
    await label(page, '打洞 · 2.6 ms').waitFor();
    await label(page, '拨号 · 79 ms').waitFor();
    await label(page, '拨号 · 75 ms').waitFor();
    const held = label(page, '线路不稳，暂停使用');
    const dash = await page.locator('.mesh-edge[data-kind=relay][data-look=suppressed] .mesh-edge-line').evaluate(el => getComputedStyle(el).strokeDasharray);
    assert.notEqual(dash, 'none');

    // A dialled link: how it connects, what both ends measure, the preference.
    await held.click();
    await label(page, '线路不稳，暂停使用 · 最近反复断开，稳定后自动恢复').waitFor();
    const metric = await panel.locator('.mesh-relay-metric').innerText();
    await panel.getByText('由 Home 主动拨号：站点中转不对外宣告地址。').waitFor();
    const bias = panel.getByRole('spinbutton', {name: '偏好（毫秒）'});
    await bias.fill('-20');
    await bias.press('Tab');
    let body = await h.saved(d => d.relayLinks.pairs[1].bias === -20, 'preference');
    await until(page, async () => /偏好 -20 ms/.test(await page.locator('.mesh-edge-label[data-kind=relay][data-selected]').getAttribute('title')), 'preference in the title');
    // What is typed survives a poll; past the range it is held at 10000.
    const polls = () => h.calls.filter(c => c.method === 'GET' && c.path === '/api/servers/mesh').length;
    const seen = polls();
    await bias.fill('20000');
    await until(page, async () => polls() > seen, 'a poll while typing', 7000);
    await bias.press('Tab');
    await h.saved(d => d.relayLinks.pairs[1].bias === 10000, 'preference held at 10000');
    await until(page, async () => await bias.inputValue() === '10000', 'the box shows what was kept');
    const before = h.puts().length;
    await bias.fill('0');
    await bias.press('Tab');
    body = await h.saved(d => h.puts().length > before && !('bias' in d.relayLinks.pairs[1]), 'preference back to 0');

    // A punched link between two site relays: no transport or dialer to choose, the punch state and the measures.
    await label(page, '打洞 · 2.6 ms').click();
    await panel.getByText('自动打洞：两台站点中转都在 NAT 后也能直接相连，打不通时经其他中转绕行').waitFor();
    await page.waitForFunction(() => /持续/.test(document.querySelector('.mesh-direct-live .mesh-stats')?.innerText || ''));
    const live = await panel.locator('.mesh-direct-live .mesh-stats').innerText();
    for (const want of [/打洞\s*直连中/, /经由\s*公网/, /持续\s*42 分钟/, /尝试次数\s*2 次/, /最近错误\s*无/]) assert.match(live, want);
    await panel.getByRole('spinbutton', {name: '偏好（毫秒）'}).waitFor();
    await shot(page, 'desktop-relay-link');
    // Both ends symmetric, but the line is already through: no prediction that it will not be.
    for (const id of ['home', 'toy']) h.mesh.nodes[id].summary.reachability = {...h.mesh.nodes[id].summary.reachability, verdict: 'symmetric'};
    // Two fresh reads of GET /mesh: the board has seen the new verdicts.
    for (let i = 0; i < 2; i++) await page.waitForResponse(r => new URL(r.url()).pathname.endsWith('/servers/mesh'), {timeout: 9000});
    await frames(page);
    for (const id of ['home', 'toy']) h.mesh.nodes[id].summary.reachability = {...h.mesh.nodes[id].summary.reachability, verdict: 'punchable'};
    await page.keyboard.press('Escape');

    // Relay mode: TOY is a site relay, as the check recommends; public makes the link to Home a dialled one.
    await node(page, 'toy').click();
    await panel.getByRole('tab', {name: '配置'}).click();
    const modes = panel.getByRole('radiogroup', {name: '中转类型'});
    await modes.getByRole('radio', {name: /^公网中转/}).check();
    await h.saved(d => memberOf(d, 'toy').relayMode === 'public', 'public relay');
    await panel.getByText('可达性检测结果是“可打洞”，建议设为站点中转。').waitFor();
    await label(page, '拨号 · 2.6 ms').waitFor();
    await modes.getByRole('radio', {name: /^站点中转/}).check();
    await h.saved(d => memberOf(d, 'toy').relayMode === 'site', 'site relay again');
    await label(page, '打洞 · 2.6 ms').waitFor();

    // Lab is behind NAT: made a relay it starts as a site relay with UDP, and needs a public relay to link to.
    await node(page, 'lab').click();
    await panel.getByRole('tab', {name: '配置'}).click();
    await panel.getByRole('checkbox', {name: /作为中转/}).check();
    body = await h.saved(d => memberOf(d, 'lab').role === 'relay', 'lab relay');
    await panel.getByText('站点中转至少要连一台公网中转，用来交换打洞地址').waitFor();
    await node(page, 'lab').locator('.pb-tag', {hasText: '需修改'}).waitFor();
    // Two site relays may be linked: by punching.
    await dragPort(page, 'lab', 'toy', 'left');
    body = await h.saved(d => d.relayLinks.pairs.length === 5, 'site-to-site link');
    assert.deepEqual(body.relayLinks.pairs[4], {a: 'lab', b: 'toy', transport: 'udp', cost: 1, dialer: 'a'});
    await label(page, '打洞').waitFor();
    assert.match(await panel.locator('.mesh-relay-facts').innerText(), /打洞：两台都是站点中转/);
    // And to a public relay, which Lab dials; the check passes then.
    await dragPort(page, 'lab', 'sg', 'top');
    body = await h.saved(d => d.relayLinks.pairs.length === 6, 'site-to-public link');
    assert.deepEqual(body.relayLinks.pairs[5], {a: 'lab', b: 'sg', transport: 'udp', cost: 1, dialer: 'a'});
    assert.match(await panel.locator('.mesh-relay-facts').innerText(), /拨号：Lab 主动连接 SG/);
    await node(page, 'lab').locator('.pb-tag', {hasText: '需修改'}).waitFor({state: 'detached'});

    // An entry through a site relay needs a public one too: Win without its SG entry.
    await node(page, 'win').click();
    await panel.getByRole('tab', {name: '入口'}).click();
    await panel.getByRole('button', {name: '删除第 2 个入口'}).click();
    await h.saved(d => memberOf(d, 'win').entry.candidates.length === 1, 'win without SG');
    await panel.getByText('入口里有站点中转时，还要有一个公网中转入口：先经它连上，再和站点中转打洞').waitFor();
    await h.close();
  });
});
