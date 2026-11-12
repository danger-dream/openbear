// BGP 上游 in Chromium: on the detail page a graph that fits the card whole (scaled when it must, centred when it
// can), an AS under the pointer lighting its paths and said under the graph, IPv4/IPv6; from a card's network
// line, one popover for the board (again on the same line closes it, another line moves it, Escape closes it,
// the detail page stays shut); on a phone a tree in the card and in a bottom sheet. Light and dark.
import test from 'node:test';
import assert from 'node:assert/strict';
import {chromium, noOverflow, settled, shot, showTab, startServersBrowser} from './testing/serversBrowser.mjs';

function tweak(items) {
  for (const [id, asn, org] of [['srv-delta', 400618, 'Prime Security Corp.'], ['srv-papa', 16276, 'OVH SAS']]) {
    const node = items.find(i => i.id === id).node;
    node.network = {ips: [node.info.ipv4, ''], v4: {country: 'JP', asn, org}, v6: null};
  }
}
// The graph inside its box: no scrolling, nothing past the edge.
const fits = graph => graph.evaluate(el => {
  const box = el.getBoundingClientRect(), canvas = el.querySelector('.bg-canvas').getBoundingClientRect();
  return el.scrollWidth <= el.clientWidth + 1 && el.scrollHeight <= el.clientHeight + 1 && canvas.left >= box.left - 1 && canvas.right <= box.right + 1;
});

