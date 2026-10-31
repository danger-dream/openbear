// Mesh board clarity without a browser: which lines are quiet (in use or not, with a problem or
// not), which carry labels, what pointing at or selecting a node brings forward and what steps back
// — in happy-dom with the browser test's mesh (testing/meshClarityFixture.mjs). Line widths and
// opacity, the arc round Rex, clicks on it and the dark theme stay in mesh.clarity.browser.test.mjs.
import test from 'node:test';
import assert from 'node:assert/strict';
import {mountPage} from './testing/pageDom.mjs';
import {server, NODES, PAIRS, clarityMesh} from './testing/meshClarityFixture.mjs';

async function open(t, {width = 1440, height = 1000, dark = false, broken = false} = {}) {
  const view = clarityMesh({broken});
  const items = NODES.map(([id, name, region]) => server(id, name, region, id === 'mac' ? 'darwin' : id === 'win' ? 'windows' : 'linux'));
  const h = await mountPage(t, {width, height, dark, load: n => import(`../views/ServersView.vue?fresh=${n}`),
    session: {'openbear.servers.tabs': JSON.stringify({tabs: [], active: 'mesh'})},
    // The panel folded away on the desktop: the board as wide as it gets.
    local: {'openbear.mesh.folded': JSON.stringify({palette: true, panel: true})},
    handle: async (method, path) => {
      let status = 200, data = {ok: true};
      if (path === '/api/servers/mesh' && method === 'GET') data = view;
      else if (path === '/api/servers/mesh/layout') data = {ok: true, layout: view.layout};
      else if (/^\/api\/servers\/mesh\/members\/[\w-]+$/.test(path)) data = {ok: true, status: {peers: []}, events: []};
      else if (path.startsWith('/api/servers/mesh')) [status, data] = [404, {ok: false, error: 'not_found'}];
      else if (path === '/api/servers' && method === 'GET') data = {ok: true, items, settings: {accessPoints: [], defaultAccess: '', timezone: 'Asia/Shanghai'}, node: {available: true, version: '0.13.1'}};
      else if (path === '/api/servers/live') data = {ok: true, at: Date.now(), items: Object.fromEntries(items.map(i => [i.id, i.live]))};
      else if (path.endsWith('/metrics')) data = {ok: true, step: 3, points: {t: [], cpu: [], steal: [], mem: [], rxRate: [], txRate: [], ioRead: [], ioWrite: [], load: [], tcp: [], udp: []}};
      else [status, data] = [404, {ok: false, error: 'not_found'}];
      return [status, data];
    }});
  await h.page.locator('.vue-flow__node').nth(NODES.length - 1).waitFor({state: 'attached'});
  return {page: h.page, close: () => h.close()};
}
const node = (page, id) => page.locator(`.vue-flow__node[data-id="${id}"]`);
const edge = (page, id) => page.locator(`[data-id="${id}"] .mesh-edge`);
const labelOf = (page, id) => page.locator(`.mesh-edge-label[data-edge="${id}"]`);
const quietOnes = page => page.locator('.mesh-edge[data-quiet]').evaluateAll(list => list.map(el => el.closest('[data-id]').dataset.id).sort());
const dimmedNodes = page => page.locator('.mesh-node').evaluateAll(list => list.filter(el => el.hasAttribute('data-dim')).map(el => el.closest('.vue-flow__node').dataset.id).sort());
// The relay links by their ends (pair:<index> in the order above).
const pair = (a, b) => `pair:${PAIRS.findIndex(p => [p[0], p[1]].sort().join('|') === [a, b].sort().join('|'))}`;
const QUIET = [pair('rex', 'sg'), pair('toy', 'rex'), pair('toy', 'berlin'), 'entry:sea:c2', 'entry:lax02:c2', 'entry:mac:c2', 'entry:mac:c3', 'entry:win:c2', 'entry:win:c3'].sort();

