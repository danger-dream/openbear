// The board's latency without a browser: the cards' 延迟 and 丢包 pairs with their half-hour strips, the
// table's columns, read once a minute apart from the live samples, and nothing of it while monitoring is off.
import test from 'node:test';
import assert from 'node:assert/strict';
import {setup} from './testing/serversDom.mjs';
import {stripBackground, stripCells, stripPaths} from './detail/detailHelpers.js';

const card = (h, name) => h.page.locator('.pb-card').filter({hasText: name});

test('cards: latency and loss for the servers measured, a strip of thirty minutes under each', async t => {
  const h = await setup(t);
  const papa = card(h, 'Papa-HK.T1');
  await papa.locator('[data-cell=latency]').waitFor();
  assert.equal(h.calls.filter(c => c.path === '/api/servers/latency').length, 1);
  assert.ok(!h.calls.some(c => c.path === '/api/servers/live' && c.query.latency), 'the live samples carry no latency');
  // Named by the group it is worked out over.
  assert.match(await papa.locator('[data-cell=latency]').innerText(), /延迟\s*国外\s*3\d\.\d ms/);
  assert.equal(await papa.locator('[data-cell=latency]').getAttribute('title'), '国外地址最近 10 分钟的平均延迟。下面 30 格是最近 30 分钟，每格 1 分钟。');
  assert.equal(await card(h, 'Delta-JP').locator('[data-cell=loss]').getAttribute('title'), '三网最近 10 分钟的丢包率。下面 30 格是最近 30 分钟，每格 1 分钟。');
  assert.match(await card(h, 'Delta-JP').locator('[data-cell=loss]').innerText(), /丢包\s*三网/);
  assert.equal(await papa.locator('[data-cell=latency]').getAttribute('data-tone'), 'ok');
  assert.match(await papa.locator('[data-cell=loss]').innerText(), /丢包\s*国外\s*0\.7%/);
  // One element per strip, its minutes in its background.
  const strip = papa.locator('[data-cell=loss] .pb-strip');
  assert.equal(await strip.evaluate(el => el.children.length), 0);
  // An offline server and one without a node have no such pair.
  assert.equal(await card(h, 'GoldenCloud').locator('[data-cell=latency]').count(), 0);
  assert.equal(await card(h, 'Hotel-HK').locator('[data-cell=latency]').count(), 0);
  await h.close();
});

test('table: 延迟 and 丢包 columns while anything is measured', async t => {
  const h = await setup(t, {view: 'list'});
  const head = h.page.locator('.pl-head');
  await h.page.locator('.pl-ping').first().waitFor();
  assert.deepEqual(await head.locator('[role=columnheader]').allInnerTexts(), ['服务器', 'CPU', '内存', '硬盘', '网速', '本期流量', '延迟', '丢包', '到期', '价格']);
  const row = name => h.page.locator('.pl-row').filter({hasText: name});
  assert.match(await row('Papa-HK.T1').locator('[data-ping=latency]').innerText(), /^3\d\.\d ms$/);
  assert.equal(await row('Papa-HK.T1').locator('[data-ping=loss]').innerText(), '0.7%');
  assert.equal(await row('Papa-HK.T1').locator('[data-ping=loss]').getAttribute('title'), '国外地址最近 10 分钟的丢包率。下面 30 格是最近 30 分钟，每格 1 分钟。');
  assert.equal(await row('Papa-HK.T1').locator('.pb-strip').count(), 2);
  assert.equal(await row('Hotel-HK').locator('[data-ping=latency]').innerText(), '—');
  await h.close();
});

test('monitoring off: no pairs, no columns, nothing read', async t => {
  const h = await setup(t, {view: 'list'});
  await h.page.locator('.pl-ping').first().waitFor();
  await h.page.getByRole('button', {name: '设置', exact: true}).click();
  const panel = h.page.getByRole('dialog', {name: '服务器设置'});
  await panel.getByRole('tab', {name: '延迟'}).click();
  await panel.getByText('开启延迟监测').click();
  await panel.getByRole('button', {name: '保存'}).click();
  await panel.waitFor({state: 'hidden'});
  await h.page.locator('.pl-ping').first().waitFor({state: 'detached'});
  assert.ok(!(await h.page.locator('.pl-head [role=columnheader]').allInnerTexts()).includes('延迟'));
  const asked = h.calls.filter(c => c.path === '/api/servers/latency').length;
  await h.page.getByRole('button', {name: '卡片视图'}).click();
  assert.equal(await h.page.locator('[data-cell=latency]').count(), 0);
  assert.equal(h.calls.filter(c => c.path === '/api/servers/latency').length, asked);
  await h.close();
});

test('a strip: a stop per run of minutes alike; a minute where nothing came back is a cut, not a gap', () => {
  const cells = [[20, 0], [22, 0], null, [200, 6], [210, 0.5], [null, 100]];
  assert.equal(stripBackground(cells, 'latency'), 'linear-gradient(90deg, rgb(var(--ob-success-rgb) / .55) 0.00% 33.33%, rgb(var(--ob-border-rgb) / .16) 33.33% 50.00%, '
    + 'rgb(var(--ob-danger-rgb) / .8) 50.00% 83.33%, var(--ob-danger) 83.33% 100.00%)');
  assert.equal(stripBackground(cells, 'loss'), 'linear-gradient(90deg, rgb(var(--ob-success-rgb) / .55) 0.00% 33.33%, rgb(var(--ob-border-rgb) / .16) 33.33% 50.00%, '
    + 'var(--ob-orange) 50.00% 66.67%, rgb(var(--ob-warning-rgb) / .6) 66.67% 83.33%, var(--ob-danger) 83.33% 100.00%)');
});

test('a strip on whole device pixels: every minute as wide, the gaps within a pixel, the width filled; too narrow, no gaps', () => {
  for (const [px, dpr] of [[143, 1], [173, 1], [179, 1.25], [288, 2], [432, 3], [215, 1.5]]) {
    const places = stripCells(px, dpr);
    const widths = new Set(places.map(([a, b]) => b - a)), gaps = places.slice(1).map(([a], i) => a - places[i][1]);
    assert.equal(places.length, 30);
    assert.equal(widths.size, 1, `${px} at ${dpr}: ${[...widths]}`);
    assert.ok(Math.min(...gaps) >= 1 && Math.max(...gaps) - Math.min(...gaps) <= 1, `${px} at ${dpr}: gaps ${gaps}`);
    assert.deepEqual([places[0][0], places.at(-1)[1]], [0, px]);
    assert.ok(places.flat().every(Number.isInteger));
  }
  // A table's narrow strip: minutes two pixels wide would leave no room for gaps, so they run on.
  const narrow = stripCells(78, 1);
  assert.ok(narrow.every(([, b], i) => i === 29 || narrow[i + 1][0] === b) && narrow.at(-1)[1] === 78);
  assert.equal(stripCells(20, 1), null);
  // A path per colour, a pixel in from the drawing's edge.
  const cells = [[20, 0], [22, 0], [200, 6]];
  assert.deepEqual(stripPaths(cells, 'loss', [[0, 3], [5, 8], [10, 13]]),
    [{fill: 'rgb(var(--ob-success-rgb) / .55)', d: 'M1 0h3v1h-3zM6 0h3v1h-3z'}, {fill: 'var(--ob-orange)', d: 'M11 0h3v1h-3z'}]);
});