test('BGP 上游', {skip: !chromium, timeout: 300000}, async t => {
  const {setup} = await startServersBrowser(t);
  await t.test('detail: fits whole, lights an AS\'s paths, IPv6', {timeout: 90000}, async t => {
    const h = await setup(t, {tweak, width: 1100, height: 800});
    await h.page.locator('.pb-card').filter({hasText: 'Delta-JP'}).click();
    await showTab(h.page.locator('.sv-detail:visible'), '网络');
    const section = h.page.locator('.sv-detail:visible [data-jump=bgp]');
    await section.scrollIntoViewIfNeeded();
    await section.locator('.bg-node').first().waitFor();
    assert.match(await section.locator('.ix-card-head').innerText(), /BGP 上游\s*198\.51\.100\.0\/24 · AS400618 Prime Security/);
    // Three steps from its Tier 1s: wider than the card, scaled to fit it whole.
    const graph = section.locator('.bg-graph');
    assert.match(await section.locator('.bg-canvas').getAttribute('style'), /scale\(0\.\d+\)/);
    assert.ok(await fits(graph));
    await settled(h.page);
    await shot(h.page, 'bgp-detail-1100');
    // An AS under the pointer: its paths lit, the rest faded, and the line under the graph says where it comes from.
    await section.locator('.bg-node[data-asn="17676"]').hover();
    assert.equal(await section.locator('.bg-node.is-lit').count(), 5, 'the origin, JINX, CTCSCI, SoftBank and PCCW');
    assert.ok(await section.locator('.bg-lines path.is-dim').count() > 0);
    assert.match(await section.locator('.bg-foot').innerText(), /AS17676 SoftBank\s*上游\s*来自 AS9886 CTCSCI TECH · 1 个观测点\s*bgp\.tools ↗/);
    assert.equal(await section.locator('.bg-node[data-asn="17676"] a').getAttribute('href'), 'https://bgp.tools/as/17676');
    await h.page.mouse.move(5, 5);
    assert.equal(await section.locator('.bg-node.is-lit').count(), 0);
    assert.match(await section.locator('.bg-foot').innerText(), /RIPE RIS · \d 小时前取得/);
    await noOverflow(h.page);
    await h.close();
    // One upstream on a wide screen: full size, centred; IPv6 a tab away.
    const w = await setup(t, {tweak, dark: true});
    await w.page.locator('.pb-card').filter({hasText: 'Papa-HK.T1'}).click();
    await showTab(w.page.locator('.sv-detail:visible'), '网络');
    const papa = w.page.locator('.sv-detail:visible [data-jump=bgp]');
    await papa.scrollIntoViewIfNeeded();
    await papa.locator('.bg-node').first().waitFor();
    assert.equal(await papa.locator('.bg-canvas').evaluate(el => el.style.transform), '');
    const [outer, inner] = await papa.locator('.bg-graph').evaluate(el => [el.getBoundingClientRect(), el.firstElementChild.getBoundingClientRect()].map(r => [r.left, r.right]));
    assert.ok(Math.abs((inner[0] - outer[0]) - (outer[1] - inner[1])) <= 2, 'centred');
    await papa.getByRole('tab', {name: 'IPv6'}).click();
    await papa.locator('.bg-node[data-asn="15169"]').waitFor();
    assert.match(await papa.locator('.ix-card-head').innerText(), /8\.8\.8\.0\/24 · AS15169 Google/);
    await settled(w.page);
    await shot(w.page, 'bgp-detail-1440-dark');
    await w.close();
  });

  await t.test('from a card: one popover, closed by the same line again, moved by another, closed by Escape or a click elsewhere, and opened again', {timeout: 90000}, async t => {
    const h = await setup(t, {tweak});
    const line = name => h.page.locator('.pb-card').filter({hasText: name}).locator('[data-network]');
    assert.equal(await line('Delta-JP').locator('svg').count(), 1, 'an icon before the network');
    await line('Delta-JP').click();
    const pop = h.page.locator('.sv-bgp-popper');
    await pop.locator('.bg-node').first().waitFor();
    assert.equal(await h.page.locator('.sv-detail:visible').count(), 0, 'the card itself does not open');
    assert.ok(await fits(pop.locator('.bg-graph')));
    // Its arrow sticks out of it; the popover itself does not scroll and sits in the window.
    const room = await pop.evaluate(el => ({overflow: getComputedStyle(el).overflowY, top: el.getBoundingClientRect().top, bottom: el.getBoundingClientRect().bottom, vh: innerHeight}));
    assert.ok(room.overflow === 'visible' && room.top >= 0 && room.bottom <= room.vh, `whole, nothing to scroll: ${JSON.stringify(room)}`);
    await settled(h.page);
    await shot(h.page, 'bgp-card-popover');
    await line('Delta-JP').click();
    await pop.waitFor({state: 'hidden'});
    await line('Delta-JP').click();
    await pop.locator('.bg-node').first().waitFor();
    await line('Papa-HK.T1').click();
    await pop.locator('.bg-node[data-asn="16276"]').waitFor();
    assert.match(await pop.locator('.bg-head-note').innerText(), /203\.0\.113\.0\/24/);
    await h.page.keyboard.press('Escape');
    await pop.waitFor({state: 'hidden'});
    // Opened again on a line it was open on before, after each way of closing it: it stays open.
    const stays = async () => { await h.page.waitForTimeout(500); assert.ok(await pop.isVisible(), 'still open'); };
    await line('Papa-HK.T1').click();
    await stays();
    await pop.locator('.bg-node[data-asn="4637"]').click();
    await stays();
    assert.match(await pop.locator('.bg-foot').innerText(), /AS4637 Telstra/, 'a click inside picks an AS, it does not close');
    await h.page.mouse.click(700, 160);
    await pop.waitFor({state: 'hidden'});
    await line('Papa-HK.T1').click();
    await stays();
    await line('Papa-HK.T1').click();
    await pop.waitFor({state: 'hidden'});
    await line('Papa-HK.T1').click();
    await stays();
    // The × in its corner.
    await pop.getByRole('button', {name: '关闭'}).click();
    await pop.waitFor({state: 'hidden'});
    await line('Papa-HK.T1').click();
    await stays();
    await h.close();
  });

  await t.test('phone, dark: a tree in the detail card and in a bottom sheet', {timeout: 90000}, async t => {
    const h = await setup(t, {tweak, width: 390, height: 844, touch: true, dark: true});
    await h.page.locator('.pb-card').filter({hasText: 'Papa-HK.T1'}).locator('[data-network]').click();
    const sheet = h.page.getByRole('dialog', {name: 'BGP 上游 · Papa-HK.T1'});
    await sheet.locator('.bg-tree-node').first().waitFor();
    assert.deepEqual(await sheet.locator('.bg-tree-node b').allInnerTexts(), ['AS16276', 'AS4637']);
    assert.deepEqual(await sheet.locator('.bg-tree-t1').first().locator('button').allInnerTexts(), ['Cogent', 'NTT', 'DTAG', 'TATA', 'Telxius']);
    assert.match(await sheet.locator('.bg-tree-t1.is-behind').first().innerText(), /经 TATA\s*AT&T/);
    assert.ok(await sheet.evaluate(el => el.scrollWidth <= el.clientWidth + 1), 'no sideways scrolling');
    await sheet.getByRole('button', {name: 'Telstra'}).click();
    assert.match(await sheet.locator('.bg-foot').innerText(), /AS4637 Telstra\s*上游\s*来自 AS16276 OVH · 44 个观测点/);
    await settled(h.page);
    await shot(h.page, 'bgp-phone-sheet');
    await sheet.getByRole('button', {name: '关闭'}).click();
    await sheet.waitFor({state: 'hidden'});
    await h.page.locator('.pb-card').filter({hasText: 'Delta-JP'}).click();
    await showTab(h.page.locator('.sv-detail:visible'), '网络');
    const section = h.page.locator('.sv-detail:visible [data-jump=bgp]');
    await section.scrollIntoViewIfNeeded();
    await section.locator('.bg-tree-node').first().waitFor();
    assert.deepEqual(await section.locator('.bg-tree-node b').allInnerTexts(), ['AS400618', 'AS140096', 'AS9886', 'AS17676']);
    await settled(h.page);
    await shot(h.page, 'bgp-phone-detail');
    await noOverflow(h.page);
    await h.close();
  });
});