test('desktop: quiet lines, a line pointed at, a node pointed at brings its lines forward', async t => {
  const {page, close} = await open(t);
  // Not in use: the three relay links nothing is routed over and every standby entry. Quiet and without labels.
  assert.deepEqual(await quietOnes(page), QUIET);
  for (const id of QUIET) assert.equal(await labelOf(page, id).count(), 0, `${id} has no label`);
  // In use: labelled as before.
  for (const id of ['entry:ks:c1', 'entry:mac:c1', pair('home', 'sg'), pair('vline', 'sg'), pair('home', 'toy'), 'direct:0']) {
    assert.equal(await edge(page, id).getAttribute('data-quiet'), null, `${id} in use`);
    assert.equal(await labelOf(page, id).count(), 1, `${id} labelled`);
  }
  await page.locator('.mesh-legend-note', {hasText: '细淡线：备用或暂时没走流量的线路'}).waitFor();

  // Pointing at a quiet line shows it as usual, label included (where the pointer lands on it: the browser test).
  await edge(page, pair('toy', 'berlin')).hover();
  await labelOf(page, pair('toy', 'berlin')).waitFor();
  assert.equal(await edge(page, pair('toy', 'berlin')).getAttribute('data-quiet'), null);
  await page.mouse.move(5, 5);
  await labelOf(page, pair('toy', 'berlin')).waitFor({state: 'detached'});

  // Pointing at Rex: its links (quiet ones too) come forward labelled, the rest steps back.
  const mine = [pair('home', 'rex'), pair('toy', 'rex'), pair('rex', 'sg'), 'entry:sea:c2', 'entry:lax02:c2'];
  await node(page, 'rex').hover();
  await page.waitForFunction(() => document.querySelectorAll('.mesh-node[data-dim]').length > 0);
  for (const id of mine) {
    assert.equal(await edge(page, id).getAttribute('data-quiet'), null, `${id} comes forward`);
    assert.equal(await edge(page, id).getAttribute('data-dim'), null, `${id} not dimmed`);
    assert.equal(await labelOf(page, id).count(), 1, `${id} labelled`);
  }
  assert.equal(await edge(page, pair('home', 'sg')).getAttribute('data-dim'), 'true', 'other links step back');
  assert.deepEqual(await dimmedNodes(page), ['berlin', 'edgees', 'ks', 'mac', 'vline', 'win']);
  await page.mouse.move(5, 5);
  await page.waitForFunction(() => !document.querySelector('.mesh-node[data-dim]'));
  assert.deepEqual(await quietOnes(page), QUIET);

  // Selecting Mac keeps its lines forward after the pointer leaves; clearing the selection puts them back.
  await node(page, 'mac').click();
  await page.mouse.move(5, 5);
  await page.waitForTimeout(250);
  for (const id of ['entry:mac:c2', 'entry:mac:c3']) assert.equal(await labelOf(page, id).count(), 1, `${id} labelled while Mac is selected`);
  assert.deepEqual(await dimmedNodes(page), ['berlin', 'edgees', 'home', 'ks', 'lax02', 'rex', 'sea']);
  await page.keyboard.press('Escape');
  await page.waitForFunction(() => !document.querySelector('.mesh-node[data-dim]'));
  assert.deepEqual(await quietOnes(page), QUIET);
  await close();
});

test('desktop: a line with a problem is never quiet', async t => {
  const {page, close} = await open(t, {broken: true});
  // TOY–Berlin carries nothing and is down; Mac's second entry is a standby that fails. Both stay up, labelled.
  for (const [id, text] of [[pair('toy', 'berlin'), '断开'], ['entry:mac:c2', '2 · TLS · 超时']]) {
    assert.equal(await edge(page, id).getAttribute('data-quiet'), null, `${id} not quiet`);
    assert.equal((await labelOf(page, id).textContent()).trim(), text);
  }
  assert.deepEqual(await quietOnes(page), QUIET.filter(id => id !== pair('toy', 'berlin') && id !== 'entry:mac:c2'));
  await close();
});

test('desktop dark: pointing at a node dims the rest', async t => {
  const {page, close} = await open(t, {dark: true});
  await node(page, 'sg').hover();
  await page.waitForFunction(() => document.querySelectorAll('.mesh-node[data-dim]').length > 0);
  await close();
});

test('phone: quiet lines too; a tap on a node brings its lines forward', async t => {
  for (const dark of [false, true]) {
    const {page, close} = await open(t, {width: 390, height: 844, dark});
    assert.deepEqual(await quietOnes(page), QUIET);
    for (const id of QUIET) assert.equal(await labelOf(page, id).count(), 0, `${id} has no label`);
    // Pointing does nothing on a phone; a tap selects (and opens the sheet), its lines forward.
    await node(page, 'rex').tap();
    const sheet = page.locator('.mesh-inspect-sheet');
    await sheet.waitFor();
    await sheet.getByRole('button', {name: '关闭'}).tap();
    await sheet.waitFor({state: 'hidden'});
    assert.equal(await edge(page, pair('rex', 'sg')).getAttribute('data-quiet'), null);
    assert.equal(await edge(page, pair('home', 'sg')).getAttribute('data-dim'), 'true');
    await page.locator('.mesh-dock').getByRole('button', {name: '取消选择'}).tap();
    await page.waitForFunction(() => !document.querySelector('.mesh-node[data-dim]'));
    await close();
  }
});
