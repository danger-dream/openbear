// The latency and loss strips as Chromium paints them: thirty minutes, each as wide as the next with crisp
// edges, the gaps between them within a pixel of each other; at 100 %, 125 % and 200 % screen scaling, on a
// card on a desktop and a phone, and in the table.
import test from 'node:test';
import assert from 'node:assert/strict';
import {chromium, startServersBrowser, shot} from './testing/serversBrowser.mjs';

// Along the strip's middle row in device pixels: runs of minutes and of gaps (pixels the colour of what is
// behind it, read just above it), and whether each minute is one colour from edge to edge.
async function painted(page, strip) {
  await strip.scrollIntoViewIfNeeded();
  await page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))));
  const box = await strip.boundingBox();
  const png = await page.screenshot({clip: {x: box.x - 2, y: box.y - 2, width: box.width + 4, height: box.height + 2}});
  return page.evaluate(async b64 => {
    const img = new Image();
    img.src = `data:image/png;base64,${b64}`;
    await img.decode();
    const canvas = Object.assign(document.createElement('canvas'), {width: img.width, height: img.height});
    const g = canvas.getContext('2d');
    g.drawImage(img, 0, 0);
    const at = y => [...g.getImageData(0, y, img.width, 1).data];
    const back = at(0).slice(0, 3), row = at(Math.round(img.height * 0.65));
    const px = x => row.slice(x * 4, x * 4 + 3), same = (a, b, d = 3) => a.every((v, i) => Math.abs(v - b[i]) <= d);
    const runs = [];
    for (let x = 0; x < img.width; x++) {
      const gap = same(px(x), back), last = runs.at(-1);
      if (last && last.gap === gap) { last.len++; last.colours.push(px(x)); } else runs.push({gap, len: 1, colours: [px(x)]});
    }
    while (runs[0]?.gap) runs.shift();
    while (runs.at(-1)?.gap) runs.pop();
    return runs.map(r => ({gap: r.gap, len: r.len, flat: r.gap || r.colours.every(c => same(c, r.colours[0], 2))}));
  }, png.toString('base64'));
}

function evenly(runs, what) {
  const minutes = runs.filter(r => !r.gap), gaps = runs.filter(r => r.gap);
  assert.equal(minutes.length, 30, `${what}: thirty minutes, ${JSON.stringify(runs.map(r => (r.gap ? '-' : '') + r.len))}`);
  assert.equal(new Set(minutes.map(r => r.len)).size, 1, `${what}: every minute as wide: ${minutes.map(r => r.len)}`);
  assert.ok(minutes.every(r => r.flat), `${what}: no minute blurred at its edges`);
  const widths = gaps.map(r => r.len);
  assert.ok(Math.max(...widths) - Math.min(...widths) <= 1, `${what}: gaps within a pixel: ${widths}`);
}

test('latency strips on whole pixels', {skip: !chromium, timeout: 300000}, async t => {
  const {setup} = await startServersBrowser(t);
  for (const scale of [1, 1.25, 2]) {
    await t.test(`desktop card at ${scale * 100} %`, {timeout: 60000}, async t => {
      const h = await setup(t, {scale});
      const papa = h.page.locator('.pb-card').filter({hasText: 'Papa-HK.T1'});
      for (const kind of ['latency', 'loss']) {
        const strip = papa.locator(`[data-cell=${kind}] .pb-strip`);
        await strip.waitFor();
        assert.ok(await strip.evaluate(el => el.classList.contains('is-exact')), 'laid out on whole pixels once measured');
        evenly(await painted(h.page, strip), `${kind} at ${scale}`);
      }
      if (scale === 1) await shot(h.page, 'strips-desktop');
      await h.close();
    });
  }
  await t.test('phone card, dark', {timeout: 60000}, async t => {
    const h = await setup(t, {width: 390, height: 844, dark: true, touch: true, scale: 3});
    const strip = h.page.locator('.pb-card').filter({hasText: 'Papa-HK.T1'}).locator('[data-cell=loss] .pb-strip');
    await strip.waitFor();
    evenly(await painted(h.page, strip), 'phone');
    await h.close();
  });
  await t.test('table, and a window made narrower', {timeout: 60000}, async t => {
    const h = await setup(t, {view: 'list'});
    const strip = h.page.locator('.pl-row').filter({hasText: 'Papa-HK.T1'}).locator('[data-ping=loss] .pb-strip');
    await strip.waitFor();
    const runs = await painted(h.page, strip);
    // Too narrow for gaps beside minutes two pixels wide: one run of colours, crisp.
    if (runs.some(r => r.gap)) evenly(runs, 'table');
    else assert.ok(runs.length >= 1 && (await strip.boundingBox()).width < 89, `the table's strip: ${JSON.stringify(runs)}`);
    await h.close();
  });
  await t.test('a window made narrower lays the minutes out again', {timeout: 60000}, async t => {
    const h = await setup(t);
    const strip = h.page.locator('.pb-card').filter({hasText: 'Papa-HK.T1'}).locator('[data-cell=loss] .pb-strip');
    await strip.waitFor();
    const wide = (await strip.boundingBox()).width;
    await h.page.setViewportSize({width: 1100, height: 900});
    for (let i = 0; i < 40 && (await strip.boundingBox()).width === wide; i++) await h.page.waitForTimeout(25);
    assert.notEqual((await strip.boundingBox()).width, wide);
    evenly(await painted(h.page, strip), 'narrower');
    await h.close();
  });
});
